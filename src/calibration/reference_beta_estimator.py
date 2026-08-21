"""
Naive reference implementation of beta estimation.

STATUS: reference implementation — not deployed

This estimator computes a simple OLS beta for each ticker against an
equal-weight index of all tickers in the same sector. It does not perform
clustering, adaptive lookbacks, or R-squared filtering.

The real implementation uses dynamic sub-sector clustering to produce
category-specific indices, resulting in more precise beta isolation.
"""

import logging

import numpy as np
import pandas as pd

from src.calibration.beta_estimator_interface import BetaEstimator

logger = logging.getLogger(__name__)


class ReferenceBetaEstimator(BetaEstimator):
    """
    Simple OLS beta estimator using an equal-weight peer index.

    All tickers in a sector are assigned to a single category ("All").
    The index is the equal-weight average daily return of all constituents.
    Beta is estimated via OLS regression of ticker returns on index returns.

    This is a reference implementation only.
    """

    def estimate(self, etf_ticker, tickers, price_data, output_dir):
        """Estimate betas using simple OLS. See interface for contract."""
        import os

        results = {}

        # Build equal-weight index returns
        all_returns = {}
        for ticker in tickers:
            df = price_data.get(ticker)
            if df is not None and "close" in df.columns and len(df) > 1:
                rets = df["close"].pct_change().dropna()
                all_returns[ticker] = rets

        if not all_returns:
            logger.warning("No valid price data for %s", etf_ticker)
            return results

        returns_df = pd.DataFrame(all_returns)
        returns_df = returns_df.dropna(how="all")

        # Equal-weight index
        index_returns = returns_df.mean(axis=1)

        # Estimate betas via OLS
        index_var = index_returns.var()
        if index_var == 0 or np.isnan(index_var):
            logger.warning("Zero variance in index returns for %s", etf_ticker)
            return results

        for ticker in tickers:
            if ticker not in returns_df.columns:
                results[ticker] = {
                    "subsector_beta": 1.0,
                    "category": "All",
                    "r_squared": 0.0,
                }
                continue

            ticker_rets = returns_df[ticker].dropna()
            common = ticker_rets.index.intersection(index_returns.index)

            if len(common) < 30:
                results[ticker] = {
                    "subsector_beta": 1.0,
                    "category": "All",
                    "r_squared": 0.0,
                }
                continue

            x = index_returns.loc[common]
            y = ticker_rets.loc[common]

            cov = np.cov(y, x)[0, 1]
            var = np.var(x, ddof=1)
            beta = cov / var if var > 0 else 1.0

            # R-squared
            y_hat = beta * x
            ss_res = ((y - y_hat) ** 2).sum()
            ss_tot = ((y - y.mean()) ** 2).sum()
            r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

            results[ticker] = {
                "subsector_beta": float(np.clip(beta, -5, 5)),
                "category": "All",
                "r_squared": float(np.clip(r_squared, 0, 1)),
            }

        # Write output Excel
        output_file = os.path.join(
            output_dir, etf_ticker, f"{etf_ticker}_SubSector_Beta_Analysis.xlsx"
        )
        os.makedirs(os.path.dirname(output_file), exist_ok=True)

        # SubSector Indices sheet — single "All" category
        indices_df = pd.DataFrame({"All": index_returns})

        # Cluster Assignments sheet
        assignments = []
        for ticker, info in results.items():
            assignments.append({
                "Ticker": ticker,
                "Category": info["category"],
                "SubSector_Beta": info["subsector_beta"],
            })
        assignments_df = pd.DataFrame(assignments)

        with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
            indices_df.to_excel(writer, sheet_name="SubSector Indices")
            assignments_df.to_excel(writer, sheet_name="Cluster Assignments", index=False)

        logger.info(
            "Reference beta estimation complete for %s: %d tickers",
            etf_ticker,
            len(results),
        )
        return results
