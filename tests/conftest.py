import datetime as dt

import pytest
from sqlalchemy.orm import sessionmaker

from app import config
from app.db import Base, make_engine
from app.importer.cli import run_import
from app.models import Customer, OpenedVia, Platform, Subscription, Term, TermKind

TODAY = dt.date(2026, 11, 12)
HAS_SOURCE = bool(list(config.SOURCE_DIR.glob("*Subscription Summary*.xlsx")))
needs_source = pytest.mark.skipif(not HAS_SOURCE, reason="no workbook in data/source")


@pytest.fixture(scope="session")
def imported(tmp_path_factory):
    """Full import of the copied workbook into a throwaway SQLite DB (read-only on the workbook)."""
    if not HAS_SOURCE:
        pytest.skip("no workbook in data/source")
    d = tmp_path_factory.mktemp("imp")
    url = f"sqlite:///{(d / 'imp.db').as_posix()}"
    report = run_import(url=url, today=TODAY, report_dir=d)
    engine = make_engine(url)
    return sessionmaker(bind=engine, expire_on_commit=False), report


@pytest.fixture
def blank(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'blank.db').as_posix()}")
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
