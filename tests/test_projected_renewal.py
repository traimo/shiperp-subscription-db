"""Projected Renewal: a contract that ended with no renewal or termination on file keeps its ARR + an uplift (default
5%), flagged as an estimate, until the renewal or a termination is recorded. Runs on a blank database."""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import config, main, metrics, services
from app.models import Platform, Subscription, Term, TermKind, TerminationReason
from app.services import ServiceError, TermInput
from tests.conftest import D, needs_source, seed

BEFORE = D(2026, 12, 15)          # seed()'s term runs 2024-01-01..2026-12-31, ARR 12,000
AFTER = D(2027, 2, 1)             # ended a month ago, nothing on file
PROJ = Decimal("12600")           # 12,000 + 5%


def run(factory, op, *a, **kw):
    return services.run(factory, op, "tester", *a, commit=True, **kw)


def ti(start, end, tcv):
    return TermInput(start_date=start, end_date=end, tcv=tcv, signed_date=start)


def _load(factory):
    s = factory()
    return s, metrics.load_customers(s)


def test_not_projected_before_the_end_date(blank):
    seed(blank)
    s, cs = _load(blank)
    t = cs[0].subscriptions[0].terms[0]
    assert not metrics.is_projected(t, BEFORE) and not metrics.is_projected(t, D(2026, 12, 31))
    assert metrics.arr_at(cs, BEFORE) == 12000 and metrics.projected_total(cs, BEFORE) == (0, 0)
    assert metrics.customer_year_arr(cs[0], 2027, as_of=BEFORE) == 0      # not ended yet: nothing projected
    s.close()


def test_projected_after_the_end_date(blank):
    seed(blank)
    s, cs = _load(blank)
    c, t = cs[0], cs[0].subscriptions[0].terms[0]
    assert metrics.is_projected(t, D(2027, 1, 1))                        # the day after the end date
    assert metrics.projected_arr(t) == PROJ
    assert metrics.arr_at(cs, AFTER) == PROJ                             # ARR today: no drop, + 5%
    assert metrics.projected_total(cs, AFTER) == (PROJ, 1)
    assert metrics.customer_year_arr(c, 2026, as_of=AFTER) == 12000      # end year: unchanged, contracted ARR
    assert [metrics.customer_year_arr(c, y, as_of=AFTER) for y in (2027, 2029)] == [PROJ, PROJ]  # flat, no compounding
    assert metrics.customer_year_arr(c, 2027, as_of=AFTER, project=False) == 0
    assert metrics.customer_year_arr(c, 2027) == 0                       # contracted view (as_of None): signed only
    arr = metrics.arr_by_year(cs, [2026, 2027], AFTER)
    assert arr["Projected renewal"] == {2026: 0, 2027: PROJ}
    assert arr["Total"][2027] == PROJ                                    # in Total once, not twice
    assert arr["CARR"][2027] == PROJ and arr["Signed, not started"][2027] == 0
    assert metrics.carr_at(cs, AFTER) == PROJ
    w = metrics.renewal_worklist(cs, AFTER)
    assert [(x.outcome, x.projected) for x in w] == [("OVERDUE", True)]
    s.close()


def test_uplift_override_and_default(blank, monkeypatch):
    _, _, t_id = seed(blank)
    run(blank, services.update_term, t_id, {"projected_uplift": Decimal("0.03")})
    s, cs = _load(blank)
    assert metrics.arr_at(cs, AFTER) == Decimal("12360")
    s.close()
    run(blank, services.update_term, t_id, {"projected_uplift": Decimal("0")})
    s, cs = _load(blank)
    assert metrics.arr_at(cs, AFTER) == 12000                            # 0% = carry forward as is
    s.close()
    run(blank, services.update_term, t_id, {"projected_uplift": None})   # back to the default
    monkeypatch.setenv("PROJECTED_RENEWAL_UPLIFT", "0.04")
    s, cs = _load(blank)
    assert metrics.arr_at(cs, AFTER) == Decimal("12480")
    s.close()
    with pytest.raises(ServiceError, match="between 0% and 50%"):
        run(blank, services.update_term, t_id, {"projected_uplift": Decimal("0.6")})
    monkeypatch.setenv("PROJECTED_RENEWAL_UPLIFT", "abc")
    with pytest.raises(config.ConfigError):
        config.projected_renewal_uplift()


