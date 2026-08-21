"""
Unified volume metrics calculator for pair trading secondaries.

Calculates ticker-level metrics (volume ratio, relative volume, rolling
intraday volatility) and pair-level metrics (volume dominance, price
dominance, true last hour volatility) from hourly OHLCV data. Supports
incremental updates, legacy metric migration, and HDF5/pickle output
for downstream consumption.

STATUS: live
"""

import pandas as pd
import numpy as np
import os
import pickle
import warnings
from datetime import datetime, timedelta
from tqdm import tqdm
import h5py
from multiprocessing import Pool, cpu_count
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import shutil
import time
import sys

from src.shared import config
from src.shared.calculations import (
    LAST_TRADING_HOUR,
    calculate_true_last_hour_vol_from_df,
)

warnings.filterwarnings('ignore')

# =============================================================================
# CONFIG-BASED DIRECTORY SETUP
# =============================================================================

VERSION_BASE_DIR = config.get_version_dir(config.ACTIVE_VERSION)
print(f"Config VERSION: {config.ACTIVE_VERSION}")
print(f"Working directory: {VERSION_BASE_DIR}")

# =============================================================================
# SHARED SECONDARIES CACHE DIRECTORY (VERSION-INDEPENDENT)
# =============================================================================

SECONDARIES_CACHE_DIR = config.get("paths.secondaries_cache_dir", "")
print(f"Secondaries cache: {SECONDARIES_CACHE_DIR}")

print(f"Imported last hour calculation from calculations (hour={LAST_TRADING_HOUR})")


