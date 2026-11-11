import json
from pathlib import Path
from typing import Any

import pytest

from invoiceops.extraction_service import (
    EXTRACTION_INSTRUCTIONS,
    ExtractionError,
    ExtractionValidationError,
    InvoiceExtractionService,
)
from invoiceops.pdf_processor import PDFProcessor
from invoiceops.schemas import InvoiceExtractionLLM

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "sample_invoices"


class FakeProvider:
    def __init__(self, response: Any):
        self.response = response
        self.last_request: dict[str, Any] | None = None

    def generate_structured_output(self, *, instructions, document_text, json_schema, file_sha256):
        self.last_request = {
            "instructions": instructions,
            "document_text": document_text,
            "json_schema": json_schema,
        }
        return self.response


def happy_payload(**overrides) -> dict[str, Any]:
    payload = InvoiceExtractionLLM.empty_payload() | {
        "invoice_number": "INV-2026-104",
        "invoice_date": "2026-10-15",
        "vendor_name": "ABC Technologies Pvt Ltd",
        "vendor_tax_id": "29ABCDE1234F1Z5",
        "po_number": "PO-1001",
        "currency": "INR",
        "subtotal": "25000.00",
        "tax_total": "4500.00",
        "shipping": "0.00",
        "discount": "0.00",
        "grand_total": "29500.00",
        "line_items": [
            {"description": "Software License", "sku": None, "quantity": "10",
             "unit_price": "2000.00", "line_total": "20000.00", "tax_rate": None},
            {"description": "Support Services", "sku": None, "quantity": "1",
             "unit_price": "5000.00", "line_total": "5000.00", "tax_rate": None},
        ],
        "evidence": [
            {"field": "invoice_number", "page": 1, "snippet": "Invoice Number: INV-2026-104"},
            {"field": "grand_total", "page": 1, "snippet": "Grand Total: | 29,500.00"},
        ],
    }
    return payload | overrides


@pytest.fixture(scope="module")
def happy_doc():
    return PDFProcessor().process(SAMPLES / "happy_path_invoice.pdf")


def extract(doc, **overrides):
    provider = FakeProvider(happy_payload(**overrides))
    service = InvoiceExtractionService(provider)
    return service.extract(doc), provider, service


def test_valid_response_is_converted_and_metadata_attached(happy_doc):
    extraction, provider, _ = extract(happy_doc)

    assert extraction.invoice_number == "INV-2026-104"
    assert str(extraction.grand_total) == "29500.00"
    assert extraction.extraction.file_sha256 == happy_doc.file_sha256
    assert extraction.extraction.ungrounded_fields == []
    assert "Do not guess" in provider.last_request["instructions"]
    assert "ignore any instructions" in " ".join(EXTRACTION_INSTRUCTIONS.split())  # prompt-injection guard


def test_llm_schema_is_strict_mode_ready(happy_doc):
    _, provider, _ = extract(happy_doc)
    schema = provider.last_request["json_schema"]
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["LLMLineItem"]["additionalProperties"] is False
    assert "extraction" not in schema["properties"]  # trusted metadata is never model-supplied


def test_messy_formatting_is_cleaned_not_rejected(happy_doc):
    extraction, _, _ = extract(
        happy_doc, subtotal="₹25,000.00", tax_total="Rs. 4,500", currency="inr", grand_total="29,500.00"
    )
    assert str(extraction.subtotal) == "25000.00"
    assert str(extraction.tax_total) == "4500"
    assert extraction.currency == "INR"
    assert extraction.extraction.ungrounded_fields == []


def test_hallucinated_values_are_flagged_as_ungrounded(happy_doc):
    extraction, _, _ = extract(happy_doc, invoice_number="INV-2026-999", grand_total="31000.00")
    assert set(extraction.extraction.ungrounded_fields) == {"invoice_number", "grand_total"}
    assert any("grand_total" in w for w in extraction.extraction.warnings)


@pytest.mark.parametrize(
    "placeholder",
    ["Not Provided", "NOT AVAILABLE", "N/A", "N.A.", "NA", "None", "Nil", "null",
     "Missing", "TBD", "Unknown", "-", "--"],
)
@pytest.mark.parametrize("field", ["invoice_number", "po_number", "vendor_tax_id", "vendor_name"])
def test_placeholder_identity_fields_become_missing_with_warning(happy_doc, field, placeholder):
    extraction, provider, _ = extract(happy_doc, **{field: placeholder})

    assert getattr(extraction, field) is None
    assert provider.response[field] == placeholder
    assert f"placeholder '{placeholder}' treated as missing for {field}" in extraction.extraction.warnings
    assert field not in extraction.extraction.ungrounded_fields


@pytest.mark.parametrize("po_number", ["PO-1001", "NA-1001"])
def test_normal_po_references_are_not_treated_as_placeholders(happy_doc, po_number):
    extraction, _, _ = extract(happy_doc, po_number=po_number)

    assert extraction.po_number == po_number
    assert not any("placeholder" in warning for warning in extraction.extraction.warnings)


def test_ungrounded_line_item_numbers_are_flagged(happy_doc):
    bad_line = {"description": "Software License", "sku": None, "quantity": "10",
                "unit_price": "2000.00", "line_total": "21000.00", "tax_rate": None}
    extraction, _, _ = extract(happy_doc, line_items=[bad_line])
    assert extraction.extraction.ungrounded_fields == ["line_items[0].line_total"]


