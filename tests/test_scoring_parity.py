"""
State parity checks.

Verify that calibration and implementation consume identical scoring
definitions from the shared layer, preventing silent drift between
research and execution code.
"""

import pytest


class TestScoringConstantsParity:
    """Both pipelines consume the same scoring constants."""

    def test_percentile_bands_are_canonical(self):
        """PERCENTILE_BANDS has exactly 10 contiguous decile buckets."""
        from src.shared.scoring_constants import PERCENTILE_BANDS

        assert len(PERCENTILE_BANDS) == 10
        assert PERCENTILE_BANDS[0] == "0-10%"
        assert PERCENTILE_BANDS[-1] == "90-100%"

    def test_excluded_buckets_are_neutral_zone(self):
        """EXCLUDED_BUCKETS contains the two central deciles."""
        from src.shared.scoring_constants import EXCLUDED_BUCKETS

        assert "40-50%" in EXCLUDED_BUCKETS
        assert "50-60%" in EXCLUDED_BUCKETS
        assert len(EXCLUDED_BUCKETS) == 2

    def test_excluded_buckets_are_subset_of_bands(self):
        """Every excluded bucket must appear in PERCENTILE_BANDS."""
        from src.shared.scoring_constants import PERCENTILE_BANDS, EXCLUDED_BUCKETS

        for bucket in EXCLUDED_BUCKETS:
            assert bucket in PERCENTILE_BANDS

    def test_stability_weights_returns_dict(self):
        """stability_weights() returns a dict (possibly empty with synthetic config)."""
        from src.shared.scoring_constants import stability_weights

        result = stability_weights()
        assert isinstance(result, dict)

    def test_secondary_filter_config_returns_dict(self):
        """secondary_filter_config() returns a dict."""
        from src.shared.scoring_constants import secondary_filter_config

        result = secondary_filter_config()
        assert isinstance(result, dict)


class TestConstraintsParity:
    """Constraint functions are available to both calibration and live."""

    def test_tradeable_bucket_excludes_neutral_zone(self):
        """Neutral-zone buckets have zero position size → not tradeable."""
        from src.shared.constraints import is_tradeable_bucket

        assert not is_tradeable_bucket("lower", "40-50%")
        assert not is_tradeable_bucket("lower", "50-60%")
        assert not is_tradeable_bucket("upper", "40-50%")
        assert not is_tradeable_bucket("upper", "50-60%")

    def test_tradeable_bucket_allows_extremes(self):
        """Extreme buckets are tradeable."""
        from src.shared.constraints import is_tradeable_bucket

        assert is_tradeable_bucket("lower", "0-10%")
        assert is_tradeable_bucket("lower", "90-100%")
        assert is_tradeable_bucket("upper", "0-10%")

    def test_leg_weights_sum_to_one(self):
        """All configured leg weights sum to 1.0."""
        from src.shared.constraints import get_leg_weights
        from src.shared.scoring_constants import PERCENTILE_BANDS

        for tail in ["lower", "upper"]:
            for bucket in PERCENTILE_BANDS:
                w1, w2 = get_leg_weights(tail, bucket)
                assert abs(w1 + w2 - 1.0) < 1e-9, (
                    f"Leg weights for {tail}/{bucket} sum to {w1 + w2}"
                )

    def test_leverage_check_symmetric(self):
        """Leverage check returns consistent results."""
        from src.shared.constraints import check_leverage_limit

        within, current, max_allowed = check_leverage_limit(100000, 100000)
        assert within is True
        assert abs(current - 1.0) < 1e-9

        within, current, max_allowed = check_leverage_limit(300000, 100000)
        assert within is False
        assert abs(current - 3.0) < 1e-9


class TestOptimizerConsumesScoringConstants:
    """Reference optimizer imports and uses shared scoring definitions."""

    def test_reference_optimizer_imports_shared_definitions(self):
        """ReferenceOptimizer must import from scoring_constants and constraints."""
        import inspect
        from src.calibration import reference_optimizer

        source = inspect.getsource(reference_optimizer)
        assert "from src.shared.scoring_constants import" in source
        assert "from src.shared.constraints import" in source

    def test_reference_optimizer_uses_percentile_bands(self):
        """ReferenceOptimizer.optimize() references PERCENTILE_BANDS."""
        import inspect
        from src.calibration.reference_optimizer import ReferenceOptimizer

        source = inspect.getsource(ReferenceOptimizer.optimize)
        assert "PERCENTILE_BANDS" in source

    def test_reference_optimizer_uses_excluded_buckets(self):
        """ReferenceOptimizer.optimize() references EXCLUDED_BUCKETS."""
        import inspect
        from src.calibration.reference_optimizer import ReferenceOptimizer

        source = inspect.getsource(ReferenceOptimizer.optimize)
        assert "EXCLUDED_BUCKETS" in source
