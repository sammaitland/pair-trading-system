"""
Data contract tests.

Verify that the data structures flowing between modules conform to
the contracts documented in ARCHITECTURE.md: pair cache schema,
parameter extraction, Longlist/Shortlist structure, and reference
implementation output shapes.
"""

import pandas as pd
import pytest


class TestPairCacheSchema:
    """The pair cache schema documented in fixtures/ is enforceable."""

    def test_pair_cache_schema_doc_exists(self):
        """The schema documentation file exists."""
        import os

        schema_path = os.path.join(
            os.path.dirname(__file__), "..", "fixtures", "pair_cache_schema.md"
        )
        assert os.path.exists(schema_path)

    def test_synthetic_pairs_match_schema(self, synthetic_parameters):
        """Synthetic parameters file conforms to the pair cache schema."""
        pairs = pd.read_excel(synthetic_parameters, sheet_name="Pairs")

        # Required columns from the schema
        assert "Tag" in pairs.columns
        assert "Co1" in pairs.columns
        assert "Co2" in pairs.columns
        assert "Index" in pairs.columns
        assert "Tail" in pairs.columns
        assert "EMA_Multiplier" in pairs.columns
        assert "CDF_Threshold" in pairs.columns

        # Type checks
        assert pairs["EMA_Multiplier"].dtype == float
        assert pairs["CDF_Threshold"].dtype == float
        assert all(pairs["Tail"].isin(["L", "U"]))

    def test_all_pairs_have_both_tails(self, synthetic_parameters):
        """Every Co1/Co2 combination has both an L and a U entry."""
        pairs = pd.read_excel(synthetic_parameters, sheet_name="Pairs")

        for _, group in pairs.groupby(["Co1", "Co2"]):
            tails = set(group["Tail"].values)
            assert tails == {"L", "U"}, (
                f"Pair {group['Co1'].iloc[0]}/{group['Co2'].iloc[0]} "
                f"has tails {tails}, expected {{'L', 'U'}}"
            )


class TestReferencePairGeneratorOutputContract:
    """Reference pair generator output matches the fixture schema."""

    def test_build_parameters_file(self, tmp_path):
        """build_parameters_file() produces a valid multi-sheet Excel."""
        from src.calibration.reference_pair_generator import ReferencePairGenerator

        gen = ReferencePairGenerator(seed=42, n_pairs_per_index=2)

        # Generate pairs for one index
        universe = pd.DataFrame({"Ticker": ["AA", "BB", "CC"]})
        pairs = gen.generate("VGT", universe, {}, pd.DataFrame(), str(tmp_path))

        # Build parameters file
        ticker_betas = {
            "AA": {"subsector_beta": 1.1, "index": "VGT"},
            "BB": {"subsector_beta": 0.9, "index": "VGT"},
            "CC": {"subsector_beta": 1.0, "index": "VGT"},
        }
        output_path = str(tmp_path / "params.xlsx")
        gen.build_parameters_file(pairs, ticker_betas, output_path)

        # Verify schema
        xl = pd.ExcelFile(output_path)
        assert "Pairs" in xl.sheet_names
        assert "Tickers" in xl.sheet_names
        assert "15Day_Cumulative_Stats" in xl.sheet_names
        assert "Sum_Deviation_Params" in xl.sheet_names

        tickers = pd.read_excel(output_path, sheet_name="Tickers")
        assert "SubSector_Beta" in tickers.columns


class TestReferenceOptimizerOutputContract:
    """Reference optimizer output matches the expected data contract."""

    def test_exclusion_range_matches_scoring_constants(self, synthetic_parameters):
        """Optimizer exclusion range uses the same buckets as EXCLUDED_BUCKETS."""
        from src.calibration.reference_optimizer import ReferenceOptimizer
        from src.shared.scoring_constants import EXCLUDED_BUCKETS

        pair_data = pd.read_excel(synthetic_parameters, sheet_name="Pairs")
        opt = ReferenceOptimizer(seed=42)
        result = opt.optimize(pair_data, {}, "VGT", "/tmp")

        assert set(result["exclusion_range"]) == EXCLUDED_BUCKETS
