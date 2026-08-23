"""
Unified configuration for the V9 pairs trading system.

STATUS: live — deployed TODO(sam): date

This module is the single source of truth for all system-wide parameters.
Pair-specific data (betas, hurdles, coefficients) lives in the parameters
Excel file, not here.

Configuration is loaded from a YAML file (config.yaml) at import time.
The schema is defined in config.example.yaml. Every field that was
previously a hardcoded literal in the source now lives in that file.

Principles:
    1. This module is authoritative — no fallbacks to Excel for config values.
    2. All filesystem paths are derived from the base directory set in the
       YAML file. No module may hardcode a path.
    3. Helper functions (connection, validation, printing) live in
       config_helper.py, not here.
"""

import os
import logging
import yaml
from datetime import datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

_CONFIG_FILE = os.environ.get(
    "V9_CONFIG_FILE",
    os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml"),
)

_cfg = {}


def _load_config(path=None):
    """Load configuration from YAML file."""
    global _cfg
    config_path = path or _CONFIG_FILE
    config_path = os.path.abspath(config_path)

    if not os.path.exists(config_path):
        logger.warning(
            "Config file not found at %s — using empty config. "
            "Copy config.example.yaml to config.yaml and fill in values.",
            config_path,
        )
        _cfg = {}
        return

    with open(config_path, "r") as f:
        _cfg = yaml.safe_load(f) or {}

    logger.info("Configuration loaded from %s", config_path)


def get(key, default=None):
    """
    Retrieve a dotted config key.

    Example::

        get("risk.max_portfolio_beta")
        get("ibkr.host", "127.0.0.1")
    """
    parts = key.split(".")
    node = _cfg
    for part in parts:
        if not isinstance(node, dict):
            return default
        node = node.get(part)
        if node is None:
            return default
    return node


# ---------------------------------------------------------------------------
# Trading environment
# ---------------------------------------------------------------------------

def trading_env():
    return get("trading_env", "paper")

# ---------------------------------------------------------------------------
# Version control
# ---------------------------------------------------------------------------

def active_version():
    return get("active_version", "V9.3")

def supported_versions():
    return get("supported_versions", [])

# ---------------------------------------------------------------------------
# Directory paths
# ---------------------------------------------------------------------------

def _base_dir():
    return get("paths.base_dir", "")

def implementation_dir():
    return get("paths.implementation_dir", os.path.join(_base_dir(), "Master Implementation"))

def options_cache_dir():
    return get("paths.options_cache_dir", os.path.join(implementation_dir(), "options_cache"))

def cache_dir():
    return get("paths.cache_dir", "")


def get_version_dir(version=None):
    """Get the directory for a specific calibration version."""
    version = version or active_version()
    return os.path.join(_base_dir(), version)


def get_parameters_file(version=None):
    """Get the parameters Excel file for a specific version."""
    version = version or active_version()
    return os.path.join(get_version_dir(version), "Implementation", f"{version}_Parameters.xlsx")


def get_alpha_data_dir(version=None):
    return get_version_dir(version)


def get_historical_percentiles_file(version=None):
    version = version or active_version()
    return os.path.join(get_version_dir(version), "historical_percentile_distributions.pkl")


def get_beta_files_dir(version=None):
    return get_version_dir(version)


def get_index_subdir(version=None, index=None):
    version_dir = get_version_dir(version)
    if index:
        return os.path.join(version_dir, index)
    return version_dir


def get_tickers_file(version=None):
    """
    Get the All Vanguard ETF Tickers file for a specific version.

    Contains: ETF holdings by sector, megacap definitions and weights,
    category structure.
    """
    version = version or active_version()
    return os.path.join(get_version_dir(version), f"{version} All Vanguard ETF Tickers.xlsx")


def parse_version_from_model(model_version_str):
    """
    Parse version key from Model_Version column in portfolio/trades.

    Examples::

        'V9.2_single_factor' -> 'V9.2'
        '9.2'                -> 'V9.2'
        None                 -> active_version()
    """
    if model_version_str is None:
        return active_version()

    model_str = str(model_version_str).strip()
    if model_str.startswith("V"):
        return model_str.split("_")[0]
    if model_str.replace(".", "").isdigit() or model_str[0].isdigit():
        return f"V{model_str}"
    return active_version()


def is_version_supported(version):
    parsed = parse_version_from_model(version)
    return parsed in supported_versions()

# ---------------------------------------------------------------------------
# Derived file paths (shared, not version-specific)
# ---------------------------------------------------------------------------

