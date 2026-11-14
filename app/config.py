"""Settings. DATABASE_URL in .env (gitignored) or the environment; defaults to SQLite in ./data."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
SOURCE_DIR = DATA_DIR / "source"
REPORT_DIR = DATA_DIR / "reports"


def _load_dotenv() -> None:
    env = PROJECT_DIR / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


_load_dotenv()


def database_url() -> str:
    return os.environ.get("DATABASE_URL") or f"sqlite:///{(DATA_DIR / 'subscriptions.db').as_posix()}"


def maintenance_workbook() -> Path | None:
    """The maintenance workbook in data/source (optional)."""
    files = sorted(p for p in SOURCE_DIR.glob("*Maintenance*.xlsx") if not p.name.startswith("~$"))
    if len(files) > 1:
        raise FileNotFoundError(f"More than one maintenance workbook in {SOURCE_DIR}")
    return files[0] if files else None


def source_workbook() -> Path:
    files = sorted(p for p in SOURCE_DIR.glob("*Subscription Summary*.xlsx") if not p.name.startswith("~$"))
    if len(files) != 1:
        raise FileNotFoundError(f"Expected exactly one *Subscription Summary*.xlsx in {SOURCE_DIR}, found {len(files)}")
    return files[0]
