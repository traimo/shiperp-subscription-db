"""Full import of the real workbook copy; checks counts, lineage cases from the spec, and reconciliation."""
from collections import Counter

from sqlalchemy import func, select

from decimal import Decimal

from app import metrics
from app.money import cents
from app.models import (Customer, NonRecurringFee, Term, TermKind, TermSupersession, Termination,
                        TerminationReason)
from tests.conftest import D, needs_source

pytestmark = needs_source


def cust(s, qb):
    return next(c for c in metrics.load_customers(s) if c.qb_number == qb)


def terms_by_ref(c):
    return {t.source_ref: t for sub in c.subscriptions for t in sub.terms}


def test_counts(imported):
    _, r = imported
    assert r.sheet_counts["On-Prem"] == {"A": 80, "B": 17, "C": 33, "D": 29, "E": 6}
    assert r.sheet_counts["Cloud"] == {"A": 27, "B": 3, "C": 5, "D": 5, "E": 6}
    k = r.db_counts["terms_by_kind"]
    assert k["INITIAL"] == 107                       # every A row
    assert k["ADD_ON"] == 37                         # C rows minus the one non-recurring fee
    assert k["RENEWAL"] + k.get("AMENDMENT", 0) == 33   # D rows minus the 'pending' row rejected
    assert k["CONVERSION"] == 19                     # B rows minus the negative adjustment folded into A45
    assert r.db_counts["terminations"] == 12 and r.db_counts["non_recurring_fees"] == 1
    rejected = [n for n in r.notes if n.severity == "rejected"]
    assert [n.ref for n in rejected] == ["On-Prem D196"]          # 'pending' period, no signed date


def test_lineage_80072_chain(imported):
    Session, _ = imported
    with Session() as s:
        t = terms_by_ref(cust(s, 80072))
        a, d1, d2 = t["On-Prem A9"], t["On-Prem D176"], t["On-Prem D202"]
        assert [x.new_term_id for x in a.superseded_by] == [d1.id]
        assert [x.new_term_id for x in d1.superseded_by] == [d2.id]       # formula pointed elsewhere; dates used
        assert cents(metrics.term_arr(a)) == Decimal("61762.67")
        assert metrics.term_year_arr(a, 2022) > 0 and metrics.term_year_arr(a, 2023) == 0


def test_lineage_80197_rollup(imported):
    Session, _ = imported
    with Session() as s:
        t = terms_by_ref(cust(s, 80197))
        d = t["On-Prem D193"]
        olds = {x.old_term.source_ref for x in d.supersedes}
        assert olds == {"On-Prem A32", "On-Prem C142", "On-Prem C149", "On-Prem C155"}
        assert t["On-Prem C142"].kind == TermKind.ADD_ON


def test_termination_80168_and_fee_80148(imported):
    Session, _ = imported
    with Session() as s:
        c = cust(s, 80168)
        x = c.subscriptions[0].terminations[0]
        assert x.cancelled_on == D(2026, 6, 30)            # "06/31/2026" clamped
        assert x.reason in (TerminationReason.FINAL_PERIOD, TerminationReason.CANCELLED)
        assert metrics.subscription_status(c.subscriptions[0], D(2026, 11, 12)) == "CHURNED"
        fee = s.scalar(select(NonRecurringFee))
        assert fee.customer.qb_number == 80148 and fee.amount > 0


def test_negative_adjustment_folded_80218(imported):
    Session, _ = imported
    with Session() as s:
        t = terms_by_ref(cust(s, 80218))
        a45 = t["On-Prem A45"]
        assert a45.tcv == 108000 and round(metrics.term_arr(a45), 2) == 21600 and a45.is_revised
        assert "On-Prem B122" not in t
        assert {x.new_term.source_ref for x in a45.superseded_by} == {"On-Prem B123"}


def test_no_negative_money_and_constraints(imported):
    Session, _ = imported
    with Session() as s:
        assert s.scalar(select(func.count()).select_from(Term).where(Term.tcv < 0)) == 0
        assert s.scalar(select(func.count()).select_from(Term).where(Term.end_date < Term.start_date)) == 0
        assert s.scalar(select(func.count()).select_from(Customer)) >= 259
        assert s.scalar(select(func.count()).select_from(TermSupersession)) > 40
        assert s.scalar(select(func.count()).select_from(Termination)) == 12