class UnifiedVolumeMetricsCalculator:
    def __init__(self, calc_config=None):
        """Initialize the unified volume metrics calculator"""
        self.config = {
            'base_dir': VERSION_BASE_DIR,
            'indexes': ['VFH', 'VGT', 'VIS', 'VHT', 'VCR'],
            'volume_ratio_short_days': 3,
            'volume_ratio_long_days': 30,
            'relative_volume_lookback_days': 3,
            'dominance_lookback_days': 10,
            'rolling_volatility_lookback_days': 10,
            'last_hour_spread_days': 10,
            'last_hour_volatility_approach': 'average_individual',
            'use_hdf5': True,
            'cache_dir': SECONDARIES_CACHE_DIR,
            'force_recalculate': False,
            'ensure_downstream_compatibility': True,
            'standardize_column_names': True,
            'validate_outputs': True,
            'use_parallel': True,
            'n_jobs': cpu_count() - 1,
        }
        if calc_config:
            self.config.update(calc_config)

    def analyze_existing_metrics(self, index_ticker):
        """Analyze existing metrics and determine what needs to be updated"""
        analysis = {
            'volume_ratio': {'status': 'unknown', 'count': 0, 'tickers': []},
            'relative_volume': {'status': 'unknown', 'count': 0, 'tickers': []},
            'rolling_intraday_volatility': {'status': 'unknown', 'count': 0, 'tickers': []},
            'volume_dominance': {'status': 'unknown', 'count': 0, 'pairs': []},
            'price_dominance': {'status': 'unknown', 'count': 0, 'pairs': []},
            'true_last_hour_volatility': {'status': 'unknown', 'count': 0, 'pairs': []},
            'hourly_volatility': {'status': 'unknown', 'count': 0, 'tickers': []},
            'dominance': {'status': 'unknown', 'count': 0, 'pairs': []},
            'needs_update': False,
            'needs_migration': False,
            'has_no_metrics': False
        }

        index_dir = os.path.join(os.path.expanduser(self.config['base_dir']), index_ticker)
        cache_dir = self.config['cache_dir']  # Shared secondaries cache
        existing_metrics = self.load_existing_caches(index_ticker, cache_dir)

        total_ticker_metrics = len(existing_metrics.get('ticker_metrics', {}))
        total_pair_metrics = len(existing_metrics.get('pair_metrics', {}))

        if total_ticker_metrics == 0 and total_pair_metrics == 0:
            analysis['has_no_metrics'] = True
            analysis['needs_update'] = True

        for ticker, ticker_data in existing_metrics.get('ticker_metrics', {}).items():
            for metric_name in ['volume_ratio', 'relative_volume', 'rolling_intraday_volatility']:
                metric_data = ticker_data.get(metric_name, {})
                if metric_data and len(metric_data) > 100:
                    analysis[metric_name]['status'] = 'good'
                    analysis[metric_name]['count'] += len(metric_data)
                    analysis[metric_name]['tickers'].append(ticker)
                else:
                    analysis['needs_update'] = True

            if 'hourly_volatility' in ticker_data and 'rolling_intraday_volatility' not in ticker_data:
                analysis['needs_migration'] = True
                analysis['hourly_volatility']['status'] = 'legacy'

        for pair_name, pair_data in existing_metrics.get('pair_metrics', {}).items():
            for metric_name in ['volume_dominance', 'price_dominance', 'true_last_hour_volatility']:
                metric_data = pair_data.get(metric_name, {})
                if metric_data and len(metric_data) > 10:
                    analysis[metric_name]['status'] = 'good'
                    analysis[metric_name]['count'] += len(metric_data)
                    analysis[metric_name]['pairs'].append(pair_name)
                else:
                    analysis['needs_update'] = True

            if 'dominance' in pair_data and 'volume_dominance' not in pair_data:
                analysis['needs_migration'] = True
                analysis['dominance']['status'] = 'legacy'

        return analysis, existing_metrics

    def unified_enhance_index(self, index_ticker):
        """Unified enhancement with guaranteed downstream compatibility"""
        print(f"\nUNIFIED METRICS UPDATE for {index_ticker}")
        print("="*60)

        index_dir = os.path.join(os.path.expanduser(self.config['base_dir']), index_ticker)
        cache_dir = self.config['cache_dir']  # Now uses shared SECONDARIES_CACHE_DIR
        os.makedirs(cache_dir, exist_ok=True)

        print(f"  Analyzing existing metrics...")
        analysis, existing_metrics = self.analyze_existing_metrics(index_ticker)

        print(f"  Current status:")
        for metric_type, info in analysis.items():
            if metric_type not in ['needs_update', 'needs_migration', 'has_no_metrics']:
                print(f"    {metric_type}: {info['status']} ({info['count']} values)")

        if analysis['needs_migration']:
            print(f"  Legacy metrics detected - will migrate")
        if analysis['has_no_metrics']:
            print(f"  No existing metrics - will calculate from scratch")

        if (not analysis['needs_update'] and not analysis['needs_migration'] and
            not analysis['has_no_metrics'] and not self.config['force_recalculate']):
            print(f"  All unified metrics are ready for {index_ticker}")
            return

        hourly_file = os.path.join(SECONDARIES_CACHE_DIR, f"{index_ticker}_Hourly_Data.csv")
        if not os.path.exists(hourly_file):
            print(f"  Hourly data not found for {index_ticker} at {hourly_file}")
            return

        print(f"  Loading hourly data...")
        hourly_df = pd.read_csv(hourly_file)

        try:
            hourly_df['timestamp'] = pd.to_datetime(hourly_df['timestamp'], dayfirst=True)
        except:
            try:
                hourly_df['timestamp'] = pd.to_datetime(hourly_df['timestamp'], dayfirst=False)
            except:
                hourly_df['timestamp'] = pd.to_datetime(hourly_df['timestamp'], format='mixed')

        hourly_df = hourly_df.set_index('timestamp')

        updated_ticker_metrics = existing_metrics.get('ticker_metrics', {}).copy()
        updated_pair_metrics = existing_metrics.get('pair_metrics', {}).copy()

        if analysis['needs_migration']:
            print(f"  Migrating legacy metrics...")
            self.migrate_legacy_metrics(updated_ticker_metrics, updated_pair_metrics)

        print(f"  Processing ticker metrics...")
        updated_ticker_metrics = self.calculate_ticker_metrics(
            index_ticker, hourly_df, updated_ticker_metrics, analysis)

        print(f"  Processing pair metrics...")
        updated_pair_metrics = self.calculate_pair_metrics(
            index_ticker, hourly_df, updated_pair_metrics, analysis)

        if self.config['validate_outputs']:
            print(f"  Validating downstream compatibility...")
            self.validate_outputs(updated_ticker_metrics, updated_pair_metrics)

        cache_file = os.path.join(cache_dir, f'{index_ticker}_volume_metrics.h5')
        self.save_unified_metrics_hdf5(cache_file, updated_ticker_metrics, updated_pair_metrics)

        ticker_pkl = os.path.join(cache_dir, f'{index_ticker}_volume_metrics_ticker_metrics.pkl')
        pair_pkl = os.path.join(cache_dir, f'{index_ticker}_volume_metrics_pair_metrics.pkl')

        with open(ticker_pkl, 'wb') as f:
            pickle.dump(updated_ticker_metrics, f)
        with open(pair_pkl, 'wb') as f:
            pickle.dump(updated_pair_metrics, f)

        print(f"  Unified metrics saved for {index_ticker}")
        self.report_final_unified_metrics(updated_ticker_metrics, updated_pair_metrics)

    def load_existing_caches(self, index_ticker, cache_dir):
        """Load existing metrics from cache sources"""
        existing_metrics = {'ticker_metrics': {}, 'pair_metrics': {}, 'sources': []}

        h5_file = os.path.join(cache_dir, f'{index_ticker}_volume_metrics.h5')
        if os.path.exists(h5_file):
            existing_metrics['sources'].append('hdf5')
            self._load_from_hdf5(h5_file, existing_metrics)

        for pkl_file in [f'{index_ticker}_volume_metrics_ticker_metrics.pkl',
                         f'{index_ticker}_ticker_metrics.pkl']:
            full_path = os.path.join(cache_dir, pkl_file)
            if os.path.exists(full_path):
                with open(full_path, 'rb') as f:
                    existing_metrics['ticker_metrics'].update(pickle.load(f))
                break

        for pkl_file in [f'{index_ticker}_volume_metrics_pair_metrics.pkl',
                         f'{index_ticker}_pair_metrics.pkl']:
            full_path = os.path.join(cache_dir, pkl_file)
            if os.path.exists(full_path):
                with open(full_path, 'rb') as f:
                    existing_metrics['pair_metrics'].update(pickle.load(f))
                break

        return existing_metrics

    def _load_from_hdf5(self, h5_file, existing_metrics):
        """Load metrics from HDF5 file"""
        try:
            with pd.HDFStore(h5_file, 'r') as store:
                for key in store.keys():
                    if key.startswith('/ticker/'):
                        parts = key.split('/')
                        ticker, metric_type = parts[2], parts[3]
                        if ticker not in existing_metrics['ticker_metrics']:
                            existing_metrics['ticker_metrics'][ticker] = {}
                        df = store[key]
                        col_name = 'ratio' if 'ratio' in df.columns else df.columns[0]
                        existing_metrics['ticker_metrics'][ticker][metric_type] = df.to_dict()[col_name]
                    elif key.startswith('/pair/'):
                        parts = key.split('/')
                        pair, metric_type = parts[2], parts[3]
                        if pair not in existing_metrics['pair_metrics']:
                            existing_metrics['pair_metrics'][pair] = {}
                        df = store[key]
                        existing_metrics['pair_metrics'][pair][metric_type] = df.to_dict()[df.columns[0]]
        except Exception as e:
            print(f"    Warning: Error loading HDF5: {e}")

    def migrate_legacy_metrics(self, ticker_metrics, pair_metrics):
        """Migrate legacy metric names to standard names"""
        for ticker, ticker_data in ticker_metrics.items():
            if 'hourly_volatility' in ticker_data and 'rolling_intraday_volatility' not in ticker_data:
                ticker_data['rolling_intraday_volatility'] = ticker_data['hourly_volatility']

        for pair_name, pair_data in pair_metrics.items():
            if 'dominance' in pair_data and 'volume_dominance' not in pair_data:
                pair_data['volume_dominance'] = pair_data['dominance']

    def calculate_ticker_metrics(self, index_ticker, hourly_df, existing_ticker_metrics, analysis):
        """Calculate all ticker-level metrics"""
        volume_cols = [col for col in hourly_df.columns if col.endswith('_volume')]
        tickers = [col.replace('_volume', '') for col in volume_cols
                   if col.replace('_volume', '') + '_close' in hourly_df.columns]

        print(f"    Processing {len(tickers)} tickers...")
        updated_metrics = existing_ticker_metrics.copy()

        for ticker in tqdm(tickers, desc="    Processing tickers"):
            if ticker not in updated_metrics:
                updated_metrics[ticker] = {}

            if ('volume_ratio' not in updated_metrics[ticker] or
                not updated_metrics[ticker].get('volume_ratio') or self.config['force_recalculate']):
                result = self.calculate_volume_ratio(hourly_df, ticker)
                if result:
                    updated_metrics[ticker]['volume_ratio'] = result

            if ('relative_volume' not in updated_metrics[ticker] or
                not updated_metrics[ticker].get('relative_volume') or self.config['force_recalculate']):
                result = self.calculate_relative_volume(hourly_df, ticker)
                if result:
                    updated_metrics[ticker]['relative_volume'] = result

            if ('rolling_intraday_volatility' not in updated_metrics[ticker] or
                not updated_metrics[ticker].get('rolling_intraday_volatility') or self.config['force_recalculate']):
                result = self.calculate_rolling_intraday_volatility(hourly_df, ticker)
                if result:
                    updated_metrics[ticker]['rolling_intraday_volatility'] = result

        return updated_metrics

    def calculate_pair_metrics(self, index_ticker, hourly_df, existing_pair_metrics, analysis):
        """Calculate all pair-level metrics"""
        required_pairs = self.get_required_pairs(index_ticker)
        print(f"    Processing {len(required_pairs)} pairs...")

        updated_metrics = existing_pair_metrics.copy()
        close_cols = [col for col in hourly_df.columns if col.endswith('_close')]
        available_tickers = set(col.replace('_close', '') for col in close_cols)

        for stock1, stock2 in tqdm(required_pairs, desc="    Processing pairs"):
            pair_key = f"{stock1}_{stock2}"
            if pair_key not in updated_metrics:
                updated_metrics[pair_key] = {}

            if stock1 not in available_tickers or stock2 not in available_tickers:
                continue

            if ('volume_dominance' not in updated_metrics[pair_key] or
                not updated_metrics[pair_key].get('volume_dominance') or self.config['force_recalculate']):
                result = self.calculate_volume_dominance(hourly_df, stock1, stock2)
                if result:
                    updated_metrics[pair_key]['volume_dominance'] = result

            if ('price_dominance' not in updated_metrics[pair_key] or
                not updated_metrics[pair_key].get('price_dominance') or self.config['force_recalculate']):
                result = self.calculate_price_dominance(hourly_df, stock1, stock2)
                if result:
                    updated_metrics[pair_key]['price_dominance'] = result

            if ('true_last_hour_volatility' not in updated_metrics[pair_key] or
                not updated_metrics[pair_key].get('true_last_hour_volatility') or self.config['force_recalculate']):
                result = self.calculate_true_last_hour_volatility(hourly_df, stock1, stock2)
                if result:
                    updated_metrics[pair_key]['true_last_hour_volatility'] = result

        return updated_metrics

    def calculate_volume_ratio(self, hourly_df, ticker):
        """Calculate volume ratio (short vs long term average)"""
        volume_col = f'{ticker}_volume'
        if volume_col not in hourly_df.columns:
            return {}

        daily_volume = hourly_df[volume_col].resample('D').sum()
        short_avg = daily_volume.rolling(window=self.config['volume_ratio_short_days']).mean()
        long_avg = daily_volume.rolling(window=self.config['volume_ratio_long_days']).mean()

        volume_ratio = {}
        for date in short_avg.index[self.config['volume_ratio_long_days']:]:
            if long_avg[date] > 0 and not pd.isna(short_avg[date]) and not pd.isna(long_avg[date]):
                volume_ratio[date] = short_avg[date] / long_avg[date]
        return volume_ratio

    def calculate_relative_volume(self, hourly_df, ticker):
        """Calculate relative volume"""
        volume_col = f'{ticker}_volume'
        if volume_col not in hourly_df.columns:
            return {}

        daily_volume = hourly_df[volume_col].resample('D').sum()
        lookback = self.config['relative_volume_lookback_days']

        relative_volume = {}
        for i in range(lookback, len(daily_volume)):
            current_date = daily_volume.index[i]
            current_volume = daily_volume.iloc[i]
            historical_avg = daily_volume.iloc[i-lookback:i].mean()
            if historical_avg > 0 and not pd.isna(current_volume):
                relative_volume[current_date] = current_volume / historical_avg
        return relative_volume

    def calculate_rolling_intraday_volatility(self, hourly_df, ticker):
        """Calculate rolling intraday volatility"""
        close_col = f'{ticker}_close'
        if close_col not in hourly_df.columns:
            return {}

        hourly_returns = hourly_df[close_col].pct_change().fillna(0)
        daily_groups = hourly_returns.groupby(hourly_returns.index.date)

        daily_volatilities, dates = [], []
        for date, day_returns in daily_groups:
            if len(day_returns) > 1:
                daily_vol = day_returns.std()
                if not pd.isna(daily_vol):
                    daily_volatilities.append(daily_vol)
                    dates.append(pd.Timestamp(date))

        if len(daily_volatilities) < self.config['rolling_volatility_lookback_days']:
            return {}

        vol_series = pd.Series(daily_volatilities, index=dates)
        rolling_vol = {}
        lookback = self.config['rolling_volatility_lookback_days']
        for i in range(lookback, len(vol_series)):
            date = vol_series.index[i]
            window_vol = vol_series.iloc[i-lookback:i].mean()
            if not pd.isna(window_vol):
                rolling_vol[date] = window_vol
        return rolling_vol

    def calculate_volume_dominance(self, hourly_df, stock1, stock2):
        """Calculate volume dominance"""
        vol1_col, vol2_col = f'{stock1}_volume', f'{stock2}_volume'
        if vol1_col not in hourly_df.columns or vol2_col not in hourly_df.columns:
            return {}

        daily_vol1 = hourly_df[vol1_col].resample('D').sum()
        daily_vol2 = hourly_df[vol2_col].resample('D').sum()
        lookback = self.config['dominance_lookback_days']

        volume_dominance = {}
        for date in daily_vol1.index[lookback:]:
            window_start = date - timedelta(days=lookback)
            window_vol1 = daily_vol1[window_start:date].sum()
            window_vol2 = daily_vol2[window_start:date].sum()
            total = window_vol1 + window_vol2
            if total > 0:
                volume_dominance[date] = window_vol1 / total
        return volume_dominance

    def calculate_price_dominance(self, hourly_df, stock1, stock2):
        """Calculate price dominance"""
        close1_col, close2_col = f'{stock1}_close', f'{stock2}_close'
        if close1_col not in hourly_df.columns or close2_col not in hourly_df.columns:
            return {}

        returns1 = hourly_df[close1_col].pct_change().fillna(0)
        returns2 = hourly_df[close2_col].pct_change().fillna(0)
        daily_abs1 = returns1.abs().resample('D').sum()
        daily_abs2 = returns2.abs().resample('D').sum()
        lookback = self.config['dominance_lookback_days']

        price_dominance = {}
        for date in daily_abs1.index[lookback:]:
            window_start = date - timedelta(days=lookback)
            window_abs1 = daily_abs1[window_start:date].sum()
            window_abs2 = daily_abs2[window_start:date].sum()
            total = window_abs1 + window_abs2
            if total > 0:
                price_dominance[date] = window_abs1 / total
        return price_dominance

    def calculate_true_last_hour_volatility(self, hourly_df, stock1, stock2):
        """
        Calculate true last hour volatility - DELEGATES TO shared calculations.

        No local implementation - shared calculations module is the single source of truth.
        Uses hour==15 filter (3-4pm ET) matching live implementation.
        """
        lookback_days = self.config.get('last_hour_spread_days', 10)
        return calculate_true_last_hour_vol_from_df(
            hourly_df, stock1, stock2, lookback_days=lookback_days
        )

    def get_required_pairs(self, index_ticker):
        """Get required pairs from selected pairs file"""
        required_pairs = set()
        index_dir = os.path.join(os.path.expanduser(self.config['base_dir']), index_ticker)
        pair_results_file = os.path.join(index_dir, f"{index_ticker}_Pair_Trading_Results.xlsx")

        if os.path.exists(pair_results_file):
            try:
                pairs_df = pd.read_excel(pair_results_file, sheet_name='Selected Pairs')
                stock1_col = stock2_col = None
                for col in ['Stock1', 'stock1', 'Ticker1', 'Co1']:
                    if col in pairs_df.columns:
                        stock1_col = col
                        break
                for col in ['Stock2', 'stock2', 'Ticker2', 'Co2']:
                    if col in pairs_df.columns:
                        stock2_col = col
                        break
                if stock1_col and stock2_col:
                    for _, row in pairs_df.iterrows():
                        s1, s2 = row[stock1_col], row[stock2_col]
                        if pd.notna(s1) and pd.notna(s2):
                            required_pairs.add((s1, s2))
            except Exception as e:
                print(f"    Warning: Error reading pairs: {e}")
        return required_pairs

    def validate_outputs(self, ticker_metrics, pair_metrics):
        """Validate outputs for downstream compatibility"""
        for ticker, data in ticker_metrics.items():
            for metric in ['volume_ratio', 'relative_volume', 'rolling_intraday_volatility']:
                if metric in data and not isinstance(data[metric], dict):
                    print(f"      Warning: {ticker}.{metric} is not a dict")
        for pair, data in pair_metrics.items():
            for metric in ['volume_dominance', 'price_dominance', 'true_last_hour_volatility']:
                if metric in data and not isinstance(data[metric], dict):
                    print(f"      Warning: {pair}.{metric} is not a dict")
        print(f"    Output validation completed")

    def save_unified_metrics_hdf5(self, cache_file, ticker_metrics, pair_metrics):
        """Save unified metrics to HDF5 file"""
        if os.path.exists(cache_file):
            shutil.copy2(cache_file, cache_file.replace('.h5', '_backup.h5'))

        with pd.HDFStore(cache_file, 'w') as store:
            for ticker, metrics in ticker_metrics.items():
                for metric_name, metric_col in [('volume_ratio', 'ratio'),
                                                 ('relative_volume', 'ratio'),
                                                 ('rolling_intraday_volatility', 'volatility')]:
                    if metrics.get(metric_name):
                        df = pd.DataFrame.from_dict(metrics[metric_name], orient='index', columns=[metric_col])
                        store[f'ticker/{ticker}/{metric_name}'] = df

            for pair, metrics in pair_metrics.items():
                for metric_name, metric_col in [('volume_dominance', 'dominance'),
                                                 ('price_dominance', 'dominance'),
                                                 ('true_last_hour_volatility', 'volatility')]:
                    if metrics.get(metric_name):
                        df = pd.DataFrame.from_dict(metrics[metric_name], orient='index', columns=[metric_col])
                        store[f'pair/{pair}/{metric_name}'] = df

    def report_final_unified_metrics(self, ticker_metrics, pair_metrics):
        """Report final metrics summary"""
        print(f"\n  Final unified metrics summary:")

        ticker_counts = {}
        for ticker_data in ticker_metrics.values():
            for metric in ['volume_ratio', 'relative_volume', 'rolling_intraday_volatility']:
                if ticker_data.get(metric):
                    ticker_counts[metric] = ticker_counts.get(metric, 0) + len(ticker_data[metric])

        pair_counts = {}
        for pair_data in pair_metrics.values():
            for metric in ['volume_dominance', 'price_dominance', 'true_last_hour_volatility']:
                if pair_data.get(metric):
                    pair_counts[metric] = pair_counts.get(metric, 0) + len(pair_data[metric])

        print(f"    Ticker metrics:")
        for metric, count in ticker_counts.items():
            print(f"      {metric}: {count:,} values")
        print(f"    Pair metrics:")
        for metric, count in pair_counts.items():
            print(f"      {metric}: {count:,} values")


