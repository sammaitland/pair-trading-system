"""
Signal scoring interface.

STATUS: interface only — implementation out of scope

This module defines the contract that any signal-scoring implementation must
satisfy. The live pipeline (LAM) and calibration pipeline (Optimizer) both
consume a scorer through this interface.

The real implementation uses proprietary signal logic from the shared toolbox.
A naive reference implementation is provided in ``reference_scorer.py`` so
the pipeline executes end to end.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

import pandas as pd


class SignalScorer(ABC):
    """
    Abstract interface for composite signal scoring.

    A scorer takes a candidate pair that has passed primary filters and
    produces a composite score incorporating secondary signals (volume
    metrics, implied volatility, intraday patterns, etc.).

    Implementations must be deterministic for the same inputs.
    """

    @abstractmethod
    def calculate_composite_score(
        self,
        ticker1: str,
        ticker2: str,
        tail: str,
        index: str,
        market_data: Dict,
        historical_percentiles: Optional[Dict] = None,
    ) -> float:
        """
        Calculate composite score for a candidate pair.

        Parameters
        ----------
        ticker1 : str
            First ticker in the pair (Co1).
        ticker2 : str
            Second ticker in the pair (Co2).
        tail : str
            'L' (lower) or 'U' (upper) — which tail triggered.
        index : str
            Sector index ETF (e.g. 'VGT').
        market_data : dict
            Live market data for both tickers. Expected keys per ticker:
            ``live_price``, ``bid``, ``ask``, ``spread``, ``volume``,
            ``historical_data`` (pd.DataFrame with OHLCV).
        historical_percentiles : dict, optional
            Pre-computed percentile distributions from calibration.

        Returns
        -------
        float
            Composite score in [0, 1]. Higher is better.
        """

    @abstractmethod
    def apply_retention_filter(
        self,
        candidates: pd.DataFrame,
        tail: str,
    ) -> pd.DataFrame:
        """
        Filter candidates by retention rate, keeping the top-scoring subset.

        Parameters
        ----------
        candidates : pd.DataFrame
            Must contain at least ``'composite_score'`` column.
        tail : str
            'L' or 'U' — determines which retention rate to use.

        Returns
        -------
        pd.DataFrame
            Filtered subset of candidates.
        """

    @abstractmethod
    def get_signal_names(self, tail: str) -> List[str]:
        """
        Return the list of signal names used for this tail.

        Parameters
        ----------
        tail : str
            'L' or 'U'.

        Returns
        -------
        list of str
            Signal names (e.g. ``['volume_ratio', 'rolling_intraday_vol']``).
        """
