"""
Pair generation interface.

STATUS: interface only — implementation out of scope

The pair generator takes the calibrated universe, beta estimates, historical
metrics, and intraday/IV data, then produces the candidate pair universe with
per-pair parameters (EMA multipliers, CDF thresholds, tail assignments).

The selection logic — which pairs survive and with what thresholds — is
proprietary and excluded. This module publishes the contract: inputs, outputs,
and the structural requirements that downstream consumers depend on.

Data contract — inputs:
    - Universe from universe_determination
    - Beta estimates from beta_estimator (per-index Excel files)
    - Historical metrics from metrics_calculator
    - IV data from iv_generation
    - Earnings calendar from historical_earnings_fetch

Data contract — outputs:
    - ``{VERSION}_Parameters.xlsx`` with sheets:
        - ``Pairs``: Tag, Pair, Co1, Co2, Index, Tail, EMA_Multiplier,
          CDF_Threshold
        - ``Tickers``: Ticker, Index, SubSector_Beta, Treasury_Beta, VO_Beta
        - ``15Day_Cumulative_Stats``: Tag, Mean, StdDev, Skew, Kurtosis
        - ``Sum_Deviation_Params``: Parameter, Value
"""

from abc import ABC, abstractmethod
from typing import Dict

import pandas as pd


class PairGenerator(ABC):
    """
    Abstract interface for pair candidate generation.

    Implementations analyse historical relationships between securities in a
    sector to identify pairs with suitable mean-reversion characteristics,
    then calibrate per-pair filter thresholds.
    """

    @abstractmethod
    def generate(
        self,
        index: str,
        universe: pd.DataFrame,
        beta_results: Dict,
        metrics: pd.DataFrame,
        output_dir: str,
    ) -> pd.DataFrame:
        """
        Generate candidate pairs for a single sector index.

        Parameters
        ----------
        index : str
            Sector ETF ticker (e.g. 'VGT').
        universe : pd.DataFrame
            Constituent tickers for this index from universe_determination.
        beta_results : dict
            ``{ticker: {'subsector_beta': float, 'category': str, ...}}``
            from the beta estimator.
        metrics : pd.DataFrame
            Historical pair metrics from metrics_calculator.
        output_dir : str
            Directory for output files.

        Returns
        -------
        pd.DataFrame
            Candidate pairs with columns matching the Pairs sheet contract:
            Tag, Pair, Co1, Co2, Index, Tail, EMA_Multiplier, CDF_Threshold.
        """

    @abstractmethod
    def build_parameters_file(
        self,
        all_pairs: pd.DataFrame,
        ticker_betas: Dict,
        output_path: str,
    ) -> None:
        """
        Assemble the full parameters Excel file from generated pairs.

        Writes the multi-sheet ``{VERSION}_Parameters.xlsx`` file that
        downstream modules (pre_filter, LAM, parameters_extraction) consume.

        Parameters
        ----------
        all_pairs : pd.DataFrame
            Concatenated pairs across all indices.
        ticker_betas : dict
            ``{ticker: {'subsector_beta': float, ...}}`` across all indices.
        output_path : str
            Path for the output Excel file.
        """
