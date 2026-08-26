"""
Shared fixtures for the test suite.

Provides a synthetic config, synthetic parameters file, and synthetic
market data so that tests run offline without broker or API dependencies.
"""

import os
import sys
import tempfile
import shutil

import numpy as np
import pandas as pd
import pytest
import yaml


# ---------------------------------------------------------------------------
# Ensure the repo root is on sys.path so `from src...` imports work
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ---------------------------------------------------------------------------
# Synthetic config
# ---------------------------------------------------------------------------

_SYNTHETIC_CONFIG = {
    "trading_env": "paper",
    "active_version": "V9.3",
    "supported_versions": ["V9.2"],
    "paths": {
        "base_dir": "",  # populated per-test with tmpdir
        "implementation_dir": "",
        "cache_dir": "",
    },
    "index_etfs": ["VGT", "VFH", "VIS", "VHT", "VCR"],
    "risk": {
        "max_portfolio_beta": 0.20,
        "min_portfolio_beta": -0.20,
        "target_portfolio_beta": 0.0,
        "allow_corrective_trades": True,
        "max_long_ticker_concentration": 0.15,
        "max_short_ticker_concentration": 0.15,
        "max_account_leverage": 2.0,
        "margin_safety_buffer": 0.10,
        "emergency_leverage_threshold": 2.5,
        "max_index_gross_exposure_pct": 0.40,
        "max_index_dollar_beta_concentration": 0.50,
        "index_specific_min_beta": -0.10,
        "index_specific_max_beta": 0.10,
        "enable_index_concentration_limits": True,
    },
    "sizing": {
        "base_trade_size": 10000,
        "min_position_size": 1000,
        "max_position_size": 50000,
        "max_new_positions_per_ticker": 1,
        "min_stock_price": 1.0,
        "max_stock_price": 10000.0,
    },
    "portfolio_building": {
        "min_value_for_strict_constraints": 50000,
        "min_trades_for_concentration": 3,
        "relaxed_min_beta": -0.50,
        "relaxed_max_beta": 0.50,
        "relaxed_max_long_concentration": 0.30,
        "relaxed_max_short_concentration": 0.30,
    },
    "strategy_config": {
        "lower": {
            "leg_weights": {
                "0-10%": [0.50, 0.50],
                "10-20%": [0.50, 0.50],
                "20-30%": [0.50, 0.50],
                "30-40%": [0.50, 0.50],
                "40-50%": [0.50, 0.50],
                "50-60%": [0.50, 0.50],
                "60-70%": [0.50, 0.50],
                "70-80%": [0.50, 0.50],
                "80-90%": [0.50, 0.50],
                "90-100%": [0.50, 0.50],
            },
            "position_sizes": {
                "0-10%": 1.5,
                "10-20%": 1.2,
                "20-30%": 1.0,
                "30-40%": 0.8,
                "40-50%": 0.0,
                "50-60%": 0.0,
                "60-70%": 0.8,
                "70-80%": 1.0,
                "80-90%": 1.2,
                "90-100%": 1.5,
            },
        },
        "upper": {
            "leg_weights": {
                "0-10%": [0.50, 0.50],
                "10-20%": [0.50, 0.50],
                "20-30%": [0.50, 0.50],
                "30-40%": [0.50, 0.50],
                "40-50%": [0.50, 0.50],
                "50-60%": [0.50, 0.50],
                "60-70%": [0.50, 0.50],
                "70-80%": [0.50, 0.50],
                "80-90%": [0.50, 0.50],
                "90-100%": [0.50, 0.50],
            },
            "position_sizes": {
                "0-10%": 1.5,
                "10-20%": 1.2,
                "20-30%": 1.0,
                "30-40%": 0.8,
                "40-50%": 0.0,
                "50-60%": 0.0,
                "60-70%": 0.8,
                "70-80%": 1.0,
                "80-90%": 1.2,
                "90-100%": 1.5,
            },
        },
    },
    "secondary_signals": {
        "lower": {
            "filters": ["volume_ratio", "rolling_intraday_vol"],
            "retention_rate": 0.20,
        },
        "upper": {
            "filters": ["volume_ratio", "rolling_intraday_vol"],
            "retention_rate": 0.20,
        },
        "stability_weights": {
            "volume_ratio": 0.5,
            "rolling_intraday_vol": 0.5,
        },
    },
    "index_biases": {},
    "spreads": {
        "prefilter_max_spread_bps": 50,
        "max_spread_bps": 40,
    },
    "market_data": {
        "price_staleness_threshold": 300,
        "default_timeout": 10,
        "historical_lookback_days": 365,
        "volume_lookback_days": 30,
    },
    "prefilter": {
        "cdf_adjustment": 0.05,
        "sum_dev_neutral_zone": [0.4, 0.6],
    },
    "exits": {
        "max_holding_days": 15,
        "earnings_lookback_days": 5,
        "earnings_exclusion_days_ahead": 6,
        "earnings_exclusion_days_behind": 1,
    },
    "stop_loss": {
        "execute_stops": False,
        "enable_short_squeeze_protection": True,
        "alpha_threshold": -0.08,
    },
    "trending_filter": {
        "enabled": False,
    },
    "mcap_filter": {
        "enabled": False,
    },
    "sum_deviation_global_std": 0.035,
    "logging": {
        "level": "WARNING",
        "console_level": "WARNING",
    },
}


