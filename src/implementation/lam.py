"""
Live Analytics Module (LAM) for processing pre-filtered pairs through strict
primary filters, secondary signal scoring, and retention filtering.

Integrates with the pre-filter to analyze only active trades. Uses a single-factor
alpha model with sub-sector indices, appends live intraday prices to historical
data before filtering, and applies canonical composite scoring from the shared
calculations toolbox.

STATUS: live
"""

from src.shared import config
import asyncio
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, date
import pytz
from scipy.stats import norm
import os
import traceback
import pickle
from ib_insync import IB
from src.shared.fetch_market_data import (
    fetch_live_prices_batch,
    fetch_all_data,
    load_from_cache,
    HISTORICAL_CACHE_FILE
)

from src.shared import calculations

from src.shared.calculations import (
    check_15day_alpha_variance_v92,
    check_2day_deviation_v92,
    align_historical_data_v92,
    calculate_net_alpha_series_v92,
    get_subsector_manager,
    load_subsector_indices,
    get_v92_output_dir,
    assign_sum_dev_bucket,
    # Legacy functions still needed
    check_same_direction,
    check_nominal_direction,
    check_trend_filter,
    check_spread_hurdle,
    check_earnings_filter,
    calculate_daily_return,
    is_earnings_date,
    has_upcoming_earnings,
    get_alpha_cache,
    reset_alpha_cache,
    _beta_manager,
    # Canonical scoring functions (shared with optimizer)
    PERCENTILE_BANDS,
    STABILITY_WEIGHTS,
    MAX_BAND_POINTS,
    get_band_points,
    value_to_percentile,
    calculate_composite_score,
    apply_retention_filter,
    apply_retention_filter_by_tail,
    get_scoring_summary
)

from src.shared.config_helper import get_index_bias

# ============================================================================
# OUTPUT DIRECTORY
# ============================================================================

ACTIVE_VERSION = config.active_version()
VERSION_DIR = config.get_version_dir(ACTIVE_VERSION)
PARAMETERS_FILE = config.get_parameters_file(ACTIVE_VERSION)

# Shared secondaries cache (version-independent)
SECONDARIES_CACHE_DIR = os.path.join(config.v9_base_dir(), "Master Implementation", "secondaries_cache")

def diagnose_sum_dev_distribution(df, stage_name, sum_dev_col='Sum_Deviation',
                                  cdf_col='Sum_Dev_Percentile', bucket_col='Sum_Dev_Bucket'):
    """Diagnostic function to check sum deviation distribution at any stage"""
    print(f"\n{'='*80}")
    print(f"SUM DEVIATION DIAGNOSTIC - {stage_name}")
    print(f"{'='*80}")

    if sum_dev_col not in df.columns:
        print(f"Column '{sum_dev_col}' not found")
        print(f"   Available columns: {df.columns.tolist()}")
        return

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

    if cdf_col in df.columns:
        print(f"\n2. CDF PERCENTILE DISTRIBUTION:")
        cdfs = df[cdf_col].dropna()
        print(f"   Valid CDFs: {len(cdfs)}")

        if len(cdfs) > 0:
            print(f"   CDF range: [{cdfs.min():.4f}, {cdfs.max():.4f}]")
            print(f"\n   CDF distribution (should be ~10% per decile):")
            for i in range(10):
                lower = i * 0.1
                upper = (i + 1) * 0.1
                count = ((cdfs >= lower) & (cdfs < upper)).sum()
                pct = count / len(cdfs) * 100
                print(f"     {lower*100:5.0f}-{upper*100:5.0f}%: {count:4d} ({pct:5.1f}%)")

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

    if 'Tail' in df.columns:
        print(f"\n4. DISTRIBUTION BY TAIL:")
        for tail in ['L', 'U']:
            tail_df = df[df['Tail'] == tail]
            print(f"\n   {tail}-Tail: {len(tail_df)} pairs")

            if bucket_col in df.columns and len(tail_df) > 0:
                tail_buckets = tail_df[bucket_col].value_counts().sort_index()
                for bucket, count in tail_buckets.items():
                    print(f"     {bucket}: {count}")

    if 'Active' in df.columns:
        print(f"\n5. ACTIVE STATUS:")
        active = df[df['Active'] == 1]
        inactive = df[df['Active'] == 0]
        print(f"   Active: {len(active)} ({len(active)/len(df)*100:.1f}%)")
        print(f"   Inactive: {len(inactive)} ({len(inactive)/len(df)*100:.1f}%)")

    print(f"\n{'='*80}\n")