# ==============================================================================
# MAIN FUNCTIONS
# ==============================================================================

def unify_all_volume_metrics(indexes=None, force_recalculate=False):
    """Unify all volume metrics with guaranteed downstream compatibility."""
    calc_config = {
        'base_dir': VERSION_BASE_DIR,
        'indexes': indexes or ['VFH', 'VGT', 'VIS', 'VHT', 'VCR'],
        'force_recalculate': force_recalculate,
    }

    print("UNIFIED VOLUME METRICS CALCULATOR")
    print("="*70)
    print(f"Working directory: {VERSION_BASE_DIR}")
    print(f"Last hour calc: calculations.calculate_true_last_hour_vol_from_df (hour={LAST_TRADING_HOUR})")
    print("="*70)

    calculator = UnifiedVolumeMetricsCalculator(calc_config)
    for index_ticker in calc_config['indexes']:
        calculator.unified_enhance_index(index_ticker)

    print("\nUNIFIED METRICS CALCULATION COMPLETED!")


def unify_single_index(index_ticker, force_recalculate=False):
    """Unify metrics for a single index"""
    calculator = UnifiedVolumeMetricsCalculator({
        'base_dir': VERSION_BASE_DIR, 'force_recalculate': force_recalculate})
    calculator.unified_enhance_index(index_ticker)


