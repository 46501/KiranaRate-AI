"""
tests/test_gst.py
=================
Unit and integration tests for Indian GST & Input Tax Credit (ITC) reconciliation.
"""

from datetime import date, datetime, timezone
import pytest
from fastapi.testclient import TestClient

from app.models import CostBasis, Product, ProductActive, Purchase, Supplier, SupplierActive
from app.services.tax_engine import TaxEngine
from app.storage.base import BaseStore

UTC = timezone.utc


def test_tax_engine_slab_estimation():
    assert TaxEngine.get_estimated_gst_rate("OIL-FORTUNE-1L", "Fortune Oil 1L") == 0.05
    assert TaxEngine.get_estimated_gst_rate("ATTA-AASHIRVAAD-5KG", "Aashirvaad Atta") == 0.05
    assert TaxEngine.get_estimated_gst_rate("BUTTER-AMUL-500G", "Amul Butter 500g") == 0.12
    assert TaxEngine.get_estimated_gst_rate("BISCUIT-PARLEG-10", "Parle-G Biscuit 10pk") == 0.18


def test_gst_itc_endpoint(client: TestClient, store: BaseStore):
    store.upsert_supplier(Supplier(supplier_id="SUP_GST_TEST", supplier_name="GST Test Wholesale", active=SupplierActive.YES))
    store.upsert_product(Product(sku="OIL-FORTUNE-1L", product_name="Fortune Sunlite Refined Oil 1L", active=ProductActive.YES))

    # Pre-seed purchase: Rs 1000.00 @ 5% GST = Rs 50.00 ITC (CGST Rs 25.00, SGST Rs 25.00)
    p = Purchase(
        purchase_id="purch_gst_01",
        bill_id="bill_gst_01",
        supplier_id="SUP_GST_TEST",
        sku="OIL-FORTUNE-1L",
        invoice_no="INV-GST-100",
        invoice_date=date(2026, 11, 20),
        cost_basis=CostBasis.EX_GST,
        units=10,
        net_paise=100000,  # Rs 1000.00
        posted_at=datetime(2026, 11, 20, 11, 0, tzinfo=UTC),
    )
    store.insert_purchase(p)

    resp = client.get("/analytics/gst-itc")
    assert resp.status_code == 200
    data = resp.json()

    assert "total_taxable_inr" in data
    assert "total_itc_inr" in data
    assert "by_supplier" in data
    assert "by_slab" in data

    # Verify supplier summary
    sup_entries = [s for s in data["by_supplier"] if s["supplier_id"] == "SUP_GST_TEST"]
    assert len(sup_entries) == 1
    s_data = sup_entries[0]
    assert s_data["taxable_amount_inr"] == 1000.0
    assert s_data["total_itc_inr"] == 50.0
    assert s_data["cgst_inr"] == 25.0
    assert s_data["sgst_inr"] == 25.0
    assert s_data["gross_amount_inr"] == 1050.0
