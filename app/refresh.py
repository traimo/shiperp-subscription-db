"""Refresh data: pull the workbooks from SharePoint, back up the database, re-import, optionally reapply app changes.

Steps (see README "Refresh data"):
1. download both workbooks (read-only Graph) to a temp folder, check them, then swap them into data/source
   (the previous copies go to data/backups/source-<stamp>/); with pull=False the files already in data/source are used
2. back up the database (data/backups/subscriptions-before-refresh-<stamp>.db; a SQLite file also on PostgreSQL) and archive its change list (.json)
3. company ARR / CARR before
4. re-import (drops every table, including the audit log)
5. reapply the backup's pending changes (only when asked)
6. ARR / CARR after; the result goes to data/reports/latest-refresh.json
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import sqlite3
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from . import config, metrics, reapply as rp, sharepoint
from .db import make_engine
from .money import cents

PATTERNS = {"subscription": "*Subscription Summary*.xlsx", "maintenance": "*Maintenance*.xlsx"}
RESULT_FILE = "latest-refresh.json"
_LOCK = threading.Lock()


class RefreshError(RuntimeError):
    pass


@dataclass
class Paths:
    """Where a refresh reads and writes (tests point these at a temp folder)."""
    source: Path = config.SOURCE_DIR
    backups: Path = config.DATA_DIR / "backups"
    reports: Path = config.REPORT_DIR


PATHS = Paths()


def in_progress() -> bool:
    return _LOCK.locked()


def local_sources(source_dir: Path) -> dict[str, Path]:
    out = {}
    for key, pat in PATTERNS.items():
        files = sorted(p for p in source_dir.glob(pat) if not p.name.startswith("~$"))
        if len(files) > 1:
            raise RefreshError(f"More than one {pat} in {source_dir}.")
        if files:
            out[key] = files[0]
    if "subscription" not in out:
        raise RefreshError(f"No {PATTERNS['subscription']} in {source_dir}.")
    return out


# ------------------------------------------------------------------------------------------------ steps

def _check_workbook(key: str, path: Path, name: str) -> None:
    if not Path(name).match(PATTERNS[key]):
        raise RefreshError(f"The SharePoint file '{name}' does not look like the {key} workbook "
                           f"(expected a name like {PATTERNS[key]}).")
    try:
        from openpyxl import load_workbook
        load_workbook(path, read_only=True).close()
    except Exception as e:
        raise RefreshError(f"'{name}' could not be opened as an Excel workbook ({type(e).__name__}).") from None


def pull(paths: Paths, stamp: str, client=None) -> list[dict]:
    """Download both workbooks and swap them into the source folder. Nothing changes unless both are good."""
    client = client or sharepoint.GraphClient()
    files = sharepoint.resolve_all(client)
    if "subscription" not in files:
        raise RefreshError("SP_SUBSCRIPTION_URL is not set.")
    with tempfile.TemporaryDirectory() as tmp:
        got = {}
        for key, f in files.items():
            p = client.download(f, Path(tmp) / f.name)
            _check_workbook(key, p, f.name)
            got[key] = p
        old = local_sources(paths.source) if any(paths.source.glob("*.xlsx")) else {}
        keep = paths.backups / f"source-{stamp}"
        for key, p in got.items():
            if key in old:
                keep.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old[key]), keep / old[key].name)
            paths.source.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, paths.source / p.name)
    return [{"key": f.key, "name": f.name, "size": f.size, "modified_by": f.modified_by,
             "modified": f.modified.isoformat(timespec="minutes") if f.modified else None} for f in files.values()]


def backup_db(engine, paths: Paths, stamp: str) -> Path:
    """Copy the live database to data/backups/subscriptions-before-refresh-<stamp>.db. Always a SQLite file:
    from SQLite with the SQLite backup API, from PostgreSQL table by table (app/dbcopy.py), so reapply reads
    either kind the same way."""
    paths.backups.mkdir(parents=True, exist_ok=True)
    dst = paths.backups / f"subscriptions-before-refresh-{stamp}.db"
    if engine.url.get_backend_name() == "sqlite":
        src = sqlite3.connect(engine.url.database)
        out = sqlite3.connect(dst)
        try:
            src.backup(out)                                 # consistent copy even while the app is connected
        finally:
            out.close()
            src.close()
        return dst
    from .dbcopy import copy_engines
    out_engine = make_engine(f"sqlite:///{dst.as_posix()}")
    try:
        copy_engines(engine, out_engine)
    except Exception:
        out_engine.dispose()
        dst.unlink(missing_ok=True)
        raise
    out_engine.dispose()
    return dst


def archive_changes(factory, paths: Paths, stamp: str) -> tuple[Path, int]:
    from .models import AuditLog
    from sqlalchemy import select
    with factory() as s:
        rows = list(s.scalars(select(AuditLog).order_by(AuditLog.id)))
        data = [{"id": a.id, "at": a.at.isoformat(timespec="seconds"), "actor": a.actor, "action": a.action,
                 "entity": a.entity, "entity_id": a.entity_id, "customer_qb": a.customer_qb, "summary": a.summary,
                 "detail": a.detail, "undone_at": a.undone_at.isoformat(timespec="seconds") if a.undone_at else None,
                 "undone_by": a.undone_by, "undo_of_id": a.undo_of_id} for a in rows]
        pending = len(rp.pending_changes(s))
    p = paths.backups / f"audit-before-refresh-{stamp}.json"
    p.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    return p, pending


def figures(factory, today: dt.date) -> dict:
    """Company ARR / CARR as on the dashboard (this year and next) plus a few counts."""
    with factory() as s:
        cs = metrics.load_customers(s)
        years = [today.year, today.year + 1]
        arr = metrics.arr_by_year(cs, years, today)
        maint_now = metrics.maintenance_arr_total(cs, today.year, today)
        return {"arr_now": str(cents(metrics.arr_at(cs, today) + maint_now)),
                "carr_now": str(cents(metrics.carr_at(cs, today) + maint_now)),
                "years": {str(y): {k: str(cents(arr[k][y])) for k in ("Subscription", "Maintenance", "Total", "CARR")}
                          for y in years},
                "customers": sum(1 for c in cs if c.subscriptions),
                "terms": sum(len(sub.terms) for c in cs for sub in c.subscriptions)}


# ------------------------------------------------------------------------------------------------ run

def run(factory, actor: str, today: dt.date, reapply: bool = False, pull_from_sharepoint: bool = True,
        paths: Paths | None = None, client=None) -> dict:
    """The whole refresh. Returns (and saves) the result dict. Raises RefreshError before anything was changed
    when the download or a check fails."""
    paths = paths or PATHS
    if not _LOCK.acquire(blocking=False):
        raise RefreshError("A refresh is already running - wait a few seconds.")
    try:
        engine = factory.kw["bind"]
        url = engine.url
        if url.get_backend_name() == "sqlite" and (not url.database or url.database == ":memory:"):
            raise RefreshError("Refresh data needs a database file or server (backup + reapply use a copy).")
        started = dt.datetime.now()
        stamp = started.strftime("%Y%m%d-%H%M%S")
        result = {"started": started.isoformat(timespec="seconds"), "actor": actor, "reapply": reapply,
                  "pulled": pull_from_sharepoint, "files": [], "items": []}

        if pull_from_sharepoint:
            result["files"] = pull(paths, stamp, client)    # raises before anything changed
        srcs = local_sources(paths.source)
        if not pull_from_sharepoint:
            result["files"] = [{"key": k, "name": p.name, "size": p.stat().st_size, "modified_by": "",
                                "modified": dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="minutes")}
                               for k, p in srcs.items()]

        result["before"] = figures(factory, today)
        backup = backup_db(engine, paths, stamp)
        archive, pending = archive_changes(factory, paths, stamp)
        result.update(backup=str(backup), archive=str(archive), pending=pending)

        engine.dispose()                                    # the import rebuilds the tables on this file
        from .importer.cli import run_import
        report = run_import(source=srcs["subscription"], maintenance=srcs.get("maintenance", False),
                            url=url.render_as_string(hide_password=False), today=today, report_dir=paths.reports,
                            force=True)
        engine.dispose()
        result["import"] = {"counts": report.db_counts,
                            "warnings": sum(1 for n in report.notes if n.severity == "warning"),
                            "rejected": sum(1 for n in report.notes if n.severity == "rejected")}

        if reapply and pending:
            old_engine = make_engine(f"sqlite:///{backup.as_posix()}")
            try:
                items = rp.reapply(sessionmaker(bind=old_engine, expire_on_commit=False), factory, actor, today)
            finally:
                old_engine.dispose()
            result["items"] = [i.to_dict() for i in items]

        result["after"] = figures(factory, today)
        result["finished"] = dt.datetime.now().isoformat(timespec="seconds")
        paths.reports.mkdir(parents=True, exist_ok=True)
        (paths.reports / RESULT_FILE).write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
        return result
    finally:
        _LOCK.release()


def last_result(paths: Paths | None = None) -> dict | None:
    p = (paths or PATHS).reports / RESULT_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
