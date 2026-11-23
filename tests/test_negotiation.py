"""
tests/test_negotiation.py
=========================
Integration tests for WhatsApp Supplier Negotiation Note Generator.
"""

from datetime import date, datetime, timezone
import pytest
from fastapi.testclient import TestClient

from app.models import Alert, AlertStatus, Product, ProductActive
from app.services.negotiation_generator import NegotiationGenerator
from app.storage.base import BaseStore

UTC = timezone.utc


@pytest.fixture
def sample_alert(store: BaseStore) -> Alert:
    store.upsert_product(Product(sku="BISCUIT-PARLEG-10", product_name="Parle-G 10pk Biscuits", active=ProductActive.YES))
    alert = Alert(
        alert_id="alert_neg_test_01",
        bill_id="bill_test_high_01",
        sku="BISCUIT-PARLEG-10",
        low_supplier="Suresh Wholesale",
        low_rate=4600,  # Rs 46.00
        low_date=date(2026, 11, 18),
        low_bill="bill_low_01",
        high_supplier="Ramesh Traders",
        high_rate=5000,  # Rs 50.00
        high_date=date(2026, 11, 22),
        high_bill="bill_high_01",
        gap_pct=8.0,
        status=AlertStatus.PENDING,
        created_at=datetime(2026, 11, 22, 12, 0, tzinfo=UTC),
    )
    store.insert_alert(alert)
    return alert


def test_negotiation_generator_messages(sample_alert, store: BaseStore):
    note = NegotiationGenerator.generate_note(sample_alert, store)
    assert note.sku == "BISCUIT-PARLEG-10"
    assert note.high_supplier == "Ramesh Traders"
    assert note.low_supplier == "Suresh Wholesale"
    assert note.high_rate_inr == 50.0
    assert note.low_rate_inr == 46.0
    assert note.gap_diff_inr == 4.0
    assert note.gap_pct == 8.0

    # Message contents
    assert "Namaste Ramesh Traders" in note.english_message
    assert "50.00" in note.english_message
    assert "46.00" in note.english_message
    assert "8.00%" in note.english_message

    assert "Namaste Ramesh Traders ji" in note.hinglish_message
    assert "50.00" in note.hinglish_message
    assert "46.00" in note.hinglish_message

    # URLs
    assert note.whatsapp_url_en.startswith("https://api.whatsapp.com/send?text=")
    assert note.whatsapp_url_hi.startswith("https://api.whatsapp.com/send?text=")


def test_negotiation_endpoint_success(client: TestClient, sample_alert):
    resp = client.get(f"/alerts/{sample_alert.alert_id}/negotiation-note")
    assert resp.status_code == 200
    data = resp.json()
    assert data["alert_id"] == sample_alert.alert_id
    assert data["high_supplier"] == "Ramesh Traders"
    assert data["low_rate_inr"] == 46.0
    assert "english_message" in data
    assert "hinglish_message" in data
    assert "whatsapp_url_en" in data


def test_negotiation_endpoint_404_for_unknown_alert(client: TestClient):
    resp = client.get("/alerts/unknown_alert_id/negotiation-note")
    assert resp.status_code == 404
