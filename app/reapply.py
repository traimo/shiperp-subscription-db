"""Reapply app changes after a re-import ("Refresh data" with "Reapply").

A re-import rebuilds every table from the workbooks, so changes made in the app are gone. This module replays them,
best effort, through the normal service functions (so each gets its own audit row and can be undone):

* the change list comes from the pre-refresh database backup (audit rows not undone, oldest first);
* the full inputs (platform, AE, reseller, ...) are read from the backup's rows, because the audit detail is only a
  partial snapshot;
* ids differ after an import, so rows are matched by natural keys: customer number, term (customer, sheet ref) or -
  for terms created in the app - (customer, platform, kind, dates, TCV), maintenance line (customer, sheet ref).

What cannot be matched, is already in the sheet, or is not supported is reported, never forced.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import maintenance_services as ms
from . import services
from .models import (AuditLog, ChangeRecord, Customer, MaintenanceContract, Subscription, Term, TermKind,
                     TerminationReason)
from .money import cents
from .services import ServiceError, TermInput

PREFIX = "Reapplied: "
# (action, entity) pairs that are replayed; documents survive an import on their own (re-linked by sheet ref)
REPLAYABLE = {("create", "customer"), ("create", "term"), ("renew", "term"), ("amend", "term"),
              ("terminate", "subscription"), ("update", "term"), ("convert", "maintenance_contract")}
KEPT = {"document"}


@dataclass
class Item:
    audit_id: int
    at: str
    actor: str
    action: str
    entity: str
    customer_qb: int | None
    summary: str
    status: str = ""             # reapplied | skipped | manual | kept | failed
    message: str = ""
    new_audit_id: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def classify(a: AuditLog) -> str:
    """'can' (will be replayed), 'kept' (survives the import anyway) or 'manual' (has to be redone by hand)."""
    if a.entity in KEPT:
        return "kept"
    return "can" if (a.action, a.entity) in REPLAYABLE else "manual"


def pending_changes(s: Session) -> list[AuditLog]:
    """App changes still in effect (not undone, not undo rows), oldest first."""
    return list(s.scalars(select(AuditLog).where(AuditLog.action != "undo", AuditLog.undone_at.is_(None))
                          .order_by(AuditLog.id)))


# ------------------------------------------------------------------------------------------------ matching

def _qb(t: Term) -> int:
    return t.subscription.customer.qb_number


def _terms_of(ns: Session, qb: int):
    return list(ns.scalars(select(Term).join(Subscription, Term.subscription_id == Subscription.id)
                           .join(Customer, Subscription.customer_id == Customer.id).where(Customer.qb_number == qb)))


def map_term(old: Term, ns: Session) -> Term | None:
    """The new database's term for a term of the backup database."""
    terms = _terms_of(ns, _qb(old))
    if old.source_ref:
        hits = [t for t in terms if t.source_ref == old.source_ref]
    else:
        hits = [t for t in terms if not t.source_ref and t.subscription.platform == old.subscription.platform
                and t.kind == old.kind and t.start_date == old.start_date and t.end_date == old.end_date
                and cents(t.tcv) == cents(old.tcv)]
    return hits[0] if len(hits) == 1 else None


def map_subscription(old: Subscription, ns: Session) -> Subscription | None:
    for t in old.terms:                                     # via any of its terms
        n = map_term(t, ns)
        if n is not None:
            return n.subscription
    subs = list(ns.scalars(select(Subscription).join(Customer, Subscription.customer_id == Customer.id)
                           .where(Customer.qb_number == old.customer.qb_number,
                                  Subscription.platform == old.platform)))
    return subs[0] if len(subs) == 1 else None


def same_period_exists(ns: Session, old: Term) -> bool:
    """A term with the same platform, dates and TCV is already there (typed into the workbook, or reapplied).
    TCV matters: one amendment can replace a base term and its add-on with two terms of the same period."""
    return any(t.subscription.platform == old.subscription.platform and t.start_date == old.start_date
               and t.end_date == old.end_date and cents(t.tcv) == cents(old.tcv) for t in _terms_of(ns, _qb(old)))


def term_input(t: Term) -> TermInput:
    return TermInput(start_date=t.start_date, end_date=t.end_date, tcv=t.tcv, signed_date=t.signed_date,
                     term_months=t.term_months, payment_terms=t.payment_terms,
                     ae=t.ae.initials if t.ae else None, reseller=t.reseller.name if t.reseller else None,
                     golive_date=t.golive_date, golive_note=t.golive_note,
                     transactional_volume=t.transactional_volume, renewal_note=t.renewal_note,
                     next_action=t.next_action, notes=t.notes)


class Skip(Exception):
    """Not reapplied on purpose (already there, nothing to do)."""


