"""
tests/test_export.py
====================
Integration tests for Excel CSV and Tally Prime XML accounting exports.
"""

import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
import pytest
from fastapi.testclient import TestClient

from app.models import Bill, BillStatus, CostBasis, Product, ProductActive, Purchase, Supplier, SupplierActive
from app.storage.base import BaseStore

UTC = timezone.utc


@pytest.fixture
def seeded_export_store(store: BaseStore):
    store.upsert_supplier(Supplier(supplier_id="SUP_EXPORT", supplier_name="Export Wholesale Ltd", active=SupplierActive.YES))
    store.upsert_product(Product(sku="BISCUIT-PARLEG-10", product_name="Parle-G 10pk Biscuits", base_unit="PACK", active=ProductActive.YES))

    bill = Bill(
        bill_id="wamid.EXPORT_BILL_001",
        sender="919876543210",
        media_id="local:export.jpg",
        mime_type="image/jpeg",
        received_at=datetime(2026, 11, 20, 10, 0, tzinfo=UTC),
        status=BillStatus.POSTED,
        supplier_id="SUP_EXPORT",
        invoice_no="INV-EXP-999",
        invoice_date="2026-11-20",
        cost_basis=CostBasis.EX_GST,
        reviewer="manager_test",
    )
    store.insert_bill(bill)

    purchase = Purchase(
        purchase_id="purch_exp_001",
        bill_id="wamid.EXPORT_BILL_001",
        supplier_id="SUP_EXPORT",
        sku="BISCUIT-PARLEG-10",
        invoice_no="INV-EXP-999",
        invoice_date=date(2026, 11, 20),
        cost_basis=CostBasis.EX_GST,
        units=20,
        net_paise=100000,  # Rs 1000.00 (Rs 50.00/unit)
        posted_at=datetime(2026, 11, 20, 10, 30, tzinfo=UTC),
    )
    store.insert_purchase(purchase)
    return store


def test_export_csv_endpoint(client: TestClient, seeded_export_store):
    resp = client.get("/bills/export/csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "attachment" in resp.headers.get("content-disposition", "")

    content = resp.text
    assert "Invoice Date,Invoice No,Supplier ID,Supplier Name,Bill ID,SKU" in content
    assert "INV-EXP-999" in content
    assert "SUP_EXPORT" in content
    assert "BISCUIT-PARLEG-10" in content
    assert "50.00" in content
    assert "1000.00" in content

    # Test alias
    resp_alias = client.get("/export/csv")
    assert resp_alias.status_code == 200


def test_export_tally_xml_endpoint(client: TestClient, seeded_export_store):
    resp = client.get("/bills/export/tally")
    assert resp.status_code == 200
    assert "xml" in resp.headers["content-type"]
    assert "attachment" in resp.headers.get("content-disposition", "")

    xml_content = resp.text
    assert "<ENVELOPE>" in xml_content
    assert "<VOUCHER" in xml_content

    # Validate XML syntax
    root = ET.fromstring(xml_content)
    assert root.tag == "ENVELOPE"

    # Verify voucher contents
    vouchers = root.findall(".//VOUCHER")
    assert len(vouchers) >= 1

    voucher = vouchers[0]
    assert voucher.attrib.get("VCHTYPE") == "Purchase"
    assert voucher.find("VOUCHERNUMBER").text == "INV-EXP-999"
    assert voucher.find("PARTYLEDGERNAME").text == "Export Wholesale Ltd"

    # Verify inventory line item
    inv_item = voucher.find(".//ALLINVENTORYENTRIES.LIST")
    assert inv_item is not None
    assert inv_item.find("STOCKITEMNAME").text == "Parle-G 10pk Biscuits"
    assert "20 PACK" in inv_item.find("ACTUALQTY").text

    # Test alias
    resp_alias = client.get("/export/tally")
    assert resp_alias.status_code == 200
