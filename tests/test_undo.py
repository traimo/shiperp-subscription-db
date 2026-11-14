"""Undo: every kind of app change can be reversed exactly; later changes block an undo until undone; previews,
pre-feature rows and undo rows are refused; web pages and migration 0009."""
import datetime as dt
import shutil

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app import document_services as docs
from app import main, maintenance_services as ms, services, undo
from app.db import Base, make_engine
from app.models import (AuditLog, ChangeRecord, Customer, Invoice, MaintenanceBlock, MaintenanceContract, OpenedVia,
                        Platform, Subscription, Term, TerminationReason)
from app.services import TermInput
from tests.conftest import D, needs_source, seed

TODAY = D(2026, 11, 13)
SP = "https://erpis.sharepoint.com/sites/Finance/Shared%20Documents/Contracts/X.pdf"


@pytest.fixture
def db(imported, tmp_path):
    """A private copy of the imported database (undo tests write to it)."""
    Session, _ = imported
    src = Session.kw["bind"].url.database
    Session.kw["bind"].dispose()
    dst = tmp_path / "undo.db"
    shutil.copy(src, dst)
    eng = make_engine(f"sqlite:///{dst.as_posix()}")
    S = sessionmaker(bind=eng, expire_on_commit=False)
    old = main.SESSION_FACTORY, main.TODAY
    main.SESSION_FACTORY, main.TODAY = S, TODAY
    yield S, eng
    main.SESSION_FACTORY, main.TODAY = old
    eng.dispose()


def snap(eng):
    with eng.connect() as c:
        return {t: sorted(map(tuple, c.execute(text(f"SELECT * FROM {t}")).fetchall()), key=repr)
                for t in Base.metadata.tables if t not in ("audit_log", "change_record")}


def run(S, op, *a, **kw):
    return services.run(S, op, "tester", *a, commit=True, **kw)


def undo_it(S, out):
    assert out.audit_id, "change was not recorded for undo"
    return run(S, undo.undo_change, out.audit_id)


def ref(S, source_ref):
    with S() as s:
        return s.scalar(select(Term.id).where(Term.source_ref == source_ref))


# ------------------------------------------------------------------------------------------------ every action

@needs_source
def test_undo_restores_every_kind_of_change(db):
    S, eng = db
    with S() as s:
        mc = s.scalars(select(MaintenanceContract).where(MaintenanceContract.block == MaintenanceBlock.PERPETUAL_LIVE,
                                                         MaintenanceContract.ended_on.is_(None))).first()
        mc_id = mc.id
        inv_id = s.scalar(select(Invoice.id))
        sub_id = s.scalar(select(Term.subscription_id).where(Term.id == ref(S, "On-Prem A47")))
    base, addon = ref(S, "On-Prem A47"), ref(S, "On-Prem C146")       # Cytiva: initial + add-on
    actions = [
        lambda: run(S, services.renew, addon, TermInput(start_date=D(2026, 12, 31), end_date=D(2029, 12, 30),
                                                       tcv=252000, signed_date=TODAY), [base], today=TODAY),
        lambda: run(S, services.amend, base, TermInput(start_date=D(2025, 1, 1), end_date=D(2026, 12, 30),
                                                      tcv=200000, signed_date=TODAY), today=TODAY),
        lambda: run(S, services.terminate, sub_id, D(2026, 12, 30), TerminationReason.CHURN, today=TODAY),
        lambda: run(S, services.update_term, base, {"next_action": "call", "golive_date": D(2026, 1, 2)}, ae="ZZ"),
        lambda: run(S, services.record_invoice, base, 2026, D(2026, 6, 1), 1234.5, "INV-1"),
        lambda: run(S, services.delete_invoice, inv_id),
        lambda: run(S, ms.set_fee, mc_id, 2028, 999.99, "test"),
        lambda: run(S, ms.convert, mc_id, TODAY),
        lambda: run(S, ms.annual_roll, 2028, 0.04, None, None),
        lambda: run(S, docs.link, SP, term_id=base),
    ]
    for i, act in enumerate(actions):
        before = snap(eng)
        out = act()
        assert snap(eng) != before, f"action {i} changed nothing"
        u = undo_it(S, out)
        assert u.lines[0].startswith("Undid:")
        assert snap(eng) == before, f"action {i} not fully undone"


