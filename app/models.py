"""Normalised schema. Derived figures (ARR, ARR by year, CARR, statuses, revenue schedule) are computed in
metrics.py, never stored - except `term.arr_override`, which only records a legacy ARR the sheet used that differs
from TCV / term."""
from __future__ import annotations

import datetime as dt
import enum
from decimal import Decimal

from sqlalchemy import (Boolean, CheckConstraint, Date, DateTime, Enum, Float, ForeignKey, Integer, Numeric, String,
                        Text, UniqueConstraint, event)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .money import ZERO, cents

# Money: exact decimal, 2 places (NUMERIC(14,2) in PostgreSQL). Assignments are converted to Decimal cents by
# _money_hooks below.
Money = Numeric(14, 2, asdecimal=True)


def _enum(e):
    return Enum(e, native_enum=False, validate_strings=True, length=24)


class Platform(str, enum.Enum):
    ON_PREM = "ON_PREM"
    CLOUD = "CLOUD"


class OpenedVia(str, enum.Enum):
    NET_NEW = "NET_NEW"
    CONVERSION = "CONVERSION"


class TermKind(str, enum.Enum):
    INITIAL = "INITIAL"          # net new (sheet section A)
    CONVERSION = "CONVERSION"    # perpetual -> subscription (B)
    ADD_ON = "ADD_ON"            # modules / plants / users (C)
    RENEWAL = "RENEWAL"          # (D)
    AMENDMENT = "AMENDMENT"      # revised term replacing another mid-term


class TerminationReason(str, enum.Enum):
    CHURN = "CHURN"
    TERMINATED = "TERMINATED"
    FINAL_PERIOD = "FINAL_PERIOD"
    COLLECTIONS = "COLLECTIONS"
    CANCELLED = "CANCELLED"


class CheckResult(str, enum.Enum):
    YES = "YES"
    DONE = "DONE"
    TO_CHECK = "TO_CHECK"
    MISMATCHED = "MISMATCHED"
    MISSING = "MISSING"
    NA = "NA"
    OTHER = "OTHER"


class Customer(Base):
    __tablename__ = "customer"
    id: Mapped[int] = mapped_column(primary_key=True)
    qb_number: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    has_perpetual_license: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="customer", order_by="Subscription.id")
    maintenance_contracts: Mapped[list["MaintenanceContract"]] = relationship(
        back_populates="customer", order_by="MaintenanceContract.id")
    documents: Mapped[list["ContractDocument"]] = relationship(back_populates="customer",
                                                               order_by="ContractDocument.uploaded_at")
    __table_args__ = (CheckConstraint("qb_number BETWEEN 80000 AND 89999", name="ck_customer_qb_number"),)


class AccountExec(Base):
    __tablename__ = "account_exec"
    id: Mapped[int] = mapped_column(primary_key=True)
    initials: Mapped[str] = mapped_column(String(8), unique=True)
    name: Mapped[str | None] = mapped_column(String(120))


class Reseller(Base):
    __tablename__ = "reseller"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    commission_rate: Mapped[float | None] = mapped_column(Float)


class Subscription(Base):
    """One licence line of a customer on one platform; a sequence of terms."""
    __tablename__ = "subscription"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"), index=True)
    platform: Mapped[Platform] = mapped_column(_enum(Platform))
    opened_via: Mapped[OpenedVia] = mapped_column(_enum(OpenedVia))
    license_category: Mapped[str | None] = mapped_column(String(40))
    hybrid_class: Mapped[str | None] = mapped_column(String(60))
    notes: Mapped[str | None] = mapped_column(Text)
    customer: Mapped[Customer] = relationship(back_populates="subscriptions")
    terms: Mapped[list["Term"]] = relationship(back_populates="subscription", order_by="Term.start_date",
                                               cascade="all, delete-orphan")
    terminations: Mapped[list["Termination"]] = relationship(back_populates="subscription",
                                                             cascade="all, delete-orphan")


