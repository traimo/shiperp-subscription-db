"""Undo for changes made in the app.

Capture: while services.run(..., commit=True) executes an operation, ORM mapper events record every row it inserts,
updates or deletes, reading the row from the database before and after (so relationship / cascade changes are seen
exactly as stored). The net effect per row is saved as ChangeRecord rows attached to the operation's audit row.

Undo: reverse the records in reverse order (delete inserted rows, restore updated columns, re-insert deleted rows with
their original id) - only when every row is still as the change left it. Otherwise a later change touched the same rows
and must be undone first (the message names it). The undo is itself audited and marks the original row as undone.
"""
from __future__ import annotations

import datetime as dt
import enum
import json
from decimal import Decimal

from sqlalchemy import Date, DateTime, Numeric, event, select
from sqlalchemy.orm import Mapper, Session

from .db import Base
from .models import AuditLog, ChangeRecord

SKIP_TABLES = {"audit_log", "change_record"}
LABELS = {"term": "contract term", "term_supersession": "renewal / merge link", "termination": "termination",
          "subscription": "subscription", "customer": "customer", "invoice": "invoice", "control_check": "checklist item",
          "maintenance_contract": "maintenance line", "maintenance_fee": "maintenance fee",
          "license_purchase": "licence purchase", "maintenance_invoice": "maintenance invoice",
          "contract_document": "contract link", "account_exec": "AE", "reseller": "reseller"}
KEY = "undo_capture"                  # session.info[KEY] = list of raw events while capturing


class UndoError(Exception):
    pass


# ------------------------------------------------------------------------------------------------ values

def _dump(v):
    if isinstance(v, enum.Enum):
        return v.name
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


def _load(col, v):
    if v is None:
        return None
    t = col.type
    if isinstance(t, DateTime):
        return dt.datetime.fromisoformat(v)
    if isinstance(t, Date):
        return dt.date.fromisoformat(v)
    if isinstance(t, Numeric):
        return Decimal(v) if t.asdecimal else float(v)
    return v


def _norm(row: dict | None) -> dict | None:
    """Comparable form: Decimals compared as numbers rounded to cents (values read back from JSON may carry more places)."""
    if row is None:
        return None
    out = {}
    for k, v in row.items():
        if isinstance(v, str):
            try:
                if "." in v and v.replace(".", "", 1).lstrip("-").isdigit():
                    v = str(Decimal(v).quantize(Decimal("0.01")))
            except Exception:
                pass
        out[k] = v
    return out


def _read(conn, table, pk: int) -> dict | None:
    row = conn.execute(select(table).where(table.c.id == pk)).mappings().first()
    return {k: _dump(v) for k, v in row.items()} if row is not None else None


# ------------------------------------------------------------------------------------------------ capture

def _session_of(target) -> Session | None:
    from sqlalchemy.orm import object_session
    return object_session(target)


def _log(target, conn, op: str, when: str):
    s = _session_of(target)
    if s is None or KEY not in s.info:
        return
    table = target.__table__
    if table.name in SKIP_TABLES:
        return
    s.info[KEY].append((table.name, target.id, op, when, _read(conn, table, target.id)))


@event.listens_for(Base, "before_update", propagate=True)
def _before_update(mapper: Mapper, conn, target):
    _log(target, conn, "update", "before")


@event.listens_for(Base, "after_update", propagate=True)
def _after_update(mapper: Mapper, conn, target):
    _log(target, conn, "update", "after")


@event.listens_for(Base, "after_insert", propagate=True)
def _after_insert(mapper: Mapper, conn, target):
    _log(target, conn, "insert", "after")


@event.listens_for(Base, "before_delete", propagate=True)
def _before_delete(mapper: Mapper, conn, target):
    _log(target, conn, "delete", "before")


def start(s: Session) -> None:
    s.info[KEY] = []


def finish(s: Session, audit: AuditLog | None) -> int:
    """Turn the captured events into net ChangeRecords on `audit`. Returns the number of rows recorded."""
    raw = s.info.pop(KEY, None)
    if not raw or audit is None:
        return 0
    net: dict[tuple[str, int], dict] = {}
    for pos, (table, pk, op, when, values) in enumerate(raw):
        r = net.get((table, pk))
        if r is None:
            r = net[(table, pk)] = {"first": op, "before": None, "after": None}
            if op in ("update", "delete") and when == "before":
                r["before"] = values                            # the row as it was before this change
        r["seq"] = pos                                          # order of the row's last write (undo runs reverse)
        if op == "delete":
            r["after"] = None
            r["last"] = "delete"
        elif when == "after":
            r["after"] = values
            r["last"] = op
    n = 0
    for (table, pk), r in sorted(net.items(), key=lambda kv: kv[1]["seq"]):
        before, after = r["before"], r["after"]
        if r["first"] == "insert":
            before = None
        if before is None and after is None:
            continue                                            # inserted and deleted again
        if before is not None and after is not None and _norm(before) == _norm(after):
            continue                                            # touched but unchanged
        op = "insert" if before is None else "delete" if after is None else "update"
        audit.changes.append(ChangeRecord(seq=n, table_name=table, row_pk=pk, op=op,
                                          before=json.dumps(before) if before is not None else None,
                                          after=json.dumps(after) if after is not None else None))
        n += 1
    s.flush()
    return n


