"""Downloads: the Excel issue list, and highlighted copies of the workbooks (problem cells filled + a comment).
The source workbooks are only read; copies are written to data/checks/."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import openpyxl
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from . import SEVERITIES, CheckResult, Issue

FILL = {"error": "F8B4B4", "decision": "B9D3F5", "warning": "FAD7A0", "info": "DDDDDD"}
COLUMNS = [("Severity", 10), ("Status", 10), ("Rule", 7), ("Problem", 34), ("Workbook", 12), ("Tab", 26),
           ("Cell", 9), ("Row", 6), ("Column", 22), ("Customer", 10), ("Message", 70), ("Current value", 22),
           ("Expected", 22), ("How to fix", 50), ("Accepted reason", 30), ("Accepted by", 12)]


def _row(i: Issue) -> list:
    acc = i.accepted or {}
    return [i.severity, i.status, i.rule, i.title, i.workbook, i.tab, i.cell or "", i.row or "", i.column,
            i.customer or "", i.message, i.current or "", i.expected or "", i.fix, acc.get("reason", ""),
            acc.get("by", "")]


def _issue_sheet(ws, issues: list[Issue], link: bool) -> None:
    ws.append([c for c, _ in COLUMNS])
    for j, (_, w) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(j)].width = w
        ws.cell(1, j).font = Font(bold=True)
    for i in issues:
        ws.append(_row(i))
        r = ws.max_row
        ws.cell(r, 1).fill = PatternFill("solid", fgColor=FILL[i.severity])
        if link and i.cell and i.tab in ws.parent.sheetnames:
            c = ws.cell(r, 7)
            c.hyperlink = f"#'{i.tab}'!{i.cell}"
            c.font = Font(color="1F4E99", underline="single")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def _sorted(issues: list[Issue]) -> list[Issue]:
    return sorted(issues, key=lambda i: (i.status == "accepted", SEVERITIES.index(i.severity), i.workbook, i.tab,
                                         i.row or 0, i.rule))


def issue_list(res: CheckResult, path: Path) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Issues"
    _issue_sheet(ws, _sorted(res.issues), link=False)
    s = wb.create_sheet("Summary")
    s.append(["Checked", res.ran_at])
    for name, info in (res.sources or {}).items():
        if info:
            s.append([name, info["name"], info["modified"]])
    s.append([])
    s.append(["Severity", "Open", "Accepted"])
    for sev, n in res.counts().items():
        s.append([sev, n["open"], n["accepted"]])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def annotated_copy(res: CheckResult, workbook: str, out_dir: Path) -> Path:
    """Copy of the source workbook with every issue cell highlighted and commented, plus a 'Check issues' sheet."""
    info = (res.sources or {}).get(workbook)
    if not info:
        raise FileNotFoundError(f"No {workbook} workbook in the last check.")
    src = Path(info["path"])
    wb = openpyxl.load_workbook(src)                       # formulas kept; the source file is not written
    issues = _sorted([i for i in res.issues if i.workbook == workbook])
    for i in issues:
        if not i.cell or i.tab not in wb.sheetnames or i.status == "accepted":
            continue
        try:
            c = wb[i.tab][i.cell]
        except (ValueError, KeyError):
            continue
        c.fill = PatternFill("solid", fgColor=FILL[i.severity])
        text = f"[{i.severity.upper()} {i.rule}] {i.title}\n{i.message}"
        if i.expected:
            text += f"\nExpected: {i.expected}"
        text += f"\nFix: {i.fix}"
        if c.comment is not None and "Spreadsheet check" in (c.comment.author or ""):
            text = c.comment.text + "\n\n" + text
        elif c.comment is not None:
            text = c.comment.text + "\n\n---\n" + text
        cm = Comment(text[:2000], "Spreadsheet check")
        cm.width, cm.height = 360, 160
        c.comment = cm
    ws = wb.create_sheet("Check issues", 0)
    _issue_sheet(ws, issues, link=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{src.stem} - CHECKED {dt.date.today():%Y-%m-%d}.xlsx"
    wb.save(out)
    return out
