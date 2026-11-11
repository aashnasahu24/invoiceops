import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from invoiceops.db import create_db_engine, create_session_factory, init_db
from invoiceops.models import InvoiceORM, RunStageORM, WorkflowRunORM
from invoiceops.orchestrator import process_invoice
from invoiceops.pdf_processor import OCRUnavailableError, PDFProcessor
from invoiceops.providers.errors import ProviderResponseError
from invoiceops.schemas import Decision, ProcessingStatus, StageName, StageStatus
from invoiceops.seed import seed_reference_data

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "sample_invoices"
LABELS = json.loads((ROOT / "data" / "expected_extractions.json").read_text(encoding="utf-8"))


class FakeProvider:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def generate_structured_output(self, *, instructions, document_text, json_schema, file_sha256):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


def fixture_response(filename):
    label = LABELS[filename]
    response = {
        "invoice_number": label.get("invoice_number"),
        "invoice_date": label.get("invoice_date"),
        "due_date": label.get("due_date"),
        "vendor_name": label.get("vendor_name"),
        "vendor_tax_id": label.get("vendor_tax_id"),
        "po_number": label.get("po_number"),
        "currency": label.get("currency"),
        "subtotal": label.get("subtotal"),
        "tax_total": label.get("tax_total"),
        "shipping": label.get("shipping"),
        "discount": label.get("discount"),
        "grand_total": label.get("grand_total"),
        "line_items": [
            {
                "description": item["description"],
                "sku": item.get("sku"),
                "quantity": item["quantity"],
                "unit_price": item["unit_price"],
                "line_total": item["line_total"],
                "tax_rate": item.get("tax_rate"),
            }
            for item in label["line_items"]
        ],
        "evidence": [],
    }
    return response


@pytest.fixture
def session_factory(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'orchestrator.db'}")
    init_db(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        seed_reference_data(session, ROOT / "data")
        session.commit()
    yield factory
    engine.dispose()


def process(factory, provider, filename="happy_path_invoice.pdf", **kwargs):
    return list(
        process_invoice(
            factory,
            provider,
            (SAMPLES / filename).read_bytes(),
            filename,
            **kwargs,
        )
    )


def test_happy_path_persists_invoice_and_stages_in_order(session_factory):
    provider = FakeProvider(fixture_response("happy_path_invoice.pdf"))
    yielded = process(session_factory, provider)
    result = yielded[-1]
    assert result.decision == Decision.APPROVED

    with session_factory() as session:
        runs = session.scalars(select(WorkflowRunORM)).all()
        assert len(runs) == 1
        run = runs[0]
        assert run.processing_status == ProcessingStatus.COMPLETED
        assert run.invoice.invoice_number == "INV-2026-104"
        assert run.extracted_text
        assert run.llm_response["invoice_number"] == "INV-2026-104"
        assert [row.stage for row in run.stages] == list(StageName)
        assert all(row.status == StageStatus.COMPLETED for row in run.stages)
        assert session.scalar(select(func.count()).select_from(InvoiceORM)) == 1
        assert [event.stage for event in yielded[:-1] if event.status == StageStatus.RUNNING] == list(StageName)


def test_exact_hash_duplicate_rejected_without_second_invoice(session_factory):
    provider = FakeProvider(fixture_response("happy_path_invoice.pdf"))
    first = process(session_factory, provider)
    second = process(session_factory, provider)
    assert first[-1].decision == Decision.APPROVED
    assert second[-1].decision == Decision.REJECTED
    assert second[-1].checks[0].name == "duplicate_file"
    assert provider.calls == 1
    assert any(event.status == StageStatus.SKIPPED for event in second[:-1])
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(InvoiceORM)) == 1
        assert session.scalar(select(func.count()).select_from(WorkflowRunORM)) == 2


