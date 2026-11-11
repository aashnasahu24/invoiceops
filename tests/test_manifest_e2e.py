import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest
import pymupdf as fitz
from sqlalchemy import select

from invoiceops.config import REPLAY_FIXTURE_MODEL
from invoiceops.db import create_db_engine, create_session_factory, init_db
from invoiceops.extraction_service import InvoiceExtractionService
from invoiceops.matching import match_invoice_lines
from invoiceops.models import InvoiceORM, PurchaseOrderORM, VendorORM
from invoiceops.orchestrator import process_invoice
from invoiceops.pdf_processor import OCRUnavailableError, PDFProcessor
from invoiceops.providers.replay import ReplayProvider
from invoiceops.rules import decide, run_checks
from invoiceops.schemas import Decision, DocumentType, InvoiceExtraction, InvoiceExtractionLLM
from invoiceops.seed import seed_reference_data
from scripts.run_manifest import run_manifest

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "sample_invoices"
LABELS = json.loads((ROOT / "data" / "expected_extractions.json").read_text(encoding="utf-8"))
CACHE = ROOT / "data" / "llm_cache_demo"


@pytest.fixture
def session_factory(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'manifest.db'}")
    init_db(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        seed_reference_data(session, ROOT / "data")
        session.commit()
    yield factory
    engine.dispose()


def test_manifest_replay_recorded_cases_pass(session_factory, tmp_path):
    try:
        PDFProcessor().process(SAMPLES / "scanned_missing_po.pdf")
    except OCRUnavailableError:
        pytest.skip("Tesseract is unavailable for the scanned manifest fixture")
    model = REPLAY_FIXTURE_MODEL
    manifest = json.loads((ROOT / "data" / "test_manifest.json").read_text(encoding="utf-8"))
    manifest["cases"] = manifest["cases"][:8]
    manifest_path = tmp_path / "recorded_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = run_manifest(
        ReplayProvider(CACHE, model=model),
        session_factory,
        manifest_path=manifest_path,
        reset=True,
    )
    assert report["passed"], report["rows"]
    assert len(report["rows"]) == 8
    assert all(row["cache_hit"] for row in report["rows"])
    assert next(row for row in report["rows"] if row["fixture"] == "resubmitted_clean.pdf")[
        "actual_decision"
    ] == "APPROVED"
    with session_factory() as session:
        returned = session.scalar(
            select(InvoiceORM).where(InvoiceORM.file_name == "scanned_missing_po.pdf")
        )
        resubmitted = session.scalar(
            select(InvoiceORM).where(InvoiceORM.file_name == "resubmitted_clean.pdf")
        )
        assert returned.is_active is False
        assert resubmitted.supersedes_invoice_id == returned.invoice_id
        scan_run = max(returned.workflow_runs, key=lambda run: run.started_at)
        scan_checks = {check.check_name: check for check in scan_run.validation_checks}
        assert "PO-1002" in scan_checks["po_found"].details["candidate_po_numbers"]
        arithmetic_message = scan_checks["arithmetic_consistency"].message.lower()
        assert "off by a factor of 100: possible ocr loss of decimal point" in arithmetic_message


def test_new_adversarial_manifest_fixtures_run_offline(session_factory, tmp_path):
    class LabelProvider:
        last_call_was_cache_hit = False

        def __init__(self):
            self.fixture_by_sha = {
                hashlib.sha256((SAMPLES / name).read_bytes()).hexdigest(): name
                for name in (
                    "impersonation_wrong_taxid.pdf",
                    "inflated_line_total.pdf",
                    "currency_mismatch.pdf",
                )
            }

        def generate_structured_output(self, *, file_sha256, **_kwargs):
            label = LABELS[self.fixture_by_sha[file_sha256]]
            payload = {
                **InvoiceExtractionLLM.empty_payload(),
                **{key: value for key, value in label.items() if key != "line_items"},
            }
            payload["line_items"] = [
                {
                    "description": item["description"],
                    "sku": item.get("sku"),
                    "quantity": item["quantity"],
                    "unit_price": item["unit_price"],
                    "line_total": item["line_total"],
                    "tax_rate": item.get("tax_rate"),
                }
                for item in label["line_items"]
            ]
            return payload

    manifest = json.loads((ROOT / "data" / "test_manifest.json").read_text(encoding="utf-8"))
    manifest["cases"] = manifest["cases"][-3:]
    manifest_path = tmp_path / "adversarial_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = run_manifest(
        LabelProvider(),
        session_factory,
        manifest_path=manifest_path,
        reset=True,
    )
    assert report["passed"], report["rows"]
    assert [row["fixture"] for row in report["rows"]] == [
        "impersonation_wrong_taxid.pdf",
        "inflated_line_total.pdf",
        "currency_mismatch.pdf",
    ]


