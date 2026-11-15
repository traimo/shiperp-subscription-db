"""FastAPI UI on the subscription database. Run: uvicorn app.main:app --port 8001"""
from __future__ import annotations

import csv
import datetime as dt
import re
from urllib.parse import quote, urlencode
import getpass
import io
import json
import logging
import os
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select

from . import auth, config, csvsafe, metrics, services
from .db import SessionLocal
from .money import D, D0, ZERO, cents
from .importer.sheet.rules import HYBRID_CLASS
from .models import (AccountExec, AuditLog, CheckResult, ControlCheckType, Customer, NonRecurringFee, OpenedVia,
                     Platform, Subscription, Term, TerminationReason, TermKind)
from .services import ServiceError, TermInput

BASE = Path(__file__).parent
app = FastAPI(title="Subscription Database")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
SESSION_FACTORY = SessionLocal          # tests swap this
TODAY = None                            # tests may pin "today"

# The app runs on the user's own machine without sign-in, so a web page open in the same browser must not be able to
# drive it: only these host names are served (blocks DNS rebinding) and form posts must come from the app's own pages.
# "testserver" is the host name of Starlette's TestClient. More hosts: APP_ALLOWED_HOSTS=name1,name2 in .env.
AUTH = auth.Settings.from_env()       # Microsoft sign-in settings (installed below, after the host check)
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "[::1]", "testserver", *AUTH.allowed_hosts]


@app.middleware("http")
async def same_origin_posts(request: Request, call_next):
    """Refuse state-changing requests sent from another site (CSRF): Origin, or else Referer, must be this host."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        from urllib.parse import urlsplit
        source = request.headers.get("origin") or request.headers.get("referer")
        if source and (source == "null" or urlsplit(source).netloc != request.headers.get("host", "")):
            return HTMLResponse("Refused: this form was sent from another site.", status_code=403)
    return await call_next(request)


from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402



class _TrustedHostExceptHealth(TrustedHostMiddleware):
    """The host check, except for GET /healthz: container probes call it by IP / internal name (no data in it)."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/healthz":
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


app.add_middleware(_TrustedHostExceptHealth, allowed_hosts=ALLOWED_HOSTS)

auth.install(app, AUTH)          # Microsoft sign-in (AUTH_DISABLED=1 only when reachable on this machine alone)


@app.get("/healthz")
def healthz():
    """Liveness for Docker / container probes. Does not touch the database. On Azure it also says whether data/ is the
    Azure Files share, but stays 200 either way (a failing probe would block the deploy; the page banner is the alarm)."""
    body = {"status": "ok"}
    if os.environ.get("CONTAINER_APP_NAME"):
        body["data_mount"] = "missing" if config.data_mount_problem() else "ok"
    return JSONResponse(body)


if config.data_mount_problem():
    logging.getLogger(__name__).warning("DATA MOUNT: %s", config.data_mount_problem())


def today() -> dt.date:
    return TODAY or dt.date.today()


def actor() -> str:
    """Who made the change, for the audit log: the signed-in e-mail; APP_USER / the OS user when sign-in is off."""
    return auth.CURRENT_EMAIL.get() or os.environ.get("APP_USER") or getpass.getuser()


def money(v):
    if v is None or v == "":
        return ""
    v = cents(v)                         # half-up, like accounting (Decimal's own format rounds half-even)
    return f"({abs(v):,.2f})" if v < 0 else f"{v:,.2f}"


def _float(v):
    """Non-money form numbers (quantity) stay float."""
    return None if v is None else float(v)


def fdate(v):
    return v.strftime("%m/%d/%Y") if isinstance(v, (dt.date, dt.datetime)) else ("" if v is None else str(v))


templates.env.filters.update(money=money, fdate=fdate)
templates.env.globals.update(metrics=metrics, Platform=Platform, CheckResult=CheckResult,
                             TerminationReason=TerminationReason, data_mount_problem=config.data_mount_problem)


def render(request: Request, template: str, **ctx):
    ctx.setdefault("today", today())
    return templates.TemplateResponse(request, template, ctx)


def redirect(path: str, *, msg=None, error=None, undo=None, anchor: str = "", query: str = "",
             **params) -> RedirectResponse:
    """303 redirect to a same-site path. Query values are percent-encoded, so messages with & # + % arrive intact.
    Order: an existing query (in `path` or `query`), extra params, msg, error, undo; empty values are left out."""
    path, _, inline = path.partition("?")
    parts = [x for x in (inline, query.lstrip("?")) if x]
    pairs = [(k, v) for k, v in [*params.items(), ("msg", msg), ("error", error), ("undo", undo)]
             if v is not None and v != ""]
    if pairs:
        parts.append(urlencode(pairs, quote_via=quote))
    return RedirectResponse(path + ("?" + "&".join(parts) if parts else "") + (f"#{anchor}" if anchor else ""),
                            status_code=303)


def _safe_back(back, default: str) -> str:
    """A same-site path to return to (no open redirect): starts with one '/', no backslash, no control characters
    (browsers read a backslash or a tab after the first '/' as '//', i.e. another site)."""
    back = str(back or "")
    ok = (back.startswith("/") and not back.startswith("//") and "\\" not in back
          and not any(ord(ch) < 32 or ord(ch) == 127 for ch in back))
    return back if ok else default


def _text(form, key) -> str | None:
    """Form text: stripped, empty -> None."""
    return (form.get(key) or "").strip() or None


def _opt_int(form, key, strict: bool = True) -> int | None:
    """Optional whole number from a form; strict=False returns None for a bad value (used to redisplay a form)."""
    v = form.get(key)
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        if strict:
            raise
        return None


def _date(form, key, required=True):
    v = (form.get(key) or "").strip()
    if not v:
        if required:
            raise ServiceError(f"{key.replace('_', ' ').capitalize()} is required.")
        return None
    try:
        return dt.date.fromisoformat(v)
    except ValueError:
        raise ServiceError(f"{key}: '{v}' is not a date.") from None


def _num(form, key, required=True) -> Decimal | None:
    """Form number as Decimal (money stays exact; callers convert to float for non-money values)."""
    v = str(form.get(key) or "").replace(",", "").replace("$", "").strip()
    if not v:
        if required:
            raise ServiceError(f"{key.replace('_', ' ').upper() if key == 'tcv' else key} is required.")
        return None
    try:
        return D(v)
    except ValueError:
        raise ServiceError(f"{key}: '{v}' is not a number.") from None


def _ae_value(form) -> str | None:
    """AE drop-down: existing initials, or the typed initials when "Other..." was chosen."""
    v = form.get("ae")
    return form.get("ae_other") if v == "__other__" else v