def portfolio_file():
    return os.path.join(implementation_dir(), "Portfolio.xlsx")

def completed_trades_file():
    return os.path.join(implementation_dir(), "Completed_Trades.xlsx")

def execution_summary_file():
    return os.path.join(implementation_dir(), "Execution_Summary.xlsx")

def daily_terminated_trades_file():
    return os.path.join(implementation_dir(), "daily_terminated_trades.xlsx")

def shortlist_file():
    return os.path.join(get_version_dir(), "V9_Shortlist.xlsx")

def longlist_file():
    return os.path.join(get_version_dir(), "V9_Longlist.xlsx")

def filter_details_file():
    return os.path.join(get_version_dir(), "V9_Filter_Details.xlsx")

def parameters_file():
    return get_parameters_file()

def earnings_calendar_file():
    return os.path.join(implementation_dir(), "VGT_earnings_calendar.xlsx")

def alpha_history_file():
    return os.path.join(implementation_dir(), "alpha_history.pkl")

def closing_prices_file():
    return os.path.join(implementation_dir(), "daily_closes.xlsx")

def delisted_tickers_file():
    return os.path.join(implementation_dir(), "delisted_tickers.json")

def rejected_trades_archive_file():
    return os.path.join(implementation_dir(), "Rejected_Trades_Archive.xlsx")

# ---------------------------------------------------------------------------
# Index configuration
# ---------------------------------------------------------------------------

def index_etfs():
    return get("index_etfs", ["VGT", "VIS", "VCR", "VHT", "VFH"])

# ---------------------------------------------------------------------------
# Risk parameters
# ---------------------------------------------------------------------------

def max_portfolio_beta():
    return get("risk.max_portfolio_beta")

def min_portfolio_beta():
    return get("risk.min_portfolio_beta")

def target_portfolio_beta():
    return get("risk.target_portfolio_beta")

def allow_corrective_trades():
    return get("risk.allow_corrective_trades", True)

def max_long_ticker_concentration():
    return get("risk.max_long_ticker_concentration")

def max_short_ticker_concentration():
    return get("risk.max_short_ticker_concentration")

def max_account_leverage():
    return get("risk.max_account_leverage")

def margin_safety_buffer():
    return get("risk.margin_safety_buffer")

def emergency_leverage_threshold():
    return get("risk.emergency_leverage_threshold")

def max_index_gross_exposure_pct():
    return get("risk.max_index_gross_exposure_pct")

def max_index_dollar_beta_concentration():
    return get("risk.max_index_dollar_beta_concentration")

def index_specific_min_beta():
    return get("risk.index_specific_min_beta")

def index_specific_max_beta():
    return get("risk.index_specific_max_beta")

def enable_index_concentration_limits():
    return get("risk.enable_index_concentration_limits", True)

# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

def base_trade_size():
    return get("sizing.base_trade_size")

def min_position_size():
    return get("sizing.min_position_size")

def max_position_size():
    return get("sizing.max_position_size")

def max_new_positions_per_ticker():
    return get("sizing.max_new_positions_per_ticker", 1)

# ---------------------------------------------------------------------------
# Portfolio building (relaxed constraints)
# ---------------------------------------------------------------------------

def min_portfolio_value_for_strict_constraints():
    return get("portfolio_building.min_value_for_strict_constraints")

def min_trades_for_concentration_check():
    return get("portfolio_building.min_trades_for_concentration")

def portfolio_building_min_beta():
    return get("portfolio_building.relaxed_min_beta")

def portfolio_building_max_beta():
    return get("portfolio_building.relaxed_max_beta")

def portfolio_building_max_long_concentration():
    return get("portfolio_building.relaxed_max_long_concentration")

def portfolio_building_max_short_concentration():
    return get("portfolio_building.relaxed_max_short_concentration")

# ---------------------------------------------------------------------------
# Strategy config
# ---------------------------------------------------------------------------

def strategy_config():
    return get("strategy_config", {})

def index_biases():
    return get("index_biases", {})

def secondary_filter_config():
    return get("secondary_signals", {})

def stability_weights():
    return get("secondary_signals.stability_weights", {})

# ---------------------------------------------------------------------------
# Pre-filter leniency
# ---------------------------------------------------------------------------

def prefilter_leniency():
    return get("prefilter", {})

def prefilter_max_spread_bps():
    return get("spreads.prefilter_max_spread_bps")

def max_spread_bps():
    return get("spreads.max_spread_bps")

