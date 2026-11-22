"""
sample_data/seed.py
===================
Populates the database with realistic seed fixtures so that developers can
exercise the full pipeline locally without real WhatsApp messages or a live
Gemini API key.

Usage
-----
    # From the project root with the virtual-env active:
    python sample_data/seed.py

The script is idempotent: re-running it will skip records that already exist
(it uses INSERT OR IGNORE for SQLite).  Set STORAGE_BACKEND=sqlite in .env
(the default) before running.

Seed data overview
------------------
Suppliers  : 3  (Ramesh Traders, Suresh Wholesale, Priya Distributors)
Products   : 5  (various FMCG SKUs with different pack sizes)
Bills      : 4  (one per status: QUEUED, REVIEW, APPROVED, POSTED)
ReviewLines: 6  (attached to the REVIEW and POSTED bills)
Purchases  : 2  (aggregated per SKU for the POSTED bill)
Alerts     : 1  (PENDING — price gap between Ramesh and Suresh for AMUL_BUTTER_500G)
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

# ---------------------------------------------------------------------------
# Make sure we can import from the project root regardless of CWD.
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings  # noqa: E402  (import after sys.path tweak)
from app.models import (  # noqa: E402
    AlertStatus,
    BillStatus,
    CostBasis,
    HeaderVerified,
    ProductActive,
    Reviewed,
    SupplierActive,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime.now(timezone.utc)
TODAY = date.today()
IST_TODAY = TODAY  # For seed purposes we use UTC date; real code uses pytz.


def _uid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Seed fixtures
# ---------------------------------------------------------------------------

SUPPLIERS = [
    ("SUP_RAMESH", "Ramesh Traders", SupplierActive.YES.value),
    ("SUP_SURESH", "Suresh Wholesale", SupplierActive.YES.value),
    ("SUP_PRIYA", "Priya Distributors", SupplierActive.NO.value),
]

PRODUCTS = [
    ("AMUL_BUTTER_500G", "Amul Butter 500g", "PACK", ProductActive.YES.value),
    ("AMUL_MILK_1L", "Amul Toned Milk 1L", "LITRE", ProductActive.YES.value),
    ("BRITANNIA_BREAD_400G", "Britannia Brown Bread 400g", "PACK", ProductActive.YES.value),
    ("PARLE_G_800G", "Parle-G Biscuits 800g", "PACK", ProductActive.YES.value),
    ("HORLICKS_500G", "Horlicks 500g", "PACK", ProductActive.NO.value),
]

# --- Bills (one per interesting status) ---

BILL_QUEUED_ID = "wamid.queued001"
BILL_REVIEW_ID = "wamid.review001"
BILL_APPROVED_ID = "wamid.approved01"
BILL_POSTED_ID = "wamid.posted001"

BILLS = [
    # (bill_id, sender, media_id, mime_type, received_at, status,
    #  scan_id, scan_started_at, supplier_raw, invoice_no, invoice_date,
    #  currency, invoice_total, notes, line_count,
    #  supplier_id, cost_basis, verified_net_total, header_verified,
    #  reviewer, posted_at, reason)
    (
        BILL_QUEUED_ID, "919876543210", "media_queued_001", "image/jpeg",
        NOW - timedelta(minutes=5), BillStatus.QUEUED.value,
        None, None, None, None, None,
        None, None, None, None,
        None, None, None, None,
        None, None, None,
    ),
    (
        BILL_REVIEW_ID, "919876543210", "media_review_001", "image/jpeg",
        NOW - timedelta(hours=2), BillStatus.REVIEW.value,
        "scan_review_001", NOW - timedelta(hours=1, minutes=55),
        "Ramesh Traders", "INV-2024-0781",
        str(TODAY - timedelta(days=2)),
        "INR", "4500.00", "Partial delivery — 2 items short", 3,
        None, None, None, None,
        None, None, None,
    ),
    (
        BILL_APPROVED_ID, "919123456789", "media_approved_01", "image/png",
        NOW - timedelta(hours=5), BillStatus.APPROVED.value,
        "scan_approved_01", NOW - timedelta(hours=4, minutes=50),
        "Suresh Wholesale", "SW/24-25/0042",
        str(TODAY - timedelta(days=1)),
        "INR", "9800.00", "", 2,
        "SUP_SURESH", CostBasis.EX_GST.value, Decimal("9800.00"),
        HeaderVerified.YES.value,
        "reviewer_alice", None, None,
    ),
    (
        BILL_POSTED_ID, "919876543210", "media_posted_001", "image/jpeg",
        NOW - timedelta(days=3), BillStatus.POSTED.value,
        "scan_posted_001", NOW - timedelta(days=3, hours=0, minutes=55),
        "Ramesh Traders", "RT/24-25/1001",
        str(TODAY - timedelta(days=3)),
        "INR", "7250.00", "", 2,
        "SUP_RAMESH", CostBasis.EX_GST.value, Decimal("7250.00"),
        HeaderVerified.YES.value,
        "reviewer_bob", NOW - timedelta(days=3), None,
    ),
]

# --- ReviewLines ---

RL_REVIEW_1_ID = _uid()
RL_REVIEW_2_ID = _uid()
RL_REVIEW_3_ID = _uid()
RL_POSTED_1_ID = _uid()
RL_POSTED_2_ID = _uid()

REVIEW_LINES = [
    # (review_id, bill_id, scan_id, line_no,
    #  product_raw, pack_raw, quantity_raw, unit_raw, rate_raw, line_total_raw,
    #  sku, received_units, net_amount, reviewed)
    (
        RL_REVIEW_1_ID, BILL_REVIEW_ID, "scan_review_001", 1,
        "Amul Butter", "500g", "10", "PACK", "270.00", "2700.00",
        "AMUL_BUTTER_500G", None, None, Reviewed.NO.value,
    ),
    (
        RL_REVIEW_2_ID, BILL_REVIEW_ID, "scan_review_001", 2,
        "Amul Toned Milk", "1 Litre", "50", "LITRE", "28.00", "1400.00",
        "AMUL_MILK_1L", None, None, Reviewed.NO.value,
    ),
    (
        RL_REVIEW_3_ID, BILL_REVIEW_ID, "scan_review_001", 3,
        "Parle G Biscuits", "800g", "5", "PACK", "80.00", "400.00",
        None, None, None, Reviewed.NO.value,
    ),
    # Posted bill — both lines fully reviewed
    (
        RL_POSTED_1_ID, BILL_POSTED_ID, "scan_posted_001", 1,
        "Amul Butter", "500gm", "20", "PACK", "280.00", "5600.00",
        "AMUL_BUTTER_500G", 20, Decimal("5600.00"), Reviewed.YES.value,
    ),
    (
        RL_POSTED_2_ID, BILL_POSTED_ID, "scan_posted_001", 2,
        "Britannia Brown Bread", "400g", "15", "PACK", "110.00", "1650.00",
        "BRITANNIA_BREAD_400G", 15, Decimal("1650.00"), Reviewed.YES.value,
    ),
]

# --- Purchases (aggregated from POSTED bill) ---

PURCHASES = [
    # (purchase_id, bill_id, sku, supplier_id, invoice_no, invoice_date,
    #  cost_basis, units, net_paise, posted_at)
    (
        _uid(), BILL_POSTED_ID, "AMUL_BUTTER_500G", "SUP_RAMESH",
        "RT/24-25/1001", str(TODAY - timedelta(days=3)),
        CostBasis.EX_GST.value, 20,
        560000,  # 5600.00 INR * 100 paise = 560000 paise  (28000 paise/unit)
        NOW - timedelta(days=3),
    ),
    (
        _uid(), BILL_POSTED_ID, "BRITANNIA_BREAD_400G", "SUP_RAMESH",
        "RT/24-25/1001", str(TODAY - timedelta(days=3)),
        CostBasis.EX_GST.value, 15,
        165000,  # 1650.00 INR * 100 = 165000 paise  (11000 paise/unit)
        NOW - timedelta(days=3),
    ),
]

# --- Alert: price gap for AMUL_BUTTER_500G between Ramesh (28000 p/u) and
#     hypothetical Suresh rate of 25200 p/u (₹252/unit).
#     high_rate=28000, low_rate=25200
#     gap_pct = 100 * (28000 - 25200) / 28000 ≈ 10.00 %  (> 2.00 threshold)
#     gap_paise = 2800  (> 1 paise threshold)  -> alert fires.

ALERT_ID = _uid()

ALERTS = [
    # (alert_id, bill_id, sku, low_supplier, high_supplier,
    #  low_date, high_date, low_bill, high_bill,
    #  low_rate, high_rate, gap_pct, status,
    #  created_at, attempted_at, sent_at, wamid)
    (
        ALERT_ID,
        BILL_POSTED_ID,
        "AMUL_BUTTER_500G",
        "SUP_SURESH",   # cheaper
        "SUP_RAMESH",   # dearer
        str(TODAY - timedelta(days=5)),  # low_date (hypothetical previous Suresh bill)
        str(TODAY - timedelta(days=3)),  # high_date (the posted Ramesh bill)
        "wamid.suresh_prev_01",           # low_bill
        BILL_POSTED_ID,                   # high_bill
        25200,   # low_rate  (paise/unit)
        28000,   # high_rate (paise/unit)
        10.00,   # gap_pct
        AlertStatus.PENDING.value,
        NOW - timedelta(days=3),
        None,
        None,
        None,
    ),
]

# ---------------------------------------------------------------------------
# DDL — minimal schema for seeding (the real DDL lives in app/storage)
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id   TEXT PRIMARY KEY,
    supplier_name TEXT NOT NULL,
    active        TEXT NOT NULL DEFAULT 'YES'
);

CREATE TABLE IF NOT EXISTS products (
    sku          TEXT PRIMARY KEY,
    product_name TEXT NOT NULL,
    base_unit    TEXT NOT NULL DEFAULT 'PACK',
    active       TEXT NOT NULL DEFAULT 'YES'
);

CREATE TABLE IF NOT EXISTS bills (
    bill_id            TEXT PRIMARY KEY,
    sender             TEXT NOT NULL,
    media_id           TEXT NOT NULL,
    mime_type          TEXT NOT NULL,
    received_at        TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'QUEUED',
    scan_id            TEXT,
    scan_started_at    TEXT,
    supplier_raw       TEXT,
    invoice_no         TEXT,
    invoice_date       TEXT,
    currency           TEXT,
    invoice_total      TEXT,
    notes              TEXT,
    line_count         INTEGER,
    supplier_id        TEXT REFERENCES suppliers(supplier_id),
    cost_basis         TEXT,
    verified_net_total TEXT,
    header_verified    TEXT,
    reviewer           TEXT,
    posted_at          TEXT,
    reason             TEXT
);

CREATE TABLE IF NOT EXISTS review_lines (
    review_id      TEXT PRIMARY KEY,
    bill_id        TEXT NOT NULL REFERENCES bills(bill_id),
    scan_id        TEXT NOT NULL,
    line_no        INTEGER NOT NULL,
    product_raw    TEXT NOT NULL,
    pack_raw       TEXT NOT NULL,
    quantity_raw   TEXT NOT NULL,
    unit_raw       TEXT NOT NULL,
    rate_raw       TEXT NOT NULL,
    line_total_raw TEXT NOT NULL,
    sku            TEXT REFERENCES products(sku),
    received_units INTEGER,
    net_amount     TEXT,
    reviewed       TEXT NOT NULL DEFAULT 'NO'
);

CREATE TABLE IF NOT EXISTS purchases (
    purchase_id  TEXT PRIMARY KEY,
    bill_id      TEXT NOT NULL REFERENCES bills(bill_id),
    sku          TEXT NOT NULL REFERENCES products(sku),
    supplier_id  TEXT NOT NULL REFERENCES suppliers(supplier_id),
    invoice_no   TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    cost_basis   TEXT NOT NULL,
    units        INTEGER NOT NULL,
    net_paise    INTEGER NOT NULL,
    posted_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id     TEXT PRIMARY KEY,
    bill_id      TEXT NOT NULL REFERENCES bills(bill_id),
    sku          TEXT NOT NULL REFERENCES products(sku),
    low_supplier TEXT NOT NULL,
    high_supplier TEXT NOT NULL,
    low_date     TEXT NOT NULL,
    high_date    TEXT NOT NULL,
    low_bill     TEXT NOT NULL,
    high_bill    TEXT NOT NULL,
    low_rate     INTEGER NOT NULL,
    high_rate    INTEGER NOT NULL,
    gap_pct      REAL NOT NULL,
    status       TEXT NOT NULL DEFAULT 'PENDING',
    created_at   TEXT NOT NULL,
    attempted_at TEXT,
    sent_at      TEXT,
    wamid        TEXT
);
"""

