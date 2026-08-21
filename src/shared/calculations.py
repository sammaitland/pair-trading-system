#!/usr/bin/env python3
"""
calculations.py - Shared Calculations Module

Provides data alignment, alpha calculations, filter functions, and caching
infrastructure for both calibration and live pipelines. This module is the
single source of truth consumed by pre-filter, LAM, optimizer, and
execution workflows.

Core capabilities:
- SubsectorIndexManager: Loads and serves category-based sub-sector indices
- AlphaCache: Singleton cache for per-ticker alpha series with staleness guards
- BetaDataManager: Loads and serves beta coefficients from parameters files
- Single-factor alpha model: alpha = stock_return - (beta * subsector_return)
- Primary filter functions: 15-day alpha variance, 2-day deviation, earnings,
  spread, same-direction, nominal-direction, trending-stock, T-stat trend
- Sum deviation calculations and bucketing
- Ticker exposure calculations

STATUS: live -- deployed TODO(sam): date
"""

import os
import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
from scipy.stats import norm
import pytz
import logging

from src.shared.fetch_market_data import (
    fetch_daily_volumes_with_extrapolation,
    get_hourly_price_data
)

from src.shared import config

logger = logging.getLogger(__name__)


# ============================================================================
# SUBSECTOR INDEX MANAGER
# ============================================================================

class SubsectorIndexManager:
    """
    Manager for sub-sector indices loaded from beta calibration output.

    Uses category-based sub-sector indices for alpha calculations:
    - Each ticker is assigned to a category (e.g., Banks, Semiconductors_AI)
    - Each category has its own index (equal-weighted average of category members)
    - Alpha = stock_return - (subsector_beta * subsector_index_return)

    Data Sources:
    - {ETF}_SubSector_Beta_Analysis.xlsx files in version directory
    - "SubSector Indices" sheet: Time series of daily sub-sector returns
    - "Cluster Assignments" sheet: Ticker -> Category mapping
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._indices = {}  # {etf: {category_name: pd.Series of returns}}
        self._index_prices = {}  # {etf: {category_name: pd.Series of prices}}
        self._ticker_to_category = {}  # {ticker: category_name}
        self._ticker_to_etf = {}  # {ticker: etf_ticker}
        self._subsector_betas = {}  # {ticker: subsector_beta}
        self._loaded_etfs = set()
        self._initialized = True

    def load_from_beta_output(self, etf_ticker, beta_output_dir=None):
        """
        Load sub-sector indices from beta estimation output for a specific ETF.

        Parameters
        ----------
        etf_ticker : str
            ETF ticker (VGT, VFH, VIS, VHT, VCR)
        beta_output_dir : str, optional
            Directory containing beta output files. Defaults to version directory.

        Returns
        -------
        bool: True if loaded successfully
        """
        if etf_ticker in self._loaded_etfs:
            logger.debug(f"Sub-sector indices for {etf_ticker} already loaded")
            return True

        if beta_output_dir is None:
            beta_output_dir = config.get_version_dir()

        beta_file = os.path.join(beta_output_dir, etf_ticker, f"{etf_ticker}_SubSector_Beta_Analysis.xlsx")

        if not os.path.exists(beta_file):
            logger.warning(f"Beta output file not found: {beta_file}")
            return False

        logger.info(f"Loading sub-sector indices from: {beta_file}")

        try:
            # Load SubSector Indices sheet (time series of index prices)
            try:
                indices_df = pd.read_excel(beta_file, sheet_name='SubSector Indices', index_col=0)
                indices_df.index = pd.to_datetime(indices_df.index)
                indices_df.index.name = 'Date'

                self._index_prices[etf_ticker] = {}
                self._indices[etf_ticker] = {}

                # Store category names for cluster ID mapping
                category_names = list(indices_df.columns)

                for col in indices_df.columns:
                    # Store the raw data (these are already returns, not prices)
                    self._index_prices[etf_ticker][col] = indices_df[col]
                    # Data is ALREADY returns - do NOT call pct_change()
                    self._indices[etf_ticker][col] = indices_df[col]

                # Create cluster ID to category name mapping (0 -> first col, 1 -> second col, etc.)
                self._cluster_to_category = getattr(self, '_cluster_to_category', {})
                self._cluster_to_category[etf_ticker] = {i: name for i, name in enumerate(category_names)}

                logger.info(f"  Loaded {len(indices_df.columns)} sub-sector indices for {etf_ticker}")
                logger.info(f"    Categories: {category_names}")
                logger.info(f"    Cluster mapping: {self._cluster_to_category[etf_ticker]}")

            except Exception as e:
                logger.warning(f"  Could not load SubSector Indices sheet: {e}")
                # Try alternative sheet name
                try:
                    indices_df = pd.read_excel(beta_file, sheet_name='Subsector_Indices', index_col=0)
                    indices_df.index = pd.to_datetime(indices_df.index)
                    indices_df.index.name = 'Date'

                    self._index_prices[etf_ticker] = {}
                    self._indices[etf_ticker] = {}

                    category_names = list(indices_df.columns)

                    for col in indices_df.columns:
                        # Store the raw data (these are already returns, not prices)
                        self._index_prices[etf_ticker][col] = indices_df[col]
                        # Data is ALREADY returns - do NOT call pct_change()
                        self._indices[etf_ticker][col] = indices_df[col]

                    self._cluster_to_category = getattr(self, '_cluster_to_category', {})
                    self._cluster_to_category[etf_ticker] = {i: name for i, name in enumerate(category_names)}

                    logger.info(f"  Loaded {len(indices_df.columns)} sub-sector indices (alt sheet)")
                except:
                    logger.error(f"  Failed to load sub-sector indices for {etf_ticker}")
                    return False

            # Load Cluster Assignments sheet (ticker -> category mapping)
            try:
                assignments_df = pd.read_excel(beta_file, sheet_name='Cluster Assignments')

                # Find ticker and category columns
                ticker_col = None
                category_col = None
                beta_col = None

                for col in assignments_df.columns:
                    col_lower = str(col).lower()
                    if 'ticker' in col_lower:
                        ticker_col = col
                    elif 'category' in col_lower or 'cluster' in col_lower or 'subsector' in col_lower:
                        category_col = col
                    elif 'subsector_beta' in col_lower or 'market_beta' in col_lower:
                        beta_col = col

                if ticker_col is None or category_col is None:
                    logger.warning(f"  Could not find ticker/category columns in Cluster Assignments")
                    logger.warning(f"  Available columns: {list(assignments_df.columns)}")
                else:
                    # Get cluster-to-category mapping for this ETF
                    cluster_map = getattr(self, '_cluster_to_category', {}).get(etf_ticker, {})

                    for _, row in assignments_df.iterrows():
                        ticker = str(row[ticker_col]).strip().upper()
                        raw_category = row[category_col]

                        # Convert numeric cluster ID to category name if needed
                        if isinstance(raw_category, (int, float)) and not pd.isna(raw_category):
                            category = cluster_map.get(int(raw_category), str(int(raw_category)))
                        else:
                            category = str(raw_category).strip()

                        self._ticker_to_category[ticker] = category
                        self._ticker_to_etf[ticker] = etf_ticker

                        if beta_col and pd.notna(row[beta_col]):
                            self._subsector_betas[ticker] = float(row[beta_col])

                    logger.info(f"  Loaded {len([t for t, e in self._ticker_to_etf.items() if e == etf_ticker])} ticker assignments")

            except Exception as e:
                logger.warning(f"  Could not load Cluster Assignments sheet: {e}")

            # Load SubSector Beta Summary sheet for per-ticker betas
            try:
                beta_summary_df = pd.read_excel(beta_file, sheet_name='SubSector Beta Summary')

                # Find ticker and beta columns
                ticker_col = None
                beta_col = None

                for col in beta_summary_df.columns:
                    col_lower = str(col).lower()
                    if col_lower == 'ticker':
                        ticker_col = col
                    elif 'subsector_beta' in col_lower:
                        beta_col = col

                if ticker_col and beta_col:
                    betas_loaded = 0
                    for _, row in beta_summary_df.iterrows():
                        ticker = str(row[ticker_col]).strip().upper()
                        if pd.notna(row[beta_col]):
                            self._subsector_betas[ticker] = float(row[beta_col])
                            betas_loaded += 1

                    logger.info(f"  Loaded {betas_loaded} subsector betas from SubSector Beta Summary")
                else:
                    logger.warning(f"  Could not find Ticker/subsector_beta columns in SubSector Beta Summary")
                    logger.warning(f"  Available columns: {list(beta_summary_df.columns)}")

            except Exception as e:
                logger.warning(f"  Could not load SubSector Beta Summary sheet: {e}")
                # Try Traditional Beta Summary as fallback for ticker info
                try:
                    summary_df = pd.read_excel(beta_file, sheet_name='Traditional Beta Summary')
                    for _, row in summary_df.iterrows():
                        if 'Ticker' in summary_df.columns:
                            ticker = str(row['Ticker']).strip().upper()
                            self._ticker_to_etf[ticker] = etf_ticker
                            if 'Category' in summary_df.columns:
                                self._ticker_to_category[ticker] = str(row['Category'])
                            if 'market_beta' in summary_df.columns:
                                self._subsector_betas[ticker] = float(row['market_beta'])
                except:
                    pass

            self._loaded_etfs.add(etf_ticker)
            return True

        except Exception as e:
            logger.error(f"Error loading beta output for {etf_ticker}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    def load_all_etfs(self, beta_output_dir=None):
        """Load sub-sector indices for all ETFs"""
        etfs = ['VGT', 'VFH', 'VIS', 'VHT', 'VCR']
        loaded = 0

        for etf in etfs:
            if self.load_from_beta_output(etf, beta_output_dir):
                loaded += 1

        logger.info(f"Loaded sub-sector indices for {loaded}/{len(etfs)} ETFs")
        return loaded

    def get_subsector_returns(self, ticker, date_index=None):
        """
        Get sub-sector index returns for a ticker's category.

        Parameters
        ----------
        ticker : str
            Stock ticker
        date_index : pd.DatetimeIndex, optional
            If provided, aligns returns to this index

        Returns
        -------
        pd.Series: Sub-sector index returns, or None if not available
        """
        ticker = ticker.upper()

        if ticker not in self._ticker_to_category:
            logger.warning(f"Ticker {ticker} not found in category assignments")
            return None

        category = self._ticker_to_category[ticker]
        etf = self._ticker_to_etf.get(ticker)

        if etf is None or etf not in self._indices:
            logger.warning(f"ETF data not loaded for ticker {ticker}")
            return None

        if category not in self._indices[etf]:
            logger.warning(f"Category {category} not found in {etf} indices")
            return None

        returns = self._indices[etf][category]

        if date_index is not None:
            # Align to provided index
            returns = returns.reindex(date_index)

        return returns

    def get_subsector_prices(self, ticker, date_index=None):
        """
        Get sub-sector index prices for a ticker's category.

        Parameters
        ----------
        ticker : str
            Stock ticker
        date_index : pd.DatetimeIndex, optional
            If provided, aligns prices to this index

        Returns
        -------
        pd.Series: Sub-sector index prices, or None if not available
        """
        ticker = ticker.upper()

        if ticker not in self._ticker_to_category:
            return None

        category = self._ticker_to_category[ticker]
        etf = self._ticker_to_etf.get(ticker)

        if etf is None or etf not in self._index_prices:
            return None

        if category not in self._index_prices[etf]:
            return None

        prices = self._index_prices[etf][category]

        if date_index is not None:
            prices = prices.reindex(date_index)

        return prices

    def get_category(self, ticker):
        """Get category name for a ticker"""
        return self._ticker_to_category.get(ticker.upper())

    def get_etf(self, ticker):
        """Get ETF for a ticker"""
        return self._ticker_to_etf.get(ticker.upper())

    def get_subsector_beta(self, ticker):
        """Get sub-sector beta for a ticker"""
        return self._subsector_betas.get(ticker.upper(), 1.0)

    def get_categories_for_etf(self, etf_ticker):
        """Get all categories for an ETF"""
        if etf_ticker not in self._indices:
            return []
        return list(self._indices[etf_ticker].keys())

    def is_loaded(self, etf_ticker):
        """Check if an ETF's data is loaded"""
        return etf_ticker in self._loaded_etfs

    def clear_cache(self):
        """Clear all cached data"""
        self._indices = {}
        self._index_prices = {}
        self._ticker_to_category = {}
        self._ticker_to_etf = {}
        self._subsector_betas = {}
        self._loaded_etfs = set()
        logger.info("SubsectorIndexManager cache cleared")


