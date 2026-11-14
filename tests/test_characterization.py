"""Characterization tests (refactor 2026-11-14): pin where each form post redirects and what message it carries,
and the number of SQL queries of the busiest pages, so refactoring cannot change them unnoticed.
Redirects are compared decoded (path, query parameters, anchor) - the exact percent-encoding is not behavior."""
import shutil
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker

from app import main
from app.db import make_engine
from app.models import AuditLog, Invoice, MaintenanceContract, Term
from tests.conftest import D, needs_source

TODAY = D(2026, 11, 13)
SP = "https://erpis.sharepoint.com/sites/Finance/Shared%20Documents/Contracts/X.pdf"


@pytest.fixture
def app_db(imported, tmp_path):
    Session, _ = imported
    src = Session.kw["bind"].url.database
    Session.kw["bind"].dispose()
    dst = tmp_path / "char.db"
    shutil.copy(src, dst)
    eng = make_engine(f"sqlite:///{dst.as_posix()}")
    S = sessionmaker(bind=eng, expire_on_commit=False)
    old = main.SESSION_FACTORY, main.TODAY
    main.SESSION_FACTORY, main.TODAY = S, TODAY
    yield S, eng, TestClient(main.app)
    main.SESSION_FACTORY, main.TODAY = old
    eng.dispose()


def loc(res):
    """(path, {param: value}, fragment) of a redirect."""
    assert res.status_code == 303, res.text[:300]
    u = urlsplit(res.headers["location"])
    return u.path, {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}, u.fragment


def ref(S, source_ref):
    with S() as s:
        return s.scalar(select(Term.id).where(Term.source_ref == source_ref))


def last_audit(S):
    with S() as s:
        return s.scalar(select(AuditLog.id).order_by(AuditLog.id.desc()).limit(1))


@needs_source
def test_customer_and_term_redirects(app_db):
    S, eng, c = app_db
    path, q, _ = loc(c.post("/customers/new", data={"qb_number": "89990", "name": "Char Test"},
                            follow_redirects=False))
    assert (path, q) == ("/customer/89990", {"msg": "Customer created"})

    t = ref(S, "On-Prem A47")
    path, q, _ = loc(c.post(f"/term/{t}/edit", data={}, follow_redirects=False))
    assert (path, q) == (f"/term/{t}", {"msg": "No changes"})
    path, q, _ = loc(c.post(f"/term/{t}/edit", data={"next_action": "call"}, follow_redirects=False))
    assert path == f"/term/{t}" and q == {"msg": "Saved: next_action: 'For renewal - 12/31/2026' -> 'call'",
                                          "undo": str(last_audit(S))}

    path, q, _ = loc(c.post(f"/term/{t}/checks", data={}, follow_redirects=False))
    assert (path, q) == (f"/term/{t}", {"msg": "Checklist: 0 change(s)"})

    path, q, _ = loc(c.post(f"/term/{t}/invoice", data={"billing_year": "2026", "invoice_date": "2026-06-01",
                                                         "amount": "1234.50", "reference": "INV-1"},
                            follow_redirects=False))
    assert (path, q) == (f"/term/{t}", {"msg": "Invoice 2026: 1,234.50 on 06/01/2026 - Total invoiced 181,234.50 "
                                                "exceeds the term's TCV 180,000.00."})
    path, q, _ = loc(c.post(f"/term/{t}/invoice", data={"billing_year": "x", "amount": "1"},
                            follow_redirects=False))
    assert path == f"/term/{t}" and "invalid literal" in q["error"]
    with S() as s:
        inv = s.scalar(select(Invoice.id).where(Invoice.reference == "INV-1"))
    path, q, _ = loc(c.post(f"/invoice/{inv}/delete?term_id={t}", follow_redirects=False))
    assert (path, q) == (f"/term/{t}", {"msg": "Invoice removed"})


@needs_source
def test_workflow_undo_and_document_redirects(app_db):
    S, eng, c = app_db
    t = ref(S, "On-Prem A47")
    path, q, _ = loc(c.post("/wf/amend", data={"term": t, "start_date": "2025-01-01", "end_date": "2026-12-30",
                                               "tcv": "200000", "_confirm": "1"}, follow_redirects=False))
    assert path == "/customer/80221" and q == {"msg": "Amendment for 80221 saved", "undo": str(last_audit(S))}

    audit = int(q["undo"])
    path, q, _ = loc(c.post(f"/audit/{audit}/undo", data={"back": "/customer/80221"}, follow_redirects=False))
    assert path == "/customer/80221" and q["msg"].startswith("Undid: AMENDMENT term 01/01/2025-12/30/2026")
    path, q, _ = loc(c.post(f"/audit/{audit}/undo", data={"back": "//evil.example"}, follow_redirects=False))
    assert path == "/audit" and q["error"].startswith("Already undone on ")

    path, q, frag = loc(c.post(f"/term/{t}/documents", data={"url": SP, "back": "/customer/80221",
                                                             "anchor": "sub-1"}, follow_redirects=False))
    assert path == "/customer/80221" and frag == "sub-1" and q["msg"].startswith("Linked SharePoint file 'X.pdf'")
    path, q, frag = loc(c.post(f"/term/{t}/documents", data={"url": "http://x", "back": "https://evil.example"},
                               follow_redirects=False))
    assert path == f"/term/{t}" and frag == "documents" and "error" in q and "msg" not in q


@needs_source
def test_maintenance_redirects(app_db):
    S, eng, c = app_db
    with S() as s:
        mc = s.scalar(select(MaintenanceContract.id).where(MaintenanceContract.ended_on.is_(None)).limit(1))
    path, q, _ = loc(c.post(f"/maintenance/{mc}/fee", data={"year": "2030", "amount": "999.99", "note": "t"},
                            follow_redirects=False))
    assert path == f"/maintenance/{mc}" and set(q) == {"msg"} and "2029-2030" in q["msg"]
    path, q, _ = loc(c.post(f"/maintenance/{mc}/fee", data={"year": "x"}, follow_redirects=False))
    assert path == f"/maintenance/{mc}" and set(q) == {"error"}


@needs_source
def test_query_counts_do_not_grow(app_db):
    """Upper bounds measured before the refactor (dashboard 206, maintenance contract page ~845)."""
    S, eng, c = app_db
    n = {"q": 0}
    event.listen(eng, "before_cursor_execute", lambda *a: n.__setitem__("q", n["q"] + 1))

    def queries(url):
        n["q"] = 0
        assert c.get(url).status_code == 200
        return n["q"]
    with S() as s:
        mc = s.scalar(select(MaintenanceContract.id).limit(1))
    # after eager loading (refactor 2026-11-14): 12 / 15 / 10 queries
    assert queries("/") <= 20
    assert queries(f"/maintenance/{mc}") <= 25
    assert queries("/customers") <= 20
