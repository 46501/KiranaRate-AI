"""
tests/test_real_flow_e2e.py
===========================
End-to-end integration tests for real-flow pipeline verification:
  1. WhatsApp Webhook Intake (POST) -> QUEUED
  2. Gemini AI Scan Worker -> REVIEW
  3. Reviewer Verification & Approval (API/UI) -> APPROVED
  4. Verify & Post Worker (Ledger + 30-day Price Comparison) -> POSTED + PENDING Alert
  5. Price Alert Outbox Worker (10-param WhatsApp template) -> SENT with wamid
  6. Outbox Failure Resilience: network timeout/500 leaves status as SENDING without retry spam.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import pytest
from starlette.testclient import TestClient
import httpx

from app.models import (
    Alert,
    AlertStatus,
    BillStatus,
    CostBasis,
    HeaderVerified,
    Product,
    ProductActive,
    Purchase,
    Reviewed,
    Supplier,
    SupplierActive,
)
from app.services.whatsapp import MockWhatsAppClient
from app.services.workflow_engine import WorkflowEngine
from app.storage.sqlite_store import SQLiteStore

UTC = timezone.utc


@pytest.mark.asyncio
async def test_full_pipeline_webhook_to_alert_sent(
    client: TestClient,
    store: SQLiteStore,
    engine: WorkflowEngine,
    sample_webhook_factory,
    mock_whatsapp: MockWhatsAppClient,
):
    """
    Test the entire autonomous pipeline from inbound WhatsApp message to outbound price alert.
    """
    # 0. Setup store master catalog
    store.upsert_supplier(Supplier(supplier_id="SUP001", supplier_name="Ramesh Traders", active=SupplierActive.YES))
    store.upsert_supplier(Supplier(supplier_id="SUP002", supplier_name="Suresh Wholesale", active=SupplierActive.YES))
    store.upsert_product(Product(sku="BISCUIT-PARLEG-10", product_name="Parle-G Biscuit 10pk", base_unit="PACK", active=ProductActive.YES))
    store.upsert_product(Product(sku="SNACK-LAYS-26", product_name="Lays Chips 26g", base_unit="PACK", active=ProductActive.YES))

    # Pre-seed a cheaper purchase 4 days ago from Suresh Wholesale: 10 units @ 4200 paise (Rs 42.00/unit)
    hist_date = date.today() - timedelta(days=4)
    store.insert_purchase(
        Purchase(
            purchase_id="purch_hist_e2e",
            bill_id="bill_hist_e2e",
            sku="BISCUIT-PARLEG-10",
            supplier_id="SUP002",
            invoice_no="INV-PREV-42",
            invoice_date=hist_date,
            cost_basis=CostBasis.EX_GST,
            units=10,
            net_paise=42000,
            posted_at=datetime.now(UTC) - timedelta(days=4),
        )
    )

    # 1. Pipeline 1: Inbound Webhook POST
    bill_id = "wamid.E2E_REAL_TEST_001"
    payload = sample_webhook_factory(message_id=bill_id, sender="919876543210")
    resp_webhook = client.post("/webhook/whatsapp", json=payload)
    assert resp_webhook.status_code == 200
    data = resp_webhook.json()
    assert data["status"] == "ok"
    assert data["inserted_count"] == 1
    assert bill_id in data["bill_ids"]

    # Verify Bill persisted as QUEUED
    queued_bill = store.get_bill(bill_id)
    assert queued_bill is not None
    assert queued_bill.status == BillStatus.QUEUED

    # 2. Pipeline 2: AI Scan Worker
    processed_scan = await engine.run_scan_worker()
    assert processed_scan == bill_id

    review_bill = store.get_bill(bill_id)
    assert review_bill.status == BillStatus.REVIEW
    assert review_bill.supplier_raw is not None

    review_lines = store.get_review_lines(bill_id)
    assert len(review_lines) == 2

    # 3. Human Review & Verification (Approve all lines)
    approval_payload = {
        "reviewer": "store_manager",
        "supplier_id": "SUP001",
        "cost_basis": "EX_GST",
        "verified_net_total": "500.00",
        "header_verified": "YES",
        "invoice_date": date.today().isoformat(),
        "invoice_no": "INV-E2E-2026",
        "currency": "INR",
        "line_updates": [
            {
                "review_id": review_lines[0].review_id,
                "sku": "BISCUIT-PARLEG-10",
                "received_units": 10,
                "net_amount": "460.00",
                "reviewed": "YES",
            },
            {
                "review_id": review_lines[1].review_id,
                "sku": "SNACK-LAYS-26",
                "received_units": 2,
                "net_amount": "40.00",
                "reviewed": "YES",
            },
        ],
    }
    resp_approve = client.post(f"/bills/{bill_id}/approve", json=approval_payload)
    assert resp_approve.status_code == 200

    approved_bill = store.get_bill(bill_id)
    assert approved_bill.status == BillStatus.APPROVED

    # 4. Pipeline 3: Verify & Post Worker (Ledger + Rate Comparison)
    processed_post = await engine.run_post_worker()
    assert processed_post == bill_id

    posted_bill = store.get_bill(bill_id)
    assert posted_bill.status == BillStatus.POSTED

    # Rate comparison generated an alert: 4600 paise vs 4200 paise -> 400 paise gap (8.70% >= 2.00%)
    alert = store.get_oldest_pending_alert()
    assert alert is not None
    assert alert.bill_id == bill_id
    assert alert.sku == "BISCUIT-PARLEG-10"
    assert alert.low_supplier == "SUP002"
    assert alert.high_supplier == "SUP001"
    assert alert.gap_pct == pytest.approx(8.695, rel=1e-2)
    assert alert.status == AlertStatus.PENDING

    # 5. Pipeline 4: Price Alert Outbox Worker
    processed_alert = await engine.run_alert_worker()
    assert processed_alert == alert.alert_id

    # Verify WhatsApp message dispatch
    assert len(mock_whatsapp.sent_alerts) == 1
    sent_alert = mock_whatsapp.sent_alerts[0]
    assert sent_alert.to_number == "919999999999"
    assert sent_alert.template_name == "purchase_rate_review"
    assert len(sent_alert.params) == 10
    assert sent_alert.params[0] == "BISCUIT-PARLEG-10"
    assert "Rs 42.00/unit" in sent_alert.params[2]
    assert "Rs 46.00/unit" in sent_alert.params[6]

    # Verify Alert status in store transitioned to SENT with wamid
    db_alert = store.get_alert(alert.alert_id)
    assert db_alert.status == AlertStatus.SENT
    assert db_alert.wamid == sent_alert.wamid
    assert db_alert.sent_at is not None


@pytest.mark.asyncio
async def test_alert_outbox_failure_leaves_status_sending_no_spam(
    store: SQLiteStore,
    engine: WorkflowEngine,
    monkeypatch,
):
    """
    When WhatsApp API dispatch fails or times out, alert status MUST remain SENDING
    and never blindly retry or double-send to prevent duplicate spam to store owner.
    """
    store.upsert_product(Product(sku="TEA-TAZA-250", product_name="Brooke Bond Taaza Tea 250g", active=ProductActive.YES))
    from app.models import Bill
    store.insert_bill(
        Bill(
            bill_id="bill_err_01",
            sender="919876543210",
            media_id="media_err_01",
            mime_type="image/jpeg",
            received_at=datetime.now(UTC),
            status=BillStatus.POSTED,
        )
    )

    alert = Alert(
        alert_id="alert_err_test_01",
        bill_id="bill_err_01",
        sku="TEA-TAZA-250",
        low_supplier="Suresh Wholesale",
        low_rate=9000,
        low_date=date.today() - timedelta(days=2),
        low_bill="bill_low_prev",
        high_supplier="Ramesh Traders",
        high_rate=10000,
        high_date=date.today(),
        high_bill="bill_err_01",
        gap_pct=10.0,
        status=AlertStatus.PENDING,
        created_at=datetime.now(UTC),
    )
    store.insert_alert(alert)

    # Mock WhatsApp dispatch to fail with network error
    async def _failing_send(to_number: str, params: list[str]) -> str:
        raise httpx.ConnectTimeout("Connection to Meta Graph API timed out")

    monkeypatch.setattr(engine._whatsapp, "send_template_alert", _failing_send)

    # First attempt: worker runs, catches network error internally, and logs it
    res1 = await engine.run_alert_worker()
    assert res1 == alert.alert_id

    # Verify status transitioned to SENDING with attempted_at set, but not SENT
    db_alert = store.get_alert(alert.alert_id)
    assert db_alert.status == AlertStatus.SENDING
    assert db_alert.attempted_at is not None
    assert db_alert.sent_at is None
    assert db_alert.wamid is None

    # Second attempt: run_alert_worker should find 0 PENDING alerts and return None
    res = await engine.run_alert_worker()
    assert res is None


def test_webhook_handshake_verification(client: TestClient, test_settings):
    """
    Verify Meta webhook verification handshake GET route:
      - Valid token returns challenge string with status 200
      - Invalid token or mode returns 403 Forbidden
    """
    # Success
    resp = client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": test_settings.whatsapp_verify_token,
            "hub.challenge": "challenge_string_abc_123",
        },
    )
    assert resp.status_code == 200
    assert resp.text == "challenge_string_abc_123"

    # Bad token
    resp_bad_token = client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong_token",
            "hub.challenge": "challenge_string_abc_123",
        },
    )
    assert resp_bad_token.status_code == 403

    # Bad mode
    resp_bad_mode = client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "publish",
            "hub.verify_token": test_settings.whatsapp_verify_token,
            "hub.challenge": "challenge_string_abc_123",
        },
    )
    assert resp_bad_mode.status_code == 403