def check_unified_metrics_status(index_ticker=None):
    """Check the current status of unified metrics"""
    indexes = [index_ticker] if index_ticker else ['VFH', 'VGT', 'VIS', 'VHT', 'VCR']

    print(f"UNIFIED METRICS STATUS CHECK")
    print(f"Working directory: {VERSION_BASE_DIR}")
    print("="*50)

    for index in indexes:
        calculator = UnifiedVolumeMetricsCalculator({'base_dir': VERSION_BASE_DIR})
        analysis, _ = calculator.analyze_existing_metrics(index)

        print(f"\n{index}:")
        for metric in ['volume_ratio', 'relative_volume', 'rolling_intraday_volatility',
                       'volume_dominance', 'price_dominance', 'true_last_hour_volatility']:
            info = analysis[metric]
            print(f"    {metric}: {info['status']} ({info['count']} values)")

        status = "All unified"
        if analysis['needs_migration']:
            status = "NEEDS MIGRATION"
        elif analysis['needs_update']:
            status = "NEEDS UPDATE"
        print(f"  Status: {status}")


if __name__ == "__main__":
    print("UNIFIED VOLUME METRICS CALCULATOR")
    print("="*60)
    print(f"Working directory: {VERSION_BASE_DIR}")
    print(f"Last hour calculation: calculations (hour={LAST_TRADING_HOUR})")
    print("="*60)

    unify_all_volume_metrics()
