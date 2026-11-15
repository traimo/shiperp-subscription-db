"""Contract documents = SharePoint links only (no SharePoint connection, no copies, no uploads): link rules, open,
remove, customer links CSV, customer page, re-imports, migration 0007 -> 0008."""
import csv
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import sessionmaker

from app import document_services as docs
from app import main, services
from app.db import make_engine
from app.importer.cli import run_import, run_maintenance_import
from app.models import AuditLog, ContractDocument, MaintenanceContract, Term
from tests.conftest import TODAY, needs_source

HOST = "erpis.sharepoint.com"
LIB = f"https://{HOST}/sites/Finance/Shared%20Documents"
SP = f"{LIB}/Contracts/FFF%20Renewal%202025.pdf"
SP_SHARE = f"https://{HOST}/:b:/s/Finance/EaBcDeF123?e=XyZ"
PDF = b"%PDF-1.4\n%%EOF\n"


@pytest.fixture
def db(tmp_path, monkeypatch, new_schema):
    monkeypatch.delenv("SP_ALLOWED_HOSTS", raising=False)
    engine = new_schema()
    url = engine.url.render_as_string(hide_password=False)
    run_import(url=url, today=TODAY, report_dir=tmp_path)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    old = main.SESSION_FACTORY, main.TODAY
    main.SESSION_FACTORY, main.TODAY = Session, TODAY
    yield url, Session
    main.SESSION_FACTORY, main.TODAY = old
    engine.dispose()


def _term(Session, ref="On-Prem D182"):
    with Session() as s:
        return s.scalar(select(Term.id).where(Term.source_ref == ref))


def _line_with_ref(Session):
    with Session() as s:
        mc = s.scalars(select(MaintenanceContract).where(MaintenanceContract.source_ref.is_not(None))).first()
        return mc.id, mc.source_ref


def _link(Session, url, **kw):
    return services.run(Session, docs.link, "tester", url, commit=True, **kw)


def _doc(Session, doc_id):
    with Session() as s:
        return s.get(ContractDocument, doc_id)


# ------------------------------------------------------------------------------------------------ rules

def test_sharepoint_url_rules(monkeypatch):
    monkeypatch.delenv("SP_ALLOWED_HOSTS", raising=False)
    assert docs.check_sharepoint_url("  " + SP + " ") == SP
    for bad, msg in [("", "Paste"), ("http://erpis.sharepoint.com/x.pdf", "https"),
                     ("https://evil.example/x.pdf", "SharePoint"), ("https://sharepoint.com.evil.example/x", "SharePoint"),
                     ("https://user:pw@erpis.sharepoint.com/x.pdf", "user name"),
                     ("https://erpis.sharepoint.com/" + "a" * 1000, "1,000")]:
        with pytest.raises(services.ServiceError, match=msg):
            docs.check_sharepoint_url(bad)
    monkeypatch.setenv("SP_ALLOWED_HOSTS", HOST)
    with pytest.raises(services.ServiceError, match="must point to"):
        docs.check_sharepoint_url("https://other.sharepoint.com/sites/x/a.pdf")
    assert docs.check_sharepoint_url(SP) == SP
    assert docs.name_from_url(SP) == "FFF Renewal 2025.pdf"
    assert docs.name_from_url(SP_SHARE) == "SharePoint document"
    assert docs.clean_filename("../../etc/passwd.pdf") == "passwd.pdf"


# ------------------------------------------------------------------------------------------------ links

