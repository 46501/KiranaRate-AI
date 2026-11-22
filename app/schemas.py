"""
app/schemas.py
==============
Pydantic models used for:
  - FastAPI request / response serialisation (API layer)
  - Gemini Vision output validation (AI Scan pipeline)
  - WhatsApp template parameter construction (Alert Outbox)

All models derive from ``pydantic.BaseModel`` with ``model_config`` set to
``populate_by_name=True`` so that field aliases can be used freely alongside
the Python attribute names.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import AlertStatus, BillStatus, CostBasis, HeaderVerified, Reviewed


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------


class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, use_enum_values=True)


# ---------------------------------------------------------------------------
# Gemini Vision output schema
# ---------------------------------------------------------------------------


class GeminiLineItem(_Base):
    """
    Represents one line item as returned by Gemini Vision.

    All values are raw strings exactly as they appear on the bill image.
    Downstream validation (Verify pipeline) normalises them.
    """

    line_no: int = Field(..., ge=1, description="1-based line number.")
    product_raw: str = Field(..., description="Product / item description from the bill.")
    pack_raw: str = Field(..., description="Pack size / variant string, e.g. '500g', '1L'.")
    quantity_raw: str = Field(..., description="Quantity ordered/delivered as a raw string.")
    unit_raw: str = Field(..., description="Unit of measure, e.g. 'PCS', 'KG', 'BTL'.")
    rate_raw: str = Field(..., description="Unit rate as printed on the bill.")
    line_total_raw: str = Field(..., description="Line total as printed on the bill.")

    @field_validator("product_raw", "pack_raw", "quantity_raw", "unit_raw", "rate_raw", "line_total_raw", mode="before")
    @classmethod
    def _strip_strings(cls, v: object) -> str:
        return str(v).strip() if v is not None else ""


class GeminiScanResult(_Base):
    """
    Top-level JSON object that Gemini Vision must return for every bill scan.

    The schema is passed verbatim to the Gemini API as the ``response_schema``
    so that the model is constrained to produce exactly this structure.

    All header fields are raw strings; ``lines`` contains 1–75 line items.
    """

    supplier_raw: str = Field(..., description="Supplier name as printed on the bill.")
    invoice_no: str = Field(..., description="Invoice / bill number as printed.")
    invoice_date: str = Field(
        ...,
        description="Invoice date in YYYY-MM-DD format. Convert from any printed format.",
    )
    currency: str = Field(
        ...,
        description="Three-letter ISO-4217 currency code, e.g. 'INR'.",
    )
    invoice_total: str = Field(..., description="Grand total as printed on the bill.")
    notes: str = Field(default="", description="Any relevant notes or remarks from the bill.")
    lines: List[GeminiLineItem] = Field(
        ...,
        min_length=1,
        max_length=75,
        description="Ordered list of line items; must contain between 1 and 75 items.",
    )

    @field_validator("lines")
    @classmethod
    def _validate_line_numbers(cls, lines: List[GeminiLineItem]) -> List[GeminiLineItem]:
        """Ensure line_no values are unique and 1-based sequential."""
        seen: set[int] = set()
        for item in lines:
            if item.line_no in seen:
                raise ValueError(f"Duplicate line_no {item.line_no} in Gemini response.")
            seen.add(item.line_no)
        return sorted(lines, key=lambda x: x.line_no)


# ---------------------------------------------------------------------------
# API response schemas
# ---------------------------------------------------------------------------


class ReviewLineResponse(_Base):
    """
    Read-only view of a single ReviewLine record returned by the API.
    """

    review_id: str
    bill_id: str
    scan_id: str
    line_no: int

    # Raw scan fields
    product_raw: str
    pack_raw: str
    quantity_raw: str
    unit_raw: str
    rate_raw: str
    line_total_raw: str

    # Resolved fields (may be None before reviewer action)
    sku: Optional[str] = None
    received_units: Optional[int] = None
    net_amount: Optional[Decimal] = None
    reviewed: Reviewed = Reviewed.NO


class BillResponse(_Base):
    """
    Read-only view of a Bill record returned by the API.

    ``lines`` is included when fetching a single bill detail; omitted in list views.
    """

    bill_id: str
    sender: str
    media_id: str
    mime_type: str
    received_at: datetime
    status: BillStatus

    scan_id: Optional[str] = None
    scan_started_at: Optional[datetime] = None

    # Scan output
    supplier_raw: Optional[str] = None
    invoice_no: Optional[str] = None
    invoice_date: Optional[str] = None
    currency: Optional[str] = None
    invoice_total: Optional[str] = None
    notes: Optional[str] = None
    line_count: Optional[int] = None

    # Resolved / posted fields
    supplier_id: Optional[str] = None
    cost_basis: Optional[CostBasis] = None
    verified_net_total: Optional[Decimal] = None
    header_verified: Optional[HeaderVerified] = None
    reviewer: Optional[str] = None
    posted_at: Optional[datetime] = None
    reason: Optional[str] = None

    # Nested detail — populated only on single-bill GET
    lines: Optional[List[ReviewLineResponse]] = None


# ---------------------------------------------------------------------------
# API request schemas
# ---------------------------------------------------------------------------


class LineUpdateItem(_Base):
    """Update details for an individual review line when approving a bill."""

    review_id: str = Field(..., description="Unique ID of the ReviewLine.")
    sku: str = Field(..., min_length=1, description="Mapped SKU.")
    received_units: int = Field(..., gt=0, description="Positive integer received units.")
    net_amount: Decimal = Field(..., gt=Decimal("0"), description="Positive net line amount (EX_GST).")
    reviewed: Reviewed = Field(default=Reviewed.YES, description="Mark as reviewed.")


class ApproveRequest(_Base):
    """
    Payload sent by a reviewer to approve a bill and move it to APPROVED.

    All fields are required; the Verify/Post pipeline enforces deeper
    business-rule validation before accepting the bill as POSTED.
    """

    reviewer: str = Field(..., min_length=1, description="Username of the reviewer approving the bill.")
    supplier_id: str = Field(..., min_length=1, description="Resolved supplier ID (must exist and be active).")
    cost_basis: CostBasis = Field(..., description="Cost basis for purchase recording; must be EX_GST.")
    verified_net_total: Decimal = Field(
        ...,
        gt=Decimal("0"),
        description="Reviewer-confirmed net total in INR (must be positive).",
    )
    header_verified: HeaderVerified = Field(
        ...,
        description="Confirmation that the bill header matches the original image.",
    )
    invoice_date: date = Field(..., description="Validated invoice date in YYYY-MM-DD format.")
    invoice_no: str = Field(..., min_length=1, description="Invoice number, confirmed by reviewer.")
    currency: str = Field(
        default="INR",
        min_length=3,
        max_length=3,
        description="ISO-4217 currency code; must be 'INR'.",
    )
    line_updates: Optional[List[LineUpdateItem]] = Field(
        default=None,
        description="Optional line item updates (SKU, units, net amount) submitted during approval.",
    )

    @field_validator("currency")
    @classmethod
    def _currency_must_be_inr(cls, v: str) -> str:
        if v.upper() != "INR":
            raise ValueError("Only INR bills are supported at this time.")
        return v.upper()

    @field_validator("invoice_date")
    @classmethod
    def _date_not_in_future(cls, v: date) -> date:
        from datetime import date as _date

        if v > _date.today():
            raise ValueError("invoice_date cannot be in the future.")
        return v


class RejectRequest(_Base):
    """
    Payload sent by a reviewer to permanently reject a bill.
    """

    reviewer: str = Field(..., min_length=1, description="Username of the reviewer rejecting the bill.")
    reason: str = Field(
        ...,
        min_length=1,
        description="Human-readable reason for rejection; stored on the bill record.",
    )


# ---------------------------------------------------------------------------
# WhatsApp template alert parameters
# ---------------------------------------------------------------------------


class AlertParams(_Base):
    """
    Ten ordered string parameters for the ``purchase_rate_review`` (en) WhatsApp
    message template per specification:
        {{1}}  SKU
        {{2}}  Low supplier name
        {{3}}  Low rate (formatted, e.g. "Rs 46.00/unit")
        {{4}}  Low invoice date (e.g. "15-Nov-2026")
        {{5}}  Low bill ID
        {{6}}  High supplier name
        {{7}}  High rate (formatted, e.g. "Rs 50.00/unit")
        {{8}}  High invoice date (e.g. "20-Nov-2026")
        {{9}}  High bill ID
        {{10}} Gap percentage (e.g. "8.00%")
    """

    param_1_sku: str = Field(..., description="SKU code / product identifier.")
    param_2_low_supplier: str = Field(..., description="Name of supplier with lower rate.")
    param_3_low_rate: str = Field(..., description="Formatted lower rate, e.g. 'Rs 46.00/unit'.")
    param_4_low_date: str = Field(..., description="Invoice date of lower-rate purchase.")
    param_5_low_bill: str = Field(..., description="Bill ID of lower-rate purchase.")
    param_6_high_supplier: str = Field(..., description="Name of supplier with higher rate.")
    param_7_high_rate: str = Field(..., description="Formatted higher rate, e.g. 'Rs 50.00/unit'.")
    param_8_high_date: str = Field(..., description="Invoice date of higher-rate purchase.")
    param_9_high_bill: str = Field(..., description="Bill ID of higher-rate purchase.")
    param_10_gap_pct: str = Field(..., description="Gap percentage, e.g. '8.00%'.")

    def to_ordered_list(self) -> List[str]:
        """Return parameters as an ordered list for the WhatsApp API payload."""
        return [
            self.param_1_sku,
            self.param_2_low_supplier,
            self.param_3_low_rate,
            self.param_4_low_date,
            self.param_5_low_bill,
            self.param_6_high_supplier,
            self.param_7_high_rate,
            self.param_8_high_date,
            self.param_9_high_bill,
            self.param_10_gap_pct,
        ]

    def to_whatsapp_components(self) -> List[dict]:
        """
        Render the WhatsApp ``components`` payload fragment expected by the
        Cloud API ``/messages`` endpoint for a template message.

        Returns a list containing a single ``body`` component with 10
        ``text`` parameters.
        """
        return [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": value}
                    for value in self.to_ordered_list()
                ],
            }
        ]
