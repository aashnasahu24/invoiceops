import json
from pathlib import Path

import pytest
from sqlalchemy import select

from invoiceops.db import create_db_engine, create_session_factory, init_db
from invoiceops.models import InvoiceORM, PurchaseOrderORM, ReviewActionORM, VendorORM, WorkflowRunORM
from invoiceops.orchestrator import process_invoice
from invoiceops.reviewer import apply_review_action, list_review_queue
from invoiceops.schemas import Decision, POStatus, ReviewActionType
from invoiceops.seed import seed_reference_data

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "sample_invoices"
LABELS = json.loads((ROOT / "data" / "expected_extractions.json").read_text(encoding="utf-8"))


class FixtureProvider:
    def __init__(self, filename, *, po_override="default"):
        label = LABELS[filename]
        self.response = {
            "invoice_number": label.get("invoice_number"),
            "invoice_date": label.get("invoice_date"),
            "due_date": label.get("due_date"),
            "vendor_name": label.get("vendor_name"),
            "vendor_tax_id": label.get("vendor_tax_id"),
            "po_number": label.get("po_number") if po_override == "default" else None,
            "currency": label.get("currency"),
            "subtotal": label.get("subtotal"),
            "tax_total": label.get("tax_total"),
            "shipping": label.get("shipping"),
            "discount": label.get("discount"),
            "grand_total": label.get("grand_total"),
            "line_items": [
                {
                    "description": line["description"],
                    "sku": line.get("sku"),
                    "quantity": line["quantity"],
                    "unit_price": line["unit_price"],
                    "line_total": line["line_total"],
                    "tax_rate": line.get("tax_rate"),
                }
                for line in label["line_items"]
            ],
            "evidence": [],
        }

    def generate_structured_output(self, **_kwargs):
        return self.response


@pytest.fixture
def factory(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'review.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        seed_reference_data(session, ROOT / "data")
        session.commit()
    yield session_factory
    engine.dispose()


def _process(factory, provider, filename):
    result = list(process_invoice(
        factory,
        provider,
        (SAMPLES / filename).read_bytes(),
        filename,
    ))[-1]
    with factory() as session:
        invoice = session.scalar(select(InvoiceORM).where(InvoiceORM.file_name == filename))
        invoice_id = invoice.invoice_id if invoice else None
    return result, invoice_id


def test_request_information_deactivates_and_saves_email_and_resubmission_link(factory):
    provider = FixtureProvider("happy_path_invoice.pdf", po_override=None)
    first_result, first_id = _process(factory, provider, "happy_path_invoice.pdf")
    assert first_result.decision == Decision.MANUAL_REVIEW
    assert first_id is not None
    with factory() as session:
        assert [row.invoice_id for row in list_review_queue(session)] == [first_id]
        with pytest.raises(ValueError, match="explanation is required"):
            apply_review_action(
                session, first_id, ReviewActionType.REQUEST_INFORMATION, reviewer="AP reviewer"
            )
        outcome = apply_review_action(
            session,
            first_id,
            ReviewActionType.REQUEST_INFORMATION,
            reviewer="AP reviewer",
            comment="Please include the purchase order number.",
        )
        session.commit()
        original = session.get(InvoiceORM, first_id)
        action = session.scalar(select(ReviewActionORM))
        assert original.is_active is False
        assert action.reviewer == "AP reviewer"
        assert "Subject: Information required" in action.generated_email
        assert outcome.generated_email == action.generated_email

    corrected = FixtureProvider("happy_path_invoice.pdf")
    second_result, second_id = _process(factory, corrected, "duplicate_invoice.pdf")
    assert second_result.decision == Decision.APPROVED
    with factory() as session:
        second = session.get(InvoiceORM, second_id)
        assert second.supersedes_invoice_id == first_id


def test_failed_checks_need_comment_to_override_and_hard_block_stays_blocked(factory):
    _, invoice_id = _process(factory, FixtureProvider("po_overrun_invoice.pdf"), "po_overrun_invoice.pdf")
    assert invoice_id is not None
    with factory() as session:
        with pytest.raises(ValueError, match="comment is required"):
            apply_review_action(
                session,
                invoice_id,
                ReviewActionType.APPROVE,
                reviewer="AP reviewer",
            )
        outcome = apply_review_action(
            session,
            invoice_id,
            ReviewActionType.APPROVE,
            reviewer="AP reviewer",
            comment="Approved per documented finance exception.",
        )
        session.commit()
        assert outcome.decision == Decision.APPROVED
        assert session.get(InvoiceORM, invoice_id).current_decision == Decision.APPROVED
        action = session.scalar(select(ReviewActionORM).order_by(ReviewActionORM.id.desc()))
        assert action.reviewer == "AP reviewer"
        assert action.created_at is not None
        assert action.overridden_checks
        assert {row["check_name"] for row in action.overridden_checks} >= {"po_balance"}
        session.expire_all()
        reloaded = session.scalar(select(ReviewActionORM).where(ReviewActionORM.id == action.id))
        assert reloaded.overridden_checks[0]["original_status"] == "FAILED"
        run = session.scalar(select(WorkflowRunORM).where(WorkflowRunORM.run_id == action.run_id))
        original = {row.check_name: row for row in run.validation_checks}
        assert original["po_balance"].status.value == "FAILED"


def test_reject_requires_reason_and_closed_po_cannot_be_overridden(factory):
    _, invoice_id = _process(
        factory,
        FixtureProvider("happy_path_invoice.pdf", po_override=None),
        "happy_path_invoice.pdf",
    )
    with factory() as session:
        with pytest.raises(ValueError, match="rejection reason is required"):
            apply_review_action(
                session,
                invoice_id,
                ReviewActionType.REJECT,
                reviewer="AP reviewer",
            )
        outcome = apply_review_action(
            session,
            invoice_id,
            ReviewActionType.REJECT,
            reviewer="AP reviewer",
            comment="No valid PO was provided.",
        )
        session.commit()
        assert outcome.decision == Decision.REJECTED

    with factory() as session:
        po = session.get(PurchaseOrderORM, "POID-1001")
        po.status = POStatus.CLOSED
        session.commit()
    closed_po_provider = FixtureProvider("happy_path_invoice.pdf")
    closed_po_provider.response["invoice_number"] = "INV-CLOSED"
    _, second_id = _process(factory, closed_po_provider, "duplicate_invoice.pdf")
    with factory() as session:
        with pytest.raises(ValueError, match="Approval blocked"):
            apply_review_action(
                session,
                second_id,
                ReviewActionType.APPROVE,
                reviewer="AP reviewer",
                comment="Attempted override",
            )
