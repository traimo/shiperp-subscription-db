"""Rules on the Subscription Summary (On-Prem / Cloud ARR&CARR tabs), straight from the sheet (no database)."""
from __future__ import annotations

import datetime as dt
import re

from openpyxl.utils import get_column_letter

from ..importer.sheet import rules
from ..importer.sheet.headers import CHECKLIST_KEYS, label
from ..importer.sheet.model import ContractRow, Tab, Workbook
from . import Issue

WB = "subscription"
OPEN_CHECKS = {"to check": "To check", "mismatched": "mismatched", "missing": "Missing"}
CLOSED_RENEWAL = re.compile(r"(?i)^(renewed|terminated|amended|churn-in collection|see 20\d\d)")
ACTIVE_RENEWAL = {"active", "for renewal", "not due for renewal", "invoiced but pending doc"}


def col_of(tab: Tab, c: ContractRow, key: str) -> int | None:
    sec = tab.sections.get(c.section)
    return (sec.colmap if sec else tab.master).get(key)


def cell(tab: Tab, c: ContractRow, key: str) -> str | None:
    col = col_of(tab, c, key)
    return f"{get_column_letter(col)}{c.row}" if col else None


def header(tab: Tab, c: ContractRow, key: str) -> str:
    sec = tab.sections.get(c.section)
    return (sec.header_text.get(key) if sec else None) or label(key)


def issue(rule: str, tab: Tab, c: ContractRow, key: str, message: str, current=None, expected=None,
          severity: str = "") -> Issue:
    return Issue(rule, WB, tab.sheet_name, cell(tab, c, key), c.row, message, header(tab, c, key),
                 c.customer_number, None if current is None else str(current),
                 None if expected is None else str(expected), severity)


def _date(v) -> dt.date | None:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return rules.parse_date(v)


def _fmt(v) -> str:
    if isinstance(v, (dt.date, dt.datetime)):
        return f"{v:%m/%d/%Y}"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return "" if v is None else str(v)


def _text(v) -> str:
    return "" if v is None else str(v)


def check(wb: Workbook) -> list[Issue]:
    out: list[Issue] = []
    for tab in wb.tabs.values():
        for (row, col, val) in tab.error_cells:
            out.append(Issue("S01", WB, tab.sheet_name, f"{get_column_letter(col)}{row}", row,
                             f"Cell shows {val}.", current=val))
        for (row, why) in tab.skipped_rows:
            out.append(Issue("S02", WB, tab.sheet_name, f"A{row}", row, why + " - the row is not imported."))
        for c in tab.contracts:
            out += _row_checks(tab, c)
    return out