def _ae_list() -> list[str]:
    with SESSION_FACTORY() as s:
        return list(s.scalars(select(AccountExec.initials).order_by(AccountExec.initials)))


templates.env.globals.update(ae_list=_ae_list, HYBRID_CLASSES=HYBRID_CLASS)


def term_input(form) -> TermInput:
    years = _num(form, "term_years", required=False)
    return TermInput(
        start_date=_date(form, "start_date"), end_date=_date(form, "end_date"), tcv=_num(form, "tcv"),
        signed_date=_date(form, "signed_date", required=False), term_months=float(years * 12) if years else None,
        payment_terms=(form.get("payment_terms") or "").strip() or None, ae=_ae_value(form),
        reseller=form.get("reseller"), golive_date=_date(form, "golive_date", required=False),
        transactional_volume=(form.get("transactional_volume") or "").strip() or None,
        renewal_note=(form.get("renewal_note") or "").strip() or None,
        next_action=(form.get("next_action") or "").strip() or None,
        notes=(form.get("notes") or "").strip() or None)


# --------------------------------------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, platform: str = "", outcome: str = ""):
    with SESSION_FACTORY() as s:
        customers = metrics.load_customers(s)
        years = [y for y in metrics.year_range(s) if y >= 2020]
        t = today()
        maint_now = metrics.maintenance_arr_total(customers, t.year, t)
        prepaid_now = metrics.maintenance_total(customers, t.year) - maint_now
        all_rows = metrics.renewal_worklist(customers, t, back_days=365, ahead_days=120)
        wanted = {"OPEN": set(metrics.OPEN_OUTCOMES)}.get(outcome, {outcome} if outcome else None)
        work = [w for w in all_rows if (not platform or w.term.subscription.platform.value == platform)
                and (not wanted or w.outcome in wanted)]
        tally = {k: sum(1 for w in all_rows if w.outcome == k
                        and (not platform or w.term.subscription.platform.value == platform))
                 for k in metrics.OUTCOMES}
        sub_now = metrics.arr_at(customers, t)
        ctx = dict(years=years, arr=metrics.arr_by_year(customers, years, t), sub_now=sub_now,
                   maint_now=maint_now, prepaid_now=prepaid_now, arr_now=sub_now + maint_now,
                   carr_now=metrics.carr_at(customers, t) + maint_now, work=work, tally=tally,
                   f=dict(platform=platform, outcome=outcome), OUTCOMES=metrics.OUTCOMES,
                   retention=[metrics.retention(customers, y, t) for y in (t.year - 1, t.year)],
                   new_customers=metrics.new_customers_by_year(customers),
                   counts={"customers": sum(1 for c in customers if c.subscriptions),
                           "maintenance": sum(1 for c in customers if metrics.maintenance_year_arr(c, t.year) > 0),
                           "active": sum(1 for c in customers for sub in c.subscriptions
                                         if metrics.subscription_status(sub, t) == "ACTIVE")})
        return render(request, "dashboard.html", **ctx)


@app.get("/customers", response_class=HTMLResponse)
def customers_page(request: Request, q: str = "", status: str = "", platform: str = ""):
    with SESSION_FACTORY() as s:
        t = today()
        rows = []
        for c in metrics.load_customers(s):
            subs = [x for x in c.subscriptions if not platform or x.platform.value == platform]
            if platform and not subs:
                continue
            st = {metrics.subscription_status(x, t) for x in subs}
            maint = metrics.maintenance_year_arr(c, t.year)
            cstat = next((x for x in ("ACTIVE", "ENDING", "CHURNED", "ENDED") if x in st),
                         "MAINTENANCE" if maint > 0 else "NONE")
            if status and cstat != status:
                continue
            if q and q.lower() not in c.name.lower() and q not in str(c.qb_number):
                continue
            plats = [x.platform.value for x in subs] + (["MAINTENANCE"] if maint > 0 and not platform else [])
            rows.append((c, cstat, metrics.customer_year_arr(c, t.year, as_of=t)
                         + (0 if platform else metrics.maintenance_arr(c, t.year, t)), plats))
        return render(request, "customers.html", rows=rows, f=dict(q=q, status=status, platform=platform))


@app.get("/customers/new", response_class=HTMLResponse)
def customer_new_form(request: Request, error: str = ""):
    return render(request, "customer_new.html", error=error, form={})


@app.post("/customers/new")
async def customer_new(request: Request):
    form = await request.form()
    try:
        qb = int(str(form.get("qb_number", "")).strip())
        services.run(SESSION_FACTORY, services.create_customer, actor(), qb, str(form.get("name", "")), commit=True)
    except (ServiceError, ValueError) as e:
        return render(request, "customer_new.html", error=str(e), form=dict(form))
    return redirect(f"/customer/{qb}", msg="Customer created")


@app.get("/customer/{qb}", response_class=HTMLResponse)
def customer_page(request: Request, qb: int, msg: str = "", error: str = ""):
    with SESSION_FACTORY() as s:
        c = metrics.load_customer(s, qb)
        if c is None:
            raise HTTPException(404, "Customer not found")
        years = [y for y in metrics.year_range(s) if y >= 2020]
        fees = list(s.scalars(select(NonRecurringFee).where(NonRecurringFee.customer_id == c.id)))
        audit = list(s.scalars(select(AuditLog).where(AuditLog.customer_qb == qb).order_by(AuditLog.at.desc())
                               .limit(30)))
        return render(request, "customer.html", c=c, years=years, fees=fees, audit=audit, msg=msg, error=error)


@app.get("/term/{term_id}", response_class=HTMLResponse)
def term_page(request: Request, term_id: int, msg: str = "", error: str = ""):
    with SESSION_FACTORY() as s:
        t = s.get(Term, term_id)
        if t is None:
            raise HTTPException(404, "Term not found")
        types = list(s.scalars(select(ControlCheckType).order_by(ControlCheckType.sort)))
        checks = {x.type_code: x for x in t.checks}
        sched = metrics.monthly_schedule(t)
        by_year: dict[int, float] = {}
        for d, v in sched:
            by_year[d.year] = by_year.get(d.year, 0) + v
        return render(request, "term.html", t=t, c=t.subscription.customer, types=types, checks=checks,
                      sched_by_year=by_year, msg=msg, error=error)


