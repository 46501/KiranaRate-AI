"""
app/models.py
=============
Domain entity definitions for the AI Bill Scanner.

All entities are plain Python dataclasses.  Enums capture every finite-valued
field described in the specification so that invalid states can never be
accidentally stored or compared.

Monetary amounts that represent *rates* (paise per unit) are stored as ``int``
to avoid floating-point drift.  Quantities and totals that originate from raw
scan text keep their ``Decimal`` type until the Verify pipeline converts them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class BillStatus(str, Enum):
    """Lifecycle states of a Bill record."""

    QUEUED = "QUEUED"
    """Bill image received; waiting for the AI Scan pipeline."""

    SCANNING = "SCANNING"
    """AI Scan pipeline is actively processing this bill."""

    REVIEW = "REVIEW"
    """Scan complete; awaiting human review and approval."""

    APPROVED = "APPROVED"
    """Reviewer approved; waiting for the Verify/Post pipeline."""

    POSTED = "POSTED"
    """Successfully validated and posted to the purchase ledger."""

    REJECTED = "REJECTED"
    """Permanently rejected by a reviewer or by an unrecoverable error."""


class AlertStatus(str, Enum):
    """Lifecycle states of a price-comparison Alert record."""

    PENDING = "PENDING"
    """Alert generated; not yet sent."""

    SENDING = "SENDING"
    """WhatsApp API call in-flight (set *before* the network call)."""

    SENT = "SENT"
    """WhatsApp message delivered; wamid recorded."""

    EXPIRED = "EXPIRED"
    """Alert was not sent within ALERT_EXPIRY_HOURS; permanently discarded."""


class CostBasis(str, Enum):
    """Supported cost-basis values for purchase recording."""

    EX_GST = "EX_GST"
    """Amount excluding GST — the only basis currently supported."""


class HeaderVerified(str, Enum):
    """Whether the reviewer confirmed the bill header matches the original image."""

    YES = "YES"
    NO = "NO"


class Reviewed(str, Enum):
    """Whether an individual ReviewLine has been reviewed by a human."""

    YES = "YES"
    NO = "NO"


class SupplierActive(str, Enum):
    """Whether a Supplier is active and eligible for new bills."""

    YES = "YES"
    NO = "NO"


class ProductActive(str, Enum):
    """Whether a Product/SKU is active and eligible for purchase recording."""

    YES = "YES"
    NO = "NO"


# ---------------------------------------------------------------------------
# Entity dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Bill:
    """
    Represents a single supplier bill submitted via WhatsApp.

    ``bill_id`` is the WhatsApp ``message.id`` and acts as the primary key.
    Fields suffixed ``_raw`` are populated from the Gemini scan output and
    may contain uncleaned strings.
    """

    # --- Identity & transport ---
    bill_id: str
    """WhatsApp message ID; primary key."""

    sender: str
    """E.164 phone number of the WhatsApp sender."""

    media_id: str
    """WhatsApp media object ID used to download the image."""

    mime_type: str
    """MIME type of the received image, e.g. 'image/jpeg'."""

    received_at: datetime
    """UTC timestamp when the webhook delivered the message."""

    # --- Pipeline state ---
    status: BillStatus = BillStatus.QUEUED

    scan_id: Optional[str] = None
    """Unique ID assigned to each Gemini scan attempt."""

    scan_started_at: Optional[datetime] = None
    """UTC timestamp when the AI Scan pipeline picked up this bill."""

    # --- Scan output (raw strings from Gemini) ---
    supplier_raw: Optional[str] = None
    invoice_no: Optional[str] = None
    invoice_date: Optional[str] = None
    """ISO-8601 date string as extracted by Gemini, e.g. '2024-07-15'."""

    currency: Optional[str] = None
    invoice_total: Optional[str] = None
    notes: Optional[str] = None
    line_count: Optional[int] = None

    # --- Resolved / validated fields (set during Verify pipeline) ---
    supplier_id: Optional[str] = None
    """Foreign key to Suppliers.supplier_id; resolved from supplier_raw."""

    cost_basis: Optional[CostBasis] = None
    verified_net_total: Optional[Decimal] = None
    """Net total in INR confirmed by the reviewer."""

    header_verified: Optional[HeaderVerified] = None
    reviewer: Optional[str] = None
    """Username or identifier of the human reviewer."""

    posted_at: Optional[datetime] = None
    """UTC timestamp when the bill was successfully posted."""

    reason: Optional[str] = None
    """Human-readable reason when a bill is sent back to REVIEW or REJECTED."""


@dataclass
class ReviewLine:
    """
    A single line item extracted from a bill by the AI Scan pipeline.

    ``review_id`` is a surrogate key; ``(bill_id, scan_id, line_no)`` is the
    natural composite key within a scan attempt.
    """

    review_id: str
    """Surrogate primary key (e.g. a UUID)."""

    bill_id: str
    """Foreign key to Bills.bill_id."""

    scan_id: str
    """Scan attempt ID; matches Bill.scan_id."""

    line_no: int
    """1-based line number within the scan output."""

    # --- Raw strings directly from Gemini ---
    product_raw: str
    pack_raw: str
    quantity_raw: str
    unit_raw: str
    rate_raw: str
    line_total_raw: str

    # --- Resolved / validated fields ---
    sku: Optional[str] = None
    """Foreign key to Products.sku; resolved by a reviewer."""

    received_units: Optional[int] = None
    """Positive integer number of units actually received."""

    net_amount: Optional[Decimal] = None
    """Net line amount in INR (EX_GST); must be positive."""

    reviewed: Reviewed = Reviewed.NO


@dataclass
class Supplier:
    """A known supplier whose bills the system accepts."""

    supplier_id: str
    """Primary key; short slug, e.g. 'RAMESH_TRADERS'."""

    supplier_name: str
    """Display name shown in alerts and the UI."""

    active: SupplierActive = SupplierActive.YES


@dataclass
class Product:
    """
    A product / SKU in the master catalogue.

    ``sku`` uniquely identifies a specific product at a specific pack size.
    Different pack sizes of the same product are intentionally *separate* SKUs
    — the system must never auto-merge them.
    """

    sku: str
    """Primary key; e.g. 'AMUL_BUTTER_500G'."""

    product_name: str
    """Human-readable display name."""

    base_unit: str = "PACK"
    """Base unit of measure for this SKU, e.g. 'PACK', 'KG', 'LITRE'."""

    active: ProductActive = ProductActive.YES


@dataclass
class Purchase:
    """
    A posted purchase record aggregated per SKU from a verified bill.

    All rate comparisons use ``net_paise`` / ``units`` to derive paise-per-unit.
    ``net_paise`` is an integer to prevent floating-point drift.
    """

    purchase_id: str
    """Surrogate primary key (e.g. a UUID)."""

    bill_id: str
    """Foreign key to Bills.bill_id."""

    sku: str
    """Foreign key to Products.sku."""

    supplier_id: str
    """Foreign key to Suppliers.supplier_id."""

    invoice_no: str
    invoice_date: date
    """Validated invoice date as a Python date object."""

    cost_basis: CostBasis
    """Always EX_GST for posted purchases."""

    units: int
    """Total units purchased across all lines for this SKU on this bill."""

    net_paise: int
    """
    Total net amount in integer paise (net_amount_INR * 100, rounded).
    Paise-per-unit rate = net_paise // units.
    """

    posted_at: datetime
    """UTC timestamp when this purchase record was written."""


@dataclass
class Alert:
    """
    A price-comparison alert generated when a newly posted bill reveals a
    significant rate discrepancy for a SKU relative to the last 30 days.

    ``low_*`` fields refer to the cheaper purchase; ``high_*`` to the dearer one.
    ``gap_pct`` = 100 × (high_rate − low_rate) / high_rate, stored as a float
    for display purposes only.  The trigger threshold is evaluated on integer paise.
    """

    alert_id: str
    """Surrogate primary key (e.g. a UUID)."""

    bill_id: str
    """Foreign key to the *new* bill that triggered this alert."""

    sku: str
    """Foreign key to Products.sku."""

    # --- Comparison pair ---
    low_supplier: str
    """supplier_id with the lower (cheaper) rate."""

    high_supplier: str
    """supplier_id with the higher (dearer) rate."""

    low_date: date
    """invoice_date of the cheaper purchase."""

    high_date: date
    """invoice_date of the dearer purchase."""

    low_bill: str
    """bill_id of the cheaper purchase."""

    high_bill: str
    """bill_id of the dearer purchase."""

    low_rate: int
    """Rate of the cheaper purchase in integer paise per unit."""

    high_rate: int
    """Rate of the dearer purchase in integer paise per unit."""

    gap_pct: float
    """100 × (high_rate − low_rate) / high_rate; for display only."""

    # --- Outbox state ---
    status: AlertStatus = AlertStatus.PENDING

    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    attempted_at: Optional[datetime] = None
    """UTC timestamp set *before* the WhatsApp API call is made."""

    sent_at: Optional[datetime] = None
    """UTC timestamp set when a wamid is received from WhatsApp."""

    wamid: Optional[str] = None
    """WhatsApp message ID returned on successful delivery."""