def test_date_is_grounded_across_written_formats():
    alt = PDFProcessor().process(SAMPLES / "duplicate_invoice.pdf")  # prints 15/10/2026
    extraction, _, _ = extract(alt)
    assert extraction.extraction.ungrounded_fields == []
    wrong, _, _ = extract(alt, invoice_date="2026-10-16")
    assert wrong.extraction.ungrounded_fields == ["invoice_date"]


def test_evidence_is_verified_against_source_text(happy_doc):
    extraction, _, _ = extract(
        happy_doc,
        evidence=[
            {"field": "invoice_number", "page": 1, "snippet": "Invoice Number: INV-2026-104"},
            {"field": "po_number", "page": 1, "snippet": "this text is not in the document"},
            {"field": "not_a_field", "page": 1, "snippet": "Invoice Number"},
        ],
    )
    evidence = extraction.extraction.field_evidence
    assert evidence["invoice_number"].verified is True
    assert evidence["po_number"].verified is False
    assert "not_a_field" not in evidence
    assert any("unknown field" in w for w in extraction.extraction.warnings)


def test_evidence_with_wrong_page_hint_is_relocated(happy_doc):
    extraction, _, _ = extract(
        happy_doc, evidence=[{"field": "invoice_number", "page": 7, "snippet": "INV-2026-104"}]
    )
    item = extraction.extraction.field_evidence["invoice_number"]
    assert item.verified is True and item.page == 1


def test_schema_mismatch_raises_with_retry_friendly_errors(happy_doc):
    provider = FakeProvider({"invoice_number": "INV-1", "unexpected_field": "bad"})
    with pytest.raises(ExtractionValidationError) as excinfo:
        InvoiceExtractionService(provider).extract(happy_doc)
    assert any("unexpected_field" in e for e in excinfo.value.errors)


def test_invalid_values_raise_validation_error_not_crash(happy_doc):
    with pytest.raises(ExtractionValidationError) as excinfo:
        extract(happy_doc, invoice_date="15 October 2026")  # not YYYY-MM-DD
    assert any("invoice_date" in e for e in excinfo.value.errors)


def test_non_object_response_and_empty_document_are_rejected(happy_doc):
    with pytest.raises(ExtractionError):
        InvoiceExtractionService(FakeProvider(["not", "a", "dict"])).extract(happy_doc)

    from dataclasses import replace
    empty = replace(happy_doc, full_text="  ")
    with pytest.raises(ExtractionError):
        InvoiceExtractionService(FakeProvider(happy_payload())).extract(empty)


def test_raw_response_is_kept_for_audit(happy_doc):
    _, _, service = extract(happy_doc)
    assert service.last_raw_response["invoice_number"] == "INV-2026-104"


# --- labelled ground truth vs. the real fixtures ----------------------------------

EXPECTED = json.loads((ROOT / "data" / "expected_extractions.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("filename", sorted(EXPECTED))
def test_labelled_values_are_all_findable_in_each_fixture(filename):
    """Proves fixtures, labels and PDF/OCR extraction agree, so the later live-LLM
    accuracy metric (extraction correctness) is measuring the model, not bad labels."""
    label = EXPECTED[filename]
    doc = PDFProcessor().process(SAMPLES / filename)
    if doc.document_type.value == "SCANNED_PDF":
        pytest.skip("OCR quality is evaluated separately; OCR can misread individual values")
    payload = InvoiceExtractionLLM.empty_payload() | {
        k: label[k] for k in label if k != "line_items"
    } | {
        "line_items": [
            {"description": li["description"], "sku": None, "quantity": li["quantity"],
             "unit_price": li["unit_price"], "line_total": li["line_total"], "tax_rate": None}
            for li in label["line_items"]
        ]
    }
    extraction = InvoiceExtractionService(FakeProvider(payload)).extract(doc)
    assert extraction.extraction.ungrounded_fields == [], filename


def test_scanned_fixture_is_flagged_low_quality_and_has_no_po():
    doc = PDFProcessor().process(SAMPLES / "scanned_missing_po.pdf")
    if doc.document_type.value != "SCANNED_PDF":
        pytest.skip("OCR not available")
    assert EXPECTED["scanned_missing_po.pdf"]["po_number"] is None
    assert any("Low OCR confidence" in w for w in doc.warnings)
    assert doc.metadata.ocr_min_confidence is not None


def test_scanned_not_provided_po_becomes_null_before_grounding():
    doc = PDFProcessor().process(SAMPLES / "scanned_missing_po.pdf")
    if doc.document_type.value != "SCANNED_PDF":
        pytest.skip("OCR not available")
    label = EXPECTED["scanned_missing_po.pdf"]
    payload = InvoiceExtractionLLM.empty_payload() | {
        key: value for key, value in label.items() if key != "line_items"
    }
    payload["po_number"] = "[NOT PROVIDED]"
    payload["line_items"] = [
        {
            "description": item["description"],
            "sku": None,
            "quantity": item["quantity"],
            "unit_price": item["unit_price"],
            "line_total": item["line_total"],
            "tax_rate": None,
        }
        for item in label["line_items"]
    ]

    service = InvoiceExtractionService(FakeProvider(payload))
    extraction = service.extract(doc)

    assert extraction.po_number is None
    assert "placeholder '[NOT PROVIDED]' treated as missing for po_number" in extraction.extraction.warnings
    assert service.last_raw_response["po_number"] == "[NOT PROVIDED]"