# Global instance
_subsector_manager = SubsectorIndexManager()


def get_subsector_manager():
    """Get the global SubsectorIndexManager instance"""
    return _subsector_manager


def load_subsector_indices(etf_ticker=None, beta_output_dir=None):
    """
    Convenience function to load sub-sector indices.

    Parameters
    ----------
    etf_ticker : str, optional
        Specific ETF to load. If None, loads all ETFs.
    beta_output_dir : str, optional
        Directory containing beta output files.

    Returns
    -------
    SubsectorIndexManager: The loaded manager instance
    """
    manager = get_subsector_manager()

    if etf_ticker:
        manager.load_from_beta_output(etf_ticker, beta_output_dir)
    else:
        manager.load_all_etfs(beta_output_dir)

    return manager


# ============================================================================
# ALPHA CACHE - SINGLETON
# ============================================================================

class AlphaCache:
    """
    Centralized cache for per-ticker alpha series.

    CRITICAL: This cache will REFUSE to operate until it has verified that
    today's live prices have been appended to historical data. This prevents
    the stale data bug where filters used yesterday's close instead of live prices.

    Alpha is calculated as: stock_return - (beta * subsector_return)

    The cache stores a full alpha series per ticker (typically 365 days),
    which can then be sliced for any filter's needs:
    - 15-day alpha variance: alpha_series[-15:]
    - 2-day deviation: alpha_series[-2:]
    - Sum deviation: sum of alpha_series[-15:]
    - T-stat filter: full series for std calculation
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._cache = {}  # {ticker: pd.Series of daily alphas}
        self._today_verified = False
        self._populated = False
        self._population_timestamp = None
        self._verification_timestamp = None
        self._today_date = None
        self._verification_details = {}
        self._initialized = True

    def reset(self):
        """
        Reset the cache for a fresh run.

        Call this at the start of each pre-filter/LAM run to ensure
        we don't accidentally use stale cached data from a previous run.
        """
        self._cache = {}
        self._today_verified = False
        self._populated = False
        self._population_timestamp = None
        self._verification_timestamp = None
        self._today_date = None
        self._verification_details = {}
        logger.info("AlphaCache: Reset for new run")

    def verify_today_data_present(self, historical_data, subsector_manager,
                                     market_data=None, max_stale_minutes=30):
        """
        CRITICAL: Verify that today's live prices are in the data BEFORE allowing cache use.

        This is a MANDATORY safety check that prevents the stale data bug.

        Checks:
        1. Historical data for stocks has today's date
        2. Subsector indices have today's returns
        3. (Optional) If market_data provided, checks that live prices are recent

        Args:
            historical_data: Dict of {ticker: DataFrame}
            subsector_manager: SubsectorIndexManager instance
            market_data: Optional dict of {ticker: {live_price, ...}} for freshness check
            max_stale_minutes: Max acceptable age for live prices (default 30)

        Raises:
            ValueError: If today's data is missing (cache population blocked)

        Returns:
            bool: True if verification passes
        """
        today_utc = pd.Timestamp.now(tz='UTC').normalize()
        today_naive = pd.Timestamp.now().normalize()
        current_time = datetime.now()

        verification_results = {
            'timestamp': datetime.now(),
            'today_utc': today_utc,
            'today_naive': today_naive,
            'stocks_checked': 0,
            'stocks_with_today': 0,
            'stocks_missing_today': [],
            'subsectors_checked': 0,
            'subsectors_with_today': 0,
            'subsectors_missing_today': []
        }

        # Check 1: Sample of stock historical data has today
        sample_tickers = list(historical_data.keys())[:20]  # Check first 20

        for ticker in sample_tickers:
            hist = historical_data.get(ticker)
            if hist is None or len(hist) == 0:
                continue

            verification_results['stocks_checked'] += 1

            last_date = pd.Timestamp(hist.index[-1])
            if hasattr(last_date, 'tz') and last_date.tz is not None:
                last_date_normalized = last_date.tz_convert('UTC').normalize()
            else:
                last_date_normalized = last_date.normalize()

            # Allow for timezone differences - check if within 1 day
            if hasattr(last_date_normalized, 'tz') and last_date_normalized.tz is not None:
                gap = abs((today_utc - last_date_normalized).days)
            else:
                gap = abs((today_naive - last_date_normalized).days)

            if gap <= 1:  # Today or yesterday (for timezone edge cases on weekends)
                verification_results['stocks_with_today'] += 1
            else:
                verification_results['stocks_missing_today'].append(
                    f"{ticker}: last={last_date_normalized.strftime('%Y-%m-%d')} ({gap} days ago)"
                )

        # Check 2: Subsector indices have today
        for etf in subsector_manager._indices.keys():
            for category, returns_series in subsector_manager._indices[etf].items():
                if returns_series is None or len(returns_series) == 0:
                    continue

                verification_results['subsectors_checked'] += 1

                last_date = pd.Timestamp(returns_series.index[-1])
                if hasattr(last_date, 'tz') and last_date.tz is not None:
                    last_date = last_date.tz_localize(None)

                gap = abs((today_naive - last_date.normalize()).days)

                if gap <= 1:
                    verification_results['subsectors_with_today'] += 1
                else:
                    verification_results['subsectors_missing_today'].append(
                        f"{etf}/{category}: last={last_date.strftime('%Y-%m-%d')} ({gap} days ago)"
                    )

        self._verification_details = verification_results

        # Evaluate results
        stock_coverage = (verification_results['stocks_with_today'] /
                         max(verification_results['stocks_checked'], 1))
        subsector_coverage = (verification_results['subsectors_with_today'] /
                             max(verification_results['subsectors_checked'], 1))

        # Require at least 80% coverage for stocks and 90% for subsectors
        if stock_coverage < 0.80:
            missing_sample = verification_results['stocks_missing_today'][:5]
            raise ValueError(
                f"ALPHA CACHE BLOCKED: Only {stock_coverage:.0%} of stocks have today's data!\n"
                f"  Missing examples: {missing_sample}\n"
                f"  This indicates append_today_live_prices_to_historical() was not called.\n"
                f"  Filters would use STALE DATA without this fix."
            )

        if subsector_coverage < 0.90:
            missing_sample = verification_results['subsectors_missing_today'][:5]
            raise ValueError(
                f"ALPHA CACHE BLOCKED: Only {subsector_coverage:.0%} of subsector indices have today's data!\n"
                f"  Missing examples: {missing_sample}\n"
                f"  This indicates append_today_subsector_returns() was not called.\n"
                f"  Filters would use STALE DATA without this fix."
            )

        self._today_verified = True
        self._today_date = today_naive
        self._verification_timestamp = current_time

        print(f"ALPHA CACHE: Today's data verified at {current_time.strftime('%H:%M:%S')}")
        print(f"   Stocks: {verification_results['stocks_with_today']}/{verification_results['stocks_checked']} have today")
        print(f"   Subsectors: {verification_results['subsectors_with_today']}/{verification_results['subsectors_checked']} have today")

        return True

    def get_data_freshness_minutes(self):
        """
        Get how many minutes old the cached data is.

        Returns:
            float: Minutes since cache was populated, or None if not populated
        """
        if not self._population_timestamp:
            return None
        return (datetime.now() - self._population_timestamp).total_seconds() / 60

    def warn_if_stale(self, max_stale_minutes=30):
        """
        Print a warning if the cache data is older than the threshold.

        This should be called at the start of LAM to warn if it's using
        pre-filter's stale cache instead of repopulating.

        Args:
            max_stale_minutes: Threshold for staleness warning (default 30)

        Returns:
            bool: True if data is fresh enough, False if stale
        """
        if not self._populated:
            return True  # Not populated yet, no staleness concern

        freshness = self.get_data_freshness_minutes()

        if freshness is None:
            return True

        if freshness > max_stale_minutes:
            print(f"\n  WARNING: ALPHA CACHE DATA IS {freshness:.1f} MINUTES OLD!")
            print(f"   Cache was populated at: {self._population_timestamp.strftime('%H:%M:%S')}")
            print(f"   Current time: {datetime.now().strftime('%H:%M:%S')}")
            print(f"   This may indicate LAM is using pre-filter's stale cache.")
            print(f"   Call reset_alpha_cache() at start of LAM to force fresh data.\n")
            return False
        else:
            print(f"ALPHA CACHE: Data is {freshness:.1f} minutes old (within {max_stale_minutes} min threshold)")
            return True

    def populate(self, tickers, historical_data, subsector_manager, lookback_days=365):
        """
        Populate cache with alpha series for all tickers.

        CRITICAL: This method REFUSES to run unless verify_today_data_present()
        has confirmed that today's live data is in the historical data.

        Args:
            tickers: List of tickers to calculate alpha for
            historical_data: Dict of {ticker: DataFrame with 'close' column}
            subsector_manager: SubsectorIndexManager instance
            lookback_days: How many days of alpha to calculate (default 365)

        Returns:
            int: Number of tickers successfully cached
        """
        # CRITICAL: Verify today's data first
        if not self._today_verified:
            print("  ALPHA CACHE: Running verification before population...")
            self.verify_today_data_present(historical_data, subsector_manager)

        print(f"\nALPHA CACHE: Populating cache for {len(tickers)} tickers...")

        successful = 0
        skipped_no_hist = 0
        skipped_no_beta = 0
        skipped_no_subsector = 0
        skipped_insufficient = 0

        for ticker in tickers:
            ticker = ticker.upper()

            # Skip if already cached
            if ticker in self._cache:
                successful += 1
                continue

            # Get historical data
            hist = historical_data.get(ticker)
            if hist is None or len(hist) < 2:
                skipped_no_hist += 1
                continue

            # Get subsector beta
            beta = subsector_manager.get_subsector_beta(ticker)
            if beta is None:
                skipped_no_beta += 1
                continue

            # Get subsector returns
            subsector_returns = subsector_manager.get_subsector_returns(ticker)
            if subsector_returns is None or len(subsector_returns) < 2:
                skipped_no_subsector += 1
                continue

            # Normalize indices for alignment
            hist_index = hist.index
            if hasattr(hist_index[0], 'tz') and hist_index[0].tz is not None:
                hist_index = hist_index.tz_localize(None)

            subsector_index = subsector_returns.index
            if hasattr(subsector_index[0], 'tz') and subsector_index[0].tz is not None:
                subsector_index = subsector_index.tz_localize(None)

            # Find common dates
            hist_dates = pd.DatetimeIndex([d.normalize() if hasattr(d, 'normalize') else pd.Timestamp(d).normalize()
                                          for d in hist_index])
            subsector_dates = pd.DatetimeIndex([d.normalize() if hasattr(d, 'normalize') else pd.Timestamp(d).normalize()
                                               for d in subsector_index])

            common_dates = hist_dates.intersection(subsector_dates)

            if len(common_dates) < 2:
                skipped_insufficient += 1
                continue

            # Limit to lookback period
            if len(common_dates) > lookback_days:
                common_dates = common_dates[-lookback_days:]

            # Calculate alpha series
            alphas = []
            dates = []

            # Create aligned data
            hist_reindexed = hist.copy()
            hist_reindexed.index = hist_dates

            subsector_reindexed = subsector_returns.copy()
            subsector_reindexed.index = subsector_dates

            for i in range(1, len(common_dates)):
                current_date = common_dates[i]
                prev_date = common_dates[i-1]

                try:
                    # Stock return (from close prices)
                    current_price = hist_reindexed.loc[current_date, 'close']
                    prev_price = hist_reindexed.loc[prev_date, 'close']

                    if pd.isna(current_price) or pd.isna(prev_price) or prev_price == 0:
                        continue

                    stock_return = (current_price - prev_price) / prev_price

                    # Subsector return (already a return, not a price)
                    subsector_return = subsector_reindexed.loc[current_date]

                    if pd.isna(subsector_return):
                        subsector_return = 0.0

                    # Calculate alpha
                    alpha = stock_return - (beta * subsector_return)

                    alphas.append(alpha)
                    dates.append(current_date)

                except (KeyError, TypeError):
                    continue

            if len(alphas) >= 15:  # Need at least 15 days for filters
                self._cache[ticker] = pd.Series(alphas, index=dates)
                successful += 1
            else:
                skipped_insufficient += 1

        self._populated = True
        self._population_timestamp = datetime.now()

        print(f"ALPHA CACHE: Population complete at {self._population_timestamp.strftime('%H:%M:%S')}")
        print(f"   Cached: {successful} tickers")
        print(f"   Skipped - no historical: {skipped_no_hist}")
        print(f"   Skipped - no beta: {skipped_no_beta}")
        print(f"   Skipped - no subsector: {skipped_no_subsector}")
        print(f"   Skipped - insufficient data: {skipped_insufficient}")

        return successful

    def get_ticker_alpha(self, ticker, last_n_days=None):
        """
        Get alpha series for a single ticker.

        Args:
            ticker: Stock ticker
            last_n_days: If provided, return only the last N days

        Returns:
            pd.Series: Alpha series, or None if not cached

        Raises:
            ValueError: If cache not populated
        """
        if not self._populated:
            raise ValueError(
                "ALPHA CACHE ERROR: Cache not populated!\n"
                "Call alpha_cache.populate() AFTER appending today's live prices."
            )

        ticker = ticker.upper()

        if ticker not in self._cache:
            return None

        series = self._cache[ticker]

        if last_n_days is not None and len(series) >= last_n_days:
            return series.iloc[-last_n_days:]

        return series

    def get_pair_net_alpha(self, ticker1, ticker2, last_n_days=15):
        """
        Get NET alpha (alpha1 - alpha2) for a pair, aligned and sliced.

        This is what the 15-day and 2-day filters need.

        Args:
            ticker1: First ticker
            ticker2: Second ticker
            last_n_days: How many days to return (default 15)

        Returns:
            Tuple: (net_alpha_series, details_dict) or (None, error_dict)
        """
        if not self._populated:
            raise ValueError("ALPHA CACHE ERROR: Cache not populated!")

        alpha1 = self.get_ticker_alpha(ticker1)
        alpha2 = self.get_ticker_alpha(ticker2)

        if alpha1 is None:
            return None, {'error': f'{ticker1} not in cache'}
        if alpha2 is None:
            return None, {'error': f'{ticker2} not in cache'}

        # Align indices
        common_dates = alpha1.index.intersection(alpha2.index)

        if len(common_dates) < last_n_days:
            return None, {'error': f'Insufficient common dates ({len(common_dates)} < {last_n_days})'}

        # Get last N days
        common_dates = common_dates[-last_n_days:]

        alpha1_aligned = alpha1.loc[common_dates]
        alpha2_aligned = alpha2.loc[common_dates]

        net_alpha = alpha1_aligned - alpha2_aligned

        details = {
            'ticker1': ticker1,
            'ticker2': ticker2,
            'days': len(net_alpha),
            'alpha1_sum': alpha1_aligned.sum(),
            'alpha2_sum': alpha2_aligned.sum(),
            'net_alpha_sum': net_alpha.sum(),
            'last_date': common_dates[-1],
            'from_cache': True
        }

        return net_alpha, details

    def get_pair_sum_alpha(self, ticker1, ticker2, last_n_days=15):
        """
        Get SUM alpha (alpha1 + alpha2) for a pair.

        This is what sum deviation calculation needs.

        Args:
            ticker1: First ticker
            ticker2: Second ticker
            last_n_days: How many days to sum (default 15)

        Returns:
            Tuple: (sum_alpha_value, details_dict) or (None, error_dict)
        """
        if not self._populated:
            raise ValueError("ALPHA CACHE ERROR: Cache not populated!")

        alpha1 = self.get_ticker_alpha(ticker1)
        alpha2 = self.get_ticker_alpha(ticker2)

        if alpha1 is None:
            return None, {'error': f'{ticker1} not in cache'}
        if alpha2 is None:
            return None, {'error': f'{ticker2} not in cache'}

        # Align indices
        common_dates = alpha1.index.intersection(alpha2.index)

        if len(common_dates) < last_n_days:
            return None, {'error': f'Insufficient common dates ({len(common_dates)} < {last_n_days})'}

        # Get last N days
        common_dates = common_dates[-last_n_days:]

        alpha1_aligned = alpha1.loc[common_dates]
        alpha2_aligned = alpha2.loc[common_dates]

        sum_alpha = (alpha1_aligned + alpha2_aligned).sum()

        details = {
            'ticker1': ticker1,
            'ticker2': ticker2,
            'days': last_n_days,
            'alpha1_sum': alpha1_aligned.sum(),
            'alpha2_sum': alpha2_aligned.sum(),
            'sum_alpha': sum_alpha,
            'from_cache': True
        }

        return sum_alpha, details

    def is_populated(self):
        """Check if cache is populated and ready for use"""
        return self._populated

    def is_today_verified(self):
        """Check if today's data has been verified"""
        return self._today_verified

    def get_cache_stats(self):
        """Get statistics about the cache"""
        freshness = self.get_data_freshness_minutes()
        return {
            'populated': self._populated,
            'today_verified': self._today_verified,
            'today_date': self._today_date,
            'verification_timestamp': self._verification_timestamp,
            'population_timestamp': self._population_timestamp,
            'data_freshness_minutes': freshness,
            'tickers_cached': len(self._cache),
            'verification_details': self._verification_details
        }

    def __contains__(self, ticker):
        """Allow 'ticker in alpha_cache' syntax"""
        return ticker.upper() in self._cache

    def __len__(self):
        """Return number of cached tickers"""
        return len(self._cache)


