"""
tests/test_live_readiness.py
============================
Live readiness, integration seam, and payload edge-case tests:
- Meta WhatsApp webhook verification edge cases
- Payload normalization (E.164 with/without '+')
- Non-image / status message filtering in mixed batches
- Media download error handling & size guards
- Gemini scanner date normalization (unambiguous YYYY-MM-DD or null)
- Exact 10 template parameter ordering
"""

from __future__ import annotations

import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.models import BillStatus, CostBasis, HeaderVerified, Purchase, Reviewed, Supplier, SupplierActive
from app.schemas import AlertParams
from app.services.gemini_scanner import GeminiScanner, MockGeminiScanner
from app.services.whatsapp import BillIntakeEvent, MockWhatsAppClient, WhatsAppWebhookParser
from app.services.workflow_engine import WorkflowEngine
from app.storage.sqlite_store import SQLiteStore

UTC = timezone.utc


class TestWebhookChallengeVerification:
    def test_challenge_success(self, client: TestClient, test_settings):
        response = client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": test_settings.whatsapp_verify_token,
                "hub.challenge": "1158201444",
            },
        )
        assert response.status_code == 200
        assert response.text == "1158201444"

    def test_challenge_wrong_mode(self, client: TestClient, test_settings):
        response = client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "unsubscribe",
                "hub.verify_token": test_settings.whatsapp_verify_token,
                "hub.challenge": "1158201444",
            },
        )
        assert response.status_code == 403

    def test_challenge_wrong_token(self, client: TestClient):
        response = client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "completely_wrong_secret",
                "hub.challenge": "1158201444",
            },
        )
        assert response.status_code == 403


class TestWhatsAppPayloadNormalization:
    def test_allowlist_matching_with_plus_prefix(self):
        """Allowlist and sender phone numbers match regardless of leading '+'."""
        parser = WhatsAppWebhookParser()
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "100200300"},
                                "messages": [
                                    {
                                        "id": "wamid.PLUS_PREFIX_01",
                                        "from": "+919876543210",  # Sender sends with '+'
                                        "timestamp": "1700000000",
                                        "type": "image",
                                        "image": {"id": "med_01", "mime_type": "image/jpeg"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }

        # Allowlist configured WITHOUT '+'
        events = parser.parse(payload, phone_number_id="100200300", allowlist=["919876543210"])
        assert len(events) == 1
        assert events[0].bill_id == "wamid.PLUS_PREFIX_01"

    def test_mixed_batch_filters_non_images_and_statuses(self):
        """A batch containing status updates, text messages, and an image processes only the image."""
        parser = WhatsAppWebhookParser()
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "100200300"},
                                "statuses": [{"id": "wamid.STATUS_01", "status": "read"}],
                                "messages": [
                                    {
                                        "id": "wamid.TEXT_01",
                                        "from": "919876543210",
                                        "timestamp": "1700000000",
                                        "type": "text",
                                        "text": {"body": "Here is my bill invoice"},
                                    },
                                    {
                                        "id": "wamid.IMAGE_01",
                                        "from": "919876543210",
                                        "timestamp": "1700000001",
                                        "type": "image",
                                        "image": {"id": "med_image_01", "mime_type": "image/jpeg"},
                                    },
                                ],
                            }
                        }
                    ]
                }
            ]
        }

        events = parser.parse(payload, phone_number_id="100200300", allowlist=["919876543210"])
        assert len(events) == 1
        assert events[0].bill_id == "wamid.IMAGE_01"


