"""
Portfolio constraint functions used by both the implementation pipeline and
calibration scripts. Ensures alignment between what calibration assumes is
permissible and what portfolio management enforces live.

Categories:
- Leg weight and position sizing getters
- Leverage and beta limit checks
- Index concentration checks
- Ticker concentration checks
- IGV beta exposure calculation

STATUS: live
"""

from src.shared import config

import logging
logger = logging.getLogger(__name__)


# ============================================================================
# LEG WEIGHTS & POSITION SIZING
# ============================================================================

def get_leg_weights(trigger_type, sum_dev_bucket):
    """
    Get leg weights for a specific strategy and bucket.

    Parameters
    ----------
    trigger_type : str
        'lower' or 'upper'
    sum_dev_bucket : str
        CDF bucket like '0-10%', '10-20%', etc.

    Returns
    -------
    tuple : (stock1_weight, stock2_weight)
    """
    strategy_cfg = config.strategy_config()
    if (trigger_type in strategy_cfg and
            sum_dev_bucket in strategy_cfg[trigger_type]['leg_weights']):
        return strategy_cfg[trigger_type]['leg_weights'][sum_dev_bucket]
    return (0.50, 0.50)


def get_position_multiplier(trigger_type, sum_dev_bucket):
    """
    Get position size multiplier for a specific strategy and bucket.

    Parameters
    ----------
    trigger_type : str
        'lower' or 'upper'
    sum_dev_bucket : str
        CDF bucket like '0-10%', '10-20%', etc.

    Returns
    -------
    float : Position size multiplier (applied to base_trade_size)
    """
    strategy_cfg = config.strategy_config()
    if (trigger_type in strategy_cfg and
            sum_dev_bucket in strategy_cfg[trigger_type]['position_sizes']):
        return strategy_cfg[trigger_type]['position_sizes'][sum_dev_bucket]
    return 1.0


def is_tradeable_bucket(trigger_type, sum_dev_bucket):
    """
    Check if a bucket is tradeable (position size > 0).

    Parameters
    ----------
    trigger_type : str
        'lower' or 'upper'
    sum_dev_bucket : str
        CDF bucket like '0-10%', '10-20%', etc.

    Returns
    -------
    bool : True if tradeable, False otherwise
    """
    return get_position_multiplier(trigger_type, sum_dev_bucket) > 0


def get_effective_trade_size(trigger_type, sum_dev_bucket):
    """
    Calculate effective trade size for a strategy/bucket combination.

    Returns
    -------
    float : Effective notional trade size
    """
    return config.base_trade_size() * get_position_multiplier(trigger_type, sum_dev_bucket)


def get_strategy_config_summary():
    """
    Get a summary DataFrame of all strategy configurations.

    Returns
    -------
    pd.DataFrame : Summary of all configurations
    """
    import pandas as pd

    strategy_cfg = config.strategy_config()
    summary = []
    for strategy in ['lower', 'upper']:
        for bucket in sorted(strategy_cfg[strategy]['leg_weights'].keys()):
            weights = strategy_cfg[strategy]['leg_weights'][bucket]
            size = strategy_cfg[strategy]['position_sizes'][bucket]

            summary.append({
                'strategy': strategy,
                'bucket': bucket,
                'stock1_weight': weights[0],
                'stock2_weight': weights[1],
                'position_multiplier': size,
                'tradeable': size > 0,
                'actual_base_size': config.base_trade_size() * size
            })

    return pd.DataFrame(summary)


# ============================================================================
# LEVERAGE & BETA LIMIT CHECKS
# ============================================================================

def get_max_gross_exposure(available_equity):
    """
    Calculate maximum allowed gross exposure based on equity.

    Parameters
    ----------
    available_equity : float
        Current account equity

    Returns
    -------
    float : Maximum gross exposure allowed
    """
    return available_equity * config.max_account_leverage()


def check_leverage_limit(current_gross_exposure, available_equity):
    """
    Check if current leverage is within limits.

    Parameters
    ----------
    current_gross_exposure : float
        Sum of absolute values of all positions
    available_equity : float
        Current account equity

    Returns
    -------
    tuple : (within_limits, current_leverage, max_allowed)
    """
    if available_equity <= 0:
        return False, float('inf'), config.max_account_leverage()

    current_leverage = current_gross_exposure / available_equity
    max_allowed = config.max_account_leverage()
    within_limits = current_leverage <= max_allowed

    return within_limits, current_leverage, max_allowed


