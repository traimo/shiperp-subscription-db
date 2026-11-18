"""Domain operations. Each runs inside one transaction, validates, writes an audit entry, and can be previewed
(run, summarise, roll back) before it is committed."""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, metrics
from .money import D, cents
from .models import (AccountExec, AuditLog, CheckResult, ControlCheck, Customer, Invoice, OpenedVia, Platform,
                     Reseller, Subscription, Term, TermKind, TermSupersession, Termination, TerminationReason,
                     TerminationTerm)


class ServiceError(ValueError):
    pass


@dataclass
class TermInput:
    start_date: dt.date
    end_date: dt.date
    tcv: Decimal
    signed_date: dt.date | None = None
    term_months: float | None = None          # contractual term; default = months in the period
    payment_terms: str | None = "Annual"
    ae: str | None = None
    reseller: str | None = None
    golive_date: dt.date | None = None
    golive_note: str | None = None
    transactional_volume: str | None = None
    renewal_note: str | None = None
    next_action: str | None = None
    notes: str | None = None

    def __post_init__(self):
        self.tcv = D(self.tcv)

    def validate(self):
        if self.end_date < self.start_date:
            raise ServiceError("End date is before start date.")
        if self.tcv is None or self.tcv < 0:
            raise ServiceError("TCV must be zero or positive.")
        if self.term_months is not None and self.term_months <= 0:
            raise ServiceError("Term (months) must be positive.")

    @property
    def months(self) -> float:
        return self.term_months or metrics.months_between(self.start_date, self.end_date)

    @property
    def arr(self) -> Decimal:
        return metrics.computed_arr(self.tcv, self.months)


@dataclass
class Outcome:
    title: str
    customer_qb: int | None
    lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    arr_before: dict[int, Decimal] = field(default_factory=dict)
    arr_after: dict[int, Decimal] = field(default_factory=dict)
    entity_id: int | None = None
    audit_id: int | None = None               # audit row of a committed change (for its Undo link)


# ------------------------------------------------------------------------------------------------ helpers

def _ae(s: Session, initials: str | None) -> AccountExec | None:
    v = (initials or "").strip().upper()
    if not v:
        return None
    ae = s.scalar(select(AccountExec).where(AccountExec.initials == v))
    if ae is None:
        ae = AccountExec(initials=v)
        s.add(ae)
    return ae


def _reseller(s: Session, name: str | None) -> Reseller | None:
    v = (name or "").strip()
    if not v or v.lower() in ("n/a", "none"):
        return None
    r = s.scalar(select(Reseller).where(Reseller.name == v))
    if r is None:
        r = Reseller(name=v)
        s.add(r)
    return r


def _new_term(s: Session, sub: Subscription, kind: TermKind, inp: TermInput) -> Term:
    inp.validate()
    t = Term(subscription=sub, kind=kind, signed_date=inp.signed_date, start_date=inp.start_date,
             end_date=inp.end_date, tcv=cents(inp.tcv), term_months=round(inp.months, 2),
             payment_terms=inp.payment_terms, ae=_ae(s, inp.ae), reseller=_reseller(s, inp.reseller),
             golive_date=inp.golive_date, golive_note=inp.golive_note,
             transactional_volume=inp.transactional_volume, renewal_note=inp.renewal_note,
             next_action=inp.next_action, notes=inp.notes)
    s.add(t)
    return t


def _years(today: dt.date) -> list[int]:
    return list(range(today.year - 2, today.year + 4))


def _arr_snapshot(c: Customer, today: dt.date) -> dict[int, Decimal]:
    return {y: cents(metrics.customer_year_arr(c, y)) for y in _years(today)}


def _audit(s: Session, actor: str, action: str, entity: str, entity_id, qb, summary: str, detail=None) -> AuditLog:
    row = AuditLog(at=dt.datetime.now(), actor=actor, action=action, entity=entity, entity_id=entity_id,
                   customer_qb=qb, summary=summary[:400],
                   detail=json.dumps(detail, default=str) if detail is not None else None)
    s.add(row)
    s.info.setdefault("audit_rows", []).append(row)          # services.run attaches the undo records to the first
    return row


def get_customer(s: Session, qb: int) -> Customer:
    c = s.scalar(select(Customer).where(Customer.qb_number == qb))
    if c is None:
        raise ServiceError(f"Customer {qb} not found.")
    return c


def get_term(s: Session, term_id: int) -> Term:
    t = s.get(Term, term_id)
    if t is None:
        raise ServiceError(f"Term {term_id} not found.")
    return t


