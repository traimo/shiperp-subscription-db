"""A user adds, renames or duplicates a workbook column, then clicks Refresh data.

The workbooks are changed in memory only (openpyxl insert_cols on both the formula and the cached-value copy); the
files in data/source are never written.
"""
from pathlib import Path

import openpyxl
import pytest
from sqlalchemy import func, select

from app import config, refresh
from app.importer import cli
from app.importer.columns import ColumnProblem, check_register, check_subscription
from app.importer.maintenance import read_register
from app.importer.sheet import reader
from app.importer.sheet.headers import HeaderIssues, classify, norm
from app.importer.sheet.model import Workbook
from app.models import Customer
from tests.conftest import needs_source
from tests.test_refresh import TODAY, env  # noqa: F401  (fixture)


# ------------------------------------------------------------------------------------------------ helpers

def _subscription_path() -> Path:
    return Path(config.source_workbook())


def _maintenance_path() -> Path:
    return Path(config.maintenance_workbook())


def _read(wf, wv, path) -> Workbook:
    tabs = {}
    for name in wf.sheetnames:
        key = reader._tab_key(name)
        if key and key not in tabs:
            tabs[key] = reader.read_tab(key, wf[name], wv[name])
    return Workbook(str(path), path.stat().st_mtime, tabs, reader.read_masterlist(wv))


def _header_rows(ws):
    return [r for r in range(1, ws.max_row + 1) if norm(ws.cell(r, 1).value).startswith("counter")]


def _insert(wf, wv, header, after=None, before=None):
    """Insert a column on every ARR tab, next to the first header that starts with one of `after` / `before`
    ('|'-separated), and write `header` into every header row."""
    for name in wf.sheetnames:
        if not reader._tab_key(name):
            continue
        rows = _header_rows(wf[name])
        anchor = (after or before).split("|")
        col = next(c.column for c in wf[name][rows[0]]
                   if c.value and any(norm(c.value).startswith(a) for a in anchor))
        at = col + 1 if after else col
        for ws in (wf[name], wv[name]):
            ws.insert_cols(at)
            for r in rows:
                ws.cell(r, at).value = header


def _rename(wf, wv, starts, new):
    for name in wf.sheetnames:
        if not reader._tab_key(name):
            continue
        for ws in (wf[name], wv[name]):
            for r in _header_rows(ws):
                for c in ws[r]:
                    if c.value and norm(c.value).startswith(starts):
                        c.value = new


def _modified(change) -> Workbook:
    path = _subscription_path()
    wf, wv = openpyxl.load_workbook(path), openpyxl.load_workbook(path, data_only=True)
    change(wf, wv)
    return _read(wf, wv, path)


def _contracts(wb: Workbook):
    return sorted((c.tab, c.section, c.values.get("customer_number"), str(c.values.get("period")),
                   str(c.values.get("customer_name")), c.values.get("arr"), c.values.get("status"))
                  for t in wb.tabs.values() for c in t.contracts)


# ------------------------------------------------------------------------------------------------ header matching

def test_classify_reports_unknown_and_look_alike_headers():
    issues = HeaderIssues()
    cols = classify({1: "Counter - Grand Total", 2: "Customer Number", 3: "Customer Name (old)", 4: "Customer Name",
                     5: "Subscription Period", 6: "Notes", 7: "Status", 8: "Status",
                     9: "ARR - Annual Recurring Revenue"}, issues)
    assert cols["customer_name"] == 3                         # the left one wins ...
    assert issues.checked_duplicates() == {"customer_name": [3, 4], "status": [7, 8]}   # ... so it is reported
    assert issues.unknown == [(6, "Notes")]


def test_classify_ignores_repeats_in_the_revenue_schedule():
    issues = HeaderIssues()
    classify({1: "Customer Number", 2: "Review by Tin", 3: "Q1-2020", 4: "Something new", 5: "Q1-2020"}, issues)
    assert issues.checked_duplicates() == {} and issues.unknown == []


# ------------------------------------------------------------------------------------------------ subscription workbook

@needs_source
def test_current_workbooks_pass_the_check():
    sub =check_subscription(reader.read_workbook(_subscription_path()))
    assert sub.errors == [] and sub.new_columns == []
    maint = check_register(read_register(_maintenance_path()))
    assert maint.errors == [] and maint.new_columns == []


@needs_source
def test_new_column_is_listed_and_changes_nothing():
    base = _contracts(reader.read_workbook(_subscription_path()))
    wb = _modified(lambda wf, wv: _insert(wf, wv, "Notes", after="customer name|contract name"))
    assert _contracts(wb) == base
    check = check_subscription(wb)
    assert check.errors == []
    assert len(check.new_columns) == 2 and all("'Notes'" in c for c in check.new_columns)   # one per ARR tab


@needs_source
@pytest.mark.parametrize("header, before", [("Customer Name (old)", "customer name|contract name"),
                                            ("Status", "status")])