# Singleton accessor
_alpha_cache_instance = None


def get_alpha_cache():
    """Get the singleton AlphaCache instance"""
    global _alpha_cache_instance
    if _alpha_cache_instance is None:
        _alpha_cache_instance = AlphaCache()
    return _alpha_cache_instance


def reset_alpha_cache():
    """Reset the alpha cache for a new run"""
    cache = get_alpha_cache()
    cache.reset()
    return cache


def calculate_sum_deviation_cached(ticker1, ticker2, alpha_cache, global_sum_dev_std):
    """
    Calculate sum deviation using cached alpha values.

    Sum deviation = sum(alpha1 + alpha2) over 15 days

    Args:
        ticker1: First ticker
        ticker2: Second ticker
        alpha_cache: Populated AlphaCache instance
        global_sum_dev_std: Standard deviation for CDF calculation

    Returns:
        Tuple: (sum_dev_value, sum_dev_cdf, sum_dev_bucket) or (nan, nan, None)
    """
    if alpha_cache is None or not alpha_cache.is_populated():
        return np.nan, np.nan, None

    sum_alpha, details = alpha_cache.get_pair_sum_alpha(ticker1, ticker2, last_n_days=15)

    if sum_alpha is None:
        return np.nan, np.nan, None

    # Calculate CDF
    sum_dev_cdf = norm.cdf(sum_alpha, loc=0, scale=global_sum_dev_std)

    # Assign bucket
    sum_dev_bucket = assign_sum_dev_bucket(sum_dev_cdf * 100)

    return sum_alpha, sum_dev_cdf, sum_dev_bucket


# ============================================================================
# BETA DATA MANAGEMENT
# ============================================================================

class BetaDataManager:
    """
    Singleton manager for beta coefficients.

    Beta Framework:
    - SubSector_Beta: Ticker vs its assigned sector index (VGT/VFH/VIS/VHT/VCR)
                     Used in alpha calculations to isolate stock-specific returns
    - VO_Beta: Ticker vs broad market (Vanguard Mid-Cap)
               Used in portfolio hedging to measure total market exposure
    """
    _instance = None
    _subsector_betas = None
    _vo_betas = None
    _ticker_indices = None  # Maps ticker -> its assigned index (VGT/VFH/etc)

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def load_betas(self, parameters_file):
        """Load all beta coefficients from parameters file"""
        if self._subsector_betas is not None:
            logger.debug("Beta coefficients already loaded, skipping")
            return

        logger.info(f"Loading beta coefficients from: {parameters_file}")

        try:
            # Check file exists
            if not os.path.exists(parameters_file):
                raise FileNotFoundError(f"Parameters file not found: {parameters_file}")

            # Load from "Tickers" sheet
            logger.info("  Reading Tickers sheet...")
            df_tickers = pd.read_excel(parameters_file, sheet_name='Tickers')
            logger.info(f"  Loaded {len(df_tickers)} rows")

            # Validate required columns
            required_cols = ['Ticker', 'Index', 'SubSector_Beta', 'VO_Beta']
            logger.info(f"  Available columns: {df_tickers.columns.tolist()}")

            missing_cols = [col for col in required_cols if col not in df_tickers.columns]

            if missing_cols:
                raise ValueError(f"Missing required columns in Tickers sheet: {missing_cols}")

            # Load beta dictionaries
            self._subsector_betas = df_tickers.set_index('Ticker')['SubSector_Beta'].to_dict()
            self._vo_betas = df_tickers.set_index('Ticker')['VO_Beta'].to_dict()
            self._ticker_indices = df_tickers.set_index('Ticker')['Index'].to_dict()

            logger.info(f"  Loaded {len(self._subsector_betas)} SubSector betas")
            logger.info(f"  Loaded {len(self._vo_betas)} VO betas")
            logger.info(f"  Loaded {len(self._ticker_indices)} ticker-index mappings")

            # CRITICAL: Verify betas actually loaded
            if len(self._subsector_betas) == 0:
                raise ValueError("No betas loaded - Tickers sheet may be empty")

            # Show index distribution
            from collections import Counter
            index_counts = Counter(self._ticker_indices.values())
            logger.info("  Index distribution:")
            for index_name, count in sorted(index_counts.items()):
                logger.info(f"    {index_name}: {count} tickers")

        except Exception as e:
            logger.error(f"FATAL ERROR loading betas: {e}")
            import traceback
            logger.error(traceback.format_exc())

            # Initialize empty dictionaries (but warn loudly!)
            logger.error("BETA MANAGER FAILED - ALL BETAS WILL BE 0.0")
            self._subsector_betas = {}
            self._vo_betas = {}
            self._ticker_indices = {}

    def get_subsector_beta(self, ticker):
        """
        Get SubSector_Beta for a ticker.

        This is the ticker's beta to its assigned sector index (VGT/VFH/VIS/VHT/VCR).
        Used in alpha calculations to isolate stock-specific returns.

        Returns:
            float: SubSector beta (1.0 if not found)
        """
        if self._subsector_betas is None:
            logger.warning("Betas not loaded! Call load_betas() first")
            return 1.0

        beta = self._subsector_betas.get(ticker)
        if beta is None:
            return 1.0

        return beta

    def get_vo_beta(self, ticker):
        """
        Get VO_Beta for a ticker.

        This is the ticker's beta to broad market (Vanguard Mid-Cap).
        Used in portfolio hedging to measure total market exposure.

        Returns:
            float: VO beta (0.0 if not found)
        """
        if self._vo_betas is None:
            logger.warning("Betas not loaded! Call load_betas() first")
            return 0.0

        beta = self._vo_betas.get(ticker, 0.0)
        if beta == 0.0 and ticker not in self._vo_betas:
            logger.warning(f"VO beta not found for {ticker}, using 0.0")

        return beta

    def get_ticker_index(self, ticker):
        """
        Get the assigned sector index for a ticker.

        Returns:
            str: Index symbol (VGT, VFH, VIS, VHT, VCR) or None if not found
        """
        if self._ticker_indices is None:
            logger.warning("Indices not loaded! Call load_betas() first")
            return None

        index = self._ticker_indices.get(ticker)
        if index is None:
            logger.warning(f"Index not found for {ticker}")

        return index


_beta_manager = BetaDataManager()


def load_beta_dicts(parameters_file):
    """
    Load beta dictionaries (convenience function).

    Returns:
        dict: subsector_betas_dict
    """
    _beta_manager.load_betas(parameters_file)
    return _beta_manager._subsector_betas


