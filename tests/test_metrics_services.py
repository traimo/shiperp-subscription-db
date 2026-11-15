import pytest
from sqlalchemy import func, select

from app import metrics, services
from app.models import AuditLog, CheckResult, ControlCheckType, OpenedVia, Platform, Term, TerminationReason
from app.services import ServiceError, TermInput
from tests.conftest import D, seed

TODAY = D(2026, 11, 12)


def ti(start, end, tcv, **kw):
    return TermInput(start_date=start, end_date=end, tcv=tcv, signed_date=start, **kw)


def run(factory, op, *a, commit=True, **kw):
    return services.run(factory, op, "tester", *a, commit=commit, **kw)


def test_year_convention_and_renewal(blank):
    _, sub_id, t_id = seed(blank)
    out = run(blank, services.renew, t_id, ti(D(2027, 1, 1), D(2029, 12, 31), 39600), today=TODAY)
    assert out.arr_before[2027] == 0 and out.arr_after[2027] == 13200
    assert "+1,200.00" in out.lines[2]
    with blank() as s:
        c = metrics.load_customers(s)[0]
        old = s.get(Term, t_id)
        assert [metrics.term_year_arr(old, y) for y in (2024, 2026, 2027)] == [12000, 12000, 0]
        assert metrics.customer_year_arr(c, 2028) == 13200
        assert metrics.term_status(old, D(2027, 2, 1)) == "SUPERSEDED"
        assert s.scalar(select(func.count()).select_from(AuditLog)) == 1


def test_renewal_rules(blank):
    _, _, t_id = seed(blank)
    with pytest.raises(ServiceError, match="31 days"):
        run(blank, services.renew, t_id, ti(D(2027, 6, 1), D(2029, 12, 31), 1), today=TODAY)
    run(blank, services.renew, t_id, ti(D(2027, 1, 1), D(2029, 12, 31), 1), today=TODAY)
    with pytest.raises(ServiceError, match="already"):
        run(blank, services.renew, t_id, ti(D(2027, 1, 1), D(2029, 12, 31), 1), today=TODAY)


def test_preview_rolls_back(blank):
    _, _, t_id = seed(blank)
    out = run(blank, services.renew, t_id, ti(D(2027, 1, 1), D(2029, 12, 31), 36000), commit=False, today=TODAY)
    assert out.lines
    with blank() as s:
        assert s.scalar(select(func.count()).select_from(Term)) == 1
        assert s.scalar(select(func.count()).select_from(AuditLog)) == 0


def test_addon_rollup_and_coterminus(blank):
    _, sub_id, t_id = seed(blank)
    a = run(blank, services.add_on, sub_id, ti(D(2025, 7, 1), D(2025, 12, 31), 9000), coterminus_term_id=t_id,
            today=TODAY)
    with blank() as s:
        add = s.get(Term, a.entity_id)
        assert add.end_date == D(2026, 12, 31) and add.term_months == 18
        assert metrics.term_arr(add) == 6000
    r = run(blank, services.renew, t_id, ti(D(2027, 1, 1), D(2029, 12, 31), 60000), [a.entity_id], today=TODAY)
    assert "18,000.00 -> 20,000.00" in r.lines[2]


def test_terminate_scope_and_status(blank):
    _, sub_id, t_id = seed(blank)
    out = run(blank, services.terminate, sub_id, D(2025, 6, 30), TerminationReason.CHURN, today=TODAY)
    assert out.arr_after[2026] == 0 and out.arr_before[2026] == 12000
    with blank() as s:
        c = metrics.load_customers(s)[0]
        assert metrics.subscription_status(c.subscriptions[0], TODAY) == "CHURNED"
        assert metrics.customer_year_arr(c, 2025) == 12000          # retained in the cancel year
    with pytest.raises(ServiceError, match="already terminated"):
        run(blank, services.terminate, sub_id, D(2025, 7, 1), TerminationReason.CHURN, today=TODAY)
    assert metrics.subscription_status(c.subscriptions[0], D(2025, 1, 1)) == "ENDING"
    with pytest.raises(ServiceError, match="terminated"):
        run(blank, services.add_on, sub_id, ti(D(2026, 1, 1), D(2026, 12, 31), 1), today=TODAY)


