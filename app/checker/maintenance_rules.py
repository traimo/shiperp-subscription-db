"""Rules on the maintenance register tab (Maintenance-Adj Advance Billing), straight from the sheet."""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict

from openpyxl.utils import get_column_letter

from ..importer import maintenance as m
from . import Issue

WB = "maintenance"


def _cell(j: int | None, row: int) -> str | None:
    return f"{get_column_letter(j + 1)}{row}" if j is not None else None


def _label(y: int) -> str:
    return f"{y - 1}-{y}"


def check(ms: m.MaintenanceSheet) -> list[Issue]:
    out: list[Issue] = []
    C = ms.cols
    tab = m.SHEET
    j_num = C.first("customer") if C.first("customer") is not None else C.first("customer number")
    j_list, j_disc, j_net = C.first("original before discount"), C.first("discount"), C.first("original, net of discount")
    j_pct, j_basis, j_mrev = C.first("maintenance %"), C.first("basis for budget"), C.first("monthly revenue")
    j_anniv = C.first("billing anniversary")
    year_col = {y: j for j, y in C.year.items()}
    # current contract year = the one ending in this calendar year ("2025-2026" in 2026)
    keys = [y for y in ms.grand_total if ms.grand_total.get(y)]
    current = min(dt.date.today().year, max(keys)) if keys else None
    rev_cols = {}
    for name, cols in C.by_name.items():
        if name.endswith("total revenue"):
            yy = re.search(r"(20\d\d)", name)
            if yy:
                rev_cols[int(yy.group(1))] = cols[0]
    totals = defaultdict(float)

    def issue(rule, r, j, msg, current=None, expected=None, severity="", column=""):
        cust = m._num(r.values[j_num]) if j_num is not None else None
        return Issue(rule, WB, tab, _cell(j, r.row), r.row, msg, column, int(cust) if cust else None,
                     current, expected, severity)

    for r in ms.rows:
        v = r.values
        num = m._num(v[j_num]) if j_num is not None else None
        if num is None:
            continue
        for y, f in r.fees.items():
            totals[y] += f
        # M01 original net
        l, d, n = (m._num(v[j]) if j is not None else None for j in (j_list, j_disc, j_net))
        if l is not None and n is not None and abs(l + (d or 0) - n) > 1:
            out.append(issue("M01", r, j_net, f"{l:,.2f} + discount {d or 0:,.2f} = {l + (d or 0):,.2f}.",
                             f"{n:,.2f}", f"{l + (d or 0):,.2f}", column="Original, net of discount"))
        # M02 maintenance %
        pct = m._num(v[j_pct]) if j_pct is not None else None
        if pct and not 0.15 <= pct <= 0.31:
            out.append(issue("M02", r, j_pct, f"Maintenance % is {pct:.1%}.", f"{pct:.1%}", "15 %-31 %",
                             column="Maintenance %"))
        # M03 decrease / M04 fee after stop
        ys = sorted(r.fees)
        paid, stopped = False, None
        for a, b in zip(ys, ys[1:]):
            fa, fb = r.fees[a], r.fees[b]
            if fa > 0 and fb > 0 and fb < fa - 1:
                out.append(issue("M03", r, year_col.get(b), f"Fee went down from {fa:,.2f} ({_label(a)}) to "
                                 f"{fb:,.2f} ({_label(b)}).", f"{fb:,.2f}", f"≥ {fa:,.2f}", column=_label(b)))
        for y in ys:
            f = r.fees[y]
            if f > 0 and stopped is not None:
                out.append(issue("M04", r, year_col.get(y), f"Fee {f:,.2f} in {_label(y)} after an explicit 0 in "
                                 f"{_label(stopped)}.", f"{f:,.2f}", "blank", column=_label(y)))
                stopped = None
            if f > 0:
                paid = True
            elif f == 0 and paid and stopped is None:
                stopped = y
        # M05 / M06 basis and monthly revenue
        basis = m._num(v[j_basis]) if j_basis is not None else None
        if basis is not None and current is not None:
            fee = r.fees.get(current)
            if fee is not None and abs(basis - fee) > 1:
                out.append(issue("M05", r, j_basis, f"Current contract year {_label(current)} fee is {fee:,.2f}.",
                                 f"{basis:,.2f}", f"{fee:,.2f}", column="Basis for Budget"))
        mrev = m._num(v[j_mrev]) if j_mrev is not None else None
        if basis and mrev is not None and abs(mrev - basis / 12) > 0.5:
            out.append(issue("M06", r, j_mrev, f"{basis:,.2f} ÷ 12 = {basis / 12:,.2f}.", f"{mrev:,.2f}",
                             f"{basis / 12:,.2f}", column="Monthly Revenue"))
        # M07 Σ monthly = Total Revenue (the 12 columns before 'YYYY Total Revenue')
        for y, jt in rev_cols.items():
            tr = m._num(v[jt]) if jt < len(v) else None
            months = [m._num(v[j]) for j in range(jt - 12, jt)]
            if tr is None or not any(x is not None for x in months):
                continue
            s = sum(x or 0 for x in months)
            if abs(s - tr) > 1:
                out.append(issue("M07", r, jt, f"Σ monthly revenue {y} = {s:,.2f}.", f"{tr:,.2f}", f"{s:,.2f}",
                                 column=f"{y} Total Revenue"))
        # M09 n/a in fee cells (one issue per row)
        na = [y for j, y in C.year.items() if isinstance(v[j], str) and v[j].strip().lower() == "n/a"]
        if na:
            out.append(issue("M09", r, year_col[na[0]], "'n/a' in " + ", ".join(_label(y) for y in sorted(na)) + ".",
                             "n/a", "blank", column=_label(na[0])))
        # M10 billing anniversary
        if j_anniv is not None:
            raw = v[j_anniv]
            month, cal, conv, note = m.anniversary(raw)
            if raw not in (None, "") and month is None and not cal and "not applicable" not in str(raw).lower():
                out.append(issue("M10", r, j_anniv, f"Billing Anniversary '{raw}' is not a month.", raw,
                                 "month name / January - Calendar / not applicable", column="Billing Anniversary"))
    # M08 GRAND TOTAL
    if ms.grand_total_row:
        for y, g in ms.grand_total.items():
            s = totals.get(y, 0.0)
            if abs(s - g) > 1:
                out.append(Issue("M08", WB, tab, _cell(year_col.get(y), ms.grand_total_row), ms.grand_total_row,
                                 f"Customer rows add up to {s:,.2f} for {_label(y)}.", _label(y), None,
                                 f"{g:,.2f}", f"{s:,.2f}"))
    return out