@app.post("/term/{term_id}/edit")
async def term_edit(request: Request, term_id: int):
    form = await request.form()
    try:
        changes = {k: (_date(form, k, required=False) if k.endswith("_date") else form.get(k))
                   for k in services.EDITABLE_TERM_FIELDS if k in form}
        out = services.run(SESSION_FACTORY, services.update_term, actor(), term_id, changes,
                           ae=_ae_value(form), reseller=form.get("reseller"), commit=True)
    except ServiceError as e:
        return redirect(f"/term/{term_id}", error=str(e))
    return redirect(f"/term/{term_id}", msg="Saved: " + "; ".join(out.lines) if out.lines else "No changes",
                    undo=out.audit_id)


@app.post("/term/{term_id}/checks")
async def term_checks(request: Request, term_id: int):
    form = await request.form()
    with SESSION_FACTORY() as s:
        codes = [x.code for x in s.scalars(select(ControlCheckType))]
    results = {c: (form.get(f"res_{c}") or "", form.get(f"note_{c}") or "") for c in codes if f"res_{c}" in form}
    try:
        out = services.run(SESSION_FACTORY, services.set_checks, actor(), term_id, results, commit=True)
    except (ServiceError, ValueError) as e:
        return redirect(f"/term/{term_id}", error=str(e))
    return redirect(f"/term/{term_id}", msg=f"Checklist: {len(out.lines)} change(s)")


@app.post("/term/{term_id}/invoice")
async def term_invoice(request: Request, term_id: int):
    form = await request.form()
    try:
        out = services.run(SESSION_FACTORY, services.record_invoice, actor(), term_id,
                           int(form.get("billing_year")), _date(form, "invoice_date", required=False),
                           _num(form, "amount"), form.get("reference"), commit=True)
    except (ServiceError, ValueError, TypeError) as e:
        return redirect(f"/term/{term_id}", error=str(e))
    return redirect(f"/term/{term_id}", msg=out.lines[0] + (" - " + "; ".join(out.warnings) if out.warnings else ""))


@app.post("/invoice/{invoice_id}/delete")
def invoice_delete(invoice_id: int, term_id: int):
    try:
        services.run(SESSION_FACTORY, services.delete_invoice, actor(), invoice_id, commit=True)
    except ServiceError as e:
        return redirect(f"/term/{term_id}", error=str(e))
    return redirect(f"/term/{term_id}", msg="Invoice removed")


# --------------------------------------------------------------------------------------------- workflows

WF_TITLES = {"new": "New subscription (net new)", "convert": "Conversion: perpetual to subscription",
             "addon": "Add-on (modules / plants)", "renew": "Renewal", "amend": "Amendment",
             "terminate": "Terminate subscription"}


def _wf_context(s, kind: str, qb: int | None, subscription: int | None, term: int | None) -> dict:
    t = s.get(Term, term) if term else None
    sub = t.subscription if t else (s.get(Subscription, subscription) if subscription else None)
    c = sub.customer if sub else (s.scalar(select(Customer).where(Customer.qb_number == qb)) if qb else None)
    pre: dict = {"signed_date": today().isoformat(), "payment_terms": "Annual"}
    rollups = []
    if t:
        pre.update(ae=t.ae.initials if t.ae else "", reseller=t.reseller.name if t.reseller else "",
                   payment_terms=t.payment_terms or "Annual")
        if kind == "renew":
            # other open terms ending near this one (any kind): merged into the one renewal contract
            rollups = [o for o in metrics.open_terms(t.subscription) if o is not t
                       and abs((o.end_date - t.end_date).days) <= 92]
            # default length from the main contract (not an add-on) when renewing from an add-on row
            main_t = t
            if t.kind == TermKind.ADD_ON:
                main_t = max((o for o in rollups if o.kind != TermKind.ADD_ON), key=lambda o: o.term_months,
                             default=t)
            start = t.end_date + dt.timedelta(days=1)
            months = round(main_t.term_months)
            y, m = divmod(start.month - 1 + months, 12)
            end = dt.date(start.year + y, m + 1, min(start.day, 28)) - dt.timedelta(days=1)
            pre.update(start_date=start.isoformat(), end_date=end.isoformat())
            if abs(main_t.term_months - metrics.months_between(main_t.start_date, main_t.end_date)) > 0.5:
                pre["term_years"] = round(main_t.term_months / 12, 2)
        if kind == "amend":
            pre.update(start_date=t.start_date.isoformat(), end_date=t.end_date.isoformat(), tcv=t.tcv)
            # only when the contract's term differs from its dates - otherwise a date change (co-terming) would
            # silently keep the old length and give a wrong ARR
            if abs(t.term_months - metrics.months_between(t.start_date, t.end_date)) > 0.5:
                pre["term_years"] = round(t.term_months / 12, 2)
        if kind == "addon":
            pre.update(end_date=t.end_date.isoformat())
    contracts, maint_lines = [], []
    if kind == "terminate" and sub:
        # contracts that can still be ended; all ticked = the whole subscription, a term link = just that contract
        contracts = [x for x in metrics.open_terms(sub) if not metrics.cancels_term(x)]
        picked = [t] if t and t in contracts else contracts
        pre["terms"] = {x.id for x in picked}
        live = [x for x in picked if metrics.is_live(x, today())]
        last = metrics.latest_open_term(sub)
        # live: end of the current term; lapsed without renewal: the day the last term ended
        default = max(x.end_date for x in live) if live else (last.end_date if last else today())
        pre["cancelled_on"] = default.isoformat()
        t = None                                              # the form is about the subscription
    if kind == "convert" and c:
        maint_lines = [mc for mc in c.maintenance_contracts if not mc.ended_on]
    return dict(kind=kind, title=WF_TITLES[kind], c=c, sub=sub, base=t, pre=pre, rollups=rollups,
                contracts=contracts, maint_lines=maint_lines,
                customers=list(s.scalars(select(Customer).order_by(Customer.qb_number))))


@app.get("/wf/{kind}", response_class=HTMLResponse)
def wf_form(request: Request, kind: str, customer: int | None = None, subscription: int | None = None,
            term: int | None = None):
    if kind not in WF_TITLES:
        raise HTTPException(404)
    with SESSION_FACTORY() as s:
        return render(request, "workflow.html", error="", **_wf_context(s, kind, customer, subscription, term))


