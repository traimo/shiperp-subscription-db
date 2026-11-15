"""Maintenance register: import, calculations, workflows, reload safety, pages."""
import datetime as dt
import shutil
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import main, metrics, services
from app import maintenance_services as ms
from app.money import cents
from app.models import (AuditLog, Customer, FeeSource, LicenseProduct, MaintenanceBlock, MaintenanceContract,
                        MaintenanceEndReason, MaintenanceFee, OpenedVia, Platform, PurchaseKind, Subscription)
from tests.conftest import TODAY, needs_source

D = dt.date


def contract_by_ref(s, ref):
    return s.scalar(select(MaintenanceContract).where(MaintenanceContract.source_ref == ref))


def run(factory, op, *a, commit=True, **kw):
    return services.run(factory, op, "tester", *a, commit=commit, **kw)


# ------------------------------------------------------------------------------------------------ import

@needs_source
def test_import_register(imported):
    Session, r = imported
    for y, (sheet, db) in r.maintenance_totals.items():
        assert abs(sheet - db) < 0.01, y                                    # GRAND TOTAL ties every year
    with Session() as s:
        assert s.scalar(select(func.count()).select_from(MaintenanceContract)) == 276
        blocks = dict(s.execute(select(MaintenanceContract.block, func.count()).group_by(MaintenanceContract.block)).all())
        assert blocks[MaintenanceBlock.NOT_LIVE_PAYING] == 1 and blocks[MaintenanceBlock.NOT_LIVE_NOT_PAYING] == 3
        assert blocks[MaintenanceBlock.SUBSCRIPTION_ONPREM] == 73
        zeros_2027 = s.scalar(select(func.count()).select_from(MaintenanceFee)
                              .where(MaintenanceFee.year == 2027, MaintenanceFee.amount == 0))
        assert zeros_2027 == 4                                              # "stops next year"
        epp = contract_by_ref(s, "Maintenance row 137")
        assert epp.parent is not None and epp.parent.customer.qb_number == 80083
        ao = contract_by_ref(s, "Maintenance row 3")                       # A.O. Smith, February anniversary
        assert ao.anniversary_month == 2
        assert metrics.fee_for(ao, 2027) == 29082.25
        p = metrics.maintenance_pricing(ao, TODAY)
        assert p.total_current_license == 119500 and p.original_maintenance == 10900
        assert {pu.kind for pu in ao.purchases} >= {PurchaseKind.ORIGINAL, PurchaseKind.PLANT}
        closed = contract_by_ref(s, "Maintenance row 285")                 # IGT, old closed account
        assert closed.block == MaintenanceBlock.CLOSED and closed.end_reason == MaintenanceEndReason.OTHER
    assert r.maintenance_invoices["matched"] >= 180


@needs_source
def test_schedule_matches_sheet(imported):
    Session, r = imported
    with Session() as s:
        ao = contract_by_ref(s, "Maintenance row 3")
        assert cents(metrics.maintenance_month_revenue(ao, 2026, 1)) == Decimal("2017.81")   # 2025-2026 fee / 12
        assert cents(metrics.maintenance_month_revenue(ao, 2026, 2)) == Decimal("2423.52")   # steps at the anniversary
        assert cents(metrics.maintenance_deferred(ao, 2025)) == Decimal("2017.81")
    for y in (2025, 2026):
        sheet, db, sd, dd = r.maintenance_revenue[y]
        assert abs(sheet - db) / sheet < 0.01, y                            # GL 4300 within 1 %


# ------------------------------------------------------------------------------------------------ workflows

@pytest.fixture
def mdb(blank):
    with blank() as s:
        c = Customer(qb_number=80500, name="PERP CO")
        s.add(c)
        s.commit()
    return blank


