import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from invoiceops.matching import match_invoice_lines, match_po, match_vendor
from invoiceops.models import PurchaseOrderORM, VendorORM
from invoiceops.rules import (
    arithmetic_consistency,
    currency_match,
    decide,
    derive_po_status,
    duplicate_business_key,
    line_on_po,
    missing_invoice_number,
    ocr_confidence,
    po_balance,
    po_found,
    po_status,
    quantity_vs_ordered,
    required_fields_present,
    soft_duplicate,
    unit_price_variance,
    ungrounded_fields,
    vendor_identity_conflict,
    vendor_status,
)
from invoiceops.schemas import (
    CheckResult,
    CheckStatus,
    Decision,
    DocumentType,
    ExtractionMetadata,
    InvoiceExtraction,
    POStatus,
)
from invoiceops.seed import seed_reference_data


ROOT = Path(__file__).resolve().parents[1]
LABELS = json.loads((ROOT / "data" / "expected_extractions.json").read_text(encoding="utf-8"))


def extraction(name="happy_path_invoice.pdf", **updates):
    payload = dict(LABELS[name])
    payload["line_items"] = [
        {
            "description": item["description"],
            "quantity": item["quantity"],
            "unit_price": item["unit_price"],
            "line_total": item["line_total"],
            "tax_rate": None,
        }
        for item in payload["line_items"]
    ]
    payload.update(updates)
    result = InvoiceExtraction.model_validate(payload)
    result.extraction = ExtractionMetadata(
        document_type=DocumentType.SCANNED_PDF if name == "scanned_missing_po.pdf" else DocumentType.DIGITAL_PDF,
        page_count=1,
        ungrounded_fields=[],
        ocr_min_confidence=45 if name == "scanned_missing_po.pdf" else None,
    )
    return result


def test_match_vendor_tax_id_first_and_detects_conflicting_name(db_session):
    seed_reference_data(db_session, ROOT / "data")
    vendors = db_session.query(VendorORM).all()
    result = match_vendor(
        extraction(vendor_name="XYZ Solutions", vendor_tax_id="29ABCDE1234F1Z5"),
        vendors,
    )
    assert result.vendor.vendor_id == "V001"
    assert result.method == "tax_id"
    assert result.conflict is True


def test_match_vendor_by_alias_and_po_exact_only(db_session):
    seed_reference_data(db_session, ROOT / "data")
    vendors = db_session.query(VendorORM).all()
    pos = db_session.query(PurchaseOrderORM).all()
    extracted = extraction(vendor_name="ABC Tech", vendor_tax_id=None)
    vendor_match = match_vendor(extracted, vendors)
    assert vendor_match.vendor.vendor_id == "V001"
    assert vendor_match.method == "alias"

    exact = match_po(extracted, vendor_match.vendor, pos)
    assert exact.purchase_order.po_number == "PO-1001"

    near_match = match_po(
        extraction(po_number="PO-1001X"),
        vendor_match.vendor,
        pos,
    )
    assert near_match.purchase_order is None


def test_missing_po_returns_ranked_open_candidates(db_session):
    seed_reference_data(db_session, ROOT / "data")
    vendor = db_session.get(VendorORM, "V001")
    pos = db_session.query(PurchaseOrderORM).all()
    result = match_po(
        extraction(po_number=None),
        vendor,
        pos,
        balances={"POID-1001": Decimal("10000"), "POID-1003": Decimal("100000")},
    )
    assert [po.po_number for po in result.candidates] == ["PO-1001", "PO-1003"]