@pytest.fixture(autouse=True)
def synthetic_config(tmp_path):
    """
    Write a synthetic config.yaml and reload config before each test.

    This ensures every test runs against known configuration values
    and no test depends on a user's local config.yaml.
    """
    cfg = _SYNTHETIC_CONFIG.copy()
    cfg["paths"] = {
        "base_dir": str(tmp_path / "data"),
        "implementation_dir": str(tmp_path / "data" / "Master Implementation"),
        "cache_dir": str(tmp_path / "cache"),
    }

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(cfg, default_flow_style=False))

    # Create directories
    os.makedirs(cfg["paths"]["base_dir"], exist_ok=True)
    os.makedirs(cfg["paths"]["implementation_dir"], exist_ok=True)
    os.makedirs(cfg["paths"]["cache_dir"], exist_ok=True)

    # Reload config from this file
    from src.shared import config
    config._load_config(str(config_path))

    yield cfg


@pytest.fixture
def synthetic_parameters(tmp_path):
    """
    Generate a synthetic parameters Excel file in tmp_path.

    Returns the path to the file.
    """
    from fixtures.synthetic_pairs import generate_synthetic_parameters

    output_path = str(tmp_path / "Synthetic_Parameters.xlsx")
    generate_synthetic_parameters(output_path, n_pairs_per_index=3)
    return output_path


@pytest.fixture
def synthetic_price_data():
    """
    Generate synthetic daily OHLCV price data for test tickers.

    Returns a dict of {ticker: DataFrame} with 252 rows.
    """
    rng = np.random.default_rng(42)
    tickers = [
        "SYNTA", "SYNTB", "SYNTC",
        "SYNFA", "SYNFB", "SYNFC",
        "VGT", "VFH",
    ]
    data = {}
    dates = pd.bdate_range(end=pd.Timestamp.today(), periods=252)

    for ticker in tickers:
        base_price = rng.uniform(50, 200)
        returns = rng.normal(0.0005, 0.02, size=len(dates))
        prices = base_price * np.cumprod(1 + returns)

        df = pd.DataFrame({
            "open": prices * rng.uniform(0.99, 1.0, size=len(dates)),
            "high": prices * rng.uniform(1.0, 1.02, size=len(dates)),
            "low": prices * rng.uniform(0.98, 1.0, size=len(dates)),
            "close": prices,
            "volume": rng.integers(100000, 5000000, size=len(dates)),
        }, index=dates)
        data[ticker] = df

    return data
