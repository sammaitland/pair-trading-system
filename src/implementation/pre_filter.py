"""
Pre-filter module for identifying candidate pairs for the Live Analytics Module.

Runs a single-stage filter with leniency at 2:30 PM ET. Loads calibrated parameters
and sub-sector indices, fetches live market data, and applies primary filters
(spread, earnings, alpha variance, deviation, trending, market cap, factor shock)
to produce a set of active pairs for downstream processing.

STATUS: live
"""

import pandas as pd
import numpy as np
from scipy.stats import norm
import os
import traceback
from ib_insync import IB
from datetime import datetime, timedelta, date

from src.shared import config

from src.shared.calculations import (
    apply_primary_filters_with_leniency_v92,
    align_historical_data_v92,
    calculate_pair_alphas_v92,
    get_subsector_manager,
    load_subsector_indices,
    get_v92_output_dir,
    assign_sum_dev_bucket,
    check_trending_stock_filter,
    get_alpha_cache,
    reset_alpha_cache,
    calculate_sum_deviation_cached,
    _beta_manager
)
from src.shared.fetch_market_data import (
    fetch_all_data,
)

from src.shared import calculations

from src.signals.factor_shock_interface import FactorShockDetector
from src.signals.reference_factor_shock import ReferenceFactorShockDetector

# ============================================================================
# VERSION-AWARE PATH CONFIGURATION
# ============================================================================

ACTIVE_VERSION = config.active_version()
VERSION_DIR = config.get_version_dir(ACTIVE_VERSION)
PARAMETERS_FILE = config.get_parameters_file(ACTIVE_VERSION)

# Shared implementation directory (pre-filter output goes here - it's transient/daily)
IMPLEMENTATION_DIR = config.implementation_dir()

# ============================================================================
# TICKER EXCLUSION SETS
# ============================================================================

CRYPTO_TICKERS = config.crypto_tickers()

MREIT_TICKERS = config.mreit_tickers()

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def calculate_sum_deviation_from_historical_v92(ticker1, ticker2, hist1, hist2,
                                                 subsector_manager,
                                                 earnings_dates=None):
    """
    Calculate 15-day sum deviation using single-factor model.

    Sum deviation = alpha1 + alpha2 (how PAIR deviates from index)
    NOT alpha1 - alpha2 (that's NET deviation for tail assignment)

    Key Difference: Each ticker uses its own sub-sector index.

    Note: SubSector Indices contain daily returns (not prices), so we use
    them directly instead of calling calculate_daily_return on them.
    """
    try:
        # Get sub-sector betas
        beta1_sub = subsector_manager.get_subsector_beta(ticker1)
        beta2_sub = subsector_manager.get_subsector_beta(ticker2)

        # Align data using V9.2 method (sub-sector indices)
        aligned = align_historical_data_v92(
            ticker1, ticker2, hist1, hist2, subsector_manager
        )

        if len(aligned) < 16:
            return np.nan

        # Calculate individual alpha sums and ADD them (not subtract!)
        alpha1_sum = 0.0
        alpha2_sum = 0.0

        for i in range(max(1, len(aligned) - 15), len(aligned)):
            # Stock returns - calculate from prices
            t1_ret = calculations.calculate_daily_return(aligned['ticker1'].iloc[i-1:i+1])
            t2_ret = calculations.calculate_daily_return(aligned['ticker2'].iloc[i-1:i+1])

            # Subsector returns - use directly (already returns, not prices!)
            idx1_ret = aligned['subsector1'].iloc[i]
            idx2_ret = aligned['subsector2'].iloc[i]

            # Single-factor alpha (no treasury)
            alpha1 = t1_ret - (beta1_sub * idx1_ret)
            alpha2 = t2_ret - (beta2_sub * idx2_ret)

            alpha1_sum += alpha1
            alpha2_sum += alpha2

        # SUM deviation = alpha1 + alpha2 (ADDITION!)
        return alpha1_sum + alpha2_sum

    except Exception as e:
        print(f"Error calculating sum deviation for {ticker1}/{ticker2}: {e}")
        return np.nan