def get_ticker_betas(ticker, parameters_file=None):
    """
    Get all betas for a ticker.

    Returns:
        dict: {
            'subsector_beta': float,
            'vo_beta': float,
            'index': str (VGT/VFH/VIS/VHT/VCR)
        }
    """
    if parameters_file and _beta_manager._subsector_betas is None:
        _beta_manager.load_betas(parameters_file)

    return {
        'subsector_beta': _beta_manager.get_subsector_beta(ticker),
        'vo_beta': _beta_manager.get_vo_beta(ticker),
        'index': _beta_manager.get_ticker_index(ticker)
    }


def calculate_dynamic_pair_beta(co1, co2, w1, tail, parameters_file=None, beta_type='market'):
    """
    Calculate pair beta dynamically based on weights and trade direction.

    Parameters:
        co1: First ticker symbol
        co2: Second ticker symbol
        w1: Weight for Co1
        tail: 'L' or 'U' indicating trade direction
        parameters_file: Optional parameters file path (used for vo/subsector betas)
        beta_type: Which beta to use:
            - 'market': Market beta vs parent ETF (for display/alpha calculations)
            - 'vo': VO beta vs mid-cap index (for portfolio-level market exposure)
            - 'subsector': Subsector beta (for trade selection)

    Returns:
        float: Combined pair beta
    """
    w2 = 1 - w1

    # Get betas based on type
    if beta_type == 'vo':
        # VO betas for overall market exposure (portfolio constraint, Summary Portfolio Beta)
        if parameters_file and _beta_manager._subsector_betas is None:
            _beta_manager.load_betas(parameters_file)
        beta_co1 = _beta_manager.get_vo_beta(co1)
        beta_co2 = _beta_manager.get_vo_beta(co2)
    elif beta_type == 'subsector':
        # Subsector betas (for trade selection in LAM)
        if parameters_file and _beta_manager._subsector_betas is None:
            _beta_manager.load_betas(parameters_file)
        beta_co1 = _beta_manager.get_subsector_beta(co1)
        beta_co2 = _beta_manager.get_subsector_beta(co2)
    else:  # 'market' (default)
        # Market betas vs parent ETF (for display and alpha calculations)
        beta_co1 = get_single_ticker_beta(co1, fallback=1.0)
        beta_co2 = get_single_ticker_beta(co2, fallback=1.0)

    # Calculate weighted pair beta based on trade direction
    if tail == 'L':
        # Long Co1, Short Co2
        pair_beta = w1 * beta_co1 - w2 * beta_co2
    else:  # tail == 'U'
        # Short Co1, Long Co2
        pair_beta = -w1 * beta_co1 + w2 * beta_co2

    return pair_beta


# ============================================================================
# INDIVIDUAL TICKER BETA LOADING (from SubSector_Beta_Analysis files)
# ============================================================================

_ticker_betas_cache = {}  # {ticker: market_beta}
_ticker_betas_loaded = False
_ticker_beta_warnings_shown = set()  # Track which tickers we've warned about


def load_ticker_betas_from_files():
    """
    Load individual ticker betas from SubSector_Beta_Analysis.xlsx files.

    Reads from: {config.BETA_FILES_DIR}/{INDEX}/{INDEX}_SubSector_Beta_Analysis.xlsx
    Uses the 'Traditional Beta Summary' tab, columns: Ticker, market_beta

    Returns
    -------
    dict: {ticker: market_beta} for all tickers across all indexes
    """
    global _ticker_betas_cache, _ticker_betas_loaded

    if _ticker_betas_loaded:
        return _ticker_betas_cache

    logger.info("Loading individual ticker betas from SubSector_Beta_Analysis files...")

    # Check if BETA_FILES_DIR is configured
    if not hasattr(config, 'BETA_FILES_DIR'):
        logger.warning("config.BETA_FILES_DIR not set - ticker betas unavailable")
        _ticker_betas_loaded = True
        return _ticker_betas_cache

    beta_dir = config.BETA_FILES_DIR
    if not os.path.exists(beta_dir):
        logger.warning(f"Beta files directory not found: {beta_dir}")
        _ticker_betas_loaded = True
        return _ticker_betas_cache

    # Load betas for each index
    indexes_loaded = 0
    tickers_loaded = 0

    for index in config.INDEX_ETFS:
        file_path = os.path.join(beta_dir, index, f"{index}_SubSector_Beta_Analysis.xlsx")

        if not os.path.exists(file_path):
            logger.warning(f"Beta file not found: {file_path}")
            continue

        try:
            # Read the Traditional Beta Summary tab
            df = pd.read_excel(file_path, sheet_name='Traditional Beta Summary')

            # Find Ticker and market_beta columns
            ticker_col = None
            beta_col = None

            for col in df.columns:
                col_lower = str(col).lower().strip()
                if col_lower == 'ticker':
                    ticker_col = col
                elif col_lower == 'market_beta':
                    beta_col = col

            if ticker_col is None or beta_col is None:
                logger.warning(f"Could not find Ticker/market_beta columns in {file_path}")
                logger.warning(f"Available columns: {list(df.columns)}")
                continue

            # Load betas
            count = 0
            for _, row in df.iterrows():
                ticker = row[ticker_col]
                beta = row[beta_col]

                if pd.notna(ticker) and pd.notna(beta):
                    ticker = str(ticker).strip().upper()
                    _ticker_betas_cache[ticker] = float(beta)
                    count += 1

            indexes_loaded += 1
            tickers_loaded += count
            logger.info(f"  {index}: Loaded {count} ticker betas")

        except Exception as e:
            logger.error(f"Error loading betas from {file_path}: {e}")
            continue

    _ticker_betas_loaded = True
    logger.info(f"Loaded {tickers_loaded} ticker betas from {indexes_loaded} index files")

    return _ticker_betas_cache


def get_single_ticker_beta(ticker, fallback=1.0):
    """
    Get the market beta for a specific ticker.

    Parameters
    ----------
    ticker : str
        Stock ticker symbol
    fallback : float
        Value to return if ticker not found (default 1.0)

    Returns
    -------
    float: The ticker's market beta
    """
    global _ticker_betas_loaded, _ticker_beta_warnings_shown

    # Ensure betas are loaded
    if not _ticker_betas_loaded:
        load_ticker_betas_from_files()

    ticker = str(ticker).strip().upper()

    if ticker in _ticker_betas_cache:
        return _ticker_betas_cache[ticker]
    else:
        # Only warn once per ticker to avoid log spam
        if ticker not in _ticker_beta_warnings_shown:
            logger.warning(f"Ticker beta not found for {ticker} - using fallback {fallback}")
            _ticker_beta_warnings_shown.add(ticker)
        return fallback


def get_all_ticker_betas():
    """
    Get all cached ticker betas, loading from files if necessary.

    Returns
    -------
    dict: {ticker: market_beta} for all loaded tickers
    """
    global _ticker_betas_loaded

    if not _ticker_betas_loaded:
        load_ticker_betas_from_files()
    return _ticker_betas_cache


def clear_ticker_betas_cache():
    """Clear the ticker betas cache (call if beta files are updated)"""
    global _ticker_betas_cache, _ticker_betas_loaded, _ticker_beta_warnings_shown
    _ticker_betas_cache = {}
    _ticker_betas_loaded = False
    _ticker_beta_warnings_shown = set()
    logger.info("Ticker betas cache cleared")


# ============================================================================
# SUM DEVIATION CALCULATIONS
# ============================================================================

BUCKET_BOUNDARIES = [
    (0, 10, '0-10%'),
    (10, 20, '10-20%'),
    (20, 30, '20-30%'),
    (30, 40, '30-40%'),
    (40, 50, '40-50%'),
    (50, 60, '50-60%'),
    (60, 70, '60-70%'),
    (70, 80, '70-80%'),
    (80, 90, '80-90%'),
    (90, 100, '90-100%'),
]


def calculate_sum_deviation(alpha1, alpha2, window=15, method='forward'):
    """
    Calculate sum deviation between two alpha time series.
    SINGLE SOURCE OF TRUTH for sum deviation calculations.

    Parameters:
        alpha1: Alpha time series for stock 1
        alpha2: Alpha time series for stock 2
        window: Rolling window size (default 15 days)
        method: 'forward' (default) or 'backward'

    Returns:
        pd.Series: Sum deviation values (alpha1_sum + alpha2_sum)
    """
    if method == 'forward':
        sum1 = alpha1.shift(-window).rolling(window).sum()
        sum2 = alpha2.shift(-window).rolling(window).sum()
    elif method == 'backward':
        sum1 = alpha1.rolling(window).sum()
        sum2 = alpha2.rolling(window).sum()
    else:
        raise ValueError(f"Unknown method: {method}. Use 'forward' or 'backward'")

    sum_deviation = sum1 + sum2

    logger.debug(f"Calculated sum deviation: window={window}, method={method}, "
                f"valid_count={sum_deviation.notna().sum()}")

    return sum_deviation


def assign_sum_dev_bucket(sum_deviation_pct):
    """
    Assign a sum deviation to its CDF bucket.

    Parameters
    ----------
    sum_deviation_pct : float
        Sum deviation as percentage (0-100)

    Returns
    -------
    str : Bucket label like '0-10%', '10-20%', etc.
    """
    if pd.isna(sum_deviation_pct):
        return None

    # Define bucket boundaries
    buckets = [
        (0, 10, '0-10%'),
        (10, 20, '10-20%'),
        (20, 30, '20-30%'),
        (30, 40, '30-40%'),
        (40, 50, '40-50%'),
        (50, 60, '50-60%'),
        (60, 70, '60-70%'),
        (70, 80, '70-80%'),
        (80, 90, '80-90%'),
        (90, 100, '90-100%'),
    ]

    for lower, upper, label in buckets:
        if lower <= sum_deviation_pct < upper:
            return label

    # Handle edge case: exactly 100%
    if sum_deviation_pct == 100:
        return '90-100%'

    return None


def validate_sum_dev_bucket(bucket):
    """Check if a bucket string is valid"""
    valid_buckets = [name for _, _, name in BUCKET_BOUNDARIES]
    return bucket in valid_buckets


def get_bucket_boundaries_for_bucket(bucket):
    """Get the percentile boundaries for a bucket"""
    for lower, upper, name in BUCKET_BOUNDARIES:
        if name == bucket:
            return lower, upper
    raise ValueError(f"Invalid bucket: {bucket}")


# ============================================================================
# BASIC CALCULATIONS
# ============================================================================

def calculate_percentage_change(initial_price, current_price):
    """
    Calculate percentage change between prices.

    Parameters:
        initial_price: Starting price
        current_price: Ending price

    Returns:
        float: Percentage change (as decimal, e.g., 0.05 for 5%)
    """
    if initial_price == 0 or pd.isna(initial_price) or pd.isna(current_price):
        logger.warning("Invalid price for percentage change calculation")
        return 0.0
    return ((current_price - initial_price) / initial_price)


def calculate_trading_days(start_date, end_date):
    """
    Calculate number of trading days between two dates.

    Parameters:
        start_date: Start date
        end_date: End date

    Returns:
        int: Number of trading days
    """
    return len(pd.bdate_range(start=start_date, end=end_date))


# ============================================================================
# CORE ALPHA CALCULATIONS
# ============================================================================

def calculate_daily_return(price_series):
    """
    Calculate daily return from price series.

    Args:
        price_series: Series with at least 2 prices

    Returns:
        Float: Daily return, or 0.0 if insufficient data
    """
    if len(price_series) < 2:
        return 0.0

    prev_price = price_series.iloc[-2]
    curr_price = price_series.iloc[-1]

    if prev_price == 0 or pd.isna(prev_price) or pd.isna(curr_price):
        return 0.0

    return (curr_price / prev_price) - 1


def calculate_stock_alpha_v92(stock_return, subsector_index_return, beta_subsector):
    """
    Calculate single stock alpha using single-factor model.

    Formula:
        alpha = stock_return - (beta_subsector * subsector_index_return)

    Args:
        stock_return: Daily stock return
        subsector_index_return: Daily sub-sector index return (category-specific)
        beta_subsector: Stock's beta to its sub-sector index

    Returns:
        Float: Alpha value
    """
    if pd.isna(stock_return) or pd.isna(subsector_index_return) or pd.isna(beta_subsector):
        return 0.0

    return stock_return - (beta_subsector * subsector_index_return)


# ============================================================================
# SHORT-TERM TREND FILTER (Co1 T-stat)
# ============================================================================

