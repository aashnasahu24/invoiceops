"""Persisted, provider-neutral invoice processing workflow."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Generator
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import repository
from .config import DEFAULT_POLICY, PolicyConfig
from .extraction_service import InvoiceExtractionService, StructuredOutputProvider
from .matching import match_invoice_lines, match_po, match_vendor
from .models import PurchaseOrderORM, VendorORM, WorkflowRunORM
from .normalization import normalize_invoice_number
from .pdf_processor import PDFProcessingError, PDFProcessor
from .providers.resilient import ExtractionFailed, extract_with_retry
from .rules import derive_po_status, duplicate_business_key, run_checks, decide
from .schemas import (
    CheckResult,
    CheckStatus,
    Decision,
    DecisionResult,
    InvoiceExtraction,
    POImpact,
    ProcessingStatus,
    StageEvent,
    StageName,
    StageStatus,
)

SessionFactory = Callable[[], Session]
_STAGES = tuple(StageName)
logger = logging.getLogger(__name__)


def process_invoice(
    session_factory: SessionFactory,
    provider: StructuredOutputProvider,
    pdf_bytes: bytes,
    file_name: str,
    *,
    pdf_processor: PDFProcessor | None = None,
    policy: PolicyConfig = DEFAULT_POLICY,
) -> Generator[StageEvent | DecisionResult, None, None]:
    """Run a workflow and convert unexpected pipeline errors into a persisted failure."""
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    run_id: str | None = None
    current_stage = StageName.RECEIVED
    pipeline = _process_invoice_pipeline(
        session_factory,
        provider,
        pdf_bytes,
        file_name,
        pdf_processor=pdf_processor,
        policy=policy,
    )
    try:
        for event in pipeline:
            if isinstance(event, StageEvent):
                run_id = event.detail.get("run_id", run_id)
                if event.status == StageStatus.RUNNING:
                    current_stage = event.stage
            yield event
    except IntegrityError as exc:
        logger.exception("Database integrity error while processing invoice run %s", run_id)
        duplicate = _recover_business_key_race(
            session_factory,
            run_id,
            digest,
            current_stage,
            policy,
        )
        if duplicate is not None:
            for event in duplicate[0]:
                yield event
            yield duplicate[1]
            return
        logger.error("Integrity error was not an active business-key race; failing the run.")
        yield from _fail_unexpected_run(
            session_factory, run_id, digest, current_stage, policy, exc
        )
    except Exception as exc:
        logger.exception("Unexpected error while processing invoice run %s", run_id)
        yield from _fail_unexpected_run(
            session_factory, run_id, digest, current_stage, policy, exc
        )


def _fail_unexpected_run(
    session_factory: SessionFactory,
    run_id: str | None,
    digest: str,
    stage: StageName,
    policy: PolicyConfig,
    error: Exception,
) -> Generator[StageEvent | DecisionResult, None, None]:
    with session_factory() as session:
        run = session.get(WorkflowRunORM, run_id) if run_id else repository.get_latest_run_by_hash(
            session, digest
        )
        if run is None:
            raise error
        run_id = run.run_id
        message = f"An unexpected error occurred during {stage.value}; manual review is required."
        events = [
            StageEvent(
                stage=stage,
                status=StageStatus.FAILED,
                message=message,
                detail={"error_type": type(error).__name__},
            )
        ]
        for remaining in _STAGES[_STAGES.index(stage) + 1:]:
            events.append(
                StageEvent(
                    stage=remaining,
                    status=StageStatus.SKIPPED,
                    message=f"Skipped because {stage.value} failed.",
                )
            )
        for event in events:
            repository.record_stage(session, run_id, event)
        repository.complete_run(
            session,
            run_id,
            Decision.MANUAL_REVIEW,
            processing_status=ProcessingStatus.FAILED,
            error_message=f"{type(error).__name__}: {error}"[:1000],
            policy_version=policy.version,
        )
        session.commit()
    yield from events
    yield DecisionResult(
        decision=Decision.MANUAL_REVIEW,
        reasons=[message],
        checks=[
            CheckResult(
                name="processing_failure",
                status=CheckStatus.FAILED,
                message=message,
                details={"stage": stage.value, "error_type": type(error).__name__},
            )
        ],
        policy_version=policy.version,
        summary=message,
        recommended_action="Contact support before retrying this invoice.",
    )


def _recover_business_key_race(
    session_factory: SessionFactory,
    run_id: str | None,
    digest: str,
    stage: StageName,
    policy: PolicyConfig,
) -> tuple[list[StageEvent], DecisionResult] | None:
    if stage != StageName.DECISION:
        return None
    with session_factory() as session:
        run = session.get(WorkflowRunORM, run_id) if run_id else repository.get_latest_run_by_hash(
            session, digest
        )
        if run is None or not run.llm_response:
            return None
        raw = run.llm_response
        identity = InvoiceExtraction(
            vendor_name=raw.get("vendor_name"),
            vendor_tax_id=raw.get("vendor_tax_id"),
            invoice_number=raw.get("invoice_number"),
        )
        vendors = session.scalars(select(VendorORM).order_by(VendorORM.vendor_id)).all()
        vendor_match = match_vendor(identity, vendors, policy=policy)
        if vendor_match.vendor is None or not identity.invoice_number:
            return None
        duplicate = repository.find_active_invoice_by_key(
            session,
            vendor_match.vendor.vendor_id,
            _normalize_number(identity.invoice_number),
        )
        if duplicate is None:
            return None
        result = decide([duplicate_business_key(duplicate)], policy)
        result.decision = Decision.REJECTED
        result.summary = "Invoice rejected because an active invoice with the same business key exists."
        events = [
            StageEvent(
                stage=StageName.DECISION,
                status=StageStatus.COMPLETED,
                message="Concurrent duplicate detected; invoice rejected.",
                detail={
                    "decision": Decision.REJECTED.value,
                    "reasons": result.reasons,
                    "summary": result.summary,
                },
            )
        ]
        repository.save_checks(session, run.run_id, result.checks)
        repository.record_stage(session, run.run_id, events[0])
        repository.complete_run(
            session,
            run.run_id,
            Decision.REJECTED,
            processing_status=ProcessingStatus.COMPLETED,
            policy_version=policy.version,
        )
        session.commit()
        return events, result


def _process_invoice_pipeline(
    session_factory: SessionFactory,
    provider: StructuredOutputProvider,
    pdf_bytes: bytes,
    file_name: str,
    *,
    pdf_processor: PDFProcessor | None = None,
    policy: PolicyConfig = DEFAULT_POLICY,
) -> Generator[StageEvent | DecisionResult, None, None]:
    """Run and persist all stages, yielding live events and the final decision."""
    digest = hashlib.sha256(pdf_bytes).hexdigest()

    def emit(
        stage: StageName,
        status: StageStatus,
        message: str = "",
        detail: dict[str, Any] | None = None,
    ) -> StageEvent:
        event = StageEvent(stage=stage, status=status, message=message, detail=detail or {})
        with session_factory() as session:
            repository.record_stage(session, run_id, event)
            session.commit()
        return event

    def skip_after(stage: StageName, reason: str) -> Generator[StageEvent, None, None]:
        for remaining in _STAGES[_STAGES.index(stage) + 1:]:
            yield emit(remaining, StageStatus.SKIPPED, reason)

    def failed_result(
        stage: StageName,
        error: Exception,
    ) -> Generator[StageEvent, None, DecisionResult]:
        logger.error(
            "Invoice run %s failed during %s",
            run_id,
            stage.value,
            exc_info=(type(error), error, error.__traceback__),
        )
        message = str(error) or type(error).__name__
        yield emit(stage, StageStatus.FAILED, message, {"error_type": type(error).__name__})
        yield from skip_after(stage, f"Skipped because {stage.value} failed.")
        with session_factory() as session:
            repository.complete_run(
                session,
                run_id,
                Decision.MANUAL_REVIEW,
                processing_status=ProcessingStatus.FAILED,
                error_message=f"{type(error).__name__}: {error}"[:1000],
                policy_version=policy.version,
            )
            session.commit()
        return DecisionResult(
            decision=Decision.MANUAL_REVIEW,
            reasons=[message],
            checks=[
                CheckResult(
                    name="processing_failure",
                    status=CheckStatus.FAILED,
                    message=message,
                    details={"stage": stage.value, "error_type": type(error).__name__},
                )
            ],
            policy_version=policy.version,
            summary=f"Invoice processing stopped during {stage.value}; manual review is required. {message}",
            recommended_action="Resolve the processing issue, then upload the invoice again.",
        )

    with session_factory() as session:
        duplicate = repository.find_invoice_by_hash(session, digest)
        run = repository.create_run(
            session,
            file_name=file_name,
            file_sha256=digest,
            invoice_id=duplicate.invoice_id if duplicate else None,
        )
        run_id = run.run_id
        session.commit()

    yield emit(
        StageName.RECEIVED,
        StageStatus.RUNNING,
        "Upload received; calculating file hash.",
        {"run_id": run_id},
    )
    if duplicate is not None:
        yield emit(
            StageName.RECEIVED,
            StageStatus.COMPLETED,
            "Exact file duplicate detected.",
            {"duplicate_invoice_id": duplicate.invoice_id, "sha256": digest},
        )
        yield from skip_after(StageName.RECEIVED, "Skipped because this exact file was already processed.")
        duplicate_check = CheckResult(
            name="duplicate_file",
            status=CheckStatus.FAILED,
            message="This exact PDF has already been processed.",
            details={"invoice_id": duplicate.invoice_id, "hard_reject": True},
        )
        result = DecisionResult(
            decision=Decision.REJECTED,
            reasons=["This exact PDF has already been processed."],
            checks=[duplicate_check],
            policy_version=policy.version,
            summary="Invoice rejected because the exact file was already processed.",
            recommended_action="Do not resubmit this file; review the existing invoice record.",
        )
        with session_factory() as session:
            repository.save_checks(session, run_id, result.checks)
            repository.complete_run(
                session,
                run_id,
                result.decision,
                policy_version=policy.version,
            )
            session.commit()
        yield result
        return

    yield emit(
        StageName.RECEIVED,
        StageStatus.COMPLETED,
        "Upload received.",
        {"sha256": digest, "file_name": file_name},
    )

    processor = pdf_processor or PDFProcessor()
    yield emit(StageName.TEXT_EXTRACTION, StageStatus.RUNNING, "Extracting text from PDF.")
    try:
        document = processor.process(pdf_bytes, file_name=file_name)
    except PDFProcessingError as exc:
        result = yield from failed_result(StageName.TEXT_EXTRACTION, exc)
        yield result
        return
    yield emit(
        StageName.TEXT_EXTRACTION,
        StageStatus.COMPLETED,
        f"Extracted text from {document.page_count} page(s).",
        {
            "document_type": document.document_type.value,
            "page_count": document.page_count,
            "warnings": list(document.warnings),
        },
    )

    service = InvoiceExtractionService(provider)
    yield emit(StageName.AI_EXTRACTION, StageStatus.RUNNING, "Extracting invoice fields with the selected provider.")
    try:
        extraction = extract_with_retry(service, document)
    except ExtractionFailed as exc:
        with session_factory() as session:
            repository.save_extraction_artifacts(
                session,
                run_id,
                extracted_text=document.full_text,
                raw_llm_response=service.last_raw_response,
            )
            session.commit()
        result = yield from failed_result(StageName.AI_EXTRACTION, exc)
        yield result
        return

    with session_factory() as session:
        repository.save_extraction_artifacts(
            session,
            run_id,
            extracted_text=document.full_text,
            raw_llm_response=service.last_raw_response,
        )
        session.commit()
    yield emit(
        StageName.AI_EXTRACTION,
        StageStatus.COMPLETED,
        "Structured invoice fields extracted.",
        {
            "attempts": service.last_attempts,
            "cache_hit": bool(getattr(provider, "last_call_was_cache_hit", False)),
        },
    )

    metadata = extraction.extraction
    yield emit(StageName.SCHEMA_VALIDATION, StageStatus.RUNNING, "Checking schema, placeholders, and source grounding.")
    yield emit(
        StageName.SCHEMA_VALIDATION,
        StageStatus.COMPLETED,
        "Extraction passed schema validation.",
        {
            "warnings": list(metadata.warnings) if metadata else [],
            "ungrounded_fields": list(metadata.ungrounded_fields) if metadata else [],
            "ocr_confidence": metadata.ocr_min_confidence if metadata else None,
            "extracted_fields": extraction.model_dump(mode="json", exclude={"extraction"}),
            "field_evidence": {
                name: evidence.model_dump(mode="json")
                for name, evidence in (metadata.field_evidence.items() if metadata else [])
            },
        },
    )

    yield emit(StageName.MATCHING, StageStatus.RUNNING, "Matching vendor and purchase order.")
    with session_factory() as session:
        vendors = session.scalars(select(VendorORM).order_by(VendorORM.vendor_id)).all()
        purchase_orders = session.scalars(
            select(PurchaseOrderORM).order_by(PurchaseOrderORM.po_number)
        ).all()
        vendor_match = match_vendor(extraction, vendors, policy=policy)
        balances = {
            po.po_id: repository.po_balance(session, po.po_id)
            for po in purchase_orders
        }
        po_match = match_po(extraction, vendor_match.vendor, purchase_orders, balances=balances)
        line_matches = match_invoice_lines(extraction, po_match.purchase_order, policy=policy)
        active_duplicate = None
        soft_duplicate = None
        supersedes_invoice_id = None
        if vendor_match.vendor is not None and extraction.invoice_number:
            normalized_number = _normalize_number(extraction.invoice_number)
            active_duplicate = repository.find_active_invoice_by_key(
                session, vendor_match.vendor.vendor_id, normalized_number
            )
            previous_invoice = repository.find_invoice_by_business_key(
                session, vendor_match.vendor.vendor_id, normalized_number
            )
            if previous_invoice is not None and not previous_invoice.is_active:
                supersedes_invoice_id = previous_invoice.invoice_id
        net_amount = (
            extraction.subtotal + (extraction.shipping or Decimal("0"))
            - (extraction.discount or Decimal("0"))
            if extraction.subtotal is not None else None
        )
        if vendor_match.vendor is not None and net_amount is not None:
            soft_duplicate = repository.find_soft_duplicate(
                session,
                vendor_id=vendor_match.vendor.vendor_id,
                net_amount=net_amount,
                invoice_date=extraction.invoice_date,
                normalized_number=_normalize_number(extraction.invoice_number)
                if extraction.invoice_number else None,
                days=policy.soft_duplicate_days,
            )
        remaining_balance = (
            balances[po_match.purchase_order.po_id] if po_match.purchase_order is not None else None
        )
        previously_invoiced = (
            Decimal(po_match.purchase_order.total_amount) - remaining_balance
            if po_match.purchase_order is not None and remaining_balance is not None else None
        )
        previous_quantities = (
            repository.approved_po_quantities(session, po_match.purchase_order.po_id)
            if po_match.purchase_order is not None else {}
        )

    yield emit(
        StageName.MATCHING,
        StageStatus.COMPLETED,
        "Vendor and purchase-order matching complete.",
        {
            "vendor_id": vendor_match.vendor.vendor_id if vendor_match.vendor else None,
            "vendor_method": vendor_match.method,
            "vendor_conflict": vendor_match.conflict,
            "po_id": po_match.purchase_order.po_id if po_match.purchase_order else None,
            "po_candidates": [po.po_number for po in po_match.candidates],
            "active_duplicate_invoice_id": active_duplicate.invoice_id if active_duplicate else None,
        },
    )

    yield emit(StageName.VALIDATION, StageStatus.RUNNING, "Running deterministic validation checks.")
    checks = run_checks(
        extraction,
        vendor=vendor_match.vendor,
        purchase_order=po_match.purchase_order,
        po_candidates=po_match.candidates,
        remaining_balance=remaining_balance,
        previously_invoiced=previously_invoiced,
        previously_approved_quantities=previous_quantities,
        document_type=document.document_type,
        vendor_conflict=vendor_match.conflict,
        vendor_match=vendor_match,
        line_matches=line_matches,
        duplicate_candidate=active_duplicate,
        soft_duplicate_candidate=soft_duplicate,
        policy=policy,
    )
    with session_factory() as session:
        repository.save_checks(session, run_id, checks)
        session.commit()
    yield emit(
        StageName.VALIDATION,
        StageStatus.COMPLETED,
        f"Completed {len(checks)} validation checks.",
        {
            "failed": sum(check.status == CheckStatus.FAILED for check in checks),
            "warnings": [
                check.message for check in checks if check.status == CheckStatus.WARNING
            ],
            "warning_count": sum(check.status == CheckStatus.WARNING for check in checks),
        },
    )

    yield emit(StageName.DECISION, StageStatus.RUNNING, "Applying deterministic decision policy.")
    result = decide(checks, policy, vendor_match_method=vendor_match.method)
    with session_factory() as session:
        locked_po = None
        if po_match.purchase_order is not None:
            locked_po = session.scalar(
                select(PurchaseOrderORM)
                .where(PurchaseOrderORM.po_id == po_match.purchase_order.po_id)
                .with_for_update()
            )
        fresh_remaining = (
            repository.po_balance(session, locked_po.po_id) if locked_po is not None else None
        )
        fresh_previous = (
            Decimal(locked_po.total_amount) - fresh_remaining
            if locked_po is not None and fresh_remaining is not None else None
        )
        fresh_duplicate = (
            repository.find_active_invoice_by_key(
                session,
                vendor_match.vendor.vendor_id,
                _normalize_number(extraction.invoice_number),
            )
            if vendor_match.vendor is not None and extraction.invoice_number else None
        )
        fresh_soft_duplicate = (
            repository.find_soft_duplicate(
                session,
                vendor_id=vendor_match.vendor.vendor_id,
                net_amount=net_amount,
                invoice_date=extraction.invoice_date,
                normalized_number=_normalize_number(extraction.invoice_number)
                if extraction.invoice_number else None,
                days=policy.soft_duplicate_days,
            )
            if vendor_match.vendor is not None and net_amount is not None else None
        )
        checks = run_checks(
            extraction,
            vendor=vendor_match.vendor,
            purchase_order=locked_po,
            po_candidates=po_match.candidates,
            remaining_balance=fresh_remaining,
            previously_invoiced=fresh_previous,
            previously_approved_quantities=(
                repository.approved_po_quantities(session, locked_po.po_id)
                if locked_po is not None else {}
            ),
            document_type=document.document_type,
            vendor_conflict=vendor_match.conflict,
            vendor_match=vendor_match,
            line_matches=line_matches,
            duplicate_candidate=fresh_duplicate,
            soft_duplicate_candidate=fresh_soft_duplicate,
            policy=policy,
        )
        po_impact = None
        if locked_po is not None and net_amount is not None and fresh_previous is not None:
            after = fresh_previous + net_amount
            po_impact = POImpact(
                po_number=locked_po.po_number,
                po_net=Decimal(locked_po.total_amount),
                previously_invoiced=fresh_previous,
                this_invoice=net_amount,
                remaining_after=Decimal(locked_po.total_amount) - after,
                status_after=derive_po_status(Decimal(locked_po.total_amount), after),
            )
        updated_result = decide(
            checks,
            policy,
            po_impact=po_impact,
            vendor_match_method=vendor_match.method,
        )
        decision_downgraded = (
            result.decision == Decision.APPROVED
            and updated_result.decision != Decision.APPROVED
        )
        result = updated_result
        repository.save_checks(session, run_id, checks)
        if result.decision in (Decision.APPROVED, Decision.MANUAL_REVIEW):
            repository.save_invoice(
                session,
                run_id=run_id,
                file_name=file_name,
                file_sha256=digest,
                extraction=extraction,
                decision=result.decision,
                vendor_id=vendor_match.vendor.vendor_id if vendor_match.vendor else None,
                po_id=po_match.purchase_order.po_id if po_match.purchase_order else None,
                line_matches=line_matches,
                supersedes_invoice_id=supersedes_invoice_id,
            )
            if result.decision == Decision.APPROVED and locked_po is not None and po_impact is not None:
                locked_po.status = po_impact.status_after
        repository.complete_run(
            session,
            run_id,
            result.decision,
            processing_status=ProcessingStatus.COMPLETED,
            policy_version=policy.version,
        )
        session.commit()
    if decision_downgraded:
        message = "Concurrency re-check changed the decision to MANUAL_REVIEW; PO balance or duplicate state changed."
    else:
        message = f"Decision: {result.decision.value}."
    yield emit(
        StageName.DECISION,
        StageStatus.COMPLETED,
        message,
        {
            "decision": result.decision.value,
            "reasons": result.reasons,
            "summary": result.summary,
            "recommended_action": result.recommended_action,
            "po_impact": result.po_impact.model_dump(mode="json") if result.po_impact else None,
            "vendor_match_method": result.vendor_match_method,
        },
    )
    yield result


def _normalize_number(value: str) -> str:
    return normalize_invoice_number(value) or ""