def test_required_ungrounded_ocr_and_vendor_checks(db_session):
    seed_reference_data(db_session, ROOT / "data")
    invoice = extraction(invoice_number=None)
    assert required_fields_present(invoice).status == CheckStatus.FAILED

    invoice.extraction.ungrounded_fields = ["grand_total"]
    assert ungrounded_fields(invoice).status == CheckStatus.FAILED

    scanned = extraction("scanned_missing_po.pdf")
    low_ocr_check = ocr_confidence(scanned, DocumentType.SCANNED_PDF)
    assert low_ocr_check.status == CheckStatus.WARNING
    assert decide([low_ocr_check]).decision == Decision.MANUAL_REVIEW
    assert ocr_confidence(invoice, DocumentType.DIGITAL_PDF).status == CheckStatus.PASSED

    blocked = db_session.get(VendorORM, "V003")
    assert vendor_status(blocked).details["hard_reject"] is True
    assert vendor_status(None).status == CheckStatus.WARNING


def test_po_found_arithmetic_and_balance_checks(db_session):
    seed_reference_data(db_session, ROOT / "data")
    invoice = extraction()
    po = db_session.get(PurchaseOrderORM, "POID-1001")
    assert po_found(invoice, po).status == CheckStatus.PASSED
    assert po_found(extraction("scanned_missing_po.pdf"), None).status == CheckStatus.WARNING
    assert arithmetic_consistency(invoice).status == CheckStatus.PASSED
    assert arithmetic_consistency(extraction(subtotal=Decimal("24000"))).status == CheckStatus.FAILED
    assert po_balance(invoice, po, Decimal("25000")).status == CheckStatus.PASSED
    assert po_balance(invoice, po, Decimal("24999")).status == CheckStatus.FAILED


def test_checks_without_a_matched_po_are_skipped_not_passed_or_warning():
    invoice = extraction()
    checks = [
        currency_match(invoice, None),
        po_status(None),
        line_on_po((), None),
        po_balance(invoice, None, None),
    ]
    assert all(check.status == CheckStatus.SKIPPED for check in checks)
    assert all(check.details["outcome"] == "NOT_APPLICABLE" for check in checks)
    assert decide(checks).decision == Decision.APPROVED


def test_decide_approved_review_and_hard_reject_branches():
    passed = CheckResult(name="check", status=CheckStatus.PASSED, message="ok")
    warning = CheckResult(
        name="check",
        status=CheckStatus.WARNING,
        message="review",
        details={"escalates": True},
    )
    failed = CheckResult(name="check", status=CheckStatus.FAILED, message="failed")
    hard_failure = CheckResult(
        name="vendor_status",
        status=CheckStatus.FAILED,
        message="blocked",
        details={"hard_reject": True},
    )
    assert decide([passed]).decision == Decision.APPROVED
    assert decide([warning]).decision == Decision.MANUAL_REVIEW
    assert decide([failed]).decision == Decision.MANUAL_REVIEW
    assert decide([warning, hard_failure]).decision == Decision.REJECTED


def test_rejected_decision_reasons_only_include_hard_rejects():
    unrelated = CheckResult(name="price", status=CheckStatus.FAILED, message="Price differs.")
    blocked = CheckResult(
        name="vendor_status",
        status=CheckStatus.FAILED,
        message="Vendor is blocked.",
        details={"hard_reject": True},
    )
    result = decide([unrelated, blocked])
    assert result.decision == Decision.REJECTED
    assert result.reasons == ["Vendor is blocked."]
    assert "Price differs" not in result.summary


def test_line_matching_price_quantity_and_status_checks(db_session):
    seed_reference_data(db_session, ROOT / "data")
    po = db_session.get(PurchaseOrderORM, "POID-1002")
    invoice = extraction("po_overrun_invoice.pdf")
    matches = match_invoice_lines(invoice, po)

    assert matches[0].po_line.description == "Laptop"
    assert line_on_po(matches, po).status == CheckStatus.PASSED
    price_check = unit_price_variance(matches)
    assert price_check.status == CheckStatus.FAILED
    assert "6.7%" in price_check.message
    assert quantity_vs_ordered(matches).status == CheckStatus.FAILED
    assert po_status(po).status == CheckStatus.PASSED

    po.status = POStatus.CLOSED
    closed_check = po_status(po)
    assert closed_check.status == CheckStatus.FAILED
    assert closed_check.details["hard_block"] is True
    assert derive_po_status(Decimal("100"), Decimal("100")) == POStatus.CLOSED
    assert derive_po_status(Decimal("100"), Decimal("40")) == POStatus.PARTIALLY_INVOICED
    assert derive_po_status(Decimal("100"), Decimal("0")) == POStatus.OPEN