def calculate_alpha_trend_tstat(alpha_series, as_of_date=None, lookback_days=15):
    """
    Calculate T-statistic of cumulative alpha trend over lookback period.

    This measures how strongly a stock's alpha is trending. High absolute
    T-stats indicate the stock is in a momentum phase rather than mean-reverting.

    Returns SIGNED T-stat:
    - Positive = upward trend (alpha rising)
    - Negative = downward trend (alpha falling)

    Parameters
    ----------
    alpha_series : pd.Series
        Daily alpha values for a single ticker, indexed by date
    as_of_date : datetime or str, optional
        Calculate T-stat up to this date. If None, uses all available data.
    lookback_days : int
        Number of trading days to look back (default 15)

    Returns
    -------
    float or None
        Signed T-statistic, or None if insufficient data
    """
    from scipy import stats as scipy_stats

    if alpha_series is None or len(alpha_series) == 0:
        return None

    # Filter to as_of_date if provided
    if as_of_date is not None:
        if isinstance(as_of_date, str):
            as_of_date = pd.to_datetime(as_of_date)

        # Use .date() for comparison to avoid timezone issues
        as_of_date_only = as_of_date.date() if hasattr(as_of_date, 'date') else pd.Timestamp(as_of_date).date()

        # Create mask using date comparison
        date_mask = pd.Series([d.date() <= as_of_date_only for d in alpha_series.index], index=alpha_series.index)
        available = alpha_series[date_mask].dropna()
    else:
        available = alpha_series.dropna()

    if len(available) < lookback_days:
        return None

    # Get the lookback period daily alphas
    lookback_data = available.iloc[-lookback_days:]

    # Convert to cumulative (price level proxy)
    cum_alpha = lookback_data.cumsum().values

    # Use scipy.stats.linregress for T-stat
    x = np.arange(len(cum_alpha))

    try:
        result = scipy_stats.linregress(x, cum_alpha)

        if result.stderr > 0:
            # Return SIGNED t-stat (positive = uptrend, negative = downtrend)
            t_stat = result.slope / result.stderr
        else:
            t_stat = 0.0

        return t_stat

    except Exception:
        return None


def check_trend_filter(alpha_series, as_of_date=None, threshold=8.0, lookback_days=15):
    """
    Check if a ticker passes the short-term trend filter.

    Returns True if the ticker should be INCLUDED in trading (i.e., NOT in a strong trend).
    Returns False if the ticker should be EXCLUDED (i.e., IS in a strong trend).

    Parameters
    ----------
    alpha_series : pd.Series
        Daily alpha values for a single ticker, indexed by date
    as_of_date : datetime or str, optional
        Check trend as of this date
    threshold : float
        T-stat threshold (default 8.0). Exclude if |T-stat| > threshold.
    lookback_days : int
        Number of trading days for trend calculation (default 15)

    Returns
    -------
    tuple: (passes_filter: bool, tstat: float or None)
        - passes_filter: True if ticker is NOT trending (safe to trade)
        - tstat: The calculated T-statistic value
    """
    tstat = calculate_alpha_trend_tstat(alpha_series, as_of_date, lookback_days)

    if tstat is None:
        # Insufficient data - allow the trade but return None for tstat
        return True, None

    passes = abs(tstat) <= threshold
    return passes, tstat


def calculate_trend_tstat_for_ticker(ticker, alpha_data, as_of_date, lookback_days=15):
    """
    Convenience function to calculate trend T-stat for a ticker from alpha DataFrame.

    Parameters
    ----------
    ticker : str
        Ticker symbol
    alpha_data : pd.DataFrame
        DataFrame with tickers as columns and dates as index
    as_of_date : datetime or str
        Calculate T-stat up to this date
    lookback_days : int
        Number of trading days to look back

    Returns
    -------
    float or None
        Signed T-statistic, or None if ticker not found or insufficient data
    """
    if alpha_data is None or ticker not in alpha_data.columns:
        return None

    return calculate_alpha_trend_tstat(alpha_data[ticker], as_of_date, lookback_days)


# ============================================================================
# LIVE ALPHA RETURN CALCULATION
# ============================================================================

def calculate_live_alpha_return_v92(
    ticker1, ticker2,
    initial_price_co1, initial_price_co2,
    current_price_co1, current_price_co2,
    initial_subsector_price_co1, current_subsector_price_co1,
    initial_subsector_price_co2, current_subsector_price_co2,
    beta_co1, beta_co2,
    weight_co1, weight_co2,
    direction_co1, direction_co2
):
    """
    Calculate live alpha return for a pair trade using single-factor model.

    Each ticker uses its own sub-sector index for alpha calculation.
    This handles cross-category pairs correctly (e.g., Banks vs Asset_Management in VFH).

    Parameters:
        ticker1, ticker2: Ticker symbols
        initial_price_co1, initial_price_co2: Prices at initiation
        current_price_co1, current_price_co2: Current prices
        initial_subsector_price_co1, current_subsector_price_co1: Sub-sector index prices for ticker1
        initial_subsector_price_co2, current_subsector_price_co2: Sub-sector index prices for ticker2
        beta_co1, beta_co2: Sub-sector betas for each ticker
        weight_co1, weight_co2: Weights for each ticker
        direction_co1, direction_co2: Direction for each ticker (1 for long, -1 for short)

    Returns:
        float: Alpha return percentage
    """
    # Calculate raw returns
    co1_raw_return = calculate_percentage_change(initial_price_co1, current_price_co1)
    co2_raw_return = calculate_percentage_change(initial_price_co2, current_price_co2)

    # Calculate sub-sector index returns (each ticker uses its own index)
    subsector_return_co1 = calculate_percentage_change(initial_subsector_price_co1, current_subsector_price_co1)
    subsector_return_co2 = calculate_percentage_change(initial_subsector_price_co2, current_subsector_price_co2)

    # Calculate expected returns (single-factor model)
    expected_return_co1 = beta_co1 * subsector_return_co1
    expected_return_co2 = beta_co2 * subsector_return_co2

    # Calculate alphas
    alpha_co1 = co1_raw_return - expected_return_co1
    alpha_co2 = co2_raw_return - expected_return_co2

    # Apply direction and weights
    directional_alpha_co1 = direction_co1 * alpha_co1
    directional_alpha_co2 = direction_co2 * alpha_co2

    weighted_alpha_co1 = weight_co1 * directional_alpha_co1
    weighted_alpha_co2 = weight_co2 * directional_alpha_co2

    total_alpha = (weighted_alpha_co1 + weighted_alpha_co2) * 100

    return total_alpha


# ============================================================================
# DATA ALIGNMENT
# ============================================================================