class Term(Base):
    """One contracted period with its value. ARR = tcv / (term_months / 12) unless arr_override is set."""
    __tablename__ = "term"
    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscription.id"), index=True)
    kind: Mapped[TermKind] = mapped_column(_enum(TermKind))
    signed_date: Mapped[dt.date | None] = mapped_column(Date)
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date] = mapped_column(Date)
    tcv: Mapped[Decimal] = mapped_column(Money)
    term_months: Mapped[float] = mapped_column(Float)
    arr_override: Mapped[Decimal | None] = mapped_column(Money)
    arr_override_reason: Mapped[str | None] = mapped_column(String(200))
    is_revised: Mapped[bool] = mapped_column(Boolean, default=False)
    payment_terms: Mapped[str | None] = mapped_column(String(60))
    ae_id: Mapped[int | None] = mapped_column(ForeignKey("account_exec.id"))
    reseller_id: Mapped[int | None] = mapped_column(ForeignKey("reseller.id"))
    golive_date: Mapped[dt.date | None] = mapped_column(Date)
    golive_note: Mapped[str | None] = mapped_column(String(200))
    transactional_volume: Mapped[str | None] = mapped_column(String(60))
    renewal_note: Mapped[str | None] = mapped_column(String(120))
    # Uplift of the Projected Renewal once this term has ended unrenewed (0.05 = 5%); None = the app default.
    projected_uplift: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    billing_period: Mapped[str | None] = mapped_column(String(60))
    recent_invoice: Mapped[str | None] = mapped_column(String(120))
    next_action: Mapped[str | None] = mapped_column(String(200))
    source_ref: Mapped[str | None] = mapped_column(String(40))
    notes: Mapped[str | None] = mapped_column(Text)

    subscription: Mapped[Subscription] = relationship(back_populates="terms")
    ae: Mapped[AccountExec | None] = relationship()
    reseller: Mapped[Reseller | None] = relationship()
    checks: Mapped[list["ControlCheck"]] = relationship(back_populates="term", cascade="all, delete-orphan")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="term", cascade="all, delete-orphan",
                                                     order_by="Invoice.billing_year")
    documents: Mapped[list["ContractDocument"]] = relationship(back_populates="term",
                                                               order_by="ContractDocument.uploaded_at")
    superseded_by: Mapped[list["TermSupersession"]] = relationship(
        foreign_keys="TermSupersession.old_term_id", back_populates="old_term", cascade="all, delete-orphan")
    supersedes: Mapped[list["TermSupersession"]] = relationship(
        foreign_keys="TermSupersession.new_term_id", back_populates="new_term", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="ck_term_dates"),
        CheckConstraint("tcv >= 0", name="ck_term_tcv"),
        CheckConstraint("term_months > 0", name="ck_term_months"),
        CheckConstraint("arr_override IS NULL OR arr_override >= 0", name="ck_term_arr_override"),
        CheckConstraint("projected_uplift IS NULL OR (projected_uplift >= 0 AND projected_uplift <= 0.5)",
                        name="ck_term_projected_uplift"),
    )


class TermSupersession(Base):
    """new_term replaces old_term from new_term.start_date (renewal roll-up of base + add-ons, amendment)."""
    __tablename__ = "term_supersession"
    id: Mapped[int] = mapped_column(primary_key=True)
    new_term_id: Mapped[int] = mapped_column(ForeignKey("term.id"), index=True)
    old_term_id: Mapped[int] = mapped_column(ForeignKey("term.id"), index=True)
    new_term: Mapped[Term] = relationship(foreign_keys=[new_term_id], back_populates="supersedes")
    old_term: Mapped[Term] = relationship(foreign_keys=[old_term_id], back_populates="superseded_by")
    __table_args__ = (UniqueConstraint("new_term_id", "old_term_id"),
                      CheckConstraint("new_term_id <> old_term_id", name="ck_supersession_self"))


