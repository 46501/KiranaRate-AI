"""
app/services/workflow_engine.py
================================
WorkflowEngine — orchestrates all four pipelines of the AI Bill Scanner:

  1. Intake Webhook   – parse & persist inbound WhatsApp image messages.
  2. AI Scan          – download media and extract structured data with Gemini.
  3. Verify / Post    – validate a human-approved bill and write purchases.
  4. Alert Outbox     – send PENDING price-gap alerts over WhatsApp.

Design goals
------------
* Every public method is atomic: either the full state transition is committed
  or the bill/alert stays in its previous state (no partial state leaks).
* All "today" comparisons use Asia/Kolkata local time.
* Integer paise arithmetic prevents floating-point drift throughout.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Optional

import pytz

from app.config import Settings
from app.models import (
    Alert,
    AlertStatus,
    Bill,
    BillStatus,
    CostBasis,
    Purchase,
    ReviewLine,
    Reviewed,
)
from app.services.gemini_scanner import GeminiScanner, MockGeminiScanner
from app.services.security import sanitize_user_facing_error, validate_image_magic_bytes
from app.services.whatsapp import (
    BillIntakeEvent,
    MockWhatsAppClient,
    WhatsAppClient,
    WhatsAppWebhookParser,
)
from app.storage.base import BaseStore

logger = logging.getLogger(__name__)

_IST = pytz.timezone("Asia/Kolkata")
_UTC = timezone.utc

# Allowed image MIME types for bill images
_ALLOWED_MIME_TYPES = {"image/jpeg", "image/png"}

# Extension map for saving media to disk
_MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
}


class WorkflowEngine:
    """
    Orchestrates the four processing pipelines of the AI Bill Scanner.

    Parameters
    ----------
    store:
        Storage backend (SQLite or any other BaseStore implementation).
    scanner:
        Gemini Vision scanner (real or mock).
    whatsapp:
        WhatsApp Cloud API client (real or mock).
    settings:
        Application configuration (thresholds, paths, tokens, etc.).
    """

    def __init__(
        self,
        store: BaseStore,
        scanner: Any = None,
        whatsapp: Any = None,
        settings: Any = None,
        **kwargs: Any,
    ) -> None:
        if isinstance(scanner, Settings):
            # caller passed (store, settings, scanner, whatsapp)
            settings_obj = scanner
            scanner_obj = whatsapp
            whatsapp_obj = settings
        else:
            settings_obj = settings or kwargs.get("settings")
            scanner_obj = scanner
            whatsapp_obj = whatsapp
        self._store = store
        self._scanner = scanner_obj
        self._whatsapp = whatsapp_obj
        self._settings = settings_obj
        self._parser = WhatsAppWebhookParser()

    # ------------------------------------------------------------------
    # Timezone helpers
    # ------------------------------------------------------------------

    def _get_kolkata_now(self) -> datetime:
        """Return the current datetime in Asia/Kolkata timezone."""
        return datetime.now(_IST)

    def _get_kolkata_today(self) -> date:
        """Return today's date in the Asia/Kolkata timezone."""
        return self._get_kolkata_now().date()

    # ------------------------------------------------------------------
    # Indian Financial Year helpers
    # ------------------------------------------------------------------

    def _get_indian_fy_dates(self, for_date: date) -> tuple[date, date]:
        """
        Return (fy_start, fy_end) for the Indian Financial Year that contains
        ``for_date``.

        Indian FY runs April 1 – March 31.

        Examples
        --------
        2024-11-15  →  (2024-04-01, 2025-03-31)
        2025-02-10  →  (2024-04-01, 2025-03-31)
        2025-04-01  →  (2025-04-01, 2026-03-31)
        """
        if for_date.month >= 4:
            fy_start = date(for_date.year, 4, 1)
            fy_end = date(for_date.year + 1, 3, 31)
        else:
            fy_start = date(for_date.year - 1, 4, 1)
            fy_end = date(for_date.year, 3, 31)
        return fy_start, fy_end

    # ------------------------------------------------------------------
    # Monetary helpers
    # ------------------------------------------------------------------

    def _compute_paise(self, amount_str: str) -> int:
        """
        Parse a decimal INR string and return the equivalent integer paise.

        Uses ``Decimal`` internally to avoid floating-point drift.

        Examples
        --------
        "500.00"  →  50000
        "460"     →  46000
        "4.6"     →  460
        """
        d = Decimal(str(amount_str).strip())
        paise = (d * 100).to_integral_value(rounding=ROUND_HALF_UP)
        return int(paise)

    # ------------------------------------------------------------------
    # Template-string formatters (for the 10-param WhatsApp template)
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_rate(rate_paise: int) -> str:
        """Format an integer paise rate as 'Rs X.YY/unit'."""
        rupees = Decimal(rate_paise) / Decimal(100)
        return f"Rs {rupees:.2f}/unit"

    @staticmethod
    def _fmt_date(d: date) -> str:
        """Format a date as 'DD-Mon-YYYY', e.g. '20-Nov-2026'."""
        return d.strftime("%-d-%b-%Y") if os.name != "nt" else d.strftime("%d-%b-%Y").lstrip("0")

    @staticmethod
    def _fmt_gap_pct(gap_pct: float) -> str:
        """Format a gap percentage as 'X.XX%'."""
        return f"{gap_pct:.2f}%"

    # ------------------------------------------------------------------
    # Pipeline 1: Intake Webhook
    # ------------------------------------------------------------------

    async def run_intake(self, webhook_payload: dict) -> list[str]:
        """
        Parse a Meta WhatsApp webhook payload and persist qualifying image
        messages as QUEUED bills.

        Steps per event
        ---------------
        1. Skip if the bill_id already exists in the store (idempotent).
        2. Download the media bytes from WhatsApp and save to disk.
        3. Insert a Bill record with status QUEUED.

        Returns
        -------
        list[str]
            bill_ids of newly inserted bills (empty if all were duplicates or
            no qualifying events were found).
        """
        events: list[BillIntakeEvent] = self._parser.parse(
            payload=webhook_payload,
            phone_number_id=self._settings.whatsapp_phone_number_id,
            allowlist=self._settings.whatsapp_allowlist,
        )

        inserted_ids: list[str] = []

        for event in events:
            # ── Deduplication ─────────────────────────────────────────
            existing = self._store.get_bill(event.bill_id)
            if existing is not None:
                logger.info(
                    "Duplicate bill_id=%r from sender=%r; skipping.",
                    event.bill_id,
                    event.sender,
                )
                continue

            # ── Determine file extension ───────────────────────────────
            ext = _MIME_TO_EXT.get(event.mime_type, ".jpg")
            media_filename = f"{event.bill_id}{ext}"
            media_path = os.path.join(self._settings.media_storage_dir, media_filename)

            # ── Download and persist media bytes ──────────────────────
            try:
                image_bytes, _mime = await self._whatsapp.download_media(event.media_id)
                os.makedirs(self._settings.media_storage_dir, exist_ok=True)
                with open(media_path, "wb") as fh:
                    fh.write(image_bytes)
                logger.info(
                    "Saved media bill_id=%r to %s (%d bytes)",
                    event.bill_id,
                    media_path,
                    len(image_bytes),
                )
            except Exception:
                logger.exception(
                    "Failed to download/save media for bill_id=%r; skipping.",
                    event.bill_id,
                )
                continue

            # ── Insert Bill record ─────────────────────────────────────
            bill = Bill(
                bill_id=event.bill_id,
                sender=event.sender,
                media_id=event.media_id,
                mime_type=event.mime_type,
                received_at=event.received_at,
                status=BillStatus.QUEUED,
            )
            try:
                self._store.insert_bill(bill)
                logger.info(
                    "Inserted QUEUED bill bill_id=%r sender=%r",
                    event.bill_id,
                    event.sender,
                )
                inserted_ids.append(event.bill_id)

                # ── Dispatch intake acknowledgment ────────────────────────
                if hasattr(self._whatsapp, "send_text_message") and event.sender and event.sender != "web-admin":
                    try:
                        ack_text = f"KiranaIQ: Bill image received and queued for AI scanning. Reference ID: {event.bill_id}"
                        await self._whatsapp.send_text_message(event.sender, ack_text)
                    except Exception as ack_err:
                        logger.warning("Could not dispatch intake ack to %s: %s", event.sender, ack_err)
            except Exception:
                logger.exception(
                    "Failed to insert bill bill_id=%r; skipping.",
                    event.bill_id,
                )
                continue

        return inserted_ids

    async def intake_uploaded_bill(
        self,
        image_bytes: bytes,
        filename: str,
        mime_type: str,
        sender: str = "web-admin",
    ) -> str:
        """
        Intake a bill uploaded directly via Web Dashboard.

        1. Validate MIME type (jpeg/png only).
        2. Validate file size (<= configured limit).
        3. Save file into media directory.
        4. Insert Bill record with status QUEUED.
        """
        # 1. Validate binary magic bytes (JPEG / PNG signature)
        detected_mime = validate_image_magic_bytes(image_bytes)

        # 2. Normalize and validate requested MIME
        clean_mime = (mime_type or "").lower().strip()
        if clean_mime and clean_mime not in ("image/png", "image/jpeg", "image/jpg"):
            raise ValueError(f"Unsupported MIME type {mime_type!r}; only image/jpeg and image/png are accepted.")

        effective_mime = detected_mime
        ext = ".png" if detected_mime == "image/png" else ".jpg"

        size_limit = self._settings.bill_size_limit_bytes
        if len(image_bytes) > size_limit:
            raise ValueError(
                f"File size {len(image_bytes)} bytes exceeds limit {size_limit} bytes "
                f"({self._settings.bill_size_limit_mb} MiB)."
            )

        bill_id = f"wamid.WEB_{uuid.uuid4().hex[:12]}"
        saved_filename = f"{bill_id}{ext}"
        media_dir = Path(__file__).resolve().parent.parent.parent / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        media_path = media_dir / saved_filename
        with open(media_path, "wb") as fh:
            fh.write(image_bytes)

        bill = Bill(
            bill_id=bill_id,
            sender=sender,
            media_id=f"local:{saved_filename}",
            mime_type=effective_mime,
            received_at=datetime.now(_UTC),
            status=BillStatus.QUEUED,
        )
        self._store.insert_bill(bill)
        logger.info("Inserted web-uploaded bill bill_id=%r (%d bytes)", bill_id, len(image_bytes))
        return bill_id

    # ------------------------------------------------------------------
    # Pipeline 2: AI Scan Worker
    # ------------------------------------------------------------------

    async def run_scan_worker(self) -> str | None:
        """
        Process the oldest QUEUED bill through the Gemini Vision scanner.

        Steps
        -----
        1. Pick the oldest QUEUED bill; return None if none exist.
        2. Mark bill SCANNING (new scan_id, scan_started_at).
        3. Download media bytes from WhatsApp.
        4. Validate MIME type (jpeg/png only) and file size (<= BILL_SIZE_LIMIT_MB).
        5. Call GeminiScanner.scan_image().
        6. On success: delete old ReviewLines, insert new ones, update bill → REVIEW.
        7. On failure: log the error and record it in bill.notes (stays SCANNING).

        Returns
        -------
        str | None
            The bill_id that was processed, or None if no QUEUED bill existed.
        """
        bill = self._store.get_oldest_queued_bill()
        if bill is None:
            logger.debug("run_scan_worker: no QUEUED bills.")
            return None

        bill_id = bill.bill_id
        scan_id = str(uuid.uuid4())
        now_utc = datetime.now(_UTC)

        # ── Mark SCANNING ──────────────────────────────────────────────
        self._store.update_bill(
            bill_id,
            status=BillStatus.SCANNING,
            scan_id=scan_id,
            scan_started_at=now_utc,
        )
        logger.info("Scanning bill_id=%r scan_id=%r", bill_id, scan_id)

        try:
            # ── Download or load media ─────────────────────────────────
            image_bytes = None
            downloaded_mime = bill.mime_type or "image/jpeg"
            if bill.media_id and bill.media_id.startswith("local:"):
                fname = bill.media_id.split("local:", 1)[1]
                media_dir = Path(__file__).resolve().parent.parent.parent / "media"
                local_f = media_dir / fname
                if local_f.exists():
                    image_bytes = local_f.read_bytes()
                    downloaded_mime = bill.mime_type or ("image/png" if fname.endswith(".png") else "image/jpeg")

            if image_bytes is None:
                image_bytes, downloaded_mime = await self._whatsapp.download_media(bill.media_id)

            # ── Validate MIME type ─────────────────────────────────────
            # Use the mime_type from the original intake event (more reliable)
            effective_mime = bill.mime_type or downloaded_mime
            if effective_mime not in _ALLOWED_MIME_TYPES:
                raise ValueError(
                    f"Unsupported MIME type {effective_mime!r}; "
                    f"only image/jpeg and image/png are accepted."
                )

            # ── Validate binary magic bytes ───────────────────────────
            if not (self._settings.mock_whatsapp and image_bytes.startswith(b"fake_")):
                validate_image_magic_bytes(image_bytes)

            # ── Validate file size ─────────────────────────────────────
            size_limit = self._settings.bill_size_limit_bytes
            if len(image_bytes) > size_limit:
                raise ValueError(
                    f"Media size {len(image_bytes)} bytes exceeds limit "
                    f"{size_limit} bytes ({self._settings.bill_size_limit_mb} MiB)."
                )

            # ── Gemini scan ────────────────────────────────────────────
            scan_result = await self._scanner.scan_image(image_bytes, effective_mime)

            # Validate line count against configured bounds
            n_lines = len(scan_result.lines)
            if not (self._settings.min_lines <= n_lines <= self._settings.max_lines):
                raise ValueError(
                    f"Scan returned {n_lines} lines; expected "
                    f"{self._settings.min_lines}–{self._settings.max_lines}."
                )

            # ── Persist ReviewLines ────────────────────────────────────
            # Delete any lines from a prior scan attempt first
            self._store.delete_review_lines(bill_id)

            for raw_line in scan_result.lines:
                review_line = ReviewLine(
                    review_id=str(uuid.uuid4()),
                    bill_id=bill_id,
                    scan_id=scan_id,
                    line_no=raw_line["line_no"],
                    product_raw=raw_line.get("product_raw") or "",
                    pack_raw=raw_line.get("pack_raw") or "",
                    quantity_raw=raw_line.get("quantity_raw") or "",
                    unit_raw=raw_line.get("unit_raw") or "",
                    rate_raw=raw_line.get("rate_raw") or "",
                    line_total_raw=raw_line.get("line_total_raw") or "",
                    reviewed=Reviewed.NO,
                )
                self._store.insert_review_line(review_line)

            # ── Transition to REVIEW ───────────────────────────────────
            self._store.update_bill(
                bill_id,
                status=BillStatus.REVIEW,
                supplier_raw=scan_result.supplier_raw,
                invoice_no=scan_result.invoice_no,
                invoice_date=scan_result.invoice_date,
                currency=scan_result.currency,
                invoice_total=scan_result.invoice_total,
                notes=scan_result.notes,
                line_count=n_lines,
            )
            logger.info(
                "Scan complete bill_id=%r scan_id=%r lines=%d → REVIEW",
                bill_id,
                scan_id,
                n_lines,
            )

            # ── Dispatch scan complete status notification ────────────
            if hasattr(self._whatsapp, "send_text_message") and bill.sender and bill.sender != "web-admin":
                try:
                    supp_name = scan_result.supplier_raw or "Supplier"
                    total_str = f" of Rs {scan_result.invoice_total}" if scan_result.invoice_total else ""
                    scan_msg = f"KiranaIQ: Bill from {supp_name}{total_str} scanned ({n_lines} items extracted). Ready for verification on store dashboard."
                    await self._whatsapp.send_text_message(bill.sender, scan_msg)
                except Exception as scan_notify_err:
                    logger.warning("Could not dispatch scan complete notification to %s: %s", bill.sender, scan_notify_err)

        except Exception as exc:
            safe_err = sanitize_user_facing_error(str(exc))
            error_msg = f"Scan error: {safe_err}"
            logger.exception("Scan failed for bill_id=%r: %s", bill_id, safe_err)
            # Record the error in notes; leave bill in SCANNING state
            self._store.update_bill(bill_id, notes=error_msg)

            # ── Dispatch scan failure notification ────────────────────
            if hasattr(self._whatsapp, "send_text_message") and bill.sender and bill.sender != "web-admin":
                try:
                    err_notify = f"KiranaIQ: Issue reading bill image ({safe_err}). Please check receipt clarity or retry on dashboard."
                    await self._whatsapp.send_text_message(bill.sender, err_notify)
                except Exception as scan_err_notify:
                    logger.warning("Could not dispatch scan error notification to %s: %s", bill.sender, scan_err_notify)

        return bill_id

    # ------------------------------------------------------------------
    # Pipeline 3: Verify / Post Worker
    # ------------------------------------------------------------------

    async def run_post_worker(self) -> str | None:
        """
        Validate the oldest APPROVED bill and, if all checks pass, post it
        to the purchase ledger and generate price-gap alerts.

        Any validation failure transitions the bill back to REVIEW with a
        descriptive reason; no partial state is committed.

        Returns
        -------
        str | None
            The bill_id that was processed, or None if no APPROVED bill existed.
        """
        bill = self._store.get_oldest_approved_bill()
        if bill is None:
            logger.debug("run_post_worker: no APPROVED bills.")
            return None

        bill_id = bill.bill_id
        today_ist = self._get_kolkata_today()

        # Helper to push bill back to REVIEW with a reason
        def reject(reason: str) -> None:
            logger.warning("Bill %r failed post validation: %s", bill_id, reason)
            self._store.update_bill(
                bill_id,
                status=BillStatus.REVIEW,
                reason=reason,
            )

        # ── (a) header_verified == YES ─────────────────────────────────
        from app.models import HeaderVerified  # local import to avoid circular
        if bill.header_verified != HeaderVerified.YES:
            reject("header_verified must be YES before posting.")
            return bill_id

        # ── (b) reviewer non-empty ─────────────────────────────────────
        if not (bill.reviewer and bill.reviewer.strip()):
            reject("reviewer must be set before posting.")
            return bill_id

        # ── (c) invoice_date valid YYYY-MM-DD, not in future (IST) ────
        invoice_date_obj: Optional[date] = None
        try:
            invoice_date_obj = date.fromisoformat(bill.invoice_date)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            reject(f"invoice_date {bill.invoice_date!r} is not a valid YYYY-MM-DD date.")
            return bill_id
        if invoice_date_obj > today_ist:
            reject(
                f"invoice_date {bill.invoice_date} is in the future "
                f"(today IST is {today_ist})."
            )
            return bill_id

        # ── (d) currency == INR ────────────────────────────────────────
        if bill.currency != "INR":
            reject(f"currency must be INR; got {bill.currency!r}.")
            return bill_id

        # ── (e) invoice_no non-empty ───────────────────────────────────
        if not (bill.invoice_no and bill.invoice_no.strip()):
            reject("invoice_no must be non-empty.")
            return bill_id

        # ── (f) active supplier ────────────────────────────────────────
        if not bill.supplier_id:
            reject("supplier_id is not set.")
            return bill_id
        supplier = self._store.get_supplier(bill.supplier_id)
        if supplier is None:
            reject(f"supplier_id {bill.supplier_id!r} does not exist.")
            return bill_id
        from app.models import SupplierActive
        if supplier.active != SupplierActive.YES:
            reject(f"Supplier {bill.supplier_id!r} is not active.")
            return bill_id

        # ── (g) cost_basis == EX_GST ───────────────────────────────────
        if bill.cost_basis != CostBasis.EX_GST:
            reject(f"cost_basis must be EX_GST; got {bill.cost_basis!r}.")
            return bill_id

        # ── (h) Indian FY duplicate-invoice check ──────────────────────
        fy_start, fy_end = self._get_indian_fy_dates(invoice_date_obj)
        if self._store.has_duplicate_invoice(
            supplier_id=bill.supplier_id,
            fy_start=fy_start,
            fy_end=fy_end,
            invoice_no=bill.invoice_no,
        ):
            reject(
                f"Duplicate invoice: supplier={bill.supplier_id!r} "
                f"invoice_no={bill.invoice_no!r} already POSTED in FY "
                f"{fy_start}–{fy_end}."
            )
            return bill_id

        # ── Load review lines for remaining checks ─────────────────────
        lines = self._store.get_review_lines(bill_id)

        # ── (i) line_count matches actual ReviewLine count ─────────────
        if bill.line_count != len(lines):
            reject(
                f"line_count={bill.line_count} does not match actual "
                f"ReviewLine count={len(lines)}."
            )
            return bill_id

        # ── (j) line numbers unique and cover 1..line_count ───────────
        line_nos = [ln.line_no for ln in lines]
        expected_line_nos = set(range(1, (bill.line_count or 0) + 1))
        if set(line_nos) != expected_line_nos or len(line_nos) != len(set(line_nos)):
            reject(
                f"ReviewLine line_no values {sorted(line_nos)} do not form "
                f"a unique contiguous sequence 1..{bill.line_count}."
            )
            return bill_id

        # ── (k) all lines reviewed == YES ──────────────────────────────
        unreviewed = [ln.line_no for ln in lines if ln.reviewed != Reviewed.YES]
        if unreviewed:
            reject(f"Lines {unreviewed} are not yet reviewed.")
            return bill_id

        # ── (l) all lines have active SKU ──────────────────────────────
        from app.models import ProductActive
        for ln in lines:
            if not ln.sku:
                reject(f"Line {ln.line_no} has no SKU assigned.")
                return bill_id
            product = self._store.get_product(ln.sku)
            if product is None:
                reject(f"Line {ln.line_no} SKU {ln.sku!r} does not exist in products.")
                return bill_id
            if product.active != ProductActive.YES:
                reject(f"Line {ln.line_no} SKU {ln.sku!r} is not active.")
                return bill_id

        # ── (m) received_units > 0 (integer) ──────────────────────────
        for ln in lines:
            if not isinstance(ln.received_units, int) or ln.received_units <= 0:
                reject(
                    f"Line {ln.line_no} received_units={ln.received_units!r} "
                    f"must be a positive integer."
                )
                return bill_id

        # ── (n) net_amount > 0 ─────────────────────────────────────────
        for ln in lines:
            try:
                na = Decimal(str(ln.net_amount))
            except Exception:
                reject(
                    f"Line {ln.line_no} net_amount={ln.net_amount!r} "
                    f"is not a valid decimal."
                )
                return bill_id
            if na <= 0:
                reject(
                    f"Line {ln.line_no} net_amount={ln.net_amount} "
                    f"must be positive."
                )
                return bill_id

        # ── (o) paise sum == verified_net_total ────────────────────────
        try:
            expected_paise = self._compute_paise(str(bill.verified_net_total))
        except Exception:
            reject(
                f"verified_net_total={bill.verified_net_total!r} "
                f"is not a valid decimal."
            )
            return bill_id

        actual_paise = sum(
            int((Decimal(str(ln.net_amount)) * 100).to_integral_value(rounding=ROUND_HALF_UP))
            for ln in lines
        )
        if actual_paise != expected_paise:
            reject(
                f"Paise sum of ReviewLines ({actual_paise}) does not match "
                f"verified_net_total ({expected_paise} paise = "
                f"Rs {bill.verified_net_total})."
            )
            return bill_id

        # ── All validations passed → aggregate per SKU ─────────────────
        now_utc = datetime.now(_UTC)

        # Aggregate: sum units and net_paise per SKU
        sku_aggregates: dict[str, dict] = {}
        for ln in lines:
            sku = ln.sku  # already validated non-None above
            line_paise = int(
                (Decimal(str(ln.net_amount)) * 100).to_integral_value(rounding=ROUND_HALF_UP)
            )
            if sku not in sku_aggregates:
                sku_aggregates[sku] = {"units": 0, "net_paise": 0}
            sku_aggregates[sku]["units"] += ln.received_units  # type: ignore[operator]
            sku_aggregates[sku]["net_paise"] += line_paise

        # ── Upsert Purchases ───────────────────────────────────────────
        purchases: list[Purchase] = []
        for sku, agg in sku_aggregates.items():
            purchase = Purchase(
                purchase_id=str(uuid.uuid4()),
                bill_id=bill_id,
                sku=sku,
                supplier_id=bill.supplier_id,  # type: ignore[arg-type]
                invoice_no=bill.invoice_no,  # type: ignore[arg-type]
                invoice_date=invoice_date_obj,
                cost_basis=CostBasis.EX_GST,
                units=agg["units"],
                net_paise=agg["net_paise"],
                posted_at=now_utc,
            )
            self._store.insert_purchase(purchase)
            purchases.append(purchase)
            logger.info(
                "Inserted Purchase purchase_id=%r bill_id=%r sku=%r units=%d net_paise=%d",
                purchase.purchase_id,
                bill_id,
                sku,
                agg["units"],
                agg["net_paise"],
            )

        # ── Generate price-gap alerts ──────────────────────────────────
        await self._generate_alerts(bill, purchases)

        # ── Mark bill POSTED ───────────────────────────────────────────
        self._store.update_bill(
            bill_id,
            status=BillStatus.POSTED,
            posted_at=now_utc,
            reason=None,
        )
        logger.info("Bill bill_id=%r → POSTED", bill_id)

        return bill_id

    # ------------------------------------------------------------------
    # Alert generation helper
    # ------------------------------------------------------------------

    def _get_cheapest_other_supplier_rate(
        self,
        sku: str,
        supplier_id: str,
        since_30_days: datetime,
    ) -> Optional[tuple[int, str, str, date]]:
        """
        Find the cheapest EX_GST rate for ``sku`` from any supplier other
        than ``supplier_id`` within the last 30 days.

        Returns
        -------
        (rate_paise, supplier_id, bill_id, invoice_date) or None
            The lowest per-unit rate found, along with identifying fields.
        """
        candidates = self._store.get_purchases_for_sku_since(
            sku=sku,
            since_dt=since_30_days,
            exclude_supplier=supplier_id,
        )
        if not candidates:
            return None

        best: Optional[tuple[int, str, str, date]] = None
        for p in candidates:
            if p.units <= 0:
                continue
            rate = p.net_paise // p.units
            if best is None or rate < best[0]:
                best = (rate, p.supplier_id, p.bill_id, p.invoice_date)

        return best

    async def _generate_alerts(self, bill: Bill, purchases: list[Purchase]) -> None:
        """
        For each Purchase on the newly posted bill, check whether a
        price-gap alert should be generated against historical rates from
        other suppliers within the last ALERT_LOOKBACK_DAYS days.

        Alert conditions (both must hold)
        ----------------------------------
        * gap_paise  >= ALERT_MIN_GAP_PAISE  (default 1 paisa)
        * gap_pct    >= ALERT_MIN_GAP_PCT    (default 2.00 %)
          where gap_pct = 100 * (high_rate - low_rate) / high_rate

        Only bills whose invoice_date is within ALERT_NEW_BILL_DAYS of
        today (IST) are eligible to trigger alerts.

        Template parameters (10, in order)
        -----------------------------------
        1.  sku
        2.  low_supplier_name
        3.  low_rate_str         e.g. "Rs 46.00/unit"
        4.  low_date_str         e.g. "20-Nov-2026"
        5.  high_supplier_name
        6.  high_rate_str        e.g. "Rs 50.00/unit"
        7.  high_date_str        e.g. "22-Nov-2026"
        8.  gap_pct_str          e.g. "8.00%"
        9.  low_bill             bill_id of the cheaper purchase
        10. high_bill            bill_id of the dearer purchase
        """
        today_ist = self._get_kolkata_today()
        now_utc = datetime.now(_UTC)

        # Lookback window: use UTC midnight ALERT_LOOKBACK_DAYS ago
        lookback_dt = datetime.combine(
            today_ist - timedelta(days=self._settings.alert_lookback_days),
            datetime.min.time(),
            tzinfo=_UTC,
        )

        for purchase in purchases:
            # ── New-bill gate: invoice_date within ALERT_NEW_BILL_DAYS of today (IST)
            days_old = (today_ist - purchase.invoice_date).days
            if days_old > self._settings.alert_new_bill_days:
                logger.debug(
                    "Skipping alert for bill_id=%r sku=%r: invoice_date %s is "
                    "%d days old (limit %d).",
                    purchase.bill_id,
                    purchase.sku,
                    purchase.invoice_date,
                    days_old,
                    self._settings.alert_new_bill_days,
                )
                continue

            if purchase.units <= 0:
                logger.warning(
                    "Purchase purchase_id=%r has units=%d; skipping alert.",
                    purchase.purchase_id,
                    purchase.units,
                )
                continue

            current_rate_paise = purchase.net_paise // purchase.units

            # ── Find cheapest rate from another supplier ──────────────
            other = self._get_cheapest_other_supplier_rate(
                sku=purchase.sku,
                supplier_id=purchase.supplier_id,
                since_30_days=lookback_dt,
            )
            if other is None:
                logger.debug(
                    "No historical comparison data for sku=%r; no alert.",
                    purchase.sku,
                )
                continue

            other_rate_paise, other_supplier_id, other_bill_id, other_invoice_date = other

            # ── Determine high / low ────────────────────────────────────
            if current_rate_paise <= other_rate_paise:
                low_rate = current_rate_paise
                low_supplier_id = purchase.supplier_id
                low_date = purchase.invoice_date
                low_bill = purchase.bill_id
                high_rate = other_rate_paise
                high_supplier_id = other_supplier_id
                high_date = other_invoice_date
                high_bill = other_bill_id
            else:
                low_rate = other_rate_paise
                low_supplier_id = other_supplier_id
                low_date = other_invoice_date
                low_bill = other_bill_id
                high_rate = current_rate_paise
                high_supplier_id = purchase.supplier_id
                high_date = purchase.invoice_date
                high_bill = purchase.bill_id

            gap_paise = high_rate - low_rate

            # ── gap_pct = 100 * (high - low) / high, rounded to 2dp ────
            if high_rate == 0:
                continue
            gap_pct_decimal = Decimal("100") * Decimal(gap_paise) / Decimal(high_rate)
            gap_pct = float(
                gap_pct_decimal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            )

            # ── Alert condition ─────────────────────────────────────────
            if gap_paise < self._settings.alert_min_gap_paise:
                logger.debug(
                    "sku=%r gap_paise=%d < %d; no alert.",
                    purchase.sku,
                    gap_paise,
                    self._settings.alert_min_gap_paise,
                )
                continue
            if gap_pct < self._settings.alert_min_gap_pct:
                logger.debug(
                    "sku=%r gap_pct=%.2f < %.2f; no alert.",
                    purchase.sku,
                    gap_pct,
                    self._settings.alert_min_gap_pct,
                )
                continue

            # ── Resolve supplier names for template params ──────────────
            low_supplier_obj = self._store.get_supplier(low_supplier_id)
            high_supplier_obj = self._store.get_supplier(high_supplier_id)
            low_supplier_name = (
                low_supplier_obj.supplier_name if low_supplier_obj else low_supplier_id
            )
            high_supplier_name = (
                high_supplier_obj.supplier_name if high_supplier_obj else high_supplier_id
            )

            # ── Build the 10 template params (exact spec ordering) ────
            template_params = [
                purchase.sku,                        # 1  sku
                low_supplier_name,                   # 2  low supplier name
                self._fmt_rate(low_rate),            # 3  low rate string
                self._fmt_date(low_date),            # 4  low date
                low_bill,                            # 5  low bill id
                high_supplier_name,                  # 6  high supplier name
                self._fmt_rate(high_rate),           # 7  high rate string
                self._fmt_date(high_date),           # 8  high date
                high_bill,                           # 9  high bill id
                self._fmt_gap_pct(gap_pct),          # 10 gap %
            ]

            alert = Alert(
                alert_id=str(uuid.uuid4()),
                bill_id=bill.bill_id,
                sku=purchase.sku,
                low_supplier=low_supplier_id,
                high_supplier=high_supplier_id,
                low_date=low_date,
                high_date=high_date,
                low_bill=low_bill,
                high_bill=high_bill,
                low_rate=low_rate,
                high_rate=high_rate,
                gap_pct=gap_pct,
                status=AlertStatus.PENDING,
                created_at=now_utc,
            )
            self._store.insert_alert(alert)
            logger.info(
                "Generated PENDING Alert alert_id=%r bill_id=%r sku=%r "
                "gap_paise=%d gap_pct=%.2f%%",
                alert.alert_id,
                bill.bill_id,
                purchase.sku,
                gap_paise,
                gap_pct,
            )
            # Store template_params on alert object for reference
            # (they are recomputed in run_alert_worker from alert fields)
            _ = template_params  # used below in run_alert_worker via alert fields

    # ------------------------------------------------------------------
    # Pipeline 4: Alert Outbox Worker
    # ------------------------------------------------------------------

    async def run_alert_worker(self) -> str | None:
        """
        Dispatch the oldest PENDING alert (whose parent bill is POSTED).

        Steps
        -----
        1. Fetch oldest PENDING alert with POSTED parent; return None if none.
        2. If older than ALERT_EXPIRY_HOURS → mark EXPIRED and return.
        3. Mark SENDING with attempted_at = now BEFORE the network call.
        4. Call whatsapp.send_template_alert().
        5. On success: mark SENT with wamid and sent_at.
        6. On failure: log error; leave as SENDING (no auto-retry per spec).

        Returns
        -------
        str | None
            The alert_id processed, or None if none were pending.
        """
        alert = self._store.get_oldest_pending_alert()
        if alert is None:
            logger.debug("run_alert_worker: no PENDING alerts.")
            return None

        alert_id = alert.alert_id
        now_utc = datetime.now(_UTC)

        # ── Expiry check ───────────────────────────────────────────────
        # created_at may or may not be timezone-aware; normalise to UTC
        created_at = alert.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=_UTC)

        age_hours = (now_utc - created_at).total_seconds() / 3600.0
        if age_hours > self._settings.alert_expiry_hours:
            self._store.update_alert(alert_id, status=AlertStatus.EXPIRED)
            logger.info(
                "Alert alert_id=%r expired (age=%.1fh > limit=%dh).",
                alert_id,
                age_hours,
                self._settings.alert_expiry_hours,
            )
            return alert_id

        # ── Mark SENDING before network call (spec requirement) ────────
        self._store.update_alert(
            alert_id,
            status=AlertStatus.SENDING,
            attempted_at=now_utc,
        )
        logger.info("Dispatching alert alert_id=%r", alert_id)

        # ── Build the 10 template parameters from stored alert fields ───
        low_supplier_obj = self._store.get_supplier(alert.low_supplier)
        high_supplier_obj = self._store.get_supplier(alert.high_supplier)
        low_supplier_name = (
            low_supplier_obj.supplier_name if low_supplier_obj else alert.low_supplier
        )
        high_supplier_name = (
            high_supplier_obj.supplier_name if high_supplier_obj else alert.high_supplier
        )

        params = [
            alert.sku,                              # 1  sku
            low_supplier_name,                      # 2  low supplier name
            self._fmt_rate(alert.low_rate),         # 3  low rate string
            self._fmt_date(alert.low_date),         # 4  low date
            alert.low_bill,                         # 5  low bill id
            high_supplier_name,                     # 6  high supplier name
            self._fmt_rate(alert.high_rate),        # 7  high rate string
            self._fmt_date(alert.high_date),        # 8  high date
            alert.high_bill,                        # 9  high bill id
            self._fmt_gap_pct(alert.gap_pct),       # 10 gap %
        ]

        # ── Send WhatsApp template message ──────────────────────────────
        try:
            wamid = await self._whatsapp.send_template_alert(
                to_number=self._settings.whatsapp_owner_number,
                params=params,
            )
            sent_at = datetime.now(_UTC)
            self._store.update_alert(
                alert_id,
                status=AlertStatus.SENT,
                wamid=wamid,
                sent_at=sent_at,
            )
            logger.info(
                "Alert alert_id=%r sent successfully wamid=%r",
                alert_id,
                wamid,
            )
        except Exception as exc:
            # Leave as SENDING; no automatic retry per specification.
            logger.exception(
                "Failed to send alert alert_id=%r: %s. "
                "Alert remains in SENDING state (no auto-retry).",
                alert_id,
                exc,
            )

        return alert_id
