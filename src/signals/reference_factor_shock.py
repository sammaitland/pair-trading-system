"""
No-op reference implementation of factor shock detection.

STATUS: reference implementation — not deployed

Returns "no suppressions active" for all queries. This allows the pipeline
to run end to end without the real factor shock detection subsystem.

The real implementation analyses factor loadings across sector constituents
using rolling regressions and z-score thresholds to detect regime changes.
"""

from typing import Dict, Tuple

import pandas as pd

from src.signals.factor_shock_interface import FactorShockDetector


class ReferenceFactorShockDetector(FactorShockDetector):
    """
    No-op detector that reports no active suppressions.

    This is a reference implementation only. The real detector uses
    a multi-factor regression pipeline with configurable lookback windows,
    z-score thresholds, and per-index suppression thresholds.
    """

    def get_live_status(self) -> Tuple[Dict, Dict[str, pd.DataFrame]]:
        """Return empty status — no suppressions active."""
        return {}, {}

    def run_live_monitoring(self) -> Dict:
        """No-op monitoring cycle."""
        return {"status": "reference_implementation", "shocks_detected": 0}
