"""Import the maintenance workbook (MAINTENANCE-WORKBOOK.md), read-only.

Register tab 'Maintenance-Adj Advance Billing': one header row (>= 3 'YYYY-YYYY' labels), columns found by header
text, blocks from title rows, one row per licence line. Year cells: number (0 = explicit stop) / blank = not billed /
text = ignored (reported). Advance-billing tabs -> maintenance invoices matched to customers by name.
"""
from __future__ import annotations

import datetime as dt
import difflib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl
from sqlalchemy.orm import Session

from ..money import D, ZERO, cents
from ..models import (Customer, FeeSource, LicenseProduct, LicensePurchase, MaintenanceBlock, MaintenanceContract,
                      MaintenanceEndReason, MaintenanceFee, MaintenanceInvoice, PurchaseKind)
from .mapper import ImportReport, _name_key

SHEET = "Maintenance-Adj Advance Billing"
YEAR_RE = re.compile(r"^\s*(20\d\d)\s*-\s*(20\d\d)\s*$")
DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})")
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]


def _norm(v) -> str:
    s = re.sub(r"\s+", " ", str(v or "")).strip().lower()
    return re.sub(r"\s*-\s*", " - ", s)


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return None


def _date_in_text(v) -> dt.date | None:
    m = DATE_RE.search(str(v or ""))
    if not m:
        return None
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    y += 2000 if y < 100 else 0
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def block_for(title: str) -> MaintenanceBlock:
    t = _norm(title)
    if "closed" in t or "terminat" in t or "churn" in t:
        return MaintenanceBlock.CLOSED
    if "upsell" in t or "perpetual to subscription" in t:
        return MaintenanceBlock.CONVERTED_CLOUD if "cloud" in t else MaintenanceBlock.CONVERTED_ONPREM
    if "subscription" in t:
        return MaintenanceBlock.SUBSCRIPTION_CLOUD if "cloud" in t else MaintenanceBlock.SUBSCRIPTION_ONPREM
    if "not paying" in t:
        return MaintenanceBlock.NOT_LIVE_NOT_PAYING
    if "not live" in t:
        return MaintenanceBlock.NOT_LIVE_PAYING
    if "others" in t:
        return MaintenanceBlock.OTHERS
    return MaintenanceBlock.PERPETUAL_LIVE


def anniversary(v) -> tuple[int | None, bool, bool, str | None]:
    """'February' / 'January - Calendar (Converted)' / 'not applicable' -> (month, calendar, converted, note)."""
    s = _norm(v)
    converted = "convert" in s
    calendar = "calendar" in s
    month = next((i for i, m in enumerate(MONTHS, 1) if s.startswith(m[:3])), None)
    note = None
    if month is None or "inactive" in s or (s and not re.fullmatch(r"[a-z]+( - calendar)?( \(converted\))?", s)):
        note = str(v).strip() if v else None
    return month, calendar, converted, note


def product_of(v) -> LicenseProduct | None:
    s = _norm(v)
    return {"perpetual": LicenseProduct.PERPETUAL, "on - premise": LicenseProduct.ON_PREMISE,
            "on-premise": LicenseProduct.ON_PREMISE, "cloud": LicenseProduct.CLOUD}.get(s)


@dataclass
class Columns:
    year: dict[int, int]                    # col -> end-year key
    by_name: dict[str, list[int]]

    def first(self, name: str, after: int = -1) -> int | None:
        return next((c for c in self.by_name.get(name, []) if c > after), None)


@dataclass
class SheetRow:
    row: int
    block: MaintenanceBlock
    block_title: str
    counter: object
    values: tuple
    fees: dict[int, float] = field(default_factory=dict)
    text_years: dict[int, str] = field(default_factory=dict)


