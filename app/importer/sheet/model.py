"""In-memory model of the two ARR&CARR tabs."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from . import rules
from .headers import year_keys


@dataclass
class YearBlock:
    year: int
    start_row: int          # first row the block's subtotal covers
    total_row: int          # the "Total Year YYYY" row
    label_col: int


@dataclass
class ContractRow:
    tab: str                # tab key: "onprem" | "cloud"
    section: str            # A..E
    row: int
    values: dict            # key -> cached (computed) value
    raw: dict               # key -> raw cell content (formula text or constant)
    block_year: int | None = None

    def get(self, key, default=None):
        v = self.values.get(key)
        return default if v is None else v

    @property
    def customer_number(self) -> int:
        return int(self.values["customer_number"])

    @property
    def customer_name(self) -> str:
        return str(self.values.get("customer_name") or "").strip()

    @property
    def period(self) -> rules.Period:
        return rules.parse_period(self.values.get("period"))

    @property
    def arr(self) -> float:
        return rules.to_number(self.values.get("arr")) or 0.0

    def year_arr(self, year: int) -> float | None:
        return rules.to_number(self.values.get(f"arr_{year}"))

    @property
    def churn_flagged(self) -> bool:
        return rules.is_churn_flagged(self.values.get("status"), self.values.get("renewal_status"))

    @property
    def not_subscription(self) -> bool:
        return rules.is_not_subscription(self.values.get("renewal_status"))

    @property
    def uid(self) -> str:
        return f"{self.tab}:{self.row}"

    def is_live(self, on: dt.date) -> bool:
        p = self.period
        return (self.section != "E" and p.ok and p.start <= on <= p.end and self.arr > 0
                and not self.not_subscription)


@dataclass
class Section:
    letter: str
    title: str
    title_row: int
    header_row: int
    end_row: int                         # last row belonging to the section
    colmap: dict[str, int]               # key -> column (section header, falling back to master)
    header_text: dict[str, str]          # key -> header text as written in this section
    blocks: list[YearBlock] = field(default_factory=list)
    grand_total_row: int | None = None
    issues: "HeaderIssues | None" = None  # unknown / duplicate headers in this section's header row

    def block_for_year(self, year: int) -> YearBlock | None:
        return next((b for b in self.blocks if b.year == year), None)

    def block_for_row(self, row: int) -> YearBlock | None:
        return next((b for b in self.blocks if b.start_row <= row < b.total_row), None)

    @property
    def years(self) -> dict[int, str]:
        return year_keys(self.colmap)


@dataclass
class Tab:
    key: str                              # "onprem" | "cloud"
    sheet_name: str
    title: str
    as_of: str
    master_row: int
    master: dict[str, int]
    reviewers: dict[int, str]             # column -> person (row 1)
    max_col: int
    sections: dict[str, Section] = field(default_factory=dict)
    contracts: list[ContractRow] = field(default_factory=list)
    error_cells: list[tuple[int, int, str]] = field(default_factory=list)
    skipped_rows: list[tuple[int, str]] = field(default_factory=list)   # (row, why) rows that look like contracts

    @property
    def label(self) -> str:
        return "On-Prem" if self.key == "onprem" else "Cloud"

    def reviewer_for(self, key: str, section: str = "A") -> str:
        col = self.sections[section].colmap.get(key) if section in self.sections else self.master.get(key)
        return self.reviewers.get(col or 0, "")

    def row(self, row: int) -> ContractRow | None:
        return next((c for c in self.contracts if c.row == row), None)


@dataclass
class Workbook:
    path: str
    mtime: float
    tabs: dict[str, Tab]
    masterlist: dict[int, str]

    def all_contracts(self):
        for t in self.tabs.values():
            yield from t.contracts

    def customer_rows(self, number: int) -> list[ContractRow]:
        return [c for c in self.all_contracts() if c.customer_number == number]

    def customer_name(self, number: int) -> str:
        for c in self.customer_rows(number):
            if c.customer_name:
                return c.customer_name
        return self.masterlist.get(number, "")

    def find(self, uid: str) -> ContractRow | None:
        tab, row = uid.split(":")
        return self.tabs[tab].row(int(row))
