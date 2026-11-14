"""Contract documents: a link to the contract file on SharePoint per term / maintenance line, and a per-customer list
of those links (CSV). Same pattern as services.py (validate, change, audit, Outcome); run through
services.run(..., commit=True).

The app stores only the link. It never connects to SharePoint: the file opens in the browser with the viewer's own
SharePoint access."""
from __future__ import annotations

import csv
import datetime as dt
import io
import os
import re
from urllib.parse import unquote, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import csvsafe, metrics
from .models import ContractDocument, DocType, MaintenanceContract, Term
from .money import cents
from .services import Outcome, ServiceError, _audit, get_customer

ENTITY = "document"
DOC_TYPE_LABELS = {"SIGNED_CONTRACT": "Signed contract", "ORDER_FORM": "Order form", "AMENDMENT": "Amendment",
                   "OTHER": "Other"}


def allowed_hosts() -> set[str]:
    """Optional SP_ALLOWED_HOSTS (comma-separated) restricts links to the company's SharePoint host(s)."""
    return {h.strip().lower() for h in (os.environ.get("SP_ALLOWED_HOSTS") or "").split(",") if h.strip()}


def clean_filename(name: str) -> str:
    """Display name: last path part only, no control or reserved characters, max 200 chars."""
    name = re.split(r"[\\/]", name or "")[-1].strip()
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name).strip(". ")
    if not name:
        name = "document"
    stem, dot, ext = name.rpartition(".")
    if dot and len(name) > 200:
        name = stem[:200 - len(ext) - 1] + "." + ext
    return name[:200]


def _parent(s: Session, term_id: int | None, contract_id: int | None):
    """(parent, customer, sheet ref, label) of the term or maintenance line a document is attached to."""
    if (term_id is None) == (contract_id is None):
        raise ServiceError("A document belongs to exactly one term or maintenance line.")
    if term_id is not None:
        parent = s.get(Term, term_id)
        if parent is None:
            raise ServiceError(f"Term {term_id} not found.")
        return parent, parent.subscription.customer, parent.source_ref, f"term {parent.id}"
    parent = s.get(MaintenanceContract, contract_id)
    if parent is None:
        raise ServiceError(f"Maintenance contract {contract_id} not found.")
    return parent, parent.customer, parent.source_ref, f"maintenance line #{parent.id}"


def check_sharepoint_url(url: str) -> str:
    """https link on a *.sharepoint.com host (restricted to SP_ALLOWED_HOSTS when set). Returned stripped."""
    url = (url or "").strip()
    if not url:
        raise ServiceError("Paste the SharePoint link of the file.")
    if len(url) > 1000:
        raise ServiceError("That link is longer than 1,000 characters.")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host == "sharepoint.com" or host.endswith(".sharepoint.com")):
        raise ServiceError("Only https links to SharePoint (…sharepoint.com) can be linked.")
    if parts.username or parts.password:
        raise ServiceError("The link must not contain a user name or password.")
    allowed = allowed_hosts()
    if allowed and host not in allowed:
        raise ServiceError(f"Links must point to {', '.join(sorted(allowed))}.")
    return url


def name_from_url(url: str) -> str:
    """File name from a direct SharePoint path (…/Contract.pdf); sharing links (/:b:/s/…/Ab12…) have none."""
    last = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    return clean_filename(last) if "." in last and not last.lower().endswith(".aspx") else "SharePoint document"


def link(s: Session, actor: str, url: str, *, term_id: int | None = None, contract_id: int | None = None,
         name: str | None = None, doc_type: DocType | str = DocType.SIGNED_CONTRACT,
         note: str | None = None) -> Outcome:
    """Attach a SharePoint link to a term / maintenance line."""
    parent, c, ref, label = _parent(s, term_id, contract_id)
    url = check_sharepoint_url(url)
    dup = next((d for d in parent.documents if d.url == url), None)
    if dup:
        raise ServiceError(f"That link is already attached to {label} (as '{dup.filename}').")
    title = clean_filename(name) if (name or "").strip() else name_from_url(url)
    doc = ContractDocument(customer=c, term=parent if term_id is not None else None,
                           maintenance_contract=parent if contract_id is not None else None,
                           link_qb=c.qb_number, link_source_ref=ref, filename=title, url=url,
                           doc_type=DocType(doc_type), note=(note or "").strip()[:300] or None,
                           uploaded_by=actor, uploaded_at=dt.datetime.now())
    s.add(doc)
    s.flush()
    line = f"Linked SharePoint file '{doc.filename}' ({DOC_TYPE_LABELS[doc.doc_type.value]}) to {label}"
    _audit(s, actor, "link", ENTITY, doc.id, c.qb_number, line, {"url": url})
    return Outcome(line, c.qb_number, [line], entity_id=doc.id)


def delete(s: Session, actor: str, doc_id: int) -> Outcome:
    doc = s.get(ContractDocument, doc_id)
    if doc is None:
        raise ServiceError("Document not found.")
    qb, url = doc.customer.qb_number, doc.url
    where = (f"term {doc.term_id}" if doc.term_id else f"maintenance line #{doc.maintenance_contract_id}"
             if doc.maintenance_contract_id else "customer (unlinked)")
    line = f"Removed SharePoint link '{doc.filename}' from {where} (the SharePoint file is untouched)"
    s.delete(doc)
    s.flush()
    _audit(s, actor, "delete", ENTITY, doc_id, qb, line, {"url": url})
    return Outcome(line, qb, [line])


# ------------------------------------------------------------------------------------------------ customer list

def customer_links_csv(s: Session, qb: int) -> tuple[str, str, int]:
    """(file name, CSV text, document count): every contract link of a customer with the term / maintenance line it
    belongs to and its numbers (period, TCV, ARR, sheet ref)."""
    c = get_customer(s, qb)
    docs = list(s.scalars(select(ContractDocument).where(ContractDocument.customer_id == c.id)
                          .order_by(ContractDocument.term_id, ContractDocument.maintenance_contract_id,
                                    ContractDocument.uploaded_at)))
    if not docs:
        raise ServiceError(f"No contract documents are linked for {qb}.")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["customer_number", "customer", "attached_to", "platform", "subscription", "term", "kind", "start",
                "end", "tcv", "arr", "sheet_ref", "doc_type", "name", "sharepoint_url", "linked_by", "linked_at",
                "note"])
    for d in docs:
        t, mc = d.term, d.maintenance_contract
        txt = csvsafe.text                                                  # text cells: no Excel formulas
        w.writerow([c.qb_number, txt(c.name), "term" if t else "maintenance" if mc else "unlinked",
                    t.subscription.platform.value if t else "", t.subscription.id if t else "",
                    t.id if t else (f"maintenance #{mc.id}" if mc else ""), t.kind.value if t else "",
                    t.start_date.isoformat() if t else "", t.end_date.isoformat() if t else "",
                    cents(t.tcv) if t else "", cents(metrics.term_arr(t)) if t else "",
                    txt(d.link_source_ref), DOC_TYPE_LABELS[d.doc_type.value], txt(d.filename), txt(d.url),
                    txt(d.uploaded_by), d.uploaded_at.isoformat(timespec="seconds"), txt(d.note)])
    name = re.sub(r'[\\/<>:"|?*\x00-\x1f]', "_", f"{c.qb_number} {c.name} - contract links").strip(". ")
    return f"{name}.csv", out.getvalue(), len(docs)
