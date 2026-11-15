"""Everything the spreadsheet stored as columns/subtotals is computed here (spec §5).

Conventions kept from the sheet so numbers reconcile:
- ARR of a term = TCV / (term_months / 12) (or the legacy override).
- A term contributes its full ARR to every calendar year its period touches (not prorated), except years from the
  start year of a term that supersedes it, and years after the subscription's cancellation year.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import Customer, MaintenanceContract, OpenedVia, Platform, Subscription, Term, Termination, TermKind
from .money import D, D0, ZERO, cents
from .importer.sheet.rules import months_between  # noqa: F401  (one definition; metrics.months_between is public)


def term_arr(t: Term) -> Decimal:
    if t.arr_override is not None:
        return D(t.arr_override)
    return computed_arr(t.tcv, t.term_months)


def computed_arr(tcv, term_months) -> Decimal:
    """TCV / (months / 12) in Decimal at full precision (rounded only for display)."""
    return D0(tcv) * 12 / D(term_months) if term_months else ZERO


def cancel_date(sub: Subscription) -> dt.date | None:
    """Cancellation of the whole subscription (terminations that name no specific contracts)."""
    return max((x.cancelled_on for x in sub.terminations if not x.scope), default=None)


def term_cancel_date(t: Term) -> dt.date | None:
    """Cancellation that applies to this term: the whole subscription's, or one that names this contract."""
    return max((x.cancelled_on for x in t.subscription.terminations
                if not x.scope or t.id in x.term_ids), default=None)


def ended_early(t: Term) -> bool:
    """This contract alone was terminated (the rest of the subscription keeps running)."""
    return any(x.scope and t.id in x.term_ids for x in t.subscription.terminations)


def superseded_from(t: Term) -> dt.date | None:
    starts = [s.new_term.start_date for s in t.superseded_by]
    return min(starts) if starts else None


def effective_from(t: Term) -> dt.date:
    """Point-in-time start: a term that replaces others (renewal, amendment, merge) is the active one from its signed
    date when it was signed before its period starts. Calendar-year ARR and revenue still use start_date."""
    if t.supersedes and t.signed_date and t.signed_date < t.start_date:
        return t.signed_date
    return t.start_date


def replaced_on(t: Term) -> dt.date | None:
    """Date from which a newer signed term is the active one instead of this term."""
    dates = [effective_from(s.new_term) for s in t.superseded_by]
    return min(dates) if dates else None


def term_status(t: Term, on: dt.date) -> str:
    cd = term_cancel_date(t)
    rep = replaced_on(t)
    if rep and rep <= on:
        return "SUPERSEDED"
    if cd and cd < on and t.start_date <= cd:
        return "CANCELLED"
    if effective_from(t) > on:
        return "SIGNED"
    if t.end_date < on:
        return "ENDED"
    return "ACTIVE"


def term_year_arr(t: Term, year: int, as_of: dt.date | None = None) -> Decimal:
    """ARR of a term in a calendar year. as_of (normally today): a term counts as ARR only once it is in force
    (signed but not started = CARR only, see carr_year), and only terms in force replace it. as_of None = the
    contracted view (CARR): every signed term."""
    if year < t.start_date.year or year > t.end_date.year:
        return ZERO
    if as_of is not None and effective_from(t) > as_of:
        return ZERO
    if as_of is None:
        sup = superseded_from(t)
    else:
        starts = [x.new_term.start_date for x in t.superseded_by if effective_from(x.new_term) <= as_of]
        sup = min(starts) if starts else None
    if sup and year >= sup.year:
        return ZERO
    cd = term_cancel_date(t)
    if cd and t.start_date <= cd and year > cd.year:      # terms signed after a cancellation are unaffected
        return ZERO
    return term_arr(t)


def is_live(t: Term, on: dt.date) -> bool:
    return term_status(t, on) == "ACTIVE" and term_arr(t) > 0


def subscription_status(sub: Subscription, on: dt.date) -> str:
    cd = cancel_date(sub)
    if cd and not any(t.start_date > cd for t in sub.terms):
        return "CHURNED" if cd < on else "ENDING"          # ENDING = cancellation scheduled
    if any(term_status(t, on) in ("ACTIVE", "SIGNED") for t in sub.terms):
        return "ACTIVE"
    return "ENDED"


