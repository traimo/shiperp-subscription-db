"""Deferred billing: the part of a term's TCV not yet invoiced, at every month-end.

Balance of a term at month-end M = TCV - invoices dated on or before M. The TCV starts counting at the earlier of the
term's start date and its first invoice; before that the term adds nothing (a renewal doesn't inflate earlier months).
An invoice without a date counts at 12/31 of its billing year (the workbook's year group it came from).

TCV comes from the workbook column "TCV - NN & Existing" and the invoices from its "Amount" / monthly billing columns,
both found by header text at import (app/importer/sheet/headers.py). Invoices added in the app count too.
Pure computation: no database access.
"""
from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from .models import Customer, Invoice, Term
from .money import ZERO, cents


def month_end(d: dt.date) -> dt.date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def month_ends(first: dt.date, last: dt.date) -> list[dt.date]:
    """Every month-end from first's month through last's month (inclusive)."""
    out, y, m = [], first.year, first.month
    while (y, m) <= (last.year, last.month):
        out.append(dt.date(y, m, calendar.monthrange(y, m)[1]))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def invoice_day(inv: Invoice) -> dt.date:
    return inv.invoice_date or dt.date(inv.billing_year, 12, 31)


def counts_from(t: Term) -> dt.date:
    """The day the term's TCV starts counting: its start or its first invoice, whichever is earlier."""
    return min([t.start_date, *(invoice_day(i) for i in t.invoices)])


def term_balances(t: Term, months: list[dt.date]) -> list[Decimal | None]:
    """Remaining deferred billing of one term at each month-end; None before the term counts."""
    start = month_end(counts_from(t))
    out = []
    for m in months:
        if m < start:
            out.append(None)
        else:
            out.append(cents(t.tcv - sum((i.amount for i in t.invoices if invoice_day(i) <= m), ZERO)))
    return out


def term_invoiced(t: Term, months: list[dt.date]) -> list[Decimal | None]:
    """Amount invoiced on the term in each month (None for a month without an invoice)."""
    index = {m: n for n, m in enumerate(months)}
    out: list[Decimal | None] = [None] * len(months)
    for i in t.invoices:
        n = index.get(month_end(invoice_day(i)))
        if n is not None and i.amount:
            out[n] = (out[n] or ZERO) + i.amount
    return out


def _add(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    return a if b is None else (a or ZERO) + b


@dataclass
class TermRow:
    term: Term
    balances: list[Decimal | None]
    invoiced: list[Decimal | None]             # amount invoiced in each month
    current: Decimal = ZERO                    # balance at the current month-end

    @property
    def invoiced_months(self) -> set[int]:
        return {n for n, v in enumerate(self.invoiced) if v is not None}


@dataclass
class CustomerRow:
    customer: Customer
    tcv: Decimal
    balances: list[Decimal | None]             # None before any of the customer's terms counts
    invoiced: list[Decimal | None]             # amount invoiced in each month (None: no invoice)
    terms: list[TermRow] = field(default_factory=list)
    current: Decimal = ZERO                    # balance at the current month-end

    @property
    def invoiced_months(self) -> set[int]:
        return {n for n, v in enumerate(self.invoiced) if v is not None}

    @property
    def platforms(self) -> list[str]:
        return sorted({tr.term.subscription.platform.value for tr in self.terms})


def _relevant(t: Term) -> bool:
    return t.tcv > 0 or bool(t.invoices)


def deferred_table(customers: list[Customer], today: dt.date, platform: str = ""):
    """(months, rows, current_index) for the customers' terms (optionally one platform).

    months run from the earliest month a term counts through the later of today's month and the last invoice month;
    current_index is the position of today's month-end in months."""
    terms = [(c, t) for c in customers for s in c.subscriptions if not platform or s.platform.value == platform
             for t in s.terms if _relevant(t)]
    if not terms:
        return [], [], None
    first = min(counts_from(t) for _, t in terms)
    last = max([today, *(invoice_day(i) for _, t in terms for i in t.invoices)])
    months = month_ends(first, last)
    current_index = months.index(month_end(today))

    rows: dict[int, CustomerRow] = {}
    for c, t in terms:
        row = rows.get(c.id)
        if row is None:
            row = rows[c.id] = CustomerRow(c, ZERO, [None] * len(months), [None] * len(months))
        tr = TermRow(t, term_balances(t, months), term_invoiced(t, months))
        row.terms.append(tr)
        row.tcv += t.tcv
        row.balances = [_add(a, b) for a, b in zip(row.balances, tr.balances)]
        row.invoiced = [_add(a, b) for a, b in zip(row.invoiced, tr.invoiced)]
    out = sorted(rows.values(), key=lambda r: r.customer.qb_number)
    for r in out:
        r.current = r.balances[current_index] or ZERO
        r.terms.sort(key=lambda tr: (tr.term.start_date, tr.term.id))
        for tr in r.terms:
            tr.current = tr.balances[current_index] or ZERO
    return months, out, current_index


def column_totals(rows: list[CustomerRow], n_months: int) -> list[Decimal]:
    """Total balance at each month-end."""
    return [sum((r.balances[n] or ZERO for r in rows), ZERO) for n in range(n_months)]


def invoiced_totals(rows: list[CustomerRow], n_months: int) -> list[Decimal]:
    """Total invoiced in each month."""
    return [sum((r.invoiced[n] or ZERO for r in rows), ZERO) for n in range(n_months)]