def test_provider_failure_routes_to_failed_manual_review(session_factory):
    provider = FakeProvider(error=ProviderResponseError("blocked response"))
    yielded = process(session_factory, provider)
    assert yielded[-1].decision == Decision.MANUAL_REVIEW
    assert any(
        event.stage == StageName.AI_EXTRACTION and event.status == StageStatus.FAILED
        for event in yielded[:-1]
    )
    with session_factory() as session:
        run = session.scalar(select(WorkflowRunORM))
        assert run.processing_status == ProcessingStatus.FAILED
        assert run.decision == Decision.MANUAL_REVIEW
        assert run.stages[-1].status == StageStatus.SKIPPED
        assert session.scalar(select(func.count()).select_from(InvoiceORM)) == 0


def test_ocr_unavailable_routes_to_failed_manual_review(session_factory, monkeypatch):
    def fail_ocr(self, source, *, file_name=None):
        raise OCRUnavailableError("Tesseract is missing")

    monkeypatch.setattr(PDFProcessor, "process", fail_ocr)
    yielded = process(
        session_factory,
        FakeProvider(fixture_response("scanned_missing_po.pdf")),
        "scanned_missing_po.pdf",
    )
    assert yielded[-1].decision == Decision.MANUAL_REVIEW
    assert yielded[-1].reasons == ["Tesseract is missing"]
    assert any(
        event.stage == StageName.TEXT_EXTRACTION and event.status == StageStatus.FAILED
        for event in yielded[:-1]
    )
    assert all(
        event.status == StageStatus.SKIPPED
        for event in yielded[:-1]
        if event.stage not in (StageName.RECEIVED, StageName.TEXT_EXTRACTION)
    )


def test_invalid_pdf_routes_to_failed_manual_review(session_factory):
    yielded = list(
        process_invoice(
            session_factory,
            FakeProvider(fixture_response("happy_path_invoice.pdf")),
            b"not a PDF file",
            "broken.pdf",
        )
    )
    assert yielded[-1].decision == Decision.MANUAL_REVIEW
    assert any(
        event.stage == StageName.TEXT_EXTRACTION and event.status == StageStatus.FAILED
        for event in yielded[:-1]
    )


def test_scanned_fixture_with_missing_po_requires_manual_review(session_factory):
    document = PDFProcessor().process(SAMPLES / "scanned_missing_po.pdf")
    if document.document_type.value != "SCANNED_PDF":
        pytest.skip("OCR is unavailable for this fixture")

    yielded = process(
        session_factory,
        FakeProvider(fixture_response("scanned_missing_po.pdf")),
        "scanned_missing_po.pdf",
    )

    assert yielded[-1].decision == Decision.MANUAL_REVIEW
    checks = {check.name: check for check in yielded[-1].checks}
    assert checks["ocr_confidence"].status.value == "WARNING"
    assert checks["po_found"].status.value == "WARNING"
    assert checks["ocr_confidence"].details["confidence"] < 75


def test_blocked_vendor_is_rejected(session_factory):
    provider = FakeProvider(fixture_response("blocked_vendor_invoice.pdf"))
    yielded = process(
        session_factory,
        provider,
        "blocked_vendor_invoice.pdf",
    )
    assert yielded[-1].decision == Decision.REJECTED
    assert any("blocked" in reason.lower() for reason in yielded[-1].reasons)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(InvoiceORM)) == 0


def test_business_key_duplicate_is_rejected(session_factory):
    provider = FakeProvider(fixture_response("happy_path_invoice.pdf"))
    process(session_factory, provider)
    duplicate_provider = FakeProvider(fixture_response("duplicate_invoice.pdf"))
    yielded = process(
        session_factory,
        duplicate_provider,
        "duplicate_invoice.pdf",
    )
    assert yielded[-1].decision == Decision.REJECTED
    assert any(check.name == "duplicate_business_key" for check in yielded[-1].checks)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(InvoiceORM)) == 1
        stages = session.scalars(select(RunStageORM).order_by(RunStageORM.id)).all()
        assert stages[-1].stage == StageName.DECISION
