"""
Factor shock detection interface.

STATUS: interface only — implementation out of scope

The real implementation uses a multi-factor regression pipeline to detect
regime changes in sector-level factor exposures. When a factor shock is
detected, affected pairs are suppressed from the candidate set before
reaching LAM or portfolio management.

The pre-filter calls ``get_live_status()`` inside a try/except that degrades
silently to "no suppressions active" if the implementation is unavailable.
"""

from abc import ABC, abstractmethod
from typing import Dict, Tuple

import pandas as pd


class FactorShockDetector(ABC):
    """
    Abstract interface for factor shock detection.

    Implementations analyse factor loadings across a sector's constituents
    to identify periods where systematic factor movements would distort
    pair-level alpha signals.
    """

    @abstractmethod
    def get_live_status(self) -> Tuple[Dict, Dict[str, pd.DataFrame]]:
        """
        Return current factor shock status.

        Returns
        -------
        tuple of (status_dict, at_risk_dict)
            status_dict : Per-index summary of shock state.
            at_risk_dict : ``{index: DataFrame}`` where each DataFrame has
                columns ``['co1', 'co2', 'factor', 'factor_z', 'action']``.
                ``action`` is one of ``'SUPPRESS'``, ``'ALERT'``, or ``'OK'``.
        """

    @abstractmethod
    def run_live_monitoring(self) -> Dict:
        """Run a single monitoring cycle and return updated status."""
