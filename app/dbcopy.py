"""Copy every app table from one engine to another, table by table - in practice from one PostgreSQL schema to
another: Refresh / import backups (live -> backup_YYYYMMDD_HHMMSS, app/backups.py), restoring a backup, and the tests.

The destination is rebuilt first (drop + Alembic migrations to head), so both sides have the same schema. Rows are
read and written through the app's table definitions, so money stays Decimal and dates stay dates.
"""
from __future__ import annotations

from dataclasses import dataclass

from alembic.script import ScriptDirectory
from alembic.config import Config
from sqlalchemy import Integer, func, insert, inspect, select, text
from sqlalchemy.exc import DBAPIError

from . import config, models  # noqa: F401  (models registers the tables)
from .db import Base, make_engine

CHUNK = 1000


class CopyError(RuntimeError):
    pass


@dataclass
class TableCount:
    table: str
    source: int
    copied: int


def head_revision() -> str:
    cfg = Config(str(config.PROJECT_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(config.PROJECT_DIR / "alembic"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def schema_revision(engine) -> str | None:
    with engine.connect() as conn:
        if "alembic_version" not in inspect(conn).get_table_names():
            return None
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _reset_sequences(conn) -> None:
    """PostgreSQL: move each id sequence past the copied ids (explicit ids do not advance it)."""
    for table in Base.metadata.sorted_tables:
        if "id" not in table.c or not isinstance(table.c.id.type, Integer):
            continue
        seq = conn.execute(text("SELECT pg_get_serial_sequence(:t, 'id')"), {"t": table.name}).scalar()
        if seq:
            conn.execute(text(f"SELECT setval(:s, COALESCE((SELECT MAX(id) FROM {table.name}), 1), "
                              f"(SELECT MAX(id) FROM {table.name}) IS NOT NULL)"), {"s": seq})


def copy_engines(src_engine, dst_engine) -> list[TableCount]:
    """Replace everything in dst with src's rows. src must be on the newest migration."""
    from .importer.cli import reset_schema            # local import: the importer imports this package's models

    head = head_revision()
    rev = schema_revision(src_engine)
    if rev != head:
        raise CopyError(f"The source database is on migration {rev or 'none'}, not {head}: "
                        f"run 'alembic upgrade head' on it first.")
    known = {t.name for t in Base.metadata.sorted_tables} | {"alembic_version"}
    extra = sorted(set(inspect(src_engine).get_table_names()) - known)
    if extra:
        raise CopyError(f"The source has tables the app does not know: {', '.join(extra)}")

    reset_schema(dst_engine)                           # drop + migrate to head: same schema as the source
    counts: list[TableCount] = []
    with src_engine.connect() as src, dst_engine.begin() as dst:
        if dst.dialect.name == "postgresql":
            try:                # FK triggers off for the bulk copy (needs a superuser; else rely on table order)
                with dst.begin_nested():
                    dst.execute(text("SET LOCAL session_replication_role = replica"))
            except DBAPIError:
                pass
        for table in Base.metadata.sorted_tables:
            n_src = src.execute(select(func.count()).select_from(table)).scalar() or 0
            result = src.execute(select(table).order_by(*table.primary_key.columns)).mappings()
            while batch := result.fetchmany(CHUNK):
                dst.execute(insert(table), [dict(r) for r in batch])
            n_dst = dst.execute(select(func.count()).select_from(table)).scalar() or 0
            counts.append(TableCount(table.name, n_src, n_dst))
        if dst.dialect.name == "postgresql":
            _reset_sequences(dst)
    bad = [c for c in counts if c.source != c.copied]
    if bad:
        raise CopyError("Row counts differ after the copy: " + ", ".join(f"{c.table} {c.source}->{c.copied}" for c in bad))
    return counts


def copy_database(src_url: str, dst_url: str) -> list[TableCount]:
    src, dst = make_engine(src_url), make_engine(dst_url)
    try:
        return copy_engines(src, dst)
    finally:
        src.dispose()
        dst.dispose()