def max_spread_decimal():
    bps = max_spread_bps()
    return bps / 10000 if bps is not None else None

def priority_weights():
    return get("priority_weights", {})

# ---------------------------------------------------------------------------
# Exit rules
# ---------------------------------------------------------------------------

def max_holding_days():
    return get("exits.max_holding_days")

def earnings_lookback_days():
    return get("exits.earnings_lookback_days")

def earnings_exclusion_days_ahead():
    return get("exits.earnings_exclusion_days_ahead")

def earnings_exclusion_days_behind():
    return get("exits.earnings_exclusion_days_behind")

def early_exit_config():
    return get("early_exit", {})

# ---------------------------------------------------------------------------
# Stop-loss
# ---------------------------------------------------------------------------

def execute_stops():
    return get("stop_loss.execute_stops", False)

def enable_short_squeeze_protection():
    return get("stop_loss.enable_short_squeeze_protection", True)

def stop_loss_alpha_threshold():
    return get("stop_loss.alpha_threshold")

def stop_loss_tag_prefix():
    return get("stop_loss.tag_prefix", "SQLOSS_")

# ---------------------------------------------------------------------------
# Trending filter
# ---------------------------------------------------------------------------

def enable_trending_filter():
    return get("trending_filter.enabled", True)

def trend_positive_threshold():
    return get("trending_filter.positive_threshold")

def trend_positive_lookback_months():
    return get("trending_filter.positive_lookback_months")

def trend_negative_threshold():
    return get("trending_filter.negative_threshold")

def trend_negative_lookback_months():
    return get("trending_filter.negative_lookback_months")

# ---------------------------------------------------------------------------
# Market cap filter
# ---------------------------------------------------------------------------

def enable_mcap_filter():
    return get("mcap_filter.enabled", False)

def min_market_cap_millions():
    return get("mcap_filter.min_market_cap_millions")

# ---------------------------------------------------------------------------
# IBKR connection
# ---------------------------------------------------------------------------

def ibkr_host():
    return get("ibkr.host", "127.0.0.1")

def ibkr_port():
    env = trading_env()
    if env == "live":
        return get("ibkr.live_port")
    return get("ibkr.paper_port")

def account_id():
    return get("ibkr.account_id")

def account_type():
    return "LIVE" if trading_env() == "live" else "PAPER"

def max_retries():
    return get("ibkr.max_retries", 3)

def order_timeout():
    return get("ibkr.order_timeout_seconds", 60)

# ---------------------------------------------------------------------------
# Limit orders
# ---------------------------------------------------------------------------

def limit_order_strategy():
    return get("limit_orders.strategy", "AGGRESSIVE")

def limit_order_buffer_cents():
    return get("limit_orders.buffer_cents", 0.01)

def max_limit_order_spread_bps():
    return get("limit_orders.max_spread_bps")

def limit_order_timeout():
    return get("limit_orders.timeout_seconds", 45)

def limit_order_retry_delay():
    return get("limit_orders.retry_delay_seconds", 2)

def exit_limit_buffer_pct():
    return get("exit_limits.buffer_pct", 0.005)

def exit_limit_timeout():
    return get("exit_limits.timeout_seconds", 30)

def remedy_limit_buffer_pct():
    return get("exit_limits.remedy_buffer_pct", 0.005)

def remedy_limit_timeout():
    return get("exit_limits.remedy_timeout_seconds", 20)

# ---------------------------------------------------------------------------
# Order aggregation
# ---------------------------------------------------------------------------

def enable_order_aggregation():
    return get("aggregation.enabled", False)

def min_aggregation_threshold():
    return get("aggregation.min_threshold", 3)

def aggregation_allocation_mode():
    return get("aggregation.allocation_mode", "priority")

# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def price_staleness_threshold():
    return get("market_data.price_staleness_threshold", 300)

def default_market_data_timeout():
    return get("market_data.default_timeout", 10)

def historical_data_lookback_days():
    return get("market_data.historical_lookback_days", 365)

def volume_data_lookback_days():
    return get("market_data.volume_lookback_days", 30)

# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

def alpha_vantage_api_key():
    return get("api_keys.alphavantage", "")

# ---------------------------------------------------------------------------
# Megacap adjustment
# ---------------------------------------------------------------------------

def megacap_adjustment_config():
    return get("megacap_adjustment", {})

# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def reconciliation_tolerance_usd():
    return get("reconciliation.tolerance_usd", 100)