def test_new_subscription_rules(blank):
    seed(blank)
    with pytest.raises(ServiceError, match="active"):
        run(blank, services.new_subscription, 80001, Platform.ON_PREM, OpenedVia.NET_NEW,
            ti(D(2026, 1, 1), D(2026, 12, 31), 1), today=TODAY)
    out = run(blank, services.new_subscription, 80001, Platform.CLOUD, OpenedVia.CONVERSION,
              ti(D(2026, 1, 1), D(2028, 12, 31), 90000), today=TODAY)
    assert any("maintenance" in w for w in out.warnings)
    with pytest.raises(ServiceError):
        run(blank, services.new_subscription, 80001, Platform.CLOUD, OpenedVia.NET_NEW,
            ti(D(2026, 2, 1), D(2026, 1, 1), 1), today=TODAY)
    with pytest.raises(ServiceError, match="80xxx"):
        run(blank, services.create_customer, 12, "x")


def test_amend_and_metrics(blank):
    _, _, t_id = seed(blank)
    run(blank, services.amend, t_id, ti(D(2025, 1, 1), D(2027, 12, 31), 45000), today=TODAY)
    with blank() as s:
        cs = metrics.load_customers(s)
        assert metrics.customer_year_arr(cs[0], 2024) == 12000 and metrics.customer_year_arr(cs[0], 2025) == 15000
        assert metrics.arr_at(cs, TODAY) == 15000
        due = metrics.renewals_due(cs, D(2027, 10, 1), 120)
        assert len(due) == 1 and due[0].term.kind.value == "AMENDMENT"
        r = metrics.retention(cs, 2025)
        assert r.cohort == 1 and r.expansion_arr == 3000 and r.nrr == 1.25
        assert metrics.new_customers_by_year(cs) == {2023: 1}


def test_checks_invoices_and_edit(blank):
    _, _, t_id = seed(blank)
    with blank() as s:
        s.add(ControlCheckType(code="chk_sf", label="Verified with SF", owner="Norvy", sort=1))
        s.commit()
    out = run(blank, services.set_checks, t_id, {"chk_sf": ("MISMATCHED", "price differs")})
    assert out.lines == ["chk_sf: MISMATCHED (price differs)"]
    assert run(blank, services.set_checks, t_id, {"chk_sf": ("MISMATCHED", "price differs")}).lines == []
    inv = run(blank, services.record_invoice, t_id, 2026, D(2026, 1, 15), 40000)
    assert inv.warnings and "exceeds" in inv.warnings[0]
    with pytest.raises(ServiceError):
        run(blank, services.record_invoice, t_id, 2030, None, 1)
    e = run(blank, services.update_term, t_id, {"next_action": "For renewal - 1/1/2027"}, ae="ac")
    assert len(e.lines) == 2
    with pytest.raises(ServiceError):
        run(blank, services.update_term, t_id, {"tcv": 1})
    with blank() as s:
        t = s.get(Term, t_id)
        assert t.checks[0].result == CheckResult.MISMATCHED and t.ae.initials == "AC"


def test_maintenance_and_conversion_rule(blank):
    from app.models import Customer, MaintenanceBlock, MaintenanceContract, MaintenanceFee, OpenedVia
    seed(blank)
    with blank() as s:
        c = s.scalar(select(Customer))
        mc = MaintenanceContract(customer=c, block=MaintenanceBlock.PERPETUAL_LIVE)
        mc.fees += [MaintenanceFee(year=2025, amount=5000), MaintenanceFee(year=2026, amount=5200),
                    MaintenanceFee(year=2027, amount=300)]                # term ends 2026: no subscription in 2027
        s.add(mc)
        s.commit()
    with blank() as s:
        c = metrics.load_customers(s)[0]
        assert metrics.maintenance_year_arr(c, 2026) == 5200
        assert metrics.arr_by_year([c], [2026])["Total"][2026] == 12000 + 5200
        assert metrics.effective_year_arr(c, 2026) == 17200           # not converted: both count
        assert metrics.maintenance_arr(c, 2026) == 5200 and metrics.converted_maintenance(c, 2026) == 0
        c.subscriptions[0].opened_via = OpenedVia.CONVERSION
        assert metrics.effective_year_arr(c, 2026) == 12000           # converted: subscription replaces maintenance
        assert metrics.maintenance_arr(c, 2026) == 0                  # prepaid leftover is not ARR ...
        assert metrics.converted_maintenance(c, 2026) == 5200         # ... but is still billed, shown separately
        arr = metrics.arr_by_year([c], [2026])
        assert arr["Total"][2026] == 12000 and arr["Maintenance"][2026] == 0
        assert arr["Converted maintenance"][2026] == 5200
        assert metrics.maintenance_total([c], 2026) == 5200 and metrics.maintenance_arr_total([c], 2026) == 0
        assert metrics.maintenance_arr(c, 2025) == 0                  # subscription in 2025 too
        assert metrics.customer_year_arr(c, 2027) == 0 and metrics.maintenance_arr(c, 2027) == 300   # none: counts