# ------------------------------------------------------------------------------------------------ undo

def _table(name: str):
    return Base.metadata.tables[name]


def check(s: Session, audit_id: int) -> AuditLog:
    """The audit row, if it can be undone now; else UndoError with the reason."""
    a = s.get(AuditLog, audit_id)
    if a is None:
        raise UndoError("Change not found.")
    if a.action == "undo":
        raise UndoError("This entry is itself an undo; make the change again instead.")
    if a.undone_at:
        raise UndoError(f"Already undone on {a.undone_at:%m/%d/%Y %H:%M} by {a.undone_by}.")
    if not a.changes:
        raise UndoError("This change cannot be undone: it was made before undo was available (or changed nothing).")
    conn = s.connection()
    for r in a.changes:
        current = _read(conn, _table(r.table_name), r.row_pk)
        expected = json.loads(r.after) if r.after else None
        if _norm(current) != _norm(expected):
            raise UndoError("A later change touched the same records - undo it first: " + _blocker(s, a, r))
    own = {(r.table_name, r.row_pk) for r in a.changes}            # rows the undo itself puts back
    for r in a.changes:                              # rows this change created must not be used by later rows
        if r.op != "insert":
            continue
        for child in Base.metadata.tables.values():
            if child.name in SKIP_TABLES:
                continue
            for fk in child.foreign_keys:
                if fk.column.table.name != r.table_name:
                    continue
                ids = [i for i in conn.execute(select(child.c.id).where(fk.parent == r.row_pk)).scalars()
                       if (child.name, i) not in own]
                if ids:
                    ref = ChangeRecord(table_name=child.name, row_pk=ids[0])
                    raise UndoError(f"A later change uses the {LABELS.get(r.table_name, r.table_name)} this change "
                                    "created - undo it first: " + _blocker(s, a, ref))
    return a


def _blocker(s: Session, a: AuditLog, r: ChangeRecord) -> str:
    later = s.scalars(select(AuditLog).join(ChangeRecord).where(
        AuditLog.id > a.id, AuditLog.undone_at.is_(None), ChangeRecord.table_name == r.table_name,
        ChangeRecord.row_pk == r.row_pk).order_by(AuditLog.id.desc())).first()
    if later is None:
        return f"{r.table_name} #{r.row_pk} was changed outside the app (e.g. a re-import)."
    return f"{later.at:%m/%d/%Y %H:%M} {later.actor}: {later.summary[:160]}"


def describe(a: AuditLog) -> list[str]:
    """Readable list of what undoing `a` will do."""
    out = []
    for r in a.changes:
        name = LABELS.get(r.table_name, r.table_name.replace("_", " "))
        if r.op == "insert":
            out.append(f"Remove {name} #{r.row_pk} (added by this change)")
        elif r.op == "delete":
            out.append(f"Restore {name} #{r.row_pk} (removed by this change)")
        else:
            b, af = json.loads(r.before), json.loads(r.after)
            diffs = [f"{k}: {af[k]!r} → {b[k]!r}" for k in b if _norm({k: b[k]}) != _norm({k: af.get(k)})]
            out.append(f"Put back {name} #{r.row_pk}: " + ", ".join(diffs))
    return out


def undo_change(s: Session, actor: str, audit_id: int):
    """Reverse the change recorded on audit row `audit_id` (see check)."""
    from .services import Outcome, _audit
    a = check(s, audit_id)
    lines = [f"Undid: {a.summary}"] + describe(a)
    conn = s.connection()
    for r in sorted(a.changes, key=lambda x: x.seq, reverse=True):
        table = _table(r.table_name)
        if r.op == "insert":
            conn.execute(table.delete().where(table.c.id == r.row_pk))
        else:
            values = {k: _load(table.c[k], v) for k, v in json.loads(r.before).items() if k in table.c}
            if r.op == "delete":
                conn.execute(table.insert().values(**values))
            else:
                conn.execute(table.update().where(table.c.id == r.row_pk).values(**values))
    a.undone_at, a.undone_by = dt.datetime.now(), actor
    for part in s.scalars(select(AuditLog).where(AuditLog.detail.contains(f'"part_of": {a.id}'))):
        if json.loads(part.detail or "{}").get("part_of") == a.id:       # rows written by the same change
            part.undone_at, part.undone_by = a.undone_at, actor
    row = _audit(s, actor, "undo", "audit", a.id, a.customer_qb, lines[0][:400], {"undo_of": a.id})
    row.undo_of_id = a.id
    s.flush()
    out = Outcome(f"Undo {a.action} {a.entity}", a.customer_qb, lines, entity_id=a.id)
    s.expire_all()                                   # ORM objects must not keep pre-undo state
    return out
