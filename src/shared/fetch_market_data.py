"""
Market data fetching module with session-based caching. Provides batch
fetching of live prices, historical daily bars, and intraday volume data
via IBKR and yfinance.

Session caches are date-stamped and automatically cleaned up on import.
Old cache files from previous days are removed.

STATUS: live
"""

import asyncio
import pandas as pd
import numpy as np
try:
    from ib_insync import IB, Stock, util
except ImportError:
    IB = Stock = util = None
import time
from datetime import datetime, timedelta
import pytz
import yfinance as yf
import pickle
import os

from src.shared import config

# ==================================================
# SESSION-BASED CACHE (In-Memory + Optional File)
# ==================================================

_CACHE_DIR = config.cache_dir()
if _CACHE_DIR:
    os.makedirs(_CACHE_DIR, exist_ok=True)

# Generate session-specific cache filename with today's date
_TODAY_STR = datetime.now().strftime('%Y%m%d')
_HISTORICAL_CACHE_FILE = os.path.join(_CACHE_DIR, f'historical_data_cache_{_TODAY_STR}.pkl') if _CACHE_DIR else ''
_VOLUME_CACHE_FILE = os.path.join(_CACHE_DIR, f'volume_data_cache_{_TODAY_STR}.pkl') if _CACHE_DIR else ''

# Public alias for downstream consumers (lam.py)
HISTORICAL_CACHE_FILE = _HISTORICAL_CACHE_FILE


def cleanup_old_cache_files():
    """Delete cache files from previous days."""
    try:
        if not os.path.exists(_CACHE_DIR):
            return

        today_str = datetime.now().strftime('%Y%m%d')

        for filename in os.listdir(_CACHE_DIR):
            if filename.startswith('historical_data_cache_') or filename.startswith('volume_data_cache_'):
                # Extract date from filename
                if filename.endswith('.pkl'):
                    file_date = filename.split('_')[-1].replace('.pkl', '')

                    # Delete if not from today
                    if file_date != today_str:
                        old_file = os.path.join(_CACHE_DIR, filename)
                        os.remove(old_file)
                        print(f"Cleaned up old cache: {filename}")
    except Exception as e:
        print(f"Warning: Error cleaning old cache files: {e}")


def save_to_cache(data, filename):
    """Save data to session cache file."""
    try:
        with open(filename, 'wb') as f:
            pickle.dump(data, f)
    except Exception as e:
        print(f"Warning: Could not save to cache {filename}: {e}")


def load_from_cache(filename):
    """
    Load data from cache file (only if from today's session).
    No expiry check needed -- filename includes date.
    """
    try:
        if not os.path.exists(filename):
            return None

        with open(filename, 'rb') as f:
            return pickle.load(f)
    except Exception as e:
        print(f"Warning: Error loading from cache {filename}: {e}")
        return None


# Clean up old caches on import
cleanup_old_cache_files()

# ==================================================
# Global Contract Cache (In-Memory Only)
# ==================================================

_CONTRACT_CACHE = {}


async def qualify_contracts_batch(ib, tickers):
    """Pre-qualify all contracts in a single batch."""
    if not ib or not ib.isConnected():
        print("Warning: IB connection not available for contract qualification")
        return {}

    print(f"Pre-qualifying {len(tickers)} contracts...")
    start_time = time.time()

    new_tickers = [t for t in tickers if t not in _CONTRACT_CACHE and t != "^FVX"]

    if new_tickers:
        contracts = [Stock(ticker, 'SMART', 'USD') for ticker in new_tickers]

        try:
            await ib.qualifyContractsAsync(*contracts)

            for ticker, contract in zip(new_tickers, contracts):
                _CONTRACT_CACHE[ticker] = contract

            print(f"Qualified {len(new_tickers)} new contracts")
        except Exception as e:
            print(f"Error qualifying contracts: {e}")

    elapsed = time.time() - start_time
    print(f"Contract qualification completed in {elapsed:.2f} seconds")
    return _CONTRACT_CACHE


# ==================================================
# Market Data Fetching
# ==================================================

