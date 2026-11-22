"""
tests/test_scan.py
==================
Integration and unit tests for Pipeline 2: AI Scan (Gemini Vision).
Tests oldest QUEUED bill pickup, media download, size/MIME validation,
Gemini extraction, line-count boundaries (1..75), and transition to REVIEW.
"""

from __future__ import annotations

import pytest

from app.models import BillStatus
from app.services.gemini_scanner import MockGeminiScanner
from app.services.whatsapp import MockWhatsAppClient
from app.services.workflow_engine import WorkflowEngine
from app.storage.sqlite_store import SQLiteStore


@pytest.mark.asyncio
class TestScanPipeline:
    async def test_scan_happy_path(
        self,
        store: SQLiteStore,
        make_bill,
        test_settings,
    ):
        """A QUEUED bill is picked up, scanned via Gemini, ReviewLines inserted, and moved to REVIEW."""
        bill = make_bill(status=BillStatus.QUEUED, mime_type="image/jpeg")

        mock_gemini = MockGeminiScanner(
            fixture_response={
                "supplier_raw": "Ramesh Traders",
                "invoice_no": "INV-555",
                "invoice_date": "2026-11-20",
                "currency": "INR",
                "invoice_total": "500.00",
                "notes": "Clear bill image",
                "lines": [
                    {
                        "line_no": 1,
                        "product_raw": "Parle-G 10pk",
                        "pack_raw": "10pk",
                        "quantity_raw": "10",
                        "unit_raw": "PACK",
                        "rate_raw": "46.00",
                        "line_total_raw": "460.00",
                    },
                    {
                        "line_no": 2,
                        "product_raw": "Lays Chips 26g",
                        "pack_raw": "26g",
                        "quantity_raw": "2",
                        "unit_raw": "PACK",
                        "rate_raw": "20.00",
                        "line_total_raw": "40.00",
                    },
                ],
            }
        )
        mock_whatsapp = MockWhatsAppClient(mock_image_bytes=b"fake_jpeg_content", mock_mime_type="image/jpeg")
        engine = WorkflowEngine(store=store, scanner=mock_gemini, whatsapp=mock_whatsapp, settings=test_settings)

        processed_id = await engine.run_scan_worker()
        assert processed_id == bill.bill_id

        # Verify bill updated
        updated = store.get_bill(bill.bill_id)
        assert updated.status == BillStatus.REVIEW
        assert updated.supplier_raw == "Ramesh Traders"
        assert updated.invoice_no == "INV-555"
        assert updated.invoice_date == "2026-11-20"
        assert updated.currency == "INR"
        assert updated.invoice_total == "500.00"
        assert updated.notes == "Clear bill image"
        assert updated.line_count == 2
        assert updated.scan_id is not None
        assert updated.scan_started_at is not None

        # Verify review lines persisted
        lines = store.get_review_lines(bill.bill_id)
        assert len(lines) == 2
        assert lines[0].line_no == 1
        assert lines[0].product_raw == "Parle-G 10pk"
        assert lines[1].line_no == 2
        assert lines[1].product_raw == "Lays Chips 26g"

    async def test_scan_handles_null_optional_fields(
        self,
        store: SQLiteStore,
        make_bill,
        test_settings,
    ):
        """When Gemini returns null for optional fields, scan succeeds and moves to REVIEW."""
        bill = make_bill(status=BillStatus.QUEUED)

        mock_gemini = MockGeminiScanner(
            fixture_response={
                "supplier_raw": None,
                "invoice_no": None,
                "invoice_date": None,
                "currency": "INR",
                "invoice_total": "100.00",
                "notes": "Cropped top header",
                "lines": [
                    {
                        "line_no": 1,
                        "product_raw": "Unknown Item",
                        "pack_raw": None,
                        "quantity_raw": "1",
                        "unit_raw": None,
                        "rate_raw": "100.00",
                        "line_total_raw": "100.00",
                    }
                ],
            }
        )
        mock_whatsapp = MockWhatsAppClient()
        engine = WorkflowEngine(store=store, scanner=mock_gemini, whatsapp=mock_whatsapp, settings=test_settings)

        processed_id = await engine.run_scan_worker()
        assert processed_id == bill.bill_id

        updated = store.get_bill(bill.bill_id)
        assert updated.status == BillStatus.REVIEW
        assert updated.supplier_raw is None
        assert updated.invoice_no is None
        assert updated.notes == "Cropped top header"

    async def test_scan_rejects_empty_lines(
        self,
        store: SQLiteStore,
        make_bill,
        test_settings,
    ):
        """When Gemini returns 0 lines, error is handled and bill is not moved to REVIEW."""
        bill = make_bill(status=BillStatus.QUEUED)

        mock_gemini = MockGeminiScanner(
            fixture_response={
                "supplier_raw": "Test",
                "invoice_no": "123",
                "invoice_date": "2026-11-20",
                "currency": "INR",
                "invoice_total": "0",
                "notes": None,
                "lines": [],  # 0 lines violates min_lines=1
            }
        )
        mock_whatsapp = MockWhatsAppClient()
        engine = WorkflowEngine(store=store, scanner=mock_gemini, whatsapp=mock_whatsapp, settings=test_settings)

        processed_id = await engine.run_scan_worker()
        assert processed_id == bill.bill_id

        updated = store.get_bill(bill.bill_id)
        # Bill stays in SCANNING with error recorded in notes
        assert updated.status == BillStatus.SCANNING
        assert ("Scan failed" in (updated.notes or "")) or ("Scan error" in (updated.notes or ""))

    async def test_scan_rejects_too_many_lines(
        self,
        store: SQLiteStore,
        make_bill,
        test_settings,
    ):
        """When Gemini returns > 75 lines, validation rejects it."""
        bill = make_bill(status=BillStatus.QUEUED)

        # 76 lines exceeds max_lines=75
        too_many_lines = [
            {
                "line_no": i,
                "product_raw": f"Product {i}",
                "pack_raw": None,
                "quantity_raw": "1",
                "unit_raw": "PACK",
                "rate_raw": "10.00",
                "line_total_raw": "10.00",
            }
            for i in range(1, 77)
        ]

        mock_gemini = MockGeminiScanner(
            fixture_response={
                "supplier_raw": "Wholesaler",
                "invoice_no": "999",
                "invoice_date": "2026-11-20",
                "currency": "INR",
                "invoice_total": "760.00",
                "notes": None,
                "lines": too_many_lines,
            }
        )
        mock_whatsapp = MockWhatsAppClient()
        engine = WorkflowEngine(store=store, scanner=mock_gemini, whatsapp=mock_whatsapp, settings=test_settings)

        processed_id = await engine.run_scan_worker()
        assert processed_id == bill.bill_id

        updated = store.get_bill(bill.bill_id)
        assert updated.status == BillStatus.SCANNING
        assert ("Scan failed" in (updated.notes or "")) or ("Scan error" in (updated.notes or ""))