def test_uplift_edit_is_audited_and_undoable(blank):
    from app import undo
    _, _, t_id = seed(blank)
    out = run(blank, services.update_term, t_id, {"projected_uplift": Decimal("0.054")})
    assert out.lines == ["projected_uplift: None -> Decimal('0.0540')"] and out.audit_id
    out2 = run(blank, services.update_term, t_id, {"projected_uplift": Decimal("0.05")})
    assert out2.audit_id                       # 5.4% -> 5% is a real change (not lost to rounding to cents)
    services.run(blank, undo.undo_change, "tester", out2.audit_id, commit=True)
    with blank() as s:
        assert s.get(Term, t_id).projected_uplift == Decimal("0.0540")


def test_recording_the_renewal_ends_the_projection(blank):
    _, _, t_id = seed(blank)
    run(blank, services.renew, t_id, ti(D(2027, 1, 1), D(2029, 12, 31), 39600), today=AFTER)  # ARR 13,200
    s, cs = _load(blank)
    assert not metrics.is_projected(s.get(Term, t_id), AFTER)
    assert metrics.projected_total(cs, AFTER) == (0, 0)
    assert metrics.arr_at(cs, AFTER) == 13200
    assert metrics.customer_year_arr(cs[0], 2027, as_of=AFTER) == 13200  # the renewal, not 12,600 + 13,200
    s.close()


@pytest.mark.parametrize("scoped", [False, True])
def test_termination_ends_the_projection(blank, scoped):
    _, sub_id, t_id = seed(blank)
    run(blank, services.terminate, sub_id, D(2026, 12, 31), TerminationReason.CHURN, today=AFTER,
        term_ids=[t_id] if scoped else None)
    s, cs = _load(blank)
    assert metrics.projected_total(cs, AFTER) == (0, 0) and metrics.arr_at(cs, AFTER) == 0
    s.close()


@pytest.mark.parametrize("note", ["Churn", "Final Period", "Not renewing - moved to SAP", "Cancelled 1/5/2027"])
def test_a_not_renewing_note_is_not_projected(blank, note):
    _, _, t_id = seed(blank)
    run(blank, services.update_term, t_id, {"renewal_note": note})
    s, cs = _load(blank)
    assert metrics.projected_total(cs, AFTER) == (0, 0)
    s.close()


def test_for_renewal_note_is_projected(blank):
    _, _, t_id = seed(blank)
    run(blank, services.update_term, t_id, {"renewal_note": "For renewal"})
    s, cs = _load(blank)
    assert metrics.projected_total(cs, AFTER) == (PROJ, 1)
    s.close()


def test_add_on_ended_inside_an_active_subscription_is_not_projected(blank):
    _, sub_id, base = seed(blank)
    with blank() as s:
        sub = s.get(Subscription, sub_id)
        s.get(Term, base).end_date = D(2028, 12, 31)                     # base still running
        s.add(Term(subscription=sub, kind=TermKind.ADD_ON, start_date=D(2025, 1, 1), end_date=D(2026, 12, 31),
                   tcv=12000, term_months=24, signed_date=D(2024, 12, 1)))
        s.commit()
    s, cs = _load(blank)
    assert metrics.projected_total(cs, AFTER) == (0, 0)
    s.close()


def test_zero_arr_term_is_not_projected(blank):
    _, _, t_id = seed(blank)
    with blank() as s:
        s.get(Term, t_id).tcv = 0
        s.commit()
    s, cs = _load(blank)
    assert metrics.projected_total(cs, AFTER) == (0, 0)
    s.close()


@pytest.fixture
def app_db(blank):
    old = main.SESSION_FACTORY, main.TODAY
    main.SESSION_FACTORY, main.TODAY = blank, AFTER
    yield blank
    main.SESSION_FACTORY, main.TODAY = old


def test_pages_flag_the_projection(app_db):
    _, _, t_id = seed(app_db)
    seed(app_db, qb=80002, platform=Platform.CLOUD)
    _, _, long_id = seed(app_db, qb=80003)                               # runs to 2028, so 2027 is a dashboard column
    with app_db() as s:
        s.get(Term, long_id).end_date = D(2028, 12, 31)
        s.commit()
    c = TestClient(main.app)
    home = c.get("/").text
    assert "projected renewals" in home and "25,200.00" in home and "2 contracts" in home
    assert "of which projected" in home
    projected = c.get("/?outcome=PROJECTED").text
    assert projected.count('title="Carried in ARR as a projected renewal"') == 2
    page = c.get("/customer/80001").text
    assert "projected renewal +5%" in page and "↳ projected renewal of" in page and "12,600.00" in page
    term = c.get(f"/term/{t_id}").text
    assert 'name="projected_uplift_pct"' in term and "Projected now: 12,600.00" in term
    csv = c.get("/export/arr.csv").text.splitlines()
    proj = [r for r in csv if "_PROJECTED" in r]
    assert any(r.startswith("80001,") and ",ON_PREM_PROJECTED," in r for r in proj)
    assert any(r.startswith("80002,") and ",CLOUD_PROJECTED," in r for r in proj)
    on_prem = next(r for r in csv if r.startswith("80001,") and ",ON_PREM," in r)
    assert "12600" not in on_prem                                       # ON_PREM rows stay signed-only