def _dispatch(kind: str, form, commit: bool):
    a = actor()
    if kind == "convert":
        # a conversion replaces the perpetual licence; keeping it and adding a subscription (hybrid) is a New subscription
        if form.get("hybrid_class") or (form.get("license_category") or "").strip().lower() == "hybrid":
            raise ServiceError("A conversion replaces the perpetual licence. To keep the perpetual licence and add a "
                               "subscription (hybrid), use New subscription instead.")
        from . import maintenance_services as ms
        return services.run(SESSION_FACTORY, ms.convert_to_subscription, a, int(form.get("customer")),
                            Platform(form.get("platform")), term_input(form),
                            [int(x) for x in form.getlist("stop_line")], form.get("license_category") or None,
                            today=today(), commit=commit)
    if kind == "new":
        return services.run(SESSION_FACTORY, services.new_subscription, a, int(form.get("customer")),
                            Platform(form.get("platform")),
                            OpenedVia.NET_NEW if kind == "new" else OpenedVia.CONVERSION, term_input(form),
                            form.get("license_category") or None, form.get("hybrid_class") or None,
                            today=today(), commit=commit)
    if kind == "addon":
        return services.run(SESSION_FACTORY, services.add_on, a, int(form.get("subscription")), term_input(form),
                            int(form.get("coterminus")) if form.get("coterminus") else None, today=today(),
                            commit=commit)
    if kind == "renew":
        return services.run(SESSION_FACTORY, services.renew, a, int(form.get("term")), term_input(form),
                            [int(x) for x in form.getlist("rollup")], today=today(), commit=commit)
    if kind == "amend":
        return services.run(SESSION_FACTORY, services.amend, a, int(form.get("term")), term_input(form),
                            today=today(), commit=commit)
    if kind == "terminate":
        term_ids = [int(x) for x in form.getlist("terms")] if form.get("terms_listed") else None
        return services.run(SESSION_FACTORY, services.terminate, a, int(form.get("subscription")),
                            _date(form, "cancelled_on"), TerminationReason(form.get("reason")),
                            form.get("note") or None, today=today(), term_ids=term_ids, commit=commit)
    raise HTTPException(404)


@app.post("/wf/{kind}", response_class=HTMLResponse)
async def wf_submit(request: Request, kind: str):
    form = await request.form()
    confirm = form.get("_confirm") == "1"
    try:
        out = _dispatch(kind, form, commit=confirm)
    except (ServiceError, ValueError, TypeError) as e:
        with SESSION_FACTORY() as s:
            ctx = _wf_context(s, kind, _opt_int(form, "customer", strict=False),
                              _opt_int(form, "subscription", strict=False), _opt_int(form, "term", strict=False))
            ctx["pre"].update({k: v for k, v in form.items() if not k.startswith("_") and k not in ("terms",)})
            if form.get("terms_listed"):
                ctx["pre"]["terms"] = {int(x) for x in form.getlist("terms")}
            return render(request, "workflow.html", error=str(e), **ctx)
    if confirm:
        return redirect(f"/customer/{out.customer_qb}", msg=out.title + " saved", undo=out.audit_id)
    fields = [(k, v) for k, v in form.multi_items() if not k.startswith("_")]
    return render(request, "confirm.html", out=out, kind=kind, fields=fields)


