"""
Intraday hourly data retrieval from Interactive Brokers.

Automatically determines what data to fetch based on pair generator output
and existing data files. Only fetches missing data to avoid overwrites.
Includes multi-index batch processing with resume capability.
Skips fetching data if missing data is only within the current month.

STATUS: live
"""

import pandas as pd
from ib_insync import *
import time
import os
import logging
import sys
from pathlib import Path
from datetime import datetime, timedelta
import numpy as np
import calendar

# Handle event loop issues early
try:
    import asyncio
    import nest_asyncio
    nest_asyncio.apply()
    print("Applied nest_asyncio patch for event loop compatibility")
except ImportError:
    print("nest_asyncio not available - may have issues in Jupyter")
except Exception as e:
    print(f"Event loop setup warning: {e}")

from src.shared import config

# =============================================================================
# CONFIG-BASED DIRECTORY SETUP
# =============================================================================

VERSION_BASE_DIR = config.get_version_dir(config.ACTIVE_VERSION)
print(f"Config VERSION: {config.ACTIVE_VERSION}")
print(f"Working directory: {VERSION_BASE_DIR}")

TWS_PORT = config.get("ibkr.port", 7497)
print(f"TWS Port: {TWS_PORT}")

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# =============================================================================
# SHARED SECONDARIES CACHE DIRECTORY (VERSION-INDEPENDENT)
# =============================================================================

SECONDARIES_CACHE_DIR = config.get("paths.secondaries_cache_dir", "")

# Ensure directory exists
os.makedirs(SECONDARIES_CACHE_DIR, exist_ok=True)
print(f"Secondaries cache: {SECONDARIES_CACHE_DIR}")

