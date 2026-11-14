"""Read the Subscription Summary workbook (read-only) into the model. Never saves anything.

Rules (spec §3): a section starts at a column-A cell "<A-E>. <name>"; its header row is the first following row whose
column A starts with "Counter"; a contract row has a numeric Customer Number and a non-empty Subscription Period;
"Total Year YYYY" rows close each signing-year block.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

import openpyxl
from openpyxl.utils import get_column_letter

from .headers import classify, norm
from .model import ContractRow, Section, Tab, Workbook, YearBlock

SECTION_RE = re.compile(r"^\s*([A-E])\.\s+(.+)$")
TOTAL_YEAR_RE = re.compile(r"(?i)^\s*total year\s+(20\d\d)\s*$")
ERROR_VALUES = ("#VALUE!", "#REF!", "#DIV/0!", "#N/A", "#NAME?", "#NUM!", "#NULL!")
TAB_KEYS = {"onprem": ("on prem", "arr"), "cloud": ("cloud", "arr")}


def _tab_key(sheet_name: str) -> str | None:
    n = sheet_name.lower()
    for key, words in TAB_KEYS.items():
        if all(w in n for w in words):
            return key
    return None


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _grid(ws, max_col: int) -> list[list]:
    """1-based-friendly grid: grid[r][c] with an unused row 0 / column 0."""
    rows = [[None] * (max_col + 1)]
    for r in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=max_col, values_only=True):
        rows.append([None, *r] + [None] * (max_col - len(r)))
    return rows


def _used_width(ws, header_row: int) -> int:
    last = 1
    for c in ws[header_row]:
        if c.value is not None:
            last = c.column
    return last + 2


def read_tab(key: str, ws_f, ws_v) -> Tab:
    # find master header row (first "Counter..." in column A)
    master_row = next(r for r in range(1, ws_f.max_row + 1)
                      if norm(ws_f.cell(r, 1).value).startswith("counter"))
    max_col = _used_width(ws_f, master_row)
    F = _grid(ws_f, max_col)        # raw (formulas)
    V = _grid(ws_v, max_col)        # cached values
    nrows = len(F) - 1

    master_headers = {c: F[master_row][c] for c in range(1, max_col + 1)}
    master = classify(master_headers)
    reviewers = {c: str(F[1][c]).strip() for c in range(1, max_col + 1) if F[1][c]}
    title = str(F[4][1] or "")
    as_of = str(F[5][1] or "")

    tab = Tab(key=key, sheet_name=ws_f.title, title=title, as_of=as_of, master_row=master_row,
              master=master, reviewers=reviewers, max_col=max_col)

    # sections
    starts: list[tuple[int, str, str]] = []
    for r in range(1, nrows + 1):
        a = F[r][1]
        if isinstance(a, str) and len(a.strip()) > 3:
            m = SECTION_RE.match(a)
            if m:
                starts.append((r, m.group(1), a.strip()))
    for i, (r, letter, title_text) in enumerate(starts):
        end = (starts[i + 1][0] - 1) if i + 1 < len(starts) else nrows
        header_row = next((x for x in range(r + 1, end + 1) if norm(F[x][1]).startswith("counter")), master_row)
        raw_headers = {c: (F[header_row][c] if F[header_row][c] is not None else master_headers.get(c))
                       for c in range(1, max_col + 1)}
        sec_map = classify(raw_headers)
        colmap = dict(master)
        # Drop master year keys that the section relabels to a different column
        colmap.update(sec_map)
        header_text = {k: str(F[header_row][c] if F[header_row][c] is not None else master_headers.get(c) or "")
                       for k, c in colmap.items()}
        sec = Section(letter, title_text, r, header_row, end, colmap, header_text)

        prev_end = header_row
        for x in range(header_row + 1, end + 1):
            for c in range(1, min(max_col, 40) + 1):
                v = F[x][c]
                if isinstance(v, str):
                    m = TOTAL_YEAR_RE.match(v)
                    if m:
                        sec.blocks.append(YearBlock(int(m.group(1)), prev_end + 1, x, c))
                        prev_end = x
                        break
        if sec.blocks:
            last_total = sec.blocks[-1].total_row
            probe_cols = [colmap.get(k) for k in ("counter_year", "arr", "tcv_total", "tcv_existing", "monthly")]
            for x in range(last_total + 1, end + 1):
                hit = False
                for c in filter(None, probe_cols):
                    f = F[x][c]
                    if isinstance(f, str) and f.startswith("=") and re.search(
                            rf"\b{get_column_letter(c)}\$?{last_total}\b", f):
                        hit = True
                        break
                if hit:
                    sec.grand_total_row = x
                    break
        tab.sections[letter] = sec

        # contract rows
        cn_col, per_col = colmap.get("customer_number"), colmap.get("period")
        if not cn_col or not per_col:
            continue
        for x in range(header_row + 1, end + 1):
            cn, per = V[x][cn_col], V[x][per_col]
            if not _is_number(cn) or per is None or not str(per).strip():
                has_cn = cn is not None and str(cn).strip() != ""
                has_per = per is not None and str(per).strip() != ""
                if has_cn and has_per:          # e.g. customer number typed as text: not imported
                    tab.skipped_rows.append((x, f"Customer Number '{cn}' is not a number"))
                elif _is_number(cn) and not has_per and any(_is_number(V[x][c]) and V[x][c] for c in (
                        colmap.get("arr"), colmap.get("tcv_total")) if c):
                    tab.skipped_rows.append((x, "Subscription Period is blank"))
                continue
            values = {k: V[x][c] for k, c in colmap.items()}
            raw = {k: F[x][c] for k, c in colmap.items()}
            values["customer_number"] = int(cn)
            blk = sec.block_for_row(x)
            tab.contracts.append(ContractRow(key, letter, x, values, raw, blk.year if blk else None))
            for c in range(1, max_col + 1):
                v = V[x][c]
                if isinstance(v, str) and v.strip() in ERROR_VALUES:
                    tab.error_cells.append((x, c, v.strip()))
    return tab


def read_masterlist(wb) -> dict[int, str]:
    if "Masterlist" not in wb.sheetnames:
        return {}
    out = {}
    for num, name, *_ in wb["Masterlist"].iter_rows(min_row=2, max_col=2, values_only=True):
        if _is_number(num):
            out[int(num)] = str(name or "").strip()
    return out


def read_workbook(path: Path) -> Workbook:
    path = Path(path)
    wb_f = openpyxl.load_workbook(path, data_only=False)
    wb_v = openpyxl.load_workbook(path, data_only=True)
    tabs = {}
    for name in wb_f.sheetnames:
        key = _tab_key(name)
        if key and key not in tabs:
            tabs[key] = read_tab(key, wb_f[name], wb_v[name])
    return Workbook(str(path), path.stat().st_mtime, tabs, read_masterlist(wb_v))


class WorkbookCache:
    """Reloads only when the file's mtime changes."""

    def __init__(self, path_fn):
        self._path_fn = path_fn
        self._wb: Workbook | None = None
        self._lock = threading.Lock()

    def get(self) -> Workbook:
        path = Path(self._path_fn())
        with self._lock:
            if self._wb is None or self._wb.path != str(path) or self._wb.mtime != path.stat().st_mtime:
                self._wb = read_workbook(path)
            return self._wb

    def invalidate(self):
        with self._lock:
            self._wb = None
