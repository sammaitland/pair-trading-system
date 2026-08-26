"""
Golden-master regression tests for shared calculations.

These tests verify that the core mathematical functions produce
deterministic, numerically exact results. If a refactor changes the
output of these functions, these tests fail — forcing explicit
acknowledgement that the change alters system behaviour.
"""

import numpy as np
import pandas as pd
import pytest


class TestSumDeviationBucketing:
    """Sum deviation bucketing must be deterministic and consistent."""

    def test_assign_bucket_extreme_low(self):
        """A very low percentile maps to the 0-10% bucket."""
        from src.shared.calculations import assign_sum_dev_bucket

        bucket = assign_sum_dev_bucket(5)
        assert bucket == "0-10%"

    def test_assign_bucket_extreme_high(self):
        """A very high percentile maps to the 90-100% bucket."""
        from src.shared.calculations import assign_sum_dev_bucket

        bucket = assign_sum_dev_bucket(95)
        assert bucket == "90-100%"

    def test_assign_bucket_midpoint(self):
        """A 50th percentile maps to the 50-60% bucket."""
        from src.shared.calculations import assign_sum_dev_bucket

        bucket = assign_sum_dev_bucket(50)
        assert bucket == "50-60%"

    def test_assign_bucket_boundary_100(self):
        """Exactly 100 maps to the 90-100% bucket."""
        from src.shared.calculations import assign_sum_dev_bucket

        bucket = assign_sum_dev_bucket(100)
        assert bucket == "90-100%"

    def test_bucket_boundaries_are_contiguous(self):
        """Every integer percentile from 0 to 99 maps to a valid bucket."""
        from src.shared.calculations import assign_sum_dev_bucket
        from src.shared.scoring_constants import PERCENTILE_BANDS

        for pct in range(100):
            bucket = assign_sum_dev_bucket(pct)
            assert bucket in PERCENTILE_BANDS, f"Percentile {pct} mapped to invalid bucket {bucket}"

    def test_validate_sum_dev_bucket(self):
        """validate_sum_dev_bucket accepts valid buckets and rejects invalid ones."""
        from src.shared.calculations import validate_sum_dev_bucket

        assert validate_sum_dev_bucket("0-10%") is True
        assert validate_sum_dev_bucket("90-100%") is True
        assert validate_sum_dev_bucket("invalid") is False


class TestAlphaCalculation:
    """Alpha calculation uses arithmetic returns (by design, not a bug)."""

    def test_single_factor_alpha_arithmetic(self):
        """Alpha = stock_return - (beta * subsector_return), arithmetic."""
        from src.shared.calculations import calculate_stock_alpha_v92

        stock_return = 0.05
        subsector_return = 0.03
        beta = 1.2

        alpha = calculate_stock_alpha_v92(stock_return, subsector_return, beta)
        expected = stock_return - (beta * subsector_return)
        assert abs(alpha - expected) < 1e-10

    def test_alpha_zero_beta(self):
        """With zero beta, alpha equals the raw stock return."""
        from src.shared.calculations import calculate_stock_alpha_v92

        alpha = calculate_stock_alpha_v92(0.05, 0.03, 0.0)
        assert abs(alpha - 0.05) < 1e-10

    def test_alpha_unit_beta(self):
        """With unit beta, alpha is the excess return over the subsector."""
        from src.shared.calculations import calculate_stock_alpha_v92

        alpha = calculate_stock_alpha_v92(0.05, 0.03, 1.0)
        assert abs(alpha - 0.02) < 1e-10


class TestSumDeviationCalculation:
    """Sum deviation uses arithmetic summing (intentional — see ARCHITECTURE.md)."""

    def test_sum_deviation_returns_series(self):
        """Sum deviation returns a pd.Series of rolling sums."""
        from src.shared.calculations import calculate_sum_deviation

        alpha1 = pd.Series([0.01, 0.02, -0.01, 0.03, 0.01] * 5)
        alpha2 = pd.Series([-0.01, 0.01, 0.02, -0.02, 0.00] * 5)

        result = calculate_sum_deviation(alpha1, alpha2, window=5, method='backward')
        assert isinstance(result, pd.Series)
        assert len(result) == len(alpha1)

    def test_sum_deviation_symmetric_inputs(self):
        """When alpha series are negatives of each other, sum deviation is zero."""
        from src.shared.calculations import calculate_sum_deviation

        alpha = pd.Series([0.01, 0.02, -0.01, 0.03, 0.01] * 5)
        neg_alpha = -alpha
        result = calculate_sum_deviation(alpha, neg_alpha, window=5, method='backward')
        # Where valid (not NaN), values should be zero
        valid = result.dropna()
        assert (valid.abs() < 1e-10).all()


class TestPercentageChange:
    """Basic percentage change calculation."""

    def test_positive_change(self):
        from src.shared.calculations import calculate_percentage_change

        result = calculate_percentage_change(100, 110)
        assert abs(result - 0.10) < 1e-10

    def test_negative_change(self):
        from src.shared.calculations import calculate_percentage_change

        result = calculate_percentage_change(100, 90)
        assert abs(result - (-0.10)) < 1e-10

    def test_no_change(self):
        from src.shared.calculations import calculate_percentage_change

        result = calculate_percentage_change(100, 100)
        assert abs(result) < 1e-10


class TestDailyReturn:
    """Daily return calculation from a price series."""

    def test_returns_float(self):
        """calculate_daily_return returns the last single-day return as a float."""
        from src.shared.calculations import calculate_daily_return

        prices = pd.Series([100, 105, 103, 108])
        ret = calculate_daily_return(prices)
        assert isinstance(ret, float)
        # 108/103 - 1
        expected = (108 / 103) - 1
        assert abs(ret - expected) < 1e-10

    def test_insufficient_data_returns_zero(self):
        """With fewer than 2 prices, returns 0.0."""
        from src.shared.calculations import calculate_daily_return

        assert calculate_daily_return(pd.Series([100])) == 0.0
        assert calculate_daily_return(pd.Series([])) == 0.0
