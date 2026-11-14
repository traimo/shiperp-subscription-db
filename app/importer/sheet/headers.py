"""Header text -> canonical column keys.

The workbook is read by header *text*, never by column letter (spec §3): columns may move, and header spelling drifts
("ARR -2024 ", "TCV  - Net New", "Cutomer Number", Cloud "Contract Name", section-E "ARR 12/31/2020").
"""
from __future__ import annotations

import datetime as dt
import re

YEAR_COL_RE = re.compile(r"^arr\b(?!\s*-\s*annual).*?(20\d\d)\s*$")
DEFERRED_BILLING_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(20\d\d)\s*-\s*gl#?\s*1215")
QUARTER_RE = re.compile(r"^q([1-4])\s*-\s*(20\d\d)$")
YTD_RE = re.compile(r"ytd\s+gaap\s+revenue\s*-?\s*(20\d\d)")
# 2026 layout: Date Invoiced | "Jan to June 2026" | one column per month-end (Jul..Dec) | Total Invoice |
# "GL#1215.3 Deferred Billing" (no date)
HALF_YEAR_RE = re.compile(r"^jan(uary)?\s+(to|-)\s+june?\s+(20\d\d)$")
UNDATED_DEFERRED_RE = re.compile(r"^gl#?\s*1215[\d.]*\s+deferred billing")


def norm(text) -> str:
    """Lower-case, unify dashes, collapse whitespace."""
    if text is None:
        return ""
    s = str(text).replace("–", "-").replace("—", "-").replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip().lower()
    s = re.sub(r"\s*-\s*", " - ", s)
    return s


# (key, label, predicate on normalised header). First match wins, so order matters.
_SIMPLE: list[tuple[str, str, re.Pattern]] = [
    ("counter_total", "Counter - Grand Total", re.compile(r"^counter - grand total")),
    ("counter_year", "Counter - per Year", re.compile(r"^counter - per year")),
    ("license_category", "License Category", re.compile(r"^license category")),
    ("hybrid_class", "If Hybrid - License Class", re.compile(r"^if hybrid")),
    ("chk_commission", "Verified with Commission Report", re.compile(r"^verified with commission")),
    ("chk_sf", "Verified with SF", re.compile(r"^verified with sf")),
    ("chk_amort", "With Amortization Schedule", re.compile(r"^with amortization")),
    ("chk_invoice_coding", "Invoice - Rev Coding", re.compile(r"^invoice - rev coding")),
    ("chk_so_memo", "SO Memo Rev coding", re.compile(r"^so memo")),
    ("chk_celoxis", "SO Matched with Celoxis", re.compile(r"^so matched with celoxis")),
    ("chk_so_gl", "SO - GL / OS and CS", re.compile(r"^so - gl")),
    ("chk_posted_qb", "Posted in QB", re.compile(r"^posted in qb")),
    ("chk_capitalized", "Match with Summary of Capitalized Services", re.compile(r"^match with summary of capitalized")),
    ("chk_deferred_review", "Review of Deferred Rev Balance", re.compile(r"review of deferred rev")),
    ("customer_number", "Customer Number", re.compile(r"^cu(s)?tomer number")),
    ("customer_name", "Customer Name", re.compile(r"^(customer name|contract name)")),
    ("product", "Product", re.compile(r"^product$")),
    ("signed_date", "Contract Signed Date", re.compile(r"^(contract signed date|date cancelled)")),
    ("new_existing", "New/Existing", re.compile(r"^new/existing")),
    ("ae", "AE", re.compile(r"^ae$")),
    ("payment_terms", "Payment Terms", re.compile(r"^payment terms")),
    ("reseller", "Reseller", re.compile(r"^reseller")),
    ("period", "Subscription Period", re.compile(r"^subscription period")),
    ("renewal_date", "Subscription Renewal Date", re.compile(r"^subscription renewal date")),
    ("status", "Status", re.compile(r"^status$")),
    ("anniversary_month", "Anniversary Month", re.compile(r"^anniversary month")),
    ("tcv_new", "TCV - Net New", re.compile(r"^tcv - net new")),
    ("tcv_existing", "TCV - Existing", re.compile(r"^tcv - existing")),
    ("tcv_total", "TCV - NN & Existing", re.compile(r"^tcv - nn")),
    ("term_years", "Term in yrs", re.compile(r"^term in yrs")),
    ("arr", "ARR - Annual Recurring Revenue", re.compile(r"^arr( - annual recurring revenue)?$")),
    ("monthly", "Monthly Subscription Revenue", re.compile(r"^monthly subscription revenue")),
    ("renewal_status", "Renewal Status", re.compile(r"^renewal status")),
    ("golive", "GLive Status", re.compile(r"^glive status")),
    ("transactional_volume", "Transactional Volume", re.compile(r"^transactional volume")),
    ("carr", "CARR TODATE", re.compile(r"^carr todate")),
    ("recent_invoice", "Recent Invoice", re.compile(r"^recent invoice")),
    ("billing_period", "Period (billing)", re.compile(r"^period$")),
    ("sales_person", "Sales Person", re.compile(r"^sales person")),
    ("review_by_tin", "Review by Tin", re.compile(r"^review by tin")),
]
_NOT_APPLICABLE = re.compile(r"^not applicable")
HELPER_KEYS = ("prior_arr", "arr_change", "not_applicable_3")