@dataclass
class MaintenanceSheet:
    path: str
    cols: Columns
    rows: list[SheetRow]
    grand_total: dict[int, float]
    sheet_revenue: dict[int, float]         # calendar year -> Σ 'YYYY Total Revenue' over customer rows
    sheet_deferred: dict[int, float]        # calendar year -> Σ '12/31/YYYY Deferred'
    row_revenue: dict[int, dict[int, float]]  # sheet row -> {year: Total Revenue}
    header_row: int = 0                     # Excel row of the header
    grand_total_row: int | None = None      # Excel row of GRAND TOTAL


def read_register(path: Path) -> MaintenanceSheet:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:                                    # read-only workbooks keep the file open (locked on Windows) until closed
        if SHEET not in wb.sheetnames:
            raise ValueError(f"Tab '{SHEET}' not found in {Path(path).name}")
        rows = list(wb[SHEET].iter_rows(values_only=True))
    finally:
        wb.close()
    hdr_i = next(i for i, r in enumerate(rows[:10]) if sum(1 for v in r if YEAR_RE.match(str(v or ""))) >= 3)
    header = rows[hdr_i]
    years, by_name = {}, defaultdict(list)
    revenue_cols, deferred_cols = {}, {}
    for j, h in enumerate(header):
        m = YEAR_RE.match(str(h or ""))
        if m:
            years[j] = int(m.group(2))
            continue
        n = _norm(h)
        if n:
            by_name[n].append(j)
        if "total revenue" in n and re.search(r"20\d\d", n):
            revenue_cols[int(re.search(r"(20\d\d)", n).group(1))] = j
        if "deferred" in n and re.search(r"20\d\d", n):
            deferred_cols[int(re.findall(r"(20\d\d)", n)[-1])] = j
    cols = Columns(years, dict(by_name))
    c_name = cols.first("customer name")
    c_num = cols.first("customer") if cols.first("customer") is not None else cols.first("customer number")
    out: list[SheetRow] = []
    title = "A. PERPETUAL CUSTOMERS - LIVE"
    grand: dict[int, float] = {}
    rev, dfr = defaultdict(float), defaultdict(float)
    row_rev: dict[int, dict[int, float]] = {}
    grand_row = None
    for i, r in enumerate(rows[hdr_i + 1:], start=hdr_i + 2):
        a = r[0]
        if isinstance(a, str) and _norm(a).startswith("grand total"):
            grand = {years[j]: float(r[j] or 0) for j in years if _num(r[j]) is not None}
            grand_row = i
            break
        name = r[c_name]
        if name is None or not str(name).strip():
            if isinstance(a, str) and a.strip() and not _norm(a).startswith("total"):
                has_numbers = any(_num(v) is not None for v in r[9:])
                if re.match(r"^[A-Z]\.\s", a.strip()) or not has_numbers:
                    title = a.strip()
            continue
        sr = SheetRow(i, block_for(title), title, a, r)
        for j, y in years.items():
            v = r[j]
            if _num(v) is not None:
                sr.fees[y] = float(v)
            elif isinstance(v, str) and v.strip() and _norm(v) != "n/a":
                sr.text_years[y] = v.strip()
        out.append(sr)
        if _num(r[c_num]) is not None:
            row_rev[i] = {}
            for y, j in revenue_cols.items():
                if _num(r[j]) is not None:
                    rev[y] += float(r[j])
                    row_rev[i][y] = float(r[j])
            for y, j in deferred_cols.items():
                if _num(r[j]) is not None:
                    dfr[y] += float(r[j])
    return MaintenanceSheet(str(path), cols, out, grand, dict(rev), dict(dfr), row_rev, hdr_i + 1, grand_row)


# --------------------------------------------------------------------------------------------- invoices

@dataclass
class InvoiceRow:
    tab: str
    row: int
    customer_text: str
    number: str
    date: dt.date | None
    due: dt.date | None
    amount: float
    memo: str