# ------------------------------------------------------------------------------------------------ operations

def create_customer(s: Session, actor: str, qb: int, name: str) -> Outcome:
    if not 80000 <= qb <= 89999:
        raise ServiceError("Customer number must be an 80xxx QuickBooks number.")
    if s.scalar(select(Customer).where(Customer.qb_number == qb)):
        raise ServiceError(f"Customer {qb} already exists.")
    if not name.strip():
        raise ServiceError("Customer name is required.")
    c = Customer(qb_number=qb, name=name.strip().upper())
    s.add(c)
    s.flush()
    _audit(s, actor, "create", "customer", c.id, qb, f"Created customer {qb} {c.name}")
    return Outcome(f"New customer {qb}", qb, [f"Customer {qb} {c.name}"], entity_id=c.id)


def new_subscription(s: Session, actor: str, qb: int, platform: Platform, opened_via: OpenedVia, inp: TermInput,
                     today: dt.date | None = None) -> Outcome:
    today = today or dt.date.today()
    c = get_customer(s, qb)
    out = Outcome(f"{'Net new' if opened_via == OpenedVia.NET_NEW else 'Conversion'} subscription {qb}", qb,
                  arr_before=_arr_snapshot(c, today))
    existing = [x for x in c.subscriptions if x.platform == platform
                and metrics.subscription_status(x, today) in ("ACTIVE", "ENDING")]
    if existing:
        raise ServiceError(f"Customer already has an active {platform.value} subscription - use Add-on or Renew.")
    if opened_via == OpenedVia.NET_NEW and any(x.terms for x in c.subscriptions):
        out.warnings.append("Customer had subscriptions before - this counts as a new logo / re-activation, "
                            "not as a new customer for CAC.")
    sub = Subscription(customer=c, platform=platform, opened_via=opened_via)
    s.add(sub)
    kind = TermKind.INITIAL if opened_via == OpenedVia.NET_NEW else TermKind.CONVERSION
    t = _new_term(s, sub, kind, inp)
    if opened_via == OpenedVia.CONVERSION:
        c.has_perpetual_license = True
        out.warnings.append("Reminder: stop the maintenance line in the maintenance workbook.")
    s.flush()
    out.entity_id = t.id
    out.lines.append(f"New {platform.value} subscription, {kind.value} term {inp.start_date:%m/%d/%Y}-"
                     f"{inp.end_date:%m/%d/%Y}, TCV {inp.tcv:,.2f}, ARR {inp.arr:,.2f}")
    _audit(s, actor, "create", "term", t.id, qb, out.lines[-1], {"term": _term_dict(t)})
    out.arr_after = _arr_snapshot(c, today)
    return out


def add_on(s: Session, actor: str, subscription_id: int, inp: TermInput, coterminus_term_id: int | None = None,
           today: dt.date | None = None) -> Outcome:
    today = today or dt.date.today()
    sub = s.get(Subscription, subscription_id)
    if sub is None:
        raise ServiceError("Subscription not found.")
    c = sub.customer
    out = Outcome(f"Add-on for {c.qb_number}", c.qb_number, arr_before=_arr_snapshot(c, today))
    if coterminus_term_id:
        base = get_term(s, coterminus_term_id)
        inp.end_date = base.end_date
        out.lines.append(f"Co-terminus with term {base.id} (ends {base.end_date:%m/%d/%Y})")
    if metrics.subscription_status(sub, today) == "CHURNED":
        raise ServiceError("Subscription is terminated - open a new subscription instead.")
    t = _new_term(s, sub, TermKind.ADD_ON, inp)
    s.flush()
    out.entity_id = t.id
    out.lines.append(f"ADD_ON term {inp.start_date:%m/%d/%Y}-{inp.end_date:%m/%d/%Y}, TCV {inp.tcv:,.2f}, "
                     f"ARR {inp.arr:,.2f}")
    _audit(s, actor, "create", "term", t.id, c.qb_number, out.lines[-1], {"term": _term_dict(t)})
    out.arr_after = _arr_snapshot(c, today)
    return out


