"""
app/storage/sqlite_store.py
===========================
SQLite-backed implementation of ``BaseStore``.

Design decisions
----------------
* Uses Python's built-in ``sqlite3`` module — no third-party ORM.
* Thread safety: each call acquires a connection from a
  ``threading.local()`` cache so that the same thread always reuses the
  same connection (avoiding "objects created in a thread can only be
  used in that same thread" errors from SQLite's check_same_thread).
* All monetary amounts are stored as INTEGER paise.
* All datetime values are stored as ISO-8601 TEXT (UTC).
* All date values are stored as TEXT 'YYYY-MM-DD'.
* Enum fields are stored as their ``.value`` string.
* Parameterised queries only — no f-string SQL interpolation.
* ``PRAGMA journal_mode=WAL`` is set per-connection for better
  read/write concurrency.
* ``PRAGMA foreign_keys=ON`` is set per-connection for referential
  integrity enforcement.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from app.models import (
    Alert,
    AlertStatus,
    Bill,
    BillStatus,
    CostBasis,
    HeaderVerified,
    Product,
    ProductActive,
    Purchase,
    Reviewed,
    ReviewLine,
    Supplier,
    SupplierActive,
)
from app.storage.base import BaseStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dt_to_str(dt: Optional[datetime]) -> Optional[str]:
    """Convert a datetime to an ISO-8601 string (UTC), or None."""
    if dt is None:
        return None
    return dt.isoformat()


def _str_to_dt(s: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 string to a datetime, or return None."""
    if s is None:
        return None
    # Handle both 'Z' suffix and '+00:00' offset
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # Fallback for plain ISO without timezone
        return datetime.fromisoformat(s.rstrip("+00:00").rstrip("Z"))


def _date_to_str(d: Optional[date]) -> Optional[str]:
    """Convert a date to 'YYYY-MM-DD' string, or None."""
    if d is None:
        return None
    return d.isoformat()


def _str_to_date(s: Optional[str]) -> Optional[date]:
    """Parse a 'YYYY-MM-DD' string to a date, or return None."""
    if s is None:
        return None
    return date.fromisoformat(s)


def _decimal_to_str(d: Optional[Decimal]) -> Optional[str]:
    """Serialise a Decimal as a plain string for TEXT storage."""
    if d is None:
        return None
    return str(d)


def _str_to_decimal(s: Optional[str]) -> Optional[Decimal]:
    """Deserialise a TEXT column back to Decimal."""
    if s is None:
        return None
    return Decimal(s)


# ---------------------------------------------------------------------------
# Row → dataclass converters
# ---------------------------------------------------------------------------

def _row_to_bill(row: sqlite3.Row) -> Bill:
    return Bill(
        bill_id=row["bill_id"],
        sender=row["sender"],
        media_id=row["media_id"],
        mime_type=row["mime_type"],
        received_at=_str_to_dt(row["received_at"]),  # type: ignore[arg-type]
        status=BillStatus(row["status"]),
        scan_id=row["scan_id"],
        scan_started_at=_str_to_dt(row["scan_started_at"]),
        supplier_raw=row["supplier_raw"],
        invoice_no=row["invoice_no"],
        invoice_date=row["invoice_date"],
        currency=row["currency"],
        invoice_total=row["invoice_total"],
        notes=row["notes"],
        line_count=row["line_count"],
        supplier_id=row["supplier_id"],
        cost_basis=CostBasis(row["cost_basis"]) if row["cost_basis"] else None,
        verified_net_total=_str_to_decimal(row["verified_net_total"]),
        header_verified=HeaderVerified(row["header_verified"]) if row["header_verified"] else None,
        reviewer=row["reviewer"],
        posted_at=_str_to_dt(row["posted_at"]),
        reason=row["reason"],
    )


def _row_to_review_line(row: sqlite3.Row) -> ReviewLine:
    return ReviewLine(
        review_id=row["review_id"],
        bill_id=row["bill_id"],
        scan_id=row["scan_id"],
        line_no=row["line_no"],
        product_raw=row["product_raw"],
        pack_raw=row["pack_raw"],
        quantity_raw=row["quantity_raw"],
        unit_raw=row["unit_raw"],
        rate_raw=row["rate_raw"],
        line_total_raw=row["line_total_raw"],
        sku=row["sku"],
        received_units=row["received_units"],
        net_amount=_str_to_decimal(row["net_amount"]),
        reviewed=Reviewed(row["reviewed"]),
    )


