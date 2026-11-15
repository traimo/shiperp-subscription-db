"""Spreadsheet check: rules on small in-memory rows, accepting exceptions, and a full run on the real workbooks
(read-only: the real database, import report and source files must not change)."""
import datetime as dt
import hashlib
import re
import shutil

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app import checker, config, main
from app.checker import Issue, accept, maintenance_rules, subscription_rules
from app.importer import maintenance as mimp
from app.importer.sheet.model import ContractRow, Section, Tab
from app.models import MaintenanceBlock
from tests.conftest import needs_source

COLS = {"customer_number": 2, "customer_name": 3, "period": 4, "renewal_date": 5, "term_years": 6, "tcv_total": 7,
        "arr": 8, "monthly": 9, "status": 10, "renewal_status": 11, "new_existing": 12, "hybrid_class": 13,
        "license_category": 14, "chk_sf": 15, "counter_year": 1, "reseller": 16, "payment_terms": 17}


def _tab(*rows) -> Tab:
    t = Tab("onprem", "On Prem_License ARR&CARR", "", "", 1, dict(COLS), {}, 20)
    t.sections["A"] = Section("A", "A. Name", 2, 3, 100, dict(COLS), {k: k for k in COLS})
    for i, v in enumerate(rows, start=10):
        base = {"customer_number": 80001, "customer_name": "ACME", "period": "01/01/2024-12/31/2026",
                "renewal_date": dt.datetime(2027, 1, 1), "term_years": 3, "tcv_total": 36000, "arr": 12000,
                "monthly": 1000, "status": "Active", "renewal_status": "Active", "new_existing": "New Account",
                "license_category": "On Premise", "counter_year": 1}
        base.update(v)
        t.contracts.append(ContractRow("onprem", "A", i, base, {}))
    return t


def _rules(tab, code):
    return [i for i in subscription_rules.check(type("W", (), {"tabs": {"onprem": tab}})()) if i.rule == code]


def test_clean_row_has_no_issues():
    assert subscription_rules.check(type("W", (), {"tabs": {"onprem": _tab({})}})()) == []


@pytest.mark.parametrize("change, rule, cell, expected", [
    ({"renewal_date": dt.datetime(2026, 12, 31)}, "S04", "E10", "01/01/2027"),
    ({"term_years": 5}, "S05", "F10", "3"),
    ({"arr": 15000, "monthly": 1250}, "S06", "H10", "12,000.00"),
    ({"monthly": 900}, "S07", "I10", "1,000.00"),
    ({"period": "06/31/2026-12/31/2026"}, "S03", "D10", "MM/DD/YYYY-MM/DD/YYYY"),
    ({"renewal_status": "Renewed"}, "S17", "J10", "Closed"),
    ({"new_existing": "new account - us"}, "S10", "L10", "New Account"),
    ({"hybrid_class": "On-Premise/Cloud"}, "S14", "M10", "On Prem/Cloud"),
    ({"chk_sf": "mismatched"}, "S18", "O10", "Yes / done"),
    ({"counter_year": 2}, "S19", "A10", "1"),
    ({"reseller": "nia"}, "S16", "P10", "n/a"),
    ({"payment_terms": "Annua1"}, "S15", "Q10", "Annual"),
])
def test_subscription_rules(change, rule, cell, expected):
    found = _rules(_tab(change), rule)
    assert len(found) == 1, found
    i = found[0]
    assert i.cell == cell and i.expected == expected and i.row == 10 and i.customer == 80001
    assert i.ref == f"'On Prem_License ARR&CARR'!{cell}"


def test_arr_zero_is_info_and_cancel_date_is_error():
    assert _rules(_tab({"arr": 0, "monthly": 0}), "S06")[0].severity == "info"
    i = _rules(_tab({"renewal_status": "Cancelled 6/31/2026"}), "S12")[0]
    assert i.severity == "error" and "6/31/2026" in i.message
    t = _tab({})
    t.error_cells.append((10, 8, "#REF!"))
    t.skipped_rows.append((12, "Customer Number 'x' is not a number"))
    found = subscription_rules.check(type("W", (), {"tabs": {"onprem": t}})())
    assert {(i.rule, i.cell) for i in found} == {("S01", "H10"), ("S02", "A12")}


def test_maintenance_rules():
    cols = mimp.Columns({5: 2024, 6: 2025, 7: 2026, 8: 2027},
                        {"customer": [1], "customer name": [2], "original before discount": [3], "discount": [4],
                         "original, net of discount": [9], "basis for budget": [10], "monthly revenue": [11],
                         "billing anniversary": [12]})
    def row(n, fees, extra=()):
        v = [1, 80001, "ACME", 10000, -1000, *[fees.get(y) for y in (2024, 2025, 2026, 2027)], 9000, 1300, 100,
             "March", *extra]
        r = mimp.SheetRow(n, MaintenanceBlock.PERPETUAL_LIVE, "A.", 1, tuple(v))
        r.fees = {y: f for y, f in fees.items() if isinstance(f, (int, float))}
        return r
    rows = [row(5, {2024: 1000, 2025: 900, 2026: 0, 2027: 1100})]
    ms = mimp.MaintenanceSheet("x.xlsx", cols, rows, {2024: 1000, 2025: 900, 2026: 5, 2027: 1100}, {}, {}, {},
                               1, 40)
    found = {(i.rule, i.cell) for i in maintenance_rules.check(ms)}
    assert ("M03", "G5") in found                    # 1000 -> 900
    assert ("M04", "I5") in found                    # 1100 after the explicit 0
    assert ("M06", "L5") in found                    # 100 != 1300 / 12
    assert ("M08", "H40") in found                   # GRAND TOTAL 2026 = 5 but rows add up to 0


