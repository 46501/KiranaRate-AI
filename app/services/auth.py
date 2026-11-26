"""
app/services/auth.py
====================
Authentication, password hashing, and CSRF utilities for KiranaIQ.
- Secure PBKDF2-HMAC-SHA256 password hashing & constant-time verification.
- Anti-CSRF token generation & constant-time validation.
- Session authentication verification.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Optional, Tuple


# Default PBKDF2 iterations (OWASP recommended range)
_PBKDF2_ROUNDS = 200_000


def hash_password(password: str) -> str:
    """
    Generate a cryptographically secure PBKDF2-HMAC-SHA256 password hash.

    Format: pbkdf2:sha256:<rounds>$<salt_hex>$<hash_hex>
    """
    if not password:
        return ""
    salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _PBKDF2_ROUNDS,
    )
    return f"pbkdf2:sha256:{_PBKDF2_ROUNDS}${salt.hex()}${key.hex()}"


def verify_password(plain_password: str, stored_credential: str) -> bool:
    """
    Validate a plain-text password against a stored hash or secret.
    Uses constant-time comparison to protect against timing attacks.

    Supports:
    1. PBKDF2 hashes: pbkdf2:sha256:<rounds>$<salt_hex>$<hash_hex>
    2. Direct secret strings: uses hmac.compare_digest for constant-time comparison
    """
    if not plain_password or not stored_credential:
        return False

    stored = stored_credential.strip()

    if stored.startswith("pbkdf2:sha256:"):
        parts = stored.split("$")
        if len(parts) != 3:
            return False
        header, salt_hex, expected_hex = parts
        try:
            rounds = int(header.split(":")[-1])
            salt = bytes.fromhex(salt_hex)
            expected_key = bytes.fromhex(expected_hex)
        except (ValueError, TypeError):
            return False

        actual_key = hashlib.pbkdf2_hmac(
            "sha256",
            plain_password.encode("utf-8"),
            salt,
            rounds,
        )
        return hmac.compare_digest(actual_key, expected_key)

    # Fallback to direct constant-time string comparison for plain dev passwords
    return hmac.compare_digest(plain_password, stored)


def generate_csrf_token() -> str:
    """Generate a high-entropy 256-bit cryptographically secure hex token."""
    return secrets.token_hex(32)


def verify_csrf_token(session_token: Optional[str], submitted_token: Optional[str]) -> bool:
    """
    Validate an anti-CSRF token using constant-time comparison.
    Returns True only if both tokens exist, are non-empty, and match exactly.
    """
    if not session_token or not submitted_token:
        return False
    return hmac.compare_digest(session_token.strip(), submitted_token.strip())


# ---------------------------------------------------------------------------
# Input Validation Helpers (Email, Password Strength, Username)
# ---------------------------------------------------------------------------

def validate_email_format(email: str) -> Tuple[bool, str]:
    """
    Validate email address format.
    Returns (is_valid, error_message).
    """
    if not email or not email.strip():
        return False, "Email address is required."
    email_clean = email.strip()
    if len(email_clean) > 254:
        return False, "Email address is too long."
    pattern = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
    if not pattern.match(email_clean):
        return False, "Please enter a valid email address (e.g., admin@store.com)."
    return True, ""


def validate_password_strength(password: str) -> Tuple[bool, str]:
    """
    Validate password meets production security strength criteria:
    - Minimum 8 characters (maximum 128)
    - At least 1 uppercase letter (A-Z)
    - At least 1 lowercase letter (a-z)
    - At least 1 numeric digit (0-9)
    - At least 1 special character (!@#$%^&*...)
    Returns (is_valid, error_message).
    """
    if not password:
        return False, "Password is required."
    if len(password) < 8:
        return False, "Password must be at least 8 characters long."
    if len(password) > 128:
        return False, "Password cannot exceed 128 characters."
    if not re.search(r"[A-Z]", password):
        return False, "Password must include at least one uppercase letter (A-Z)."
    if not re.search(r"[a-z]", password):
        return False, "Password must include at least one lowercase letter (a-z)."
    if not re.search(r"[0-9]", password):
        return False, "Password must include at least one numeric digit (0-9)."
    if not re.search(r'[!@#$%^&*()_+\-=\[\]{}|;:,.<>?/~`]', password):
        return False, "Password must include at least one special character (e.g. !@#$%^&*)."
    return True, ""


def validate_username(username: str) -> Tuple[bool, str]:
    """
    Validate username format:
    - 3 to 30 characters
    - Alphanumeric, underscores, hyphens only
    Returns (is_valid, error_message).
    """
    if not username or not username.strip():
        return False, "Username is required."
    uname = username.strip()
    if len(uname) < 3 or len(uname) > 30:
        return False, "Username must be between 3 and 30 characters."
    if not re.match(r"^[a-zA-Z0-9_-]+$", uname):
        return False, "Username may only contain letters, numbers, underscores, and hyphens."
    return True, ""