def read_invoices(path: Path) -> list[InvoiceRow]:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        sheets = {name: list(wb[name].iter_rows(values_only=True)) for name in wb.sheetnames
                  if "advance billing" in name.lower() or name.lower().startswith("addition")}
    finally:
        wb.close()
    out = []
    for name, rows in sheets.items():
        hi = next((i for i, r in enumerate(rows[:6]) if any(_norm(v) == "customer" for v in r)), None)
        if hi is None:
            continue
        h = {_norm(v): j for j, v in enumerate(rows[hi]) if v is not None}
        jc, jn, jd, jdue, ja = h.get("customer"), h.get("num"), h.get("date"), h.get("due date"), h.get("amount")
        jm = h.get("advance billing") if "advance billing" in h else h.get("description")
        for i, r in enumerate(rows[hi + 1:], start=hi + 2):
            cust, amt = r[jc] if jc is not None else None, _num(r[ja]) if ja is not None else None
            if not cust or amt is None or not (jn is not None and r[jn]):
                continue
            memo = ""
            for j in range(len(r)):     # the period text sits in the last text cell (column varies)
                if isinstance(r[j], str) and re.search(r"(?i)maintenance fee|period|to (december|20\d\d)", r[j]) \
                        and j != jc:
                    memo = r[j].strip()
            if jm is not None and isinstance(r[jm], str):
                memo = r[jm].strip()
            out.append(InvoiceRow(name, i, str(cust), str(r[jn]).strip(), _date(r[jd]) if jd is not None else None,
                                  _date(r[jdue]) if jdue is not None else None, amt, memo))
    return out


PERIOD_RE = re.compile(r"(?i)(january|february|march|april|may|june|july|august|september|october|november|"
                       r"december)\s*(20\d\d)?\s*(?:-|to)\s*(january|february|march|april|may|june|july|august|"
                       r"september|october|november|december)\s*(20\d\d)")


def invoice_contract_year(memo: str, inv_date: dt.date | None, anniv: int | None) -> int | None:
    """End-year key of the contract year an invoice covers: from the period text, else the first anniversary
    on/after (invoice date - 45 days)."""
    m = PERIOD_RE.search(memo or "")
    if m:
        start_month = MONTHS.index(m.group(1).lower()) + 1
        end_month = MONTHS.index(m.group(3).lower()) + 1
        end_year = int(m.group(4))
        start_year = int(m.group(2)) if m.group(2) else (end_year if start_month <= end_month else end_year - 1)
        return start_year + 1
    if inv_date is None:
        return None
    a = anniv or 1
    ref = inv_date - dt.timedelta(days=45)
    start = dt.date(ref.year, a, 1)
    if start < ref.replace(day=1):
        start = dt.date(ref.year + 1, a, 1)
    return start.year + 1


