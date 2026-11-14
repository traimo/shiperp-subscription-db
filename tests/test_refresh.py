"""Refresh data: SharePoint pull (faked), backup + change archive, re-import, reapply (best effort), web pages."""
import datetime as dt
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import config, document_services as docs, main, reapply as rp, refresh, services, sharepoint, undo
from app.db import make_engine
from app.models import AuditLog, ContractDocument, Customer, Subscription, Term, TermKind, TerminationReason
from app.models import OpenedVia, Platform
from app.services import TermInput
from tests.conftest import D, needs_source

TODAY = D(2026, 11, 13)
SP = "https://erpis.sharepoint.com/sites/Finance/Shared%20Documents/Contracts/X.pdf"


@pytest.fixture
def env(imported, tmp_path):
    """Private copy of the imported DB + a private source / backups / reports folder."""
    Session, _ = imported
    src = Session.kw["bind"].url.database
    Session.kw["bind"].dispose()
    dst = tmp_path / "refresh.db"
    shutil.copy(src, dst)
    eng = make_engine(f"sqlite:///{dst.as_posix()}")
    S = sessionmaker(bind=eng, expire_on_commit=False)
    source = tmp_path / "source"
    source.mkdir()
    for p in config.SOURCE_DIR.glob("*.xlsx"):
        if not p.name.startswith("~$"):
            shutil.copy2(p, source / p.name)
    paths = refresh.Paths(source=source, backups=tmp_path / "backups", reports=tmp_path / "reports")
    old = main.SESSION_FACTORY, main.TODAY, refresh.PATHS
    main.SESSION_FACTORY, main.TODAY, refresh.PATHS = S, TODAY, paths
    yield S, eng, paths
    main.SESSION_FACTORY, main.TODAY, refresh.PATHS = old
    eng.dispose()


def run(S, op, *a, **kw):
    return services.run(S, op, "tester", *a, commit=True, **kw)


def ref(S, source_ref):
    with S() as s:
        return s.scalar(select(Term.id).where(Term.source_ref == source_ref))


def baseline(S) -> int:
    """Changes already in the shared imported DB (other test modules write to it)."""
    with S() as s:
        return len(rp.pending_changes(s))


def make_changes(S):
    """One of each kind: replayable, kept (document), manual (invoice), and an undone change (not pending)."""
    run(S, services.create_customer, 89990, "Refresh Test")
    new = run(S, services.new_subscription, 89990, Platform.ON_PREM, OpenedVia.NET_NEW,
              TermInput(start_date=D(2026, 12, 1), end_date=D(2029, 11, 30), tcv=30000, signed_date=TODAY, ae="TR"),
              today=TODAY)
    with S() as s:
        sub_id = s.get(Term, new.entity_id).subscription_id
    run(S, services.add_on, sub_id, TermInput(start_date=D(2027, 1, 1), end_date=D(2029, 11, 30), tcv=9000),
        today=TODAY)
    base, addon = ref(S, "On-Prem A47"), ref(S, "On-Prem C146")          # Cytiva: initial + add-on
    run(S, services.update_term, base, {"next_action": "call accounting"}, ae="ZZ")
    run(S, services.renew, addon, TermInput(start_date=D(2026, 12, 31), end_date=D(2029, 12, 30), tcv=252000,
                                            signed_date=TODAY), [base], today=TODAY)
    run(S, services.terminate, sub_id, D(2028, 6, 30), TerminationReason.CHURN, note="test", today=TODAY)
    run(S, services.record_invoice, base, 2026, D(2026, 6, 1), 1234.5, "INV-1")
    run(S, docs.link, SP, term_id=base)
    tmp = run(S, services.update_term, base, {"notes": "undone later"})
    run(S, undo.undo_change, tmp.audit_id)


def customer(S, qb):
    with S() as s:
        return s.scalar(select(Customer).where(Customer.qb_number == qb))


# ------------------------------------------------------------------------------------------------ refresh

