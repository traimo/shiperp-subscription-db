"""Deferred Billing: TCV less invoices to date, at every month-end."""
import datetime as dt
from decimal import Decimal

from sqlalchemy import select

from app import deferred, metrics
from app.config import source_workbook
from app.importer.mapper import ref
from app.importer.sheet.reader import read_workbook
from app.models import Customer, Invoice, Platform, Subscription, Term
from tests.conftest import TODAY, needs_source


def d(y, m, day):
    return dt.date(y, m, day)


def term(tcv, start, invoices=(), end=None):
    t = Term(tcv=Decimal(tcv), start_date=start, end_date=end or start.replace(year=start.year + 3))
    t.invoices = [Invoice(billing_year=y, invoice_date=day, amount=Decimal(a)) for y, day, a in invoices]
    return t


def customer(*terms, qb=80001, name="ACME", platform=Platform.ON_PREM):
    c = Customer(qb_number=qb, name=name)
    c.id = qb
    c.subscriptions = [Subscription(platform=platform, terms=list(terms))]
    return c


ALLISON = [(2023, d(2023, 11, 30), "28000"), (2024, d(2024, 9, 30), "28000"), (2025, d(2025, 9, 15), "28000")]


def bal(months, balances, y, m):
    return balances[months.index(deferred.month_end(d(y, m, 1)))]


def test_month_ends_cross_years_and_february():
    assert deferred.month_ends(d(2023, 11, 5), d(2024, 3, 1)) == [
        d(2023, 11, 30), d(2023, 12, 31), d(2024, 1, 31), d(2024, 2, 29), d(2024, 3, 31)]
    assert deferred.month_ends(d(2025, 2, 1), d(2025, 2, 28)) == [d(2025, 2, 28)]


def test_allison_example_steps_down_at_each_invoice():
    t = term("84000", d(2023, 10, 1), ALLISON)
    months = deferred.month_ends(d(2023, 10, 1), d(2025, 12, 31))
    b = deferred.term_balances(t, months)
    assert bal(months, b, 2023, 10) == Decimal("84000.00")
    assert bal(months, b, 2023, 11) == Decimal("56000.00")
    assert bal(months, b, 2024, 8) == Decimal("56000.00")
    assert bal(months, b, 2024, 9) == Decimal("28000.00")
    assert bal(months, b, 2025, 8) == Decimal("28000.00")
    assert bal(months, b, 2025, 9) == Decimal("0.00")
    assert bal(months, b, 2025, 12) == Decimal("0.00")


def test_term_counts_from_first_invoice_when_billed_before_start():
    t = term("1200", d(2024, 3, 1), [(2024, d(2024, 1, 15), "400")])
    months = deferred.month_ends(d(2023, 12, 1), d(2024, 3, 31))
    assert deferred.term_balances(t, months) == [None, Decimal("800.00"), Decimal("800.00"), Decimal("800.00")]


def test_renewal_adds_nothing_before_it_counts():
    c = customer(term("84000", d(2023, 10, 1), ALLISON),
                 term("87360", d(2026, 10, 1), [(2026, d(2026, 11, 17), "29120")]))
    months, rows, cur = deferred.deferred_table([c], d(2026, 11, 12))
    (r,) = rows
    assert months[0] == d(2023, 10, 31) and months[-1] == d(2026, 11, 30) and months[cur] == d(2026, 11, 30)
    assert r.balances[0] == Decimal("84000.00")
    assert bal(months, r.balances, 2025, 9) == 0
    assert bal(months, r.balances, 2026, 9) == 0                 # renewal not started yet
    assert bal(months, r.balances, 2026, 10) == Decimal("87360.00")
    assert bal(months, r.balances, 2026, 11) == Decimal("58240.00")
    assert r.current == Decimal("58240.00") and r.tcv == Decimal("171360")
    assert len(r.terms) == 2 and r.platforms == [Platform.ON_PREM.value]
    assert bal(months, r.invoiced, 2023, 10) is None
    assert bal(months, r.invoiced, 2023, 11) == Decimal("28000")
    assert bal(months, r.invoiced, 2026, 11) == Decimal("29120")
    assert sum(x or 0 for x in r.invoiced) == Decimal("113120")


