"""
tests/test_priority3_security.py
=================================
Dedicated test suite verifying Priority 3 Security Hardening in KiranaIQ:
1. Rate Limiting on login, upload, webhook, and worker endpoints.
2. Login Protection against repeated failed attempts (brute-force account lockout).
3. File Validation with JPEG/PNG binary magic byte verification.
4. HTTP Security Headers (X-Content-Type-Options, X-Frame-Options, Referrer-Policy, CSP).
5. Upload Memory Protection (early Content-Length rejection and safe chunked streaming).
6. Error Message Sanitization (path scrubbing and internal exception suppression).
"""

from __future__ import annotations

import io
import re
import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.main import app
from app.services.security import (
    RateLimiter,
    LoginAttemptTracker,
    login_tracker,
    rate_limiter,
    read_upload_file_safely,
    sanitize_user_facing_error,
    validate_image_magic_bytes,
)


# ---------------------------------------------------------------------------
# Test Helpers & Fixtures
# ---------------------------------------------------------------------------

_VALID_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 64
_VALID_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 32
_INVALID_IMAGE = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 32  # PE Windows Executable header


@pytest.fixture
def p3_client(store, engine, test_settings: Settings, monkeypatch) -> TestClient:
    """Configured TestClient with auth, store, engine, and rate limits enabled."""
    from app.api.dependencies import get_engine, get_settings, get_store
    from app.config import settings as global_settings

    monkeypatch.setattr(global_settings, "dashboard_username", "admin")
    monkeypatch.setattr(global_settings, "dashboard_password", "supersecretpassword123")
    monkeypatch.setattr(global_settings, "api_key", "test_p3_api_key_xyz")
    monkeypatch.setattr(global_settings, "rate_limit_enabled", True)
    monkeypatch.setattr(global_settings, "rate_limit_login_per_minute", 5)
    monkeypatch.setattr(global_settings, "rate_limit_upload_per_minute", 5)
    monkeypatch.setattr(global_settings, "rate_limit_webhook_per_minute", 10)
    monkeypatch.setattr(global_settings, "rate_limit_worker_per_minute", 5)
    monkeypatch.setattr(global_settings, "login_max_failed_attempts", 3)
    monkeypatch.setattr(global_settings, "login_lockout_seconds", 60)

    test_settings.dashboard_username = "admin"
    test_settings.dashboard_password = "supersecretpassword123"
    test_settings.api_key = "test_p3_api_key_xyz"
    test_settings.rate_limit_enabled = True
    test_settings.rate_limit_login_per_minute = 5
    test_settings.rate_limit_upload_per_minute = 5
    test_settings.rate_limit_webhook_per_minute = 10
    test_settings.rate_limit_worker_per_minute = 5
    test_settings.login_max_failed_attempts = 3
    test_settings.login_lockout_seconds = 60

    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_settings] = lambda: global_settings

    app.state.store = store
    app.state.engine = engine
    app.state.settings = global_settings

    with TestClient(app, raise_server_exceptions=False) as client:
        rate_limiter.reset()
        login_tracker.reset()
        yield client


# ---------------------------------------------------------------------------
# 1. Rate Limiting Tests
# ---------------------------------------------------------------------------