async def fetch_market_data_batch(ib, tickers, batch_size=50, wait_time=5, max_wait=10):
    """Fetch live market data (price, bid, ask, spread) in batches with smart waiting."""
    if not ib or not ib.isConnected():
        print("Warning: IB connection not available for market data")
        return {}

    print(f"Fetching live market data for {len(tickers)} tickers...")
    start_time = time.time()

    market_data = {}

    for i in range(0, len(tickers), batch_size):
        batch_tickers = tickers[i:i+batch_size]

        batch_contracts = []
        valid_tickers = []
        for ticker in batch_tickers:
            if ticker in _CONTRACT_CACHE:
                batch_contracts.append(_CONTRACT_CACHE[ticker])
                valid_tickers.append(ticker)

        if not batch_contracts:
            continue

        # Request market data
        ticker_data_reqs = {}
        for ticker, contract in zip(valid_tickers, batch_contracts):
            ticker_data_reqs[ticker] = ib.reqMktData(contract, '', False, False)

        # Wait with progress checking
        await asyncio.sleep(wait_time)

        # Check if data has arrived, wait a bit more if needed
        for attempt in range(3):
            pending = []
            for ticker, data_req in ticker_data_reqs.items():
                if not data_req.bid or not data_req.ask:
                    pending.append(ticker)

            if not pending or attempt == 2:
                break

            # Wait a bit more for pending tickers
            await asyncio.sleep(2)

        # Now extract data
        for ticker, data_req in ticker_data_reqs.items():
            try:
                live_price = data_req.last if data_req.last else data_req.close
                bid = data_req.bid
                ask = data_req.ask

                # Handle None and NaN properly
                if bid is None or pd.isna(bid) or bid <= 0:
                    bid = None
                if ask is None or pd.isna(ask) or ask <= 0:
                    ask = None

                spread = None
                if bid is not None and ask is not None:
                    if bid > ask:
                        print(f"  Inverted bid/ask for {ticker}: bid={bid:.2f}, ask={ask:.2f}")
                        spread = None
                    else:
                        mid = (bid + ask) / 2
                        if mid > 0:
                            spread = (ask - bid) / mid

                market_data[ticker] = {
                    'live_price': live_price,
                    'bid': bid,
                    'ask': ask,
                    'spread': spread,
                    'mid': (bid + ask) / 2 if (bid and ask) else None
                }

                # Cancel subscription
                ib.cancelMktData(_CONTRACT_CACHE[ticker])

            except Exception as e:
                print(f"  Error processing market data for {ticker}: {e}")
                market_data[ticker] = {
                    'live_price': None,
                    'bid': None,
                    'ask': None,
                    'spread': None,
                    'mid': None
                }

        if i + batch_size < len(tickers):
            await asyncio.sleep(0.5)

    # Count success rate
    has_bid_ask = sum(1 for v in market_data.values() if v['bid'] is not None and v['ask'] is not None)
    total = len(market_data)

    elapsed = time.time() - start_time
    print(f"Live market data fetched in {elapsed:.2f} seconds")
    print(f"  Bid/Ask available: {has_bid_ask}/{total} ({has_bid_ask/total*100:.1f}%)")

    if has_bid_ask < total * 0.8:
        print(f"  WARNING: Low bid/ask success rate - may need market data subscriptions")

    return market_data


