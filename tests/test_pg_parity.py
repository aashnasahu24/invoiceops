"""Run the DB-sensitive checks against real PostgreSQL when PG_TEST_URI is set.

SQLite is only a stand-in for unit tests: it ignores row locks, stores NUMERIC as
float, and has different partial-index/enum behaviour. Run this file against
Postgres before deploying (see README). Skipped when PG_TEST_URI is unset.
"""

import os
import re
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import pymupdf as fitz
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from invoiceops.config import get_settings, normalize_database_url
from invoiceops.db import create_db_engine, create_session_factory, init_db
from invoiceops.models import Base, InvoiceORM, PurchaseOrderORM, ReviewActionORM, WorkflowRunORM
from invoiceops.orchestrator import process_invoice
from invoiceops.reviewer import apply_review_action
from invoiceops.schemas import Decision, ProcessingStatus, ReviewActionType
from invoiceops.seed import reset_demo_state, seed_reference_data

ROOT = Path(__file__).resolve().parents[1]
# Reuse the application's local .env loading so PG_TEST_URI works when configured
# there, without changing DATABASE_URL or falling back to it as the test target.
get_settings()
PG_URI = os.getenv("PG_TEST_URI")
pytestmark = pytest.mark.skipif(not PG_URI, reason="set PG_TEST_URI to run PostgreSQL parity tests")


@pytest.fixture()
def pg():
    # Fail closed before any DROP TABLE if the target is not clearly a
    # disposable test database or aliases the application's configured DB.
    test_url = make_url(normalize_database_url(PG_URI))
    if test_url.get_backend_name() != "postgresql":
        pytest.fail("PG_TEST_URI must be a PostgreSQL URL; DATABASE_URL is never used as fallback.", pytrace=False)
    if not test_url.database or not re.search(r"(?:^|[_-])test(?:$|[_-])", test_url.database.lower()):
        pytest.fail(
            "PG_TEST_URI must target an explicitly test-named database (for example invoiceops_test).",
            pytrace=False,
        )
    app_url = make_url(get_settings().database_url)
    app_host = (app_url.host or "").lower().replace("-pooler.", ".")
    test_host = (test_url.host or "").lower().replace("-pooler.", ".")
    same_database = (
        app_url.get_backend_name() == test_url.get_backend_name()
        and app_host == test_host
        and (app_url.port or 5432) == (test_url.port or 5432)
        and app_url.database == test_url.database
    )
    if same_database:
        pytest.fail("PG_TEST_URI resolves to the same host/database as DATABASE_URL; refusing destructive tests.", pytrace=False)

    engine = create_db_engine(normalize_database_url(PG_URI))
    assert engine.dialect.name == "postgresql" and engine.dialect.driver == "psycopg"
    try:
        Base.metadata.drop_all(engine)
        init_db(engine)
        yield engine, create_session_factory(engine)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def inv(i, number, sha, **kw):
    return InvoiceORM(invoice_id=i, vendor_id="V001", invoice_number=number, invoice_number_normalized=number,
                      file_name=f"{i}.pdf", file_sha256=sha * 64, extracted_data={}, field_evidence={},
                      extraction_warnings=[], **kw)


def test_seed_is_idempotent_and_reset_works(pg):
    _, Session = pg
    with Session.begin() as s:
        seed_reference_data(s, ROOT / "data")
        seed_reference_data(s, ROOT / "data")
    with Session() as s:
        assert s.scalar(select(func.count()).select_from(PurchaseOrderORM)) == 5
        s.add(inv("a", "INV1", "a")); s.commit()
        counts = reset_demo_state(s, ROOT / "data"); s.commit()
        assert counts["invoices"] == 1 and s.scalar(select(func.count()).select_from(InvoiceORM)) == 0


def test_partial_unique_index_on_postgres(pg):
    _, Session = pg
    with Session.begin() as s:
        seed_reference_data(s, ROOT / "data")
        s.add(inv("old", "INV1", "a", is_active=False))
    with Session.begin() as s:
        s.add(inv("new", "INV1", "b"))            # allowed: old is inactive
    with pytest.raises(IntegrityError):
        with Session.begin() as s:
            s.add(inv("dup", "INV1", "c"))        # blocked: two active


def test_numeric_is_exact_and_timestamps_are_aware(pg):
    _, Session = pg
    with Session.begin() as s:
        seed_reference_data(s, ROOT / "data")
        s.add(inv("m", "INV2", "d", net_amount=Decimal("33333.33")))
        s.add(WorkflowRunORM(run_id="r", source_file_name="a.pdf", processing_status=ProcessingStatus.COMPLETED,
                             started_at=datetime.now(timezone.utc) - timedelta(seconds=3)))
    with Session() as s:
        assert s.get(InvoiceORM, "m").net_amount == Decimal("33333.33")
        run = s.get(WorkflowRunORM, "r")
        assert run.started_at.tzinfo is not None
        assert s.scalar(text("SELECT SUM(net_amount) FROM invoices")) == Decimal("33333.33")  # exact in SQL too


