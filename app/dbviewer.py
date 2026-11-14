"""Read-only database browser helpers. Only SELECTs; table/column names come from the SQLAlchemy metadata whitelist,
never from raw request text."""
from __future__ import annotations

from decimal import Decimal

import enum
import html
import math
from dataclasses import dataclass, field

from sqlalchemy import Enum as SAEnum, String, Text, asc, desc as sdesc, func, or_, select
from sqlalchemy.orm import Session

from . import models  # noqa: F401  (registers all tables)
from .db import Base


def _model_for(table_name: str):
    for mapper in Base.registry.mappers:
        if mapper.local_table.name == table_name:
            return mapper.class_
    return None


@dataclass
class ColumnInfo:
    name: str
    type: str
    nullable: bool
    pk: bool
    fk: str | None              # "table.column"
    enum_values: list[str] = field(default_factory=list)


@dataclass
class TableInfo:
    name: str
    purpose: str
    count: int
    columns: list[ColumnInfo]
    checks: list[str]
    uniques: list[str]
    indexes: list[str]
    referenced_by: list[tuple[str, str]]   # (child table, child column)

    def column(self, name: str) -> ColumnInfo | None:
        return next((c for c in self.columns if c.name == name), None)


def table_names() -> list[str]:
    return [t.name for t in Base.metadata.sorted_tables]


PURPOSE = {
    "customer": "QuickBooks customer (80xxx number) - the key that ties subscriptions and maintenance together.",
    "account_exec": "Account executives (AE initials) used on terms.",
    "reseller": "Partners a deal came through, with commission rate.",
    "control_check_type": "The accounting close checks (checklist columns) and who owns each.",
    "control_check": "Result of one close check for one term.",
    "invoice": "Invoice issued for a term in a billing year.",
    "termination": "A subscription's cancellation (date and reason) - replaces the sheet's negative E rows.",
    "termination_term": "Contracts ended by a termination that does not cancel the whole subscription (none = all).",
    "audit_log": "Every change made through the app: who, when, what (before/after).",
    "alembic_version": "Schema migration version.",
}


def _purpose(name: str) -> str:
    cls = _model_for(name)
    doc = (cls.__doc__ or "").strip() if cls else ""
    if not doc or doc.startswith(cls.__name__ + "("):   # dataclass-style auto docstring
        return PURPOSE.get(name, "")
    return " ".join(doc.split())


def _pytype(col):
    try:
        return col.type.python_type
    except NotImplementedError:
        return None


def table_info(session: Session, name: str) -> TableInfo:
    t = Base.metadata.tables[name]
    cols = []
    for c in t.columns:
        fk = next(iter(c.foreign_keys), None)
        enum_vals = []
        if isinstance(c.type, SAEnum):
            enum_vals = list(c.type.enums)
        cols.append(ColumnInfo(c.name, "ENUM" if enum_vals else str(c.type), bool(c.nullable), c.primary_key,
                               f"{fk.column.table.name}.{fk.column.name}" if fk else None, enum_vals))
    checks, uniques = [], []
    for con in t.constraints:
        kind = type(con).__name__
        if kind == "CheckConstraint":
            text_ = str(con.sqltext)
            if not any(text_.startswith(c.name + " IN") for c in t.columns):   # skip enum checks (shown as ENUM)
                checks.append(text_)
        elif kind == "UniqueConstraint":
            uniques.append(", ".join(c.name for c in con.columns))
    uniques += [c.name for c in t.columns if c.unique]
    referenced_by = [(other.name, c.name) for other in Base.metadata.sorted_tables for c in other.columns
                     for fk in c.foreign_keys if fk.column.table.name == name]
    count = session.scalar(select(func.count()).select_from(t)) or 0
    return TableInfo(name, _purpose(name), count, cols, checks, uniques,
                     [", ".join(c.name for c in ix.columns) for ix in t.indexes], referenced_by)


def all_tables(session: Session) -> list[TableInfo]:
    return [table_info(session, n) for n in table_names()]


@dataclass
class RowPage:
    rows: list[dict]
    total: int
    page: int
    pages: int
    page_size: int