@app.post("/api/derive")
async def api_derive(request: Request):
    d = await request.json()
    try:
        start, end = dt.date.fromisoformat(d.get("start_date")), dt.date.fromisoformat(d.get("end_date"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Enter start and end dates"})
    if end < start:
        return JSONResponse({"ok": False, "error": "End is before start"})
    try:
        tcv = D0(d.get("tcv"))
        years = D0(d.get("term_years"))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    months = years * 12 if years > 0 else Decimal(metrics.months_between(start, end))
    return JSONResponse({"ok": True, "term_years": float(round(months / 12, 2)), "months": float(months),
                         "arr": float(cents(metrics.computed_arr(tcv, months))),
                         "monthly": float(cents(tcv / months)),
                         "years": list(range(start.year, end.year + 1)),
                         "renewal_date": (end + dt.timedelta(days=1)).isoformat()})


# --------------------------------------------------------------------------------------------- reports

@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request, show: str = "current"):
    p = config.REPORT_DIR / "latest-import.json"
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    if data and show == "current":
        data["reconciliation"] = [r for r in data["reconciliation"] if not r.get("future")]
    name_notes = [n for n in (data or {}).get("notes", []) if n["message"].startswith("Masterlist names")]
    from .importer.cli import check_status
    check_msg, check_blocking = check_status()
    return render(request, "import.html", d=data, show=show, name_notes=name_notes, check_msg=check_msg,
                  check_blocking=check_blocking)


# --------------------------------------------------------------------------------------------- refresh data

@app.middleware("http")
async def refreshing_guard(request: Request, call_next):
    """While a refresh rebuilds the tables (a few seconds) other pages would fail - show a short notice instead."""
    from . import refresh
    if refresh.in_progress() and not request.url.path.startswith("/static"):
        return HTMLResponse("<!doctype html><meta http-equiv='refresh' content='4'><body style='font:15px Segoe UI,"
                            "sans-serif;padding:40px'>Refreshing data from the workbooks&hellip; this page reloads "
                            "in a few seconds.</body>", status_code=503)
    return await call_next(request)


def _last_import() -> dict | None:
    p = config.REPORT_DIR / "latest-import.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


@app.get("/refresh/dialog", response_class=HTMLResponse)
def refresh_dialog(request: Request):
    """Popup body for the header's Refresh data button."""
    from . import reapply, sharepoint
    from .importer.cli import check_status
    files, sp_error = [], ""
    try:
        files = list(sharepoint.resolve_all().values())
    except Exception as e:                       # not set up, no access, offline ...
        sp_error = str(e) if isinstance(e, sharepoint.SharePointError) else f"{type(e).__name__}: {e}"
    imp = _last_import()
    imported = dt.datetime.fromisoformat(imp["started"]) if imp and imp.get("started") else None
    with SESSION_FACTORY() as s:
        changes = [(a, reapply.classify(a)) for a in reapply.pending_changes(s)]
    check_msg, check_blocking = check_status()
    return templates.TemplateResponse(request, "_refresh_dialog.html", dict(
        files=files, sp_error=sp_error, imported=imported, changes=changes, check_msg=check_msg,
        check_blocking=check_blocking, n_can=sum(1 for _, k in changes if k == "can")))


@app.post("/refresh")
async def refresh_submit(request: Request):
    from starlette.concurrency import run_in_threadpool
    from . import refresh, sharepoint
    form = await request.form()
    reapply_changes = form.get("reapply") == "yes"            # default: do not reapply
    try:                                                       # always the SharePoint copies (single source of truth)
        await run_in_threadpool(refresh.run, SESSION_FACTORY, actor(), today(), reapply=reapply_changes,
                                pull_from_sharepoint=True)
    except (refresh.RefreshError, sharepoint.SharePointError) as e:
        return redirect("/refresh/result", error=str(e)[:1500])
    return redirect("/refresh/result", done=1)


@app.get("/refresh/result", response_class=HTMLResponse)
def refresh_result(request: Request, error: str = "", done: str = "", msg: str = ""):
    from . import refresh
    r = refresh.last_result()
    rows = []
    if r and r.get("before") and r.get("after"):
        b, a = r["before"], r["after"]
        rows.append(("ARR today", D(b["arr_now"]), D(a["arr_now"])))
        rows.append(("CARR today", D(b["carr_now"]), D(a["carr_now"])))
        for y in a["years"]:
            for k in ("Subscription", "Maintenance", "Total", "CARR"):
                rows.append((f"{y} {k} ARR" if k != "CARR" else f"{y} CARR", D(b["years"].get(y, {}).get(k, 0)),
                             D(a["years"][y][k])))
        rows.append(("Customers with subscriptions", b["customers"], a["customers"]))
        rows.append(("Subscription terms", b["terms"], a["terms"]))
    groups = {}
    for i in (r or {}).get("items", []):
        groups.setdefault(i["status"], []).append(i)
    return render(request, "refresh_result.html", r=r, rows=rows, groups=groups, error=error, done=done, msg=msg)


# --------------------------------------------------------------------------------------------- spreadsheet check

def _check_redirect(msg: str = "", error: str = "", query: str = "") -> RedirectResponse:
    return redirect("/check", msg=msg, error=error, query=query)


@app.get("/check", response_class=HTMLResponse)
def check_page(request: Request, severity: str = "", rule: str = "", tab: str = "", status: str = "open",
               q: str = "", customer: str = "", msg: str = "", error: str = ""):
    from . import checker
    res = checker.load()
    rows, by_rule, by_tab, changed = [], {}, {}, []
    if res is not None:
        changed = checker.sources_changed(res)
        for i in res.issues:
            if status == "open" and i.status == "accepted" or status == "accepted" and i.status != "accepted":
                continue
            if i.status != "accepted":
                by_rule.setdefault((i.rule, i.title, i.severity), 0)
                by_rule[(i.rule, i.title, i.severity)] += 1
                by_tab[i.tab] = by_tab.get(i.tab, 0) + 1
            if severity and i.severity != severity or rule and i.rule != rule or tab and i.tab != tab:
                continue
            if customer and str(i.customer or "") != customer.strip():
                continue
            if q and q.lower() not in f"{i.message} {i.current} {i.ref} {i.title}".lower():
                continue
            rows.append(i)
    f = dict(severity=severity, rule=rule, tab=tab, status=status, q=q, customer=customer)
    return render(request, "check.html", res=res, rows=rows, f=f, msg=msg, error=error, changed=changed,
                  by_rule=sorted(by_rule.items(), key=lambda kv: (checker.SEVERITIES.index(kv[0][2]), -kv[1])),
                  by_tab=by_tab, SEVERITIES=checker.SEVERITIES, SEVERITY_HELP=checker.SEVERITY_HELP,
                  counts=res.counts() if res else None, query=request.url.query)


@app.post("/check/run")
def check_run():
    from . import checker
    try:
        res = checker.run_check()
    except Exception as e:                                     # unreadable workbook etc.
        return _check_redirect(error=f"Check failed: {type(e).__name__}: {e}")
    n = res.counts()
    return _check_redirect(f"Checked: {n['error']['open']} errors, {n['decision']['open']} decisions, "
                           f"{n['warning']['open']} warnings, {n['info']['open']} info open.")


def _find_issue(res, issue_id: str):
    i = next((x for x in (res.issues if res else []) if x.id == issue_id), None)
    if i is None:
        raise HTTPException(404, "Issue not found - run the check again.")
    return i


@app.post("/check/accept")
async def check_accept(request: Request):
    from . import checker
    from .checker import accept
    form = await request.form()
    res = checker.load()
    i = _find_issue(res, str(form.get("id")))
    try:
        accept.accept(checker.CHECK_DIR, i, str(form.get("reason") or ""), actor())
    except ValueError as e:
        return _check_redirect(error=str(e), query=str(form.get("query") or ""))
    checker.save(res, checker.CHECK_DIR)
    return _check_redirect(f"Accepted {i.ref}: {i.title}", query=str(form.get("query") or ""))


@app.post("/check/reopen")
async def check_reopen(request: Request):
    from . import checker
    from .checker import accept
    form = await request.form()
    res = checker.load()
    i = _find_issue(res, str(form.get("id")))
    accept.reopen(checker.CHECK_DIR, i)
    checker.save(res, checker.CHECK_DIR)
    return _check_redirect(f"Re-opened {i.ref}", query=str(form.get("query") or ""))


@app.get("/check/issues.xlsx")
def check_issue_list():
    from fastapi.responses import FileResponse
    from . import checker
    from .checker import export
    res = checker.load()
    if res is None:
        raise HTTPException(404, "Run the check first.")
    p = export.issue_list(res, checker.CHECK_DIR / "spreadsheet-issues.xlsx")
    return FileResponse(p, filename=f"Spreadsheet issues {res.ran_at[:10]}.xlsx")


@app.get("/check/annotated/{workbook}.xlsx")
def check_annotated(workbook: str):
    from fastapi.responses import FileResponse
    from . import checker
    from .checker import export
    if workbook not in ("subscription", "maintenance"):
        raise HTTPException(404)
    res = checker.load()
    if res is None:
        raise HTTPException(404, "Run the check first.")
    try:
        p = export.annotated_copy(res, workbook, checker.CHECK_DIR)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from None
    return FileResponse(p, filename=p.name)


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, q: str = "", msg: str = "", error: str = ""):
    with SESSION_FACTORY() as s:
        stmt = select(AuditLog).order_by(AuditLog.at.desc()).limit(300)
        if q:
            stmt = stmt.where(or_(AuditLog.summary.contains(q), AuditLog.actor.contains(q)))
        return render(request, "audit.html", rows=list(s.scalars(stmt)), q=q, msg=msg, error=error)


def undoable(a: AuditLog) -> bool:
    """Show an Undo button (the safety check runs on the preview page)."""
    return a.action != "undo" and not a.undone_at and bool(a.changes)


templates.env.globals.update(undoable=undoable)


@app.get("/audit/{audit_id}/undo", response_class=HTMLResponse)
def undo_preview(request: Request, audit_id: int, back: str = "/audit"):
    from . import undo
    with SESSION_FACTORY() as s:
        a = s.get(AuditLog, audit_id)
        if a is None:
            raise HTTPException(404, "Change not found")
        try:
            undo.check(s, audit_id)
            reason, steps = "", undo.describe(a)
        except undo.UndoError as e:
            reason, steps = str(e), []
        return render(request, "undo_confirm.html", a=a, reason=reason, steps=steps,
                      back=_safe_back(back, "/audit"))


