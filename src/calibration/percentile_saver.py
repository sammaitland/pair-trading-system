"""
Historical percentile distribution generator for secondary signals.

Calculates and saves percentile distributions for all secondary metrics
(volume ratio, rolling intraday volatility, volume dominance, last hour
volatility, IV percentile). Used by the live allocation module to convert
real-time metric values to percentile ranks.

STATUS: live
"""

import pandas as pd
import numpy as np
import pickle
import os
from pathlib import Path

from src.shared import config

# =============================================================================
# VERSION-AWARE DIRECTORY CONFIGURATION
# =============================================================================

def get_version_aware_paths():
    """
    Determine correct directory paths based on config.active_version()
    Returns tuple: (version, v9_root, working_dir, secondaries_cache_dir, combined_portfolio_dir)
    """
    version = config.active_version()

    # Ensure it starts with 'V'
    if not version.startswith('V'):
        version = f'V{version}'
    print(f"Config VERSION: {version}")

    v9_root = Path(config.get("paths.v9_root", ""))
    working_dir = Path(config.get_version_dir(config.active_version()))
    print(f"Using {version} directory structure")

    # Secondaries cache is SHARED (version-independent)
    secondaries_cache_dir = Path(config.get("paths.secondaries_cache_dir", ""))

    # Combined_Portfolio is SHARED at root level
    combined_portfolio_dir = Path(config.get("paths.combined_portfolio_dir", ""))

    return version, v9_root, working_dir, secondaries_cache_dir, combined_portfolio_dir


# Get version-aware paths
VERSION, V9_ROOT, WORKING_DIR, SECONDARIES_CACHE_DIR, COMBINED_PORTFOLIO_DIR = get_version_aware_paths()

# Indexes to process
INDEXES = ['VGT', 'VIS', 'VHT', 'VCR', 'VFH']

# Ensure secondaries cache directory exists
SECONDARIES_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def load_all_volume_metrics():
    """Load all volume metrics from shared cache files across all indexes"""
    print("Loading volume metrics from shared secondaries cache...")
    print(f"  Secondaries cache: {SECONDARIES_CACHE_DIR}")

    all_metrics = {
        'ticker_metrics': {},
        'pair_metrics': {}
    }

    if not SECONDARIES_CACHE_DIR.exists():
        print(f"  ERROR: Secondaries cache directory not found at {SECONDARIES_CACHE_DIR}")
        return all_metrics

    for index_ticker in INDEXES:
        # Use shared secondaries cache directory
        cache_dir = SECONDARIES_CACHE_DIR

        # Try loading from pickle files first (faster)
        ticker_pkl = cache_dir / f'{index_ticker}_volume_metrics_ticker_metrics.pkl'
        pair_pkl = cache_dir / f'{index_ticker}_volume_metrics_pair_metrics.pkl'

        if ticker_pkl.exists():
            with open(ticker_pkl, 'rb') as f:
                ticker_data = pickle.load(f)
                all_metrics['ticker_metrics'].update(ticker_data)
            print(f"  Loaded ticker metrics for {index_ticker}: {len(ticker_data)} tickers")

        if pair_pkl.exists():
            with open(pair_pkl, 'rb') as f:
                pair_data = pickle.load(f)
                all_metrics['pair_metrics'].update(pair_data)
            print(f"  Loaded pair metrics for {index_ticker}: {len(pair_data)} pairs")

        # Fallback to HDF5 if pickle files don't exist
        if not ticker_pkl.exists() or not pair_pkl.exists():
            h5_file = cache_dir / f'{index_ticker}_volume_metrics.h5'
            if h5_file.exists():
                print(f"  Loading from HDF5 for {index_ticker}...")
                _load_from_hdf5(h5_file, all_metrics)

    print(f"\nTotal loaded:")
    print(f"  Ticker metrics: {len(all_metrics['ticker_metrics'])} tickers")
    print(f"  Pair metrics: {len(all_metrics['pair_metrics'])} pairs")

    return all_metrics


def _load_from_hdf5(h5_file, all_metrics):
    """Load metrics from HDF5 file"""
    try:
        with pd.HDFStore(h5_file, 'r') as store:
            for key in store.keys():
                if key.startswith('/ticker/'):
                    parts = key.split('/')
                    ticker = parts[2]
                    metric_type = parts[3]

                    if ticker not in all_metrics['ticker_metrics']:
                        all_metrics['ticker_metrics'][ticker] = {}

                    df = store[key]
                    col_name = df.columns[0]
                    all_metrics['ticker_metrics'][ticker][metric_type] = df.to_dict()[col_name]

                elif key.startswith('/pair/'):
                    parts = key.split('/')
                    pair = parts[2]
                    metric_type = parts[3]

                    if pair not in all_metrics['pair_metrics']:
                        all_metrics['pair_metrics'][pair] = {}

                    df = store[key]
                    all_metrics['pair_metrics'][pair][metric_type] = df.to_dict()[df.columns[0]]
    except Exception as e:
        print(f"    Error loading HDF5: {e}")


