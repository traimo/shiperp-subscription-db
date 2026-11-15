"""Restore a database backup (a backup_YYYYMMDD_HHMMSS schema) over the live tables.

    python scripts/restore_backup.py                      # list the backups
    python scripts/restore_backup.py backup_20261114_153000 [--yes]

The current data is backed up first (a new backup schema), so a restore can itself be undone. Run it on the host
(uses POSTGRES_PASSWORD from .env, database at 127.0.0.1:5433) or in the app container:
    docker compose exec app python scripts/restore_backup.py <backup>
Stop using the app while it runs.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import backups, config  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("backup", nargs="?", help="backup schema name (omit to list them)")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    a = ap.parse_args(argv)

    print(f"Database: {config.database_label()}")
    found = backups.list_all()
    if not a.backup:
        if not found:
            print("No backups yet.")
        for b in found:
            print(f"  {b.name}   {b.taken:%Y-%m-%d %H:%M:%S}   {b.label}" if b.taken else f"  {b.name}   {b.label}")
        return 0
    if a.backup not in {b.name for b in found}:
        print(f"No backup named {a.backup}. Run without arguments to list them.")
        return 2
    if not a.yes:
        answer = input(f"Replace ALL live data with {a.backup}? The current data is backed up first. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1
    before, counts = backups.restore(a.backup)
    print(f"Current data saved as {before}.")
    for c in counts:
        print(f"  {c.table:<28} {c.copied:>7}")
    print(f"Restored {a.backup}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
