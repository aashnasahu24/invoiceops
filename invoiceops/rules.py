"""Small deterministic validation set and decision templating."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .config import DEFAULT_POLICY, PolicyConfig
from .matching import LineMatch, VendorMatch
from .schemas import (
    CheckResult,
    CheckStatus,
    Decision,
    DecisionResult,
    DocumentType,
    InvoiceExtraction,
    POImpact,
    POStatus,
    VendorStatus,
)


def required_fields_present(extraction: InvoiceExtraction) -> CheckResult:
    required = ("invoice_number", "vendor_name", "grand_total")
    missing = [field for field in required if getattr(extraction, field) is None]
    return CheckResult(
        name="required_fields_present",
        status=CheckStatus.FAILED if missing else CheckStatus.PASSED,
        message=("Missing required fields: " + ", ".join(missing)) if missing else "Required fields are present.",
        details={"missing_fields": missing},
    )


def ungrounded_fields(extraction: InvoiceExtraction) -> CheckResult:
    fields = list(extraction.extraction.ungrounded_fields if extraction.extraction else [])
    return CheckResult(
        name="ungrounded_fields",
        status=CheckStatus.FAILED if fields else CheckStatus.PASSED,
        message=("Extracted values are not grounded in the document: " + ", ".join(fields))
        if fields else "Extracted values are grounded in the document.",
        details={"fields": fields},
    )


def ocr_confidence(
    extraction: InvoiceExtraction,
    document_type: DocumentType,
    policy: PolicyConfig = DEFAULT_POLICY,
) -> CheckResult:
    confidence = extraction.extraction.ocr_min_confidence if extraction.extraction else None
    low_confidence = (
        document_type == DocumentType.SCANNED_PDF
        and (confidence is None or confidence < policy.min_ocr_confidence)
    )
    return CheckResult(
        name="ocr_confidence",
        status=CheckStatus.WARNING if low_confidence else CheckStatus.PASSED,
        message=(
            f"OCR confidence {confidence:.0f}%" if confidence is not None else "OCR confidence unavailable"
        ) + (
            f" is below the {policy.min_ocr_confidence:.0f}% threshold; manual review is required."
            if low_confidence else
            " meets policy or OCR was not needed."
        ),
        details={
            "confidence": confidence,
            "threshold": policy.min_ocr_confidence,
            "escalates": low_confidence,
        },
    )


def vendor_status(vendor: Any | None) -> CheckResult:
    return vendor_status_with_conflict(vendor)


def vendor_status_with_conflict(vendor: Any | None, *, conflict: bool = False) -> CheckResult:
    if vendor is None:
        return CheckResult(
            name="vendor_status",
            status=CheckStatus.WARNING,
            message="No vendor matched the extracted identity.",
            details={"vendor_id": None, "escalates": True},
        )
    status = vendor.status
    if status == VendorStatus.BLOCKED:
        return CheckResult(
            name="vendor_status",
            status=CheckStatus.FAILED,
            message=f"Vendor {vendor.legal_name} is blocked.",
            details={"vendor_id": vendor.vendor_id, "status": status.value, "hard_reject": True, "hard_block": True},
            recommended_action="Do not process this invoice; contact the vendor compliance owner.",
        )
    if conflict:
        return CheckResult(
            name="vendor_status",
            status=CheckStatus.WARNING,
            message=f"Vendor tax ID matched {vendor.legal_name}, but the extracted name matched a different vendor.",
            details={"vendor_id": vendor.vendor_id, "status": status.value, "identity_conflict": True, "escalates": True},
        )
    if status != VendorStatus.APPROVED:
        return CheckResult(
            name="vendor_status",
            status=CheckStatus.WARNING,
            message=f"Vendor {vendor.legal_name} is not approved.",
            details={"vendor_id": vendor.vendor_id, "status": status.value, "escalates": True},
        )
    return CheckResult(
        name="vendor_status",
        status=CheckStatus.PASSED,
        message=f"Vendor {vendor.legal_name} is approved.",
        details={"vendor_id": vendor.vendor_id, "status": status.value},
    )


def po_found(extraction: InvoiceExtraction, purchase_order: Any | None, candidates: tuple[Any, ...] = ()) -> CheckResult:
    if purchase_order is not None:
        return CheckResult(
            name="po_found",
            status=CheckStatus.PASSED,
            message=f"Purchase order {purchase_order.po_number} matched exactly.",
            details={"po_id": purchase_order.po_id, "po_number": purchase_order.po_number},
        )
    missing = extraction.po_number is None
    candidate_numbers = [po.po_number for po in candidates]
    message = (
        "Purchase order reference is missing."
        if missing else f"Purchase order {extraction.po_number} was not found for the matched vendor."
    )
    return CheckResult(
        name="po_found",
        status=CheckStatus.WARNING,
        message=message,
        details={
            "requested_po_number": extraction.po_number,
            "candidate_po_numbers": candidate_numbers,
            "escalates": True,
        },
    )


def arithmetic_consistency(
    extraction: InvoiceExtraction,
    policy: PolicyConfig = DEFAULT_POLICY,
) -> CheckResult:
    tolerance = policy.rounding_tolerance
    differences: dict[str, str] = {}
    if extraction.subtotal is None or not extraction.line_items:
        return CheckResult(
            name="arithmetic_consistency",
            status=CheckStatus.WARNING,
            message="Line-item arithmetic cannot be checked because subtotal or line items are missing.",
            details={"escalates": True},
        )

    line_sum = sum((item.line_total for item in extraction.line_items), Decimal("0"))
    differences["line_sum_vs_subtotal"] = str(line_sum - extraction.subtotal)
    failures = [abs(line_sum - extraction.subtotal) > tolerance]
    ocr_decimal_hint = _factor_of_ten_or_hundred(line_sum, extraction.subtotal)

    if extraction.grand_total is None:
        failures.append(True)
        message = "Grand total is missing; arithmetic checks failed."
    elif extraction.tax_total is None:
        failures.append(False)
        message = "Tax is not itemized; total arithmetic cannot be fully checked."
    else:
        expected_total = (
            extraction.subtotal
            + extraction.tax_total
            + (extraction.shipping or Decimal("0"))
            - (extraction.discount or Decimal("0"))
        )
        differences["components_vs_grand_total"] = str(expected_total - extraction.grand_total)
        failures.append(abs(expected_total - extraction.grand_total) > tolerance)
        ocr_decimal_hint = ocr_decimal_hint or _factor_of_ten_or_hundred(
            expected_total, extraction.grand_total
        )
        message = "Invoice arithmetic is consistent." if not any(failures) else "Invoice arithmetic differs beyond rounding tolerance."

    failed = any(failures)
    if failed and ocr_decimal_hint is not None:
        message += f" off by a factor of {ocr_decimal_hint}: possible OCR loss of decimal point."
    return CheckResult(
        name="arithmetic_consistency",
        status=(
            CheckStatus.FAILED if failed
            else CheckStatus.WARNING if extraction.tax_total is None
            else CheckStatus.PASSED
        ),
        message=message,
        details={
            "differences": differences,
            "rounding_tolerance": str(tolerance),
            "ocr_decimal_hint": ocr_decimal_hint,
            **({"escalates": True} if extraction.tax_total is None else {}),
        },
    )


def _factor_of_ten_or_hundred(left: Decimal, right: Decimal) -> int | None:
    if not left or not right:
        return None
    ratio = abs(left / right)
    factor = max(ratio, Decimal("1") / ratio)
    if factor in (Decimal("10"), Decimal("100")):
        return int(factor)
    return None


def line_on_po(line_matches: tuple[LineMatch, ...], purchase_order: Any | None) -> CheckResult:
    if purchase_order is None:
        return CheckResult(
            name="line_on_po",
            status=CheckStatus.SKIPPED,
            message="Not evaluated because no purchase order matched.",
            details={"outcome": "NOT_APPLICABLE", "unmatched_lines": [], "prerequisite": "po_found"},
        )
    unmatched = [
        {"description": match.invoice_line.description, "score": match.score}
        for match in line_matches if match.po_line is None
    ]
    return CheckResult(
        name="line_on_po",
        status=CheckStatus.FAILED if unmatched else CheckStatus.PASSED,
        message=(
            "Invoice lines not found on the purchase order: "
            + ", ".join(item["description"] for item in unmatched)
            if unmatched else "All invoice lines match purchase-order lines."
        ),
        details={"unmatched_lines": unmatched},
        recommended_action="Confirm the unmatched invoice lines with the PO owner." if unmatched else None,
    )


def unit_price_variance(
    line_matches: tuple[LineMatch, ...],
    policy: PolicyConfig = DEFAULT_POLICY,
) -> CheckResult:
    comparisons: list[dict[str, Any]] = []
    for match in line_matches:
        if match.po_line is None:
            continue
        po_price = Decimal(match.po_line.unit_price)
        invoice_price = Decimal(match.invoice_line.unit_price)
        variance = (
            abs(invoice_price - po_price) / po_price if po_price else
            (Decimal("0") if invoice_price == 0 else Decimal("Infinity"))
        )
        comparisons.append({
            "description": match.invoice_line.description,
            "invoice_price": str(invoice_price),
            "po_price": str(po_price),
            "variance_pct": str(variance * 100),
            "allowed_pct": str(policy.price_tolerance_pct * 100),
        })
    if not comparisons:
        return CheckResult(
            name="unit_price_variance",
            status=CheckStatus.SKIPPED,
            message="Not evaluated because no invoice lines matched a purchase-order line.",
            details={"outcome": "NOT_APPLICABLE", "comparisons": [], "prerequisite": "line_on_po"},
        )
    excessive = [
        comparison for comparison in comparisons
        if Decimal(comparison["variance_pct"]) > policy.price_tolerance_pct * 100
    ]
    nonzero = [
        comparison for comparison in comparisons
        if Decimal(comparison["variance_pct"]) > 0
    ]
    message = (
        "Invoice unit price exceeds policy tolerance: "
        + "; ".join(
            f"{item['description']}: {item['invoice_price']} vs {item['po_price']} "
            f"({Decimal(item['variance_pct']):.1f}% variance)"
            for item in excessive
        )
        if excessive else
        "Unit prices match the PO within tolerance; small differences are noted."
        if nonzero else "Invoice unit prices match the PO."
    )
    return CheckResult(
        name="unit_price_variance",
        status=CheckStatus.FAILED if excessive else CheckStatus.WARNING if nonzero else CheckStatus.PASSED,
        message=message,
        details={"comparisons": comparisons, "escalates": bool(excessive)},
        recommended_action="Ask the PO owner to approve the unit-price variance." if excessive else None,
    )


def quantity_vs_ordered(
    line_matches: tuple[LineMatch, ...],
    previously_approved_quantities: dict[int, Decimal] | None = None,
) -> CheckResult:
    previously_approved_quantities = previously_approved_quantities or {}
    comparisons = []
    for match in line_matches:
        if match.po_line is None:
            continue
        prior = previously_approved_quantities.get(match.po_line.id, Decimal("0"))
        remaining = Decimal(match.po_line.quantity_ordered) - prior
        billed = Decimal(match.invoice_line.quantity)
        comparisons.append({
            "description": match.invoice_line.description,
            "po_line_id": match.po_line.id,
            "billed_quantity": str(billed),
            "previously_approved_quantity": str(prior),
            "ordered_quantity": str(match.po_line.quantity_ordered),
            "remaining_quantity": str(remaining),
        })
    exceeded = [
        item for item in comparisons
        if Decimal(item["billed_quantity"]) > Decimal(item["remaining_quantity"])
    ]
    if not comparisons:
        return CheckResult(
            name="quantity_vs_ordered",
            status=CheckStatus.SKIPPED,
            message="Not evaluated because no invoice lines matched a purchase-order line.",
            details={"outcome": "NOT_APPLICABLE", "comparisons": [], "prerequisite": "line_on_po"},
        )
    return CheckResult(
        name="quantity_vs_ordered",
        status=CheckStatus.FAILED if exceeded else CheckStatus.PASSED,
        message=(
            "Billed quantity exceeds remaining ordered quantity for: "
            + ", ".join(item["description"] for item in exceeded)
            if exceeded else "Billed quantities fit within the remaining PO quantities."
        ),
        details={"comparisons": comparisons},
        recommended_action="Confirm the excess quantity with the PO owner." if exceeded else None,
    )


def po_status(purchase_order: Any | None) -> CheckResult:
    if purchase_order is None:
        return CheckResult(
            name="po_status",
            status=CheckStatus.SKIPPED,
            message="Not evaluated because no purchase order matched.",
            details={"outcome": "NOT_APPLICABLE", "prerequisite": "po_found"},
        )
    allowed = purchase_order.status in (POStatus.OPEN, POStatus.PARTIALLY_INVOICED)
    return CheckResult(
        name="po_status",
        status=CheckStatus.PASSED if allowed else CheckStatus.FAILED,
        message=(
            f"Purchase order {purchase_order.po_number} is {purchase_order.status.value}."
            if allowed else
            f"Purchase order {purchase_order.po_number} is {purchase_order.status.value} and cannot accept invoices."
        ),
        details={
            "po_id": purchase_order.po_id,
            "status": purchase_order.status.value,
            **({"hard_block": True} if not allowed else {}),
        },
        recommended_action="Ask the PO owner to reopen or replace the purchase order." if not allowed else None,
    )


def vendor_identity_conflict(
    match: VendorMatch | bool,
    policy: PolicyConfig = DEFAULT_POLICY,
) -> CheckResult:
    if isinstance(match, bool):
        conflict = match
        message = (
            "Extracted tax ID and vendor name resolve to different vendors; possible identity fraud signal."
            if conflict else "Tax ID and vendor name do not conflict."
        )
        details = {"conflict": conflict}
    elif match.tax_id_mismatch:
        conflict = True
        message = (
            "The extracted tax ID did not match a supplier record, but the vendor name matched "
            f"{match.vendor.legal_name}."
        )
        details = {"tax_id_mismatch": True, "vendor_id": match.vendor.vendor_id}
    elif match.conflict:
        conflict = True
        tax_name = match.vendor.legal_name if match.vendor is not None else "an unknown supplier"
        name_name = (
            match.name_vendor.legal_name if match.name_vendor is not None else "an unknown supplier"
        )
        message = f"The tax ID matches {tax_name}, but the vendor name matches {name_name}."
        details = {
            "conflict": True,
            "tax_vendor_id": match.vendor.vendor_id if match.vendor is not None else None,
            "name_vendor_id": match.name_vendor.vendor_id if match.name_vendor is not None else None,
        }
    elif match.name_mismatch:
        conflict = True
        similarity = match.name_similarity or 0
        message = (
            f"The tax ID matches {match.vendor.legal_name}, but the extracted vendor name "
            f"similarity is {similarity:.0f}%, below the {policy.vendor_name_threshold}% threshold."
        )
        details = {
            "name_mismatch": True,
            "name_similarity": similarity,
            "threshold": policy.vendor_name_threshold,
            "vendor_id": match.vendor.vendor_id,
        }
    else:
        conflict = False
        message = "Tax ID and vendor name do not conflict."
        details = {
            "conflict": False,
            "tax_id_mismatch": False,
            "name_similarity": match.name_similarity,
            "name_mismatch": False,
        }
    return CheckResult(
        name="vendor_identity_conflict",
        status=CheckStatus.FAILED if conflict else CheckStatus.PASSED,
        message=message,
        details=details,
        recommended_action="Verify the vendor identity against the supplier master" if conflict else None,
    )


def line_arithmetic(
    extraction: InvoiceExtraction,
    policy: PolicyConfig = DEFAULT_POLICY,
) -> CheckResult:
    mismatches = []
    for index, item in enumerate(extraction.line_items, start=1):
        expected = item.quantity * item.unit_price
        actual = item.line_total
        difference = abs(expected - actual)
        if difference > policy.rounding_tolerance:
            mismatches.append({
                "line": index,
                "description": item.description,
                "expected": str(expected),
                "actual": str(actual),
                "difference": str(difference),
            })
    return CheckResult(
        name="line_arithmetic",
        status=CheckStatus.FAILED if mismatches else CheckStatus.PASSED,
        message=(
            "Line arithmetic differs beyond rounding tolerance: "
            + "; ".join(
                f"line {item['line']} ({item['description']}): expected "
                f"{item['expected']}, actual {item['actual']}"
                for item in mismatches
            )
            if mismatches else "Every invoice line's quantity times unit price matches its line total."
        ),
        details={"mismatches": mismatches, "rounding_tolerance": str(policy.rounding_tolerance)},
        recommended_action="Verify the invoice line calculations against the source document."
        if mismatches else None,
    )


def currency_match(extraction: InvoiceExtraction, purchase_order: Any | None) -> CheckResult:
    if purchase_order is None and extraction.currency is not None:
        return CheckResult(
            name="currency_match",
            status=CheckStatus.SKIPPED,
            message="Not evaluated because no purchase order matched.",
            details={"outcome": "NOT_APPLICABLE", "invoice_currency": extraction.currency,
                     "prerequisite": "po_found"},
        )
    if extraction.currency is None:
        return CheckResult(
            name="currency_match",
            status=CheckStatus.WARNING,
            message="Invoice currency is missing and cannot be compared with the purchase order.",
            details={"invoice_currency": None, "escalates": True},
            recommended_action="Confirm the invoice currency against the purchase order.",
        )
    if purchase_order is None:
        return CheckResult(
            name="currency_match",
            status=CheckStatus.WARNING,
            message="Invoice currency cannot be compared because no purchase order matched.",
            details={"invoice_currency": extraction.currency, "escalates": True},
        )
    matches = extraction.currency == purchase_order.currency
    return CheckResult(
        name="currency_match",
        status=CheckStatus.PASSED if matches else CheckStatus.FAILED,
        message=(
            f"Invoice currency {extraction.currency} matches purchase-order currency."
            if matches else
            f"Invoice currency {extraction.currency} does not match purchase-order currency "
            f"{purchase_order.currency}."
        ),
        details={
            "invoice_currency": extraction.currency,
            "po_currency": purchase_order.currency,
        },
        recommended_action="Verify the invoice currency against the purchase order."
        if not matches else None,
    )


def duplicate_business_key(candidate: Any | None) -> CheckResult:
    if candidate is None:
        return CheckResult(name="duplicate_business_key", status=CheckStatus.PASSED, message="No active business-key duplicate.")
    run = max(candidate.workflow_runs, key=lambda row: row.started_at) if candidate.workflow_runs else None
    when = candidate.invoice_date.isoformat() if candidate.invoice_date else "unknown date"
    decision = candidate.current_decision.value if candidate.current_decision else "unknown decision"
    run_id = run.run_id if run else "unknown run"
    return CheckResult(
        name="duplicate_business_key",
        status=CheckStatus.FAILED,
        message=(
            f"Active duplicate invoice {candidate.invoice_id}, run {run_id}, "
            f"dated {when}, currently {decision}."
        ),
        details={
            "invoice_id": candidate.invoice_id,
            "run_id": run_id,
            "invoice_date": when,
            "decision": decision,
            "hard_reject": True,
            "hard_block": True,
        },
        recommended_action="Do not process the duplicate; review the original invoice.",
    )


def soft_duplicate(
    candidate: Any | None,
    *,
    invoice_number: str | None,
) -> CheckResult:
    if candidate is None:
        return CheckResult(name="soft_duplicate", status=CheckStatus.PASSED, message="No likely soft duplicate.")
    candidate_date = candidate.invoice_date.isoformat() if candidate.invoice_date else "unknown date"
    return CheckResult(
        name="soft_duplicate",
        status=CheckStatus.WARNING,
        message=f"Possible duplicate invoice {candidate.invoice_id} dated {candidate_date} has the same amount but a different number.",
        details={"candidate_invoice_id": candidate.invoice_id, "candidate_date": candidate_date, "invoice_number": invoice_number, "escalates": True},
        recommended_action="Compare the candidate invoice before processing.",
    )


def missing_invoice_number(extraction: InvoiceExtraction) -> CheckResult:
    missing = not extraction.invoice_number
    return CheckResult(
        name="missing_invoice_number",
        status=CheckStatus.WARNING if missing else CheckStatus.PASSED,
        message="Invoice number is missing; it cannot be keyed reliably." if missing else "Invoice number is present.",
        details={"escalates": missing},
        recommended_action="Ask the vendor for an invoice number." if missing else None,
    )


def po_balance(
    extraction: InvoiceExtraction,
    purchase_order: Any | None,
    remaining_balance: Decimal | None,
    *,
    previously_invoiced: Decimal | None = None,
) -> CheckResult:
    net_amount = _net_amount(extraction)
    if purchase_order is None or remaining_balance is None or net_amount is None:
        if purchase_order is None:
            return CheckResult(
                name="po_balance",
                status=CheckStatus.SKIPPED,
                message="Not evaluated because no purchase order matched.",
                details={"this_invoice": str(net_amount) if net_amount is not None else None,
                         "outcome": "NOT_APPLICABLE", "prerequisite": "po_found"},
            )
        return CheckResult(
            name="po_balance",
            status=CheckStatus.WARNING,
            message="Purchase order balance cannot be checked.",
            details={
                "this_invoice": str(net_amount) if net_amount is not None else None,
                "escalates": True,
            },
        )
    exceeds = net_amount > remaining_balance
    po_net = Decimal(purchase_order.total_amount)
    previously_invoiced = (
        po_net - remaining_balance if previously_invoiced is None else previously_invoiced
    )
    remaining_after = remaining_balance - net_amount
    utilisation = (
        (previously_invoiced + net_amount) / po_net * 100 if po_net else Decimal("0")
    )
    return CheckResult(
        name="po_balance",
        status=CheckStatus.FAILED if exceeds else CheckStatus.PASSED,
        message=(
            f"Invoice net amount {net_amount} exceeds remaining PO balance {remaining_balance}."
            if exceeds else f"Invoice net amount {net_amount} fits remaining PO balance {remaining_balance}."
        ),
        details={
            "po_net": str(po_net),
            "previously_invoiced": str(previously_invoiced),
            "this_invoice": str(net_amount),
            "remaining": str(remaining_balance),
            "remaining_after": str(remaining_after),
            "utilisation_pct": str(utilisation),
            "net_amount": str(net_amount),
            "remaining_balance": str(remaining_balance),
        },
        recommended_action="Ask the PO owner to confirm the amount exceeding the PO balance." if exceeds else None,
    )


def derive_po_status(po_net: Decimal, invoiced: Decimal) -> POStatus:
    if invoiced >= po_net:
        return POStatus.CLOSED
    if invoiced > 0:
        return POStatus.PARTIALLY_INVOICED
    return POStatus.OPEN


def run_checks(
    extraction: InvoiceExtraction,
    *,
    vendor: Any | None,
    purchase_order: Any | None,
    po_candidates: tuple[Any, ...] = (),
    remaining_balance: Decimal | None = None,
    document_type: DocumentType,
    vendor_conflict: bool = False,
    vendor_match: VendorMatch | None = None,
    line_matches: tuple[LineMatch, ...] = (),
    previously_invoiced: Decimal | None = None,
    previously_approved_quantities: dict[int, Decimal] | None = None,
    duplicate_candidate: Any | None = None,
    soft_duplicate_candidate: Any | None = None,
    policy: PolicyConfig = DEFAULT_POLICY,
) -> list[CheckResult]:
    return [
        required_fields_present(extraction),
        ungrounded_fields(extraction),
        ocr_confidence(extraction, document_type, policy),
        vendor_status_with_conflict(vendor, conflict=vendor_conflict),
        vendor_identity_conflict(vendor_match or vendor_conflict, policy),
        currency_match(extraction, purchase_order),
        missing_invoice_number(extraction),
        duplicate_business_key(duplicate_candidate),
        soft_duplicate(soft_duplicate_candidate, invoice_number=extraction.invoice_number),
        po_found(extraction, purchase_order, po_candidates),
        po_status(purchase_order),
        line_on_po(line_matches, purchase_order),
        unit_price_variance(line_matches, policy),
        quantity_vs_ordered(line_matches, previously_approved_quantities),
        arithmetic_consistency(extraction, policy),
        line_arithmetic(extraction, policy),
        po_balance(
            extraction,
            purchase_order,
            remaining_balance,
            previously_invoiced=previously_invoiced,
        ),
    ]


def decide(
    checks: list[CheckResult],
    policy: PolicyConfig = DEFAULT_POLICY,
    *,
    po_impact: POImpact | None = None,
    vendor_match_method: str | None = None,
) -> DecisionResult:
    hard_reject = any(
        check.status == CheckStatus.FAILED and check.details.get("hard_reject", False)
        for check in checks
    )
    failed = any(check.status == CheckStatus.FAILED for check in checks)
    escalating_warning = any(
        check.status == CheckStatus.WARNING and check.details.get("escalates", True)
        for check in checks
    )

    if hard_reject:
        decision = Decision.REJECTED
    elif failed or escalating_warning:
        decision = Decision.MANUAL_REVIEW
    else:
        decision = Decision.APPROVED

    for check in checks:
        if check.status in (CheckStatus.FAILED, CheckStatus.WARNING) and check.recommended_action is None:
            check.recommended_action = _action_for_check(check.name)
    reasons = [
        check.message for check in sorted(checks, key=_severity_rank)
        if check.status == CheckStatus.FAILED
        and (decision != Decision.REJECTED or check.details.get("hard_reject", False))
        or (
            decision != Decision.REJECTED
            and
            check.status == CheckStatus.WARNING
            and check.details.get("escalates", True)
        )
    ][:3]
    action = _recommended_action(decision, checks)
    summary = _summary(decision, reasons, po_impact)
    return DecisionResult(
        decision=decision,
        reasons=reasons,
        checks=checks,
        policy_version=policy.version,
        summary=summary,
        recommended_action=action,
        po_impact=po_impact,
        vendor_match_method=vendor_match_method,
    )


def _net_amount(extraction: InvoiceExtraction) -> Decimal | None:
    if extraction.subtotal is None:
        return None
    return extraction.subtotal + (extraction.shipping or Decimal("0")) - (extraction.discount or Decimal("0"))


def _recommended_action(decision: Decision, checks: list[CheckResult]) -> str:
    if decision == Decision.APPROVED:
        return "No action required; continue normal invoice processing."
    failed_names = {check.name for check in checks if check.status in (CheckStatus.FAILED, CheckStatus.WARNING)}
    if "vendor_identity_conflict" in failed_names:
        return "Verify the vendor identity against the supplier master"
    if "missing_invoice_number" in failed_names:
        return "Ask the vendor for an invoice number."
    if "vendor_status" in failed_names and any(
        check.details.get("hard_reject") for check in checks if check.name == "vendor_status"
    ):
        return "Do not process this invoice; contact the vendor compliance owner."
    if "duplicate_business_key" in failed_names:
        return "Do not process the duplicate; review the original invoice."
    if "po_status" in failed_names:
        return "Ask the PO owner to reopen or replace the purchase order."
    if "required_fields_present" in failed_names:
        return "Request the missing invoice number, vendor name, or grand total from the vendor."
    if "ocr_confidence" in failed_names:
        return "Manually verify the scanned invoice against the original document."
    if "po_found" in failed_names:
        return "Request the purchase order reference or confirm the matching open PO."
    if "po_balance" in failed_names:
        return "Ask the reviewer to confirm the invoice amount against the remaining PO balance."
    return "Route the invoice to an AP reviewer for verification."


def _action_for_check(name: str) -> str:
    return {
        "missing_invoice_number": "Ask the vendor for an invoice number.",
        "required_fields_present": "Request the missing required invoice details.",
        "ungrounded_fields": "Verify the ungrounded values against the original document.",
        "ocr_confidence": "Manually verify the scanned invoice against the original document.",
        "po_found": "Request the PO reference or confirm the matching open PO.",
        "line_on_po": "Confirm unmatched invoice lines with the PO owner.",
        "unit_price_variance": "Ask the PO owner to approve the unit-price variance.",
        "quantity_vs_ordered": "Confirm billed quantities with the PO owner.",
        "po_balance": "Ask the PO owner to confirm the amount against remaining PO balance.",
        "po_status": "Ask the PO owner to reopen or replace the purchase order.",
        "vendor_identity_conflict": "Verify vendor identity against the supplier master.",
        "duplicate_business_key": "Review the original invoice; do not process a duplicate.",
        "soft_duplicate": "Compare the candidate invoice before processing.",
        "arithmetic_consistency": "Verify the invoice calculations against the source document.",
    }.get(name, "Route the invoice to an AP reviewer for verification.")


def _severity_rank(check: CheckResult) -> int:
    if check.status == CheckStatus.FAILED and check.details.get("hard_reject"):
        return 0
    if check.status == CheckStatus.FAILED and check.details.get("hard_block"):
        return 1
    if check.status == CheckStatus.FAILED:
        return {
            "po_balance": 2,
            "unit_price_variance": 3,
            "vendor_identity_conflict": 4,
            "line_on_po": 5,
            "quantity_vs_ordered": 6,
        }.get(check.name, 7)
    if check.status == CheckStatus.WARNING:
        return {
            "ocr_confidence": 8,
            "po_found": 9,
            "missing_invoice_number": 10,
        }.get(check.name, 11)
    return 12


def _summary(
    decision: Decision,
    reasons: list[str],
    po_impact: POImpact | None = None,
) -> str:
    label = {
        Decision.APPROVED: "Approved.",
        Decision.MANUAL_REVIEW: "Sent to review.",
        Decision.REJECTED: "Rejected.",
    }[decision]
    if po_impact is not None and decision == Decision.APPROVED:
        cumulative = po_impact.previously_invoiced + po_impact.this_invoice
        percent = cumulative / po_impact.po_net * 100 if po_impact.po_net else Decimal("0")
        summary = (
            f"{label} {cumulative:,.0f} of {po_impact.po_net:,.0f} "
            f"on {po_impact.po_number} used ({percent:.0f}%)."
        )
        if po_impact.status_after == POStatus.CLOSED:
            summary += f" {po_impact.po_number} is now fully invoiced (closed)."
        return summary
    if not reasons:
        return label
    sentence_parts = [reason.strip().rstrip(".!?") for reason in reasons]
    sentences = ". ".join(part[:1].upper() + part[1:] for part in sentence_parts)
    return label + " " + sentences + "."