def _row_to_supplier(row: sqlite3.Row) -> Supplier:
    return Supplier(
        supplier_id=row["supplier_id"],
        supplier_name=row["supplier_name"],
        active=SupplierActive(row["active"]),
    )


def _row_to_product(row: sqlite3.Row) -> Product:
    return Product(
        sku=row["sku"],
        product_name=row["product_name"],
        base_unit=row["base_unit"],
        active=ProductActive(row["active"]),
    )


def _row_to_purchase(row: sqlite3.Row) -> Purchase:
    return Purchase(
        purchase_id=row["purchase_id"],
        bill_id=row["bill_id"],
        sku=row["sku"],
        supplier_id=row["supplier_id"],
        invoice_no=row["invoice_no"],
        invoice_date=_str_to_date(row["invoice_date"]),  # type: ignore[arg-type]
        cost_basis=CostBasis(row["cost_basis"]),
        units=row["units"],
        net_paise=row["net_paise"],
        posted_at=_str_to_dt(row["posted_at"]),  # type: ignore[arg-type]
    )


def _row_to_alert(row: sqlite3.Row) -> Alert:
    return Alert(
        alert_id=row["alert_id"],
        bill_id=row["bill_id"],
        sku=row["sku"],
        low_supplier=row["low_supplier"],
        high_supplier=row["high_supplier"],
        low_date=_str_to_date(row["low_date"]),  # type: ignore[arg-type]
        high_date=_str_to_date(row["high_date"]),  # type: ignore[arg-type]
        low_bill=row["low_bill"],
        high_bill=row["high_bill"],
        low_rate=row["low_rate"],
        high_rate=row["high_rate"],
        gap_pct=row["gap_pct"],
        status=AlertStatus(row["status"]),
        created_at=_str_to_dt(row["created_at"]),  # type: ignore[arg-type]
        attempted_at=_str_to_dt(row["attempted_at"]),
        sent_at=_str_to_dt(row["sent_at"]),
        wamid=row["wamid"],
    )


# ---------------------------------------------------------------------------
# SQLiteStore
# ---------------------------------------------------------------------------