def test_new_perpetual_prorated_then_roll(mdb):
    out = run(mdb, ms.new_perpetual, 80500, LicenseProduct.PERPETUAL, D(2025, 3, 1), 100000, 10000, 0.22, 10, False,
              D(2025, 4, 1))
    assert "net 90,000.00" in out.lines[0] and "19,800.00" in out.lines[0]
    with mdb() as s:
        mc = s.scalar(select(MaintenanceContract))
        f = mc.fees[0]
        # anniversary October: go-live April 2025 is in contract year 2024-2025 (Oct 2024 - Sep 2025): 6 months
        assert f.year == 2025 and f.prorated and f.amount == 9900.0
        assert mc.block == MaintenanceBlock.PERPETUAL_LIVE
        cid = mc.id
    roll = run(mdb, ms.annual_roll, 2026, 0.04)
    assert roll.lines and "80500" in roll.lines[1]
    with mdb() as s:
        assert metrics.fee_for(s.get(MaintenanceContract, cid), 2026) == Decimal("20592.00")   # stub -> annual: 19,800 x 1.04
    run(mdb, ms.annual_roll, 2027, 0.04)
    with mdb() as s:
        assert metrics.fee_for(s.get(MaintenanceContract, cid), 2027) == Decimal("21415.68")    # 20,592 x 1.04
        assert s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "roll")) == 2
    with pytest.raises(services.ServiceError, match="Nothing to roll"):
        run(mdb, ms.annual_roll, 2027, 0.04)


def test_not_live_then_mark_live(mdb):
    run(mdb, ms.new_perpetual, 80500, LicenseProduct.PERPETUAL, D(2026, 1, 15), 50000, 0, 0.2, None, True)
    with mdb() as s:
        mc = s.scalar(select(MaintenanceContract))
        assert mc.block == MaintenanceBlock.NOT_LIVE_NOT_PAYING and not mc.fees
        cid = mc.id
    run(mdb, ms.mark_live, cid, D(2026, 7, 1))
    with mdb() as s:
        mc = s.get(MaintenanceContract, cid)
        assert mc.block == MaintenanceBlock.PERPETUAL_LIVE
        assert metrics.fee_for(mc, 2027) == 5000.0                         # calendar: Jul-Dec 2026 = 6/12 of 10,000


def _live(mdb, fee26=10000.0, fee27=None):
    with mdb() as s:
        c = s.scalar(select(Customer))
        mc = MaintenanceContract(customer=c, block=MaintenanceBlock.PERPETUAL_LIVE, anniversary_month=1,
                                 calendar_billing=True, go_live=True)
        mc.fees.append(MaintenanceFee(year=2026, amount=fee26, source=FeeSource.IMPORT))
        if fee27 is not None:
            mc.fees.append(MaintenanceFee(year=2027, amount=fee27, source=FeeSource.IMPORT))
        s.add(mc)
        s.commit()
        return mc.id


def test_purchase_and_decommission(mdb):
    cid = _live(mdb, 10000, 10400)
    run(mdb, ms.add_purchase, cid, PurchaseKind.PLANT, "2 plants", 40000, 8000, 2027, 2)
    with mdb() as s:
        mc = s.get(MaintenanceContract, cid)
        assert metrics.fee_for(mc, 2026) == 10000 and metrics.fee_for(mc, 2027) == 18400
        assert metrics.maintenance_pricing(mc, D(2026, 6, 1)).additional_license == 40000
    run(mdb, ms.add_purchase, cid, PurchaseKind.DECOMMISSION, "plant closed", 20000, 4000, 2027)
    with mdb() as s:
        assert metrics.fee_for(s.get(MaintenanceContract, cid), 2027) == 14400
    with pytest.raises(services.ServiceError):
        run(mdb, ms.add_purchase, cid, PurchaseKind.MODULE, "x", -1, 5, 2027)


def test_convert_and_terminate(mdb):
    cid = _live(mdb, 10000, 10400)
    with mdb() as s:
        c = s.scalar(select(Customer))
        sub = Subscription(customer=c, platform=Platform.CLOUD, opened_via=OpenedVia.CONVERSION)
        s.add(sub)
        s.commit()
        sid = sub.id
    out = run(mdb, ms.convert, cid, D(2026, 1, 1), sid, commit=False)               # preview: nothing saved
    assert "explicit 0 from 2026-2027" in out.lines[0]          # calendar billing: 2026-2027 = Jan-Dec 2026
    run(mdb, ms.convert, cid, D(2026, 1, 1), sid)
    with mdb() as s:
        mc = s.get(MaintenanceContract, cid)
        # calendar billing: 2026-2027 covers Jan-Dec 2026, starts on 01/01/2026 -> explicit 0 there
        assert metrics.fee_map(mc) == {2026: 10000.0, 2027: 0.0}
        assert mc.converted and mc.block == MaintenanceBlock.CONVERTED_CLOUD and mc.converted_subscription_id == sid
    with pytest.raises(services.ServiceError, match="already ended"):
        run(mdb, ms.terminate, cid, D(2026, 6, 1), MaintenanceEndReason.CHURN, None)
    cid2 = _live(mdb, 9000)
    run(mdb, ms.terminate, cid2, D(2026, 3, 15), MaintenanceEndReason.TERMINATED, "per email")
    with mdb() as s:
        mc = s.get(MaintenanceContract, cid2)
        # calendar billing: 03/15/2026 is inside 2026-2027 (Jan-Dec 2026), so 2027-2028 is the first unbilled year
        assert mc.block == MaintenanceBlock.CLOSED and metrics.fee_for(mc, 2028) == 0
        assert metrics.fee_for(mc, 2026) == 9000                                  # earlier years kept


