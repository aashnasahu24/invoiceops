"""Deterministic grounding: is each extracted value actually in the document?

LLM self-reported confidence is not evidence. This module checks extracted
values and evidence snippets against the real source text (PDF text or OCR).
A value the model "found" that does not appear in the text is a review signal,
not something to silently trust. Everything here is pure and unit-testable.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from .pdf_processor import DocumentTextResult
from .schemas import FieldEvidence, InvoiceExtraction

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_MONTHS_FULL = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]
_CURRENCY_MARKERS = {
    "INR": ("inr", "₹"),
    "USD": ("usd", "$"),
    "EUR": ("eur", "€"),
    "GBP": ("gbp", "£"),
}

TEXT_FIELDS = ("invoice_number", "po_number", "vendor_tax_id", "vendor_name")
DATE_FIELDS = ("invoice_date", "due_date")
MONEY_FIELDS = ("subtotal", "tax_total", "shipping", "discount", "grand_total")


def squash(text: str) -> str:
    """Case-, whitespace- and width-insensitive form used for substring checks."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()


def numbers_in(text: str) -> set[Decimal]:
    found: set[Decimal] = set()
    for match in _NUMBER.finditer(text):
        try:
            found.add(Decimal(match.group().replace(",", "")))
        except InvalidOperation:  # pragma: no cover - regex guarantees digits
            continue
    return found


def date_forms(value: date) -> set[str]:
    """Common written forms of a date, squashed, so layout choices don't matter."""
    d, m, y = value.day, value.month, value.year
    month_full = _MONTHS_FULL[m - 1]
    forms: set[str] = set()
    for day in {str(d), f"{d:02d}"}:
        for mon in {str(m), f"{m:02d}"}:
            for sep in ("/", "-", "."):
                forms.add(f"{day}{sep}{mon}{sep}{y}")  # 15/10/2026
                forms.add(f"{mon}{sep}{day}{sep}{y}")  # 10/15/2026
                forms.add(f"{y}{sep}{mon}{sep}{day}")  # 2026-10-15
        for name in (month_full, month_full[:3]):
            forms.add(f"{day} {name} {y}")
            forms.add(f"{day}-{name}-{y}")
            forms.add(f"{name} {day}, {y}")
            forms.add(f"{name} {day} {y}")
    return {squash(form) for form in forms}


@dataclass
class GroundingResult:
    ungrounded_fields: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def check_grounding(extraction: InvoiceExtraction, document: DocumentTextResult) -> GroundingResult:
    """Flag extracted values that cannot be found in the source text."""
    source = squash(document.full_text)
    numbers = numbers_in(document.full_text)
    result = GroundingResult()

    def flag(name: str, value: object) -> None:
        result.ungrounded_fields.append(name)
        result.warnings.append(f"Grounding: {name}={value!s} not found in document text.")

    for name in TEXT_FIELDS:
        value = getattr(extraction, name)
        if value is not None and squash(value) not in source:
            flag(name, value)

    for name in DATE_FIELDS:
        value = getattr(extraction, name)
        if value is not None and not any(form in source for form in date_forms(value)):
            flag(name, value)

    for name in MONEY_FIELDS:
        value = getattr(extraction, name)
        if value is not None and value not in numbers:
            flag(name, value)

    if extraction.currency is not None:
        markers = _CURRENCY_MARKERS.get(extraction.currency, (extraction.currency.casefold(),))
        if not any(
            marker in document.full_text
            if marker in {"₹", "$", "€", "£"}
            else re.search(rf"\b{re.escape(marker)}\b", document.full_text, re.IGNORECASE)
            for marker in markers
        ):
            flag("currency", extraction.currency)

    for index, item in enumerate(extraction.line_items):
        prefix = f"line_items[{index}]"
        if squash(item.description) not in source:
            flag(f"{prefix}.description", item.description)
        for name in ("quantity", "unit_price", "line_total"):
            value = getattr(item, name)
            if value not in numbers:
                flag(f"{prefix}.{name}", value)

    return result


def locate_snippet(snippet: str, document: DocumentTextResult, page_hint: int | None) -> tuple[bool, int | None]:
    """Find a snippet in the document. Returns (found, page where found).

    The hinted page is tried first; otherwise the first page containing it.
    """
    needle = squash(snippet)
    if not needle:
        return False, page_hint
    pages = {p.page_number: squash(p.text) for p in document.pages}
    if page_hint in pages and needle in pages[page_hint]:
        return True, page_hint
    for number, text in pages.items():
        if needle in text:
            return True, number
    return False, page_hint


def build_evidence(
    raw_evidence: list[dict],
    document: DocumentTextResult,
    valid_fields: set[str],
) -> tuple[dict[str, FieldEvidence], list[str]]:
    """Turn model-supplied evidence into verified ``FieldEvidence`` records."""
    page_confidence = {
        p.page_number: p.ocr_confidence / 100 for p in document.pages if p.ocr_confidence is not None
    }
    evidence: dict[str, FieldEvidence] = {}
    warnings: list[str] = []
    for item in raw_evidence:
        name = item["field"]
        if name not in valid_fields:
            warnings.append(f"Ignored evidence for unknown field '{name}'.")
            continue
        found, page = locate_snippet(item["snippet"], document, item.get("page"))
        if not found:
            warnings.append(f"Evidence snippet for '{name}' was not found in the document text.")
        record = FieldEvidence(
            page=page if page and page >= 1 else None,
            snippet=item["snippet"],
            confidence=page_confidence.get(page),
            verified=found,
        )
        # Keep a verified snippet over an unverified one for the same field.
        existing = evidence.get(name)
        if existing is None or (record.verified and not existing.verified):
            evidence[name] = record
    return evidence, warnings