@app.post("/audit/{audit_id}/undo")
async def undo_submit(request: Request, audit_id: int):
    from . import undo
    form = await request.form()
    back = _safe_back(form.get("back"), "/audit")
    try:
        out = services.run(SESSION_FACTORY, undo.undo_change, actor(), audit_id, commit=True)
    except (undo.UndoError, ServiceError) as e:
        return redirect(back, error=str(e))
    if not _page_exists(back):                  # e.g. undoing "create customer" from that customer's page
        back = "/audit"
    return redirect(back, msg=out.lines[0][:300])


def _page_exists(path: str) -> bool:
    m = re.fullmatch(r"/(customer|term|maintenance)/(\d+)", path)
    if not m:
        return True
    model = {"customer": None, "term": Term, "maintenance": MaintenanceContract}[m.group(1)]
    with SESSION_FACTORY() as s:
        if model is None:
            return s.scalar(select(Customer.id).where(Customer.qb_number == int(m.group(2)))) is not None
        return s.get(model, int(m.group(2))) is not None


@app.get("/export/arr.csv")
def export_arr():
    with SESSION_FACTORY() as s:
        customers = metrics.load_customers(s)
        years = [y for y in metrics.year_range(s) if y >= 2020]
        t = today()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["customer_number", "customer_name", "platform", *years])
        for c in customers:
            name = csvsafe.text(c.name)                                      # text cell: no Excel formulas
            for p in Platform:
                vals = [cents(metrics.customer_year_arr(c, y, p, as_of=t)) for y in years]
                if any(vals):
                    w.writerow([c.qb_number, name, p.value, *vals])
                signed = [cents(metrics.customer_year_arr(c, y, p) - metrics.customer_year_arr(c, y, p, as_of=t))
                          for y in years]                                  # CARR only: signed, not started
                if any(signed):
                    w.writerow([c.qb_number, name, f"{p.value}_SIGNED_NOT_STARTED", *signed])
            vals = [cents(metrics.maintenance_arr(c, y, t)) for y in years]
            if any(vals):
                w.writerow([c.qb_number, name, "MAINTENANCE", *vals])
            vals = [cents(metrics.converted_maintenance(c, y, t)) for y in years]  # billed, not ARR
            if any(vals):
                w.writerow([c.qb_number, name, "MAINTENANCE_PREPAID_CONVERTED", *vals])
        buf.seek(0)
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=arr_by_year.csv"})


# --------------------------------------------------------------------------------------------- database viewer (read-only)

from . import dbviewer  # noqa: E402


@app.get("/db", response_class=HTMLResponse)
def db_index(request: Request):
    from . import backups
    with SESSION_FACTORY() as s:
        infos = dbviewer.all_tables(s)
        size = dbviewer.database_size(s)
        found = backups.list_all(s.get_bind())
        return render(request, "db_index.html", infos=infos, svg=dbviewer.diagram_svg(infos),
                      db_label=config.database_label(), db_size=size, backups=found, keep=backups.KEEP)


@app.get("/db/{table}", response_class=HTMLResponse)
def db_table(request: Request, table: str, q: str = "", sort: str = "", desc: int = 0, page: int = 1,
             where: str = ""):
    if table not in dbviewer.table_names():
        raise HTTPException(404, f"No table named {table!r}")
    filters = {}
    for part in [p for p in where.split(",") if p]:
        col, _, val = part.partition(":")
        filters[col] = val
    with SESSION_FACTORY() as s:
        info = dbviewer.table_info(s, table)
        try:
            result = dbviewer.rows(s, table, q=q, sort=sort, descending=bool(desc), page=page, where=filters)
        except KeyError as e:
            raise HTTPException(400, f"Unknown column {e.args[0]!r} for {table}") from None
        return render(request, "db_table.html", info=info, result=result,
                      f=dict(q=q, sort=sort, desc=desc, where=where), filters=filters)


# --------------------------------------------------------------------------------------------- maintenance

from . import maintenance_services as ms  # noqa: E402
from .models import (BLOCK_LABELS, LicenseProduct, MaintenanceBlock, MaintenanceContract,  # noqa: E402
                     MaintenanceEndReason, PurchaseKind)

templates.env.globals.update(BLOCK_LABELS=BLOCK_LABELS, MaintenanceBlock=MaintenanceBlock,
                             LicenseProduct=LicenseProduct, PurchaseKind=PurchaseKind,
                             MaintenanceEndReason=MaintenanceEndReason, year_label=metrics.year_label,
                             MONTHS=["", "January", "February", "March", "April", "May", "June", "July", "August",
                                     "September", "October", "November", "December"])


def _maint_contracts(s):
    from sqlalchemy.orm import selectinload
    stmt = (select(MaintenanceContract).options(
        selectinload(MaintenanceContract.fees), selectinload(MaintenanceContract.purchases),
        selectinload(MaintenanceContract.invoices), selectinload(MaintenanceContract.customer)))
    return list(s.scalars(stmt))


@app.get("/maintenance", response_class=HTMLResponse)
def maintenance_page(request: Request, year: int | None = None, block: str = "", q: str = "", flag: str = "",
                     month: int | None = None, msg: str = ""):
    t = today()
    year = year or t.year
    with SESSION_FACTORY() as s:
        all_mc = _maint_contracts(s)
        totals = {b.value: [0, ZERO, ZERO] for b in MaintenanceBlock}    # lines, fee year, fee year-1
        fee_maps = {mc.id: metrics.fee_map(mc) for mc in all_mc}
        for mc in all_mc:
            fees = fee_maps[mc.id]
            tb = totals[mc.block.value]
            tb[0] += 1
            tb[1] += fees.get(year) or 0
            tb[2] += fees.get(year - 1) or 0
        grand = (sum((v[1] for v in totals.values()), ZERO), sum((v[2] for v in totals.values()), ZERO))
        rows = []
        for mc in all_mc:
            fees = fee_maps[mc.id]
            if block and mc.block.value != block:
                continue
            if month and (mc.anniversary_month or 0) != month:
                continue
            if q and q.lower() not in mc.customer.name.lower() and q not in str(mc.customer.qb_number):
                continue
            if flag == "missing_next" and not (fees.get(year) and year + 1 not in fees and not mc.ended_on):
                continue
            if flag == "stops_next" and not (fees.get(year) and fees.get(year + 1) == 0):
                continue
            if flag == "ended" and not mc.ended_on:
                continue
            if flag == "not_live" and mc.go_live:
                continue
            if flag == "paying" and not fees.get(year):
                continue
            rows.append((mc, metrics.maintenance_pricing(mc, t), fees))     # priced only when shown
        rows.sort(key=lambda r: (-(r[2].get(year) or 0), r[0].customer.name))
        checks = ms.validations(s, t)
        return render(request, "maint_index.html", rows=rows, totals=totals, grand=grand, year=year,
                      years=list(range(year - 3, year + 2)), checks=checks, msg=msg,
                      f=dict(block=block, q=q, flag=flag, month=month or ""))


