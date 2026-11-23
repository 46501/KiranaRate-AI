"""
tests/test_quick_approve.py
===========================
Integration tests for 1-Click Quick Approval workflow.
"""

from decimal import Decimal
import pytest
from fastapi.testclient import TestClient

from app.models import BillStatus, HeaderVerified, Product, ProductActive, Reviewed, Supplier, SupplierActive
from app.storage.base import BaseStore


def test_quick_approve_endpoint_success(client: TestClient, make_bill, make_review_line, store: BaseStore):
    store.upsert_supplier(Supplier(supplier_id="RAMESH_TRADERS", supplier_name="Ramesh Traders", active=SupplierActive.YES))
    store.upsert_product(Product(sku="BISCUIT-PARLEG-10", product_name="Parle-G 10pk Biscuits", active=ProductActive.YES))

    bill = make_bill(
        bill_id="bill_quick_app_01",
        status=BillStatus.REVIEW,
        supplier_raw="Ramesh Traders",
        invoice_total="500.00",
    )
    ln = make_review_line(
        bill_id=bill.bill_id,
        line_no=1,
        product_raw="Parle-G 10pk Biscuit",
        rate_raw="50.00",
        quantity_raw="10",
        line_total_raw="500.00",
        net_amount=None,
    )

    resp = client.post(f"/bills/{bill.bill_id}/quick-approve")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "APPROVED"
    assert data["supplier_id"] in ("RAMESH_TRADERS", "SUP001")
    assert data["header_verified"] == "YES"
    assert data["reviewer"] == "quick_approver"
    assert len(data["lines"]) == 1
    assert data["lines"][0]["sku"] == "BISCUIT-PARLEG-10"
    assert data["lines"][0]["reviewed"] == "YES"
    assert data["lines"][0]["received_units"] == 10
    assert str(data["lines"][0]["net_amount"]) == "500.00"


def test_quick_approve_rejects_non_review_state(client: TestClient, make_bill):
    bill = make_bill(bill_id="bill_quick_queued", status=BillStatus.QUEUED)
    resp = client.post(f"/bills/{bill.bill_id}/quick-approve")
    assert resp.status_code == 400


def test_quick_approve_rejects_zero_lines(client: TestClient, make_bill):
    bill = make_bill(bill_id="bill_quick_empty", status=BillStatus.REVIEW)
    resp = client.post(f"/bills/{bill.bill_id}/quick-approve")
    assert resp.status_code == 400
