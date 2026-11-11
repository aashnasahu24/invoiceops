"""Score the extraction provider against the labelled invoice PDF fixtures."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from invoiceops.config import DEFAULT_POLICY, get_settings
from invoiceops.extraction_service import (
    EXTRACTION_INSTRUCTIONS,
    ExtractionError,
    InvoiceExtractionService,
)
from invoiceops.normalization import normalize_company_name
from invoiceops.pdf_processor import PDFProcessingError, PDFProcessor
from invoiceops.providers.gemini_provider import GeminiProvider
from invoiceops.providers.errors import ProviderError
from invoiceops.providers.replay import RecordingProvider, ReplayCacheMissError, ReplayProvider
from invoiceops.providers.resilient import ExtractionFailed, extract_with_retry

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "sample_invoices"
EXPECTED_PATH = ROOT / "data" / "expected_extractions.json"
REQUIRED_DIGITAL_FIELDS = ("invoice_number", "vendor_name", "grand_total")
NUMERIC_FIELDS = {
    "subtotal",
    "tax_total",
    "shipping",
    "discount",
    "grand_total",
    "quantity",
    "unit_price",
    "line_total",
}
DATE_FIELDS = {"invoice_date", "due_date"}


@dataclass(frozen=True)
class FixtureScore:
    field_results: dict[str, bool]
    line_items_match: bool

    @property
    def fully_correct(self) -> bool:
        return all(self.field_results.values())


def _decimal_equal(expected: Any, actual: Any) -> bool:
    if expected is None or actual is None:
        return expected is actual
    try:
        return Decimal(str(expected)) == Decimal(str(actual))
    except (InvalidOperation, ValueError):
        return False


def _date_equal(expected: Any, actual: Any) -> bool:
    if expected is None or actual is None:
        return expected is actual
    try:
        expected_date = expected if isinstance(expected, date) else date.fromisoformat(str(expected))
        actual_date = actual if isinstance(actual, date) else date.fromisoformat(str(actual))
    except (TypeError, ValueError):
        return False
    return expected_date == actual_date


def _value_equal(
    field: str,
    expected: Any,
    actual: Any,
    *,
    lenient_names: bool,
) -> bool:
    if field in NUMERIC_FIELDS:
        return _decimal_equal(expected, actual)
    if field in DATE_FIELDS:
        return _date_equal(expected, actual)
    if field == "vendor_name" and lenient_names:
        return normalize_company_name(expected) == normalize_company_name(actual)
    return expected == actual


def score_extraction(
    expected: dict[str, Any],
    extraction: Any,
    *,
    lenient_names: bool = False,
) -> FixtureScore:
    """Compare labelled fields by their domain value, not their string format."""
    field_results: dict[str, bool] = {}
    for field, expected_value in expected.items():
        if field == "line_items":
            actual_items = getattr(extraction, "line_items", None)
            field_results[field] = (
                isinstance(actual_items, list)
                and len(actual_items) == len(expected_value)
                and all(
                    all(
                        _value_equal(
                            key,
                            expected_item.get(key),
                            getattr(actual_item, key, None),
                            lenient_names=lenient_names,
                        )
                        for key in expected_item
                    )
                    for expected_item, actual_item in zip(expected_value, actual_items)
                )
            )
        else:
            field_results[field] = _value_equal(
                field,
                expected_value,
                getattr(extraction, field, None),
                lenient_names=lenient_names,
            )
    return FixtureScore(field_results, field_results.get("line_items", False))


def evaluate_fixtures(
    provider: Any,
    fixture_names: list[str],
    *,
    lenient_names: bool = False,
) -> dict[str, Any]:
    expected_data = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    processor = PDFProcessor()
    fixtures: list[dict[str, Any]] = []
    started_all = time.perf_counter()
    total_fields = correct_fields = fully_correct = 0

    for filename in fixture_names:
        expected = expected_data[filename]
        result: dict[str, Any] = {
            "fixture": filename,
            "field_results": {},
            "line_items_match": False,
            "ungrounded_fields": [],
            "ocr_confidence": None,
            "ocr_low_confidence": False,
            "latency_seconds": 0.0,
            "attempts": 0,
            "unverified_evidence_count": 0,
            "cache_hit": False,
            "error": None,
            "fully_correct": False,
            "required_digital_fields_wrong": [],
            "document_type": None,
            "expected": expected,
            "actual": {},
        }
        request_started: float | None = None
        service: InvoiceExtractionService | None = None
        try:
            document = processor.process(SAMPLES / filename)
            result["document_type"] = document.document_type.value
            result["ocr_confidence"] = document.metadata.ocr_min_confidence
            result["ocr_low_confidence"] = (
                document.metadata.ocr_min_confidence is not None
                and document.metadata.ocr_min_confidence < DEFAULT_POLICY.min_ocr_confidence
            )
            service = InvoiceExtractionService(provider)
            request_started = time.perf_counter()
            extraction = extract_with_retry(service, document)
            result["attempts"] = service.last_attempts
            result["ungrounded_fields"] = extraction.extraction.ungrounded_fields
            result["unverified_evidence_count"] = sum(
                evidence.verified is False
                for evidence in extraction.extraction.field_evidence.values()
            )
            result["actual"] = {
                field: (
                    (
                        [item.model_dump(mode="json") for item in extraction.line_items]
                        if field == "line_items"
                        else extraction.model_dump(mode="json")[field]
                    )
                )
                for field in expected
            }
            score = score_extraction(expected, extraction, lenient_names=lenient_names)
            result["field_results"] = score.field_results
            result["line_items_match"] = score.line_items_match
        except (PDFProcessingError, ExtractionError, ExtractionFailed, ProviderError, ReplayCacheMissError) as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["field_results"] = {field: False for field in expected}
        finally:
            if service is not None:
                result["attempts"] = service.last_attempts
            if request_started is not None:
                result["latency_seconds"] = time.perf_counter() - request_started
            result["cache_hit"] = bool(getattr(provider, "last_call_was_cache_hit", False))

        if result["document_type"] == "DIGITAL_PDF":
            result["required_digital_fields_wrong"] = [
                field
                for field in REQUIRED_DIGITAL_FIELDS
                if field in expected and not result["field_results"].get(field, False)
            ]
        total_fields += len(result["field_results"])
        correct_fields += sum(result["field_results"].values())
        result["fully_correct"] = (
            not result["error"]
            and len(result["field_results"]) == len(expected)
            and all(result["field_results"].values())
        )
        fully_correct += int(result["fully_correct"])
        fixtures.append(result)

    total_seconds = time.perf_counter() - started_all
    return {
        "fixtures": fixtures,
        "summary": {
            "total_fields": total_fields,
            "correct_fields": correct_fields,
            "field_accuracy_percent": (100 * correct_fields / total_fields) if total_fields else 0.0,
            "fixtures_fully_correct": fully_correct,
            "fixture_count": len(fixtures),
            "total_time_seconds": total_seconds,
            "has_required_digital_errors": any(
                fixture["required_digital_fields_wrong"] for fixture in fixtures
            ),
            "has_errors": any(fixture["error"] for fixture in fixtures),
        },
    }


def _print_report(report: dict[str, Any]) -> None:
    print("Fixture evaluation")
    print("-" * 118)
    print(
        f"{'Fixture':32} {'Fields':12} {'Lines':7} {'Ungrounded':26} "
        f"{'OCR confidence':16} {'Attempts':9} {'Unverified':11} {'Latency':10} {'Cache':6}"
    )
    print("-" * 118)
    for fixture in report["fixtures"]:
        field_count = len(fixture["field_results"])
        correct_count = sum(fixture["field_results"].values())
        confidence = fixture["ocr_confidence"]
        ocr = f"{confidence:.1f}%" if confidence is not None else "n/a"
        if fixture["ocr_low_confidence"]:
            ocr += " LOW"
        print(
            f"{fixture['fixture'][:32]:32} {correct_count}/{field_count} correct "
            f"{'OK' if fixture['line_items_match'] else 'WRONG':7} "
            f"{str(fixture['ungrounded_fields']):26} {ocr:16} "
            f"{fixture['attempts']:<9} {fixture['unverified_evidence_count']:<11} "
            f"{fixture['latency_seconds']:.2f}s{'':5} "
            f"{'hit' if fixture['cache_hit'] else 'miss':6}"
        )
        print(
            "  Fields: "
            + ", ".join(
                f"{field}: {'CORRECT' if correct else 'WRONG'}"
                for field, correct in fixture["field_results"].items()
            )
        )
        for field, correct in fixture["field_results"].items():
            if not correct:
                print(
                    f"  WRONG {field}: expected={fixture['expected'].get(field)!r}; "
                    f"actual={fixture['actual'].get(field)!r}"
                )
        if fixture["error"]:
            print(f"  ERROR: {fixture['error']}")
        if fixture["ocr_low_confidence"]:
            print(f"  WARNING: OCR confidence is below the 75% review threshold for {fixture['fixture']}.")

    summary = report["summary"]
    print("-" * 118)
    print(
        f"Field accuracy: {summary['correct_fields']}/{summary['total_fields']} "
        f"({summary['field_accuracy_percent']:.2f}%) | "
        f"Fully correct fixtures: {summary['fixtures_fully_correct']}/{summary['fixture_count']} | "
        f"Total time: {summary['total_time_seconds']:.2f}s"
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("gemini", "replay"), default="gemini")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "data" / "llm_cache_demo")
    parser.add_argument("--fixture", help="One fixture filename or stem, e.g. happy_path_invoice")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--lenient-names", action="store_true")
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Force Gemini requests and overwrite existing cache entries.",
    )
    args = parser.parse_args(argv)
    if args.fixture:
        fixture_name = args.fixture if args.fixture.endswith(".pdf") else f"{args.fixture}.pdf"
        if fixture_name not in json.loads(EXPECTED_PATH.read_text(encoding="utf-8")):
            parser.error(f"Unknown fixture: {args.fixture}")
        args.fixtures = [fixture_name]
    else:
        args.fixtures = sorted(json.loads(EXPECTED_PATH.read_text(encoding="utf-8")))
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    if not settings.gemini_model:
        print("GEMINI_MODEL must be set in .env or the environment.", file=sys.stderr)
        return 2
    if args.provider == "gemini":
        if not settings.gemini_api_key:
            print("GEMINI_API_KEY must be set in .env or the environment.", file=sys.stderr)
            return 2
        provider = RecordingProvider(
            GeminiProvider(api_key=settings.gemini_api_key, model=settings.gemini_model),
            args.cache_dir,
            model=settings.gemini_model,
            force_refresh=args.refresh_cache,
        )
    else:
        if args.refresh_cache:
            parser.error("--refresh-cache requires --provider gemini")
        provider = ReplayProvider(args.cache_dir, model=settings.gemini_model)

    report = evaluate_fixtures(provider, args.fixtures, lenient_names=args.lenient_names)
    _print_report(report)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return int(
        report["summary"]["has_required_digital_errors"]
        or report["summary"]["has_errors"]
    )


if __name__ == "__main__":
    raise SystemExit(main())