async def fetch_historical_data_batch(ib, tickers, vgt_earliest, vgt_latest,
                                      batch_size=20, use_cache=True):
    """
    Fetch historical daily price data in batches.

    Args:
        use_cache: If True, uses today's session cache. If False, forces fresh fetch.

    Adaptive behavior:
        - When cache is warm (>50% hit rate): fast settings (batch=20, timeout=10s, delay=0.5s)
        - When cache is cold (<50% hit rate): conservative settings (batch=10, timeout=30s, delay=1.5s)
    """
    if not ib or not ib.isConnected():
        print("Warning: IB connection not available for historical data")
        return {}

    print(f"Fetching historical data for {len(tickers)} tickers...")
    start_time = time.time()

    # Load session cache if enabled
    historical_cache = load_from_cache(_HISTORICAL_CACHE_FILE) if use_cache else {}
    if not historical_cache:
        historical_cache = {}

    new_data = {}
    tickers_to_fetch = []

    # Check what's already in cache
    for ticker in tickers:
        if use_cache and ticker in historical_cache:
            cached_data = historical_cache[ticker]

            # Use cache if it has data (no date check needed - cache is today's)
            if not cached_data.empty:
                new_data[ticker] = cached_data
            else:
                tickers_to_fetch.append(ticker)
        else:
            tickers_to_fetch.append(ticker)

    # ADAPTIVE SETTINGS based on cache status
    cache_hit_rate = len(new_data) / len(tickers) if len(tickers) > 0 else 0
    cold_cache = cache_hit_rate < 0.5

    if cold_cache and len(tickers_to_fetch) > 100:
        # Cold cache with many tickers: use conservative settings
        effective_batch_size = 10
        effective_timeout = 30
        effective_delay = 1.5
        print(f"   Cold cache detected ({len(new_data)}/{len(tickers)} cached)")
        print(f"   Using conservative settings: batch={effective_batch_size}, timeout={effective_timeout}s, delay={effective_delay}s")
    else:
        # Warm cache or small fetch: use fast settings
        effective_batch_size = batch_size
        effective_timeout = 10
        effective_delay = 0.5

    if len(tickers_to_fetch) > 0:
        print(f"   Fetching fresh data for {len(tickers_to_fetch)} tickers...")

    # Fetch missing tickers
    for i in range(0, len(tickers_to_fetch), effective_batch_size):
        batch_tickers = tickers_to_fetch[i:i+effective_batch_size]

        fetch_tasks = []
        for ticker in batch_tickers:
            if ticker == "^FVX":
                continue

            if ticker in _CONTRACT_CACHE:
                task = fetch_historical_data_for_ticker(ib, _CONTRACT_CACHE[ticker], ticker, timeout=effective_timeout)
                fetch_tasks.append(task)

        if fetch_tasks:
            batch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

            for result in batch_results:
                if isinstance(result, Exception):
                    continue

                ticker, hist_data = result
                if not hist_data.empty:
                    new_data[ticker] = hist_data
                    historical_cache[ticker] = hist_data

            # Progress indicator for cold cache
            if cold_cache and len(tickers_to_fetch) > 100:
                fetched_so_far = min(i + effective_batch_size, len(tickers_to_fetch))
                if fetched_so_far % 50 == 0 or fetched_so_far == len(tickers_to_fetch):
                    print(f"   Progress: {fetched_so_far}/{len(tickers_to_fetch)} tickers fetched...")

            if i + effective_batch_size < len(tickers_to_fetch):
                await asyncio.sleep(effective_delay)

    # Save updated cache
    if use_cache:
        save_to_cache(historical_cache, _HISTORICAL_CACHE_FILE)

    elapsed = time.time() - start_time
    print(f"Historical data fetched in {elapsed:.2f} seconds")
    return new_data


async def fetch_historical_data_for_ticker(ib, contract, ticker, timeout=10):
    """Fetch historical data for a single ticker.

    Args:
        timeout: Request timeout in seconds (default 10, use 30 for cold cache)
    """
    try:
        bars = await asyncio.wait_for(
            ib.reqHistoricalDataAsync(
                contract,
                endDateTime='',  # Empty = most recent
                durationStr='365 D',
                barSizeSetting='1 day',
                whatToShow='TRADES',
                useRTH=True,
                formatDate=1
            ),
            timeout=timeout
        )

        historical_df = util.df(bars)
        if not historical_df.empty:
            historical_df['date'] = pd.to_datetime(historical_df['date'], utc=True)
            historical_df.set_index('date', inplace=True)
            historical_df = historical_df[['close']]
            return ticker, historical_df
        else:
            return ticker, pd.DataFrame()
    except Exception as e:
        print(f"Error fetching historical data for {ticker}: {e}")
        return ticker, pd.DataFrame()


# ==================================================
# Volume Data Functions (session caching for performance)
# ==================================================

