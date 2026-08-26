"""
Implied volatility data fetcher for index constituents.

Fetches monthly options IV data from Alpha Vantage for all tickers in a given
index (e.g., VGT, VHT). Collects ATM and OTM call/put implied volatility with
smart cache management: only fetches missing data, handles tickers without
options, and supports checkpoint/resume. Outputs Parquet files.

STATUS: live
"""

import requests
from datetime import datetime, timedelta
import pandas as pd
import time
import os
import json
import numpy as np
import sys
from dateutil.relativedelta import relativedelta
import logging

from src.shared import config

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


# =============================================================================
# VERSION-AWARE DIRECTORY CONFIGURATION
# =============================================================================

def get_version_info():
    """
    Get version info from config.
    Returns tuple: (version, v9_root, working_dir)
    """
    version = config.active_version()
    v9_root = config.get("paths.v9_root", "")
    working_dir = config.get_version_dir(version)

    logger.info(f"Config VERSION: {version}")

    return version, v9_root, working_dir


class EnhancedOptionsIVFetcher:
    def __init__(self, index_ticker='VCR'):
        """
        Initialize the enhanced IV fetcher for a specific index

        Args:
            index_ticker (str): The index ticker symbol (e.g., 'VHT', 'VGT', 'XLF', etc.)
        """
        self.index_ticker = index_ticker.upper()

        # Get version-aware paths
        version, v9_root, working_dir = get_version_info()

        # VERSION-AWARE path configuration
        self.version = version
        self.v9_root = v9_root
        self.working_dir = working_dir

        # Index directory is VERSION-SPECIFIC
        self.index_dir = os.path.join(working_dir, self.index_ticker)

        # Combined_Portfolio and options_cache are SHARED at root level
        self.combined_portfolio_dir = config.get("paths.implementation_dir", "")
        self.cache_dir = config.get("paths.options_cache_dir", "")

        # Load tickers from the index's pair trading results file
        self.tickers = self._load_index_tickers()

        # Configuration
        self.config = {
            'api_key': config.get("api_keys.alphavantage", ""),
            'cache_dir': self.cache_dir,
            'index_ticker': self.index_ticker,
            'tickers': self.tickers,
            'start_date': '2017-07-01',
            'end_date': datetime.now().strftime('%Y-%m-%d'),
            'sampling_frequency': 'monthly',  # Only sample once per month
            'otm_percentage': 0.10,  # 10% OTM
            'target_maturity_days': 120,

            # VERSION INFO
            'version': version,

            # OPTIMIZED SETTINGS FOR SPEED
            'rate_limit_delay': 0.3,  # Faster rate limit
            'max_calls_per_minute': 100,  # Higher limit for monthly sampling
            'max_retries_per_call': 3,  # Fewer retries
            'retry_delay': 15,
            'timeout': 30,
            'network_failure_delay': 120,
            'max_consecutive_failures': 5,

            # Ticker failure tracking
            'max_consecutive_ticker_failures': 3,  # Stop trying a ticker after 3 consecutive date failures
            'ticker_blacklist_duration_days': 30,  # How long to avoid retrying failed tickers

            # State management - dynamic file names
            'checkpoint_frequency': 10,  # Save every 10 calls
            'state_file': f'{self.index_ticker}_monthly_iv_state.json',
            'output_file': f'{self.index_ticker}_Monthly_Options_IV.parquet',
            'blacklist_file': f'{self.index_ticker}_options_blacklist.json'
        }

        # Tracking
        self.total_api_calls = 0
        self.total_records_collected = 0
        self.api_calls_this_minute = 0
        self.minute_start_time = time.time()
        self.consecutive_failures = 0
        self.session_start_time = datetime.now()

        # Results storage
        self.iv_data = []
        self.existing_data = None
        self.data_analysis = {}
        self.fetch_plan = {}

        # State management - set paths first
        self.state_file_path = os.path.join(self.config['cache_dir'], self.config['state_file'])
        self.blacklist_file_path = os.path.join(self.config['cache_dir'], self.config['blacklist_file'])

        # Ticker failure tracking
        self.ticker_failures = {}  # Track consecutive failures per ticker per date
        self.ticker_blacklist = self._load_ticker_blacklist()

        # Load state
        self.current_state = self.load_state()

        # Ensure cache directory exists
        os.makedirs(self.config['cache_dir'], exist_ok=True)

        self._print_initialization_summary()

    def _load_index_tickers(self):
        """
        Load ticker list from the index's pair trading results file
        Uses version-aware index directory path

        Returns:
            list: List of ticker symbols for the index
        """
        pair_trading_file = os.path.join(self.index_dir, f'{self.index_ticker}_Pair_Trading_Results.xlsx')

        if not os.path.exists(pair_trading_file):
            raise FileNotFoundError(
                f"Could not find pair trading results file: {pair_trading_file}\n"
                f"Please ensure the file exists in the {self.index_ticker} directory.\n"
                f"(Using version-aware path: {self.index_dir})"
            )

        try:
            # Read the Ticker Statistics tab
            df = pd.read_excel(pair_trading_file, sheet_name='Ticker Statistics')

            # Extract tickers from the Ticker column
            if 'Ticker' not in df.columns:
                raise ValueError(
                    f"'Ticker' column not found in 'Ticker Statistics' sheet of {pair_trading_file}.\n"
                    f"Available columns: {list(df.columns)}"
                )

            # Get unique tickers and remove any NaN values
            tickers = df['Ticker'].dropna().unique().tolist()

            # Convert to strings and remove any empty strings
            tickers = [str(ticker).strip() for ticker in tickers if str(ticker).strip()]

            if not tickers:
                raise ValueError(f"No tickers found in the Ticker column of {pair_trading_file}")

            logger.info(f"Loaded {len(tickers)} tickers from {self.index_ticker} index")
            return sorted(tickers)  # Sort for consistency

        except Exception as e:
            raise RuntimeError(
                f"Error loading tickers from {pair_trading_file}: {e}\n"
                f"Please check the file format and ensure 'Ticker Statistics' sheet exists with a 'Ticker' column."
            )

    def _load_ticker_blacklist(self):
        """Load ticker blacklist from file"""
        if os.path.exists(self.blacklist_file_path):
            try:
                with open(self.blacklist_file_path, 'r') as f:
                    blacklist_data = json.load(f)

                # Clean up old entries
                current_time = datetime.now()
                cleaned_blacklist = {}

                for ticker, data in blacklist_data.items():
                    blacklist_date = datetime.fromisoformat(data['blacklisted_date'])
                    days_since_blacklist = (current_time - blacklist_date).days

                    if days_since_blacklist < self.config['ticker_blacklist_duration_days']:
                        cleaned_blacklist[ticker] = data
                    else:
                        logger.info(f"Removing {ticker} from blacklist (expired)")

                return cleaned_blacklist

            except Exception as e:
                logger.warning(f"Could not load ticker blacklist: {e}")

        return {}

    def _save_ticker_blacklist(self):
        """Save ticker blacklist to file"""
        try:
            with open(self.blacklist_file_path, 'w') as f:
                json.dump(self.ticker_blacklist, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save ticker blacklist: {e}")

    def _is_ticker_blacklisted(self, ticker):
        """Check if a ticker is currently blacklisted"""
        if ticker not in self.ticker_blacklist:
            return False

        blacklist_date = datetime.fromisoformat(self.ticker_blacklist[ticker]['blacklisted_date'])
        days_since = (datetime.now() - blacklist_date).days

        return days_since < self.config['ticker_blacklist_duration_days']

    def _blacklist_ticker(self, ticker, reason):
        """Add a ticker to the blacklist"""
        self.ticker_blacklist[ticker] = {
            'blacklisted_date': datetime.now().isoformat(),
            'reason': reason,
            'consecutive_failures': self.ticker_failures.get(ticker, 0)
        }
        self._save_ticker_blacklist()
        logger.warning(f"Blacklisted {ticker}: {reason}")

    def analyze_existing_data(self):
        """Analyze existing IV data file to determine what's missing"""
        logger.info("Analyzing existing IV data...")

        output_file_path = os.path.join(self.cache_dir, self.config['output_file'])

        self.data_analysis = {
            'file_exists': False,
            'existing_records': 0,
            'existing_ticker_dates': set(),
            'missing_ticker_dates': set(),
            'date_range': None,
            'ticker_coverage': {},
            'needs_update': False,
            'oldest_date': None,
            'newest_date': None
        }

        if not os.path.exists(output_file_path):
            logger.info("No existing IV data file found - will create new file")
            self.data_analysis['needs_update'] = True
            return self.data_analysis

        try:
            # Read existing data
            logger.info("Reading existing IV data file...")
            self.existing_data = pd.read_parquet(output_file_path)

            if self.existing_data.empty:
                logger.info("Existing file is empty")
                self.data_analysis['needs_update'] = True
                return self.data_analysis

            # Parse date column (parquet preserves dtypes, but ensure datetime)
            self.existing_data['date'] = pd.to_datetime(self.existing_data['date'])

            self.data_analysis['file_exists'] = True
            self.data_analysis['existing_records'] = len(self.existing_data)
            self.data_analysis['oldest_date'] = self.existing_data['date'].min()
            self.data_analysis['newest_date'] = self.existing_data['date'].max()

            # Create set of existing ticker-date combinations
            existing_combinations = set()
            for _, row in self.existing_data.iterrows():
                ticker_date = (row['ticker'], row['date'].strftime('%Y-%m-%d'))
                existing_combinations.add(ticker_date)

            self.data_analysis['existing_ticker_dates'] = existing_combinations

            # Analyze coverage per ticker
            for ticker in self.config['tickers']:
                ticker_data = self.existing_data[self.existing_data['ticker'] == ticker]

                if len(ticker_data) > 0:
                    ticker_dates = set(ticker_data['date'].dt.strftime('%Y-%m-%d'))
                    self.data_analysis['ticker_coverage'][ticker] = {
                        'records': len(ticker_data),
                        'date_count': len(ticker_dates),
                        'dates': ticker_dates,
                        'oldest': ticker_data['date'].min(),
                        'newest': ticker_data['date'].max()
                    }
                else:
                    self.data_analysis['ticker_coverage'][ticker] = {
                        'records': 0,
                        'date_count': 0,
                        'dates': set(),
                        'oldest': None,
                        'newest': None
                    }

            logger.info(f"Existing data analysis:")
            logger.info(f"  File exists: {self.data_analysis['file_exists']}")
            logger.info(f"  Total records: {self.data_analysis['existing_records']}")
            if self.data_analysis['oldest_date'] and self.data_analysis['newest_date']:
                logger.info(f"  Date range: {self.data_analysis['oldest_date'].date()} to {self.data_analysis['newest_date'].date()}")

            # Count tickers with data
            tickers_with_data = sum(1 for coverage in self.data_analysis['ticker_coverage'].values()
                                  if coverage['records'] > 0)
            logger.info(f"  Tickers with data: {tickers_with_data}/{len(self.config['tickers'])}")

        except Exception as e:
            logger.error(f"Error analyzing existing data: {e}")
            self.data_analysis['needs_update'] = True

        return self.data_analysis

    def create_fetch_plan(self):
        """Create a plan for what ticker-date combinations need to be fetched"""
        logger.info("Creating IV fetch plan...")

        # Generate all monthly sampling dates
        sampling_dates = self.get_monthly_sampling_dates()

        # Create set of all required ticker-date combinations
        all_required_combinations = set()
        for ticker in self.config['tickers']:
            for date_obj in sampling_dates:
                date_str = date_obj.strftime('%Y-%m-%d')
                ticker_date = (ticker, date_str)
                all_required_combinations.add(ticker_date)

        # Determine missing combinations
        existing_combinations = self.data_analysis.get('existing_ticker_dates', set())
        missing_combinations = all_required_combinations - existing_combinations

        # Filter out blacklisted tickers
        filtered_missing = set()
        blacklisted_count = 0

        for ticker, date_str in missing_combinations:
            if self._is_ticker_blacklisted(ticker):
                blacklisted_count += 1
                continue
            filtered_missing.add((ticker, date_str))

        self.fetch_plan = {
            'total_required': len(all_required_combinations),
            'existing': len(existing_combinations),
            'missing': len(missing_combinations),
            'blacklisted': blacklisted_count,
            'to_fetch': len(filtered_missing),
            'missing_combinations': filtered_missing,
            'sampling_dates': sampling_dates
        }

        logger.info(f"Fetch plan created:")
        logger.info(f"  Total required combinations: {self.fetch_plan['total_required']}")
        logger.info(f"  Existing combinations: {self.fetch_plan['existing']}")
        logger.info(f"  Missing combinations: {self.fetch_plan['missing']}")
        logger.info(f"  Blacklisted combinations: {self.fetch_plan['blacklisted']}")
        logger.info(f"  Combinations to fetch: {self.fetch_plan['to_fetch']}")

        if self.fetch_plan['to_fetch'] == 0:
            logger.info("All required data is already available!")

        return self.fetch_plan

    def _print_initialization_summary(self):
        """Print initialization summary"""
        logger.info(f"{self.index_ticker} ENHANCED OPTIONS IV FETCHER")
        logger.info("=" * 60)
        logger.info(f"Config VERSION: {self.version}")
        logger.info(f"Index: {self.index_ticker}")
        logger.info(f"Working Directory: {self.working_dir}")
        logger.info(f"Index Directory: {self.index_dir}")
        logger.info(f"Cache Directory: {self.cache_dir}")
        logger.info(f"Constituents: {len(self.config['tickers'])} tickers")
        logger.info(f"Period: {self.config['start_date']} to {self.config['end_date']}")
        logger.info(f"Sampling: Monthly (4 contracts per ticker per month)")
        logger.info(f"Output: {self.config['output_file']} (Parquet format)")
        logger.info("Smart cache management enabled")
        logger.info("Ticker failure tracking enabled")
        logger.info("=" * 60)

        # Show first few tickers as preview
        preview_tickers = self.config['tickers'][:10]
        if len(self.config['tickers']) > 10:
            preview_tickers.append('...')
        logger.info(f"Ticker Preview: {', '.join(preview_tickers)}")

        # Show blacklisted tickers if any
        if self.ticker_blacklist:
            logger.info(f"Blacklisted tickers: {len(self.ticker_blacklist)}")
            blacklisted_names = list(self.ticker_blacklist.keys())[:5]
            if len(self.ticker_blacklist) > 5:
                blacklisted_names.append('...')
            logger.info(f"   {', '.join(blacklisted_names)}")

        logger.info("=" * 60)

    def load_state(self):
        if os.path.exists(self.state_file_path):
            try:
                with open(self.state_file_path, 'r') as f:
                    state = json.load(f)

                # Ensure completed_combinations is a set of tuples
                completed_list = state.get('completed_combinations', [])
                if completed_list:
                    # Convert list of lists to set of tuples
                    state['completed_combinations'] = set(tuple(item) if isinstance(item, list) else item
                                                        for item in completed_list)
                else:
                    state['completed_combinations'] = set()

                # Ensure all required keys exist with defaults
                state.setdefault('total_records_collected', 0)
                state.setdefault('current_combination_index', 0)
                state.setdefault('last_session_time', None)

                logger.info(f"RESUMING from previous session:")
                logger.info(f"   Last run: {state.get('last_session_time', 'Unknown')}")
                logger.info(f"   Records collected: {state.get('total_records_collected', 0)}")
                return state

            except Exception as e:
                logger.warning(f"Could not load previous state: {e}")

        return {
            'total_records_collected': 0,
            'completed_combinations': set(),
            'current_combination_index': 0,
            'last_session_time': None
        }

    def save_state(self):
        # Convert set to list for JSON serialization
        completed_combinations_list = [list(item) for item in self.current_state.get('completed_combinations', set())]

        state_to_save = {
            'total_records_collected': self.total_records_collected,
            'completed_combinations': completed_combinations_list,
            'current_combination_index': self.current_state.get('current_combination_index', 0),
            'last_session_time': datetime.now().isoformat(),
            'session_duration': str(datetime.now() - self.session_start_time),
            'total_api_calls': self.total_api_calls,
            'index_ticker': self.index_ticker,
            'version': self.version
        }

        try:
            with open(self.state_file_path, 'w') as f:
                json.dump(state_to_save, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save state: {e}")

    def get_monthly_sampling_dates(self):
        """Generate monthly sampling dates (1st business day of each month)"""
        start = datetime.strptime(self.config['start_date'], '%Y-%m-%d')
        end = datetime.strptime(self.config['end_date'], '%Y-%m-%d')

        sampling_dates = []
        current = start.replace(day=1)  # Start at beginning of month

        while current <= end:
            # Find first business day of the month
            first_business_day = current
            while first_business_day.weekday() >= 5:  # Skip weekends
                first_business_day += timedelta(days=1)

            if first_business_day <= end:
                sampling_dates.append(first_business_day)

            # Move to next month
            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1, day=1)
            else:
                current = current.replace(month=current.month + 1, day=1)

        return sampling_dates

    def rate_limit_check(self):
        """Optimized rate limiting"""
        current_time = time.time()

        if current_time - self.minute_start_time >= 60:
            self.api_calls_this_minute = 0
            self.minute_start_time = current_time

        if self.api_calls_this_minute >= self.config['max_calls_per_minute']:
            wait_time = 60 - (current_time - self.minute_start_time)
            if wait_time > 0:
                logger.info(f"      Rate limit: waiting {wait_time:.1f}s...")
                time.sleep(wait_time)
                self.api_calls_this_minute = 0
                self.minute_start_time = time.time()

        # Check for too many consecutive failures
        if self.consecutive_failures >= self.config['max_consecutive_failures']:
            logger.info(f"      Too many failures, taking 2-minute break...")
            time.sleep(self.config['network_failure_delay'])
            self.consecutive_failures = 0

        time.sleep(self.config['rate_limit_delay'])
        self.api_calls_this_minute += 1
        self.total_api_calls += 1

    def fetch_options_iv_data(self, ticker, date_str):
        """Fetch options data and extract IV for ATM and OTM contracts"""
        url = f'https://www.alphavantage.co/query?function=HISTORICAL_OPTIONS&symbol={ticker}&date={date_str}&apikey={self.config["api_key"]}'

        for attempt in range(self.config['max_retries_per_call']):
            try:
                response = requests.get(url, timeout=self.config['timeout'])

                if response.status_code != 200:
                    logger.warning(f"        HTTP {response.status_code}, retrying...")
                    time.sleep(self.config['retry_delay'])
                    continue

                data = response.json()

                # Handle API responses
                if 'Error Message' in data:
                    error_msg = data['Error Message']
                    logger.warning(f"        API Error: {error_msg}")

                    # If it's a symbol not found error, this ticker likely doesn't have options
                    if 'Invalid API call' in error_msg or 'symbol' in error_msg.lower():
                        return 'NO_OPTIONS'
                    return []

                if 'Note' in data and 'rate limit' in data['Note'].lower():
                    logger.info(f"        Rate limited, waiting 60s...")
                    time.sleep(60)
                    continue

                if 'data' not in data or not data['data']:
                    logger.info(f"        No options data available")
                    self.consecutive_failures = 0
                    return 'NO_OPTIONS'

                # Process options data
                df = pd.DataFrame(data['data'])
                if df.empty:
                    self.consecutive_failures = 0
                    return 'NO_OPTIONS'

                # Filter by target maturity and process data
                df['expiration'] = pd.to_datetime(df['expiration'])
                target_date = datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=self.config['target_maturity_days'])

                # Find nearest expiration
                df['days_to_expiry'] = (df['expiration'] - pd.to_datetime(date_str)).dt.days
                valid_expirations = df[df['days_to_expiry'] > 0]

                if valid_expirations.empty:
                    logger.info(f"        No valid expirations")
                    return 'NO_OPTIONS'

                nearest_expiry = min(valid_expirations['expiration'],
                                   key=lambda x: abs((target_date - x).days))
                df_filtered = df[df['expiration'] == nearest_expiry].copy()

                # Convert numeric columns
                df_filtered['strike'] = pd.to_numeric(df_filtered['strike'], errors='coerce')
                df_filtered['implied_volatility'] = pd.to_numeric(df_filtered['implied_volatility'], errors='coerce')

                # Remove rows with missing data
                df_filtered = df_filtered.dropna(subset=['strike', 'implied_volatility'])

                if df_filtered.empty:
                    logger.info(f"        No valid IV data")
                    return 'NO_OPTIONS'

                # Select strikes from options chain directly
                available_strikes = sorted(df_filtered['strike'].unique())

                if len(available_strikes) < 4:
                    logger.info(f"        Insufficient strikes available ({len(available_strikes)})")
                    return 'NO_OPTIONS'

                # Select ATM strike (middle of available range)
                atm_idx = len(available_strikes) // 2
                atm_strike = available_strikes[atm_idx]

                # Select OTM strikes
                otm_call_target = atm_strike * 1.125  # 12.5% above ATM
                otm_call_candidates = [s for s in available_strikes if s > atm_strike]
                if otm_call_candidates:
                    otm_call_strike = min(otm_call_candidates, key=lambda x: abs(x - otm_call_target))
                else:
                    otm_call_strike = available_strikes[-1]  # Highest available

                otm_put_target = atm_strike * 0.875  # 12.5% below ATM
                otm_put_candidates = [s for s in available_strikes if s < atm_strike]
                if otm_put_candidates:
                    otm_put_strike = min(otm_put_candidates, key=lambda x: abs(x - otm_put_target))
                else:
                    otm_put_strike = available_strikes[0]  # Lowest available

                # Extract IV data for each contract type
                iv_records = []

                # ATM Call
                atm_calls = df_filtered[(df_filtered['type'] == 'call') &
                                      (df_filtered['strike'] == atm_strike)]
                if not atm_calls.empty:
                    best_atm_call = atm_calls.iloc[0]
                    iv_records.append({
                        'date': date_str,
                        'ticker': ticker,
                        'index': self.index_ticker,
                        'contract_type': 'ATM_Call',
                        'strike': best_atm_call['strike'],
                        'expiration': best_atm_call['expiration'].strftime('%Y-%m-%d'),
                        'implied_volatility': best_atm_call['implied_volatility'],
                        'days_to_expiry': (nearest_expiry - pd.to_datetime(date_str)).days,
                        'atm_strike': atm_strike,
                        'strike_rank': atm_idx + 1,
                        'total_strikes': len(available_strikes)
                    })

                # ATM Put
                atm_puts = df_filtered[(df_filtered['type'] == 'put') &
                                     (df_filtered['strike'] == atm_strike)]
                if not atm_puts.empty:
                    best_atm_put = atm_puts.iloc[0]
                    iv_records.append({
                        'date': date_str,
                        'ticker': ticker,
                        'index': self.index_ticker,
                        'contract_type': 'ATM_Put',
                        'strike': best_atm_put['strike'],
                        'expiration': best_atm_put['expiration'].strftime('%Y-%m-%d'),
                        'implied_volatility': best_atm_put['implied_volatility'],
                        'days_to_expiry': (nearest_expiry - pd.to_datetime(date_str)).days,
                        'atm_strike': atm_strike,
                        'strike_rank': atm_idx + 1,
                        'total_strikes': len(available_strikes)
                    })

                # OTM Call
                otm_calls = df_filtered[(df_filtered['type'] == 'call') &
                                      (df_filtered['strike'] == otm_call_strike)]
                if not otm_calls.empty:
                    best_otm_call = otm_calls.iloc[0]
                    iv_records.append({
                        'date': date_str,
                        'ticker': ticker,
                        'index': self.index_ticker,
                        'contract_type': 'OTM_Call',
                        'strike': best_otm_call['strike'],
                        'expiration': best_otm_call['expiration'].strftime('%Y-%m-%d'),
                        'implied_volatility': best_otm_call['implied_volatility'],
                        'days_to_expiry': (nearest_expiry - pd.to_datetime(date_str)).days,
                        'atm_strike': atm_strike,
                        'strike_rank': available_strikes.index(otm_call_strike) + 1,
                        'total_strikes': len(available_strikes)
                    })

                # OTM Put
                otm_puts = df_filtered[(df_filtered['type'] == 'put') &
                                     (df_filtered['strike'] == otm_put_strike)]
                if not otm_puts.empty:
                    best_otm_put = otm_puts.iloc[0]
                    iv_records.append({
                        'date': date_str,
                        'ticker': ticker,
                        'index': self.index_ticker,
                        'contract_type': 'OTM_Put',
                        'strike': best_otm_put['strike'],
                        'expiration': best_otm_put['expiration'].strftime('%Y-%m-%d'),
                        'implied_volatility': best_otm_put['implied_volatility'],
                        'days_to_expiry': (nearest_expiry - pd.to_datetime(date_str)).days,
                        'atm_strike': atm_strike,
                        'strike_rank': available_strikes.index(otm_put_strike) + 1,
                        'total_strikes': len(available_strikes)
                    })

                logger.info(f"        Found {len(iv_records)} IV records (ATM: ${atm_strike:.2f})")
                self.consecutive_failures = 0

                # Reset ticker failure counter on success
                if ticker in self.ticker_failures:
                    del self.ticker_failures[ticker]

                return iv_records

            except Exception as e:
                logger.warning(f"        Error: {e}")
                self.consecutive_failures += 1
                time.sleep(self.config['retry_delay'])

        logger.warning(f"        Failed after {self.config['max_retries_per_call']} attempts")
        self.consecutive_failures += 1

        # Track ticker failures
        self.ticker_failures[ticker] = self.ticker_failures.get(ticker, 0) + 1

        return []

    def save_checkpoint(self):
        """Save current IV data to Parquet in the combined portfolio cache directory"""
        if not self.iv_data and (self.existing_data is None or self.existing_data.empty):
            return

        try:
            output_path = os.path.join(self.cache_dir, self.config['output_file'])

            # Combine existing and new data
            if self.existing_data is not None and not self.existing_data.empty:
                # Combine with existing data
                new_df = pd.DataFrame(self.iv_data) if self.iv_data else pd.DataFrame()

                if not new_df.empty:
                    # Ensure date columns are consistent
                    new_df['date'] = pd.to_datetime(new_df['date'])
                    self.existing_data['date'] = pd.to_datetime(self.existing_data['date'])

                    # Combine dataframes
                    combined_df = pd.concat([self.existing_data, new_df], ignore_index=True)
                else:
                    combined_df = self.existing_data.copy()
            else:
                combined_df = pd.DataFrame(self.iv_data) if self.iv_data else pd.DataFrame()

            if combined_df.empty:
                logger.warning("No data to save")
                return

            # Remove duplicates and sort
            combined_df = combined_df.drop_duplicates(
                subset=['date', 'ticker', 'contract_type'],
                keep='last'
            )
            combined_df = combined_df.sort_values(['date', 'ticker', 'contract_type']).reset_index(drop=True)

            # Save to Parquet (atomic write via temp file)
            temp_path = output_path + '.tmp'
            combined_df.to_parquet(temp_path, index=False)
            os.replace(temp_path, output_path)  # Atomic rename

            logger.info(f"        Saved checkpoint: {len(combined_df)} total records to {output_path}")

        except Exception as e:
            logger.warning(f"Checkpoint save error: {e}")

    def run_iv_collection(self):
        """Main collection process with smart cache management"""
        logger.info(f"\nANALYZING EXISTING DATA...")
        self.analyze_existing_data()

        logger.info(f"\nCREATING FETCH PLAN...")
        self.create_fetch_plan()

        if self.fetch_plan['to_fetch'] == 0:
            logger.info(f"\nALL DATA IS UP TO DATE!")
            logger.info(f"Total records: {self.data_analysis.get('existing_records', 0)}")
            return True

        logger.info(f"\nSTARTING {self.index_ticker} IV COLLECTION...")
        logger.info(f"Will fetch {self.fetch_plan['to_fetch']} missing combinations")
        logger.info("-" * 50)

        successful_fetches = 0
        failed_fetches = 0
        no_options_count = 0

        try:
            # Convert missing combinations to list for indexing
            missing_combinations = list(self.fetch_plan['missing_combinations'])

            # Resume from where we left off
            start_index = self.current_state.get('current_combination_index', 0)
            completed_combinations = set(self.current_state.get('completed_combinations', []))

            for combo_idx in range(start_index, len(missing_combinations)):
                ticker, date_str = missing_combinations[combo_idx]

                # Skip if already completed
                if (ticker, date_str) in completed_combinations:
                    continue

                # Skip if ticker is blacklisted
                if self._is_ticker_blacklisted(ticker):
                    continue

                self.current_state['current_combination_index'] = combo_idx

                progress = ((combo_idx + 1) / len(missing_combinations)) * 100
                logger.info(f"\n[{combo_idx+1}/{len(missing_combinations)}] {ticker} - {date_str} - {progress:.1f}%")

                self.rate_limit_check()

                iv_result = self.fetch_options_iv_data(ticker, date_str)

                if iv_result == 'NO_OPTIONS':
                    no_options_count += 1

                    # Check if this ticker should be blacklisted
                    ticker_failure_count = self.ticker_failures.get(ticker, 0)
                    if ticker_failure_count >= self.config['max_consecutive_ticker_failures']:
                        self._blacklist_ticker(ticker, f"No options data after {ticker_failure_count} attempts")

                elif isinstance(iv_result, list) and iv_result:
                    self.iv_data.extend(iv_result)
                    self.total_records_collected += len(iv_result)
                    successful_fetches += 1

                    # Reset ticker failure counter
                    if ticker in self.ticker_failures:
                        del self.ticker_failures[ticker]

                else:
                    failed_fetches += 1

                # Mark combination as completed
                completed_combinations.add((ticker, date_str))
                self.current_state['completed_combinations'] = completed_combinations

                # Checkpoint save
                if (successful_fetches + no_options_count) % self.config['checkpoint_frequency'] == 0:
                    self.save_checkpoint()
                    self.save_state()

                # Progress update
                if (combo_idx + 1) % 50 == 0:
                    logger.info(f"    Progress: {successful_fetches} successful, {no_options_count} no options, {failed_fetches} failed")

        except KeyboardInterrupt:
            logger.info(f"\nPROCESS INTERRUPTED - Progress saved!")
            self.save_checkpoint()
            self.save_state()
            return False

        except Exception as e:
            logger.error(f"\nUnexpected error: {e}")
            self.save_checkpoint()
            self.save_state()
            return False

        # Final save
        self.save_checkpoint()

        # Success summary
        duration = datetime.now() - self.session_start_time

        logger.info(f"\n{self.index_ticker} IV COLLECTION COMPLETED!")
        logger.info("=" * 60)
        logger.info(f"Version: {self.version}")
        logger.info(f"Index: {self.index_ticker}")
        logger.info(f"Session duration: {duration}")
        logger.info(f"Total API calls: {self.total_api_calls}")
        logger.info(f"Successful fetches: {successful_fetches}")
        logger.info(f"No options available: {no_options_count}")
        logger.info(f"Failed fetches: {failed_fetches}")
        logger.info(f"New IV records: {self.total_records_collected}")
        logger.info(f"Output file: {os.path.join(self.cache_dir, self.config['output_file'])}")

        if self.ticker_blacklist:
            logger.info(f"Blacklisted tickers: {len(self.ticker_blacklist)}")

        # Clean up state file
        try:
            os.remove(self.state_file_path)
            logger.info(f"Cleaned up state file")
        except:
            pass

        return True


