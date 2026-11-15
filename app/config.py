"""Settings. The database is PostgreSQL: DATABASE_URL from the environment (docker-compose.yml sets it for the app
container); on the host it defaults to the compose database at 127.0.0.1:5433 with POSTGRES_PASSWORD from .env."""
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


HOST_DB = "127.0.0.1:5433"     # the compose database as seen from the host ("localhost" stalls on Windows: IPv6 first)


class ConfigError(RuntimeError):
    pass


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        pw = os.environ.get("POSTGRES_PASSWORD", "").strip()
        if not pw:
            raise ConfigError("No database configured: set DATABASE_URL (postgresql+psycopg://...) or POSTGRES_PASSWORD "
                              "in .env.")
        url = f"postgresql+psycopg://subs:{pw}@{HOST_DB}/subscriptions"
    if not url.startswith("postgresql"):
        raise ConfigError("DATABASE_URL must be a PostgreSQL URL (postgresql+psycopg://...); SQLite is no longer "
                          "supported.")
    return url


def database_label(url: str | None = None) -> str:
    """'PostgreSQL · subscriptions on db:5432' - for pages and logs; never the user or password."""
    from sqlalchemy.engine import make_url
    u = make_url(url or database_url())
    return f"PostgreSQL · {u.database} on {u.host}{f':{u.port}' if u.port else ''}"


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
