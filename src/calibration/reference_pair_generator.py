"""
Reference implementation of pair generation.

STATUS: reference implementation — not deployed

Produces a structurally valid pair universe using synthetic selection logic.
Pairs are formed from adjacent tickers within each sector and assigned
random but plausible EMA multipliers and CDF thresholds.

This implementation exists solely to allow the calibration pipeline to
execute end to end against synthetic data. It is deliberately simplistic
and is NOT representative of the deployed pair selection logic.

The real implementation analyses historical spread distributions, applies
cointegration and t-statistic filters, evaluates mean-reversion half-lives,
and calibrates per-pair thresholds through a walk-forward process.
None of that is replicated here.
"""

import logging

import numpy as np
import pandas as pd

from src.calibration.pair_generator_interface import PairGenerator

logger = logging.getLogger(__name__)


class ReferencePairGenerator(PairGenerator):
    """
    Synthetic pair generator that creates structurally valid output.

    Selection method:
        1. Form pairs from adjacent tickers in the universe (sorted alphabetically).
        2. Assign each pair both an upper and lower tail entry.
        3. Generate EMA multipliers and CDF thresholds from fixed random seeds.

    The output conforms to the parameters file contract so that downstream
    modules (pre_filter, LAM, parameters_extraction) can consume it without
    modification.
    """

    def __init__(self, seed: int = 42, n_pairs_per_index: int = 5):
        self._rng = np.random.default_rng(seed)
        self._n_pairs = n_pairs_per_index

    def generate(self, index, universe, beta_results, metrics, output_dir):
        """Generate synthetic pairs for one index. See interface for contract."""
        tickers = sorted(universe["Ticker"].tolist()) if "Ticker" in universe.columns else []

        if len(tickers) < 2:
            logger.warning("Fewer than 2 tickers for %s — no pairs generated", index)
            return pd.DataFrame()

        n_pairs = min(self._n_pairs, len(tickers) - 1)
        rows = []

        for i in range(n_pairs):
            co1, co2 = tickers[i], tickers[i + 1]

            for tail in ["L", "U"]:
                tag = f"{index}_{co1}_{co2}_{tail}"
                rows.append({
                    "Tag": tag,
                    "Pair": f"{co1}/{co2}",
                    "Co1": co1,
                    "Co2": co2,
                    "Index": index,
                    "Tail": tail,
                    "EMA_Multiplier": round(self._rng.uniform(1.0, 2.0), 2),
                    "CDF_Threshold": round(self._rng.uniform(0.10, 0.90), 2),
                })

        pairs_df = pd.DataFrame(rows)
        logger.info(
            "Reference pair generation for %s: %d pairs (%d tickers)",
            index, len(pairs_df), len(tickers),
        )
        return pairs_df

    def build_parameters_file(self, all_pairs, ticker_betas, output_path):
        """Assemble parameters Excel file. See interface for contract."""
        # --- Tickers sheet ---
        ticker_rows = []
        for ticker, info in ticker_betas.items():
            ticker_rows.append({
                "Ticker": ticker,
                "Index": info.get("index", ""),
                "SubSector_Beta": info.get("subsector_beta", 1.0),
                "Treasury_Beta": 0.0,
                "VO_Beta": round(self._rng.uniform(0.3, 1.2), 4),
            })
        tickers_df = pd.DataFrame(ticker_rows)

        # --- 15Day_Cumulative_Stats sheet ---
        stats_rows = []
        for _, pair in all_pairs.iterrows():
            stats_rows.append({
                "Tag": pair["Tag"],
                "Mean": round(self._rng.normal(0, 0.02), 6),
                "StdDev": round(self._rng.uniform(0.01, 0.05), 6),
                "Skew": round(self._rng.normal(0, 0.5), 4),
                "Kurtosis": round(self._rng.uniform(2.0, 5.0), 4),
            })
        stats_df = pd.DataFrame(stats_rows)

        # --- Sum_Deviation_Params sheet ---
        params_df = pd.DataFrame([
            {"Parameter": "Sum Deviation StdDev", "Value": 0.035},
            {"Parameter": "Mean Sum Deviation", "Value": 0.0},
        ])

        # --- Write ---
        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            all_pairs.to_excel(writer, sheet_name="Pairs", index=False)
            tickers_df.to_excel(writer, sheet_name="Tickers", index=False)
            stats_df.to_excel(writer, sheet_name="15Day_Cumulative_Stats", index=False)
            params_df.to_excel(writer, sheet_name="Sum_Deviation_Params", index=False)

        logger.info(
            "Reference parameters file written to %s: %d pairs, %d tickers",
            output_path, len(all_pairs), len(tickers_df),
        )
