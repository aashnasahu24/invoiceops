"""SQLAlchemy ORM models for InvoiceOps.

The ORM layer is deliberately separate from Pydantic schemas:
- schemas.py describes validated application/API data.
- models.py describes persisted relational data.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from .schemas import (
    AmountBasis,
    CheckStatus,
    Decision,
    DocumentType,
    POStatus,
    ProcessingStatus,
    ReviewActionType,  # re-exported: historically importable from this module
    StageName,
    StageStatus,
    VendorStatus,
)

__all__ = ["ReviewActionType"]


def enum_column(enum_cls: type[Enum], name: str) -> SAEnum:
    """Persist enum values (e.g. 'APPROVED'), not Python member names."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every backend.

    SQLite drops tzinfo on read, which makes ``now(utc) - row.started_at`` raise
    ``TypeError`` (naive vs aware). This normalises both directions to UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime supplied; use timezone-aware UTC datetimes")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )


class VendorORM(TimestampMixin, Base):
    __tablename__ = "vendors"

    vendor_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    legal_name: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    tax_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    country: Mapped[str] = mapped_column(String(2), default="IN", nullable=False)
    status: Mapped[VendorStatus] = mapped_column(
        enum_column(VendorStatus, "vendor_status"), nullable=False
    )

    purchase_orders: Mapped[list[PurchaseOrderORM]] = relationship(
        back_populates="vendor", passive_deletes=True
    )
    invoices: Mapped[list[InvoiceORM]] = relationship(back_populates="vendor")

    __table_args__ = (
        CheckConstraint("length(trim(legal_name)) > 0", name="ck_vendor_legal_name_nonempty"),
        Index("ix_vendors_legal_name", "legal_name"),
    )


class PurchaseOrderORM(TimestampMixin, Base):
    __tablename__ = "purchase_orders"

    po_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    po_number: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    vendor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("vendors.vendor_id", ondelete="RESTRICT"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    amount_basis: Mapped[AmountBasis] = mapped_column(
        enum_column(AmountBasis, "po_amount_basis"), nullable=False
    )
    status: Mapped[POStatus] = mapped_column(
        enum_column(POStatus, "po_status"), nullable=False
    )

    vendor: Mapped[VendorORM] = relationship(back_populates="purchase_orders")
    line_items: Mapped[list[POLineItemORM]] = relationship(
        back_populates="purchase_order", cascade="all, delete-orphan"
    )
    invoices: Mapped[list[InvoiceORM]] = relationship(back_populates="purchase_order")

    __table_args__ = (
        CheckConstraint("total_amount > 0", name="ck_po_total_positive"),
        CheckConstraint("length(currency) = 3", name="ck_po_currency_length"),
        Index("ix_purchase_orders_vendor_status", "vendor_id", "status"),
    )


class POLineItemORM(Base):
    __tablename__ = "po_line_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    po_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("purchase_orders.po_id", ondelete="CASCADE"), nullable=False
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    sku: Mapped[str | None] = mapped_column(String(100))
    quantity_ordered: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)

    purchase_order: Mapped[PurchaseOrderORM] = relationship(back_populates="line_items")

    __table_args__ = (
        UniqueConstraint("po_id", "line_number", name="uq_po_line_number"),
        CheckConstraint("quantity_ordered > 0", name="ck_po_line_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_po_line_unit_price_nonnegative"),
        CheckConstraint("line_total >= 0", name="ck_po_line_total_nonnegative"),
    )


class InvoiceORM(TimestampMixin, Base):
    __tablename__ = "invoices"

    invoice_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    vendor_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("vendors.vendor_id", ondelete="RESTRICT")
    )
    po_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("purchase_orders.po_id", ondelete="RESTRICT")
    )

    # Keep original and normalised values. The latter supports stable duplicate checks.
    invoice_number: Mapped[str | None] = mapped_column(String(128))
    invoice_number_normalized: Mapped[str | None] = mapped_column(String(128))
    extracted_po_number: Mapped[str | None] = mapped_column(String(64))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    extracted_vendor_name: Mapped[str | None] = mapped_column(String(255))
    extracted_vendor_tax_id: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str | None] = mapped_column(String(3))

    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    tax_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    shipping: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    discount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    grand_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    # subtotal + shipping - discount; the amount that consumes PO balance when approved.
    net_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))

    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_sha256: Mapped[str | None] = mapped_column(String(64), unique=True)
    document_type: Mapped[DocumentType | None] = mapped_column(
        enum_column(DocumentType, "document_type")
    )

    extracted_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    field_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    extraction_warnings: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # Effective decision: the automated one, or the reviewer's override. The
    # original automated decision always stays on ``workflow_runs.decision``.
    current_decision: Mapped[Decision | None] = mapped_column(
        enum_column(Decision, "invoice_current_decision")
    )
    # Only *active* invoices hold the (vendor, invoice number) identity. A
    # returned/withdrawn invoice is set inactive so a corrected resubmission
    # with the same number is allowed instead of being blocked as a duplicate.
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    supersedes_invoice_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("invoices.invoice_id", ondelete="SET NULL")
    )

    vendor: Mapped[VendorORM | None] = relationship(back_populates="invoices")
    purchase_order: Mapped[PurchaseOrderORM | None] = relationship(back_populates="invoices")
    line_items: Mapped[list[InvoiceLineItemORM]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )
    workflow_runs: Mapped[list[WorkflowRunORM]] = relationship(back_populates="invoice")

    __table_args__ = (
        Index(
            "uq_invoice_active_vendor_number",
            "vendor_id",
            "invoice_number_normalized",
            unique=True,
            sqlite_where=text("is_active = 1"),
            postgresql_where=text("is_active"),
        ),
        CheckConstraint("net_amount IS NULL OR net_amount >= 0", name="ck_invoice_net_nonnegative"),
        CheckConstraint("subtotal IS NULL OR subtotal >= 0", name="ck_invoice_subtotal_nonnegative"),
        CheckConstraint("tax_total IS NULL OR tax_total >= 0", name="ck_invoice_tax_nonnegative"),
        CheckConstraint("shipping IS NULL OR shipping >= 0", name="ck_invoice_shipping_nonnegative"),
        CheckConstraint("discount IS NULL OR discount >= 0", name="ck_invoice_discount_nonnegative"),
        CheckConstraint("grand_total IS NULL OR grand_total >= 0", name="ck_invoice_total_nonnegative"),
        Index("ix_invoices_vendor_id", "vendor_id"),
        Index("ix_invoices_po_id", "po_id"),
        Index("ix_invoices_decision", "current_decision"),
    )