def run_enhanced_iv_collection(index_ticker='VCR'):
    """
    Run enhanced IV collection for a specific index with smart cache management.
    Uses config for directory structure routing.

    Args:
        index_ticker (str): The index ticker symbol (default: 'VCR')

    Usage:
        # For VCR (default)
        run_enhanced_iv_collection()

        # For other indices
        run_enhanced_iv_collection('VGT')
        run_enhanced_iv_collection('VHT')
        run_enhanced_iv_collection('XLF')
    """
    logger.info(f"Starting {index_ticker} Enhanced Options IV Collection...")
    logger.info("Smart cache management: Only fetches missing data")
    logger.info("Ticker failure tracking: Avoids repeatedly trying tickers without options")
    logger.info("Data format: Parquet (fast, compact, crash-safe)")
    logger.info("=" * 60)

    try:
        fetcher = EnhancedOptionsIVFetcher(index_ticker=index_ticker)
        success = fetcher.run_iv_collection()

        if success:
            logger.info(f"\n{index_ticker} IV COLLECTION COMPLETE!")
            logger.info(f"Monthly implied volatility data ready for analysis")
            logger.info(f"ATM and OTM IV data for all {index_ticker} constituents since 2017")
            logger.info(f"Saved to: {fetcher.cache_dir}")
        else:
            logger.info(f"\nPROGRESS SAVED")
            logger.info(f"Run this script again to resume from last checkpoint")

        logger.info(f"\nDATA STRUCTURE:")
        logger.info(f"  Monthly sampling (1st business day of each month)")
        logger.info(f"  4 contracts per ticker per month: ATM Call/Put + 10% OTM Call/Put")
        logger.info(f"  Key fields: date, ticker, index, contract_type, strike, implied_volatility")

        return success

    except FileNotFoundError as e:
        logger.error(f"FILE ERROR: {e}")
        logger.info(f"\nPlease ensure:")
        logger.info(f"1. The {index_ticker} directory exists in the correct version directory")
        logger.info(f"2. The file {index_ticker}_Pair_Trading_Results.xlsx exists in that directory")
        logger.info(f"3. The file has a 'Ticker Statistics' sheet with a 'Ticker' column")
        return False

    except Exception as e:
        logger.error(f"UNEXPECTED ERROR: {e}")
        return False