class EnhancedHourlyDataRetrieval:
    def __init__(self, index_ticker, retrieval_config=None):
        """Initialize the enhanced hourly data retrieval system"""
        self.config = {
            # Base directory - NOW VERSION-AWARE
            'base_dir': VERSION_BASE_DIR,

            # Index ticker (must be specified)
            'index_ticker': index_ticker,

            # Data parameters
            'target_years': 8,  # Total years of data we want
            'chunk_years': 4,   # Years to fetch per API call (due to IB limitations)
            'min_history_days': 1000,  # Minimum days to consider a ticker worth fetching

            # File patterns
            'pair_results_pattern': '{index}_Pair_Trading_Results.xlsx',
            'beta_analysis_pattern': '{index}_SubSector_Beta_Analysis.xlsx',
            'hourly_data_pattern': '{index}_Hourly_Data.csv',

            # Data quality parameters
            'max_missing_days': 10,  # Max missing days to still consider data complete
            'min_recent_days': 5,    # Data must be within this many days to be considered recent
            'skip_current_month': True,  # Skip fetching if missing data is only in current month

            # Connection parameters
            'ib_host': '127.0.0.1',
            'ib_port': TWS_PORT,
            'connection_timeout': 10,
            'rate_limit_delay': 1,  # Seconds between API calls
        }

        if retrieval_config:
            self.config.update(retrieval_config)

        # Initialize containers
        self.required_tickers = set()
        self.existing_data = None
        self.data_analysis = {}
        self.fetch_plan = {}

    def detect_index_from_directory(self, directory_path):
        """Detect index ticker from directory structure"""
        # Get the last part of the directory path
        dir_name = os.path.basename(os.path.normpath(directory_path))

        # Common index tickers to check
        common_indexes = ['VGT', 'XLK', 'IGM', 'SPY', 'QQQ', 'IWM', 'DIA', 'VOO', 'VTI', 'VFH', 'VIS', 'VHT', 'VCR', 'XLF', 'XLE']

        # Check if directory name is a known index
        if dir_name.upper() in common_indexes:
            return dir_name.upper()

        # Otherwise, try to find files and extract index from filename
        try:
            files = os.listdir(directory_path)
            for file in files:
                if 'Beta_Analysis.xlsx' in file or 'Pair_Trading_Results.xlsx' in file:
                    # Extract index ticker from filename
                    index_ticker = file.split('_')[0]
                    if index_ticker.upper() in common_indexes:
                        return index_ticker.upper()
        except:
            pass

        # If still not found, use directory name as index ticker
        return dir_name.upper()

    def setup_paths(self):
        """Setup file paths based on configured index ticker"""
        if not self.config['index_ticker']:
            raise ValueError("Index ticker must be specified in configuration")

        base_dir = os.path.expanduser(self.config['base_dir'])
        index_ticker = self.config['index_ticker'].upper()

        # Create index-specific directory path (for reading pair results/beta analysis)
        self.index_dir = os.path.join(base_dir, index_ticker)

        # Verify directory exists
        if not os.path.exists(self.index_dir):
            raise ValueError(f"Directory {self.index_dir} does not exist. "
                           f"Please ensure the {index_ticker} analysis has been run first.")

        # Set up file paths for reading (version-specific)
        self.pair_results_file = os.path.join(
            self.index_dir,
            self.config['pair_results_pattern'].format(index=index_ticker)
        )
        self.beta_analysis_file = os.path.join(
            self.index_dir,
            self.config['beta_analysis_pattern'].format(index=index_ticker)
        )

        # Hourly data file goes to shared secondaries cache (version-independent)
        self.hourly_data_file = os.path.join(
            SECONDARIES_CACHE_DIR,
            self.config['hourly_data_pattern'].format(index=index_ticker)
        )

        # Verify at least one analysis file exists
        if not os.path.exists(self.pair_results_file) and not os.path.exists(self.beta_analysis_file):
            raise ValueError(f"No analysis files found in {self.index_dir}. "
                           f"Please run the pair generator for {index_ticker} first.")

        logger.info(f"{index_ticker} ENHANCED HOURLY DATA RETRIEVAL")
        logger.info("=" * 60)
        logger.info(f"Index: {index_ticker}")
        logger.info(f"Index directory (config source): {self.index_dir}")
        logger.info(f"Hourly data file (shared): {self.hourly_data_file}")
        logger.info("Smart cache management enabled")
        logger.info(f"Skip current month: {'YES' if self.config['skip_current_month'] else 'NO'}")
        logger.info("=" * 60)

        return index_ticker

    def extract_required_tickers(self):
        """Extract all tickers needed based on pair generator output"""
        logger.info("Extracting required tickers from analysis files...")

        # Try to get tickers from pair results first
        if os.path.exists(self.pair_results_file):
            logger.info("Reading tickers from pair trading results...")
            try:
                # Check for Selected Pairs sheet
                excel_file = pd.ExcelFile(self.pair_results_file)
                if 'Selected Pairs' in excel_file.sheet_names:
                    pairs_df = pd.read_excel(self.pair_results_file, sheet_name='Selected Pairs')

                    # Find stock columns
                    stock1_col = None
                    stock2_col = None

                    for col in ['Stock1', 'stock1', 'Stock_1', 'Ticker1', 'Co1']:
                        if col in pairs_df.columns:
                            stock1_col = col
                            break

                    for col in ['Stock2', 'stock2', 'Stock_2', 'Ticker2', 'Co2']:
                        if col in pairs_df.columns:
                            stock2_col = col
                            break

                    if stock1_col and stock2_col:
                        self.required_tickers.update(pairs_df[stock1_col].dropna())
                        self.required_tickers.update(pairs_df[stock2_col].dropna())
                        logger.info(f"Found {len(self.required_tickers)} tickers from Selected Pairs")

                # Also check Individual Alpha Series if available
                if 'Individual Alpha Series' in excel_file.sheet_names:
                    alpha_df = pd.read_excel(self.pair_results_file,
                                           sheet_name='Individual Alpha Series',
                                           index_col=0, nrows=1)  # Just read header
                    alpha_tickers = [col for col in alpha_df.columns if not col.startswith('_')]
                    self.required_tickers.update(alpha_tickers)
                    logger.info(f"Added {len(alpha_tickers)} tickers from Individual Alpha Series")

                # Check Pair Alpha Series sheet
                if 'Pair Alpha Series' in excel_file.sheet_names:
                    pair_alpha_df = pd.read_excel(self.pair_results_file,
                                                  sheet_name='Pair Alpha Series',
                                                  index_col=0, nrows=1)
                    # Extract tickers from pair names (format: TICKER1_TICKER2)
                    for col in pair_alpha_df.columns:
                        if '_' in str(col):
                            parts = str(col).split('_')
                            if len(parts) == 2:
                                self.required_tickers.add(parts[0])
                                self.required_tickers.add(parts[1])
                    logger.info(f"Added tickers from Pair Alpha Series")

            except Exception as e:
                logger.warning(f"Error reading pair results: {e}")

        # Fallback to beta analysis file
        if not self.required_tickers and os.path.exists(self.beta_analysis_file):
            logger.info("Falling back to beta analysis file...")
            try:
                excel_file = pd.ExcelFile(self.beta_analysis_file)

                if 'SubSector Beta Summary' in excel_file.sheet_names:
                    beta_df = pd.read_excel(self.beta_analysis_file,
                                           sheet_name='SubSector Beta Summary', index_col=0)
                    self.required_tickers.update(beta_df.index.tolist())
                    logger.info(f"Found {len(self.required_tickers)} tickers from SubSector Beta Summary")
                elif 'Beta Summary' in excel_file.sheet_names:
                    beta_df = pd.read_excel(self.beta_analysis_file,
                                           sheet_name='Beta Summary', index_col=0)
                    self.required_tickers.update(beta_df.index.tolist())
                    logger.info(f"Found {len(self.required_tickers)} tickers from Beta Summary")

            except Exception as e:
                logger.error(f"Error reading beta analysis: {e}")
                return False

        # Additional fallback: Try Ticker Statistics sheet
        if not self.required_tickers and os.path.exists(self.pair_results_file):
            logger.info("Trying Ticker Statistics sheet...")
            try:
                ticker_stats_df = pd.read_excel(self.pair_results_file, sheet_name='Ticker Statistics')
                if 'Ticker' in ticker_stats_df.columns:
                    self.required_tickers.update(ticker_stats_df['Ticker'].dropna())
                    logger.info(f"Found {len(self.required_tickers)} tickers from Ticker Statistics")
            except Exception as e:
                logger.warning(f"Error reading ticker statistics: {e}")

        if not self.required_tickers:
            logger.error("No tickers found in analysis files!")
            return False

        # Clean ticker names (remove any whitespace, invalid characters)
        cleaned_tickers = set()
        for ticker in self.required_tickers:
            if isinstance(ticker, str) and ticker.strip():
                cleaned_tickers.add(ticker.strip().upper())

        self.required_tickers = cleaned_tickers
        logger.info(f"Final required tickers: {len(self.required_tickers)}")

        # Show ticker preview
        preview_tickers = sorted(list(self.required_tickers))[:10]
        if len(self.required_tickers) > 10:
            preview_tickers.append('...')
        logger.info(f"Ticker Preview: {', '.join(preview_tickers)}")

        return True

    def get_current_month_start(self):
        """Get the first day of the current month"""
        now = datetime.now()
        return datetime(now.year, now.month, 1)

    def is_gap_in_current_month_only(self, gap_start, gap_end):
        """Check if a data gap falls entirely within the current month"""
        current_month_start = self.get_current_month_start()

        # If the gap starts before the current month, it's not current-month-only
        if gap_start < current_month_start:
            return False

        # Check if it also ends in the current month
        now = datetime.now()
        current_month_end = datetime(now.year, now.month, calendar.monthrange(now.year, now.month)[1])

        return gap_end <= current_month_end

    def analyze_existing_data(self):
        """Analyze existing hourly data file to determine what's missing"""
        logger.info("Analyzing existing hourly data...")

        self.data_analysis = {
            'file_exists': False,
            'existing_tickers': set(),
            'missing_tickers': set(),
            'date_range': None,
            'total_days': 0,
            'ticker_coverage': {},
            'needs_update': False,
            'oldest_date': None,
            'newest_date': None,
            'current_month_skip_applied': False
        }

        if not os.path.exists(self.hourly_data_file):
            logger.info("No existing hourly data file found - will create new file")
            self.data_analysis['missing_tickers'] = self.required_tickers.copy()
            self.data_analysis['needs_update'] = True
            return self.data_analysis

        try:
            # Read existing data
            logger.info("Reading existing hourly data file...")
            self.existing_data = pd.read_csv(self.hourly_data_file)

            # Parse timestamp column
            timestamp_col = 'timestamp'
            if timestamp_col not in self.existing_data.columns:
                timestamp_col = self.existing_data.columns[0]

            self.existing_data[timestamp_col] = pd.to_datetime(
                self.existing_data[timestamp_col],
                dayfirst=True,
                errors='coerce'
            )

            # Remove rows with invalid dates
            self.existing_data = self.existing_data.dropna(subset=[timestamp_col])

            if self.existing_data.empty:
                logger.info("Existing file is empty or has no valid data")
                self.data_analysis['missing_tickers'] = self.required_tickers.copy()
                self.data_analysis['needs_update'] = True
                return self.data_analysis

            self.data_analysis['file_exists'] = True
            self.data_analysis['oldest_date'] = self.existing_data[timestamp_col].min()
            self.data_analysis['newest_date'] = self.existing_data[timestamp_col].max()
            self.data_analysis['total_days'] = len(self.existing_data[timestamp_col].dt.date.unique())

            # Extract existing tickers
            existing_tickers = set()
            for col in self.existing_data.columns:
                if '_close' in col:
                    ticker = col.replace('_close', '')
                    existing_tickers.add(ticker)

            self.data_analysis['existing_tickers'] = existing_tickers
            self.data_analysis['missing_tickers'] = self.required_tickers - existing_tickers

            # Analyze coverage for each ticker
            target_start_date = datetime.now() - timedelta(days=self.config['target_years'] * 365)
            recent_cutoff = datetime.now() - timedelta(days=self.config['min_recent_days'])
            current_month_start = self.get_current_month_start()

            for ticker in existing_tickers:
                close_col = f"{ticker}_close"
                if close_col in self.existing_data.columns:
                    ticker_data = self.existing_data[self.existing_data[close_col].notna()]

                    if len(ticker_data) > 0:
                        ticker_start = ticker_data[timestamp_col].min()
                        ticker_end = ticker_data[timestamp_col].max()
                        ticker_days = len(ticker_data)

                        # Check if data goes back far enough
                        sufficient_history = ticker_start <= target_start_date

                        # Check if data is recent enough
                        recent_enough = ticker_end >= recent_cutoff

                        # Check if the gap is only in the current month
                        needs_backfill = not sufficient_history
                        needs_update = not recent_enough

                        # Apply current month skip logic
                        if self.config['skip_current_month'] and needs_update:
                            if ticker_end >= current_month_start:
                                needs_update = False
                                self.data_analysis['current_month_skip_applied'] = True
                                logger.info(f"  Skipping {ticker} update (gap only in current month)")

                        self.data_analysis['ticker_coverage'][ticker] = {
                            'start_date': ticker_start,
                            'end_date': ticker_end,
                            'total_days': ticker_days,
                            'sufficient_history': sufficient_history,
                            'recent_enough': recent_enough,
                            'needs_backfill': needs_backfill,
                            'needs_update': needs_update,
                            'gap_in_current_month_only': ticker_end >= current_month_start if needs_update else False
                        }

            logger.info(f"Existing data analysis:")
            logger.info(f"  File exists: {self.data_analysis['file_exists']}")
            logger.info(f"  Date range: {self.data_analysis['oldest_date'].date()} to {self.data_analysis['newest_date'].date()}")
            logger.info(f"  Total days: {self.data_analysis['total_days']}")
            logger.info(f"  Existing tickers: {len(self.data_analysis['existing_tickers'])}")
            logger.info(f"  Missing tickers: {len(self.data_analysis['missing_tickers'])}")

            # Count tickers needing updates
            needs_backfill = sum(1 for t in self.data_analysis['ticker_coverage'].values()
                               if t['needs_backfill'])
            needs_update = sum(1 for t in self.data_analysis['ticker_coverage'].values()
                             if t['needs_update'])

            logger.info(f"  Tickers needing backfill: {needs_backfill}")
            logger.info(f"  Tickers needing recent data: {needs_update}")

            if self.data_analysis['current_month_skip_applied']:
                logger.info(f"  Current month skip applied: YES (saved API calls)")

            # Determine if any updates are needed
            self.data_analysis['needs_update'] = (
                len(self.data_analysis['missing_tickers']) > 0 or
                needs_backfill > 0 or
                needs_update > 0
            )

        except Exception as e:
            logger.error(f"Error analyzing existing data: {e}")
            self.data_analysis['needs_update'] = True

        return self.data_analysis

    def create_fetch_plan(self):
        """Create a plan for what data to fetch"""
        logger.info("Creating data fetch plan...")

        self.fetch_plan = {
            'new_tickers': [],
            'backfill_tickers': [],
            'update_tickers': [],
            'fetch_periods': [],
            'total_api_calls': 0,
            'current_month_skips': 0
        }

        if not self.data_analysis['needs_update']:
            logger.info("No data updates needed!")
            return self.fetch_plan

        current_date = datetime.now()
        target_start_date = current_date - timedelta(days=self.config['target_years'] * 365)
        current_month_start = self.get_current_month_start()

        # Handle completely new tickers
        for ticker in self.data_analysis['missing_tickers']:
            if self.config['skip_current_month']:
                logger.info(f"  Adding new ticker {ticker} (will fetch full history)")

            self.fetch_plan['new_tickers'].append(ticker)

        # Handle existing tickers that need updates
        for ticker, coverage in self.data_analysis['ticker_coverage'].items():
            if ticker in self.required_tickers:  # Only update tickers we actually need
                if coverage['needs_backfill']:
                    self.fetch_plan['backfill_tickers'].append(ticker)

                if coverage['needs_update']:
                    # Check if this update was skipped due to current month logic
                    if self.config['skip_current_month'] and coverage.get('gap_in_current_month_only', False):
                        self.fetch_plan['current_month_skips'] += 1
                        logger.info(f"  Skipped {ticker} update (current month only)")
                    else:
                        self.fetch_plan['update_tickers'].append(ticker)

        # Create fetch periods (chunked to avoid IB limitations)
        all_tickers_to_fetch = (self.fetch_plan['new_tickers'] +
                               self.fetch_plan['backfill_tickers'] +
                               self.fetch_plan['update_tickers'])

        if all_tickers_to_fetch:
            # For new tickers and backfills, we need full history
            chunk_size = timedelta(days=self.config['chunk_years'] * 365)
            period_start = target_start_date

            while period_start < current_date:
                period_end = min(period_start + chunk_size, current_date)

                # Skip periods that fall entirely within the current month if skip is enabled
                if self.config['skip_current_month']:
                    if period_start >= current_month_start:
                        logger.info(f"  Skipping period {period_start.date()} to {period_end.date()} (current month)")
                        period_start = period_end
                        continue

                # Determine which tickers need this period
                period_tickers = []

                # New tickers need all periods
                period_tickers.extend(self.fetch_plan['new_tickers'])

                # Backfill tickers need early periods
                for ticker in self.fetch_plan['backfill_tickers']:
                    ticker_start = self.data_analysis['ticker_coverage'][ticker]['start_date']
                    if period_end <= ticker_start:
                        period_tickers.append(ticker)

                # Update tickers need recent periods
                for ticker in self.fetch_plan['update_tickers']:
                    ticker_end = self.data_analysis['ticker_coverage'][ticker]['end_date']
                    if period_start >= ticker_end:
                        period_tickers.append(ticker)

                if period_tickers:
                    self.fetch_plan['fetch_periods'].append({
                        'start_date': period_start,
                        'end_date': period_end,
                        'tickers': list(set(period_tickers)),  # Remove duplicates
                        'duration_str': f"{self.config['chunk_years']} Y",
                        'bar_size': "1 hour"
                    })

                period_start = period_end

        # Calculate total API calls needed
        self.fetch_plan['total_api_calls'] = sum(
            len(period['tickers']) for period in self.fetch_plan['fetch_periods']
        )

        # Log the plan
        logger.info(f"Fetch plan created:")
        logger.info(f"  New tickers: {len(self.fetch_plan['new_tickers'])}")
        logger.info(f"  Backfill tickers: {len(self.fetch_plan['backfill_tickers'])}")
        logger.info(f"  Update tickers: {len(self.fetch_plan['update_tickers'])}")
        logger.info(f"  Current month skips: {self.fetch_plan['current_month_skips']}")
        logger.info(f"  Fetch periods: {len(self.fetch_plan['fetch_periods'])}")
        logger.info(f"  Total API calls: {self.fetch_plan['total_api_calls']}")

        if self.fetch_plan['current_month_skips'] > 0:
            logger.info(f"  API calls saved by current month skip: ~{self.fetch_plan['current_month_skips']}")

        for i, period in enumerate(self.fetch_plan['fetch_periods']):
            logger.info(f"  Period {i+1}: {period['start_date'].date()} to {period['end_date'].date()} "
                       f"({len(period['tickers'])} tickers)")

        return self.fetch_plan

    def fetch_ticker_data(self, ib_connection, ticker, start_date, end_date,
                         duration="4 Y", bar_size="1 hour"):
        """Fetch data for a single ticker and date range"""
        try:
            # Define the contract
            contract = Stock(ticker, "SMART", "USD")

            # Calculate end datetime string
            end_datetime = end_date.strftime("%Y%m%d %H:%M:%S US/Eastern")

            logger.info(f"    Requesting data for {ticker} ending {end_datetime}...")

            # Request historical data
            trade_data = ib_connection.reqHistoricalData(
                contract,
                endDateTime=end_datetime,
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1
            )

            if not trade_data:
                logger.warning(f"    No data retrieved for {ticker}")
                return None

            # Convert to DataFrame
            df = util.df(trade_data)
            df['ticker'] = ticker

            # Ensure date column is datetime
            df['date'] = pd.to_datetime(df['date'])
            if hasattr(df['date'].dtype, 'tz'):
                df['date'] = df['date'].dt.tz_localize(None)

            # Filter to requested date range (IB sometimes returns extra data)
            mask = (df['date'] >= start_date) & (df['date'] <= end_date)
            df = df[mask]

            logger.info(f"    Successfully fetched {len(df)} data points for {ticker}")
            return df

        except Exception as e:
            logger.error(f"    Error fetching data for {ticker}: {str(e)}")
            return None

    def execute_fetch_plan(self, auto_confirm=True):
        """Execute the fetch plan to retrieve missing data"""
        if not self.fetch_plan['fetch_periods']:
            logger.info("No data fetching required!")

            if self.fetch_plan['current_month_skips'] > 0:
                logger.info(f"Skipped {self.fetch_plan['current_month_skips']} tickers (current month data gaps)")
                logger.info("   Run again next month to fetch complete data")

            return True

        logger.info(f"Executing fetch plan with {self.fetch_plan['total_api_calls']} API calls...")

        if self.fetch_plan['current_month_skips'] > 0:
            logger.info(f"Saved approximately {self.fetch_plan['current_month_skips']} API calls by skipping current month gaps")

        # Ask for user confirmation (with auto-confirm option)
        if self.fetch_plan['total_api_calls'] > 0:
            logger.info(f"Ready to fetch data requiring {self.fetch_plan['total_api_calls']} API calls")

            if auto_confirm:
                logger.info("Auto-confirm enabled - proceeding with data fetching...")
                user_input = 'y'
            else:
                try:
                    user_input = input("Proceed with data fetching? (y/n): ")
                except (EOFError, KeyboardInterrupt):
                    logger.info("Input not available or interrupted - using auto-confirm")
                    user_input = 'y'

            if user_input.lower() not in ['y', 'yes']:
                logger.info("Data fetching cancelled by user")
                return False

        # Initialize IB connection
        ib = IB()
        new_data_dfs = []
        failed_tickers = set()  # Track tickers that failed

        try:
            # Connect to IB
            logger.info("Connecting to Interactive Brokers...")

            try:
                ib.connect(self.config['ib_host'], self.config['ib_port'], clientId=1, timeout=self.config['connection_timeout'])
                logger.info("Connected successfully with clientId=1")
            except Exception as e:
                logger.warning(f"First connection attempt failed: {e}")
                try:
                    ib.connect(self.config['ib_host'], self.config['ib_port'], clientId=2, timeout=self.config['connection_timeout'])
                    logger.info("Connected successfully with clientId=2")
                except Exception as e2:
                    logger.error(f"Second connection attempt failed: {e2}")
                    raise e2

            time.sleep(2)

            # Process each fetch period
            for period_idx, period in enumerate(self.fetch_plan['fetch_periods']):
                logger.info(f"\nProcessing period {period_idx + 1}/{len(self.fetch_plan['fetch_periods'])}")
                logger.info(f"   Date range: {period['start_date'].date()} to {period['end_date'].date()}")
                logger.info(f"   Tickers: {len(period['tickers'])}")

                period_data = []
                successful_tickers = 0

                for ticker_idx, ticker in enumerate(period['tickers']):
                    # Skip if ticker has already failed in a previous period
                    if ticker in failed_tickers:
                        logger.info(f"  [{ticker_idx + 1}/{len(period['tickers'])}] Skipping {ticker} (previously failed)")
                        continue

                    logger.info(f"  [{ticker_idx + 1}/{len(period['tickers'])}] Fetching {ticker}")

                    # Rate limiting
                    time.sleep(self.config['rate_limit_delay'])

                    df = self.fetch_ticker_data(
                        ib, ticker,
                        period['start_date'], period['end_date'],
                        period['duration_str'], period['bar_size']
                    )

                    if df is not None and len(df) > 0:
                        period_data.append(df)
                        successful_tickers += 1
                    else:
                        # Mark ticker as failed so we don't try it again
                        failed_tickers.add(ticker)
                        logger.warning(f"    Marked {ticker} as unavailable - will skip in future periods")

                    # Progress update every 10 tickers
                    if (ticker_idx + 1) % 10 == 0:
                        logger.info(f"    Completed {ticker_idx + 1}/{len(period['tickers'])} tickers ({successful_tickers} successful)")

                if period_data:
                    # Combine period data
                    period_combined = pd.concat(period_data, ignore_index=True)
                    new_data_dfs.append(period_combined)
                    logger.info(f"  Period {period_idx + 1} completed: {successful_tickers}/{len(period['tickers'])} tickers successful, {len(period_combined)} total records")
                else:
                    logger.warning(f"  No data retrieved for period {period_idx + 1}")

            # Report on failed tickers
            if failed_tickers:
                logger.warning(f"\nThe following {len(failed_tickers)} tickers had no data available:")
                for ticker in sorted(failed_tickers):
                    logger.warning(f"    - {ticker}")
                logger.info("These tickers will be excluded from the hourly data file")

        except Exception as e:
            logger.error(f"Error during data fetching: {str(e)}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            return False

        finally:
            # Disconnect from IB
            try:
                if ib.isConnected():
                    ib.disconnect()
                    logger.info("Disconnected from Interactive Brokers")
            except Exception as e:
                logger.warning(f"Error during disconnect: {e}")

        # Process and merge the new data
        if new_data_dfs:
            # Update required_tickers to exclude failed ones
            self.required_tickers = self.required_tickers - failed_tickers

            success = self.merge_new_data(new_data_dfs)

            # Report final status
            if success and failed_tickers:
                logger.info(f"\nData fetching completed with some exclusions:")
                logger.info(f"Successfully fetched: {len(self.required_tickers)} tickers")
                logger.info(f"No data available: {len(failed_tickers)} tickers")
                logger.info(f"The hourly data file contains only tickers with available data")

            return success
        else:
            existing_ticker_count = len(self.data_analysis.get('existing_tickers', set()))

            if failed_tickers and existing_ticker_count > 0:
                logger.info(f"\nNo new data fetched - all requested tickers unavailable:")
                for ticker in sorted(failed_tickers):
                    logger.info(f"    - {ticker} (delisted, renamed, or insufficient history)")

                logger.info(f"\nExisting data remains valid:")
                logger.info(f"   {existing_ticker_count} tickers with complete data")
                logger.info(f"   Date range: {self.data_analysis.get('oldest_date', 'N/A')} to {self.data_analysis.get('newest_date', 'N/A')}")
                logger.info(f"   No changes needed to hourly data file")

                self.required_tickers = self.required_tickers - failed_tickers
                return True

            elif failed_tickers and existing_ticker_count == 0:
                logger.error("No existing data and all new ticker fetches failed")
                logger.error("   This index has no usable hourly data")
                return False

            else:
                logger.warning("No new data was fetched (unexpected state)")
                return False

    def merge_new_data(self, new_data_dfs):
        """Merge new data with existing data file"""
        logger.info("Merging new data with existing data...")

        try:
            # Combine all new data
            all_new_data = pd.concat(new_data_dfs, ignore_index=True)

            # Create pivoted format matching existing file structure
            logger.info("Reshaping new data to match existing format...")

            # Get unique timestamps and tickers
            all_timestamps = sorted(all_new_data['date'].unique())
            all_tickers = sorted(all_new_data['ticker'].unique())

            # Create result DataFrame with timestamps as index
            result_df = pd.DataFrame(index=pd.DatetimeIndex(all_timestamps))
            result_df.index.name = 'timestamp'

            # Add columns for each ticker
            for ticker in all_tickers:
                ticker_data = all_new_data[all_new_data['ticker'] == ticker].set_index('date')

                for col in ['open', 'high', 'low', 'close', 'volume']:
                    col_name = f"{ticker}_{col}"
                    if col in ticker_data.columns:
                        result_df[col_name] = ticker_data[col]

            # Merge with existing data if it exists
            if self.existing_data is not None and not self.existing_data.empty:
                logger.info("Merging with existing data...")

                # Prepare existing data
                existing_df = self.existing_data.copy()
                timestamp_col = 'timestamp' if 'timestamp' in existing_df.columns else existing_df.columns[0]
                existing_df[timestamp_col] = pd.to_datetime(existing_df[timestamp_col], dayfirst=True)
                existing_df = existing_df.set_index(timestamp_col)
                existing_df.index.name = 'timestamp'

                # Combine dataframes
                combined_df = pd.concat([existing_df, result_df])

                # Remove duplicates (keep existing data in case of overlap)
                combined_df = combined_df[~combined_df.index.duplicated(keep='first')]

                # Sort by timestamp
                combined_df = combined_df.sort_index()

                result_df = combined_df

            # Reset index to make timestamp a column for CSV saving
            result_df = result_df.reset_index()

            # Save the merged data
            logger.info(f"Saving merged data to {self.hourly_data_file}...")
            result_df.to_csv(self.hourly_data_file, index=False)

            logger.info("Data merge completed successfully!")
            logger.info(f"Final dataset shape: {result_df.shape}")
            logger.info(f"Date range: {result_df['timestamp'].min()} to {result_df['timestamp'].max()}")

            # Summary of what was added
            if self.existing_data is not None and not self.existing_data.empty:
                original_shape = self.existing_data.shape
                new_rows = result_df.shape[0] - original_shape[0]
                new_cols = result_df.shape[1] - original_shape[1]
                logger.info(f"Added {new_rows} new rows and {new_cols} new columns")
            else:
                logger.info(f"Created new file with {result_df.shape[0]} rows and {result_df.shape[1]} columns")

            return True

        except Exception as e:
            logger.error(f"Error merging data: {str(e)}")
            return False

    def run(self):
        """Run the complete dynamic data retrieval process"""
        try:
            logger.info("Starting Enhanced Hourly Data Retrieval...")

            # Step 1: Setup paths
            configured_index = self.setup_paths()

            # Step 2: Extract required tickers
            if not self.extract_required_tickers():
                logger.error("Failed to extract required tickers")
                return False

            # Step 3: Analyze existing data
            self.analyze_existing_data()

            # Step 4: Create fetch plan
            self.create_fetch_plan()

            # Step 5: Execute fetch plan if needed
            if self.fetch_plan['total_api_calls'] > 0:
                return self.execute_fetch_plan()
            else:
                logger.info("All required data is already available!")

                if self.fetch_plan['current_month_skips'] > 0:
                    logger.info(f"Note: {self.fetch_plan['current_month_skips']} tickers skipped due to current month gaps")
                    logger.info("   This saves API calls - run again next month for complete data")

                return True

        except Exception as e:
            logger.error(f"Error in enhanced data retrieval: {str(e)}")
            return False


def run_enhanced_hourly_data_retrieval(index_ticker='VGT', skip_current_month=True, auto_confirm=True):
    """
    Run enhanced hourly data retrieval for a specific index with smart cache management

    Args:
        index_ticker (str): The index ticker symbol (default: 'VGT')
        skip_current_month (bool): Whether to skip fetching data if gaps are only in current month (default: True)
        auto_confirm (bool): Whether to automatically confirm data fetching without user input (default: True)

    Usage:
        # For VGT with auto-confirm (default)
        run_enhanced_hourly_data_retrieval()

        # For other indices with auto-confirm
        run_enhanced_hourly_data_retrieval('VHT')

        # With manual confirmation
        run_enhanced_hourly_data_retrieval('VGT', auto_confirm=False)
    """
    logger.info(f"Starting {index_ticker} Enhanced Hourly Data Retrieval...")
    logger.info("Smart cache management: Only fetches missing data")
    logger.info(f"Current month skip: {'ENABLED' if skip_current_month else 'DISABLED'}")
    logger.info(f"Auto-confirm: {'ENABLED' if auto_confirm else 'DISABLED'}")
    logger.info(f"Config source directory: {VERSION_BASE_DIR}")
    logger.info(f"Hourly data output: {SECONDARIES_CACHE_DIR}")
    logger.info("IB Connection: Optimized for reliability and speed")
    logger.info("=" * 60)

    try:
        retrieval_config = {
            'base_dir': VERSION_BASE_DIR,
            'target_years': 8,
            'chunk_years': 4,
            'min_recent_days': 7,
            'skip_current_month': skip_current_month,
            'ib_host': '127.0.0.1',
            'ib_port': TWS_PORT,
            'connection_timeout': 10,
            'rate_limit_delay': 1,
            'auto_confirm': auto_confirm,
        }

        retrieval_system = EnhancedHourlyDataRetrieval(index_ticker, retrieval_config)
        success = retrieval_system.run()

        if success:
            logger.info(f"\n{index_ticker} HOURLY DATA RETRIEVAL COMPLETE!")
            logger.info(f"Hourly price data ready for analysis")
            logger.info(f"OHLCV data for all {index_ticker} constituents")
            logger.info(f"Saved to: {retrieval_system.hourly_data_file}")

            if retrieval_system.fetch_plan.get('current_month_skips', 0) > 0:
                logger.info(f"Efficiency: Skipped {retrieval_system.fetch_plan['current_month_skips']} current month updates")
        else:
            logger.info(f"\nRETRIEVAL FAILED OR CANCELLED")
            logger.info(f"Check logs for error details")

        logger.info(f"\nDATA STRUCTURE:")
        logger.info(f"  Hourly OHLCV data for each ticker")
        logger.info(f"  Columns: timestamp, {index_ticker}_ticker1_open, {index_ticker}_ticker1_high, etc.")
        logger.info(f"  Output: {index_ticker}_Hourly_Data.csv")

        return success

    except FileNotFoundError as e:
        logger.error(f"FILE ERROR: {e}")
        logger.info(f"\nPlease ensure:")
        logger.info(f"1. The {index_ticker} directory exists in {VERSION_BASE_DIR}")
        logger.info(f"2. The file {index_ticker}_Pair_Trading_Results.xlsx exists in that directory")
        logger.info(f"3. Interactive Brokers TWS or IB Gateway is running")
        return False

    except Exception as e:
        logger.error(f"UNEXPECTED ERROR: {e}")
        return False


def run_multiple_hourly_data_retrieval(indexes, resume_from=None, skip_current_month=True):
    """Run hourly data retrieval for multiple indexes with resume capability"""

    if resume_from:
        start_idx = indexes.index(resume_from)
        indexes = indexes[start_idx:]
        logger.info(f"Resuming from {resume_from}")

    completed = []
    failed = []

    logger.info(f"Current month skip: {'ENABLED' if skip_current_month else 'DISABLED'}")
    logger.info(f"Config source: {VERSION_BASE_DIR}")
    logger.info(f"Hourly data output: {SECONDARIES_CACHE_DIR}")

    for i, index in enumerate(indexes):
        logger.info(f"\n{'='*60}")
        logger.info(f"[{i+1}/{len(indexes)}] PROCESSING {index}")
        logger.info(f"{'='*60}")

        try:
            success = run_enhanced_hourly_data_retrieval(index, skip_current_month=skip_current_month)

            if success:
                completed.append(index)
                logger.info(f"{index} completed successfully")
            else:
                failed.append(index)
                logger.info(f"{index} failed")

        except KeyboardInterrupt:
            logger.info(f"\nInterrupted during {index}")
            logger.info(f"Completed: {completed}")
            logger.info(f"Failed: {failed}")
            logger.info(f"To resume, use: run_multiple_hourly_data_retrieval({indexes}, resume_from='{index}')")
            return False

        except Exception as e:
            failed.append(index)
            logger.error(f"{index} error: {e}")

    logger.info(f"\nBATCH HOURLY DATA RETRIEVAL COMPLETE")
    logger.info(f"Completed: {completed}")
    logger.info(f"Failed: {failed}")

    if skip_current_month:
        logger.info(f"Current month skip was ENABLED - saved significant API calls")

    return len(failed) == 0


# =============================================================================
# EXECUTION CODE
# =============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("ENHANCED HOURLY DATA RETRIEVAL")
    print("=" * 60)
    print(f"Config source: {VERSION_BASE_DIR}")
    print(f"Hourly data output: {SECONDARIES_CACHE_DIR}")
    print("=" * 60)

    # Multi-index batch processing with current month skip (default)
    indexes = ['VGT', 'VHT', 'VIS', 'VCR', 'VFH']
    run_multiple_hourly_data_retrieval(indexes)
