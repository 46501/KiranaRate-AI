"""
WhatsApp Cloud API adapter for AI Bill Scanner.

Provides:
- BillIntakeEvent       — dataclass representing a parsed inbound bill image.
- WhatsAppWebhookParser — stateless parser for Meta webhook payloads.
- WhatsAppClient        — async HTTP client wrapping Graph API calls.
- MockWhatsAppClient    — in-process test double with no network I/O.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

import httpx

UTC = timezone.utc
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Webhook Signature Utilities (HMAC-SHA256)
# ---------------------------------------------------------------------------

def compute_webhook_signature(raw_body: bytes, app_secret: str) -> str:
    """
    Compute Meta X-Hub-Signature-256 for a raw request body and app secret.

    Returns the signature formatted as 'sha256=<hex_digest>'.
    """
    if not app_secret:
        return ""
    digest = hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def verify_webhook_signature(
    raw_body: bytes,
    signature_header: str | None,
    app_secret: str,
) -> bool:
    """
    Validate an incoming X-Hub-Signature-256 header using constant-time comparison.

    Parameters
    ----------
    raw_body:
        Raw HTTP request body bytes as received from Meta.
    signature_header:
        Value of the X-Hub-Signature-256 header (e.g. 'sha256=abcdef...').
    app_secret:
        The Meta App Secret configured for this application.

    Returns
    -------
    bool:
        True if signature matches, False otherwise.
    """
    if not signature_header or not app_secret:
        return False
    header_val = signature_header.strip()
    expected = compute_webhook_signature(raw_body, app_secret)
    return hmac.compare_digest(header_val.lower(), expected.lower())


# ---------------------------------------------------------------------------
# BillIntakeEvent
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BillIntakeEvent:
    """Immutable record of a single inbound WhatsApp image that represents a bill."""

    bill_id: str        # WhatsApp message.id — used as Bills.bill_id PK
    sender: str         # E.164 sender phone number (message.from)
    media_id: str       # WhatsApp media object id (message.image.id)
    mime_type: str      # e.g. "image/jpeg", "image/png"
    received_at: datetime  # UTC-aware timestamp derived from message.timestamp


# ---------------------------------------------------------------------------
# WhatsAppWebhookParser
# ---------------------------------------------------------------------------

class WhatsAppWebhookParser:
    """
    Stateless parser for Meta WhatsApp Business webhook payloads.

    Filtering rules (all must pass for a message to become a BillIntakeEvent):
      1. value.metadata.phone_number_id == configured phone_number_id.
      2. Entry is a 'messages' event (not a status update).
      3. message.type == 'image'.
      4. message.from (sender) is in the allowlist.
    """

    def parse(
        self,
        payload: dict,
        phone_number_id: str,
        allowlist: list[str],
    ) -> list[BillIntakeEvent]:
        """
        Parse a full Meta webhook POST body and return intake events.

        Parameters
        ----------
        payload:
            The JSON-decoded request body from Meta.
        phone_number_id:
            The WhatsApp Business phone number id that this service owns.
            Messages addressed to any other id are silently ignored.
        allowlist:
            E.164 phone numbers permitted to submit bills.  Any sender not in
            this list is silently skipped (never raises).

        Returns
        -------
        list[BillIntakeEvent]
            Zero or more events, one per qualifying image message.
        """
        events: list[BillIntakeEvent] = []

        entries = payload.get("entry", [])
        if not isinstance(entries, list):
            logger.warning("Webhook payload missing 'entry' list; skipping.")
            return events

        for entry in entries:
            changes = entry.get("changes", [])
            if not isinstance(changes, list):
                continue

            for change in changes:
                value = change.get("value", {})

                # ── 1. phone_number_id guard ─────────────────────────────────
                meta = value.get("metadata", {})
                if meta.get("phone_number_id") != phone_number_id:
                    logger.debug(
                        "Skipping change: phone_number_id %r != expected %r",
                        meta.get("phone_number_id"),
                        phone_number_id,
                    )
                    continue

                # ── 2. Only 'messages' entries (ignore statuses) ─────────────
                messages = value.get("messages")
                if not messages:
                    # Could be a status-only update; skip silently.
                    continue

                for message in messages:
                    if not isinstance(message, dict):
                        continue

                    # ── 3. Accept image and document messages ────────────────
                    msg_type = message.get("type")
                    if msg_type not in ("image", "document"):
                        logger.debug(
                            "Skipping message id=%r: type=%r (neither image nor document)",
                            message.get("id"),
                            msg_type,
                        )
                        continue

                    # ── 4. Allowlist check (normalize E.164 with/without '+') ────
                    sender = message.get("from", "").strip()
                    sender_clean = sender.lstrip("+")
                    normalized_allowlist = {s.lstrip("+").strip() for s in allowlist}
                    if sender_clean not in normalized_allowlist:
                        logger.warning(
                            "Skipping message id=%r: sender %r not in allowlist",
                            message.get("id"),
                            sender,
                        )
                        continue

                    # ── Extract fields ───────────────────────────────────────
                    if msg_type == "image":
                        media_obj = message.get("image", {})
                        mime_type = media_obj.get("mime_type", "image/jpeg")
                    else:  # document
                        media_obj = message.get("document", {})
                        mime_type = media_obj.get("mime_type", "application/octet-stream")
                        filename = media_obj.get("filename", "").lower()
                        # Resolve mime_type if sent as general document
                        if mime_type not in ("image/jpeg", "image/png"):
                            if filename.endswith((".jpg", ".jpeg")):
                                mime_type = "image/jpeg"
                            elif filename.endswith(".png"):
                                mime_type = "image/png"
                            else:
                                logger.warning(
                                    "Skipping document message id=%r: unsupported document MIME/file %r (%s)",
                                    message.get("id"),
                                    filename,
                                    mime_type,
                                )
                                continue

                    # Normalize image/jpg to image/jpeg
                    if mime_type == "image/jpg":
                        mime_type = "image/jpeg"

                    bill_id = message.get("id", "")
                    media_id = media_obj.get("id", "")

                    try:
                        received_at = datetime.fromtimestamp(
                            int(message.get("timestamp", 0)), tz=UTC
                        )
                    except (ValueError, TypeError) as exc:
                        logger.error(
                            "Invalid timestamp for message id=%r: %s", bill_id, exc
                        )
                        continue

                    if not bill_id or not media_id:
                        logger.error(
                            "Message missing bill_id or media_id; skipping. "
                            "bill_id=%r media_id=%r",
                            bill_id,
                            media_id,
                        )
                        continue

                    events.append(
                        BillIntakeEvent(
                            bill_id=bill_id,
                            sender=sender,
                            media_id=media_id,
                            mime_type=mime_type,
                            received_at=received_at,
                        )
                    )
                    logger.info(
                        "Parsed BillIntakeEvent bill_id=%r sender=%r media_id=%r",
                        bill_id,
                        sender,
                        media_id,
                    )

        return events


# ---------------------------------------------------------------------------
# WhatsAppClient
# ---------------------------------------------------------------------------

_GRAPH_BASE = "https://graph.facebook.com"
_TEMPLATE_NAME = "purchase_rate_review"
_TEMPLATE_LANG = "en"


class WhatsAppClient:
    """
    Async adapter for the WhatsApp Cloud (Graph) API.

    Parameters
    ----------
    access_token:
        Meta system-user or page access token with whatsapp_business_messaging scope.
    phone_number_id:
        WhatsApp Business phone number id (not the display number) used as the
        sender when posting outbound messages.
    api_version:
        Graph API version string, e.g. ``"v19.0"``.
    owner_number:
        The E.164 business phone number (informational; used for routing checks).
    http_client:
        Optional pre-configured ``httpx.AsyncClient``.  If *None* (default) a new
        client is created on first use.  Pass a mock client in tests.
    """

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        api_version: str,
        owner_number: str,
        template_name: str = _TEMPLATE_NAME,
        template_lang: str = _TEMPLATE_LANG,
        app_secret: str = "",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self._api_version = api_version
        self._owner_number = owner_number
        self._template_name = template_name
        self._template_lang = template_lang
        self._app_secret = app_secret
        self._http_client = http_client  # may be None → lazy init

    compute_signature = staticmethod(compute_webhook_signature)
    verify_signature = staticmethod(verify_webhook_signature)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        """Return the shared async HTTP client, creating it if necessary."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0),
                headers={"User-Agent": "AIBillScanner/1.0"},
            )
        return self._http_client

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    def _graph_url(self, *path_segments: str) -> str:
        segments = "/".join(str(s) for s in path_segments)
        return f"{_GRAPH_BASE}/{segments}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def verify_webhook(
        self,
        mode: str,
        token: str,
        challenge: str,
        verify_token: str,
    ) -> str:
        """
        Validate a Meta webhook verification handshake.

        Meta sends GET ?hub.mode=subscribe&hub.verify_token=…&hub.challenge=…
        to the webhook URL when you configure or re-verify it.

        Parameters
        ----------
        mode:
            Value of ``hub.mode`` query parameter — must be ``"subscribe"``.
        token:
            Value of ``hub.verify_token`` query parameter — must equal
            ``verify_token`` (the shared secret stored in config).
        challenge:
            Value of ``hub.challenge`` query parameter — echoed back on success.
        verify_token:
            The secret token stored in application config.

        Returns
        -------
        str
            The ``challenge`` string to echo back in the HTTP 200 response.

        Raises
        ------
        ValueError
            If ``mode`` is not ``"subscribe"`` or ``token`` does not match
            ``verify_token``.
        """
        if mode != "subscribe":
            raise ValueError(
                f"Webhook verification failed: hub.mode must be 'subscribe', got {mode!r}"
            )
        if not (token and verify_token and hmac.compare_digest(token, verify_token)):
            raise ValueError(
                "Webhook verification failed: hub.verify_token mismatch"
            )
        logger.info("Webhook verified successfully.")
        return challenge

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """
        Download a WhatsApp media object by its id.

        Flow
        ----
        1. GET ``/v{api_version}/{media_id}`` → JSON with ``url`` and ``mime_type``.
        2. GET the ``url`` with Bearer auth → raw bytes.

        Parameters
        ----------
        media_id:
            WhatsApp media object id (from ``message.image.id``).

        Returns
        -------
        tuple[bytes, str]
            ``(raw_bytes, mime_type)`` where ``mime_type`` is the value returned
            by the Graph API (e.g. ``"image/jpeg"``).

        Raises
        ------
        httpx.HTTPStatusError
            On any non-2xx response from either Graph API call.
        KeyError
            If the Graph API response is missing expected fields.
        """
        client = self._client()
        meta_url = self._graph_url(self._api_version, media_id)

        # Step 1 — resolve media metadata (url + mime_type)
        logger.debug("Fetching media metadata: %s", meta_url)
        meta_resp = await client.get(meta_url, headers=self._auth_headers())
        meta_resp.raise_for_status()
        meta_json: dict = meta_resp.json()

        download_url: str = meta_json["url"]
        mime_type: str = meta_json.get("mime_type", "application/octet-stream")

        # Step 2 — download the actual media bytes
        logger.debug("Downloading media from: %s", download_url)
        media_resp = await client.get(download_url, headers=self._auth_headers())
        media_resp.raise_for_status()

        logger.info(
            "Downloaded media_id=%r mime_type=%r size=%d bytes",
            media_id,
            mime_type,
            len(media_resp.content),
        )
        return media_resp.content, mime_type

    async def send_template_alert(self, to_number: str, params: list[str]) -> str:
        """
        Send the ``purchase_rate_review`` template message via WhatsApp.

        The template must be pre-approved in Meta Business Manager with exactly
        ``len(params)`` body parameters (10 for the standard alert template).

        Parameters
        ----------
        to_number:
            Recipient E.164 phone number (e.g. ``"919876543210"``).
        params:
            Ordered list of text substitution values for the template body
            components.  Must match the template's parameter count exactly.

        Returns
        -------
        str
            The WhatsApp message id (``wamid``), e.g.
            ``"wamid.HBgNOTE5ODc2..."``

        Raises
        ------
        httpx.HTTPStatusError
            On non-2xx response from Graph API.
        KeyError / IndexError
            If the response body lacks ``messages[0].id``.
        """
        client = self._client()
        url = self._graph_url(self._api_version, self._phone_number_id, "messages")

        body = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "template",
            "template": {
                "name": self._template_name,
                "language": {"code": self._template_lang},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": p} for p in params
                        ],
                    }
                ],
            },
        }

        logger.debug("Sending template alert to %r with %d params", to_number, len(params))
        resp = await client.post(url, json=body, headers=self._auth_headers())
        resp.raise_for_status()

        resp_json: dict = resp.json()
        wamid: str = resp_json["messages"][0]["id"]
        logger.info("Template alert sent to %r wamid=%r", to_number, wamid)
        return wamid

    async def send_text_message(self, to_number: str, text: str) -> str:
        """
        Send an outbound freeform text message via WhatsApp Cloud API.
        Useful for acknowledgement messages and processing status updates.
        """
        client = self._client()
        url = self._graph_url(self._api_version, self._phone_number_id, "messages")

        body = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "text",
            "text": {
                "body": text,
            },
        }

        logger.debug("Sending text message to %r: %s", to_number, text[:60])
        resp = await client.post(url, json=body, headers=self._auth_headers())
        resp.raise_for_status()

        resp_json: dict = resp.json()
        wamid: str = resp_json["messages"][0]["id"]
        logger.info("Text message sent to %r wamid=%r", to_number, wamid)
        return wamid