async def fetch_intraday_volume_data(ib, ticker, start_time, end_time):
    """Fetch intraday volume data for a ticker with session caching."""
    if not ib or not ib.isConnected():
        print(f"IB connection not available for volume data for {ticker}")
        return pd.DataFrame(columns=['Datetime', 'Volume'])

    start_date_str = start_time.strftime("%Y%m%d")
    end_date_str = end_time.strftime("%Y%m%d")
    cache_key = f"{ticker}_{start_date_str}_{end_date_str}"

    volume_cache = load_from_cache(_VOLUME_CACHE_FILE) or {}
    if cache_key in volume_cache:
        return volume_cache[cache_key]

    if ticker not in _CONTRACT_CACHE:
        try:
            contract = Stock(ticker, 'SMART', 'USD')
            await ib.qualifyContractsAsync(contract)
            _CONTRACT_CACHE[ticker] = contract
        except Exception as e:
            print(f"Error qualifying contract for {ticker}: {e}")
            return pd.DataFrame(columns=['Datetime', 'Volume'])

    try:
        days_to_fetch = (end_time - start_time).days + 1
        days_str = f"{days_to_fetch} D"

        bars = await asyncio.wait_for(
            ib.reqHistoricalDataAsync(
                _CONTRACT_CACHE[ticker],
                endDateTime=end_time.strftime('%Y%m%d %H:%M:%S'),
                durationStr=days_str,
                barSizeSetting='2 hours',
                whatToShow='TRADES',
                useRTH=True,
                formatDate=1
            ),
            timeout=10
        )

        if bars:
            volume_data = pd.DataFrame([(bar.date, bar.volume) for bar in bars],
                                       columns=['Datetime', 'Volume'])
            volume_data.set_index('Datetime', inplace=True)

            if volume_data.index.tz is None:
                volume_data.index = pd.to_datetime(volume_data.index, utc=True)

            volume_cache[cache_key] = volume_data
            save_to_cache(volume_cache, _VOLUME_CACHE_FILE)

            return volume_data
        else:
            return pd.DataFrame(columns=['Datetime', 'Volume'])

    except Exception as e:
        print(f"Error fetching intraday volume data for {ticker}: {e}")
        return pd.DataFrame(columns=['Datetime', 'Volume'])


async def fetch_intraday_volume_simple_extrapolation(ib, ticker, lookback_days=10):
    """Extrapolate current incomplete day volume using recent averages."""
    end_time = datetime.now(pytz.UTC)
    start_time = end_time - timedelta(days=lookback_days + 1)

    volume_data = await fetch_intraday_volume_data(ib, ticker, start_time, end_time)

    if volume_data.empty:
        return None

    current_date = datetime.now(pytz.UTC).date()
    current_hour = datetime.now(pytz.UTC).hour

    volume_data['date'] = pd.to_datetime(volume_data.index).date
    volume_data['hour'] = pd.to_datetime(volume_data.index).hour

    historical = volume_data[volume_data['date'] < current_date]
    current = volume_data[volume_data['date'] == current_date]

    if len(historical) == 0 or len(current) == 0:
        return None

    historical_daily = historical.groupby('date')['Volume'].sum()

    if len(historical_daily) == 0:
        return None

    current_volume_so_far = current['Volume'].sum()

    historical_partial = historical[historical['hour'] <= current_hour]
    partial_daily = historical_partial.groupby('date')['Volume'].sum()

    valid_dates = partial_daily.index.intersection(historical_daily.index)

    if len(valid_dates) == 0:
        extrapolated_volume = current_volume_so_far / 0.85
        return extrapolated_volume

    completion_ratios = partial_daily.loc[valid_dates] / historical_daily.loc[valid_dates]
    avg_completion_ratio = completion_ratios.mean()

    if avg_completion_ratio > 0 and not pd.isna(avg_completion_ratio):
        extrapolated_volume = current_volume_so_far / avg_completion_ratio
    else:
        extrapolated_volume = current_volume_so_far * 1.18

    return extrapolated_volume


async def fetch_daily_volumes_with_extrapolation(ib, ticker, lookback_days=30):
    """Fetch daily volume data including extrapolation for current incomplete day."""
    end_time = datetime.now(pytz.UTC)
    start_time = end_time - timedelta(days=lookback_days + 1)

    volume_data = await fetch_intraday_volume_data(ib, ticker, start_time, end_time)

    if volume_data.empty:
        return pd.Series(dtype=float)

    current_date = datetime.now(pytz.UTC).date()

    volume_data['date'] = pd.to_datetime(volume_data.index).date

    historical = volume_data[volume_data['date'] < current_date]
    current = volume_data[volume_data['date'] == current_date]

    daily_volumes = historical.groupby('date')['Volume'].sum()

    if len(current) > 0:
        extrapolated_today = await fetch_intraday_volume_simple_extrapolation(ib, ticker, lookback_days=10)
        if extrapolated_today is not None:
            daily_volumes.loc[current_date] = extrapolated_today

    return daily_volumes


