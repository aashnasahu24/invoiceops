"""Regression tests for defects found in the v1 audit (docs/05_Audit_v1.md).

Every test asserts the CORRECT behaviour and is marked ``xfail(strict=True)``:
- today it fails (the bug exists) so the suite stays green;
- when you fix the bug it XPASSes, strict mode turns that into a failure, and you
  delete the marker. The test then guards the fix permanently.

Invoices are generated on the fly with the fixture generator, and a fake provider
returns the document's TRUE labels (a perfect extractor), so each test isolates
the rules / orchestration, not the LLM.
"""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import scripts.generate_sample_invoices as gen
from invoiceops import repository
from invoiceops import rules
from invoiceops.db import create_db_engine, create_session_factory, init_db
from invoiceops.grounding import check_grounding
from invoiceops.models import InvoiceORM, PurchaseOrderORM, WorkflowRunORM
from invoiceops.orchestrator import process_invoice
from invoiceops.pdf_processor import DocumentTextResult, PageText
from invoiceops.reviewer import apply_review_action
from invoiceops.schemas import (
    Decision,
    DocumentType,
    InvoiceExtraction,
    InvoiceExtractionLLM,
    InvoiceLineItem,
    POStatus,
    ProcessingStatus,
    ReviewActionType,
)
from invoiceops.seed import seed_reference_data

ROOT = Path(__file__).resolve().parents[1]
bug = lambda reason: pytest.mark.xfail(strict=True, reason=reason)  # noqa: E731


class PerfectExtractor:
    """Returns the true labels of whatever document it is asked about."""

    last_call_was_cache_hit = False

    def __init__(self) -> None:
        self.labels: dict[str, dict] = {}

    def register(self, pdf: bytes, label: dict) -> None:
        self.labels[hashlib.sha256(pdf).hexdigest()] = label

    def generate_structured_output(self, *, instructions, document_text, json_schema, file_sha256):
        label = self.labels[file_sha256]
        payload = InvoiceExtractionLLM.empty_payload() | {k: v for k, v in label.items() if k != "line_items"}
        payload["line_items"] = [
            dict(description=i["description"], sku=None, quantity=i["quantity"],
                 unit_price=i["unit_price"], line_total=i["line_total"], tax_rate=None)
            for i in label["line_items"]
        ]
        return payload


def _line(description, qty, price, total=None):
    return {"description": description, "quantity": qty, "unit_price": price,
            "line_total": qty * price if total is None else total}


