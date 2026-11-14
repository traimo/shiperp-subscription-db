import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import main
from app.models import AuditLog, Term
from tests.conftest import TODAY, needs_source

pytestmark = needs_source


@pytest.fixture(scope="module")
def client(imported):
    Session, _ = imported
    main.SESSION_FACTORY, main.TODAY = Session, TODAY
    yield TestClient(main.app)
    main.TODAY = None


def term_id(Session, ref):
    with Session() as s:
        return s.scalar(select(Term.id).where(Term.source_ref == ref))


@pytest.mark.parametrize("url", ["/", "/customers", "/customers?status=CHURNED", "/customers?q=deere",
                                 "/customer/80072", "/customer/80197", "/customer/80168", "/customer/80148",
                                 "/customers/new", "/wf/new", "/wf/convert?customer=80008", "/import",
                                 "/import?show=all", "/audit", "/export/arr.csv"])
def test_pages(client, url):
    r = client.get(url)
    assert r.status_code == 200, r.text[:400]


def test_term_and_workflow_pages(client, imported):
    Session, _ = imported
    tid = term_id(Session, "On-Prem D180")
    for url in (f"/term/{tid}", f"/wf/renew?term={tid}", f"/wf/amend?term={tid}", f"/wf/addon?term={tid}"):
        assert client.get(url).status_code == 200
    with Session() as s:
        sub = s.get(Term, tid).subscription_id
    assert client.get(f"/wf/terminate?subscription={sub}").status_code == 200


def test_renew_preview_then_confirm(client, imported):
    Session, _ = imported
    tid = term_id(Session, "On-Prem D180")
    form = {"term": str(tid), "signed_date": "2026-12-15", "start_date": "2027-02-01", "end_date": "2030-01-31",
            "tcv": "126000", "term_years": "3", "ae": "AC"}
    r = client.post("/wf/renew", data=form)
    assert r.status_code == 200 and "Confirm and save" in r.text and "42,000.00" in r.text
    with Session() as s:
        assert s.scalar(select(Term).where(Term.start_date == TODAY.replace(2027, 2, 1))) is None
    r = client.post("/wf/renew", data={**form, "_confirm": "1"}, follow_redirects=False)
    assert r.status_code == 303 and "/customer/80170" in r.headers["location"]
    with Session() as s:
        new = s.scalar(select(Term).where(Term.subscription_id == s.get(Term, tid).subscription_id,
                                          Term.start_date == TODAY.replace(2027, 2, 1)))
        assert new is not None and new.supersedes[0].old_term_id == tid
        assert s.scalar(select(AuditLog).where(AuditLog.action == "renew"))


def test_errors_are_shown(client, imported):
    Session, _ = imported
    tid = term_id(Session, "On-Prem A9")
    r = client.post("/wf/renew", data={"term": str(tid), "start_date": "2027-01-01", "end_date": "2029-12-31",
                                       "tcv": "1"})
    assert r.status_code == 200 and "already renewed" in r.text


def test_derive(client):
    d = client.post("/api/derive", json={"start_date": "2020-05-01", "end_date": "2023-04-30",
                                         "tcv": "185288"}).json()
    assert d["ok"] and d["term_years"] == 3 and d["arr"] == 61762.67


def test_overdue_customer_80208_can_renew(client, imported):
    Session, _ = imported
    tid = term_id(Session, "On-Prem A37")
    page = client.get("/customer/80208").text
    assert "renewal overdue" in page and f"/wf/renew?term={tid}" in page and "Terminate (did not renew)" in page
    assert "80208" in client.get("/").text                      # listed on the dashboard as overdue
    r = client.post("/wf/renew", data={"term": str(tid), "start_date": "2026-09-22", "end_date": "2029-09-21",
                                       "tcv": "72000", "signed_date": "2026-11-12"})
    assert r.status_code == 200 and "Confirm and save" in r.text          # preview only, nothing saved
    with Session() as s:
        t = s.get(Term, tid)
        assert not t.superseded_by
        sub_id = t.subscription_id
    # terminate form defaults the cancellation date to the day the last term ended
    assert 'value="2026-09-21"' in client.get(f"/wf/terminate?subscription={sub_id}").text


def test_dashboard_renewal_filters(client):
    page = client.get("/").text
    for name in ("PRATT &amp; WHITNEY", "UNION PACIFIC", "ALLISON TRANSMISSION", "ENTRUST", "HERBER", "QUALFON",
                 "CUTTER &amp; BUCK", "INNOVATIVE TECHNOLOGY"):
        assert name in page, name
    cloud = client.get("/?platform=CLOUD").text
    assert "ENTRUST" in cloud and "PRATT &amp; WHITNEY" not in cloud
    open_ = client.get("/?outcome=OPEN").text
    assert "INNOVATIVE TECHNOLOGY" in open_ and "ALLISON TRANSMISSION" not in open_
    renewed = client.get("/?outcome=RENEWED&platform=ON_PREM").text
    assert "UNION PACIFIC" in renewed and "LUTRON" not in renewed


def test_database_viewer(client, imported):
    from sqlalchemy import func, select as sel
    from app import dbviewer
    from app.db import Base
    Session, _ = imported
    page = client.get("/db").text
    assert "<svg" in page and "How the tables connect" in page
    with Session() as s:
        for name in dbviewer.table_names():
            n = s.scalar(sel(func.count()).select_from(Base.metadata.tables[name]))
            assert f'href="/db/{name}"' in page and f"{n:,}" in page
    r = client.get("/db/term?q=A37")
    assert r.status_code == 200 and "On-Prem A37" in r.text
    tid = term_id(Session, "On-Prem A37")
    with Session() as s:
        sub = s.get(Term, tid).subscription_id
    assert f'/db/subscription?where=id:{sub}' in r.text                       # FK link
    f = client.get(f"/db/term?where=subscription_id:{sub}").text
    assert "On-Prem A37" in f and "1 matching rows" in f
    assert client.get("/db/term?sort=end_date&desc=1&page=2").status_code == 200
    assert client.get("/db/customer?where=id:1").status_code == 200
    assert client.get("/db/not_a_table").status_code == 404
    assert client.get("/db/term?sort=bogus").status_code == 400
    assert client.get("/db/term?where=bogus:1").status_code == 400
    assert client.get("/db/term?q='; DROP TABLE term; --").status_code == 200     # parameterised, harmless
    with Session() as s:
        assert s.scalar(sel(func.count()).select_from(Term)) > 0
    assert client.post("/db/term").status_code == 405                         # read-only
