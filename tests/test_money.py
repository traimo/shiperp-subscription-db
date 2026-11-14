"""Money as Decimal: conversion helpers, the model hook, storage round-trip, exact sums, and migration 0004."""
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app import config, main, metrics
from app.db import make_engine
from app.models import Customer, MaintenanceBlock, MaintenanceContract, MaintenanceFee, OpenedVia, Platform, \
    Subscription, Term, TermKind
from app.money import D, D0, ZERO, cents
from tests.conftest import D as day, seed


# ------------------------------------------------------------------------------------------------ helpers

def test_d_converts_without_binary_noise():
    assert D(0.1) == Decimal("0.1")                    # via repr, not Decimal(0.1) = 0.1000000000000000055...
    assert D("1,234.50") == Decimal("1234.50")
    assert D(5) == Decimal(5) and D(None) is None and D("") is None and D0(None) == ZERO
    with pytest.raises(ValueError):
        D("abc")


def test_cents_rounds_half_up():
    assert cents(Decimal("0.125")) == Decimal("0.13")  # Python's round() would give 0.12 (half-even)
    assert cents(2.675) == Decimal("2.68")             # round(2.675, 2) on a float gives 2.67
    assert cents(Decimal("-0.125")) == Decimal("-0.13")
    assert main.money(Decimal("0.125")) == "0.13" and main.money(Decimal("-1234.5")) == "(1,234.50)"


# ------------------------------------------------------------------------------------------------ model / storage

def test_model_hook_stores_decimal_cents(blank):
    with blank() as s:
        c = Customer(qb_number=80001, name="X")
        sub = Subscription(customer=c, platform=Platform.ON_PREM, opened_via=OpenedVia.NET_NEW)
        t = Term(subscription=sub, kind=TermKind.INITIAL, start_date=day(2024, 1, 1), end_date=day(2026, 12, 31),
                 tcv=0.1 + 0.2, term_months=36)
        assert t.tcv == Decimal("0.30") and isinstance(t.tcv, Decimal)     # 0.30000000000000004 -> 0.30
        t.tcv = "1,234,567.895"
        assert t.tcv == Decimal("1234567.90")
        s.add_all([c, sub, t])
        s.commit()
        tid = t.id
    with blank() as s:
        back = s.get(Term, tid).tcv
        assert isinstance(back, Decimal) and back == Decimal("1234567.90")


def test_sums_are_exact(blank):
    """Ten fees of 0.10 add up to exactly 1.00 (float: 0.9999999999999999)."""
    with blank() as s:
        c = Customer(qb_number=80002, name="Y")
        for i in range(10):
            mc = MaintenanceContract(customer=c, block=MaintenanceBlock.PERPETUAL_LIVE)
            mc.fees.append(MaintenanceFee(year=2026, amount=0.1))
            s.add(mc)
        s.commit()
    with blank() as s:
        c = s.query(Customer).one()
        assert metrics.maintenance_year_arr(c, 2026) == Decimal("1.00")
        assert sum([0.1] * 10) != 1.0                                       # the float problem this avoids


def test_arr_is_full_precision_and_displays_rounded(blank):
    _, _, tid = seed(blank)
    with blank() as s:
        t = s.get(Term, tid)
        t.tcv = 100
        t.term_months = 36
        arr = metrics.term_arr(t)
        assert isinstance(arr, Decimal) and cents(arr) == Decimal("33.33")
        assert cents(arr * 3) == Decimal("100.00")                         # no drift from rounding early


def test_derive_endpoint_returns_numbers():
    from fastapi.testclient import TestClient
    r = TestClient(main.app).post("/api/derive", json={"start_date": "2026-11-07", "end_date": "2029-11-06",
                                                      "tcv": "53,820.00", "term_years": ""})
    d = r.json()
    assert d["ok"] and d["term_years"] == 3 and d["arr"] == 17940.0 and d["monthly"] == 1495.0
    bad = TestClient(main.app).post("/api/derive", json={"start_date": "2026-11-07", "end_date": "2029-11-06",
                                                        "tcv": "abc"}).json()
    assert not bad["ok"]


# ------------------------------------------------------------------------------------------------ migration 0004

def _alembic(engine, target):
    cfg = Config(str(config.PROJECT_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(config.PROJECT_DIR / "alembic"))
    cfg.attributes["engine"] = engine
    command.upgrade(cfg, target)


def test_migration_0004_converts_existing_float_data(tmp_path):
    """A 0003 database holding float amounts (like the real one) upgrades in place: values rounded to the cent,
    rows and foreign keys intact."""
    engine = make_engine(f"sqlite:///{(tmp_path / 'm.db').as_posix()}")
    _alembic(engine, "0003")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO customer (id, qb_number, name, has_perpetual_license) "
                          "VALUES (1, 80001, 'X', 0)"))
        conn.execute(text("INSERT INTO subscription (id, customer_id, platform, opened_via) "
                          "VALUES (1, 1, 'ON_PREM', 'NET_NEW')"))
        conn.execute(text("INSERT INTO term (id, subscription_id, kind, start_date, end_date, tcv, term_months, "
                          "is_revised) VALUES (1, 1, 'INITIAL', '2024-01-01', '2026-12-31', 53820.0, 36.0, 0)"))
        conn.execute(text("INSERT INTO invoice (id, term_id, billing_year, amount) VALUES (1, 1, 2024, 17940.0)"))
        conn.execute(text("INSERT INTO maintenance_contract (id, customer_id, block, go_live, calendar_billing, "
                          "converted) VALUES (1, 1, 'PERPETUAL_LIVE', 1, 0, 0)"))
        conn.execute(text("INSERT INTO license_purchase (id, contract_id, kind, amount, maintenance_amount) "
                          "VALUES (1, 1, 'ORIGINAL', 50000.0, 8333.33333333333)"))
        conn.execute(text("INSERT INTO maintenance_fee (id, contract_id, year, amount, prorated, source) "
                          "VALUES (1, 1, 2026, 52934.35, 0, 'IMPORT')"))
    _alembic(engine, "0004")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0004"
        types = {r[1]: r[2] for r in conn.execute(text("PRAGMA table_info(term)"))}
        assert types["tcv"].upper().startswith("NUMERIC") and types["term_months"].upper() == "FLOAT"
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
    S = sessionmaker(bind=engine)
    with S() as s:
        mc = s.get(MaintenanceContract, 1)
        assert mc.purchases[0].maintenance_amount == Decimal("8333.33")
        assert mc.fees[0].amount == Decimal("52934.35")
        t = s.get(Term, 1)
        assert t.tcv == Decimal("53820.00") and t.invoices[0].amount == Decimal("17940.00")
        assert metrics.term_arr(t) == Decimal("17940")
    engine.dispose()
