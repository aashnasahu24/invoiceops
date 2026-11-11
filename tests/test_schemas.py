import json
from pathlib import Path

from invoiceops.schemas import PurchaseOrder, Vendor

ROOT = Path(__file__).resolve().parents[1]


def test_seed_vendors_validate():
    rows = json.loads((ROOT / "data" / "vendors.json").read_text())
    vendors = [Vendor.model_validate(row) for row in rows]
    assert len(vendors) == 4
    assert {v.vendor_id for v in vendors} == {"V001", "V002", "V003", "V004"}


def test_seed_purchase_orders_validate():
    rows = json.loads((ROOT / "data" / "purchase_orders.json").read_text())
    orders = [PurchaseOrder.model_validate(row) for row in rows]
    assert len(orders) == 5
    assert {po.po_number for po in orders} == {"PO-1001", "PO-1002", "PO-1003", "PO-1004", "PO-1005"}


def test_sample_invoice_pdfs_exist():
    manifest = json.loads((ROOT / "data" / "test_manifest.json").read_text())
    for case in manifest["cases"]:
        path = ROOT / "data" / "sample_invoices" / case["file"]
        assert path.exists(), f"Missing fixture: {path.name}"
        assert path.read_bytes().startswith(b"%PDF-")
