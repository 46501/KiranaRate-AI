"""
tests/test_intake.py
====================
Integration and unit tests for Pipeline 1: WhatsApp Intake Webhook.
Tests payload parsing, allowlist enforcement, phone_number_id checking,
image media filtering, deduplication, and QUEUED status persistence.
"""

from __future__ import annotations

import pytest

from app.models import BillStatus
from app.services.workflow_engine import WorkflowEngine
from app.storage.sqlite_store import SQLiteStore


@pytest.mark.asyncio
class TestIntakePipeline:
    async def test_intake_happy_path(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        sample_webhook_factory,
    ):
        """A valid image message from an allowlisted sender creates a QUEUED bill."""
        payload = sample_webhook_factory(
            message_id="wamid.HBgLMTIz",
            sender="919876543210",
            phone_number_id="100200300",
            media_id="media_001",
            mime_type="image/jpeg",
        )

        inserted_ids = await engine.run_intake(payload)
        assert len(inserted_ids) == 1
        assert inserted_ids[0] == "wamid.HBgLMTIz"

        bill = store.get_bill("wamid.HBgLMTIz")
        assert bill is not None
        assert bill.bill_id == "wamid.HBgLMTIz"
        assert bill.sender == "919876543210"
        assert bill.media_id == "media_001"
        assert bill.mime_type == "image/jpeg"
        assert bill.status == BillStatus.QUEUED

    async def test_intake_deduplication(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        sample_webhook_factory,
    ):
        """The same WhatsApp message ID sent multiple times is processed idempotently."""
        payload = sample_webhook_factory(message_id="wamid.DUPLICATE_TEST")

        # First delivery
        first_run = await engine.run_intake(payload)
        assert len(first_run) == 1

        # Second delivery (duplicate message.id)
        second_run = await engine.run_intake(payload)
        assert len(second_run) == 0

        # Bill remains in database exactly once
        bills = store.list_bills()
        matching = [b for b in bills if b.bill_id == "wamid.DUPLICATE_TEST"]
        assert len(matching) == 1

    async def test_intake_rejects_non_image_messages(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        sample_webhook_factory,
    ):
        """Text or audio messages are ignored and do not create bills."""
        payload = sample_webhook_factory(
            message_id="wamid.TEXT_MSG",
            msg_type="text",
        )

        inserted = await engine.run_intake(payload)
        assert len(inserted) == 0
        assert store.get_bill("wamid.TEXT_MSG") is None

    async def test_intake_rejects_unauthorized_sender(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        sample_webhook_factory,
    ):
        """Senders not present in the allowlist are ignored."""
        payload = sample_webhook_factory(
            message_id="wamid.STRANGER",
            sender="919999999999",  # Not in test allowlist
        )

        inserted = await engine.run_intake(payload)
        assert len(inserted) == 0
        assert store.get_bill("wamid.STRANGER") is None

    async def test_intake_rejects_wrong_phone_number_id(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        sample_webhook_factory,
    ):
        """Messages received on a different business phone number ID are ignored."""
        payload = sample_webhook_factory(
            message_id="wamid.WRONG_PHONE",
            phone_number_id="999999999",  # Mismatch with settings.whatsapp_phone_number_id
        )

        inserted = await engine.run_intake(payload)
        assert len(inserted) == 0
        assert store.get_bill("wamid.WRONG_PHONE") is None

    async def test_intake_ignores_status_callbacks(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
    ):
        """Delivery status callbacks (sent, delivered, read) do not create bills."""
        status_payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WHATSAPP_ENTRY_ID",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "1234567890",
                                    "phone_number_id": "100200300",
                                },
                                "statuses": [
                                    {
                                        "id": "wamid.STATUS_MSG",
                                        "status": "delivered",
                                        "timestamp": "1700000000",
                                        "recipient_id": "919876543210",
                                    }
                                ],
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        }

        inserted = await engine.run_intake(status_payload)
        assert len(inserted) == 0
        assert store.get_bill("wamid.STATUS_MSG") is None
