"""Map the Subscription Summary workbook (read-only) into the normalised schema.

Every inference, fix and rejection is recorded in the ImportReport with its sheet reference (tab + row).
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from .. import metrics
from ..money import D, D0, ZERO, cents
from ..models import (AccountExec, CheckResult, ControlCheck, ControlCheckType, Customer, Invoice, NonRecurringFee,
                      OpenedVia, Platform, Reseller, Subscription, Term, TermKind, TermSupersession, Termination,
                      TerminationReason)
from .sheet import rules
from .sheet.headers import CHECKLIST_KEYS, LABELS
from .sheet.model import ContractRow, Workbook

PLATFORM = {"onprem": Platform.ON_PREM, "cloud": Platform.CLOUD}
TAB_LABEL = {"onprem": "On-Prem", "cloud": "Cloud"}
KIND = {"A": TermKind.INITIAL, "B": TermKind.CONVERSION, "C": TermKind.ADD_ON, "D": TermKind.RENEWAL}
RESELLER_RATES = {"NTT": 0.10, "Mehrwerk": 0.075, "Netgo": None}
OUTLOOK_RE = re.compile(r"(?i)^(for renewal|not due for renewal|see 20\d\d|invoiced but pending doc)")


_NAME_NOISE = re.compile(r"\b(inc|llc|corp|corporation|incorporated|company|co|ltd|limited|gmbh|ag|the|sa|srl)\b")


def _name_key(s) -> str:
    return re.sub(r"[^a-z0-9]", "", _NAME_NOISE.sub("", str(s or "").lower()))


def names_differ(a, b) -> bool:
    """True when two customer names are materially different (not just case, punctuation or legal suffix)."""
    import difflib
    ka, kb = _name_key(a), _name_key(b)
    if not ka or not kb or ka == kb or ka.startswith(kb) or kb.startswith(ka) or ka[:5] == kb[:5]:
        return False
    return difflib.SequenceMatcher(None, ka, kb).ratio() < 0.6


def ref(c: ContractRow) -> str:
    return f"{TAB_LABEL[c.tab]} {c.section}{c.row}"


@dataclass
class Note:
    severity: str        # fix | inference | warning | rejected
    ref: str
    customer: int | None
    message: str


@dataclass
class ImportReport:
    source: str = ""
    source_sha256: str = ""
    started: str = ""
    sheet_counts: dict = field(default_factory=dict)
    db_counts: dict = field(default_factory=dict)
    notes: list[Note] = field(default_factory=list)
    reconciliation: list[dict] = field(default_factory=list)
    reconciliation_totals: dict = field(default_factory=dict)
    maintenance_source: str = ""
    maintenance_totals: dict = field(default_factory=dict)   # year -> [sheet GRAND TOTAL, imported]
    maintenance_sheet_revenue: dict = field(default_factory=dict)   # calendar year -> sheet Σ Total Revenue
    maintenance_sheet_deferred: dict = field(default_factory=dict)  # calendar year -> sheet Σ 12/31 Deferred
    maintenance_revenue: dict = field(default_factory=dict)  # year -> [sheet rev, db rev, sheet deferred, db deferred]
    maintenance_invoices: dict = field(default_factory=dict)
    maintenance_only: bool = False

    def add(self, severity, c: ContractRow | None, message, customer=None, where=None):
        self.notes.append(Note(severity, where or (ref(c) if c else ""),
                               customer or (c.customer_number if c else None), message))


def _check_result(raw) -> tuple[CheckResult, str | None]:
    s = str(raw).strip()
    low = s.lower()
    if low in ("yes", "y"):
        return CheckResult.YES, None
    if low.startswith("done") or low.endswith("done"):
        return CheckResult.DONE, (s if low != "done" else None)
    if low == "to check":
        return CheckResult.TO_CHECK, None
    if low == "mismatched":
        return CheckResult.MISMATCHED, None
    if low == "missing":
        return CheckResult.MISSING, None
    if low in ("n/a", "na"):
        return CheckResult.NA, None
    return CheckResult.OTHER, s[:200]


def _reseller_name(raw) -> str | None:
    s = str(raw or "").strip().lower()
    if not s or s in ("n/a", "none", "na", "-"):
        return None
    for name in RESELLER_RATES:
        if name.lower() in s:
            return name
    return str(raw).strip()


def _text(v, maxlen=200) -> str | None:
    if v is None:
        return None
    if isinstance(v, (dt.datetime, dt.date)):
        return v.strftime("%m/%d/%Y")
    s = str(v).strip()
    return s[:maxlen] or None


class Mapper:
    def __init__(self, session: Session, wb: Workbook, report: ImportReport, today: dt.date):
        self.s = session
        self.wb = wb
        self.r = report
        self.today = today
        self.customers: dict[int, Customer] = {}
        self.aes: dict[str, AccountExec] = {}
        self.resellers: dict[str, Reseller] = {}
        self.term_by_row: dict[tuple[str, int], Term] = {}
        self.row_by_term: dict[int, ContractRow] = {}
        self.subs: dict[tuple[int, str], list[Subscription]] = defaultdict(list)

    # ------------------------------------------------------------------ lookups
    def customer(self, number: int, name: str = "") -> Customer:
        c = self.customers.get(number)
        if c is None:
            c = Customer(qb_number=number, name=(self.wb.masterlist.get(number) or name or f"Customer {number}"))
            self.s.add(c)
            self.customers[number] = c
        return c

    def ae(self, initials) -> AccountExec | None:
        s = str(initials or "").strip().upper()
        if not s or s in ("N/A", "NONE"):
            return None
        if s not in self.aes:
            self.aes[s] = AccountExec(initials=s)
            self.s.add(self.aes[s])
        return self.aes[s]

    def reseller(self, raw) -> Reseller | None:
        name = _reseller_name(raw)
        if not name:
            return None
        if name not in self.resellers:
            self.resellers[name] = Reseller(name=name, commission_rate=RESELLER_RATES.get(name))
            self.s.add(self.resellers[name])
        return self.resellers[name]

    def check_types(self):
        for i, key in enumerate(CHECKLIST_KEYS):
            label, owner = LABELS[key], None
            for t in self.wb.tabs.values():
                sec = t.sections.get("A")
                col = sec.colmap.get(key) if sec else None
                if col:
                    label = (sec.header_text.get(key) or label).strip()[:160]
                    owner = owner or t.reviewers.get(col)
            self.s.add(ControlCheckType(code=key, label=label, owner=owner, sort=i))

    # ------------------------------------------------------------------ main
    def run(self):
        self.check_types()
        for number, name in self.wb.masterlist.items():
            self.customer(number, name)
        for tab in self.wb.tabs.values():
            self.r.sheet_counts[TAB_LABEL[tab.key]] = {s: sum(1 for c in tab.contracts if c.section == s)
                                                       for s in "ABCDE"}
            self.map_tab(tab)
        self.apply_contract_names()
        self.s.flush()

    def apply_contract_names(self):
        """The customer name comes from its most recent contract row (the legal contract name); the Masterlist name
        is only a fallback for customers without contracts. Material differences are reported for accounting."""
        rows: dict[int, list[ContractRow]] = defaultdict(list)
        for t in self.wb.tabs.values():
            for c in t.contracts:
                if c.section != "E" and c.customer_name:
                    rows[c.customer_number].append(c)
        for number, rs in rows.items():
            cust = self.customers.get(number)
            if cust is None:
                continue
            latest = max(rs, key=lambda c: (rules.parse_date(c.values.get("signed_date")) or dt.date.min,
                                            c.period.start or dt.date.min, c.row))
            name = " ".join(latest.customer_name.split()).upper()
            ml = self.wb.masterlist.get(number)
            if ml and names_differ(ml, name):
                cust.notes = f"Masterlist name: {ml}"
                self.r.add("warning", latest, f"Masterlist names {number} '{ml}' but the contracts say '{name}' - "
                                              "using the contract name; please correct the Masterlist.")
            cust.name = name

    def map_tab(self, tab):
        by_cust: dict[int, list[ContractRow]] = defaultdict(list)
        seen = {}
        for c in tab.contracts:
            key = (c.section, c.customer_number, str(c.values.get("period")).strip(), round(c.arr, 2))
            if key in seen:
                self.r.add("rejected", c, f"Duplicate of {seen[key]} (same section, customer, period, ARR) - "
                                          "not imported.")
                continue
            seen[key] = ref(c)
            by_cust[c.customer_number].append(c)
        for number, rows in by_cust.items():
            self.map_customer(tab, number, rows)
        # supersessions need all terms of the tab first
        for number, rows in by_cust.items():
            for c in rows:
                t = self.term_by_row.get((tab.key, c.row))
                if t is not None and c.section == "D":
                    self.link_renewal(tab, c, t)
            for c in rows:
                t = self.term_by_row.get((tab.key, c.row))
                if t is not None and t.source_ref == ref(c) and                         "amend" in str(c.values.get("renewal_status") or "").lower():
                    self.link_amendment(c, t)
        for number, rows in by_cust.items():
            for c in rows:
                if c.section == "E":
                    self.map_termination(tab, c)

    # ------------------------------------------------------------------ rows -> terms
    def map_customer(self, tab, number: int, rows: list[ContractRow]):
        cust = self.customer(number, rows[0].customer_name)
        if any(c.section == "B" or "perpetual" in str(c.values.get("hybrid_class") or "").lower() for c in rows):
            cust.has_perpetual_license = True
        cancel_dates = sorted(d for d in (self.cancel_date(tab, c) for c in rows if c.section == "E") if d)
        live_rows = [c for c in rows if c.section != "E"]
        # Negative non-E rows are adjustments of another row with the same period (e.g. an amendment that cuts the
        # contract value). Fold them into that row's term instead of storing negative contracts.
        adjustments: dict[int, list[ContractRow]] = defaultdict(list)
        for c in [c for c in live_rows if self._is_negative(c)]:
            per = str(c.values.get("period") or "").strip().removeprefix("Rev ")
            target = next((o for o in live_rows if o is not c and not self._is_negative(o)
                           and str(o.values.get("period") or "").strip().removeprefix("Rev ") == per), None)
            live_rows.remove(c)
            if target is None:
                self.r.add("rejected", c, "Negative row with no matching row to adjust - not imported.")
            else:
                adjustments[target.row].append(c)
        live_rows.sort(key=lambda c: (self.period_for(c)[0] or dt.date.max, "ABCD".index(c.section), c.row))
        for c in live_rows:
            if c.not_subscription:
                self.map_fee(tab, cust, c)
                continue
            start, end, est = self.period_for(c)
            if start is None:
                self.r.add("rejected", c, f"No usable period ('{c.values.get('period')}') and no signed date - "
                                          "not imported.")
                continue
            sub = self.subscription_for(tab, cust, c, start, cancel_dates)
            term = self.make_term(tab, sub, c, start, end, est)
            if term is not None:
                self.term_by_row[(tab.key, c.row)] = term
                for adj in adjustments.get(c.row, []):
                    self.apply_adjustment(tab, term, c, adj)

    @staticmethod
    def _is_negative(c: ContractRow) -> bool:
        tcv = rules.to_number(c.values.get("tcv_total"))
        return (c.arr or 0) < 0 or (tcv is not None and tcv < 0)

    def apply_adjustment(self, tab, term: Term, base: ContractRow, adj: ContractRow):
        delta_tcv = D0(rules.to_number(adj.values.get("tcv_total")))
        delta_arr = D0(adj.arr)
        term.tcv = term.tcv + delta_tcv
        if term.tcv < 0:
            self.r.add("warning", adj, f"Adjustment makes TCV negative ({term.tcv:,.2f}); set to 0.")
            term.tcv = ZERO
        if term.arr_override is not None:
            term.arr_override = max(term.arr_override + delta_arr, ZERO)
        term.is_revised = True
        term.notes = ((term.notes + "; ") if term.notes else "") + (
            f"Adjusted by {ref(adj)}: TCV {delta_tcv:,.2f}, ARR {delta_arr:,.2f} "
            f"({adj.values.get('new_existing') or ''} / {adj.values.get('renewal_status') or ''}).")
        self.term_by_row[(tab.key, adj.row)] = term
        self.r.add("fix", adj, f"Negative adjustment row folded into {ref(base)}: TCV {delta_tcv:,.2f}, "
                               f"ARR {delta_arr:,.2f} -> term ARR {metrics.term_arr(term):,.2f}.")

    def period_for(self, c: ContractRow):
        p = c.period
        if p.ok:
            return p.start, p.end, False
        signed = rules.parse_date(c.values.get("signed_date"))
        rdate = rules.parse_date(c.values.get("renewal_date"))
        years = rules.to_number(c.values.get("term_years")) or 3
        months = round(years * 12)
        if signed:
            y, m = divmod(signed.month - 1 + months, 12)
            end = dt.date(signed.year + y, m + 1, min(signed.day, 28)) - dt.timedelta(days=1)
            return signed, end, True
        if rdate:
            end = rdate - dt.timedelta(days=1)
            y, m = divmod(rdate.month - 1 - months, 12)
            return dt.date(rdate.year + y, m + 1, min(rdate.day, 28)), end, True
        return None, None, True

    def subscription_for(self, tab, cust: Customer, c: ContractRow, start: dt.date, cancel_dates) -> Subscription:
        key = (cust.qb_number, tab.key)
        subs = self.subs[key]
        current = subs[-1] if subs else None
        reopened = current is not None and c.section in "AB" and any(
            d < start and d >= min(t.start_date for t in current.terms) for d in cancel_dates) if current and \
            current.terms else False
        if current is None or reopened:
            opened = OpenedVia.CONVERSION if c.section == "B" else OpenedVia.NET_NEW
            sub = Subscription(customer=cust, platform=PLATFORM[tab.key], opened_via=opened,
                               license_category=_text(c.values.get("license_category"), 40),
                               hybrid_class=_text(c.values.get("hybrid_class"), 60))
            self.s.add(sub)
            subs.append(sub)
            if c.section in "CD":
                self.r.add("inference", c, f"First row of this customer on {TAB_LABEL[tab.key]} is in section "
                                           f"{c.section}; subscription opened as NET_NEW without an initial term.")
            if reopened:
                self.r.add("inference", c, "Starts after an earlier cancellation - opened a new subscription "
                                           "(re-activation).")
            return sub
        if c.section == "B" and current.opened_via == OpenedVia.NET_NEW and not any(
                t.kind == TermKind.INITIAL for t in current.terms):
            current.opened_via = OpenedVia.CONVERSION
        return current

    def make_term(self, tab, sub: Subscription, c: ContractRow, start, end, estimated) -> Term | None:
        v = c.values
        years = rules.to_number(v.get("term_years"))
        months = round(years * 12, 2) if years and years > 0 else metrics.months_between(start, end)
        if not (years and years > 0):
            self.r.add("fix", c, f"Term in yrs '{v.get('term_years')}' unusable - used {months} months from the "
                                 "period.")
        tcv = rules.to_number(v.get("tcv_total"))
        if tcv is None:
            tcv = rules.to_number(v.get("tcv_new")) or rules.to_number(v.get("tcv_existing"))
        sheet_arr = rules.to_number(v.get("arr"))
        if tcv is None:
            if sheet_arr is None:
                self.r.add("rejected", c, "No TCV and no ARR - not imported.")
                return None
            tcv = sheet_arr * months / 12
            self.r.add("fix", c, f"TCV missing - derived {tcv:,.2f} from ARR x term.")
        if tcv < 0:
            self.r.add("fix", c, f"Negative TCV {tcv:,.2f} stored as {abs(tcv):,.2f}.")
            tcv = abs(tcv)
        arr = metrics.computed_arr(tcv, months)
        override = reason = None
        if sheet_arr is not None and abs(D(sheet_arr) - arr) > 1:
            override = cents(max(sheet_arr, 0.0))
            reason = ("Sheet ARR 0 - contract not live / not billed yet" if sheet_arr == 0 else
                      f"Sheet ARR {sheet_arr:,.2f} differs from TCV/term {arr:,.2f}")
            self.r.add("inference", c, reason + " - kept the sheet's ARR as an override.")
        if estimated:
            self.r.add("fix", c, f"Unparsable period '{v.get('period')}' - estimated {start:%m/%d/%Y}-"
                                 f"{end:%m/%d/%Y} from signed/renewal date and term.")
        p = c.period
        for prob in p.problems if p.ok else []:
            self.r.add("fix", c, f"Period: {prob}.")
        kind = KIND[c.section]
        ne = rules.canonical(v.get("new_existing"), rules.NEW_EXISTING)
        if ne == "Amendment" or (p.revised and c.section in "CD"):
            kind = TermKind.AMENDMENT if c.section == "D" else kind
        golive = v.get("golive")
        rs = _text(v.get("renewal_status"), 120)
        t = Term(
            subscription=sub, kind=kind,
            signed_date=rules.parse_date(v.get("signed_date")), start_date=start, end_date=end,
            tcv=cents(tcv), term_months=months, arr_override=override, arr_override_reason=reason,
            is_revised=bool(p.revised), payment_terms=_text(v.get("payment_terms"), 60), ae=self.ae(v.get("ae")),
            reseller=self.reseller(v.get("reseller")),
            golive_date=rules.parse_date(golive) if isinstance(golive, (dt.date, dt.datetime)) else None,
            golive_note=None if isinstance(golive, (dt.date, dt.datetime)) else _text(golive),
            transactional_volume=_text(v.get("transactional_volume"), 60),
            renewal_note=rs if rs and OUTLOOK_RE.match(rs) else None,
            billing_period=_text(v.get("billing_period"), 60), recent_invoice=_text(v.get("recent_invoice"), 120),
            next_action=_text(v.get("review_by_tin")),
            source_ref=ref(c),
            notes=None,
        )
        self.s.add(t)
        for key in CHECKLIST_KEYS:
            raw = v.get(key)
            if raw is None or not str(raw).strip():
                continue
            res, note = _check_result(raw)
            t.checks.append(ControlCheck(type_code=key, result=res, note=note))
        for k, val in v.items():
            m = re.fullmatch(r"bill_amount_(20\d\d)", k)
            if not m:
                continue
            year = int(m.group(1))
            if any(re.fullmatch(fr"bill_month_{year}_\d\d", x) for x in v):
                self.map_monthly_invoices(c, t, v, year)
                continue
            amount = rules.to_number(val)
            date = rules.parse_date(v.get(f"bill_date_{year}"))
            if (amount is None or abs(amount) < 0.005) and not date:
                continue
            if amount is not None and amount < 0:
                self.r.add("fix", c, f"Negative invoice amount {amount:,.2f} ({year}) stored as positive.")
            t.invoices.append(Invoice(billing_year=year, invoice_date=date, amount=cents(abs(amount or 0))))
        return t

    def map_monthly_invoices(self, c: ContractRow, t: Term, v: dict, year: int) -> None:
        """2026 layout: one amount for Jan-June plus one column per month (Jul-Dec), a 'Date Invoiced' cell that may
        list several dates, and a Total Invoice. One invoice per filled amount; its date is the listed date in that
        month (Jan-June: in the first half), else the month-end (Jan-June: none). Zero amounts are skipped."""
        dates, problems = rules.parse_dates(v.get(f"bill_date_{year}"), year)
        if problems:
            self.r.add("fix", c, f"Date Invoiced {year} '{v.get(f'bill_date_{year}')}': {'; '.join(problems)} - "
                       "ignored (month-end used where needed).")
        pieces = [(f"Jan-Jun {year}", v.get(f"bill_amount_{year}"), range(1, 7), None)]
        for k in sorted(x for x in v if re.fullmatch(fr"bill_month_{year}_\d\d", x)):
            mo = int(k[-2:])
            end = dt.date(year, mo, 1) + dt.timedelta(days=31)
            pieces.append((f"{dt.date(year, mo, 1):%b %Y}", v[k], range(mo, mo + 1), end.replace(day=1) - dt.timedelta(days=1)))
        billed = 0.0
        for label, raw, months, fallback in pieces:
            amount = rules.to_number(raw)
            if amount is None or abs(amount) < 0.005:
                continue
            if amount < 0:
                self.r.add("fix", c, f"Negative invoice amount {amount:,.2f} ({label}) stored as positive.")
            date = next((d for d in dates if d.month in months), fallback)
            billed += abs(amount)
            t.invoices.append(Invoice(billing_year=year, invoice_date=date, amount=cents(abs(amount)),
                                      reference=label))
        total = rules.to_number(v.get(f"bill_total_{year}"))
        if total is not None and abs(total - billed) > 0.01:
            self.r.add("warning", c, f"Invoices {year} add up to {billed:,.2f} but Total Invoice is {total:,.2f}.")

    def map_fee(self, tab, cust: Customer, c: ContractRow):
        p = c.period
        self.s.add(NonRecurringFee(customer=cust, platform=PLATFORM[tab.key],
                                   description=f"{c.values.get('new_existing') or ''} - {c.values.get('renewal_status')}",
                                   amount=cents(abs(rules.to_number(c.values.get("tcv_total")) or 0)),
                                   start_date=p.start, end_date=p.end, source_ref=ref(c)))
        self.r.add("inference", c, "'Not a subscription' - imported as a non-recurring fee (excluded from ARR).")

    # ------------------------------------------------------------------ links
    def _supersede(self, new: Term, old: Term):
        if old is new or any(s.old_term is old for s in new.supersedes):
            return
        self.s.add(TermSupersession(new_term=new, old_term=old))

    def link_renewal(self, tab, c: ContractRow, new: Term):
        sec = tab.sections["D"]
        from openpyxl.utils import get_column_letter
        letter = get_column_letter(sec.colmap["arr"]) if "arr" in sec.colmap else "AE"
        raw = c.raw.get("prior_arr")
        linked = []
        if isinstance(raw, str) and raw.startswith("="):
            for r in re.findall(rf"(?<![A-Z]){letter}\$?(\d+)", raw):
                old = self.term_by_row.get((tab.key, int(r)))
                old_row = tab.row(int(r))
                if old is None or old_row is None:
                    continue
                if old_row.customer_number != c.customer_number:
                    self.r.add("warning", c, f"Previous-ARR formula {raw} points to {ref(old_row)} of customer "
                                             f"{old_row.customer_number} - ignored; matched by dates instead.")
                    continue
                linked.append(old)
        if not linked:
            for old in new.subscription.terms:
                if old is new or old.start_date >= new.start_date or old.superseded_by:
                    continue
                if abs((new.start_date - (old.end_date + dt.timedelta(days=1))).days) <= 31:
                    linked.append(old)
            if linked:
                self.r.add("inference", c, "Renewal linked by dates to " +
                           ", ".join(o.source_ref for o in linked) + ".")
        if not linked:
            self.r.add("warning", c, "Renewal with no predecessor term found (no formula link, nothing ending within "
                                     "31 days before its start).")
        for old in linked:
            self._supersede(new, old)

    def link_amendment(self, c: ContractRow, old: Term):
        cands = [t for t in old.subscription.terms if t is not old and old.start_date < t.start_date
                 <= old.end_date + dt.timedelta(days=31)]
        if cands:
            new = min(cands, key=lambda t: t.start_date)
            self._supersede(new, old)
            self.r.add("inference", c, f"'Amended' row superseded by {new.source_ref}.")
        else:
            self.r.add("warning", c, "Renewal Status 'Amended' but no replacing term found.")

    # ------------------------------------------------------------------ terminations
    def cancel_date(self, tab, c: ContractRow) -> dt.date | None:
        header = (tab.sections["E"].header_text.get("signed_date") or "").lower()
        if "cancel" in header:
            d = rules.parse_date(c.values.get("signed_date"))
            if d:
                return d
        m = rules._DATE_RE.search(str(c.values.get("renewal_status") or ""))
        if m and "cancel" in str(c.values.get("renewal_status")).lower():
            return rules._mkdate(m, [])
        p = c.period
        return p.end if p.ok else rules.parse_date(c.values.get("signed_date"))

    def map_termination(self, tab, c: ContractRow):
        subs = self.subs.get((c.customer_number, tab.key))
        if not subs:
            self.r.add("rejected", c, "Terminated (E) row for a customer with no subscription on this tab.")
            return
        cancelled = self.cancel_date(tab, c)
        if cancelled is None:
            self.r.add("rejected", c, "Terminated (E) row without a usable cancellation date.")
            return
        sub = next((s for s in reversed(subs) if s.terms and s.terms[0].start_date <= cancelled), subs[-1])
        if sub.terminations:
            self.r.add("inference", c, f"Second E row for the same subscription - kept the cancellation "
                                       f"{max(sub.terminations[0].cancelled_on, cancelled):%m/%d/%Y}.")
            sub.terminations[0].cancelled_on = max(sub.terminations[0].cancelled_on, cancelled)
            return
        text = f"{c.values.get('status') or ''} {c.values.get('renewal_status') or ''}".lower()
        reason = (TerminationReason.FINAL_PERIOD if "final period" in text else
                  TerminationReason.COLLECTIONS if "collection" in text else
                  TerminationReason.CANCELLED if "cancel" in text else
                  TerminationReason.TERMINATED if "terminat" in text else TerminationReason.CHURN)
        period = str(c.values.get("period") or "").strip()
        last = next((t for t in sub.terms if t.source_ref and
                     str(self._period_text(t)) == period.removeprefix("Rev ")), None)
        if last is None:
            started = [t for t in sub.terms if t.start_date <= cancelled]
            last = max(started, key=lambda t: t.start_date) if started else None
        if c.arr > 0:
            self.r.add("fix", c, "E row had a positive ARR; E amounts are not stored (termination is an event).")
        self.s.add(Termination(subscription=sub, cancelled_on=cancelled, reason=reason,
                               note=_text(c.values.get("renewal_status")), source_ref=ref(c)))
        self._pending_last = getattr(self, "_pending_last", [])
        self._pending_last.append((sub.terminations[-1], last))
        later = [t for t in sub.terms if t.start_date > cancelled]
        if later:
            self.r.add("warning", c, f"Cancelled {cancelled:%m/%d/%Y} but {len(later)} term(s) start later: " +
                       ", ".join(t.source_ref for t in later))

    @staticmethod
    def _period_text(t: Term) -> str:
        return rules.format_period(t.start_date, t.end_date)

    def finish(self):
        self.s.flush()
        for term_row, last in getattr(self, "_pending_last", []):
            term_row.last_term_id = last.id if last else None
        self.s.flush()
