"""
tests/test_post.py
==================
Integration tests for Pipeline 3: Verify, Post & Compare.
Tests every validation gate (rejection back to REVIEW with reason on fail),
Indian Financial Year duplicate prevention, paise sum reconciliation,
and successful purchase ledger aggregation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models import (
    BillStatus,
    CostBasis,
    HeaderVerified,
    Product,
    ProductActive,
    Purchase,
    Reviewed,
    Supplier,
    SupplierActive,
)
from app.services.workflow_engine import WorkflowEngine
from app.storage.sqlite_store import SQLiteStore

UTC = timezone.utc


@pytest.mark.asyncio
class TestPostPipeline:
    async def test_post_happy_path(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        make_bill,
        make_review_line,
    ):
        """A fully verified, valid APPROVED bill transitions to POSTED and creates Purchases."""
        bill = make_bill(
            status=BillStatus.APPROVED,
            reviewer="reviewer_alex",
            header_verified=HeaderVerified.YES,
            supplier_id="SUP001",  # Ramesh Traders (seeded & active)
            invoice_no="INV-POST-001",
            invoice_date="2026-11-20",
            currency="INR",
            cost_basis=CostBasis.EX_GST,
            verified_net_total=Decimal("500.00"),
            line_count=2,
        )

        make_review_line(
            bill_id=bill.bill_id,
            line_no=1,
            sku="BISCUIT-PARLEG-10",
            received_units=10,
            net_amount=Decimal("460.00"),
            reviewed=Reviewed.YES,
        )
        make_review_line(
            bill_id=bill.bill_id,
            line_no=2,
            sku="CHIPS-LAYS-26",
            received_units=2,
            net_amount=Decimal("40.00"),
            reviewed=Reviewed.YES,
        )

        processed_id = await engine.run_post_worker()
        assert processed_id == bill.bill_id

        # Verify bill moved to POSTED
        updated = store.get_bill(bill.bill_id)
        assert updated.status == BillStatus.POSTED
        assert updated.posted_at is not None
        assert updated.reason is None

        # Verify purchases were inserted for each SKU
        purchases = store.get_purchases_by_bill(bill.bill_id)
        assert len(purchases) == 2

        parleg_p = next(p for p in purchases if p.sku == "BISCUIT-PARLEG-10")
        assert parleg_p.units == 10
        assert parleg_p.net_paise == 46000
        assert parleg_p.cost_basis == CostBasis.EX_GST
        assert parleg_p.invoice_no == "INV-POST-001"

        lays_p = next(p for p in purchases if p.sku == "CHIPS-LAYS-26")
        assert lays_p.units == 2
        assert lays_p.net_paise == 4000

    async def test_post_fails_when_header_not_verified(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        make_bill,
        make_review_line,
    ):
        """Missing header verification moves bill back to REVIEW with specific reason."""
        bill = make_bill(
            status=BillStatus.APPROVED,
            reviewer="reviewer_alex",
            header_verified=HeaderVerified.NO,  # Invalid
            supplier_id="SUP001",
            invoice_no="INV-102",
            invoice_date="2026-11-20",
            currency="INR",
            cost_basis=CostBasis.EX_GST,
            verified_net_total=Decimal("460.00"),
            line_count=1,
        )
        make_review_line(bill_id=bill.bill_id, line_no=1, net_amount=Decimal("460.00"))

        processed_id = await engine.run_post_worker()
        assert processed_id == bill.bill_id

        updated = store.get_bill(bill.bill_id)
        assert updated.status == BillStatus.REVIEW
        assert "header_verified must be YES" in updated.reason

    async def test_post_fails_when_reviewer_empty(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        make_bill,
        make_review_line,
    ):
        """Empty reviewer moves bill back to REVIEW."""
        bill = make_bill(
            status=BillStatus.APPROVED,
            reviewer="",  # Empty
            header_verified=HeaderVerified.YES,
            supplier_id="SUP001",
            invoice_no="INV-103",
            invoice_date="2026-11-20",
            currency="INR",
            cost_basis=CostBasis.EX_GST,
            verified_net_total=Decimal("460.00"),
            line_count=1,
        )
        make_review_line(bill_id=bill.bill_id, line_no=1, net_amount=Decimal("460.00"))

        await engine.run_post_worker()
        updated = store.get_bill(bill.bill_id)
        assert updated.status == BillStatus.REVIEW
        assert "reviewer must be set before posting" in updated.reason

    async def test_post_fails_on_future_invoice_date(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        make_bill,
        make_review_line,
    ):
        """Future invoice date (IST) moves bill back to REVIEW."""
        tomorrow_ist = (datetime.now(timezone.utc) + timedelta(days=2)).date()
        bill = make_bill(
            status=BillStatus.APPROVED,
            reviewer="alex",
            header_verified=HeaderVerified.YES,
            supplier_id="SUP001",
            invoice_no="INV-FUTURE",
            invoice_date=tomorrow_ist.isoformat(),
            currency="INR",
            cost_basis=CostBasis.EX_GST,
            verified_net_total=Decimal("460.00"),
            line_count=1,
        )
        make_review_line(bill_id=bill.bill_id, line_no=1, net_amount=Decimal("460.00"))

        await engine.run_post_worker()
        updated = store.get_bill(bill.bill_id)
        assert updated.status == BillStatus.REVIEW
        assert "is in the future" in updated.reason

    async def test_post_fails_on_wrong_currency(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        make_bill,
        make_review_line,
    ):
        """Non-INR currency moves bill back to REVIEW."""
        bill = make_bill(
            status=BillStatus.APPROVED,
            reviewer="alex",
            header_verified=HeaderVerified.YES,
            supplier_id="SUP001",
            invoice_no="INV-USD",
            invoice_date="2026-11-20",
            currency="USD",  # Not supported
            cost_basis=CostBasis.EX_GST,
            verified_net_total=Decimal("460.00"),
            line_count=1,
        )
        make_review_line(bill_id=bill.bill_id, line_no=1, net_amount=Decimal("460.00"))

        await engine.run_post_worker()
        updated = store.get_bill(bill.bill_id)
        assert updated.status == BillStatus.REVIEW
        assert "currency must be INR" in updated.reason

    async def test_post_duplicate_invoice_in_same_indian_fy(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        make_bill,
        make_review_line,
    ):
        """Same supplier + same invoice_no within same Indian FY is rejected back to REVIEW."""
        # 1. Create and post first bill (INV-DUP-1 in FY 2026-27 on 2026-06-15)
        b1 = make_bill(
            bill_id="b1_first",
            status=BillStatus.APPROVED,
            reviewer="alex",
            header_verified=HeaderVerified.YES,
            supplier_id="SUP001",
            invoice_no="INV-DUP-1",
            invoice_date="2026-06-15",
            currency="INR",
            cost_basis=CostBasis.EX_GST,
            verified_net_total=Decimal("460.00"),
            line_count=1,
        )
        make_review_line(bill_id=b1.bill_id, line_no=1, net_amount=Decimal("460.00"))
        await engine.run_post_worker()
        assert store.get_bill(b1.bill_id).status == BillStatus.POSTED

        # 2. Try to post second bill with same supplier + same invoice_no in same FY (2026-10-20)
        b2 = make_bill(
            bill_id="b2_duplicate",
            status=BillStatus.APPROVED,
            reviewer="alex",
            header_verified=HeaderVerified.YES,
            supplier_id="SUP001",
            invoice_no="INV-DUP-1",
            invoice_date="2026-10-20",
            currency="INR",
            cost_basis=CostBasis.EX_GST,
            verified_net_total=Decimal("460.00"),
            line_count=1,
        )
        make_review_line(bill_id=b2.bill_id, line_no=1, net_amount=Decimal("460.00"))
        await engine.run_post_worker()

        # b2 must be rejected back to REVIEW due to FY duplicate
        updated_b2 = store.get_bill(b2.bill_id)
        assert updated_b2.status == BillStatus.REVIEW
        assert "Duplicate invoice" in updated_b2.reason

    async def test_post_same_invoice_in_different_indian_fy_is_allowed(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        make_bill,
        make_review_line,
    ):
        """Same supplier + invoice_no across DIFFERENT Indian Financial Years is permitted."""
        # Bill 1 in FY 2025-26 (March 15, 2026)
        b1 = make_bill(
            bill_id="b1_fy2526",
            status=BillStatus.APPROVED,
            reviewer="alex",
            header_verified=HeaderVerified.YES,
            supplier_id="SUP001",
            invoice_no="INV-RENEWAL-1",
            invoice_date="2026-03-15",
            currency="INR",
            cost_basis=CostBasis.EX_GST,
            verified_net_total=Decimal("460.00"),
            line_count=1,
        )
        make_review_line(bill_id=b1.bill_id, line_no=1, net_amount=Decimal("460.00"))
        await engine.run_post_worker()
        assert store.get_bill(b1.bill_id).status == BillStatus.POSTED

        # Bill 2 in FY 2026-27 (April 15, 2026) with same invoice number
        b2 = make_bill(
            bill_id="b2_fy2627",
            status=BillStatus.APPROVED,
            reviewer="alex",
            header_verified=HeaderVerified.YES,
            supplier_id="SUP001",
            invoice_no="INV-RENEWAL-1",
            invoice_date="2026-04-15",
            currency="INR",
            cost_basis=CostBasis.EX_GST,
            verified_net_total=Decimal("460.00"),
            line_count=1,
        )
        make_review_line(bill_id=b2.bill_id, line_no=1, net_amount=Decimal("460.00"))
        await engine.run_post_worker()

        # Both posted cleanly because Indian FY boundaries differ!
        assert store.get_bill(b2.bill_id).status == BillStatus.POSTED

    async def test_post_paise_sum_mismatch(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        make_bill,
        make_review_line,
    ):
        """Sum of line items paise (46000) != verified_net_total (50000) rejected back to REVIEW."""
        bill = make_bill(
            status=BillStatus.APPROVED,
            reviewer="alex",
            header_verified=HeaderVerified.YES,
            supplier_id="SUP001",
            invoice_no="INV-SUM-ERR",
            invoice_date="2026-11-20",
            currency="INR",
            cost_basis=CostBasis.EX_GST,
            verified_net_total=Decimal("500.00"),  # Expected: 50000 paise
            line_count=1,
        )
        # Line is only 460.00 (46000 paise) -> 4000 paise mismatch
        make_review_line(bill_id=bill.bill_id, line_no=1, net_amount=Decimal("460.00"))

        await engine.run_post_worker()
        updated = store.get_bill(bill.bill_id)
        assert updated.status == BillStatus.REVIEW
        assert "Paise sum of ReviewLines" in updated.reason

    async def test_post_unreviewed_lines_rejected(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        make_bill,
        make_review_line,
    ):
        """Lines marked reviewed=NO prevent posting."""
        bill = make_bill(
            status=BillStatus.APPROVED,
            reviewer="alex",
            header_verified=HeaderVerified.YES,
            supplier_id="SUP001",
            invoice_no="INV-UNREV",
            invoice_date="2026-11-20",
            currency="INR",
            cost_basis=CostBasis.EX_GST,
            verified_net_total=Decimal("460.00"),
            line_count=1,
        )
        make_review_line(bill_id=bill.bill_id, line_no=1, reviewed=Reviewed.NO)

        await engine.run_post_worker()
        updated = store.get_bill(bill.bill_id)
        assert updated.status == BillStatus.REVIEW
        assert "not yet reviewed" in updated.reason

    async def test_post_inactive_sku_rejected(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        make_bill,
        make_review_line,
    ):
        """Mapping an inactive SKU is blocked."""
        store.upsert_product(Product(sku="INACTIVE-SKU", product_name="Old Product", active=ProductActive.NO))

        bill = make_bill(
            status=BillStatus.APPROVED,
            reviewer="alex",
            header_verified=HeaderVerified.YES,
            supplier_id="SUP001",
            invoice_no="INV-INACT",
            invoice_date="2026-11-20",
            currency="INR",
            cost_basis=CostBasis.EX_GST,
            verified_net_total=Decimal("460.00"),
            line_count=1,
        )
        make_review_line(bill_id=bill.bill_id, line_no=1, sku="INACTIVE-SKU", net_amount=Decimal("460.00"))

        await engine.run_post_worker()
        updated = store.get_bill(bill.bill_id)
        assert updated.status == BillStatus.REVIEW
        assert "is not active" in updated.reason