def diagnose_sum_dev_distribution(df, stage_name, sum_dev_col='Sum_Deviation',
                                  cdf_col='Sum_Dev_Percentile', bucket_col='Sum_Dev_Bucket'):
    """
    Diagnostic function to check sum deviation distribution at any stage
    """
    print(f"\n{'='*80}")
    print(f"SUM DEVIATION DIAGNOSTIC - {stage_name}")
    print(f"{'='*80}")

    if sum_dev_col not in df.columns:
        print(f"Column '{sum_dev_col}' not found")
        print(f"   Available columns: {df.columns.tolist()}")
        return

    # 1. RAW VALUES
    print(f"\n1. RAW SUM DEVIATION VALUES:")
    print(f"   Total rows: {len(df)}")

    sum_devs = df[sum_dev_col].dropna()
    print(f"   Valid values: {len(sum_devs)}")

    if len(sum_devs) == 0:
        print("   No valid sum deviation values")
        return

    print(f"   Range: [{sum_devs.min():.6f}, {sum_devs.max():.6f}]")
    print(f"   Mean: {sum_devs.mean():.6f}")
    print(f"   Median: {sum_devs.median():.6f}")
    print(f"   Std Dev: {sum_devs.std():.6f}")

    # 2. CDF DISTRIBUTION
    if cdf_col in df.columns:
        print(f"\n2. CDF PERCENTILE DISTRIBUTION:")

        cdfs = df[cdf_col].dropna()
        print(f"   Valid CDFs: {len(cdfs)}")

        if len(cdfs) > 0:
            print(f"   CDF range: [{cdfs.min():.4f}, {cdfs.max():.4f}]")
            print(f"   CDF mean: {cdfs.mean():.4f}")
            print(f"   CDF median: {cdfs.median():.4f}")

            print(f"\n   CDF distribution (should be ~10% per decile):")
            for i in range(10):
                lower = i * 0.1
                upper = (i + 1) * 0.1
                count = ((cdfs >= lower) & (cdfs < upper)).sum()
                pct = count / len(cdfs) * 100
                print(f"     {lower*100:5.0f}-{upper*100:5.0f}%: {count:4d} ({pct:5.1f}%)")

    # 3. BUCKET DISTRIBUTION
    if bucket_col in df.columns:
        print(f"\n3. BUCKET DISTRIBUTION:")

        bucket_counts = df[bucket_col].value_counts().sort_index()

        if len(bucket_counts) == 0:
            print("   No buckets assigned")
        else:
            print(f"   Total with buckets: {bucket_counts.sum()}")

            expected_buckets = ['0-10%', '10-20%', '20-30%', '30-40%', '40-50%',
                              '50-60%', '60-70%', '70-80%', '80-90%', '90-100%']

            print(f"\n   Distribution:")
            for bucket in expected_buckets:
                count = bucket_counts.get(bucket, 0)
                pct = count / len(df) * 100 if len(df) > 0 else 0

                if count > 0:
                    status = "OK"
                elif 0 < count < len(df) * 0.02:
                    status = "LOW"
                else:
                    status = "MISSING"

                print(f"     {bucket:10s}: {count:4d} ({pct:5.1f}%) {status}")

    # 4. TAIL DISTRIBUTION
    if 'Tail' in df.columns:
        print(f"\n4. DISTRIBUTION BY TAIL:")

        for tail in ['L', 'U']:
            tail_df = df[df['Tail'] == tail]
            print(f"\n   {tail}-Tail: {len(tail_df)} pairs")

            if bucket_col in df.columns and len(tail_df) > 0:
                tail_buckets = tail_df[bucket_col].value_counts().sort_index()
                for bucket, count in tail_buckets.items():
                    print(f"     {bucket}: {count}")

    # 5. ACTIVE STATUS
    if 'Active' in df.columns:
        print(f"\n5. ACTIVE STATUS:")
        active = df[df['Active'] == 1]
        inactive = df[df['Active'] == 0]

        print(f"   Active: {len(active)} ({len(active)/len(df)*100:.1f}%)")
        print(f"   Inactive: {len(inactive)} ({len(inactive)/len(df)*100:.1f}%)")

        if len(inactive) > 0 and 'Reason' in df.columns:
            print(f"\n   Inactive reasons:")
            reasons = inactive['Reason'].value_counts()
            for reason, count in reasons.head(10).items():
                print(f"     {reason}: {count}")

    print(f"\n{'='*80}\n")


def diagnose_spread_calculation(ticker1, ticker2, market_data, max_spread):
    """Detailed spread calculation diagnostic"""
    print(f"\n{'='*80}")
    print(f"SPREAD DIAGNOSTIC: {ticker1}/{ticker2}")
    print(f"{'='*80}")

    data1 = market_data.get(ticker1, {})
    data2 = market_data.get(ticker2, {})

    bid1 = data1.get('bid')
    ask1 = data1.get('ask')
    mid1 = data1.get('mid')

    if mid1 is None and bid1 is not None and ask1 is not None:
        mid1 = (bid1 + ask1) / 2

    print(f"\n{ticker1}:")
    print(f"  Bid:    ${bid1:.2f}" if bid1 is not None else "  Bid:    MISSING")
    print(f"  Ask:    ${ask1:.2f}" if ask1 is not None else "  Ask:    MISSING")
    print(f"  Mid:    ${mid1:.2f}" if mid1 is not None else "  Mid:    MISSING")

    if bid1 is not None and ask1 is not None and mid1 is not None:
        spread1 = (ask1 - bid1) / mid1
        spread1_bps = spread1 * 10000
        print(f"  Spread: {spread1:.6f} ({spread1_bps:.1f} bps)")
    else:
        spread1 = None
        print(f"  Spread: CANNOT CALCULATE")

    bid2 = data2.get('bid')
    ask2 = data2.get('ask')
    mid2 = data2.get('mid')

    if mid2 is None and bid2 is not None and ask2 is not None:
        mid2 = (bid2 + ask2) / 2

    print(f"\n{ticker2}:")
    print(f"  Bid:    ${bid2:.2f}" if bid2 is not None else "  Bid:    MISSING")
    print(f"  Ask:    ${ask2:.2f}" if ask2 is not None else "  Ask:    MISSING")
    print(f"  Mid:    ${mid2:.2f}" if mid2 is not None else "  Mid:    MISSING")

    if bid2 is not None and ask2 is not None and mid2 is not None:
        spread2 = (ask2 - bid2) / mid2
        spread2_bps = spread2 * 10000
        print(f"  Spread: {spread2:.6f} ({spread2_bps:.1f} bps)")
    else:
        spread2 = None
        print(f"  Spread: CANNOT CALCULATE")

    print(f"\nPAIR SPREAD CALCULATION:")
    print(f"  Method: Equal weighting (50/50)")

    if spread1 is not None and spread2 is not None:
        pair_spread = (spread1 + spread2) / 2
        pair_spread_bps = pair_spread * 10000
        print(f"  Result:  {pair_spread:.6f} ({pair_spread_bps:.1f} bps)")

        hurdle_bps = max_spread * 10000
        print(f"\n  Hurdle:  {max_spread:.6f} ({hurdle_bps:.1f} bps)")

        if pair_spread <= max_spread:
            print(f"  Status:  PASS")
        else:
            excess = (pair_spread - max_spread) * 10000
            print(f"  Status:  FAIL (exceeds by {excess:.1f} bps)")
    else:
        print(f"  Result:  CANNOT CALCULATE")
        print(f"  Status:  FAIL (insufficient data)")

    print(f"{'='*80}\n")