# ---------------------------------------------------------------------------
# MockWhatsAppClient  (test double — no network I/O)
# ---------------------------------------------------------------------------

@dataclass
class _SentAlert:
    """Record of a send_template_alert call, stored for test assertions."""
    to_number: str
    params: list[str]
    wamid: str
    template_name: str = _TEMPLATE_NAME
    template_lang: str = _TEMPLATE_LANG


@dataclass
class _SentText:
    """Record of a send_text_message call, stored for test assertions."""
    to_number: str
    text: str
    wamid: str


class MockWhatsAppClient:
    """
    In-process test double for WhatsAppClient.

    Implements the same public interface as WhatsAppClient but performs no
    network I/O.  Useful in unit tests and local development.

    Parameters
    ----------
    mock_image_bytes:
        Bytes returned by ``download_media``.  Defaults to ``b'fake_jpeg_data'``.
    mock_mime_type:
        MIME type returned alongside ``mock_image_bytes``.  Defaults to
        ``'image/jpeg'``.
    template_name:
        Configured template name.
    template_lang:
        Configured template language code.

    Attributes
    ----------
    sent_alerts : list[_SentAlert]
        Grows with every ``send_template_alert`` call; inspect in tests.
    download_calls : list[str]
        List of ``media_id`` values passed to ``download_media``.
    """

    def __init__(
        self,
        mock_image_bytes: bytes = b"fake_jpeg_data",
        mock_mime_type: str = "image/jpeg",
        template_name: str = _TEMPLATE_NAME,
        template_lang: str = _TEMPLATE_LANG,
    ) -> None:
        self._mock_image_bytes = mock_image_bytes
        self._mock_mime_type = mock_mime_type
        self.template_name = template_name
        self.template_lang = template_lang

        # Test-assertion surfaces
        self.sent_alerts: list[_SentAlert] = []
        self.sent_texts: list[_SentText] = []
        self.download_calls: list[str] = []

    compute_signature = staticmethod(compute_webhook_signature)
    verify_signature = staticmethod(verify_webhook_signature)

    # Webhook verification mirrors the real implementation exactly.
    def verify_webhook(
        self,
        mode: str,
        token: str,
        challenge: str,
        verify_token: str,
    ) -> str:
        """Identical logic to WhatsAppClient.verify_webhook."""
        if mode != "subscribe":
            raise ValueError(
                f"Webhook verification failed: hub.mode must be 'subscribe', got {mode!r}"
            )
        if not (token and verify_token and hmac.compare_digest(token, verify_token)):
            raise ValueError(
                "Webhook verification failed: hub.verify_token mismatch"
            )
        return challenge

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Return configured fixture bytes without any HTTP call."""
        self.download_calls.append(media_id)
        logger.debug("[Mock] download_media media_id=%r → %d bytes", media_id, len(self._mock_image_bytes))
        return self._mock_image_bytes, self._mock_mime_type

    async def send_template_alert(self, to_number: str, params: list[str]) -> str:
        """Record the call and return a deterministic-looking fake wamid."""
        wamid = "mock-wamid-" + uuid4().hex[:8]
        record = _SentAlert(
            to_number=to_number,
            params=list(params),
            wamid=wamid,
            template_name=self.template_name,
            template_lang=self.template_lang,
        )
        self.sent_alerts.append(record)
        logger.debug(
            "[Mock] send_template_alert to=%r params_count=%d wamid=%r",
            to_number,
            len(params),
            wamid,
        )
        return wamid

    async def send_text_message(self, to_number: str, text: str) -> str:
        """Record the call and return a deterministic-looking fake text wamid."""
        wamid = "mock-wamid-txt-" + uuid4().hex[:8]
        record = _SentText(to_number=to_number, text=text, wamid=wamid)
        self.sent_texts.append(record)
        logger.debug("[Mock] send_text_message to=%r text=%r wamid=%r", to_number, text[:60], wamid)
        return wamid
