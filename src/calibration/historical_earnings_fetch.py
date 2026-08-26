#!/usr/bin/env python3
"""
Historical earnings date fetcher for the trading universe.

Loads tickers from the universe determination output, fetches historical
earnings dates from yfinance, determines pre-market vs post-market timing,
calculates the affected trading date, and saves results to Excel and pickle
for downstream use.

STATUS: live
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta, date
from pathlib import Path
import time
import warnings
warnings.filterwarnings('ignore')

import yfinance as yf

from src.shared import config

# =============================================================================
# VERSION CONFIGURATION
# =============================================================================

VERSION = config.active_version()
VERSION_DIR = config.get_version_dir(VERSION)

# =============================================================================
# CONFIGURATION
# =============================================================================
OUTPUT_DIR = Path(VERSION_DIR)

# Read tickers from Universe Determination output
UNIVERSE_FILE = OUTPUT_DIR / f"{VERSION} All Vanguard ETF Tickers.xlsx"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
HISTORICAL_EARNINGS_FILE = OUTPUT_DIR / "Historical_Earnings_Calendar.xlsx"
EARNINGS_CACHE_FILE = OUTPUT_DIR / "historical_earnings_dict.pkl"

# Date range for filtering
ANALYSIS_START = datetime(2015, 1, 1)
ANALYSIS_END = datetime.now()

# Rate limiting
REQUEST_DELAY = 0.25
BATCH_SIZE = 25
BATCH_DELAY = 2.0

EARNINGS_LIMIT = 100

print(f"Output: {OUTPUT_DIR}")
print(f"Keeping data from: {ANALYSIS_START.date()} onwards")

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def determine_report_time_from_timestamp(dt):
    """Determine pre-market vs post-market from timestamp."""
    if not hasattr(dt, 'hour'):
        return 'post-market'

    hour = dt.hour
    if hour < 10:
        return 'pre-market'
    elif hour >= 16:
        return 'post-market'
    else:
        return 'pre-market'


def calculate_affected_date(report_date, report_time):
    """Calculate the trading date affected by earnings."""
    if isinstance(report_date, datetime):
        report_date = report_date.date()

    if report_time == 'post-market':
        affected = report_date + timedelta(days=1)
        while affected.weekday() >= 5:
            affected += timedelta(days=1)
    else:
        affected = report_date
        while affected.weekday() >= 5:
            affected += timedelta(days=1)

    return affected


def fetch_earnings_for_ticker(ticker):
    """Fetch historical earnings dates for a ticker."""
    earnings = []

    try:
        stock = yf.Ticker(ticker)
        ed = stock.get_earnings_dates(limit=EARNINGS_LIMIT)

        if ed is not None and len(ed) > 0:
            for idx in ed.index:
                try:
                    dt = pd.to_datetime(idx)

                    # Convert to naive datetime for comparison
                    if dt.tzinfo is not None:
                        dt_naive = dt.tz_localize(None)
                    else:
                        dt_naive = dt

                    # Filter to analysis period
                    if dt_naive < ANALYSIS_START:
                        continue
                    if dt_naive > ANALYSIS_END:
                        continue

                    # Extract report time from original timestamp (before tz removal)
                    report_time = determine_report_time_from_timestamp(dt)
                    report_date = dt_naive.date()

                    affected_date = calculate_affected_date(report_date, report_time)

                    row = ed.loc[idx]
                    eps_actual = row.get('Reported EPS', np.nan) if isinstance(row, pd.Series) else np.nan
                    eps_estimate = row.get('EPS Estimate', np.nan) if isinstance(row, pd.Series) else np.nan

                    earnings.append({
                        'ticker': ticker.upper(),
                        'reportDate': report_date,
                        'reportTime': report_time,
                        'tradingDateAffected': affected_date,
                        'eps_actual': eps_actual,
                        'eps_estimate': eps_estimate,
                        'source': 'yfinance'
                    })

                except Exception:
                    continue

        # Deduplicate
        seen = set()
        unique = []
        for e in earnings:
            if e['reportDate'] not in seen:
                seen.add(e['reportDate'])
                unique.append(e)

        return unique

    except Exception:
        return []

# =============================================================================
# MAIN EXECUTION
# =============================================================================

print("\n" + "=" * 70)
print("HISTORICAL EARNINGS FETCHER")
print("=" * 70)

# Load tickers from all ETF sheets in Universe file
all_tickers = []
etf_sheets = ['VGT', 'VHT', 'VFH', 'VIS', 'VCR']

print(f"Loading tickers from: {UNIVERSE_FILE}")
for sheet in etf_sheets:
    try:
        df = pd.read_excel(UNIVERSE_FILE, sheet_name=sheet, skiprows=5)
        tickers = df['Ticker'].dropna().unique().tolist()
        all_tickers.extend(tickers)
        print(f"   {sheet}: {len(tickers)} tickers")
    except Exception as e:
        print(f"   {sheet}: Error - {e}")

all_tickers = list(set([t.upper().strip() for t in all_tickers]))

print(f"\nLoaded {len(all_tickers)} tickers")

# Quick sanity check
print(f"\nQuick test with AAP...")
test_earnings = fetch_earnings_for_ticker('AAP')
print(f"   AAP: {len(test_earnings)} earnings dates")
if test_earnings:
    print(f"   Range: {test_earnings[-1]['reportDate']} to {test_earnings[0]['reportDate']}")
    print(f"   Fix working!")
else:
    print(f"   Still broken - check debug output")
    raise SystemExit(1)

# Storage
all_earnings = []
failed_tickers = []
tickers_with_data = 0

total = len(all_tickers)
start_time = time.time()

print(f"\nFetching all {total} tickers...")
print("-" * 70)

for batch_start in range(0, total, BATCH_SIZE):
    batch_end = min(batch_start + BATCH_SIZE, total)
    batch = all_tickers[batch_start:batch_end]

    pct = batch_end / total * 100
    elapsed = time.time() - start_time
    if batch_end > BATCH_SIZE:
        rate = batch_end / elapsed
        remaining = total - batch_end
        eta_str = f"ETA: {remaining / rate / 60:.0f}m"
    else:
        eta_str = ""

    print(f"\nBatch {batch_start//BATCH_SIZE + 1}: [{batch_start+1:4d}-{batch_end:4d}] ({pct:5.1f}%) {eta_str}")
    print("   ", end="")

    batch_success = 0
    batch_fail = 0
    batch_events = 0

    for ticker in batch:
        earnings = fetch_earnings_for_ticker(ticker)

        if earnings:
            all_earnings.extend(earnings)
            tickers_with_data += 1
            batch_success += 1
            batch_events += len(earnings)
            print(".", end="", flush=True)
        else:
            failed_tickers.append(ticker)
            batch_fail += 1
            print("x", end="", flush=True)

        time.sleep(REQUEST_DELAY)

    print(f"  ({batch_success} ok {batch_fail} fail | {batch_events} events)")

    if batch_end < total:
        time.sleep(BATCH_DELAY)

elapsed = time.time() - start_time

# =============================================================================
# SAVE RESULTS
# =============================================================================

print("\n" + "=" * 70)
print("PROCESSING RESULTS")
print("=" * 70)

if not all_earnings:
    print("\nNo earnings data!")
else:
    df = pd.DataFrame(all_earnings)
    df['reportDate'] = pd.to_datetime(df['reportDate'])
    df['tradingDateAffected'] = pd.to_datetime(df['tradingDateAffected'])
    df = df.drop_duplicates(subset=['ticker', 'reportDate'], keep='first')
    df = df.sort_values(['ticker', 'reportDate'])

    print(f"\nTime: {elapsed/60:.1f} minutes")
    print(f"Total events: {len(df):,}")
    print(f"Tickers with data: {df['ticker'].nunique()} / {total}")
    print(f"Range: {df['reportDate'].min().date()} to {df['reportDate'].max().date()}")
    print(f"Avg per ticker: {len(df) / df['ticker'].nunique():.1f}")

    # Report time breakdown
    print(f"\nReport times:")
    for rt, count in df['reportTime'].value_counts().items():
        print(f"   {rt}: {count:,}")

    # Yearly
    df['year'] = df['reportDate'].dt.year
    print(f"\nBy year:")
    for year, count in df.groupby('year').size().sort_index().items():
        bar = "#" * (count // 200)
        print(f"   {year}: {count:5,} {bar}")

    # Save Excel
    print(f"\nSaving...")
    with pd.ExcelWriter(HISTORICAL_EARNINGS_FILE, engine='openpyxl') as writer:
        save_df = df[['ticker', 'reportDate', 'reportTime', 'tradingDateAffected', 'source']].copy()
        save_df['reportDate'] = save_df['reportDate'].dt.strftime('%Y-%m-%d')
        save_df['tradingDateAffected'] = save_df['tradingDateAffected'].dt.strftime('%Y-%m-%d')
        save_df.to_excel(writer, sheet_name='Earnings Calendar', index=False)

        summary = df.groupby('ticker').agg({'reportDate': ['min', 'max', 'count']})
        summary.columns = ['Earliest', 'Latest', 'Count']
        summary = summary.reset_index()
        summary['Earliest'] = pd.to_datetime(summary['Earliest']).dt.strftime('%Y-%m-%d')
        summary['Latest'] = pd.to_datetime(summary['Latest']).dt.strftime('%Y-%m-%d')
        summary.to_excel(writer, sheet_name='Ticker Summary', index=False)

        if failed_tickers:
            pd.DataFrame({'ticker': sorted(failed_tickers)}).to_excel(
                writer, sheet_name='Failed', index=False
            )

    print(f"   Saved: {HISTORICAL_EARNINGS_FILE}")

    # Save pickle dict
    earnings_dict = {}
    for ticker in df['ticker'].unique():
        ticker_df = df[df['ticker'] == ticker]
        affected_dates = sorted(set(ticker_df['tradingDateAffected'].dt.date.tolist()))
        earnings_dict[ticker.upper()] = affected_dates

    pd.to_pickle(earnings_dict, EARNINGS_CACHE_FILE)
    print(f"   Saved: {EARNINGS_CACHE_FILE}")

print("\n" + "=" * 70)
print("COMPLETE")
print("=" * 70)
