"""
tests/conftest.py
=================
Shared pytest fixtures for the AI Bill Scanner test suite.
Configured with in-memory SQLite and mock external adapters for deterministic,
network-free test execution.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Generator

import pytest

from app.config import Settings
from app.models import (
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
from app.services.gemini_scanner import MockGeminiScanner
from app.services.whatsapp import MockWhatsAppClient
from app.services.workflow_engine import WorkflowEngine
from app.storage.sqlite_store import SQLiteStore

UTC = timezone.utc


@pytest.fixture
def test_settings() -> Settings:
    """Settings configured specifically for offline unit/integration tests."""
    return Settings(
        whatsapp_verify_token="test_verify_token",
        whatsapp_access_token="test_access_token",
        whatsapp_phone_number_id="100200300",
        whatsapp_api_version="v19.0",
        whatsapp_owner_number="919999999999",
        whatsapp_allowlist=["919876543210"],
        gemini_api_key="test_gemini_key",
        gemini_model="gemini-1.5-flash",
        storage_backend="sqlite",
        sqlite_db_path=":memory:",
        mock_whatsapp=True,
        mock_gemini=True,
        tz_name="Asia/Kolkata",
        bill_size_limit_mb=5,
        max_lines=75,
        min_lines=1,
        alert_expiry_hours=24,
        alert_lookback_days=30,
        alert_new_bill_days=7,
        alert_min_gap_pct=2.00,
        alert_min_gap_paise=1,
        media_storage_dir="./media_test",
    )


@pytest.fixture
def store(test_settings: Settings) -> SQLiteStore:
    """In-memory SQLite store with all tables created and standard demo data seeded."""
    s = SQLiteStore(db_path=":memory:")
    s.create_tables()
    s.seed_demo_data()
    return s


@pytest.fixture
def mock_whatsapp() -> MockWhatsAppClient:
    """Mock WhatsApp client returning deterministic fake media and tracking sent alerts."""
    return MockWhatsAppClient(mock_image_bytes=b"fake_jpeg_image_content", mock_mime_type="image/jpeg")


@pytest.fixture
def mock_gemini() -> MockGeminiScanner:
    """Mock Gemini scanner with standard two-line kirana bill fixture."""
    return MockGeminiScanner()


@pytest.fixture
def engine(
    store: SQLiteStore,
    mock_gemini: MockGeminiScanner,
    mock_whatsapp: MockWhatsAppClient,
    test_settings: Settings,
) -> WorkflowEngine:
    """Initialized WorkflowEngine wired to in-memory store and mock adapters."""
    return WorkflowEngine(
        store=store,
        scanner=mock_gemini,
        whatsapp=mock_whatsapp,
        settings=test_settings,
    )


@pytest.fixture
def sample_webhook_factory():
    """Factory creating valid WhatsApp webhook payloads with customizable attributes."""
    def _create(
        message_id: str = "wamid.HBgLMTIz",
        sender: str = "919876543210",
        phone_number_id: str = "100200300",
        media_id: str = "media_xyz_987",
        mime_type: str = "image/jpeg",
        msg_type: str = "image",
    ) -> dict:
        msg_obj: dict = {
            "id": message_id,
            "from": sender,
            "timestamp": "1700000000",
            "type": msg_type,
        }
        if msg_type == "image":
            msg_obj["image"] = {
                "id": media_id,
                "mime_type": mime_type,
            }

        return {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WHATSAPP_ENTRY_ID",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "1234567890",
                                    "phone_number_id": phone_number_id,
                                },
                                "messages": [msg_obj],
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        }

    return _create


@pytest.fixture
def make_bill(store: SQLiteStore):
    """Helper to persist a test bill with custom field overrides."""
    def _maker(
        bill_id: str | None = None,
        status: BillStatus = BillStatus.QUEUED,
        **overrides,
    ) -> Bill:
        bid = bill_id or f"bill_{uuid.uuid4().hex[:8]}"
        bill = Bill(
            bill_id=bid,
            sender=overrides.get("sender", "919876543210"),
            media_id=overrides.get("media_id", f"media_{uuid.uuid4().hex[:8]}"),
            mime_type=overrides.get("mime_type", "image/jpeg"),
            received_at=overrides.get("received_at", datetime.now(UTC)),
            status=status,
            scan_id=overrides.get("scan_id"),
            scan_started_at=overrides.get("scan_started_at"),
            supplier_raw=overrides.get("supplier_raw", "Ramesh Traders"),
            invoice_no=overrides.get("invoice_no", "INV-101"),
            invoice_date=overrides.get("invoice_date", "2026-11-20"),
            currency=overrides.get("currency", "INR"),
            invoice_total=overrides.get("invoice_total", "500.00"),
            notes=overrides.get("notes"),
            line_count=overrides.get("line_count"),
            supplier_id=overrides.get("supplier_id"),
            cost_basis=overrides.get("cost_basis"),
            verified_net_total=overrides.get("verified_net_total"),
            header_verified=overrides.get("header_verified"),
            reviewer=overrides.get("reviewer"),
            posted_at=overrides.get("posted_at"),
            reason=overrides.get("reason"),
        )
        store.insert_bill(bill)
        return bill

    return _maker


@pytest.fixture
def make_review_line(store: SQLiteStore):
    """Helper to persist a review line with custom field overrides."""
    def _maker(
        bill_id: str,
        line_no: int = 1,
        scan_id: str = "scan_001",
        **overrides,
    ) -> ReviewLine:
        rid = overrides.get("review_id", f"rev_{uuid.uuid4().hex[:8]}")
        ln = ReviewLine(
            review_id=rid,
            bill_id=bill_id,
            scan_id=scan_id,
            line_no=line_no,
            product_raw=overrides.get("product_raw", "Parle-G Biscuit 10pk"),
            pack_raw=overrides.get("pack_raw", "10pk"),
            quantity_raw=overrides.get("quantity_raw", "10"),
            unit_raw=overrides.get("unit_raw", "PACK"),
            rate_raw=overrides.get("rate_raw", "46.00"),
            line_total_raw=overrides.get("line_total_raw", "460.00"),
            sku=overrides.get("sku", "BISCUIT-PARLEG-10"),
            received_units=overrides.get("received_units", 10),
            net_amount=overrides.get("net_amount", Decimal("460.00")),
            reviewed=overrides.get("reviewed", Reviewed.YES),
        )
        store.insert_review_line(ln)
        return ln

    return _maker


@pytest.fixture
def client(store: SQLiteStore, engine: WorkflowEngine, test_settings: Settings):
    """Synchronous test client for FastAPI sharing the in-memory test store & engine."""
    from starlette.testclient import TestClient
    from app.api.dependencies import get_engine, get_settings, get_store
    from app.main import app

    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_settings] = lambda: test_settings
    with TestClient(app) as c:
        app.state.store = store
        app.state.engine = engine
        app.state.settings = test_settings
        yield c
    app.dependency_overrides.clear()