def _row_checks(tab: Tab, c: ContractRow) -> list[Issue]:
    out: list[Issue] = []
    v = c.values
    p = c.period
    # ---- period and derived values (not on section E mirrors)
    if not p.ok or p.problems:
        out.append(issue("S03", tab, c, "period", "Subscription Period: " + ("; ".join(p.problems) or "not a period"),
                         v.get("period"), "MM/DD/YYYY-MM/DD/YYYY"))
    if p.ok and c.section != "E":
        rd = _date(v.get("renewal_date"))
        want = p.end + dt.timedelta(days=1)
        if v.get("renewal_date") not in (None, "") and rd != want:
            out.append(issue("S04", tab, c, "renewal_date", "Renewal Date should be the day after the period ends.",
                             _fmt(v.get("renewal_date")), f"{want:%m/%d/%Y}"))
        ty = rules.to_number(v.get("term_years"))
        per = rules.term_years(p.start, p.end)
        if ty and abs(ty - per) > 0.05:
            out.append(issue("S05", tab, c, "term_years", f"The period {p.start:%m/%d/%Y}-{p.end:%m/%d/%Y} is "
                             f"{per:g} years.", f"{ty:g}", f"{per:g}"))
        tcv = rules.to_number(v.get("tcv_total"))
        arr = rules.to_number(v.get("arr"))
        if tcv and ty and arr is not None and abs(arr - tcv / ty) > 1:
            if arr == 0:
                out.append(issue("S06", tab, c, "arr", "ARR is 0 while TCV is filled (not live / not billed yet?).",
                                 "0", f"{tcv / ty:,.2f}", severity="info"))
            else:
                out.append(issue("S06", tab, c, "arr", f"TCV {tcv:,.2f} ÷ {ty:g} yrs = {tcv / ty:,.2f}.",
                                 f"{arr:,.2f}", f"{tcv / ty:,.2f}"))
        mo = rules.to_number(v.get("monthly"))
        if arr and mo is not None and abs(mo - arr / 12) > 0.5:
            out.append(issue("S07", tab, c, "monthly", f"ARR {arr:,.2f} ÷ 12 = {arr / 12:,.2f}.",
                             f"{mo:,.2f}", f"{arr / 12:,.2f}"))
        nn, ex = rules.to_number(v.get("tcv_new")) or 0, rules.to_number(v.get("tcv_existing")) or 0
        if tcv is not None and (nn or ex) and abs(nn + ex - tcv) > 1:
            out.append(issue("S08", tab, c, "tcv_total", f"Net New {nn:,.2f} + Existing {ex:,.2f} = {nn + ex:,.2f}.",
                             f"{tcv:,.2f}", f"{nn + ex:,.2f}"))
    # ---- allowed values
    out += _enum(tab, c)
    # ---- checklist
    for k in CHECKLIST_KEYS:
        raw = _text(v.get(k)).strip()
        if raw.lower() in OPEN_CHECKS:
            out.append(issue("S18", tab, c, k, f"{header(tab, c, k)}: {raw}.", raw, "Yes / done"))
    # ---- counter
    cy = v.get("counter_year")
    if c.section != "E" and cy is not None and rules.to_number(cy) != 1:
        out.append(issue("S19", tab, c, "counter_year", "Counter - per Year should be 1 on a contract row.", cy, 1))
    # ---- monthly revenue vs YTD
    years = sorted({int(k[10:14]) for k in v if k.startswith("rev_month_")})
    for y in years:
        ytd = rules.to_number(v.get(f"rev_ytd_{y}"))
        months = [rules.to_number(v.get(f"rev_month_{y}_{m:02d}")) for m in range(1, 13)]
        if ytd is None or not any(x is not None for x in months):
            continue
        s = sum(x or 0 for x in months)
        if abs(s - ytd) > 1:
            out.append(issue("S20", tab, c, f"rev_ytd_{y}", f"Σ monthly revenue {y} = {s:,.2f}.",
                             f"{ytd:,.2f}", f"{s:,.2f}"))
    return out


def _spelling_or_unknown(rule, tab, c, key, raw, canon, allowed_text, unknown_severity="warning"):
    if canon is None:
        return issue(rule, tab, c, key, f"'{raw}' is not a standard value ({allowed_text}).", raw, None,
                     unknown_severity)
    if raw != canon:
        return issue(rule, tab, c, key, f"Spelling: '{raw}' should be '{canon}'.", raw, canon, "info")
    return None