def extract_metric_values(all_metrics, metric_name, metric_level='ticker'):
    """Extract all values for a specific metric across all tickers/pairs"""
    values = []

    if metric_level == 'ticker':
        source = all_metrics['ticker_metrics']
    else:  # pair
        source = all_metrics['pair_metrics']

    for entity, metrics in source.items():
        if metric_name in metrics:
            metric_dict = metrics[metric_name]
            if isinstance(metric_dict, dict):
                values.extend([v for v in metric_dict.values() if pd.notna(v)])

    return np.array(values)


def calculate_percentile_distribution(values, num_points=10000):
    """
    Calculate percentile distribution from raw values
    Creates a lookup table mapping percentile ranks to actual values
    """
    if len(values) == 0:
        print("    Warning: No values found for metric")
        return None

    # Remove NaN and infinite values
    clean_values = values[np.isfinite(values)]

    if len(clean_values) == 0:
        print("    Warning: No finite values found for metric")
        return None

    # Create percentile points from 0 to 100
    percentile_points = np.linspace(0, 100, num_points)

    # Calculate the value at each percentile
    percentile_values = np.percentile(clean_values, percentile_points)

    # Create lookup dictionary
    distribution = dict(zip(percentile_points, percentile_values))

    return {
        'distribution': distribution,
        'min': float(clean_values.min()),
        'max': float(clean_values.max()),
        'mean': float(clean_values.mean()),
        'std': float(clean_values.std()),
        'count': len(clean_values)
    }


def calculate_all_percentile_distributions():
    """Calculate percentile distributions for all secondary signals"""
    print("\nCalculating percentile distributions...")
    print("=" * 60)

    # Load all metrics
    all_metrics = load_all_volume_metrics()

    # Define metrics to process
    ticker_metrics = {
        'volume_ratio': 'volume_ratio',
        'rolling_intraday_vol': 'rolling_intraday_volatility',
        'iv_percentile': None  # Handled separately
    }

    pair_metrics = {
        'volume_dominance': 'volume_dominance',
        'true_last_hour_volatility': 'true_last_hour_volatility'
    }

    percentile_distributions = {}

    # Process ticker-level metrics
    print("\nProcessing ticker-level metrics...")
    for display_name, storage_name in ticker_metrics.items():
        if storage_name is None:  # Skip iv_percentile for now
            continue

        print(f"\n  {display_name} ({storage_name}):")
        values = extract_metric_values(all_metrics, storage_name, 'ticker')
        print(f"    Extracted {len(values):,} values")

        distribution = calculate_percentile_distribution(values)
        if distribution:
            percentile_distributions[display_name] = distribution
            print(f"    Range: {distribution['min']:.6f} to {distribution['max']:.6f}")
            print(f"    Mean: {distribution['mean']:.6f}, Std: {distribution['std']:.6f}")

    # Process pair-level metrics
    print("\nProcessing pair-level metrics...")
    for display_name, storage_name in pair_metrics.items():
        print(f"\n  {display_name} ({storage_name}):")
        values = extract_metric_values(all_metrics, storage_name, 'pair')
        print(f"    Extracted {len(values):,} values")

        distribution = calculate_percentile_distribution(values)
        if distribution:
            percentile_distributions[display_name] = distribution
            print(f"    Range: {distribution['min']:.6f} to {distribution['max']:.6f}")
            print(f"    Mean: {distribution['mean']:.6f}, Std: {distribution['std']:.6f}")

    # Handle IV percentile specially (load from options data)
    print("\n  iv_percentile (from options data):")
    iv_values = load_iv_percentile_values()
    if len(iv_values) > 0:
        print(f"    Extracted {len(iv_values):,} values")
        distribution = calculate_percentile_distribution(iv_values)
        if distribution:
            percentile_distributions['iv_percentile'] = distribution
            print(f"    Range: {distribution['min']:.6f} to {distribution['max']:.6f}")
            print(f"    Mean: {distribution['mean']:.6f}, Std: {distribution['std']:.6f}")
    else:
        print("    Warning: No IV data found")

    return percentile_distributions


