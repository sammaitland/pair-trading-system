"""
Deterministic fixture harness.

Validates that the synthetic fixture generator produces structurally
valid output that downstream modules can consume, and that the
reference implementations conform to their interface contracts.
"""

import numpy as np
import pandas as pd
import pytest


class TestSyntheticParametersFile:
    """Fixture generator produces a valid parameters file."""

    def test_generates_file(self, synthetic_parameters):
        """The generator creates an Excel file at the specified path."""
        import os

        assert os.path.exists(synthetic_parameters)

    def test_has_required_sheets(self, synthetic_parameters):
        """Output file contains all sheets the pipeline expects."""
        xl = pd.ExcelFile(synthetic_parameters)
        required = {"Pairs", "Tickers", "15Day_Cumulative_Stats", "Sum_Deviation_Params"}
        assert required.issubset(set(xl.sheet_names))

    def test_pairs_sheet_columns(self, synthetic_parameters):
        """Pairs sheet has the columns downstream modules consume."""
        df = pd.read_excel(synthetic_parameters, sheet_name="Pairs")
        required_cols = {"Tag", "Pair", "Co1", "Co2", "Index", "Tail", "EMA_Multiplier", "CDF_Threshold"}
        assert required_cols.issubset(set(df.columns))

    def test_pairs_tails_are_valid(self, synthetic_parameters):
        """Every pair has tail 'L' or 'U'."""
        df = pd.read_excel(synthetic_parameters, sheet_name="Pairs")
        assert set(df["Tail"].unique()) == {"L", "U"}

    def test_pairs_indices_are_valid(self, synthetic_parameters):
        """Every pair maps to one of the five sector ETFs."""
        df = pd.read_excel(synthetic_parameters, sheet_name="Pairs")
        valid = {"VGT", "VFH", "VIS", "VHT", "VCR"}
        assert set(df["Index"].unique()).issubset(valid)

    def test_tickers_sheet_columns(self, synthetic_parameters):
        """Tickers sheet has required columns."""
        df = pd.read_excel(synthetic_parameters, sheet_name="Tickers")
        required_cols = {"Ticker", "Index", "SubSector_Beta"}
        assert required_cols.issubset(set(df.columns))

    def test_sum_deviation_params(self, synthetic_parameters):
        """Sum_Deviation_Params sheet contains required parameters."""
        df = pd.read_excel(synthetic_parameters, sheet_name="Sum_Deviation_Params")
        params = set(df["Parameter"].values)
        assert "Sum Deviation StdDev" in params
        assert "Mean Sum Deviation" in params

    def test_deterministic_output(self, tmp_path):
        """Same seed produces identical output."""
        from fixtures.synthetic_pairs import generate_synthetic_parameters

        path1 = str(tmp_path / "params1.xlsx")
        path2 = str(tmp_path / "params2.xlsx")
        generate_synthetic_parameters(path1, n_pairs_per_index=3)
        generate_synthetic_parameters(path2, n_pairs_per_index=3)

        df1 = pd.read_excel(path1, sheet_name="Pairs")
        df2 = pd.read_excel(path2, sheet_name="Pairs")
        pd.testing.assert_frame_equal(df1, df2)


class TestReferencePairGenerator:
    """Reference pair generator conforms to the interface contract."""

    def test_inherits_interface(self):
        """ReferencePairGenerator is a subclass of PairGenerator."""
        from src.calibration.pair_generator_interface import PairGenerator
        from src.calibration.reference_pair_generator import ReferencePairGenerator

        assert issubclass(ReferencePairGenerator, PairGenerator)

    def test_generate_produces_valid_dataframe(self):
        """generate() returns a DataFrame with required columns."""
        from src.calibration.reference_pair_generator import ReferencePairGenerator

        gen = ReferencePairGenerator(seed=42, n_pairs_per_index=3)
        universe = pd.DataFrame({"Ticker": ["AAA", "BBB", "CCC", "DDD"]})
        result = gen.generate("VGT", universe, {}, pd.DataFrame(), "/tmp")

        assert isinstance(result, pd.DataFrame)
        assert not result.empty
        required = {"Tag", "Pair", "Co1", "Co2", "Index", "Tail", "EMA_Multiplier", "CDF_Threshold"}
        assert required.issubset(set(result.columns))

    def test_generate_deterministic(self):
        """Same seed produces identical output."""
        from src.calibration.reference_pair_generator import ReferencePairGenerator

        universe = pd.DataFrame({"Ticker": ["AA", "BB", "CC", "DD"]})
        gen1 = ReferencePairGenerator(seed=99)
        gen2 = ReferencePairGenerator(seed=99)

        r1 = gen1.generate("VGT", universe, {}, pd.DataFrame(), "/tmp")
        r2 = gen2.generate("VGT", universe, {}, pd.DataFrame(), "/tmp")
        pd.testing.assert_frame_equal(r1, r2)