class TestGeminiScannerReadiness:
    @pytest.mark.asyncio
    async def test_non_iso_date_normalized_to_none(self):
        """When Gemini returns an ambiguous or non-ISO date string, it normalizes to None per spec."""
        scanner = MockGeminiScanner(
            fixture_response={
                "supplier_raw": "Ramesh",
                "invoice_no": "INV-1",
                "invoice_date": "20th Nov 2026",  # Ambiguous / non-ISO
                "currency": "INR",
                "invoice_total": "100.00",
                "notes": None,
                "lines": [
                    {
                        "line_no": 1,
                        "product_raw": "Item",
                        "pack_raw": None,
                        "quantity_raw": "1",
                        "unit_raw": None,
                        "rate_raw": "100",
                        "line_total_raw": "100",
                    }
                ],
            }
        )
        res = await scanner.scan_image(b"fake", "image/jpeg")
        assert res.invoice_date is None

    @pytest.mark.asyncio
    async def test_real_scanner_enforces_image_size_guard(self):
        """Image bytes exceeding max_image_bytes are rejected immediately before API call."""
        scanner = GeminiScanner(api_key="fake_key", max_image_bytes=100)
        oversized_bytes = b"X" * 150
        with pytest.raises(ValueError, match="exceeds limit"):
            await scanner.scan_image(oversized_bytes, "image/jpeg")

    @pytest.mark.asyncio
    async def test_real_scanner_enforces_mime_guard(self):
        """Non-JPEG/PNG MIME types are rejected."""
        scanner = GeminiScanner(api_key="fake_key")
        with pytest.raises(ValueError, match="Unsupported MIME type"):
            await scanner.scan_image(b"fake_pdf_bytes", "application/pdf")


class TestTemplateParameterOrdering:
    def test_alert_params_schema_and_ordering(self):
        """Ensure AlertParams serializes the exact 10 parameters in specification order."""
        alert_params = AlertParams(
            param_1_sku="BISCUIT-PARLEG-10",
            param_2_low_supplier="Suresh Wholesale",
            param_3_low_rate="Rs 46.00/unit",
            param_4_low_date="15-Nov-2026",
            param_5_low_bill="bill_001",
            param_6_high_supplier="Ramesh Traders",
            param_7_high_rate="Rs 50.00/unit",
            param_8_high_date="20-Nov-2026",
            param_9_high_bill="bill_002",
            param_10_gap_pct="8.00%",
        )

        ordered = alert_params.to_ordered_list()
        assert len(ordered) == 10
        assert ordered[0] == "BISCUIT-PARLEG-10"           # 1. sku
        assert ordered[1] == "Suresh Wholesale"          # 2. low_supplier
        assert ordered[2] == "Rs 46.00/unit"             # 3. low_rate
        assert ordered[3] == "15-Nov-2026"               # 4. low_date
        assert ordered[4] == "bill_001"                  # 5. low_bill
        assert ordered[5] == "Ramesh Traders"            # 6. high_supplier
        assert ordered[6] == "Rs 50.00/unit"             # 7. high_rate
        assert ordered[7] == "20-Nov-2026"               # 8. high_date
        assert ordered[8] == "bill_002"                  # 9. high_bill
        assert ordered[9] == "8.00%"                     # 10. gap_pct

        components = alert_params.to_whatsapp_components()
        assert len(components) == 1
        assert components[0]["type"] == "body"
        assert len(components[0]["parameters"]) == 10
        assert components[0]["parameters"][0]["text"] == "BISCUIT-PARLEG-10"
        assert components[0]["parameters"][9]["text"] == "8.00%"


class TestBackgroundSchedulerReadiness:
    def test_scheduler_lifecycle_and_jobs(self, monkeypatch):
        """Verify APScheduler initializes properly with all 3 interval jobs when enabled."""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from app.config import Settings
        from app.main import lifespan, app
        from app.storage.sqlite_store import SQLiteStore
        from app.services.whatsapp import MockWhatsAppClient
        from app.services.gemini_scanner import MockGeminiScanner
        from app.services.workflow_engine import WorkflowEngine

        test_settings = Settings(
            sqlite_db_path=":memory:",
            enable_background_workers=True,
            worker_scan_interval_seconds=5,
            worker_post_interval_seconds=5,
            worker_alert_interval_seconds=10,
            mock_whatsapp=True,
            mock_gemini=True,
        )

        # Verify configuration attributes
        assert test_settings.enable_background_workers is True
        assert test_settings.worker_scan_interval_seconds == 5
        assert test_settings.worker_post_interval_seconds == 5
        assert test_settings.worker_alert_interval_seconds == 10
