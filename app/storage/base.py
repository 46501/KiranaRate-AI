"""
app/storage/base.py
===================
Abstract base class defining the full CRUD contract for all domain entities.

Every concrete storage backend (SQLite, Google Sheets, etc.) must subclass
``BaseStore`` and implement every abstract method.  The interface is intentionally
narrow — no ORM, no query builder — so it stays easy to implement and test.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Optional

from app.models import (
    Alert,
    Bill,
    BillStatus,
    Product,
    Purchase,
    ReviewLine,
    Supplier,
)


class BaseStore(ABC):
    """
    Abstract storage interface for the AI Bill Scanner.

    All methods that return a single entity return ``None`` when the record
    does not exist.  Methods that return a list always return a (possibly
    empty) list — never ``None``.

    Implementations must be thread-safe; the FastAPI server runs in a
    multi-threaded context.
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    def create_tables(self) -> None:
        """
        Create all required tables / sheets / collections if they do not
        already exist.  Safe to call on every application startup.
        """

    @abstractmethod
    def seed_demo_data(self) -> None:
        """
        Insert a minimal set of demo suppliers and products so the
        application is immediately usable after first setup.
        Records that already exist (by primary key) must not be overwritten.
        """

    def close(self) -> None:
        """Release any database connections or open file handles."""
        pass

    # ------------------------------------------------------------------
    # Bills
    # ------------------------------------------------------------------

    @abstractmethod
    def insert_bill(self, bill: Bill) -> None:
        """Persist a new Bill record.  Raises if ``bill_id`` already exists."""

    @abstractmethod
    def get_bill(self, bill_id: str) -> Optional[Bill]:
        """Return the Bill with the given ``bill_id``, or ``None``."""

    @abstractmethod
    def update_bill(self, bill_id: str, **fields) -> None:
        """
        Update arbitrary fields on an existing Bill.

        Only the keyword arguments supplied are written; all other fields
        are left unchanged.  Raises if ``bill_id`` does not exist.

        Example::

            store.update_bill(bill_id, status=BillStatus.SCANNING,
                              scan_id="abc", scan_started_at=datetime.utcnow())
        """

    @abstractmethod
    def list_bills(self, status: Optional[BillStatus] = None) -> list[Bill]:
        """
        Return all bills, optionally filtered by ``status``.
        Results are ordered by ``received_at`` ascending (oldest first).
        """

    @abstractmethod
    def get_oldest_queued_bill(self) -> Optional[Bill]:
        """
        Return the oldest bill (by ``received_at``) whose status is QUEUED,
        or ``None`` if none exist.
        """

    @abstractmethod
    def get_oldest_approved_bill(self) -> Optional[Bill]:
        """
        Return the oldest bill (by ``received_at``) whose status is APPROVED,
        or ``None`` if none exist.
        """

    # ------------------------------------------------------------------
    # ReviewLines
    # ------------------------------------------------------------------

    @abstractmethod
    def insert_review_line(self, line: ReviewLine) -> None:
        """Persist a new ReviewLine.  Raises if ``review_id`` already exists."""

    @abstractmethod
    def get_review_lines(self, bill_id: str) -> list[ReviewLine]:
        """
        Return all ReviewLines for the given ``bill_id``,
        ordered by ``line_no`` ascending.
        """

    @abstractmethod
    def update_review_line(self, review_id: str, **fields) -> None:
        """
        Update arbitrary fields on an existing ReviewLine.
        Raises if ``review_id`` does not exist.
        """

    @abstractmethod
    def delete_review_lines(self, bill_id: str) -> None:
        """
        Delete all ReviewLines associated with ``bill_id``.
        Used when a bill is re-scanned (old lines replaced by new ones).
        """

    # ------------------------------------------------------------------
    # Suppliers
    # ------------------------------------------------------------------

    @abstractmethod
    def get_supplier(self, supplier_id: str) -> Optional[Supplier]:
        """Return the Supplier with the given ``supplier_id``, or ``None``."""

    @abstractmethod
    def list_suppliers(self, active_only: bool = True) -> list[Supplier]:
        """
        Return all suppliers.
        When ``active_only`` is True (the default), only suppliers whose
        ``active`` field is ``SupplierActive.YES`` are returned.
        """

    @abstractmethod
    def upsert_supplier(self, supplier: Supplier) -> None:
        """
        Insert or replace a Supplier record.
        If a supplier with the same ``supplier_id`` already exists, all
        fields are overwritten with the supplied values.
        """

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    @abstractmethod
    def get_product(self, sku: str) -> Optional[Product]:
        """Return the Product with the given ``sku``, or ``None``."""

    @abstractmethod
    def list_products(self, active_only: bool = True) -> list[Product]:
        """
        Return all products.
        When ``active_only`` is True (the default), only products whose
        ``active`` field is ``ProductActive.YES`` are returned.
        """

    @abstractmethod
    def upsert_product(self, product: Product) -> None:
        """
        Insert or replace a Product record.
        If a product with the same ``sku`` already exists, all fields are
        overwritten with the supplied values.
        """

    # ------------------------------------------------------------------
    # Purchases
    # ------------------------------------------------------------------

    @abstractmethod
    def insert_purchase(self, purchase: Purchase) -> None:
        """Persist a new Purchase record.  Raises if ``purchase_id`` already exists."""

    @abstractmethod
    def get_purchases_by_bill(self, bill_id: str) -> list[Purchase]:
        """Return all Purchase records for the given ``bill_id``."""

    def list_purchases(self, limit: int = 50) -> list[Purchase]:
        """Return recently posted purchases (up to limit)."""
        return []

    def get_purchases_for_sku(self, sku: str) -> list[Purchase]:
        """Return all posted purchases for the given SKU ordered by invoice_date ascending."""
        return []

    @abstractmethod
    def get_purchases_for_sku_since(
        self,
        sku: str,
        since_dt: datetime,
        exclude_supplier: str,
    ) -> list[Purchase]:
        """
        Return all posted EX_GST Purchases for ``sku`` whose ``posted_at``
        is >= ``since_dt``, excluding purchases from ``exclude_supplier``.

        Used by the alert-generation logic to find historical comparison
        purchases within the last 30 days from other suppliers.

        Results are ordered by ``posted_at`` descending (most recent first).
        """

    @abstractmethod
    def has_duplicate_invoice(
        self,
        supplier_id: str,
        fy_start: date,
        fy_end: date,
        invoice_no: str,
    ) -> bool:
        """
        Return True if a POSTED bill already exists for the combination of
        ``supplier_id`` + ``invoice_no`` where the bill's ``invoice_date``
        falls within [``fy_start``, ``fy_end``] (inclusive).

        Used to prevent double-posting of the same invoice within an Indian
        financial year (April 1 – March 31).
        """

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    @abstractmethod
    def insert_alert(self, alert: Alert) -> None:
        """Persist a new Alert record.  Raises if ``alert_id`` already exists."""

    @abstractmethod
    def get_oldest_pending_alert(self) -> Optional[Alert]:
        """
        Return the oldest Alert (by ``created_at``) whose status is PENDING
        and whose parent bill has status POSTED, or ``None`` if none exist.
        """

    def list_alerts(self, limit: int = 50) -> list[Alert]:
        """Return all alerts ordered by creation date descending (up to limit)."""
        return []

    @abstractmethod
    def update_alert(self, alert_id: str, **fields) -> None:
        """
        Update arbitrary fields on an existing Alert.
        Raises if ``alert_id`` does not exist.
        """
