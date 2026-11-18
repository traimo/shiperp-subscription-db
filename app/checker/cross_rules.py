"""Rules across the Masterlist, the contract tabs and the maintenance register."""
from __future__ import annotations

from collections import defaultdict

from openpyxl.utils import get_column_letter

from ..importer import maintenance as m
from ..importer.mapper import names_differ
from ..importer.sheet import rules
from ..importer.sheet.model import Workbook
from ..models import MaintenanceBlock
from . import Issue
from .subscription_rules import cell, header


def _masterlist_rows(path) -> dict[int, int]:
    """customer number -> Excel row on the Masterlist tab."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:                                    # read-only workbooks keep the file open (locked on Windows) until closed
        if "Masterlist" not in wb.sheetnames:
            return {}
        out = {}
        for i, (num, *_rest) in enumerate(wb["Masterlist"].iter_rows(min_row=2, max_col=2, values_only=True),
                                          start=2):
            if isinstance(num, (int, float)) and not isinstance(num, bool):
                out.setdefault(int(num), i)
        return out
    finally:
        wb.close()


def check(wb: Workbook, ms: m.MaintenanceSheet | None) -> list[Issue]:
    out: list[Issue] = []
    ml = wb.masterlist
    ml_rows = _masterlist_rows(wb.path)
    # names used on the contract rows (latest row wins for the comparison, like the importer)
    rows_by_cust = defaultdict(list)
    for tab in wb.tabs.values():
        for c in tab.contracts:
            rows_by_cust[c.customer_number].append((tab, c))
    for qb, rows in sorted(rows_by_cust.items()):
        named = [(t, c) for t, c in rows if c.customer_name and c.section != "E"]
        if qb not in ml:
            t, c = rows[0]
            out.append(Issue("X02", "subscription", t.sheet_name, cell(t, c, "customer_number"), c.row,
                             f"Customer {qb} is on the contracts but not on the Masterlist tab.",
                             header(t, c, "customer_number"), qb, str(qb)))
        if named:
            t, c = max(named, key=lambda tc: tc[1].row)
            if qb in ml and names_differ(ml[qb], c.customer_name):
                out.append(Issue("X01", "subscription", "Masterlist", f"B{ml_rows[qb]}" if qb in ml_rows else None,
                                 ml_rows.get(qb), f"Masterlist says '{ml[qb]}', the contracts say '{c.customer_name}' "
                                 f"({t.label} row {c.row}).", "Customer Name", qb, ml[qb], c.customer_name))
            names = {c2.customer_name.strip().upper() for _, c2 in named}
            if len(names) > 1:
                out.append(Issue("X03", "subscription", t.sheet_name, cell(t, c, "customer_name"), c.row,
                                 f"Customer {qb} is named " + " / ".join(sorted(names)) + " on different rows.",
                                 header(t, c, "customer_name"), qb, c.customer_name))
    out.extend(check_hybrid(wb, ms))
    if ms is None:
        return out
    C = ms.cols
    j_num = C.first("customer") if C.first("customer") is not None else C.first("customer number")
    j_name = C.first("customer name")
    for r in ms.rows:
        num = m._num(r.values[j_num])
        if num is None:
            continue
        qb, name = int(num), str(r.values[j_name] or "").strip()
        ref = f"{get_column_letter(j_name + 1)}{r.row}"
        if qb in ml and name and names_differ(ml[qb], name):
            out.append(Issue("X04", "maintenance", m.SHEET, ref, r.row, f"Maintenance says '{name}', the Masterlist "
                             f"says '{ml[qb]}'.", "Customer Name", qb, name, ml[qb]))
        if r.block in (MaintenanceBlock.CONVERTED_ONPREM, MaintenanceBlock.CONVERTED_CLOUD) \
                and qb not in rows_by_cust:
            out.append(Issue("X05", "maintenance", m.SHEET, f"{get_column_letter(j_num + 1)}{r.row}", r.row,
                             f"Customer {qb} is in the converted block but has no row on the Subscription Summary.",
                             "Customer", qb, str(qb)))
    return out


def _paying_maintenance(ms: m.MaintenanceSheet) -> set[int]:
    """Customers with a fee > 0 in one of the register's two latest years."""
    latest = max(ms.cols.year.values(), default=0)
    j_num = ms.cols.first("customer") if ms.cols.first("customer") is not None else ms.cols.first("customer number")
    out: set[int] = set()
    if j_num is None:
        return out
    for r in ms.rows:
        num = m._num(r.values[j_num])
        if num is not None and any(y >= latest - 1 and (f or 0) > 0 for y, f in r.fees.items()):
            out.add(int(num))
    return out


def _converted(rows) -> bool:
    """A section B row or a 'Converted…' New/Existing label (what the importer turns into a conversion)."""
    return any(c.section == "B" or (rules.canonical(c.values.get("new_existing"), rules.NEW_EXISTING) or "")
               .startswith("Converted") for _, c in rows)


def check_hybrid(wb: Workbook, ms: m.MaintenanceSheet | None) -> list[Issue]:
    """Hybrid accounts (License Category "Hybrid"): an active subscription next to perpetual maintenance, both ARR
    (accounting, 2026-11-18). X06: a converted customer still paying maintenance is not marked Hybrid. X07: a Hybrid
    customer with a perpetual class pays no maintenance. X08: the customer's rows disagree on the category (one
    Hybrid, others On Premise / Cloud) - the category is per customer, so every row should say Hybrid."""
    out: list[Issue] = []
    rows_by_cust = defaultdict(list)
    for tab in wb.tabs.values():
        for c in tab.contracts:
            if c.section != "E":
                rows_by_cust[c.customer_number].append((tab, c))
    paying = _paying_maintenance(ms) if ms is not None else set()
    for qb, rows in sorted(rows_by_cust.items()):
        hybrid = [(t, c) for t, c in rows if rules.is_hybrid(c.values.get("license_category"))]
        t, c = max(hybrid or rows, key=lambda tc: tc[1].row)
        current = str(c.values.get("license_category") or "").strip()
        key = "license_category"
        if hybrid:
            for t2, c2 in rows:
                v = str(c2.values.get(key) or "").strip()
                if v and not rules.is_hybrid(v):
                    out.append(Issue("X08", "subscription", t2.sheet_name, cell(t2, c2, key), c2.row,
                                     f"Customer {qb} is Hybrid on {t.label} row {c.row} but '{v}' here - one "
                                     "License Category per customer.", header(t2, c2, key), qb, v, "Hybrid"))
            classes = [str(c2.values.get("hybrid_class") or "").strip() for _, c2 in hybrid]
            perpetual = any("perpetual" in (rules.canonical_hybrid_class(h) or h).lower() for h in classes if h)
            if ms is not None and perpetual and qb not in paying:
                out.append(Issue("X07", "subscription", t.sheet_name, cell(t, c, key), c.row,
                                 f"Customer {qb} is Hybrid ({', '.join(h for h in classes if h)}) but no maintenance "
                                 "is billed in the register's last two years.", header(t, c, key), qb, current,
                                 "On Premise / Cloud"))
        elif ms is not None and qb in paying and _converted(rows):
            out.append(Issue("X06", "subscription", t.sheet_name, cell(t, c, key), c.row,
                             f"Customer {qb} converted to a subscription and still pays maintenance: a Hybrid "
                             "account, both count as ARR.", header(t, c, key), qb, current or "(blank)", "Hybrid"))
    return out