class TestRateLimiting:
    def test_rate_limiter_sliding_window_logic(self):
        limiter = RateLimiter()
        key = "test_client_1"

        # Allowed for first 3 requests
        for _ in range(3):
            allowed, retry = limiter.is_allowed(key, max_requests=3, window_seconds=10)
            assert allowed is True
            assert retry == 0

        # 4th request must be rejected
        allowed, retry = limiter.is_allowed(key, max_requests=3, window_seconds=10)
        assert allowed is False
        assert retry > 0

        # Resetting permits new requests
        limiter.reset(key)
        allowed, _ = limiter.is_allowed(key, max_requests=3, window_seconds=10)
        assert allowed is True

    def test_login_endpoint_enforces_rate_limit(self, p3_client: TestClient):
        # Initial login requests up to limit
        for _ in range(5):
            resp = p3_client.post(
                "/ui/login",
                data={"username": "wrong", "password": "bad", "csrf_token": ""},
            )
            # Should be 403 (invalid CSRF) or 401
            assert resp.status_code in (401, 403)

        # 6th request must trigger HTTP 429 Too Many Requests
        resp = p3_client.post(
            "/ui/login",
            data={"username": "wrong", "password": "bad", "csrf_token": ""},
        )
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers

    def test_upload_endpoint_enforces_rate_limit(self, p3_client: TestClient):
        headers = {"Authorization": "Bearer test_p3_api_key_xyz"}
        file_data = ("invoice.jpg", io.BytesIO(_VALID_JPEG), "image/jpeg")

        for _ in range(5):
            file_data[1].seek(0)
            resp = p3_client.post(
                "/bills/upload",
                files={"file": ("invoice.jpg", io.BytesIO(_VALID_JPEG), "image/jpeg")},
                headers=headers,
            )
            assert resp.status_code == 200

        # 6th request must trigger HTTP 429
        resp = p3_client.post(
            "/bills/upload",
            files={"file": ("invoice.jpg", io.BytesIO(_VALID_JPEG), "image/jpeg")},
            headers=headers,
        )
        assert resp.status_code == 429
        assert "Rate limit exceeded for upload" in resp.json()["detail"]
        assert "Retry-After" in resp.headers

    def test_webhook_endpoint_enforces_rate_limit(self, p3_client: TestClient):
        for _ in range(10):
            resp = p3_client.get(
                "/webhook/whatsapp",
                params={"hub.mode": "subscribe", "hub.verify_token": "wrong"},
            )
            assert resp.status_code == 403

        # 11th request triggers HTTP 429
        resp = p3_client.get(
            "/webhook/whatsapp",
            params={"hub.mode": "subscribe", "hub.verify_token": "wrong"},
        )
        assert resp.status_code == 429
        assert "Rate limit exceeded for webhook" in resp.json()["detail"]

    def test_worker_endpoint_enforces_rate_limit(self, p3_client: TestClient):
        headers = {"Authorization": "Bearer test_p3_api_key_xyz"}
        for _ in range(5):
            resp = p3_client.post("/workers/run/scan", headers=headers)
            assert resp.status_code == 200

        # 6th request triggers HTTP 429
        resp = p3_client.post("/workers/run/scan", headers=headers)
        assert resp.status_code == 429
        assert "Rate limit exceeded for worker" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 2. Login Protection & Brute-Force Lockout Tests
# ---------------------------------------------------------------------------

class TestLoginProtection:
    def test_login_attempt_tracker_lockout_and_success_reset(self):
        tracker = LoginAttemptTracker(max_failed_attempts=3, lockout_seconds=60)
        key = "admin:127.0.0.1"

        # 1st and 2nd failure: not locked
        locked, retry = tracker.record_failure(key)
        assert locked is False
        assert retry == 0

        locked, retry = tracker.record_failure(key)
        assert locked is False
        assert retry == 0

        # 3rd failure: locked!
        locked, retry = tracker.record_failure(key)
        assert locked is True
        assert retry > 0

        # Further check confirms lock
        is_locked, retry = tracker.is_locked(key)
        assert is_locked is True

        # Successful login resets the lock
        tracker.record_success(key)
        is_locked, _ = tracker.is_locked(key)
        assert is_locked is False

    def test_repeated_failed_logins_triggers_account_lockout_in_ui(self, p3_client: TestClient):
        # Fetch fresh CSRF token
        get_resp = p3_client.get("/ui/login")
        csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', get_resp.text)
        assert csrf_match is not None
        csrf_token = csrf_match.group(1)

        # Send 2 failed login attempts (threshold = 3)
        for _ in range(2):
            resp = p3_client.post(
                "/ui/login",
                data={"username": "admin", "password": "wrongpassword", "csrf_token": csrf_token},
            )
            assert resp.status_code == 401
            assert "Invalid username or password" in resp.text

        # 3rd failed attempt triggers lockout
        resp = p3_client.post(
            "/ui/login",
            data={"username": "admin", "password": "wrongpassword", "csrf_token": csrf_token},
        )
        assert resp.status_code == 429
        assert "Account temporarily locked" in resp.text
        assert "Retry-After" in resp.headers


# ---------------------------------------------------------------------------
# 3. File Validation (Magic Bytes) Tests
# ---------------------------------------------------------------------------