class LiveAnalytics:
    """Live Analytics with single-factor model and sub-sector indices"""

    def __init__(self, parameters_file_path=None, prefilter_file=None):
        """Initialize with pre-filter results"""
        self.parameters_file = parameters_file_path
        self.prefilter_file = prefilter_file
        self.model_version = ACTIVE_VERSION

        # Load configuration
        self.load_parameters()

        # Initialize SubsectorIndexManager
        self.subsector_manager = get_subsector_manager()

        # Load pre-filter if provided
        if prefilter_file:
            self.load_prefilter_results()
        else:
            self.active_trades = None

        # Secondary signal weights (from optimizer)
        self.secondary_signal_weights = {
            'lower': {
                'volume_dominance': 0.3,
                'rolling_intraday_vol': 0.75,
                'true_last_hour_volatility': 0.85
            },
            'upper': {
                'volume_ratio': 0.4,
                'rolling_intraday_vol': 0.75,
                'iv_percentile': 0.7
            }
        }

        # Load optimizer outputs
        self.optimizer_config = self._load_optimizer_outputs()

        # Load historical percentile distributions
        self.historical_percentiles = self._load_historical_percentiles()

        # NOTE: Percentile bands and stability weights are now imported from calculations
        # to ensure consistency with optimizer scoring

        # Data containers
        self.market_data = {}
        self.historical_data = {}
        self.earnings_dates = {}
        self.volume_data_cache = {}
        self.index_data = {}

        # Track live price appending for diagnostics
        self.live_prices_appended = False
        self.live_price_append_count = 0

        # Results containers
        self.primary_filter_results = []
        self.sum_deviation_results = []
        self.secondary_signal_results = []

        # Alpha cache (will be populated after fetching data)
        self.alpha_cache = None

        # Initialize IB connection
        self.ib = None

    def _load_optimizer_outputs(self):
        """Load secondary filter configuration from config"""
        print(f"Loading secondary filter configuration from config")
        secondary_config = config.secondary_filter_config()
        print(f"  Lower: {secondary_config['lower']['filters']} (retention: {secondary_config['lower']['retention_rate']:.1%})")
        print(f"  Upper: {secondary_config['upper']['filters']} (retention: {secondary_config['upper']['retention_rate']:.1%})")
        return secondary_config

    def _load_historical_percentiles(self):
        """Load historical percentile distributions from shared secondaries cache"""
        try:
            # Use shared secondaries cache (version-independent)
            percentile_file = os.path.join(SECONDARIES_CACHE_DIR, 'historical_percentile_distributions.pkl')

            if not os.path.exists(percentile_file):
                print(f"WARNING: Historical percentile file not found: {percentile_file}")
                print("Secondary signals will use default 50th percentile")
                return None

            with open(percentile_file, 'rb') as f:
                percentiles = pickle.load(f)

            print(f"Loaded historical percentile distributions from shared cache:")
            print(f"  Location: {SECONDARIES_CACHE_DIR}")
            for metric_name in percentiles.keys():
                # Skip metadata keys
                if metric_name.startswith('_'):
                    continue
                count = percentiles[metric_name]['count']
                print(f"  {metric_name}: {count:,} historical values")

            return percentiles

        except Exception as e:
            print(f"Error loading historical percentiles: {e}")
            traceback.print_exc()
            return None

    def _value_to_percentile(self, value, metric_name):
        """
        Convert a raw metric value to its percentile rank.

        DELEGATES to calculations.value_to_percentile for canonical implementation.
        """
        if self.historical_percentiles is None or metric_name not in self.historical_percentiles:
            return 50.0

        return value_to_percentile(value, self.historical_percentiles[metric_name])

    def load_prefilter_results(self):
        """Load pre-filtered active trades with sum dev data"""
        try:
            prefilter_df = pd.read_excel(self.prefilter_file)

            print(f"Pre-filter columns: {prefilter_df.columns.tolist()}")

            if 'Active' in prefilter_df.columns:
                active_df = prefilter_df[prefilter_df['Active'] == 1]
            else:
                active_df = prefilter_df
                print("No 'Active' column found - treating all rows as active")

            # Convert Tags to match pairs_df type
            pairs_tag_type = self.pairs_df['Tag'].dtype
            if pairs_tag_type == 'int64' or pairs_tag_type == 'int32':
                active_df['Tag'] = active_df['Tag'].astype(int)
            else:
                active_df['Tag'] = active_df['Tag'].astype(str)

            self.active_trades = set(active_df['Tag'].values)

            # Store sum dev data for reuse
            self.prefilter_sum_dev_data = {}
            for _, row in active_df.iterrows():
                tag = row['Tag']
                self.prefilter_sum_dev_data[tag] = {
                    'Sum_Deviation': row.get('Sum_Deviation'),
                    'Sum_Dev_Percentile': row.get('Sum_Dev_Percentile'),
                    'Sum_Dev_Bucket': row.get('Sum_Dev_Bucket')
                }

            print(f"Loaded {len(self.active_trades)} active trades from pre-filter")
            print(f"Cached sum dev data for {len(self.prefilter_sum_dev_data)} pairs")

            if 'Sum_Dev_Bucket' in active_df.columns:
                bucket_counts = active_df['Sum_Dev_Bucket'].value_counts().sort_index()
                print(f"\n   Sum dev buckets in pre-filter:")
                for bucket, count in bucket_counts.items():
                    print(f"     {bucket}: {count}")

        except Exception as e:
            print(f"Error loading pre-filter: {e}")
            traceback.print_exc()
            self.active_trades = None
            self.prefilter_sum_dev_data = {}

    def is_trade_active(self, tag):
        """Check if a trade should be analyzed based on pre-filter"""
        if self.active_trades is None:
            return True
        return str(tag) in self.active_trades

    async def initialize_data_connection(self, ib_host='127.0.0.1', ib_port=7497, client_id=1):
        """Initialize Interactive Brokers connection"""
        try:
            self.ib = IB()
            await self.ib.connectAsync(ib_host, ib_port, clientId=client_id)
            print(f"Connected to Interactive Brokers: {ib_host}:{ib_port}")

            if self.ib.isConnected():
                print("IB connection verified successfully")
                return True
            else:
                print("IB connection verification failed")
                return False

        except Exception as e:
            print(f"Failed to connect to Interactive Brokers: {e}")
            self.ib = None
        return False

    def verify_subsector_has_today(self):
        """
        Verify that subsector indices include today's data after appending.
        This confirms the live price fix is working correctly.
        """
        today = pd.Timestamp.now().normalize()

        missing = []
        has_today = []

        for etf in self.subsector_manager._loaded_etfs:
            if etf not in self.subsector_manager._indices:
                continue

            for category, returns_series in self.subsector_manager._indices[etf].items():
                if returns_series is None or len(returns_series) == 0:
                    missing.append(f"{etf}/{category}: empty")
                    continue

                last_date = pd.Timestamp(returns_series.index[-1]).normalize()
                if hasattr(last_date, 'tz') and last_date.tz is not None:
                    last_date = last_date.tz_localize(None)

                gap_days = (today - last_date).days

                if gap_days <= 1:  # Allow for timezone edge cases
                    has_today.append(f"{etf}/{category}")
                else:
                    missing.append(f"{etf}/{category}: last={last_date.strftime('%Y-%m-%d')} ({gap_days} days ago)")

        if missing:
            print(f"\nWARNING: {len(missing)} subsector indices missing today's data:")
            for m in missing[:10]:
                print(f"     {m}")
            if len(missing) > 10:
                print(f"     ... and {len(missing) - 10} more")
            print(f"\n   This may indicate append_today_subsector_returns() failed.")
            print(f"   Filters may be using stale data!\n")
        else:
            print(f"VERIFIED: All {len(has_today)} subsector indices have today's data")

    def append_today_subsector_returns(self):
        """
        Calculate and append today's subsector index values from live constituent prices.

        Must be called AFTER append_today_live_prices_to_historical() so that
        individual stock historical data already has today's live prices.

        Subsector index return = equal-weighted mean of constituent stock returns
        """
        today_utc = pd.Timestamp.now(tz='UTC').normalize()

        # Also need tz-naive version for subsector data (which is tz-naive)
        today_naive = pd.Timestamp.now().normalize()

        # Group stocks by (ETF, Category)
        category_stocks = {}  # {(etf, category): [tickers]}

        for ticker in self.historical_data.keys():
            category = self.subsector_manager.get_category(ticker)
            etf = self.subsector_manager.get_etf(ticker)

            if category and etf:
                key = (etf, category)
                if key not in category_stocks:
                    category_stocks[key] = []
                category_stocks[key].append(ticker)

        # Calculate today's return for each subsector
        updated_count = 0

        for (etf, category), tickers in category_stocks.items():
            returns = []

            for ticker in tickers:
                hist = self.historical_data.get(ticker)
                if hist is None or len(hist) < 2:
                    continue

                # After appending live prices: iloc[-1] = today, iloc[-2] = yesterday
                today_price = hist['close'].iloc[-1]
                yesterday_price = hist['close'].iloc[-2]

                if yesterday_price and yesterday_price > 0 and today_price and not pd.isna(today_price):
                    ret = (today_price - yesterday_price) / yesterday_price
                    returns.append(ret)

            if not returns:
                continue

            # Equal-weighted average return for this subsector
            today_return = np.mean(returns)

            # Update returns series (_indices)
            if etf in self.subsector_manager._indices and category in self.subsector_manager._indices[etf]:
                returns_series = self.subsector_manager._indices[etf][category]

                if today_naive in returns_series.index:
                    returns_series.loc[today_naive] = today_return
                else:
                    new_row = pd.Series([today_return], index=[today_naive])
                    self.subsector_manager._indices[etf][category] = pd.concat([returns_series, new_row])

                updated_count += 1

            # Update prices series (_index_prices)
            if etf in self.subsector_manager._index_prices and category in self.subsector_manager._index_prices[etf]:
                prices_series = self.subsector_manager._index_prices[etf][category]

                if len(prices_series) > 0:
                    yesterday_idx_price = prices_series.iloc[-1]
                    today_idx_price = yesterday_idx_price * (1 + today_return)

                    if today_naive in prices_series.index:
                        prices_series.loc[today_naive] = today_idx_price
                    else:
                        new_row = pd.Series([today_idx_price], index=[today_naive])
                        self.subsector_manager._index_prices[etf][category] = pd.concat([prices_series, new_row])

        print(f"SUBSECTOR INDICES UPDATED: {updated_count} categories now include today's return from live prices")

    # =========================================================================
    # CRITICAL FIX: Append today's live prices to historical data
    # =========================================================================

    def append_today_live_prices_to_historical(self):
        """
        Append today's live prices to historical data so filters use current prices.

        Without this, all filters use yesterday's close as 'today' via iloc[-1],
        missing any intraday moves entirely.

        Must be called AFTER fetch_all_market_data() and BEFORE running any filters.
        """
        today_utc = pd.Timestamp.now(tz='UTC').normalize()
        appended_count = 0
        skipped_no_live = 0
        skipped_no_hist = 0
        updated_existing = 0

        for ticker, live_data in self.market_data.items():
            live_price = live_data.get('live_price')

            # Skip if no valid live price
            if live_price is None or pd.isna(live_price) or live_price <= 0:
                skipped_no_live += 1
                continue

            # Skip if no historical data
            hist = self.historical_data.get(ticker)
            if hist is None or hist.empty:
                skipped_no_hist += 1
                continue

            # Check if today already exists in historical data
            if today_utc in hist.index:
                # Update today's value with live price
                hist.loc[today_utc, 'close'] = live_price
                updated_existing += 1
            else:
                # Append today's value as new row
                new_row = pd.DataFrame({'close': [live_price]}, index=[today_utc])
                self.historical_data[ticker] = pd.concat([hist, new_row])

            appended_count += 1

        # Also update index data
        for index_ticker in self.index_data.keys():
            if index_ticker in self.market_data:
                live_price = self.market_data[index_ticker].get('live_price')
                if live_price and not pd.isna(live_price) and live_price > 0:
                    hist = self.index_data[index_ticker]
                    if hist is not None and not hist.empty:
                        if today_utc in hist.index:
                            hist.loc[today_utc, 'close'] = live_price
                        else:
                            new_row = pd.DataFrame({'close': [live_price]}, index=[today_utc])
                            self.index_data[index_ticker] = pd.concat([hist, new_row])

        # Store for later verification
        self.live_prices_appended = True
        self.live_price_append_count = appended_count

        # Single confirmation message
        print(f"\nLIVE PRICES APPENDED: {appended_count} tickers now include today's live price in historical data")
        print(f"   (Updated existing: {updated_existing}, New rows: {appended_count - updated_existing})")
        if skipped_no_live > 0 or skipped_no_hist > 0:
            print(f"   Skipped: {skipped_no_live} (no live price), {skipped_no_hist} (no historical data)")

        # =========================================================================
        # CRITICAL: Also update subsector indices with today's returns
        # Without this, align_historical_data_v92() excludes today from common_index
        # because subsector data wouldn't have today, making all filters use yesterday
        # =========================================================================
        self.append_today_subsector_returns()

    async def fetch_all_market_data(self):
        """
        Fetch market data - NO treasury data needed (single-factor model)
        """
        try:
            print("\nFetching market data (single-factor model - no treasury)...")

            # Determine which pairs to fetch data for
            if self.active_trades is not None:
                pairs_to_fetch = self.pairs_df[
                    self.pairs_df['Tag'].isin(self.active_trades)
                ]
                print(f"Fetching data for {len(pairs_to_fetch)} active pairs")
            else:
                pairs_to_fetch = self.pairs_df
                print(f"No pre-filter loaded - fetching data for ALL {len(pairs_to_fetch)} pairs")

            if len(pairs_to_fetch) == 0:
                print("ERROR: No pairs found to fetch!")
                return False

            # Get unique tickers
            tickers_to_fetch = set()

            if 'Ticker1' in pairs_to_fetch.columns:
                ticker1_col = 'Ticker1'
                ticker2_col = 'Ticker2'
            elif 'Co1' in pairs_to_fetch.columns:
                ticker1_col = 'Co1'
                ticker2_col = 'Co2'
            else:
                print(f"ERROR: Could not find ticker columns")
                return False

            for _, row in pairs_to_fetch.iterrows():
                tickers_to_fetch.add(row[ticker1_col])
                tickers_to_fetch.add(row[ticker2_col])

            # Get unique index tickers
            index_tickers = list(pairs_to_fetch['Index'].unique())

            # Add indices to fetch list
            for idx in index_tickers:
                tickers_to_fetch.add(idx)

            tickers_to_fetch = list(tickers_to_fetch)

            print(f"Unique tickers to fetch: {len(tickers_to_fetch)}")
            print(f"Index tickers: {index_tickers}")

            # Load sub-sector indices from beta calibration output
            print(f"\n[{ACTIVE_VERSION}] Loading sub-sector indices from {VERSION_DIR}...")
            for etf in index_tickers:
                if self.subsector_manager.load_from_beta_output(etf, VERSION_DIR):
                    categories = self.subsector_manager.get_categories_for_etf(etf)
                    print(f"   {etf}: {len(categories)} categories - {', '.join(categories)}")
                else:
                    print(f"   {etf}: Failed to load sub-sector indices")

            # Fetch market data (no treasury)
            treasury_latest = pd.Timestamp.now()
            treasury_earliest = treasury_latest - pd.Timedelta(days=365)

            print("\nCalling fetch_all_data...")
            all_market_data = await fetch_all_data(
                tickers=tickers_to_fetch,
                ib=self.ib,
                treasury_earliest=treasury_earliest,
                treasury_latest=treasury_latest,
                index_tickers=index_tickers,
                force_refresh=False
            )

            # Parse results
            self.market_data = {}
            self.historical_data = {}
            self.index_data = {}

            for ticker, data in all_market_data.items():
                self.market_data[ticker] = {
                    'live_price': data.get('live_price'),
                    'bid': data.get('bid'),
                    'ask': data.get('ask'),
                    'spread': data.get('spread')
                }

                hist = data.get('historical_data')
                if hist is not None and not hist.empty:
                    self.historical_data[ticker] = hist

            # Store index data separately
            for index_ticker in index_tickers:
                if index_ticker in all_market_data:
                    hist = all_market_data[index_ticker].get('historical_data')
                    if hist is not None and not hist.empty:
                        self.index_data[index_ticker] = hist

            print(f"Market data fetched for {len(self.market_data)} tickers")
            print(f"Historical data fetched for {len(self.historical_data)} tickers")
            print(f"Index data loaded for {len(self.index_data)} indices")
            print(f"No treasury data needed (single-factor model)")

            # =========================================================================
            # CRITICAL: Append today's live prices to historical data
            self.append_today_live_prices_to_historical()

            # VERIFY: Subsector indices now include today's data
            self.verify_subsector_has_today()

            # Reset and populate alpha cache (MUST reset to get fresh data!)
            print("\n[NEW] Resetting and populating alpha cache with fresh LAM data...")
            self.alpha_cache = reset_alpha_cache()

            # Verify we're not accidentally using stale data
            if self.alpha_cache.is_populated():
                self.alpha_cache.warn_if_stale(max_stale_minutes=30)

            all_tickers = list(self.historical_data.keys())
            self.alpha_cache.populate(all_tickers, self.historical_data, self.subsector_manager)

            return True

        except Exception as e:
            print(f"Error fetching market data: {e}")
            traceback.print_exc()
            return False

    def load_earnings_dates(self, earnings_file_path=None):
        """Load earnings dates from the earnings calendar"""
        try:
            if earnings_file_path is None:
                earnings_file_path = config.earnings_calendar_file()

            print(f"Loading earnings calendar from: {earnings_file_path}")

            if not os.path.exists(earnings_file_path):
                print(f"WARNING: Earnings calendar file not found")
                return {}

            df_calendar = pd.read_excel(earnings_file_path, sheet_name='Earnings Calendar')
            df_calendar.columns = [col.strip() for col in df_calendar.columns]

            if 'ticker' not in df_calendar.columns or 'reportDate' not in df_calendar.columns:
                print("ERROR: Required columns not found in earnings calendar")
                return {}

            df_calendar = df_calendar[df_calendar['ticker'].notna()]
            df_calendar = df_calendar[df_calendar['reportDate'].notna()]
            df_calendar['ticker'] = df_calendar['ticker'].str.upper().str.strip()
            df_calendar['reportDate'] = pd.to_datetime(df_calendar['reportDate']).dt.date

            earnings_dates = {}
            for _, row in df_calendar.iterrows():
                ticker = row['ticker']
                report_date = row['reportDate']
                report_time = row.get('reportTime', 'pre-market')

                if pd.notna(report_time) and isinstance(report_time, str):
                    if 'post' in report_time.lower():
                        effective_date = report_date + timedelta(days=1)
                        while effective_date.weekday() >= 5:
                            effective_date += timedelta(days=1)
                        earnings_dates[ticker] = effective_date
                    else:
                        earnings_dates[ticker] = report_date
                else:
                    earnings_dates[ticker] = report_date

            self.earnings_dates = earnings_dates
            print(f"Loaded {len(earnings_dates)} earnings dates")

            today = date.today()
            upcoming_5_days = [d for d in earnings_dates.values() if 0 <= (d - today).days <= 5]
            print(f"Upcoming earnings (next 5 days): {len(upcoming_5_days)}")

            return earnings_dates

        except Exception as e:
            print(f"Error loading earnings dates: {e}")
            traceback.print_exc()
            return {}

    def load_parameters(self):
        """Load parameters from parameters Excel file"""
        try:
            self.pairs_df = pd.read_excel(self.parameters_file, sheet_name='Pairs')
            self.tickers_df = pd.read_excel(self.parameters_file, sheet_name='Tickers')

            self.cumulative_stats_df = pd.read_excel(
                self.parameters_file,
                sheet_name='15Day_Cumulative_Stats'
            )

            sum_dev_df = pd.read_excel(
                self.parameters_file,
                sheet_name='Sum_Deviation_Params'
            )
            self.sum_deviation_stddev = float(sum_dev_df[
                sum_dev_df['Parameter'] == 'Sum Deviation StdDev'
            ]['Value'].iloc[0])

            self.pairs_df['Tag'] = self.pairs_df['Tag'].astype(str)

            self.create_parameter_lookups()

            print(f"Loaded {len(self.pairs_df)} pairs and {len(self.tickers_df)} tickers")
            print(f"Loaded {len(self.cumulative_stats_df)} 15-day cumulative statistics")
            print(f"Sum deviation global std dev: {self.sum_deviation_stddev}")

        except Exception as e:
            print(f"Error loading parameters: {e}")
            raise

    async def generate_and_save_alpha_data(self, lookback_days=252):
        """
        Generate daily alpha data using single-factor model
        """
        print("\n" + "="*80)
        print("GENERATING ALPHA DATA FILES (SINGLE-FACTOR MODEL)")
        print("="*80)

        try:
            # Process each index separately
            for index_ticker in self.index_etfs:
                print(f"\nProcessing {index_ticker}...")

                # Get index historical data
                index_data = self.historical_data.get(index_ticker)
                if index_data is None or len(index_data) < lookback_days:
                    print(f"  WARNING: Insufficient index data for {index_ticker}, skipping")
                    continue

                # Find all stocks assigned to this index
                all_index_stocks = self.tickers_df[
                    self.tickers_df['Index'] == index_ticker
                ]['Ticker'].tolist()

                # Filter to only stocks we have historical data for
                index_stocks = [t for t in all_index_stocks if t in self.historical_data]

                if len(index_stocks) == 0:
                    print(f"  WARNING: No stocks with data for {index_ticker}, skipping")
                    continue

                print(f"  Found {len(index_stocks)} stocks with data (of {len(all_index_stocks)} assigned)")

                # Calculate daily alphas for each stock
                alpha_dict = {}
                successful_stocks = 0

                for ticker in index_stocks:
                    stock_data = self.historical_data.get(ticker)
                    if stock_data is None or len(stock_data) < 30:
                        continue

                    # Get sub-sector beta and index
                    beta_subsector = self.subsector_manager.get_subsector_beta(ticker)
                    if beta_subsector is None:
                        continue

                    try:
                        # Get sub-sector index returns for this ticker's category
                        subsector_returns = self.subsector_manager.get_subsector_returns(
                            ticker, stock_data.index
                        )

                        if subsector_returns is None or len(subsector_returns) < 30:
                            continue

                        # Align data
                        common_dates = stock_data.index.intersection(subsector_returns.index)
                        if len(common_dates) < 30:
                            continue

                        stock_aligned = stock_data.loc[common_dates]
                        subsector_aligned = subsector_returns.loc[common_dates]

                        # Calculate daily alphas (single-factor)
                        daily_alphas = []
                        dates = []

                        for i in range(1, len(common_dates)):
                            current_date = common_dates[i]
                            stock_return = calculate_daily_return(stock_aligned['close'].iloc[i-1:i+1])
                            subsector_return = subsector_aligned.iloc[i]

                            # Handle NaN values appropriately
                            if pd.isna(stock_return):
                                alpha = 0.0  # No valid stock data
                            elif pd.isna(beta_subsector):
                                alpha = stock_return  # No beta, use raw stock return as alpha
                            elif pd.isna(subsector_return):
                                alpha = stock_return  # No subsector return, use raw stock return
                            else:
                                # Single-factor alpha
                                alpha = stock_return - (beta_subsector * subsector_return)

                            daily_alphas.append(alpha)
                            dates.append(current_date)

                        alpha_series = pd.Series(daily_alphas, index=dates)
                        alpha_dict[ticker] = alpha_series
                        successful_stocks += 1

                    except Exception as e:
                        print(f"    Error calculating alpha for {ticker}: {e}")
                        continue

                if len(alpha_dict) == 0:
                    print(f"  WARNING: No successful alpha calculations for {index_ticker}")
                    continue

                # Convert to DataFrame
                alpha_df = pd.DataFrame(alpha_dict)

                print(f"  Calculated alphas for {len(alpha_df.columns)} stocks")
                alpha_df.index = pd.to_datetime(alpha_df.index)
                alpha_df = alpha_df.sort_index()

                # Save to pickle
                output_file = os.path.join(config.alpha_data_dir(), f'{index_ticker}_alpha_data.pkl')
                alpha_df.to_pickle(output_file)

                print(f"  Saved: {output_file}")
                print(f"    Date range: {alpha_df.index[0]} to {alpha_df.index[-1]}")
                print(f"    Total days: {len(alpha_df)}")
                print(f"    Total stocks: {len(alpha_df.columns)}")

            print("\n" + "="*80)
            print("ALPHA DATA GENERATION COMPLETE")
            print("="*80)

            return True

        except Exception as e:
            print(f"ERROR generating alpha data: {e}")
            traceback.print_exc()
            return False

    def _load_alpha_data_for_index(self, index_ticker):
        """Load pre-generated alpha data for an index"""
        try:
            alpha_file = os.path.join(config.alpha_data_dir(), f'{index_ticker}_alpha_data.pkl')
            if os.path.exists(alpha_file):
                return pd.read_pickle(alpha_file)
            return None
        except Exception as e:
            print(f"  Error loading alpha data for {index_ticker}: {e}")
            return None

    def create_parameter_lookups(self):
        """Create lookup dictionaries"""
        self.alpha_variance_hurdles = self.pairs_df.set_index('Tag')['CDF_Threshold'].to_dict()
        self.ema_multipliers = self.pairs_df.set_index('Tag')['EMA_Multiplier'].to_dict()
        self.tag_to_pair = self.pairs_df.set_index('Tag')['Pair'].to_dict()

        # Only sub-sector betas needed (no treasury)
        self.subsector_betas = self.tickers_df.set_index('Ticker')['SubSector_Beta'].to_dict()

        self.all_tickers = list(set(self.pairs_df['Co1'].tolist() + self.pairs_df['Co2'].tolist()))
        self.index_etfs = list(self.pairs_df['Index'].unique())

        print(f"Created lookups for {len(self.pairs_df)} pairs")
        print(f"Treasury betas not needed (single-factor model)")

    # =========================================================================
    # DIAGNOSTIC: Calculate today's changes for shortlist tickers
    # =========================================================================

    def calculate_todays_changes_for_shortlist(self, final_pairs):
            """
            Calculate and display today's nominal and alpha changes for each ticker in shortlist.

            This allows manual verification that live prices are being used correctly
            and helps spot any anomalous intraday moves that might warrant attention.

            Uses sub-sector returns (not main ETF) for alpha calculation to match
            the single-factor model used in filters.
            """
            if not final_pairs:
                print("\nNo pairs in shortlist to analyze for today's changes")
                return

            print("\n" + "="*100)
            print("TODAY'S INTRADAY CHANGES - SHORTLIST TICKERS".center(100))
            print("="*100)
            print("\nThis shows today's price moves for manual verification that filters are using live data.")
            print("Alpha calculated using sub-sector returns (not main ETF).\n")

            # Collect unique tickers from shortlist
            shortlist_tickers = set()

            for pair in final_pairs:
                shortlist_tickers.add(pair['Ticker1'])
                shortlist_tickers.add(pair['Ticker2'])

            # Calculate changes for each ticker
            changes_data = []

            for ticker in sorted(shortlist_tickers):
                hist = self.historical_data.get(ticker)
                live_data = self.market_data.get(ticker, {})
                live_price = live_data.get('live_price')

                # Get subsector beta
                beta = self.subsector_manager.get_subsector_beta(ticker)

                # Get category
                category = self.subsector_manager.get_category(ticker)

                # Find which index this ticker belongs to
                index_ticker = None
                ticker_row = self.tickers_df[self.tickers_df['Ticker'] == ticker]
                if len(ticker_row) > 0:
                    index_ticker = ticker_row['Index'].iloc[0]

                if hist is None or len(hist) < 2:
                    changes_data.append({
                        'Ticker': ticker,
                        'Yesterday_Close': None,
                        'Live_Price': live_price,
                        'Nominal_Change_%': None,
                        'Alpha_Change_%': None,
                        'Index': index_ticker,
                        'Category': category,
                        'Beta': beta
                    })
                    continue

                # Get yesterday's close (second to last after we appended today)
                # After appending, iloc[-1] is today, iloc[-2] is yesterday
                if len(hist) >= 2:
                    yesterday_close = hist['close'].iloc[-2]
                    today_close = hist['close'].iloc[-1]  # This should be live price now
                else:
                    yesterday_close = hist['close'].iloc[-1]
                    today_close = live_price

                # Calculate nominal change
                if yesterday_close and yesterday_close > 0 and today_close:
                    nominal_change = (today_close - yesterday_close) / yesterday_close * 100
                    stock_return = nominal_change / 100
                else:
                    nominal_change = None
                    stock_return = None

                # Calculate alpha change using sub-sector returns
                alpha_change = None

                if stock_return is not None and beta is not None:
                    try:
                        # Get sub-sector returns for this ticker's category
                        subsector_returns = self.subsector_manager.get_subsector_returns(ticker)

                        if subsector_returns is not None and len(subsector_returns) >= 2:
                            # Get today's subsector return (last value in series)
                            subsector_today_return = subsector_returns.iloc[-1]

                            if not pd.isna(subsector_today_return):
                                alpha_change = (stock_return - beta * subsector_today_return) * 100

                    except Exception as e:
                        # Fallback: if subsector returns unavailable, leave alpha as None
                        pass

                changes_data.append({
                    'Ticker': ticker,
                    'Yesterday_Close': yesterday_close,
                    'Live_Price': today_close,
                    'Nominal_Change_%': nominal_change,
                    'Alpha_Change_%': alpha_change,
                    'Index': index_ticker,
                    'Category': category,
                    'Beta': beta
                })

            # Create DataFrame and display
            changes_df = pd.DataFrame(changes_data)

            # Sort by absolute nominal change to highlight big movers
            changes_df['Abs_Nominal'] = changes_df['Nominal_Change_%'].abs()
            changes_df = changes_df.sort_values('Abs_Nominal', ascending=False)
            changes_df = changes_df.drop('Abs_Nominal', axis=1)

            # Print with formatting
            print(f"{'Ticker':<8} {'Yest Close':>12} {'Live Price':>12} {'Nominal %':>10} {'Alpha %':>10} {'Index':<6} {'Category':<28} {'Beta':>6}")
            print("-" * 105)

            for _, row in changes_df.iterrows():
                ticker = row['Ticker']
                yest = f"${row['Yesterday_Close']:.2f}" if row['Yesterday_Close'] else "N/A"
                live = f"${row['Live_Price']:.2f}" if row['Live_Price'] else "N/A"
                nominal = f"{row['Nominal_Change_%']:+.2f}%" if pd.notna(row['Nominal_Change_%']) else "N/A"
                alpha = f"{row['Alpha_Change_%']:+.2f}%" if pd.notna(row['Alpha_Change_%']) else "N/A"
                idx = row.get('Index', 'N/A') or 'N/A'
                cat = row.get('Category', 'N/A') or 'N/A'
                cat = cat[:27] if len(cat) > 27 else cat  # Truncate long category names
                beta_val = f"{row['Beta']:.2f}" if pd.notna(row.get('Beta')) else "N/A"

                # Highlight large moves
                if pd.notna(row['Nominal_Change_%']) and abs(row['Nominal_Change_%']) >= 5:
                    print(f"!! {ticker:<6} {yest:>12} {live:>12} {nominal:>10} {alpha:>10} {idx:<6} {cat:<28} {beta_val:>6}")
                else:
                    print(f"   {ticker:<6} {yest:>12} {live:>12} {nominal:>10} {alpha:>10} {idx:<6} {cat:<28} {beta_val:>6}")

            # Summary statistics
            print("\n" + "-"*105)
            nominal_changes = changes_df['Nominal_Change_%'].dropna()
            alpha_changes = changes_df['Alpha_Change_%'].dropna()

            if len(nominal_changes) > 0:
                print(f"\nNominal Change Summary: Min={nominal_changes.min():+.2f}%, Max={nominal_changes.max():+.2f}%, Avg={nominal_changes.mean():+.2f}%")

            if len(alpha_changes) > 0:
                print(f"Alpha Change Summary:   Min={alpha_changes.min():+.2f}%, Max={alpha_changes.max():+.2f}%, Avg={alpha_changes.mean():+.2f}%")

            # Flag any extreme moves
            extreme_moves = changes_df[changes_df['Nominal_Change_%'].abs() >= 10]
            if len(extreme_moves) > 0:
                print(f"\nWARNING: {len(extreme_moves)} ticker(s) with moves >= 10% today:")
                for _, row in extreme_moves.iterrows():
                    print(f"   {row['Ticker']}: {row['Nominal_Change_%']:+.2f}%")

            print("\n" + "="*100)

            return changes_df

    # ===================== PRIMARY FILTERS =====================

    def calculate_15_day_alpha_variance_v92(self, ticker1, ticker2, tag, index_ticker):
        """Calculate 15-day alpha variance using single-factor model"""
        try:
            hist1 = self.historical_data.get(ticker1)
            hist2 = self.historical_data.get(ticker2)

            if hist1 is None or hist2 is None:
                return False, 0.0, {"error": "Insufficient historical data"}

            # Use single-factor filter
            return check_15day_alpha_variance_v92(
                ticker1, ticker2, tag, hist1, hist2,
                self.cumulative_stats_df, self.alpha_variance_hurdles,
                self.subsector_manager, self.earnings_dates
            )

        except Exception as e:
            return False, 0.0, {"error": str(e)}

    def calculate_2_day_deviation_v92(self, ticker1, ticker2, tag, index_ticker):
        """Calculate 2-day deviation using single-factor model"""
        try:
            hist1 = self.historical_data.get(ticker1)
            hist2 = self.historical_data.get(ticker2)

            if hist1 is None or hist2 is None:
                return False, 0.0, {"error": "Insufficient historical data"}

            # Use single-factor filter
            return check_2day_deviation_v92(
                ticker1, ticker2, tag, hist1, hist2,
                self.ema_multipliers, self.subsector_manager
            )

        except Exception as e:
            return False, 0.0, {"error": str(e)}

    def check_same_direction(self, alpha_variance_details, two_day_details):
        """Check same direction - uses calculations implementation"""
        return check_same_direction(alpha_variance_details, two_day_details)

    def check_nominal_direction(self, ticker1, ticker2, tail):
        """Check 5-day nominal price direction - uses calculations implementation"""
        hist1 = self.historical_data.get(ticker1)
        hist2 = self.historical_data.get(ticker2)

        if hist1 is None or hist2 is None:
            return False, {"error": "Historical data not available"}

        return check_nominal_direction(ticker1, ticker2, tail, hist1, hist2)

    def check_spread_hurdle(self, ticker1, ticker2, max_spread_pct=0.004):
        """Check spread hurdle - uses calculations implementation"""
        return check_spread_hurdle(ticker1, ticker2, self.market_data, max_spread_pct)

    def check_earnings_filter(self, ticker1, ticker2, days_ahead=5):
        """Check earnings filter - uses calculations implementation"""
        return check_earnings_filter(ticker1, ticker2, self.earnings_dates, days_ahead)

    # ===================== SUM DEVIATION =====================

    # _calculate_sum_deviation_v92 removed -- sum deviation is calculated once
    # in Pre_Filter and read from self.prefilter_sum_dev_data in LAM. See apply_secondary_signals.

    # ===================== SECONDARY SIGNALS =====================

    async def _prefetch_secondary_signal_data(self, unique_tickers):
        """
        Pre-fetch daily volumes and hourly prices for all unique tickers.
        This eliminates redundant API calls when the same ticker appears in multiple pairs.

        Returns:
            dict: {
                'daily_volumes': {ticker: pd.Series},
                'hourly_prices': {ticker: pd.DataFrame}
            }
        """
        from src.shared.fetch_market_data import fetch_daily_volumes_with_extrapolation, get_hourly_price_data

        print(f"\nPRE-FETCHING secondary signal data for {len(unique_tickers)} unique tickers...")

        daily_volumes = {}
        hourly_prices = {}

        # Fetch daily volumes (30 days needed for volume_ratio)
        print(f"   [1/2] Fetching daily volumes...")
        fetched_vol = 0
        for i, ticker in enumerate(unique_tickers):
            try:
                volumes = await fetch_daily_volumes_with_extrapolation(self.ib, ticker, 30)
                if volumes is not None and len(volumes) > 0:
                    daily_volumes[ticker] = volumes
                    fetched_vol += 1
            except Exception as e:
                pass  # Skip silently, will use NaN for this ticker

            # Progress indicator
            if (i + 1) % 50 == 0:
                print(f"       Volumes: {i + 1}/{len(unique_tickers)}...")

            # Small delay every 10 tickers to avoid overwhelming IB
            if i % 10 == 0:
                await asyncio.sleep(0.05)

        print(f"       Daily volumes fetched for {fetched_vol}/{len(unique_tickers)} tickers")

        # Fetch hourly prices (15 days needed for intraday vol)
        print(f"   [2/2] Fetching hourly prices...")
        fetched_hourly = 0
        for i, ticker in enumerate(unique_tickers):
            try:
                hourly = await get_hourly_price_data(self.ib, ticker, 15)
                if hourly is not None and not hourly.empty:
                    hourly_prices[ticker] = hourly
                    fetched_hourly += 1
            except Exception as e:
                pass  # Skip silently

            # Progress indicator
            if (i + 1) % 50 == 0:
                print(f"       Hourly: {i + 1}/{len(unique_tickers)}...")

            # Small delay every 10 tickers
            if i % 10 == 0:
                await asyncio.sleep(0.05)

        print(f"       Hourly prices fetched for {fetched_hourly}/{len(unique_tickers)} tickers")
        print(f"   Pre-fetch complete\n")

        return {
            'daily_volumes': daily_volumes,
            'hourly_prices': hourly_prices
        }

    def _calculate_volume_ratio_from_cache(self, ticker1, ticker2, cache, short_days=3, long_days=30):
        """Calculate volume ratio from pre-fetched cached data."""
        try:
            volumes1 = cache['daily_volumes'].get(ticker1)
            volumes2 = cache['daily_volumes'].get(ticker2)

            if volumes1 is None or volumes2 is None:
                return np.nan
            if len(volumes1) < long_days or len(volumes2) < long_days:
                return np.nan

            short_avg_1 = volumes1.iloc[-short_days:].mean()
            long_avg_1 = volumes1.iloc[-long_days:].mean()
            ratio1 = short_avg_1 / long_avg_1 if long_avg_1 > 0 else np.nan

            short_avg_2 = volumes2.iloc[-short_days:].mean()
            long_avg_2 = volumes2.iloc[-long_days:].mean()
            ratio2 = short_avg_2 / long_avg_2 if long_avg_2 > 0 else np.nan

            if not pd.isna(ratio1) and not pd.isna(ratio2):
                return (ratio1 + ratio2) / 2
            return np.nan
        except Exception:
            return np.nan

    def _calculate_rolling_intraday_vol_from_cache(self, ticker1, ticker2, cache, lookback_days=10):
        """Calculate rolling intraday volatility from pre-fetched cached data."""
        try:
            hourly1 = cache['hourly_prices'].get(ticker1)
            hourly2 = cache['hourly_prices'].get(ticker2)

            if hourly1 is None or hourly2 is None:
                return np.nan
            if hourly1.empty or hourly2.empty:
                return np.nan

            # Calculate returns
            hourly1 = hourly1.copy()
            hourly2 = hourly2.copy()
            hourly1['returns'] = hourly1['close'].pct_change().fillna(0)
            hourly2['returns'] = hourly2['close'].pct_change().fillna(0)

            # Group by date
            hourly1_with_date = hourly1.reset_index()
            hourly2_with_date = hourly2.reset_index()
            hourly1_with_date['date'] = pd.to_datetime(hourly1_with_date['date']).dt.date
            hourly2_with_date['date'] = pd.to_datetime(hourly2_with_date['date']).dt.date

            daily_vol_1 = hourly1_with_date.groupby('date')['returns'].std()
            daily_vol_2 = hourly2_with_date.groupby('date')['returns'].std()

            if len(daily_vol_1) < lookback_days or len(daily_vol_2) < lookback_days:
                return np.nan

            rolling_vol_1 = daily_vol_1.iloc[-lookback_days:].mean()
            rolling_vol_2 = daily_vol_2.iloc[-lookback_days:].mean()

            return (rolling_vol_1 + rolling_vol_2) / 2
        except Exception:
            return np.nan

    def _calculate_volume_dominance_from_cache(self, ticker1, ticker2, cache, lookback_days=10):
        """Calculate volume dominance from pre-fetched cached data."""
        try:
            volumes1 = cache['daily_volumes'].get(ticker1)
            volumes2 = cache['daily_volumes'].get(ticker2)

            if volumes1 is None or volumes2 is None:
                return np.nan
            if len(volumes1) < lookback_days or len(volumes2) < lookback_days:
                return np.nan

            recent_vol_1 = volumes1.iloc[-lookback_days:].sum()
            recent_vol_2 = volumes2.iloc[-lookback_days:].sum()
            total_volume = recent_vol_1 + recent_vol_2

            if total_volume > 0:
                return recent_vol_1 / total_volume
            return np.nan
        except Exception:
            return np.nan

    def _calculate_true_last_hour_vol_from_cache(self, ticker1, ticker2, cache, lookback_days=10):
        """Calculate true last hour volatility from pre-fetched cached data."""
        try:
            hourly1 = cache['hourly_prices'].get(ticker1)
            hourly2 = cache['hourly_prices'].get(ticker2)

            if hourly1 is None or hourly2 is None:
                return np.nan
            if hourly1.empty or hourly2.empty:
                return np.nan

            current_date = datetime.now(pytz.UTC).date()

            # Prepare data
            hourly1_complete = hourly1.copy()
            hourly2_complete = hourly2.copy()

            hourly1_complete['date'] = pd.to_datetime(hourly1_complete.index).date
            hourly1_complete['hour'] = pd.to_datetime(hourly1_complete.index).hour
            hourly2_complete['date'] = pd.to_datetime(hourly2_complete.index).date
            hourly2_complete['hour'] = pd.to_datetime(hourly2_complete.index).hour

            # Exclude current incomplete day
            hourly1_complete = hourly1_complete[hourly1_complete['date'] < current_date]
            hourly2_complete = hourly2_complete[hourly2_complete['date'] < current_date]

            if hourly1_complete.empty or hourly2_complete.empty:
                return np.nan

            # Calculate returns
            hourly1_complete['returns'] = hourly1_complete['close'].pct_change().fillna(0)
            hourly2_complete['returns'] = hourly2_complete['close'].pct_change().fillna(0)

            # Filter to last hour (3-4 PM = hour 15)
            last_hour_1 = hourly1_complete[hourly1_complete['hour'] == 15]
            last_hour_2 = hourly2_complete[hourly2_complete['hour'] == 15]

            if len(last_hour_1) < lookback_days or len(last_hour_2) < lookback_days:
                return np.nan

            vol_1 = last_hour_1['returns'].iloc[-lookback_days:].std()
            vol_2 = last_hour_2['returns'].iloc[-lookback_days:].std()

            if not pd.isna(vol_1) and not pd.isna(vol_2):
                return (vol_1 + vol_2) / 2
            return np.nan
        except Exception:
            return np.nan

    async def apply_secondary_signals(self, pairs_list):
        """
        Apply secondary signals and calculate composite scores.

        Pre-fetches data for unique tickers first, then calculates
        all signals from cache. Reduces API calls significantly.

        Uses canonical scoring from calculations module:
        - PERCENTILE_BANDS, STABILITY_WEIGHTS (imported constants)
        - calculate_composite_score() for score calculation
        - apply_retention_filter_by_tail() for retention filtering

        Reads sum deviation from pre-filter (no recalculation unless missing)
        """
        from scipy.stats import norm

        print(f"\nCalculating secondary signals for {len(pairs_list)} pairs (OPTIMIZED)...")
        print(f"   Using canonical scoring from calculations (bands: {list(PERCENTILE_BANDS.keys())})")

        # =====================================================================
        # Collect unique tickers and pre-fetch data ONCE
        # =====================================================================
        unique_tickers = set()
        for pair in pairs_list:
            unique_tickers.add(pair['Ticker1'])
            unique_tickers.add(pair['Ticker2'])
        unique_tickers = list(unique_tickers)

        print(f"   Found {len(unique_tickers)} unique tickers across {len(pairs_list)} pairs")

        # Pre-fetch all data (ONE API call per ticker instead of per-pair)
        signal_cache = await self._prefetch_secondary_signal_data(unique_tickers)

        # =====================================================================
        # Now calculate signals from cache (NO API calls in this loop!)
        # =====================================================================
        print(f"   Calculating signals from cached data...")

        all_pairs_with_signals = []

        for idx, pair in enumerate(pairs_list):
            tag = pair['Tag']
            ticker1 = pair['Ticker1']
            ticker2 = pair['Ticker2']
            tail = pair['Tail']
            index_ticker = pair['Index']

            # Initialize sum dev variables
            sum_dev_value = np.nan
            sum_dev_cdf = np.nan
            sum_dev_bucket = None

            # Read pre-calculated sum dev from Pre_Filter longlist.
            # Sum deviation is computed ONCE in Pre_Filter and passed forward.
            # LAM never recalculates -- pairs absent from the pre-filter cache
            # should not have reached this stage.
            try:
                if hasattr(self, 'prefilter_sum_dev_data') and tag in self.prefilter_sum_dev_data:
                    sum_dev_data = self.prefilter_sum_dev_data[tag]
                    sum_dev_value = sum_dev_data.get('Sum_Deviation', np.nan)
                    sum_dev_cdf = sum_dev_data.get('Sum_Dev_Percentile', np.nan)
                    sum_dev_bucket = sum_dev_data.get('Sum_Dev_Bucket', None)

                    if idx % 50 == 0 or idx < 3:
                        print(f"  {ticker1}/{ticker2}: Using pre-filter sum dev (bucket: {sum_dev_bucket})")

                else:
                    print(f"  {ticker1}/{ticker2}: sum dev not in pre-filter cache -- skipping pair")
                    continue

            except Exception as e:
                print(f"  Error reading sum dev for {ticker1}/{ticker2}: {e}")
                continue

            # Calculate signals FROM CACHE (no API calls!)
            volume_ratio = self._calculate_volume_ratio_from_cache(ticker1, ticker2, signal_cache)
            rolling_intraday_vol = self._calculate_rolling_intraday_vol_from_cache(ticker1, ticker2, signal_cache)
            volume_dominance = self._calculate_volume_dominance_from_cache(ticker1, ticker2, signal_cache)
            last_hour_vol = self._calculate_true_last_hour_vol_from_cache(ticker1, ticker2, signal_cache)

            # IV percentile (already cached from parquet - fast)
            try:
                iv_percentile = calculations.get_iv_percentile(ticker1, ticker2, self.parameters_file)
                if pd.isna(iv_percentile):
                    iv_percentile = 50.0
            except Exception:
                iv_percentile = 50.0

            # Build signal values dict
            signal_values = {
                'volume_ratio': volume_ratio,
                'rolling_intraday_vol': rolling_intraday_vol,
                'volume_dominance': volume_dominance,
                'true_last_hour_volatility': last_hour_vol,
                'iv_percentile': iv_percentile
            }

            # Get selected signals for this tail
            strategy_key = 'lower' if tail.upper() == 'L' else 'upper'
            tail_config = self.optimizer_config.get(strategy_key, {})
            selected_signals = tail_config.get('filters', [])

            # USE CANONICAL SCORING from calculations
            composite_score, total_points, max_possible_points, signal_percentiles = calculate_composite_score(
                signal_values=signal_values,
                selected_signals=selected_signals,
                historical_percentiles=self.historical_percentiles,
                stability_weights=STABILITY_WEIGHTS,
                percentile_bands=PERCENTILE_BANDS
            )

            # Apply index bias
            index_bias = get_index_bias(index_ticker, tail)
            composite_score_biased = min(composite_score * index_bias, 1.0)
            composite_score = composite_score_biased

            sum_dev_signal = max(abs(sum_dev_cdf - 0.5) * 2, 0) if not pd.isna(sum_dev_cdf) else 0.0

            signal_results = {
                'Tag': tag,
                'Ticker1': ticker1,
                'Ticker2': ticker2,
                'Tail': tail,
                'Sum_Deviation_Value': sum_dev_value,
                'Sum_Deviation_CDF': sum_dev_cdf,
                'Sum_Deviation_Bucket': sum_dev_bucket,
                'Sum_Deviation_Signal': sum_dev_signal,
                'Index_Bias': index_bias,
                'Volume_Ratio': volume_ratio,
                'Rolling_Intraday_Vol': rolling_intraday_vol,
                'Volume_Dominance': volume_dominance,
                'Last_Hour_Vol': last_hour_vol,
                'IV_Percentile': iv_percentile,
                'Volume_Ratio_Percentile': signal_percentiles.get('volume_ratio', np.nan),
                'Intraday_Vol_Percentile': signal_percentiles.get('rolling_intraday_vol', np.nan),
                'Volume_Dom_Percentile': signal_percentiles.get('volume_dominance', np.nan),
                'Last_Hour_Percentile': signal_percentiles.get('true_last_hour_volatility', np.nan),
                'IV_Pct_Percentile': signal_percentiles.get('iv_percentile', np.nan),
                'Composite_Score': composite_score,
                'Total_Points': total_points,
                'Max_Possible_Points': max_possible_points,
                'Weighted_Score': composite_score
            }

            self.secondary_signal_results.append(signal_results)

            all_pairs_with_signals.append({
                **pair,
                'signals': signal_results,
                'composite_score': composite_score,
                'weighted_score': composite_score,
                'sum_dev_value': sum_dev_value,
                'sum_dev_cdf': sum_dev_cdf,
                'sum_dev_bucket': sum_dev_bucket
            })

            # Progress indicator (no sleep needed - just calculating from cache!)
            if (idx + 1) % 50 == 0:
                print(f"     Processed {idx + 1}/{len(pairs_list)} pairs...")

        print(f"\nSecondary signals calculated for {len(all_pairs_with_signals)} pairs")

        # USE CANONICAL RETENTION FILTERING from calculations
        print(f"\nApplying retention rate filtering (using calculations.apply_retention_filter_by_tail)...")

        filtered_pairs, rejected_pairs, thresholds = apply_retention_filter_by_tail(
            pairs_with_scores=all_pairs_with_signals,
            optimizer_config=self.optimizer_config,
            score_key='composite_score'
        )

        # Archive rejected trades
        if rejected_pairs:
            self.save_rejected_trades_to_archive(rejected_pairs, filtered_pairs)

        print(f"\nRetention filtering complete: {len(filtered_pairs)} pairs selected")

        # Return both filtered pairs AND all pairs with signals (for Longlist)
        return filtered_pairs, all_pairs_with_signals

    # NOTE: _get_band_points has been removed - using calculations.get_band_points instead

    def save_rejected_trades_to_archive(self, rejected_pairs, passed_pairs):
        """Save trades rejected by secondary signal filtering to archive"""
        from datetime import date, timedelta

        if not rejected_pairs:
            return

        print(f"\nArchiving {len(rejected_pairs)} rejected trades...")

        thresholds = {}
        for tail_type in ['L', 'U']:
            tail_passed = [p for p in passed_pairs if p['Tail'].upper() == tail_type]
            if tail_passed:
                thresholds[tail_type] = min(p['composite_score'] for p in tail_passed)
            else:
                thresholds[tail_type] = 1.0

        archive_records = []
        today = date.today()
        termination_date = today + timedelta(days=config.max_holding_days())

        while termination_date.weekday() >= 5:
            termination_date += timedelta(days=1)

        excluded_earnings = 0

        for pair in rejected_pairs:
            ticker1 = pair['Ticker1']
            ticker2 = pair['Ticker2']
            tail = pair['Tail']

            has_earnings_conflict = False
            if self.earnings_dates:
                for ticker in [ticker1, ticker2]:
                    if ticker in self.earnings_dates:
                        earnings_date = self.earnings_dates[ticker]
                        if hasattr(earnings_date, 'date'):
                            earnings_date = earnings_date.date()
                        elif isinstance(earnings_date, str):
                            earnings_date = pd.to_datetime(earnings_date).date()
                        if today <= earnings_date <= termination_date:
                            has_earnings_conflict = True
                            break

            if has_earnings_conflict:
                excluded_earnings += 1
                continue

            signals = pair.get('signals', {})
            index_ticker = pair.get('Index', 'VGT')
            index_price = self.market_data.get(index_ticker, {}).get('live_price')

            # Get sub-sector betas
            beta1_subsector = self.subsector_manager.get_subsector_beta(ticker1)
            beta2_subsector = self.subsector_manager.get_subsector_beta(ticker2)

            record = {
                'Tag': pair['Tag'],
                'Pair': f"{ticker1}-{ticker2}",
                'Co1': ticker1,
                'Co2': ticker2,
                'Index': index_ticker,
                'Tail': tail,
                'Co1_at_Entry': self.market_data.get(ticker1, {}).get('live_price'),
                'Co2_at_Entry': self.market_data.get(ticker2, {}).get('live_price'),
                'Index_at_Entry': index_price,
                'Co1_SubSector_Beta': beta1_subsector,
                'Co2_SubSector_Beta': beta2_subsector,
                'Model': f'{ACTIVE_VERSION}_single_factor',
                'W1': pair.get('W1', 0.5),
                'W2': pair.get('W2', 0.5),
                'sum_dev_bucket': pair.get('sum_dev_bucket'),
                'Sum_Dev_CDF': pair.get('sum_dev_cdf'),
                'Sum_Dev_Value': pair.get('sum_dev_value'),
                'Volume_Ratio': signals.get('Volume_Ratio'),
                'Rolling_Intraday_Vol': signals.get('Rolling_Intraday_Vol'),
                'Volume_Dominance': signals.get('Volume_Dominance'),
                'Last_Hour_Vol': signals.get('Last_Hour_Vol'),
                'IV_Percentile': signals.get('IV_Percentile'),
                'Composite_Score': pair.get('composite_score', 0),
                'Score_Threshold': thresholds.get(tail.upper(), 1.0),
                'Would_Be_Initiation_Date': today,
                'Would_Be_Termination_Date': termination_date,
                'Status': 'Pending',
                'Archived_At': datetime.now()
            }

            archive_records.append(record)

        if not archive_records:
            return

        archive_file = config.rejected_trades_archive_file()

        if os.path.exists(archive_file):
            try:
                existing_df = pd.read_excel(archive_file)
            except Exception:
                existing_df = pd.DataFrame()
        else:
            existing_df = pd.DataFrame()

        new_df = pd.DataFrame(archive_records)
        updated_df = pd.concat([existing_df, new_df], ignore_index=True) if not existing_df.empty else new_df
        updated_df.to_excel(archive_file, index=False)

        print(f"  Archived {len(archive_records)} rejected trades")

    # ===================== MAIN PROCESSING WORKFLOW =====================

    async def process_pairs(self):
        """
        Main processing pipeline with single-factor model
        """
        print("\n" + "="*100)
        print("PROCESSING PAIRS (SINGLE-FACTOR MODEL)".center(100))
        print("="*100)

        # Verify live prices were appended
        if not self.live_prices_appended:
            print("\nWARNING: Live prices may not have been appended to historical data!")
            print("    Filters may be using stale (yesterday's) prices!")

        if self.active_trades:
            pairs_to_analyze = self.pairs_df[
                self.pairs_df['Tag'].isin(self.active_trades)
            ].copy()
            print(f"\nAnalyzing {len(pairs_to_analyze)} pre-filtered active trades")
        else:
            pairs_to_analyze = self.pairs_df.copy()
            print(f"\nAnalyzing all {len(pairs_to_analyze)} trades")

        if 'Ticker1' in pairs_to_analyze.columns:
            ticker1_col = 'Ticker1'
            ticker2_col = 'Ticker2'
        elif 'Co1' in pairs_to_analyze.columns:
            ticker1_col = 'Co1'
            ticker2_col = 'Co2'
        else:
            print(f"ERROR: Could not find ticker columns")
            return {'primary_results': [], 'final_pairs': [], 'summary': {}}

        # STAGE 1: PRIMARY FILTERS
        print("\n" + "-"*100)
        print("STAGE 1: PRIMARY FILTERS (SINGLE-FACTOR)".center(100))
        print("-"*100)

        pairs_passed_primary = []

        # Load alpha data for each index (for T-stat filter)
        alpha_data_by_index = {}
        for index_ticker in self.index_etfs:
            alpha_data_by_index[index_ticker] = self._load_alpha_data_for_index(index_ticker)
            if alpha_data_by_index[index_ticker] is not None:
                print(f"  Loaded alpha data for {index_ticker}: {len(alpha_data_by_index[index_ticker].columns)} stocks")
                print(f"    Sample columns: {list(alpha_data_by_index[index_ticker].columns)[:10]}")

        for idx, pair_row in pairs_to_analyze.iterrows():
            tag = pair_row['Tag']
            ticker1 = pair_row[ticker1_col]
            ticker2 = pair_row[ticker2_col]
            tail = pair_row['Tail']
            index_ticker = pair_row['Index']

            hist1 = self.historical_data.get(ticker1)
            hist2 = self.historical_data.get(ticker2)
            index_data = self.index_data.get(index_ticker)

            if hist1 is None or hist2 is None or index_data is None:
                continue

            # Check sub-sector data availability
            cat1 = self.subsector_manager.get_category(ticker1)
            cat2 = self.subsector_manager.get_category(ticker2)

            if cat1 is None or cat2 is None:
                filter_results = {
                    'Tag': tag, 'Ticker1': ticker1, 'Ticker2': ticker2,
                    'Tail': tail, 'Index': index_ticker,
                    'Primary_Status': 'Fail',
                    'Reason': f'Missing category: {ticker1 if cat1 is None else ticker2}'
                }
                self.primary_filter_results.append(filter_results)
                continue

            # Prepare params dict (no treasury)
            params_dict = {
                'cumulative_stats_df': self.cumulative_stats_df,
                'alpha_variance_hurdles': self.alpha_variance_hurdles,
                'ema_multipliers': self.ema_multipliers,
            }

            strict_config = {
                'cdf_adjustment': 0.0,
                'two_day_reduction': 0.0,
                'skip_same_direction': False,
                'skip_nominal_direction': False,
                'skip_spread': True,
                'skip_earnings': True,
                'tstat_filter': {
                    'skip': False,
                    'threshold': 8.0,
                    'lookback_days': 15
                }
            }

            try:
                # Use single-factor filter
                # Get alpha data for this index (for T-stat filter)
                alpha_data = alpha_data_by_index.get(index_ticker)

                passed, detailed_results = calculations.apply_primary_filters_with_leniency_v92(
                    ticker1, ticker2, tag, tail, hist1, hist2,
                    self.market_data, params_dict, self.earnings_dates,
                    strict_config, max_spread=config.prefilter_max_spread_decimal(),
                    subsector_manager=self.subsector_manager, alpha_data=alpha_data,
                    alpha_cache=self.alpha_cache
                )

                # Extract alpha CDF value from detailed results
                alpha_cdf = detailed_results.get('alpha_variance', {}).get('cdf_value')

                # Extract T-stat value from detailed results (if filter exists)
                tstat_details = detailed_results.get('co1_trend', {}).get('details', {})
                tstat_value = tstat_details.get('tstat_value')

                filter_results = {
                    'Tag': tag,
                    'Ticker1': ticker1,
                    'Ticker2': ticker2,
                    'Tail': tail,
                    'Index': index_ticker,
                    'Category1': cat1,
                    'Category2': cat2,
                    'Earnings': 'Pass' if detailed_results.get('earnings', {}).get('passed') else 'Fail',
                    'Spread': 'Pass' if detailed_results.get('spread', {}).get('passed') else 'Fail',
                    '15Day_Alpha': 'Pass' if detailed_results.get('alpha_variance', {}).get('passed') else 'Fail',
                    '15Day_Alpha_CDF': alpha_cdf,
                    '2Day_Deviation': 'Pass' if detailed_results.get('two_day_deviation', {}).get('passed') else 'Fail',
                    'Same_Direction': 'Pass' if detailed_results.get('same_direction', {}).get('passed') else 'Fail',
                    'Nominal_Direction': 'Pass' if detailed_results.get('nominal_direction', {}).get('passed') else 'Fail',
                    'Co1_Trend': 'Pass' if detailed_results.get('co1_trend', {}).get('passed') else 'Fail',
                    'Co1_Tstat': tstat_value,
                    'Primary_Status': 'Pass' if passed else 'Fail',
                    'Model': f'{ACTIVE_VERSION}_single_factor'
                }

                self.primary_filter_results.append(filter_results)

                if passed:
                    pairs_passed_primary.append({
                        'Tag': tag,
                        'Ticker1': ticker1,
                        'Ticker2': ticker2,
                        'Tail': tail,
                        'Index': index_ticker,
                        'Category1': cat1,
                        'Category2': cat2,
                        'hist1': hist1,
                        'hist2': hist2,
                        'index_data': index_data
                    })

            except Exception as e:
                print(f"  Error applying filters to {ticker1}/{ticker2}: {e}")
                filter_results = {
                    'Tag': tag, 'Ticker1': ticker1, 'Ticker2': ticker2,
                    'Tail': tail, 'Index': index_ticker,
                    'Primary_Status': 'Error'
                }
                self.primary_filter_results.append(filter_results)

        print(f"\n{len(pairs_passed_primary)}/{len(pairs_to_analyze)} pairs passed primary filters")

        # All pairs from pre-filter already passed sum dev exclusion
        pairs_passed_sum_deviation = pairs_passed_primary.copy()

        # STAGE 3: SECONDARY SIGNALS
        print("\n" + "-"*100)
        print("STAGE 3: SECONDARY SIGNALS & SCORING".center(100))
        print("-"*100)

        final_pairs, all_pairs_with_signals = await self.apply_secondary_signals(pairs_passed_sum_deviation)

        # Diagnostic
        if len(final_pairs) > 0:
            diagnostic_df = pd.DataFrame([{
                'Tag': p['Tag'],
                'Ticker1': p['Ticker1'],
                'Ticker2': p['Ticker2'],
                'Tail': p['Tail'],
                'Sum_Dev_Value': p.get('sum_dev_value'),
                'Sum_Dev_CDF': p.get('sum_dev_cdf'),
                'Sum_Dev_Bucket': p.get('sum_dev_bucket'),
                'Weighted_Score': p.get('weighted_score')
            } for p in final_pairs])

            diagnose_sum_dev_distribution(
                diagnostic_df, "LAM FINAL SHORTLIST",
                sum_dev_col='Sum_Dev_Value', cdf_col='Sum_Dev_CDF', bucket_col='Sum_Dev_Bucket'
            )

        summary_stats = {
            'total_pairs': len(self.pairs_df),
            'prefiltered_active': len(pairs_to_analyze),
            'skipped_by_prefilter': len(self.pairs_df) - len(pairs_to_analyze),
            'passed_strict_primary': len(pairs_passed_primary),
            'passed_sum_deviation': len(pairs_passed_sum_deviation),
            'final_selected': len(final_pairs),
            'Model': f'{ACTIVE_VERSION}_single_factor',
            'live_prices_appended': self.live_prices_appended,
            'live_price_append_count': self.live_price_append_count
        }

        return {
            'primary_results': self.primary_filter_results,
            'final_pairs': final_pairs,
            'all_pairs_with_signals': all_pairs_with_signals,
            'summary': summary_stats
        }

    async def disconnect(self):
        """Clean up IB connection"""
        if self.ib and self.ib.isConnected():
            self.ib.disconnect()
            print("Disconnected from Interactive Brokers")