class Manual(Exception):
    """Cannot be reapplied automatically - redo by hand."""


def _detail(a: AuditLog) -> dict:
    try:
        return json.loads(a.detail) if a.detail else {}
    except ValueError:
        return {}


def _old(os_: Session, model, pk, what: str):
    row = os_.get(model, pk) if pk is not None else None
    if row is None:
        raise Manual(f"{what} is no longer in the backup database.")
    return row


# ------------------------------------------------------------------------------------------------ handlers
# Each handler resolves the new ids (in a short session) and returns (op, args, kwargs) for services.run.

def _create_customer(a, os_, ns, today):
    c = os_.get(Customer, a.entity_id)
    qb, name = (c.qb_number, c.name) if c else (a.customer_qb, a.summary.split(str(a.customer_qb), 1)[-1].strip())
    if ns.scalar(select(Customer).where(Customer.qb_number == qb)):
        raise Skip(f"Customer {qb} already exists (now in the workbook).")
    return services.create_customer, (qb, name), {}


def _create_term(a, os_, ns, today):
    old = _old(os_, Term, a.entity_id, f"Term {a.entity_id}")
    if same_period_exists(ns, old):
        raise Skip("A term with the same platform, dates and TCV already exists (now in the workbook).")
    if ns.scalar(select(Customer).where(Customer.qb_number == _qb(old))) is None:
        raise Manual(f"Customer {_qb(old)} is not in the new import.")
    sub = old.subscription
    if old.kind in (TermKind.INITIAL, TermKind.CONVERSION):
        return services.new_subscription, (_qb(old), sub.platform, sub.opened_via, term_input(old)), \
            dict(license_category=sub.license_category, hybrid_class=sub.hybrid_class, today=today)
    new_sub = map_subscription(sub, ns)
    if new_sub is None:
        raise Manual(f"Could not find the {sub.platform.value} subscription of {_qb(old)} for this add-on.")
    return services.add_on, (new_sub.id, term_input(old)), dict(today=today)


def _replacing(a, os_, ns, old_ids, verb):
    old_new = _old(os_, Term, a.entity_id, f"Term {a.entity_id}")
    targets = []
    for i in old_ids:
        o = _old(os_, Term, i, f"Replaced term {i}")
        n = map_term(o, ns)
        if n is None:
            raise Manual(f"Could not find {o.source_ref or f'term {i}'} of {_qb(o)} in the new import.")
        if n.superseded_by:
            raise Skip(f"{n.source_ref or f'Term {n.id}'} is already {verb} in the new import (now in the workbook).")
        targets.append(n)
    if same_period_exists(ns, old_new):
        raise Skip("A term with the same platform, dates and TCV already exists (now in the workbook).")
    return old_new, targets


def _renew(a, os_, ns, today):
    old_new, targets = _replacing(a, os_, ns, _detail(a).get("supersedes") or [], "renewed/amended")
    if not targets:
        raise Manual("The renewal does not name the terms it replaced.")
    return services.renew, (targets[0].id, term_input(old_new)), \
        dict(rollup_term_ids=[t.id for t in targets[1:]], today=today)


def _amend(a, os_, ns, today):
    old_id = _detail(a).get("old")
    old_new, targets = _replacing(a, os_, ns, [old_id] if old_id else [], "renewed/amended")
    if not targets:
        raise Manual("The amendment does not name the term it replaced.")
    return services.amend, (targets[0].id, term_input(old_new)), dict(today=today)


def _terminate(a, os_, ns, today):
    d = _detail(a)
    old = _old(os_, Subscription, a.entity_id, f"Subscription {a.entity_id}")
    new = map_subscription(old, ns)
    if new is None:
        raise Manual(f"Could not find the {old.platform.value} subscription of {old.customer.qb_number}.")
    on = dt.date.fromisoformat(str(d.get("cancelled_on"))[:10])
    term_ids = None
    if d.get("terms"):                                  # only some contracts were ended
        term_ids = []
        for i in d["terms"]:
            o = _old(os_, Term, i, f"Terminated term {i}")
            n = map_term(o, ns)
            if n is None:
                raise Manual(f"Could not find {o.source_ref or f'term {i}'} of {_qb(o)} in the new import.")
            term_ids.append(n.id)
    if any(x.cancelled_on == on and (term_ids is None) == (not x.scope)
           and (term_ids is None or set(term_ids) <= x.term_ids) for x in new.terminations):
        raise Skip(f"Already cancelled {on:%m/%d/%Y} in the new import (now in the workbook).")
    return services.terminate, (new.id, on, TerminationReason(d.get("reason"))),         dict(note=d.get("note"), today=today, term_ids=term_ids)


