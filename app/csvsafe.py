"""CSV exports are opened in Excel: a text cell starting with = + - @ (or a tab / carriage return) would run as a
formula (CSV injection). Text cells get a leading apostrophe; numbers are written unchanged."""
from __future__ import annotations

FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def text(value) -> str:
    """A text cell for a CSV export (None -> '')."""
    s = "" if value is None else str(value)
    return "'" + s if s.startswith(FORMULA_START) else s