def _enum(tab: Tab, c: ContractRow) -> list[Issue]:
    out: list[Issue] = []
    v = c.values
    found = []
    # New / Existing
    raw = _text(v.get("new_existing"))
    if raw.strip():
        canon = rules.canonical(raw, rules.NEW_EXISTING)
        found.append(_spelling_or_unknown("S10", tab, c, "new_existing", raw, canon,
                                          ", ".join(rules.NEW_EXISTING)))
        allowed = rules.SECTION_NEW_EXISTING.get(c.section, [])
        if canon and allowed and canon not in allowed:
            found.append(issue("S10", tab, c, "new_existing", f"'{canon}' is not expected in section {c.section} "
                               f"(expected {', '.join(allowed)}).", raw, " / ".join(allowed), "info"))
    # Status
    st = _text(v.get("status"))
    st_c = rules.canonical(st, rules.STATUS) if st.strip() else None
    if st.strip():
        found.append(_spelling_or_unknown("S11", tab, c, "status", st, st_c, "Active, Closed, Churn"))
    # Renewal status
    rs = _text(v.get("renewal_status"))
    rs_c = rules.canonical_renewal_status(rs) if rs.strip() else None
    if rs.strip():
        m = re.match(r"(?i)cancell?ed\s+(.*)", rs.strip())
        if m:
            _, problems = rules.parse_dates(m.group(1))
            if problems or not rules.parse_date(m.group(1)):
                found.append(issue("S12", tab, c, "renewal_status", "Cancellation date is not a valid date: "
                                   + "; ".join(problems or ["unreadable"]), rs, None, "error"))
        if rs_c is None:
            found.append(issue("S12", tab, c, "renewal_status", f"'{rs}' is not a standard Renewal Status.", rs))
        elif rs_c.lower() != rs.strip().lower():
            found.append(issue("S12", tab, c, "renewal_status", f"Spelling: '{rs}' should be '{rs_c}'.", rs, rs_c,
                               "info"))
    elif c.section != "E":
        found.append(issue("S12", tab, c, "renewal_status", "Renewal Status is blank.", "", None, "info"))
    # Status vs Renewal Status
    if st_c and rs_c and c.section != "E":
        rl = rs_c.lower()
        if st_c == "Active" and CLOSED_RENEWAL.match(rl):
            found.append(issue("S17", tab, c, "status", f"Status Active but Renewal Status '{rs_c}'.", st, "Closed"))
        elif st_c == "Active" and rl == "churn":
            found.append(issue("S17", tab, c, "status", "Status Active but Renewal Status 'Churn'.", st, "Churn"))
        elif st_c == "Closed" and rl in ACTIVE_RENEWAL:
            found.append(issue("S17", tab, c, "status", f"Status Closed but Renewal Status '{rs_c}'.", st, "Active"))
    # License category
    lc = _text(v.get("license_category"))
    allowed = rules.LICENSE_CATEGORY.get(tab.key, [])
    if lc.strip():
        canon = next((a for a in allowed if a.lower() == lc.strip().lower()), None)
        found.append(_spelling_or_unknown("S13", tab, c, "license_category", lc, canon, ", ".join(allowed)))
    # Hybrid class
    hc = _text(v.get("hybrid_class"))
    if hc.strip():
        canon = rules.canonical_hybrid_class(hc)
        found.append(_spelling_or_unknown("S14", tab, c, "hybrid_class", hc, canon, ", ".join(rules.HYBRID_CLASS)))
    elif hc and not hc.strip():
        found.append(issue("S14", tab, c, "hybrid_class", "Cell contains only spaces.", repr(hc), "", "info"))
    if lc.strip().lower() == "hybrid" and (not hc.strip() or hc.strip().lower() == "n/a"):
        found.append(issue("S14", tab, c, "hybrid_class", "License Category is Hybrid but the hybrid class is not set.",
                           hc.strip() or "(blank)"))
    # Payment terms
    pt = _text(v.get("payment_terms"))
    if pt.strip():
        canon = next((a for a in rules.PAYMENT_TERMS if a.lower() == pt.strip().lower()), None)
        if canon is None and re.search(r"(?i)annua1", pt):
            canon = "Annual"
        found.append(_spelling_or_unknown("S15", tab, c, "payment_terms", pt, canon,
                                          ", ".join(rules.PAYMENT_TERMS), "info"))
    # Reseller
    rsl = _text(v.get("reseller"))
    if rsl.strip():
        canon = next((a for a in rules.RESELLERS if a.lower() == rsl.strip().lower()), None)
        if canon is None and rsl.strip().lower() in ("nia", "n.a.", "na"):
            canon = "n/a"
        found.append(_spelling_or_unknown("S16", tab, c, "reseller", rsl, canon, ", ".join(rules.RESELLERS), "info"))
    return [f for f in found if f is not None]

