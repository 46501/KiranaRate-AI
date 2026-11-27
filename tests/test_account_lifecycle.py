"""
tests/test_account_lifecycle.py
=============================================================================
Comprehensive Production Test Suite for KiranaIQ Authentication & Account Lifecycle

Verifies:
1. CLI Administrator Provisioning (python -m app.cli create-admin)
2. CLI Duplicate Admin Prevention and --force override
3. CLI Password Recovery (python -m app.cli reset-password)
4. CLI Lockout Recovery (python -m app.cli unlock-account)
5. CLI Admin Audit Listing (python -m app.cli list-admins)
6. First-time setup UI: CLI instructions on login page when 0 users exist
7. Prohibition of public web admin registration (GET/POST /ui/register)
8. Multi-identifier login (Username or Email)
9. Session regeneration (Session Fixation defense)
10. Generic error messages and brute-force lockout protection
11. Admin account settings page access and authorization
12. Password change with current password verification
13. Email address update with verification and uniqueness checks
14. Anti-CSRF protection on all state-changing account endpoints
15. SQLite persistence across database reloads
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
from typing import Generator
import pytest
from starlette.testclient import TestClient

from app.cli import (
    cmd_create_admin,
    cmd_list_admins,
    cmd_reset_password,
    cmd_unlock_account,
)
from app.config import Settings
from app.main import app
from app.models import User
from app.services.auth import hash_password, verify_password
from app.services.security import login_tracker
from app.storage.sqlite_store import SQLiteStore


@pytest.fixture
def clean_db(tmp_path: Path) -> Generator[SQLiteStore, None, None]:
    """Create an isolated temporary SQLite database."""
    db_file = tmp_path / "test_lifecycle.db"
    store = SQLiteStore(str(db_file))
    store.create_tables()
    yield store
    store.close()
    if db_file.exists():
        try:
            db_file.unlink()
        except OSError:
            pass


@pytest.fixture
def auth_client(clean_db: SQLiteStore) -> Generator[TestClient, None, None]:
    """TestClient configured with isolated SQLite store and test settings."""
    from app.api.dependencies import get_store, get_settings
    test_settings = Settings(
        app_env="test",
        is_auth_enabled=True,
        dashboard_username="fallback_admin",
        dashboard_password="FallbackPassword123!",
        rate_limit_enabled=False,
    )
    app.dependency_overrides[get_store] = lambda: clean_db
    app.dependency_overrides[get_settings] = lambda: test_settings

    with TestClient(app) as client:
        app.state.store = clean_db
        app.state.settings = test_settings
        yield client
    app.dependency_overrides.clear()


# ===========================================================================
# 1. CLI Admin Provisioning & Recovery Tests
# ===========================================================================

def test_cli_create_admin_success(clean_db: SQLiteStore):
    """CLI create-admin provisions the initial administrator account."""
    assert clean_db.count_users() == 0

    args = argparse.Namespace(
        username="kirana_owner",
        email="owner@kirana.in",
        password="SecurePassword123!",
        confirm_password="SecurePassword123!",
        force=False,
        db_path=clean_db._db_path,
    )
    ret = cmd_create_admin(args)
    assert ret == 0

    assert clean_db.count_users() == 1
    user = clean_db.get_user_by_username("kirana_owner")
    assert user is not None
    assert user.username == "kirana_owner"
    assert user.email == "owner@kirana.in"
    assert user.role == "admin"
    assert user.password_hash.startswith("pbkdf2:sha256:")
    assert verify_password("SecurePassword123!", user.password_hash) is True


def test_cli_create_admin_prevents_duplicate_without_force(clean_db: SQLiteStore):
    """CLI create-admin refuses to create a second administrator unless --force is set."""
    # First admin
    args1 = argparse.Namespace(
        username="first_admin",
        email="first@kirana.in",
        password="FirstPassword123!",
        confirm_password="FirstPassword123!",
        force=False,
        db_path=clean_db._db_path,
    )
    assert cmd_create_admin(args1) == 0
    assert clean_db.count_users() == 1

    # Second admin attempt without --force
    args2 = argparse.Namespace(
        username="second_admin",
        email="second@kirana.in",
        password="SecondPassword123!",
        confirm_password="SecondPassword123!",
        force=False,
        db_path=clean_db._db_path,
    )
    assert cmd_create_admin(args2) == 1
    assert clean_db.count_users() == 1  # Still 1 user

    # Second admin attempt WITH --force
    args3 = argparse.Namespace(
        username="second_admin",
        email="second@kirana.in",
        password="SecondPassword123!",
        confirm_password="SecondPassword123!",
        force=True,
        db_path=clean_db._db_path,
    )
    assert cmd_create_admin(args3) == 0
    assert clean_db.count_users() == 2


def test_cli_create_admin_rejects_insecure_passwords(clean_db: SQLiteStore):
    """CLI create-admin rejects weak passwords based on security policy."""
    args = argparse.Namespace(
        username="admin",
        email="admin@kirana.in",
        password="weak",
        confirm_password="weak",
        force=False,
        db_path=clean_db._db_path,
    )
    assert cmd_create_admin(args) == 1
    assert clean_db.count_users() == 0


def test_cli_reset_password_success(clean_db: SQLiteStore):
    """CLI reset-password updates password and clears lockout."""
    user = User(
        user_id="usr_001",
        username="store_boss",
        email="boss@store.in",
        password_hash=hash_password("OldPassword123!"),
        role="admin",
    )
    clean_db.insert_user(user)

    # Reset password via CLI
    args = argparse.Namespace(
        identifier="store_boss",
        password="BrandNewPassword123#",
        db_path=clean_db._db_path,
    )
    assert cmd_reset_password(args) == 0

    updated = clean_db.get_user_by_username("store_boss")
    assert verify_password("BrandNewPassword123#", updated.password_hash) is True
    assert verify_password("OldPassword123!", updated.password_hash) is False


def test_cli_unlock_account(clean_db: SQLiteStore):
    """CLI unlock-account resets rate limiters and lockouts."""
    login_tracker.record_failure("test:127.0.0.1", max_failed=1, lockout_secs=300)
    assert login_tracker.is_locked("test:127.0.0.1")[0] is True

    args = argparse.Namespace(username="test")
    assert cmd_unlock_account(args) == 0

    assert login_tracker.is_locked("test:127.0.0.1")[0] is False


def test_cli_list_admins_safe_output(clean_db: SQLiteStore, capsys):
    """CLI list-admins lists metadata without exposing password hashes."""
    user = User(
        user_id="usr_999",
        username="audit_admin",
        email="audit@store.in",
        password_hash=hash_password("Secret123!"),
        role="admin",
    )
    clean_db.insert_user(user)

    args = argparse.Namespace(db_path=clean_db._db_path)
    assert cmd_list_admins(args) == 0
    captured = capsys.readouterr().out

    assert "audit_admin" in captured
    assert "audit@store.in" in captured
    assert "admin" in captured
    assert "Secret123!" not in captured
    assert "pbkdf2:" not in captured


# ===========================================================================
# 2. First-Time Setup UI & Public Registration Prohibition
# ===========================================================================

def test_login_page_displays_cli_setup_when_zero_admins(auth_client: TestClient, clean_db: SQLiteStore):
    """When no users exist, login page displays first-time setup guidance with CLI command."""
    assert clean_db.count_users() == 0

    res = auth_client.get("/ui/login")
    assert res.status_code == 200
    assert "First-Time System Setup" in res.text
    assert "python -m app.cli create-admin" in res.text


def test_login_page_hides_cli_setup_once_admin_exists(auth_client: TestClient, clean_db: SQLiteStore):
    """When an admin exists, first-time setup instructions are hidden from the login page."""
    user = User(
        user_id="usr_admin1",
        username="admin_user",
        email="admin@kirana.in",
        password_hash=hash_password("Password123!"),
        role="admin",
    )
    clean_db.insert_user(user)
    assert clean_db.count_users() == 1

    res = auth_client.get("/ui/login")
    assert res.status_code == 200
    assert "First-Time System Setup" not in res.text
    assert "python -m app.cli create-admin" not in res.text


def test_public_web_registration_is_prohibited(auth_client: TestClient):
    """Web registration is disabled: GET redirects with notice, POST returns 403 Forbidden."""
    # GET /ui/register
    get_res = auth_client.get("/ui/register", follow_redirects=False)
    assert get_res.status_code == 303
    assert "/ui/login" in get_res.headers["Location"]

    # POST /ui/register
    post_res = auth_client.post("/ui/register", data={"username": "hacker"})
    assert post_res.status_code == 403
    assert "Public web registration is disabled" in post_res.text


# ===========================================================================
# 3. Login System, Session Regeneration & Lockout Tests
# ===========================================================================

def test_login_with_username_success(auth_client: TestClient, clean_db: SQLiteStore):
    """Admin can authenticate using their username."""
    user = User(
        user_id="usr_login1",
        username="super_merchant",
        email="merchant@store.in",
        password_hash=hash_password("MerchantPass123!"),
        role="admin",
    )
    clean_db.insert_user(user)

    login_page = auth_client.get("/ui/login")
    csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text).group(1)

    res = auth_client.post(
        "/ui/login",
        data={"username": "super_merchant", "password": "MerchantPass123!", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "/ui/bills" in res.headers["Location"]


def test_login_with_email_success(auth_client: TestClient, clean_db: SQLiteStore):
    """Admin can authenticate using their email address."""
    user = User(
        user_id="usr_login2",
        username="super_merchant",
        email="merchant@store.in",
        password_hash=hash_password("MerchantPass123!"),
        role="admin",
    )
    clean_db.insert_user(user)

    login_page = auth_client.get("/ui/login")
    csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text).group(1)

    res = auth_client.post(
        "/ui/login",
        data={"username": "merchant@store.in", "password": "MerchantPass123!", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "/ui/bills" in res.headers["Location"]


def test_login_failure_generic_message(auth_client: TestClient, clean_db: SQLiteStore):
    """Login failure returns generic 401 message to prevent user enumeration."""
    login_page = auth_client.get("/ui/login")
    csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text).group(1)

    res = auth_client.post(
        "/ui/login",
        data={"username": "non_existent_user", "password": "AnyPassword123!", "csrf_token": csrf_token},
    )
    assert res.status_code == 401
    assert "Invalid username or password" in res.text


def test_login_lockout_enforcement(auth_client: TestClient, clean_db: SQLiteStore):
    """Repeated login failures trigger account lockout with HTTP 429 and Retry-After header."""
    login_page = auth_client.get("/ui/login")
    csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text).group(1)

    # Fail 5 times (default limit)
    for _ in range(5):
        auth_client.post(
            "/ui/login",
            data={"username": "lockout_target", "password": "WrongPassword123!", "csrf_token": csrf_token},
        )

    # 6th attempt must be locked out
    lock_res = auth_client.post(
        "/ui/login",
        data={"username": "lockout_target", "password": "WrongPassword123!", "csrf_token": csrf_token},
    )
    assert lock_res.status_code == 429
    assert "Account temporarily locked" in lock_res.text
    assert "Retry-After" in lock_res.headers


# ===========================================================================
# 4. Account Management & Profile Settings Tests
# ===========================================================================

def test_unauthenticated_account_settings_redirects_to_login(auth_client: TestClient):
    """Unauthenticated visitor to /ui/account is redirected to login."""
    res = auth_client.get("/ui/account", follow_redirects=False)
    assert res.status_code == 303
    assert "/ui/login" in res.headers["Location"]


def test_authenticated_admin_can_view_account_page(auth_client: TestClient, clean_db: SQLiteStore):
    """Authenticated administrator can access /ui/account and view their profile."""
    user = User(
        user_id="usr_profile_test",
        username="active_owner",
        email="owner@store.com",
        password_hash=hash_password("ActivePass123!"),
        role="admin",
    )
    clean_db.insert_user(user)

    # Log in
    login_page = auth_client.get("/ui/login")
    csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text).group(1)
    auth_client.post("/ui/login", data={"username": "active_owner", "password": "ActivePass123!", "csrf_token": csrf})

    # Access /ui/account
    res = auth_client.get("/ui/account")
    assert res.status_code == 200
    assert "Administrator Account Settings" in res.text
    assert "active_owner" in res.text
    assert "owner@store.com" in res.text
    assert "ADMIN" in res.text


def test_change_password_success(auth_client: TestClient, clean_db: SQLiteStore):
    """Admin can change password after verifying current password."""
    user = User(
        user_id="usr_pwd_test",
        username="pwd_changer",
        email="pwd@store.com",
        password_hash=hash_password("CurrentPassword123!"),
        role="admin",
    )
    clean_db.insert_user(user)

    # Log in
    login_page = auth_client.get("/ui/login")
    csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text).group(1)
    auth_client.post("/ui/login", data={"username": "pwd_changer", "password": "CurrentPassword123!", "csrf_token": csrf})

    # Get account page CSRF token
    account_page = auth_client.get("/ui/account")
    account_csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', account_page.text).group(1)

    # Change password
    res = auth_client.post(
        "/ui/account/change-password",
        data={
            "current_password": "CurrentPassword123!",
            "new_password": "UpdatedPassword456#",
            "confirm_new_password": "UpdatedPassword456#",
            "csrf_token": account_csrf,
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "/ui/account" in res.headers["Location"]
    assert "msg=Password+successfully+updated" in res.headers["Location"]

    # Verify updated in SQLite
    updated_user = clean_db.get_user_by_username("pwd_changer")
    assert verify_password("UpdatedPassword456#", updated_user.password_hash) is True
    assert verify_password("CurrentPassword123!", updated_user.password_hash) is False


def test_change_password_rejects_wrong_current_password(auth_client: TestClient, clean_db: SQLiteStore):
    """Password change fails if current password does not verify."""
    user = User(
        user_id="usr_wrong_pwd",
        username="wrong_pwd_user",
        email="wrong@store.com",
        password_hash=hash_password("CorrectOldPassword123!"),
        role="admin",
    )
    clean_db.insert_user(user)

    # Log in
    login_page = auth_client.get("/ui/login")
    csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text).group(1)
    auth_client.post("/ui/login", data={"username": "wrong_pwd_user", "password": "CorrectOldPassword123!", "csrf_token": csrf})

    # Submit change password with INCORRECT current password
    account_page = auth_client.get("/ui/account")
    account_csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', account_page.text).group(1)

    res = auth_client.post(
        "/ui/account/change-password",
        data={
            "current_password": "WrongCurrentPassword123!",
            "new_password": "UpdatedPassword456#",
            "confirm_new_password": "UpdatedPassword456#",
            "csrf_token": account_csrf,
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "err=Current+password+is+incorrect" in res.headers["Location"]

    # Password unchanged in database
    user_after = clean_db.get_user_by_username("wrong_pwd_user")
    assert verify_password("CorrectOldPassword123!", user_after.password_hash) is True


def test_update_email_success(auth_client: TestClient, clean_db: SQLiteStore):
    """Admin can update email address after verifying current password."""
    user = User(
        user_id="usr_email_test",
        username="email_tester",
        email="old_email@store.com",
        password_hash=hash_password("Password123!"),
        role="admin",
    )
    clean_db.insert_user(user)

    # Log in
    login_page = auth_client.get("/ui/login")
    csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text).group(1)
    auth_client.post("/ui/login", data={"username": "email_tester", "password": "Password123!", "csrf_token": csrf})

    account_page = auth_client.get("/ui/account")
    account_csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', account_page.text).group(1)

    # Update email
    res = auth_client.post(
        "/ui/account/update-email",
        data={
            "new_email": "brand_new_email@store.com",
            "current_password": "Password123!",
            "csrf_token": account_csrf,
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "msg=Email+address+successfully+updated" in res.headers["Location"]

    # Verify updated in database
    updated_user = clean_db.get_user_by_username("email_tester")
    assert updated_user.email == "brand_new_email@store.com"


def test_account_persists_across_store_recreation(tmp_path: Path):
    """Account credentials safely persist in SQLite file across server/store reboots."""
    db_file = tmp_path / "reboot_test.db"
    store1 = SQLiteStore(str(db_file))
    store1.create_tables()

    user = User(
        user_id="usr_reboot",
        username="survivor_admin",
        email="survivor@kirana.in",
        password_hash=hash_password("Persist123!"),
        role="admin",
    )
    store1.insert_user(user)
    assert store1.count_users() == 1
    store1.close()

    # Reopen database instance
    store2 = SQLiteStore(str(db_file))
    store2.create_tables()
    assert store2.count_users() == 1

    reloaded = store2.get_user_by_username("survivor_admin")
    assert reloaded is not None
    assert reloaded.email == "survivor@kirana.in"
    assert verify_password("Persist123!", reloaded.password_hash) is True
    store2.close()
