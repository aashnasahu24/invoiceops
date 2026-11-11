"""Deterministic reviewer queue and actions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import repository
from .config import DEFAULT_POLICY, PolicyConfig
from .grounding import check_grounding
from .matching import match_invoice_lines, match_po, match_vendor
from .models import (
    InvoiceORM,
    PurchaseOrderORM,
    ReviewActionORM,
    VendorORM,
    WorkflowRunORM,
)
from .pdf_processor import DocumentTextResult, PageText
from .rules import derive_po_status, decide, run_checks
from .schemas import (
    CheckStatus,
    Decision,
    DocumentType,
    ExtractionMetadata,
    InvoiceExtraction,
    POImpact,
    ReviewActionType,
)


@dataclass(frozen=True)
class ReviewOutcome:
    decision: Decision
    checks: tuple
    generated_email: str | None = None


def list_review_queue(session: Session) -> list[InvoiceORM]:
    return session.scalars(
        select(InvoiceORM)
        .where(
            InvoiceORM.current_decision == Decision.MANUAL_REVIEW,
            InvoiceORM.is_active.is_(True),
        )
        .options(
            selectinload(InvoiceORM.vendor),
            selectinload(InvoiceORM.workflow_runs),
        )
        .order_by(InvoiceORM.created_at.asc())
    ).all()


def apply_review_action(
    session: Session,
    invoice_id: str,
    action: ReviewActionType,
    *,
    reviewer: str,
    comment: str | None = None,
    policy: PolicyConfig = DEFAULT_POLICY,
) -> ReviewOutcome:
    invoice = session.scalar(
        select(InvoiceORM)
        .where(InvoiceORM.invoice_id == invoice_id)
        .options(
            selectinload(InvoiceORM.line_items),
            selectinload(InvoiceORM.workflow_runs).selectinload(WorkflowRunORM.stages),
        )
        .with_for_update()
    )
    if invoice is None:
        raise ValueError(f"Unknown invoice id: {invoice_id}")
    if invoice.current_decision != Decision.MANUAL_REVIEW or not invoice.is_active:
        raise ValueError("Only active invoices in MANUAL_REVIEW can be actioned.")
    if not reviewer.strip():
        raise ValueError("Reviewer name is required.")

    generated_email = None
    checks = ()
    overridden_checks = None
    if action == ReviewActionType.APPROVE:
        checks, po_impact = _rerun_checks(session, invoice, policy)
        hard_blocked = any(
            check.status == CheckStatus.FAILED and check.details.get("hard_block")
            for check in checks
        )
        failures = [
            check for check in checks
            if check.status == CheckStatus.FAILED
            or (
                check.status == CheckStatus.WARNING
                and check.details.get("escalates", True)
            )
        ]
        if hard_blocked:
            raise ValueError("Approval blocked by an active duplicate, blocked vendor, or closed PO.")
        if failures and not (comment and comment.strip()):
            raise ValueError("A reviewer comment is required to override failed checks.")
        if failures and any(check.details.get("hard_reject") for check in failures):
            raise ValueError("A hard policy rejection cannot be overridden.")
        if failures:
            overridden_checks = [
                {
                    "check_name": check.name,
                    "original_status": check.status.value,
                    "original_message": check.message,
                    "original_details": check.details,
                }
                for check in failures
            ]
        invoice.current_decision = Decision.APPROVED
        if invoice.po_id and po_impact:
            po = session.get(PurchaseOrderORM, invoice.po_id)
            if po is not None:
                po.status = po_impact.status_after
    elif action == ReviewActionType.REJECT:
        if not (comment and comment.strip()):
            raise ValueError("A rejection reason is required.")
        invoice.current_decision = Decision.REJECTED
    elif action == ReviewActionType.REQUEST_INFORMATION:
        if not (comment and comment.strip()):
            raise ValueError("A request-information explanation is required.")
        checks, _ = _rerun_checks(session, invoice, policy)
        generated_email = _vendor_email(invoice, checks)
        invoice.is_active = False
    else:
        raise ValueError(f"Unsupported reviewer action: {action.value}")

    session.add(
        ReviewActionORM(
            run_id=_latest_run(invoice).run_id,
            action=action,
            reviewer=reviewer.strip(),
            comments=comment.strip() if comment and comment.strip() else None,
            generated_email=generated_email,
            overridden_checks=overridden_checks,
        )
    )
    session.flush()
    return ReviewOutcome(
        decision=invoice.current_decision or Decision.MANUAL_REVIEW,
        checks=tuple(checks),
        generated_email=generated_email,
    )


def _rerun_checks(
    session: Session,
    invoice: InvoiceORM,
    policy: PolicyConfig,
) -> tuple[list, POImpact | None]:
    run = _latest_run(invoice)
    schema_stage = next(
        (stage for stage in run.stages if stage.stage.value == "SCHEMA_VALIDATION"),
        None,
    )
    ocr_confidence = (
        schema_stage.detail.get("ocr_confidence") if schema_stage and schema_stage.detail else None
    )
    metadata = ExtractionMetadata(
        document_type=invoice.document_type or DocumentType.DIGITAL_PDF,
        page_count=1,
        file_sha256=invoice.file_sha256,
        field_evidence=invoice.field_evidence,
        warnings=invoice.extraction_warnings,
        ocr_min_confidence=ocr_confidence,
    )
    extraction = InvoiceExtraction.model_validate(
        {**invoice.extracted_data, "extraction": metadata.model_dump(mode="python")}
    )
    document = DocumentTextResult(
        file_name=invoice.file_name,
        file_sha256=invoice.file_sha256 or "",
        document_type=metadata.document_type,
        page_count=metadata.page_count,
        pages=(PageText(1, run.extracted_text or "", "PDF_TEXT"),),
        full_text=run.extracted_text or "",
        warnings=tuple(metadata.warnings),
    )
    grounding = check_grounding(extraction, document)
    extraction.extraction.ungrounded_fields = grounding.ungrounded_fields
    extraction.extraction.warnings = [*metadata.warnings, *grounding.warnings]

    vendors = session.scalars(select(VendorORM)).all()
    vendor_match = match_vendor(extraction, vendors, policy=policy)
    purchase_orders = session.scalars(
        select(PurchaseOrderORM).options(selectinload(PurchaseOrderORM.line_items))
    ).all()
    balance_map = {
        po.po_id: repository.po_balance(session, po.po_id) for po in purchase_orders
    }
    po_match = match_po(
        extraction, vendor_match.vendor, purchase_orders, balances=balance_map, policy=policy
    )
    po = po_match.purchase_order
    if po is not None:
        po = session.scalar(
            select(PurchaseOrderORM)
            .where(PurchaseOrderORM.po_id == po.po_id)
            .options(selectinload(PurchaseOrderORM.line_items))
            .with_for_update()
        )
    remaining = repository.po_balance(session, po.po_id) if po is not None else None
    previous = Decimal(po.total_amount) - remaining if po is not None and remaining is not None else None
    normalized_number = invoice.invoice_number_normalized
    duplicate = (
        repository.find_active_invoice_by_key(
            session,
            vendor_match.vendor.vendor_id,
            normalized_number,
            exclude_invoice_id=invoice.invoice_id,
        )
        if vendor_match.vendor is not None and normalized_number else None
    )
    net = invoice.net_amount
    soft = (
        repository.find_soft_duplicate(
            session,
            vendor_id=vendor_match.vendor.vendor_id,
            net_amount=net,
            invoice_date=invoice.invoice_date,
            normalized_number=normalized_number,
            days=policy.soft_duplicate_days,
            exclude_invoice_id=invoice.invoice_id,
        )
        if vendor_match.vendor is not None and net is not None else None
    )
    line_matches = match_invoice_lines(extraction, po, policy=policy)
    checks = run_checks(
        extraction,
        vendor=vendor_match.vendor,
        purchase_order=po,
        po_candidates=po_match.candidates,
        remaining_balance=remaining,
        previously_invoiced=previous,
        previously_approved_quantities=(
            repository.approved_po_quantities(session, po.po_id) if po else {}
        ),
        document_type=metadata.document_type,
        vendor_conflict=vendor_match.conflict,
        vendor_match=vendor_match,
        line_matches=line_matches,
        duplicate_candidate=duplicate,
        soft_duplicate_candidate=soft,
        policy=policy,
    )
    impact = None
    if po is not None and net is not None and previous is not None:
        after = previous + net
        impact = POImpact(
            po_number=po.po_number,
            po_net=Decimal(po.total_amount),
            previously_invoiced=previous,
            this_invoice=net,
            remaining_after=Decimal(po.total_amount) - after,
            status_after=derive_po_status(Decimal(po.total_amount), after),
        )
    return checks, impact


def _latest_run(invoice: InvoiceORM) -> WorkflowRunORM:
    if not invoice.workflow_runs:
        raise ValueError(f"Invoice {invoice.invoice_id} has no workflow run.")
    return max(invoice.workflow_runs, key=lambda run: run.started_at)


def _vendor_email(invoice: InvoiceORM, checks: tuple | list) -> str:
    # Translate root causes into short requests. Never expose internal check
    # messages, confidence values, or cascading diagnostics to vendors.
    request_by_check = {
        "po_found": "Please confirm the purchase order number associated with this invoice.",
        "required_fields_present": "Please provide the missing invoice details.",
        "missing_invoice_number": "Please include the invoice number.",
        "vendor_identity_conflict": "Please confirm your registered business name and tax identification number.",
        "vendor_status": "Please contact our accounts payable team to confirm your supplier details.",
        "arithmetic_consistency": "Please check the invoice subtotal, tax, and total calculations.",
        "line_arithmetic": "Please check the quantity, unit price, and line total for each item.",
        "currency_match": "Please confirm the invoice currency matches the purchase order.",
        "ungrounded_fields": "Please verify the invoice details are clearly shown on the document.",
        "ocr_confidence": "Please provide a clear, readable copy of the invoice.",
    }
    actionable = {
        check.name for check in checks
        if check.status in (CheckStatus.FAILED, CheckStatus.WARNING)
        and check.name in request_by_check
    }
    issues = [request_by_check[name] for name in request_by_check if name in actionable][:3]
    issue_text = "\n".join(f"- {issue}" for issue in issues) or "- Please provide corrected invoice details."
    vendor_name = invoice.extracted_vendor_name or "Vendor"
    return (
        f"Subject: Information required for invoice {invoice.invoice_number or '(number missing)'}\n\n"
        f"Dear {vendor_name},\n\n"
        "Please provide a corrected invoice or the following information so we can continue processing:\n"
        f"{issue_text}\n\nThank you,\nAccounts Payable"
    )
