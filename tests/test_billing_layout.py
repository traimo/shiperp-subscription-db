"""Billing columns of the subscription sheet: the classic per-year layout and the 2026 monthly layout
(Date Invoiced | Jan to June 2026 | Jul..Dec month-ends | Total Invoice | GL#1215 Deferred Billing)."""
import datetime as dt

from app.importer.sheet import rules
from app.importer.sheet.headers import classify, label


def _cols(*headers):
    return {i + 1: h for i, h in enumerate(headers)}


def test_classic_and_monthly_layouts_side_by_side():
    months = [dt.datetime(2026, m, 1) + dt.timedelta(days=-1) for m in range(8, 13)] + [dt.datetime(2026, 12, 31)]
    cm = classify(_cols("Date Invoiced", "Amount", "12/31/2025-GL#1215.3 Deferred Billing",
                        "Date Invoiced", "Jan to June 2026", *months, "Total Invoice", "GL#1215.3 Deferred Billing",
                        "Recent Invoice"))
    assert cm["bill_date_2025"] == 1 and cm["bill_amount_2025"] == 2 and cm["deferred_billing_2025"] == 3
    assert cm["bill_date_2026"] == 4 and cm["bill_amount_2026"] == 5
    assert [cm[f"bill_month_2026_{m:02d}"] for m in range(7, 13)] == [6, 7, 8, 9, 10, 11]
    assert cm["bill_total_2026"] == 12 and cm["deferred_billing_2026"] == 13 and cm["recent_invoice"] == 14
    assert not any(k.startswith("rev_month") for k in cm)          # month headers are billing, not revenue
    assert label("bill_month_2026_07") == "Invoiced Jul 2026" and label("bill_total_2026") == "Total Invoice 2026"


def test_revenue_schedule_months_still_detected_after_billing():
    cm = classify(_cols("Date Invoiced", "Jan to June 2026", dt.datetime(2026, 7, 31), "Total Invoice",
                        "GL#1215.3 Deferred Billing", "GL#2320 Deferred Revenue,Beg", dt.datetime(2026, 1, 31)))
    assert cm["bill_month_2026_07"] == 3 and cm["rev_month_2026_01"] == 7


def test_parse_dates_lists_typos_and_year_filter():
    assert rules.parse_dates("1/5/2026,5/1/2026", 2026) == ([dt.date(2026, 1, 5), dt.date(2026, 5, 1)], [])
    dates, problems = rules.parse_dates("1/1/2026, 4/1/2026, 7/8/206, 8/1/2026", 2026)
    assert dates == [dt.date(2026, 1, 1), dt.date(2026, 4, 1), dt.date(2026, 8, 1)]
    assert problems and "7/8/206" in problems[0]
    assert rules.parse_dates("2/28/26,7/15/26", 2026)[0] == [dt.date(2026, 2, 28), dt.date(2026, 7, 15)]
    assert rules.parse_dates(dt.datetime(2026, 1, 15), 2026) == ([dt.date(2026, 1, 15)], [])
    assert rules.parse_dates("see OP $170,000", 2026) == ([], [])
    assert rules.parse_dates(None) == ([], [])
