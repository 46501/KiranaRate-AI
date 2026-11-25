"""
tests/test_priority2_security.py
================================
Tests for Priority 2 Security:
- PBKDF2 password hashing & verification
- CSRF token generation & validation
- Dashboard session authentication (login, logout, session fixation prevention)
- Protected invoice media endpoints (removal of public static /media)
- Protected financial export endpoints (/export/csv & /export/tally)
- Protected sensitive REST API endpoints
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.api.dependencies import get_engine, get_settings, get_store
from app.config import Settings
from app.main import MEDIA_DIR, app
from app.models import (
    Bill,
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
from app.services.auth import (
    generate_csrf_token,
    hash_password,
    verify_csrf_token,
    verify_password,
)
from app.services.workflow_engine import WorkflowEngine
from app.storage.sqlite_store import SQLiteStore

UTC = timezone.utc


# ===========================================================================
# 1. Password Hashing & CSRF Unit Tests
# ===========================================================================

class TestPasswordHashingAndCsrf:
    def test_hash_password_format(self):
        pw = "KiranaSecure123!"
        hashed = hash_password(pw)
        assert hashed.startswith("pbkdf2:sha256:200000$")
        parts = hashed.split("$")
        assert len(parts) == 3
        salt_hex, key_hex = parts[1], parts[2]
        assert len(salt_hex) == 32  # 16 bytes
        assert len(key_hex) == 64   # 32 bytes SHA256

    def test_verify_password_valid(self):
        pw = "StoreManager2026#"
        hashed = hash_password(pw)
        assert verify_password(pw, hashed) is True

    def test_verify_password_invalid(self):
        pw = "CorrectPassword"
        hashed = hash_password(pw)
        assert verify_password("WrongPassword", hashed) is False

    def test_verify_password_plain_fallback(self):
        assert verify_password("plain_secret", "plain_secret") is True
        assert verify_password("wrong_secret", "plain_secret") is False

    def test_csrf_token_generation_and_validation(self):
        token1 = generate_csrf_token()
        token2 = generate_csrf_token()
        assert len(token1) == 64
        assert token1 != token2
        assert verify_csrf_token(token1, token1) is True
        assert verify_csrf_token(token1, token2) is False
        assert verify_csrf_token(token1, "") is False
        assert verify_csrf_token(None, token1) is False


# ===========================================================================
# 2. Dashboard Session Authentication & CSRF Integration Tests
# ===========================================================================

class TestDashboardAuthenticationAndSession:
    @pytest.fixture
    def auth_app(self, store: SQLiteStore, engine: WorkflowEngine):
        """TestClient configured with strict authentication enabled."""
        test_settings = Settings(
            dashboard_username="kirana_admin",
            dashboard_password="SecurePassword2026",
            session_secret_key="test-session-secret-key-32-bytes-long",
            session_cookie_secure=False,
            api_key="kirana_api_secret_key_123",
            sqlite_db_path=":memory:",
            mock_whatsapp=True,
            mock_gemini=True,
        )
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_engine] = lambda: engine
        app.dependency_overrides[get_settings] = lambda: test_settings
        with TestClient(app) as client:
            app.state.store = store
            app.state.engine = engine
            app.state.settings = test_settings
            yield client, test_settings
        app.dependency_overrides.clear()

    def test_unauthenticated_dashboard_redirects_to_login(self, auth_app):
        client, _ = auth_app
        response = client.get("/ui/bills", follow_redirects=False)
        assert response.status_code == 303
        assert "/ui/login" in response.headers["location"]

    def test_login_page_renders_with_csrf_token(self, auth_app):
        client, _ = auth_app
        response = client.get("/ui/login")
        assert response.status_code == 200
        assert "KiranaIQ" in response.text
        assert 'name="csrf_token"' in response.text

    def test_login_failure_with_wrong_password(self, auth_app):
        client, _ = auth_app
        # First fetch login page to seed CSRF token into session
        get_resp = client.get("/ui/login")
        # Extract CSRF token
        import re
        csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', get_resp.text)
        assert csrf_match is not None
        csrf_token = csrf_match.group(1)

        post_resp = client.post(
            "/ui/login",
            data={
                "username": "kirana_admin",
                "password": "WrongPassword!",
                "csrf_token": csrf_token,
            },
        )
        assert post_resp.status_code == 401
        assert "Invalid username or password" in post_resp.text

    def test_login_success_sets_session_and_redirects(self, auth_app):
        client, _ = auth_app
        get_resp = client.get("/ui/login")
        import re
        csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', get_resp.text)
        assert csrf_match is not None
        csrf_token = csrf_match.group(1)

        post_resp = client.post(
            "/ui/login",
            data={
                "username": "kirana_admin",
                "password": "SecurePassword2026",
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )
        assert post_resp.status_code == 303
        assert post_resp.headers["location"] == "/ui/bills"
        assert "kirana_session" in client.cookies

        # Subsequent GET /ui/bills is authorized!
        dash_resp = client.get("/ui/bills")
        assert dash_resp.status_code == 200
        assert "KiranaIQ" in dash_resp.text

    def test_logout_clears_session_and_redirects(self, auth_app):
        client, _ = auth_app
        # Login first
        get_resp = client.get("/ui/login")
        import re
        csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', get_resp.text).group(1)
        client.post(
            "/ui/login",
            data={"username": "kirana_admin", "password": "SecurePassword2026", "csrf_token": csrf_token},
        )
        assert client.get("/ui/bills").status_code == 200

        # Logout
        logout_resp = client.get("/ui/logout", follow_redirects=False)
        assert logout_resp.status_code == 303
        assert "/ui/login" in logout_resp.headers["location"]

        # Subsequent visit to dashboard is redirected back to login
        dash_after_logout = client.get("/ui/bills", follow_redirects=False)
        assert dash_after_logout.status_code == 303
        assert "/ui/login" in dash_after_logout.headers["location"]


# ===========================================================================
# 3. CSRF Protection on State-Changing Dashboard Forms
# ===========================================================================

class TestCsrfProtection:
    @pytest.fixture
    def auth_client_with_bill(self, store: SQLiteStore, engine: WorkflowEngine, make_bill):
        test_settings = Settings(
            dashboard_username="admin",
            dashboard_password="adminpassword",
            session_secret_key="csrf-test-secret-key-32-bytes-long",
            session_cookie_secure=False,
            api_key="api_key_test",
            sqlite_db_path=":memory:",
            mock_whatsapp=True,
            mock_gemini=True,
        )
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_engine] = lambda: engine
        app.dependency_overrides[get_settings] = lambda: test_settings
        with TestClient(app) as client:
            app.state.store = store
            app.state.engine = engine
            app.state.settings = test_settings

            # Login
            get_resp = client.get("/ui/login")
            import re
            csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', get_resp.text).group(1)
            client.post(
                "/ui/login",
                data={"username": "admin", "password": "adminpassword", "csrf_token": csrf_token},
            )

            bill = make_bill(bill_id="bill_csrf_test", status=BillStatus.REVIEW)
            yield client, bill
        app.dependency_overrides.clear()

    def test_post_worker_without_csrf_fails_with_403(self, auth_client_with_bill):
        client, _ = auth_client_with_bill
        # No CSRF token provided
        response = client.post("/ui/workers/run/scan")
        assert response.status_code == 403
        assert "CSRF" in response.json()["detail"]

    def test_post_worker_with_valid_csrf_succeeds(self, auth_client_with_bill):
        client, _ = auth_client_with_bill
        dash = client.get("/ui/bills")
        import re
        csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', dash.text).group(1)

        response = client.post(
            "/ui/workers/run/scan",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert response.status_code == 303


# ===========================================================================
# 4. Invoice Image Protection
# ===========================================================================

class TestInvoiceImageProtection:
    @pytest.fixture
    def setup_image_env(self, store: SQLiteStore, engine: WorkflowEngine, make_bill):
        test_settings = Settings(
            dashboard_username="admin",
            dashboard_password="adminpassword",
            session_secret_key="img-test-secret-key-32-bytes-long",
            api_key="secure_api_key_456",
            sqlite_db_path=":memory:",
            mock_whatsapp=True,
            mock_gemini=True,
        )
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_engine] = lambda: engine
        app.dependency_overrides[get_settings] = lambda: test_settings

        # Create dummy image on disk
        bill_id = "wamid.PROTECTED_IMG_001"
        test_img_path = MEDIA_DIR / f"{bill_id}.jpg"
        test_img_path.write_bytes(b"\xFF\xD8\xFF\xE0fake_jpeg_data")

        bill = make_bill(bill_id=bill_id, status=BillStatus.REVIEW)

        with TestClient(app) as client:
            app.state.store = store
            app.state.engine = engine
            app.state.settings = test_settings
            yield client, test_settings, bill
        app.dependency_overrides.clear()

        # Cleanup
        if test_img_path.exists():
            test_img_path.unlink()

    def test_public_static_media_mount_removed(self, setup_image_env):
        client, _, bill = setup_image_env
        # Direct /media/ filename access must return 404 (static mount removed)
        resp = client.get(f"/media/{bill.bill_id}.jpg")
        assert resp.status_code == 404

    def test_unauthenticated_media_access_returns_401(self, setup_image_env):
        client, _, bill = setup_image_env
        resp = client.get(f"/ui/media/{bill.bill_id}")
        assert resp.status_code == 401
        assert "Authentication required" in resp.json()["detail"]

    def test_authenticated_session_can_access_media(self, setup_image_env):
        client, _, bill = setup_image_env
        # Login
        get_resp = client.get("/ui/login")
        import re
        csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', get_resp.text).group(1)
        client.post("/ui/login", data={"username": "admin", "password": "adminpassword", "csrf_token": csrf_token})

        # Request media with session
        resp = client.get(f"/ui/media/{bill.bill_id}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content.startswith(b"\xFF\xD8\xFF")

    def test_api_key_authenticated_can_access_media(self, setup_image_env):
        client, settings, bill = setup_image_env
        resp = client.get(
            f"/ui/media/{bill.bill_id}",
            headers={"Authorization": f"Bearer {settings.api_key}"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"


# ===========================================================================
# 5. Financial Exports & Sensitive API Protection
# ===========================================================================

class TestExportsAndApiProtection:
    @pytest.fixture
    def secure_api_env(self, store: SQLiteStore, engine: WorkflowEngine, make_bill):
        store.upsert_supplier(Supplier(supplier_id="SUP_TEST", supplier_name="Test Wholesale", active=SupplierActive.YES))
        store.upsert_product(Product(sku="BISCUIT-10", product_name="Test Biscuit", base_unit="PACK", active=ProductActive.YES))
        bill = make_bill(bill_id="wamid.API_AUTH_TEST_001", status=BillStatus.REVIEW, supplier_id="SUP_TEST")

        test_settings = Settings(
            dashboard_username="admin",
            dashboard_password="adminpassword",
            session_secret_key="api-test-secret-key-32-bytes-long",
            api_key="super_secret_rest_api_key_999",
            sqlite_db_path=":memory:",
            mock_whatsapp=True,
            mock_gemini=True,
        )
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_engine] = lambda: engine
        app.dependency_overrides[get_settings] = lambda: test_settings
        with TestClient(app) as client:
            app.state.store = store
            app.state.engine = engine
            app.state.settings = test_settings
            yield client, test_settings, bill
        app.dependency_overrides.clear()

    def test_unauthenticated_export_csv_returns_401(self, secure_api_env):
        client, _, _ = secure_api_env
        resp = client.get("/export/csv")
        assert resp.status_code == 401
        assert "Authentication required" in resp.json()["detail"]

    def test_unauthenticated_export_tally_returns_401(self, secure_api_env):
        client, _, _ = secure_api_env
        resp = client.get("/export/tally")
        assert resp.status_code == 401
        assert "Authentication required" in resp.json()["detail"]

    def test_export_csv_with_valid_api_key_succeeds(self, secure_api_env):
        client, settings, _ = secure_api_env
        resp = client.get(
            "/export/csv",
            headers={"Authorization": f"Bearer {settings.api_key}"},
        )
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]

    def test_export_tally_with_valid_api_key_succeeds(self, secure_api_env):
        client, settings, _ = secure_api_env
        resp = client.get(
            "/export/tally",
            headers={"X-API-Key": settings.api_key},
        )
        assert resp.status_code == 200
        assert "xml" in resp.headers["content-type"]

    def test_unauthenticated_bill_approve_returns_401(self, secure_api_env):
        client, _, bill = secure_api_env
        resp = client.post(
            f"/bills/{bill.bill_id}/approve",
            json={
                "reviewer": "manager",
                "supplier_id": "SUP_TEST",
                "cost_basis": "EX_GST",
                "invoice_no": "INV-123",
                "invoice_date": "2026-11-20",
                "currency": "INR",
                "verified_net_total": "500.00",
                "header_verified": "YES",
            },
        )
        assert resp.status_code == 401

    def test_bill_approve_with_valid_api_key_succeeds(self, secure_api_env):
        client, settings, bill = secure_api_env
        resp = client.post(
            f"/bills/{bill.bill_id}/approve",
            headers={"Authorization": f"Bearer {settings.api_key}"},
            json={
                "reviewer": "manager",
                "supplier_id": "SUP_TEST",
                "cost_basis": "EX_GST",
                "invoice_no": "INV-123",
                "invoice_date": "2026-11-20",
                "currency": "INR",
                "verified_net_total": "500.00",
                "header_verified": "YES",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "APPROVED"