def _update_term(a, os_, ns, today):
    old = _old(os_, Term, a.entity_id, f"Term {a.entity_id}")
    new = map_term(old, ns)
    if new is None:
        raise Manual(f"Could not find {old.source_ref or f'term {old.id}'} of {_qb(old)} in the new import.")
    d = _detail(a)
    before, after = d.get("before") or {}, d.get("after") or {}
    cols = {k for k in after if before.get(k) != after.get(k)}
    for cr in os_.scalars(select(ChangeRecord).where(ChangeRecord.audit_id == a.id,
                                                     ChangeRecord.table_name == "term")):
        b, f = json.loads(cr.before or "{}"), json.loads(cr.after or "{}")
        cols |= {k for k in f if b.get(k) != f.get(k)}
    changes = {k: getattr(old, k) for k in services.EDITABLE_TERM_FIELDS if k in cols}   # final values (backup)
    ae = (old.ae.initials if old.ae else "") if ("ae_id" in cols or "AE ->" in a.summary) else None
    reseller = (old.reseller.name if old.reseller else "") if ("reseller_id" in cols or "Reseller ->" in a.summary) \
        else None
    if not changes and ae is None and reseller is None:
        raise Manual("Could not tell which fields this edit changed.")
    return services.update_term, (new.id, changes), dict(ae=ae, reseller=reseller)


def _convert(a, os_, ns, today):
    old = _old(os_, MaintenanceContract, a.entity_id, f"Maintenance line {a.entity_id}")
    qb = old.customer.qb_number
    hits = list(ns.scalars(select(MaintenanceContract).join(Customer, MaintenanceContract.customer_id == Customer.id)
                           .where(Customer.qb_number == qb, MaintenanceContract.source_ref == old.source_ref))) \
        if old.source_ref else []
    if len(hits) != 1:
        raise Manual(f"Could not find maintenance line {old.source_ref or old.id} of {qb} in the new import.")
    if hits[0].ended_on:
        raise Skip(f"Maintenance line {old.source_ref} already ended in the new import (now in the workbook).")
    if not old.ended_on:
        raise Manual("The backup has no conversion date for this line.")
    sub_id = None
    if old.converted_subscription_id:
        new_sub = map_subscription(_old(os_, Subscription, old.converted_subscription_id, "Converted subscription"),
                                   ns)
        if new_sub is None:
            raise Manual("Could not find the subscription this line was converted to - reapply the conversion "
                         "subscription first.")
        sub_id = new_sub.id
    return ms.convert, (hits[0].id, old.ended_on), dict(subscription_id=sub_id)


HANDLERS = {("create", "customer"): _create_customer, ("create", "term"): _create_term,
            ("renew", "term"): _renew, ("amend", "term"): _amend, ("terminate", "subscription"): _terminate,
            ("update", "term"): _update_term, ("convert", "maintenance_contract"): _convert}


# ------------------------------------------------------------------------------------------------ runner

def reapply(old_factory, new_factory, actor: str, today: dt.date) -> list[Item]:
    """Replay the backup's pending changes into the freshly imported database. One transaction per change."""
    items: list[Item] = []
    with old_factory() as os_:
        for a in pending_changes(os_):
            it = Item(a.id, a.at.strftime("%Y-%m-%d %H:%M"), a.actor, a.action, a.entity, a.customer_qb, a.summary)
            items.append(it)
            kind = classify(a)
            if kind == "kept":
                it.status, it.message = "kept", "Kept by the import (contract links are re-attached by sheet ref)."
                continue
            if kind == "manual":
                it.status, it.message = "manual", "Not reapplied automatically - redo it by hand."
                continue
            try:
                with new_factory() as ns:
                    op, args, kwargs = HANDLERS[(a.action, a.entity)](a, os_, ns, today)
                out = services.run(new_factory, op, actor, *args, commit=True, **kwargs)
            except Skip as e:
                it.status, it.message = "skipped", str(e)
                continue
            except Manual as e:
                it.status, it.message = "manual", str(e)
                continue
            except ServiceError as e:
                it.status, it.message = "failed", str(e)
                continue
            except Exception as e:                          # never stop the loop - report it
                it.status, it.message = "failed", f"{type(e).__name__}: {e}"
                continue
            if out.audit_id is None:
                it.status, it.message = "skipped", "Nothing to change (already as in the new import)."
                continue
            with new_factory() as ns:
                row = ns.get(AuditLog, out.audit_id)
                row.summary = (PREFIX + row.summary)[:400]
                d = json.loads(row.detail) if row.detail else {}
                d["reapplied_from"] = {"audit_id": a.id, "at": a.at.isoformat(timespec="seconds"), "actor": a.actor}
                row.detail = json.dumps(d, default=str)
                ns.commit()
            it.status, it.new_audit_id = "reapplied", out.audit_id
            it.message = "; ".join(out.lines)[:300]
    return items