def test_arithmetic_decimal_point_loss_hint_uses_scanned_scale():
    invoice = extraction(
        "scanned_missing_po.pdf",
        subtotal=Decimal("10000"),
        tax_total=Decimal("180000"),
        grand_total=Decimal("11800"),
        line_items=[
            {
                "description": "Laptop Service",
                "quantity": Decimal("1"),
                "unit_price": Decimal("1000000"),
                "line_total": Decimal("1000000"),
            }
        ],
    )
    check = arithmetic_consistency(invoice)
    assert check.status == CheckStatus.FAILED
    assert check.details["ocr_decimal_hint"] == 100
    assert "off by a factor of 100: possible ocr loss of decimal point" in check.message.lower()


def test_vendor_duplicate_soft_duplicate_and_missing_number_checks():
    assert vendor_identity_conflict(True).status == CheckStatus.FAILED
    assert missing_invoice_number(extraction(invoice_number=None)).recommended_action == (
        "Ask the vendor for an invoice number."
    )
    original = SimpleNamespace(
        invoice_id="invoice-1",
        invoice_date=None,
        current_decision=Decision.APPROVED,
        workflow_runs=[SimpleNamespace(run_id="run-1", started_at=1)],
    )
    duplicate = duplicate_business_key(original)
    assert duplicate.status == CheckStatus.FAILED
    assert duplicate.details["hard_block"] is True
    assert "run-1" in duplicate.message
    soft = soft_duplicate(
        SimpleNamespace(invoice_id="invoice-2", invoice_date=None),
        invoice_number="INV-2",
    )
    assert soft.status == CheckStatus.WARNING


def test_non_escalating_price_note_and_remaining_quantity_balance_details(db_session):
    seed_reference_data(db_session, ROOT / "data")
    po = db_session.get(PurchaseOrderORM, "POID-1002")
    invoice = extraction(
        "resubmitted_clean.pdf",
        line_items=[{
            "description": "Laptop Service",
            "quantity": "1",
            "unit_price": "10150.00",
            "line_total": "10150.00",
        }],
        subtotal="10150.00",
    )
    matches = match_invoice_lines(invoice, po)
    price_check = unit_price_variance(matches)
    assert price_check.status == CheckStatus.WARNING
    assert price_check.details["escalates"] is False
    assert decide([price_check]).decision == Decision.APPROVED

    quantity_check = quantity_vs_ordered(
        matches,
        {matches[0].po_line.id: Decimal("1")},
    )
    assert quantity_check.status == CheckStatus.FAILED
    unmatched = match_invoice_lines(
        extraction(
            "resubmitted_clean.pdf",
            line_items=[{
                "description": "Unrelated item",
                "quantity": "1",
                "unit_price": "10.00",
                "line_total": "10.00",
            }],
        ),
        po,
    )
    assert line_on_po(unmatched, po).status == CheckStatus.FAILED

    balance = po_balance(invoice, po, Decimal("65000"), previously_invoiced=Decimal("10000"))
    assert balance.details["po_net"] == "75000.00"
    assert balance.details["previously_invoiced"] == "10000"
    assert balance.details["this_invoice"] == "10150.00"
    assert balance.details["remaining_after"] == "54850.00"
    assert duplicate_business_key(None).status == CheckStatus.PASSED
    assert soft_duplicate(None, invoice_number=invoice.invoice_number).status == CheckStatus.PASSED
    assert vendor_identity_conflict(False).status == CheckStatus.PASSED
    assert missing_invoice_number(invoice).status == CheckStatus.PASSED