def test_split_second_first_is_approved_then_po_closes(session_factory):
    model = REPLAY_FIXTURE_MODEL
    provider = ReplayProvider(CACHE, model=model)
    split_2 = list(process_invoice(
        session_factory,
        provider,
        (SAMPLES / "split_invoice_2.pdf").read_bytes(),
        "split_invoice_2.pdf",
    ))[-1]
    split_1 = list(process_invoice(
        session_factory,
        provider,
        (SAMPLES / "split_invoice_1.pdf").read_bytes(),
        "split_invoice_1.pdf",
    ))[-1]
    assert split_2.decision == split_1.decision == Decision.APPROVED
    with session_factory() as session:
        po = session.get(PurchaseOrderORM, "POID-1003")
        assert po.status.value == "CLOSED"


def test_third_invoice_against_closed_po_requires_review(session_factory):
    model = REPLAY_FIXTURE_MODEL
    provider = ReplayProvider(CACHE, model=model)
    for filename in ("split_invoice_2.pdf", "split_invoice_1.pdf"):
        result = list(process_invoice(
            session_factory,
            provider,
            (SAMPLES / filename).read_bytes(),
            filename,
        ))[-1]
        assert result.decision == Decision.APPROVED

    text = (
        "TAX INVOICE\nVendor: ABC Technologies\nGSTIN: 29ABCDE1234F1Z5\n"
        "Invoice Number: INV-2026-399\nInvoice Date: 2026-10-24\n"
        "PO Reference: PO-1003\nCurrency: INR\n"
        "Description | Qty | Unit Price | Line Total\n"
        "Network Equipment | 6 | 10000.00 | 60000.00\n"
        "Subtotal: 60000.00\nGST: 10800.00\nShipping: 0.00\n"
        "Discount: 0.00\nGrand Total: 70800.00"
    )
    pdf = fitz.open()
    pdf.new_page().insert_text((40, 50), text)
    pdf_bytes = pdf.tobytes()
    response = {
        "invoice_number": "INV-2026-399",
        "invoice_date": "2026-10-24",
        "due_date": None,
        "vendor_name": "ABC Technologies",
        "vendor_tax_id": "29ABCDE1234F1Z5",
        "po_number": "PO-1003",
        "currency": "INR",
        "line_items": [{
            "description": "Network Equipment",
            "sku": None,
            "quantity": "6",
            "unit_price": "10000.00",
            "line_total": "60000.00",
            "tax_rate": None,
        }],
        "subtotal": "60000.00",
        "tax_total": "10800.00",
        "shipping": "0.00",
        "discount": "0.00",
        "grand_total": "70800.00",
        "evidence": [],
    }

    class FixedProvider:
        def generate_structured_output(self, **_kwargs):
            return response

    result = list(process_invoice(
        session_factory,
        FixedProvider(),
        pdf_bytes,
        "closed_po_invoice.pdf",
    ))[-1]
    checks = {check.name: check for check in result.checks}
    assert result.decision == Decision.MANUAL_REVIEW
    assert checks["po_status"].status.value == "FAILED"


def test_prompt_injection_text_does_not_change_deterministic_decision(session_factory):
    label = LABELS["prompt_injection_line.pdf"]
    raw_response = {
        **label,
        "due_date": label.get("due_date"),
        "evidence": [],
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
    }

    class FixedProvider:
        def generate_structured_output(self, **_kwargs):
            return raw_response

    document = PDFProcessor().process(SAMPLES / "prompt_injection_line.pdf")
    extracted = InvoiceExtractionService(FixedProvider()).extract(document)
    assert extracted.grand_total == 8260
    with session_factory() as session:
        vendor = session.get(VendorORM, "V001")
        po = session.get(PurchaseOrderORM, "POID-1001")
        line_matches = match_invoice_lines(extracted, po)
        prompt_result = decide(run_checks(
            extracted,
            vendor=vendor,
            purchase_order=po,
            remaining_balance=Decimal("50000"),
            document_type=DocumentType.DIGITAL_PDF,
            line_matches=line_matches,
        ))
        clean_extraction = InvoiceExtraction.model_validate({
            **label,
            "extraction": extracted.extraction.model_copy(deep=True),
        })
        clean_result = decide(run_checks(
            clean_extraction,
            vendor=vendor,
            purchase_order=po,
            remaining_balance=Decimal("50000"),
            document_type=DocumentType.DIGITAL_PDF,
            line_matches=match_invoice_lines(clean_extraction, po),
        ))
    assert prompt_result.decision == clean_result.decision
    assert prompt_result.reasons == clean_result.reasons
