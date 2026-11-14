"""Business rules: enums, period parsing/formatting and derived fields (spec §4, §5, §8)."""
from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass, field

SECTIONS = {
    "A": "Net new",
    "B": "Conversion (perpetual -> subscription)",
    "C": "Upsell (modules / plants)",
    "D": "Renewal",
    "E": "Terminated",
}

# canonical value -> recognised spellings (lower-case). The canonical value is what the UI writes.
NEW_EXISTING = {
    "New Account": ["new account", "new", "new account - us", "new account - see cloud"],
    "Existing": ["existing", "exisiting", "renewal"],
    "Existing Account": ["existing account"],
    "Converted": ["converted", "coverted"],
    "Converted P-C": ["converted p-c", "coverted p-c"],
    "Converted P-OP&C": ["converted p-op&c", "coverted p-op&c"],
    "Hybrid": ["hybrid"],
    "Amendment": ["amendment"],
    "Plant Location": ["plant location"],
    "HazMat": ["hazmat"],
}
STATUS = {"Active": ["active"], "Closed": ["closed"], "Churn": ["churn"]}
RENEWAL_STATUS_FIXED = [
    "Active", "Renewed", "For renewal", "Not due for renewal", "Amended", "Final Period", "Churn",
    "Churn-in collection", "Terminated", "Invoiced but pending doc", "Terminated, not a subscription",
]
PAYMENT_TERMS = ["Annual", "Upfront", "Upfront 3 yrs", "Annual (NTT)", "Annual - Quarterly"]
RESELLERS = ["n/a", "NTT", "Netgo", "Mehrwerk"]
LICENSE_CATEGORY = {"onprem": ["On Premise", "Hybrid"], "cloud": ["Cloud", "Hybrid"]}
HYBRID_CLASS = ["n/a", "Perpetual/On Prem", "Perpetual/Cloud", "On Prem/Cloud", "Cloud"]
CHECK_VALUES = ["Yes", "To check", "mismatched", "Missing", "not in commission", "done", "Done-Tin", "n/a"]
BILLING_PERIODS = ["Y1", "Y2", "Y3", "Y4", "Y5", "Y1, Y2, Y3 - Upfront 3yrs", "Y1 - 50%"]
SECTION_NEW_EXISTING = {
    "A": ["New Account"],
    "B": ["Converted", "Converted P-C", "Converted P-OP&C", "Hybrid", "Existing"],
    "C": ["Existing Account", "Plant Location", "HazMat", "Amendment"],
    "D": ["Existing", "Amendment"],
    "E": ["Existing", "New Account"],
}
CHURN_WORDS = ("churn", "terminat", "cancel", "final period", "not renewing")


