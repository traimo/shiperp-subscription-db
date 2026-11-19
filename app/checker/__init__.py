"""Spreadsheet check: every issue in the two source workbooks, with the exact cell, what is there, what is expected
and how to fix it - so the sheets can be made 100 % accurate before the final import.

Read-only on the workbooks; never touches the real database (importer-based findings come from a throwaway import in
a temp folder, see importer_findings.py). Result: data/checks/latest-check.json, shown on /check and exported by
export.py. Exceptions that are correct as they are can be accepted with a reason (accept.py).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import config

CHECK_DIR = config.DATA_DIR / "checks"
LATEST = "latest-check.json"
SEVERITIES = ("error", "decision", "warning", "info")
SEVERITY_HELP = {
    "error": "Wrong data - changes the numbers or what gets imported. Fix in the sheet.",
    "decision": "Depends on an open accounting rule (ARR after a cancellation, renewal transition year).",
    "warning": "Probably wrong - review; fix the sheet or accept with a reason.",
    "info": "Cosmetic / spelling the importer tolerates - tidy up when convenient.",
}

# code -> (title, default severity, how to fix)
RULES: dict[str, tuple[str, str, str]] = {
    # subscription summary
    "S01": ("Excel error in a cell", "error", "Fix the formula or value (#REF!, #VALUE!, ...)."),
    "S02": ("Row looks like a contract but is skipped", "error",
            "Enter a numeric Customer Number and a Subscription Period MM/DD/YYYY-MM/DD/YYYY."),
    "S03": ("Subscription Period not valid", "error", "Use MM/DD/YYYY-MM/DD/YYYY with real dates; end after start."),
    "S04": ("Renewal Date is not period end + 1 day", "warning", "Set Subscription Renewal Date = period end + 1."),
    "S05": ("Term in yrs differs from the period", "warning",
            "Correct Term in yrs, or accept if the contractual term really differs (co-terminus stub)."),
    "S06": ("ARR is not TCV / Term", "warning", "ARR should be TCV ÷ Term in yrs; ARR 0 means not live yet."),
    "S07": ("Monthly is not ARR / 12", "warning", "Monthly Subscription Revenue should be ARR ÷ 12."),
    "S08": ("TCV Net New + Existing differs from TCV", "warning", "Make TCV - Net New + TCV - Existing = TCV total."),
    "S10": ("New/Existing value", "warning", "Use one of the listed values (and the one allowed in this section)."),
    "S11": ("Status value", "warning", "Use Active, Closed or Churn."),
    "S12": ("Renewal Status value", "warning", "Use one of the standard Renewal Status values."),
    "S13": ("License Category value", "warning", "Use On Premise / Cloud / Hybrid as allowed on this tab."),
    "S14": ("Hybrid class value", "warning", "Use n/a, Perpetual/On Prem, Perpetual/Cloud, On Prem/Cloud or Cloud."),
    "S15": ("Payment Terms value", "info", "Use a standard value (Annual, Upfront, Upfront 3 yrs, ...)."),
    "S16": ("Reseller value", "info", "Use n/a, NTT, Netgo or Mehrwerk."),
    "S17": ("Status and Renewal Status disagree", "warning",
            "A renewed / terminated / amended row is Closed; an active row is not 'Churn' or 'Renewed'."),
    "S18": ("Checklist item open", "warning", "Resolve the control check (To check / mismatched / Missing)."),
    "S19": ("Counter - per Year is not 1", "warning", "Each contract row counts 1."),
    "S20": ("Monthly revenue does not add up to YTD", "warning", "Σ monthly GAAP revenue of the year = YTD column."),
    "S21": ("Customer Number is not a QuickBooks number", "error",
            "Enter the customer's QB number (80000-89999). A placeholder such as 9999 blocks the whole refresh."),
    # cross-workbook / masterlist
    "X01": ("Masterlist name differs from the contracts", "warning", "Correct the name on the Masterlist tab."),
    "X02": ("Customer number missing from the Masterlist", "warning", "Add the customer to the Masterlist tab."),
    "X03": ("Same customer number, different names", "info", "Use one name for a customer number."),
    "X04": ("Maintenance name differs from the Masterlist", "info", "Align the customer name in both workbooks."),
    "X05": ("Converted maintenance customer has no subscription", "warning",
            "A converted customer must appear on the Subscription Summary with the same number."),
    "X06": ("Converted customer not marked Hybrid", "warning",
            "The customer converted (section B / 'Converted') and still pays maintenance: that is a Hybrid account. "
            "Set License Category = Hybrid and If Hybrid - License Class on its rows, or stop the maintenance line."),
    "X07": ("Hybrid customer without maintenance", "warning",
            "License Category is Hybrid with a perpetual class but no maintenance is billed in the register: set the "
            "category to On Premise / Cloud, or add the maintenance line."),
    "X08": ("License Category differs between a customer's rows", "warning",
            "A Hybrid customer is Hybrid on every row (both tabs): correct the rows that say On Premise / Cloud."),
    # maintenance workbook
    "M01": ("Original net is not list + discount", "warning", "Original, net of discount = before discount + discount."),
    "M02": ("Maintenance % outside 15-31 %", "info", "Check the maintenance amount against the licence value."),
    "M03": ("Maintenance fee decreased", "warning", "Accept if it was a deliberate re-price, else correct the year."),
    "M04": ("Fee after an explicit 0 (stopped)", "warning", "A stopped line has 0 then blanks; re-activation is rare."),
    "M05": ("Basis for Budget is not the current-year fee", "warning", "Basis for Budget = current contract year fee."),
    "M06": ("Monthly Revenue is not Basis / 12", "warning", "Monthly Revenue = Basis for Budget ÷ 12."),
    "M07": ("Monthly revenue does not add up to Total Revenue", "warning", "Σ 12 months = YYYY Total Revenue."),
    "M08": ("GRAND TOTAL differs from the rows", "error", "GRAND TOTAL must be the sum of the customer rows."),
    "M09": ("'n/a' in a fee cell", "info", "Leave fee cells blank instead of n/a."),
    "M10": ("Billing Anniversary value", "warning", "Use a month name, 'January - Calendar' or 'not applicable'."),
    # from the importer / reconciliation
    "I1": ("Row not imported", "error", "See the message - the row cannot be imported as it is."),
    "I2": ("Value corrected on import", "warning", "The importer had to correct this - fix it in the sheet."),
    "I3": ("Needs review", "warning", "See the message."),
    "I4": ("Importer assumption", "info", "The importer inferred this - confirm it is right."),
    "R1": ("ARR year cell disagrees with the contract", "warning",
           "ARR - YYYY should be the term's ARR in every calendar year it covers and blank outside it."),
    "R2": ("ARR year cell: open accounting rule", "decision",
           "Depends on the ARR rule for cancellations / renewal transition years - decide, then align the sheet."),
    "R3": ("Future ARR year not filled", "info", "Future years are not filled on the sheet yet."),
    "R4": ("ARR year cell: projected renewal", "info",
           "The contract ended with no renewal on file; the app carries prior ARR + uplift as an estimate. The sheet "
           "and the app agree again once the renewal is signed and recorded."),
}


@dataclass
class Issue:
    rule: str
    workbook: str                       # "subscription" | "maintenance"
    tab: str                            # sheet name as in Excel
    cell: str | None                    # e.g. "AR57" (None = whole row / sheet)
    row: int | None
    message: str
    column: str = ""                    # header of the column
    customer: int | None = None
    current: str | None = None
    expected: str | None = None
    severity: str = ""                  # default from RULES
    status: str = "open"                # open | accepted | reopened
    accepted: dict | None = None        # {reason, by, at} when accepted (or accepted before, value changed)

    def __post_init__(self):
        self.severity = self.severity or RULES[self.rule][1]
        if self.current is not None:
            self.current = str(self.current)[:200]
        if self.expected is not None:
            self.expected = str(self.expected)[:200]

    @property
    def key(self) -> str:
        return f"{self.rule}|{self.workbook}|{self.tab}|{self.cell or self.row or ''}"

    @property
    def fingerprint(self) -> str:
        return hashlib.sha1(f"{self.current}|{self.expected}|{self.message}".encode()).hexdigest()[:16]

    @property
    def id(self) -> str:
        return hashlib.sha1(self.key.encode()).hexdigest()[:12]

    @property
    def title(self) -> str:
        return RULES[self.rule][0]

    @property
    def fix(self) -> str:
        return RULES[self.rule][2]

    @property
    def ref(self) -> str:
        return f"'{self.tab}'!{self.cell}" if self.cell else (f"'{self.tab}' row {self.row}" if self.row else self.tab)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update(id=self.id, key=self.key, title=self.title, fix=self.fix, ref=self.ref, fingerprint=self.fingerprint)
        return d


@dataclass
class CheckResult:
    ran_at: str
    sources: dict                      # workbook -> {path, sha256, modified}
    issues: list[Issue] = field(default_factory=list)

    def counts(self) -> dict:
        out = {s: {"open": 0, "accepted": 0} for s in SEVERITIES}
        for i in self.issues:
            out[i.severity]["accepted" if i.status == "accepted" else "open"] += 1
        return out


def _source_info(path: Path | None) -> dict | None:
    if not path or not Path(path).exists():
        return None
    p = Path(path)
    return {"path": str(p), "name": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "modified": dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")}


def run_check(subscription: Path | None = None, maintenance: Path | None = None, with_import: bool = True,
              out_dir: Path | None = None) -> CheckResult:
    """Run every rule; apply acceptances; save latest-check.json in out_dir (default data/checks)."""
    from . import accept, cross_rules, maintenance_rules, subscription_rules
    from ..importer import maintenance as mimp
    from ..importer.sheet.reader import read_workbook
    subscription = Path(subscription or config.source_workbook())
    if maintenance is None:
        maintenance = config.maintenance_workbook()
    res = CheckResult(dt.datetime.now().isoformat(timespec="seconds"),
                      {"subscription": _source_info(subscription), "maintenance": _source_info(maintenance)})
    wb = read_workbook(subscription)
    ms = mimp.read_register(Path(maintenance)) if maintenance else None
    res.issues += subscription_rules.check(wb)
    if ms is not None:
        res.issues += maintenance_rules.check(ms)
    res.issues += cross_rules.check(wb, ms)
    if with_import:
        from . import importer_findings
        res.issues += importer_findings.check(subscription, maintenance, wb, ms)
    res.issues = _dedupe(res.issues)
    out_dir = Path(out_dir or CHECK_DIR)
    accept.apply(res.issues, out_dir)
    res.issues.sort(key=lambda i: (SEVERITIES.index(i.severity), i.workbook, i.tab, i.row or 0, i.rule))
    save(res, out_dir)
    return res


def _dedupe(issues: list[Issue]) -> list[Issue]:
    seen, out = set(), []
    for i in issues:
        if i.key in seen:
            continue
        seen.add(i.key)
        out.append(i)
    return out


def save(res: CheckResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / LATEST
    p.write_text(json.dumps({"ran_at": res.ran_at, "sources": res.sources,
                             "issues": [i.as_dict() for i in res.issues]}, indent=1, default=str), encoding="utf-8")
    return p


def load(out_dir: Path | None = None) -> CheckResult | None:
    p = Path(out_dir or CHECK_DIR) / LATEST
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    names = set(Issue.__dataclass_fields__)
    issues = [Issue(**{k: v for k, v in x.items() if k in names}) for x in d["issues"]]
    return CheckResult(d["ran_at"], d["sources"], issues)


def sources_changed(res: CheckResult) -> list[str]:
    """Workbooks that changed since the check ran (so the result is out of date)."""
    out = []
    for wb, info in (res.sources or {}).items():
        now = _source_info(Path(info["path"])) if info else None
        if info and (now is None or now["sha256"] != info["sha256"]):
            out.append(wb)
    current = {"subscription": config.source_workbook(), "maintenance": config.maintenance_workbook()}
    for wb, path in current.items():
        info = (res.sources or {}).get(wb)
        if path and info and Path(path).name != info["name"] and wb not in out:
            out.append(wb)
    return out