def test_term_form_takes_a_percent(app_db):
    _, _, t_id = seed(app_db)
    c = TestClient(main.app)
    r = c.post(f"/term/{t_id}/edit", data={"projected_uplift_pct": "3%"}, follow_redirects=False)
    assert r.status_code == 303
    with app_db() as s:
        assert s.get(Term, t_id).projected_uplift == Decimal("0.03")
    for bad in ("lots", "NaN", "60"):
        r = c.post(f"/term/{t_id}/edit", data={"projected_uplift_pct": bad}, follow_redirects=False)
        assert r.status_code == 303 and "error=" in r.headers["location"], bad      # a message, not a 500
    c.post(f"/term/{t_id}/edit", data={"projected_uplift_pct": ""}, follow_redirects=False)
    with app_db() as s:
        assert s.get(Term, t_id).projected_uplift is None


@needs_source
def test_lutron_from_the_workbook(imported):
    """Lutron 80213 (On-Prem A42, 63,000, "For renewal") ends 2026-11-29: from the next day it is carried at
    66,150 (+5%) - the 63,000 the workbook types into "ARR - 2026" by hand."""
    Session, _ = imported
    with Session() as s:
        cs = metrics.load_customers(s)
        lutron = next(c for c in cs if c.qb_number == 80213)
        t = next(t for sub in lutron.subscriptions for t in sub.terms if t.source_ref == "On-Prem A42")
        assert not metrics.is_projected(t, D(2026, 11, 29))
        on = D(2026, 12, 15)
        assert metrics.is_projected(t, on) and metrics.projected_arr(t) == Decimal("66150")
        assert metrics.customer_year_arr(lutron, 2027, Platform.ON_PREM, as_of=on) == Decimal("66150")
        assert any(x.term is t and x.projected for x in metrics.renewal_worklist(cs, on))


def test_older_unlinked_term_is_not_projected_while_the_subscription_runs(blank):
    """A renewal imported without its link to the old term: the old term is not projected next to the live one."""
    _, sub_id, old = seed(blank)
    with blank() as s:
        s.add(Term(subscription=s.get(Subscription, sub_id), kind=TermKind.RENEWAL, start_date=D(2027, 1, 1),
                   end_date=D(2029, 12, 31), tcv=39600, term_months=36, signed_date=D(2026, 12, 20)))
        s.commit()
    s, cs = _load(blank)
    assert metrics.projected_total(cs, AFTER) == (0, 0)
    assert metrics.arr_at(cs, AFTER) == 13200                            # the renewal only, not + 12,600
    s.close()


def test_only_the_last_contracts_of_a_lapsed_subscription_are_projected(blank):
    """An add-on that ended in 2025 (rolled in, never linked) does not come back when the base lapses; a co-terminus
    add-on ending with the base is projected with it."""
    _, sub_id, base = seed(blank)
    with blank() as s:
        sub = s.get(Subscription, sub_id)
        s.add_all([Term(subscription=sub, kind=TermKind.ADD_ON, start_date=D(2024, 6, 1), end_date=D(2025, 5, 31),
                        tcv=5000, term_months=12, signed_date=D(2024, 5, 1)),
                   Term(subscription=sub, kind=TermKind.ADD_ON, start_date=D(2025, 1, 1), end_date=D(2026, 12, 31),
                        tcv=12000, term_months=24, signed_date=D(2024, 12, 1))])
        s.commit()
    s, cs = _load(blank)
    assert metrics.projected_total(cs, AFTER) == (Decimal("18900"), 2)  # (12,000 + 6,000) × 1.05
    s.close()


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1"])
def test_uplift_rejects_non_numbers(blank, bad):
    _, _, t_id = seed(blank)
    with pytest.raises(ServiceError):
        run(blank, services.update_term, t_id, {"projected_uplift": Decimal(bad)})