@needs_source
def test_refresh_without_reapply_matches_workbook_and_keeps_backup(env, imported):
    S, eng, paths = env
    n0 = baseline(S)
    make_changes(S)
    r = refresh.run(S, "tester", TODAY, reapply=False, pull_from_sharepoint=False, paths=paths)
    assert customer(S, 89990) is None                                   # app changes are gone
    with S() as s:
        assert len(s.scalars(select(Term.id)).all()) == imported[1].db_counts["terms"]
        assert s.scalars(select(AuditLog)).all() == []
        assert s.scalar(select(ContractDocument).where(ContractDocument.url == SP)) is not None   # links survive
    assert Path(r["backup"]).exists() and Path(r["archive"]).exists()
    assert r["pending"] == n0 + 8 and r["items"] == []
    archived = json.loads(Path(r["archive"]).read_text(encoding="utf-8"))
    assert any("89990" in a["summary"] for a in archived)
    old = make_engine(f"sqlite:///{Path(r['backup']).as_posix()}")     # the backup still has the changes
    with sessionmaker(bind=old)() as s:
        assert s.scalar(select(Customer).where(Customer.qb_number == 89990)) is not None
    old.dispose()
    assert r["before"]["arr_now"] != r["after"]["arr_now"] or r["before"]["carr_now"] != r["after"]["carr_now"]
    assert refresh.last_result(paths)["started"] == r["started"]


@needs_source
def test_refresh_with_reapply_replays_supported_changes(env):
    S, eng, paths = env
    with S() as s:
        first = (s.scalar(select(AuditLog.id).order_by(AuditLog.id.desc()).limit(1)) or 0) + 1
    make_changes(S)
    r = refresh.run(S, "tester", TODAY, reapply=True, pull_from_sharepoint=False, paths=paths)
    st = {}
    for i in (x for x in r["items"] if x["audit_id"] >= first):
        st.setdefault(i["status"], []).append(i)
    assert len(st["reapplied"]) == 6, [(i["action"], i["status"], i["message"]) for i in r["items"]]
    assert [i["action"] for i in st["manual"]] == ["invoice"]
    assert [i["entity"] for i in st["kept"]] == ["document"]
    assert "failed" not in st
    c = customer(S, 89990)
    with S() as s:
        sub = s.scalar(select(Subscription).join(Customer).where(Customer.qb_number == 89990))
        assert sorted(t.kind for t in sub.terms) == [TermKind.ADD_ON, TermKind.INITIAL]
        assert sub.terminations[0].cancelled_on == D(2028, 6, 30)
        assert next(t for t in sub.terms if t.kind == TermKind.INITIAL).ae.initials == "TR"
        base = s.get(Term, ref(S, "On-Prem A47"))
        assert base.next_action == "call accounting" and base.ae.initials == "ZZ" and base.notes != "undone later"
        new = base.superseded_by[0].new_term
        assert new.kind == TermKind.RENEWAL and new.tcv == 252000
        assert {x.old_term.source_ref for x in new.supersedes} == {"On-Prem A47", "On-Prem C146"}
        rows = [a for a in s.scalars(select(AuditLog).order_by(AuditLog.id))
                if json.loads(a.detail)["reapplied_from"]["audit_id"] >= first]
        assert len(rows) == 6 and all(a.summary.startswith(rp.PREFIX) for a in rows)
        assert json.loads(rows[0].detail)["reapplied_from"]["actor"] == "tester"
    assert c is not None
    last = st["reapplied"][-1]["new_audit_id"]                          # reapplied changes can be undone
    with S() as s:
        undo.check(s, last)


@needs_source
def test_reapply_skips_changes_that_are_already_there(env):
    """Replaying into a database that already has the changes (as if accounting typed them into the workbook)."""
    S, eng, paths = env
    with S() as s:
        first = (s.scalar(select(AuditLog.id).order_by(AuditLog.id.desc()).limit(1)) or 0) + 1
    make_changes(S)
    items = [i for i in rp.reapply(S, S, "tester", TODAY) if i.audit_id >= first]
    st = {i.status for i in items if i.action not in ("invoice", "link")}
    assert st == {"skipped"}, [(i.action, i.status, i.message) for i in items]


@needs_source
def test_refresh_is_refused_while_running(env):
    S, eng, paths = env
    assert refresh._LOCK.acquire(blocking=False)
    try:
        with pytest.raises(refresh.RefreshError, match="already running"):
            refresh.run(S, "tester", TODAY, pull_from_sharepoint=False, paths=paths)
        res = TestClient(main.app).get("/customers")
        assert res.status_code == 503 and "Refreshing data" in res.text
    finally:
        refresh._LOCK.release()


# ------------------------------------------------------------------------------------------------ SharePoint pull

class FakeGraph:
    """Serves the files from a folder as if they were on SharePoint; junk=True returns a broken download."""

    def __init__(self, folder: Path, junk: bool = False):
        self.folder, self.junk = folder, junk

    def resolve(self, key, url):
        p = next(x for x in self.folder.glob(refresh.PATTERNS[key]))
        return sharepoint.RemoteFile(key, p.name, p.stat().st_size, dt.datetime(2026, 11, 13, 9, 0), "Accounting",
                                     "drive", str(p))

    def download(self, f, dest):
        if self.junk:
            dest.write_bytes(b"not a workbook")
        else:
            shutil.copy2(f.item_id, dest)
        return dest


