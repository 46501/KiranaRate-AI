"""
tests/test_analytics.py
======================
Integration tests for SKU Price History & Volatility Analytics endpoints.
"""

from datetime import date, datetime, timezone
import pytest
from fastapi.testclient import TestClient

from app.models import CostBasis, Product, ProductActive, Purchase
from app.storage.base import BaseStore

UTC = timezone.utc


def test_get_sku_price_history_with_purchases(client: TestClient, store: BaseStore):
    # Ensure product exists
    store.upsert_product(
        Product(sku="ANALYTICS-TEST-SKU", product_name="Analytics Test Biscuit 100g", active=ProductActive.YES)
    )

    # Insert two purchases with different rates
    p1 = Purchase(
        purchase_id="purch_analytics_1",
        bill_id="bill_test_1",
        supplier_id="SUP_A",
        sku="ANALYTICS-TEST-SKU",
        invoice_no="INV-001",
        invoice_date=date(2026, 11, 10),
        cost_basis=CostBasis.EX_GST,
        units=10,
        net_paise=40000,  # Rs 40.00/unit
        posted_at=datetime(2026, 11, 10, 10, 0, tzinfo=UTC),
    )
    p2 = Purchase(
        purchase_id="purch_analytics_2",
        bill_id="bill_test_2",
        supplier_id="SUP_B",
        sku="ANALYTICS-TEST-SKU",
        invoice_no="INV-002",
        invoice_date=date(2026, 11, 15),
        cost_basis=CostBasis.EX_GST,
        units=10,
        net_paise=48000,  # Rs 48.00/unit
        posted_at=datetime(2026, 11, 15, 10, 0, tzinfo=UTC),
    )
    store.insert_purchase(p1)
    store.insert_purchase(p2)

    resp = client.get("/analytics/sku-history", params={"sku": "ANALYTICS-TEST-SKU"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["sku"] == "ANALYTICS-TEST-SKU"
    assert data["purchase_count"] == 2
    assert data["min_rate_inr"] == 40.0
    assert data["max_rate_inr"] == 48.0
    assert data["current_rate_inr"] == 48.0
    assert data["volatility_percent"] == 20.0  # (48 - 40) / 40 * 100
    assert len(data["history"]) == 2


def test_get_sku_price_history_unknown_sku(client: TestClient):
    resp = client.get("/analytics/sku-history", params={"sku": "NON-EXISTENT-SKU"})
    assert resp.status_code == 404


def test_get_sku_price_history_zero_purchases(client: TestClient, store: BaseStore):
    store.upsert_product(
        Product(sku="ZERO-PURCH-SKU", product_name="Zero Purchase Item", active=ProductActive.YES)
    )
    resp = client.get("/analytics/sku-history", params={"sku": "ZERO-PURCH-SKU"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["purchase_count"] == 0
    assert data["current_rate_inr"] is None
    assert data["history"] == []


def test_get_analytics_summary(client: TestClient):
    resp = client.get("/analytics/summary")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
