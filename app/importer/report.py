"""Reconciliation of the imported database against the sheet, and the HTML/JSON import report."""
from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import metrics
from ..money import cents
from ..models import (LicensePurchase, MaintenanceInvoice, MaintenanceContract, MaintenanceFee, Customer, Invoice, NonRecurringFee, Platform, Subscription, Term, TermSupersession,
                      Termination, ControlCheck)
from .mapper import PLATFORM, TAB_LABEL, ImportReport
from .sheet.model import Workbook


def reconcile(session: Session, wb: Workbook, report: ImportReport, tolerance: float = 1.0,
              report_year: int | None = None, as_of=None) -> None:
    """report_year: differences in later years are flagged 'future' (the sheet often leaves them unfilled).
    as_of: count only terms in force on that date (signed-not-started terms are CARR; the sheet leaves their year
    cells blank too)."""
    import datetime as dt
    report_year = report_year or dt.date.today().year
    customers = {c.qb_number: c for c in metrics.load_customers(session)}
    totals: dict = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    for tab in wb.tabs.values():
        years = sorted(y for y in next(iter(tab.sections.values())).years)
        plat = PLATFORM[tab.key]
        sheet: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
        seen = set()
        for c in tab.contracts:
            if c.section == "E" or c.not_subscription:
                continue
            key = (c.section, c.customer_number, str(c.values.get("period")).strip(), round(c.arr, 2))
            if key in seen:
                continue
            seen.add(key)
            for y in years:
                sheet[c.customer_number][y] += c.year_arr(y) or 0.0
        numbers = set(sheet) | {n for n, cu in customers.items() if any(s.platform == plat for s in cu.subscriptions)}
        for n in sorted(numbers):
            cu = customers.get(n)
            for y in years:
                s_val = sheet[n][y]
                d_val = float(metrics.customer_year_arr(cu, y, plat, as_of=as_of)) if cu else 0.0    # report compares floats
                totals[TAB_LABEL[tab.key]][y][0] += s_val
                totals[TAB_LABEL[tab.key]][y][1] += d_val
                if abs(s_val - d_val) > tolerance:
                    terms = [t for s in (cu.subscriptions if cu else []) if s.platform == plat for t in s.terms
                             if t.start_date.year <= y <= t.end_date.year]
                    subs = [s for s in (cu.subscriptions if cu else []) if s.platform == plat]
                    cancels = [metrics.cancel_date(s) for s in subs if metrics.cancel_date(s)]
                    renew_years = {ss.new_term.start_date.year for s in subs for t in s.terms for ss in t.superseded_by}
                    projected = [t for s in subs for t in s.terms if metrics.projected_year_arr(t, y, as_of)]
                    p_sum = float(sum(metrics.projected_arr(t) for t in projected))
                    p_prior = float(sum(metrics.term_arr(t) for t in projected))
                    # R4 only when the projection explains the gap: the sheet leaves the year blank (gap = projected
                    # ARR) or carries the prior ARR by hand (gap = the uplift); any other gap is a real difference
                    if projected and (abs(d_val - s_val - p_sum) <= tolerance
                                      or abs(d_val - s_val - (p_sum - p_prior)) <= tolerance):
                        terms = projected
                        pct = ", ".join(sorted({f"{metrics.projected_uplift(t) * 100:g}%" for t in projected}))
                        why = (f"projected renewal: the contract ended with no renewal on file, so the database "
                               f"carries prior ARR + {pct} uplift until the renewal is recorded (an estimate)")
                    elif y in renew_years:
                        why = ("renewal transition year: the sheet keeps the old term's ARR for this year, the "
                               "database uses the renewal's")
                    elif s_val > d_val and cancels and min(cancels).year < y:
                        why = (f"sheet still carries ARR after the cancellation date "
                               f"({min(cancels):%m/%d/%Y}) - confirm with accounting")
                    elif d_val > s_val:
                        why = "sheet year cell blank/0 for a term covering the year (sheet undercounts)"
                    elif not terms:
                        why = "sheet has ARR in a year outside every imported term"
                    else:
                        why = "sheet counts ARR the database treats as superseded/cancelled or overridden"
                    report.reconciliation.append({
                        "platform": TAB_LABEL[tab.key], "customer": n, "name": cu.name if cu else "",
                        "year": y, "sheet": round(s_val, 2), "db": round(d_val, 2),
                        "diff": round(d_val - s_val, 2), "why": why, "future": y > report_year,
                        "terms": ", ".join(t.source_ref or f"term {t.id}" for t in terms)})
    report.reconciliation_totals = {p: {y: [round(a, 2), round(b, 2)] for y, (a, b) in sorted(ys.items())}
                                    for p, ys in totals.items()}


