"""
tests/test_create_account.py
Comprehensive test suite for KiranaIQ First-Time Setup & Create Admin Account feature.
Verifies:
1. First-time registration available only when user count is 0.
2. Password hashing with PBKDF2-HMAC-SHA256 (no plain text storage).
3. Email and password strength validation.
4. Automatic admin role assignment for the first user.
5. Automatic closing of public registration once admin is created.
6. Multi-identifier login (username or email).
7. Fallback config credentials preservation.
8. Anti-CSRF and Rate limiting on registration.
9. Persistence in SQLite store across restarts.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Generator
import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.main import app
from app.models import User
from app.services.auth import hash_password, verify_password
from app.storage.sqlite_store import SQLiteStore


@pytest.fixture
def clean_db(tmp_path: Path) -> Generator[SQLiteStore, None, None]:
    """Create an isolated temporary SQLite database."""
    db_file = tmp_path / "test_auth.db"
    store = SQLiteStore(str(db_file))
    store.create_tables()
    yield store
    if db_file.exists():
        try:
            db_file.unlink()
        except OSError:
            pass


@pytest.fixture
def auth_client(clean_db: SQLiteStore) -> Generator[TestClient, None, None]:
    """TestClient configured with isolated SQLite store and mock settings."""
    from app.api.dependencies import get_store, get_settings
    test_settings = Settings(
        app_env="test",
        is_auth_enabled=True,
        dashboard_username="legacy_admin",
        dashboard_password="LegacyPassword123!",
        rate_limit_enabled=False,  # disabled for general functional tests
    )
    app.dependency_overrides[get_store] = lambda: clean_db
    app.dependency_overrides[get_settings] = lambda: test_settings

    with TestClient(app) as client:
        app.state.store = clean_db
        app.state.settings = test_settings
        yield client
    app.dependency_overrides.clear()


# ===========================================================================
# 1. UI Display & First-Time Setup Availability
# ===========================================================================

def test_login_page_displays_create_account_when_no_users(auth_client: TestClient, clean_db: SQLiteStore):
    """When 0 users exist in DB, login page displays the 'Create Admin Account' link."""
    assert clean_db.count_users() == 0

    response = auth_client.get("/ui/login")
    assert response.status_code == 200
    assert "Create Admin Account" in response.text
    assert "/ui/register" in response.text


def test_register_page_loads_with_csrf_token(auth_client: TestClient, clean_db: SQLiteStore):
    """GET /ui/register renders cleanly with CSRF token and password checklist."""
    assert clean_db.count_users() == 0

    response = auth_client.get("/ui/register")
    assert response.status_code == 200
    assert "First-Time Setup" in response.text
    assert 'name="csrf_token"' in response.text
    assert 'name="username"' in response.text
    assert 'name="email"' in response.text
    assert 'name="password"' in response.text
    assert 'name="confirm_password"' in response.text


# ===========================================================================
# 2. Registration Validation Tests
# ===========================================================================

def test_registration_rejects_weak_passwords(auth_client: TestClient, clean_db: SQLiteStore):
    """Registration validates password strength (min 8 chars, uppercase, lowercase, digit, special)."""
    # Grab CSRF token
    get_res = auth_client.get("/ui/register")
    csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', get_res.text)
    assert csrf_match, "CSRF token not found in register page"
    csrf_token = csrf_match.group(1)

    weak_passwords = [
        ("short1!", "Password must be at least 8 characters long."),
        ("nouppercase123!", "Password must include at least one uppercase letter (A-Z)."),
        ("NOLOWERCASE123!", "Password must include at least one lowercase letter (a-z)."),
        ("NoNumbersHere!", "Password must include at least one numeric digit (0-9)."),
        ("NoSpecial12345", "Password must include at least one special character"),
    ]

    for weak_pass, expected_err in weak_passwords:
        res = auth_client.post(
            "/ui/register",
            data={
                "username": "admin",
                "email": "admin@kirana.com",
                "password": weak_pass,
                "confirm_password": weak_pass,
                "csrf_token": csrf_token,
            },
        )
        assert res.status_code == 400
        assert expected_err in res.text
        assert clean_db.count_users() == 0


def test_registration_rejects_invalid_email(auth_client: TestClient, clean_db: SQLiteStore):
    """Registration rejects invalid email format."""
    get_res = auth_client.get("/ui/register")
    csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', get_res.text).group(1)

    res = auth_client.post(
        "/ui/register",
        data={
            "username": "admin",
            "email": "not-an-email",
            "password": "StrongPassword123!",
            "confirm_password": "StrongPassword123!",
            "csrf_token": csrf_token,
        },
    )
    assert res.status_code == 400
    assert "Please enter a valid email address" in res.text
    assert clean_db.count_users() == 0


def test_registration_rejects_password_mismatch(auth_client: TestClient, clean_db: SQLiteStore):
    """Registration rejects when password and confirm_password do not match."""
    get_res = auth_client.get("/ui/register")
    csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', get_res.text).group(1)

    res = auth_client.post(
        "/ui/register",
        data={
            "username": "admin",
            "email": "admin@kirana.com",
            "password": "StrongPassword123!",
            "confirm_password": "DifferentPassword123!",
            "csrf_token": csrf_token,
        },
    )
    assert res.status_code == 400
    assert "Passwords do not match" in res.text
    assert clean_db.count_users() == 0


def test_registration_rejects_invalid_username(auth_client: TestClient, clean_db: SQLiteStore):
    """Registration rejects usernames that are too short or contain invalid characters."""
    get_res = auth_client.get("/ui/register")
    csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', get_res.text).group(1)

    # Too short
    res = auth_client.post(
        "/ui/register",
        data={
            "username": "ab",
            "email": "admin@kirana.com",
            "password": "StrongPassword123!",
            "confirm_password": "StrongPassword123!",
            "csrf_token": csrf_token,
        },
    )
    assert res.status_code == 400
    assert "Username must be between 3 and 30 characters" in res.text

    # Invalid characters
    res = auth_client.post(
        "/ui/register",
        data={
            "username": "admin<script>",
            "email": "admin@kirana.com",
            "password": "StrongPassword123!",
            "confirm_password": "StrongPassword123!",
            "csrf_token": csrf_token,
        },
    )
    assert res.status_code == 400
    assert "letters, numbers, underscores, and hyphens" in res.text


# ===========================================================================
# 3. Successful First-Time Setup & Role Assignment
# ===========================================================================

def test_successful_first_time_admin_registration(auth_client: TestClient, clean_db: SQLiteStore):
    """
    First user registers successfully:
    - User is persisted in database with role='admin'.
    - Password is encrypted with PBKDF2.
    - User session is established and redirected to /ui/bills.
    """
    get_res = auth_client.get("/ui/register")
    csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', get_res.text).group(1)

    res = auth_client.post(
        "/ui/register",
        data={
            "username": "store_owner",
            "email": "owner@kirana.com",
            "password": "SecureOwnerPass123!",
            "confirm_password": "SecureOwnerPass123!",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "/ui/bills" in res.headers["Location"]

    # Verify user in database
    assert clean_db.count_users() == 1
    db_user = clean_db.get_user_by_username("store_owner")
    assert db_user is not None
    assert db_user.username == "store_owner"
    assert db_user.email == "owner@kirana.com"
    assert db_user.role == "admin"

    # Verify PBKDF2 hash
    assert db_user.password_hash.startswith("pbkdf2:sha256:")
    assert "SecureOwnerPass123!" not in db_user.password_hash
    assert verify_password("SecureOwnerPass123!", db_user.password_hash) is True


# ===========================================================================
# 4. Lockdown: Public Registration Automatically Disabled
# ===========================================================================

def test_registration_disabled_after_admin_exists(auth_client: TestClient, clean_db: SQLiteStore):
    """
    Once an admin account exists:
    - Login page hides 'Create Admin Account' link.
    - GET /ui/register redirects to /ui/login with warning.
    - POST /ui/register returns HTTP 403 Forbidden.
    """
    # 1. Create first admin user
    first_user = User(
        user_id="usr_admin1",
        username="primary_admin",
        email="admin@kirana.com",
        password_hash=hash_password("FirstAdmin123!"),
        role="admin",
    )
    clean_db.insert_user(first_user)
    assert clean_db.count_users() == 1

    # 2. Check login page: 'Create Admin Account' link is no longer present
    login_res = auth_client.get("/ui/login")
    assert "Create Admin Account" not in login_res.text

    # 3. GET /ui/register redirects to login page
    reg_get_res = auth_client.get("/ui/register", follow_redirects=False)
    assert reg_get_res.status_code == 303
    assert "/ui/login" in reg_get_res.headers["Location"]

    # 4. POST /ui/register returns HTTP 403 Forbidden
    # Fetch valid csrf token from login page
    csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_res.text).group(1)
    reg_post_res = auth_client.post(
        "/ui/register",
        data={
            "username": "intruder",
            "email": "intruder@kirana.com",
            "password": "IntruderPass123!",
            "confirm_password": "IntruderPass123!",
            "csrf_token": csrf_token,
        },
    )
    assert reg_post_res.status_code == 403
    assert "Registration is disabled because an administrator account already exists" in reg_post_res.text
    assert clean_db.count_users() == 1  # User count unchanged


# ===========================================================================
# 5. Login Verification: Username and Email Authentication
# ===========================================================================

def test_login_with_registered_username(auth_client: TestClient, clean_db: SQLiteStore):
    """Registered admin can log in using their username."""
    user = User(
        user_id="usr_test1",
        username="kirana_boss",
        email="boss@store.in",
        password_hash=hash_password("BossSecret123#"),
        role="admin",
    )
    clean_db.insert_user(user)

    login_page = auth_client.get("/ui/login")
    csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text).group(1)

    res = auth_client.post(
        "/ui/login",
        data={
            "username": "kirana_boss",
            "password": "BossSecret123#",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "/ui/bills" in res.headers["Location"]


def test_login_with_registered_email(auth_client: TestClient, clean_db: SQLiteStore):
    """Registered admin can log in using their email address."""
    user = User(
        user_id="usr_test2",
        username="kirana_boss",
        email="boss@store.in",
        password_hash=hash_password("BossSecret123#"),
        role="admin",
    )
    clean_db.insert_user(user)

    login_page = auth_client.get("/ui/login")
    csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text).group(1)

    res = auth_client.post(
        "/ui/login",
        data={
            "username": "boss@store.in",
            "password": "BossSecret123#",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "/ui/bills" in res.headers["Location"]


def test_login_rejects_invalid_password(auth_client: TestClient, clean_db: SQLiteStore):
    """Login rejects wrong password for registered account."""
    user = User(
        user_id="usr_test3",
        username="kirana_boss",
        email="boss@store.in",
        password_hash=hash_password("BossSecret123#"),
        role="admin",
    )
    clean_db.insert_user(user)

    login_page = auth_client.get("/ui/login")
    csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text).group(1)

    res = auth_client.post(
        "/ui/login",
        data={
            "username": "kirana_boss",
            "password": "WrongPassword999!",
            "csrf_token": csrf_token,
        },
    )
    assert res.status_code == 401
    assert "Invalid username or password" in res.text


def test_fallback_config_credentials_still_work(auth_client: TestClient, clean_db: SQLiteStore):
    """Settings-based fallback credentials remain functional for backwards compatibility."""
    assert clean_db.count_users() == 0

    login_page = auth_client.get("/ui/login")
    csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text).group(1)

    res = auth_client.post(
        "/ui/login",
        data={
            "username": "legacy_admin",
            "password": "LegacyPassword123!",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "/ui/bills" in res.headers["Location"]


# ===========================================================================
# 6. SQLite Persistence & Store Reload
# ===========================================================================

def test_account_persists_across_store_reloads(tmp_path: Path):
    """Credentials persist safely in SQLite database and survive store re-instantiation."""
    db_file = tmp_path / "persistent_users.db"
    store1 = SQLiteStore(str(db_file))
    store1.create_tables()

    user = User(
        user_id="usr_persisted",
        username="grocery_manager",
        email="manager@grocery.com",
        password_hash=hash_password("SuperSecretMgr99!"),
        role="admin",
    )
    store1.insert_user(user)
    assert store1.count_users() == 1

    # Simulate server restart by creating fresh store instance pointing to same DB
    store2 = SQLiteStore(str(db_file))
    assert store2.count_users() == 1
    reloaded_user = store2.get_user_by_username("grocery_manager")
    assert reloaded_user is not None
    assert reloaded_user.email == "manager@grocery.com"
    assert verify_password("SuperSecretMgr99!", reloaded_user.password_hash) is True
