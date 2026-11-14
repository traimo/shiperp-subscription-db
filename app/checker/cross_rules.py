"""Rules across the Masterlist, the contract tabs and the maintenance register."""
from __future__ import annotations

from collections import defaultdict

from openpyxl.utils import get_column_letter

from ..importer import maintenance as m
from ..importer.mapper import names_differ
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