def test_accept_lapses_when_value_changes(tmp_path):
    i = Issue("S07", "subscription", "Tab", "I10", 10, "msg", current="900", expected="1000")
    with pytest.raises(ValueError):
        accept.accept(tmp_path, i, "  ", "me")
    accept.accept(tmp_path, i, "agreed with customer", "me")
    j = Issue("S07", "subscription", "Tab", "I10", 10, "msg", current="900", expected="1000")
    accept.apply([j], tmp_path)
    assert j.status == "accepted" and j.accepted["reason"] == "agreed with customer"
    k = Issue("S07", "subscription", "Tab", "I10", 10, "msg", current="950", expected="1000")
    accept.apply([k], tmp_path)
    assert k.status == "reopened"
    accept.reopen(tmp_path, j)
    accept.apply([k], tmp_path)
    assert k.status == "open"


# ------------------------------------------------------------------------------------------------ real workbooks

def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


@pytest.fixture(scope="module")
def real_check(tmp_path_factory):
    if not config.source_workbook() or not list(config.SOURCE_DIR.glob("*Subscription Summary*.xlsx")):
        pytest.skip("no workbook in data/source")
    watched = [config.REPORT_DIR / "latest-import.json",
               config.source_workbook(), config.maintenance_workbook()]
    before = {p: _sha(p) for p in watched if p}
    out = tmp_path_factory.mktemp("check")
    res = checker.run_check(out_dir=out)
    return res, out, before


@needs_source
def test_real_check_is_read_only_and_finds_known_items(real_check):
    res, out, before = real_check
    for p, h in before.items():
        assert _sha(p) == h, f"{p} changed"
    assert res.issues and (out / "latest-check.json").exists()
    typo = [i for i in res.issues if "7/8/206" in i.message]
    assert typo and typo[0].tab == "On Prem_License ARR&CARR" and re.fullmatch(r"[A-Z]+25", typo[0].cell)
    assert any(i.rule == "X01" for i in res.issues)                 # Masterlist name mismatches
    assert any(i.rule == "S03" and "pending" in (i.current or "") for i in res.issues)
    for i in res.issues:
        assert i.severity in checker.SEVERITIES and i.rule in checker.RULES
        assert i.cell is None or re.fullmatch(r"[A-Z]{1,3}\d+", i.cell), i
    assert len({i.key for i in res.issues}) == len(res.issues)
    again = checker.load(out)
    assert len(again.issues) == len(res.issues)


@needs_source
def test_real_exports(real_check, tmp_path):
    from app.checker import export
    res, out, _ = real_check
    p = export.issue_list(res, tmp_path / "issues.xlsx")
    ws = openpyxl.load_workbook(p)["Issues"]
    assert ws.max_row == len(res.issues) + 1 and ws["A1"].value == "Severity"
    copy = export.annotated_copy(res, "subscription", tmp_path)
    wb = openpyxl.load_workbook(copy)
    assert wb.sheetnames[0] == "Check issues"
    typo = next(i for i in res.issues if "7/8/206" in i.message)
    c = wb[typo.tab][typo.cell]
    assert c.comment is not None and "7/8/206" in c.comment.text and c.fill.fgColor.rgb.endswith("FAD7A0")
    assert copy.parent == tmp_path and "CHECKED" in copy.name


@needs_source
def test_check_pages_accept_and_downloads(real_check, monkeypatch, tmp_path):
    res, out, _ = real_check
    d = tmp_path / "checks"
    shutil.copytree(out, d)
    monkeypatch.setattr(checker, "CHECK_DIR", d)
    c = TestClient(main.app)
    page = c.get("/check").text
    assert "Spreadsheet check" in page and "Run check" in page and "Highlighted Subscription Summary" in page
    i = next(x for x in checker.load(d).issues if x.status == "open")
    assert c.get(f"/check?rule={i.rule}").status_code == 200
    r = c.post("/check/accept", data={"id": i.id, "reason": "", "query": ""}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    r = c.post("/check/accept", data={"id": i.id, "reason": "known exception", "query": f"rule={i.rule}"},
               follow_redirects=False)
    assert r.headers["location"].startswith(f"/check?rule={i.rule}&msg=Accepted")
    j = next(x for x in checker.load(d).issues if x.id == i.id)
    assert j.status == "accepted"
    assert "known exception" in c.get("/check?status=accepted").text
    c.post("/check/reopen", data={"id": i.id, "query": ""})
    assert next(x for x in checker.load(d).issues if x.id == i.id).status == "open"
    assert c.get("/check/issues.xlsx").status_code == 200
    r = c.get("/check/annotated/maintenance.xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert c.get("/check/annotated/other.xlsx").status_code == 404
    assert "Open the spreadsheet check" in c.get("/import").text


def test_import_cli_warns_but_does_not_block(monkeypatch, tmp_path):
    from app.importer import cli
    monkeypatch.setattr(checker, "CHECK_DIR", tmp_path)
    msg, blocking = cli.check_status()
    assert "not been checked" in msg and blocking == 0
    res = checker.CheckResult("2026-11-13T10:00:00", {}, [Issue("S03", "subscription", "T", "D10", 10, "bad")])
    checker.save(res, tmp_path)
    msg, blocking = cli.check_status()
    assert "1 open error" in msg and blocking == 1
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    called = []
    monkeypatch.setattr(cli, "run_import", lambda *a, **k: called.append(1))
    with pytest.raises(SystemExit, match="Import cancelled"):
        cli.main([])
    assert not called
