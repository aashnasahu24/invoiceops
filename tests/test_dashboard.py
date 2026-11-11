from types import SimpleNamespace

import app
from invoiceops.db import create_db_engine, create_session_factory
from invoiceops.models import (
    InvoiceORM,
    POLineItemORM,
    PurchaseOrderORM,
    ReviewActionORM,
    VendorORM,
    WorkflowRunORM,
)
from invoiceops.schemas import Decision, ProcessingStatus, ReviewActionType


def test_dashboard_uses_effective_decision_for_outcome_counts(db_session):
    invoice = InvoiceORM(
        invoice_id="reviewed-invoice",
        file_name="reviewed.pdf",
        extracted_data={},
        field_evidence={},
        extraction_warnings=[],
        current_decision=Decision.APPROVED,
    )
    db_session.add(invoice)
    db_session.commit()

    counts = app._dashboard_outcome_counts(db_session)

    assert counts[Decision.APPROVED.value] == 1
    assert counts[Decision.MANUAL_REVIEW.value] == 0
    assert app._effective_decision(invoice, Decision.MANUAL_REVIEW) == Decision.APPROVED


def test_returned_invoice_lifecycle_uses_persisted_request_information_action():
    invoice = SimpleNamespace(is_active=False)
    action = SimpleNamespace(action=ReviewActionType.REQUEST_INFORMATION)

    assert app._lifecycle_status(invoice, action) == "Returned — information requested"


def test_latest_invoice_action_is_selected_across_workflow_runs():
    first = SimpleNamespace(
        action=ReviewActionType.REQUEST_INFORMATION,
        created_at=1,
    )
    latest = SimpleNamespace(action=ReviewActionType.REJECT, created_at=2)
    invoice = SimpleNamespace(
        workflow_runs=[
            SimpleNamespace(review_actions=[first]),
            SimpleNamespace(review_actions=[latest]),
        ]
    )

    assert app._latest_invoice_review_action(invoice) is latest


def test_run_detail_displays_effective_decision_without_changing_automated_decision(db_session):
    invoice = InvoiceORM(
        invoice_id="effective-detail",
        file_name="effective.pdf",
        extracted_data={},
        field_evidence={},
        extraction_warnings=[],
        current_decision=Decision.APPROVED,
    )
    run = WorkflowRunORM(
        run_id="effective-detail-run",
        source_file_name="effective.pdf",
        processing_status=ProcessingStatus.COMPLETED,
        decision=Decision.MANUAL_REVIEW,
        invoice=invoice,
    )
    run.review_actions.append(
        ReviewActionORM(action=ReviewActionType.APPROVE, reviewer="Reviewer")
    )
    db_session.add(run)
    db_session.commit()

    factory = create_session_factory(db_session.get_bind())
    detail = app._run_detail(factory, "effective-detail-run")

    assert detail["automated_decision"] == Decision.MANUAL_REVIEW
    assert detail["effective_decision"] == Decision.APPROVED
    assert detail["result"].decision == Decision.APPROVED
    assert detail["latest_review_action"]["action"] == ReviewActionType.APPROVE.value
    assert detail["lifecycle_status"] == "Active"


def test_startup_seeding_can_be_disabled_or_enabled(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'seed-control.db'}")
    try:
        app._initialize_app_database(engine, seed_on_startup=False)
        factory = create_session_factory(engine)
        with factory() as session:
            assert session.query(VendorORM).count() == 0
            assert session.query(PurchaseOrderORM).count() == 0
            assert session.query(POLineItemORM).count() == 0

        app._initialize_app_database(engine, seed_on_startup=True)
        with factory() as session:
            assert session.query(VendorORM).count() == 4
            assert session.query(PurchaseOrderORM).count() == 5
            assert session.query(POLineItemORM).count() == 8
    finally:
        engine.dispose()


def test_production_reviewer_actions_require_enabled_flag_and_access_key(monkeypatch):
    messages = []
    monkeypatch.setattr(app.st, "info", messages.append)
    settings = SimpleNamespace(
        enable_reviewer_actions=False,
        reviewer_access_key=None,
        is_production=True,
    )
    assert not app._review_actions_authorized(settings)

    settings.enable_reviewer_actions = True
    assert not app._review_actions_authorized(settings)

    settings.reviewer_access_key = "secret"
    monkeypatch.setattr(app.st, "text_input", lambda *_args, **_kwargs: "wrong")
    assert not app._review_actions_authorized(settings)
    monkeypatch.setattr(app.st, "text_input", lambda *_args, **_kwargs: "secret")
    assert app._review_actions_authorized(settings)
