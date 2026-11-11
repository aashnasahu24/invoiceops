from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from invoiceops import config
from invoiceops.db import create_db_engine, create_session_factory, init_db
from invoiceops.models import (
    InvoiceORM,
    RunStageORM,
    WorkflowRunORM,
)
from invoiceops.schemas import Decision, ProcessingStatus, StageName, StageStatus
from invoiceops.seed import reset_demo_state, seed_reference_data

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db_session(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 't.db'}")
    init_db(engine)
    with create_session_factory(engine)() as session:
        yield session
    engine.dispose()


def make_invoice(invoice_id, number, sha, **kw):
    return InvoiceORM(
        invoice_id=invoice_id, vendor_id="V001", invoice_number=number,
        invoice_number_normalized=number.replace("-", ""), file_name=f"{invoice_id}.pdf",
        file_sha256=sha * 64, extracted_data={}, field_evidence={}, extraction_warnings=[], **kw,
    )


# --- configuration ------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    ["postgres://u:p@host/db", "postgresql://u:p@host/db", "  postgresql://u:p@host/db  "],
)
def test_provider_style_postgres_urls_use_the_psycopg_driver(raw):
    assert config.normalize_database_url(raw) == "postgresql+psycopg://u:p@host/db"


def test_explicit_driver_and_sqlite_urls_are_left_alone():
    assert config.normalize_database_url("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert config.normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"


def test_default_database_is_an_absolute_path_and_empty_env_falls_back(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")  # `.env.example` ships this blank
    config.get_settings.cache_clear()
    try:
        url = config.get_settings().database_url
    finally:
        config.get_settings.cache_clear()
    assert url.startswith("sqlite:///")
    assert Path(url.removeprefix("sqlite:///")).is_absolute()


def test_environment_controls_are_safe_by_default_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    for name in (
        "AUTO_SEED_REFERENCE_DATA",
        "ENABLE_DEMO_RESET",
        "ENABLE_REVIEWER_ACTIONS",
        "REVIEWER_ACCESS_KEY",
    ):
        monkeypatch.setenv(name, "")
    config.get_settings.cache_clear()
    try:
        settings = config.get_settings()
    finally:
        config.get_settings.cache_clear()

    assert settings.is_production
    assert not settings.auto_seed_reference_data
    assert not settings.enable_demo_reset
    assert not settings.enable_reviewer_actions
    assert settings.reviewer_access_key is None


def test_environment_controls_keep_local_demo_convenient(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    for name in (
        "AUTO_SEED_REFERENCE_DATA",
        "ENABLE_DEMO_RESET",
        "ENABLE_REVIEWER_ACTIONS",
    ):
        monkeypatch.setenv(name, "")
    config.get_settings.cache_clear()
    try:
        settings = config.get_settings()
    finally:
        config.get_settings.cache_clear()

    assert not settings.is_production
    assert settings.auto_seed_reference_data
    assert settings.enable_demo_reset
    assert settings.enable_reviewer_actions


def test_environment_controls_allow_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTO_SEED_REFERENCE_DATA", "true")
    monkeypatch.setenv("ENABLE_DEMO_RESET", "true")
    monkeypatch.setenv("ENABLE_REVIEWER_ACTIONS", "true")
    monkeypatch.setenv("REVIEWER_ACCESS_KEY", "review-key")
    config.get_settings.cache_clear()
    try:
        settings = config.get_settings()
    finally:
        config.get_settings.cache_clear()

    assert settings.auto_seed_reference_data
    assert settings.enable_demo_reset
    assert settings.enable_reviewer_actions
    assert settings.reviewer_access_key == "review-key"


def test_production_seed_cli_rejects_reset_by_default(monkeypatch):
    from scripts import seed_data

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENABLE_DEMO_RESET", "")
    monkeypatch.setattr("sys.argv", ["seed_data", "--reset"])
    config.get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as exit_info:
            seed_data.main()
    finally:
        config.get_settings.cache_clear()

    assert exit_info.value.code == 2


def test_gemini_settings_are_read_without_openai_settings(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "test-model")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    config.get_settings.cache_clear()
    try:
        settings = config.get_settings()
    finally:
        config.get_settings.cache_clear()
    assert settings.gemini_api_key == "test-gemini-key"
    assert settings.gemini_model == "test-model"
    assert not hasattr(settings, "openai_api_key")


# --- datetimes ---------------------------------------------------------------------

def test_datetimes_round_trip_timezone_aware(db_session):
    start = datetime.now(timezone.utc) - timedelta(seconds=5)
    db_session.add(WorkflowRunORM(run_id="r1", source_file_name="a.pdf",
                                  processing_status=ProcessingStatus.COMPLETED, started_at=start))
    db_session.commit()
    db_session.expire_all()
    saved = db_session.get(WorkflowRunORM, "r1")
    assert saved.started_at.tzinfo is not None
    assert (datetime.now(timezone.utc) - saved.started_at).total_seconds() >= 5  # no TypeError


def test_naive_datetimes_are_rejected(db_session):
    db_session.add(WorkflowRunORM(run_id="r2", source_file_name="a.pdf",
                                  processing_status=ProcessingStatus.RECEIVED,
                                  started_at=datetime(2026, 1, 1)))
    with pytest.raises(Exception):
        db_session.commit()
    db_session.rollback()


# --- invoice identity: active-only uniqueness --------------------------------------

def test_two_active_invoices_cannot_share_a_business_key(db_session):
    seed_reference_data(db_session, ROOT / "data")
    db_session.add_all([make_invoice("a", "INV-1", "a"), make_invoice("b", "INV-1", "b")])
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_returned_invoice_does_not_block_a_corrected_resubmission(db_session):
    seed_reference_data(db_session, ROOT / "data")
    db_session.add(make_invoice("v1", "INV-1", "a", is_active=False, current_decision=Decision.MANUAL_REVIEW))
    db_session.commit()

    db_session.add(make_invoice("v2", "INV-1", "b", supersedes_invoice_id="v1"))
    db_session.commit()  # allowed: v1 is inactive

    active = db_session.scalars(select(InvoiceORM).where(InvoiceORM.is_active.is_(True))).all()
    assert [i.invoice_id for i in active] == ["v2"]
    assert db_session.get(InvoiceORM, "v2").supersedes_invoice_id == "v1"


def test_money_survives_a_round_trip_exactly(db_session):
    seed_reference_data(db_session, ROOT / "data")
    db_session.add(make_invoice("m", "INV-9", "c", subtotal=Decimal("33333.33"),
                                net_amount=Decimal("33333.33")))
    db_session.commit()
    db_session.expire_all()
    saved = db_session.get(InvoiceORM, "m")
    assert saved.net_amount == Decimal("33333.33")
    assert isinstance(saved.net_amount, Decimal)


# --- stage tracking & audit trail --------------------------------------------------

def test_run_stages_and_audit_text_persist_and_cascade(db_session):
    seed_reference_data(db_session, ROOT / "data")
    now = datetime.now(timezone.utc)
    run = WorkflowRunORM(
        run_id="run-1", source_file_name="a.pdf", processing_status=ProcessingStatus.COMPLETED,
        extracted_text="[Page 1]\nTAX INVOICE", llm_response={"invoice_number": "INV-1"},
    )
    run.stages.append(RunStageORM(stage=StageName.TEXT_EXTRACTION, status=StageStatus.COMPLETED,
                                  message="1 page, PDF_TEXT", detail={"pages": 1},
                                  started_at=now, completed_at=now))
    run.stages.append(RunStageORM(stage=StageName.AI_EXTRACTION, status=StageStatus.FAILED,
                                  message="timeout", started_at=now))
    db_session.add(run)
    db_session.commit()

    saved = db_session.get(WorkflowRunORM, "run-1")
    assert [s.stage for s in saved.stages] == [StageName.TEXT_EXTRACTION, StageName.AI_EXTRACTION]
    assert saved.llm_response["invoice_number"] == "INV-1"

    db_session.delete(saved)
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(RunStageORM)) == 0


def test_one_row_per_stage_per_run(db_session):
    db_session.add(WorkflowRunORM(run_id="r", source_file_name="a.pdf",
                                  processing_status=ProcessingStatus.PROCESSING))
    db_session.add_all([
        RunStageORM(run_id="r", stage=StageName.MATCHING, status=StageStatus.RUNNING),
        RunStageORM(run_id="r", stage=StageName.MATCHING, status=StageStatus.COMPLETED),
    ])
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# --- demo reset --------------------------------------------------------------------

def test_reset_demo_state_clears_history_and_restores_reference_data(db_session):
    seed_reference_data(db_session, ROOT / "data")
    first = make_invoice("i1", "INV-1", "a", current_decision=Decision.APPROVED)
    second = make_invoice("i2", "INV-1", "b", supersedes_invoice_id="i1")
    first.is_active = False
    run = WorkflowRunORM(run_id="r1", invoice=first, source_file_name="a.pdf",
                         processing_status=ProcessingStatus.COMPLETED, decision=Decision.APPROVED)
    run.stages.append(RunStageORM(stage=StageName.DECISION, status=StageStatus.COMPLETED))
    db_session.add_all([first, second, run])
    db_session.commit()

    counts = reset_demo_state(db_session, ROOT / "data")
    db_session.commit()

    assert counts["invoices"] == 2 and counts["workflow_runs"] == 1 and counts["run_stages"] == 1
    assert db_session.scalar(select(func.count()).select_from(InvoiceORM)) == 0
    assert db_session.scalar(select(func.count()).select_from(WorkflowRunORM)) == 0
    assert counts["vendors"] == 4 and counts["purchase_orders"] == 5

    # The happy-path fixture is processable again after a reset.
    db_session.add(make_invoice("again", "INV-1", "a"))
    db_session.commit()