@needs_source
def test_link_open_and_audit(db):
    _, Session = db
    tid = _term(Session)
    out = _link(Session, SP, term_id=tid, note="Countersigned")
    assert out.lines[0].startswith("Linked SharePoint file 'FFF Renewal 2025.pdf'") and not out.warnings
    d = _doc(Session, out.entity_id)
    assert d.url == SP and d.term_id == tid and d.link_source_ref == "On-Prem D182" and d.note == "Countersigned"
    named = _doc(Session, _link(Session, SP_SHARE, term_id=tid, name="Order form.pdf").entity_id)
    assert named.filename == "Order form.pdf"
    with pytest.raises(services.ServiceError, match="already attached"):
        _link(Session, SP, term_id=tid)
    with pytest.raises(services.ServiceError, match="exactly one"):
        _link(Session, SP)
    c = TestClient(main.app)
    r = c.get(f"/document/{d.id}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == SP
    assert c.get("/document/999999", follow_redirects=False).status_code == 404
    page = c.get(f"/term/{tid}").text
    assert "Open in SharePoint" in page and "copy" not in page.lower().replace("countersigned copy", "")
    with Session() as s:
        a = s.scalars(select(AuditLog).where(AuditLog.action == "link")).first()
        assert SP in a.detail


@needs_source
def test_customer_page_link_button_and_csv(db):
    _, Session = db
    tid = _term(Session)
    mid, mref = _line_with_ref(Session)
    with Session() as s:
        t = s.get(Term, tid)
        qb, sid = t.subscription.customer.qb_number, t.subscription.id
    c = TestClient(main.app)
    assert c.get(f"/customer/{qb}/contract-links.csv").status_code == 404           # nothing linked yet
    page = c.get(f"/customer/{qb}").text
    assert "linkDoc(" in page and 'type="file"' not in page and ".zip" not in page
    r = c.post(f"/term/{tid}/documents", data={"url": SP, "back": f"/customer/{qb}", "anchor": f"sub-{sid}"},
               follow_redirects=False)
    loc = r.headers["location"]
    assert loc.startswith(f"/customer/{qb}?msg=") and loc.endswith(f"#sub-{sid}")
    r = c.post(f"/term/{tid}/documents", data={"url": "https://x.example/a.pdf", "back": f"/customer/{qb}"},
               follow_redirects=False)
    assert r.headers["location"].startswith(f"/customer/{qb}?error=")
    r = c.post(f"/term/{tid}/documents", data={"url": SP.replace("FFF", "X"), "back": "https://evil.example"},
               follow_redirects=False)
    assert r.headers["location"].startswith(f"/term/{tid}?")                          # foreign 'back' ignored
    r = c.post(f"/term/{tid}/documents", files=[("files", ("c.pdf", PDF, "application/pdf"))],
               follow_redirects=False)
    assert "Uploads%20are%20turned%20off" in r.headers["location"]
    page = c.get(f"/customer/{qb}").text
    assert "Contract links (.csv, 2)" in page
    r = c.get(f"/customer/{qb}/contract-links.csv")
    assert r.headers["content-type"].startswith("text/csv") and "contract%20links.csv" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 2 and {x["sharepoint_url"] for x in rows} == {SP, SP.replace("FFF", "X")}
    row = next(x for x in rows if x["sharepoint_url"] == SP)
    assert row["attached_to"] == "term" and row["sheet_ref"] == "On-Prem D182" and row["tcv"] and row["arr"]


@needs_source
def test_remove_link(db):
    _, Session = db
    tid = _term(Session)
    d = _doc(Session, _link(Session, SP, term_id=tid).entity_id)
    r = TestClient(main.app).post(f"/document/{d.id}/delete?back=/term/{tid}", follow_redirects=False)
    assert "SharePoint%20file%20is%20untouched" in r.headers["location"]
    assert _doc(Session, d.id) is None


# ------------------------------------------------------------------------------------------------ re-import

@needs_source
def test_links_survive_reimports(db, tmp_path):
    url, Session = db
    tid = _term(Session)
    mid, mref = _line_with_ref(Session)
    _link(Session, SP, term_id=tid)
    _link(Session, f"{LIB}/Contracts/Order.docx", contract_id=mid)
    lost = _link(Session, SP_SHARE, term_id=_term(Session, "On-Prem A15")).entity_id
    with Session() as s:                                            # simulate a term created in the app (no ref)
        s.get(ContractDocument, lost).link_source_ref = None
        s.commit()
    report = run_import(url=url, today=TODAY, report_dir=tmp_path)  # document links never block an import
    with Session() as s:
        d = {x.url: x for x in s.scalars(select(ContractDocument))}
        assert d[SP].term.source_ref == "On-Prem D182"
        assert d[f"{LIB}/Contracts/Order.docx"].maintenance_contract.source_ref == mref
        assert d[SP_SHARE].term_id is None
    assert any("could not find" in n.message for n in report.notes)
    run_maintenance_import(url=url, report_dir=tmp_path)
    with Session() as s:
        d = {x.url: x for x in s.scalars(select(ContractDocument))}
        assert d[f"{LIB}/Contracts/Order.docx"].maintenance_contract.source_ref == mref
        qb = d[SP_SHARE].customer.qb_number
    assert "Unlinked contract documents" in TestClient(main.app).get(f"/customer/{qb}").text


# ------------------------------------------------------------------------------------------------ migration

@needs_source
def test_migration_0008_keeps_links_and_drops_copy_columns(db):
    """0008 -> 0007 -> 0008 round trip keeps every link; the copy columns are gone at head."""
    from alembic import command
    from alembic.config import Config
    url, Session = db
    tid = _term(Session)
    _link(Session, SP, term_id=tid, note="n")
    from app import config
    eng = make_engine(url)
    cfg = Config(str(config.PROJECT_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(config.PROJECT_DIR / "alembic"))
    cfg.attributes["engine"] = eng
    try:
        command.downgrade(cfg, "0007")
        cols = {c["name"] for c in inspect(eng).get_columns("contract_document")}
        assert {"storage_key", "sha256", "copy_error"} <= cols
        command.upgrade(cfg, "head")
        insp = inspect(eng)
        cols = {c["name"] for c in insp.get_columns("contract_document")}
        assert not cols & {"content_type", "size_bytes", "sha256", "storage_key", "copied_at", "copy_error",
                           "sp_etag", "sp_modified"}
        assert {i["name"] for i in insp.get_indexes("contract_document")} == {
            "ix_contract_document_customer_id", "ix_contract_document_term_id",
            "ix_contract_document_maintenance_contract_id"}
        with eng.connect() as conn:
            assert conn.execute(text("SELECT url, note, term_id FROM contract_document")).all() == [(SP, "n", tid)]
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() >= "0008"
    finally:
        eng.dispose()
