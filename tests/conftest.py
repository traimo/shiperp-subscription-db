"""Tests run on PostgreSQL: the throwaway database `subs_test` of the compose Postgres (docker compose up -d).

TEST_DATABASE_URL overrides it; by default it is built from POSTGRES_PASSWORD in .env at 127.0.0.1:5433/subs_test.
DATABASE_URL is pointed at the same test database before the app is imported, so no test can reach the live
`subscriptions` database. Every test that writes gets its own schema (t_<pid>_<n>), dropped afterwards.
"""
import datetime as dt
import itertools
import os
from pathlib import Path


def _test_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        env = Path(__file__).resolve().parents[1] / ".env"
        pw = ""
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("POSTGRES_PASSWORD="):
                    pw = line.split("=", 1)[1].strip().strip('"')
        url = f"postgresql+psycopg://subs:{pw}@127.0.0.1:5433/subs_test"
    from sqlalchemy.engine import make_url
    if make_url(url).database in (None, "", "subscriptions", "postgres"):
        raise RuntimeError("TEST_DATABASE_URL must point at a throwaway database (e.g. subs_test), "
                           "never the live one")
    return url


TEST_URL = _test_url()
os.environ["DATABASE_URL"] = TEST_URL         # before any app import: the app's own engine uses the test database
os.environ["AUTH_DISABLED"] = "1"             # page tests run without sign-in (tests/test_auth.py covers sign-in)
os.environ["APP_ALLOWED_HOSTS"] = ""          # loopback only, so AUTH_DISABLED is allowed whatever .env says

import pytest                                  # noqa: E402
from sqlalchemy import create_engine, text     # noqa: E402
from sqlalchemy.orm import sessionmaker        # noqa: E402

from app import config, dbcopy                 # noqa: E402
from app.db import Base, drop_schema, make_engine  # noqa: E402
from app.importer.cli import run_import        # noqa: E402
from app.models import Customer, OpenedVia, Platform, Subscription, Term, TermKind  # noqa: E402

TODAY = dt.date(2026, 11, 12)
HAS_SOURCE = bool(list(config.SOURCE_DIR.glob("*Subscription Summary*.xlsx")))
needs_source = pytest.mark.skipif(not HAS_SOURCE, reason="no workbook in data/source")
_n = itertools.count()


def _reachable() -> str:
    try:
        from sqlalchemy.engine import make_url
        u = make_url(TEST_URL)
        admin = create_engine(u.set(database="postgres"), future=True, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as c:
                if not c.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": u.database}).first():
                    c.execute(text(f'CREATE DATABASE "{u.database}"'))
        finally:
            admin.dispose()
        return ""
    except Exception as e:                    # noqa: BLE001
        return f"PostgreSQL test database not reachable ({type(e).__name__}) - start it with: docker compose up -d db"


def pytest_collection_modifyitems(config, items):
    why = _reachable()
    if why:
        skip = pytest.mark.skip(reason=why)
        for item in items:
            item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def _drop_leftover_schemas():
    """Backups made by Refresh tests and any schema a crashed run left behind (test database only)."""
    yield
    if _reachable():
        return
    eng = create_engine(TEST_URL, future=True)
    try:
        from app.db import list_schemas
        for prefix in ("backup_", "sheetcheck_", "t_"):
            for name in list_schemas(eng, prefix):
                drop_schema(TEST_URL, name)
    finally:
        eng.dispose()


@pytest.fixture
def new_schema():
    """Factory: an engine on a fresh, empty schema of the test database (dropped after the test)."""
    made = []

    def make(prefix: str = "t"):
        name = f"{prefix}_{os.getpid()}_{next(_n)}"
        eng = make_engine(TEST_URL, schema=name)
        made.append((name, eng))
        return eng
    yield make
    for name, eng in made:
        eng.dispose()
        drop_schema(TEST_URL, name)


@pytest.fixture(scope="session")
def imported(tmp_path_factory):
    """Full import of the workbooks in data/source into schema test_imported (read-only on the workbooks)."""
    if not HAS_SOURCE:
        pytest.skip("no workbook in data/source")
    d = tmp_path_factory.mktemp("imp")
    engine = make_engine(TEST_URL, schema="test_imported")
    url = engine.url.render_as_string(hide_password=False)
    report = run_import(url=url, today=TODAY, report_dir=d, force=True)
    yield sessionmaker(bind=engine, expire_on_commit=False), report
    engine.dispose()


@pytest.fixture
def imported_copy(imported, new_schema):
    """Factory: a private writable copy of the imported data (engine on its own schema)."""
    def make():
        eng = new_schema()
        dbcopy.copy_engines(imported[0].kw["bind"], eng)
        return eng
    return make


@pytest.fixture
def blank(new_schema):
    engine = new_schema()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def D(y, m, d):
    return dt.date(y, m, d)


def seed(factory, qb=80001, platform=Platform.ON_PREM):
    """Customer with one 3-year INITIAL term 2024-01-01..2026-12-31, TCV 36,000 (ARR 12,000)."""
    with factory() as s:
        c = Customer(qb_number=qb, name=f"CUST {qb}")
        sub = Subscription(customer=c, platform=platform, opened_via=OpenedVia.NET_NEW)
        t = Term(subscription=sub, kind=TermKind.INITIAL, start_date=D(2024, 1, 1), end_date=D(2026, 12, 31),
                 tcv=36000, term_months=36, signed_date=D(2023, 12, 1))
        s.add_all([c, sub, t])
        s.commit()
        return c.id, sub.id, t.id
