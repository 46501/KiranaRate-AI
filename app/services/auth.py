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
import secrets
from typing import Optional


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