def test_two_invoices_in_one_month_add_up_and_totals_sum_both():
    t = term("1000", d(2024, 1, 1), [(2024, d(2024, 1, 5), "100"), (2024, d(2024, 1, 20), "150")])
    c2 = customer(term("500", d(2024, 1, 1), [(2024, d(2024, 1, 31), "50")]), qb=80002)
    months, rows, _ = deferred.deferred_table([customer(t), c2], d(2024, 2, 10))
    assert rows[0].invoiced == [Decimal("250"), None] and rows[0].balances == [Decimal("750.00")] * 2
    assert deferred.invoiced_totals(rows, len(months)) == [Decimal("300"), 0]
    assert deferred.column_totals(rows, len(months)) == [Decimal("1200.00")] * 2


def test_undated_invoice_counts_at_year_end_of_its_billing_year():
    t = term("1000", d(2022, 1, 1), [(2022, None, "250")])
    months = deferred.month_ends(d(2022, 11, 1), d(2023, 1, 31))
    assert deferred.term_balances(t, months) == [Decimal("1000.00"), Decimal("750.00"), Decimal("750.00")]


def test_over_billing_shows_negative_balance():
    t = term("100", d(2024, 1, 1), [(2024, d(2024, 2, 1), "150")])
    assert deferred.term_balances(t, [d(2024, 2, 29)]) == [Decimal("-50.00")]


def test_columns_run_to_last_invoice_month_and_platform_filter():
    c1 = customer(term("100", d(2024, 1, 1), [(2027, d(2027, 2, 1), "100")]), qb=80001)
    c2 = customer(term("500", d(2023, 6, 1)), qb=80002, platform=Platform.CLOUD)
    months, rows, cur = deferred.deferred_table([c1, c2], d(2026, 11, 12))
    assert months[0] == d(2023, 6, 30) and months[-1] == d(2027, 2, 28) and months[cur] == d(2026, 11, 30)
    assert [r.customer.qb_number for r in rows] == [80001, 80002]
    totals = deferred.column_totals(rows, len(months))
    assert rows[0].terms[0].current == Decimal("100.00")                        # today's month, not the last column
    assert totals[cur] == Decimal("600.00") and totals[-1] == Decimal("500.00")
    assert rows[0].balances[0] is None and totals[0] == Decimal("500.00")      # 80001 not counting yet
    months, rows, _ = deferred.deferred_table([c1, c2], d(2026, 11, 12), Platform.CLOUD.value)
    assert [r.customer.qb_number for r in rows] == [80002] and months[0] == d(2023, 6, 30)


def test_terms_without_tcv_or_invoices_are_skipped():
    assert deferred.deferred_table([customer(term("0", d(2024, 1, 1)))], d(2026, 11, 12)) == ([], [], None)


# --- against the imported workbook ---------------------------------------------------------------------------------

def _allison(s):
    months, rows, _ = deferred.deferred_table(metrics.load_customers_with_invoices(s), TODAY)
    return next(r for r in rows if r.customer.name.upper().startswith("ALLISON TRANSMISSION")), months


@needs_source
def test_allison_from_the_workbook(imported):
    Session, _ = imported
    with Session() as s:
        r, months = _allison(s)
        first = next(tr for tr in r.terms if tr.term.tcv == Decimal("84000.00"))
        assert bal(months, first.balances, 2023, 11) == Decimal("56000.00")
        assert bal(months, first.balances, 2024, 9) == Decimal("28000.00")
        assert bal(months, first.balances, 2025, 9) == Decimal("0.00")
        renewal = next(tr for tr in r.terms if tr.term.tcv == Decimal("87360.00"))
        assert bal(months, renewal.balances, 2026, 11) == Decimal("58240.00")      # the workbook's BW


