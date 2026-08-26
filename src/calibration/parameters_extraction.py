#!/usr/bin/env python3
"""
Parameters extraction from calibration outputs.

Extracts ticker and pair data from beta analysis and pair trading outputs,
then creates a Parameters.xlsx file with Tickers, Pairs, Net Alpha Deviations,
and Sum Deviation parameters. Reads ACTIVE_VERSION from config to determine
which calibration outputs to use.

STATUS: live
"""

import pandas as pd
import numpy as np
import os
from pathlib import Path

from src.shared import config

# =============================================================================
# VERSION CONFIGURATION - Read from config
# =============================================================================

def get_version_paths():
    """
    Get version-aware paths from config.
    Returns tuple: (version, base_dir, implementation_dir)
    """
    version = config.active_version()

    # Ensure version string format (e.g., "9.3" -> "V9.3")
    if not version.startswith('V'):
        version_str = f'V{version}'
    else:
        version_str = version

    base_dir = Path(config.get_version_dir(config.active_version()))
    implementation_dir = base_dir / "Implementation"

    print(f"Using ACTIVE_VERSION: {version_str}")
    print(f"Base directory: {base_dir}")
    print(f"Implementation directory: {implementation_dir}")

    return version_str, base_dir, implementation_dir


def extract_parameters():
    """
    Extract parameters from beta analysis and pair trading outputs.
    Version-aware: reads from config.active_version()
    """

    # Get version-aware paths
    VERSION, base_dir, implementation_dir = get_version_paths()

    # Create Implementation directory if it doesn't exist
    implementation_dir.mkdir(exist_ok=True)

    # Define indexes and their configuration
    indexes = ['VGT', 'VFH', 'VIS', 'VHT', 'VCR']

    # Index-specific configuration from the pair trading script (MANUAL - UPDATE AS NEEDED)
    # These should match the values in your pair generator's index_specific_config
    index_config = {
        'VGT': {
            'long_ema_multiplier': 1.3,
            'short_ema_multiplier': 1.2,
            'long_cdf_threshold': 0.19,
            'short_cdf_threshold': 0.75
        },
        'VHT': {
            'long_ema_multiplier': 1.2,
            'short_ema_multiplier': 1.2,
            'long_cdf_threshold': 0.20,
            'short_cdf_threshold': 0.74
        },
        'VCR': {
            'long_ema_multiplier': 1.45,
            'short_ema_multiplier': 1.3,
            'long_cdf_threshold': 0.14,
            'short_cdf_threshold': 0.83
        },
        'VIS': {
            'long_ema_multiplier': 1.45,
            'short_ema_multiplier': 1.3,
            'long_cdf_threshold': 0.12,
            'short_cdf_threshold': 0.86
        },
        'VFH': {
            'long_ema_multiplier': 1.6,
            'short_ema_multiplier': 1.5,
            'long_cdf_threshold': 0.11,
            'short_cdf_threshold': 0.88
        }
    }

    print(f"\nExtracting {VERSION} Parameters...")
    print("="*60)

    # Initialize data containers
    all_tickers = {}  # {ticker: {index: str, subsector_beta: float, treasury_beta: float, vo_beta: float}}
    all_pairs = []    # List of pair dictionaries
    all_net_deviations = []  # List of 15-day cumulative stats

    # Process each index
    for index in indexes:
        print(f"\nProcessing {index}...")

        index_dir = base_dir / index
        beta_file = index_dir / f"{index}_SubSector_Beta_Analysis.xlsx"
        pairs_file = index_dir / f"{index}_Pair_Trading_Results.xlsx"

        # Check if files exist
        if not beta_file.exists():
            print(f"  Warning: Beta analysis file not found: {beta_file}")
            continue

        if not pairs_file.exists():
            print(f"  Warning: Pair trading file not found: {pairs_file}")
            continue

        # Extract ticker data from beta analysis
        try:
            # Load SubSector Beta Summary to get the most recent betas
            beta_df = pd.read_excel(beta_file, sheet_name='SubSector Beta Summary', index_col=0)

            print(f"  Found {len(beta_df)} tickers in {index} beta analysis")

            for ticker in beta_df.index:
                if ticker not in all_tickers:
                    all_tickers[ticker] = {
                        'Index': index,
                        'SubSector_Beta': beta_df.loc[ticker, 'subsector_beta'] if 'subsector_beta' in beta_df.columns else np.nan,
                        'Treasury_Beta': beta_df.loc[ticker, 'treasury_beta'] if 'treasury_beta' in beta_df.columns else np.nan,
                        'VO_Beta': beta_df.loc[ticker, 'vo_beta'] if 'vo_beta' in beta_df.columns else np.nan
                    }

        except Exception as e:
            print(f"  Error processing beta data for {index}: {e}")
            continue

        # Extract pair data
        try:
            # Load selected pairs
            pairs_df = pd.read_excel(pairs_file, sheet_name='Selected Pairs')

            print(f"  Found {len(pairs_df)} pairs in {index} pair trading results")

            # Get index-specific trading parameters
            idx_config = index_config.get(index, {})

            for _, row in pairs_df.iterrows():
                stock1 = str(row['Stock1']).strip()
                stock2 = str(row['Stock2']).strip()

                # Create pair identifier (format: TICKER-TICKER)
                pair_id = f"{stock1}-{stock2}"

                # Add Lower (Long) trade
                lower_trade = {
                    'Index': index,
                    'Pair': pair_id,
                    'Co1': stock1,
                    'Co2': stock2,
                    'Tail': 'L',  # Lower (previously "long")
                    'EMA_Multiplier': idx_config.get('long_ema_multiplier', np.nan),
                    'CDF_Threshold': idx_config.get('long_cdf_threshold', np.nan)
                }
                all_pairs.append(lower_trade)

                # Add Upper (Short) trade
                upper_trade = {
                    'Index': index,
                    'Pair': pair_id,
                    'Co1': stock1,
                    'Co2': stock2,
                    'Tail': 'U',  # Upper (previously "short")
                    'EMA_Multiplier': idx_config.get('short_ema_multiplier', np.nan),
                    'CDF_Threshold': idx_config.get('short_cdf_threshold', np.nan)
                }
                all_pairs.append(upper_trade)

        except Exception as e:
            print(f"  Error processing pair data for {index}: {e}")
            continue

        # Extract 15-day cumulative alpha statistics
        try:
            xl_file = pd.ExcelFile(pairs_file)
            if '15Day Cumulative Stats' in xl_file.sheet_names:
                cumulative_stats_df = pd.read_excel(pairs_file, sheet_name='15Day Cumulative Stats')

                # Add index information
                cumulative_stats_df['Index'] = index

                # Reorder columns to put Index first
                cols = ['Index'] + [col for col in cumulative_stats_df.columns if col != 'Index']
                cumulative_stats_df = cumulative_stats_df[cols]

                all_net_deviations.append(cumulative_stats_df)
                print(f"  Found {len(cumulative_stats_df)} 15-day cumulative statistics")
            else:
                print(f"  Warning: No '15Day Cumulative Stats' sheet found in {index}")

        except Exception as e:
            print(f"  Error processing 15-day cumulative stats for {index}: {e}")
            continue

    # Extract sum deviation parameters from optimization report
    print(f"\nExtracting Sum Deviation Parameters...")
    sum_dev_params = None
    opt_report_path = base_dir / "Enhanced_Optimization_Report_SpreadCosts_SumDeviation.xlsx"

    if opt_report_path.exists():
        try:
            # Read the Optimization Report tab
            opt_df = pd.read_excel(opt_report_path, sheet_name='Optimization Report')

            # Find the Sum Deviation StdDev parameter
            sum_dev_stddev = None
            sum_dev_mean = None
            for idx, row in opt_df.iterrows():
                if pd.notna(row.iloc[0]):
                    param_name = str(row.iloc[0])
                    if 'Sum Deviation StdDev' in param_name:
                        sum_dev_stddev = row.iloc[1]
                    elif 'Sum Deviation Mean' in param_name:
                        sum_dev_mean = row.iloc[1]

            if sum_dev_stddev is not None:
                print(f"  Found Sum Deviation StdDev: {sum_dev_stddev}")
                sum_dev_params = {
                    'sum_deviation_stddev': sum_dev_stddev,
                    'sum_deviation_mean': sum_dev_mean if sum_dev_mean is not None else 0,
                    'source_file': str(opt_report_path)
                }
            else:
                print("  Warning: Sum Deviation StdDev parameter not found in optimization report")

        except Exception as e:
            print(f"  Error reading optimization report: {e}")
    else:
        print(f"  Warning: Optimization report not found: {opt_report_path}")

    # Create Tickers DataFrame
    print(f"\nCreating Tickers tab with {len(all_tickers)} unique tickers...")
    tickers_df = pd.DataFrame.from_dict(all_tickers, orient='index')
    tickers_df.index.name = 'Ticker'
    tickers_df = tickers_df.reset_index()

    # Sort by Index then by Ticker
    tickers_df = tickers_df.sort_values(['Index', 'Ticker']).reset_index(drop=True)

    # Create Pairs DataFrame
    print(f"Creating Pairs tab with {len(all_pairs)} trades...")
    pairs_df = pd.DataFrame(all_pairs)

    # Sort by Index and Tail (L trades first, then U trades)
    pairs_df = pairs_df.sort_values(['Index', 'Tail', 'Pair']).reset_index(drop=True)

    # Add Tag and Number columns
    tag_counter = 1
    l_number = 1
    u_number = 1

    tags = []
    numbers = []

    for _, row in pairs_df.iterrows():
        if row['Tail'] == 'L':
            tags.append(tag_counter)
            numbers.append(l_number)
            l_number += 1
        else:  # Tail == 'U'
            tags.append(tag_counter)
            numbers.append(u_number)
            u_number += 1
        tag_counter += 1

    pairs_df['Tag'] = tags
    pairs_df['Number'] = numbers

    # Reorder columns for final output
    pairs_df = pairs_df[['Tag', 'Number', 'Index', 'Pair', 'Co1', 'Co2', 'Tail', 'EMA_Multiplier', 'CDF_Threshold']]

    # Create 15-Day Cumulative Stats DataFrame
    if all_net_deviations:
        cumulative_stats_df = pd.concat(all_net_deviations, ignore_index=True)
        print(f"Creating 15-Day Cumulative Stats tab with {len(cumulative_stats_df)} records...")
    else:
        # Create empty DataFrame with expected columns
        cumulative_stats_df = pd.DataFrame(columns=[
            'Index', 'Pair_Name', 'Stock1', 'Stock2',
            'Mean_15Day_Cumulative', 'Std_15Day_Cumulative',
            'Min_15Day_Cumulative', 'Max_15Day_Cumulative', 'Sample_Count'
        ])
        print(f"Creating empty 15-Day Cumulative Stats tab (no data found)...")

    # Save to Excel - VERSION-AWARE FILENAME
    output_file = implementation_dir / f"{VERSION}_Parameters.xlsx"

    with pd.ExcelWriter(output_file, engine='xlsxwriter') as writer:
        # Write Tickers tab
        tickers_df.to_excel(writer, sheet_name='Tickers', index=False)

        # Write Pairs tab
        pairs_df.to_excel(writer, sheet_name='Pairs', index=False)

        # Write 15-Day Cumulative Stats tab
        cumulative_stats_df.to_excel(writer, sheet_name='15Day_Cumulative_Stats', index=False)

        # Write Sum Deviation Parameters tab
        if sum_dev_params:
            sum_df = pd.DataFrame([
                ['Sum Deviation StdDev', sum_dev_params['sum_deviation_stddev']],
                ['Sum Deviation Mean', sum_dev_params.get('sum_deviation_mean', 0)],
                ['Source File', sum_dev_params['source_file']],
                ['Version', VERSION],
                ['Note', 'Single global standard deviation used for all pairs'],
                ['Calculation Method', 'Calculated from collection of all sum deviation values']
            ], columns=['Parameter', 'Value'])
        else:
            sum_df = pd.DataFrame([
                ['Sum Deviation StdDev', 'NOT FOUND'],
                ['Sum Deviation Mean', 'NOT FOUND'],
                ['Source File', 'Enhanced_Optimization_Report_SpreadCosts_SumDeviation.xlsx'],
                ['Version', VERSION],
                ['Note', 'File not found or parameter missing'],
                ['Calculation Method', 'Calculated from collection of all sum deviation values']
            ], columns=['Parameter', 'Value'])

        sum_df.to_excel(writer, sheet_name='Sum_Deviation_Params', index=False)

        # Write Data Sources Summary tab
        sources_data = []
        sources_data.append(['Data Type', 'Source Location', 'Status', 'Records/Value'])
        sources_data.append(['Tickers', 'Individual index files / SubSector Beta Summary',
                           'Found' if len(tickers_df) > 0 else 'Missing', len(tickers_df)])
        sources_data.append(['Pairs', 'Individual index files / Selected Pairs',
                           'Found' if len(pairs_df) > 0 else 'Missing', len(pairs_df)])
        sources_data.append(['15-Day Cumulative Stats', 'Individual index files / 15Day Cumulative Stats',
                           'Found' if len(cumulative_stats_df) > 0 else 'Missing', len(cumulative_stats_df)])
        sources_data.append(['Sum Deviation StdDev', 'Enhanced_Optimization_Report_SpreadCosts_SumDeviation.xlsx',
                           'Found' if sum_dev_params else 'Missing',
                           sum_dev_params['sum_deviation_stddev'] if sum_dev_params else 'N/A'])

        sources_df = pd.DataFrame(sources_data[1:], columns=sources_data[0])
        sources_df.to_excel(writer, sheet_name='Data_Sources', index=False)

        # Write Version Info tab
        version_info = pd.DataFrame([
            ['Version', VERSION],
            ['Base Directory', str(base_dir)],
            ['Implementation Directory', str(implementation_dir)],
            ['Generated At', pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')],
            ['Config Source', 'config.active_version()'],
        ], columns=['Parameter', 'Value'])
        version_info.to_excel(writer, sheet_name='Version_Info', index=False)

        # Write Usage Instructions tab
        instructions = [
            ['Component', 'Usage', 'Notes'],
            ['Tickers', 'Beta coefficients for individual stocks',
             'SubSector, Treasury, and VO betas from beta analysis'],
            ['Pairs', 'Trading pair configuration and parameters',
             'EMA multipliers and CDF thresholds for each tail'],
            ['15-Day Cumulative Stats', 'CDF hurdle calculations for trade triggering',
             'Arithmetic sum approach - mean and std for each pair'],
            ['Sum Deviation StdDev', 'Sum deviation calculations in optimizer',
             'Single global std for exclusion filtering'],
            ['Tag/Number System', 'Unique identifiers for each trade',
             'Tag increments for each trade, Number tracks L/U within pairs'],
            ['Version Info', 'Tracks which calibration version produced these parameters',
             'Used for position versioning during transitions']
        ]

        instructions_df = pd.DataFrame(instructions[1:], columns=instructions[0])
        instructions_df.to_excel(writer, sheet_name='Usage_Instructions', index=False)

        # Format the sheets
        workbook = writer.book

        # Format Tickers sheet
        tickers_worksheet = writer.sheets['Tickers']
        tickers_worksheet.set_column('A:A', 12)  # Ticker
        tickers_worksheet.set_column('B:B', 8)   # Index
        tickers_worksheet.set_column('C:C', 15)  # SubSector_Beta
        tickers_worksheet.set_column('D:D', 15)  # Treasury_Beta
        tickers_worksheet.set_column('E:E', 15)  # VO_Beta

        # Format Pairs sheet
        pairs_worksheet = writer.sheets['Pairs']
        pairs_worksheet.set_column('A:A', 8)   # Tag
        pairs_worksheet.set_column('B:B', 8)   # Number
        pairs_worksheet.set_column('C:C', 8)   # Index
        pairs_worksheet.set_column('D:D', 20)  # Pair
        pairs_worksheet.set_column('E:E', 8)   # Co1
        pairs_worksheet.set_column('F:F', 8)   # Co2
        pairs_worksheet.set_column('G:G', 6)   # Tail
        pairs_worksheet.set_column('H:H', 15)  # EMA_Multiplier
        pairs_worksheet.set_column('I:I', 15)  # CDF_Threshold

        # Format 15-Day Cumulative Stats sheet
        cumulative_worksheet = writer.sheets['15Day_Cumulative_Stats']
        cumulative_worksheet.set_column('A:A', 8)   # Index
        cumulative_worksheet.set_column('B:B', 20)  # Pair_Name
        cumulative_worksheet.set_column('C:C', 10)  # Stock1
        cumulative_worksheet.set_column('D:D', 10)  # Stock2
        cumulative_worksheet.set_column('E:E', 18)  # Mean_15Day_Cumulative
        cumulative_worksheet.set_column('F:F', 18)  # Std_15Day_Cumulative
        cumulative_worksheet.set_column('G:G', 18)  # Min_15Day_Cumulative
        cumulative_worksheet.set_column('H:H', 18)  # Max_15Day_Cumulative
        cumulative_worksheet.set_column('I:I', 15)  # Sample_Count

        # Format Sum Deviation Parameters sheet
        sum_dev_worksheet = writer.sheets['Sum_Deviation_Params']
        sum_dev_worksheet.set_column('A:A', 25)  # Parameter
        sum_dev_worksheet.set_column('B:B', 50)  # Value

        # Format Data Sources sheet
        sources_worksheet = writer.sheets['Data_Sources']
        sources_worksheet.set_column('A:A', 25)  # Data Type
        sources_worksheet.set_column('B:B', 50)  # Source Location
        sources_worksheet.set_column('C:C', 10)  # Status
        sources_worksheet.set_column('D:D', 15)  # Records/Value

        # Format Usage Instructions sheet
        usage_worksheet = writer.sheets['Usage_Instructions']
        usage_worksheet.set_column('A:A', 25)  # Component
        usage_worksheet.set_column('B:B', 40)  # Usage
        usage_worksheet.set_column('C:C', 50)  # Notes

        # Add number formatting
        number_format = workbook.add_format({'num_format': '0.0000'})
        tickers_worksheet.set_column('C:E', 15, number_format)
        pairs_worksheet.set_column('H:I', 15, number_format)
        cumulative_worksheet.set_column('E:H', 18, number_format)

    # Print summary
    print(f"\n" + "="*60)
    print(f"{VERSION} Parameters extraction completed!")
    print(f"Output file: {output_file}")
    print(f"\nTickers tab: {len(tickers_df)} unique tickers (with VO betas)")
    print(f"Pairs tab: {len(all_pairs)} trades")
    print(f"15-Day Cumulative Stats tab: {len(cumulative_stats_df)} records")
    print(f"Sum Deviation Params: {'Found' if sum_dev_params else 'Not found'}")

    # Show breakdown by index
    print(f"\nBreakdown by Index:")
    ticker_counts = tickers_df['Index'].value_counts().sort_index()
    for index, count in ticker_counts.items():
        print(f"  {index}: {count} tickers")

    print(f"\nTrades by Index and Tail:")
    if len(pairs_df) > 0:
        trade_counts = pairs_df.groupby(['Index', 'Tail']).size().unstack(fill_value=0)
        for index in trade_counts.index:
            l_count = trade_counts.loc[index, 'L'] if 'L' in trade_counts.columns else 0
            u_count = trade_counts.loc[index, 'U'] if 'U' in trade_counts.columns else 0
            total = l_count + u_count
            print(f"  {index}: {l_count} Lower + {u_count} Upper = {total} total trades")

    print(f"\nTotal trades: {len(pairs_df)} ({len(pairs_df)//2} pairs x 2 tails)")

    if len(cumulative_stats_df) > 0:
        print(f"\n15-Day Cumulative Stats by Index:")
        stats_counts = cumulative_stats_df['Index'].value_counts().sort_index()
        for index, count in stats_counts.items():
            print(f"  {index}: {count} pair statistics")

    print("="*60)

    return output_file, tickers_df, pairs_df, cumulative_stats_df, sum_dev_params


# Execute the extraction
if __name__ == "__main__":
    try:
        output_file, tickers_df, pairs_df, cumulative_stats_df, sum_dev_params = extract_parameters()

        # Display sample data
        print(f"\nSample Tickers data:")
        print(tickers_df.head(10))

        print(f"\nSample Pairs data:")
        print(pairs_df.head(10))

        if len(cumulative_stats_df) > 0:
            print(f"\nSample 15-Day Cumulative Stats:")
            print(cumulative_stats_df.head(10))

        if sum_dev_params:
            print(f"\nSum Deviation Parameters:")
            print(f"  StdDev: {sum_dev_params['sum_deviation_stddev']}")
            print(f"  Source: {sum_dev_params['source_file']}")

        print(f"\nFile saved successfully: {output_file}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
