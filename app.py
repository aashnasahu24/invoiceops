"""Thin Streamlit entry point for the InvoiceOps Stage 3 vertical slice."""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import streamlit as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from invoiceops.config import PROJECT_ROOT, get_settings
from invoiceops.db import create_db_engine, create_session_factory, init_db
from invoiceops.models import InvoiceORM, ReviewActionORM, WorkflowRunORM
from invoiceops.orchestrator import process_invoice
from invoiceops.providers.gemini_provider import GeminiProvider
from invoiceops.providers.replay import RecordingProvider, ReplayProvider
from invoiceops.repository import get_latest_run_by_hash, list_recent_runs
from invoiceops.reviewer import apply_review_action, list_review_queue
from invoiceops.schemas import (
    CheckResult,
    CheckStatus,
    Decision,
    DecisionResult,
    ProcessingStatus,
    POImpact,
    ReviewActionType,
    StageEvent,
    StageName,
    StageStatus,
)
from invoiceops.seed import reset_demo_state, seed_reference_data
from invoiceops.ui_components import render_decision_card, render_persisted_timeline, update_live_stage

CACHE_DIR = PROJECT_ROOT / "data" / "llm_cache_demo"


@st.cache_resource
def get_resources(
    database_url: str,
    seed_reference_data_on_startup: bool = True,
) -> tuple[Any, sessionmaker[Session]]:
    engine = create_db_engine(database_url)
    _initialize_app_database(engine, seed_reference_data_on_startup)
    factory = create_session_factory(engine)
    return engine, factory


def _initialize_app_database(engine, seed_on_startup: bool) -> None:
    init_db(engine)
    if seed_on_startup:
        factory = create_session_factory(engine)
        with factory.begin() as session:
            seed_reference_data(session, PROJECT_ROOT / "data")


def _effective_decision(invoice: InvoiceORM | None, automated: Decision | None) -> Decision | None:
    if invoice is not None and invoice.current_decision is not None:
        return invoice.current_decision
    return automated


def _latest_invoice_review_action(invoice: InvoiceORM | None) -> ReviewActionORM | None:
    if invoice is None:
        return None
    actions = [
        action
        for run in invoice.workflow_runs
        for action in run.review_actions
    ]
    return max(actions, key=lambda action: action.created_at, default=None)


def _lifecycle_status(
    invoice: InvoiceORM | None,
    review_action: ReviewActionORM | None,
) -> str:
    if invoice is None:
        return "No invoice record"
    if not invoice.is_active:
        if review_action and review_action.action == ReviewActionType.REQUEST_INFORMATION:
            return "Returned — information requested"
        return "Inactive"
    return "Active"


def _dashboard_outcome_counts(session: Session) -> dict[str, int]:
    counts = {decision.value: 0 for decision in Decision}
    rows = session.execute(
        select(InvoiceORM.current_decision, func.count())
        .where(InvoiceORM.current_decision.is_not(None))
        .group_by(InvoiceORM.current_decision)
    )
    for decision, count in rows:
        counts[decision.value] = count
    return counts


def _review_actions_authorized(settings) -> bool:
    if not settings.enable_reviewer_actions:
        st.info("Reviewer actions are disabled by configuration; this view is read-only.")
        return False
    if settings.reviewer_access_key:
        entered_key = st.text_input("Reviewer access key", type="password")
        if not hmac.compare_digest(entered_key, settings.reviewer_access_key):
            st.info("Enter the reviewer access key to enable actions.")
            return False
    elif settings.is_production:
        st.info("Reviewer actions are read-only in production until REVIEWER_ACCESS_KEY is configured.")
        return False
    return True


def _render_decision_metadata(detail: dict[str, Any]) -> None:
    columns = st.columns(3)
    columns[0].metric("Automated decision", _display_decision(detail["automated_decision"]))
    columns[1].metric("Effective decision", _display_decision(detail["effective_decision"]))
    columns[2].metric("Invoice status", detail["lifecycle_status"])
    action = detail["latest_review_action"]
    if action:
        st.caption(
            f"Latest reviewer action: {action['action']} by {action['reviewer'] or 'unnamed reviewer'}"
        )


