"""
Canonical scoring constants consumed by both calibration and live pipelines.

STATUS: live

This module exists to enforce a single invariant: the constants that the
calibration pipeline uses to define "permissible" must be identical to the
constants that the live pipeline uses to evaluate candidates.

Both pipelines import from here. There is no second copy.

The *structure* of these constants is part of the published architecture.
The *values* are loaded from configuration and are out of scope.

Constants defined here:
    PERCENTILE_BANDS   — CDF bucket boundaries for sum-deviation sizing
    STABILITY_WEIGHTS  — per-signal weights for composite scoring
    SECONDARY_FILTERS  — signal names used per strategy tail
    RETENTION_RATES    — fraction of candidates retained per tail
"""

from src.shared import config

# ---------------------------------------------------------------------------
# Percentile bands
# ---------------------------------------------------------------------------
# These define the CDF bucket boundaries used for sum-deviation-based
# position sizing and exclusion-zone filtering. The same boundaries must
# be used in the optimiser (calibration) and in the live filter (LAM).

PERCENTILE_BANDS = [
    "0-10%", "10-20%", "20-30%", "30-40%", "40-50%",
    "50-60%", "60-70%", "70-80%", "80-90%", "90-100%",
]

# Excluded buckets — these represent the neutral zone where conviction
# is too low to trade. The live pipeline rejects candidates falling here.
EXCLUDED_BUCKETS = {"40-50%", "50-60%"}


def stability_weights():
    """
    Per-signal stability weights for composite scoring.

    These are fitted during calibration and must match between the
    optimiser output and the live LAM scoring function.

    Returns a dict of ``{signal_name: weight}``.
    """
    return config.stability_weights()


def secondary_filter_config():
    """
    Signal names and retention rates per strategy tail.

    Returns a dict with keys ``'lower'`` and ``'upper'``, each containing
    ``'filters'`` (list of signal names) and ``'retention_rate'`` (float).
    """
    return config.secondary_filter_config()


def strategy_config():
    """
    Leg weights and position-size multipliers per strategy and CDF bucket.

    Returns a dict with keys ``'lower'`` and ``'upper'``, each containing
    ``'leg_weights'`` and ``'position_sizes'`` dicts keyed by bucket string.
    """
    return config.strategy_config()


def index_biases():
    """
    Index-specific bias multipliers for composite score adjustment.

    Returns a dict of ``{index: {'lower': float, 'upper': float}}``.
    """
    return config.index_biases()