def _customer_query():
    """Customers with everything the ARR computations read, loaded up front (no query per term)."""
    return select(Customer).options(
        selectinload(Customer.subscriptions).selectinload(Subscription.terms).selectinload(Term.superseded_by),
        selectinload(Customer.subscriptions).selectinload(Subscription.terms).selectinload(Term.supersedes),
        selectinload(Customer.subscriptions).selectinload(Subscription.terminations).selectinload(Termination.scope),
        selectinload(Customer.maintenance_contracts).selectinload(MaintenanceContract.fees),
        selectinload(Customer.maintenance_contracts).selectinload(MaintenanceContract.purchases))


def load_customers(session: Session) -> list[Customer]:
    return list(session.scalars(_customer_query().order_by(Customer.qb_number)))


def load_customers_with_invoices(session: Session) -> list[Customer]:
    """load_customers plus each term's invoices (the Deferred Billing page)."""
    q = _customer_query().options(
        selectinload(Customer.subscriptions).selectinload(Subscription.terms).selectinload(Term.invoices))
    return list(session.scalars(q.order_by(Customer.qb_number)))


def load_customer(session: Session, qb: int) -> Customer | None:
    """One customer, loaded like load_customers."""
    return session.scalar(_customer_query().where(Customer.qb_number == qb))


def customer_year_arr(c: Customer, year: int, platform: Platform | None = None,
                      as_of: dt.date | None = None) -> Decimal:
    return sum((term_year_arr(t, year, as_of) for s in c.subscriptions if platform is None or s.platform == platform
               for t in s.terms), ZERO)


def maintenance_year_arr(c: Customer, year: int) -> Decimal:
    """Maintenance ARR of a calendar year = fees of the maintenance year ending in it ("2025-2026" -> 2026)."""
    return sum((f.amount for mc in c.maintenance_contracts for f in mc.fees if f.year == year), ZERO)


def is_converted(c: Customer) -> bool:
    return any(s.opened_via == OpenedVia.CONVERSION or any(t.kind == TermKind.CONVERSION for t in s.terms)
               for s in c.subscriptions)


def maintenance_arr(c: Customer, year: int, as_of: dt.date | None = None) -> Decimal:
    """Maintenance that counts as ARR. A converted customer's maintenance in a year it also has subscription ARR is a
    prepaid leftover, not recurring revenue: 0 (it is still billed - see maintenance_year_arr).
    Years after as_of's year: a signed conversion already counts (confirmed 2026-11-14, Belimo): the maintenance of
    a future year it covers is not ARR from signing, even before the subscription starts. In the current year it
    stops counting once the subscription is in force (point in time)."""
    if is_converted(c):
        future = as_of is not None and year > as_of.year
        if customer_year_arr(c, year, as_of=None if future else as_of) > 0:
            return ZERO
    return maintenance_year_arr(c, year)


def converted_maintenance(c: Customer, year: int, as_of: dt.date | None = None) -> Decimal:
    """The billed maintenance left out of ARR by maintenance_arr (prepaid leftover of a converted customer)."""
    return maintenance_year_arr(c, year) - maintenance_arr(c, year, as_of)


def effective_year_arr(c: Customer, year: int, as_of: dt.date | None = None) -> Decimal:
    """Customer ARR (dashboard, customer page, cohort spec 5.5): subscription + maintenance, except a converted
    customer whose subscription has started counts only the subscription (the prepaid maintenance tail is not ARR)."""
    return customer_year_arr(c, year, as_of=as_of) + maintenance_arr(c, year, as_of)


def carr_year(c: Customer, year: int) -> Decimal:
    """Contracted ARR (CARR) of a calendar year: every signed term, started or not (+ maintenance ARR)."""
    return effective_year_arr(c, year, None)


def year_range(session: Session) -> list[int]:
    lo = session.scalar(select(Term.start_date).order_by(Term.start_date).limit(1))
    hi = session.scalar(select(Term.end_date).order_by(Term.end_date.desc()).limit(1))
    if not lo:
        y = dt.date.today().year
        return [y]
    return list(range(lo.year, min(hi.year, dt.date.today().year + 3) + 1))