def _invoice(**overrides):
    inv = {
        "vendor": "ABC Technologies Pvt Ltd", "tax_id": gen.TAX_IDS["V001"], "number": "INV-A-1",
        "date": "15 October 2026", "date_alt": "15/10/2026", "date_iso": "2026-10-15", "po": "PO-1003",
        "items": [_line("Network Equipment", 4, 10000.0)],
        "subtotal": 40000.0, "tax": 7200.0, "shipping": 0.0, "discount": 0.0, "total": 47200.0,
    }
    return inv | overrides


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "OUT", tmp_path)
    engine = create_db_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    init_db(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        seed_reference_data(session, ROOT / "data")
    provider = PerfectExtractor()

    class World:
        def __init__(self):
            self.factory, self.provider, self.n = factory, provider, 0

        def pdf(self, inv: dict, label_overrides: dict | None = None) -> bytes:
            self.n += 1
            name = f"inv_{self.n}.pdf"
            gen.draw_digital_invoice(name, inv, inv.get("layout", "standard"))
            data = (tmp_path / name).read_bytes()
            self.provider.register(data, gen.expected_record(inv) | (label_overrides or {}))
            return data

        def run(self, data: bytes, provider=None):
            events = list(process_invoice(factory, provider or self.provider, data, "x.pdf"))
            return events[-1]

    yield World()
    engine.dispose()


# --- 1. Vendor identity (security) -------------------------------------------------

def test_unknown_tax_id_with_known_vendor_name_is_not_auto_approved(world):
    result = world.run(world.pdf(_invoice(tax_id="29ZZZZZ9999Z9Z9")))
    assert result.decision != Decision.APPROVED
    identity = next(check for check in result.checks if check.name == "vendor_identity_conflict")
    assert identity.status.value == "FAILED"
    assert "tax id did not match" in identity.message.lower()
    assert identity.recommended_action == "Verify the vendor identity against the supplier master"


def test_known_tax_id_with_unrelated_name_is_not_auto_approved(world):
    result = world.run(world.pdf(_invoice(vendor="Totally Different Corp")))
    assert result.decision != Decision.APPROVED
    identity = next(check for check in result.checks if check.name == "vendor_identity_conflict")
    assert identity.status.value == "FAILED"
    assert "similarity" in identity.message.lower()
    assert identity.details["name_similarity"] < 70


def test_tax_id_and_name_resolving_to_different_vendors_is_not_auto_approved(world):
    result = world.run(world.pdf(_invoice(vendor="XYZ Solutions", tax_id=gen.TAX_IDS["V001"])))
    assert result.decision != Decision.APPROVED
    identity = next(check for check in result.checks if check.name == "vendor_identity_conflict")
    assert identity.status.value == "FAILED"
    assert "matches xyz solutions" in identity.message.lower()


def test_legitimate_vendor_aliases_and_missing_tax_id_still_pass(world):
    alias_with_tax = world.run(world.pdf(_invoice(vendor="ABC Technologies", tax_id=gen.TAX_IDS["V001"])))
    alias_without_tax = world.run(world.pdf(_invoice(
        number="INV-A-2", vendor="ABC Technologies", tax_id=None,
        items=[_line("Network Equipment", 2, 10000.0)],
        subtotal=20000.0, tax=3600.0, total=23600.0,
    )))
    for result in (alias_with_tax, alias_without_tax):
        identity = next(check for check in result.checks if check.name == "vendor_identity_conflict")
        assert identity.status.value == "PASSED"
    assert alias_without_tax.decision == Decision.APPROVED


# --- 2. Financial controls ---------------------------------------------------------

def test_inflated_line_total_is_not_auto_approved(world):
    inv = _invoice(items=[_line("Network Equipment", 4, 10000.0, 45000.0)],
                   subtotal=45000.0, tax=8100.0, total=53100.0)
    result = world.run(world.pdf(inv))
    assert result.decision != Decision.APPROVED
    arithmetic = next(check for check in result.checks if check.name == "line_arithmetic")
    assert arithmetic.status.value == "FAILED"
    assert "expected 40000" in arithmetic.message and "actual 45000" in arithmetic.message


def test_currency_mismatch_is_not_auto_approved(world):
    inv = _invoice(currency="USD")
    data = world.pdf(inv, label_overrides={"currency": "USD"})
    assert world.run(data).decision != Decision.APPROVED
    missing_currency = world.run(world.pdf(
        _invoice(number="INV-CURRENCY-2", currency=None),
        label_overrides={"currency": None},
    ))
    currency_check = next(check for check in missing_currency.checks if check.name == "currency_match")
    assert currency_check.status.value == "WARNING"


def test_inr_is_not_grounded_by_accidental_substring():
    text = "Vendor Orders Customer Reference Terms 100.00"
    doc = DocumentTextResult(
        file_name="x.pdf", file_sha256="a" * 64, document_type=DocumentType.DIGITAL_PDF, page_count=1,
        pages=(PageText(1, text, "PDF_TEXT"),), full_text=text, warnings=(),
    )
    extraction = InvoiceExtraction(currency="INR")
    assert "currency" in check_grounding(extraction, doc).ungrounded_fields
    usd = InvoiceExtraction(currency="USD")
    usd_text = "MUSD Invoice 100.00"
    usd_doc = DocumentTextResult(
        file_name="x.pdf", file_sha256="b" * 64, document_type=DocumentType.DIGITAL_PDF,
        page_count=1, pages=(PageText(1, usd_text, "PDF_TEXT"),),
        full_text=usd_text, warnings=(),
    )
    assert "currency" in check_grounding(usd, usd_doc).ungrounded_fields


# --- 3. Failure handling -----------------------------------------------------------

class _ExplodingProvider:
    last_call_was_cache_hit = False

    def generate_structured_output(self, **kwargs):
        raise RuntimeError("unexpected SDK failure")


def test_unexpected_provider_exception_ends_in_a_failed_run(world):
    data = world.pdf(_invoice())
    try:
        result = world.run(data, provider=_ExplodingProvider())
    except Exception:  # noqa: BLE001
        result = None
    with world.factory() as session:
        run = session.scalar(select(WorkflowRunORM))
        assert run.processing_status == ProcessingStatus.FAILED
    assert result is not None and result.decision == Decision.MANUAL_REVIEW


def test_unexpected_rules_exception_ends_in_a_failed_run(world, monkeypatch):
    data = world.pdf(_invoice(number="INV-A-5"))

    def boom(*args, **kwargs):
        raise ZeroDivisionError("rules bug")

    monkeypatch.setattr(rules, "po_balance", boom)
    try:
        result = world.run(data)
    except Exception:  # noqa: BLE001
        result = None
    with world.factory() as session:
        run = session.scalar(select(WorkflowRunORM))
        assert run.processing_status == ProcessingStatus.FAILED
        assert run.error_message and len(run.error_message) <= 1000
        stages = {stage.stage.value: stage.status.value for stage in run.stages}
        assert stages["VALIDATION"] == "FAILED"
        assert stages["DECISION"] == "SKIPPED"
    assert result is not None and result.decision == Decision.MANUAL_REVIEW


def test_active_business_key_integrity_race_is_rejected_as_duplicate(world, monkeypatch):
    def race_on_insert(session, **kwargs):
        with world.factory.begin() as competing:
            competing.add(InvoiceORM(
                invoice_id="competing-invoice",
                vendor_id="V001",
                invoice_number="INV-A-5",
                invoice_number_normalized="INVA5",
                current_decision=Decision.APPROVED,
                file_name="competing.pdf",
                file_sha256="c" * 64,
                extracted_data={},
                field_evidence={},
                extraction_warnings=[],
            ))
        raise IntegrityError("insert invoice", {}, RuntimeError("unique business key"))

    monkeypatch.setattr(repository, "save_invoice", race_on_insert)
    result = world.run(world.pdf(_invoice(number="INV-A-5")))
    assert result.decision == Decision.REJECTED
    assert any(check.name == "duplicate_business_key" for check in result.checks)
    with world.factory() as session:
        run = session.scalar(select(WorkflowRunORM))
        assert run.decision == Decision.REJECTED
        assert run.processing_status == ProcessingStatus.COMPLETED


# --- 4. Explanations shown to humans -----------------------------------------------

def test_second_split_summary_reports_cumulative_use(world):
    world.run(world.pdf(_invoice(number="INV-S-1")))
    second = world.run(world.pdf(_invoice(
        number="INV-S-2", items=[_line("Network Equipment", 6, 10000.0)],
        subtotal=60000.0, tax=10800.0, total=70800.0)))
    assert second.decision == Decision.APPROVED
    assert "100%" in second.summary or "closed" in second.summary.lower()
    assert "PO-1003 is now fully invoiced (closed)." in second.summary


def test_factor_of_ten_message_is_accurate():
    extraction = InvoiceExtraction(
        subtotal=Decimal("1000"), tax_total=Decimal("180"), shipping=Decimal("0"),
        discount=Decimal("0"), grand_total=Decimal("1180"),
        line_items=[InvoiceLineItem(description="x", quantity=Decimal("1"),
                                    unit_price=Decimal("100"), line_total=Decimal("100"))],
    )
    message = rules.arithmetic_consistency(extraction).message
    assert "factor of 10" in message and "factor of 100" not in message


def test_ocr_confidence_message_is_formatted():
    from invoiceops.schemas import ExtractionMetadata
    extraction = InvoiceExtraction(extraction=ExtractionMetadata(
        document_type=DocumentType.SCANNED_PDF, page_count=1, file_sha256="a" * 64,
        ocr_min_confidence=42.46153846153846))
    message = rules.ocr_confidence(extraction, DocumentType.SCANNED_PDF).message
    assert not re.search(r"\d+\.\d{3,}", message)


def test_vendor_email_contains_only_vendor_actionable_items(world):
    world.run(world.pdf(_invoice(number="INV-A-9", po=None)))
    with world.factory.begin() as session:
        invoice = session.scalar(select(InvoiceORM).where(InvoiceORM.invoice_number == "INV-A-9"))
        email = apply_review_action(
            session, invoice.invoice_id, ReviewActionType.REQUEST_INFORMATION,
            reviewer="QA", comment="Please confirm the purchase order number."
        ).generated_email
    bullets = [line for line in email.splitlines() if line.startswith("- ")]
    assert any("purchase order" in b.lower() for b in bullets)
    assert not any("cannot be checked" in b.lower() or "cannot be compared" in b.lower() for b in bullets)
    assert len(bullets) <= 3


# --- 5. State integrity ------------------------------------------------------------

def test_restart_does_not_reopen_a_closed_po(world):
    world.run(world.pdf(_invoice(number="INV-S-1")))
    world.run(world.pdf(_invoice(number="INV-S-2", items=[_line("Network Equipment", 6, 10000.0)],
                                 subtotal=60000.0, tax=10800.0, total=70800.0)))
    with world.factory.begin() as session:
        seed_reference_data(session, ROOT / "data")  # what the Streamlit app does on startup
    with world.factory() as session:
        po = session.scalar(select(PurchaseOrderORM).where(PurchaseOrderORM.po_number == "PO-1003"))
        assert po.status == POStatus.CLOSED


def test_reviewer_reject_preserves_active_identity_and_blocks_resubmission(world):
    world.run(world.pdf(_invoice(number="INV-A-7", po=None)))
    with world.factory.begin() as session:
        invoice = session.scalar(select(InvoiceORM).where(InvoiceORM.invoice_number == "INV-A-7"))
        apply_review_action(session, invoice.invoice_id, ReviewActionType.REJECT,
                            reviewer="QA", comment="PO missing; vendor must correct")
    corrected = world.run(world.pdf(_invoice(number="INV-A-7", po="PO-1003", layout="alternate")))
    assert corrected.decision == Decision.REJECTED
    assert any(check.name == "duplicate_business_key" for check in corrected.checks)
