"""Engine / session setup. PostgreSQL only.

`make_engine(url, schema=...)` points an engine at another schema of the same database (search_path). Used for the
Refresh / import backups (`backup_YYYYMMDD_HHMMSS`), the Spreadsheet check's throwaway import (`sheetcheck_<pid>`) and
the tests. Schema names come from code only and are checked against SCHEMA_RE.
"""
from __future__ import annotations

import re
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import database_url

SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class Base(DeclarativeBase):
    pass


def check_schema(schema: str) -> str:
    if not SCHEMA_RE.match(schema or "") or schema in ("public", "information_schema") or schema.startswith("pg_"):
        raise ValueError(f"Not an allowed schema name: {schema!r}")
    return schema


def schema_url(url: str | None, schema: str) -> str:
    """The same database with `schema` as search_path. The schema travels in the URL (libpq `options`), so anything
    that is handed engine.url (the importer, Alembic, backups) stays in that schema."""
    check_schema(schema)
    u = make_url(url or database_url()).update_query_dict({"options": f"-csearch_path={schema}"})
    return u.render_as_string(hide_password=False)


def _base_url(url: str | None) -> str:
    u = make_url(url or database_url())
    return u.set(query={k: v for k, v in u.query.items() if k != "options"}).render_as_string(hide_password=False)


def make_engine(url: str | None = None, schema: str | None = None, create_schema: bool = True):
    url = url or database_url()
    if schema is None:
        return create_engine(url, future=True)
    if create_schema:
        check_schema(schema)
        admin = create_engine(_base_url(url), future=True)
        try:
            with admin.begin() as conn:
                conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        finally:
            admin.dispose()
    return create_engine(schema_url(url, schema), future=True)


def drop_schema(url: str | None, schema: str) -> None:
    check_schema(schema)
    eng = create_engine(_base_url(url), future=True)
    try:
        with eng.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        eng.dispose()


def list_schemas(engine, prefix: str) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE :p "
                                 "ORDER BY schema_name"), {"p": prefix.replace("_", r"\_") + "%"})
        return [r[0] for r in rows]


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory=None):
    s = (factory or SessionLocal)()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