@app.get("/maintenance/{contract_id}", response_class=HTMLResponse)
def maintenance_contract_page(request: Request, contract_id: int, msg: str = "", error: str = ""):
    t = today()
    with SESSION_FACTORY() as s:
        mc = s.get(MaintenanceContract, contract_id)
        if mc is None:
            raise HTTPException(404, "Maintenance contract not found")
        p = metrics.maintenance_pricing(mc, t)
        keys = sorted({f.year for f in mc.fees} | {p.current_key, p.current_key + 1})
        first = max(2020, min(keys) - 1) if keys else t.year
        cal_years = list(range(first, t.year + 2))
        schedule = {y: {"months": [cents(metrics.maintenance_month_revenue(mc, y, m)) for m in range(1, 13)],
                        "revenue": metrics.maintenance_calendar_revenue(mc, y),
                        "deferred": metrics.maintenance_deferred(mc, y)} for y in cal_years}
        invoiced = {}
        for inv in mc.invoices:
            invoiced[inv.contract_year] = invoiced.get(inv.contract_year, 0) + inv.amount
        subs = list(mc.customer.subscriptions)
        audit = list(s.scalars(select(AuditLog).where(AuditLog.entity == ms.ENTITY, AuditLog.entity_id == mc.id)
                               .order_by(AuditLog.at.desc()).limit(20)))
        return render(request, "maint_contract.html", mc=mc, pr=p, keys=keys, schedule=schedule,
                      invoiced=invoiced, subs=subs, audit=audit, msg=msg, error=error,
                      validations=[v for v in ms.validations(s, t, customer_id=mc.customer_id)
                                   if v["contract"].id == mc.id])


def _redirect_contract(cid, msg="", error="", undo_id=None):
    return redirect(f"/maintenance/{cid}", msg=msg or None, error=None if msg else error, undo=undo_id)


@app.post("/maintenance/{contract_id}/fee")
async def maintenance_fee(request: Request, contract_id: int):
    form = await request.form()
    try:
        year = int(form.get("year"))
        raw = str(form.get("amount", "")).replace(",", "").strip()
        amount = None if raw == "" else D(raw)
        out = services.run(SESSION_FACTORY, ms.set_fee, actor(), contract_id, year, amount, form.get("note"),
                           commit=True)
    except (ServiceError, ValueError, TypeError) as e:
        return _redirect_contract(contract_id, error=str(e))
    return _redirect_contract(contract_id, msg="; ".join(out.lines + out.warnings) or "No change")


@app.post("/maintenance/{contract_id}/details")
async def maintenance_details(request: Request, contract_id: int):
    form = await request.form()
    try:
        changes = {"notes": _text(form, "notes"), "golive_note": _text(form, "golive_note"),
                   "calendar_billing": form.get("calendar_billing") == "on",
                   "block": MaintenanceBlock(form.get("block")), "anniversary_month": _opt_int(form, "anniversary_month"),
                   "product": LicenseProduct(form.get("product")) if form.get("product") else None}
        out = services.run(SESSION_FACTORY, ms.update_details, actor(), contract_id, changes, commit=True)
    except (ServiceError, ValueError) as e:
        return _redirect_contract(contract_id, error=str(e))
    return _redirect_contract(contract_id, msg=out.title)


@app.post("/maintenance/{contract_id}/invoice")
async def maintenance_invoice_add(request: Request, contract_id: int):
    form = await request.form()
    try:
        out = services.run(SESSION_FACTORY, ms.record_invoice, actor(), contract_id, int(form.get("year")),
                           str(form.get("invoice_no", "")), _date(form, "invoice_date"), _num(form, "amount"),
                           _date(form, "due_date", required=False), form.get("memo"),
                           form.get("reseller") == "on", commit=True)
    except (ServiceError, ValueError, TypeError) as e:
        return _redirect_contract(contract_id, error=str(e))
    return _redirect_contract(contract_id, msg="; ".join(out.lines + out.warnings))


@app.post("/maintenance/invoice/{invoice_id}/delete")
def maintenance_invoice_delete(invoice_id: int, contract_id: int):
    try:
        services.run(SESSION_FACTORY, ms.delete_invoice, actor(), invoice_id, commit=True)
    except ServiceError as e:
        return _redirect_contract(contract_id, error=str(e))
    return _redirect_contract(contract_id, msg="Invoice removed")


MAINT_WF = {"new": "New perpetual licence", "live": "Mark live (start maintenance)",
            "purchase": "Additional purchase (module / plant / carrier)", "decommission": "Decommission",
            "convert": "Convert to subscription (stop maintenance)", "terminate": "Terminate maintenance",
            "roll": "Annual roll (uplift)"}


def _maint_wf_ctx(s, kind, contract_id=None, customer=None, year=None, rate=None, month=None):
    mc = s.get(MaintenanceContract, contract_id) if contract_id else None
    cust = mc.customer if mc else (s.scalar(select(Customer).where(Customer.qb_number == customer))
                                   if customer else None)
    t = today()
    ctx = dict(kind=kind, title=MAINT_WF[kind], mc=mc, c=cust, error="", today=t, form={},
               customers=list(s.scalars(select(Customer).order_by(Customer.qb_number))))
    if mc:
        p = metrics.maintenance_pricing(mc, t)
        ctx.update(pr=p, next_year=p.current_key + 1, subs=list(mc.customer.subscriptions))
    if kind == "roll":
        year = year or (t.year + 1)
        rate = 4.0 if rate is None else rate
        cands = ms.roll_candidates(s, year, month)
        ctx.update(year=year, rate=rate, month=month or "",
                   cands=[(m, metrics.fee_for(m, year - 1), ms.roll_amount(m, year, rate / 100)) for m in cands])
    return ctx


@app.get("/maintenance/wf/{kind}", response_class=HTMLResponse)
def maint_wf_form(request: Request, kind: str, contract: int | None = None, customer: int | None = None,
                  year: int | None = None, rate: float | None = None, month: int | None = None):
    if kind not in MAINT_WF:
        raise HTTPException(404)
    with SESSION_FACTORY() as s:
        return render(request, "maint_workflow.html", **_maint_wf_ctx(s, kind, contract, customer, year, rate, month))