def append_today_subsector_returns_prefilter(historical_data, subsector_manager):
    """
    Calculate and append today's subsector index returns from constituent stock prices.

    Call this AFTER fetching historical market data but BEFORE running filters.
    """
    today_naive = pd.Timestamp.now().normalize()

    # Group stocks by (ETF, Category)
    category_stocks = {}

    for ticker in historical_data.keys():
        category = subsector_manager.get_category(ticker)
        etf = subsector_manager.get_etf(ticker)

        if category and etf:
            key = (etf, category)
            if key not in category_stocks:
                category_stocks[key] = []
            category_stocks[key].append(ticker)

    updated_count = 0

    for (etf, category), tickers in category_stocks.items():
        returns = []

        for ticker in tickers:
            hist = historical_data.get(ticker)
            if hist is None or len(hist) < 2:
                continue

            today_price = hist['close'].iloc[-1]
            yesterday_price = hist['close'].iloc[-2]

            if yesterday_price and yesterday_price > 0 and today_price and not pd.isna(today_price):
                ret = (today_price - yesterday_price) / yesterday_price
                returns.append(ret)

        if not returns:
            continue

        today_return = np.mean(returns)

        # Update returns series
        if etf in subsector_manager._indices and category in subsector_manager._indices[etf]:
            returns_series = subsector_manager._indices[etf][category]

            if today_naive in returns_series.index:
                returns_series.loc[today_naive] = today_return
            else:
                new_row = pd.Series([today_return], index=[today_naive])
                subsector_manager._indices[etf][category] = pd.concat([returns_series, new_row])

            updated_count += 1

        # Update prices series
        if etf in subsector_manager._index_prices and category in subsector_manager._index_prices[etf]:
            prices_series = subsector_manager._index_prices[etf][category]

            if len(prices_series) > 0:
                yesterday_idx_price = prices_series.iloc[-1]
                today_idx_price = yesterday_idx_price * (1 + today_return)

                if today_naive in prices_series.index:
                    prices_series.loc[today_naive] = today_idx_price
                else:
                    new_row = pd.Series([today_idx_price], index=[today_naive])
                    subsector_manager._index_prices[etf][category] = pd.concat([prices_series, new_row])

    return updated_count

# ============================================================================
# MAIN PRE-FILTER
# ============================================================================

