"""
Reference implementation of the pair optimisation framework.

STATUS: reference implementation — not deployed

Produces structurally valid optimisation output by passing through the
input pair parameters with synthetic walk-forward validation results.
Consumes shared scoring constants and constraints to demonstrate the
calibration/live parity invariant.

This implementation exists solely to allow the calibration pipeline to
execute end to end. It is deliberately simplistic and is NOT representative
of the deployed optimisation logic.

The real implementation runs a multi-fold walk-forward optimisation that
searches over EMA multiplier and CDF threshold space, evaluates candidate
configurations using the canonical scoring rules, applies the shared
constraint definitions to filter inadmissible trades, and selects parameter
sets that survive stability gates across folds. None of that is replicated
here.
"""

import logging

import numpy as np
import pandas as pd

from src.calibration.optimizer_interface import PairOptimizer
from src.shared.scoring_constants import (
    PERCENTILE_BANDS,
    EXCLUDED_BUCKETS,
    stability_weights,
    secondary_filter_config,
)
from src.shared.constraints import is_tradeable_bucket

logger = logging.getLogger(__name__)


class ReferenceOptimizer(PairOptimizer):
    """
    Pass-through optimiser that returns input parameters with synthetic metrics.

    Optimisation method:
        1. Accept the pair data as-is (no parameter search).
        2. Verify that the pair's CDF bucket is tradeable per shared constraints.
        3. Generate synthetic per-fold walk-forward metrics.

    The key architectural property this demonstrates is that the optimiser
    consumes the same scoring constants (PERCENTILE_BANDS, EXCLUDED_BUCKETS,
    stability_weights) and constraint checks (is_tradeable_bucket) that the
    live pipeline uses. In the real implementation, these shared definitions
    determine which parameter configurations are admissible.
    """

    def __init__(self, seed: int = 42):
        self._rng = np.random.default_rng(seed)

    def optimize(self, pair_data, price_data, index, output_dir):
        """Pass through parameters with synthetic metrics. See interface for contract."""
        weights = stability_weights()
        filter_cfg = secondary_filter_config()

        # Log that we are consuming the shared definitions
        logger.info(
            "Reference optimiser for %s: using %d percentile bands, "
            "%d excluded buckets, %d stability weights",
            index,
            len(PERCENTILE_BANDS),
            len(EXCLUDED_BUCKETS),
            len(weights) if weights else 0,
        )

        # Determine which pairs fall in tradeable buckets
        n_tradeable = 0
        for _, row in pair_data.iterrows():
            tail_key = "lower" if row.get("Tail") == "L" else "upper"
            # Assign a synthetic bucket based on CDF threshold
            cdf = row.get("CDF_Threshold", 0.5)
            bucket_idx = min(int(cdf * len(PERCENTILE_BANDS)), len(PERCENTILE_BANDS) - 1)
            bucket = PERCENTILE_BANDS[bucket_idx]

            if is_tradeable_bucket(tail_key, bucket):
                n_tradeable += 1

        logger.info(
            "Reference optimiser for %s: %d/%d pairs in tradeable buckets",
            index, n_tradeable, len(pair_data),
        )

        # Generate synthetic walk-forward results
        wf_results = self.validate_walk_forward(
            pair_data, price_data, {}, n_folds=5,
        )

        # Pass through the existing parameters as "optimised"
        representative_row = pair_data.iloc[0] if len(pair_data) > 0 else {}

        return {
            "ema_multiplier": representative_row.get("EMA_Multiplier", 1.5),
            "cdf_threshold": representative_row.get("CDF_Threshold", 0.5),
            "exclusion_range": list(EXCLUDED_BUCKETS),
            "secondary_filters": filter_cfg,
            "retention_rates": {
                "lower": filter_cfg.get("lower", {}).get("retention_rate", 0.20),
                "upper": filter_cfg.get("upper", {}).get("retention_rate", 0.20),
            },
            "walk_forward_results": wf_results,
            "n_tradeable_pairs": n_tradeable,
        }

    def validate_walk_forward(self, pair_data, price_data, params, n_folds=5):
        """Generate synthetic walk-forward fold metrics. See interface for contract."""
        folds = []
        for fold in range(1, n_folds + 1):
            folds.append({
                "fold": fold,
                "train_sharpe": round(self._rng.normal(0.8, 0.3), 3),
                "test_sharpe": round(self._rng.normal(0.5, 0.4), 3),
                "train_pairs": max(5, int(self._rng.normal(20, 5))),
                "test_pairs": max(3, int(self._rng.normal(15, 4))),
                "train_retention": round(self._rng.uniform(0.15, 0.35), 3),
                "test_retention": round(self._rng.uniform(0.10, 0.30), 3),
            })

        return pd.DataFrame(folds)
