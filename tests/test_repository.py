import hashlib
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from invoiceops.matching import LineMatch
from invoiceops.models import InvoiceORM, PurchaseOrderORM, RunStageORM
from invoiceops.repository import (
    approved_po_quantities,
    create_run,
    find_soft_duplicate,
    po_balance,
    record_stage,
    save_invoice,
)
from invoiceops.schemas import (
    Decision,
    DocumentType,
    ExtractionMetadata,
    InvoiceExtraction,
    StageEvent,
    StageName,
    StageStatus,
)
from invoiceops.seed import seed_reference_data


def test_po_balance_subtracts_zero_one_and_two_approved_invoices(db_session):
    from pathlib import Path

    seed_reference_data(db_session, Path(__file__).resolve().parents[1] / "data")
    assert po_balance(db_session, "POID-1001") == Decimal("50000.00")

    run = create_run(db_session, file_name="balance.pdf")
    db_session.commit()
    extraction = InvoiceExtraction(
        invoice_number="BAL-1",
        subtotal=Decimal("12500"),
        grand_total=Decimal("14750"),
        line_items=[],
        extraction=ExtractionMetadata(document_type=DocumentType.DIGITAL_PDF, page_count=1),
    )
    save_invoice(
        db_session,
        run_id=run.run_id,
        file_name="balance-1.pdf",
        file_sha256="1" * 64,
        extraction=extraction,
        decision=Decision.APPROVED,
        vendor_id="V001",
        po_id="POID-1001",
    )
    db_session.commit()
    assert po_balance(db_session, "POID-1001") == Decimal("37500.00")

    run2 = create_run(db_session, file_name="balance-2.pdf")
    db_session.commit()
    extraction.invoice_number = "BAL-2"
    save_invoice(
        db_session,
        run_id=run2.run_id,
        file_name="balance-2.pdf",
        file_sha256="2" * 64,
        extraction=extraction.model_copy(update={"subtotal": Decimal("7000")}),
        decision=Decision.APPROVED,
        vendor_id="V001",
        po_id="POID-1001",
    )
    # A rejected invoice never consumes PO balance.
    run3 = create_run(db_session, file_name="rejected.pdf")
    db_session.commit()
    save_invoice(
        db_session,
        run_id=run3.run_id,
        file_name="rejected.pdf",
        file_sha256="3" * 64,
        extraction=extraction.model_copy(
            update={"invoice_number": "BAL-3", "subtotal": Decimal("9000")}
        ),
        decision=Decision.REJECTED,
        vendor_id="V001",
        po_id="POID-1001",
    )
    db_session.commit()
    assert po_balance(db_session, "POID-1001") == Decimal("30500.00")


def test_record_stage_upserts_running_status(db_session):
    from pathlib import Path

    seed_reference_data(db_session, Path(__file__).resolve().parents[1] / "data")
    run = create_run(db_session, file_name="stage.pdf")
    running = StageEvent(stage=StageName.RECEIVED, status=StageStatus.RUNNING, message="Started")
    completed = StageEvent(stage=StageName.RECEIVED, status=StageStatus.COMPLETED, message="Done")
    record_stage(db_session, run.run_id, running)
    record_stage(db_session, run.run_id, completed)
    db_session.commit()

    rows = db_session.scalars(
        select(RunStageORM).where(RunStageORM.run_id == run.run_id)
    ).all()
    assert len(rows) == 1
    assert rows[0].status == StageStatus.COMPLETED
    assert rows[0].started_at is not None
    assert rows[0].completed_at is not None


def test_approved_line_quantities_and_soft_duplicate_lookup(db_session):
    from pathlib import Path

    seed_reference_data(db_session, Path(__file__).resolve().parents[1] / "data")
    po = db_session.get(PurchaseOrderORM, "POID-1003")
    po_line = po.line_items[0]
    for number, quantity in (("SPLIT-A", "4"), ("SPLIT-B", "3")):
        qty = Decimal(quantity)
        extraction = InvoiceExtraction(
            invoice_number=number,
            invoice_date=date(2026, 10, 22),
            vendor_name="ABC Technologies",
            po_number="PO-1003",
            subtotal=qty * Decimal("10000"),
            tax_total=qty * Decimal("1800"),
            grand_total=qty * Decimal("11800"),
            line_items=[{
                "description": "Network Equipment",
                "quantity": qty,
                "unit_price": Decimal("10000"),
                "line_total": qty * Decimal("10000"),
            }],
            extraction=ExtractionMetadata(
                document_type=DocumentType.DIGITAL_PDF,
                page_count=1,
            ),
        )
        run = create_run(db_session, file_name=f"{number}.pdf")
        db_session.flush()
        save_invoice(
            db_session,
            run_id=run.run_id,
            file_name=f"{number}.pdf",
            file_sha256=hashlib.sha256(number.encode()).hexdigest(),
            extraction=extraction,
            decision=Decision.APPROVED,
            vendor_id="V001",
            po_id="POID-1003",
            line_matches=(LineMatch(extraction.line_items[0], po_line, 100.0),),
        )
        db_session.flush()

    assert approved_po_quantities(db_session, "POID-1003") == {
        po_line.id: Decimal("7.000")
    }
    candidate = find_soft_duplicate(
        db_session,
        vendor_id="V001",
        net_amount=Decimal("40000"),
        invoice_date=date(2026, 10, 25),
        normalized_number="DIFFERENT",
    )
    assert candidate is not None
    assert candidate.invoice_number == "SPLIT-A"
    candidate.is_active = False
    db_session.flush()
    assert find_soft_duplicate(
        db_session,
        vendor_id="V001",
        net_amount=Decimal("40000"),
        invoice_date=date(2026, 10, 25),
        normalized_number="DIFFERENT",
    ) is None
