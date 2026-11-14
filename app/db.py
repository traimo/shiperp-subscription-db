from __future__ import annotations

import warnings
from contextlib import contextmanager

from sqlalchemy import create_engine, event

# SQLite has no DECIMAL type: Numeric(14, 2) is stored as REAL and rounded back to 2 places on read (exact for
# amounts below ~90 trillion). SQL Server / PostgreSQL store DECIMAL natively. The warning is expected for SQLite.
warnings.filterwarnings("ignore", message=r"Dialect sqlite\+pysqlite does \*not\* support Decimal objects natively")
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import database_url


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    url = url or database_url()
    engine = create_engine(url, future=True)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):          # SQLite enforces foreign keys only when asked
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    return engine


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
