"""PostgreSQL lane: the same scenarios as the SQLite suite, on a real PostgreSQL database.

Skipped unless TEST_DATABASE_URL points at a *throwaway* PostgreSQL database (every test drops its tables), e.g.
    TEST_DATABASE_URL=postgresql+psycopg://subs:<pw>@127.0.0.1:5433/subs_test python -m pytest -q tests/test_postgres.py

How: the test functions of the modules below are imported here, and the database fixtures they use (`blank`, `db`,
`env`) are redefined at the bottom of this file to hand out PostgreSQL sessions instead of SQLite ones. The
`imported` fixture stays the session-wide SQLite import; PostgreSQL copies of it are made with app/dbcopy.py.
"""
import os
import shutil

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import sessionmaker

from app import config, dbcopy, main, refresh
from app.db import Base, make_engine
from app.importer.cli import reset_schema, run_import
from app.models import Customer, Term

PG_URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL.startswith("postgresql"), reason="TEST_DATABASE_URL not set (PostgreSQL)")

# --- the scenarios (their own helpers keep working: a function looks up names in the module that defined it)
from tests.test_undo import *                     # noqa: E402,F401,F403
from tests.test_refresh import *                  # noqa: E402,F401,F403
from tests.test_feedback_1114 import *            # noqa: E402,F401,F403
from tests.test_renewal_merge_and_forms import *  # noqa: E402,F401,F403
from tests.test_maintenance import *              # noqa: E402,F401,F403
from tests.test_metrics_services import *         # noqa: E402,F401,F403
from tests.conftest import needs_source           # noqa: E402

del test_migration_0009_down_and_up               # noqa: F821  (reads sqlite_master; PostgreSQL version below)

TODAY_PG = refresh.dt.date(2026, 11, 13)


# ------------------------------------------------------------------------------------------------ fixtures (PostgreSQL)

@pytest.fixture
def pg_engine():
    eng = make_engine(PG_URL)
    yield eng
    eng.dispose()


def _pg_copy_of(imported, pg_engine):
    Session, _ = imported
    dbcopy.copy_engines(Session.kw["bind"], pg_engine)
    return sessionmaker(bind=pg_engine, expire_on_commit=False)


@pytest.fixture
def blank(pg_engine):
    reset_schema(pg_engine)                       # empty tables at the newest migration
    return sessionmaker(bind=pg_engine, expire_on_commit=False)


@pytest.fixture
def db(imported, pg_engine):
    S = _pg_copy_of(imported, pg_engine)
    old = main.SESSION_FACTORY, main.TODAY
    main.SESSION_FACTORY, main.TODAY = S, TODAY_PG
    yield S, pg_engine
    main.SESSION_FACTORY, main.TODAY = old


@pytest.fixture
def env(imported, pg_engine, tmp_path):
    S = _pg_copy_of(imported, pg_engine)
    source = tmp_path / "source"
    source.mkdir()
    for p in config.SOURCE_DIR.glob("*.xlsx"):
        if not p.name.startswith("~$"):
            shutil.copy2(p, source / p.name)
    paths = refresh.Paths(source=source, backups=tmp_path / "backups", reports=tmp_path / "reports")
    old = main.SESSION_FACTORY, main.TODAY, refresh.PATHS
    main.SESSION_FACTORY, main.TODAY, refresh.PATHS = S, TODAY_PG, paths
    yield S, pg_engine, paths
    main.SESSION_FACTORY, main.TODAY, refresh.PATHS = old


# ------------------------------------------------------------------------------------------------ PostgreSQL-only checks

def test_pg_migrations_reach_head(pg_engine):
    reset_schema(pg_engine)
    assert dbcopy.schema_revision(pg_engine) == dbcopy.head_revision()
    tables = set(inspect(pg_engine).get_table_names())
    assert {t.name for t in Base.metadata.sorted_tables} <= tables


@needs_source
def test_pg_import_twice_gives_the_same_rows(pg_engine, tmp_path):
    counts = []
    for _ in range(2):
        report = run_import(url=PG_URL, today=TODAY_PG, report_dir=tmp_path)
        counts.append(report.db_counts)
    assert counts[0] == counts[1] and counts[0]["customers"] > 0


@needs_source
def test_pg_copy_round_trip_keeps_rows_and_figures(imported, pg_engine, tmp_path):
    Session, _ = imported
    before = refresh.figures(Session, TODAY_PG)
    counts = dbcopy.copy_engines(Session.kw["bind"], pg_engine)
    assert all(c.source == c.copied for c in counts)
    S = sessionmaker(bind=pg_engine, expire_on_commit=False)
    assert refresh.figures(S, TODAY_PG) == before
    # new rows get fresh ids (the sequences were moved past the copied ids)
    with S() as s:
        top = s.scalar(select(Customer.id).order_by(Customer.id.desc()).limit(1))
        c = Customer(qb_number=89998, name="SEQ CHECK")
        s.add(c)
        s.commit()
        assert c.id > top
    # and back into a SQLite file (what a Refresh backup does)
    back = make_engine(f"sqlite:///{(tmp_path / 'back.db').as_posix()}")
    try:
        dbcopy.copy_engines(pg_engine, back)
        B = sessionmaker(bind=back, expire_on_commit=False)
        with B() as s:
            assert s.scalar(select(Customer.id).where(Customer.qb_number == 89998)) is not None
    finally:
        back.dispose()


def test_pg_copy_refuses_an_old_source(pg_engine, tmp_path):
    old = make_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    try:
        with old.begin() as c:
            c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            c.execute(text("INSERT INTO alembic_version VALUES ('0009')"))
        with pytest.raises(dbcopy.CopyError, match="0009"):
            dbcopy.copy_engines(old, pg_engine)
    finally:
        old.dispose()


@needs_source
def test_pg_refresh_backup_is_a_sqlite_file(env):
    S, eng, paths = env
    r = refresh.run(S, "tester", TODAY_PG, reapply=False, pull_from_sharepoint=False, paths=paths)
    backup = make_engine(f"sqlite:///{r['backup']}")
    try:
        with backup.connect() as c:
            assert c.execute(select(Term.id).limit(1)).first() is not None
    finally:
        backup.dispose()
    assert r["before"] and r["after"]


def test_pg_migration_0009_down_and_up(db):
    from alembic import command
    from alembic.config import Config
    S, eng = db
    cfg = Config(str(config.PROJECT_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(config.PROJECT_DIR / "alembic"))
    cfg.attributes["engine"] = eng
    command.downgrade(cfg, "0008")
    assert "change_record" not in inspect(eng).get_table_names()
    command.upgrade(cfg, "head")
    assert dbcopy.schema_revision(eng) == dbcopy.head_revision()
    assert "termination_term" in inspect(eng).get_table_names()
    cols = {c["name"] for c in inspect(eng).get_columns("audit_log")}
    assert {"undone_at", "undone_by", "undo_of_id"} <= cols
