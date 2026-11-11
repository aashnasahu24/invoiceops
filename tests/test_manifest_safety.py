import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

import app
from invoiceops.config import get_settings
from invoiceops.db import create_db_engine, create_session_factory
from invoiceops.models import InvoiceORM, ReviewActionORM, WorkflowRunORM
from invoiceops.schemas import Decision, ProcessingStatus, ReviewActionType
from scripts import run_manifest as manifest_runner


def test_manifest_target_must_be_disposable_and_not_application_database():
    app_url = "postgresql://app:secret@ep-app.neon.tech/neondb?sslmode=require"

    with pytest.raises(ValueError, match="separate from DATABASE_URL") as error:
        manifest_runner.validate_manifest_database_url(app_url, app_url)
    assert "secret" not in str(error.value)

    with pytest.raises(ValueError, match="test-named"):
        manifest_runner.validate_manifest_database_url(
            "postgresql://test:secret@ep-test.neon.tech/neondb?sslmode=require",
            app_url,
        )


def test_manifest_target_accepts_separate_test_postgres_and_temp_sqlite(tmp_path):
    app_url = "postgresql://app:secret@ep-app.neon.tech/neondb?sslmode=require"
    assert manifest_runner.validate_manifest_database_url(
        "postgresql://test:secret@ep-test.neon.tech/invoiceops_test?sslmode=require",
        app_url,
    ).startswith("postgresql+psycopg://")

    temp_sqlite = f"sqlite:///{tmp_path / 'manifest_test.db'}"
    assert manifest_runner.validate_manifest_database_url(temp_sqlite, app_url) == temp_sqlite


def test_manifest_runner_rejects_session_bound_to_database_url(tmp_path, monkeypatch):
    app_url = f"sqlite:///{tmp_path / 'invoiceops.db'}"
    monkeypatch.setattr(
        manifest_runner,
        "get_settings",
        lambda: SimpleNamespace(database_url=app_url),
    )
    engine = create_db_engine(app_url)
    try:
        factory = create_session_factory(engine)
        with pytest.raises(ValueError, match="separate from DATABASE_URL"):
            manifest_runner.run_manifest(object(), factory)
    finally:
        engine.dispose()


def test_manifest_cli_never_falls_back_to_database_url(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///invoiceops.db")
    monkeypatch.setenv("PG_TEST_URI", "")
    get_settings.cache_clear()
    try:
        # An explicit empty PG_TEST_URI is not replaced by load_dotenv().
        result = manifest_runner.main(["--provider", "replay"])
    finally:
        get_settings.cache_clear()
    assert result == 2
    assert "requires PG_TEST_URI" in capsys.readouterr().err


def test_manifest_post_action_error_is_reported_without_bypassing_reviewer_validation(db_session):
    digest = "a" * 64
    invoice = InvoiceORM(
        invoice_id="returned-invoice",
        file_name="returned.pdf",
        file_sha256=digest,
        extracted_data={},
        field_evidence={},
        extraction_warnings=[],
        current_decision=Decision.MANUAL_REVIEW,
        is_active=False,
    )
    run = WorkflowRunORM(
        run_id="returned-run",
        source_file_name="returned.pdf",
        source_file_sha256=digest,
        processing_status=ProcessingStatus.COMPLETED,
        decision=Decision.MANUAL_REVIEW,
        invoice=invoice,
    )
    run.review_actions.append(
        ReviewActionORM(
            action=ReviewActionType.REQUEST_INFORMATION,
            reviewer="Reviewer",
            comments="Already returned.",
        )
    )
    db_session.add(run)
    db_session.commit()
    factory = create_session_factory(db_session.get_bind())

    error = manifest_runner._apply_manifest_post_action(
        factory,
        digest,
        "returned.pdf",
        {
            "action": "REQUEST_INFORMATION",
            "reviewer": "Reviewer",
            "comment": "Repeated action.",
        },
    )

    assert error is not None
    assert "Reviewer post-action rejected for returned.pdf" in error
    assert "Only active invoices" in error
    assert db_session.scalar(select(func.count()).select_from(ReviewActionORM)) == 1


def test_manifest_report_marks_rejected_post_action_as_failed(
    db_session,
    tmp_path,
    monkeypatch,
):
    pdf_bytes = b"synthetic returned invoice"
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "returned.pdf").write_bytes(pdf_bytes)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "cases": [{
            "file": "returned.pdf",
            "expected_decision": Decision.MANUAL_REVIEW.value,
            "must_include_checks": [],
            "post_action": {
                "action": ReviewActionType.REQUEST_INFORMATION.value,
                "reviewer": "Reviewer",
                "comment": "Repeated return action.",
            },
        }]
    }), encoding="utf-8")

    invoice = InvoiceORM(
        invoice_id="report-returned-invoice",
        file_name="returned.pdf",
        file_sha256=digest,
        extracted_data={},
        field_evidence={},
        extraction_warnings=[],
        current_decision=Decision.MANUAL_REVIEW,
        is_active=False,
    )
    run = WorkflowRunORM(
        run_id="report-returned-run",
        source_file_name="returned.pdf",
        source_file_sha256=digest,
        processing_status=ProcessingStatus.COMPLETED,
        decision=Decision.MANUAL_REVIEW,
        invoice=invoice,
    )
    run.review_actions.append(
        ReviewActionORM(
            action=ReviewActionType.REQUEST_INFORMATION,
            reviewer="Reviewer",
            comments="Already returned.",
        )
    )
    db_session.add(run)
    db_session.commit()

    monkeypatch.setattr(
        manifest_runner,
        "process_invoice",
        lambda *_args, **_kwargs: [
            SimpleNamespace(decision=Decision.MANUAL_REVIEW, checks=[], summary="Manual review")
        ],
    )
    factory = create_session_factory(db_session.get_bind())
    report = manifest_runner.run_manifest(
        provider=object(),
        session_factory=factory,
        manifest_path=manifest_path,
        samples_dir=sample_dir,
    )

    assert report["passed"] is False
    assert "Only active invoices" in report["rows"][0]["post_action_error"]
    assert report["rows"][0]["pass"] is False


def test_production_hides_manifest_page_and_direct_page_entry_is_read_only(monkeypatch):
    settings = SimpleNamespace(is_production=True)
    assert "Run all fixtures" not in app._page_options(settings)
    errors = []
    monkeypatch.setattr(app.st, "error", errors.append)

    app._manifest_page("Replay", settings)

    assert errors == ["Manifest execution is disabled in production."]