async def get_hourly_price_data(ib, ticker, lookback_days=30):
    """Fetch hourly price data for volatility calculations."""
    if not ib or not ib.isConnected():
        print(f"IB connection not available for hourly data for {ticker}")
        return pd.DataFrame()

    if ticker not in _CONTRACT_CACHE:
        try:
            contract = Stock(ticker, 'SMART', 'USD')
            await ib.qualifyContractsAsync(contract)
            _CONTRACT_CACHE[ticker] = contract
        except Exception as e:
            print(f"Error qualifying contract for {ticker}: {e}")
            return pd.DataFrame()

    try:
        bars = await asyncio.wait_for(
            ib.reqHistoricalDataAsync(
                _CONTRACT_CACHE[ticker],
                endDateTime='',
                durationStr=f'{lookback_days} D',
                barSizeSetting='1 hour',
                whatToShow='TRADES',
                useRTH=True,
                formatDate=1
            ),
            timeout=15
        )

        if bars:
            df = pd.DataFrame([{
                'date': bar.date,
                'open': bar.open,
                'high': bar.high,
                'low': bar.low,
                'close': bar.close,
                'volume': bar.volume
            } for bar in bars])

            df['date'] = pd.to_datetime(df['date'], utc=True)
            df.set_index('date', inplace=True)

            return df
        else:
            return pd.DataFrame()

    except Exception as e:
        print(f"Error fetching hourly data for {ticker}: {e}")
        return pd.DataFrame()


async def fetch_live_prices_batch(ib, tickers, batch_size=50):
    """Fast fetch of only live prices (no historical data)."""
    print(f"Fetching live prices for {len(tickers)} tickers...")

    live_prices = {}

    # Qualify contracts
    await qualify_contracts_batch(ib, tickers)

    # Batch request live data
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i+batch_size]

        ticker_data_reqs = {}
        for ticker in batch:
            if ticker in _CONTRACT_CACHE:
                ticker_data_reqs[ticker] = ib.reqMktData(_CONTRACT_CACHE[ticker])

        await asyncio.sleep(2)

        for ticker, data_req in ticker_data_reqs.items():
            live_price = data_req.last if data_req.last else data_req.close
            bid = data_req.bid
            ask = data_req.ask

            spread = None
            if bid and ask and bid > 0 and ask > 0 and bid <= ask:
                spread = (ask - bid) / ((bid + ask) / 2)

            live_prices[ticker] = {
                'live_price': live_price,
                'bid': bid,
                'ask': ask,
                'spread': spread
            }

            ib.cancelMktData(_CONTRACT_CACHE[ticker])

    print(f"Fetched live prices for {len(live_prices)} tickers")
    return live_prices


