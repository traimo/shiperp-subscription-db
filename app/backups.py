"""Database backups as PostgreSQL schemas: `backup_YYYYMMDD_HHMMSS` in the same database, a full copy of every app
table (app/dbcopy.py). Made before every Refresh / import; the newest KEEP are kept.

- Reapply (app/reapply.py) reads the pre-refresh backup through `engine_for(name)`.
- `restore(name)` copies a backup back over the live tables (scripts/restore_backup.py), after backing up the
  current data first.
- The dashboard's read-only login has no USAGE on these schemas, so it never sees them.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from sqlalchemy import text

from . import config
from .db import check_schema, drop_schema, list_schemas, make_engine

PREFIX = "backup_"
KEEP = 10
NAME_RE = re.compile(r"^backup_(\d{8})_(\d{6})(?:_\d+)?$")


@dataclass
class Backup:
    name: str
    taken: dt.datetime | None
    label: str


def _url(engine=None) -> str:
    return engine.url.render_as_string(hide_password=False) if engine is not None else config.database_url()


def _new_name(engine, now: dt.datetime) -> str:
    base = f"{PREFIX}{now:%Y%m%d_%H%M%S}"
    taken = set(list_schemas(engine, PREFIX))
    name, n = base, 1
    while name in taken:                                    # two backups in the same second
        n += 1
        name = f"{base}_{n}"
    return name


def make(engine, label: str, keep: int = KEEP) -> str:
    """Copy the live tables of `engine` into a new backup schema; returns its name. The live data is only read."""
    from .dbcopy import copy_engines
    url = _url(engine)
    name = _new_name(engine, dt.datetime.now())
    dst = make_engine(url, schema=name)
    try:
        copy_engines(engine, dst)
        with dst.begin() as conn:
            conn.exec_driver_sql(f'COMMENT ON SCHEMA "{name}" IS ' + _literal(label[:200]))
    except Exception:
        dst.dispose()
        drop_schema(url, name)
        raise
    dst.dispose()
    prune(engine, keep)
    return name


def _literal(s: str) -> str:
    """SQL string literal (COMMENT ON does not take bind parameters)."""
    return "'" + s.replace("'", "''") + "'"


def prune(engine, keep: int = KEEP) -> list[str]:
    names = list_schemas(engine, PREFIX)
    old = [n for n in names if NAME_RE.match(n)][:-keep] if keep else []
    for n in old:
        drop_schema(_url(engine), n)
    return old


def list_all(engine=None) -> list[Backup]:
    """Newest first."""
    own = engine is None
    engine = engine or make_engine()
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT n.nspname, obj_description(n.oid, 'pg_namespace') FROM pg_namespace n "
                "WHERE n.nspname LIKE 'backup\\_%' ORDER BY n.nspname DESC")).all()
    finally:
        if own:
            engine.dispose()
    out = []
    for name, label in rows:
        m = NAME_RE.match(name)
        taken = dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S") if m else None
        out.append(Backup(name, taken, label or ""))
    return out


def engine_for(name: str, url: str | None = None):
    """Engine on an existing backup schema (never creates one)."""
    check_schema(name)
    if not name.startswith(PREFIX):
        raise ValueError(f"Not a backup: {name}")
    return make_engine(url, schema=name, create_schema=False)


def exists(name: str, engine=None) -> bool:
    own = engine is None
    engine = engine or make_engine()
    try:
        return name in list_schemas(engine, PREFIX)
    finally:
        if own:
            engine.dispose()


def restore(name: str, url: str | None = None) -> tuple[str, list]:
    """Replace the live tables with backup `name`. Backs up the current data first; returns (that backup, counts)."""
    from .dbcopy import copy_engines
    live = make_engine(url)
    try:
        if not exists(name, live):
            raise ValueError(f"No backup schema {name}")
        before = make(live, f"before restoring {name}")
        src = engine_for(name, _url(live))
        try:
            counts = copy_engines(src, live)
        finally:
            src.dispose()
        return before, counts
    finally:
        live.dispose()