def test_look_alike_column_on_the_left_is_refused(header, before):
    wb = _modified(lambda wf, wv: _insert(wf, wv, header, before=before))
    check = check_subscription(wb)
    assert check.errors and all("both look like" in e for e in check.errors)
    with pytest.raises(ColumnProblem, match="Rename or remove one"):
        check.raise_if_errors()


@needs_source
def test_renamed_customer_number_is_refused():
    wb = _modified(lambda wf, wv: _rename(wf, wv, "customer number", "Cust #"))
    errors = check_subscription(wb).errors
    assert errors and any("'Customer Number'" in e and "renamed or deleted" in e for e in errors)


@needs_source
def test_refresh_refuses_before_the_database_changes(env, monkeypatch):  # noqa: F811
    S, eng, paths = env
    wb = _modified(lambda wf, wv: _insert(wf, wv, "Customer Name (old)", before="customer name|contract name"))
    monkeypatch.setattr(cli, "read_workbook", lambda path: wb)
    with S() as s:
        names_before = sorted(s.scalars(select(Customer.name)))
    before = refresh.figures(S, TODAY)
    with pytest.raises(refresh.RefreshError, match="both look like 'Customer Name'"):
        refresh.run(S, "tester", TODAY, pull_from_sharepoint=False, paths=paths)
    assert refresh.figures(S, TODAY) == before
    with S() as s:
        assert sorted(s.scalars(select(Customer.name))) == names_before
        assert s.scalar(select(func.count()).select_from(Customer)) == len(names_before)


@needs_source
def test_refresh_lists_new_columns(env, monkeypatch):  # noqa: F811
    S, eng, paths = env
    # Inserted right of the ARR / previous-ARR columns: openpyxl does not rewrite formula references the way Excel
    # does, and renewals are linked through the "=AE12" previous-ARR formula, so an insert left of ARR would break
    # those links in this test only.
    plain = refresh.run(S, "tester", TODAY, pull_from_sharepoint=False, paths=paths)   # the workbook as it is
    assert plain["columns"] == []
    wb = _modified(lambda wf, wv: _insert(wf, wv, "Notes", before="review by tin"))
    monkeypatch.setattr(cli, "read_workbook", lambda path: wb)
    r = refresh.run(S, "tester", TODAY, pull_from_sharepoint=False, paths=paths)
    assert len(r["columns"]) == 2 and all("'Notes'" in c for c in r["columns"])
    assert r["after"] == plain["after"]


# ------------------------------------------------------------------------------------------------ maintenance register

def _maintenance_copy(tmp_path, change) -> Path:
    wb = openpyxl.load_workbook(_maintenance_path(), data_only=True)   # the register is read as values only
    ws = wb["Maintenance-Adj Advance Billing"]
    change(ws)
    out = tmp_path / "maintenance.xlsx"
    wb.save(out)
    return out


def _maint_col(ws, starts):
    hdr = next(r for r in range(1, 11) if sum(1 for c in ws[r] if c.value and "-" in str(c.value)
                                              and str(c.value).strip()[:2] == "20") >= 3)
    col = next(c.column for c in ws[hdr] if c.value and str(c.value).strip().lower().startswith(starts))
    return hdr, col


def _maint_insert(header, before):
    def change(ws):
        hdr, col = _maint_col(ws, before)
        ws.insert_cols(col)
        ws.cell(hdr, col).value = header
    return change


@needs_source
def test_maintenance_new_column_is_listed(tmp_path):
    check = check_register(read_register(_maintenance_copy(tmp_path, _maint_insert("Notes", "billing anniversary"))))
    assert check.errors == [] and len(check.new_columns) == 1 and "'Notes'" in check.new_columns[0]


@needs_source
def test_maintenance_extra_go_live_date_is_refused(tmp_path):
    check = check_register(read_register(_maintenance_copy(tmp_path, _maint_insert("Go Live Date", "product"))))
    assert any("4 columns named 'go live date'" in e for e in check.errors)


@needs_source
def test_maintenance_renamed_customer_name_is_refused(tmp_path):
    def change(ws):
        hdr, col = _maint_col(ws, "customer name")
        ws.cell(hdr, col).value = "Client"
    check = check_register(read_register(_maintenance_copy(tmp_path, change)))
    assert any("no 'customer name' column" in e for e in check.errors)


@needs_source
def test_maintenance_only_import_refuses_before_deleting(tmp_path, env):  # noqa: F811
    S, eng, paths = env
    bad = _maintenance_copy(tmp_path, _maint_insert("Go Live Date", "product"))
    before = refresh.figures(S, TODAY)
    with pytest.raises(ColumnProblem):
        cli.run_maintenance_import(bad, url=str(eng.url.render_as_string(hide_password=False)), force=True,
                                   report_dir=tmp_path)
    assert refresh.figures(S, TODAY) == before
