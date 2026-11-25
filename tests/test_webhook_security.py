"""
tests/test_webhook_security.py
==============================
Unit and integration tests for WhatsApp Webhook HMAC-SHA256 signature verification:
- Meta X-Hub-Signature-256 computation and validation helpers
- Inbound POST /webhook/whatsapp with valid, invalid, missing, and tampered signatures
- GET /webhook/whatsapp hub challenge verification with constant-time comparison
- Mock mode vs. live enforcement behaviors
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from app.api.dependencies import get_engine, get_settings, get_store
from app.config import Settings
from app.main import app
from app.models import BillStatus
from app.services.whatsapp import (
    MockWhatsAppClient,
    WhatsAppClient,
    compute_webhook_signature,
    verify_webhook_signature,
)
from app.services.workflow_engine import WorkflowEngine
from app.storage.sqlite_store import SQLiteStore

UTC = timezone.utc


# ===========================================================================
# 1. Direct Helper Function Unit Tests
# ===========================================================================

class TestSignatureHelpers:
    def test_compute_signature_known_vector(self):
        secret = "super_secret_app_key"
        body = b'{"test": "payload"}'
        expected_digest = hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()

        sig = compute_webhook_signature(body, secret)
        assert sig == f"sha256={expected_digest}"

    def test_compute_signature_empty_secret_returns_empty(self):
        assert compute_webhook_signature(b"data", "") == ""

    def test_verify_signature_valid(self):
        secret = "test_key_123"
        body = b'{"object": "whatsapp_business_account"}'
        sig = compute_webhook_signature(body, secret)

        assert verify_webhook_signature(body, sig, secret) is True
        # Case insensitive header check
        assert verify_webhook_signature(body, sig.upper(), secret) is True

    def test_verify_signature_invalid_fails(self):
        secret = "test_key_123"
        body = b'{"object": "whatsapp_business_account"}'
        bad_sig = "sha256=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

        assert verify_webhook_signature(body, bad_sig, secret) is False

    def test_verify_signature_tampered_payload_fails(self):
        secret = "test_key_123"
        orig_body = b'{"amount": 100}'
        tampered_body = b'{"amount": 999}'
        sig = compute_webhook_signature(orig_body, secret)

        assert verify_webhook_signature(tampered_body, sig, secret) is False

    def test_verify_signature_missing_or_empty_inputs(self):
        assert verify_webhook_signature(b"body", None, "secret") is False
        assert verify_webhook_signature(b"body", "", "secret") is False
        assert verify_webhook_signature(b"body", "sha256=abc", "") is False

    def test_class_static_methods(self):
        secret = "static_method_test"
        body = b"hello world"
        sig = WhatsAppClient.compute_signature(body, secret)
        assert WhatsAppClient.verify_signature(body, sig, secret) is True
        assert MockWhatsAppClient.verify_signature(body, sig, secret) is True


# ===========================================================================
# 2. Webhook Challenge Verification (GET /webhook/whatsapp)
# ===========================================================================

class TestWebhookChallengeVerification:
    def test_challenge_success_with_matching_token(self, client: TestClient, test_settings: Settings):
        response = client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": test_settings.whatsapp_verify_token,
                "hub.challenge": "CONFIRM_CHALLENGE_999",
            },
        )
        assert response.status_code == 200
        assert response.text == "CONFIRM_CHALLENGE_999"

    def test_challenge_rejected_with_invalid_token(self, client: TestClient):
        response = client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "WRONG_TOKEN_VALUE",
                "hub.challenge": "CONFIRM_CHALLENGE_999",
            },
        )
        assert response.status_code == 403
        assert "Verification failed" in response.text

    def test_challenge_rejected_with_invalid_mode(self, client: TestClient, test_settings: Settings):
        response = client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "not_subscribe",
                "hub.verify_token": test_settings.whatsapp_verify_token,
                "hub.challenge": "CONFIRM_CHALLENGE_999",
            },
        )
        assert response.status_code == 403

    def test_client_class_verify_webhook_constant_time(self, test_settings: Settings):
        wa_client = WhatsAppClient(
            access_token=test_settings.whatsapp_access_token,
            phone_number_id=test_settings.whatsapp_phone_number_id,
            api_version=test_settings.whatsapp_api_version,
            owner_number=test_settings.whatsapp_owner_number,
        )
        challenge = wa_client.verify_webhook(
            mode="subscribe",
            token="secret_token",
            challenge="echome",
            verify_token="secret_token",
        )
        assert challenge == "echome"

        with pytest.raises(ValueError, match="mismatch"):
            wa_client.verify_webhook(
                mode="subscribe",
                token="wrong_token",
                challenge="echome",
                verify_token="secret_token",
            )


# ===========================================================================
# 3. Webhook POST Signature Verification (POST /webhook/whatsapp)
# ===========================================================================

class TestWebhookPostSignatureSecurity:
    @pytest.fixture
    def secure_client(self, store: SQLiteStore, engine: WorkflowEngine, test_settings: Settings):
        """Client configured with an active WHATSAPP_APP_SECRET."""
        sec_settings = Settings(
            whatsapp_verify_token="verify_secret_123",
            whatsapp_app_secret="test_meta_app_secret_abc123",
            whatsapp_access_token="test_token",
            whatsapp_phone_number_id="100200300",
            whatsapp_owner_number="919999999999",
            whatsapp_allowlist=["919876543210"],
            gemini_api_key="test_gemini_key",
            sqlite_db_path=":memory:",
            mock_whatsapp=True,
            mock_gemini=True,
        )
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_engine] = lambda: engine
        app.dependency_overrides[get_settings] = lambda: sec_settings
        with TestClient(app) as c:
            app.state.store = store
            app.state.engine = engine
            app.state.settings = sec_settings
            yield c, sec_settings
        app.dependency_overrides.clear()

    def test_post_valid_signature_accepted(self, secure_client, sample_webhook_factory):
        client, sec_settings = secure_client
        payload = sample_webhook_factory(message_id="wamid.VALID_SIG_001")
        body_bytes = json.dumps(payload).encode("utf-8")
        sig = compute_webhook_signature(body_bytes, sec_settings.whatsapp_app_secret)

        response = client.post(
            "/webhook/whatsapp",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "wamid.VALID_SIG_001" in data["bill_ids"]

    def test_post_missing_signature_rejected_with_403(self, secure_client, sample_webhook_factory):
        client, _ = secure_client
        payload = sample_webhook_factory(message_id="wamid.NO_SIG_001")

        response = client.post(
            "/webhook/whatsapp",
            json=payload,
            # No X-Hub-Signature-256 header sent
        )
        assert response.status_code == 403
        data = response.json()
        assert "Missing X-Hub-Signature-256" in data["detail"]

    def test_post_invalid_signature_rejected_with_403(self, secure_client, sample_webhook_factory):
        client, _ = secure_client
        payload = sample_webhook_factory(message_id="wamid.BAD_SIG_001")
        body_bytes = json.dumps(payload).encode("utf-8")
        bad_sig = "sha256=1111111111222222222233333333334444444444555555555566666666667777"

        response = client.post(
            "/webhook/whatsapp",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": bad_sig,
            },
        )
        assert response.status_code == 403
        data = response.json()
        assert "Invalid webhook signature" in data["detail"]

    def test_post_tampered_payload_rejected_with_403(self, secure_client, sample_webhook_factory):
        client, sec_settings = secure_client
        payload = sample_webhook_factory(message_id="wamid.ORIGINAL_SIG_001")
        body_bytes = json.dumps(payload).encode("utf-8")
        sig = compute_webhook_signature(body_bytes, sec_settings.whatsapp_app_secret)

        # Tamper payload bytes after computing signature
        tampered_bytes = body_bytes.replace(b"wamid.ORIGINAL_SIG_001", b"wamid.TAMPERED_SIG_001")

        response = client.post(
            "/webhook/whatsapp",
            content=tampered_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )
        assert response.status_code == 403
        data = response.json()
        assert "Invalid webhook signature" in data["detail"]

    def test_post_live_mode_without_secret_rejected_with_403(
        self, store: SQLiteStore, engine: WorkflowEngine, sample_webhook_factory
    ):
        """In live mode (mock_whatsapp=False), omitting WHATSAPP_APP_SECRET raises 403."""
        live_settings = Settings(
            whatsapp_verify_token="token",
            whatsapp_app_secret="",  # Unset in live mode
            whatsapp_access_token="test_token",
            whatsapp_phone_number_id="100200300",
            whatsapp_owner_number="919999999999",
            whatsapp_allowlist=["919876543210"],
            gemini_api_key="test_gemini_key",
            sqlite_db_path=":memory:",
            mock_whatsapp=False,  # LIVE mode!
            mock_gemini=True,
        )
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_engine] = lambda: engine
        app.dependency_overrides[get_settings] = lambda: live_settings
        with TestClient(app) as c:
            app.state.store = store
            app.state.engine = engine
            app.state.settings = live_settings

            payload = sample_webhook_factory(message_id="wamid.LIVE_TEST_001")
            response = c.post(
                "/webhook/whatsapp",
                json=payload,
                headers={"X-Hub-Signature-256": "sha256=dummy"},
            )
            assert response.status_code == 403
            assert "Webhook secret not configured" in response.json()["detail"]
        app.dependency_overrides.clear()
