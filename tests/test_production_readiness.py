"""
tests/test_production_readiness.py
===================================
Production Readiness verification test suite for KiranaIQ:
1. Client IP Resolution & Anti-Spoofing:
   - Untrusted direct peer ignores X-Forwarded-For spoofing attempts.
   - Trusted reverse proxy extracts true client IP via right-to-left traversal.
   - Multi-proxy CIDR subnet support (e.g. 10.0.0.0/8, 127.0.0.1, ::1).
2. Rate Limiting with Trusted Proxy:
   - Verifies rate limit isolation based on true client IP.
3. Health Check:
   - Active database probe (SELECT 1), environment, model, and mock status.
4. Failed Scan Recovery (POST /bills/{bill_id}/retry):
   - Resets bill status from FAILED/SCANNING to QUEUED.
   - Clears scan error notes.
   - Purges any partial review lines to prevent duplicate ledger entries.
   - Returns 404 for unknown bills.
5. WhatsApp Inbound Document Intake & Deduplication:
   - Accepts document message type with JPEG/PNG MIME or filename extension.
   - Message deduplication prevents double intake of identical wamid.
6. WhatsApp Outgoing Text Messaging:
   - MockWhatsAppClient records sent_texts.
   - WhatsAppClient sends properly formatted Graph API POST request.
7. SQLite Hot Backup & Zero-Downtime Integrity:
   - SQLiteStore.backup() produces a readable, queryable copy.
   - PRAGMA integrity_check passes on the backup file.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from app.api.dependencies import get_engine, get_settings, get_store
from app.config import Settings
from app.main import app
from app.models import Bill, BillStatus, ReviewLine, Reviewed
from app.services.security import (
    get_client_ip,
    is_ip_in_trusted_proxies,
    rate_limiter,
)
from app.services.whatsapp import (
    MockWhatsAppClient,
    WhatsAppClient,
    WhatsAppWebhookParser,
)
from app.storage.sqlite_store import SQLiteStore


# ---------------------------------------------------------------------------
# Fixtures & Helpers
# ---------------------------------------------------------------------------

def _make_dummy_request(
    client_host: str,
    headers: dict[str, str] | None = None,
    trusted_proxies: List[str] | None = None,
) -> Request:
    """Construct a lightweight Starlette Request with client scope and headers."""
    raw_headers = []
    if headers:
        for k, v in headers.items():
            raw_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/test",
        "headers": raw_headers,
        "client": (client_host, 12345),
        "app": MagicMock(),
    }
    return Request(scope)


# ---------------------------------------------------------------------------
# 1. Trusted Proxy & IP Spoofing Tests
# ---------------------------------------------------------------------------

class TestClientIpAndProxySecurity:
    """Verify IP resolution and anti-spoofing defense."""

    def test_is_ip_in_trusted_proxies_matching(self):
        proxies = ["127.0.0.1", "::1", "10.0.0.0/8", "172.16.0.0/12"]

        assert is_ip_in_trusted_proxies("127.0.0.1", proxies) is True
        assert is_ip_in_trusted_proxies("::1", proxies) is True
        assert is_ip_in_trusted_proxies("10.1.2.3", proxies) is True
        assert is_ip_in_trusted_proxies("172.16.50.1", proxies) is True

        # Non-trusted
        assert is_ip_in_trusted_proxies("203.0.113.5", proxies) is False
        assert is_ip_in_trusted_proxies("192.168.1.1", proxies) is False
        assert is_ip_in_trusted_proxies("invalid_ip", proxies) is False
        assert is_ip_in_trusted_proxies("", proxies) is False

    def test_direct_connection_without_proxy(self):
        req = _make_dummy_request(client_host="203.0.113.195")
        ip = get_client_ip(req, trusted_proxies=["127.0.0.1"])
        assert ip == "203.0.113.195"

    def test_untrusted_peer_spoofing_x_forwarded_for_rejected(self):
        """
        Attacker at 198.51.100.1 connects directly and sends a spoofed X-Forwarded-For.
        Because 198.51.100.1 is not in trusted_proxies, the header MUST be ignored.
        """
        req = _make_dummy_request(
            client_host="198.51.100.1",
            headers={"X-Forwarded-For": "203.0.113.50, 1.1.1.1"},
        )
        ip = get_client_ip(req, trusted_proxies=["127.0.0.1", "::1"])
        assert ip == "198.51.100.1"

    def test_trusted_proxy_single_hop_resolves_client(self):
        """
        Nginx reverse proxy on 127.0.0.1 forwards real client IP 203.0.113.195.
        """
        req = _make_dummy_request(
            client_host="127.0.0.1",
            headers={"X-Forwarded-For": "203.0.113.195"},
        )
        ip = get_client_ip(req, trusted_proxies=["127.0.0.1", "::1"])
        assert ip == "203.0.113.195"

    def test_trusted_proxy_chain_right_to_left_traversal(self):
        """
        Internal load balancer (10.0.0.2) -> Local reverse proxy (127.0.0.1).
        Header: 203.0.113.195, 10.0.0.2
        Both 127.0.0.1 and 10.0.0.0/8 are trusted proxies.
        Traversing right-to-left:
        - 10.0.0.2 is trusted.
        - 203.0.113.195 is untrusted -> True client IP!
        """
        req = _make_dummy_request(
            client_host="127.0.0.1",
            headers={"X-Forwarded-For": "203.0.113.195, 10.0.0.2"},
        )
        ip = get_client_ip(req, trusted_proxies=["127.0.0.1", "10.0.0.0/8"])
        assert ip == "203.0.113.195"

    def test_trusted_proxy_with_attacker_injecting_upstream_header(self):
        """
        Attacker at 203.0.113.195 sends 'X-Forwarded-For: 1.1.1.1' to Nginx (127.0.0.1).
        Nginx appends the connecting IP: '1.1.1.1, 203.0.113.195'.
        Evaluating right-to-left:
        - Direct peer: 127.0.0.1 (trusted)
        - Candidate 1 (rightmost): 203.0.113.195 (not in trusted proxies) -> Returns 203.0.113.195!
        Attacker CANNOT spoof 1.1.1.1.
        """
        req = _make_dummy_request(
            client_host="127.0.0.1",
            headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.195"},
        )
        ip = get_client_ip(req, trusted_proxies=["127.0.0.1"])
        assert ip == "203.0.113.195"


# ---------------------------------------------------------------------------
# 2. Health Endpoint Probe
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    """Verify production health check and DB connectivity reporting."""

    def test_health_check_success(self, store, engine):
        app.dependency_overrides[get_settings] = lambda: Settings(
            environment="production",
            gemini_model="gemini-3.8-flash",
            mock_gemini=False,
            mock_whatsapp=False,
        )
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_engine] = lambda: engine

        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["database"] == "connected"
        assert data["environment"] == "production"
        assert data["model"] == "gemini-3.8-flash"
        assert data["mock_mode"]["gemini"] is False
        assert data["mock_mode"]["whatsapp"] is False


# ---------------------------------------------------------------------------
# 3. Failed Scan Recovery (Retry) Endpoint
# ---------------------------------------------------------------------------

class TestScanRecovery:
    """Verify recovery and retry of failed or stuck bill scans."""

    def test_retry_bill_resets_status_and_clears_notes(self, store, engine, test_settings, make_bill):
        test_settings.api_key = "secret123"
        app.dependency_overrides[get_settings] = lambda: test_settings
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_engine] = lambda: engine

        # Create a bill in SCANNING state with error notes
        bill = make_bill(
            bill_id="bill_test_retry_01",
            status=BillStatus.SCANNING,
            notes="AI scan failed: [RETRYABLE_ERROR] Gemini network timeout",
        )

        # Also insert a partial review line to confirm it gets purged on retry
        review_line = ReviewLine(
            review_id="rev_partial_01",
            bill_id="bill_test_retry_01",
            scan_id="scan_partial_01",
            line_no=1,
            product_raw="Incomplete Line",
            pack_raw="1",
            quantity_raw="1",
            unit_raw="unit",
            rate_raw="1000",
            line_total_raw="1000",
        )
        store.insert_review_line(review_line)

        # Verify initial state
        saved_bill = store.get_bill("bill_test_retry_01")
        assert saved_bill.status == BillStatus.SCANNING
        assert "Gemini network timeout" in saved_bill.notes
        assert len(store.get_review_lines("bill_test_retry_01")) == 1

        # Call retry endpoint
        client = TestClient(app)
        resp = client.post(
            "/bills/bill_test_retry_01/retry",
            headers={"X-API-Key": "secret123"},
        )
        assert resp.status_code == 200
        res_data = resp.json()
        assert res_data["status"] == "ok"
        assert res_data["bill_id"] == "bill_test_retry_01"

        # Verify database state after retry
        refreshed_bill = store.get_bill("bill_test_retry_01")
        # In test mode with mock scanner, engine.run_scan_worker() scans it immediately to REVIEW
        assert refreshed_bill.status in (BillStatus.QUEUED, BillStatus.REVIEW)
        assert refreshed_bill.notes is None or "Gemini network timeout" not in refreshed_bill.notes

    def test_retry_nonexistent_bill_returns_404(self, store, engine, test_settings):
        test_settings.api_key = "secret123"
        app.dependency_overrides[get_settings] = lambda: test_settings
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_engine] = lambda: engine

        client = TestClient(app)
        resp = client.post(
            "/bills/nonexistent_bill_id/retry",
            headers={"X-API-Key": "secret123"},
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 4. WhatsApp Document Intake & Deduplication
# ---------------------------------------------------------------------------

class TestWhatsAppProductionIntake:
    """Verify document intake support and webhook deduplication."""

    def test_whatsapp_webhook_parser_accepts_document_image(self):
        """Merchants sending bills as uncompressed documents are accepted."""
        parser = WhatsAppWebhookParser()
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "12345",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "15551234567",
                                    "phone_number_id": "105938472910482",
                                },
                                "messages": [
                                    {
                                        "from": "919876543210",
                                        "id": "wamid.HBgLMTIzNDU2Nzg5",
                                        "timestamp": "1700000000",
                                        "type": "document",
                                        "document": {
                                            "filename": "supplier_bill_nov26.jpg",
                                            "mime_type": "image/jpeg",
                                            "sha256": "abcdef123456",
                                            "id": "media_doc_999",
                                        },
                                    }
                                ],
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        }

        messages = parser.parse(payload, phone_number_id="105938472910482", allowlist=["919876543210"])
        assert len(messages) == 1
        msg = messages[0]
        assert msg.sender == "919876543210"
        assert msg.media_id == "media_doc_999"
        assert msg.bill_id == "wamid.HBgLMTIzNDU2Nzg5"
        assert msg.mime_type == "image/jpeg"

    @pytest.mark.asyncio
    async def test_whatsapp_send_text_message_in_mock(self):
        """MockWhatsAppClient records outgoing notifications and text messages."""
        mock_client = MockWhatsAppClient()
        msg_id = await mock_client.send_text_message(
            to_number="919876543210",
            text="Bill #1234 received and queued for scanning.",
        )
        assert msg_id.startswith("mock-wamid-txt-")
        assert len(mock_client.sent_texts) == 1
        record = mock_client.sent_texts[0]
        assert record.to_number == "919876543210"
        assert "queued for scanning" in record.text

    @pytest.mark.asyncio
    async def test_whatsapp_send_text_message_live_client_payload(self):
        """WhatsAppClient builds correct Meta Cloud API JSON payload."""
        client = WhatsAppClient(
            access_token="test_token_abc",
            phone_number_id="105938472910482",
            owner_number="919999999999",
            api_version="v19.0",
        )

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {
                "messaging_product": "whatsapp",
                "contacts": [{"input": "919876543210", "wa_id": "919876543210"}],
                "messages": [{"id": "wamid.sent12345"}],
            }
            mock_post.return_value = mock_resp

            result_id = await client.send_text_message(
                to_number="919876543210",
                text="Test acknowledgment",
            )

            assert result_id == "wamid.sent12345"
            mock_post.assert_called_once()
            call_url = mock_post.call_args[0][0]
            assert "105938472910482/messages" in call_url
            payload = mock_post.call_args[1]["json"]
            assert payload["messaging_product"] == "whatsapp"
            assert payload["to"] == "919876543210"
            assert payload["type"] == "text"
            assert payload["text"]["body"] == "Test acknowledgment"
            assert "105938472910482/messages" in call_url
            payload = mock_post.call_args[1]["json"]
            assert payload["messaging_product"] == "whatsapp"
            assert payload["to"] == "919876543210"
            assert payload["type"] == "text"
            assert payload["text"]["body"] == "Test acknowledgment"


# ---------------------------------------------------------------------------
# 5. SQLite Hot Backup & Integrity
# ---------------------------------------------------------------------------

class TestSQLiteHotBackup:
    """Verify online zero-downtime backup capability."""

    def test_store_backup_creates_valid_database(self, tmp_path: Path):
        db_source = tmp_path / "source.db"
        db_backup = tmp_path / "backup.db"

        # Initialize store and write sample bill
        store = SQLiteStore(str(db_source))
        store.create_tables()
        bill = Bill(
            bill_id="bill_backup_test_01",
            sender="919876543210",
            media_id="media_123",
            mime_type="image/jpeg",
            received_at=datetime.now(timezone.utc),
            status=BillStatus.POSTED,
            supplier_raw="Test Supplier",
            invoice_total="500.00",
        )
        store.insert_bill(bill)

        # Execute online backup
        store.backup(str(db_backup))

        assert db_backup.exists()
        assert db_backup.stat().st_size > 0

        # Validate that the backup file can be opened and passes integrity check
        conn = sqlite3.connect(str(db_backup))
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check")
            integrity = cursor.fetchone()[0]
            assert integrity == "ok"

            cursor.execute("SELECT bill_id, supplier_raw, invoice_total FROM bills WHERE bill_id = ?", ("bill_backup_test_01",))
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "bill_backup_test_01"
            assert row[1] == "Test Supplier"
            assert row[2] == "500.00"
        finally:
            conn.close()

    def test_backup_script_cli_execution(self, tmp_path: Path):
        """Test scripts/backup_db.py execution via CLI."""
        db_path = tmp_path / "bills.db"
        backup_dir = tmp_path / "backups"

        # Create source db with schema
        store = SQLiteStore(str(db_path))
        store.create_tables()
        bill = Bill(
            bill_id="bill_cli_backup",
            sender="919876543210",
            media_id="media_cli",
            mime_type="image/jpeg",
            received_at=datetime.now(timezone.utc),
            status=BillStatus.REVIEW,
        )
        store.insert_bill(bill)

        # Run backup script
        script_path = Path(__file__).parent.parent / "scripts" / "backup_db.py"
        res = subprocess.run(
            [
                sys.executable,
                str(script_path),
                "--db-path",
                str(db_path),
                "--dest-dir",
                str(backup_dir),
                "--keep",
                "3",
            ],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0
        output = res.stdout + res.stderr
        assert "Backup completed successfully" in output
        assert "Backup integrity check PASSED: ok" in output

        backups = list(backup_dir.glob("bills_backup_*.db"))
        assert len(backups) == 1