def test_set_fee_blank_zero_and_invoice(mdb):
    cid = _live(mdb, 10000)
    run(mdb, ms.set_fee, cid, 2027, 10400.0, "agreed")
    run(mdb, ms.set_fee, cid, 2028, 0.0, "stops")
    run(mdb, ms.set_fee, cid, 2028, None, None)
    with mdb() as s:
        mc = s.get(MaintenanceContract, cid)
        assert metrics.fee_map(mc) == {2026: 10000.0, 2027: 10400.0}
    out = run(mdb, ms.record_invoice, cid, 2027, "24106", D(2025, 12, 1), 10500.0)
    assert out.warnings and "differs" in out.warnings[0]
    with pytest.raises(services.ServiceError, match="already recorded"):
        run(mdb, ms.record_invoice, cid, 2027, "24106", D(2025, 12, 1), 10400.0)
    with pytest.raises(services.ServiceError):
        run(mdb, ms.set_fee, cid, 2027, -5.0, None)


# ------------------------------------------------------------------------------------------------ reload safety

@needs_source
def test_maintenance_only_reload_keeps_subscriptions(imported_copy, tmp_path):
    from app.importer.cli import ImportRefused, run_maintenance_import
    from sqlalchemy.orm import sessionmaker
    eng = imported_copy()
    url = eng.url.render_as_string(hide_password=False)
    S = sessionmaker(bind=eng)
    with S() as s:
        s.add(AuditLog(at=dt.datetime.now(), actor="t", action="renew", entity="term", summary="user change"))
        s.commit()
        terms = s.scalar(select(func.count()).select_from(MaintenanceContract))
        audits = s.scalar(select(func.count()).select_from(AuditLog))
    r = run_maintenance_import(url=url, report_dir=tmp_path)
    with S() as s:
        assert s.scalar(select(func.count()).select_from(AuditLog)) == audits     # subscription changes kept
        assert s.scalar(select(func.count()).select_from(MaintenanceContract)) == terms
        s.add(AuditLog(at=dt.datetime.now(), actor="t", action="fee", entity="maintenance_contract", summary="x"))
        s.commit()
    with pytest.raises(ImportRefused, match="maintenance change"):
        run_maintenance_import(url=url, report_dir=tmp_path)
    assert all(abs(a - b) < 0.01 for a, b in r.maintenance_totals.values())


# ------------------------------------------------------------------------------------------------ pages

@needs_source
def test_pages(imported):
    Session, _ = imported
    main.SESSION_FACTORY, main.TODAY = Session, TODAY
    c = TestClient(main.app)
    try:
        page = c.get("/maintenance").text
        assert "GRAND TOTAL" in page and "3,251,918.03" in page
        assert c.get("/maintenance?year=2027&flag=stops_next").status_code == 200
        assert c.get("/maintenance?block=CLOSED&q=igt").status_code == 200
        with Session() as s:
            ao = contract_by_ref(s, "Maintenance row 3").id
        p = c.get(f"/maintenance/{ao}").text
        assert "2423.52" not in p and "2,424" in p and "Total current licence" in p
        for k in ("purchase", "decommission", "convert", "terminate", "live"):
            assert c.get(f"/maintenance/wf/{k}?contract={ao}").status_code == 200
        assert c.get("/maintenance/wf/new?customer=80082").status_code == 200
        roll = c.get("/maintenance/wf/roll?year=2027&rate=4").text
        assert "Annual roll" in roll
        prev = c.post("/maintenance/wf/terminate", data={"contract": str(ao), "on": "2026-12-31",
                                                         "reason": "CHURN", "note": "test"})
        assert prev.status_code == 200 and "Confirm and save" in prev.text
        with Session() as s:
            assert s.get(MaintenanceContract, ao).ended_on is None                  # preview did not save
        assert "/maintenance/" in c.get("/customer/80082").text
    finally:
        main.TODAY = None