def _display_decision(decision: Decision | None) -> str:
    return decision.value.replace("_", " ").title() if decision else "—"


def _page_options(settings) -> list[str]:
    pages = ["Process invoice", "Dashboard", "Review queue"]
    if not settings.is_production:
        pages.append("Run all fixtures")
    return pages


def _provider(provider_name: str):
    settings = get_settings()
    if not settings.gemini_model:
        raise ValueError("Set GEMINI_MODEL in .env before choosing Gemini or Replay.")
    if provider_name == "Replay":
        return ReplayProvider(CACHE_DIR, model=settings.gemini_model)
    if not settings.gemini_api_key:
        raise ValueError("Set GEMINI_API_KEY in .env before choosing Gemini.")
    inner = GeminiProvider(api_key=settings.gemini_api_key, model=settings.gemini_model)
    return RecordingProvider(inner, CACHE_DIR, model=settings.gemini_model)


def _run_detail(factory: sessionmaker[Session], run_id: str) -> dict[str, Any] | None:
    with factory() as session:
        run = session.scalar(
            select(WorkflowRunORM)
            .where(WorkflowRunORM.run_id == run_id)
            .options(
                selectinload(WorkflowRunORM.invoice),
                selectinload(WorkflowRunORM.stages),
                selectinload(WorkflowRunORM.validation_checks),
                selectinload(WorkflowRunORM.review_actions),
                selectinload(WorkflowRunORM.invoice)
                .selectinload(InvoiceORM.workflow_runs)
                .selectinload(WorkflowRunORM.review_actions),
            )
        )
        if run is None:
            return None
        stages = sorted(run.stages, key=lambda row: list(StageName).index(row.stage))
        stage_detail = {row.stage.value: row.detail for row in stages}
        schema_detail = stage_detail.get(StageName.SCHEMA_VALIDATION.value, {})
        decision_detail = stage_detail.get(StageName.DECISION.value, {})
        received_detail = stage_detail.get(StageName.RECEIVED.value, {})
        ai_detail = stage_detail.get(StageName.AI_EXTRACTION.value, {})
        if run.invoice is not None:
            fields = run.invoice.extracted_data
            evidence = run.invoice.field_evidence
        else:
            fields = schema_detail.get("extracted_fields", {})
            evidence = schema_detail.get("field_evidence", {})

        checks = [
            CheckResult(
                name=check.check_name,
                status=check.status,
                message=check.message,
                details=check.details,
                recommended_action=check.details.get("recommended_action"),
            )
            for check in run.validation_checks
        ]
        if run.error_message and not checks:
            checks.append(
                CheckResult(
                    name="processing_failure",
                    status=CheckStatus.FAILED,
                    message=run.error_message,
                )
            )
        reasons = decision_detail.get("reasons", [])
        if not reasons and run.error_message:
            reasons = [run.error_message]
        if not reasons and received_detail.get("duplicate_invoice_id"):
            reasons = ["This exact PDF has already been processed."]
        latest_action = _latest_invoice_review_action(run.invoice)
        effective_decision = _effective_decision(run.invoice, run.decision) or Decision.MANUAL_REVIEW
        summary = decision_detail.get(
            "summary",
            "Invoice processing is incomplete." if run.processing_status != ProcessingStatus.COMPLETED
            else f"Invoice decision: {(run.decision or Decision.MANUAL_REVIEW).value}.",
        )
        if run.decision is not None and effective_decision != run.decision:
            summary = (
                f"Reviewer outcome: {effective_decision.value}. "
                f"Original automated outcome: {run.decision.value}."
            )
        result = DecisionResult(
            decision=effective_decision,
            reasons=reasons,
            checks=checks,
            policy_version=run.policy_version,
            summary=summary,
            recommended_action=decision_detail.get(
                "recommended_action",
                "Resolve the processing issue, then upload the invoice again."
                if run.processing_status == ProcessingStatus.FAILED
                else "Review the existing invoice record.",
            ),
            po_impact=(
                POImpact.model_validate(decision_detail["po_impact"])
                if decision_detail.get("po_impact") else None
            ),
            vendor_match_method=decision_detail.get("vendor_match_method"),
        )
        events = [
            StageEvent(
                stage=row.stage,
                status=row.status,
                message=row.message,
                detail=row.detail,
                timestamp=row.completed_at or row.started_at or run.started_at,
            )
            for row in stages
        ]
        return {
            "result": result,
            "fields": fields,
            "evidence": evidence,
            "stages": stages,
            "events": events,
            "cache_hit": bool(ai_detail.get("cache_hit")),
            "file_name": run.source_file_name,
            "source_text": run.extracted_text,
            "automated_decision": run.decision,
            "effective_decision": effective_decision,
            "invoice_active": run.invoice.is_active if run.invoice is not None else None,
            "lifecycle_status": _lifecycle_status(run.invoice, latest_action),
            "latest_review_action": (
                {
                    "action": latest_action.action.value,
                    "reviewer": latest_action.reviewer,
                    "created_at": latest_action.created_at,
                }
                if latest_action is not None else None
            ),
            "supersedes_invoice_id": (
                run.invoice.supersedes_invoice_id if run.invoice is not None else None
            ),
        }