def rows(session: Session, name: str, q: str = "", sort: str = "", descending: bool = False, page: int = 1,
         page_size: int = 50, where: dict[str, str] | None = None) -> RowPage:
    t = Base.metadata.tables[name]
    stmt = select(t)
    count_stmt = select(func.count()).select_from(t)
    conds = []
    for col_name, raw in (where or {}).items():
        if col_name not in t.c:
            raise KeyError(col_name)
        col = t.c[col_name]
        try:
            value = _pytype(col)(raw) if _pytype(col) in (int, float, Decimal) else raw
        except (ValueError, ArithmeticError):     # decimal.InvalidOperation is an ArithmeticError
            value = raw
        conds.append(col == value)
    if q:
        texty = [c for c in t.columns if isinstance(c.type, (String, Text, SAEnum))]
        like = f"%{q.lower()}%"
        ors = [func.lower(c).like(like) for c in texty]
        ints = [c for c in t.columns if _pytype(c) is int] if q.strip().isdigit() else []
        ors += [c == int(q) for c in ints]
        if ors:
            conds.append(or_(*ors))
    for c in conds:
        stmt, count_stmt = stmt.where(c), count_stmt.where(c)
    if sort:
        if sort not in t.c:
            raise KeyError(sort)
        stmt = stmt.order_by(sdesc(t.c[sort]) if descending else asc(t.c[sort]))
    else:
        stmt = stmt.order_by(*t.primary_key.columns)
    total = session.scalar(count_stmt) or 0
    pages = max(1, math.ceil(total / page_size))
    page = min(max(1, page), pages)
    data = [dict(r._mapping) for r in session.execute(stmt.limit(page_size).offset((page - 1) * page_size))]
    for r in data:
        for k, v in r.items():
            if isinstance(v, enum.Enum):
                r[k] = v.value
    return RowPage(data, total, page, pages, page_size)


# --------------------------------------------------------------------------------------------- diagram

LAYOUT = {                       # column, row in a grid
    "customer": (1, 0), "subscription": (1, 1), "term": (1, 2), "term_supersession": (0, 2),
    "termination": (0, 1), "invoice": (0, 3), "control_check": (1, 3), "control_check_type": (1, 4),
    "account_exec": (2, 3), "reseller": (2, 4), "maintenance_contract": (2, 0), "maintenance_fee": (3, 0),
    "non_recurring_fee": (0, 0), "audit_log": (3, 2), "termination_term": (0, 4),
}
BOX_W, BOX_H, GAP_X, GAP_Y = 190, 46, 70, 52


def diagram_svg(infos: list[TableInfo]) -> str:
    pos = {}
    spare = 0
    for ti in infos:
        if ti.name in LAYOUT:
            gx, gy = LAYOUT[ti.name]
        else:
            gx, gy = 3, 3 + spare
            spare += 1
        pos[ti.name] = (20 + gx * (BOX_W + GAP_X), 20 + gy * (BOX_H + GAP_Y))
    width = max(x for x, _ in pos.values()) + BOX_W + 20
    height = max(y for _, y in pos.values()) + BOX_H + 20
    lines, boxes = [], []
    for ti in infos:
        for c in ti.columns:
            if not c.fk:
                continue
            target = c.fk.split(".")[0]
            if target not in pos:
                continue
            x1, y1 = pos[ti.name]
            x2, y2 = pos[target]
            ax, ay = x1 + BOX_W / 2, y1 + BOX_H / 2
            bx, by = x2 + BOX_W / 2, y2 + BOX_H / 2
            lines.append(f'<line x1="{ax}" y1="{ay}" x2="{bx}" y2="{by}" class="dbl">'
                         f'<title>{ti.name}.{c.name} → {c.fk}</title></line>')
    for ti in infos:
        x, y = pos[ti.name]
        boxes.append(
            f'<a href="/db/{ti.name}"><g class="dbb"><rect x="{x}" y="{y}" width="{BOX_W}" height="{BOX_H}" rx="6"/>'
            f'<text x="{x + 10}" y="{y + 20}" class="dbt">{html.escape(ti.name)}</text>'
            f'<text x="{x + 10}" y="{y + 37}" class="dbn">{ti.count:,} rows · {len(ti.columns)} columns</text>'
            f'<title>{html.escape(ti.purpose)}</title></g></a>')
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px" role="img" '
            f'aria-label="Table relationship diagram">{"".join(lines)}{"".join(boxes)}</svg>')