def load_iv_percentile_values():
    """Load all IV values from monthly options data (from shared Combined_Portfolio)"""
    iv_values = []

    # Options cache is at root level (shared)
    options_cache_dir = COMBINED_PORTFOLIO_DIR / 'options_cache'

    if not options_cache_dir.exists():
        print(f"    Warning: Options cache directory not found at {options_cache_dir}")
        return np.array(iv_values)

    for index_ticker in INDEXES:
        # Try parquet first (new format), fall back to xlsx (legacy)
        iv_file_parquet = options_cache_dir / f'{index_ticker}_Monthly_Options_IV.parquet'
        iv_file_xlsx = options_cache_dir / f'{index_ticker}_Monthly_Options_IV.xlsx'

        iv_file = None
        file_format = None

        if iv_file_parquet.exists():
            iv_file = iv_file_parquet
            file_format = 'parquet'
        elif iv_file_xlsx.exists():
            iv_file = iv_file_xlsx
            file_format = 'xlsx'

        if iv_file:
            try:
                if file_format == 'parquet':
                    df = pd.read_parquet(iv_file)
                else:
                    df = pd.read_excel(iv_file)

                if 'implied_volatility' in df.columns:
                    values = df['implied_volatility'].dropna()
                    iv_values.extend(values.tolist())
                    print(f"      Loaded {len(values):,} IV values from {index_ticker} ({file_format})")
            except Exception as e:
                print(f"      Error loading {index_ticker} IV data: {e}")

    return np.array(iv_values)


def value_to_percentile(value, distribution):
    """
    Convert a raw metric value to its percentile rank using the distribution
    Returns percentile rank from 0-100
    """
    if value < distribution['min']:
        return 0.0
    if value > distribution['max']:
        return 100.0

    # Find the percentile by interpolating in the distribution
    dist_dict = distribution['distribution']
    percentiles = sorted(dist_dict.keys())
    values = [dist_dict[p] for p in percentiles]

    # Use numpy's interp for efficient lookup
    percentile_rank = np.interp(value, values, percentiles)

    return float(percentile_rank)


def save_percentile_distributions(distributions):
    """Save percentile distributions to shared secondaries cache"""
    output_file = SECONDARIES_CACHE_DIR / 'historical_percentile_distributions.pkl'

    print(f"\nSaving distributions to: {output_file}")

    # Add version metadata
    distributions['_metadata'] = {
        'version': VERSION,
        'working_dir': str(WORKING_DIR),
        'generated_at': pd.Timestamp.now().isoformat()
    }

    with open(output_file, 'wb') as f:
        pickle.dump(distributions, f)

    print(f"Saved successfully!")

    # Also save a human-readable summary
    summary_file = SECONDARIES_CACHE_DIR / 'percentile_distributions_summary.txt'
    with open(summary_file, 'w') as f:
        f.write("HISTORICAL PERCENTILE DISTRIBUTIONS SUMMARY\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"VERSION: {VERSION}\n")
        f.write(f"Working Directory: {WORKING_DIR}\n")
        f.write(f"Secondaries Cache: {SECONDARIES_CACHE_DIR}\n")
        f.write(f"Generated: {pd.Timestamp.now()}\n\n")

        for metric_name, dist_data in distributions.items():
            if metric_name == '_metadata':
                continue
            f.write(f"{metric_name}:\n")
            f.write(f"  Count: {dist_data['count']:,} values\n")
            f.write(f"  Range: {dist_data['min']:.6f} to {dist_data['max']:.6f}\n")
            f.write(f"  Mean: {dist_data['mean']:.6f}\n")
            f.write(f"  Std Dev: {dist_data['std']:.6f}\n")
            f.write(f"  Key percentiles:\n")
            dist = dist_data['distribution']
            for p in [0, 25, 50, 75, 100]:
                closest_key = min(dist.keys(), key=lambda x: abs(x - p))
                f.write(f"    {p:3d}th: {dist[closest_key]:.6f}\n")
            f.write("\n")

    print(f"Summary saved to: {summary_file}")


# Main execution
if __name__ == "__main__":
    print("HISTORICAL PERCENTILE DISTRIBUTION GENERATOR")
    print("=" * 60)
    print(f"Config VERSION: {VERSION}")
    print(f"V9 Root: {V9_ROOT}")
    print(f"Working directory: {WORKING_DIR}")
    print(f"Secondaries cache: {SECONDARIES_CACHE_DIR}")
    print(f"Combined Portfolio: {COMBINED_PORTFOLIO_DIR}")
    print(f"Indexes: {', '.join(INDEXES)}")
    print()

    # Calculate distributions
    distributions = calculate_all_percentile_distributions()

    # Save to file
    save_percentile_distributions(distributions)

    print("\n" + "=" * 60)
    print("COMPLETE!")
    print(f"Generated distributions for {len([k for k in distributions.keys() if k != '_metadata'])} metrics:")
    for metric in distributions.keys():
        if metric != '_metadata':
            print(f"  - {metric}")

    # Test the lookup function
    print("\nTesting percentile lookup function...")
    test_metrics = [k for k in distributions.keys() if k != '_metadata']
    if test_metrics:
        test_metric = test_metrics[0]
        test_dist = distributions[test_metric]
        test_value = test_dist['mean']
        percentile_rank = value_to_percentile(test_value, test_dist)
        print(f"  Example: {test_metric} value {test_value:.6f} = {percentile_rank:.1f}th percentile")

    print(f"\nOutput saved to: {SECONDARIES_CACHE_DIR}")