CHECKLIST_KEYS = [k for k, _, _ in _SIMPLE if k.startswith("chk_")]
LABELS = {k: lbl for k, lbl, _ in _SIMPLE}
LABELS.update({"prior_arr": "Previous ARR (helper)", "arr_change": "ARR change at renewal (helper)"})


def classify(raw_headers: dict[int, object]) -> dict[str, int]:
    """Map canonical keys to 1-based column numbers for one header row.

    Keys produced: the simple keys above, prior_arr / arr_change (the two "NOT APPLICABLE" columns),
    arr_YYYY (year ARR columns), bill_date_YYYY / bill_amount_YYYY / deferred_billing_YYYY (billing groups),
    rev_month_YYYY_MM (monthly revenue columns), rev_q_YYYY_N, rev_ytd_YYYY.
    Monthly billing layout (2026 on): bill_date_YYYY, bill_amount_YYYY (the "Jan to June" amount),
    bill_month_YYYY_MM (one per month-end column), bill_total_YYYY, deferred_billing_YYYY.
    """
    out: dict[str, int] = {}
    na_seen = 0
    pending_billing: list[tuple[str, int]] = []   # Date Invoiced / Amount waiting for their year header
    in_schedule = False
    month_year = None                             # inside a monthly billing block of this year
    for col in sorted(raw_headers):
        raw = raw_headers[col]
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        if isinstance(raw, (dt.datetime, dt.date)):
            if month_year and not in_schedule:
                out.setdefault(f"bill_month_{raw.year}_{raw.month:02d}", col)
                continue
            if in_schedule:
                key = f"rev_month_{raw.year}_{raw.month:02d}"
                out.setdefault(key, col)
            continue
        h = norm(raw)
        if h.startswith("gl#2320 deferred revenue,beg") or h.startswith("gl#2340 deferred revenue,beg"):
            in_schedule = True
            out.setdefault("rev_deferred_beg", col)
            continue
        if not in_schedule and (h == "date invoiced" or h == "amount"):
            pending_billing.append(("bill_date" if h == "date invoiced" else "bill_amount", col))
            continue
        if not in_schedule:
            m = HALF_YEAR_RE.match(h)
            if m:
                month_year = m.group(3)
                for kind, c in pending_billing:
                    if kind == "bill_date":
                        out.setdefault(f"bill_date_{month_year}", c)
                pending_billing.clear()
                out.setdefault(f"bill_amount_{month_year}", col)
                continue
            if month_year and h == "total invoice":
                out.setdefault(f"bill_total_{month_year}", col)
                continue
            if month_year and UNDATED_DEFERRED_RE.match(h):
                out.setdefault(f"deferred_billing_{month_year}", col)
                month_year = None
                continue
        m = DEFERRED_BILLING_RE.search(h)
        if m and not in_schedule:
            year = m.group(3)
            for kind, c in pending_billing:
                out.setdefault(f"{kind}_{year}", c)
            pending_billing.clear()
            out.setdefault(f"deferred_billing_{year}", col)
            continue
        if in_schedule:
            m = QUARTER_RE.match(h)
            if m:
                out.setdefault(f"rev_q_{m.group(2)}_{m.group(1)}", col)
                continue
            m = YTD_RE.search(h)
            if m:
                out.setdefault(f"rev_ytd_{m.group(1)}", col)
                continue
            continue
        if _NOT_APPLICABLE.match(h):
            if na_seen < len(HELPER_KEYS):
                out.setdefault(HELPER_KEYS[na_seen], col)
            na_seen += 1
            continue
        m = YEAR_COL_RE.match(h)
        if m and not h.startswith("arr - annual"):
            out.setdefault(f"arr_{m.group(1)}", col)
            continue
        for key, _lbl, pat in _SIMPLE:
            if pat.search(h):
                out.setdefault(key, col)
                if key == "review_by_tin":   # last billing column; the revenue schedule follows (Cloud has no "Beg")
                    in_schedule = True
                break
    return out


def year_keys(colmap: dict[str, int]) -> dict[int, str]:
    """{year: key} for the ARR-by-year columns present."""
    years = {}
    for k in colmap:
        if re.fullmatch(r"arr_20\d\d", k):
            years[int(k[4:])] = k
    return dict(sorted(years.items()))


def label(key: str) -> str:
    if key in LABELS:
        return LABELS[key]
    m = re.fullmatch(r"arr_(20\d\d)", key)
    if m:
        return f"ARR - {m.group(1)}"
    m = re.fullmatch(r"bill_(date|amount)_(20\d\d)", key)
    if m:
        return f"{'Date Invoiced' if m.group(1) == 'date' else 'Amount'} {m.group(2)}"
    m = re.fullmatch(r"bill_month_(20\d\d)_(\d\d)", key)
    if m:
        return f"Invoiced {dt.date(int(m.group(1)), int(m.group(2)), 1):%b %Y}"
    m = re.fullmatch(r"bill_total_(20\d\d)", key)
    if m:
        return f"Total Invoice {m.group(1)}"
    return key