def arr_by_year(customers: list[Customer], years: list[int],
                as_of: dt.date | None = None) -> dict[str, dict[int, Decimal]]:
    """Company ARR by year = subscription ARR (both platforms) + maintenance ARR of the year, counting only terms in
    force on as_of; signed-but-not-started terms are CARR ("Signed, not started", "CARR" rows - not in Total).
    Unlike the workbook's spec 5.1, a converted customer's prepaid maintenance is not ARR (shown separately)."""
    out = {"On-Prem": {}, "Cloud": {}, "Subscription": {}, "Maintenance": {}, "Total": {},
           "Converted maintenance": {}, "Signed, not started": {}, "CARR": {}}
    for y in years:
        op = sum((customer_year_arr(c, y, Platform.ON_PREM, as_of) for c in customers), ZERO)
        cl = sum((customer_year_arr(c, y, Platform.CLOUD, as_of) for c in customers), ZERO)
        mt = maintenance_arr_total(customers, y, as_of)
        out["On-Prem"][y], out["Cloud"][y], out["Subscription"][y] = op, cl, op + cl
        out["Maintenance"][y], out["Total"][y] = mt, op + cl + mt
        out["Converted maintenance"][y] = sum((converted_maintenance(c, y, as_of) for c in customers), ZERO)
        carr = sum((carr_year(c, y) for c in customers), ZERO) if as_of is not None else op + cl + mt
        out["CARR"][y], out["Signed, not started"][y] = carr, cents(carr - (op + cl + mt)) or ZERO  # no -0.00
    return out


def maintenance_total(customers: list[Customer], year: int) -> Decimal:
    """Billed maintenance of the year (ties to the workbook GRAND TOTAL)."""
    return sum((maintenance_year_arr(c, year) for c in customers), ZERO)


def maintenance_arr_total(customers: list[Customer], year: int, as_of: dt.date | None = None) -> Decimal:
    """Maintenance that counts as ARR (converted customers' prepaid leftover excluded)."""
    return sum((maintenance_arr(c, year, as_of) for c in customers), ZERO)


def arr_at(customers: list[Customer], on: dt.date) -> Decimal:
    return sum((term_arr(t) for c in customers for s in c.subscriptions for t in s.terms if is_live(t, on)), ZERO)


def carr_at(customers: list[Customer], on: dt.date) -> Decimal:
    """Contracted ARR: live ARR + terms signed but not yet started (excluding ones that replace a live term)."""
    signed = ZERO
    for c in customers:
        for s in c.subscriptions:
            for t in s.terms:
                if term_status(t, on) == "SIGNED" and not t.supersedes:
                    signed += term_arr(t)
    return arr_at(customers, on) + signed


@dataclass
class DueRow:
    term: Term
    customer: Customer
    days: int                   # negative = renewal overdue (term already ended)

    @property
    def overdue(self) -> bool:
        return self.days < 0


def open_terms(sub: Subscription) -> list[Term]:
    """Terms nothing has superseded yet (contracts terminated on their own are not renewed or merged)."""
    return [t for t in sub.terms if not t.superseded_by and not ended_early(t)]


def latest_open_term(sub: Subscription) -> Term | None:
    """The term a renewal of this subscription should replace (newest end date among open terms)."""
    terms = open_terms(sub)
    return max(terms, key=lambda t: (t.end_date, t.start_date)) if terms else None


def cancels_term(t: Term) -> bool:
    """True when a cancellation applies to this term (terms started after it are re-signings)."""
    cd = term_cancel_date(t)
    return bool(cd and t.start_date <= cd)


def renewal_overdue(sub: Subscription, on: dt.date, grace_days: int = 365) -> bool:
    """Subscription ran out without a renewal or a termination on file, within the last `grace_days`."""
    if subscription_status(sub, on) != "ENDED":
        return False
    last = latest_open_term(sub)
    return (last is not None and not cancels_term(last)
            and on - dt.timedelta(days=grace_days) <= last.end_date < on)


def renewals_due(customers: list[Customer], on: dt.date, days: int = 365,
                 overdue_days: int = 365) -> list[DueRow]:
    """Terms ending in the next `days` with nothing replacing them, plus lapsed subscriptions whose last term ended
    in the past `overdue_days` without a renewal or termination (renewal overdue). Overdue rows first."""
    out = []
    for c in customers:
        for s in c.subscriptions:
            lapsed = renewal_overdue(s, on, overdue_days)
            for t in open_terms(s):
                if term_arr(t) <= 0 or cancels_term(t):
                    continue
                if on <= t.end_date <= on + dt.timedelta(days=days):
                    out.append(DueRow(t, c, (t.end_date - on).days))
                elif lapsed and on - dt.timedelta(days=overdue_days) <= t.end_date < on:
                    out.append(DueRow(t, c, (t.end_date - on).days))
    return sorted(out, key=lambda d: (not d.overdue, d.term.end_date))


OUTCOMES = {
    "OVERDUE": "Overdue — ended, nothing on file",
    "DUE": "Due — renewal not yet on file",
    "RENEWED": "Renewed",
    "NOT_RENEWING": "Not renewing — cancellation on file",
    "ADDON_ENDED": "Add-on ended (subscription continues)",
}
OPEN_OUTCOMES = ("OVERDUE", "DUE")


