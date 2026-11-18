"""Maintenance workflows (MAINTENANCE-WORKBOOK.md §6, §8). Same pattern as services.py: validate, change, audit,
return an Outcome; run through services.run(..., commit=False) to preview."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import metrics
from .money import D, D0, ZERO, cents
from .models import (Customer, FeeSource, LicenseProduct, LicensePurchase, MaintenanceBlock, MaintenanceContract,
                     MaintenanceEndReason, MaintenanceFee, MaintenanceInvoice, Platform, PurchaseKind, Subscription)
from .services import Outcome, ServiceError, _audit, get_customer

ENTITY = "maintenance_contract"
ROLL_BLOCKS = {MaintenanceBlock.PERPETUAL_LIVE, MaintenanceBlock.NOT_LIVE_PAYING, MaintenanceBlock.SUBSCRIPTION_ONPREM,
               MaintenanceBlock.SUBSCRIPTION_CLOUD, MaintenanceBlock.CONVERTED_ONPREM, MaintenanceBlock.CONVERTED_CLOUD}


def get_contract(s: Session, contract_id: int) -> MaintenanceContract:
    mc = s.get(MaintenanceContract, contract_id)
    if mc is None:
        raise ServiceError(f"Maintenance contract {contract_id} not found.")
    return mc


def _fee_row(mc: MaintenanceContract, year: int) -> MaintenanceFee | None:
    return next((f for f in mc.fees if f.year == year), None)


def _set(mc: MaintenanceContract, year: int, amount, source: FeeSource, note: str | None = None,
         prorated: bool = False) -> str:
    """Set (amount), explicitly stop (0) or blank (None) one contract year; returns a change line."""
    amount = D(amount)
    row = _fee_row(mc, year)
    old = row.amount if row else None
    if amount is None:
        if row:
            mc.fees.remove(row)
            return f"{metrics.year_label(year)}: {old:,.2f} -> blank"
        return ""
    if amount < 0:
        raise ServiceError("A maintenance fee cannot be negative (use a decommission to reduce it).")
    amount = cents(amount)
    if row:
        if row.amount == amount and row.note == note:
            return ""
        row.amount, row.source, row.note, row.prorated = amount, source, note, prorated
    else:
        mc.fees.append(MaintenanceFee(year=year, amount=amount, source=source, note=note, prorated=prorated))
    return f"{metrics.year_label(year)}: {'blank' if old is None else f'{old:,.2f}'} -> {amount:,.2f}"


def _snapshot(mc: MaintenanceContract) -> dict:
    return {f.year: f.amount for f in mc.fees}


def _outcome(title, mc, before, lines, warnings=None) -> Outcome:
    out = Outcome(title, mc.customer.qb_number, [l for l in lines if l], warnings or [], entity_id=mc.id)
    keys = sorted(set(before) | {f.year for f in mc.fees})[-6:]
    out.arr_before = {k: before.get(k, ZERO) for k in keys}
    out.arr_after = {k: _snapshot(mc).get(k, ZERO) for k in keys}
    return out


# --------------------------------------------------------------------------------------------- new customer / go-live

def first_year_fee(annual, anniversary: int, start: dt.date) -> tuple[int, Decimal, bool]:
    """Contract year (end key) the start date falls in, and its fee prorated from the start month to the end of
    that contract year (full fee when starting at the anniversary)."""
    class _C:
        anniversary_month = anniversary
    key = metrics.contract_year_key(_C, start)
    _, end = metrics.contract_year_span(_C, key)
    months = (end.year - start.year) * 12 + end.month - start.month + 1
    months = max(1, min(12, months))
    return key, cents(D0(annual) * months / 12), months < 12


def new_perpetual(s: Session, actor: str, qb: int, product: LicenseProduct, contract_date: dt.date,
                  list_price, discount, maintenance_pct, anniversary_month: int | None,
                  calendar_billing: bool, golive_date: dt.date | None = None, notes: str | None = None) -> Outcome:
    list_price, discount, maintenance_pct = D(list_price), D(discount), D(maintenance_pct)
    c = get_customer(s, qb)
    if list_price is None or list_price < 0:
        raise ServiceError("Licence price is required.")
    if maintenance_pct is None or not 0 < maintenance_pct < 1:
        raise ServiceError("Maintenance % must be between 0 and 100.")
    discount = -abs(discount or ZERO)
    net = list_price + discount
    annual = cents(net * maintenance_pct)
    anniv = 1 if calendar_billing else anniversary_month
    if anniv is None and golive_date:
        anniv = golive_date.month
    mc = MaintenanceContract(customer=c, product=product, contract_year=contract_date.year,
                             contract_date=contract_date, anniversary_month=anniv, calendar_billing=calendar_billing,
                             go_live=bool(golive_date), golive_date=golive_date, notes=notes,
                             block=MaintenanceBlock.PERPETUAL_LIVE if golive_date else
                             MaintenanceBlock.NOT_LIVE_NOT_PAYING)
    mc.purchases.append(LicensePurchase(kind=PurchaseKind.ORIGINAL, description=product.value.title(),
                                        list_price=list_price, discount=discount, amount=net,
                                        maintenance_amount=annual, golive_date=golive_date))
    s.add(mc)
    c.has_perpetual_license = True
    lines = [f"Licence {list_price:,.2f} {discount:,.2f} = net {net:,.2f}; maintenance {maintenance_pct:.1%} = "
             f"{annual:,.2f} a year"]
    warnings = []
    if golive_date:
        key, fee, pr = first_year_fee(annual, anniv or golive_date.month, golive_date)
        lines.append(_set(mc, key, fee, FeeSource.MANUAL, "first contract year" + (" (prorated)" if pr else ""),
                          prorated=pr))
    else:
        warnings.append("Not live: maintenance starts at go-live (use 'Mark live' then).")
    s.flush()
    _audit(s, actor, "create", ENTITY, mc.id, qb, "New perpetual licence: " + "; ".join(lines))
    return _outcome(f"New perpetual licence for {qb}", mc, {}, lines, warnings)


def mark_live(s: Session, actor: str, contract_id: int, golive_date: dt.date) -> Outcome:
    mc = get_contract(s, contract_id)
    if mc.go_live and mc.golive_date:
        raise ServiceError("Already live.")
    before = _snapshot(mc)
    annual = sum((p.maintenance_amount or ZERO for p in mc.purchases), ZERO)
    if annual <= 0:
        raise ServiceError("No maintenance amount on the licence history - record the licence first.")
    if mc.anniversary_month is None:
        mc.anniversary_month = golive_date.month
    mc.go_live, mc.golive_date = True, golive_date
    if mc.block in (MaintenanceBlock.NOT_LIVE_NOT_PAYING, MaintenanceBlock.NOT_LIVE_PAYING):
        mc.block = MaintenanceBlock.PERPETUAL_LIVE
    key, fee, pr = first_year_fee(annual, mc.anniversary_month, golive_date)
    lines = [f"Live {golive_date:%m/%d/%Y}", _set(mc, key, fee, FeeSource.MANUAL,
                                                   "first contract year" + (" (prorated)" if pr else ""), pr)]
    s.flush()
    _audit(s, actor, "go-live", ENTITY, mc.id, mc.customer.qb_number, "; ".join(l for l in lines if l))
    return _outcome(f"Go-live {mc.customer.qb_number}", mc, before, lines)


# --------------------------------------------------------------------------------------------- fees

def set_fee(s: Session, actor: str, contract_id: int, year: int, amount, note: str | None) -> Outcome:
    amount = D(amount)
    mc = get_contract(s, contract_id)
    before = _snapshot(mc)
    line = _set(mc, year, amount, FeeSource.MANUAL if amount else FeeSource.STOP if amount == 0 else
                FeeSource.MANUAL, (note or "").strip() or None)
    if not line:
        return Outcome("No change", mc.customer.qb_number)
    warnings = []
    zeros = [f.year for f in mc.fees if f.amount == 0 and f.year < year]
    if amount and zeros:
        warnings.append(f"An earlier year ({metrics.year_label(max(zeros))}) is an explicit 0 (stopped) - this "
                        "re-activates the line.")
    s.flush()
    _audit(s, actor, "fee", ENTITY, mc.id, mc.customer.qb_number, line + (f" ({note})" if note else ""))
    return _outcome(f"Fee {metrics.year_label(year)} for {mc.customer.qb_number}", mc, before, [line], warnings)


def roll_candidates(s: Session, year: int, month: int | None = None) -> list[MaintenanceContract]:
    out = []
    for mc in s.scalars(select(MaintenanceContract)):
        fees = metrics.fee_map(mc)
        if mc.block not in ROLL_BLOCKS or mc.ended_on or year in fees or not fees.get(year - 1):
            continue
        if month and (mc.anniversary_month or 1) != month:
            continue
        out.append(mc)
    return sorted(out, key=lambda m: (m.anniversary_month or 1, m.customer.name))


def roll_amount(mc: MaintenanceContract, year: int, rate) -> Decimal:
    rate = D(rate)
    prior = next(f for f in mc.fees if f.year == year - 1)
    base = prior.amount
    if prior.prorated:                       # a stub year is not the annual fee
        base = sum((p.maintenance_amount or ZERO for p in mc.purchases if (p.effective_year or 0) < year), ZERO) or base
    added = sum((p.maintenance_amount or ZERO for p in mc.purchases if p.effective_year == year), ZERO)
    return cents(base * (1 + rate) + added)


def annual_roll(s: Session, actor: str, year: int, rate=Decimal("0.04"), month: int | None = None,
                contract_ids: list[int] | None = None) -> Outcome:
    rate = D(rate)
    if not 0 <= rate < Decimal("0.5"):
        raise ServiceError("Uplift rate must be between 0% and 50%.")
    cands = roll_candidates(s, year, month)
    if contract_ids is not None:
        cands = [m for m in cands if m.id in set(contract_ids)]
    if not cands:
        raise ServiceError(f"Nothing to roll into {metrics.year_label(year)}.")
    out = Outcome(f"Annual roll into {metrics.year_label(year)} (+{rate:.1%})", None)
    total_prior = total_new = ZERO
    for mc in cands:
        prior = metrics.fee_for(mc, year - 1)
        new = roll_amount(mc, year, rate)
        _set(mc, year, new, FeeSource.ROLL, f"annual uplift {rate:.1%}")
        total_prior += prior
        total_new += new
        out.lines.append(f"{mc.customer.qb_number} {mc.customer.name}: {prior:,.2f} -> {new:,.2f}")
    s.flush()
    out.arr_before = {year: cents(total_prior)}
    out.arr_after = {year: cents(total_new)}
    out.lines.insert(0, f"{len(cands)} contracts; {total_prior:,.2f} -> {total_new:,.2f} "
                        f"(+{total_new - total_prior:,.2f})")
    _audit(s, actor, "roll", ENTITY, None, None, f"Annual roll {metrics.year_label(year)} +{rate:.1%}: "
                                                 f"{len(cands)} contracts, {total_new:,.2f}",
           {"year": year, "rate": rate, "contracts": [m.id for m in cands]})
    return out


# --------------------------------------------------------------------------------------------- purchases

def add_purchase(s: Session, actor: str, contract_id: int, kind: PurchaseKind, description: str,
                 amount, maintenance_amount, effective_year: int, quantity: float | None = None,
                 golive_date: dt.date | None = None) -> Outcome:
    amount, maintenance_amount = D0(amount), D0(maintenance_amount)
    mc = get_contract(s, contract_id)
    if kind == PurchaseKind.ORIGINAL:
        raise ServiceError("Use a new licence for the original purchase.")
    if mc.ended_on:
        raise ServiceError("This maintenance line has ended.")
    if kind == PurchaseKind.DECOMMISSION:
        amount, maintenance_amount = -abs(amount or 0), -abs(maintenance_amount or 0)
    elif (maintenance_amount or 0) < 0 or (amount or 0) < 0:
        raise ServiceError("Purchase amounts are positive (use Decommission to reduce).")
    before = _snapshot(mc)
    mc.purchases.append(LicensePurchase(kind=kind, description=description.strip() or None, quantity=quantity,
                                        amount=amount, maintenance_amount=maintenance_amount,
                                        golive_date=golive_date, effective_year=effective_year))
    lines = [f"{kind.value.title()} '{description}': licence {amount:+,.2f}, maintenance {maintenance_amount:+,.2f} "
             f"a year from {metrics.year_label(effective_year)}"]
    applied = 0
    for f in sorted(mc.fees, key=lambda f: f.year):
        if f.year >= effective_year and f.amount > 0:
            lines.append(_set(mc, f.year, max(ZERO, f.amount + maintenance_amount), FeeSource.PURCHASE,
                              f"{kind.value.lower()}: {description}"))
            applied += 1
    warnings = [] if applied else [f"No fee exists yet for {metrics.year_label(effective_year)} or later; the annual "
                                   "roll will add this maintenance when it creates that year."]
    s.flush()
    _audit(s, actor, kind.value.lower(), ENTITY, mc.id, mc.customer.qb_number, "; ".join(l for l in lines if l))
    return _outcome(f"{kind.value.title()} for {mc.customer.qb_number}", mc, before, lines, warnings)


# --------------------------------------------------------------------------------------------- stop

def _stop(mc: MaintenanceContract, first_unbilled: int) -> list[str]:
    lines = [_set(mc, first_unbilled, ZERO, FeeSource.STOP, "maintenance stopped")]
    for f in [f for f in mc.fees if f.year > first_unbilled]:
        lines.append(_set(mc, f.year, None, FeeSource.STOP))
    return lines


def first_unbilled_year(mc: MaintenanceContract, on: dt.date) -> int:
    """First contract year that starts on/after the date (the current year stays billed)."""
    key = metrics.contract_year_key(mc, on)
    start, _ = metrics.contract_year_span(mc, key)
    return key if start >= on else key + 1


def convert(s: Session, actor: str, contract_id: int, on: dt.date, subscription_id: int | None = None) -> Outcome:
    mc = get_contract(s, contract_id)
    if mc.ended_on:
        raise ServiceError("This maintenance line has already ended.")
    sub = s.get(Subscription, subscription_id) if subscription_id else None
    if subscription_id and (sub is None or sub.customer_id != mc.customer_id):
        raise ServiceError("Pick a subscription of the same customer.")
    before = _snapshot(mc)
    year = first_unbilled_year(mc, on)
    lines = _stop(mc, year)
    mc.converted, mc.ended_on, mc.end_reason = True, on, MaintenanceEndReason.CONVERTED
    mc.converted_subscription_id = sub.id if sub else None
    mc.block = MaintenanceBlock.CONVERTED_CLOUD if sub and sub.platform == Platform.CLOUD else \
        MaintenanceBlock.CONVERTED_ONPREM
    if mc.anniversary_month and "(converted)" not in (mc.notes or "").lower():
        mc.notes = ((mc.notes + "; ") if mc.notes else "") + "Billing anniversary (Converted)"
    lines.insert(0, f"Converted {on:%m/%d/%Y}" + (f" to {sub.platform.value} subscription #{sub.id}" if sub else "")
                 + f"; explicit 0 from {metrics.year_label(year)}")
    s.flush()
    _audit(s, actor, "convert", ENTITY, mc.id, mc.customer.qb_number, "; ".join(l for l in lines if l))
    warnings = [] if sub else ["No subscription linked - add the Conversion subscription so ARR is not lost."]
    return _outcome(f"Convert maintenance {mc.customer.qb_number}", mc, before, lines, warnings)


def convert_to_subscription(s: Session, actor: str, qb: int, platform: Platform, inp, line_ids: list[int],
                            today: dt.date | None = None) -> Outcome:
    """Conversion in one step: the CONVERSION subscription plus, for each ticked maintenance line, the maintenance
    conversion (explicit 0 from the first contract year starting on/after the subscription start)."""
    from . import services
    from .models import OpenedVia, Term
    out = services.new_subscription(s, actor, qb, platform, OpenedVia.CONVERSION, inp, today=today)
    sub_id = s.get(Term, out.entity_id).subscription_id
    for lid in line_ids:
        mc = get_contract(s, lid)
        if mc.customer.qb_number != qb:
            raise ServiceError(f"Maintenance line {lid} belongs to another customer.")
        o = convert(s, actor, lid, inp.start_date, sub_id)
        out.lines += [f"Maintenance line {mc.id}: {o.lines[0]}"] + o.lines[1:]
    if line_ids:
        out.warnings = [w for w in out.warnings if "maintenance workbook" not in w]
    return out


def terminate(s: Session, actor: str, contract_id: int, on: dt.date, reason: MaintenanceEndReason,
              note: str | None) -> Outcome:
    mc = get_contract(s, contract_id)
    if mc.ended_on:
        raise ServiceError("This maintenance line has already ended.")
    before = _snapshot(mc)
    year = first_unbilled_year(mc, on)
    lines = _stop(mc, year)
    mc.ended_on, mc.end_reason, mc.end_note = on, reason, (note or "").strip() or None
    mc.block = MaintenanceBlock.CLOSED
    lines.insert(0, f"Terminated {on:%m/%d/%Y} ({reason.value}); explicit 0 from {metrics.year_label(year)}")
    s.flush()
    _audit(s, actor, "terminate", ENTITY, mc.id, mc.customer.qb_number, "; ".join(l for l in lines if l))
    return _outcome(f"Terminate maintenance {mc.customer.qb_number}", mc, before, lines)


# --------------------------------------------------------------------------------------------- invoices

def record_invoice(s: Session, actor: str, contract_id: int, year: int, invoice_no: str, invoice_date: dt.date,
                   amount, due_date: dt.date | None = None, memo: str | None = None,
                   reseller: bool = False) -> Outcome:
    amount = D(amount)
    mc = get_contract(s, contract_id)
    if amount is None or amount < 0:
        raise ServiceError("Invoice amount must be zero or positive.")
    if any(i.invoice_no == invoice_no.strip() for i in mc.invoices if invoice_no.strip()):
        raise ServiceError(f"Invoice {invoice_no} is already recorded on this line.")
    mc.invoices.append(MaintenanceInvoice(contract_year=year, invoice_no=invoice_no.strip() or None,
                                          invoice_date=invoice_date, due_date=due_date, amount=cents(amount),
                                          memo=(memo or "").strip() or None, reseller_invoice=reseller))
    fee = metrics.fee_for(mc, year)
    warnings = []
    if fee is None:
        warnings.append(f"No fee is set for {metrics.year_label(year)} yet.")
    elif abs(fee - amount) > 1:
        warnings.append(f"Invoice {amount:,.2f} differs from the {metrics.year_label(year)} fee {fee:,.2f}.")
    s.flush()
    line = f"Invoice {invoice_no} {amount:,.2f} for {metrics.year_label(year)} ({invoice_date:%m/%d/%Y})"
    _audit(s, actor, "invoice", ENTITY, mc.id, mc.customer.qb_number, line)
    return Outcome(line, mc.customer.qb_number, [line], warnings, entity_id=mc.id)


def delete_invoice(s: Session, actor: str, invoice_id: int) -> Outcome:
    inv = s.get(MaintenanceInvoice, invoice_id)
    if inv is None:
        raise ServiceError("Invoice not found.")
    mc = inv.contract
    line = f"Removed invoice {inv.invoice_no} {inv.amount:,.2f} ({metrics.year_label(inv.contract_year)})"
    mc.invoices.remove(inv)
    s.flush()
    _audit(s, actor, "delete-invoice", ENTITY, mc.id, mc.customer.qb_number, line)
    return Outcome(line, mc.customer.qb_number, [line], entity_id=mc.id)


def update_details(s: Session, actor: str, contract_id: int, changes: dict) -> Outcome:
    mc = get_contract(s, contract_id)
    allowed = {"anniversary_month", "calendar_billing", "golive_note", "notes", "block", "product"}
    lines = []
    for k, v in changes.items():
        if k not in allowed:
            raise ServiceError(f"{k} cannot be edited here.")
        if getattr(mc, k) != v:
            lines.append(f"{k}: {getattr(mc, k)!r} -> {v!r}")
            setattr(mc, k, v)
    if lines:
        s.flush()
        _audit(s, actor, "update", ENTITY, mc.id, mc.customer.qb_number, "; ".join(lines)[:400])
    return Outcome("Details saved" if lines else "No changes", mc.customer.qb_number, lines, entity_id=mc.id)


# --------------------------------------------------------------------------------------------- validation

def validations(s: Session, today: dt.date, customer_id: int | None = None) -> list[dict]:
    """Checks on the maintenance register. customer_id: only that customer's lines (same results for them - the
    duplicate check looks within one customer)."""
    from sqlalchemy.orm import selectinload
    out = []
    stmt = select(MaintenanceContract).options(
        selectinload(MaintenanceContract.fees), selectinload(MaintenanceContract.invoices),
        selectinload(MaintenanceContract.purchases),
        selectinload(MaintenanceContract.customer).selectinload(Customer.subscriptions))
    if customer_id is not None:
        stmt = stmt.where(MaintenanceContract.customer_id == customer_id)
    contracts = list(s.scalars(stmt))
    by_cust: dict[int, list] = {}
    for mc in contracts:
        by_cust.setdefault(mc.customer_id, []).append(mc)
        fees = sorted(mc.fees, key=lambda f: f.year)
        first_paid = next((f.year for f in fees if f.amount > 0), None)
        zeros = [f.year for f in fees if f.amount == 0 and first_paid and f.year > first_paid]
        later = [f for f in fees if zeros and f.year > zeros[0] and f.amount > 0]
        if later:
            out.append({"contract": mc, "check": "value-after-stop",
                        "message": f"{metrics.year_label(later[0].year)} has a fee after the explicit 0 in "
                                   f"{metrics.year_label(zeros[0])}."})
        if mc.converted and not mc.converted_subscription_id and not mc.customer.subscriptions:
            out.append({"contract": mc, "check": "converted-no-subscription",
                        "message": "Marked converted but the customer has no subscription."})
        billed: dict[int, list] = {}
        for inv in mc.invoices:
            if not inv.reseller_invoice:
                billed.setdefault(inv.contract_year, []).append(inv)
        for year, invs in sorted(billed.items()):
            fee = metrics.fee_for(mc, year)
            total = sum(i.amount for i in invs)
            if fee is not None and abs(fee - total) > 1:
                out.append({"contract": mc, "check": "invoiced-vs-fee",
                            "message": f"{metrics.year_label(year)}: invoiced {total:,.2f} "
                                       f"({', '.join(i.invoice_no or '?' for i in invs)}) vs fee {fee:,.2f}."})
        p = metrics.maintenance_pricing(mc, today)
        if mc.block in ROLL_BLOCKS and p.current_fee and p.next_fee is None and not mc.ended_on:
            _, end = metrics.contract_year_span(mc, p.current_key)
            if (end - today).days <= 60:
                out.append({"contract": mc, "check": "roll-due",
                            "message": f"Contract year ends {end:%m/%d/%Y}; no {metrics.year_label(p.current_key + 1)} "
                                       "fee yet (annual roll)."})
    for cid, mcs in by_cust.items():
        main = [m for m in mcs if m.parent_contract_id is None]
        if len(main) > 1:
            out.append({"contract": main[1], "check": "duplicate-customer",
                        "message": f"Customer has {len(main)} maintenance lines (also "
                                   f"{', '.join(m.source_ref or str(m.id) for m in main if m is not main[1])})."})
    return out