class InvoiceLineItemORM(Base):
    __tablename__ = "invoice_line_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("invoices.invoice_id", ondelete="CASCADE"), nullable=False
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    sku: Mapped[str | None] = mapped_column(String(100))
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    matched_po_line_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("po_line_items.id", ondelete="SET NULL")
    )

    invoice: Mapped[InvoiceORM] = relationship(back_populates="line_items")

    __table_args__ = (
        UniqueConstraint("invoice_id", "line_number", name="uq_invoice_line_number"),
        CheckConstraint("quantity > 0", name="ck_invoice_line_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_invoice_line_unit_price_nonnegative"),
        CheckConstraint("line_total >= 0", name="ck_invoice_line_total_nonnegative"),
        CheckConstraint("tax_rate IS NULL OR (tax_rate >= 0 AND tax_rate <= 100)", name="ck_invoice_line_tax_rate_range"),
    )


class WorkflowRunORM(Base):
    __tablename__ = "workflow_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Nullable so a duplicate upload can still be recorded without inserting a second invoice.
    invoice_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("invoices.invoice_id", ondelete="SET NULL")
    )
    source_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_file_sha256: Mapped[str | None] = mapped_column(String(64))
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        enum_column(ProcessingStatus, "processing_status"), nullable=False
    )
    decision: Mapped[Decision | None] = mapped_column(enum_column(Decision, "run_decision"))
    policy_version: Mapped[str] = mapped_column(String(32), default="v1", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    # Audit/debug trail: the text the LLM saw and what it returned.
    extracted_text: Mapped[str | None] = mapped_column(Text)
    llm_response: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    invoice: Mapped[InvoiceORM | None] = relationship(back_populates="workflow_runs")
    validation_checks: Mapped[list[ValidationCheckORM]] = relationship(
        back_populates="workflow_run", cascade="all, delete-orphan"
    )
    review_actions: Mapped[list[ReviewActionORM]] = relationship(
        back_populates="workflow_run", cascade="all, delete-orphan"
    )
    stages: Mapped[list[RunStageORM]] = relationship(
        back_populates="workflow_run",
        cascade="all, delete-orphan",
        order_by="RunStageORM.id",
    )

    __table_args__ = (
        Index("ix_workflow_runs_started_at", "started_at"),
        Index("ix_workflow_runs_decision_status", "decision", "processing_status"),
    )


class ValidationCheckORM(Base):
    __tablename__ = "validation_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    check_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[CheckStatus] = mapped_column(
        enum_column(CheckStatus, "validation_check_status"), nullable=False
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    workflow_run: Mapped[WorkflowRunORM] = relationship(back_populates="validation_checks")

    __table_args__ = (
        UniqueConstraint("run_id", "check_name", name="uq_run_check_name"),
        Index("ix_validation_checks_run_id", "run_id"),
    )


class ReviewActionORM(Base):
    __tablename__ = "review_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[ReviewActionType] = mapped_column(
        enum_column(ReviewActionType, "review_action_type"), nullable=False
    )
    reviewer: Mapped[str | None] = mapped_column(String(120))
    comments: Mapped[str | None] = mapped_column(Text)
    generated_email: Mapped[str | None] = mapped_column(Text)
    # Explicit snapshot of failed checks accepted by a reviewer. Original run
    # validation rows remain unchanged and continue to describe automation.
    overridden_checks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    workflow_run: Mapped[WorkflowRunORM] = relationship(back_populates="review_actions")


class RunStageORM(Base):
    """One row per pipeline stage per run; drives the live run view and history."""

    __tablename__ = "run_stages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflow_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[StageName] = mapped_column(enum_column(StageName, "run_stage_name"), nullable=False)
    status: Mapped[StageStatus] = mapped_column(
        enum_column(StageStatus, "run_stage_status"), nullable=False
    )
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    workflow_run: Mapped[WorkflowRunORM] = relationship(back_populates="stages")

    __table_args__ = (
        UniqueConstraint("run_id", "stage", name="uq_run_stage"),
        Index("ix_run_stages_run_id", "run_id"),
    )