class TestFileValidation:
    def test_validate_image_magic_bytes_valid_jpeg(self):
        mime = validate_image_magic_bytes(_VALID_JPEG)
        assert mime == "image/jpeg"

    def test_validate_image_magic_bytes_valid_png(self):
        mime = validate_image_magic_bytes(_VALID_PNG)
        assert mime == "image/png"

    def test_validate_image_magic_bytes_rejects_executable_or_text(self):
        with pytest.raises(ValueError, match="does not match JPEG or PNG binary magic bytes"):
            validate_image_magic_bytes(_INVALID_IMAGE)

        with pytest.raises(ValueError, match="does not match JPEG or PNG binary magic bytes"):
            validate_image_magic_bytes(b"<html><body>test</body></html>")

    def test_validate_image_magic_bytes_rejects_truncated(self):
        with pytest.raises(ValueError, match="too small"):
            validate_image_magic_bytes(b"\xff\xd8")

    def test_upload_api_rejects_disguised_file(self, p3_client: TestClient):
        headers = {"Authorization": "Bearer test_p3_api_key_xyz"}
        # File has .jpg extension and image/jpeg header, but content is an executable header
        files = {"file": ("malicious.jpg", io.BytesIO(_INVALID_IMAGE), "image/jpeg")}
        resp = p3_client.post("/bills/upload", files=files, headers=headers)
        assert resp.status_code == 400
        assert "does not match JPEG or PNG binary magic bytes" in resp.json()["detail"]

    def test_upload_api_accepts_valid_jpeg(self, p3_client: TestClient):
        headers = {"Authorization": "Bearer test_p3_api_key_xyz"}
        files = {"file": ("valid_bill.jpg", io.BytesIO(_VALID_JPEG), "image/jpeg")}
        resp = p3_client.post("/bills/upload", files=files, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    def test_upload_api_accepts_valid_png(self, p3_client: TestClient):
        headers = {"Authorization": "Bearer test_p3_api_key_xyz"}
        files = {"file": ("valid_bill.png", io.BytesIO(_VALID_PNG), "image/png")}
        resp = p3_client.post("/bills/upload", files=files, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"


# ---------------------------------------------------------------------------
# 4. HTTP Security Headers Tests
# ---------------------------------------------------------------------------

class TestHttpSecurityHeaders:
    def test_security_headers_present_on_api_responses(self, p3_client: TestClient):
        resp = p3_client.get("/health")
        assert resp.status_code == 200
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert "default-src 'self'" in resp.headers.get("Content-Security-Policy", "")
        assert "frame-ancestors 'none'" in resp.headers.get("Content-Security-Policy", "")

    def test_security_headers_present_on_html_responses(self, p3_client: TestClient):
        resp = p3_client.get("/ui/login")
        assert resp.status_code == 200
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert "Content-Security-Policy" in resp.headers


# ---------------------------------------------------------------------------
# 5. Upload Memory Protection Tests
# ---------------------------------------------------------------------------

class TestUploadMemoryProtection:
    def test_content_length_exceeding_limit_rejected_early_with_413(self, p3_client: TestClient):
        headers = {
            "Authorization": "Bearer test_p3_api_key_xyz",
            "Content-Length": str(10 * 1024 * 1024),  # 10 MB > 5 MB limit
        }
        files = {"file": ("huge.jpg", io.BytesIO(_VALID_JPEG), "image/jpeg")}
        resp = p3_client.post("/bills/upload", files=files, headers=headers)
        assert resp.status_code == 413
        assert "exceeds limit" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_chunked_stream_reading_aborts_when_exceeding_max_bytes(self):
        from starlette.datastructures import UploadFile as StarletteUploadFile
        # Create a mock stream of 2 MB when limit is 1 MB
        oversized_data = io.BytesIO(_VALID_JPEG + b"A" * (2 * 1024 * 1024))
        upload_file = StarletteUploadFile(file=oversized_data, filename="stream.jpg")

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await read_upload_file_safely(upload_file, max_bytes=1024 * 1024)

        assert exc_info.value.status_code == 413
        assert "exceeds maximum allowed limit" in exc_info.value.detail


# ---------------------------------------------------------------------------
# 6. Error Sanitization Tests
# ---------------------------------------------------------------------------

class TestErrorSanitization:
    def test_sanitize_user_facing_error_scrubs_paths(self):
        msg_windows = r"Failed to open file at C:\Users\Admin\KiranaIQ\bills.db: database locked"
        scrubbed = sanitize_user_facing_error(msg_windows)
        assert r"C:\Users\Admin" not in scrubbed
        assert "[redacted-path]" in scrubbed

        msg_linux = "File not found: /var/data/app/media/secret.jpg on server"
        scrubbed_linux = sanitize_user_facing_error(msg_linux)
        assert "/var/data" not in scrubbed_linux
        assert "[redacted-path]" in scrubbed_linux

    def test_webhook_intake_error_is_sanitized(self, p3_client: TestClient, engine, monkeypatch):
        # Force WorkflowEngine.run_intake to raise an unexpected internal exception with file path
        async def fake_run_intake(*args, **kwargs):
            raise RuntimeError(r"Internal crash in C:\Windows\System32\driver.dll")

        monkeypatch.setattr(engine, "run_intake", fake_run_intake)

        # Set app secret and sign the request body with valid HMAC
        import hashlib
        import hmac
        from app.config import settings as global_settings

        secret = "test_webhook_secret_key"
        monkeypatch.setattr(global_settings, "whatsapp_app_secret", secret)
        body = b'{"entry": []}'
        sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

        resp = p3_client.post(
            "/webhook/whatsapp",
            content=body,
            headers={
                "X-Hub-Signature-256": f"sha256={sig}",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        # Must not expose the internal path or exception name
        assert "C:\\Windows" not in data["message"]
        assert "RuntimeError" not in data["message"]
        assert data["message"] == "Failed to process webhook intake"
