"""PostgreSQL specifics: migrations, schema copies, backups / restore, workbook upload, Database page."""
import io
import shutil

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import sessionmaker

from app import backups, config, dbcopy, main, refresh
from app.db import Base, check_schema, drop_schema, list_schemas, schema_url
from app.importer.cli import reset_schema, run_import
from app.models import Customer
from tests.conftest import TEST_URL, needs_source
from tests.test_refresh import TODAY, env  # noqa: F401  (fixture)


# ------------------------------------------------------------------------------------------------ config / schemas

def test_sqlite_is_refused(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///x.db")
    with pytest.raises(config.ConfigError, match="PostgreSQL"):
        config.database_url()


def test_label_never_shows_the_password():
    label = config.database_label("postgresql+psycopg://subs:s3cret@db:5432/subscriptions")
    assert label == "PostgreSQL · subscriptions on db:5432" and "s3cret" not in label


@pytest.mark.parametrize("bad", ["public", "pg_catalog", "x;drop", "Backup", "", "a" * 70, "information_schema"])
def test_schema_names_are_checked(bad):
    with pytest.raises(ValueError):
        check_schema(bad)


def test_schema_travels_in_the_url(new_schema):
    eng = new_schema()
    name = eng.url.query["options"].split("=", 1)[1]
    with eng.connect() as c:
        assert c.execute(text("SELECT current_schema()")).scalar() == name
    assert schema_url(TEST_URL, name) == eng.url.render_as_string(hide_password=False)


def test_migrations_reach_head(new_schema):
    eng = new_schema()
    reset_schema(eng)
    assert dbcopy.schema_revision(eng) == dbcopy.head_revision()
    assert {t.name for t in Base.metadata.sorted_tables} <= set(inspect(eng).get_table_names())


@needs_source
def test_import_twice_gives_the_same_rows(new_schema, tmp_path):
    url = new_schema().url.render_as_string(hide_password=False)
    counts = [run_import(url=url, today=TODAY, report_dir=tmp_path, force=True).db_counts for _ in range(2)]
    assert counts[0] == counts[1] and counts[0]["customers"] > 0


@needs_source
def test_copy_keeps_rows_figures_and_sequences(imported, new_schema):
    Session, _ = imported
    before = refresh.figures(Session, TODAY)
    eng = new_schema()
    counts = dbcopy.copy_engines(Session.kw["bind"], eng)
    assert all(c.source == c.copied for c in counts)
    S = sessionmaker(bind=eng, expire_on_commit=False)
    assert refresh.figures(S, TODAY) == before
    with S() as s:                                   # new rows get fresh ids (sequences moved past the copied ids)
        top = s.scalar(select(Customer.id).order_by(Customer.id.desc()).limit(1))
        c = Customer(qb_number=89998, name="SEQ CHECK")
        s.add(c)
        s.commit()
        assert c.id > top


def test_copy_refuses_an_old_source(new_schema):
    old, dst = new_schema(), new_schema()
    with old.begin() as c:
        c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        c.execute(text("INSERT INTO alembic_version VALUES ('0009')"))
    with pytest.raises(dbcopy.CopyError, match="0009"):
        dbcopy.copy_engines(old, dst)


# ------------------------------------------------------------------------------------------------ backups / restore

@needs_source
def test_backup_restore_round_trip(imported_copy):
    eng = imported_copy()
    url = eng.url.render_as_string(hide_password=False)
    S = sessionmaker(bind=eng, expire_on_commit=False)
    name = backups.make(eng, "test")
    made = [name]
    try:
        assert any(b.name == name and b.label == "test" and b.taken for b in backups.list_all(eng))
        with S() as s:                                # change the live data after the backup
            s.add(Customer(qb_number=89997, name="AFTER BACKUP"))
            s.commit()
        before_restore, counts = backups.restore(name, url)
        made.append(before_restore)
        assert counts and all(c.source == c.copied for c in counts)
        with S() as s:
            assert s.scalar(select(Customer).where(Customer.qb_number == 89997)) is None
        old = backups.engine_for(before_restore, url)   # the pre-restore data was kept
        try:
            with sessionmaker(bind=old)() as s:
                assert s.scalar(select(Customer).where(Customer.qb_number == 89997)) is not None
        finally:
            old.dispose()
    finally:
        for n in made:
            drop_schema(url, n)


def test_prune_keeps_the_newest(new_schema):
    eng = new_schema()
    url = eng.url.render_as_string(hide_password=False)
    names = [f"backup_20000101_00000{i}" for i in range(5)]
    try:
        with eng.begin() as c:
            for n in names:
                c.execute(text(f'CREATE SCHEMA "{n}"'))
        dropped = backups.prune(eng, keep=len(list_schemas(eng, "backup_")) - 2)
        assert dropped == names[:2]
        assert not set(names[:2]) & set(list_schemas(eng, "backup_"))
    finally:
        for n in names:
            drop_schema(url, n)


def test_engine_for_only_opens_backups():
    with pytest.raises(ValueError):
        backups.engine_for("test_imported")


# ------------------------------------------------------------------------------------------------ upload in Refresh

def _upload(c, sub=None, maint=None, reapply="no"):
    files = {}
    if sub:
        files["subscription_file"] = sub
    if maint:
        files["maintenance_file"] = maint
    return c.post("/refresh", data={"source": "upload", "reapply": reapply}, files=files,
                  headers={"origin": "http://testserver"}, follow_redirects=False)


@needs_source
def test_upload_replaces_the_workbooks_and_refreshes(env):  # noqa: F811
    S, eng, paths = env
    sub = config.source_workbook()
    maint_before = sorted(p.name for p in paths.source.glob("*Maintenance*.xlsx"))
    c = TestClient(main.app)
    r = _upload(c, sub=(sub.name, sub.read_bytes(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
    assert r.status_code == 303 and "done=1" in r.headers["location"]
    res = refresh.last_result(paths)
    assert res["uploaded"] and not res["pulled"] and res["backup"].startswith("backup_")
    assert [f["name"] for f in res["files"]] == [sub.name]
    assert sorted(p.name for p in paths.source.glob("*Maintenance*.xlsx")) == maint_before   # kept
    assert len(list(paths.backups.glob("source-*/*.xlsx"))) == 1                             # old copy kept
    drop_schema(None, res["backup"])


@needs_source
@pytest.mark.parametrize("name, body, msg", [
    ("Q3 numbers.xlsx", None, "does not look like"),
    ("x Subscription Summary.xlsx", b"not a workbook", "could not be opened"),
    ("x Subscription Summary.csv", b"a,b", "not an .xlsx"),
    ("x Subscription Summary.xlsx", b"", "is empty"),
])
def test_bad_uploads_change_nothing(env, name, body, msg):  # noqa: F811
    S, eng, paths = env
    if body is None:
        body = config.source_workbook().read_bytes()
    before = sorted((p.name, p.stat().st_mtime) for p in paths.source.iterdir())
    figures = refresh.figures(S, TODAY)
    r = _upload(TestClient(main.app), sub=(name, body, "application/octet-stream"))
    assert r.status_code == 303 and "error=" in r.headers["location"]
    page = TestClient(main.app).get(r.headers["location"]).text
    assert msg.replace("'", "&#39;") in page or msg in page
    assert sorted((p.name, p.stat().st_mtime) for p in paths.source.iterdir()) == before
    assert refresh.figures(S, TODAY) == figures


def test_upload_without_the_subscription_workbook_is_refused(env):  # noqa: F811
    r = _upload(TestClient(main.app))
    assert "error=" in r.headers["location"] and "Subscription" in r.headers["location"].replace("+", " ")


def test_upload_names_are_reduced_to_a_file_name():
    assert refresh.safe_upload_name(r"C:\Users\x\2. Subscription Summary.xlsx") == "2. Subscription Summary.xlsx"
    assert refresh.safe_upload_name("../../etc/x Subscription Summary.xlsx") == "x Subscription Summary.xlsx"
    for bad in ("", "..", "~$x.xlsx", ".x.xlsx", "x.xlsm"):
        with pytest.raises(refresh.RefreshError):
            refresh.safe_upload_name(bad)


@needs_source
def test_refresh_dialog_offers_upload(env):  # noqa: F811
    page = TestClient(main.app).get("/refresh/dialog").text
    assert 'enctype="multipart/form-data"' in page and 'name="subscription_file"' in page
    assert 'value="local"' in page and "disabled" not in page.split('value="local"')[1].split(">")[0]


# ------------------------------------------------------------------------------------------------ Database page

@needs_source
def test_database_page_shows_no_credentials(env):  # noqa: F811
    S, eng, paths = env
    pw = eng.url.password
    page = TestClient(main.app).get("/db").text
    assert "PostgreSQL" in page and "Backups (" in page and "MB including backups" in page
    assert pw and pw not in page and "postgresql+psycopg" not in page


# ------------------------------------------------------------------------------------------------ spreadsheet check

@needs_source
def test_spreadsheet_check_leaves_no_schema_behind(tmp_path):
    from app.checker.importer_findings import run_temp_import
    rep, tmp = run_temp_import(config.source_workbook(), config.maintenance_workbook())
    shutil.rmtree(tmp, ignore_errors=True)
    assert rep.db_counts["customers"] > 0
    from app.db import make_engine
    eng = make_engine(TEST_URL)
    try:
        assert list_schemas(eng, "sheetcheck_") == []
    finally:
        eng.dispose()