def run_multiple_indexes(indexes, resume_from=None):
    """Run IV collection for multiple indexes with resume capability"""

    if resume_from:
        start_idx = indexes.index(resume_from)
        indexes = indexes[start_idx:]
        print(f"Resuming from {resume_from}")

    completed = []
    failed = []

    for i, index in enumerate(indexes):
        print(f"\n{'='*60}")
        print(f"[{i+1}/{len(indexes)}] PROCESSING {index}")
        print(f"{'='*60}")

        try:
            success = run_enhanced_iv_collection(index)

            if success:
                completed.append(index)
                print(f"{index} completed successfully")
            else:
                failed.append(index)
                print(f"{index} failed")

        except KeyboardInterrupt:
            print(f"\nInterrupted during {index}")
            print(f"Completed: {completed}")
            print(f"Failed: {failed}")
            print(f"To resume, use: run_multiple_indexes({indexes}, resume_from='{index}')")
            return False

        except Exception as e:
            failed.append(index)
            print(f"{index} error: {e}")

    print(f"\nBATCH PROCESSING COMPLETE")
    print(f"Completed: {completed}")
    print(f"Failed: {failed}")
    return len(failed) == 0


# Usage
if __name__ == "__main__":
    indexes = ['VGT', 'VHT', 'VFH', 'VIS', 'VCR']
    run_multiple_indexes(indexes)
