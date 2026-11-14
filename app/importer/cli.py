"""python -m app.importer  - rebuild the database from data/source/*.xlsx (drop + migrate + load)."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from .. import config
from ..db import Base, make_engine
from . import report as rep
from .maintenance import import_maintenance
from .mapper import ImportReport, Mapper
from .sheet.reader import read_workbook


def reset_schema(engine) -> None:
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        if "alembic_version" in inspect(conn).get_table_names():
            conn.execute(text("DROP TABLE alembic_version"))
    cfg = Config(str(config.PROJECT_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(config.PROJECT_DIR / "alembic"))
    cfg.attributes["engine"] = engine
    command.upgrade(cfg, "head")


class ImportRefused(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------------ contract documents

DOC_COLS = ("id", "customer_id", "term_id", "maintenance_contract_id", "link_qb", "link_source_ref", "filename",
            "url", "doc_type", "note", "uploaded_by", "uploaded_at")


def snapshot_documents(engine) -> list[dict]:
    """Document links before the tables are rebuilt (the SharePoint files are never touched by an import)."""
    with engine.connect() as conn:
        if "contract_document" not in inspect(conn).get_table_names():
            return []
        have = {c["name"] for c in inspect(conn).get_columns("contract_document")}   # older schema: no url column
        cols = [c for c in DOC_COLS if c in have]
        rows = conn.execute(text(f"SELECT {', '.join(cols)} FROM contract_document")).mappings().all()
        return [{c: r.get(c) for c in DOC_COLS} for r in rows]


def relink_documents(s, docs: list[dict], report: ImportReport, maintenance_only: bool = False) -> None:
    """Re-insert / re-attach documents after a rebuild. A document goes back to the term or maintenance line with the
    same customer number and sheet ref; if that is gone (or the term was created in the app, no sheet ref) it stays
    on the customer as 'unlinked'. If the customer itself is gone the metadata is written to the report."""
    import datetime as _dt
    from sqlalchemy import select
    from ..models import ContractDocument, Customer, DocType, MaintenanceContract, Subscription, Term
    customers = {c.qb_number: c for c in s.scalars(select(Customer))}
    terms: dict = {}
    for t, qb in s.execute(select(Term, Customer.qb_number)
                           .join(Subscription, Term.subscription_id == Subscription.id)
                           .join(Customer, Subscription.customer_id == Customer.id)):
        if t.source_ref:
            terms.setdefault((qb, t.source_ref), []).append(t)
    lines: dict = {}
    for mc, qb in s.execute(select(MaintenanceContract, Customer.qb_number)
                            .join(Customer, MaintenanceContract.customer_id == Customer.id)):
        if mc.source_ref:
            lines.setdefault((qb, mc.source_ref), []).append(mc)
    for d in docs:
        was_term = d["term_id"] is not None
        was_line = d["maintenance_contract_id"] is not None
        if maintenance_only and not was_line:
            continue                                            # term / unlinked documents were not touched
        c = customers.get(d["link_qb"])
        where = f"Document {d['id']}"
        if c is None:
            report.add("warning", None, f"Contract document '{d['filename']}' ({d['url']}): "
                       f"customer {d['link_qb']} no longer exists - not re-linked; the SharePoint file is untouched.",
                       customer=d["link_qb"], where=where)
            continue
        key = (d["link_qb"], d["link_source_ref"])
        hits = (terms if was_term else lines).get(key, []) if d["link_source_ref"] else []
        target = hits[0] if len(hits) == 1 else None
        if maintenance_only:
            doc = s.get(ContractDocument, d["id"])
            doc.maintenance_contract = target
        else:
            values = {k: d[k] for k in DOC_COLS if k not in ("customer_id", "term_id", "maintenance_contract_id")}
            values["doc_type"] = DocType(values["doc_type"])
            if isinstance(values["uploaded_at"], str):
                values["uploaded_at"] = _dt.datetime.fromisoformat(values["uploaded_at"])
            doc = ContractDocument(**values, customer=c)
            if was_term:
                doc.term = target
            elif was_line:
                doc.maintenance_contract = target
            s.add(doc)
        if (was_term or was_line) and target is None:
            report.add("warning", None, f"Contract document '{d['filename']}' was on "
                       f"{d['link_source_ref'] or 'a term created in the app'}, which this import could not find "
                       "again - kept on the customer as unlinked.", customer=d["link_qb"], where=where)
    s.flush()


def user_changes(engine) -> int:
    """Number of audited changes made through the app (they would be lost by a re-import)."""
    with engine.connect() as conn:
        if "audit_log" not in inspect(conn).get_table_names():
            return 0
        # document uploads are not lost by a re-import (they are re-linked), so they do not block it
        return conn.execute(text("SELECT COUNT(*) FROM audit_log WHERE entity <> 'document'")).scalar() or 0


def run_import(source: Path | None = None, url: str | None = None, today: dt.date | None = None,
               report_dir: Path | None = None, maintenance: Path | None | bool = None,
               force: bool = False) -> ImportReport:
    """maintenance: path, None = auto-detect in data/source, False = skip.
    Refuses to wipe a database that has changes made in the app, unless force=True."""
    source = Path(source or config.source_workbook())
    if maintenance is None:
        maintenance = config.maintenance_workbook()
    engine = make_engine(url)
    n = user_changes(engine)
    if n and not force:
        engine.dispose()
        raise ImportRefused(f"The database has {n} change(s) made in the app (see the Audit log). Re-importing "
                            "would erase them. Re-run with --force only if that is intended.")
    try:                                              # read the workbook first: a locked / broken file must not
        sha = hashlib.sha256(source.read_bytes()).hexdigest()       # leave an emptied database behind
        wb = read_workbook(source)                    # read-only
    except Exception:
        engine.dispose()
        raise
    docs = snapshot_documents(engine)
    reset_schema(engine)
    report = ImportReport(source=str(source), source_sha256=sha, started=dt.datetime.now().isoformat(timespec="seconds"))
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        m = Mapper(s, wb, report, today or dt.date.today())
        m.run()
        m.finish()
        if maintenance:
            import_maintenance(s, Path(maintenance), report, m.customers)
        relink_documents(s, docs, report)
        s.commit()
        report.db_counts = rep.db_counts(s)
        if maintenance:
            rep.reconcile_maintenance(s, report)
        rep.reconcile(s, wb, report, report_year=(today or dt.date.today()).year, as_of=today or dt.date.today())
    rep.write(report, report_dir or config.REPORT_DIR)
    engine.dispose()
    return report


MAINT_TABLES = ("maintenance_invoice", "license_purchase", "maintenance_fee", "maintenance_contract")


def maintenance_changes(engine) -> int:
    with engine.connect() as conn:
        if "audit_log" not in inspect(conn).get_table_names():
            return 0
        return conn.execute(text("SELECT COUNT(*) FROM audit_log WHERE entity LIKE 'maintenance%'")).scalar() or 0


def run_maintenance_import(path: Path | None = None, url: str | None = None, report_dir: Path | None = None,
                           force: bool = False, today: dt.date | None = None) -> ImportReport:
    """Rebuild only the maintenance tables from the maintenance workbook; subscriptions and app changes to them are
    untouched. Refuses if maintenance was changed in the app (audit log), unless force=True."""
    import json
    from sqlalchemy import select
    from ..models import Customer
    path = Path(path or config.maintenance_workbook() or "")
    if not path.name:
        raise ImportRefused("No maintenance workbook in data/source.")
    engine = make_engine(url)
    n = maintenance_changes(engine)
    if n and not force:
        engine.dispose()
        raise ImportRefused(f"{n} maintenance change(s) were made in the app (see the Audit log). Reloading would "
                            "erase them. Re-run with --force only if that is intended.")
    cfg = Config(str(config.PROJECT_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(config.PROJECT_DIR / "alembic"))
    cfg.attributes["engine"] = engine
    command.upgrade(cfg, "head")
    report = ImportReport(source=str(path), source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                          started=dt.datetime.now().isoformat(timespec="seconds"), maintenance_only=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    docs = snapshot_documents(engine)
    with Session() as s:
        s.execute(text("UPDATE contract_document SET maintenance_contract_id = NULL "
                       "WHERE maintenance_contract_id IS NOT NULL"))
        for t in MAINT_TABLES:
            s.execute(text(f"DELETE FROM {t}"))
        customers = {c.qb_number: c for c in s.scalars(select(Customer))}
        import_maintenance(s, path, report, customers)
        relink_documents(s, docs, report, maintenance_only=True)
        s.commit()
        report.db_counts = rep.db_counts(s)
        rep.reconcile_maintenance(s, report)
    engine.dispose()
    # merge into the latest full-import report so the Import page shows both parts
    out = Path(report_dir or config.REPORT_DIR)
    latest = out / "latest-import.json"
    if latest.exists():
        data = json.loads(latest.read_text(encoding="utf-8"))
        data["notes"] = [n for n in data.get("notes", []) if not str(n.get("ref", "")).startswith(
            ("Maintenance row", "12-", "Addition", "Document"))] + [n.__dict__ for n in report.notes]
        for k in ("maintenance_source", "maintenance_totals", "maintenance_sheet_revenue",
                  "maintenance_sheet_deferred", "maintenance_revenue", "maintenance_invoices"):
            data[k] = getattr(report, k)
        data["db_counts"] = report.db_counts
        data["maintenance_reloaded"] = report.started
        latest.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    else:
        rep.write(report, out)
    return report


def check_status() -> tuple[str, int]:
    """(message, open errors) from the latest spreadsheet check - shown before an import; never blocks."""
    from .. import checker
    res = checker.load()
    if res is None:
        return "The spreadsheets have not been checked yet (python -m app.checker, or the Spreadsheet check page).", 0
    n = res.counts()
    errors, decisions = n["error"]["open"], n["decision"]["open"]
    msg = (f"Spreadsheet check {res.ran_at}: {errors} open error(s), {decisions} decision(s), "
           f"{n['warning']['open']} warning(s).")
    changed = checker.sources_changed(res)
    if changed:
        msg += " The " + " and ".join(changed) + " workbook changed since - run the check again."
    return msg, errors + (1 if changed else 0)


def backup_database(label: str) -> Path | None:
    """Copy the database to data/backups/subscriptions-before-<label>-<stamp>.db (None: nothing to back up).
    The backup is a SQLite file; from PostgreSQL it is copied table by table (app/dbcopy.py)."""
    import sqlite3
    from sqlalchemy.engine import make_url
    url = make_url(config.database_url())
    dst = config.DATA_DIR / "backups" / f"subscriptions-before-{label}-{dt.datetime.now():%Y%m%d-%H%M%S}.db"
    if url.get_backend_name() != "sqlite":
        from ..dbcopy import copy_engines, schema_revision
        engine = make_engine(url.render_as_string(hide_password=False))
        try:
            if schema_revision(engine) is None:
                return None                                  # empty database
            dst.parent.mkdir(parents=True, exist_ok=True)
            out = make_engine(f"sqlite:///{dst.as_posix()}")
            try:
                copy_engines(engine, out)
            except Exception:
                out.dispose()
                dst.unlink(missing_ok=True)
                raise
            out.dispose()
            return dst
        finally:
            engine.dispose()
    db = Path(url.database) if url.database else None
    if db is None or not db.exists() or db.stat().st_size == 0:
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    src, out = sqlite3.connect(db), sqlite3.connect(dst)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dst


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", help="workbook path (default: the one in data/source)")
    ap.add_argument("--force", action="store_true", help="re-import even if the app has recorded changes")
    ap.add_argument("--maintenance-only", action="store_true",
                    help="rebuild only the maintenance tables (keeps subscriptions and their app changes)")
    ap.add_argument("--yes", action="store_true", help="import even if the spreadsheet check has open errors")
    args = ap.parse_args(argv)
    msg, blocking = check_status()
    print(msg)
    if blocking and not args.yes:
        answer = input("Import anyway? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            raise SystemExit("Import cancelled - fix the spreadsheet issues first (or re-run with --yes).")
    try:
        backup = backup_database("maintenance-import" if args.maintenance_only else "import")
        if backup:
            print(f"Backup: {backup}")
        if args.maintenance_only:
            r = run_maintenance_import(force=args.force)
            print(f"Maintenance reloaded: {r.db_counts}")
            print(f"GRAND TOTAL vs database: {r.maintenance_totals}")
            print(f"Revenue/deferred (sheet, db): {r.maintenance_revenue}")
            print(f"Invoices: {r.maintenance_invoices}  Notes: {len(r.notes)}")
            return
        r = run_import(args.source, force=args.force)
    except ImportRefused as e:
        raise SystemExit(f"Import refused: {e}")
    print(f"Imported: {r.db_counts}")
    print(f"Notes: {len(r.notes)}  Reconciliation differences: {len(r.reconciliation)}")
    print(f"Report: {config.REPORT_DIR}")


if __name__ == "__main__":
    main()
