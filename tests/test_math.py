"""
tests/test_math.py
==================
Unit tests for deterministic financial math, paise normalization,
Indian Financial Year boundaries, and percentage gap calculations.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.workflow_engine import WorkflowEngine


class TestFinancialMath:
    """Test integer paise conversion to eliminate float drift."""

    def test_compute_paise_standard_decimals(self, engine: WorkflowEngine):
        assert engine._compute_paise("500.00") == 50000
        assert engine._compute_paise("460.00") == 46000
        assert engine._compute_paise("460") == 46000
        assert engine._compute_paise("4.6") == 460
        assert engine._compute_paise("0.01") == 1
        assert engine._compute_paise("0.10") == 10
        assert engine._compute_paise("1234.56") == 123456

    def test_compute_paise_half_up_rounding(self, engine: WorkflowEngine):
        # 12.345 INR -> 1234.5 paise -> rounds half-up to 1235 paise
        assert engine._compute_paise("12.345") == 1235
        # 12.344 INR -> 1234.4 paise -> rounds down to 1234 paise
        assert engine._compute_paise("12.344") == 1234

    def test_indian_financial_year_boundaries(self, engine: WorkflowEngine):
        # Indian FY runs April 1 to March 31 of next year
        # March 31, 2026 belongs to FY 2025-26
        fy_start_mar, fy_end_mar = engine._get_indian_fy_dates(date(2026, 3, 31))
        assert fy_start_mar == date(2025, 4, 1)
        assert fy_end_mar == date(2026, 3, 31)

        # April 1, 2026 belongs to FY 2026-27
        fy_start_apr, fy_end_apr = engine._get_indian_fy_dates(date(2026, 4, 1))
        assert fy_start_apr == date(2026, 4, 1)
        assert fy_end_apr == date(2027, 3, 31)

        # Mid-year check (November 22, 2026) -> FY 2026-27
        fy_start_nov, fy_end_nov = engine._get_indian_fy_dates(date(2026, 11, 22))
        assert fy_start_nov == date(2026, 4, 1)
        assert fy_end_nov == date(2027, 3, 31)

        # January check (January 15, 2027) -> FY 2026-27
        fy_start_jan, fy_end_jan = engine._get_indian_fy_dates(date(2027, 1, 15))
        assert fy_start_jan == date(2026, 4, 1)
        assert fy_end_jan == date(2027, 3, 31)

    def test_gap_percentage_calculation(self):
        # High: Rs 50.00 (5000 paise), Low: Rs 46.00 (4600 paise)
        # Gap = 400 paise. Gap % = 100 * 400 / 5000 = 8.00%
        high_rate = 5000
        low_rate = 4600
        gap_paise = high_rate - low_rate
        gap_pct = float(Decimal("100") * Decimal(gap_paise) / Decimal(high_rate))
        assert round(gap_pct, 2) == 8.00

        # Sub-threshold gap: High 5100, Low 5000 -> 100/5100 = 1.96% (< 2.00%)
        high_rate_2 = 5100
        low_rate_2 = 5000
        gap_paise_2 = high_rate_2 - low_rate_2
        gap_pct_2 = float(Decimal("100") * Decimal(gap_paise_2) / Decimal(high_rate_2))
        assert round(gap_pct_2, 2) == 1.96
        assert gap_pct_2 < 2.00