def canonical(value, table: dict[str, list[str]]) -> str | None:
    """Canonical spelling for a value, or None if unrecognised."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value)).strip().lower()
    for canon, spellings in table.items():
        if s == canon.lower() or s in spellings:
            return canon
    return None


def canonical_renewal_status(value) -> str | None:
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value)).strip()
    for v in RENEWAL_STATUS_FIXED:
        if s.lower() == v.lower():
            return v
    if re.fullmatch(r"(?i)renewed( 20\d\d)?", s) or re.fullmatch(r"(?i)see 20\d\d", s):
        return s[0].upper() + s[1:].lower()
    if re.fullmatch(r"(?i)cancell?ed \d{1,2}/\d{1,2}/\d{2,4}", s):
        return "Cancelled " + s.split(" ", 1)[1]
    return None


def is_churn_flagged(status, renewal_status) -> bool:
    text = f"{status or ''} {renewal_status or ''}".lower()
    return any(w in text for w in CHURN_WORDS) and "not a subsc" not in text


def is_not_subscription(renewal_status) -> bool:
    return "not a subsc" in str(renewal_status or "").lower()


# ----------------------------------------------------------------------------------------------- periods

@dataclass
class Period:
    start: dt.date | None
    end: dt.date | None
    revised: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.start is not None and self.end is not None


_DATE_RE = re.compile(r"(\d{1,2})\s*/+\s*(\d{1,2})\s*/+\s*(\d{2,4})")


def _mkdate(m: re.Match, problems: list[str]) -> dt.date | None:
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    if not 1 <= mo <= 12:
        problems.append(f"invalid month in {m.group(0)}")
        return None
    last = calendar.monthrange(y, mo)[1]
    if d > last:
        problems.append(f"invalid day {m.group(0)} (clamped to {mo:02d}/{last:02d}/{y})")
        d = last
    if d < 1:
        problems.append(f"invalid day in {m.group(0)}")
        return None
    return dt.date(y, mo, d)


def parse_period(text) -> Period:
    """Parse 'MM/DD/YYYY-MM/DD/YYYY' (tolerates 'Rev ', '//', spaces, 2-digit years, invalid days)."""
    if text is None or not str(text).strip():
        return Period(None, None, problems=["blank subscription period"])
    s = str(text).strip()
    revised = s.lower().startswith("rev")
    dates = list(_DATE_RE.finditer(s))
    if len(dates) != 2:
        return Period(None, None, revised, [f"unparsable subscription period '{s}'"])
    problems: list[str] = []
    start, end = _mkdate(dates[0], problems), _mkdate(dates[1], problems)
    if start and end and end < start:
        problems.append("period ends before it starts")
    return Period(start, end, revised, problems)


def format_period(start: dt.date, end: dt.date, revised: bool = False) -> str:
    p = f"{start:%m/%d/%Y}-{end:%m/%d/%Y}"
    return f"Rev {p}" if revised else p


def parse_date(value) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    m = _DATE_RE.search(s)
    return _mkdate(m, []) if m else None


def parse_dates(value, year: int | None = None) -> tuple[list[dt.date], list[str]]:
    """All dates in a cell ("1/5/2026,5/1/2026", a real date, ...). With `year`, dates in other years are rejected.
    Returns (dates, problems) - problems name the text that was not understood (e.g. the typo '7/8/206')."""
    if value is None or value == "":
        return [], []
    if isinstance(value, dt.datetime):
        value = value.date()
    if isinstance(value, dt.date):
        return ([value], []) if year is None or value.year == year else ([], [f"{value:%m/%d/%Y} is not in {year}"])
    dates, problems = [], []
    for m in _DATE_RE.finditer(str(value)):
        d = _mkdate(m, problems)
        if d is None:
            continue
        if year is not None and d.year != year:
            problems.append(f"'{m.group(0)}' is not a date in {year}")
            continue
        dates.append(d)
    return dates, problems


# ----------------------------------------------------------------------------------------------- derivations

DAYS_PER_MONTH = 365.25 / 12          # 30.4375: average month length


def months_between(start: dt.date, end: dt.date) -> int:
    """Whole months in an inclusive term (05/01/2020-04/30/2023 -> 36). Also used by the app (metrics)."""
    return max(1, round(((end - start).days + 1) / DAYS_PER_MONTH))


def term_years(start: dt.date, end: dt.date) -> float:
    return round(months_between(start, end) / 12, 2)


def renewal_date(end: dt.date) -> dt.date:
    return end + dt.timedelta(days=1)


def anniversary_month(start: dt.date, upfront: bool = False) -> str:
    name = calendar.month_name[start.month]
    return f"{name}-upfront" if upfront else name


def years_touched(start: dt.date, end: dt.date) -> list[int]:
    return list(range(start.year, end.year + 1))


@dataclass
class Derived:
    period_text: str
    renewal_date: dt.date
    term_years: float
    arr: float
    monthly: float
    anniversary_month: str
    years: list[int]

    def as_dict(self) -> dict:
        return {
            "period_text": self.period_text,
            "renewal_date": self.renewal_date.isoformat(),
            "term_years": self.term_years,
            "arr": round(self.arr, 2),
            "monthly": round(self.monthly, 2),
            "anniversary_month": self.anniversary_month,
            "years": self.years,
        }


def derive(start: dt.date, end: dt.date, tcv: float, payment_terms: str = "", revised: bool = False,
           term: float | None = None) -> Derived:
    """term: contractual term in years when it differs from the period (co-terminus stubs, billed periods)."""
    if end < start:
        raise ValueError("Period end is before period start")
    if term is not None and term <= 0:
        raise ValueError("Term in yrs must be positive")
    term = term or term_years(start, end)
    arr = float(tcv) / term if term else 0.0
    upfront = "upfront" in (payment_terms or "").lower()
    return Derived(
        period_text=format_period(start, end, revised),
        renewal_date=renewal_date(end),
        term_years=term,
        arr=arr,
        monthly=arr / 12,
        anniversary_month=anniversary_month(start, upfront),
        years=years_touched(start, end),
    )


def to_number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace(",", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        return None