def check_emergency_leverage(current_gross_exposure, available_equity):
    """
    Check if emergency leverage threshold is exceeded.

    Returns
    -------
    bool : True if emergency threshold exceeded (STOP TRADING)
    """
    if available_equity <= 0:
        return True

    current_leverage = current_gross_exposure / available_equity
    return current_leverage >= config.emergency_leverage_threshold()


# ============================================================================
# INDEX CONCENTRATION CHECKS
# ============================================================================

def check_index_concentration(index_breakdown_df, total_portfolio_dollar_beta):
    """
    Check which indexes exceed concentration threshold.

    Parameters
    ----------
    index_breakdown_df : DataFrame
        From calculate_index_breakdown() with Dollar_Beta_Exposure column
    total_portfolio_dollar_beta : float
        Total portfolio dollar beta exposure

    Returns
    -------
    dict : {index: {'dollar_beta': X, 'pct_of_total': Y, 'exceeds_threshold': bool}}
    """
    if not config.enable_index_concentration_limits():
        return {}

    if total_portfolio_dollar_beta == 0:
        return {}

    concentrated_indexes = {}

    for _, row in index_breakdown_df.iterrows():
        index = row['Index']
        dollar_beta = row['Dollar_Beta_Exposure']
        pct_of_total = abs(dollar_beta) / abs(total_portfolio_dollar_beta)
        exceeds = pct_of_total > config.max_index_dollar_beta_concentration()

        concentrated_indexes[index] = {
            'dollar_beta': dollar_beta,
            'pct_of_total': pct_of_total,
            'exceeds_threshold': exceeds
        }

    return concentrated_indexes


# ============================================================================
# TICKER CONCENTRATION CHECKS
# ============================================================================

def calculate_ticker_exposures(portfolio_df, approved_trades_list):
    """
    Calculate current long and short notional exposure per ticker.

    Parameters
    ----------
    portfolio_df : DataFrame
        Current portfolio (Co1, Co2, Tail, Trade Value Co1 ($), Trade Value Co2 ($))
    approved_trades_list : list of dict
        Trades approved so far in the current evaluation run.
        Each dict must have: Co1, Co2, trigger_type,
        Trade Value Co1 ($), Trade Value Co2 ($)

    Returns
    -------
    tuple : (long_exposures, short_exposures) -- both {ticker: notional}
    """
    long_exposures = {}
    short_exposures = {}

    if portfolio_df is not None and not portfolio_df.empty:
        for _, trade in portfolio_df.iterrows():
            ticker1 = trade['Co1']
            ticker2 = trade['Co2']
            tail = str(trade.get('Tail', 'L')).strip().upper()
            notional1 = abs(trade.get('Trade Value Co1 ($)', 0))
            notional2 = abs(trade.get('Trade Value Co2 ($)', 0))

            if tail == 'L':
                long_exposures[ticker1] = long_exposures.get(ticker1, 0) + notional1
                short_exposures[ticker2] = short_exposures.get(ticker2, 0) + notional2
            else:
                short_exposures[ticker1] = short_exposures.get(ticker1, 0) + notional1
                long_exposures[ticker2] = long_exposures.get(ticker2, 0) + notional2

    for trade in approved_trades_list:
        ticker1 = trade['Co1']
        ticker2 = trade['Co2']
        trigger_type = trade.get('trigger_type', 'lower')
        notional1 = trade['Trade Value Co1 ($)']
        notional2 = trade['Trade Value Co2 ($)']

        if trigger_type == 'lower':
            long_exposures[ticker1] = long_exposures.get(ticker1, 0) + notional1
            short_exposures[ticker2] = short_exposures.get(ticker2, 0) + notional2
        else:
            short_exposures[ticker1] = short_exposures.get(ticker1, 0) + notional1
            long_exposures[ticker2] = long_exposures.get(ticker2, 0) + notional2

    return long_exposures, short_exposures