def _reset_data(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        reset_demo_state(session, PROJECT_ROOT / "data")
        session.commit()


def _process_page(factory: sessionmaker[Session], provider_name: str) -> None:
    st.header("Process invoice")
    uploaded = st.file_uploader("Upload a PDF invoice", type=["pdf"])
    if not uploaded or not st.button("Run", type="primary"):
        return
    try:
        provider = _provider(provider_name)
    except ValueError as exc:
        st.error(str(exc))
        return

    pdf_bytes = uploaded.getvalue()
    status_handles: dict[str, Any] = {}
    started_at: dict[str, datetime] = {}
    result: DecisionResult | None = None
    timeline = st.container()
    with timeline:
        for item in process_invoice(factory, provider, pdf_bytes, uploaded.name):
            if isinstance(item, StageEvent):
                update_live_stage(item, status_handles, started_at)
            else:
                result = item
    if result is None:
        st.error("The workflow ended without a decision result.")
        return

    digest = hashlib.sha256(pdf_bytes).hexdigest()
    with factory() as session:
        run = get_latest_run_by_hash(session, digest)
        run_id = run.run_id if run else None
    if run_id is None:
        st.error("The workflow result was not found in the database.")
        return
    detail = _run_detail(factory, run_id)
    if detail is not None:
        st.subheader("Decision")
        if detail["supersedes_invoice_id"]:
            st.info(f"Resubmission of {detail['supersedes_invoice_id']}.")
        render_decision_card(
            detail["result"],
            extracted_fields=detail["fields"],
            field_evidence=detail["evidence"],
            cache_hit=bool(getattr(provider, "last_call_was_cache_hit", False)),
        )


def _dashboard_page(factory: sessionmaker[Session]) -> None:
    st.header("Dashboard")
    with factory() as session:
        counts = _dashboard_outcome_counts(session)
        runs = list_recent_runs(session, limit=15)
        rows = []
        run_ids = []
        for run in runs:
            invoice = run.invoice
            action = _latest_invoice_review_action(invoice)
            extracted = invoice.extracted_data if invoice else (run.llm_response or {})
            elapsed_end = run.completed_at or datetime.now(timezone.utc)
            duration = max(0.0, (elapsed_end - run.started_at).total_seconds())
            effective_decision = _effective_decision(invoice, run.decision)
            rows.append(
                {
                    "Time": run.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "File": run.source_file_name,
                    "Vendor": (
                        invoice.extracted_vendor_name if invoice else extracted.get("vendor_name") or "—"
                    ),
                    "Invoice no.": (
                        invoice.invoice_number if invoice else extracted.get("invoice_number") or "—"
                    ),
                    "Automated decision": _display_decision(run.decision),
                    "Effective decision": _display_decision(effective_decision),
                    "Invoice status": _lifecycle_status(invoice, action),
                    "Reviewer action": action.action.value if action else "—",
                    "Duration (s)": round(duration, 2),
                }
            )
            run_ids.append(run.run_id)
    columns = st.columns(3)
    for column, decision in zip(columns, Decision):
        column.metric(decision.value.replace("_", " ").title(), counts[decision.value])
    if not rows:
        st.info("No invoice runs yet.")
        return

    selected = st.dataframe(
        rows,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
    )
    selected_rows = selected.selection.rows
    if selected_rows:
        index = selected_rows[0]
        detail = _run_detail(factory, run_ids[index])
        if detail is not None:
            st.subheader(f"Run details — {detail['file_name']}")
            _render_decision_metadata(detail)
            if detail["supersedes_invoice_id"]:
                st.info(f"Resubmission of {detail['supersedes_invoice_id']}.")
            render_persisted_timeline(detail["stages"])
            render_decision_card(
                detail["result"],
                extracted_fields=detail["fields"],
                field_evidence=detail["evidence"],
                cache_hit=detail["cache_hit"],
            )


def _review_queue_page(factory: sessionmaker[Session], settings) -> None:
    st.header("Review queue")
    with factory() as session:
        invoices = list_review_queue(session)
        queue_rows = []
        invoice_ids = []
        for invoice in invoices:
            latest = max(invoice.workflow_runs, key=lambda run: run.started_at)
            issues = [
                check.message for check in latest.validation_checks
                if check.status.value in ("FAILED", "WARNING")
            ]
            recommended = next(
                (
                    check.details.get("recommended_action")
                    for check in latest.validation_checks
                    if check.status.value in ("FAILED", "WARNING")
                    and check.details.get("recommended_action")
                ),
                "Review the invoice and determine next steps.",
            )
            queue_rows.append({
                "Invoice": invoice.invoice_number or "Missing invoice number",
                "Vendor": invoice.extracted_vendor_name or "Unknown vendor",
                "Received": invoice.created_at.strftime("%Y-%m-%d %H:%M"),
                "Top reason": issues[0] if issues else "Manual review required.",
                "Recommended action": recommended,
            })
            invoice_ids.append(invoice.invoice_id)
    if not queue_rows:
        st.info("The active review queue is empty.")
        return

    st.dataframe(queue_rows, use_container_width=True, hide_index=True)
    selected_id = st.selectbox(
        "Invoice detail",
        invoice_ids,
        format_func=lambda value: next(
            (f"{row['Invoice']} — {row['Vendor']}" for row, item_id in zip(queue_rows, invoice_ids)
             if item_id == value),
            value,
        ),
    )
    # selected_id is an invoice id, so resolve its latest workflow run.
    with factory() as session:
        invoice_row = session.scalar(
            select(InvoiceORM)
            .where(InvoiceORM.invoice_id == selected_id)
            .options(selectinload(InvoiceORM.workflow_runs))
        )
        run_id = (
            max(invoice_row.workflow_runs, key=lambda run: run.started_at).run_id
            if invoice_row is not None and invoice_row.workflow_runs else None
        )
    if run_id is None:
        st.error("The selected review item has no workflow run.")
        return
    detail = _run_detail(factory, run_id)
    if detail is None:
        st.error("The selected workflow run could not be loaded.")
        return
    if detail["supersedes_invoice_id"]:
        st.info(f"Resubmission of {detail['supersedes_invoice_id']}.")
    _render_decision_metadata(detail)
    render_persisted_timeline(detail["stages"])
    render_decision_card(
        detail["result"],
        extracted_fields=detail["fields"],
        field_evidence=detail["evidence"],
        cache_hit=detail["cache_hit"],
    )
    with st.expander("Source document text"):
        st.text(detail["source_text"] or "No source text was persisted.")

    if not _review_actions_authorized(settings):
        return

    reviewer = st.text_input("Reviewer name", key=f"reviewer-{selected_id}")
    comment = st.text_area("Comment / reason", key=f"review-comment-{selected_id}")
    approve_col, reject_col, request_col = st.columns(3)
    action = None
    if approve_col.button("Approve", key=f"approve-{selected_id}"):
        action = ReviewActionType.APPROVE
    elif reject_col.button("Reject", key=f"reject-{selected_id}"):
        action = ReviewActionType.REJECT
    elif request_col.button("Request information", key=f"request-{selected_id}"):
        action = ReviewActionType.REQUEST_INFORMATION
    if action is not None:
        try:
            with factory() as session:
                outcome = apply_review_action(
                    session,
                    selected_id,
                    action,
                    reviewer=reviewer,
                    comment=comment,
                )
                session.commit()
            st.success(f"Reviewer action recorded: {action.value}.")
            if outcome.generated_email:
                st.text_area("Generated vendor email", outcome.generated_email, height=220)
        except ValueError as exc:
            st.error(str(exc))


def _manifest_page(provider_name: str, settings) -> None:
    if settings.is_production:
        st.error("Manifest execution is disabled in production.")
        return
    st.header("Run all fixtures")
    test_database_url = os.getenv("PG_TEST_URI")
    if not test_database_url:
        st.info("Configure PG_TEST_URI for a separate disposable test database to run the manifest.")
        return
    from scripts.run_manifest import run_manifest, validate_manifest_database_url

    try:
        test_database_url = validate_manifest_database_url(
            test_database_url,
            settings.database_url,
        )
    except ValueError as exc:
        st.error(f"Manifest execution is unavailable: {exc}")
        return

    reset = False
    if settings.enable_demo_reset:
        reset = st.checkbox(
            "Reset the disposable manifest database before running",
            key="manifest-reset",
        )
    else:
        st.caption("Manifest database reset is disabled by configuration.")
    if not st.button("Run manifest", type="primary"):
        return
    try:
        provider = _provider(provider_name)
    except ValueError as exc:
        st.error(str(exc))
        return

    engine = create_db_engine(test_database_url)
    try:
        init_db(engine)
        manifest_factory = create_session_factory(engine)
        with manifest_factory.begin() as session:
            seed_reference_data(session, PROJECT_ROOT / "data")
        report = run_manifest(provider, manifest_factory, reset=reset)
    except ValueError as exc:
        st.error(f"Manifest execution failed: {exc}")
        return
    finally:
        engine.dispose()

    st.dataframe(
        [
            {
                "Fixture": row["fixture"],
                "Expected decision": row["expected_decision"],
                "Actual decision": row["actual_decision"],
                "Expected / present checks": (
                    ", ".join(row["expected_checks"]) + " / " + ", ".join(row["present_checks"])
                ),
                "Pass": row["pass"],
                "Post-action error": row.get("post_action_error") or "",
                "Duration (s)": round(row["duration_seconds"], 2),
                "Cache hit": row["cache_hit"],
            }
            for row in report["rows"]
        ],
        use_container_width=True,
        hide_index=True,
    )
    for row in report["rows"]:
        st.write(f"**{row['fixture']}** — {row['summary']}")
        if row.get("post_action_error"):
            st.error(row["post_action_error"])
    if report["passed"]:
        st.success(f"Manifest passed in {report['total_seconds']:.2f}s.")
    else:
        st.error("One or more manifest cases failed.")


def main() -> None:
    st.set_page_config(page_title="InvoiceOps", page_icon="🧾", layout="wide")
    st.markdown(
        """
        <style>
        .decision-card { border: 1px solid #d6dbe1; border-radius: 12px; padding: 1rem 1.2rem; }
        .decision-badge { display: inline-block; border-radius: 999px; padding: .45rem .8rem;
                          font-size: 1.15rem; font-weight: 700; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    settings = get_settings()
    _engine, factory = get_resources(
        settings.database_url,
        settings.auto_seed_reference_data,
    )
    st.title("InvoiceOps")
    page = st.sidebar.radio("Page", _page_options(settings))
    provider_name = st.sidebar.selectbox("Extraction provider", ["Gemini", "Replay"])
    if settings.enable_demo_reset:
        confirm_reset = st.sidebar.checkbox("I understand this deletes demo processing history")
        if st.sidebar.button("Reset demo data"):
            if not confirm_reset:
                st.sidebar.error("Confirm the reset checkbox first.")
            else:
                _reset_data(factory)
                st.sidebar.success("Demo data reset.")
    else:
        st.sidebar.caption("Demo reset is disabled by configuration.")

    if not settings.auto_seed_reference_data:
        st.sidebar.caption("Automatic reference-data seeding is disabled.")

    if page == "Process invoice":
        _process_page(factory, provider_name)
    elif page == "Dashboard":
        _dashboard_page(factory)
    elif page == "Review queue":
        _review_queue_page(factory, settings)
    elif page == "Run all fixtures":
        _manifest_page(provider_name, settings)


if __name__ == "__main__":
    main()