async def run_prefilter(ib):
    """
    Run single-stage pre-filter with leniency using single-factor model
    and sub-sector indices.
    """

    parameters_file = PARAMETERS_FILE
    implementation_dir = IMPLEMENTATION_DIR

    print("=" * 80)
    print(f"{ACTIVE_VERSION} PRE-FILTER - SINGLE-FACTOR MODEL WITH SUB-SECTOR INDICES")
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    # Tracking lists
    spread_failures = []
    trending_failures = []
    subsector_failures = []

    # Print configuration
    leniency = config.prefilter_leniency()
    print(f"\nLeniency Configuration:")
    print(f"  CDF adjustment: +/-{leniency['cdf_adjustment']:.2f}")
    print(f"  2-day reduction: {leniency['two_day_reduction']:.0%}")
    print(f"  Sum dev exclusion: {leniency['sum_dev_neutral_zone'][0]:.0%}-{leniency['sum_dev_neutral_zone'][1]:.0%}")
    print(f"\n{ACTIVE_VERSION} Model:")
    print(f"  Single-factor (no treasury)")
    print(f"  Sub-sector indices per category")
    print(f"  Parameters: {parameters_file}")
    print(f"  Output: {implementation_dir}")

    # [1/7] Load Parameters
    print(f"\n[1/7] Loading {ACTIVE_VERSION} parameters...")
    params_df = pd.read_excel(parameters_file, sheet_name='Pairs')
    tickers_df = pd.read_excel(parameters_file, sheet_name='Tickers')
    cumulative_stats_df = pd.read_excel(parameters_file, sheet_name='15Day_Cumulative_Stats')
    params_df['Tag'] = params_df['Tag'].astype(str)
    print(f"   Loaded {len(params_df)} pairs")

    # [2/7] Load Sub-Sector Indices from Beta Calibration
    print(f"\n[2/7] Loading {ACTIVE_VERSION} sub-sector indices...")
    subsector_manager = get_subsector_manager()

    # Get unique indices from pairs
    all_indices = list(params_df['Index'].unique())
    print(f"   Loading indices for: {', '.join(all_indices)}")

    loaded_count = 0
    for etf in all_indices:
        if subsector_manager.load_from_beta_output(etf, VERSION_DIR):
            loaded_count += 1
            categories = subsector_manager.get_categories_for_etf(etf)
            print(f"   {etf}: {len(categories)} categories - {', '.join(categories)}")
        else:
            print(f"   {etf}: Failed to load sub-sector indices")

    if loaded_count == 0:
        raise ValueError(f"No sub-sector indices loaded - run beta calibration for {ACTIVE_VERSION} first")

    print(f"   Loaded sub-sector data for {loaded_count}/{len(all_indices)} ETFs")

    print("\n[NEW] Resetting alpha cache...")
    alpha_cache = reset_alpha_cache()

    # [3/7] Fetch Historical Market Data
    print("\n[3/7] Fetching historical market data...")
    all_tickers = list(set(params_df['Co1'].unique()) | set(params_df['Co2'].unique()))
    tickers_to_fetch = all_tickers + all_indices
    print(f"   Fetching data for {len(tickers_to_fetch)} tickers...")

    # Note: We still fetch index data for trending filter (uses raw index)
    treasury_earliest = datetime.now() - timedelta(days=365)
    treasury_latest = datetime.now()

    market_data = await fetch_all_data(
        tickers_to_fetch, ib,
        treasury_earliest, treasury_latest,
        index_tickers=all_indices
    )

    # Market data quality check
    print("\n" + "="*80)
    print("MARKET DATA QUALITY CHECK")
    print("="*80)

    stock_tickers = list(set(params_df['Co1'].unique()) | set(params_df['Co2'].unique()))

    has_full_data = 0
    has_bid_ask_only = 0
    missing_all = 0
    missing_tickers = []

    for ticker in stock_tickers:
        data = market_data.get(ticker, {})
        bid = data.get('bid')
        ask = data.get('ask')
        spread = data.get('spread')

        if spread is not None and not pd.isna(spread):
            has_full_data += 1
        elif bid is not None and ask is not None and not pd.isna(bid) and not pd.isna(ask):
            has_bid_ask_only += 1
        else:
            missing_all += 1
            if len(missing_tickers) < 30:
                missing_tickers.append(ticker)

    print(f"Total stock tickers: {len(stock_tickers)}")
    print(f"  Has spread calculated: {has_full_data}")
    print(f"  Has bid/ask (can calculate spread): {has_bid_ask_only}")
    print(f"  Missing bid/ask entirely: {missing_all}")
    print(f"\nUsable for spread filter: {(has_full_data + has_bid_ask_only) / len(stock_tickers) * 100:.1f}%")

    if missing_all > 0 and len(missing_tickers) > 0:
        print(f"\nFirst {min(10, len(missing_tickers))} tickers missing bid/ask:")
        for t in missing_tickers[:10]:
            data = market_data.get(t, {})
            print(f"  {t}: live_price={data.get('live_price')}, bid={data.get('bid')}, ask={data.get('ask')}")

    print("="*80 + "\n")

    # Extract historical data
    historical_data = {}
    for ticker, data in market_data.items():
        if 'historical_data' in data and not data['historical_data'].empty:
            historical_data[ticker] = data['historical_data']

    print(f"   Historical data ready for {len(historical_data)} tickers")

    # =========================================================================
    # CRITICAL: Append today's subsector returns from live constituent prices
    # Without this, align_historical_data_v92() excludes today from intersection
    # =========================================================================
    updated_categories = append_today_subsector_returns_prefilter(historical_data, subsector_manager)
    print(f"   Subsector indices updated: {updated_categories} categories include today's returns")

    # ADD: Populate alpha cache (MUST be after appending today's data!)
    print("\n[NEW] Populating alpha cache...")
    all_stock_tickers = list(set(params_df['Co1'].unique()) | set(params_df['Co2'].unique()))
    alpha_cache.populate(all_stock_tickers, historical_data, subsector_manager)

    # [4/7] Load Earnings Calendar
    print("\n[4/7] Loading earnings calendar...")
    earnings_file = config.earnings_calendar_file()
    if os.path.exists(earnings_file):
        earnings_df = pd.read_excel(earnings_file, sheet_name='Earnings Calendar')
        earnings_df['ticker'] = earnings_df['ticker'].str.upper()
        earnings_df['reportDate'] = pd.to_datetime(earnings_df['reportDate']).dt.date

        print(f"   Raw calendar: {len(earnings_df)} rows, {earnings_df['ticker'].nunique()} unique tickers")

        today_date = date.today()
        lookback_date = today_date - timedelta(days=config.earnings_exclusion_days_behind())
        future_window = today_date + timedelta(days=30)

        earnings_df = earnings_df[
            (earnings_df['reportDate'] >= lookback_date) &
            (earnings_df['reportDate'] <= future_window)
        ]
        print(f"   After date filter: {len(earnings_df)} rows")

        earnings_df = earnings_df.sort_values('reportDate')
        earnings_df = earnings_df.drop_duplicates(subset=['ticker'], keep='first')
        print(f"   After deduplication: {len(earnings_df)} tickers")

        earnings_dates = {}
        for _, row in earnings_df.iterrows():
            ticker = row['ticker']
            report_date = row['reportDate']
            report_time = row.get('reportTime', 'pre-market')

            if pd.notna(report_time) and 'post' in str(report_time).lower():
                effective_date = report_date + timedelta(days=1)
                while effective_date.weekday() >= 5:
                    effective_date += timedelta(days=1)
                earnings_dates[ticker] = effective_date
            else:
                earnings_dates[ticker] = report_date

        print(f"   Loaded {len(earnings_dates)} earnings dates")

        in_window = sum(
            1 for d in earnings_dates.values()
            if -config.earnings_exclusion_days_behind() <= (d - today_date).days <= config.earnings_exclusion_days_ahead()
        )
        print(f"   Earnings in exclusion window: {in_window} tickers")

    else:
        print(f"   WARNING: Earnings calendar not found")
        earnings_dates = {}

    # [5/7] Prepare Parameter Dictionaries (single-factor model)
    print(f"\n[5/7] Preparing {ACTIVE_VERSION} parameter lookups...")

    # Only need cumulative stats and EMA multipliers
    # Sub-sector betas come from SubsectorIndexManager
    params_dict = {
        'cumulative_stats_df': cumulative_stats_df,
        'alpha_variance_hurdles': params_df.set_index('Tag')['CDF_Threshold'].to_dict(),
        'ema_multipliers': params_df.set_index('Tag')['EMA_Multiplier'].to_dict(),
    }

    # Global sum dev std for CDF calculation
    sum_dev_params = pd.read_excel(parameters_file, sheet_name='Sum_Deviation_Params')
    global_sum_dev_std = float(sum_dev_params[
        sum_dev_params['Parameter'] == 'Sum Deviation StdDev'
    ]['Value'].iloc[0])
    print(f"   Global sum dev std: {global_sum_dev_std:.6f}")

    # Factor shock suppression -- run once before pair loop.
    # Pairs with action='SUPPRESS' in the returned at_risk dict are excluded.
    at_risk = {}
    try:
        _, at_risk = ReferenceFactorShockDetector().get_live_status()
        suppressed_count_total = sum(
            len(df[df['action'] == 'SUPPRESS']) for df in at_risk.values()
            if not df.empty
        )
        if suppressed_count_total:
            print(f"\n   Factor shock: {suppressed_count_total} pair(s) flagged for suppression")
        else:
            print(f"\n   Factor shock: no suppressions active")
    except Exception as e:
        print(f"\n   Factor shock detection unavailable: {e} -- continuing without suppression")
        at_risk = {}

    # [6/7] Process All Pairs
    print(f"\n[6/7] Processing pairs with {ACTIVE_VERSION} single-factor model...")
    results = []
    passed_primary = 0

    # Market cap filter
    mcap_cache = {}
    if config.enable_mcap_filter():
        from src.implementation.daily_data_capture import load_market_caps

        print(f"   Loading cached market caps from {config.closing_prices_file()}...")
        mcap_cache = load_market_caps(config.closing_prices_file())

        if not mcap_cache:
            print(f"   WARNING: No cached market caps found!")
        else:
            print(f"   Loaded {len(mcap_cache)} cached market caps")

    # Load delisted tickers
    delisted_tickers = set()
    if os.path.exists(config.delisted_tickers_file()):
        try:
            import json
            with open(config.delisted_tickers_file(), 'r') as f:
                data = json.load(f)
            # Support both legacy (plain list) and new format (dict with 'tickers' key)
            if isinstance(data, list):
                delisted_tickers = set(data)
            else:
                delisted_tickers = set(data.get('tickers', []))
            print(f"   Loaded {len(delisted_tickers)} delisted tickers for exclusion")
        except Exception as e:
            print(f"   Could not load delisted tickers: {e}")

    # Load alpha data for T-stat filter (if not skipping)
    alpha_data_by_index = {}
    if not leniency.get('tstat_filter', {}).get('skip', True):
        print("   Loading alpha data for T-stat filter...")
        # Alpha data is version-specific
        alpha_data_dir = config.get_alpha_data_dir(ACTIVE_VERSION)
        for index_ticker in all_indices:
            alpha_file = os.path.join(alpha_data_dir, f'{index_ticker}_alpha_data.pkl')
            if os.path.exists(alpha_file):
                alpha_data_by_index[index_ticker] = pd.read_pickle(alpha_file)
                print(f"     {index_ticker}: {len(alpha_data_by_index[index_ticker].columns)} stocks")
            else:
                print(f"     {index_ticker}: No alpha data file found")

    for idx, pair_row in params_df.iterrows():
        if (idx + 1) % 100 == 0:
            print(f"   Progress: {idx + 1}/{len(params_df)} pairs")

        tag = str(pair_row['Tag'])
        ticker1 = pair_row['Co1']
        ticker2 = pair_row['Co2']
        index_ticker = pair_row['Index']
        tail = pair_row['Tail']

        hist1 = historical_data.get(ticker1)
        hist2 = historical_data.get(ticker2)
        index_data = historical_data.get(index_ticker)

        # Initialize defaults
        sum_dev = None
        sum_dev_pct = None
        sum_dev_bucket = None

        # Check historical data availability
        if any(d is None for d in [hist1, hist2, index_data]):
            results.append({
                'Tag': tag, 'Pair': pair_row['Pair'], 'Co1': ticker1, 'Co2': ticker2,
                'Tail': tail, 'Index': index_ticker, 'Active': 0,
                'Reason': 'Missing historical data',
                'Sum_Deviation': sum_dev,
                'Sum_Dev_Percentile': sum_dev_pct,
                'Sum_Dev_Bucket': sum_dev_bucket,
                'Model_Version': ACTIVE_VERSION
            })
            continue

        # Check sub-sector data availability
        cat1 = subsector_manager.get_category(ticker1)
        cat2 = subsector_manager.get_category(ticker2)

        if cat1 is None:
            results.append({
                'Tag': tag, 'Pair': pair_row['Pair'], 'Co1': ticker1, 'Co2': ticker2,
                'Tail': tail, 'Index': index_ticker, 'Active': 0,
                'Reason': f'No {ACTIVE_VERSION} category for {ticker1}',
                'Sum_Deviation': None, 'Sum_Dev_Percentile': None, 'Sum_Dev_Bucket': None,
                'Model_Version': ACTIVE_VERSION
            })
            subsector_failures.append(ticker1)
            continue

        if cat2 is None:
            results.append({
                'Tag': tag, 'Pair': pair_row['Pair'], 'Co1': ticker1, 'Co2': ticker2,
                'Tail': tail, 'Index': index_ticker, 'Active': 0,
                'Reason': f'No {ACTIVE_VERSION} category for {ticker2}',
                'Sum_Deviation': None, 'Sum_Dev_Percentile': None, 'Sum_Dev_Bucket': None,
                'Model_Version': ACTIVE_VERSION
            })
            subsector_failures.append(ticker2)
            continue

        # Market cap filter
        if config.enable_mcap_filter():
            mcap1 = mcap_cache.get(ticker1)
            mcap2 = mcap_cache.get(ticker2)

            if mcap1 is None:
                results.append({
                    'Tag': tag, 'Pair': pair_row['Pair'], 'Co1': ticker1, 'Co2': ticker2,
                    'Tail': tail, 'Index': index_ticker, 'Active': 0,
                    'Reason': f'MCAP unavailable: {ticker1}',
                    'Sum_Deviation': None, 'Sum_Dev_Percentile': None, 'Sum_Dev_Bucket': None,
                    'Model_Version': ACTIVE_VERSION
                })
                continue

            if mcap2 is None:
                results.append({
                    'Tag': tag, 'Pair': pair_row['Pair'], 'Co1': ticker1, 'Co2': ticker2,
                    'Tail': tail, 'Index': index_ticker, 'Active': 0,
                    'Reason': f'MCAP unavailable: {ticker2}',
                    'Sum_Deviation': None, 'Sum_Dev_Percentile': None, 'Sum_Dev_Bucket': None,
                    'Model_Version': ACTIVE_VERSION
                })
                continue

            min_mcap = config.min_market_cap_millions()
            if mcap1 < min_mcap:
                results.append({
                    'Tag': tag, 'Pair': pair_row['Pair'], 'Co1': ticker1, 'Co2': ticker2,
                    'Tail': tail, 'Index': index_ticker, 'Active': 0,
                    'Reason': f'{ticker1} MCAP ${mcap1:.0f}M < ${min_mcap}M',
                    'Sum_Deviation': None, 'Sum_Dev_Percentile': None, 'Sum_Dev_Bucket': None,
                    'Model_Version': ACTIVE_VERSION
                })
                continue

            if mcap2 < min_mcap:
                results.append({
                    'Tag': tag, 'Pair': pair_row['Pair'], 'Co1': ticker1, 'Co2': ticker2,
                    'Tail': tail, 'Index': index_ticker, 'Active': 0,
                    'Reason': f'{ticker2} MCAP ${mcap2:.0f}M < ${min_mcap}M',
                    'Sum_Deviation': None, 'Sum_Dev_Percentile': None, 'Sum_Dev_Bucket': None,
                    'Model_Version': ACTIVE_VERSION
                })
                continue

        # Delisted ticker filter
        if delisted_tickers and ticker1 in delisted_tickers:
            results.append({
                'Tag': tag, 'Pair': pair_row['Pair'], 'Co1': ticker1, 'Co2': ticker2,
                'Tail': tail, 'Index': index_ticker, 'Active': 0,
                'Reason': f'Delisted ticker: {ticker1}',
                'Sum_Deviation': None, 'Sum_Dev_Percentile': None, 'Sum_Dev_Bucket': None,
                'Model_Version': ACTIVE_VERSION
            })
            continue

        if delisted_tickers and ticker2 in delisted_tickers:
            results.append({
                'Tag': tag, 'Pair': pair_row['Pair'], 'Co1': ticker1, 'Co2': ticker2,
                'Tail': tail, 'Index': index_ticker, 'Active': 0,
                'Reason': f'Delisted ticker: {ticker2}',
                'Sum_Deviation': None, 'Sum_Dev_Percentile': None, 'Sum_Dev_Bucket': None,
                'Model_Version': ACTIVE_VERSION
            })
            continue

        # Crypto ticker filter
        if ticker1 in CRYPTO_TICKERS:
            results.append({
                'Tag': tag, 'Pair': pair_row['Pair'], 'Co1': ticker1, 'Co2': ticker2,
                'Tail': tail, 'Index': index_ticker, 'Active': 0,
                'Reason': f'Crypto ticker excluded: {ticker1}',
                'Sum_Deviation': None, 'Sum_Dev_Percentile': None, 'Sum_Dev_Bucket': None,
                'Model_Version': ACTIVE_VERSION
            })
            continue

        if ticker2 in CRYPTO_TICKERS:
            results.append({
                'Tag': tag, 'Pair': pair_row['Pair'], 'Co1': ticker1, 'Co2': ticker2,
                'Tail': tail, 'Index': index_ticker, 'Active': 0,
                'Reason': f'Crypto ticker excluded: {ticker2}',
                'Sum_Deviation': None, 'Sum_Dev_Percentile': None, 'Sum_Dev_Bucket': None,
                'Model_Version': ACTIVE_VERSION
            })
            continue

        # mREIT ticker filter
        if ticker1 in MREIT_TICKERS:
            results.append({
                'Tag': tag, 'Pair': pair_row['Pair'], 'Co1': ticker1, 'Co2': ticker2,
                'Tail': tail, 'Index': index_ticker, 'Active': 0,
                'Reason': f'mREIT ticker excluded: {ticker1}',
                'Sum_Deviation': None, 'Sum_Dev_Percentile': None, 'Sum_Dev_Bucket': None,
                'Model_Version': ACTIVE_VERSION
            })
            continue

        if ticker2 in MREIT_TICKERS:
            results.append({
                'Tag': tag, 'Pair': pair_row['Pair'], 'Co1': ticker1, 'Co2': ticker2,
                'Tail': tail, 'Index': index_ticker, 'Active': 0,
                'Reason': f'mREIT ticker excluded: {ticker2}',
                'Sum_Deviation': None, 'Sum_Dev_Percentile': None, 'Sum_Dev_Bucket': None,
                'Model_Version': ACTIVE_VERSION
            })
            continue

        # Apply primary filters with single-factor model
        # Get alpha data for this index (for T-stat filter)
        alpha_data = alpha_data_by_index.get(index_ticker)

        passed, filter_results = apply_primary_filters_with_leniency_v92(
            ticker1, ticker2, tag, tail, hist1, hist2,
            market_data, params_dict, earnings_dates,
            leniency, max_spread=config.prefilter_max_spread_decimal(),
            subsector_manager=subsector_manager,
            alpha_data=alpha_data
        )

        if passed:
            passed_primary += 1

            # Trending filter (still uses raw index data, not sub-sector)
            if config.enable_trending_filter():
                ticker1_passed, ticker1_result = check_trending_stock_filter(
                    ticker1, hist1, index_data, index_ticker,
                    config.trend_positive_threshold(), config.trend_positive_lookback_months(),
                    config.trend_negative_threshold(), config.trend_negative_lookback_months()
                )

                if not ticker1_passed:
                    active = 0
                    reason = f"Trending filter: {ticker1} - {ticker1_result['failed_reason']}"
                    trending_failures.append((ticker1, ticker1_result['failed_reason']))

                    results.append({
                        'Tag': tag, 'Pair': pair_row['Pair'], 'Co1': ticker1, 'Co2': ticker2,
                        'Tail': tail, 'Index': index_ticker, 'Active': active, 'Reason': reason,
                        'Sum_Deviation': None, 'Sum_Dev_Percentile': None, 'Sum_Dev_Bucket': None,
                        'Model_Version': ACTIVE_VERSION
                    })
                    continue

                ticker2_passed, ticker2_result = check_trending_stock_filter(
                    ticker2, hist2, index_data, index_ticker,
                    config.trend_positive_threshold(), config.trend_positive_lookback_months(),
                    config.trend_negative_threshold(), config.trend_negative_lookback_months()
                )

                if not ticker2_passed:
                    active = 0
                    reason = f"Trending filter: {ticker2} - {ticker2_result['failed_reason']}"
                    trending_failures.append((ticker2, ticker2_result['failed_reason']))

                    results.append({
                        'Tag': tag, 'Pair': pair_row['Pair'], 'Co1': ticker1, 'Co2': ticker2,
                        'Tail': tail, 'Index': index_ticker, 'Active': active, 'Reason': reason,
                        'Sum_Deviation': None, 'Sum_Dev_Percentile': None, 'Sum_Dev_Bucket': None,
                        'Model_Version': ACTIVE_VERSION
                    })
                    continue

            # Calculate sum deviation using single-factor model
            sum_dev, sum_dev_pct, sum_dev_bucket = calculate_sum_deviation_cached(
                ticker1, ticker2, alpha_cache, global_sum_dev_std
            )

            if not np.isnan(sum_dev):
                sum_dev_pct = norm.cdf(sum_dev, loc=0, scale=global_sum_dev_std)
                sum_dev_bucket = assign_sum_dev_bucket(sum_dev_pct * 100)

                neutral_low, neutral_high = leniency['sum_dev_neutral_zone']

                if neutral_low <= sum_dev_pct <= neutral_high:
                    active = 0
                    reason = f"Excluded by sum deviation (neutral: {sum_dev_pct:.1%}, bucket: {sum_dev_bucket})"
                else:
                    active = 1
                    reason = f"Passed all {ACTIVE_VERSION} filters with leniency"

                    # Factor shock suppression -- applied only to pairs that passed all other filters
                    if at_risk:
                        ar_df = at_risk.get(index_ticker)
                        if ar_df is not None and not ar_df.empty:
                            match = ar_df[
                                (ar_df['co1'] == ticker1) & (ar_df['co2'] == ticker2) &
                                (ar_df['action'] == 'SUPPRESS')
                            ]
                            if not match.empty:
                                active = 0
                                row0 = match.iloc[0]
                                reason = (
                                    f"Factor shock suppression: {row0['factor']} "
                                    f"z={row0['factor_z']:.2f}"
                                )
            else:
                active = 0
                reason = f"{ACTIVE_VERSION} sum deviation calculation failed"
                sum_dev_pct = None
                sum_dev_bucket = None
        else:
            active = 0
            sum_dev = None
            sum_dev_pct = None
            sum_dev_bucket = None

            failed_filter = None
            for filter_name, result in filter_results.items():
                if isinstance(result, dict) and not result.get('passed', False):
                    failed_filter = filter_name
                    break
            reason = f"Failed {failed_filter} with leniency" if failed_filter else "Failed filters"

            if failed_filter == 'spread':
                spread_failures.append((ticker1, ticker2))

        results.append({
            'Tag': tag, 'Pair': pair_row['Pair'], 'Co1': ticker1, 'Co2': ticker2,
            'Tail': tail, 'Index': index_ticker, 'Active': active, 'Reason': reason,
            'Sum_Deviation': sum_dev,
            'Sum_Dev_Percentile': sum_dev_pct,
            'Sum_Dev_Bucket': sum_dev_bucket,
            'Category1': cat1,
            'Category2': cat2,
            'Model_Version': ACTIVE_VERSION
        })

    # [7/7] Save Results
    print(f"\n[7/7] Saving {ACTIVE_VERSION} results...")
    results_df = pd.DataFrame(results)

    # Diagnostic
    diagnose_sum_dev_distribution(
        results_df,
        f"{ACTIVE_VERSION} PRE-FILTER OUTPUT",
        sum_dev_col='Sum_Deviation',
        cdf_col='Sum_Dev_Percentile',
        bucket_col='Sum_Dev_Bucket'
    )

    total_pairs = len(results_df)
    active_pairs = (results_df['Active'] == 1).sum()
    excluded_sum_dev = passed_primary - active_pairs

    print(f"\n   {ACTIVE_VERSION} SUMMARY:")
    print(f"   Total pairs: {total_pairs}")
    print(f"   Passed primary (lenient): {passed_primary} ({passed_primary/total_pairs*100:.1f}%)")
    print(f"   Excluded by sum deviation: {excluded_sum_dev}")
    print(f"   Active for LAM: {active_pairs} ({active_pairs/total_pairs*100:.1f}%)")

    l_tail_active = results_df[(results_df['Tail'] == 'L') & (results_df['Active'] == 1)]
    u_tail_active = results_df[(results_df['Tail'] == 'U') & (results_df['Active'] == 1)]
    print(f"\n   By tail: L={len(l_tail_active)}, U={len(u_tail_active)}")

    if (results_df['Active'] == 0).any():
        print(f"\n   REJECTIONS:")
        rejection_counts = results_df[results_df['Active'] == 0]['Reason'].str.extract(r'^([^:]+)')[0].value_counts()
        for reason, count in rejection_counts.head(15).items():
            print(f"     {reason}: {count}")

    # Sub-sector failures summary
    if len(subsector_failures) > 0:
        unique_failures = set(subsector_failures)
        print(f"\n   {ACTIVE_VERSION} SUBSECTOR FAILURES: {len(unique_failures)} unique tickers")
        print(f"     (Run beta calibration for these ETFs)")

    # Spread failures summary
    if len(spread_failures) > 0:
        print(f"\n   SPREAD FILTER FAILURES: {len(spread_failures)} pairs")
        print(f"     Sample failures (first 10):")
        for ticker1, ticker2 in spread_failures[:10]:
            data1 = market_data.get(ticker1, {})
            data2 = market_data.get(ticker2, {})
            bid1, ask1 = data1.get('bid'), data1.get('ask')
            bid2, ask2 = data2.get('bid'), data2.get('ask')
            status1 = "OK" if (bid1 and ask1) else "MISSING"
            status2 = "OK" if (bid2 and ask2) else "MISSING"
            print(f"       {ticker1}/{ticker2}: {ticker1} {status1}, {ticker2} {status2}")

    # Trending filter summary
    if config.enable_trending_filter() and len(trending_failures) > 0:
        print(f"\n   TRENDING FILTER EXCLUSIONS: {len(trending_failures)} tickers")

        from collections import Counter
        failure_types = Counter([reason for _, reason in trending_failures])

        print(f"     Positive trending: {sum(1 for _, r in trending_failures if 'Positive' in r)}")
        print(f"     Negative trending: {sum(1 for _, r in trending_failures if 'Negative' in r)}")
        print(f"     Sample failures (first 10):")
        for ticker, reason in trending_failures[:10]:
            print(f"       {ticker}: {reason}")

    # Save to Excel in shared Implementation directory
    output_file = os.path.join(implementation_dir, 'trade_prefilter_active.xlsx')
    with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
        results_df.to_excel(writer, sheet_name='Active_Pairs', index=False)

        summary_data = pd.DataFrame({
            'Metric': ['Version', 'Total Pairs', 'Passed Primary (Lenient)', 'Excluded by Sum Dev',
                      'Active for LAM', 'L-Tail Active', 'U-Tail Active',
                      'Model', 'Treasury Factor', 'Parameters File'],
            'Value': [ACTIVE_VERSION, total_pairs, passed_primary, excluded_sum_dev, active_pairs,
                     len(l_tail_active), len(u_tail_active),
                     f'{ACTIVE_VERSION} Single-Factor', 'Removed', parameters_file],
        })
        summary_data.to_excel(writer, sheet_name='Summary', index=False)

        failure_reasons = results_df[results_df['Active'] == 0]['Reason'].value_counts()
        pd.DataFrame({'Reason': failure_reasons.index, 'Count': failure_reasons.values}).to_excel(
            writer, sheet_name='Failure_Reasons', index=False
        )

    print(f"\n   Saved to: {output_file}")
    print("\n" + "=" * 80)
    print(f"{ACTIVE_VERSION} PRE-FILTER COMPLETE")
    print("=" * 80)

    return results_df


# ============================================================================
# ENTRY POINT
# ============================================================================

async def main():
    """Connect to IBKR and run the pre-filter pipeline."""

    ib, connected = await config.connect_ib_async()

    if connected:
        try:
            results = await run_prefilter(ib)
            print(f"\n{(results['Active']==1).sum()} active pairs ready for LAM ({ACTIVE_VERSION})")
        finally:
            config.disconnect_ib(ib)
    else:
        print("Failed to connect to IBKR")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