@pytest.fixture
def sp_env(monkeypatch):
    for k in ("SP_TENANT_ID", "SP_CLIENT_ID", "SP_CLIENT_SECRET", "SP_SUBSCRIPTION_URL", "SP_MAINTENANCE_URL"):
        monkeypatch.setenv(k, "x")


@needs_source
def test_pull_swaps_files_and_keeps_old_copies(env, sp_env, tmp_path):
    S, eng, paths = env
    remote = tmp_path / "remote"
    shutil.copytree(paths.source, remote)
    r = refresh.run(S, "tester", TODAY, pull_from_sharepoint=True, paths=paths, client=FakeGraph(remote))
    assert {f["key"] for f in r["files"]} == {"subscription", "maintenance"}
    assert r["files"][0]["modified_by"] == "Accounting"
    assert len(list(paths.source.glob("*.xlsx"))) == 2
    assert len(list(paths.backups.glob("source-*/*.xlsx"))) == 2


@needs_source
def test_bad_download_changes_nothing(env, sp_env, tmp_path):
    S, eng, paths = env
    make_changes(S)
    before = sorted((p.name, p.stat().st_mtime) for p in paths.source.iterdir())
    with pytest.raises(refresh.RefreshError, match="could not be opened"):
        refresh.run(S, "tester", TODAY, pull_from_sharepoint=True, paths=paths, client=FakeGraph(paths.source, True))
    assert sorted((p.name, p.stat().st_mtime) for p in paths.source.iterdir()) == before
    assert not paths.backups.exists()
    assert customer(S, 89990) is not None


def test_sharepoint_not_configured(monkeypatch):
    for k in ("SP_SUBSCRIPTION_URL", "SP_MAINTENANCE_URL"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(sharepoint.SharePointError, match="SP_SUBSCRIPTION_URL"):
        sharepoint.resolve_all()
    assert sharepoint.encode_share_url("https://a/b") == "u!aHR0cHM6Ly9hL2I"


# ------------------------------------------------------------------------------------------------ pages

@needs_source
def test_dialog_defaults_to_do_not_reapply(env, monkeypatch):
    S, eng, paths = env
    n0 = baseline(S)
    make_changes(S)
    monkeypatch.setattr(sharepoint, "resolve_all", lambda *a: (_ for _ in ()).throw(
        sharepoint.SharePointError("not set up")))
    html = TestClient(main.app).get("/refresh/dialog").text
    assert 'name="reapply" value="no" checked' in html
    assert 'value="yes" checked' not in html
    assert f"{n0 + 8} changes made in the app since the last import" in html
    assert 'name="source" value="local" checked' in html and "not set up" in html


@needs_source
def test_refresh_post_and_result_page(env, monkeypatch):
    S, eng, paths = env
    monkeypatch.delenv("SP_SUBSCRIPTION_URL", raising=False)
    make_changes(S)
    client = TestClient(main.app)
    res = client.post("/refresh", data={"source": "local", "reapply": "no"}, follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "/refresh/result?done=1"
    page = client.get(res.headers["location"]).text
    assert "How the numbers changed" in page and "changes not reapplied" in page
    assert customer(S, 89990) is None
    res = client.post("/refresh", data={"source": "sharepoint"}, follow_redirects=False)   # not configured
    assert "error=" in res.headers["location"]


@needs_source
def test_reapply_two_amendments_with_the_same_period(env):
    """One contract change amends a base term and its add-on: two AMENDMENT terms with equal dates (seen on 80232)."""
    S, eng, paths = env
    base, addon = ref(S, "On-Prem A47"), ref(S, "On-Prem C146")
    with S() as s:
        start = max(s.get(Term, base).start_date, s.get(Term, addon).start_date)
    period = dict(start_date=start, end_date=D(2027, 6, 30), signed_date=TODAY)
    run(S, services.amend, base, TermInput(tcv=150000, **period), today=TODAY)
    run(S, services.amend, addon, TermInput(tcv=12000, **period), today=TODAY)
    r = refresh.run(S, "tester", TODAY, reapply=True, pull_from_sharepoint=False, paths=paths)
    amends = [i for i in r["items"] if i["action"] == "amend"]
    assert [i["status"] for i in amends][-2:] == ["reapplied", "reapplied"], amends