def _clean_qb_name(s: str) -> str:
    """QuickBooks 'RESELLER (..):END CUSTOMER (..):2. ShipERP Maintenance (...)' -> end customer name."""
    parts = [p.strip() for p in s.split(":")]
    parts = [p for p in parts if p and not re.match(r"^\d+\.\s", p)] or parts
    name = parts[-1]
    name = re.sub(r"(?i)^c/o\s+", "", name)
    name = re.sub(r"\([^)]*\)?", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def is_reseller_billed(s: str) -> bool:
    parts = [p for p in s.split(":") if p.strip() and not re.match(r"^\s*\d+\.\s", p)]
    return len(parts) > 1


# --------------------------------------------------------------------------------------------- import

def import_maintenance(session: Session, path: Path, report: ImportReport, customers: dict[int, Customer]) -> None:
    sheet = read_register(path)
    report.maintenance_source = str(path)
    cols = sheet.cols
    c = {k: cols.first(k) for k in ("customer", "customer name", "product", "year", "contract month",
                                    "go live date", "billing anniversary", "original before discount", "discount",
                                    "original, net of discount", "total current license", "original maintenance",
                                    "additional maintenance", "total maintenance")}
    c_mod = cols.first("addl purchase - module")
    c_plant = cols.first("addl purchase - plant")
    c_carrier = cols.first("purchase - carrier")
    mod_golive, mod_amt = cols.first("go live date", c_mod or 0), cols.first("additional purchase - amount", c_mod or 0)
    plant_qty = cols.first("plant qty (excluding core)")
    plant_golive = cols.first("go live date", c_plant or 0)
    plant_amt = cols.first("additional purchase - amount", c_plant or 0)
    car_golive, car_amt = cols.first("go - live", c_carrier or 0), cols.first("purchase amount", c_carrier or 0)

    def v(r: SheetRow, col):
        return r.values[col] if col is not None and col < len(r.values) else None

    by_number: dict[int, MaintenanceContract] = {}
    imported = defaultdict(float)
    license_checks = 0
    for r in sheet.rows:
        ref = f"Maintenance row {r.row}"
        num = _num(v(r, c["customer"]))
        name = str(v(r, c["customer name"])).strip()
        if num is None or not 80000 <= int(num) <= 89999:
            total = sum(r.fees.values())
            report.add("rejected" if total else "fix", None,
                       f"No customer number for '{name}' ({r.block_title})" +
                       (f" - {total:,.2f} of maintenance not imported." if total else " - skipped (no fees)."),
                       where=ref)
            continue
        qb = int(num)
        cust = customers.get(qb)
        if cust is None:
            cust = Customer(qb_number=qb, name=name.upper())
            session.add(cust)
            customers[qb] = cust
            report.add("inference", None, f"Customer {qb} '{name}' only in the maintenance workbook - created.",
                       customer=qb, where=ref)
        if r.block not in (MaintenanceBlock.SUBSCRIPTION_ONPREM, MaintenanceBlock.SUBSCRIPTION_CLOUD):
            cust.has_perpetual_license = True       # perpetual / not live / converted / closed blocks hold a licence
        prod_raw = v(r, c["product"])
        product = product_of(prod_raw)
        month, calendar, converted, anniv_note = anniversary(v(r, c["billing anniversary"]))
        gl = v(r, c["go live date"])
        gl_date = _date(gl) or _date_in_text(gl)
        live = bool(gl_date) and "not live" not in _norm(gl)
        mc = MaintenanceContract(
            customer=cust, block=r.block, product=product,
            description=(str(prod_raw).strip()[:120] if prod_raw and product is None and r.block !=
                         MaintenanceBlock.CLOSED else None),
            contract_year=int(_num(v(r, c["year"]))) if _num(v(r, c["year"])) else None,
            contract_date=_date(v(r, c["contract month"])),
            go_live=live, golive_date=gl_date,
            golive_note=None if isinstance(gl, (dt.date, dt.datetime)) or gl is None else str(gl).strip()[:160],
            anniversary_month=month, calendar_billing=calendar,
            converted=converted or r.block in (MaintenanceBlock.CONVERTED_ONPREM, MaintenanceBlock.CONVERTED_CLOUD),
            notes="; ".join(x for x in [f"Block: {r.block_title}", anniv_note and f"Billing anniversary: {anniv_note}"]
                            if x),
            source_ref=ref)
        if r.counter is None or (isinstance(r.counter, str) and not r.counter.strip()):
            parent = by_number.get(qb)
            if parent is not None:
                mc.parent = parent
                report.add("inference", None, f"Row without a Customer Count - linked as an add-on of "
                                              f"{parent.source_ref}.", customer=qb, where=ref)
            else:
                report.add("inference", None, "Row without a Customer Count and no main row for this customer - "
                                              "kept as its own maintenance line.", customer=qb, where=ref)
        else:
            if qb in by_number:
                report.add("warning", None, f"Customer {qb} appears again (also {by_number[qb].source_ref}); "
                                            "kept as a separate maintenance line.", customer=qb, where=ref)
            by_number.setdefault(qb, mc)
        session.add(mc)

        # licence history
        lp, disc, net = _num(v(r, c["original before discount"])), _num(v(r, c["discount"])), \
            _num(v(r, c["original, net of discount"]))
        orig_maint = _num(v(r, c["original maintenance"]))
        if lp or net or orig_maint:
            mc.purchases.append(LicensePurchase(
                kind=PurchaseKind.ORIGINAL, description=(product.value.title() if product else "Licence"),
                list_price=lp, discount=disc, amount=net if net is not None else (lp or 0) + (disc or 0),
                maintenance_amount=orig_maint, golive_date=gl_date))
        add_maint = _num(v(r, c["additional maintenance"])) or 0.0
        extras = []
        for kind, ncol, qcol, gcol, acol in ((PurchaseKind.MODULE, c_mod, None, mod_golive, mod_amt),
                                             (PurchaseKind.PLANT, c_plant, plant_qty, plant_golive, plant_amt),
                                             (PurchaseKind.CARRIER, c_carrier, None, car_golive, car_amt)):
            desc, amt = v(r, ncol), _num(v(r, acol))
            if (desc is None or not str(desc).strip()) and not amt:
                continue
            g = v(r, gcol)
            p = LicensePurchase(kind=kind, description=str(desc).strip()[:200] if desc else None,
                                quantity=_num(v(r, qcol)) if qcol is not None else None, amount=amt or 0.0,
                                golive_date=_date(g) or _date_in_text(g),
                                golive_note=None if isinstance(g, (dt.date, dt.datetime)) or g is None
                                else str(g).strip()[:120])
            mc.purchases.append(p)
            extras.append(p)
        if add_maint < 0:
            mc.purchases.append(LicensePurchase(kind=PurchaseKind.DECOMMISSION, amount=0.0,
                                                description="Decommissioned plants / modules (negative additional "
                                                            "maintenance)", maintenance_amount=add_maint))
        elif add_maint > 0:
            target = next((p for p in extras if p.kind != PurchaseKind.CARRIER), None) or (extras[0] if extras
                                                                                         else None)
            if target is None:
                target = LicensePurchase(kind=PurchaseKind.MODULE, amount=0.0,
                                         description="Additional maintenance (purchases not itemised)")
                mc.purchases.append(target)
            target.maintenance_amount = add_maint
            if len([p for p in extras if p.kind != PurchaseKind.CARRIER]) > 1:
                target.description = (target.description or "") + " (maintenance covers all additional purchases)"
        tcl = _num(v(r, c["total current license"]))
        derived = sum((p.amount for p in mc.purchases if p.kind != PurchaseKind.CARRIER), ZERO)
        if tcl is not None and abs(D(tcl) - derived) > 1:
            license_checks += 1
            report.add("warning", None, f"Total Current License on the sheet {tcl:,.2f} differs from the licence "
                                        f"history {derived:,.2f}.", customer=qb, where=ref)

        # fees
        ordered = sorted(r.fees.items())
        positives = [y for y, a in ordered if a > 0]
        for y, amt in ordered:
            if amt < 0:
                report.add("fix", None, f"Negative maintenance {amt:,.2f} in {y - 1}-{y} stored as 0.",
                           customer=qb, where=ref)
                amt = 0.0
            prorated = bool(positives) and y == positives[0] and len(positives) > 1 and \
                amt < 0.6 * r.fees[positives[1]]
            mc.fees.append(MaintenanceFee(year=y, amount=cents(amt), prorated=prorated, source=FeeSource.IMPORT))
            imported[y] += amt
        for y, text in r.text_years.items():
            report.add("fix", None, f"Text '{text}' in the {y - 1}-{y} cell read as blank.", customer=qb, where=ref)

        # ended / converted
        zeros_after_pay = [y for y, a in ordered if a == 0 and positives and y > positives[0]]
        note_text = str(prod_raw).strip() if prod_raw and product is None else ""
        end_like = bool(re.search(r"(?i)terminat|inactive|cancel|churn", note_text)) and r.block !=             MaintenanceBlock.CLOSED
        if end_like:
            mc.description = None
            mc.end_note = note_text[:300]
            mc.ended_on = _date_in_text(note_text)
            mc.end_reason = MaintenanceEndReason.TERMINATED if "terminat" in note_text.lower() else                 MaintenanceEndReason.CHURN
        if r.block == MaintenanceBlock.CLOSED:
            note = str(prod_raw).strip() if prod_raw and product is None else None
            text = f"{note or ''} {v(r, c['billing anniversary']) or ''}".lower()
            mc.end_reason = (MaintenanceEndReason.CONVERTED if converted else
                             MaintenanceEndReason.OTHER if "unable to retrieve" in text else
                             MaintenanceEndReason.TERMINATED if "terminat" in text else
                             MaintenanceEndReason.INACTIVE if "inactive" in text else
                             MaintenanceEndReason.CHURN)
            mc.end_note = note[:300] if note else None
            ended = _date_in_text(note) or _date_in_text(v(r, c["billing anniversary"]))
            if not ended and zeros_after_pay:
                ended = dt.date(zeros_after_pay[0] - 1, month or 1, 1)
            if not ended and positives:
                ended = dt.date(positives[-1], month or 1, 1) - dt.timedelta(days=1)
            mc.ended_on = ended
        elif mc.converted and zeros_after_pay:
            mc.ended_on = dt.date(zeros_after_pay[0] - 1, month or 1, 1)
            mc.end_reason = MaintenanceEndReason.CONVERTED
    session.flush()

    # link converted lines to the customer's subscription when there is exactly one candidate
    for mc in [m for m in by_number.values() if m.converted]:
        subs = mc.customer.subscriptions
        if len(subs) == 1:
            mc.converted_subscription_id = subs[0].id
    report.maintenance_totals = {y: [round(sheet.grand_total.get(y, 0.0), 2), round(imported.get(y, 0.0), 2)]
                                 for y in sorted(set(sheet.grand_total) | set(imported))}
    report.maintenance_sheet_revenue = {y: round(a, 2) for y, a in sorted(sheet.sheet_revenue.items())}
    report.maintenance_sheet_deferred = {y: round(a, 2) for y, a in sorted(sheet.sheet_deferred.items())}

    # invoices
    import_invoices(session, path, report, customers)
    session.flush()


def _match_customer(text: str, candidates: dict[str, Customer]) -> Customer | None:
    key = _name_key(_clean_qb_name(text))
    if not key:
        return None
    if key in candidates:
        return candidates[key]
    for k, cu in candidates.items():
        if k and (key.startswith(k) or k.startswith(key)) and min(len(k), len(key)) >= 5:
            return cu
    best = difflib.get_close_matches(key, list(candidates), n=1, cutoff=0.82)
    return candidates[best[0]] if best else None


def import_invoices(session: Session, path: Path, report: ImportReport, customers: dict[int, Customer]) -> None:
    rows = read_invoices(path)
    cand: dict[str, Customer] = {}
    for cu in customers.values():
        if cu.maintenance_contracts:
            cand.setdefault(_name_key(cu.name), cu)
    matched = unmatched = 0
    for inv in rows:
        cu = _match_customer(inv.customer_text, cand)
        ref = f"{inv.tab} row {inv.row}"
        if cu is None:
            unmatched += 1
            report.add("warning", None, f"Invoice {inv.number} ({inv.amount:,.2f}) for '{inv.customer_text}' - no "
                                        "maintenance customer matched; not imported.", where=ref)
            continue
        contracts = [m for m in cu.maintenance_contracts if m.parent_contract_id is None] or cu.maintenance_contracts
        mc = max(contracts, key=lambda m: (m.ended_on is None, len(m.fees)))
        year = invoice_contract_year(inv.memo, inv.date, mc.anniversary_month)
        if year is None:
            report.add("warning", None, f"Invoice {inv.number}: contract year unknown - not imported.", where=ref)
            continue
        mc.invoices.append(MaintenanceInvoice(
            contract_year=year, invoice_no=inv.number,
            reseller_invoice=inv.number.upper().endswith("RI") or is_reseller_billed(inv.customer_text),
            invoice_date=inv.date, due_date=inv.due, amount=cents(inv.amount), memo=inv.memo[:200] or None,
            source_ref=ref[:60]))
        matched += 1
    report.maintenance_invoices = {"matched": matched, "unmatched": unmatched}