def test_overdue_renewal(blank):
    """A term that ended without a renewal or termination is 'renewal overdue', listed first, and renewable."""
    _, sub_id, t_id = seed(blank)                                  # term ends 12/31/2026
    after = D(2027, 2, 15)
    with blank() as s:
        cs = metrics.load_customers(s)
        sub = cs[0].subscriptions[0]
        assert metrics.subscription_status(sub, after) == "ENDED"
        assert metrics.renewal_overdue(sub, after)
        due = metrics.renewals_due(cs, after, 120)
        assert len(due) == 1 and due[0].overdue and due[0].days == -46
        assert not metrics.renewal_overdue(sub, D(2028, 6, 1))     # beyond the 12-month grace window
    # late, back-dated renewal is accepted and clears the overdue state
    run(blank, services.renew, t_id, ti(D(2027, 1, 1), D(2029, 12, 31), 39600), today=after)
    with blank() as s:
        cs = metrics.load_customers(s)
        assert not metrics.renewal_overdue(cs[0].subscriptions[0], after)
        assert metrics.renewals_due(cs, after, 120) == []


def test_overdue_cleared_by_termination(blank):
    _, sub_id, _ = seed(blank)
    run(blank, services.terminate, sub_id, D(2026, 12, 31), TerminationReason.CHURN, today=D(2027, 2, 15))
    with blank() as s:
        cs = metrics.load_customers(s)
        assert not metrics.renewal_overdue(cs[0].subscriptions[0], D(2027, 2, 15))
        assert metrics.renewals_due(cs, D(2027, 2, 15), 120) == []


def test_reimport_refused_when_app_changes_exist(tmp_path, new_schema):
    from app.db import Base
    from app.importer.cli import ImportRefused, run_import
    from app.models import AuditLog
    import datetime as dt
    eng = new_schema()
    url = eng.url.render_as_string(hide_password=False)
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(AuditLog.__table__.insert().values(at=dt.datetime.now(), actor="t", action="renew",
                                                        entity="term", summary="x"))
    with pytest.raises(ImportRefused, match="1 change"):
        run_import(url=url, report_dir=tmp_path, maintenance=False)


def test_worklist_outcomes(blank):
    _, sub_id, t_id = seed(blank)                                  # term ends 12/31/2026, renewal date 01/01/2027
    on = D(2026, 11, 12)
    with blank() as s:
        w = metrics.renewal_worklist(metrics.load_customers(s), on)
        assert [(x.outcome, x.days) for x in w] == [("DUE", 50)]
    run(blank, services.renew, t_id, ti(D(2027, 1, 1), D(2029, 12, 31), 39600), today=on)
    with blank() as s:
        w = metrics.renewal_worklist(metrics.load_customers(s), on)
        assert w[0].outcome == "RENEWED" and w[0].successor_arr == 13200 and not w[0].is_open
        assert metrics.renewal_worklist(metrics.load_customers(s), on, outcomes={"DUE"}) == []
        assert metrics.renewal_worklist(metrics.load_customers(s), on, platform="CLOUD") == []


def test_worklist_cancellation_scope(blank):
    """A cancellation only closes terms that started before it; a later re-signed term is still due (Cutter & Buck)."""
    _, sub_id, t_id = seed(blank)
    run(blank, services.terminate, sub_id, D(2025, 6, 30), TerminationReason.CHURN, today=D(2025, 7, 1))
    with blank() as s:
        from app.models import Subscription, Term as T, TermKind
        sub = s.get(Subscription, sub_id)
        s.add(T(subscription=sub, kind=TermKind.RENEWAL, start_date=D(2025, 7, 15), end_date=D(2026, 12, 31),
                tcv=18000, term_months=18))
        s.commit()
    with blank() as s:
        w = metrics.renewal_worklist(metrics.load_customers(s), D(2026, 11, 12))
        by_kind = {x.term.kind.value: x.outcome for x in w}
        assert by_kind == {"INITIAL": "NOT_RENEWING", "RENEWAL": "DUE"}
        assert len(metrics.renewals_due(metrics.load_customers(s), D(2026, 11, 12), 120)) == 1