async def fetch_live_prices_batch_v2(ib, tickers, timeout=30):
    """
    Fast parallel price fetching using snapshots.

    Key improvements:
    1. Uses reqTickers() -- IBKR's bulk ticker method
    2. True parallel execution
    3. Snapshot mode -- returns last price immediately
    4. Single timeout for all tickers (not per-ticker)
    """
    # Add megacap tickers for index return adjustment
    try:
        from src.shared.calculations import get_megacap_tickers_for_fetch
        megacap_tickers = get_megacap_tickers_for_fetch()
        original_count = len(tickers)
        tickers = list(set(tickers) | megacap_tickers)
        megacap_added = len(tickers) - original_count
        if megacap_added > 0:
            print(f"Fetching live prices for {len(tickers)} tickers (+{megacap_added} megacaps for adjustment)...")
        else:
            print(f"Fetching live prices for {len(tickers)} tickers...")
    except Exception:
        # Fallback if megacap function fails - continue without megacaps
        print(f"Fetching live prices for {len(tickers)} tickers...")

    start_time = time.time()

    # Qualify all contracts in parallel (using existing cache)
    await qualify_contracts_batch(ib, tickers)

    # Request all tickers at once using reqTickers()
    contracts = [_CONTRACT_CACHE[ticker] for ticker in tickers if ticker in _CONTRACT_CACHE]
    ticker_map = {_CONTRACT_CACHE[ticker].symbol: ticker for ticker in tickers if ticker in _CONTRACT_CACHE}

    print(f"  Requesting market data for {len(contracts)} contracts...")

    try:
        # Request all tickers at once
        tickers_data = await asyncio.wait_for(
            ib.reqTickersAsync(*contracts, regulatorySnapshot=False),
            timeout=timeout
        )

        live_prices = {}

        for ticker_obj in tickers_data:
            symbol = ticker_obj.contract.symbol
            ticker = ticker_map.get(symbol, symbol)

            # Extract prices
            last = ticker_obj.last
            close = ticker_obj.close
            marketPrice = ticker_obj.marketPrice()

            # Priority: last > marketPrice > close
            live_price = last if (last and last > 0) else (
                marketPrice if (marketPrice and marketPrice > 0) else close
            )

            bid = ticker_obj.bid if ticker_obj.bid and ticker_obj.bid > 0 else None
            ask = ticker_obj.ask if ticker_obj.ask and ticker_obj.ask > 0 else None

            # Calculate spread
            spread = None
            if bid and ask and bid <= ask:
                spread = (ask - bid) / ((bid + ask) / 2)

            live_prices[ticker] = {
                'live_price': live_price,
                'bid': bid,
                'ask': ask,
                'spread': spread
            }

        # Report results
        valid_prices = sum(1 for v in live_prices.values()
                          if v['live_price'] is not None and v['live_price'] > 0)
        missing_prices = len(tickers) - valid_prices

        elapsed = time.time() - start_time
        print(f"Fetched {valid_prices}/{len(tickers)} valid prices in {elapsed:.2f}s")

        if missing_prices > 0:
            print(f"  {missing_prices} tickers have missing prices")

        return live_prices

    except asyncio.TimeoutError:
        print(f"  Timeout after {timeout}s - using fallback method")
        return await fetch_live_prices_batch_fallback(ib, tickers, timeout)

    except Exception as e:
        print(f"  Error in reqTickersAsync: {e}")
        import traceback
        traceback.print_exc()
        return await fetch_live_prices_batch_fallback(ib, tickers, timeout)


async def fetch_live_prices_batch_fallback(ib, tickers, timeout=30):
    """
    Fallback method using individual snapshot requests.
    Still faster than batch method because uses snapshots + proper async.
    """
    print(f"  Using fallback: individual snapshots for {len(tickers)} tickers...")

    async def get_snapshot(ticker):
        """Get snapshot for single ticker."""
        if ticker not in _CONTRACT_CACHE:
            return ticker, None

        try:
            contract = _CONTRACT_CACHE[ticker]

            # Request snapshot (returns immediately with last available data)
            ticker_obj = await asyncio.wait_for(
                ib.reqMktDataAsync(contract, snapshot=True),
                timeout=5
            )

            # Extract price
            last = ticker_obj.last
            close = ticker_obj.close
            marketPrice = ticker_obj.marketPrice()

            live_price = last if (last and last > 0) else (
                marketPrice if (marketPrice and marketPrice > 0) else close
            )

            bid = ticker_obj.bid if ticker_obj.bid and ticker_obj.bid > 0 else None
            ask = ticker_obj.ask if ticker_obj.ask and ticker_obj.ask > 0 else None

            spread = None
            if bid and ask and bid <= ask:
                spread = (ask - bid) / ((bid + ask) / 2)

            return ticker, {
                'live_price': live_price,
                'bid': bid,
                'ask': ask,
                'spread': spread
            }

        except Exception as e:
            print(f"    Error getting snapshot for {ticker}: {e}")
            return ticker, {
                'live_price': None,
                'bid': None,
                'ask': None,
                'spread': None
            }

    # Execute all snapshots in parallel
    tasks = [get_snapshot(ticker) for ticker in tickers]

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout
        )

        live_prices = {}
        for result in results:
            if isinstance(result, Exception):
                continue
            ticker, data = result
            if data:
                live_prices[ticker] = data

        valid_prices = sum(1 for v in live_prices.values()
                          if v['live_price'] is not None and v['live_price'] > 0)

        print(f"  Fallback got {valid_prices}/{len(tickers)} valid prices")
        return live_prices

    except asyncio.TimeoutError:
        print(f"  Fallback timed out after {timeout}s")
        return {}