@needs_source
def test_year_end_balances_match_the_workbook_deferred_billing_columns(imported):
    """The workbook's 'GL#1215 Deferred Billing' columns hold the same balance. Report mismatches (the workbook's own
    errors and folded section-E adjustments can differ) but most rows must agree."""
    Session, _ = imported
    wb = read_workbook(source_workbook())
    with Session() as s:
        terms = {t.source_ref: t for t in s.scalars(select(Term))}
        for t in terms.values():
            t.invoices                                       # load
        checked, bad = 0, []
        for tab in wb.tabs.values():
            for c in tab.contracts:
                t = terms.get(ref(c))
                if t is None:
                    continue
                for k, v in c.values.items():
                    if not k.startswith("deferred_billing_") or not isinstance(v, (int, float)):
                        continue
                    y = int(k.rsplit("_", 1)[1])
                    if not t.invoices or min(deferred.invoice_day(i) for i in t.invoices).year > y:
                        continue                             # not billed yet that year: the sheet leaves it blank/TCV
                    mine = deferred.term_balances(t, [d(y, 12, 31)])[0]
                    checked += 1
                    if mine is None or abs(mine - Decimal(repr(v))) > Decimal("0.01"):
                        bad.append((ref(c), y, mine, v))
    print(f"\n{checked} year-end balances checked, {len(bad)} differ from the workbook:", *bad[:40], sep="\n  ")
    assert checked > 100
    assert len(bad) <= checked * 0.1, bad[:40]


# --- Excel download --------------------------------------------------------------------------------------------------

def test_excel_export_has_balance_and_invoiced_per_month():
    import io

    import openpyxl

    from app import deferred_export
    c = customer(term("84000", d(2023, 10, 1), ALLISON), name="=ALLISON")
    months, rows, cur = deferred.deferred_table([c], d(2025, 11, 12))
    n = len(months)
    body = deferred_export.build(months, rows, deferred.column_totals(rows, n), deferred.invoiced_totals(rows, n),
                                 cur, d(2025, 11, 12))
    ws = openpyxl.load_workbook(io.BytesIO(body))["Deferred Billing"]
    head, sub = [x.value for x in ws[4]], [x.value for x in ws[5]]
    assert head[:8] == ["Customer #", "Customer", "Contract", "Start", "End", "Platform", "TCV",
                        "Current balance 11/2025"]
    oct23 = head.index(dt.datetime(2023, 10, 31))
    nov23 = head.index(dt.datetime(2023, 11, 30))
    assert nov23 == oct23 + 2 and sub[nov23:nov23 + 2] == ["Balance", "Invoiced"]
    assert ws.max_column == 8 + 2 * n
    cust_row, term_row, total_row = ws[6], ws[7], ws[8]
    assert cust_row[1].value == "=ALLISON" and cust_row[1].data_type == "s"      # a name is never a formula
    assert cust_row[6].value == 84000
    assert (cust_row[oct23].value, cust_row[oct23 + 1].value) == (84000, None)
    assert (cust_row[nov23].value, cust_row[nov23 + 1].value) == (56000, 28000)
    assert term_row[2].value == "Term None" and term_row[nov23 + 1].value == 28000   # unsaved term: no source row
    assert ws.row_dimensions[7].outline_level == 1
    assert total_row[1].value == "Total"
    assert (total_row[nov23].value, total_row[nov23 + 1].value) == (56000, 28000)
    assert total_row[oct23 + 1].value == 0


@needs_source
def test_excel_download_route(imported):
    import io

    import openpyxl
    from fastapi.testclient import TestClient

    from app import main
    Session, _ = imported
    main.SESSION_FACTORY, main.TODAY = Session, TODAY
    try:
        r = TestClient(main.app).get("/deferred-billing.xlsx?q=allison")
        page = TestClient(main.app).get("/deferred-billing?q=allison")
    finally:
        main.TODAY = None
    assert r.status_code == 200 and "Deferred Billing 2026-11-12.xlsx" in r.headers["content-disposition"]
    assert 'href="/deferred-billing.xlsx?q=allison"' in page.text
    ws = openpyxl.load_workbook(io.BytesIO(r.content))["Deferred Billing"]
    names = {row[1].value for row in ws.iter_rows(min_row=6)}
    assert any(n and n.startswith("ALLISON TRANSMISSION") for n in names) and "Total" in names
    assert "search 'allison'" in ws["A2"].value
    assert "Invoiced" in page.text and ws["J5"].value == "Invoiced"