def renew(s: Session, actor: str, base_term_id: int, inp: TermInput, rollup_term_ids: list[int] | None = None,
          today: dt.date | None = None) -> Outcome:
    today = today or dt.date.today()
    base = get_term(s, base_term_id)
    sub = base.subscription
    c = sub.customer
    out = Outcome(f"Renewal for {c.qb_number}", c.qb_number, arr_before=_arr_snapshot(c, today))
    if base.superseded_by:
        raise ServiceError("This term was already renewed/amended.")
    if abs((inp.start_date - (base.end_date + dt.timedelta(days=1))).days) > 31:
        raise ServiceError(f"Renewal must start within 31 days of the old term's end ({base.end_date:%m/%d/%Y}).")
    olds = [base] + [get_term(s, i) for i in (rollup_term_ids or []) if i != base.id]
    for o in olds:
        if o.subscription_id != sub.id:
            raise ServiceError(f"Term {o.id} belongs to another subscription.")
        if o.superseded_by:
            raise ServiceError(f"Term {o.id} was already superseded.")
    prior = sum(metrics.term_arr(o) for o in olds)
    t = _new_term(s, sub, TermKind.RENEWAL, inp)
    for o in olds:
        s.add(TermSupersession(new_term=t, old_term=o))
    s.flush()
    out.entity_id = t.id
    delta = inp.arr - prior
    out.lines.append(f"RENEWAL term {inp.start_date:%m/%d/%Y}-{inp.end_date:%m/%d/%Y}, TCV {inp.tcv:,.2f}, "
                     f"ARR {inp.arr:,.2f}")
    out.lines.append("Supersedes " + ", ".join(f"term {o.id} ({o.kind.value}, ARR {metrics.term_arr(o):,.2f})"
                                               for o in olds))
    out.lines.append(f"ARR change {prior:,.2f} -> {inp.arr:,.2f} ({'+' if delta >= 0 else ''}{delta:,.2f}"
                     f"{f', {delta / prior * 100:+.1f}%' if prior else ''})")
    if len(olds) > 1:
        out.lines.append(f"Merged {len(olds)} contracts into one renewal (terms "
                         + ", ".join(str(o.id) for o in olds) + ")")
    _audit(s, actor, "renew", "term", t.id, c.qb_number, "; ".join(out.lines),
           {"term": _term_dict(t), "supersedes": [o.id for o in olds]})
    out.arr_after = _arr_snapshot(c, today)
    return out


def amend(s: Session, actor: str, old_term_id: int, inp: TermInput, today: dt.date | None = None) -> Outcome:
    today = today or dt.date.today()
    old = get_term(s, old_term_id)
    c = old.subscription.customer
    out = Outcome(f"Amendment for {c.qb_number}", c.qb_number, arr_before=_arr_snapshot(c, today))
    if old.superseded_by:
        raise ServiceError("This term was already renewed/amended.")
    if not (old.start_date <= inp.start_date <= old.end_date + dt.timedelta(days=31)):
        raise ServiceError("An amendment must start within the term it amends.")
    t = _new_term(s, old.subscription, TermKind.AMENDMENT, inp)
    t.is_revised = True
    s.add(TermSupersession(new_term=t, old_term=old))
    s.flush()
    out.entity_id = t.id
    out.lines.append(f"AMENDMENT term {inp.start_date:%m/%d/%Y}-{inp.end_date:%m/%d/%Y}, ARR {inp.arr:,.2f} "
                     f"replaces term {old.id} (ARR {metrics.term_arr(old):,.2f}) from {inp.start_date:%m/%d/%Y}")
    _audit(s, actor, "amend", "term", t.id, c.qb_number, out.lines[-1], {"term": _term_dict(t), "old": old.id})
    out.arr_after = _arr_snapshot(c, today)
    return out


