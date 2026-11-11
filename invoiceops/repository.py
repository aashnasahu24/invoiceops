"""Persistence operations for invoice workflow runs and derived PO balances."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .normalization import normalize_invoice_number
from .models import (
    InvoiceLineItemORM,
    InvoiceORM,
    PurchaseOrderORM,
    RunStageORM,
    ValidationCheckORM,
    WorkflowRunORM,
)
from .matching import LineMatch
from .schemas import (
    CheckResult,
    Decision,
    InvoiceExtraction,
    ProcessingStatus,
    StageEvent,
    StageStatus,
)


def create_run(
    session: Session,
    *,
    file_name: str,
    file_sha256: str | None = None,
    run_id: str | None = None,
    invoice_id: str | None = None,
) -> WorkflowRunORM:
    run = WorkflowRunORM(
        run_id=run_id or str(uuid4()),
        source_file_name=file_name,
        source_file_sha256=file_sha256,
        invoice_id=invoice_id,
        processing_status=ProcessingStatus.PROCESSING,
    )
    session.add(run)
    session.flush()
    return run


def record_stage(session: Session, run_id: str, event: StageEvent) -> RunStageORM:
    row = session.scalar(
        select(RunStageORM).where(
            RunStageORM.run_id == run_id,
            RunStageORM.stage == event.stage,
        )
    )
    if row is None:
        row = RunStageORM(run_id=run_id, stage=event.stage)
        session.add(row)

    row.status = event.status
    row.message = event.message
    row.detail = event.detail
    if event.status == StageStatus.RUNNING:
        row.started_at = event.timestamp
        row.completed_at = None
    elif event.status in (StageStatus.COMPLETED, StageStatus.FAILED, StageStatus.SKIPPED):
        row.completed_at = event.timestamp
        if row.started_at is None and event.status != StageStatus.SKIPPED:
            row.started_at = event.timestamp
    session.flush()
    return row


def save_extraction_artifacts(
    session: Session,
    run_id: str,
    *,
    extracted_text: str,
    raw_llm_response: dict[str, Any] | None,
) -> WorkflowRunORM:
    run = _get_run(session, run_id)
    run.extracted_text = extracted_text
    run.llm_response = raw_llm_response
    session.flush()
    return run


def find_invoice_by_hash(session: Session, sha: str) -> InvoiceORM | None:
    return session.scalar(select(InvoiceORM).where(InvoiceORM.file_sha256 == sha))


def get_latest_run_by_hash(session: Session, sha: str) -> WorkflowRunORM | None:
    return session.scalar(
        select(WorkflowRunORM)
        .where(WorkflowRunORM.source_file_sha256 == sha)
        .options(
            selectinload(WorkflowRunORM.invoice),
            selectinload(WorkflowRunORM.stages),
            selectinload(WorkflowRunORM.validation_checks),
        )
        .order_by(WorkflowRunORM.started_at.desc())
        .limit(1)
    )


def list_recent_runs(session: Session, limit: int = 100) -> list[WorkflowRunORM]:
    if limit < 1:
        raise ValueError("limit must be positive")
    return session.scalars(
        select(WorkflowRunORM)
        .options(
            selectinload(WorkflowRunORM.invoice)
            .selectinload(InvoiceORM.workflow_runs)
            .selectinload(WorkflowRunORM.review_actions),
            selectinload(WorkflowRunORM.review_actions),
        )
        .order_by(WorkflowRunORM.started_at.desc())
        .limit(limit)
    ).all()


def find_active_invoice_by_key(
    session: Session,
    vendor_id: str,
    normalized_number: str,
    *,
    exclude_invoice_id: str | None = None,
) -> InvoiceORM | None:
    if not normalized_number:
        return None
    return session.scalar(
        select(InvoiceORM)
        .where(
            InvoiceORM.vendor_id == vendor_id,
            InvoiceORM.invoice_number_normalized == normalized_number,
            InvoiceORM.is_active.is_(True),
            *(
                (InvoiceORM.invoice_id != exclude_invoice_id,)
                if exclude_invoice_id is not None else ()
            ),
        )
        .options(selectinload(InvoiceORM.workflow_runs))
    )


def save_invoice(
    session: Session,
    *,
    run_id: str,
    file_name: str,
    file_sha256: str,
    extraction: InvoiceExtraction,
    decision: Decision,
    vendor_id: str | None,
    po_id: str | None,
    invoice_id: str | None = None,
    line_matches: tuple[LineMatch, ...] = (),
    supersedes_invoice_id: str | None = None,
) -> InvoiceORM:
    metadata = extraction.extraction
    invoice = InvoiceORM(
        invoice_id=invoice_id or str(uuid4()),
        vendor_id=vendor_id,
        po_id=po_id,
        invoice_number=extraction.invoice_number,
        invoice_number_normalized=_normalize_business_number(extraction.invoice_number),
        extracted_po_number=extraction.po_number,
        invoice_date=extraction.invoice_date,
        due_date=extraction.due_date,
        extracted_vendor_name=extraction.vendor_name,
        extracted_vendor_tax_id=extraction.vendor_tax_id,
        currency=extraction.currency,
        subtotal=extraction.subtotal,
        tax_total=extraction.tax_total,
        shipping=extraction.shipping,
        discount=extraction.discount,
        grand_total=extraction.grand_total,
        net_amount=_net_amount(extraction),
        file_name=file_name,
        file_sha256=file_sha256,
        document_type=metadata.document_type if metadata else None,
        extracted_data=extraction.model_dump(mode="json", exclude={"extraction"}),
        field_evidence={
            name: evidence.model_dump(mode="json")
            for name, evidence in (metadata.field_evidence.items() if metadata else [])
        },
        extraction_warnings=list(metadata.warnings) if metadata else [],
        current_decision=decision,
        is_active=True,
        supersedes_invoice_id=supersedes_invoice_id,
    )
    session.add(invoice)
    run = _get_run(session, run_id)
    run.invoice = invoice
    match_by_invoice_line = {id(match.invoice_line): match for match in line_matches}
    for index, item in enumerate(extraction.line_items, start=1):
        matched = match_by_invoice_line.get(id(item))
        invoice.line_items.append(
            InvoiceLineItemORM(
                line_number=index,
                description=item.description,
                sku=item.sku,
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=item.line_total,
                tax_rate=item.tax_rate,
                matched_po_line_id=matched.po_line.id if matched and matched.po_line is not None else None,
            )
        )
    session.flush()
    return invoice


def save_checks(session: Session, run_id: str, checks: list[CheckResult]) -> list[ValidationCheckORM]:
    saved: list[ValidationCheckORM] = []
    for check in checks:
        row = session.scalar(
            select(ValidationCheckORM).where(
                ValidationCheckORM.run_id == run_id,
                ValidationCheckORM.check_name == check.name,
            )
        )
        if row is None:
            row = ValidationCheckORM(run_id=run_id, check_name=check.name)
            session.add(row)
        row.status = check.status
        row.message = check.message
        row.details = {
            **check.details,
            **({"recommended_action": check.recommended_action} if check.recommended_action else {}),
        }
        saved.append(row)
    session.flush()
    return saved


def complete_run(
    session: Session,
    run_id: str,
    decision: Decision,
    *,
    processing_status: ProcessingStatus = ProcessingStatus.COMPLETED,
    error_message: str | None = None,
    policy_version: str = "v1",
) -> WorkflowRunORM:
    run = _get_run(session, run_id)
    run.decision = decision
    run.processing_status = processing_status
    run.error_message = error_message
    run.policy_version = policy_version
    run.completed_at = datetime.now(timezone.utc)
    session.flush()
    return run


def po_balance(session: Session, po_id: str) -> Decimal:
    purchase_order = session.get(PurchaseOrderORM, po_id)
    if purchase_order is None:
        raise ValueError(f"Unknown purchase order id: {po_id}")

    approved_amounts = session.scalars(
        select(InvoiceORM.net_amount).where(
            InvoiceORM.po_id == po_id,
            InvoiceORM.current_decision == Decision.APPROVED,
        )
    ).all()
    consumed = sum((amount or Decimal("0") for amount in approved_amounts), Decimal("0"))
    return Decimal(purchase_order.total_amount) - consumed


def approved_po_quantities(session: Session, po_id: str) -> dict[int, Decimal]:
    rows = session.execute(
        select(InvoiceLineItemORM.matched_po_line_id, InvoiceLineItemORM.quantity)
        .join(InvoiceORM, InvoiceORM.invoice_id == InvoiceLineItemORM.invoice_id)
        .where(
            InvoiceORM.po_id == po_id,
            InvoiceORM.current_decision == Decision.APPROVED,
            InvoiceLineItemORM.matched_po_line_id.is_not(None),
        )
    ).all()
    totals: dict[int, Decimal] = {}
    for po_line_id, quantity in rows:
        totals[po_line_id] = totals.get(po_line_id, Decimal("0")) + Decimal(quantity)
    return totals


def find_soft_duplicate(
    session: Session,
    *,
    vendor_id: str,
    net_amount: Decimal,
    invoice_date: date | None,
    normalized_number: str | None,
    days: int = 7,
    exclude_invoice_id: str | None = None,
) -> InvoiceORM | None:
    if invoice_date is None:
        return None
    conditions = [
        InvoiceORM.vendor_id == vendor_id,
        InvoiceORM.net_amount == net_amount,
        InvoiceORM.invoice_date >= invoice_date - timedelta(days=days),
        InvoiceORM.invoice_date <= invoice_date + timedelta(days=days),
        InvoiceORM.is_active.is_(True),
    ]
    if normalized_number:
        conditions.append(InvoiceORM.invoice_number_normalized != normalized_number)
    else:
        conditions.append(InvoiceORM.invoice_number_normalized.is_not(None))
    if exclude_invoice_id:
        conditions.append(InvoiceORM.invoice_id != exclude_invoice_id)
    candidates = session.scalars(
        select(InvoiceORM)
        .where(*conditions)
        .order_by(InvoiceORM.invoice_date)
    ).all()
    return min(
        candidates,
        key=lambda row: abs((row.invoice_date - invoice_date).days),
        default=None,
    )


def find_invoice_by_business_key(
    session: Session,
    vendor_id: str,
    normalized_number: str | None,
) -> InvoiceORM | None:
    if not normalized_number:
        return None
    return session.scalar(
        select(InvoiceORM)
        .where(
            InvoiceORM.vendor_id == vendor_id,
            InvoiceORM.invoice_number_normalized == normalized_number,
        )
        .order_by(InvoiceORM.is_active.asc(), InvoiceORM.created_at.desc())
    )


def _normalize_business_number(value: str | None) -> str | None:
    return normalize_invoice_number(value)


def _net_amount(extraction: InvoiceExtraction) -> Decimal | None:
    if extraction.subtotal is None:
        return None
    return (
        extraction.subtotal
        + (extraction.shipping or Decimal("0"))
        - (extraction.discount or Decimal("0"))
    )


def _get_run(session: Session, run_id: str) -> WorkflowRunORM:
    run = session.get(WorkflowRunORM, run_id)
    if run is None:
        raise ValueError(f"Unknown workflow run id: {run_id}")
    return run