class Termination(Base):
    __tablename__ = "termination"
    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscription.id"), index=True)
    cancelled_on: Mapped[dt.date] = mapped_column(Date)
    reason: Mapped[TerminationReason] = mapped_column(_enum(TerminationReason))
    last_term_id: Mapped[int | None] = mapped_column(ForeignKey("term.id"))
    note: Mapped[str | None] = mapped_column(Text)
    source_ref: Mapped[str | None] = mapped_column(String(40))
    subscription: Mapped[Subscription] = relationship(back_populates="terminations")
    # no rows = the whole subscription is cancelled; rows = only these contracts (terms) end, the others keep running
    scope: Mapped[list["TerminationTerm"]] = relationship(back_populates="termination", cascade="all, delete-orphan")

    @property
    def term_ids(self) -> set[int]:
        return {x.term_id for x in self.scope}


class TerminationTerm(Base):
    """A contract (term) ended by a termination that does not cancel the whole subscription."""
    __tablename__ = "termination_term"
    id: Mapped[int] = mapped_column(primary_key=True)
    termination_id: Mapped[int] = mapped_column(ForeignKey("termination.id"), index=True)
    term_id: Mapped[int] = mapped_column(ForeignKey("term.id"), index=True)
    termination: Mapped[Termination] = relationship(back_populates="scope")
    term: Mapped["Term"] = relationship()


class ControlCheckType(Base):
    __tablename__ = "control_check_type"
    code: Mapped[str] = mapped_column(String(40), primary_key=True)
    label: Mapped[str] = mapped_column(String(160))
    owner: Mapped[str | None] = mapped_column(String(60))
    sort: Mapped[int] = mapped_column(Integer, default=0)


class ControlCheck(Base):
    __tablename__ = "control_check"
    id: Mapped[int] = mapped_column(primary_key=True)
    term_id: Mapped[int] = mapped_column(ForeignKey("term.id"), index=True)
    type_code: Mapped[str] = mapped_column(ForeignKey("control_check_type.code"))
    result: Mapped[CheckResult] = mapped_column(_enum(CheckResult))
    note: Mapped[str | None] = mapped_column(String(200))
    checked_by: Mapped[str | None] = mapped_column(String(60))
    checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    term: Mapped[Term] = relationship(back_populates="checks")
    type: Mapped[ControlCheckType] = relationship()
    __table_args__ = (UniqueConstraint("term_id", "type_code"),)


class Invoice(Base):
    __tablename__ = "invoice"
    id: Mapped[int] = mapped_column(primary_key=True)
    term_id: Mapped[int] = mapped_column(ForeignKey("term.id"), index=True)
    billing_year: Mapped[int] = mapped_column(Integer)
    invoice_date: Mapped[dt.date | None] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Money)
    reference: Mapped[str | None] = mapped_column(String(60))
    term: Mapped[Term] = relationship(back_populates="invoices")
    __table_args__ = (CheckConstraint("amount >= 0", name="ck_invoice_amount"),)


class NonRecurringFee(Base):
    """One-off fees the sheet recorded among subscriptions ("not a subscription"); never part of ARR."""
    __tablename__ = "non_recurring_fee"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"), index=True)
    platform: Mapped[Platform] = mapped_column(_enum(Platform))
    description: Mapped[str | None] = mapped_column(String(200))
    amount: Mapped[Decimal] = mapped_column(Money)
    start_date: Mapped[dt.date | None] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    source_ref: Mapped[str | None] = mapped_column(String(40))
    customer: Mapped[Customer] = relationship()


class MaintenanceBlock(str, enum.Enum):
    """Block of the maintenance register (MAINTENANCE-WORKBOOK.md §3)."""
    PERPETUAL_LIVE = "PERPETUAL_LIVE"
    NOT_LIVE_PAYING = "NOT_LIVE_PAYING"
    NOT_LIVE_NOT_PAYING = "NOT_LIVE_NOT_PAYING"
    SUBSCRIPTION_ONPREM = "SUBSCRIPTION_ONPREM"
    CONVERTED_ONPREM = "CONVERTED_ONPREM"
    SUBSCRIPTION_CLOUD = "SUBSCRIPTION_CLOUD"
    CONVERTED_CLOUD = "CONVERTED_CLOUD"
    OTHERS = "OTHERS"
    CLOSED = "CLOSED"


