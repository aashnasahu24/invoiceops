"""Pure exact-match helpers for vendors and purchase orders."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

from rapidfuzz import fuzz

from .config import DEFAULT_POLICY, PolicyConfig
from .normalization import normalize_company_name, normalize_tax_id
from .schemas import InvoiceExtraction, InvoiceLineItem, POStatus


@dataclass(frozen=True)
class VendorMatch:
    vendor: Any | None
    method: str | None
    conflict: bool = False
    tax_id_mismatch: bool = False
    name_similarity: float | None = None
    name_mismatch: bool = False
    name_vendor: Any | None = None


@dataclass(frozen=True)
class POMatch:
    purchase_order: Any | None
    candidates: tuple[Any, ...] = ()


@dataclass(frozen=True)
class LineMatch:
    invoice_line: InvoiceLineItem
    po_line: Any | None
    score: float


def match_vendor(
    extraction: InvoiceExtraction,
    vendors: Iterable[Any],
    *,
    policy: PolicyConfig = DEFAULT_POLICY,
) -> VendorMatch:
    vendors = list(vendors)
    tax_match = None
    if extraction.vendor_tax_id:
        normalized_tax = normalize_tax_id(extraction.vendor_tax_id)
        tax_match = next(
            (
                vendor for vendor in vendors
                if normalized_tax and normalize_tax_id(getattr(vendor, "tax_id", None)) == normalized_tax
            ),
            None,
        )

    name_match = None
    extracted_name = normalize_company_name(extraction.vendor_name)
    if extracted_name:
        for vendor in vendors:
            names = [getattr(vendor, "legal_name", None), *(getattr(vendor, "aliases", None) or [])]
            if any(normalize_company_name(name) == extracted_name for name in names if name):
                name_match = vendor
                break

    if tax_match is not None:
        conflict = name_match is not None and _vendor_id(tax_match) != _vendor_id(name_match)
        name_similarity = _vendor_name_similarity(extracted_name, tax_match)
        return VendorMatch(
            tax_match,
            "tax_id",
            conflict,
            name_mismatch=name_similarity < policy.vendor_name_threshold,
            name_similarity=name_similarity,
            name_vendor=name_match,
        )
    if name_match is not None:
        legal_name = getattr(name_match, "legal_name", None)
        return VendorMatch(
            name_match,
            "name" if extracted_name == normalize_company_name(legal_name) else "alias",
            tax_id_mismatch=bool(extraction.vendor_tax_id),
            name_vendor=name_match,
        )
    return VendorMatch(None, None)


def _vendor_name_similarity(extracted_name: str, vendor: Any) -> float:
    if not extracted_name:
        return 0.0
    names = [getattr(vendor, "legal_name", None), *(getattr(vendor, "aliases", None) or [])]
    return max(
        (
            float(fuzz.token_sort_ratio(extracted_name, normalize_company_name(name)))
            for name in names
            if name and normalize_company_name(name)
        ),
        default=0.0,
    )


def match_po(
    extraction: InvoiceExtraction,
    vendor: Any | None,
    purchase_orders: Iterable[Any],
    *,
    balances: dict[str, Decimal] | None = None,
    policy: PolicyConfig = DEFAULT_POLICY,
) -> POMatch:
    purchase_orders = list(purchase_orders)
    if vendor is None:
        return POMatch(None)

    vendor_id = _vendor_id(vendor)
    if extraction.po_number:
        normalized_number = _normalize_po_number(extraction.po_number)
        exact = next(
            (
                po for po in purchase_orders
                if po.vendor_id == vendor_id
                and _normalize_po_number(po.po_number) == normalized_number
            ),
            None,
        )
        return POMatch(exact)

    eligible = [
        po for po in purchase_orders
        if po.vendor_id == vendor_id and po.status in (POStatus.OPEN, POStatus.PARTIALLY_INVOICED)
    ]
    net_amount = _invoice_net_amount(extraction)

    def rank(po: Any) -> tuple[Decimal, float, str]:
        target = (balances or {}).get(po.po_id, Decimal(po.total_amount))
        distance = abs(target - net_amount) if net_amount is not None else Decimal("0")
        line_matches = match_invoice_lines(extraction, po, policy=policy)
        similarity = sum(match.score for match in line_matches) / max(len(line_matches), 1)
        return distance, -similarity, po.po_number

    return POMatch(None, tuple(sorted(eligible, key=rank)[:3]))


def match_invoice_lines(
    extraction: InvoiceExtraction,
    purchase_order: Any | None,
    *,
    policy: PolicyConfig = DEFAULT_POLICY,
) -> tuple[LineMatch, ...]:
    """Match invoice lines one-to-one, preferring exact SKU over description."""
    if purchase_order is None:
        return tuple(LineMatch(invoice_line=line, po_line=None, score=0) for line in extraction.line_items)

    po_lines = list(purchase_order.line_items)
    unused = set(range(len(po_lines)))
    matches: list[LineMatch] = []
    for invoice_line in extraction.line_items:
        sku = _normalize_sku(invoice_line.sku)
        sku_match_index = next(
            (
                index for index, line in enumerate(po_lines)
                if index in unused and sku and sku == _normalize_sku(line.sku)
            ),
            None,
        )
        if sku_match_index is not None:
            unused.remove(sku_match_index)
            matches.append(LineMatch(invoice_line, po_lines[sku_match_index], 100.0))
            continue

        candidates = [
            (
                fuzz.token_sort_ratio(
                    _normalize_description(invoice_line.description),
                    _normalize_description(line.description),
                ),
                index,
                line,
            )
            for index, line in enumerate(po_lines)
            if index in unused
        ]
        score, best_index, best = max(candidates, key=lambda pair: pair[0], default=(0, None, None))
        if best is not None and score >= policy.line_match_threshold:
            unused.remove(best_index)
            matches.append(LineMatch(invoice_line, best, float(score)))
        else:
            matches.append(LineMatch(invoice_line, None, float(score)))
    return tuple(matches)


def _normalize_sku(value: str | None) -> str | None:
    return re.sub(r"[^A-Z0-9]", "", value.upper()) if value else None


def _normalize_description(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", value.casefold()).split())


def _normalize_po_number(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _vendor_id(vendor: Any) -> str:
    return str(vendor.vendor_id)


def _invoice_net_amount(extraction: InvoiceExtraction) -> Decimal | None:
    if extraction.subtotal is None:
        return None
    return (
        extraction.subtotal
        + (extraction.shipping or Decimal("0"))
        - (extraction.discount or Decimal("0"))
    )