class TestReferenceOptimizer:
    """Reference optimizer conforms to the interface contract."""

    def test_inherits_interface(self):
        """ReferenceOptimizer is a subclass of PairOptimizer."""
        from src.calibration.optimizer_interface import PairOptimizer
        from src.calibration.reference_optimizer import ReferenceOptimizer

        assert issubclass(ReferenceOptimizer, PairOptimizer)

    def test_optimize_returns_expected_keys(self, synthetic_parameters):
        """optimize() returns a dict with required keys."""
        from src.calibration.reference_optimizer import ReferenceOptimizer

        pair_data = pd.read_excel(synthetic_parameters, sheet_name="Pairs")
        opt = ReferenceOptimizer(seed=42)
        result = opt.optimize(pair_data, {}, "VGT", "/tmp")

        assert isinstance(result, dict)
        for key in ["ema_multiplier", "cdf_threshold", "exclusion_range",
                     "secondary_filters", "retention_rates", "walk_forward_results"]:
            assert key in result, f"Missing key: {key}"

    def test_walk_forward_returns_dataframe(self, synthetic_parameters):
        """validate_walk_forward() returns a DataFrame with per-fold metrics."""
        from src.calibration.reference_optimizer import ReferenceOptimizer

        pair_data = pd.read_excel(synthetic_parameters, sheet_name="Pairs")
        opt = ReferenceOptimizer(seed=42)
        wf = opt.validate_walk_forward(pair_data, {}, {}, n_folds=3)

        assert isinstance(wf, pd.DataFrame)
        assert len(wf) == 3
        assert "fold" in wf.columns
        assert "train_sharpe" in wf.columns
        assert "test_sharpe" in wf.columns


class TestReferenceScorer:
    """Reference scorer conforms to the interface contract."""

    def test_inherits_interface(self):
        """ReferenceScorer is a subclass of SignalScorer."""
        from src.signals.scoring_interface import SignalScorer
        from src.signals.reference_scorer import ReferenceScorer

        assert issubclass(ReferenceScorer, SignalScorer)

    def test_score_with_no_data_returns_neutral(self):
        """Missing data produces the neutral score (0.5)."""
        from src.signals.reference_scorer import ReferenceScorer

        scorer = ReferenceScorer()
        score = scorer.calculate_composite_score("AAAA", "BBBB", "L", "VGT", {})
        assert score == 0.5

    def test_score_with_data_returns_bounded_value(self, synthetic_price_data):
        """With valid data, score is in [0, 1]."""
        from src.signals.reference_scorer import ReferenceScorer

        market_data = {}
        for ticker in ["SYNTA", "SYNTB"]:
            market_data[ticker] = {"historical_data": synthetic_price_data[ticker]}

        scorer = ReferenceScorer()
        score = scorer.calculate_composite_score("SYNTA", "SYNTB", "L", "VGT", market_data)
        assert 0.0 <= score <= 1.0


class TestReferenceFactorShock:
    """Reference factor shock detector conforms to the interface contract."""

    def test_inherits_interface(self):
        """ReferenceFactorShockDetector is a subclass of FactorShockDetector."""
        from src.signals.factor_shock_interface import FactorShockDetector
        from src.signals.reference_factor_shock import ReferenceFactorShockDetector

        assert issubclass(ReferenceFactorShockDetector, FactorShockDetector)

    def test_no_suppressions(self):
        """Reference implementation reports no active suppressions."""
        from src.signals.reference_factor_shock import ReferenceFactorShockDetector

        detector = ReferenceFactorShockDetector()
        status, details = detector.get_live_status()
        assert status == {}
        assert details == {}

    def test_monitoring_returns_zero_shocks(self):
        """run_live_monitoring() reports zero shocks."""
        from src.signals.reference_factor_shock import ReferenceFactorShockDetector

        detector = ReferenceFactorShockDetector()
        result = detector.run_live_monitoring()
        assert result["shocks_detected"] == 0