BLOCK_LABELS = {
    "PERPETUAL_LIVE": "Perpetual - live",
    "NOT_LIVE_PAYING": "Not live, paying maintenance",
    "NOT_LIVE_NOT_PAYING": "Not live, not paying yet",
    "SUBSCRIPTION_ONPREM": "Subscription (On-Prem)",
    "CONVERTED_ONPREM": "Converted perpetual -> On-Prem subscription",
    "SUBSCRIPTION_CLOUD": "Subscription (Cloud)",
    "CONVERTED_CLOUD": "Converted perpetual -> Cloud subscription",
    "OTHERS": "Others (partners)",
    "CLOSED": "Closed / terminated / churn",
}


class LicenseProduct(str, enum.Enum):
    PERPETUAL = "PERPETUAL"
    ON_PREMISE = "ON_PREMISE"
    CLOUD = "CLOUD"


class MaintenanceEndReason(str, enum.Enum):
    CONVERTED = "CONVERTED"
    TERMINATED = "TERMINATED"
    CHURN = "CHURN"
    INACTIVE = "INACTIVE"
    OTHER = "OTHER"


class PurchaseKind(str, enum.Enum):
    ORIGINAL = "ORIGINAL"            # the perpetual licence itself
    MODULE = "MODULE"
    PLANT = "PLANT"
    CARRIER = "CARRIER"
    DECOMMISSION = "DECOMMISSION"    # plants / modules given up (negative maintenance)


class FeeSource(str, enum.Enum):
    IMPORT = "IMPORT"
    ROLL = "ROLL"                    # annual uplift
    MANUAL = "MANUAL"
    PURCHASE = "PURCHASE"            # additional purchase / decommission changed it
    STOP = "STOP"                    # explicit 0 on conversion / termination


class MaintenanceContract(Base):
    """One perpetual-licence maintenance line of a customer (a row of the maintenance register).
    Pricing figures (Total Current License, Total Maintenance, Maintenance %) and the revenue schedule are computed
    from `license_purchase` and `maintenance_fee`."""
    __tablename__ = "maintenance_contract"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"), index=True)
    parent_contract_id: Mapped[int | None] = mapped_column(ForeignKey("maintenance_contract.id"))
    block: Mapped[MaintenanceBlock] = mapped_column(_enum(MaintenanceBlock))
    product: Mapped[LicenseProduct | None] = mapped_column(_enum(LicenseProduct))
    description: Mapped[str | None] = mapped_column(String(120))     # e.g. add-on line "Addl Plant"
    contract_year: Mapped[int | None] = mapped_column(Integer)
    contract_date: Mapped[dt.date | None] = mapped_column(Date)
    go_live: Mapped[bool] = mapped_column(Boolean, default=False)
    golive_date: Mapped[dt.date | None] = mapped_column(Date)
    golive_note: Mapped[str | None] = mapped_column(String(160))
    anniversary_month: Mapped[int | None] = mapped_column(Integer)   # 1-12; None = not applicable
    calendar_billing: Mapped[bool] = mapped_column(Boolean, default=False)
    converted: Mapped[bool] = mapped_column(Boolean, default=False)
    converted_subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscription.id"))
    ended_on: Mapped[dt.date | None] = mapped_column(Date)
    end_reason: Mapped[MaintenanceEndReason | None] = mapped_column(_enum(MaintenanceEndReason))
    end_note: Mapped[str | None] = mapped_column(String(300))
    notes: Mapped[str | None] = mapped_column(Text)
    source_ref: Mapped[str | None] = mapped_column(String(40))
    customer: Mapped[Customer] = relationship(back_populates="maintenance_contracts")
    parent: Mapped["MaintenanceContract | None"] = relationship(remote_side="MaintenanceContract.id",
                                                                back_populates="addons")
    addons: Mapped[list["MaintenanceContract"]] = relationship(back_populates="parent")
    fees: Mapped[list["MaintenanceFee"]] = relationship(back_populates="contract", cascade="all, delete-orphan",
                                                        order_by="MaintenanceFee.year")
    purchases: Mapped[list["LicensePurchase"]] = relationship(back_populates="contract", cascade="all, delete-orphan",
                                                              order_by="LicensePurchase.id")
    invoices: Mapped[list["MaintenanceInvoice"]] = relationship(back_populates="contract",
                                                                cascade="all, delete-orphan",
                                                                order_by="MaintenanceInvoice.invoice_date")
    documents: Mapped[list["ContractDocument"]] = relationship(back_populates="maintenance_contract",
                                                               order_by="ContractDocument.uploaded_at")
    __table_args__ = (CheckConstraint("anniversary_month IS NULL OR anniversary_month BETWEEN 1 AND 12",
                                      name="ck_mc_anniversary"),)