def test_reconciliation(imported):
    _, r = imported
    tot = r.reconciliation_totals
    for p in ("On-Prem", "Cloud"):
        for y in (2020, 2021):
            sheet, db = tot[p][y]
            assert abs(sheet - db) < 1
    past = [x for x in r.reconciliation if not x["future"]]
    assert len(past) <= 30 and all(x["why"] for x in past)
    reasons = Counter(x["why"].split(":")[0] for x in past)
    assert reasons["renewal transition year"] >= 1
    # current-year total within 3% of the sheet once blank cells / transitions are explained
    for p in ("On-Prem", "Cloud"):
        sheet, db = tot[p][2026]
        assert abs(db - sheet) / sheet < 0.07


def test_maintenance_reconciles_and_company_arr(imported):
    Session, r = imported
    if not r.maintenance_totals:
        import pytest
        pytest.skip("no maintenance workbook in data/source")
    for y, (sheet, db) in r.maintenance_totals.items():
        assert abs(sheet - db) < 0.01, y                       # every year ties to GRAND TOTAL
    assert round(r.maintenance_totals[2026][1], 2) == 3251918.03
    with Session() as s:
        cs = metrics.load_customers(s)
        arr = metrics.arr_by_year(cs, [2025, 2026])
        assert 9.8e6 < arr["Total"][2026] < 10.0e6            # accounting's ~10.2M minus converted prepaid maintenance
        assert arr["Total"][2026] == arr["Subscription"][2026] + arr["Maintenance"][2026]
        assert 0 < arr["Converted maintenance"][2026] < 0.4e6
        assert (arr["Maintenance"][2026] + arr["Converted maintenance"][2026]
                == metrics.maintenance_total(cs, 2026))            # ties back to the workbook GRAND TOTAL
        conv = next(c for c in cs if c.qb_number == 80095)     # KTM: converted perpetual -> subscription
        assert metrics.is_converted(conv) and conv.maintenance_contracts


def test_customer_names_from_contracts(imported):
    Session, r = imported
    from app.importer.mapper import names_differ
    assert names_differ("UNICOR, FEDERAL PRISON INDUSTRIES, INC.", "UNION PACIFIC RAILROAD COMPANY")
    assert not names_differ("Kyocera International, Inc.", "KYOCERA INTERNATIONAL, INC.")
    with Session() as s:
        names = {c.qb_number: c for c in s.scalars(select(Customer))}
        assert names[80212].name == "UNION PACIFIC RAILROAD COMPANY"
        assert "UNICOR" in names[80212].notes
        # 80220: accounting aligned the contract rows with the Masterlist in the 11/13 20:46 workbook
        assert names[80220].name == "HOLLISTER INCORPORATED"
        assert names[80072].name == "KYOCERA INTERNATIONAL, INC." and names[80072].notes is None
    flagged = sorted(n.customer for n in r.notes if n.message.startswith("Masterlist names"))
    assert flagged == [80052, 80212]


def test_2026_monthly_billing_layout_imported(imported):
    """2026 invoices come from 'Jan to June 2026' + month columns; each row's invoices add up to Total Invoice."""
    from app.models import Invoice
    Session, r = imported
    with Session() as s:
        inv = list(s.scalars(select(Invoice).where(Invoice.billing_year == 2026)))
        assert len(inv) > 50 and all(i.amount > 0 for i in inv)
        assert all(i.invoice_date is None or i.invoice_date.year == 2026 for i in inv)
        refs = Counter(i.reference.split()[0] for i in inv)
        assert refs["Jan-Jun"] > 30 and sum(v for k, v in refs.items() if k != "Jan-Jun") > 5
        a12 = [i for i in terms_by_ref(cust(s, s.scalar(select(Term).where(Term.source_ref == "Cloud A12"))
                                            .subscription.customer.qb_number))["Cloud A12"].invoices
               if i.billing_year == 2026]
        assert [(i.invoice_date, i.amount) for i in a12] == [(D(2026, 1, 15), Decimal("15000.00"))]
    assert not [n for n in r.notes if "Total Invoice is" in n.message]          # every row ties to Total Invoice


def test_perpetual_licence_flag_from_maintenance_blocks(imported):
    Session, _ = imported
    with Session() as s:
        n = s.scalar(select(func.count()).select_from(Customer).where(Customer.has_perpetual_license))
        assert n > 150                                       # perpetual, not-live, converted and closed blocks
        assert cust(s, 80076).has_perpetual_license          # converted perpetual customer

