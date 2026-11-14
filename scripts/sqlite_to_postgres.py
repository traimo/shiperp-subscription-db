"""Copy the SQLite database into PostgreSQL (one-off move; replaces everything in the target).

    python scripts/sqlite_to_postgres.py data/subscriptions.db postgresql+psycopg://subs:<pw>@127.0.0.1:5433/subscriptions

With no target URL, it uses POSTGRES_PASSWORD from .env and the Docker database on 127.0.0.1:5433.
The source is only read. The target is dropped, migrated to the newest schema and filled; ids, audit log and
undo history are kept, and the id sequences are moved past the copied ids.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402  (also loads .env)
from app.dbcopy import CopyError, copy_database  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sqlite_file", nargs="?", default=str(config.DATA_DIR / "subscriptions.db"))
    ap.add_argument("target_url", nargs="?", help="SQLAlchemy URL of the PostgreSQL database")
    ap.add_argument("--yes", action="store_true", help="do not ask before replacing the target's data")
    a = ap.parse_args(argv)

    src = Path(a.sqlite_file)
    if not src.is_file():
        print(f"No such file: {src}", file=sys.stderr)
        return 2
    target = a.target_url
    if not target:
        pw = os.environ.get("POSTGRES_PASSWORD")
        if not pw:
            print("Give the target URL, or set POSTGRES_PASSWORD in .env", file=sys.stderr)
            return 2
        target = f"postgresql+psycopg://subs:{pw}@127.0.0.1:5433/subscriptions"
    if not target.startswith("postgresql"):
        print("The target must be a postgresql URL", file=sys.stderr)
        return 2

    from sqlalchemy.engine import make_url
    shown = make_url(target).render_as_string(hide_password=True)
    if not a.yes:
        if input(f"Replace ALL data in {shown} with {src}? [y/N] ").strip().lower() != "y":
            print("Cancelled.")
            return 1
    try:
        counts = copy_database(f"sqlite:///{src.resolve().as_posix()}", target)
    except CopyError as e:
        print(f"Not copied: {e}", file=sys.stderr)
        return 1
    width = max(len(c.table) for c in counts)
    for c in counts:
        print(f"  {c.table:<{width}}  {c.source:>6} -> {c.copied:>6}")
    print(f"Copied {sum(c.copied for c in counts)} rows into {shown}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