class LicensePurchase(Base):
    """Licence history: the original perpetual licence and every later module / plant / carrier purchase or
    decommission, with the maintenance it added (negative when given up)."""
    __tablename__ = "license_purchase"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("maintenance_contract.id"), index=True)
    kind: Mapped[PurchaseKind] = mapped_column(_enum(PurchaseKind))
    description: Mapped[str | None] = mapped_column(String(200))
    quantity: Mapped[float | None] = mapped_column(Float)
    list_price: Mapped[Decimal | None] = mapped_column(Money)
    discount: Mapped[Decimal | None] = mapped_column(Money)         # negative amount
    amount: Mapped[Decimal] = mapped_column(Money, default=ZERO)     # net licence value added (negative = removed)
    maintenance_amount: Mapped[Decimal | None] = mapped_column(Money)  # annual maintenance it added / removed
    golive_date: Mapped[dt.date | None] = mapped_column(Date)
    golive_note: Mapped[str | None] = mapped_column(String(120))
    effective_year: Mapped[int | None] = mapped_column(Integer)     # first contract year (end-year key) affected
    contract: Mapped[MaintenanceContract] = relationship(back_populates="purchases")


class MaintenanceFee(Base):
    """Fee of one maintenance contract year. `year` = the year the contract year ends in ("2025-2026" -> 2026).
    No row = not billed / not entered yet; amount 0 = explicitly stopped (conversion, termination)."""
    __tablename__ = "maintenance_fee"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("maintenance_contract.id"), index=True)
    year: Mapped[int] = mapped_column(Integer)
    amount: Mapped[Decimal] = mapped_column(Money)
    prorated: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[FeeSource] = mapped_column(_enum(FeeSource), default=FeeSource.IMPORT)
    note: Mapped[str | None] = mapped_column(String(200))
    contract: Mapped[MaintenanceContract] = relationship(back_populates="fees")
    __table_args__ = (UniqueConstraint("contract_id", "year"), CheckConstraint("amount >= 0", name="ck_mfee_amount"))


class MaintenanceInvoice(Base):
    """QuickBooks maintenance invoice (item 1000-MT), incl. advance billing raised before the contract year."""
    __tablename__ = "maintenance_invoice"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("maintenance_contract.id"), index=True)
    contract_year: Mapped[int] = mapped_column(Integer)             # end-year key, like maintenance_fee.year
    invoice_no: Mapped[str | None] = mapped_column(String(40))
    reseller_invoice: Mapped[bool] = mapped_column(Boolean, default=False)
    invoice_date: Mapped[dt.date | None] = mapped_column(Date)
    due_date: Mapped[dt.date | None] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Money)
    memo: Mapped[str | None] = mapped_column(String(200))
    source_ref: Mapped[str | None] = mapped_column(String(60))
    contract: Mapped[MaintenanceContract] = relationship(back_populates="invoices")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now, index=True)
    actor: Mapped[str] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(40))
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    customer_qb: Mapped[int | None] = mapped_column(Integer, index=True)
    summary: Mapped[str] = mapped_column(String(400))
    detail: Mapped[str | None] = mapped_column(Text)        # JSON before/after
    undone_at: Mapped[dt.datetime | None] = mapped_column(DateTime)       # set when this change was undone
    undone_by: Mapped[str | None] = mapped_column(String(60))
    undo_of_id: Mapped[int | None] = mapped_column(Integer)               # on an "undo" row: the change it reversed
    changes: Mapped[list["ChangeRecord"]] = relationship(back_populates="audit", order_by="ChangeRecord.seq",
                                                         cascade="all, delete-orphan")