def reconcile_maintenance(session: Session, report: ImportReport) -> None:
    """Computed GL 4300 revenue / 12/31 deferred per calendar year vs the sheet's columns."""
    from ..models import MaintenanceContract
    contracts = list(session.scalars(select(MaintenanceContract)))
    for y in sorted(report.maintenance_sheet_revenue):
        rev = sum(metrics.maintenance_calendar_revenue(mc, y) for mc in contracts)
        dfr = sum(metrics.maintenance_deferred(mc, y) for mc in contracts)
        report.maintenance_revenue[y] = [report.maintenance_sheet_revenue.get(y, 0.0), float(cents(rev)),
                                         report.maintenance_sheet_deferred.get(y, 0.0), float(cents(dfr))]


def db_counts(session: Session) -> dict:
    def n(model):
        return session.scalar(select(func.count()).select_from(model))
    by_kind = dict(session.execute(select(Term.kind, func.count()).group_by(Term.kind)).all())
    return {"customers": n(Customer), "subscriptions": n(Subscription), "terms": n(Term),
            "terms_by_kind": {k.value: v for k, v in by_kind.items()},
            "supersessions": n(TermSupersession), "terminations": n(Termination), "invoices": n(Invoice),
            "control_checks": n(ControlCheck), "non_recurring_fees": n(NonRecurringFee),
            "maintenance_contracts": n(MaintenanceContract), "maintenance_fees": n(MaintenanceFee),
            "license_purchases": n(LicensePurchase), "maintenance_invoices": n(MaintenanceInvoice)}


def write(report: ImportReport, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.started.replace(":", "").replace("-", "")
    data = asdict(report)
    jpath = out_dir / "latest-import.json"
    jpath.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    hpath = out_dir / f"import-{stamp}.html"
    hpath.write_text(render_html(data), encoding="utf-8")
    return jpath, hpath


def render_html(d: dict) -> str:
    e = html.escape
    sev = Counter(n["severity"] for n in d["notes"])
    rows = "".join(f"<tr><td>{e(n['severity'])}</td><td>{e(n['ref'])}</td><td>{n['customer'] or ''}</td>"
                   f"<td>{e(n['message'])}</td></tr>" for n in d["notes"])
    rec = "".join(f"<tr><td>{r['platform']}</td><td>{r['customer']} {e(r['name'])}</td><td>{r['year']}</td>"
                  f"<td style='text-align:right'>{r['sheet']:,.2f}</td><td style='text-align:right'>{r['db']:,.2f}</td>"
                  f"<td style='text-align:right'>{r['diff']:,.2f}</td><td>{e(r['why'])}</td><td>{e(r['terms'])}</td></tr>"
                  for r in d["reconciliation"])
    tot = "".join(f"<tr><td>{p}</td>" + "".join(f"<td style='text-align:right'>{v[0]:,.0f}<br>{v[1]:,.0f}</td>"
                                                 for v in ys.values()) + "</tr>"
                  for p, ys in d["reconciliation_totals"].items())
    years = next(iter(d["reconciliation_totals"].values()), {}).keys()
    return f"""<!doctype html><meta charset="utf-8"><title>Import report</title>
<style>body{{font:13px Segoe UI,sans-serif;margin:20px}}table{{border-collapse:collapse}}td,th{{border:1px solid #ddd;
padding:3px 6px;vertical-align:top}}th{{background:#f4f4f4}}</style>
<h1>Import report {e(d['started'])}</h1><p>Source: {e(d['source'])}<br>SHA-256: {d['source_sha256']}</p>
<h2>Counts</h2><pre>{e(json.dumps({'sheet': d['sheet_counts'], 'database': d['db_counts']}, indent=1))}</pre>
<h2>ARR by year: sheet (top) vs database (bottom)</h2><table><tr><th></th>{''.join(f'<th>{y}</th>' for y in years)}</tr>{tot}</table>
<h2>Reconciliation differences ({len(d['reconciliation'])})</h2><table><tr><th>Platform</th><th>Customer</th><th>Year</th>
<th>Sheet</th><th>DB</th><th>Diff</th><th>Likely reason</th><th>Terms</th></tr>{rec}</table>
<h2>Notes ({', '.join(f'{k}: {v}' for k, v in sev.items())})</h2><table><tr><th>Type</th><th>Sheet ref</th>
<th>Customer</th><th>Message</th></tr>{rows}</table>"""