def terminate(s: Session, actor: str, subscription_id: int, cancelled_on: dt.date, reason: TerminationReason,
              note: str | None = None, today: dt.date | None = None, term_ids: list[int] | None = None) -> Outcome:
    """Cancel the subscription, or - with term_ids naming only some of its open contracts - end just those contracts
    (the others keep running and keep their ARR)."""
    today = today or dt.date.today()
    sub = s.get(Subscription, subscription_id)
    if sub is None:
        raise ServiceError("Subscription not found.")
    c = sub.customer
    out = Outcome(f"Terminate {c.qb_number} {sub.platform.value}", c.qb_number, arr_before=_arr_snapshot(c, today))
    if metrics.cancel_date(sub) and not any(t.start_date > metrics.cancel_date(sub) for t in sub.terms):
        raise ServiceError("Subscription is already terminated.")
    open_ = [t for t in metrics.open_terms(sub) if not metrics.cancels_term(t)]
    picked = None
    if term_ids is not None:
        ids = {int(i) for i in term_ids}
        if not ids:
            raise ServiceError("Tick at least one contract to terminate.")
        by_id = {t.id: t for t in sub.terms}
        for i in ids:
            t = by_id.get(i)
            if t is None:
                raise ServiceError(f"Term {i} belongs to another subscription.")
            if t not in open_:
                raise ServiceError(f"Term {i} was already renewed, amended or terminated.")
        if {t.id for t in open_} - ids:                     # only some contracts: a partial termination
            picked = sorted((by_id[i] for i in ids), key=lambda t: t.start_date)
    started = [t for t in (picked or sub.terms) if t.start_date <= cancelled_on]
    if not started:
        raise ServiceError("No term has started by the cancellation date.")
    last = max(started, key=lambda t: t.start_date)
    x = Termination(subscription=sub, cancelled_on=cancelled_on, reason=reason, last_term_id=last.id, note=note)
    for t in picked or []:
        x.scope.append(TerminationTerm(term=t))
    s.add(x)
    s.flush()
    out.entity_id = x.id
    if picked:
        keep = [t for t in open_ if t not in picked]
        out.lines.append(f"Ended contract{'s' if len(picked) > 1 else ''} "
                         + ", ".join(f"term {t.id} ({t.kind.value}, ARR {metrics.term_arr(t):,.2f})" for t in picked)
                         + f" on {cancelled_on:%m/%d/%Y} ({reason.value}); ARR of "
                         + ("them" if len(picked) > 1 else "it") + f" stops after {cancelled_on.year}")
        out.lines.append("Still running: " + ", ".join(f"term {t.id} ({t.kind.value})" for t in keep))
    else:
        later = [t for t in sub.terms if t.start_date > cancelled_on]
        if later:
            out.warnings.append(f"{len(later)} term(s) start after the cancellation date and stay active.")
        others = [o for o in c.subscriptions if o is not sub and metrics.subscription_status(o, today) == "ACTIVE"]
        if others:
            out.warnings.append("Customer keeps other active subscriptions (" +
                                ", ".join(o.platform.value for o in others) + ") - this is a partial churn.")
        out.lines.append(f"Cancelled {cancelled_on:%m/%d/%Y} ({reason.value}); ARR stops after {cancelled_on.year}; "
                         f"last term {last.id}")
    _audit(s, actor, "terminate", "subscription", sub.id, c.qb_number, "; ".join(out.lines)[:400],
           {"cancelled_on": cancelled_on, "reason": reason.value, "note": note,
            "terms": [t.id for t in picked] if picked else None})
    out.arr_after = _arr_snapshot(c, today)
    return out


EDITABLE_TERM_FIELDS = ("signed_date", "payment_terms", "golive_date", "golive_note", "transactional_volume",
                        "renewal_note", "billing_period", "recent_invoice", "next_action", "notes", "projected_uplift")