@dataclass
class WorkRow:
    term: Term
    customer: Customer
    days: int                     # days from today to the renewal date (day after the term ends); negative = past
    outcome: str
    successors: list[Term]
    cancelled_on: dt.date | None

    @property
    def renewal_date(self) -> dt.date:
        return self.term.end_date + dt.timedelta(days=1)

    @property
    def is_open(self) -> bool:
        return self.outcome in OPEN_OUTCOMES

    @property
    def successor_arr(self) -> Decimal:
        return sum((term_arr(t) for t in self.successors), ZERO)


def renewal_worklist(customers: list[Customer], on: dt.date, back_days: int = 365, ahead_days: int = 120,
                     platform: str | None = None, outcomes: set[str] | None = None) -> list[WorkRow]:
    """Every term whose renewal date falls between `back_days` ago and `ahead_days` ahead, with what happened:
    renewed (superseded), not renewing (cancellation on file), overdue, due, or an add-on that ended while the
    subscription continues. Open items first, then by renewal date."""
    lo, hi = on - dt.timedelta(days=back_days), on + dt.timedelta(days=ahead_days)
    out = []
    for c in customers:
        for s in c.subscriptions:
            if platform and s.platform.value != platform:
                continue
            for t in s.terms:
                rd = t.end_date + dt.timedelta(days=1)
                if not (lo <= rd <= hi) or term_arr(t) <= 0:
                    continue
                successors = [x.new_term for x in t.superseded_by]
                if successors:
                    outcome = "RENEWED"
                elif cancels_term(t):
                    outcome = "NOT_RENEWING"
                elif t.end_date >= on:
                    outcome = "DUE"
                elif t.kind == TermKind.ADD_ON and subscription_status(s, on) == "ACTIVE":
                    outcome = "ADDON_ENDED"
                else:
                    outcome = "OVERDUE"
                if outcomes and outcome not in outcomes:
                    continue
                out.append(WorkRow(t, c, (rd - on).days, outcome, successors,
                                   cancel_date(s) if outcome == "NOT_RENEWING" else None))
    return sorted(out, key=lambda w: (not w.is_open, w.days))


def new_customers_by_year(customers: list[Customer]) -> dict[int, int]:
    out: dict[int, int] = defaultdict(int)
    for c in customers:
        firsts = [t for s in c.subscriptions if s.opened_via == OpenedVia.NET_NEW
                  for t in s.terms if t.kind == TermKind.INITIAL]
        if firsts:
            first = min(firsts, key=lambda t: t.signed_date or t.start_date)
            out[(first.signed_date or first.start_date).year] += 1
    return dict(sorted(out.items()))


@dataclass
class Retention:
    year: int
    cohort: int
    start_arr: Decimal
    end_arr: Decimal
    churned: int
    churned_arr: Decimal
    downgrade_arr: Decimal
    expansion_arr: Decimal

    @property
    def grr(self) -> Decimal | None:
        return (self.start_arr - self.churned_arr - self.downgrade_arr) / self.start_arr if self.start_arr else None

    @property
    def nrr(self) -> Decimal | None:
        return self.end_arr / self.start_arr if self.start_arr else None


def retention(customers: list[Customer], year: int, as_of: dt.date | None = None) -> Retention:
    """Cohort on effective ARR (subscription + maintenance, conversion rule applied); terms in force on as_of."""
    r = Retention(year, 0, ZERO, ZERO, 0, ZERO, ZERO, ZERO)
    for c in customers:
        a, b = effective_year_arr(c, year - 1, as_of), effective_year_arr(c, year, as_of)
        if a <= 0:
            continue
        r.cohort += 1
        r.start_arr += a
        r.end_arr += b
        if b <= 0:
            r.churned += 1
            r.churned_arr += a
        elif b < a:
            r.downgrade_arr += a - b
        else:
            r.expansion_arr += b - a
    return r


def monthly_schedule(t: Term) -> list[tuple[dt.date, Decimal]]:
    """Straight-line monthly revenue for the term's months (stops at supersession / cancellation)."""
    cd = term_cancel_date(t)
    stop = min(filter(None, [t.end_date, superseded_from(t) and superseded_from(t) - dt.timedelta(days=1),
                             cd if cd and t.start_date <= cd else None]))
    out = []
    y, m = t.start_date.year, t.start_date.month
    while (y, m) <= (stop.year, stop.month):
        out.append((dt.date(y, m, 1), term_arr(t) / 12))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ============================================================================== maintenance (MAINTENANCE-WORKBOOK.md)