class ChangeRecord(Base):
    """One row inserted / updated / deleted by an audited change (net effect per row), so it can be undone (undo.py).
    before / after: JSON of every column ({} for the side that does not exist)."""
    __tablename__ = "change_record"
    id: Mapped[int] = mapped_column(primary_key=True)
    audit_id: Mapped[int] = mapped_column(ForeignKey("audit_log.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    table_name: Mapped[str] = mapped_column(String(60))
    row_pk: Mapped[int] = mapped_column(Integer)
    op: Mapped[str] = mapped_column(String(8))              # insert | update | delete
    before: Mapped[str | None] = mapped_column(Text)
    after: Mapped[str | None] = mapped_column(Text)
    audit: Mapped[AuditLog] = relationship(back_populates="changes")


class DocType(str, enum.Enum):
    SIGNED_CONTRACT = "SIGNED_CONTRACT"
    ORDER_FORM = "ORDER_FORM"
    AMENDMENT = "AMENDMENT"
    OTHER = "OTHER"


class ContractDocument(Base):
    """A contract for a term or a maintenance line: a link to the file on SharePoint (`url`). The app stores only the
    link; the file opens with the viewer's own SharePoint access.
    Neither term nor maintenance line = customer-level ("unlinked", e.g. a re-import could not find the term again).
    link_qb + link_source_ref (the sheet ref of the term / maintenance line when linked) let a re-import re-attach it."""
    __tablename__ = "contract_document"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customer.id"), index=True)
    term_id: Mapped[int | None] = mapped_column(ForeignKey("term.id"), index=True)
    maintenance_contract_id: Mapped[int | None] = mapped_column(ForeignKey("maintenance_contract.id"), index=True)
    link_qb: Mapped[int] = mapped_column(Integer)
    link_source_ref: Mapped[str | None] = mapped_column(String(60))
    filename: Mapped[str] = mapped_column(String(200))             # display name
    url: Mapped[str] = mapped_column(String(1000))                 # the SharePoint link (source of truth)
    doc_type: Mapped[DocType] = mapped_column(_enum(DocType), default=DocType.SIGNED_CONTRACT)
    note: Mapped[str | None] = mapped_column(String(300))
    uploaded_by: Mapped[str] = mapped_column(String(60))           # who linked it
    uploaded_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.now)
    customer: Mapped[Customer] = relationship(back_populates="documents")
    term: Mapped[Term | None] = relationship(back_populates="documents")
    maintenance_contract: Mapped[MaintenanceContract | None] = relationship(back_populates="documents")
    __table_args__ = (CheckConstraint("term_id IS NULL OR maintenance_contract_id IS NULL",
                                      name="ck_document_one_parent"),)


# ------------------------------------------------------------------------------------------------ money hooks

MONEY_COLUMNS = [(Term, "tcv"), (Term, "arr_override"), (Invoice, "amount"), (NonRecurringFee, "amount"),
                 (LicensePurchase, "list_price"), (LicensePurchase, "discount"), (LicensePurchase, "amount"),
                 (LicensePurchase, "maintenance_amount"), (MaintenanceFee, "amount"), (MaintenanceInvoice, "amount")]


def _to_cents(target, value, oldvalue, initiator):
    return cents(value)


for _cls, _attr in MONEY_COLUMNS:
    event.listen(getattr(_cls, _attr), "set", _to_cents, retval=True)
