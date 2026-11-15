"""Refactor 2026-11-14, batch B: XSS in the Remove confirm, safe 'back' paths, cross-site form posts and host names,
CSV formula injection, friendly errors instead of 500s, and an import that cannot empty the database on a bad file."""
import shutil

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.orm import sessionmaker

from app import csvsafe, main, services, sharepoint
from app import document_services as docs
from app.db import make_engine
from app.importer import cli
from app.models import Customer, Term
from tests.conftest import D, needs_source

TODAY = D(2026, 11, 13)
EVIL_NAME = "x');alert(1);('"


@pytest.fixture
def app_db(imported_copy, tmp_path):
    eng = imported_copy()
    S = sessionmaker(bind=eng, expire_on_commit=False)
    old = main.SESSION_FACTORY, main.TODAY
    main.SESSION_FACTORY, main.TODAY = S, TODAY
    yield S, eng, TestClient(main.app)
    main.SESSION_FACTORY, main.TODAY = old
    eng.dispose()


def ref(S, source_ref):
    with S() as s:
        return s.scalar(select(Term.id).where(Term.source_ref == source_ref))


# ------------------------------------------------------------------------------------------------ XSS / redirects

@needs_source
def test_document_name_is_not_placed_in_javascript(app_db):
    S, eng, c = app_db
    t = ref(S, "On-Prem A47")
    services.run(S, docs.link, "tester", "https://erpis.sharepoint.com/sites/F/Shared%20Documents/a.pdf",
                 name=EVIL_NAME, term_id=t, commit=True)
    html = c.get(f"/term/{t}").text                                          # the panel with the Remove button
    assert "confirm('Remove the link ' + this.dataset.name" in html
    assert 'data-name="x&#39;);alert(1);(&#39;"' in html                    # the name only as escaped data
    assert "confirm('Remove the link x" not in html
    assert "alert(1)" not in c.get("/customer/80221").text.split("<script>")[-1]


def test_safe_back():
    for bad in ("//evil.example", "/\\evil.example", "/\tevil", "https://evil.example", "", None, "evil"):
        assert main._safe_back(bad, "/d") == "/d"
    assert main._safe_back("/customer/80221", "/d") == "/customer/80221"


@needs_source
def test_undo_page_back_link_stays_on_site(app_db):
    S, eng, c = app_db
    out = services.run(S, services.update_term, "tester", ref(S, "On-Prem A47"), {"next_action": "x"}, commit=True)
    html = c.get(f"/audit/{out.audit_id}/undo", params={"back": "/\\evil.example"}).text
    assert 'href="/\\evil.example"' not in html and 'href="/audit"' in html


def test_redirect_encodes_messages():
    r = main.redirect("/term/1", msg="A & B #1 + 50%", undo=7)
    assert r.headers["location"] == "/term/1?msg=A%20%26%20B%20%231%20%2B%2050%25&undo=7"
    assert main.redirect("/x?a=1", error="e", anchor="docs").headers["location"] == "/x?a=1&error=e#docs"
    assert main.redirect("/x", msg="", undo=None).headers["location"] == "/x"


# ------------------------------------------------------------------------------------------------ CSRF / hosts

