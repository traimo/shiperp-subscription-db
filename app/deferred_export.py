"""Excel download of the Deferred Billing page: the same rows, filters and month columns, as numbers Excel can work
with (customer rows, then each customer's contracts grouped under it)."""
from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .deferred import CustomerRow

MONEY = '#,##0.00;(#,##0.00);0.00'
INVOICED = PatternFill("solid", fgColor="E8F0FC")
HEAD = PatternFill("solid", fgColor="F2F4F7")
CUST = PatternFill("solid", fgColor="FAFBFC")
TOP = Border(top=Side(style="thin", color="98A2B3"))
FIXED = [("Customer #", 11), ("Customer", 38), ("Contract", 15), ("Start", 11), ("End", 11), ("Platform", 10),
         ("TCV", 14)]


def _text(v):
    """Plain text, never a formula (a name starting with '=' stays text)."""
    return None if v is None else str(v)


def _num(v: Decimal | None):
    return None if v is None else float(v)


def build(months: list[dt.date], rows: list[CustomerRow], totals: list[Decimal], inv_totals: list[Decimal],
          current_index: int | None, today: dt.date, filters: str = "") -> bytes:
    """Each month has two columns: Balance (at month-end) and Invoiced (in that month)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Deferred Billing"
    ws.sheet_properties.outlinePr.summaryBelow = False          # the +/- sits on the customer row above its contracts

    ws["A1"] = "Deferred Billing"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = (f"Balance = TCV less the amounts invoiced up to that month-end; Invoiced = amount invoiced in that "
                f"month. Exported {today:%m/%d/%Y}{' - ' + filters if filters else ''}.")
    ws["A2"].font = Font(italic=True, color="667085")
    hr, sr = 4, 5                                               # month row, Balance / Invoiced row
    cur_label = f"Current balance {months[current_index]:%m/%Y}" if current_index is not None else "Current balance"
    heads = [h for h, _ in FIXED] + [cur_label]
    first = len(heads) + 1                                      # first month column
    last = first + 2 * len(months) - 1
    for j, h in enumerate(heads, start=1):
        ws.cell(hr, j, h)
        ws.merge_cells(start_row=hr, start_column=j, end_row=sr, end_column=j)
    for n, m in enumerate(months):
        col = first + 2 * n
        ws.cell(hr, col, m).number_format = "mm/yyyy"
        ws.merge_cells(start_row=hr, start_column=col, end_row=hr, end_column=col + 1)
        ws.cell(sr, col, "Balance")
        ws.cell(sr, col + 1, "Invoiced")
    for x in (hr, sr):
        for c in ws[x]:
            c.font, c.fill = Font(bold=True), HEAD
            c.alignment = Alignment(horizontal="left" if c.column < len(FIXED) else "center", vertical="center",
                                    wrap_text=True)
    for j, (_, w) in enumerate(FIXED, start=1):
        ws.column_dimensions[get_column_letter(j)].width = w
    for j in range(len(FIXED) + 1, last + 1):
        ws.column_dimensions[get_column_letter(j)].width = 13

    r = sr
    for row in rows:
        r += 1
        c = row.customer
        vals = [c.qb_number, _text(c.name), None, None, None, ", ".join(row.platforms), _num(row.tcv),
                _num(row.current)]
        _write(ws, r, vals, row.balances, row.invoiced, first)
        for cell in ws[r][:last]:
            cell.font = Font(bold=True)
            if cell.column < first:
                cell.fill = CUST
        for tr in row.terms:
            r += 1
            t = tr.term
            vals = [c.qb_number, _text(c.name), _text(t.source_ref or f"Term {t.id}"), t.start_date, t.end_date,
                    t.subscription.platform.value, _num(t.tcv), _num(tr.current)]
            _write(ws, r, vals, tr.balances, tr.invoiced, first)
            ws.row_dimensions[r].outline_level = 1
            ws.cell(r, 2).font = Font(color="667085")
    r += 1
    total = ["", "Total", None, None, None, None, _num(sum((x.tcv for x in rows), Decimal(0))),
             _num(totals[current_index]) if current_index is not None and totals else None]
    _write(ws, r, total, totals, inv_totals, first, shade=False)
    for cell in ws[r][:last]:
        cell.font, cell.border = Font(bold=True), TOP

    ws.freeze_panes = ws.cell(sr + 1, 3)                        # customer number + name stay visible
    ws.auto_filter.ref = f"A{sr}:{get_column_letter(last)}{r - 1}"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _write(ws, r: int, fixed: list, balances: list, invoiced: list, first: int, shade: bool = True) -> None:
    for j, v in enumerate(fixed, start=1):
        cell = ws.cell(r, j, v)
        if isinstance(v, str):
            cell.data_type = "s"                                # text, even when it starts with '='
        if isinstance(v, dt.date):
            cell.number_format = "mm/dd/yyyy"
        if j >= 7:
            cell.number_format = MONEY
    for n, (b, i) in enumerate(zip(balances, invoiced)):
        bc = ws.cell(r, first + 2 * n, _num(b))
        ic = ws.cell(r, first + 2 * n + 1, _num(i))
        bc.number_format = ic.number_format = MONEY
        if shade and i is not None:
            ic.fill = INVOICED
