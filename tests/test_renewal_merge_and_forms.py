"""Renewal merge (any open term ending near the renewed one), signed renewal = active from its signed date,
Hybrid class and AE drop-downs. Runs on a blank database (no workbook needed)."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main, metrics, services
from app.models import AccountExec, Customer, OpenedVia, Platform, Subscription, Term, TermKind
from app.services import TermInput
from tests.conftest import D, seed

TODAY = D(2026, 11, 13)


@pytest.fixture
def app_db(blank):
    old = main.SESSION_FACTORY, main.TODAY
    main.SESSION_FACTORY, main.TODAY = blank, TODAY
    yield blank
    main.SESSION_FACTORY, main.TODAY = old


def _cytiva(factory):
    """INITIAL 2024-2026 (ARR 12,000) + co-terminus ADD_ON (ARR 6,000), like 80221."""
    _, sub_id, base = seed(factory)
    with factory() as s:
        sub = s.get(Subscription, sub_id)
        a = Term(subscription=sub, kind=TermKind.ADD_ON, start_date=D(2025, 1, 1), end_date=D(2026, 12, 31),
                 tcv=12000, term_months=24, signed_date=D(2024, 12, 1))
        s.add(a)
        s.commit()
        return sub_id, base, a.id


# ------------------------------------------------------------------------------------------------ merge

def test_renew_form_offers_merge_from_either_term(app_db):
    _, base, addon = _cytiva(app_db)
    c = TestClient(main.app)
    page = c.get(f"/wf/renew?term={base}").text
    dlg = page[page.index('<dialog id="merge-dlg"'):page.index("</dialog>")]  # popup, opened on load
    assert 'data-autoopen="1"' in dlg and "Merge contracts into this renewal?" in dlg
    assert f'name="rollup" id="rollup-{addon}" value="{addon}"' in dlg and " checked " in dlg   # ticked by default
    assert "18,000.00" in dlg                                        # ARR being replaced: 12,000 + 6,000
    assert 'id="merge-change"' in page and "Merging into this renewal:" in page
    page = c.get(f"/wf/renew?term={addon}").text                     # renewing from the add-on row
    assert f'value="{base}"' in page[page.index("<dialog"):]


def test_no_popup_without_merge_candidates(app_db):
    _, _, base = seed(app_db)
    assert "<dialog" not in TestClient(main.app).get(f"/wf/renew?term={base}").text


def test_merge_preview_states_merge_and_unticked_renews_one(app_db):
    sub_id, base, addon = _cytiva(app_db)
    form = {"term": str(addon), "rollup": [str(base)], "start_date": "2027-01-01", "end_date": "2029-12-31",
            "tcv": "60000", "signed_date": "2026-11-13", "payment_terms": "Annual", "ae": ""}
    c = TestClient(main.app)
    assert "Merged 2 contracts into one renewal" in c.post("/wf/renew", data=form).text     # review page
    form.pop("rollup")
    form["_confirm"] = "1"
    c.post("/wf/renew", data=form, follow_redirects=False)
    with app_db() as s:
        new = [t for t in s.get(Subscription, sub_id).terms if t.kind == TermKind.RENEWAL]
        assert len(new) == 1 and {x.old_term_id for x in new[0].supersedes} == {addon}
        assert not s.get(Term, base).superseded_by


def test_merge_renewal_replaces_both_terms(app_db):
    sub_id, base, addon = _cytiva(app_db)
    form = {"term": str(addon), "rollup": [str(base)], "start_date": "2027-01-01", "end_date": "2029-12-31",
            "tcv": "60000", "signed_date": "2026-11-13", "payment_terms": "Annual", "ae": "", "_confirm": "1"}
    r = TestClient(main.app).post("/wf/renew", data=form, follow_redirects=False)
    assert r.status_code in (200, 303), r.text[:300]
    with app_db() as s:
        sub = s.get(Subscription, sub_id)
        new = [t for t in sub.terms if t.kind == TermKind.RENEWAL]
        assert len(new) == 1 and {x.old_term_id for x in new[0].supersedes} == {base, addon}
        assert metrics.term_arr(new[0]) == 20000


# ------------------------------------------------------------------------------------------------ signed = active

def test_signed_renewal_is_active_from_signed_date(blank):
    _, _, base = seed(blank)                                          # 2024-01-01..2026-12-31, ARR 12,000
    services.run(blank, services.renew, "t", base,
                 TermInput(start_date=D(2027, 1, 1), end_date=D(2029, 12, 31), tcv=39600,
                           signed_date=D(2026, 10, 21)), today=TODAY, commit=True)
    with blank() as s:
        cs = metrics.load_customers(s)
        old = s.get(Term, base)
        new = next(t for t in cs[0].subscriptions[0].terms if t.kind == TermKind.RENEWAL)
        assert metrics.effective_from(new) == D(2026, 10, 21) and metrics.replaced_on(old) == D(2026, 10, 21)
        assert metrics.term_status(new, D(2026, 10, 20)) == "SIGNED"
        assert metrics.term_status(old, D(2026, 10, 20)) == "ACTIVE"
        assert metrics.term_status(new, TODAY) == "ACTIVE" and metrics.term_status(old, TODAY) == "SUPERSEDED"
        assert metrics.arr_at(cs, TODAY) == 13200 and metrics.carr_at(cs, TODAY) == 13200
        # calendar years keep the contract start: 2026 = old term, 2027 = new term
        assert metrics.customer_year_arr(cs[0], 2026) == 12000 and metrics.customer_year_arr(cs[0], 2027) == 13200


def test_new_subscription_signed_early_stays_signed(blank):
    seed(blank)
    services.run(blank, services.new_subscription, "t", 80001, Platform.CLOUD, OpenedVia.NET_NEW,
                 TermInput(start_date=D(2027, 1, 1), end_date=D(2027, 12, 31), tcv=5000,
                           signed_date=D(2026, 11, 1)), today=TODAY, commit=True)
    with blank() as s:
        cs = metrics.load_customers(s)
        cloud = next(x for x in cs[0].subscriptions if x.platform == Platform.CLOUD)
        assert metrics.term_status(cloud.terms[0], TODAY) == "SIGNED"
        assert metrics.arr_at(cs, TODAY) == 12000 and metrics.carr_at(cs, TODAY) == 17000


# ------------------------------------------------------------------------------------------------ drop-downs

def test_ae_dropdowns(app_db):
    _, _, base = seed(app_db)
    with app_db() as s:
        s.add_all([AccountExec(initials="AC"), AccountExec(initials="JG")])
        s.commit()
    c = TestClient(main.app)
    page = c.get("/wf/new?customer=80001").text
    assert 'name="hybrid_class"' not in page and 'name="license_category"' not in page
    assert '<select name="ae"' in page and '<option value="JG"' in page and "Other…" in page
    assert 'name="ae_other"' in c.get(f"/term/{base}").text
    r = c.post(f"/term/{base}/edit", data={"ae": "__other__", "ae_other": "zz"}, follow_redirects=False)
    assert r.status_code == 303
    with app_db() as s:
        assert s.get(Term, base).ae.initials == "ZZ"
        assert s.scalar(select(AccountExec).where(AccountExec.initials == "ZZ")) is not None
    page = c.get(f"/term/{base}").text
    assert '<option value="ZZ" selected>' in page
    r = c.post(f"/term/{base}/edit", data={"ae": "JG"}, follow_redirects=False)
    with app_db() as s:
        assert s.get(Term, base).ae.initials == "JG"


# ------------------------------------------------------------------------------------------------ signed = CARR

def test_signed_not_started_addon_is_carr_not_arr(app_db):
    """Like 80190: an add-on signed 11/09/2026 that starts 01/01/2027 is CARR until it starts, not ARR."""
    _, sub_id, base = seed(app_db)
    services.run(app_db, services.add_on, "t", sub_id,
                 TermInput(start_date=D(2027, 1, 1), end_date=D(2028, 12, 31), tcv=40000,
                           signed_date=D(2026, 11, 9)), today=TODAY, commit=True)
    services.run(app_db, services.renew, "t", base,                     # renewal signed today = ARR now
                 TermInput(start_date=D(2027, 1, 1), end_date=D(2029, 12, 31), tcv=39600, signed_date=TODAY),
                 today=TODAY, commit=True)
    with app_db() as s:
        cs = metrics.load_customers(s)
        c = cs[0]
        assert metrics.effective_year_arr(c, 2027, TODAY) == 13200          # renewal only
        assert metrics.carr_year(c, 2027) == 13200 + 20000                  # + signed add-on
        assert metrics.effective_year_arr(c, 2027, D(2027, 1, 1)) == 33200  # once it starts it is ARR
        by = metrics.arr_by_year(cs, [2026, 2027], TODAY)
        assert by["Total"][2027] == 13200 and by["Signed, not started"][2027] == 20000
        assert by["CARR"][2027] == 33200 and by["Total"][2026] == 12000
    page = TestClient(main.app).get("/customer/80001").text
    assert "CARR - signed, starts 01/01/2027" in page and "Customer CARR" in page
    csv_text = TestClient(main.app).get("/export/arr.csv").text
    assert "ON_PREM_SIGNED_NOT_STARTED" in csv_text


def test_future_signed_renewal_does_not_leave_a_gap(blank):
    """A renewal whose signed date is still in the future: until then the old term keeps counting (no hole)."""
    _, _, base = seed(blank)
    services.run(blank, services.renew, "t", base,
                 TermInput(start_date=D(2027, 1, 1), end_date=D(2029, 12, 31), tcv=39600,
                           signed_date=D(2026, 12, 15)), today=TODAY, commit=True)
    with blank() as s:
        c = metrics.load_customers(s)[0]
        assert metrics.effective_year_arr(c, 2026, TODAY) == 12000
        assert metrics.effective_year_arr(c, 2027, TODAY) == 0          # old term ended 2026, renewal not signed yet
        assert metrics.carr_year(c, 2027) == 13200


def test_hybrid_badge_on_customer_page_and_list(app_db):
    from sqlalchemy import select
    from app.models import Customer
    seed(app_db)
    with app_db() as s:
        cu = s.scalar(select(Customer).where(Customer.qb_number == 80001))
        cu.license_category, cu.hybrid_class = "Hybrid", "Perpetual/On Prem"
        s.commit()
    c = TestClient(main.app)
    assert "Hybrid · Perpetual/On Prem" in c.get("/customer/80001").text
    assert ">Hybrid</span>" in c.get("/customers").text