# ==================================================
# Main Function
# ==================================================

async def fetch_all_data(tickers, ib, treasury_earliest, treasury_latest,
                         index_tickers=None, timeout=300, force_refresh=False):
    """
    Fetch all required market data for the workflow.

    Args:
        tickers: List of stock tickers to fetch
        ib: IB connection
        treasury_earliest: Start date for historical data range
        treasury_latest: End date for historical data range
        index_tickers: List of index ETF tickers (e.g., ['VGT', 'VIS', 'VCR', 'VHT', 'VFH'])
        timeout: Request timeout
        force_refresh: If True, ignores session cache and fetches fresh data
    """
    if index_tickers is None:
        index_tickers = ['VGT', 'VIS', 'VCR', 'VHT', 'VFH']

    # Add megacap tickers for index return adjustment
    try:
        from src.shared.calculations import get_megacap_tickers_for_fetch
        megacap_tickers = get_megacap_tickers_for_fetch()
        original_count = len(tickers)
        tickers = list(set(tickers) | megacap_tickers)
        megacap_added = len(tickers) - original_count
        if megacap_added > 0:
            print(f"Starting fetch_all_data for {len(tickers)} tickers (+{megacap_added} megacaps for adjustment)...")
        else:
            print(f"Starting fetch_all_data for {len(tickers)} tickers...")
    except ImportError:
        print(f"Starting fetch_all_data for {len(tickers)} tickers...")

    if force_refresh:
        print("   Force refresh: Ignoring session cache")

    start_time = time.time()
    all_market_data = {}

    try:
        # Separate special tickers from regular stocks
        special_tickers = set(index_tickers)
        stock_tickers = [t for t in tickers if t not in special_tickers]

        # Qualify stock contracts
        await qualify_contracts_batch(ib, stock_tickers)

        # Fetch all index ETFs that are in the ticker list
        for index_ticker in index_tickers:
            if index_ticker in tickers:
                if index_ticker not in _CONTRACT_CACHE:
                    contract = Stock(index_ticker, 'SMART', 'USD')
                    await ib.qualifyContractsAsync(contract)
                    _CONTRACT_CACHE[index_ticker] = contract

        # Fetch live market data and historical data
        all_tradable = stock_tickers + [idx for idx in index_tickers if idx in tickers]

        live_data = await fetch_market_data_batch(ib, all_tradable)
        historical_data = await fetch_historical_data_batch(
            ib, all_tradable, treasury_earliest, treasury_latest,
            use_cache=(not force_refresh)
        )

        # Combine data
        for ticker in all_tradable:
            ticker_data = {}

            if ticker in live_data:
                ticker_data.update(live_data[ticker])
            else:
                ticker_data.update({
                    'live_price': None,
                    'bid': None,
                    'ask': None,
                    'spread': None
                })

            if ticker in historical_data:
                ticker_data['historical_data'] = historical_data[ticker]
            else:
                ticker_data['historical_data'] = pd.DataFrame()

            all_market_data[ticker] = ticker_data

        elapsed = time.time() - start_time
        print(f"fetch_all_data completed in {elapsed:.2f} seconds for {len(all_market_data)} tickers")

        # Verify data freshness
        _verify_data_freshness(all_market_data)

        return all_market_data

    except Exception as e:
        import traceback
        print(f"Error in fetch_all_data: {e}")
        traceback.print_exc()
        return all_market_data


def _verify_data_freshness(market_data):
    """Verify that fetched data is current (not old cached data)."""
    today = datetime.now().date()

    for ticker in ['VGT']:
        if ticker in market_data:
            hist = market_data[ticker].get('historical_data')
            if hist is not None and not hist.empty:
                last_date = hist.index[-1].date()
                days_old = (today - last_date).days

                if days_old > 2:
                    print(f"   WARNING: {ticker} data is {days_old} days old")
                else:
                    print(f"   {ticker} data is current (last: {last_date})")