# ---------------------------------------------------------------------------
# Seed runner
# ---------------------------------------------------------------------------


def _iso(v: object) -> str | None:
    """Convert datetime / date / Decimal / None to a string for SQLite."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return str(v)


def seed(db_path: str) -> None:
    print(f"[seed] Connecting to {db_path}")
    con = sqlite3.connect(db_path)
    con.executescript(DDL)

    with con:
        # Suppliers
        con.executemany(
            "INSERT OR IGNORE INTO suppliers VALUES (?,?,?)",
            SUPPLIERS,
        )
        print(f"[seed] Upserted {len(SUPPLIERS)} suppliers.")

        # Products
        con.executemany(
            "INSERT OR IGNORE INTO products VALUES (?,?,?,?)",
            PRODUCTS,
        )
        print(f"[seed] Upserted {len(PRODUCTS)} products.")

        # Bills — convert datetime/Decimal columns to strings first
        bill_rows = []
        for b in BILLS:
            bill_rows.append(tuple(_iso(col) if not isinstance(col, str) or col == "" else col for col in b))
        con.executemany(
            """INSERT OR IGNORE INTO bills VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            bill_rows,
        )
        print(f"[seed] Upserted {len(BILLS)} bills.")

        # ReviewLines
        rl_rows = []
        for rl in REVIEW_LINES:
            rl_rows.append(tuple(_iso(col) if isinstance(col, (datetime, date, Decimal)) else col for col in rl))
        con.executemany(
            """INSERT OR IGNORE INTO review_lines VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rl_rows,
        )
        print(f"[seed] Upserted {len(REVIEW_LINES)} review lines.")

        # Purchases
        pur_rows = []
        for p in PURCHASES:
            pur_rows.append(tuple(_iso(col) if isinstance(col, (datetime, date, Decimal)) else col for col in p))
        con.executemany(
            """INSERT OR IGNORE INTO purchases VALUES
               (?,?,?,?,?,?,?,?,?,?)""",
            pur_rows,
        )
        print(f"[seed] Upserted {len(PURCHASES)} purchases.")

        # Alerts
        alert_rows = []
        for a in ALERTS:
            alert_rows.append(tuple(_iso(col) if isinstance(col, (datetime, date, Decimal)) else col for col in a))
        con.executemany(
            """INSERT OR IGNORE INTO alerts VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            alert_rows,
        )
        print(f"[seed] Upserted {len(ALERTS)} alerts.")

    con.close()
    print("[seed] Done. Database is ready for local development.")


if __name__ == "__main__":
    seed(settings.sqlite_db_path)