def _maint_dispatch(kind, form, commit):
    a = actor()
    cid = int(form.get("contract")) if form.get("contract") else None
    if kind == "new":
        pct = _num(form, "maintenance_pct") / 100
        return services.run(SESSION_FACTORY, ms.new_perpetual, a, int(form.get("customer")),
                            LicenseProduct(form.get("product") or "PERPETUAL"), _date(form, "contract_date"),
                            _num(form, "list_price"), _num(form, "discount", required=False) or ZERO, pct,
                            int(form.get("anniversary_month")) if form.get("anniversary_month") else None,
                            form.get("calendar_billing") == "on", _date(form, "golive_date", required=False),
                            form.get("notes") or None, commit=commit)
    if kind == "live":
        return services.run(SESSION_FACTORY, ms.mark_live, a, cid, _date(form, "golive_date"), commit=commit)
    if kind in ("purchase", "decommission"):
        k = PurchaseKind.DECOMMISSION if kind == "decommission" else PurchaseKind(form.get("purchase_kind"))
        return services.run(SESSION_FACTORY, ms.add_purchase, a, cid, k, str(form.get("description", "")),
                            _num(form, "amount", required=False) or ZERO, _num(form, "maintenance_amount"),
                            int(form.get("effective_year")), _float(_num(form, "quantity", required=False)),
                            _date(form, "golive_date", required=False), commit=commit)
    if kind == "convert":
        sid = int(form.get("subscription")) if form.get("subscription") else None
        return services.run(SESSION_FACTORY, ms.convert, a, cid, _date(form, "on"), sid, commit=commit)
    if kind == "terminate":
        return services.run(SESSION_FACTORY, ms.terminate, a, cid, _date(form, "on"),
                            MaintenanceEndReason(form.get("reason")), form.get("note"), commit=commit)
    if kind == "roll":
        ids = [int(x) for x in form.getlist("pick")]
        return services.run(SESSION_FACTORY, ms.annual_roll, a, int(form.get("year")), _num(form, "rate") / 100,
                            int(form.get("month")) if form.get("month") else None, ids, commit=commit)
    raise HTTPException(404)


@app.post("/maintenance/wf/{kind}", response_class=HTMLResponse)
async def maint_wf_submit(request: Request, kind: str):
    form = await request.form()
    confirm = form.get("_confirm") == "1"
    try:
        out = _maint_dispatch(kind, form, confirm)
    except (ServiceError, ValueError, TypeError, KeyError) as e:
        with SESSION_FACTORY() as s:
            try:
                rate = float(form.get("rate")) if form.get("rate") else None
            except ValueError:
                rate = None
            ctx = _maint_wf_ctx(s, kind, _opt_int(form, "contract", strict=False),
                                _opt_int(form, "customer", strict=False), _opt_int(form, "year", strict=False), rate,
                                _opt_int(form, "month", strict=False))
            ctx["error"] = str(e)
            ctx["form"] = dict(form)
            return render(request, "maint_workflow.html", **ctx)
    if confirm:
        if kind == "roll":
            return redirect("/maintenance", year=form.get("year"), msg="Annual roll saved", undo=out.audit_id)
        return _redirect_contract(out.entity_id, msg=out.title + " saved", undo_id=out.audit_id)
    fields = [(k, v) for k, v in form.multi_items() if not k.startswith("_")]
    return render(request, "maint_confirm.html", out=out, kind=kind, fields=fields)


# --------------------------------------------------------------------------------------------- contract documents

from fastapi.responses import Response  # noqa: E402

from . import document_services as docs  # noqa: E402
from .models import ContractDocument  # noqa: E402

templates.env.globals.update(DOC_TYPE_LABELS=docs.DOC_TYPE_LABELS)


def _attachment_headers(filename: str) -> dict:
    """Download as an attachment with the original name (RFC 5987 for non-ASCII, ASCII fallback for old clients)."""
    ascii_name = filename.encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
    return {"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"}


def _back_with(url: str, ok: list[str], errors: list[str], anchor: str = "documents") -> RedirectResponse:
    return redirect(url, msg="; ".join(ok), error="; ".join(errors), anchor=anchor)


def _link_one(form, **target) -> tuple[list[str], list[str]]:
    try:
        out = services.run(SESSION_FACTORY, docs.link, actor(), form.get("url") or "", name=form.get("name"),
                           doc_type=form.get("doc_type") or "SIGNED_CONTRACT", note=form.get("note"),
                           commit=True, **target)
        return out.lines, out.warnings
    except (ServiceError, ValueError) as e:
        return [], [str(e)]


async def _documents_post(request: Request, default_back: str, **target) -> RedirectResponse:
    """Link a SharePoint file (the only way to add a contract; uploads were removed). `back` lets the customer page
    post here and return to itself."""
    form = await request.form()
    back = _safe_back(form.get("back"), default_back)
    if any(getattr(f, "filename", "") for f in form.getlist("files")):
        ok, errors = [], ["Uploads are turned off - link the file on SharePoint instead."]
    else:
        ok, errors = _link_one(form, **target)
    return _back_with(back, ok, errors, str(form.get("anchor") or "documents"))


@app.post("/term/{term_id}/documents")
async def term_documents(request: Request, term_id: int):
    return await _documents_post(request, f"/term/{term_id}", term_id=term_id)


@app.post("/maintenance/{contract_id}/documents")
async def maintenance_documents(request: Request, contract_id: int):
    return await _documents_post(request, f"/maintenance/{contract_id}", contract_id=contract_id)


@app.get("/document/{doc_id}")
def document_open(doc_id: int):
    """Open the file in SharePoint (the viewer's own access)."""
    with SESSION_FACTORY() as s:
        d = s.get(ContractDocument, doc_id)
        if d is None:
            raise HTTPException(404, "Document not found")
        return RedirectResponse(d.url, status_code=302)


@app.post("/document/{doc_id}/delete")
def document_delete(doc_id: int, back: str = "/"):
    url = _safe_back(back, "/")
    try:
        out = services.run(SESSION_FACTORY, docs.delete, actor(), doc_id, commit=True)
    except ServiceError as e:
        return _back_with(url, [], [str(e)])
    return _back_with(url, out.lines, [])


@app.get("/customer/{qb}/contract-links.csv")
def customer_contract_links(qb: int):
    with SESSION_FACTORY() as s:
        try:
            name, text_, _ = docs.customer_links_csv(s, qb)
        except ServiceError as e:
            raise HTTPException(404, str(e)) from None
    return Response(text_, media_type="text/csv", headers=_attachment_headers(name))
