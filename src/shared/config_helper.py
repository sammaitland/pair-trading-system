"""
Configuration helper functions for IB connection management, validation,
logging setup, and print utilities.

Provides operational support on top of the config module: connecting to IBKR,
validating configuration consistency, setting up logging, and printing
configuration summaries.

STATUS: live
"""

import os
import random
import logging
import time
import asyncio
from datetime import datetime

from src.shared import config

logger = logging.getLogger(__name__)


# ============================================================================
# IB CONNECTION MANAGEMENT
# ============================================================================

def get_client_id():
    """
    Generate unique client ID based on timestamp and random number.
    Prevents client ID conflicts across multiple connections.

    Returns
    -------
    int : Unique client ID in range [100, 32767]
    """
    # Combine timestamp (last 3 digits) with random number
    timestamp_part = int(time.time() % 1000)  # Last 3 digits of timestamp
    random_part = random.randint(0, 99)
    client_id = timestamp_part * 100 + random_part

    # Ensure it's in valid range (0-32767)
    client_id = client_id % 32767

    # Avoid reserved IDs (< 100)
    if client_id < 100:
        client_id += 100

    return client_id


async def connect_ib_async(timeout=10, max_retries=3,
                           host=None, port=None, client_id=None):
    """
    Connect to IB with automatic retry and client ID management (async).

    Parameters
    ----------
    timeout : int
        Connection timeout in seconds
    max_retries : int
        Maximum number of connection attempts
    host : str, optional
        Override config.ibkr_host() (default: None -> use config value)
    port : int, optional
        Override config.ibkr_port() (default: None -> use config value)
    client_id : int, optional
        Override auto-generated client ID (default: None -> auto-generate)

    Returns
    -------
    tuple: (ib, connected)
        ib: IB instance (or None if failed)
        connected: bool indicating success

    Example
    -------
    >>> ib, connected = await connect_ib_async()
    >>> if connected:
    ...     print("Ready to trade!")
    """
    from ib_insync import IB

    connect_host = host if host is not None else config.ibkr_host()
    connect_port = port if port is not None else config.ibkr_port()

    ib = IB()
    ib.RequestTimeout = timeout

    for attempt in range(max_retries):
        try:
            # Generate unique client ID if not provided
            cid = client_id if client_id is not None else get_client_id()

            # Ensure clean state
            if ib.isConnected():
                ib.disconnect()
                await asyncio.sleep(0.5)

            if attempt > 0:
                logging.info(f"Connection attempt {attempt + 1}/{max_retries} (client ID: {cid})")
            else:
                logging.debug(f"Connecting with client ID: {cid}")

            # Attempt connection
            await ib.connectAsync(
                connect_host,
                connect_port,
                clientId=cid,
                timeout=timeout
            )

            if ib.isConnected():
                logging.info(f"Connected to IBKR ({config.account_type()}) with client ID {cid}")
                return ib, True

        except Exception as e:
            logging.warning(f"Connection attempt {attempt + 1} failed: {e}")

            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                logging.info(f"Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
            else:
                logging.error("All connection attempts failed")

    return None, False


def connect_ib_sync(timeout=10, max_retries=3,
                    host=None, port=None):
    """
    Connect to IB with automatic retry and client ID management (sync).

    For use in non-async contexts (regular Python scripts, not notebooks).

    Parameters
    ----------
    timeout : int
        Connection timeout in seconds
    max_retries : int
        Maximum number of connection attempts
    host : str, optional
        Override host (defaults to config.ibkr_host())
    port : int, optional
        Override port (defaults to config.ibkr_port())

    Returns
    -------
    tuple: (ib, connected)
    """
    from ib_insync import IB

    connect_host = host if host is not None else config.ibkr_host()
    connect_port = port if port is not None else config.ibkr_port()

    ib = IB()
    ib.RequestTimeout = timeout

    for attempt in range(max_retries):
        try:
            cid = get_client_id()

            if ib.isConnected():
                ib.disconnect()
                time.sleep(0.5)

            if attempt > 0:
                logging.info(f"Connection attempt {attempt + 1}/{max_retries} (client ID: {cid})")

            ib.connect(
                connect_host,
                connect_port,
                clientId=cid,
                timeout=timeout
            )

            if ib.isConnected():
                logging.info(f"Connected to IBKR ({config.account_type()}) with client ID {cid}")
                return ib, True

        except Exception as e:
            logging.warning(f"Connection attempt {attempt + 1} failed: {e}")

            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                logging.info(f"Retrying in {wait_time}s...")
                time.sleep(wait_time)
            else:
                logging.error("All connection attempts failed")

    return None, False


def get_available_liquidity(ib, account_id=None, verbose=False, convert_to_usd=True):
    """
    Get available trading liquidity from IBKR account.

    Uses ExcessLiquidity if available, otherwise falls back to a percentage
    of NetLiquidation.

    Parameters
    ----------
    ib : IB
        Connected IB instance
    account_id : str, optional
        Account ID (not used -- kept for backward compatibility)
    verbose : bool
        If True, print all available account values for debugging
    convert_to_usd : bool
        If True and account is in GBP, convert to USD

    Returns
    -------
    float : Available liquidity in USD (or base currency if convert_to_usd=False)
    """
    if ib is None or not ib.isConnected():
        logging.warning("No IBKR connection - returning 0 liquidity")
        return 0.0

    try:
        # Get ALL account values (don't specify account ID - that can cause issues)
        all_values = ib.accountValues()

        if not all_values:
            logging.warning("No account values returned from IBKR")
            return 0.0

        # Find the base currency first (GBP or USD)
        base_currency = 'USD'
        for item in all_values:
            if item.tag == 'NetLiquidation':
                base_currency = item.currency
                break

        # Build dict of values in base currency
        values = {}
        for item in all_values:
            if item.currency == base_currency:
                try:
                    values[item.tag] = float(item.value) if item.value else 0.0
                except (ValueError, TypeError):
                    # Skip non-numeric values (like account IDs)
                    pass

        if verbose:
            print(f"\n=== IBKR Account Values ({base_currency}) ===")
            liquidity_tags = ['ExcessLiquidity', 'FullExcessLiquidity', 'AvailableFunds',
                            'FullAvailableFunds', 'BuyingPower', 'NetLiquidation',
                            'TotalCashValue', 'GrossPositionValue']
            for tag in liquidity_tags:
                if tag in values:
                    print(f"  {tag}: {values[tag]:,.2f}")
            print("=" * 45)

        # Determine liquidity value in base currency
        liquidity = 0.0
        source = None

        # Try to get liquidity in order of preference
        # 1. ExcessLiquidity - the actual available margin for new trades
        if values.get('ExcessLiquidity', 0) > 0:
            liquidity = values['ExcessLiquidity']
            source = 'ExcessLiquidity'
        # 2. FullExcessLiquidity
        elif values.get('FullExcessLiquidity', 0) > 0:
            liquidity = values['FullExcessLiquidity']
            source = 'FullExcessLiquidity'
        # 3. AvailableFunds
        elif values.get('AvailableFunds', 0) > 0:
            liquidity = values['AvailableFunds']
            source = 'AvailableFunds'
        # 4. Fall back to 50% of NetLiquidation (conservative estimate of available margin)
        elif values.get('NetLiquidation', 0) > 0:
            liquidity = values['NetLiquidation'] * 0.5
            source = '50% of NetLiquidation'

        if liquidity <= 0:
            logging.warning("Could not determine liquidity from account values")
            return 0.0

        # Convert to USD if account is in GBP
        if base_currency == 'GBP' and convert_to_usd:
            gbp_usd_rate = get_gbp_usd_rate(ib)

            # Fallback if rate fetch fails
            if gbp_usd_rate is None or gbp_usd_rate <= 0:
                logging.warning("Invalid GBP/USD rate, using fallback 1.25")
                gbp_usd_rate = 1.25

            liquidity_usd = liquidity * gbp_usd_rate
            logging.info(f"{source} ({base_currency}): {liquidity:,.2f} x {gbp_usd_rate:.4f} = ${liquidity_usd:,.2f} USD")
            return liquidity_usd
        else:
            symbol = 'GBP' if base_currency == 'GBP' else '$'
            logging.info(f"Using {source} ({base_currency}): {symbol}{liquidity:,.2f}")
            return liquidity

    except Exception as e:
        logging.error(f"Error getting liquidity: {e}")
        import traceback
        traceback.print_exc()
        return 0.0


def disconnect_ib(ib):
    """
    Safely disconnect from IB.

    Parameters
    ----------
    ib : IB
        IB instance to disconnect

    Returns
    -------
    bool : True if disconnected successfully
    """
    if ib is None:
        return True

    try:
        if ib.isConnected():
            ib.disconnect()
            logging.info("Disconnected from IBKR")
        return True
    except Exception as e:
        logging.error(f"Error disconnecting: {e}")
        return False


def get_gbp_usd_rate(ib):
    """
    Fetch live GBP/USD exchange rate from IBKR.

    Returns
    -------
    float: GBP/USD rate (e.g., 1.27 means 1 GBP = 1.27 USD)
    """
    _GBP_USD_FALLBACK = 1.25

    if ib is None or not ib.isConnected():
        logger.warning("No IBKR connection - using default GBP/USD rate")
        return _GBP_USD_FALLBACK

    try:
        from ib_insync import Forex

        # Create GBP/USD forex contract
        contract = Forex('GBPUSD')
        qualified = ib.qualifyContracts(contract)

        if not qualified:
            logger.warning("Could not qualify GBP/USD contract")
            return _GBP_USD_FALLBACK

        # Request market data
        ib.reqMktData(qualified[0], '', False, False)
        ib.sleep(2)  # Wait for data

        ticker = ib.ticker(qualified[0])

        if ticker and ticker.midpoint():
            rate = ticker.midpoint()
            logger.info(f"Live GBP/USD rate: {rate:.4f}")
            ib.cancelMktData(qualified[0])
            return rate
        elif ticker and ticker.last:
            rate = ticker.last
            logger.info(f"GBP/USD rate (last): {rate:.4f}")
            ib.cancelMktData(qualified[0])
            return rate
        else:
            logger.warning("No GBP/USD price available - using default")
            ib.cancelMktData(qualified[0])
            return _GBP_USD_FALLBACK

    except Exception as e:
        logger.error(f"Error fetching GBP/USD rate: {e}")
        return _GBP_USD_FALLBACK


# ============================================================================
# VALIDATION UTILITIES
# ============================================================================

def verify_required_files():
    """
    Verify all required files exist.

    Call this at startup to catch missing files early.
    """
    required_files = {
        'Portfolio': config.portfolio_file(),
        'Shortlist': config.shortlist_file(),
        'Parameters': config.parameters_file(),
        'Earnings Calendar': config.earnings_calendar_file(),
        'Completed Trades': config.completed_trades_file()
    }

    missing = []
    for name, path in required_files.items():
        if not os.path.exists(path):
            missing.append(f"{name}: {path}")

    if missing:
        print("Missing required files:")
        for item in missing:
            print(f"  - {item}")
        return False

    print("All required files found")
    return True


def validate_config():
    """Validate configuration for common errors."""
    errors = []
    warnings = []

    # Check directory paths exist
    if not os.path.exists(config.implementation_dir()):
        errors.append(f"Implementation directory not found: {config.implementation_dir()}")

    if not os.path.exists(config.desktop_dir()):
        errors.append(f"Desktop directory not found: {config.desktop_dir()}")

    # Check critical files exist
    if not os.path.exists(config.parameters_file()):
        errors.append(f"Parameters file not found: {config.parameters_file()}")

    if not os.path.exists(config.earnings_calendar_file()):
        warnings.append(f"Earnings calendar not found: {config.earnings_calendar_file()}")

    # Check parameter values
    if config.base_trade_size() <= 0:
        errors.append(f"base_trade_size must be positive: {config.base_trade_size()}")

    if config.max_account_leverage() <= 0:
        errors.append(f"max_account_leverage must be positive: {config.max_account_leverage()}")

    # Check constraint logic
    if config.min_position_size() > config.max_position_size():
        errors.append(f"min_position_size ({config.min_position_size()}) > max_position_size ({config.max_position_size()})")

    # Check strategy config completeness
    strategy_cfg = config.strategy_config()
    for strategy in ['lower', 'upper']:
        if strategy not in strategy_cfg:
            errors.append(f"Missing strategy config: {strategy}")
        else:
            expected_buckets = ['0-10%', '10-20%', '20-30%', '30-40%', '40-50%',
                              '50-60%', '60-70%', '70-80%', '80-90%', '90-100%']
            for bucket in expected_buckets:
                if bucket not in strategy_cfg[strategy]['leg_weights']:
                    errors.append(f"Missing leg_weights for {strategy}/{bucket}")
                if bucket not in strategy_cfg[strategy]['position_sizes']:
                    errors.append(f"Missing position_sizes for {strategy}/{bucket}")

    # Trading mode validation
    if config.trading_env() not in ['paper', 'live']:
        errors.append(f"trading_env must be 'paper' or 'live': {config.trading_env()}")

    if config.trading_env() == 'live' and "YOUR_LIVE_ACCOUNT_ID" in config.account_id():
        errors.append("LIVE trading mode requires valid account_id")

    # Print results
    if errors:
        print("CONFIGURATION ERRORS:")
        for error in errors:
            print(f"  - {error}")

    if warnings:
        print("\nCONFIGURATION WARNINGS:")
        for warning in warnings:
            print(f"  - {warning}")

    if not errors and not warnings:
        print("Configuration validation passed")

    return len(errors) == 0


# ============================================================================
# LOGGING SETUP
# ============================================================================

def setup_logging():
    """
    Configure logging for the system.
    Call this once at the start of workflow.
    """
    from logging.handlers import RotatingFileHandler

    # Root logger
    root_logger = logging.getLogger()

    # Prevent duplicate handlers if called more than once
    if root_logger.handlers:
        return

    root_logger.setLevel(config.log_level())

    # File handler with rotation
    file_handler = RotatingFileHandler(
        config.log_file(),
        maxBytes=config.log_max_bytes(),
        backupCount=config.log_backup_count()
    )
    file_handler.setLevel(config.log_level())
    file_formatter = logging.Formatter(config.log_format(), config.log_date_format())
    file_handler.setFormatter(file_formatter)

    # Console handler (less verbose)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(config.console_log_level())
    console_formatter = logging.Formatter('%(levelname)s: %(message)s')
    console_handler.setFormatter(console_formatter)

    # Add handlers
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # Suppress verbose IBKR logs
    logging.getLogger("ib_insync").setLevel(logging.WARNING)

    logging.info(f"Logging initialized - Version {config.version()}")


# ============================================================================
# PRINT UTILITIES
# ============================================================================

def print_live_trading_config():
    """Print live trading safety configuration."""
    print("\n" + "=" * 80)
    print("LIVE TRADING SAFETY CONFIGURATION")
    print("=" * 80)

    print(f"\nAccount Settings:")
    print(f"  Environment: {config.trading_env()}")
    print(f"  Account ID: {config.account_id()}")
    print(f"  Dry Run Mode: {config.dry_run_mode()}")

    print(f"\nLeverage Limits:")
    print(f"  Max Account Leverage: {config.max_account_leverage()}x")
    print(f"  Emergency Threshold: {config.emergency_leverage_threshold()}x")
    print(f"  Margin Safety Buffer: {config.margin_safety_buffer():.0%}")

    print(f"\nPosition Limits:")
    print(f"  Min Position Size: ${config.min_position_size():,.0f}")
    print(f"  Max Position Size: ${config.max_position_size():,.0f}")

    print(f"\nOrder Settings:")
    print(f"  Order Type (Live): {'LIMIT' if config.use_limit_orders_in_live() else 'MARKET'}")
    print(f"  Order Timeout: {config.order_timeout()}s")
    print(f"  Max Retries: {config.max_retries()}")

    print("\n" + "=" * 80)


def print_config_summary():
    """Print a summary of current configuration."""
    print("=" * 80)
    print(f"CONFIGURATION SUMMARY - Version {config.version()}")
    print("=" * 80)

    print(f"\nEnvironment:")
    print(f"  Trading mode: {config.account_type()}")
    print(f"  Dry run: {config.dry_run_mode()}")
    print(f"  IBKR port: {config.ibkr_port()}")
    print(f"  Account ID: {config.account_id()}")

    print(f"\nDirectories:")
    print(f"  Desktop: {config.desktop_dir()}")
    print(f"  Implementation: {config.implementation_dir()}")
    print(f"  Cache: {config.cache_dir()}")

    print(f"\nCritical Files:")
    print(f"  Portfolio: {config.portfolio_file()}")
    print(f"  Parameters: {config.parameters_file()}")
    print(f"  Earnings: {config.earnings_calendar_file()}")
    print(f"  Log: {config.log_file()}")

    print(f"\nRisk Parameters:")
    print(f"  Portfolio beta range: [{config.min_portfolio_beta():.4f}, {config.max_portfolio_beta():.4f}]")
    print(f"  Target portfolio beta: {config.target_portfolio_beta():.4f}")
    print(f"  Max long ticker concentration: {config.max_long_ticker_concentration():.1%}")
    print(f"  Max short ticker concentration: {config.max_short_ticker_concentration():.1%}")

    print(f"\nPosition Sizing:")
    print(f"  Base trade size: ${config.base_trade_size():,.0f}")
    print(f"  Position multiplier range: 0.6x - 1.5x")
    print(f"  Excluded buckets: 40-50%, 50-60%")

    print(f"\nStrategy Weights:")
    print(f"  Lower strategy: Asymmetric (0.60-0.80 on stock1)")
    print(f"  Upper strategy: Symmetric (0.50 on both)")

    print(f"\nExit Rules:")
    print(f"  Max holding: {config.max_holding_days()} days")
    print(f"  Earnings exit: {config.earnings_lookback_days()} days before")
    print(f"  SES (model exits): {'DISABLED' if config.disable_model_based_terminations() else 'ENABLED'}")

    print(f"\nPre-Filter Leniency:")
    prefilter = config.prefilter_leniency()
    print(f"  CDF adjustment: +/-{prefilter['cdf_adjustment']:.2f}")
    print(f"  2-day reduction: {prefilter['two_day_reduction']:.0%}")
    print(f"  Sum dev exclusion: {prefilter['sum_dev_neutral_zone'][0]:.0%}-{prefilter['sum_dev_neutral_zone'][1]:.0%}")

    print("\n" + "=" * 80)


# ============================================================================
# CONFIG GETTER FUNCTIONS
# ============================================================================

def should_use_limit_orders():
    """
    Determine if limit orders should be used based on trading environment.

    Returns
    -------
    bool: True if limit orders should be used
    """
    if config.dry_run_mode():
        return False  # No orders placed anyway
    elif config.trading_env() == 'live':
        return config.use_limit_orders_in_live()
    else:  # paper
        return config.use_limit_orders_in_paper()


def get_index_bias(index, tail):
    """
    Get index-specific bias multiplier.

    Parameters
    ----------
    index : str
        Sector index (VCR, VFH, VGT, VHT, VIS)
    tail : str
        'L' or 'U' for lower/upper tail

    Returns
    -------
    float : Bias multiplier (default 1.0 if not found)
    """
    tail_key = 'lower' if tail == 'L' else 'upper'
    return config.index_biases().get(index, {}).get(tail_key, 1.0)


# Sizing and constraint functions are in constraints.py:
# get_leg_weights, get_position_multiplier, is_tradeable_bucket,
# get_effective_trade_size, get_strategy_config_summary,
# check_index_concentration, get_max_gross_exposure,
# check_leverage_limit, check_emergency_leverage
