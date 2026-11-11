"""Reusable presentation components for the Streamlit vertical slice."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any

import streamlit as st

from .schemas import Decision, DecisionResult, StageEvent, StageStatus

_BADGE_COLORS = {
    Decision.APPROVED: ("#176b3a", "#e7f5ec"),
    Decision.MANUAL_REVIEW: ("#8a5a00", "#fff4d6"),
    Decision.REJECTED: ("#9f2525", "#fdeaea"),
}


def _warning_messages(detail: Any) -> list[str]:
    """Return safe warning strings from current or legacy stage detail data."""
    if not isinstance(detail, dict):
        return []
    warnings = detail.get("warnings")
    if warnings is None:
        return []
    if isinstance(warnings, str):
        return [warnings] if warnings else []
    if isinstance(warnings, (list, tuple)):
        return [warning for warning in warnings if isinstance(warning, str) and warning]
    return []


def render_decision_card(
    result: DecisionResult,
    *,
    extracted_fields: dict[str, Any] | None = None,
    field_evidence: dict[str, dict[str, Any]] | None = None,
    cache_hit: bool = False,
) -> None:
    """Render a decision and its supporting fields/checks without business logic."""
    foreground, background = _BADGE_COLORS[result.decision]
    st.markdown(
        f'<div class="decision-card"><span class="decision-badge" '
        f'style="color:{foreground};background:{background}">'
        f'{html.escape(result.decision.value)}</span>'
        f'<p>{html.escape(result.summary)}</p></div>',
        unsafe_allow_html=True,
    )
    if cache_hit:
        st.caption("LLM response served from replay cache.")
    if result.vendor_match_method:
        st.caption(f"Vendor matched by {result.vendor_match_method}.")
    if result.reasons:
        st.markdown("**Reasons**")
        for reason in result.reasons:
            st.write(f"- {reason}")
    st.markdown("**Recommended action**")
    st.write(result.recommended_action or "No additional action.")
    if result.po_impact:
        impact = result.po_impact
        st.markdown("**PO impact**")
        st.write(
            f"{impact.po_number}: {impact.this_invoice:,.2f} this invoice; "
            f"{impact.previously_invoiced:,.2f} previously invoiced; "
            f"{impact.remaining_after:,.2f} remaining; status after invoice: "
            f"{impact.status_after.value}."
        )

    st.markdown("**Extracted fields**")
    extracted_fields = extracted_fields or {}
    field_evidence = field_evidence or {}
    field_rows = []
    for name, value in extracted_fields.items():
        evidence = field_evidence.get(name, {})
        verified = evidence.get("verified")
        marker = "Verified" if verified is True else "Unverified" if verified is False else "No evidence"
        snippet = evidence.get("snippet") or ""
        field_rows.append(
            {"Field": name, "Value": value, "Evidence": snippet, "Source check": marker}
        )
    if field_rows:
        st.dataframe(field_rows, use_container_width=True, hide_index=True)
    else:
        st.info("No extracted fields were persisted for this run.")

    st.markdown("**Validation checks**")
    if result.checks:
        st.dataframe(
            [
                {
                    "Check": check.name,
                    "Status": check.status.value,
                    "Message": check.message,
                    "Recommended action": check.recommended_action or "",
                }
                for check in result.checks
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("No validation checks were reached.")


def render_stage_timeline(events: list[StageEvent]) -> None:
    """Render persisted stage events in stage order, including elapsed time."""
    started: dict[str, datetime] = {}
    by_stage: dict[str, StageEvent] = {}
    for event in events:
        if event.status == StageStatus.RUNNING:
            started[event.stage.value] = event.timestamp
        by_stage[event.stage.value] = event

    for stage_name, event in by_stage.items():
        start = started.get(stage_name)
        elapsed = (event.timestamp - start).total_seconds() if start else 0
        state = "complete" if event.status == StageStatus.COMPLETED else (
            "error" if event.status == StageStatus.FAILED else "running"
        )
        status = st.status(
            f"{event.stage.value} — {event.status.value} — {elapsed:.2f}s",
            state=state,
            expanded=False,
        )
        if event.message:
            status.write(event.message)
        warnings = _warning_messages(event.detail)
        if warnings:
            status.write("Warnings: " + "; ".join(warnings))


def update_live_stage(
    event: StageEvent,
    status_handles: dict[str, Any],
    started_at: dict[str, datetime],
) -> None:
    """Create/update a Streamlit status element for one yielded stage event."""
    stage = event.stage.value
    if event.status == StageStatus.RUNNING:
        started_at[stage] = event.timestamp
        status_handles[stage] = st.status(
            f"{stage} — RUNNING",
            state="running",
            expanded=True,
        )
        if event.message:
            status_handles[stage].write(event.message)
    else:
        start = started_at.get(stage)
        elapsed = (event.timestamp - start).total_seconds() if start else 0
        state = "complete" if event.status == StageStatus.COMPLETED else (
            "error" if event.status == StageStatus.FAILED else "complete"
        )
        handle = status_handles.get(stage)
        if handle is None:
            handle = st.status(
                f"{stage} — {event.status.value} — {elapsed:.2f}s",
                state=state,
                expanded=False,
            )
            status_handles[stage] = handle
        else:
            handle.update(
                label=f"{stage} — {event.status.value} — {elapsed:.2f}s",
                state=state,
                expanded=False,
            )
        if event.message:
            handle.write(event.message)
        warnings = _warning_messages(event.detail)
        if warnings:
            handle.write("Warnings: " + "; ".join(warnings))


def render_persisted_timeline(stage_rows: list[Any]) -> None:
    """Display one final persisted status row per workflow stage."""
    for row in stage_rows:
        state = "complete" if row.status == StageStatus.COMPLETED else (
            "error" if row.status == StageStatus.FAILED else "running"
        )
        status = st.status(
            f"{row.stage.value} — {row.status.value}",
            state=state,
            expanded=False,
        )
        if row.message:
            status.write(row.message)
