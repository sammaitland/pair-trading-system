"""
Generate a synthetic pair cache fixture conforming to the pair cache schema.

STATUS: reference implementation — not deployed

This creates a minimal but structurally complete parameters file that allows
downstream modules (Pre_Filter, LAM) to run end to end without the real
Pair_Generator output.

The pairs, betas, and thresholds are synthetic. They do not represent real
calibration output and have no predictive content.

Usage::

    python -m fixtures.synthetic_pairs --output fixtures/Synthetic_Parameters.xlsx
"""

import argparse

import numpy as np
import pandas as pd


def generate_synthetic_parameters(output_path: str, n_pairs_per_index: int = 5):
    """
    Generate a synthetic parameters Excel file.

    Parameters
    ----------
    output_path : str
        Path to write the Excel file.
    n_pairs_per_index : int
        Number of synthetic pairs per sector index.
    """
    rng = np.random.default_rng(42)

    indices = ["VGT", "VFH", "VIS", "VHT", "VCR"]

    # Synthetic ticker pools per index
    ticker_pools = {
        "VGT": ["SYNTA", "SYNTB", "SYNTC", "SYNTD", "SYNTE", "SYNTF"],
        "VFH": ["SYNFA", "SYNFB", "SYNFC", "SYNFD", "SYNFE", "SYNFF"],
        "VIS": ["SYNIA", "SYNIB", "SYNIC", "SYNID", "SYNIE", "SYNIF"],
        "VHT": ["SYNHA", "SYNHB", "SYNHC", "SYNHD", "SYNHE", "SYNHF"],
        "VCR": ["SYNCA", "SYNCB", "SYNCC", "SYNCD", "SYNCE", "SYNCF"],
    }

    # --- Pairs sheet ---
    pairs_rows = []
    for idx in indices:
        tickers = ticker_pools[idx]
        for i in range(n_pairs_per_index):
            co1, co2 = tickers[i], tickers[i + 1]
            for tail in ["L", "U"]:
                tag = f"{idx}_{co1}_{co2}_{tail}"
                pairs_rows.append({
                    "Tag": tag,
                    "Pair": f"{co1}/{co2}",
                    "Co1": co1,
                    "Co2": co2,
                    "Index": idx,
                    "Tail": tail,
                    "EMA_Multiplier": round(rng.uniform(1.0, 2.0), 2),
                    "CDF_Threshold": round(rng.uniform(0.10, 0.90), 2),
                })

    pairs_df = pd.DataFrame(pairs_rows)

    # --- Tickers sheet ---
    all_tickers = set()
    for pool in ticker_pools.values():
        all_tickers.update(pool)

    ticker_rows = []
    for idx in indices:
        for ticker in ticker_pools[idx]:
            ticker_rows.append({
                "Ticker": ticker,
                "Index": idx,
                "SubSector_Beta": round(rng.uniform(0.5, 1.5), 4),
                "Treasury_Beta": 0.0,
                "VO_Beta": round(rng.uniform(0.3, 1.2), 4),
            })

    tickers_df = pd.DataFrame(ticker_rows)

    # --- 15Day_Cumulative_Stats sheet ---
    stats_rows = []
    for _, pair in pairs_df.iterrows():
        stats_rows.append({
            "Tag": pair["Tag"],
            "Mean": round(rng.normal(0, 0.02), 6),
            "StdDev": round(rng.uniform(0.01, 0.05), 6),
            "Skew": round(rng.normal(0, 0.5), 4),
            "Kurtosis": round(rng.uniform(2.0, 5.0), 4),
        })

    stats_df = pd.DataFrame(stats_rows)

    # --- Sum_Deviation_Params sheet ---
    params_df = pd.DataFrame([
        {"Parameter": "Sum Deviation StdDev", "Value": 0.035},
        {"Parameter": "Mean Sum Deviation", "Value": 0.0},
    ])

    # --- Write ---
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        pairs_df.to_excel(writer, sheet_name="Pairs", index=False)
        tickers_df.to_excel(writer, sheet_name="Tickers", index=False)
        stats_df.to_excel(writer, sheet_name="15Day_Cumulative_Stats", index=False)
        params_df.to_excel(writer, sheet_name="Sum_Deviation_Params", index=False)

    print(f"Synthetic parameters written to {output_path}")
    print(f"  Pairs: {len(pairs_df)}")
    print(f"  Tickers: {len(tickers_df)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default="fixtures/Synthetic_Parameters.xlsx",
        help="Output path for synthetic parameters file",
    )
    parser.add_argument(
        "--pairs-per-index",
        type=int,
        default=5,
        help="Number of synthetic pairs per sector index",
    )
    args = parser.parse_args()
    generate_synthetic_parameters(args.output, args.pairs_per_index)