@needs_source
def test_undo_new_customer_and_subscription_newest_first(db):
    S, eng = db
    before = snap(eng)
    c = run(S, services.create_customer, 89990, "Undo Test")
    sub = run(S, services.new_subscription, 89990, Platform.ON_PREM, OpenedVia.NET_NEW,
              TermInput(start_date=D(2026, 12, 1), end_date=D(2029, 11, 30), tcv=30000, signed_date=TODAY),
              today=TODAY)
    with pytest.raises(undo.UndoError, match="undo it first"):
        undo_it(S, c)                                            # the subscription depends on the customer
    undo_it(S, sub)
    undo_it(S, c)
    assert snap(eng) == before


@needs_source
def test_later_change_blocks_until_undone(db):
    S, eng = db
    before = snap(eng)
    t = ref(S, "On-Prem A47")
    e1 = run(S, services.update_term, t, {"next_action": "first"})
    e2 = run(S, services.update_term, t, {"next_action": "second"})
    with pytest.raises(undo.UndoError, match="second"):
        undo_it(S, e1)
    undo_it(S, e2)
    undo_it(S, e1)
    assert snap(eng) == before


@needs_source
def test_refusals_and_audit_trail(db):
    S, eng = db
    t = ref(S, "On-Prem A47")
    def counts():
        with S() as s:
            return len(s.scalars(select(ChangeRecord.id)).all()), len(s.scalars(select(AuditLog.id)).all())
    n = counts()
    preview = services.run(S, services.update_term, "tester", t, {"next_action": "x"}, commit=False)
    assert preview.audit_id is None and counts() == n                   # a preview records nothing
    with S() as s:
        old = AuditLog(at=dt.datetime(2026, 1, 1), actor="x", action="renew", entity="term", summary="old change")
        s.add(old)
        s.commit()
        old_id = old.id
    with pytest.raises(undo.UndoError, match="before undo was available"):
        run(S, undo.undo_change, old_id)
    out = run(S, services.update_term, t, {"next_action": "x"})
    undo_it(S, out)
    with pytest.raises(undo.UndoError, match="Already undone"):
        undo_it(S, out)
    with S() as s:
        a = s.get(AuditLog, out.audit_id)
        u = s.scalar(select(AuditLog).where(AuditLog.action == "undo").order_by(AuditLog.id.desc()))
        assert a.undone_by == "tester" and u.undo_of_id == a.id and not u.changes
        with pytest.raises(undo.UndoError, match="itself an undo"):
            undo.check(s, u.id)


# ------------------------------------------------------------------------------------------------ web

@needs_source
def test_undo_pages(db):
    S, eng = db
    t = ref(S, "On-Prem A47")
    c = TestClient(main.app)
    with S() as s:
        qb = s.get(Term, t).subscription.customer.qb_number
    r = c.post(f"/term/{t}/edit", data={"next_action": "web"}, follow_redirects=False)
    assert "undo=" in r.headers["location"]
    page = c.get(r.headers["location"]).text
    assert "Undo this change" in page
    with S() as s:
        aid = s.scalar(select(AuditLog.id).order_by(AuditLog.id.desc()))
    assert f"/audit/{aid}/undo" in c.get("/audit").text
    assert f"/audit/{aid}/undo?back=/customer/{qb}" in c.get(f"/customer/{qb}").text
    page = c.get(f"/audit/{aid}/undo?back=/customer/{qb}").text
    assert "Confirm undo" in page and "Put back contract term" in page
    r = c.post(f"/audit/{aid}/undo", data={"back": f"/customer/{qb}"}, follow_redirects=False)
    assert r.headers["location"].startswith(f"/customer/{qb}?msg=Undid")
    page = c.get(f"/audit/{aid}/undo").text
    assert "Already undone" in page and "Confirm undo" not in page
    assert "undone" in c.get("/audit").text


@needs_source
def test_undo_create_customer_from_its_page_returns_to_audit(db):
    S, eng = db
    out = run(S, services.create_customer, 89991, "Gone Soon")
    r = TestClient(main.app).post(f"/audit/{out.audit_id}/undo", data={"back": "/customer/89991"},
                                  follow_redirects=False)
    assert r.headers["location"].startswith("/audit?msg=")


# ------------------------------------------------------------------------------------------------ migration

@needs_source
def test_migration_0009_down_and_up(db):
    from alembic import command
    from alembic.config import Config
    from app import config
    S, eng = db
    cfg = Config(str(config.PROJECT_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(config.PROJECT_DIR / "alembic"))
    cfg.attributes["engine"] = eng
    command.downgrade(cfg, "0008")
    with eng.connect() as conn:
        assert "change_record" not in [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master"))]
    command.upgrade(cfg, "head")
    with eng.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0010"
        assert "termination_term" in [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master"))]
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(audit_log)"))]
        assert {"undone_at", "undone_by", "undo_of_id"} <= set(cols)
