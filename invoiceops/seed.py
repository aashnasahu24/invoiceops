"""Idempotent loader for synthetic vendor and purchase-order reference data."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from .models import (
    InvoiceLineItemORM,
    InvoiceORM,
    POLineItemORM,
    PurchaseOrderORM,
    ReviewActionORM,
    RunStageORM,
    ValidationCheckORM,
    VendorORM,
    WorkflowRunORM,
)
from .repository import po_balance
from .rules import derive_po_status
from .schemas import POStatus, PurchaseOrder, Vendor


def seed_reference_data(session: Session, data_dir: str | Path) -> dict[str, int]:
    """Validate and upsert vendors and POs; caller controls transaction commit."""
    root = Path(data_dir)
    vendor_rows = json.loads((root / "vendors.json").read_text(encoding="utf-8"))
    po_rows = json.loads((root / "purchase_orders.json").read_text(encoding="utf-8"))

    vendors = [Vendor.model_validate(row) for row in vendor_rows]
    purchase_orders = [PurchaseOrder.model_validate(row) for row in po_rows]

    for vendor_data in vendors:
        row = session.get(VendorORM, vendor_data.vendor_id)
        if row is None:
            row = VendorORM(vendor_id=vendor_data.vendor_id)
            session.add(row)
        row.legal_name = vendor_data.legal_name
        row.aliases = vendor_data.aliases
        row.tax_id = vendor_data.tax_id
        row.country = vendor_data.country
        row.status = vendor_data.status

    # Ensure vendor rows exist before inserting PO foreign keys.
    session.flush()

    for po_data in purchase_orders:
        if session.get(VendorORM, po_data.vendor_id) is None:
            raise ValueError(
                f"PO {po_data.po_number} references unknown vendor {po_data.vendor_id}"
            )

        row = session.get(PurchaseOrderORM, po_data.po_id)
        if row is None:
            row = PurchaseOrderORM(po_id=po_data.po_id, status=po_data.status)
            session.add(row)
        row.po_number = po_data.po_number
        row.vendor_id = po_data.vendor_id
        row.currency = po_data.currency
        row.total_amount = po_data.total_amount
        row.amount_basis = po_data.amount_basis

        # Upsert by line number so normal app startup preserves IDs referenced by
        # previously approved invoice lines.
        existing_lines = {line.line_number: line for line in row.line_items}
        expected_line_numbers = set()
        for index, line in enumerate(po_data.line_items, start=1):
            expected_line_numbers.add(index)
            line_row = existing_lines.get(index)
            if line_row is None:
                line_row = POLineItemORM(line_number=index)
                row.line_items.append(line_row)
            line_row.description = line.description
            line_row.sku = line.sku
            line_row.quantity_ordered = line.quantity_ordered
            line_row.unit_price = line.unit_price
            line_row.line_total = line.line_total
        for line_number, line_row in existing_lines.items():
            if line_number not in expected_line_numbers:
                row.line_items.remove(line_row)

    session.flush()
    for po_data in purchase_orders:
        row = session.get(PurchaseOrderORM, po_data.po_id)
        remaining = po_balance(session, po_data.po_id)
        invoiced = Decimal(row.total_amount) - remaining
        row.status = derive_po_status(Decimal(row.total_amount), invoiced)
    return {"vendors": len(vendors), "purchase_orders": len(purchase_orders)}


def reset_demo_state(session: Session, data_dir: str | Path) -> dict[str, int]:
    """Wipe all processing history and restore the reference data.

    Duplicate detection and PO-balance results depend on prior persisted state
    (e.g. the duplicate fixture is only a duplicate *after* the happy-path
    invoice was processed). Call this between demo rehearsals and the real run.
    Reference data (vendors, POs) is re-seeded, which also resets PO statuses.
    Caller controls the transaction commit.
    """
    # Break the self-reference first so invoices can be deleted in one statement.
    session.execute(update(InvoiceORM).values(supersedes_invoice_id=None))
    deleted = {}
    for label, model in (
        ("review_actions", ReviewActionORM),
        ("validation_checks", ValidationCheckORM),
        ("run_stages", RunStageORM),
        ("workflow_runs", WorkflowRunORM),
        ("invoice_line_items", InvoiceLineItemORM),
        ("invoices", InvoiceORM),
    ):
        deleted[label] = session.execute(delete(model)).rowcount or 0
    session.flush()
    deleted.update(seed_reference_data(session, data_dir))
    seed_rows = json.loads((Path(data_dir) / "purchase_orders.json").read_text(encoding="utf-8"))
    for seed_row in seed_rows:
        po = session.get(PurchaseOrderORM, seed_row["po_id"])
        po.status = POStatus(seed_row["status"])
    return deleted