# ===================== MAIN EXECUTION =====================

async def run_analytics(parameters_file=None, prefilter_file=None, ib_host='127.0.0.1',
                        ib_port=7497, client_id=1, earnings_file=None):
    """
    Main entry point - Single-factor model with sub-sector indices
    """
    try:
        # Default to version-aware paths if not provided
        if parameters_file is None:
            parameters_file = PARAMETERS_FILE
        if prefilter_file is None:
            prefilter_file = os.path.join(config.implementation_dir(), 'trade_prefilter_active.xlsx')

        print(f"Starting {ACTIVE_VERSION} Live Analytics (Single-Factor Model)...")

        lam = LiveAnalytics(parameters_file, prefilter_file)

        print("\nStep 1: Initializing data connection...")
        connection_success = await lam.initialize_data_connection(ib_host, ib_port, client_id)

        if not connection_success:
            print("WARNING: Failed to connect to IB - proceeding with cached data only")

        print(f"\nStep 2: Fetching market data ({ACTIVE_VERSION} - no treasury)...")
        data_success = await lam.fetch_all_market_data()

        if not data_success:
            print("ERROR: Failed to fetch market data")
            return None

        print(f"\nStep 2.5: Generating {ACTIVE_VERSION} alpha data files...")
        await lam.generate_and_save_alpha_data(lookback_days=365)

        print("\nStep 3: Loading earnings calendar...")
        lam.load_earnings_dates(earnings_file)

        print(f"\nStep 4: Processing pairs through {ACTIVE_VERSION} filters...")
        results = await lam.process_pairs()

        print("\nStep 5: Generating reports...")
        await generate_reports(results, lam)

        await lam.disconnect()

        return results

    except Exception as e:
        print(f"Error in {ACTIVE_VERSION} analytics: {e}")
        traceback.print_exc()
        return None

