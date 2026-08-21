"""
Optimisation framework interface.

STATUS: interface only — implementation out of scope

The optimiser takes the pair universe from the pair generator and calibrates
the filter thresholds, position-sizing parameters, and exclusion zones that
the live pipeline enforces. The walk-forward validation structure ensures
that calibrated parameters generalise out of sample.

The objective function internals are proprietary and excluded. This module
publishes the framework contract: inputs, outputs, and the walk-forward
validation structure.

Data contract — inputs:
    - Pair universe from pair generator (``{VERSION}_Parameters.xlsx``)
    - Historical price data for all constituent tickers
    - Spread cost data

Data contract — outputs:
    - Optimised per-index filter thresholds (EMA multipliers, CDF thresholds)
    - Sum deviation exclusion range and sizing buckets
    - Secondary signal filter configuration
    - Walk-forward validation report
"""

from abc import ABC, abstractmethod
from typing import Dict, Optional

import pandas as pd


class PairOptimizer(ABC):
    """
    Abstract interface for the pair optimisation framework.

    Implementations calibrate filter parameters using walk-forward
    validation over historical pair trade data.
    """

    @abstractmethod
    def optimize(
        self,
        pair_data: pd.DataFrame,
        price_data: Dict[str, pd.DataFrame],
        index: str,
        output_dir: str,
    ) -> Dict:
        """
        Run optimisation for a single sector index.

        Parameters
        ----------
        pair_data : pd.DataFrame
            Pair universe with columns from the pair generator output.
        price_data : dict
            ``{ticker: DataFrame}`` with OHLCV data.
        index : str
            Sector ETF ticker (e.g. 'VGT').
        output_dir : str
            Directory for output files.

        Returns
        -------
        dict
            Optimised parameters including:
            ``ema_multiplier``, ``cdf_threshold``, ``exclusion_range``,
            ``secondary_filters``, ``retention_rates``,
            ``walk_forward_results`` (DataFrame of per-fold metrics).
        """

    @abstractmethod
    def validate_walk_forward(
        self,
        pair_data: pd.DataFrame,
        price_data: Dict[str, pd.DataFrame],
        params: Dict,
        n_folds: int = 5,
    ) -> pd.DataFrame:
        """
        Run walk-forward validation on a parameter set.

        Parameters
        ----------
        pair_data : pd.DataFrame
            Pair universe.
        price_data : dict
            Historical prices.
        params : dict
            Parameters to validate.
        n_folds : int
            Number of walk-forward folds.

        Returns
        -------
        pd.DataFrame
            Per-fold metrics (train vs test performance).
        """
