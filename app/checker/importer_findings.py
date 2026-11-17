"""Findings that need the importer: run the normal import into a throwaway PostgreSQL schema (sheetcheck_<pid>,
dropped afterwards; the live tables and data/reports are never touched), then turn its notes and the ARR
reconciliation into cell issues."""
from __future__ import annotations

import datetime as dt
import os
import re
import tempfile
from pathlib import Path

from openpyxl.utils import get_column_letter

from ..importer import maintenance as m
from ..importer.sheet.model import Workbook
from . import Issue
from .subscription_rules import cell, header

SEVERITY_RULE = {"rejected": "I1", "fix": "I2", "warning": "I3", "inference": "I4"}
# notes the sheet rules already report with a better cell / expected value
COVERED = [re.compile(p) for p in (r"^Period: ", r"^Sheet ARR ", r"^Term in yrs", r"^Masterlist names")]
TAB_KEY = {"On-Prem": "onprem", "Cloud": "cloud"}
REF_RE = re.compile(r"^(On-Prem|Cloud) ([A-E])(\d+)$")
COLUMN_HINTS = [(r"(?i)period", "period"), (r"(?i)term in yrs", "term_years"), (r"(?i)\btcv\b", "tcv_total"),
                (r"(?i)\barr\b", "arr"), (r"(?i)date invoiced (20\d\d)", "bill_date_{0}"),
                (r"(?i)invoice", "bill_total_2026"), (r"(?i)renewal|amended|predecessor", "renewal_status"),
                (r"(?i)cancel", "renewal_status")]


def run_temp_import(subscription: Path, maintenance: Path | None):
    from ..db import drop_schema
    from ..importer.cli import run_import
    tmp = Path(tempfile.mkdtemp(prefix="sheetcheck-"))
    schema = f"sheetcheck_{os.getpid()}"
    try:
        rep = run_import(subscription, schema=schema, today=dt.date.today(), report_dir=tmp,
                         maintenance=maintenance if maintenance else False, force=True)
    finally:
        drop_schema(None, schema)
    return rep, tmp


def check(subscription: Path, maintenance: Path | None, wb: Workbook, ms: m.MaintenanceSheet | None) -> list[Issue]:
    from ..importer.columns import ColumnProblem
    try:
        rep, tmp = run_temp_import(subscription, maintenance)
    except ColumnProblem as e:                        # columns renamed / duplicated: nothing else can be checked
        return [Issue(SEVERITY_RULE.get("rejected", "I1"), "subscription", "", None, None, line.lstrip("- "))
                for line in str(e).splitlines()[1:]]
    out: list[Issue] = []
    try:
        for n in rep.notes:
            if any(p.search(n.message) for p in COVERED):
                continue
            out.append(_note_issue(n, wb, ms))
        for d in rep.reconciliation:
            iss = _recon_issue(d, wb)
            if iss:
                out.append(iss)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return [i for i in out if i is not None]


def _note_issue(n, wb: Workbook, ms) -> Issue | None:
    rule = SEVERITY_RULE.get(n.severity, "I3")
    mm = REF_RE.match(n.ref or "")
    if mm:
        tab = wb.tabs[TAB_KEY[mm.group(1)]]
        row = int(mm.group(3))
        c = tab.row(row)
        key = "customer_number"
        for pat, k in COLUMN_HINTS:
            hit = re.search(pat, n.message)
            if hit:
                key = k.format(*hit.groups()) if "{0}" in k else k
                break
        if c is not None:
            ref = cell(tab, c, key) or cell(tab, c, "customer_number")
            return Issue(rule, "subscription", tab.sheet_name, ref, row, n.message, header(tab, c, key),
                         n.customer or c.customer_number)
        return Issue(rule, "subscription", tab.sheet_name, f"A{row}", row, n.message, "", n.customer)
    mm = re.match(r"^Maintenance row (\d+)$", n.ref or "")
    if mm:
        row = int(mm.group(1))
        j = ms.cols.first("customer") if ms else None
        return Issue(rule, "maintenance", m.SHEET, f"{get_column_letter(j + 1)}{row}" if j is not None else None,
                     row, n.message, "Customer", n.customer)
    mm = re.match(r"^(.+) row (\d+)$", n.ref or "")
    if mm:                                     # advance-billing invoice tabs
        return Issue(rule, "maintenance", mm.group(1), f"A{mm.group(2)}", int(mm.group(2)), n.message, "",
                     n.customer)
    return Issue(rule, "subscription", n.ref or "workbook", None, None, n.message, "", n.customer)


def _recon_issue(d: dict, wb: Workbook) -> Issue | None:
    tab = wb.tabs[TAB_KEY[d["platform"]]]
    year, qb = int(d["year"]), int(d["customer"])
    why = d.get("why") or ""
    if why.startswith("projected renewal"):
        rule = "R4"
    elif "transition" in why or "cancellation" in why:
        rule = "R2"
    elif d.get("future"):
        rule = "R3"
    else:
        rule = "R1"
    c = None
    for ref in [x.strip() for x in (d.get("terms") or "").split(",") if x.strip()]:
        mm = REF_RE.match(ref)
        if mm and TAB_KEY[mm.group(1)] == tab.key:
            c = tab.row(int(mm.group(3)))
            if c is not None and (c.year_arr(year) or 0) != 0:
                break
    if c is None:      # the row that carries the sheet's ARR for that year
        rows = [x for x in tab.contracts if x.customer_number == qb and x.section != "E"]
        c = next((x for x in rows if (x.year_arr(year) or 0) != 0), rows[0] if rows else None)
    msg = (f"{d.get('name') or qb} {year}: sheet {d['sheet']:,.2f}, contracts give {d['db']:,.2f} "
           f"(difference {d['diff']:,.2f}) - {why}.")
    if c is None:
        return Issue(rule, "subscription", tab.sheet_name, None, None, msg, f"ARR - {year}", qb,
                     f"{d['sheet']:,.2f}", f"{d['db']:,.2f}")
    key = f"arr_{year}"
    return Issue(rule, "subscription", tab.sheet_name, cell(tab, c, key), c.row, msg, header(tab, c, key), qb,
                 f"{d['sheet']:,.2f}" if d["sheet"] else "(blank)", f"{d['db']:,.2f}")
