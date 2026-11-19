"""Column check before an import: the workbooks are read by header text, so a renamed, removed or look-alike column
would otherwise import wrong data without a message.

- errors: a required column is missing, a billing group has no amount column (its invoices would be skipped), or two
  columns match the same field (the importer would silently use the left one). The import is refused before the
  database is touched.
- new_columns: headers in the contract columns that the importer does not know. They are imported as nothing and
  only listed, so someone can decide whether the app should store them.

Only the contract columns are checked. The revenue schedule (subscription workbook, right of "Review by Tin") and the
monthly revenue / budget columns of the maintenance register repeat headers by design and are not imported column by
column.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from openpyxl.utils import get_column_letter

from .sheet.headers import REQUIRED_FIELDS, label
from .sheet.model import Workbook

TAB_NAMES = {"onprem": "On-Premise", "cloud": "Cloud"}
REQUIRED_TABS = ("onprem", "cloud")

# maintenance register: names the importer reads, and how often each one appears in the register on purpose
MAINT_REQUIRED = (("customer", "customer number"), ("customer name",))   # one of each group
MAINT_USED = ("customer", "customer name", "product", "year", "contract month", "go live date", "billing anniversary",
              "original before discount", "discount", "original, net of discount", "total current license",
              "original maintenance", "additional maintenance", "total maintenance", "addl purchase - module",
              "addl purchase - plant", "purchase - carrier", "plant qty (excluding core)",
              "additional purchase - amount", "go - live", "purchase amount")
MAINT_REPEATS = {"go live date": 3, "additional purchase - amount": 2}   # main, module, plant / module, plant
MAINT_KNOWN_UNUSED = ("customer count", "total addl purchase", "4% increase", "maintenance %")


class ColumnProblem(RuntimeError):
    """The workbook's columns cannot be imported safely."""


@dataclass
class ColumnCheck:
    errors: list[str] = field(default_factory=list)
    new_columns: list[str] = field(default_factory=list)

    def raise_if_errors(self) -> None:
        if self.errors:
            raise ColumnProblem("The spreadsheet cannot be imported safely - nothing was imported:\n- "
                                + "\n- ".join(self.errors))


def check_customer_numbers(wb) -> list[str]:
    """Contract rows whose Customer Number is not a QuickBooks number (80000-89999): a placeholder such as 9999 for a
    deal without a number yet. The database refuses them, so the import refuses the workbook before touching it."""
    from .sheet.rules import QB_RANGE
    lo, hi = QB_RANGE
    return [f"{tab.sheet_name} row {c.row}: Customer Number {c.customer_number} is not a QuickBooks customer "
            f"number ({lo}-{hi}). Enter the customer's QB number, or leave the row out until it has one."
            for tab in wb.tabs.values() for c in tab.contracts if not lo <= c.customer_number <= hi]


def _letters(cols) -> str:
    return " and ".join(get_column_letter(c) for c in cols)


def _billing_years_without_amount(colmap: dict[str, int]) -> list[str]:
    """Billing groups (a 'Date Invoiced' or a GL#1215 deferred-billing column) whose amount column was not found: the
    mapper reads invoices only from bill_amount_YYYY, so that year's invoices would be silently skipped."""
    years = {m.group(2) for k in colmap if (m := re.fullmatch(r"(bill_date|deferred_billing)_(20\d\d)", k))}
    return sorted(y for y in years if f"bill_amount_{y}" not in colmap)


def check_subscription(wb: Workbook) -> ColumnCheck:
    out = ColumnCheck()
    name = "subscription workbook"
    for key in REQUIRED_TABS:
        if key not in wb.tabs:
            out.errors.append(f"{name}: no '{TAB_NAMES[key]} ARR' tab found.")
    for key, tab in wb.tabs.items():
        where = f"{name}, tab '{tab.sheet_name}'"
        if not tab.sections:
            out.errors.append(f"{where}: no sections (A. ... E.) found.")
        missing: dict[str, list[str]] = {}
        dups: dict[tuple[str, tuple[int, ...]], list[str]] = {}
        new: dict[tuple[int, str], None] = {}
        no_amount: dict[str, list[str]] = {}
        for letter, sec in tab.sections.items():
            for f in REQUIRED_FIELDS:
                if f not in sec.colmap:
                    missing.setdefault(f, []).append(letter)
            for year in _billing_years_without_amount(sec.colmap):
                no_amount.setdefault(year, []).append(letter)
            if sec.issues:
                for f, cols in sec.issues.checked_duplicates().items():
                    dups.setdefault((f, tuple(cols)), []).append(letter)
                for col, text in sec.issues.unknown:
                    new[(col, text)] = None
        for f, letters in missing.items():
            out.errors.append(f"{where}: no '{label(f)}' column (section {', '.join(letters)}) - "
                              "was it renamed or deleted?")
        for year, letters in sorted(no_amount.items()):
            out.errors.append(f"{where}: no 'Amount' column for the {year} billing (section {', '.join(letters)}) - "
                              "was it renamed or deleted? Its invoices would not be imported.")
        for (f, cols), letters in dups.items():
            out.errors.append(f"{where}: columns {_letters(cols)} both look like '{label(f)}' "
                              f"(section {', '.join(letters)}); the import would use column "
                              f"{get_column_letter(cols[0])}. Rename or remove one.")
        for col, text in new:
            out.new_columns.append(f"{where}, column {get_column_letter(col)}: '{text}'")
    return out


def check_register(sheet) -> ColumnCheck:
    """`sheet`: maintenance.MaintenanceSheet."""
    out = ColumnCheck()
    where = "maintenance workbook"
    by_name = sheet.cols.by_name
    for group in MAINT_REQUIRED:
        if not any(n in by_name for n in group):
            out.errors.append(f"{where}: no '{group[0]}' column - was it renamed or deleted?")
    for n in MAINT_USED:
        cols = by_name.get(n, [])
        allowed = MAINT_REPEATS.get(n, 1)
        if len(cols) > allowed:
            out.errors.append(f"{where}: {len(cols)} columns named '{n}' ({_letters(c + 1 for c in cols)}), "
                              f"expected {allowed}. Rename or remove the extra one.")
    years = Counter(sheet.cols.year.values())
    for y, n in years.items():
        if n > 1:
            out.errors.append(f"{where}: {n} fee columns for the year ending {y}. Rename or remove the extra one.")
    first_year = min(sheet.cols.year, default=None)
    known = set(MAINT_USED) | set(MAINT_KNOWN_UNUSED)
    for n, cols in by_name.items():
        for c in cols:
            if (first_year is None or c < first_year) and n not in known:
                out.new_columns.append(f"{where}, column {get_column_letter(c + 1)}: '{sheet.header_text.get(c, n)}'")
    return out