def check_ticker_concentration_constraint(ticker, notional, is_long,
                                          long_exposures, short_exposures,
                                          total_portfolio_value, current_portfolio_value=0):
    """
    Check if adding this position would violate ticker concentration limits.

    Uses relaxed limits during portfolio building phase.

    Returns
    -------
    tuple : (passes_constraint, reason_if_failed)
    """
    if total_portfolio_value <= 0:
        return True, None

    if current_portfolio_value < config.min_portfolio_value_for_strict_constraints():
        max_long = config.portfolio_building_max_long_concentration()
        max_short = config.portfolio_building_max_short_concentration()
    else:
        max_long = config.max_long_ticker_concentration()
        max_short = config.max_short_ticker_concentration()

    if is_long:
        current_exposure = long_exposures.get(ticker, 0)
        new_exposure = current_exposure + notional
        concentration = new_exposure / total_portfolio_value
        if concentration > max_long:
            return False, (f"Long {ticker} concentration {concentration:.2%} "
                           f"exceeds max {max_long:.2%}")
    else:
        current_exposure = short_exposures.get(ticker, 0)
        new_exposure = current_exposure + notional
        concentration = new_exposure / total_portfolio_value
        if concentration > max_short:
            return False, (f"Short {ticker} concentration {concentration:.2%} "
                           f"exceeds max {max_short:.2%}")

    return True, None


# ============================================================================
# IGV BETA EXPOSURE
# ============================================================================

def calculate_igv_exposure(portfolio_df, pending_trades=None):
    """
    Compute net IGV beta-weighted dollar exposure across the existing portfolio
    and any pending trades.

    Reads IGV betas from a configurable path (Excel file with Ticker and
    igv_beta columns). Used as the pre-trade exposure constraint.

    Parameters
    ----------
    portfolio_df : DataFrame
        Current portfolio (Co1, Co2, Tail, Trade Value Co1 ($), Trade Value Co2 ($))
    pending_trades : list of dict, optional
        Trades pending execution. Each dict:
        {co1, co2, tail ('L'/'U'), notional1, notional2}

    Returns
    -------
    dict
        net_igv_exposure  : float  (long_contrib - short_contrib, dollar)
        gross_exposure    : float  (total absolute notional)
        exposure_pct      : float  (net / gross)
        long_igv_contrib  : float
        short_igv_contrib : float
    """
    import pandas as pd

    igv_betas = {}
    igv_beta_file = config.get("factor_exposure.igv_beta_file")

    if igv_beta_file:
        try:
            beta_df = pd.read_excel(igv_beta_file)
            ticker_col = next(
                (c for c in beta_df.columns if c.lower() in ('ticker', 'symbol')),
                None,
            )
            beta_col = next(
                (c for c in beta_df.columns
                 if 'igv' in c.lower() and 'beta' in c.lower()),
                None,
            )
            if ticker_col and beta_col:
                igv_betas = dict(zip(beta_df[ticker_col], beta_df[beta_col]))
            else:
                logger.warning("IGV beta file found but could not identify Ticker/igv_beta columns")
        except Exception as e:
            logger.warning(f"Could not load IGV beta file: {e}")

    long_igv_contrib = 0.0
    short_igv_contrib = 0.0
    gross_exposure = 0.0

    def _accumulate(co1, co2, tail, notional1, notional2):
        nonlocal long_igv_contrib, short_igv_contrib, gross_exposure
        tail = str(tail).strip().upper()
        if tail == 'L':
            long_t, short_t = co1, co2
            long_v, short_v = abs(notional1), abs(notional2)
        else:
            long_t, short_t = co2, co1
            long_v, short_v = abs(notional2), abs(notional1)

        long_igv_contrib += igv_betas.get(long_t, 0) * long_v
        short_igv_contrib += igv_betas.get(short_t, 0) * short_v
        gross_exposure += long_v + short_v

    if portfolio_df is not None and not portfolio_df.empty:
        for _, row in portfolio_df.iterrows():
            _accumulate(
                row.get('Co1', ''), row.get('Co2', ''),
                row.get('Tail', 'L'),
                row.get('Trade Value Co1 ($)', 0),
                row.get('Trade Value Co2 ($)', 0),
            )

    if pending_trades:
        for trade in pending_trades:
            _accumulate(
                trade.get('co1', ''), trade.get('co2', ''),
                trade.get('tail', 'L'),
                trade.get('notional1', 0),
                trade.get('notional2', 0),
            )

    net_igv_exposure = long_igv_contrib - short_igv_contrib
    exposure_pct = net_igv_exposure / gross_exposure if gross_exposure > 0 else 0.0

    return {
        'net_igv_exposure': net_igv_exposure,
        'gross_exposure': gross_exposure,
        'exposure_pct': exposure_pct,
        'long_igv_contrib': long_igv_contrib,
        'short_igv_contrib': short_igv_contrib,
    }