@needs_source
def test_cross_site_posts_are_refused(app_db):
    S, eng, c = app_db
    data = {"qb_number": "89991", "name": "Csrf Test"}
    for headers in ({"origin": "https://evil.example"}, {"referer": "https://evil.example/page"},
                    {"origin": "null"}):
        r = c.post("/customers/new", data=data, headers=headers, follow_redirects=False)
        assert r.status_code == 403
    with S() as s:
        assert s.scalar(select(Customer).where(Customer.qb_number == 89991)) is None
    r = c.post("/customers/new", data=data, headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303                                             # same origin (and no header) still work
    assert c.get("/customers", headers={"origin": "https://evil.example"}).status_code == 200   # GET unaffected


def test_unknown_host_names_are_refused():
    assert TestClient(main.app, base_url="http://evil.example").get("/api/none").status_code == 400
    assert TestClient(main.app, base_url="http://localhost:8001").post(
        "/api/derive", json={"start_date": "2026-01-01", "end_date": "2026-12-31", "tcv": "12"}).json()["ok"]


# ------------------------------------------------------------------------------------------------ CSV injection

def test_csv_text_cells():
    assert csvsafe.text("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert [csvsafe.text(v) for v in ("+1", "-x", "@a", "\tz")] == ["'+1", "'-x", "'@a", "'\tz"]
    assert csvsafe.text("ACME") == "ACME" and csvsafe.text(None) == ""


@needs_source
def test_contract_links_csv_neutralises_formulas(app_db):
    S, eng, c = app_db
    t = ref(S, "On-Prem A47")
    services.run(S, docs.link, "tester", "https://erpis.sharepoint.com/sites/F/Shared%20Documents/a.pdf",
                 name="=cmd", note="-2+3", term_id=t, commit=True)
    text = c.get("/customer/80221/contract-links.csv").text
    assert ",'=cmd," in text and text.rstrip().endswith(",'-2+3")
    assert ",-" not in c.get("/export/arr.csv").text.split("\n")[0]         # header unchanged; numbers untouched


# ------------------------------------------------------------------------------------------------ friendly errors

@needs_source
def test_bad_input_gives_a_message_not_500(app_db):
    S, eng, c = app_db
    t = ref(S, "On-Prem A47")
    r = c.post(f"/term/{t}/edit", data={"signed_date": "13/45/2026"}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    r = c.post("/invoice/999999/delete?term_id=1", follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]


def test_sharepoint_sign_in_error_without_description():
    class R:
        status_code = 400
        text = ""

        def json(self):
            return {"error": "invalid_request"}

    class Http:
        def post(self, *a, **k):
            return R()

    import os
    old = {k: os.environ.get(k) for k in ("SP_TENANT_ID", "SP_CLIENT_ID", "SP_CLIENT_SECRET")}
    os.environ.update(SP_TENANT_ID="t", SP_CLIENT_ID="c", SP_CLIENT_SECRET="s")
    try:
        with pytest.raises(sharepoint.SharePointError, match="invalid_request"):
            sharepoint.GraphClient(session=Http()).token()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ------------------------------------------------------------------------------------------------ import safety

@needs_source
def test_broken_workbook_leaves_the_database_alone(app_db, tmp_path):
    S, eng, c = app_db
    bad = tmp_path / "broken Subscription Summary.xlsx"
    bad.write_bytes(b"not a workbook")
    url = eng.url.render_as_string(hide_password=False)
    eng.dispose()
    with pytest.raises(Exception):
        cli.run_import(source=bad, url=url, force=True, maintenance=False, report_dir=tmp_path)
    with S() as s:
        assert "term" in inspect(eng).get_table_names()
        assert s.scalar(select(Term.id).limit(1)) is not None                # nothing was dropped


def test_backup_database(imported_copy, new_schema, monkeypatch):
    from app import backups
    from app.db import drop_schema
    eng = imported_copy()
    monkeypatch.setenv("DATABASE_URL", eng.url.render_as_string(hide_password=False))
    name = cli.backup_database("import")
    try:
        assert name.startswith("backup_") and backups.exists(name)
        assert any(b.name == name and b.label == "before import" for b in backups.list_all())
    finally:
        drop_schema(None, name)
    empty = new_schema()                                              # nothing imported yet: nothing to back up
    monkeypatch.setenv("DATABASE_URL", empty.url.render_as_string(hide_password=False))
    assert cli.backup_database("import") is None


def test_healthz_answers_any_host_and_nothing_else_does():
    c = TestClient(main.app)
    r = c.get("/healthz", headers={"host": "10.0.0.7:8001"})          # container probe by IP
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert c.get("/", headers={"host": "10.0.0.7:8001"}).status_code == 400   # the host check still applies
    assert c.post("/healthz", headers={"host": "10.0.0.7"}).status_code in (400, 405)
