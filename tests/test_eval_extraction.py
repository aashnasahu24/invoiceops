import hashlib
import json
from types import SimpleNamespace
from datetime import date
from decimal import Decimal

from invoiceops.pdf_processor import DocumentTextResult, PageText, PDFProcessor
from invoiceops.providers.replay import ReplayProvider, cache_key
from invoiceops.schemas import DocumentType, InvoiceExtraction, InvoiceExtractionLLM
from scripts import eval_extraction
from scripts.eval_extraction import evaluate_fixtures, score_extraction

ROOT = eval_extraction.ROOT


def _payload(expected):
    payload = InvoiceExtractionLLM.empty_payload() | {
        key: value for key, value in expected.items() if key != "line_items"
    }
    payload["line_items"] = [
        {
            "description": item["description"],
            "sku": None,
            "quantity": item["quantity"],
            "unit_price": item["unit_price"],
            "line_total": item["line_total"],
            "tax_rate": None,
        }
        for item in expected["line_items"]
    ]
    return payload


def _save_replay(cache_dir, filename, payload, model="eval-test-model"):
    document = PDFProcessor().process(ROOT / "data" / "sample_invoices" / filename)
    schema = InvoiceExtractionLLM.model_json_schema()
    key = cache_key(document.file_sha256, schema, model)
    instructions_hash = hashlib.sha256(
        eval_extraction.EXTRACTION_INSTRUCTIONS.encode("utf-8")
    ).hexdigest()
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{key}.json").write_text(
        json.dumps({"instructions_sha256": instructions_hash, "response": payload}),
        encoding="utf-8",
    )


def test_eval_replay_scores_correct_and_wrong_cached_responses(tmp_path, capsys):
    expected_data = json.loads((ROOT / "data" / "expected_extractions.json").read_text(encoding="utf-8"))
    filename = "happy_path_invoice.pdf"
    expected = expected_data[filename]

    _save_replay(tmp_path, filename, _payload(expected))
    provider = ReplayProvider(tmp_path, model="eval-test-model")
    report = evaluate_fixtures(provider, [filename])
    assert report["summary"]["fixtures_fully_correct"] == 1
    assert report["summary"]["field_accuracy_percent"] == 100
    assert report["fixtures"][0]["cache_hit"] is True
    assert report["fixtures"][0]["line_items_match"] is True

    wrong = _payload(expected)
    wrong["grand_total"] = "99999.00"
    _save_replay(tmp_path, filename, wrong)
    wrong_report = evaluate_fixtures(ReplayProvider(tmp_path, model="eval-test-model"), [filename])
    wrong_fixture = wrong_report["fixtures"][0]
    assert wrong_fixture["field_results"]["grand_total"] is False
    assert wrong_report["summary"]["fixtures_fully_correct"] == 0
    assert wrong_report["summary"]["has_required_digital_errors"] is True

    eval_extraction._print_report(wrong_report)
    assert "grand_total: WRONG" in capsys.readouterr().out


def test_score_compares_decimal_and_date_values_and_optional_lenient_names():
    expected = {
        "invoice_date": "2026-10-15",
        "vendor_name": "ABC Technologies Pvt Ltd",
        "grand_total": "29500.00",
    }
    extraction = InvoiceExtraction(
        invoice_date=date(2026, 10, 15),
        vendor_name="ABC Technologies Private Limited",
        grand_total=Decimal("29500"),
    )
    strict = score_extraction(expected, extraction)
    lenient = score_extraction(expected, extraction, lenient_names=True)
    assert strict.field_results == {
        "invoice_date": True,
        "vendor_name": False,
        "grand_total": True,
    }
    assert lenient.field_results["vendor_name"] is True


def test_replay_mismatch_for_required_digital_field_is_reported(tmp_path):
    expected_data = json.loads((ROOT / "data" / "expected_extractions.json").read_text(encoding="utf-8"))
    filename = "happy_path_invoice.pdf"
    wrong = _payload(expected_data[filename])
    wrong["invoice_number"] = "INV-999"
    _save_replay(tmp_path, filename, wrong)

    report = evaluate_fixtures(ReplayProvider(tmp_path, model="eval-test-model"), [filename])

    assert report["fixtures"][0]["required_digital_fields_wrong"] == ["invoice_number"]
    assert report["summary"]["has_required_digital_errors"] is True


def test_main_exits_one_when_a_required_digital_field_is_wrong(tmp_path, monkeypatch, capsys):
    expected_data = json.loads((ROOT / "data" / "expected_extractions.json").read_text(encoding="utf-8"))
    filename = "happy_path_invoice.pdf"
    wrong = _payload(expected_data[filename])
    wrong["grand_total"] = "99999.00"
    _save_replay(tmp_path, filename, wrong)
    monkeypatch.setattr(
        eval_extraction,
        "get_settings",
        lambda: SimpleNamespace(gemini_model="eval-test-model", gemini_api_key=None),
    )

    exit_code = eval_extraction.main(
        ["--provider", "replay", "--cache-dir", str(tmp_path), "--fixture", filename]
    )

    assert exit_code == 1
    assert "grand_total: WRONG" in capsys.readouterr().out


def test_main_supports_lenient_names_and_json_output(tmp_path, monkeypatch):
    expected_data = json.loads((ROOT / "data" / "expected_extractions.json").read_text(encoding="utf-8"))
    filename = "happy_path_invoice.pdf"
    payload = _payload(expected_data[filename])
    payload["vendor_name"] = "ABC Technologies Private Limited"
    _save_replay(tmp_path, filename, payload)
    monkeypatch.setattr(
        eval_extraction,
        "get_settings",
        lambda: SimpleNamespace(gemini_model="eval-test-model", gemini_api_key=None),
    )
    json_out = tmp_path / "report.json"

    exit_code = eval_extraction.main(
        [
            "--provider",
            "replay",
            "--cache-dir",
            str(tmp_path),
            "--fixture",
            filename,
            "--lenient-names",
            "--json-out",
            str(json_out),
        ]
    )

    assert exit_code == 0
    result = json.loads(json_out.read_text(encoding="utf-8"))
    assert result["fixtures"][0]["field_results"]["vendor_name"] is True
    assert result["summary"]["fixtures_fully_correct"] == 1


def test_eval_marks_scanned_low_confidence_and_prints_warning(monkeypatch, capsys):
    document = DocumentTextResult(
        file_name="scanned_missing_po.pdf",
        file_sha256="b" * 64,
        document_type=DocumentType.SCANNED_PDF,
        page_count=1,
        pages=(PageText(page_number=1, text="low quality", method="OCR", ocr_confidence=42.0),),
        full_text="low quality",
        warnings=("Low OCR confidence on page 1: 42.0%.",),
    )

    class FakeProcessor:
        def process(self, _path):
            return document

    class FakeProvider:
        last_call_was_cache_hit = True

        def generate_structured_output(self, *, instructions, document_text, json_schema, file_sha256):
            return InvoiceExtractionLLM.empty_payload()

    monkeypatch.setattr(eval_extraction, "PDFProcessor", FakeProcessor)
    report = evaluate_fixtures(FakeProvider(), ["scanned_missing_po.pdf"])

    assert report["fixtures"][0]["ocr_low_confidence"] is True
    assert report["fixtures"][0]["ocr_confidence"] == 42.0
    eval_extraction._print_report(report)
    assert "LOW" in capsys.readouterr().out