def test_po_row_lock_serialises_concurrent_balance_checks(pg):
    """The mechanism the plan relies on: SELECT ... FOR UPDATE on the PO row makes two
    simultaneous approvals take turns (impossible to verify on SQLite, which ignores it)."""
    _, Session = pg
    with Session.begin() as s:
        seed_reference_data(s, ROOT / "data")
    order: list[str] = []
    first_has_lock, release_first = threading.Event(), threading.Event()

    def worker(name, wait_for_release):
        with Session.begin() as s:
            s.execute(select(PurchaseOrderORM).where(PurchaseOrderORM.po_number == "PO-1003").with_for_update()).scalar_one()
            order.append(f"{name}:locked")
            if wait_for_release:
                first_has_lock.set(); release_first.wait(5)
            order.append(f"{name}:done")

    t1 = threading.Thread(target=worker, args=("A", True)); t1.start(); first_has_lock.wait(5)
    t2 = threading.Thread(target=worker, args=("B", False)); t2.start()
    t2.join(0.5)
    assert t2.is_alive()  # B is blocked while A holds the lock
    release_first.set(); t1.join(5); t2.join(5)
    assert order == ["A:locked", "A:done", "B:locked", "B:done"]


def test_concurrent_invoice_approvals_never_overrun_a_po(pg):
    """Two 60k invoices race against a 100k PO; only one may be approved."""
    _, Session = pg
    with Session.begin() as s:
        seed_reference_data(s, ROOT / "data")

    def make_pdf(number):
        text_content = (
            "TAX INVOICE\nVendor: ABC Technologies Pvt Ltd\n"
            "GSTIN: 29ABCDE1234F1Z5\n"
            f"Invoice Number: {number}\nInvoice Date: 2026-10-25\n"
            "PO Reference: PO-1003\nCurrency: INR\n"
            "Description | Qty | Unit Price | Line Total\n"
            "Network Equipment | 6 | 10000.00 | 60000.00\n"
            "Subtotal: 60000.00\nGST: 10800.00\nShipping: 0.00\n"
            "Discount: 0.00\nGrand Total: 70800.00"
        )
        document = fitz.open()
        page = document.new_page()
        page.insert_text((40, 50), text_content)
        return document.tobytes()

    barrier = threading.Barrier(2)

    class ConcurrentProvider:
        def generate_structured_output(self, *, document_text, **_kwargs):
            number = "CONC-A" if "CONC-A" in document_text else "CONC-B"
            barrier.wait(timeout=15)
            return {
                "invoice_number": number,
                "invoice_date": "2026-10-25",
                "due_date": None,
                "vendor_name": "ABC Technologies Pvt Ltd",
                "vendor_tax_id": "29ABCDE1234F1Z5",
                "po_number": "PO-1003",
                "currency": "INR",
                "line_items": [{
                    "description": "Network Equipment",
                    "sku": None,
                    "quantity": "6",
                    "unit_price": "10000.00",
                    "line_total": "60000.00",
                    "tax_rate": None,
                }],
                "subtotal": "60000.00",
                "tax_total": "10800.00",
                "shipping": "0.00",
                "discount": "0.00",
                "grand_total": "70800.00",
                "evidence": [],
            }

    provider = ConcurrentProvider()
    pdfs = [("CONC-A.pdf", make_pdf("CONC-A")), ("CONC-B.pdf", make_pdf("CONC-B"))]
    results = []
    errors = []

    def worker(file_name, pdf_bytes):
        try:
            results.append(list(process_invoice(Session, provider, pdf_bytes, file_name))[-1])
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=entry)
        for entry in pdfs
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert sorted(result.decision.value for result in results) == ["APPROVED", "MANUAL_REVIEW"]

    with Session() as s:
        approved_total = sum(
            amount or Decimal("0")
            for amount in s.scalars(
                select(InvoiceORM.net_amount).where(
                    InvoiceORM.po_id == "POID-1003",
                    InvoiceORM.current_decision == Decision.APPROVED,
                )
            ).all()
        )
        assert approved_total <= Decimal("100000.00")


def test_concurrent_reviewer_actions_are_serialized(pg):
    """Only the first terminal reviewer action may transition a manual-review invoice."""
    _, Session = pg
    invoice = inv("review-race", "REVIEW-RACE", "f", current_decision=Decision.MANUAL_REVIEW)
    run = WorkflowRunORM(
        run_id="review-race-run",
        source_file_name="review-race.pdf",
        source_file_sha256="f" * 64,
        processing_status=ProcessingStatus.COMPLETED,
        decision=Decision.MANUAL_REVIEW,
        invoice=invoice,
    )
    with Session.begin() as session:
        seed_reference_data(session, ROOT / "data")
        session.add(invoice)
        session.add(run)

    gate = threading.Barrier(2)
    outcomes: list[str] = []
    errors: list[Exception] = []

    def reject(reviewer: str) -> None:
        try:
            with Session.begin() as session:
                gate.wait(timeout=10)
                apply_review_action(
                    session,
                    "review-race",
                    ReviewActionType.REJECT,
                    reviewer=reviewer,
                    comment="Final rejection by reviewer.",
                )
            outcomes.append("applied")
        except ValueError:
            outcomes.append("transition-rejected")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=reject, args=(name,)) for name in ("Reviewer A", "Reviewer B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)

    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert sorted(outcomes) == ["applied", "transition-rejected"]
    with Session() as session:
        saved = session.get(InvoiceORM, "review-race")
        assert saved.current_decision == Decision.REJECTED
        actions = session.scalars(select(ReviewActionORM)).all()
        assert len(actions) == 1
