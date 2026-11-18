"""Accounting feedback 2026-11-13 (Marilou): end one contract of a subscription, conversion stops maintenance and its
future-year maintenance is not ARR from signing, no hybrid in Conversion, merge popup can be cancelled, keep-ARR
amendments, add-on explained."""
import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main, metrics, maintenance_services as ms, refresh, services, undo
from app.models import AuditLog, Customer, MaintenanceContract, Platform, Subscription, Term, TerminationReason
from app.services import TermInput
from tests.conftest import D, needs_source
from tests.test_refresh import env  # noqa: F401  (fixture: private DB copy + paths)

TODAY = D(2026, 11, 13)


def run(S, op, *a, **kw):
    return services.run(S, op, "tester", *a, commit=True, **kw)


def ref(S, source_ref):
    with S() as s:
        return s.scalar(select(Term.id).where(Term.source_ref == source_ref))


def cust(s, qb):
    return next(c for c in metrics.load_customers(s) if c.qb_number == qb)


# ------------------------------------------------------------------------------------------------ terminate one contract

@needs_source
def test_terminate_only_one_contract(env):
    S, eng, paths = env
    base, addon = ref(S, "On-Prem A47"), ref(S, "On-Prem C146")          # Cytiva: initial + add-on
    with S() as s:
        sub_id = s.get(Term, base).subscription_id
        arr_addon = metrics.term_arr(s.get(Term, addon))
        before = metrics.customer_year_arr(cust(s, 80221), 2026, as_of=TODAY)
    out = run(S, services.terminate, sub_id, D(2025, 12, 31), TerminationReason.CHURN, today=TODAY,
              term_ids=[addon])
    assert out.lines[0].startswith(f"Ended contract term {addon}")
    with S() as s:
        sub = s.get(Subscription, sub_id)
        t_base, t_addon = s.get(Term, base), s.get(Term, addon)
        assert metrics.cancel_date(sub) is None                          # the subscription is not cancelled
        assert metrics.subscription_status(sub, TODAY) == "ACTIVE"
        assert metrics.term_status(t_addon, TODAY) == "CANCELLED"
        assert metrics.term_status(t_base, TODAY) == "ACTIVE"
        assert t_addon not in metrics.open_terms(sub) and t_base in metrics.open_terms(sub)
        after = metrics.customer_year_arr(cust(s, 80221), 2026, as_of=TODAY)
        assert after == before - arr_addon                              # only the add-on's ARR stops
        a = s.scalars(select(AuditLog).order_by(AuditLog.id.desc())).first()
        assert json.loads(a.detail)["terms"] == [addon]
    run(S, undo.undo_change, out.audit_id)                               # and it can be undone
    with S() as s:
        assert s.get(Subscription, sub_id).terminations == []


@needs_source
def test_terminate_all_ticked_cancels_the_subscription(env):
    S, eng, paths = env
    base = ref(S, "On-Prem A47")
    with S() as s:
        sub = s.get(Term, base).subscription
        sub_id, ids = sub.id, [t.id for t in metrics.open_terms(sub)]
    run(S, services.terminate, sub_id, D(2026, 12, 30), TerminationReason.CHURN, today=TODAY, term_ids=ids)
    with S() as s:
        sub = s.get(Subscription, sub_id)
        assert metrics.cancel_date(sub) == D(2026, 12, 30) and sub.terminations[0].scope == []


@needs_source
def test_terminate_form_and_partial_reapply(env):
    S, eng, paths = env
    base, addon = ref(S, "On-Prem A47"), ref(S, "On-Prem C146")
    with S() as s:
        sub_id = s.get(Term, base).subscription_id
    html = TestClient(main.app).get(f"/wf/terminate?subscription={sub_id}&term={addon}").text
    assert f'value="{addon}" checked' in html and f'value="{base}" checked' not in html
    res = TestClient(main.app).post("/wf/terminate", data={
        "subscription": sub_id, "cancelled_on": "2026-06-30", "reason": "CHURN", "terms_listed": "1",
        "terms": str(addon), "_confirm": "1"}, follow_redirects=False)
    assert res.status_code == 303
    r = refresh.run(S, "tester", TODAY, reapply=True, pull_from_sharepoint=False, paths=paths)
    item = next(i for i in r["items"] if i["action"] == "terminate" and str(addon) in i["summary"])
    assert item["status"] == "reapplied", item
    with S() as s:
        x = s.get(Subscription, sub_id).terminations[-1]
        assert {y.term.source_ref for y in x.scope} == {"On-Prem C146"}


