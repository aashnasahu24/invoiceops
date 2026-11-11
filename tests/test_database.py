import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from invoiceops.db import create_db_engine, create_session_factory, init_db
from invoiceops.models import (
    InvoiceLineItemORM,
    InvoiceORM,
    POLineItemORM,
    PurchaseOrderORM,
    ReviewActionORM,
    ReviewActionType,
    ValidationCheckORM,
    VendorORM,
    WorkflowRunORM,
)
from invoiceops.schemas import CheckStatus, Decision, ProcessingStatus
from invoiceops.seed import seed_reference_data

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db_session(tmp_path):
    db_path = tmp_path / "test_invoiceops.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    init_db(engine)
    SessionLocal = create_session_factory(engine)
    with SessionLocal() as session:
        yield session
    engine.dispose()


def test_seed_reference_data_and_relationships(db_session):
    result = seed_reference_data(db_session, ROOT / "data")
    db_session.commit()

    assert result == {"vendors": 4, "purchase_orders": 5}
    assert db_session.scalar(select(func.count()).select_from(VendorORM)) == 4
    assert db_session.scalar(select(func.count()).select_from(PurchaseOrderORM)) == 5
    assert db_session.scalar(select(func.count()).select_from(POLineItemORM)) == 8

    vendor = db_session.get(VendorORM, "V001")
    assert vendor is not None
    assert "ABC Technologies Pvt Ltd" in vendor.aliases
    assert len(vendor.purchase_orders) == 2


def test_seed_is_idempotent(db_session):
    seed_reference_data(db_session, ROOT / "data")
    db_session.commit()
    seed_reference_data(db_session, ROOT / "data")
    db_session.commit()

    assert db_session.scalar(select(func.count()).select_from(VendorORM)) == 4
    assert db_session.scalar(select(func.count()).select_from(PurchaseOrderORM)) == 5
    assert db_session.scalar(select(func.count()).select_from(POLineItemORM)) == 8


def test_reseeding_preserves_matched_po_line_ids(db_session):
    seed_reference_data(db_session, ROOT / "data")
    po = db_session.get(PurchaseOrderORM, "POID-1002")
    po_line_id = po.line_items[0].id
    invoice = InvoiceORM(
        invoice_id="matched-line-invoice",
        vendor_id="V002",
        po_id="POID-1002",
        file_name="matched-line.pdf",
        file_sha256="e" * 64,
        extracted_data={},
        field_evidence={},
        extraction_warnings=[],
        current_decision=Decision.APPROVED,
    )
    invoice.line_items.append(
        InvoiceLineItemORM(
            line_number=1,
            description="Laptop",
            quantity=1,
            unit_price=15000,
            line_total=15000,
            matched_po_line_id=po_line_id,
        )
    )
    db_session.add(invoice)
    db_session.commit()

    seed_reference_data(db_session, ROOT / "data")
    db_session.commit()
    persisted = db_session.get(InvoiceORM, "matched-line-invoice")
    assert persisted.line_items[0].matched_po_line_id == po_line_id


def test_invoice_business_key_is_unique_per_vendor(db_session):
    seed_reference_data(db_session, ROOT / "data")
    db_session.add_all(
        [
            InvoiceORM(
                invoice_id="invoice-1",
                vendor_id="V001",
                invoice_number="INV-104",
                invoice_number_normalized="INV104",
                file_name="one.pdf",
                file_sha256="a" * 64,
                extracted_data={},
                field_evidence={},
                extraction_warnings=[],
            ),
            InvoiceORM(
                invoice_id="invoice-2",
                vendor_id="V001",
                invoice_number="INV 104",
                invoice_number_normalized="INV104",
                file_name="two.pdf",
                file_sha256="b" * 64,
                extracted_data={},
                field_evidence={},
                extraction_warnings=[],
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_workflow_checks_and_review_actions_persist(db_session):
    seed_reference_data(db_session, ROOT / "data")
    invoice = InvoiceORM(
        invoice_id="invoice-run-test",
        vendor_id="V001",
        po_id="POID-1001",
        invoice_number="INV-TEST-01",
        invoice_number_normalized="INVTEST01",
        file_name="test.pdf",
        extracted_data={"invoice_number": "INV-TEST-01"},
        field_evidence={"invoice_number": {"page": 1}},
        extraction_warnings=[],
    )
    run = WorkflowRunORM(
        run_id="run-test-1",
        invoice=invoice,
        source_file_name="test.pdf",
        source_file_sha256="c" * 64,
        processing_status=ProcessingStatus.COMPLETED,
        decision=Decision.MANUAL_REVIEW,
    )
    run.validation_checks.append(
        ValidationCheckORM(
            check_name="vendor_match",
            status=CheckStatus.PASSED,
            message="Vendor matched by tax ID.",
            details={"vendor_id": "V001"},
        )
    )
    run.review_actions.append(
        ReviewActionORM(
            action=ReviewActionType.REQUEST_INFORMATION,
            reviewer="AP Analyst",
            comments="Please confirm the missing PO reference.",
        )
    )
    db_session.add(run)
    db_session.commit()

    saved_run = db_session.get(WorkflowRunORM, "run-test-1")
    assert saved_run is not None
    assert saved_run.invoice.invoice_number == "INV-TEST-01"
    assert saved_run.validation_checks[0].status == CheckStatus.PASSED
    assert saved_run.review_actions[0].action == ReviewActionType.REQUEST_INFORMATION


def test_duplicate_upload_can_be_logged_without_new_invoice(db_session):
    seed_reference_data(db_session, ROOT / "data")
    original = InvoiceORM(
        invoice_id="invoice-original",
        vendor_id="V001",
        invoice_number="INV-104",
        invoice_number_normalized="INV104",
        file_name="original.pdf",
        file_sha256="d" * 64,
        extracted_data={},
        field_evidence={},
        extraction_warnings=[],
    )
    duplicate_attempt = WorkflowRunORM(
        run_id="run-duplicate-attempt",
        invoice=original,
        source_file_name="duplicate-copy.pdf",
        source_file_sha256="e" * 64,
        processing_status=ProcessingStatus.COMPLETED,
        decision=Decision.REJECTED,
    )
    db_session.add(duplicate_attempt)
    db_session.commit()

    assert db_session.scalar(select(func.count()).select_from(InvoiceORM)) == 1
    assert db_session.scalar(select(func.count()).select_from(WorkflowRunORM)) == 1


def test_missing_invoice_link_is_allowed_for_early_duplicate_detection(db_session):
    seed_reference_data(db_session, ROOT / "data")
    run = WorkflowRunORM(
        run_id="run-same-file-hash",
        invoice_id=None,
        source_file_name="same-file.pdf",
        source_file_sha256="f" * 64,
        processing_status=ProcessingStatus.COMPLETED,
        decision=Decision.REJECTED,
    )
    db_session.add(run)
    db_session.commit()
    assert db_session.get(WorkflowRunORM, "run-same-file-hash") is not None
