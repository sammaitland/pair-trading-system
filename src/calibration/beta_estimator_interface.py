"""
Beta estimation interface.

STATUS: interface only — implementation out of scope

The beta estimator produces per-ticker beta coefficients against sub-sector
indices. These betas are consumed by the alpha calculation pipeline to
isolate stock-specific returns from sector-level movements.

The real implementation uses dynamic clustering with rolling windows,
adaptive cluster counts, and R-squared filtering. A naive reference
implementation using simple OLS against an equal-weight peer index is
provided in ``reference_beta_estimator.py``.

Data contract — output:
    An Excel file per sector index (``{INDEX}_SubSector_Beta_Analysis.xlsx``)
    containing sheets:
        - ``SubSector Indices``: Daily return time series per sub-sector category
        - ``Cluster Assignments``: Ticker-to-category mapping with betas
        - ``SubSector Beta Summary``: Per-ticker beta coefficients
"""

from abc import ABC, abstractmethod
from typing import Dict, Optional

import pandas as pd


class BetaEstimator(ABC):
    """
    Abstract interface for beta estimation.

    Implementations must produce per-ticker betas against sub-sector indices
    and write the standard output Excel file that downstream modules consume.
    """

    @abstractmethod
    def estimate(
        self,
        etf_ticker: str,
        tickers: list,
        price_data: Dict[str, pd.DataFrame],
        output_dir: str,
    ) -> Dict:
        """
        Estimate betas for all tickers in a sector.

        Parameters
        ----------
        etf_ticker : str
            Sector ETF (e.g. 'VGT').
        tickers : list of str
            Constituent tickers.
        price_data : dict
            ``{ticker: DataFrame}`` with at minimum a ``'close'`` column.
        output_dir : str
            Directory to write the output Excel file.

        Returns
        -------
        dict
            ``{ticker: {'subsector_beta': float, 'category': str, 'r_squared': float}}``
        """