def projected_uplift_value(v) -> Decimal | None:
    """A contract's Projected Renewal uplift as a fraction (0.05 = 5%), or None for the app default. Accepts a Decimal
    / number (fraction) or blank."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        d = D(v)
    except Exception:
        raise ServiceError(f"Projected renewal uplift must be a number, not {v!r}.") from None
    if d is None or not d.is_finite() or not 0 <= d <= D(config.PROJECTED_UPLIFT_MAX):
        raise ServiceError(f"Projected renewal uplift must be between 0% and {config.PROJECTED_UPLIFT_MAX:.0%}.")
    return d.quantize(Decimal("0.0001"))


def update_term(s: Session, actor: str, term_id: int, changes: dict, ae: str | None = None,
                reseller: str | None = None) -> Outcome:
    t = get_term(s, term_id)
    c = t.subscription.customer
    out = Outcome(f"Edit term {t.id}", c.qb_number)
    before = _term_dict(t)
    for k, v in changes.items():
        if k not in EDITABLE_TERM_FIELDS:
            raise ServiceError(f"{k} cannot be edited here (money and dates change through Amend).")
        if k == "projected_uplift":
            v = projected_uplift_value(v)
        elif isinstance(v, str):
            v = v.strip() or None
        if getattr(t, k) != v:
            out.lines.append(f"{k}: {getattr(t, k)!r} -> {v!r}")
            setattr(t, k, v)
    if ae is not None and (t.ae.initials if t.ae else "") != ae.strip().upper():
        out.lines.append(f"AE -> {ae.strip().upper() or '(none)'}")
        t.ae = _ae(s, ae)
    if reseller is not None and (t.reseller.name if t.reseller else "") != reseller.strip():
        out.lines.append(f"Reseller -> {reseller.strip() or '(none)'}")
        t.reseller = _reseller(s, reseller)
    if out.lines:
        s.flush()
        _audit(s, actor, "update", "term", t.id, c.qb_number, "; ".join(out.lines)[:400],
               {"before": before, "after": _term_dict(t)})
    return out


def set_checks(s: Session, actor: str, term_id: int, results: dict[str, tuple[str | None, str | None]]) -> Outcome:
    """results: type_code -> (CheckResult value or '' to clear, note)."""
    t = get_term(s, term_id)
    c = t.subscription.customer
    out = Outcome(f"Checklist term {t.id}", c.qb_number)
    current = {x.type_code: x for x in t.checks}
    for code, (res, note) in results.items():
        note = (note or "").strip() or None
        cur = current.get(code)
        if not res:
            if cur:
                t.checks.remove(cur)
                out.lines.append(f"{code}: cleared")
            continue
        r = CheckResult(res)
        if cur and cur.result == r and cur.note == note:
            continue
        if cur:
            cur.result, cur.note, cur.checked_by, cur.checked_at = r, note, actor, dt.datetime.now()
        else:
            t.checks.append(ControlCheck(type_code=code, result=r, note=note, checked_by=actor,
                                         checked_at=dt.datetime.now()))
        out.lines.append(f"{code}: {r.value}{f' ({note})' if note else ''}")
    if out.lines:
        s.flush()
        _audit(s, actor, "checklist", "term", t.id, c.qb_number, "; ".join(out.lines)[:400])
    return out


def record_invoice(s: Session, actor: str, term_id: int, billing_year: int, invoice_date: dt.date | None,
                   amount, reference: str | None = None) -> Outcome:
    amount = D(amount)
    t = get_term(s, term_id)
    c = t.subscription.customer
    if amount is None or amount < 0:
        raise ServiceError("Invoice amount must be zero or positive.")
    if not (t.start_date.year - 1 <= billing_year <= t.end_date.year):
        raise ServiceError(f"Billing year {billing_year} is outside the term.")
    inv = Invoice(term=t, billing_year=billing_year, invoice_date=invoice_date, amount=cents(amount),
                  reference=(reference or "").strip() or None)
    s.add(inv)
    s.flush()
    billed = sum(i.amount for i in t.invoices)
    out = Outcome(f"Invoice for term {t.id}", c.qb_number, entity_id=inv.id,
                  lines=[f"Invoice {billing_year}: {amount:,.2f} on "
                         f"{invoice_date:%m/%d/%Y}" if invoice_date else f"Invoice {billing_year}: {amount:,.2f}"])
    if billed > t.tcv + 1:
        out.warnings.append(f"Total invoiced {billed:,.2f} exceeds the term's TCV {t.tcv:,.2f}.")
    _audit(s, actor, "invoice", "term", t.id, c.qb_number, out.lines[0])
    return out


def delete_invoice(s: Session, actor: str, invoice_id: int) -> Outcome:
    inv = s.get(Invoice, invoice_id)
    if inv is None:
        raise ServiceError("Invoice not found.")
    t = inv.term
    qb = t.subscription.customer.qb_number
    line = f"Removed invoice {inv.billing_year} {inv.amount:,.2f} from term {t.id}"
    t.invoices.remove(inv)
    s.flush()
    _audit(s, actor, "delete", "invoice", invoice_id, qb, line)
    return Outcome("Remove invoice", qb, [line])


def _term_dict(t: Term) -> dict:
    return {k: getattr(t, k) for k in ("id", "kind", "start_date", "end_date", "tcv", "term_months", "signed_date",
                                       "payment_terms", "golive_date", "golive_note", "renewal_note", "next_action",
                                       "notes", "projected_uplift")}


# ------------------------------------------------------------------------------------------------ runner

def run(session_factory, op: Callable[..., Outcome], *args, commit: bool, **kwargs) -> Outcome:
    """Execute op in a transaction; commit it, or roll it back (preview)."""
    from . import undo
    s: Session = session_factory()
    try:
        if commit:
            undo.start(s)                     # record row changes so this change can be undone
        out = op(s, *args, **kwargs)
        if commit:
            s.flush()
            rows = s.info.pop("audit_rows", [])
            if rows:
                undo.finish(s, rows[0])
                out.audit_id = rows[0].id
                for r in rows[1:]:              # one change written as several audit rows: undone together
                    d = json.loads(r.detail) if r.detail else {}
                    d["part_of"] = rows[0].id
                    r.detail = json.dumps(d, default=str)
            else:
                s.info.pop(undo.KEY, None)
            s.commit()
        else:
            s.rollback()
        return out
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
