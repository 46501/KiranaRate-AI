"""
app/services/security.py
========================
Priority 3 Security Hardening Services for KiranaIQ:
1. Binary Magic Byte Validation for JPEG and PNG images.
2. In-Memory Sliding-Window Rate Limiter.
3. Brute-Force Login Attempt Tracker with Account Lockout.
4. Memory-Safe Chunked Upload Streamer with Content-Length Preflight.
5. Error Message Sanitizer for safe API responses and UI redirects.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException, Request, UploadFile, status

logger = logging.getLogger(__name__)

# Standard image binary signatures (magic bytes)
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Regex pattern to identify and scrub system file paths from error messages
_PATH_PATTERN = re.compile(
    r"(?:[a-zA-Z]:\\[^ \t\n\r\"\'<>()]+)|(?:\/(?:home|usr|var|tmp|etc|app|media|root)[^ \t\n\r\"\'<>()]+)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# 1. Image Magic Byte Validation
# ---------------------------------------------------------------------------

def validate_image_magic_bytes(content: bytes) -> str:
    """
    Validate binary magic bytes for JPEG or PNG image files.

    Returns detected MIME type:
    - 'image/jpeg'
    - 'image/png'

    Raises ValueError if content does not match either format or is too short.
    """
    if not content or len(content) < 8:
        raise ValueError("Invalid file content: file is too small to be a valid image")

    if content.startswith(_JPEG_MAGIC):
        return "image/jpeg"

    if content.startswith(_PNG_MAGIC):
        return "image/png"

    raise ValueError(
        "Invalid file signature: file does not match JPEG or PNG binary magic bytes"
    )


# ---------------------------------------------------------------------------
# 2. In-Memory Sliding Window Rate Limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """
    Thread-safe in-memory sliding window rate limiter.
    Maintains request timestamps per scope/client key.
    """

    def __init__(self) -> None:
        self._requests: Dict[str, List[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def is_allowed(
        self,
        key: str,
        max_requests: int,
        window_seconds: int = 60,
    ) -> Tuple[bool, int]:
        """
        Check if a request is permitted within the given sliding window.

        Parameters
        ----------
        key : str
            Unique identifier, e.g. 'login:192.168.1.1'
        max_requests : int
            Maximum number of allowed hits in the window.
        window_seconds : int
            Window duration in seconds (default: 60).

        Returns
        -------
        Tuple[bool, int]
            (allowed, retry_after_seconds)
        """
        now = time.time()
        cutoff = now - window_seconds

        with self._lock:
            timestamps = self._requests[key]
            # Prune timestamps outside the sliding window
            self._requests[key] = [ts for ts in timestamps if ts > cutoff]
            timestamps = self._requests[key]

            if len(timestamps) >= max_requests:
                earliest = timestamps[0]
                retry_after = max(1, int(earliest + window_seconds - now))
                return False, retry_after

            # Record this request
            self._requests[key].append(now)
            return True, 0

    def reset(self, key: Optional[str] = None) -> None:
        """Reset all tracked timestamps, or a specific key."""
        with self._lock:
            if key:
                self._requests.pop(key, None)
            else:
                self._requests.clear()


# ---------------------------------------------------------------------------
# 3. Failed Login & Brute-Force Tracker
# ---------------------------------------------------------------------------

class LoginAttemptTracker:
    """
    Tracks failed login attempts and triggers temporary account lockout.
    """

    def __init__(
        self,
        max_failed_attempts: int = 5,
        lockout_seconds: int = 300,
        window_seconds: int = 900,
    ) -> None:
        self.max_failed_attempts = max_failed_attempts
        self.lockout_seconds = lockout_seconds
        self.window_seconds = window_seconds

        self._failures: Dict[str, List[float]] = defaultdict(list)
        self._locked_until: Dict[str, float] = {}
        self._lock = threading.Lock()

    def is_locked(self, key: str) -> Tuple[bool, int]:
        """
        Check if the key is currently locked out.

        Returns
        -------
        Tuple[bool, int]
            (is_locked, retry_after_seconds)
        """
        now = time.time()
        with self._lock:
            unlock_time = self._locked_until.get(key, 0)
            if now < unlock_time:
                return True, max(1, int(unlock_time - now))
            elif unlock_time > 0:
                # Lockout period expired
                self._locked_until.pop(key, None)
                self._failures.pop(key, None)
            return False, 0

    def record_failure(
        self,
        key: str,
        max_failed: Optional[int] = None,
        lockout_secs: Optional[int] = None,
    ) -> Tuple[bool, int]:
        """
        Record a failed authentication attempt.
        Locks the key if failures exceed threshold within window.

        Returns
        -------
        Tuple[bool, int]
            (is_now_locked, retry_after_seconds)
        """
        now = time.time()
        threshold = max_failed or self.max_failed_attempts
        duration = lockout_secs or self.lockout_seconds
        cutoff = now - self.window_seconds

        with self._lock:
            # Check existing lock first
            unlock_time = self._locked_until.get(key, 0)
            if now < unlock_time:
                return True, max(1, int(unlock_time - now))

            timestamps = self._failures[key]
            self._failures[key] = [ts for ts in timestamps if ts > cutoff]
            self._failures[key].append(now)

            if len(self._failures[key]) >= threshold:
                lock_until = now + duration
                self._locked_until[key] = lock_until
                logger.warning(
                    "Account lockout triggered for %r: %d failed attempts in %ds. Locked for %ds.",
                    key,
                    len(self._failures[key]),
                    self.window_seconds,
                    duration,
                )
                return True, duration

            return False, 0

    def record_success(self, key: str) -> None:
        """Clear failed attempts on successful authentication."""
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)

    def reset(self) -> None:
        """Reset all tracking records (primarily for testing)."""
        with self._lock:
            self._failures.clear()
            self._locked_until.clear()


# ---------------------------------------------------------------------------
# Global Singletons
# ---------------------------------------------------------------------------

rate_limiter = RateLimiter()
login_tracker = LoginAttemptTracker()


# ---------------------------------------------------------------------------
# Client IP & Rate Limiting Helpers
# ---------------------------------------------------------------------------

def get_client_ip(request: Request) -> str:
    """
    Extract the effective client IP address from request headers or socket.
    Respects standard X-Forwarded-For if deployed behind reverse proxy.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()
        if client_ip:
            return client_ip
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def check_rate_limit(
    request: Request,
    scope: str,
    max_requests: int,
    window_seconds: int = 60,
    enabled: bool = True,
) -> None:
    """
    Enforce rate limit for the given request and scope.
    Raises HTTPException(429) if limit exceeded.
    """
    if not enabled or max_requests <= 0:
        return

    client_ip = get_client_ip(request)
    key = f"{scope}:{client_ip}"
    allowed, retry_after = rate_limiter.is_allowed(key, max_requests, window_seconds)
    if not allowed:
        logger.warning(
            "Rate limit exceeded on scope=%r by client_ip=%s (retry_after=%ds)",
            scope,
            client_ip,
            retry_after,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded for {scope}. Please retry in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )


# ---------------------------------------------------------------------------
# 4. Safe Upload File Streamer (Memory Protection)
# ---------------------------------------------------------------------------

async def read_upload_file_safely(
    file: UploadFile,
    max_bytes: int,
    request: Optional[Request] = None,
) -> bytes:
    """
    Read uploaded file data in controlled chunks (64 KiB).
    - Checks Content-Length header early if available.
    - Aborts immediately if accumulated bytes exceed max_bytes.
    - Prevents Out-Of-Memory (OOM) / Denial-of-Service via unbounded memory ingestion.

    Raises HTTPException(413) if size exceeds max_bytes.
    """
    # 1. Early Content-Length check
    if request is not None:
        content_length_header = request.headers.get("Content-Length")
        if content_length_header:
            try:
                cl = int(content_length_header.strip())
                if cl > max_bytes:
                    max_mb = max_bytes // (1024 * 1024)
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail=f"Upload rejected: Content-Length {cl} bytes exceeds limit of {max_mb} MiB",
                    )
            except ValueError:
                pass

    # 2. Chunked stream reading
    chunk_size = 64 * 1024  # 64 KiB
    chunks: List[bytes] = []
    total_bytes = 0

    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            max_mb = max_bytes // (1024 * 1024)
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"Upload rejected: File size exceeds maximum allowed limit of {max_mb} MiB",
            )
        chunks.append(chunk)

    return b"".join(chunks)


# ---------------------------------------------------------------------------
# 5. Error Message Sanitizer
# ---------------------------------------------------------------------------

def sanitize_user_facing_error(message: str, fallback: str = "Validation failed") -> str:
    """
    Scrub system file paths, database filenames, and internal details from error messages.
    """
    if not message:
        return fallback

    # Strip local file paths
    scrubbed = _PATH_PATTERN.sub("[redacted-path]", message)

    # Scrub potential database table names or internal traces if present
    scrubbed = scrubbed.replace("sqlite3.OperationalError", "Database error")
    scrubbed = scrubbed.replace("OperationalError", "Database error")

    return scrubbed.strip() or fallback