async def generate_reports(results, lam):
    """Generate Excel reports and console output"""
    try:
        print("\n" + "="*100)
        print("ANALYTICS RESULTS".center(100))
        print("="*100)

        summary = results['summary']
        print(f"\nPROCESSING SUMMARY:")
        print(f"  Model: {summary.get('model', 'single_factor')}")
        print(f"  Total pairs in parameters: {summary['total_pairs']}")
        print(f"  Skipped by pre-filter: {summary['skipped_by_prefilter']}")
        print(f"  Passed strict primary filters: {summary['passed_strict_primary']}")
        print(f"  Passed sum deviation: {summary['passed_sum_deviation']}")
        print(f"  Final selected: {summary['final_selected']}")
        print(f"  Live prices appended: {summary.get('live_prices_appended', 'Unknown')} ({summary.get('live_price_append_count', 0)} tickers)")

        # Primary filter results
        print("\n" + "="*100)
        print("PRIMARY FILTER RESULTS".center(100))
        print("="*100)

        primary_results_data = []
        for result in results['primary_results']:
            pair_name = f"{result['Ticker1']}-{result['Ticker2']}"
            row_data = {
                'Tag': result['Tag'],
                'Pair': pair_name,
                'Ticker1': result['Ticker1'],
                'Ticker2': result['Ticker2'],
                'Tail': result['Tail'],
                'Index': result.get('Index', 'N/A'),
                'Category1': result.get('Category1', 'N/A'),
                'Category2': result.get('Category2', 'N/A'),
                'Status': result.get('Primary_Status', 'Unknown'),
                'Earnings': result.get('Earnings', 'N/A'),
                'Spread': result.get('Spread', 'N/A'),
                '15-Day Alpha': result.get('15Day_Alpha', 'N/A'),
                'Alpha_CDF': result.get('15Day_Alpha_CDF'),
                '2-Day Dev': result.get('2Day_Deviation', 'N/A'),
                'Same Direction': result.get('Same_Direction', 'N/A'),
                'Nominal Direction': result.get('Nominal_Direction', 'N/A'),
                'Co1 Trend': result.get('Co1_Trend', 'N/A'),
                'Co1_Tstat': result.get('Co1_Tstat'),
            }
            primary_results_data.append(row_data)

        primary_df = pd.DataFrame(primary_results_data)

        from tabulate import tabulate
        print(tabulate(primary_df.head(30), headers='keys', tablefmt='fancy_grid', showindex=False))
        if len(primary_df) > 30:
            print(f"\n... showing first 30 of {len(primary_df)} pairs")

        # Secondary signal details
        if len(results['final_pairs']) > 0:
            print("\n" + "="*100)
            print("SECONDARY SIGNAL SCORES - FINAL SHORTLIST".center(100))
            print("="*100)

            secondary_data = []
            for pair in results['final_pairs']:
                pair_name = f"{pair['Ticker1']}-{pair['Ticker2']}"
                signals = pair.get('signals', {})

                row_data = {
                    'Tag': pair['Tag'],
                    'Pair': pair_name,
                    'Co1': pair['Ticker1'],
                    'Co2': pair['Ticker2'],
                    'Tail': pair['Tail'],
                    'Index': pair['Index'],
                    'Category1': pair.get('Category1', 'N/A'),
                    'Category2': pair.get('Category2', 'N/A'),
                    'Sum_Dev_Value': pair.get('sum_dev_value', np.nan),
                    'Sum_Dev_CDF': pair.get('sum_dev_cdf', np.nan),
                    'Sum_Dev_Bucket': pair.get('sum_dev_bucket', ''),
                    'Weighted_Score': pair.get('weighted_score', 0),
                    'Index_Bias': signals.get('Index_Bias', 1.0),
                    'Composite_Score': signals.get('Composite_Score', np.nan),
                    'Volume_Ratio': signals.get('Volume_Ratio'),
                    'Rolling_Intraday_Vol': signals.get('Rolling_Intraday_Vol'),
                    'Volume_Dominance': signals.get('Volume_Dominance'),
                    'Last_Hour_Vol': signals.get('Last_Hour_Vol'),
                    'IV_Percentile': signals.get('IV_Percentile'),
                    'Volume_Ratio_Pct': signals.get('Volume_Ratio_Percentile'),
                    'Intraday_Vol_Pct': signals.get('Intraday_Vol_Percentile'),
                    'Volume_Dom_Pct': signals.get('Volume_Dom_Percentile'),
                    'Last_Hour_Pct': signals.get('Last_Hour_Percentile'),
                    'IV_Pct_Pct': signals.get('IV_Pct_Percentile'),
                }
                secondary_data.append(row_data)

            secondary_df = pd.DataFrame(secondary_data)
            print(tabulate(secondary_df.head(30), headers='keys', tablefmt='fancy_grid', showindex=False))

            # =========================================================================
            # Show today's intraday changes for shortlist tickers
            # =========================================================================
            lam.calculate_todays_changes_for_shortlist(results['final_pairs'])

        # Save to Excel
        longlist_file = config.longlist_file()
        shortlist_file = config.shortlist_file()
        details_file = config.filter_details_file()

        # Merge secondary signals into Longlist for pairs that have them
        all_pairs_with_signals = results.get('all_pairs_with_signals', [])
        if all_pairs_with_signals:
            # Create lookup dict by Tag
            signals_lookup = {}
            for pair in all_pairs_with_signals:
                tag = pair['Tag']
                signals = pair.get('signals', {})
                signals_lookup[tag] = {
                    'Weighted_Score': pair.get('weighted_score'),
                    'Composite_Score': signals.get('Composite_Score'),
                    'Sum_Dev_Value': pair.get('sum_dev_value'),
                    'Sum_Dev_CDF': pair.get('sum_dev_cdf'),
                    'Sum_Dev_Bucket': pair.get('sum_dev_bucket'),
                    'Volume_Ratio': signals.get('Volume_Ratio'),
                    'Rolling_Intraday_Vol': signals.get('Rolling_Intraday_Vol'),
                    'Volume_Dominance': signals.get('Volume_Dominance'),
                    'Last_Hour_Vol': signals.get('Last_Hour_Vol'),
                    'IV_Percentile': signals.get('IV_Percentile'),
                    'Volume_Ratio_Pct': signals.get('Volume_Ratio_Percentile'),
                    'Intraday_Vol_Pct': signals.get('Intraday_Vol_Percentile'),
                    'Volume_Dom_Pct': signals.get('Volume_Dom_Percentile'),
                    'Last_Hour_Pct': signals.get('Last_Hour_Percentile'),
                    'IV_Pct_Pct': signals.get('IV_Pct_Percentile'),
                    'Index_Bias': signals.get('Index_Bias'),
                }

            # Add columns to primary_df
            for col in ['Weighted_Score', 'Composite_Score', 'Sum_Dev_Value', 'Sum_Dev_CDF',
                       'Sum_Dev_Bucket', 'Volume_Ratio', 'Rolling_Intraday_Vol', 'Volume_Dominance',
                       'Last_Hour_Vol', 'IV_Percentile', 'Volume_Ratio_Pct', 'Intraday_Vol_Pct',
                       'Volume_Dom_Pct', 'Last_Hour_Pct', 'IV_Pct_Pct', 'Index_Bias']:
                primary_df[col] = primary_df['Tag'].map(lambda t: signals_lookup.get(t, {}).get(col))

        with pd.ExcelWriter(longlist_file, engine='openpyxl') as writer:
            primary_df.to_excel(writer, sheet_name='Longlist', index=False)
            summary_data = pd.DataFrame([{'Metric': k, 'Value': v} for k, v in summary.items()])
            summary_data.to_excel(writer, sheet_name='Summary', index=False)

        print(f"\nLonglist saved: {longlist_file}")

        if len(results['final_pairs']) > 0:
            with pd.ExcelWriter(shortlist_file, engine='openpyxl') as writer:
                secondary_df.to_excel(writer, sheet_name='Shortlist', index=False)
            print(f"Shortlist saved: {shortlist_file}")

        print("\n" + "="*100)
        print("REPORTING COMPLETE".center(100))
        print("="*100)

    except Exception as e:
        print(f"Error generating reports: {e}")
        traceback.print_exc()


async def main():
    """Entry point: connect to IBKR and run the full LAM pipeline."""
    print(f"{ACTIVE_VERSION} Live Analytics Module - SINGLE-FACTOR MODEL")
    print("Features:")
    print("  - Single-factor alpha model (no treasury)")
    print("  - Sub-sector indices from beta calibration")
    print("  - Each ticker uses its own category's index")
    print("  - Live prices appended to historical data before filtering")

    results = await run_analytics()

    if results:
        n_final = len(results.get('final_pairs', []))
        print(f"\n{n_final} pairs on final shortlist")


if __name__ == "__main__":
    asyncio.run(main())