# ------------------------------------------------------------------------------------------------ conversion

@needs_source
def test_conversion_stops_maintenance_from_the_next_contract_year(env):
    """Belimo: conversion signed 11/13/2026, starts 12/01/2026. The maintenance line is converted in the same step:
    explicit 0 from the first contract year starting on/after 12/01/2026; what is still billed stays ARR (a Hybrid
    account counts both, 2026-11-18)."""
    S, eng, paths = env
    with S() as s:
        lines = [mc.id for mc in s.scalars(select(MaintenanceContract).join(Customer)
                                           .where(Customer.qb_number == 80018, MaintenanceContract.ended_on.is_(None)))]
        c = cust(s, 80018)
        m26, m27 = metrics.maintenance_year_arr(c, 2026), metrics.maintenance_year_arr(c, 2027)
    assert lines and m26 > 0
    out = run(S, ms.convert_to_subscription, 80018, Platform.ON_PREM,
              TermInput(start_date=D(2026, 12, 1), end_date=D(2029, 11, 30), tcv=30000, signed_date=TODAY), lines,
              today=TODAY)
    assert not any("maintenance workbook" in w for w in out.warnings)
    with S() as s:
        c = cust(s, 80018)
        assert all(mc.ended_on == D(2026, 12, 1) for mc in c.maintenance_contracts if mc.id in lines)
        assert metrics.maintenance_year_arr(c, 2027) == m27                  # 2026-2027 is still billed: ARR
        assert metrics.maintenance_year_arr(c, 2028) == 0                    # explicit 0 from 2027-2028
        assert metrics.effective_year_arr(c, 2026, TODAY) == m26
        assert metrics.effective_year_arr(c, 2026, D(2026, 12, 15)) == m26 + 10000
        assert metrics.customer_year_arr(c, 2027, as_of=TODAY) == 0          # the subscription itself is CARR
        assert metrics.carr_year(c, 2027) == 10000 + m27
        rows = s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(1 + len(lines))).all()[::-1]
        assert [json.loads(r.detail or "{}").get("part_of") for r in rows[1:]] == [out.audit_id] * len(lines)
    run(S, undo.undo_change, out.audit_id)                                   # one Undo reverses all of it
    with S() as s:
        c = cust(s, 80018)
        assert all(mc.ended_on is None for mc in c.maintenance_contracts if mc.id in lines)
        assert all(a.undone_at for a in s.scalars(select(AuditLog).where(AuditLog.action != "undo",
                                                                          AuditLog.id >= out.audit_id)))
        assert metrics.maintenance_year_arr(c, 2027) == m27


@needs_source
def test_conversion_and_new_forms_have_no_hybrid_fields(env):
    """License Category / Hybrid class come from the workbook, per customer - no form field sets them."""
    S, eng, paths = env
    client = TestClient(main.app)
    html = client.get("/wf/convert?customer=80018").text
    assert 'name="hybrid_class"' not in html and 'name="license_category"' not in html
    assert 'name="stop_line"' in html and "checked" in html
    new = client.get("/wf/new?customer=80018").text
    assert 'name="hybrid_class"' not in new and 'name="license_category"' not in new


# ------------------------------------------------------------------------------------------------ forms

@needs_source
def test_merge_popup_can_be_cancelled_and_amend_offers_keep_arr(env):
    S, eng, paths = env
    client = TestClient(main.app)
    html = client.get(f"/wf/renew?term={ref(S, 'On-Prem C146')}").text
    assert 'id="merge-none"' in html and "Cancel renewal" in html and "/customer/80221" in html
    amend = client.get(f"/wf/amend?term={ref(S, 'On-Prem A47')}").text
    assert 'id="keep-arr"' in amend and 'id="d-change"' in amend
    assert "Add-on</b> = extra modules" in client.get(f"/wf/addon?term={ref(S, 'On-Prem A47')}").text


def test_derive_returns_exact_months():
    d = TestClient(main.app).post("/api/derive", json={"start_date": "2025-06-01", "end_date": "2027-06-30",
                                                        "tcv": "75000", "term_years": ""}).json()
    assert d["months"] == 25 and d["term_years"] == 2.08
    keep_tcv = round(25000 * d["months"] / 12, 2)                           # what "Keep current ARR" fills in
    assert round(float(metrics.computed_arr(keep_tcv, d["months"])), 2) == 25000.0