def auto_reconcile_on_startup():
    return get("reconciliation.auto_on_startup", False)

# ---------------------------------------------------------------------------
# Exclusion lists
# ---------------------------------------------------------------------------

def crypto_tickers():
    """Return set of crypto proxy tickers to exclude."""
    entries = get("exclusion_lists.crypto_tickers", [])
    if not entries:
        return set()
    if isinstance(entries[0], dict):
        return {e["ticker"] for e in entries}
    return set(entries)

def mreit_tickers():
    """Return set of mortgage REIT tickers to exclude."""
    entries = get("exclusion_lists.mreit_tickers", [])
    if not entries:
        return set()
    if isinstance(entries[0], dict):
        return {e["ticker"] for e in entries}
    return set(entries)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log_level():
    level_str = get("logging.level", "INFO")
    return getattr(logging, level_str, logging.INFO)

def console_log_level():
    level_str = get("logging.console_level", "WARNING")
    return getattr(logging, level_str, logging.WARNING)

def log_max_bytes():
    return get("logging.max_bytes", 10 * 1024 * 1024)

def log_backup_count():
    return get("logging.backup_count", 5)

# ---------------------------------------------------------------------------
# Factor shock detection
# ---------------------------------------------------------------------------

def factor_shock_config():
    return get("factor_shock", {})

# ---------------------------------------------------------------------------
# Beta estimation
# ---------------------------------------------------------------------------

def beta_estimation_config():
    return get("beta_estimation", {})

# ---------------------------------------------------------------------------
# Universe determination
# ---------------------------------------------------------------------------

def universe_config():
    return get("universe", {})

# ---------------------------------------------------------------------------
# Sum deviation global std
# ---------------------------------------------------------------------------

def sum_deviation_global_std():
    return get("sum_deviation_global_std")

# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def dry_run_mode():
    return get("dry_run_mode", False)

def use_limit_orders_in_paper():
    return get("use_limit_orders_in_paper", False)

def use_limit_orders_in_live():
    return get("use_limit_orders_in_live", True)

def require_first_trade_confirmation():
    return get("require_first_trade_confirmation", True)

def disable_model_based_terminations():
    return get("disable_model_based_terminations", True)

# ---------------------------------------------------------------------------
# Termination offsets
# ---------------------------------------------------------------------------

def termination_order_offset_alpha():
    return get("termination_offsets.alpha")

def termination_order_offset_date():
    return get("termination_offsets.date")

def termination_order_offset_earnings():
    return get("termination_offsets.earnings")

# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Additional path accessors
# ---------------------------------------------------------------------------

def alpha_data_dir(version=None):
    return get_alpha_data_dir(version)

def beta_files_dir(version=None):
    return get_beta_files_dir(version)

def desktop_dir():
    return get("paths.desktop_dir", os.path.join(os.path.expanduser("~"), "Desktop"))

def log_file():
    return get("logging.log_file", "v9_trading.log")

def log_date_format():
    return get("logging.date_format", "%Y-%m-%d %H:%M:%S")

def analyst_archive_dir():
    return os.path.join(implementation_dir(), "analyst_archive")

def data_capture_host():
    return get("ibkr.data_capture_host", ibkr_host())

def data_capture_port():
    return get("ibkr.data_capture_port", get("ibkr.live_port"))

# ---------------------------------------------------------------------------
# Additional parameter accessors
# ---------------------------------------------------------------------------

def version():
    """Return numeric version string (e.g. '9.3') for display."""
    v = active_version()
    return v.replace("V", "") if v else ""

def max_stock_price():
    return get("sizing.max_stock_price", 10000.0)

def prefilter_max_spread_decimal():
    bps = get("spreads.prefilter_max_spread_bps")
    return bps / 10000 if bps is not None else None

def enable_force_termination():
    return get("force_termination.enabled", False)

def force_terminate_tags():
    return get("force_termination.tags", [])

def enable_per_run_ticker_limit():
    return get("sizing.enable_per_run_ticker_limit", True)

def testing_mode():
    return get("testing_mode", False)

def spread_tolerance():
    return get("spreads.spread_tolerance", 0.001)

# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

CURRENCY_FORMAT = "${:,.2f}"
PERCENTAGE_FORMAT = "{:.2%}"
BETA_FORMAT = "{:.4f}"
PRICE_DECIMALS = 2
BETA_DECIMALS = 4
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# ---------------------------------------------------------------------------
# Load configuration at import time
# ---------------------------------------------------------------------------

_load_config()
