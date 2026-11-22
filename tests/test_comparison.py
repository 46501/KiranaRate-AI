"""
tests/test_comparison.py
========================
Tests for per-unit rate comparison and alert trigger rules:
- 10 packs @ Rs 500 (5000 paise/unit) vs 10 packs @ Rs 460 (4600 paise/unit) -> 8.00% gap
- Same supplier exclusion (no self-alerts)
- Gap percentage threshold (>= 2.00%)
- Minimum paise gap (>= 1 paisa)
- 30-day historical lookback cutoff
- 7-day new bill eligibility window
- Bonus pack received unit normalization
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytz

from app.models import (
    AlertStatus,
    BillStatus,
    CostBasis,
    HeaderVerified,
    Purchase,
    Reviewed,
)
from app.services.workflow_engine import WorkflowEngine
from app.storage.sqlite_store import SQLiteStore

UTC = timezone.utc
IST = pytz.timezone("Asia/Kolkata")


@pytest.mark.asyncio
class TestRateComparison:
    async def test_comparison_8_percent_gap_triggers_alert(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        make_bill,
        make_review_line,
    ):
        """
        Spec scenario:
        Historical purchase from Suresh Wholesale (SUP002):
          10 packs @ Rs 460 total -> 4600 paise/unit on 5 days ago.
        New bill from Ramesh Traders (SUP001):
          10 packs @ Rs 500 total -> 5000 paise/unit on today.
        Gap = 400 paise >= 1 paisa.
        Gap % = 100 * (5000 - 4600) / 5000 = 8.00% >= 2.00%.
        -> Triggers PENDING Alert!
        """
        today_ist = datetime.now(IST).date()

        # 1. Historical purchase from Suresh (SUP002) 5 days ago
        hist_date = today_ist - timedelta(days=5)
        store.insert_purchase(
            Purchase(
                purchase_id="purch_hist_001",
                bill_id="bill_hist_001",
                sku="BISCUIT-PARLEG-10",
                supplier_id="SUP002",
                invoice_no="INV-SURESH-10",
                invoice_date=hist_date,
                cost_basis=CostBasis.EX_GST,
                units=10,
                net_paise=46000,  # 4600 paise/unit
                posted_at=datetime.now(UTC),
            )
        )

        # 2. Post new bill from Ramesh (SUP001) for same SKU @ 5000 paise/unit
        new_bill = make_bill(
            bill_id="bill_new_001",
            status=BillStatus.APPROVED,
            reviewer="manager",
            header_verified=HeaderVerified.YES,
            supplier_id="SUP001",
            invoice_no="INV-RAMESH-20",
            invoice_date=today_ist.isoformat(),
            currency="INR",
            cost_basis=CostBasis.EX_GST,
            verified_net_total=Decimal("500.00"),
            line_count=1,
        )
        make_review_line(
            bill_id=new_bill.bill_id,
            line_no=1,
            sku="BISCUIT-PARLEG-10",
            received_units=10,
            net_amount=Decimal("500.00"),
            reviewed=Reviewed.YES,
        )

        await engine.run_post_worker()

        # 3. Assert PENDING alert generated
        alert = store.get_oldest_pending_alert()
        assert alert is not None
        assert alert.sku == "BISCUIT-PARLEG-10"
        assert alert.low_supplier == "SUP002"
        assert alert.high_supplier == "SUP001"
        assert alert.low_rate == 4600
        assert alert.high_rate == 5000
        assert alert.gap_pct == 8.00
        assert alert.status == AlertStatus.PENDING

    async def test_same_supplier_does_not_trigger_alert(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        make_bill,
        make_review_line,
    ):
        """Historical purchase from same supplier does NOT trigger an alert."""
        today_ist = datetime.now(IST).date()

        # Historical purchase from Ramesh (SUP001) @ 4600 paise
        store.insert_purchase(
            Purchase(
                purchase_id="purch_same_sup",
                bill_id="bill_same_sup",
                sku="BISCUIT-PARLEG-10",
                supplier_id="SUP001",
                invoice_no="INV-RAMESH-OLD",
                invoice_date=today_ist - timedelta(days=3),
                cost_basis=CostBasis.EX_GST,
                units=10,
                net_paise=46000,
                posted_at=datetime.now(UTC),
            )
        )

        # New bill also from Ramesh (SUP001) @ 5000 paise
        new_bill = make_bill(
            status=BillStatus.APPROVED,
            reviewer="manager",
            header_verified=HeaderVerified.YES,
            supplier_id="SUP001",
            invoice_no="INV-RAMESH-NEW",
            invoice_date=today_ist.isoformat(),
            currency="INR",
            cost_basis=CostBasis.EX_GST,
            verified_net_total=Decimal("500.00"),
            line_count=1,
        )
        make_review_line(
            bill_id=new_bill.bill_id,
            line_no=1,
            sku="BISCUIT-PARLEG-10",
            received_units=10,
            net_amount=Decimal("500.00"),
            reviewed=Reviewed.YES,
        )

        await engine.run_post_worker()
        assert store.get_oldest_pending_alert() is None

    async def test_sub_2_percent_gap_suppresses_alert(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        make_bill,
        make_review_line,
    ):
        """Price gap < 2.00% is suppressed."""
        today_ist = datetime.now(IST).date()

        # Suresh: 5000 paise/unit
        store.insert_purchase(
            Purchase(
                purchase_id="purch_suresh_sub2",
                bill_id="bill_suresh_sub2",
                sku="BISCUIT-PARLEG-10",
                supplier_id="SUP002",
                invoice_no="INV-SURESH-1",
                invoice_date=today_ist - timedelta(days=2),
                cost_basis=CostBasis.EX_GST,
                units=10,
                net_paise=50000,  # 5000 paise/unit
                posted_at=datetime.now(UTC),
            )
        )

        # Ramesh: 5080 paise/unit -> gap = 80 paise, gap % = 80/5080 = 1.57% (< 2.00%)
        new_bill = make_bill(
            status=BillStatus.APPROVED,
            reviewer="manager",
            header_verified=HeaderVerified.YES,
            supplier_id="SUP001",
            invoice_no="INV-RAMESH-SUB2",
            invoice_date=today_ist.isoformat(),
            currency="INR",
            cost_basis=CostBasis.EX_GST,
            verified_net_total=Decimal("508.00"),
            line_count=1,
        )
        make_review_line(
            bill_id=new_bill.bill_id,
            line_no=1,
            sku="BISCUIT-PARLEG-10",
            received_units=10,
            net_amount=Decimal("508.00"),
            reviewed=Reviewed.YES,
        )

        await engine.run_post_worker()
        assert store.get_oldest_pending_alert() is None

    async def test_historical_lookback_cutoff_30_days(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        make_bill,
        make_review_line,
    ):
        """Historical purchase older than 30 days is ignored."""
        today_ist = datetime.now(IST).date()

        # Purchase 35 days ago (outside 30-day lookback window)
        old_date = today_ist - timedelta(days=35)
        store.insert_purchase(
            Purchase(
                purchase_id="purch_ancient",
                bill_id="bill_ancient",
                sku="BISCUIT-PARLEG-10",
                supplier_id="SUP002",
                invoice_no="INV-OLD-35",
                invoice_date=old_date,
                cost_basis=CostBasis.EX_GST,
                units=10,
                net_paise=40000,  # Cheaper (4000 paise)
                posted_at=datetime.combine(old_date, datetime.min.time(), tzinfo=UTC),
            )
        )

        # New bill today @ 5000 paise
        new_bill = make_bill(
            status=BillStatus.APPROVED,
            reviewer="manager",
            header_verified=HeaderVerified.YES,
            supplier_id="SUP001",
            invoice_no="INV-NEW-TODAY",
            invoice_date=today_ist.isoformat(),
            currency="INR",
            cost_basis=CostBasis.EX_GST,
            verified_net_total=Decimal("500.00"),
            line_count=1,
        )
        make_review_line(
            bill_id=new_bill.bill_id,
            line_no=1,
            sku="BISCUIT-PARLEG-10",
            received_units=10,
            net_amount=Decimal("500.00"),
            reviewed=Reviewed.YES,
        )

        await engine.run_post_worker()
        # No alert because comparison data was outside 30 days
        assert store.get_oldest_pending_alert() is None

    async def test_new_bill_eligibility_window_7_days(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        make_bill,
        make_review_line,
    ):
        """Newly scanned bill with invoice_date older than 7 days is posted but produces no alert."""
        today_ist = datetime.now(IST).date()

        # Historical comparison exists within last 30 days
        store.insert_purchase(
            Purchase(
                purchase_id="purch_suresh_valid",
                bill_id="bill_suresh_valid",
                sku="BISCUIT-PARLEG-10",
                supplier_id="SUP002",
                invoice_no="INV-SURESH-VALID",
                invoice_date=today_ist - timedelta(days=5),
                cost_basis=CostBasis.EX_GST,
                units=10,
                net_paise=46000,
                posted_at=datetime.now(UTC),
            )
        )

        # But new bill being posted has invoice_date 10 days ago (> 7 days)
        backlog_bill = make_bill(
            status=BillStatus.APPROVED,
            reviewer="manager",
            header_verified=HeaderVerified.YES,
            supplier_id="SUP001",
            invoice_no="INV-BACKLOG-10D",
            invoice_date=(today_ist - timedelta(days=10)).isoformat(),
            currency="INR",
            cost_basis=CostBasis.EX_GST,
            verified_net_total=Decimal("500.00"),
            line_count=1,
        )
        make_review_line(
            bill_id=backlog_bill.bill_id,
            line_no=1,
            sku="BISCUIT-PARLEG-10",
            received_units=10,
            net_amount=Decimal("500.00"),
            reviewed=Reviewed.YES,
        )

        await engine.run_post_worker()
        # Bill successfully posted
        assert store.get_bill(backlog_bill.bill_id).status == BillStatus.POSTED
        # But no alert triggered for stale backlog bill
        assert store.get_oldest_pending_alert() is None

    async def test_bonus_pack_normalization(
        self,
        engine: WorkflowEngine,
        store: SQLiteStore,
        make_bill,
        make_review_line,
    ):
        """
        Bonus/free pack normalization:
        Billed for 10 units at Rs 500, but 1 free unit received -> received_units = 11.
        Normalized effective rate = 50000 // 11 = 4545 paise/unit.
        """
        today_ist = datetime.now(IST).date()

        bill = make_bill(
            status=BillStatus.APPROVED,
            reviewer="manager",
            header_verified=HeaderVerified.YES,
            supplier_id="SUP001",
            invoice_no="INV-BONUS-01",
            invoice_date=today_ist.isoformat(),
            currency="INR",
            cost_basis=CostBasis.EX_GST,
            verified_net_total=Decimal("500.00"),
            line_count=1,
        )
        # 11 received units entered by reviewer
        make_review_line(
            bill_id=bill.bill_id,
            line_no=1,
            sku="BISCUIT-PARLEG-10",
            received_units=11,
            net_amount=Decimal("500.00"),
            reviewed=Reviewed.YES,
        )

        await engine.run_post_worker()
        purchases = store.get_purchases_by_bill(bill.bill_id)
        assert len(purchases) == 1
        p = purchases[0]
        assert p.units == 11
        assert p.net_paise == 50000
        # Effective unit rate
        effective_rate = p.net_paise // p.units
        assert effective_rate == 4545  # integer division
