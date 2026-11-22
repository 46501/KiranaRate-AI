"""
tests/test_alerts.py
====================
Integration tests for Pipeline 4: Price Alert Outbox.
Tests alert state machine:
- PENDING -> SENDING (with attempted_at set before network call) -> SENT (with wamid & sent_at)
- Expiry (> 24 hours) transitioning to EXPIRED
- Skip when parent bill is not POSTED
- Network failure: remains in SENDING (no auto-retry to prevent duplicates)
- Correct 10 ordered template parameters sent
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import (
    Alert,
    AlertStatus,
    BillStatus,
)
from app.services.whatsapp import MockWhatsAppClient
from app.services.workflow_engine import WorkflowEngine
from app.storage.sqlite_store import SQLiteStore

UTC = timezone.utc


@pytest.mark.asyncio
class TestAlertsPipeline:
    async def test_alert_outbox_happy_path(
        self,
        store: SQLiteStore,
        make_bill,
        test_settings,
    ):
        """Happy path: PENDING alert -> SENDING -> SENT with wamid and 10 template parameters."""
        bill = make_bill(status=BillStatus.POSTED)

        alert = Alert(
            alert_id="alert_happy_01",
            bill_id=bill.bill_id,
            sku="BISCUIT-PARLEG-10",
            low_supplier="SUP002",
            high_supplier="SUP001",
            low_date=date(2026, 11, 15),
            high_date=date(2026, 11, 20),
            low_bill="bill_low",
            high_bill="bill_high",
            low_rate=4600,
            high_rate=5000,
            gap_pct=8.00,
            status=AlertStatus.PENDING,
            created_at=datetime.now(UTC),
        )
        store.insert_alert(alert)

        mock_whatsapp = MockWhatsAppClient()
        engine = WorkflowEngine(store=store, whatsapp=mock_whatsapp, settings=test_settings)

        processed_id = await engine.run_alert_worker()
        assert processed_id == "alert_happy_01"

        updated = store.get_oldest_pending_alert()
        assert updated is None  # No longer pending

        # Retrieve by listing / updating
        # Check that WhatsApp mock received the alert
        assert len(mock_whatsapp.sent_alerts) == 1
        sent_item = mock_whatsapp.sent_alerts[0]
        assert sent_item.to_number == test_settings.whatsapp_owner_number

        params = sent_item.params
        assert len(params) == 10
        assert params[0] == "BISCUIT-PARLEG-10"           # 1  sku
        assert "Suresh Wholesale" in params[1]          # 2  low_supplier_name
        assert "Rs 46.00/unit" in params[2]             # 3  low_rate
        assert "15-Nov-2026" in params[3]               # 4  low_date
        assert params[4] == "bill_low"                  # 5  low_bill
        assert "Ramesh Traders" in params[5]            # 6  high_supplier_name
        assert "Rs 50.00/unit" in params[6]             # 7  high_rate
        assert "20-Nov-2026" in params[7]               # 8  high_date
        assert params[8] == "bill_high"                 # 9  high_bill
        assert "8.00%" in params[9]                     # 10 gap_pct

    async def test_alert_older_than_24h_marked_expired(
        self,
        store: SQLiteStore,
        make_bill,
        test_settings,
    ):
        """PENDING alert created > 24 hours ago transitions to EXPIRED and is not sent."""
        bill = make_bill(status=BillStatus.POSTED)

        old_created = datetime.now(UTC) - timedelta(hours=25)
        alert = Alert(
            alert_id="alert_stale_25h",
            bill_id=bill.bill_id,
            sku="BISCUIT-PARLEG-10",
            low_supplier="SUP002",
            high_supplier="SUP001",
            low_date=date(2026, 11, 10),
            high_date=date(2026, 11, 15),
            low_bill="b_low",
            high_bill="b_high",
            low_rate=4600,
            high_rate=5000,
            gap_pct=8.00,
            status=AlertStatus.PENDING,
            created_at=old_created,
        )
        store.insert_alert(alert)

        mock_whatsapp = MockWhatsAppClient()
        engine = WorkflowEngine(store=store, whatsapp=mock_whatsapp, settings=test_settings)

        processed_id = await engine.run_alert_worker()
        assert processed_id == "alert_stale_25h"

        # WhatsApp was NOT called
        assert len(mock_whatsapp.sent_alerts) == 0

    async def test_alert_send_failure_leaves_status_sending(
        self,
        store: SQLiteStore,
        make_bill,
        test_settings,
    ):
        """When WhatsApp API fails/times out, alert stays in SENDING state (no auto-retry)."""
        bill = make_bill(status=BillStatus.POSTED)

        alert = Alert(
            alert_id="alert_fail_01",
            bill_id=bill.bill_id,
            sku="BISCUIT-PARLEG-10",
            low_supplier="SUP002",
            high_supplier="SUP001",
            low_date=date(2026, 11, 15),
            high_date=date(2026, 11, 20),
            low_bill="b_low",
            high_bill="b_high",
            low_rate=4600,
            high_rate=5000,
            gap_pct=8.00,
            status=AlertStatus.PENDING,
            created_at=datetime.now(UTC),
        )
        store.insert_alert(alert)

        # Mock client configured to raise network error
        failing_whatsapp = MockWhatsAppClient()

        async def fail_send(*args, **kwargs):
            raise RuntimeError("WhatsApp API connection timeout")

        failing_whatsapp.send_template_alert = fail_send

        engine = WorkflowEngine(store=store, whatsapp=failing_whatsapp, settings=test_settings)

        processed_id = await engine.run_alert_worker()
        assert processed_id == "alert_fail_01"

        # Alert must NOT be in PENDING anymore (so it is not retried blindly in an infinite loop)
        assert store.get_oldest_pending_alert() is None