class SQLiteStore(BaseStore):
    """
    SQLite-backed implementation of ``BaseStore``.

    Parameters
    ----------
    db_path:
        File-system path to the SQLite database file, or ``":memory:"`` for
        an in-process in-memory database (useful for unit tests).
    """

    # Allowed field names per table, used by update_* methods to validate
    # caller-supplied keyword arguments and build safe UPDATE statements.
    _BILL_FIELDS: frozenset[str] = frozenset({
        "sender", "media_id", "mime_type", "received_at", "status",
        "scan_id", "scan_started_at", "supplier_raw", "invoice_no",
        "invoice_date", "currency", "invoice_total", "notes", "line_count",
        "supplier_id", "cost_basis", "verified_net_total", "header_verified",
        "reviewer", "posted_at", "reason",
    })

    _REVIEW_LINE_FIELDS: frozenset[str] = frozenset({
        "bill_id", "scan_id", "line_no", "product_raw", "pack_raw",
        "quantity_raw", "unit_raw", "rate_raw", "line_total_raw",
        "sku", "received_units", "net_amount", "reviewed",
    })

    _ALERT_FIELDS: frozenset[str] = frozenset({
        "bill_id", "sku", "low_supplier", "high_supplier", "low_date",
        "high_date", "low_bill", "high_bill", "low_rate", "high_rate",
        "gap_pct", "status", "created_at", "attempted_at", "sent_at", "wamid",
    })

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._shared_conn: Optional[sqlite3.Connection] = None
        if db_path == ":memory:":
            # In-memory SQLite: share a single connection across all threads so that
            # TestClient / worker threads access the identical database instance.
            self._shared_conn = sqlite3.connect(
                ":memory:",
                check_same_thread=False,
                detect_types=sqlite3.PARSE_DECLTYPES,
            )
            self._shared_conn.row_factory = sqlite3.Row
            self._shared_conn.execute("PRAGMA foreign_keys=ON")

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        """
        Return a per-thread SQLite connection, or the shared connection for in-memory databases.
        """
        if self._shared_conn is not None:
            return self._shared_conn

        conn: Optional[sqlite3.Connection] = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                detect_types=sqlite3.PARSE_DECLTYPES,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Close any open SQLite connections."""
        if self._shared_conn is not None:
            try:
                self._shared_conn.close()
            except Exception:
                pass
            self._shared_conn = None
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def create_tables(self) -> None:
        """Create all six tables if they do not already exist."""
        conn = self._get_conn()
        with conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS suppliers (
                    supplier_id   TEXT PRIMARY KEY,
                    supplier_name TEXT NOT NULL,
                    active        TEXT NOT NULL DEFAULT 'YES'
                );

                CREATE TABLE IF NOT EXISTS products (
                    sku           TEXT PRIMARY KEY,
                    product_name  TEXT NOT NULL,
                    base_unit     TEXT NOT NULL DEFAULT 'PACK',
                    active        TEXT NOT NULL DEFAULT 'YES'
                );

                CREATE TABLE IF NOT EXISTS bills (
                    bill_id              TEXT PRIMARY KEY,
                    sender               TEXT NOT NULL,
                    media_id             TEXT NOT NULL,
                    mime_type            TEXT NOT NULL,
                    received_at          TEXT NOT NULL,
                    status               TEXT NOT NULL DEFAULT 'QUEUED',
                    scan_id              TEXT,
                    scan_started_at      TEXT,
                    supplier_raw         TEXT,
                    invoice_no           TEXT,
                    invoice_date         TEXT,
                    currency             TEXT,
                    invoice_total        TEXT,
                    notes                TEXT,
                    line_count           INTEGER,
                    supplier_id          TEXT,
                    cost_basis           TEXT,
                    verified_net_total   TEXT,
                    header_verified      TEXT,
                    reviewer             TEXT,
                    posted_at            TEXT,
                    reason               TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_bills_status_received
                    ON bills (status, received_at);

                CREATE TABLE IF NOT EXISTS review_lines (
                    review_id       TEXT PRIMARY KEY,
                    bill_id         TEXT NOT NULL,
                    scan_id         TEXT NOT NULL,
                    line_no         INTEGER NOT NULL,
                    product_raw     TEXT NOT NULL,
                    pack_raw        TEXT NOT NULL,
                    quantity_raw    TEXT NOT NULL,
                    unit_raw        TEXT NOT NULL,
                    rate_raw        TEXT NOT NULL,
                    line_total_raw  TEXT NOT NULL,
                    sku             TEXT,
                    received_units  INTEGER,
                    net_amount      TEXT,
                    reviewed        TEXT NOT NULL DEFAULT 'NO'
                );

                CREATE INDEX IF NOT EXISTS idx_review_lines_bill_id
                    ON review_lines (bill_id, line_no);

                CREATE TABLE IF NOT EXISTS purchases (
                    purchase_id  TEXT PRIMARY KEY,
                    bill_id      TEXT NOT NULL,
                    sku          TEXT NOT NULL,
                    supplier_id  TEXT NOT NULL,
                    invoice_no   TEXT NOT NULL,
                    invoice_date TEXT NOT NULL,
                    cost_basis   TEXT NOT NULL,
                    units        INTEGER NOT NULL,
                    net_paise    INTEGER NOT NULL,
                    posted_at    TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_purchases_sku_posted
                    ON purchases (sku, posted_at);

                CREATE INDEX IF NOT EXISTS idx_purchases_supplier_invoice
                    ON purchases (supplier_id, invoice_no, invoice_date);

                CREATE TABLE IF NOT EXISTS alerts (
                    alert_id      TEXT PRIMARY KEY,
                    bill_id       TEXT NOT NULL,
                    sku           TEXT NOT NULL,
                    low_supplier  TEXT NOT NULL,
                    high_supplier TEXT NOT NULL,
                    low_date      TEXT NOT NULL,
                    high_date     TEXT NOT NULL,
                    low_bill      TEXT NOT NULL,
                    high_bill     TEXT NOT NULL,
                    low_rate      INTEGER NOT NULL,
                    high_rate     INTEGER NOT NULL,
                    gap_pct       REAL NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'PENDING',
                    created_at    TEXT NOT NULL,
                    attempted_at  TEXT,
                    sent_at       TEXT,
                    wamid         TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_alerts_status_created
                    ON alerts (status, created_at);
            """)

    def seed_demo_data(self) -> None:
        """
        Insert demo suppliers and products if they are not already present.
        Uses INSERT OR IGNORE so existing rows are never overwritten.
        """
        conn = self._get_conn()
        with conn:
            # --- Suppliers ---
            conn.execute(
                """
                INSERT OR IGNORE INTO suppliers (supplier_id, supplier_name, active)
                VALUES (?, ?, ?)
                """,
                ("SUP001", "Ramesh Traders", SupplierActive.YES.value),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO suppliers (supplier_id, supplier_name, active)
                VALUES (?, ?, ?)
                """,
                ("SUP002", "Suresh Wholesale", SupplierActive.YES.value),
            )

            # --- Products ---
            conn.execute(
                """
                INSERT OR IGNORE INTO products (sku, product_name, base_unit, active)
                VALUES (?, ?, ?, ?)
                """,
                ("BISCUIT-PARLEG-10", "Parle-G Biscuit 10pk", "PACK", ProductActive.YES.value),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO products (sku, product_name, base_unit, active)
                VALUES (?, ?, ?, ?)
                """,
                ("CHIPS-LAYS-26", "Lays Chips 26g", "PACK", ProductActive.YES.value),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO products (sku, product_name, base_unit, active)
                VALUES (?, ?, ?, ?)
                """,
                ("SOAP-LUX-100", "Lux Soap 100g", "BAR", ProductActive.YES.value),
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _serialise_value(key: str, value: Any) -> Any:
        """
        Convert a Python value to its SQLite-storable form.
        Enums → str, datetime → ISO str, date → 'YYYY-MM-DD', Decimal → str.
        """
        if value is None:
            return None
        # Enum subclasses of str already compare as str, but .value is safest
        if hasattr(value, "value") and isinstance(value, str.__class__.__mro__[0]):
            # str Enum
            return value
        if hasattr(value, "value"):
            return value.value
        if isinstance(value, datetime):
            return _dt_to_str(value)
        if isinstance(value, date):
            return _date_to_str(value)
        if isinstance(value, Decimal):
            return _decimal_to_str(value)
        return value

    @classmethod
    def _build_update_sql(
        cls,
        table: str,
        pk_col: str,
        allowed: frozenset[str],
        fields: dict[str, Any],
    ) -> tuple[str, list[Any]]:
        """
        Build a parameterised UPDATE statement for ``table``.

        Only keys present in ``allowed`` are accepted; unknown keys raise
        ``ValueError`` to surface programmer errors early.

        Returns ``(sql, params)`` ready to pass to ``cursor.execute``.
        """
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(
                f"Unknown field(s) for {table}: {unknown!r}. "
                f"Allowed: {allowed!r}"
            )
        if not fields:
            raise ValueError("update_* called with no fields to update.")

        set_clauses = ", ".join(f"{k} = ?" for k in fields)
        params: list[Any] = [cls._serialise_value(k, v) for k, v in fields.items()]
        sql = f"UPDATE {table} SET {set_clauses} WHERE {pk_col} = ?"  # noqa: S608
        return sql, params

    # ------------------------------------------------------------------
    # Bills
    # ------------------------------------------------------------------

    def insert_bill(self, bill: Bill) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
                """
                INSERT INTO bills (
                    bill_id, sender, media_id, mime_type, received_at,
                    status, scan_id, scan_started_at, supplier_raw,
                    invoice_no, invoice_date, currency, invoice_total,
                    notes, line_count, supplier_id, cost_basis,
                    verified_net_total, header_verified, reviewer,
                    posted_at, reason
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?
                )
                """,
                (
                    bill.bill_id,
                    bill.sender,
                    bill.media_id,
                    bill.mime_type,
                    _dt_to_str(bill.received_at),
                    bill.status.value,
                    bill.scan_id,
                    _dt_to_str(bill.scan_started_at),
                    bill.supplier_raw,
                    bill.invoice_no,
                    bill.invoice_date,
                    bill.currency,
                    bill.invoice_total,
                    bill.notes,
                    bill.line_count,
                    bill.supplier_id,
                    bill.cost_basis.value if bill.cost_basis else None,
                    _decimal_to_str(bill.verified_net_total),
                    bill.header_verified.value if bill.header_verified else None,
                    bill.reviewer,
                    _dt_to_str(bill.posted_at),
                    bill.reason,
                ),
            )

    def get_bill(self, bill_id: str) -> Optional[Bill]:
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM bills WHERE bill_id = ?", (bill_id,)
        )
        row = cur.fetchone()
        return _row_to_bill(row) if row else None

    def update_bill(self, bill_id: str, **fields) -> None:
        sql, params = self._build_update_sql(
            "bills", "bill_id", self._BILL_FIELDS, fields
        )
        params.append(bill_id)
        conn = self._get_conn()
        with conn:
            cur = conn.execute(sql, params)
        if cur.rowcount == 0:
            raise KeyError(f"Bill not found: {bill_id!r}")

    def list_bills(self, status: Optional[BillStatus] = None) -> list[Bill]:
        conn = self._get_conn()
        if status is None:
            cur = conn.execute(
                "SELECT * FROM bills ORDER BY received_at ASC"
            )
        else:
            cur = conn.execute(
                "SELECT * FROM bills WHERE status = ? ORDER BY received_at ASC",
                (status.value,),
            )
        return [_row_to_bill(row) for row in cur.fetchall()]

    def get_oldest_queued_bill(self) -> Optional[Bill]:
        conn = self._get_conn()
        cur = conn.execute(
            """
            SELECT * FROM bills
            WHERE  status = ?
            ORDER  BY received_at ASC
            LIMIT  1
            """,
            (BillStatus.QUEUED.value,),
        )
        row = cur.fetchone()
        return _row_to_bill(row) if row else None

    def get_oldest_approved_bill(self) -> Optional[Bill]:
        conn = self._get_conn()
        cur = conn.execute(
            """
            SELECT * FROM bills
            WHERE  status = ?
            ORDER  BY received_at ASC
            LIMIT  1
            """,
            (BillStatus.APPROVED.value,),
        )
        row = cur.fetchone()
        return _row_to_bill(row) if row else None

    # ------------------------------------------------------------------
    # ReviewLines
    # ------------------------------------------------------------------

    def insert_review_line(self, line: ReviewLine) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
                """
                INSERT INTO review_lines (
                    review_id, bill_id, scan_id, line_no,
                    product_raw, pack_raw, quantity_raw, unit_raw,
                    rate_raw, line_total_raw,
                    sku, received_units, net_amount, reviewed
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?
                )
                """,
                (
                    line.review_id,
                    line.bill_id,
                    line.scan_id,
                    line.line_no,
                    line.product_raw,
                    line.pack_raw,
                    line.quantity_raw,
                    line.unit_raw,
                    line.rate_raw,
                    line.line_total_raw,
                    line.sku,
                    line.received_units,
                    _decimal_to_str(line.net_amount),
                    line.reviewed.value,
                ),
            )

    def get_review_lines(self, bill_id: str) -> list[ReviewLine]:
        conn = self._get_conn()
        cur = conn.execute(
            """
            SELECT * FROM review_lines
            WHERE  bill_id = ?
            ORDER  BY line_no ASC
            """,
            (bill_id,),
        )
        return [_row_to_review_line(row) for row in cur.fetchall()]

    def update_review_line(self, review_id: str, **fields) -> None:
        sql, params = self._build_update_sql(
            "review_lines", "review_id", self._REVIEW_LINE_FIELDS, fields
        )
        params.append(review_id)
        conn = self._get_conn()
        with conn:
            cur = conn.execute(sql, params)
        if cur.rowcount == 0:
            raise KeyError(f"ReviewLine not found: {review_id!r}")

    def delete_review_lines(self, bill_id: str) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
                "DELETE FROM review_lines WHERE bill_id = ?", (bill_id,)
            )

    # ------------------------------------------------------------------
    # Suppliers
    # ------------------------------------------------------------------

    def get_supplier(self, supplier_id: str) -> Optional[Supplier]:
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM suppliers WHERE supplier_id = ?", (supplier_id,)
        )
        row = cur.fetchone()
        return _row_to_supplier(row) if row else None

    def list_suppliers(self, active_only: bool = True) -> list[Supplier]:
        conn = self._get_conn()
        if active_only:
            cur = conn.execute(
                "SELECT * FROM suppliers WHERE active = ? ORDER BY supplier_name ASC",
                (SupplierActive.YES.value,),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM suppliers ORDER BY supplier_name ASC"
            )
        return [_row_to_supplier(row) for row in cur.fetchall()]

    def upsert_supplier(self, supplier: Supplier) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
                """
                INSERT INTO suppliers (supplier_id, supplier_name, active)
                VALUES (?, ?, ?)
                ON CONFLICT(supplier_id) DO UPDATE SET
                    supplier_name = excluded.supplier_name,
                    active        = excluded.active
                """,
                (
                    supplier.supplier_id,
                    supplier.supplier_name,
                    supplier.active.value,
                ),
            )

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    def get_product(self, sku: str) -> Optional[Product]:
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM products WHERE sku = ?", (sku,)
        )
        row = cur.fetchone()
        return _row_to_product(row) if row else None

    def list_products(self, active_only: bool = True) -> list[Product]:
        conn = self._get_conn()
        if active_only:
            cur = conn.execute(
                "SELECT * FROM products WHERE active = ? ORDER BY product_name ASC",
                (ProductActive.YES.value,),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM products ORDER BY product_name ASC"
            )
        return [_row_to_product(row) for row in cur.fetchall()]

    def upsert_product(self, product: Product) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
                """
                INSERT INTO products (sku, product_name, base_unit, active)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(sku) DO UPDATE SET
                    product_name = excluded.product_name,
                    base_unit    = excluded.base_unit,
                    active       = excluded.active
                """,
                (
                    product.sku,
                    product.product_name,
                    product.base_unit,
                    product.active.value,
                ),
            )

    # ------------------------------------------------------------------
    # Purchases
    # ------------------------------------------------------------------

    def insert_purchase(self, purchase: Purchase) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
                """
                INSERT INTO purchases (
                    purchase_id, bill_id, sku, supplier_id,
                    invoice_no, invoice_date, cost_basis,
                    units, net_paise, posted_at
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?
                )
                """,
                (
                    purchase.purchase_id,
                    purchase.bill_id,
                    purchase.sku,
                    purchase.supplier_id,
                    purchase.invoice_no,
                    _date_to_str(purchase.invoice_date),
                    purchase.cost_basis.value,
                    purchase.units,
                    purchase.net_paise,
                    _dt_to_str(purchase.posted_at),
                ),
            )

    def get_purchases_by_bill(self, bill_id: str) -> list[Purchase]:
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM purchases WHERE bill_id = ?", (bill_id,)
        )
        return [_row_to_purchase(row) for row in cur.fetchall()]

    def get_purchases_for_sku_since(
        self,
        sku: str,
        since_dt: datetime,
        exclude_supplier: str,
    ) -> list[Purchase]:
        """
        Return EX_GST purchases for ``sku`` posted at or after ``since_dt``
        from any supplier except ``exclude_supplier``.
        Results are ordered by ``posted_at`` descending (most recent first).
        """
        conn = self._get_conn()
        cur = conn.execute(
            """
            SELECT * FROM purchases
            WHERE  sku          = ?
              AND  cost_basis   = ?
              AND  posted_at   >= ?
              AND  supplier_id != ?
            ORDER  BY posted_at DESC
            """,
            (
                sku,
                CostBasis.EX_GST.value,
                _dt_to_str(since_dt),
                exclude_supplier,
            ),
        )
        return [_row_to_purchase(row) for row in cur.fetchall()]

    def list_purchases(self, limit: int = 50) -> list[Purchase]:
        """Return recently posted purchases (up to limit)."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM purchases ORDER BY posted_at DESC LIMIT ?", (limit,)
        )
        return [_row_to_purchase(row) for row in cur.fetchall()]

    def get_purchases_for_sku(self, sku: str) -> list[Purchase]:
        """Return all posted purchases for the given SKU ordered by invoice_date ASC, posted_at ASC."""
        conn = self._get_conn()
        cur = conn.execute(
            """
            SELECT * FROM purchases
            WHERE sku = ?
            ORDER BY invoice_date ASC, posted_at ASC
            """,
            (sku,),
        )
        return [_row_to_purchase(row) for row in cur.fetchall()]

    def has_duplicate_invoice(
        self,
        supplier_id: str,
        fy_start: date,
        fy_end: date,
        invoice_no: str,
    ) -> bool:
        """
        Return True if a POSTED bill with the same supplier + invoice_no
        exists whose invoice_date falls within [fy_start, fy_end].

        The check joins bills and purchases so that only fully posted
        records are considered.
        """
        conn = self._get_conn()
        cur = conn.execute(
            """
            SELECT 1
            FROM   purchases p
            JOIN   bills     b ON b.bill_id = p.bill_id
            WHERE  p.supplier_id  = ?
              AND  p.invoice_no   = ?
              AND  p.invoice_date >= ?
              AND  p.invoice_date <= ?
              AND  b.status       = ?
            LIMIT  1
            """,
            (
                supplier_id,
                invoice_no,
                _date_to_str(fy_start),
                _date_to_str(fy_end),
                BillStatus.POSTED.value,
            ),
        )
        return cur.fetchone() is not None

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def insert_alert(self, alert: Alert) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
                """
                INSERT INTO alerts (
                    alert_id, bill_id, sku,
                    low_supplier, high_supplier,
                    low_date, high_date,
                    low_bill, high_bill,
                    low_rate, high_rate, gap_pct,
                    status, created_at,
                    attempted_at, sent_at, wamid
                ) VALUES (
                    ?, ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?, ?, ?
                )
                """,
                (
                    alert.alert_id,
                    alert.bill_id,
                    alert.sku,
                    alert.low_supplier,
                    alert.high_supplier,
                    _date_to_str(alert.low_date),
                    _date_to_str(alert.high_date),
                    alert.low_bill,
                    alert.high_bill,
                    alert.low_rate,
                    alert.high_rate,
                    alert.gap_pct,
                    alert.status.value,
                    _dt_to_str(alert.created_at),
                    _dt_to_str(alert.attempted_at),
                    _dt_to_str(alert.sent_at),
                    alert.wamid,
                ),
            )

    def get_oldest_pending_alert(self) -> Optional[Alert]:
        """
        Return the oldest PENDING alert whose parent bill is POSTED.
        The JOIN on bills ensures we never dispatch an alert for an
        unposted bill (which should not happen in practice, but guards
        against race conditions).
        """
        conn = self._get_conn()
        cur = conn.execute(
            """
            SELECT a.*
            FROM   alerts a
            JOIN   bills  b ON b.bill_id = a.bill_id
            WHERE  a.status = ?
              AND  b.status = ?
            ORDER  BY a.created_at ASC
            LIMIT  1
            """,
            (AlertStatus.PENDING.value, BillStatus.POSTED.value),
        )
        row = cur.fetchone()
        return _row_to_alert(row) if row else None

    def list_alerts(self, limit: int = 50) -> list[Alert]:
        """Return all alerts ordered by creation date descending (up to limit)."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [_row_to_alert(row) for row in cur.fetchall()]

    def update_alert(self, alert_id: str, **fields) -> None:
        sql, params = self._build_update_sql(
            "alerts", "alert_id", self._ALERT_FIELDS, fields
        )
        params.append(alert_id)
        conn = self._get_conn()
        with conn:
            cur = conn.execute(sql, params)
        if cur.rowcount == 0:
            raise KeyError(f"Alert not found: {alert_id!r}")