def align_historical_data_v92(ticker1, ticker2, hist1, hist2, subsector_manager=None):
    """
    Align historical data using sub-sector indices.

    Each ticker uses its own sub-sector index.
    This function retrieves the appropriate sub-sector index for each ticker.

    Args:
        ticker1, ticker2: Ticker symbols
        hist1, hist2: Historical price DataFrames with 'close' column
        subsector_manager: SubsectorIndexManager instance (uses global if None)

    Returns:
        DataFrame with aligned data indexed by date:
        - ticker1: Ticker 1 prices
        - ticker2: Ticker 2 prices
        - subsector1: Sub-sector index prices for ticker1
        - subsector2: Sub-sector index prices for ticker2
    """
    if subsector_manager is None:
        subsector_manager = get_subsector_manager()

    try:
        hist1_norm = hist1.copy()
        hist2_norm = hist2.copy()

        # Normalize dates and strip timezone for comparison
        hist1_norm.index = pd.to_datetime(hist1_norm.index).normalize()
        hist2_norm.index = pd.to_datetime(hist2_norm.index).normalize()

        # Strip timezone if present (IBKR returns UTC-aware timestamps)
        if hist1_norm.index.tz is not None:
            hist1_norm.index = hist1_norm.index.tz_localize(None)
        if hist2_norm.index.tz is not None:
            hist2_norm.index = hist2_norm.index.tz_localize(None)

        # Get sub-sector index prices for each ticker
        subsector1_prices = subsector_manager.get_subsector_prices(ticker1)
        subsector2_prices = subsector_manager.get_subsector_prices(ticker2)

        if subsector1_prices is None:
            logger.warning(f"Sub-sector prices not available for {ticker1}")
            return pd.DataFrame()

        if subsector2_prices is None:
            logger.warning(f"Sub-sector prices not available for {ticker2}")
            return pd.DataFrame()

        subsector1_norm = subsector1_prices.copy()
        subsector2_norm = subsector2_prices.copy()

        # Normalize subsector dates (should already be tz-naive but be safe)
        subsector1_norm.index = pd.to_datetime(subsector1_norm.index).normalize()
        subsector2_norm.index = pd.to_datetime(subsector2_norm.index).normalize()

        if subsector1_norm.index.tz is not None:
            subsector1_norm.index = subsector1_norm.index.tz_localize(None)
        if subsector2_norm.index.tz is not None:
            subsector2_norm.index = subsector2_norm.index.tz_localize(None)

        # Find common dates
        common_index = hist1_norm.index
        for data in [hist2_norm, subsector1_norm, subsector2_norm]:
            common_index = common_index.intersection(data.index)

        if len(common_index) == 0:
            logger.warning(f"No common dates for {ticker1}/{ticker2}")
            return pd.DataFrame()

        aligned = pd.DataFrame(index=common_index)
        aligned['ticker1'] = hist1_norm.loc[common_index, 'close']
        aligned['ticker2'] = hist2_norm.loc[common_index, 'close']
        aligned['subsector1'] = subsector1_norm.loc[common_index]
        aligned['subsector2'] = subsector2_norm.loc[common_index]

        aligned.attrs['ticker1_name'] = ticker1
        aligned.attrs['ticker2_name'] = ticker2
        aligned.attrs['category1'] = subsector_manager.get_category(ticker1)
        aligned.attrs['category2'] = subsector_manager.get_category(ticker2)

        return aligned

    except Exception as e:
        logger.error(f"Error aligning historical data: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return pd.DataFrame()


# ============================================================================
# NET ALPHA SERIES CALCULATION
# ============================================================================

def calculate_net_alpha_series_v92(aligned_data, beta1_subsector, beta2_subsector,
                                    earnings_dates=None, ticker1=None, ticker2=None):
    """
    Calculate daily NET alpha series (alpha1 - alpha2) using single-factor model.

    This is the DIFFERENCE between individual stock alphas, used for:
    - 15-day alpha variance hurdle (primary filter)
    - 2-day deviation hurdle (primary filter)
    - Tail assignment (which stock is outperforming)

    NOT to be confused with SUM deviation (alpha1 + alpha2) used for exclusion/sizing.

    Single-factor model: each ticker uses its own sub-sector index.

    Args:
        aligned_data: DataFrame from align_historical_data_v92() with columns:
                     ticker1, ticker2, subsector1, subsector2
        beta1_subsector: Stock 1's beta to its sub-sector index
        beta2_subsector: Stock 2's beta to its sub-sector index
        earnings_dates: Optional dict of {ticker: date} for earnings exclusion
        ticker1, ticker2: Ticker symbols (for earnings check)

    Returns:
        pd.Series: Daily net alpha (alpha1 - alpha2), zeroed on earnings dates
    """
    net_alphas = []

    for i in range(1, len(aligned_data)):
        # Calculate returns for each ticker (from prices)
        ticker1_ret = calculate_daily_return(aligned_data['ticker1'].iloc[i-1:i+1])
        ticker2_ret = calculate_daily_return(aligned_data['ticker2'].iloc[i-1:i+1])

        # Sub-sector returns - use directly (already returns, not prices!)
        subsector1_ret = aligned_data['subsector1'].iloc[i]
        subsector2_ret = aligned_data['subsector2'].iloc[i]

        # Calculate alphas using single-factor model
        alpha1 = calculate_stock_alpha_v92(ticker1_ret, subsector1_ret, beta1_subsector)
        alpha2 = calculate_stock_alpha_v92(ticker2_ret, subsector2_ret, beta2_subsector)

        # Handle earnings dates - zero the net alpha
        current_date = aligned_data.index[i].date()
        if earnings_dates and ticker1 and ticker2:
            if is_earnings_date(ticker1, current_date, earnings_dates) or \
               is_earnings_date(ticker2, current_date, earnings_dates):
                net_alpha = 0.0
            else:
                net_alpha = alpha1 - alpha2
        else:
            net_alpha = alpha1 - alpha2

        net_alphas.append(net_alpha)

    return pd.Series(net_alphas, index=aligned_data.index[1:])


# Backward compatibility alias
calculate_pair_alphas_v92 = calculate_net_alpha_series_v92


def calculate_15day_net_alpha_v92(aligned_data, beta1_subsector, beta2_subsector,
                                   earnings_dates=None, ticker1=None, ticker2=None):
    """
    Calculate 15-day cumulative NET alpha using single-factor model.

    This is sum(alpha1 - alpha2) over 15 days, used for the primary filter hurdle.
    NOT to be confused with SUM deviation (alpha1 + alpha2) used for exclusion/sizing.

    Returns:
        Float: 15-day cumulative net alpha (for comparison against CDF threshold)
    """
    if len(aligned_data) < 16:
        return None

    net_alphas = calculate_net_alpha_series_v92(
        aligned_data, beta1_subsector, beta2_subsector,
        earnings_dates, ticker1, ticker2
    )

    if len(net_alphas) < 15:
        return None

    return net_alphas.iloc[-15:].sum()


# Backward compatibility alias
calculate_15day_alpha_sum_v92 = calculate_15day_net_alpha_v92


def calculate_2day_net_alpha_v92(aligned_data, beta1_subsector, beta2_subsector):
    """
    Calculate 2-day cumulative NET alpha using single-factor model.

    This is sum(alpha1 - alpha2) over 2 days, used for the primary filter hurdle.
    NOT to be confused with SUM deviation (alpha1 + alpha2) used for exclusion/sizing.

    Returns:
        Float: 2-day cumulative net alpha (for comparison against EMA threshold)
    """
    if len(aligned_data) < 3:
        return None

    net_alphas = calculate_net_alpha_series_v92(
        aligned_data, beta1_subsector, beta2_subsector
    )

    if len(net_alphas) < 2:
        return None

    return net_alphas.iloc[-2:].sum()


# Backward compatibility alias
calculate_2day_alpha_sum_v92 = calculate_2day_net_alpha_v92


def calculate_sum_deviation_v92(aligned_data, beta1_subsector, beta2_subsector,
                                 earnings_dates=None, ticker1=None, ticker2=None,
                                 lookback_days=15):
    """
    Calculate SUM deviation (alpha1 + alpha2) using single-factor model.

    This measures how much the PAIR as a whole deviates from its expected return.
    Used for:
    - Exclusion zone filtering (neutral zone = low conviction)
    - Position sizing (higher deviation = higher conviction)

    DIFFERENT from NET alpha (alpha1 - alpha2) used in primary hurdles.

    SUM deviation interpretation:
    - High positive: Both stocks outperforming their indices (pair-wide strength)
    - High negative: Both stocks underperforming their indices (pair-wide weakness)
    - Near zero: Alphas offsetting or both tracking indices (low conviction)

    Single-factor model: each ticker uses its own sub-sector index.
    Earnings dates are zeroed to avoid distortion.

    Args:
        aligned_data: DataFrame from align_historical_data_v92() with columns:
                     ticker1, ticker2, subsector1, subsector2
        beta1_subsector: Stock 1's beta to its sub-sector index
        beta2_subsector: Stock 2's beta to its sub-sector index
        earnings_dates: Optional dict of {ticker: date} for earnings exclusion
        ticker1, ticker2: Ticker symbols (for earnings check)
        lookback_days: Number of days to sum over (default 15)

    Returns:
        Float: Sum deviation value (alpha1_sum + alpha2_sum), or np.nan if insufficient data
    """
    if len(aligned_data) < lookback_days + 1:
        return np.nan

    alpha1_sum = 0.0
    alpha2_sum = 0.0
    valid_days = 0

    # Calculate over the lookback window (last N days)
    start_idx = max(1, len(aligned_data) - lookback_days)

    for i in range(start_idx, len(aligned_data)):
        current_date = aligned_data.index[i].date() if hasattr(aligned_data.index[i], 'date') else aligned_data.index[i]

        # Zero alphas on earnings dates
        if earnings_dates and ticker1 and ticker2:
            if is_earnings_date(ticker1, current_date, earnings_dates) or \
               is_earnings_date(ticker2, current_date, earnings_dates):
                # Skip this day (effectively zeros the contribution)
                continue

        # Calculate returns for each ticker (from prices)
        ticker1_ret = calculate_daily_return(aligned_data['ticker1'].iloc[i-1:i+1])
        ticker2_ret = calculate_daily_return(aligned_data['ticker2'].iloc[i-1:i+1])

        # Sub-sector returns - use directly (already returns, not prices!)
        subsector1_ret = aligned_data['subsector1'].iloc[i]
        subsector2_ret = aligned_data['subsector2'].iloc[i]

        # Calculate alphas using single-factor model
        alpha1 = calculate_stock_alpha_v92(ticker1_ret, subsector1_ret, beta1_subsector)
        alpha2 = calculate_stock_alpha_v92(ticker2_ret, subsector2_ret, beta2_subsector)

        alpha1_sum += alpha1
        alpha2_sum += alpha2
        valid_days += 1

    if valid_days < lookback_days // 2:
        # Too many days excluded (e.g., earnings), return nan
        return np.nan

    # SUM deviation = alpha1 + alpha2 (ADDITION!)
    return alpha1_sum + alpha2_sum


# ============================================================================
# PORTFOLIO CALCULATIONS
# ============================================================================

def calculate_ticker_exposure(portfolio_df):
    """
    Calculate current ticker exposure across portfolio.

    Properly accounts for Tail direction.
    """
    ticker_exposures = {}

    for _, row in portfolio_df.iterrows():
        co1 = row['Co1']
        co2 = row['Co2']
        value1 = row.get('Trade Value Co1 ($)', 0)
        value2 = row.get('Trade Value Co2 ($)', 0)
        tail = row.get('Tail', 'L').strip().upper()

        # Initialize
        if co1 not in ticker_exposures:
            ticker_exposures[co1] = 0
        if co2 not in ticker_exposures:
            ticker_exposures[co2] = 0

        # Add exposure based on tail direction
        if tail == 'L':
            # Long Co1, Short Co2
            ticker_exposures[co1] += value1
            ticker_exposures[co2] -= value2
        else:  # U
            # Short Co1, Long Co2
            ticker_exposures[co1] -= value1
            ticker_exposures[co2] += value2

    return pd.Series(ticker_exposures)


# ============================================================================
# EARNINGS HELPERS
# ============================================================================

def is_earnings_date(ticker, check_date, earnings_dates):
    """
    Check if a specific date is an earnings date for a ticker.

    Args:
        ticker: Stock ticker
        check_date: Date to check (date object)
        earnings_dates: Dict of {ticker: date}

    Returns:
        Bool: True if earnings date
    """
    if ticker not in earnings_dates:
        return False

    earnings_date = earnings_dates[ticker]

    if hasattr(earnings_date, 'date'):
        earnings_date = earnings_date.date()
    elif isinstance(earnings_date, pd.Timestamp):
        earnings_date = earnings_date.date()
    elif isinstance(earnings_date, str):
        earnings_date = pd.to_datetime(earnings_date).date()

    return earnings_date == check_date


def has_upcoming_earnings(ticker, earnings_dates, days_ahead=6, days_behind=1):
    """
    Check if ticker has earnings within exclusion window.

    Window includes:
    - days_behind: Past days to check (default 1 = yesterday)
    - today
    - days_ahead: Future days to check (default 6)

    Args:
        ticker: Stock ticker
        earnings_dates: Dict of {ticker: date}
        days_ahead: Number of days to look ahead (default 6)
        days_behind: Number of days to look behind (default 1)

    Returns:
        Bool: True if earnings in exclusion window

    Note:
        earnings_dates should contain ADJUSTED dates (post-market earnings
        already shifted to next trading day with weekends handled)
    """
    if ticker not in earnings_dates:
        return False

    current_date = date.today()
    earnings_date = earnings_dates[ticker]

    # Normalize earnings_date to date object
    if hasattr(earnings_date, 'date'):
        earnings_date = earnings_date.date()
    elif isinstance(earnings_date, pd.Timestamp):
        earnings_date = earnings_date.date()
    elif isinstance(earnings_date, str):
        earnings_date = pd.to_datetime(earnings_date).date()

    # Calculate days until earnings
    days_until = (earnings_date - current_date).days

    # Check if within exclusion window
    # -1 = yesterday, 0 = today, 1-6 = next 6 days
    return -days_behind <= days_until <= days_ahead


# ============================================================================
# PRIMARY FILTERS
# ============================================================================

def check_15day_alpha_variance_v92(ticker1, ticker2, tag, hist1, hist2,
                                    cumulative_stats_df, alpha_variance_hurdles,
                                    subsector_manager=None, earnings_dates=None):
    """
    Check 15-day alpha variance filter using single-factor model.

    Returns:
        Tuple: (success: bool, cdf_value: float, details: dict)
    """
    if subsector_manager is None:
        subsector_manager = get_subsector_manager()

    try:
        pair_name = f"{ticker1}_{ticker2}"

        pair_stats = cumulative_stats_df[
            cumulative_stats_df['Pair_Name'] == pair_name
        ]

        if len(pair_stats) == 0:
            return False, 0.0, {"error": f"No stats for {pair_name}"}

        std_15day = pair_stats['Std_15Day_Cumulative'].values[0]
        mean_15day = pair_stats['Mean_15Day_Cumulative'].values[0]
        hurdle = alpha_variance_hurdles.get(tag, 0.5)

        # Get sub-sector betas
        beta1_subsector = subsector_manager.get_subsector_beta(ticker1)
        beta2_subsector = subsector_manager.get_subsector_beta(ticker2)

        # Align data using sub-sector indices
        aligned_data = align_historical_data_v92(
            ticker1, ticker2, hist1, hist2, subsector_manager
        )

        if len(aligned_data) < 16:
            return False, 0.0, {"error": "Insufficient aligned data"}

        # Calculate alpha sum using single-factor model
        alpha_sum = calculate_15day_alpha_sum_v92(
            aligned_data, beta1_subsector, beta2_subsector,
            earnings_dates, ticker1, ticker2
        )

        if alpha_sum is None:
            return False, 0.0, {"error": "Could not calculate alpha sum"}

        z_score = alpha_sum / std_15day
        cdf_value = norm.cdf(z_score)

        if hurdle > 0.5:
            success = cdf_value > hurdle
        elif hurdle < 0.5:
            success = cdf_value < hurdle
        else:
            success = False

        details = {
            "pair_name": pair_name,
            'mean': mean_15day,
            'stddev': std_15day,
            "net_alpha_sum": alpha_sum,
            "cdf_value": cdf_value,
            "hurdle": hurdle,
            "std_15day": std_15day,
            "mean_15day": mean_15day,
            "z_score": z_score,
            "success": success,
            "category1": subsector_manager.get_category(ticker1),
            "category2": subsector_manager.get_category(ticker2),
            "model": "single_factor"
        }

        return success, cdf_value, details

    except Exception as e:
        return False, 0.0, {"error": str(e)}


def check_2day_deviation_v92(ticker1, ticker2, tag, hist1, hist2,
                              ema_multipliers, subsector_manager=None):
    """
    Check 2-day deviation filter using single-factor model.

    Returns:
        Tuple: (success: bool, two_day_sum: float, details: dict)
    """
    if subsector_manager is None:
        subsector_manager = get_subsector_manager()

    try:
        ema_multiplier = ema_multipliers.get(tag, 1.0)

        # Get sub-sector betas
        beta1_subsector = subsector_manager.get_subsector_beta(ticker1)
        beta2_subsector = subsector_manager.get_subsector_beta(ticker2)

        # Align data
        aligned_data = align_historical_data_v92(
            ticker1, ticker2, hist1, hist2, subsector_manager
        )

        if len(aligned_data) < 31:
            return False, 0.0, {"error": "Insufficient aligned data"}

        # Calculate pair alphas using single-factor model
        pair_alphas = calculate_pair_alphas_v92(
            aligned_data, beta1_subsector, beta2_subsector
        )

        two_day_sum = pair_alphas.iloc[-2:].sum()

        ema_30_day = pair_alphas.abs().ewm(span=30).mean().iloc[-1]

        threshold = ema_30_day * ema_multiplier

        success = abs(two_day_sum) > threshold

        details = {
            "two_day_sum": two_day_sum,
            "ema_30day": ema_30_day,
            "threshold": threshold,
            "ema_multiplier": ema_multiplier,
            "success": success,
            "model": "single_factor"
        }

        return success, two_day_sum, details

    except Exception as e:
        return False, 0.0, {"error": str(e)}


def check_same_direction(alpha_15day_details, two_day_details):
    """
    Check if 15-day and 2-day sums have same sign.

    Returns:
        Tuple: (success: bool, details: dict)
    """
    try:
        net_15_day = alpha_15day_details.get("net_alpha_sum", 0)
        two_day_sum = two_day_details.get("two_day_sum", 0)

        same_direction = (net_15_day > 0 and two_day_sum > 0) or \
                        (net_15_day < 0 and two_day_sum < 0)

        details = {
            "net_15_day_sum": net_15_day,
            "two_day_sum": two_day_sum,
            "same_direction": same_direction
        }

        return same_direction, details

    except Exception as e:
        return False, {"error": str(e)}


def check_nominal_direction(ticker1, ticker2, tail, hist1, hist2):
    """
    Check 5-day nominal price direction confirmation.

    Confirms that nominal (non-alpha) price movement aligns with trade direction.
    This is NOT a momentum filter - it checks directional alignment.

    Lower trades: Co1 must have UNDERPERFORMED Co2 nominally (confirms reversion setup)
    Upper trades: Co1 must have OUTPERFORMED Co2 nominally (confirms reversion setup)

    Returns:
        Tuple: (success: bool, details: dict)
    """
    try:
        if len(hist1) < 6 or len(hist2) < 6:
            return False, {"error": "Insufficient price data"}

        ticker1_5d_return = (hist1['close'].iloc[-1] / hist1['close'].iloc[-6]) - 1
        ticker2_5d_return = (hist2['close'].iloc[-1] / hist2['close'].iloc[-6]) - 1

        if tail.upper() == 'L':
            success = ticker1_5d_return < ticker2_5d_return  # For reversion
            expected = f"{ticker1} should underperform {ticker2}"
        elif tail.upper() == 'U':
            success = ticker2_5d_return < ticker1_5d_return  # For reversion
            expected = f"{ticker2} should underperform {ticker1}"
        else:
            return False, {"error": f"Invalid tail: {tail}"}

        details = {
            "ticker1_5d_return": ticker1_5d_return,
            "ticker2_5d_return": ticker2_5d_return,
            "return_differential": ticker1_5d_return - ticker2_5d_return,
            "tail": tail,
            "expected_direction": expected,
            "success": success
        }

        return success, details

    except Exception as e:
        return False, {"error": str(e)}


def check_spread_hurdle(ticker1, ticker2, market_data, max_spread_pct=0.004):
    """
    Check spread filter (0.4% maximum).

    Args:
        ticker1, ticker2: Stock tickers
        market_data: Dict with live market data (bid/ask or spread)
        max_spread_pct: Maximum allowed spread (default 0.004 = 0.4%)

    Returns:
        Tuple: (success: bool, weighted_spread: float, details: dict)
    """
    try:
        data1 = market_data.get(ticker1, {})
        data2 = market_data.get(ticker2, {})

        # Try to get pre-calculated spread, otherwise calculate from bid/ask
        spread1 = data1.get('spread')
        if spread1 is None:
            bid1 = data1.get('bid')
            ask1 = data1.get('ask')
            if bid1 is not None and ask1 is not None and not pd.isna(bid1) and not pd.isna(ask1):
                mid1 = (bid1 + ask1) / 2
                if mid1 > 0:
                    spread1 = (ask1 - bid1) / mid1
                else:
                    spread1 = None
            else:
                spread1 = None

        spread2 = data2.get('spread')
        if spread2 is None:
            bid2 = data2.get('bid')
            ask2 = data2.get('ask')
            if bid2 is not None and ask2 is not None and not pd.isna(bid2) and not pd.isna(ask2):
                mid2 = (bid2 + ask2) / 2
                if mid2 > 0:
                    spread2 = (ask2 - bid2) / mid2
                else:
                    spread2 = None
            else:
                spread2 = None

        if spread1 is None or spread2 is None:
            return False, None, {"error": "Spread data not available"}

        weighted_spread = (spread1 + spread2) / 2

        success = weighted_spread <= max_spread_pct

        details = {
            "ticker1_spread": spread1,
            "ticker2_spread": spread2,
            "weighted_spread": weighted_spread,
            "max_spread_hurdle": max_spread_pct,
            "success": success
        }

        return success, weighted_spread, details

    except Exception as e:
        return False, None, {"error": str(e)}


def check_earnings_filter(ticker1, ticker2, earnings_dates,
                          days_ahead=6, days_behind=1):
    """
    Check earnings filter (no earnings in exclusion window).

    Exclusion window: yesterday + today + next N days

    Args:
        ticker1, ticker2: Stock tickers
        earnings_dates: Dict of {ticker: date}
        days_ahead: Days to look ahead (default 6)
        days_behind: Days to look behind (default 1)

    Returns:
        Tuple: (success: bool, details: dict)
    """
    try:
        ticker1_earnings = has_upcoming_earnings(
            ticker1, earnings_dates, days_ahead, days_behind
        )
        ticker2_earnings = has_upcoming_earnings(
            ticker2, earnings_dates, days_ahead, days_behind
        )

        success = not (ticker1_earnings or ticker2_earnings)

        details = {
            "ticker1_upcoming_earnings": ticker1_earnings,
            "ticker2_upcoming_earnings": ticker2_earnings,
            "window": f"[-{days_behind}, +{days_ahead}] days",
            "success": success
        }

        return success, details

    except Exception as e:
        return False, {"error": str(e)}


# ============================================================================
# TRENDING STOCK FILTER
# ============================================================================

def check_trending_stock_filter(ticker, ticker_data, index_data, index_ticker,
                                positive_threshold, positive_months,
                                negative_threshold, negative_months):
    """
    Check if stock is trending excessively relative to its sector index.

    Stocks with sustained directional trends (either up or down) relative to
    their sector tend to perform poorly in mean reversion strategies as these
    trends can persist for extended periods.

    Parameters
    ----------
    ticker : str
        Ticker symbol to check
    ticker_data : DataFrame
        Historical price data with 'close' column
    index_data : DataFrame
        Index ETF historical price data with 'close' column
    index_ticker : str
        Index ticker symbol (for logging)
    positive_threshold : float
        Threshold for positive trending (e.g., 0.60 for 60% excess return)
    positive_months : int
        Lookback period in months for positive trending check
    negative_threshold : float
        Threshold for negative trending (e.g., -0.50 for -50% excess return)
    negative_months : int
        Lookback period in months for negative trending check

    Returns
    -------
    tuple: (passed, result_dict)
        passed: bool indicating if ticker passed the filter
        result_dict: dict with detailed results
    """

    result = {
        'passed': False,
        'ticker': ticker,
        'index': index_ticker,
        'positive_excess_return': None,
        'positive_lookback_days': None,
        'positive_threshold': positive_threshold,
        'negative_excess_return': None,
        'negative_lookback_days': None,
        'negative_threshold': negative_threshold,
        'failed_reason': None
    }

    try:
        # Ensure data is sorted by date
        ticker_data = ticker_data.sort_index()
        index_data = index_data.sort_index()

        # Get most recent date available in both datasets
        latest_date = min(ticker_data.index[-1], index_data.index[-1])

        # CHECK 1: Positive Trending (Upward Drift)
        positive_lookback_days = positive_months * 21
        result['positive_lookback_days'] = positive_lookback_days

        positive_start_date = latest_date - pd.Timedelta(days=positive_lookback_days * 1.5)
        ticker_positive = ticker_data[ticker_data.index >= positive_start_date].copy()
        index_positive = index_data[index_data.index >= positive_start_date].copy()

        if len(ticker_positive) < positive_lookback_days * 0.8:
            result['failed_reason'] = f'Insufficient data for {positive_months}M positive check'
            return False, result

        if len(index_positive) < positive_lookback_days * 0.8:
            result['failed_reason'] = f'Insufficient index data for {positive_months}M positive check'
            return False, result

        pos_lookback = min(positive_lookback_days, len(ticker_positive) - 1, len(index_positive) - 1)

        ticker_start_price = ticker_positive['close'].iloc[-pos_lookback]
        ticker_end_price = ticker_positive['close'].iloc[-1]
        ticker_return = (ticker_end_price - ticker_start_price) / ticker_start_price

        index_start_price = index_positive['close'].iloc[-pos_lookback]
        index_end_price = index_positive['close'].iloc[-1]
        index_return = (index_end_price - index_start_price) / index_start_price

        positive_excess_return = ticker_return - index_return
        result['positive_excess_return'] = positive_excess_return

        if positive_excess_return > positive_threshold:
            result['failed_reason'] = (
                f'Positive trending: {positive_excess_return:.2%} excess return '
                f'over {positive_months}M (threshold: {positive_threshold:.2%})'
            )
            return False, result

        # CHECK 2: Negative Trending (Downward Drift)
        negative_lookback_days = negative_months * 21
        result['negative_lookback_days'] = negative_lookback_days

        negative_start_date = latest_date - pd.Timedelta(days=negative_lookback_days * 1.5)
        ticker_negative = ticker_data[ticker_data.index >= negative_start_date].copy()
        index_negative = index_data[index_data.index >= negative_start_date].copy()

        if len(ticker_negative) < negative_lookback_days * 0.8:
            result['failed_reason'] = f'Insufficient data for {negative_months}M negative check'
            return False, result

        if len(index_negative) < negative_lookback_days * 0.8:
            result['failed_reason'] = f'Insufficient index data for {negative_months}M negative check'
            return False, result

        neg_lookback = min(negative_lookback_days, len(ticker_negative) - 1, len(index_negative) - 1)

        ticker_start_price = ticker_negative['close'].iloc[-neg_lookback]
        ticker_end_price = ticker_negative['close'].iloc[-1]
        ticker_return = (ticker_end_price - ticker_start_price) / ticker_start_price

        index_start_price = index_negative['close'].iloc[-neg_lookback]
        index_end_price = index_negative['close'].iloc[-1]
        index_return = (index_end_price - index_start_price) / index_start_price

        negative_excess_return = ticker_return - index_return
        result['negative_excess_return'] = negative_excess_return

        if negative_excess_return < negative_threshold:
            result['failed_reason'] = (
                f'Negative trending: {negative_excess_return:.2%} excess return '
                f'over {negative_months}M (threshold: {negative_threshold:.2%})'
            )
            return False, result

        # PASSED BOTH CHECKS
        result['passed'] = True
        result['failed_reason'] = None
        return True, result

    except Exception as e:
        result['failed_reason'] = f'Error: {str(e)}'
        return False, result


# ============================================================================
# PRIMARY FILTERS WITH LENIENCY
# ============================================================================

def apply_primary_filters_with_leniency_v92(ticker1, ticker2, tag, tail, hist1, hist2,
                                            market_data, params, earnings_dates,
                                            leniency_config, max_spread=0.004,
                                            subsector_manager=None, alpha_data=None, alpha_cache=None):
    """
    Apply all primary filters using single-factor model.

    Key features:
    - No treasury data needed
    - Uses SubsectorIndexManager for category-based indices
    - Each ticker uses its own sub-sector index for alpha calculation
    - OPTIMIZATION: Uses AlphaCache when available for faster filter calculations

    Args:
        ticker1, ticker2: Stock tickers
        tag: Pair tag
        tail: 'L' or 'U'
        hist1, hist2: Historical price data
        market_data: Live market data (for spreads)
        params: Dict with parameters (cumulative_stats_df, alpha_variance_hurdles, ema_multipliers)
        earnings_dates: Earnings calendar
        leniency_config: Dict with leniency settings
        max_spread: Maximum allowed spread (default 0.004 = 0.4%)
        subsector_manager: SubsectorIndexManager instance (uses global if None)
        alpha_data: DataFrame of historical alpha values for T-stat filter (optional)
        alpha_cache: AlphaCache instance for optimized calculations (optional)

    Returns:
        Tuple: (overall_passed: bool, filter_results: dict)
    """
    from scipy.stats import norm

    if subsector_manager is None:
        subsector_manager = get_subsector_manager()

    filter_results = {}
    individual_passes = []

    # Track if we're using cached calculations
    using_cache = alpha_cache is not None and alpha_cache.is_populated()
    if using_cache:
        filter_results['_cache_status'] = 'using_alpha_cache'
    else:
        filter_results['_cache_status'] = 'no_cache_available'

    # ========================================================================
    # FILTER 1: EARNINGS
    # ========================================================================
    try:
        if leniency_config.get('skip_earnings', False):
            earnings_success = True
            earnings_details = {"skipped_by_config": True}
        else:
            days_ahead = leniency_config.get('earnings_days_ahead', 6)
            days_behind = leniency_config.get('earnings_days_behind', 1)

            earnings_success, earnings_details = check_earnings_filter(
                ticker1, ticker2, earnings_dates,
                days_ahead=days_ahead,
                days_behind=days_behind
            )

        filter_results['earnings'] = {
            'passed': earnings_success,
            'details': earnings_details
        }
        individual_passes.append(earnings_success)
    except Exception as e:
        filter_results['earnings'] = {'passed': False, 'details': {'error': str(e)}}
        individual_passes.append(False)

    # ========================================================================
    # FILTER 2: SPREAD
    # ========================================================================
    try:
        if leniency_config.get('skip_spread', False):
            spread_success = True
            spread_value = None
            spread_details = {"skipped_by_config": True}
        else:
            spread_success, spread_value, spread_details = check_spread_hurdle(
                ticker1, ticker2, market_data, max_spread_pct=max_spread
            )

        filter_results['spread'] = {
            'passed': spread_success,
            'value': spread_value,
            'details': spread_details
        }
        individual_passes.append(spread_success)
    except Exception as e:
        filter_results['spread'] = {'passed': False, 'value': None, 'details': {'error': str(e)}}
        individual_passes.append(False)

    # ========================================================================
    # FILTER 3: 15-DAY ALPHA VARIANCE (SINGLE-FACTOR)
    # ========================================================================
    try:
        alpha_success = False
        alpha_cdf = None
        alpha_details = {}

        # Try cached calculation first (much faster)
        if using_cache:
            net_alpha_series, cache_details = alpha_cache.get_pair_net_alpha(
                ticker1, ticker2, last_n_days=15
            )

            if net_alpha_series is not None:
                # Get pair statistics from params
                pair_name = f"{ticker1}_{ticker2}"
                pair_stats = params['cumulative_stats_df'][
                    params['cumulative_stats_df']['Pair_Name'] == pair_name
                ]

                if len(pair_stats) > 0:
                    std_15day = pair_stats['Std_15Day_Cumulative'].values[0]
                    mean_15day = pair_stats['Mean_15Day_Cumulative'].values[0]
                    hurdle = params['alpha_variance_hurdles'].get(tag, 0.5)

                    # Calculate from cached series
                    alpha_sum = net_alpha_series.sum()
                    z_score = alpha_sum / std_15day
                    alpha_cdf = norm.cdf(z_score)

                    # Determine success based on hurdle
                    if hurdle > 0.5:
                        alpha_success = alpha_cdf > hurdle
                    elif hurdle < 0.5:
                        alpha_success = alpha_cdf < hurdle
                    else:
                        alpha_success = False

                    alpha_details = {
                        "pair_name": pair_name,
                        'mean': mean_15day,
                        'stddev': std_15day,
                        "net_alpha_sum": alpha_sum,
                        "cdf_value": alpha_cdf,
                        "hurdle": hurdle,
                        "std_15day": std_15day,
                        "mean_15day": mean_15day,
                        "z_score": z_score,
                        "success": alpha_success,
                        "category1": subsector_manager.get_category(ticker1),
                        "category2": subsector_manager.get_category(ticker2),
                        "model": "single_factor_cached",
                        "from_cache": True
                    }
                else:
                    # Pair not in stats, fall back to non-cached
                    alpha_details = {"cache_fallback_reason": f"No stats for {pair_name}"}
            else:
                # Cache miss, fall back to non-cached
                alpha_details = {"cache_fallback_reason": cache_details.get('error', 'Unknown')}

        # Fall back to original calculation if cache didn't work
        if alpha_cdf is None:
            alpha_success, alpha_cdf, alpha_details = check_15day_alpha_variance_v92(
                ticker1, ticker2, tag, hist1, hist2,
                params['cumulative_stats_df'], params['alpha_variance_hurdles'],
                subsector_manager, earnings_dates
            )
            alpha_details['from_cache'] = False

        # Apply tail-aware leniency adjustment using calibrated thresholds
        adjustment = leniency_config.get('cdf_adjustment', 0.0)

        # Get the calibrated threshold for this pair from parameters
        calibrated_hurdle = params['alpha_variance_hurdles'].get(tag, 0.5)

        if tail.upper() == 'L':
            lenient_hurdle = calibrated_hurdle + adjustment
            alpha_success = alpha_cdf < lenient_hurdle if alpha_cdf is not None else False
        elif tail.upper() == 'U':
            lenient_hurdle = calibrated_hurdle - adjustment
            alpha_success = alpha_cdf > lenient_hurdle if alpha_cdf is not None else False
        else:
            alpha_success = False
            calibrated_hurdle = 0.5
            lenient_hurdle = 0.5

        alpha_details['lenient_hurdle'] = lenient_hurdle
        alpha_details['calibrated_hurdle'] = calibrated_hurdle
        alpha_details['tail'] = tail

        filter_results['alpha_variance'] = {
            'passed': alpha_success,
            'cdf_value': alpha_cdf,
            'details': alpha_details
        }
        individual_passes.append(alpha_success)
    except Exception as e:
        filter_results['alpha_variance'] = {'passed': False, 'cdf_value': None, 'details': {'error': str(e)}}
        individual_passes.append(False)

    # ========================================================================
    # FILTER 4: 2-DAY DEVIATION (SINGLE-FACTOR)
    # ========================================================================
    try:
        two_day_success = False
        two_day_value = None
        two_day_details = {}

        # Try cached calculation first (much faster)
        if using_cache:
            # Need 31 days for EMA calculation
            net_alpha_series, cache_details = alpha_cache.get_pair_net_alpha(
                ticker1, ticker2, last_n_days=31
            )

            if net_alpha_series is not None and len(net_alpha_series) >= 31:
                ema_multiplier = params['ema_multipliers'].get(tag, 1.0)

                # Calculate 2-day sum
                two_day_value = net_alpha_series.iloc[-2:].sum()

                # Calculate 30-day EMA
                ema_30_day = net_alpha_series.abs().ewm(span=30).mean().iloc[-1]

                threshold = ema_30_day * ema_multiplier
                two_day_success = abs(two_day_value) > threshold

                two_day_details = {
                    "two_day_sum": two_day_value,
                    "ema_30day": ema_30_day,
                    "threshold": threshold,
                    "ema_multiplier": ema_multiplier,
                    "success": two_day_success,
                    "model": "single_factor_cached",
                    "from_cache": True
                }
            else:
                # Cache miss or insufficient data, fall back
                two_day_details = {"cache_fallback_reason": cache_details.get('error', 'Insufficient cached data')}

        # Fall back to original calculation if cache didn't work
        if two_day_value is None:
            two_day_success, two_day_value, two_day_details = check_2day_deviation_v92(
                ticker1, ticker2, tag, hist1, hist2,
                params['ema_multipliers'], subsector_manager
            )
            two_day_details['from_cache'] = False

        # Apply leniency if configured
        if leniency_config.get('two_day_reduction', 0) > 0 and two_day_value is not None:
            original_threshold = two_day_details.get('threshold', 0)
            reduction = leniency_config['two_day_reduction']
            lenient_threshold = original_threshold * (1 - reduction)

            two_day_success = abs(two_day_value) > lenient_threshold
            two_day_details['lenient_threshold'] = lenient_threshold
            two_day_details['original_threshold'] = original_threshold

        filter_results['two_day_deviation'] = {
            'passed': two_day_success,
            'value': two_day_value,
            'details': two_day_details
        }
        individual_passes.append(two_day_success)
    except Exception as e:
        filter_results['two_day_deviation'] = {'passed': False, 'value': None, 'details': {'error': str(e)}}
        individual_passes.append(False)

    # ========================================================================
    # FILTER 5: SAME DIRECTION
    # ========================================================================
    try:
        if leniency_config.get('skip_same_direction', False):
            same_dir_success = True
            same_dir_details = {"skipped_by_config": True}
        else:
            alpha_details_for_check = filter_results.get('alpha_variance', {}).get('details', {})
            two_day_details_for_check = filter_results.get('two_day_deviation', {}).get('details', {})

            same_dir_success, same_dir_details = check_same_direction(
                alpha_details_for_check, two_day_details_for_check
            )

        filter_results['same_direction'] = {
            'passed': same_dir_success,
            'details': same_dir_details
        }
        individual_passes.append(same_dir_success)
    except Exception as e:
        filter_results['same_direction'] = {'passed': False, 'details': {'error': str(e)}}
        individual_passes.append(False)

    # ========================================================================
    # FILTER 6: NOMINAL DIRECTION (5-day price confirmation)
    # ========================================================================
    try:
        if leniency_config.get('skip_nominal_direction', False):
            nominal_dir_success = True
            nominal_dir_details = {"skipped_by_config": True}
        else:
            nominal_dir_success, nominal_dir_details = check_nominal_direction(
                ticker1, ticker2, tail, hist1, hist2
            )

        filter_results['nominal_direction'] = {
            'passed': nominal_dir_success,
            'details': nominal_dir_details
        }
        individual_passes.append(nominal_dir_success)
    except Exception as e:
        filter_results['nominal_direction'] = {'passed': False, 'details': {'error': str(e)}}
        individual_passes.append(False)

    # ========================================================================
    # FILTER 7: CO1 SHORT-TERM TREND (T-stat filter)
    # ========================================================================
    try:
        tstat_config = leniency_config.get('tstat_filter', {})

        if tstat_config.get('skip', False):
            tstat_success = True
            tstat_details = {"skipped_by_config": True}
        elif alpha_data is None:
            tstat_success = True
            tstat_details = {"skipped": True, "reason": "No alpha_data DataFrame provided"}
        elif ticker1 not in alpha_data.columns:
            tstat_success = True
            tstat_details = {"skipped": True, "reason": f"Co1 {ticker1} not in alpha_data columns"}
        else:
            threshold = tstat_config.get('threshold', 8.0)
            lookback_days = tstat_config.get('lookback_days', 15)

            # Get current date from hist1
            as_of_date = hist1.index[-1] if hasattr(hist1.index[-1], 'date') else pd.Timestamp(hist1.index[-1])

            tstat_success, tstat_value = check_trend_filter(
                alpha_data[ticker1],
                as_of_date=as_of_date,
                threshold=threshold,
                lookback_days=lookback_days
            )

            # Handle None tstat_value (insufficient data case)
            if tstat_value is None:
                reason = "Insufficient alpha data for T-stat (passed by default)"
            elif tstat_success:
                reason = f"Co1 not trending (|T|={abs(tstat_value):.1f} <= {threshold})"
            else:
                reason = f"Co1 trending (|T|={abs(tstat_value):.1f} > {threshold})"

            tstat_details = {
                "ticker": ticker1,
                "tstat_value": tstat_value,
                "threshold": threshold,
                "lookback_days": lookback_days,
                "passed": tstat_success,
                "reason": reason
            }

        filter_results['co1_trend'] = {
            'passed': tstat_success,
            'details': tstat_details
        }
        individual_passes.append(tstat_success)
    except Exception as e:
        # On error, PASS the filter (don't block trades due to T-stat issues)
        logger.error(f"T-stat filter error for {ticker1}: {e}")
        filter_results['co1_trend'] = {'passed': True, 'details': {'error': str(e), 'passed_on_error': True}}
        individual_passes.append(True)

    # ========================================================================
    # DETERMINE OVERALL PASS STATUS
    # ========================================================================
    overall_passed = all(individual_passes)
    filter_results['model'] = 'single_factor'

    return overall_passed, filter_results
