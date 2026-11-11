"""Pydantic v2 domain and workflow schemas for InvoiceOps.

Extraction fields that are required by business policy are intentionally nullable:
the model must be able to represent missing information so the rule engine can
route the invoice to MANUAL_REVIEW instead of fabricating a value.
"""

import re
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class VendorStatus(str, Enum):
    APPROVED = "APPROVED"
    BLOCKED = "BLOCKED"
    INACTIVE = "INACTIVE"


class POStatus(str, Enum):
    OPEN = "OPEN"
    PARTIALLY_INVOICED = "PARTIALLY_INVOICED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class AmountBasis(str, Enum):
    PRE_TAX = "PRE_TAX"
    TAX_INCLUSIVE = "TAX_INCLUSIVE"


class DocumentType(str, Enum):
    DIGITAL_PDF = "DIGITAL_PDF"
    SCANNED_PDF = "SCANNED_PDF"


class ProcessingStatus(str, Enum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class CheckStatus(str, Enum):
    PASSED = "PASSED"
    WARNING = "WARNING"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class Decision(str, Enum):
    APPROVED = "APPROVED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    REJECTED = "REJECTED"


class ReviewActionType(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REQUEST_INFORMATION = "REQUEST_INFORMATION"
    COMMENT = "COMMENT"


class StageName(str, Enum):
    """Pipeline stages shown in the live run view, in execution order."""

    RECEIVED = "RECEIVED"
    TEXT_EXTRACTION = "TEXT_EXTRACTION"
    AI_EXTRACTION = "AI_EXTRACTION"
    SCHEMA_VALIDATION = "SCHEMA_VALIDATION"
    MATCHING = "MATCHING"
    VALIDATION = "VALIDATION"
    DECISION = "DECISION"


class StageStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


# --- tolerant parsing helpers -------------------------------------------------
# These only strip *formatting* (symbols, thousands separators). They never
# invent a value: anything still unparseable raises and is handled upstream.

_MONEY_NOISE = re.compile(r"(?i)(₹|\brs\.?|\binr\b|\$|,|\s)")
_CURRENCY_ALIASES = {"₹": "INR", "RS": "INR", "RS.": "INR", "RUPEES": "INR", "$": "USD"}


def _clean_number(value: Any) -> Any:
    if isinstance(value, str):
        cleaned = _MONEY_NOISE.sub("", value)
        return cleaned or None
    return value


def _clean_currency(value: Any) -> Any:
    if isinstance(value, str):
        cleaned = value.strip().upper()
        return _CURRENCY_ALIASES.get(cleaned, cleaned) or None
    return value


class InvoiceLineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    sku: str | None = None
    quantity: Decimal = Field(gt=Decimal("0"))
    unit_price: Decimal = Field(ge=Decimal("0"))
    line_total: Decimal = Field(ge=Decimal("0"))
    tax_rate: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("100"))

    @field_validator("quantity", "unit_price", "line_total", "tax_rate", mode="before")
    @classmethod
    def _tolerant_numbers(cls, value: Any) -> Any:
        return _clean_number(value)


class FieldEvidence(BaseModel):
    """Where a value came from. ``verified`` is set by code, never by the LLM."""

    model_config = ConfigDict(extra="forbid")

    page: int | None = Field(default=None, ge=1)
    snippet: str | None = None
    # Signal only (e.g. OCR word confidence / 100); not proof of correctness.
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    # True when the snippet was found verbatim (whitespace/case-insensitive)
    # in the source text; False when it could not be found.
    verified: bool | None = None


class ExtractionMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: DocumentType
    page_count: int = Field(ge=1)
    file_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    field_evidence: dict[str, FieldEvidence] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    # Lowest mean OCR word confidence (0-100) across OCR'd pages; None if no OCR.
    ocr_min_confidence: float | None = Field(default=None, ge=0.0, le=100.0)
    # Extracted fields whose values were NOT found in the source text. The
    # decision engine treats these as review signals.
    ungrounded_fields: list[str] = Field(default_factory=list)


class InvoiceExtraction(BaseModel):
    """LLM output schema. Missing fields remain None for deterministic review."""

    model_config = ConfigDict(extra="forbid")

    invoice_number: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    vendor_name: str | None = None
    vendor_tax_id: str | None = None
    po_number: str | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    line_items: list[InvoiceLineItem] = Field(default_factory=list)
    subtotal: Decimal | None = Field(default=None, ge=Decimal("0"))
    tax_total: Decimal | None = Field(default=None, ge=Decimal("0"))
    shipping: Decimal | None = Field(default=None, ge=Decimal("0"))
    discount: Decimal | None = Field(default=None, ge=Decimal("0"))
    grand_total: Decimal | None = Field(default=None, ge=Decimal("0"))
    extraction: ExtractionMetadata | None = None

    @field_validator("invoice_number", "vendor_name", "vendor_tax_id", "po_number")
    @classmethod
    def strip_optional_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("subtotal", "tax_total", "shipping", "discount", "grand_total", mode="before")
    @classmethod
    def _tolerant_numbers(cls, value: Any) -> Any:
        return _clean_number(value)

    @field_validator("currency", mode="before")
    @classmethod
    def _tolerant_currency(cls, value: Any) -> Any:
        return _clean_currency(value)


# --- LLM-facing output schema ---------------------------------------------------
# Sent to the model as the JSON schema. Designed for strict structured-output
# modes: every field is required (nullable where absent is legitimate), no
# defaults, no extra keys, and values are plain strings. The application then
# parses them into ``InvoiceExtraction`` so formatting noise is handled by code.


class LLMLineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    sku: str | None
    quantity: str = Field(description="Number as printed, e.g. '10'")
    unit_price: str = Field(description="Number as printed, without currency symbol")
    line_total: str = Field(description="Number as printed, without currency symbol")
    tax_rate: str | None = Field(description="Percentage number if printed, else null")


class LLMFieldEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(description="Name of the extracted field this supports")
    page: int | None = Field(description="1-based page number where the text appears")
    snippet: str = Field(description="Short text copied verbatim from the document")


class InvoiceExtractionLLM(BaseModel):
    """Exact shape the model must return. Use null for anything not in the text."""

    model_config = ConfigDict(extra="forbid")

    invoice_number: str | None
    invoice_date: str | None = Field(description="YYYY-MM-DD, or null")
    due_date: str | None = Field(description="YYYY-MM-DD, or null")
    vendor_name: str | None = Field(description="Name of the vendor issuing the invoice")
    vendor_tax_id: str | None
    po_number: str | None = Field(description="Purchase order reference exactly as printed, or null")
    currency: str | None = Field(description="Three-letter ISO code, or null")
    line_items: list[LLMLineItem]
    subtotal: str | None
    tax_total: str | None
    shipping: str | None
    discount: str | None
    grand_total: str | None
    evidence: list[LLMFieldEvidence] = Field(
        description="Verbatim supporting snippets for the key fields you extracted"
    )

    @classmethod
    def empty_payload(cls) -> dict[str, Any]:
        """A valid all-null response; handy as a base for tests and fakes."""
        payload: dict[str, Any] = {name: None for name in cls.model_fields}
        payload["line_items"] = []
        payload["evidence"] = []
        return payload


class Vendor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_id: str = Field(min_length=1)
    legal_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    tax_id: str | None = None
    country: str = "IN"
    status: VendorStatus


class POLineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    sku: str | None = None
    quantity_ordered: Decimal = Field(gt=Decimal("0"))
    unit_price: Decimal = Field(ge=Decimal("0"))
    line_total: Decimal = Field(ge=Decimal("0"))


class PurchaseOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    po_id: str = Field(min_length=1)
    po_number: str = Field(min_length=1)
    vendor_id: str = Field(min_length=1)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    total_amount: Decimal = Field(gt=Decimal("0"))
    amount_basis: AmountBasis = AmountBasis.PRE_TAX
    status: POStatus
    line_items: list[POLineItem] = Field(default_factory=list)


class ValidationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_name: str = Field(min_length=1)
    status: CheckStatus
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    status: CheckStatus
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    recommended_action: str | None = None


class POImpact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    po_number: str
    po_net: Decimal
    previously_invoiced: Decimal
    this_invoice: Decimal
    remaining_after: Decimal
    status_after: POStatus


class DecisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Decision
    reasons: list[str] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    policy_version: str = "v1"
    summary: str = ""
    recommended_action: str = ""
    po_impact: POImpact | None = None
    vendor_match_method: str | None = None
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WorkflowRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    file_name: str = Field(min_length=1)
    invoice_number: str | None = None
    processing_status: ProcessingStatus = ProcessingStatus.RECEIVED
    decision: Decision | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None


class StageEvent(BaseModel):
    """One live-run update emitted by the orchestrator (and persisted per run)."""

    model_config = ConfigDict(extra="forbid")

    stage: StageName
    status: StageStatus
    message: str = ""
    detail: dict[str, Any] = Field(default_factory=dict, validate_default=True)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("detail", mode="before")
    @classmethod
    def normalize_warning_detail(cls, value: Any) -> Any:
        if value is None:
            value = {}
        if not isinstance(value, dict):
            return value

        detail = dict(value)
        warnings = detail.get("warnings")
        if warnings is None:
            detail["warnings"] = []
        elif isinstance(warnings, str):
            detail["warnings"] = [warnings] if warnings else []
        elif isinstance(warnings, (list, tuple)):
            detail["warnings"] = [str(item) for item in warnings if item is not None]
        else:
            detail["warnings"] = []
        return detail
