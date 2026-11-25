"""
app/config.py
=============
Application configuration loaded from environment variables / .env file
using pydantic-settings.  All business-rule thresholds live here so they
can be tuned without touching source code.
"""

from __future__ import annotations

from typing import List, Union

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Central configuration object.

    Pydantic-settings automatically reads values from:
      1. Environment variables (highest priority)
      2. A .env file in the working directory
      3. Field default values (lowest priority)

    All field names map 1-to-1 with the keys in .env.example.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # WhatsApp Cloud API
    # ------------------------------------------------------------------

    whatsapp_verify_token: str = "changeme"
    """Secret token echoed back during webhook subscription verification."""

    whatsapp_app_secret: str = ""
    """Meta App Secret used to verify X-Hub-Signature-256 HMAC on inbound webhooks."""

    whatsapp_access_token: str = ""
    """Long-lived access token from the Meta App Dashboard."""

    whatsapp_phone_number_id: str = ""
    """Phone Number ID associated with the sending WhatsApp number."""

    whatsapp_api_version: str = "v19.0"
    """WhatsApp Cloud API version string, e.g. 'v19.0'."""

    whatsapp_owner_number: str = Field(
        default="",
        validation_alias=AliasChoices("whatsapp_owner_number", "whatsapp_alert_recipient"),
        description="E.164 phone number of the account owner who receives alert messages.",
    )

    whatsapp_allowlist: Union[List[str], str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("whatsapp_allowlist", "whatsapp_allowed_senders"),
        description="List of sender phone numbers permitted to submit bills.",
    )

    whatsapp_template_name: str = "purchase_rate_review"
    """Name of the pre-approved WhatsApp message template."""

    whatsapp_template_lang: str = "en"
    """Language code of the pre-approved WhatsApp message template."""

    @field_validator("whatsapp_allowlist", mode="before")
    @classmethod
    def _parse_allowlist(cls, v: object) -> List[str]:
        """Accept either a list (already parsed) or a comma-separated string."""
        if isinstance(v, list):
            return [str(item).strip() for item in v if str(item).strip()]
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return []

    # ------------------------------------------------------------------
    # Google Gemini AI
    # ------------------------------------------------------------------

    gemini_api_key: str = ""
    """API key obtained from Google AI Studio."""

    gemini_model: str = "gemini-1.5-flash"
    """Gemini model identifier used for bill image scanning."""

    gemini_request_timeout: int = 30
    """HTTP request timeout in seconds for Gemini API calls."""

    # ------------------------------------------------------------------
    # Storage Backend
    # ------------------------------------------------------------------

    storage_backend: str = "sqlite"
    """Active storage adapter: 'sqlite' or 'sheets'."""

    sqlite_db_path: str = "./bills.db"
    """File-system path to the SQLite database (used when storage_backend='sqlite')."""

    # ------------------------------------------------------------------
    # Development / Testing Mocks & Safe Mode
    # ------------------------------------------------------------------

    mock_whatsapp: bool = False
    """When True, WhatsApp API calls are skipped and logged instead."""

    mock_gemini: bool = False
    """When True, Gemini API calls are replaced with fixture data."""

    dry_run: bool = False
    """When True, external network mutations are suppressed for dry-run verification."""

    # ------------------------------------------------------------------
    # Localisation
    # ------------------------------------------------------------------

    tz_name: str = "Asia/Kolkata"
    """IANA timezone name used for all IST-based business-rule calculations."""

    # ------------------------------------------------------------------
    # Bill / Media Constraints
    # ------------------------------------------------------------------

    bill_size_limit_mb: int = 5
    """Maximum allowed media size in megabytes before the bill is rejected."""

    max_lines: int = 75
    """Maximum number of line items Gemini may return for a single bill."""

    min_lines: int = 1
    """Minimum number of line items required for a scan to be considered valid."""

    media_storage_dir: str = "./media"
    """Directory on disk where downloaded bill media files are stored."""

    # ------------------------------------------------------------------
    # Alert / Price-Comparison Business Rules
    # ------------------------------------------------------------------

    alert_expiry_hours: int = 24
    """PENDING alerts older than this many hours are transitioned to EXPIRED."""

    alert_lookback_days: int = 30
    """
    How many calendar days back to search for historical EX_GST purchases
    when evaluating whether a price-gap alert should be generated.
    """

    alert_new_bill_days: int = 7
    """
    Only bills whose invoice_date falls within this many days of today (IST)
    are eligible to trigger new price-comparison alerts.
    """

    alert_min_gap_pct: float = 2.00
    """
    Minimum price-gap percentage required to trigger an alert.
    gap_pct = 100 * (high_rate - low_rate) / high_rate
    """

    alert_min_gap_paise: int = 1
    """
    Minimum absolute price difference in integer paise required to trigger
    an alert (applied in addition to alert_min_gap_pct).
    """

    # ------------------------------------------------------------------
    # Background Scheduler (APScheduler)
    # ------------------------------------------------------------------

    enable_background_workers: bool = False
    """When True, background workers periodically run via APScheduler in app lifespan."""

    worker_scan_interval_seconds: int = 15
    """Interval in seconds for the scan worker to poll for QUEUED bills."""

    worker_post_interval_seconds: int = 15
    """Interval in seconds for the post worker to poll for APPROVED bills."""

    worker_alert_interval_seconds: int = 30
    """Interval in seconds for the alert outbox worker to poll for PENDING alerts."""

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    @property
    def bill_size_limit_bytes(self) -> int:
        """bill_size_limit_mb expressed in bytes for direct comparison."""
        return self.bill_size_limit_mb * 1024 * 1024

    @property
    def whatsapp_base_url(self) -> str:
        """Fully-qualified WhatsApp Cloud API base URL."""
        return f"https://graph.facebook.com/{self.whatsapp_api_version}"

    @property
    def whatsapp_allowed_senders(self) -> List[str]:
        """Alias for whatsapp_allowlist."""
        return self.whatsapp_allowlist

    @property
    def whatsapp_alert_recipient(self) -> str:
        """Alias for whatsapp_owner_number."""
        return self.whatsapp_owner_number


# ---------------------------------------------------------------------------
# Module-level singleton — import and use ``settings`` everywhere.
# ---------------------------------------------------------------------------

settings = Settings()


def is_placeholder(val: str | None) -> bool:
    """Return True if val is empty, None, or a common template placeholder."""
    if not val:
        return True
    s = str(val).strip().lower()
    return s.startswith("your_") or s in ("changeme", "none", "placeholder", "")

