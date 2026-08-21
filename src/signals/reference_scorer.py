"""
Naive reference implementation of the signal scoring interface.

STATUS: reference implementation — not deployed

This scorer assigns scores using a simple z-score on recent residual returns.
It exists solely to allow the pipeline to execute end to end against synthetic
fixtures. It is deliberately simplistic and is NOT representative of the
deployed scoring logic.

The real implementation incorporates volume metrics, implied-volatility
percentiles, intraday price patterns, and calibrated stability weights.
None of that is replicated here.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional

from src.signals.scoring_interface import SignalScorer
from src.shared import config


class ReferenceScorer(SignalScorer):
    """
    Naive scorer based on absolute residual return magnitude.

    Scoring method:
        1. Compute trailing-5-day mean absolute residual return for each ticker.
        2. Average the two values.
        3. Normalise to [0, 1] via min-max scaling across the candidate set.

    This produces a plausible-looking score distribution but has no predictive
    power. It is a reference implementation only.
    """

    def calculate_composite_score(
        self,
        ticker1: str,
        ticker2: str,
        tail: str,
        index: str,
        market_data: Dict,
        historical_percentiles: Optional[Dict] = None,
    ) -> float:
        """Score a pair by trailing residual magnitude. See interface for contract."""
        try:
            hist1 = market_data.get(ticker1, {}).get("historical_data")
            hist2 = market_data.get(ticker2, {}).get("historical_data")

            if hist1 is None or hist2 is None or len(hist1) < 6 or len(hist2) < 6:
                return 0.5  # neutral score when data is insufficient

            returns1 = hist1["close"].pct_change().dropna().iloc[-5:]
            returns2 = hist2["close"].pct_change().dropna().iloc[-5:]

            mean_abs1 = returns1.abs().mean()
            mean_abs2 = returns2.abs().mean()

            # Raw score: average absolute residual (higher = more active)
            raw = (mean_abs1 + mean_abs2) / 2

            # Clamp to reasonable range and normalise
            score = float(np.clip(raw / 0.05, 0.0, 1.0))
            return score

        except Exception:
            return 0.5

    def apply_retention_filter(
        self,
        candidates: pd.DataFrame,
        tail: str,
    ) -> pd.DataFrame:
        """Retain top candidates by composite score. See interface for contract."""
        if candidates.empty:
            return candidates

        filter_cfg = config.secondary_filter_config()
        tail_key = "lower" if tail == "L" else "upper"
        tail_cfg = filter_cfg.get(tail_key, {})
        retention_rate = tail_cfg.get("retention_rate", 0.20)

        n_keep = max(1, int(len(candidates) * retention_rate))

        return (
            candidates
            .sort_values("composite_score", ascending=False)
            .head(n_keep)
            .copy()
        )

    def get_signal_names(self, tail: str) -> List[str]:
        """Return signal names for this tail. See interface for contract."""
        filter_cfg = config.secondary_filter_config()
        tail_key = "lower" if tail == "L" else "upper"
        return filter_cfg.get(tail_key, {}).get("filters", [])