# A fee keyed K ("K-1 - K") covers the contract year starting at the anniversary month of K-1 and ending the month
# before it in K. Calendar billing = anniversary January, so K covers January-December of K-1.

def anniversary_of(mc) -> int:
    return mc.anniversary_month or 1


def contract_year_key(mc, on: dt.date) -> int:
    """End-year key of the contract year in force on a date."""
    a = anniversary_of(mc)
    return (on.year if on.month >= a else on.year - 1) + 1


def contract_year_span(mc, key: int) -> tuple[dt.date, dt.date]:
    a = anniversary_of(mc)
    start = dt.date(key - 1, a, 1)
    end = dt.date(key, a, 1) - dt.timedelta(days=1)
    return start, end


def year_label(key: int) -> str:
    return f"{key - 1}-{key}"


def fee_map(mc) -> dict[int, Decimal]:
    return {f.year: f.amount for f in mc.fees}


def fee_for(mc, key: int) -> Decimal | None:
    """None = blank (not billed / not entered); 0 = explicit stop."""
    return fee_map(mc).get(key)


def maintenance_month_revenue(mc, year: int, month: int) -> Decimal:
    return (fee_for(mc, contract_year_key(mc, dt.date(year, month, 1))) or ZERO) / 12


def maintenance_calendar_revenue(mc, year: int) -> Decimal:
    """GL 4300: revenue recognised in a calendar year (straight line, steps at the anniversary)."""
    return sum((maintenance_month_revenue(mc, year, m) for m in range(1, 13)), ZERO)


def maintenance_deferred(mc, year: int) -> Decimal:
    """GL 2310 at 12/31: unrecognised months of the contract year in force on 12/31."""
    key = contract_year_key(mc, dt.date(year, 12, 31))
    _, end = contract_year_span(mc, key)
    months_left = (end.year - year) * 12 + end.month - 12
    return (fee_for(mc, key) or ZERO) * months_left / 12


@dataclass
class MaintPricing:
    net_license: Decimal
    additional_license: Decimal
    carrier_purchases: Decimal
    total_current_license: Decimal
    original_maintenance: Decimal
    additional_maintenance: Decimal
    total_maintenance: Decimal
    increase: Decimal            # cumulative uplift = total - original - additional
    maintenance_pct: Decimal | None
    current_key: int
    current_fee: Decimal | None
    next_fee: Decimal | None
    prior_fee: Decimal | None
    status: str                  # ACTIVE / STOPS_NEXT_YEAR / STOPPED / NOT_LIVE / NO_FEES / ENDED


def maintenance_pricing(mc, on: dt.date) -> MaintPricing:
    from .models import PurchaseKind
    net = sum((p.amount for p in mc.purchases if p.kind == PurchaseKind.ORIGINAL), ZERO)
    addl = sum((p.amount for p in mc.purchases if p.kind in (PurchaseKind.MODULE, PurchaseKind.PLANT,
                                                             PurchaseKind.DECOMMISSION)), ZERO)
    carrier = sum((p.amount for p in mc.purchases if p.kind == PurchaseKind.CARRIER), ZERO)
    orig = sum((p.maintenance_amount or ZERO for p in mc.purchases if p.kind == PurchaseKind.ORIGINAL), ZERO)
    add_m = sum((p.maintenance_amount or ZERO for p in mc.purchases if p.kind != PurchaseKind.ORIGINAL), ZERO)
    fees = fee_map(mc)
    paid = [y for y, a in sorted(fees.items()) if a > 0]
    total = fees[paid[-1]] if paid else ZERO
    key = contract_year_key(mc, on)
    cur, nxt, prv = fees.get(key), fees.get(key + 1), fees.get(key - 1)
    tcl = net + addl
    if mc.ended_on and mc.ended_on <= on:
        status = "ENDED"
    elif cur and nxt == 0:
        status = "STOPS_NEXT_YEAR"
    elif cur == 0 or (not cur and paid and paid[-1] < key):
        status = "STOPPED"
    elif not mc.go_live and not paid:
        status = "NOT_LIVE"
    elif not paid:
        status = "NO_FEES"
    else:
        status = "ACTIVE"
    return MaintPricing(net, addl, carrier, tcl, orig, add_m, total, total - orig - add_m,
                        (total / tcl) if tcl else None, key, cur, nxt, prv, status)
