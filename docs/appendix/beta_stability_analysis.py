"""
Beta Stability Diagnostic
=========================
Objective: Detect ticker-level beta regime changes that make the calibrated
alpha series unreliable, using relative R² (rolling R² / calibrated R²)
as the primary signal.

For each of 4 filter levels (keep top 80%, 85%, 90%, 95% of trades
by Co1 relative R²), compute improvement vs unfiltered baseline across
mean return, win rate, Sharpe-like ratio, and trades retained.

Inputs (all on disk — no live market connection required):
    - Pair cache files: {VERSION_DIR}/cache/{INDEX}_pair_cache.pkl
      (trade_triggers_df — full historical backtest universe)
    - SubSector Beta Analysis: {VERSION_DIR}/{INDEX}/{INDEX}_SubSector_Beta_Analysis.xlsx
      (calibrated R², sub-sector indices, raw returns)

Output:
    - Diagnostics/beta_stability/data/latest_run.xlsx (4 sheets)
    - Diagnostics/beta_stability/data/chart_*.png (4 charts)
"""

import pickle
import warnings
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

warnings.filterwarnings("ignore", category=FutureWarning)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# NOTE: Set this to your calibration version directory before running.
V93_DIR = Path(".")  # e.g. Path.home() / "Desktop" / "V9" / "V9.3"
CACHE_DIR = V93_DIR / "cache"
OUTPUT_DIR = Path(__file__).resolve().parent / "data"
OUTPUT_PATH = OUTPUT_DIR / "latest_run.xlsx"

INDICES = ["VGT", "VFH", "VIS", "VHT", "VCR"]
ROLLING_WINDOW = 15

# Filter levels: keep trades where Co1 relative R² is in the top N%
FILTER_PERCENTILES = [80, 85, 90, 95]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_beta_summary(index: str) -> pd.DataFrame:
    """Load calibrated beta summary (one row per ticker)."""
    path = V93_DIR / index / f"{index}_SubSector_Beta_Analysis.xlsx"
    return pd.read_excel(path, sheet_name="SubSector Beta Summary")


def load_subsector_indices(index: str) -> pd.DataFrame:
    """Load daily sub-sector index returns."""
    path = V93_DIR / index / f"{index}_SubSector_Beta_Analysis.xlsx"
    df = pd.read_excel(path, sheet_name="SubSector Indices")
    df.rename(columns={"Unnamed: 0": "Date"}, inplace=True)
    df["Date"] = pd.to_datetime(df["Date"])
    df.set_index("Date", inplace=True)
    return df


def load_raw_returns(index: str) -> pd.DataFrame:
    """Load daily raw returns per ticker (long history)."""
    path = V93_DIR / index / f"{index}_SubSector_Beta_Analysis.xlsx"
    df = pd.read_excel(path, sheet_name="Raw Returns Series")
    df.rename(columns={"Unnamed: 0": "Date"}, inplace=True)
    df["Date"] = pd.to_datetime(df["Date"])
    df.set_index("Date", inplace=True)
    return df


def load_trade_triggers() -> pd.DataFrame:
    """
    Load the full historical trade universe from pair cache files.
    Returns a single DataFrame across all indices.
    """
    all_trades = []
    for index in INDICES:
        path = CACHE_DIR / f"{index}_pair_cache.pkl"
        with open(path, "rb") as f:
            cache = pickle.load(f)
        df = cache["trade_triggers_df"].copy()
        df["index"] = index

        pair_parts = df["Pair"].str.split("_", n=1, expand=True)
        df["co1"] = pair_parts[0]
        df["co2"] = pair_parts[1]

        df.rename(
            columns={
                "Pair": "pair",
                "Date": "date",
                "Trigger_Type": "tail",
                "Forward_15Day_Return": "forward_15d_return",
                "CDF_Value": "cdf_value",
                "Co1_Tstat": "co1_tstat",
                "Cumulative_15Day_Alpha": "cum_15d_alpha",
            },
            inplace=True,
        )

        df["tail"] = df["tail"].map({"long": "Lower", "short": "Upper"}).fillna(df["tail"])
        df["date"] = pd.to_datetime(df["date"]).dt.normalize()

        all_trades.append(
            df[
                [
                    "pair", "date", "co1", "co2", "index", "tail",
                    "forward_15d_return", "cdf_value", "co1_tstat", "cum_15d_alpha",
                ]
            ]
        )

    return pd.concat(all_trades, ignore_index=True)


# ---------------------------------------------------------------------------
# Rolling regression
# ---------------------------------------------------------------------------
def rolling_ols(y: pd.Series, x: pd.Series, window: int) -> pd.DataFrame:
    """
    Compute rolling OLS: y = alpha + beta * x.
    Returns DataFrame with columns: beta, r_squared.
    """
    results = []
    y_arr = y.values
    x_arr = x.values

    for i in range(window, len(y_arr) + 1):
        y_w = y_arr[i - window : i]
        x_w = x_arr[i - window : i]

        mask = ~(np.isnan(y_w) | np.isnan(x_w))
        if mask.sum() < max(5, window // 2):
            results.append((np.nan, np.nan))
            continue

        y_clean = y_w[mask]
        x_clean = x_w[mask]

        slope, intercept, r_value, p_value, std_err = stats.linregress(x_clean, y_clean)
        results.append((slope, r_value**2))

    idx = y.index[window - 1 :]
    return pd.DataFrame(results, index=idx, columns=["beta", "r_squared"])


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------
def compute_rolling_metrics_for_index(index: str) -> pd.DataFrame:
    """
    For each ticker in the index, compute rolling 15-day beta and R²
    vs its sub-sector index. Computes relative_r2 = rolling / calibrated.
    """
    raw_returns = load_raw_returns(index)
    subsector_idx = load_subsector_indices(index)
    beta_summary = load_beta_summary(index)

    ticker_cat = dict(zip(beta_summary["Ticker"], beta_summary["category_name"]))
    ticker_cal_r2 = dict(zip(beta_summary["Ticker"], beta_summary["r_squared"]))

    tickers = [t for t in beta_summary["Ticker"] if t in raw_returns.columns]

    cutoff = pd.Timestamp("2016-01-01")
    raw_returns = raw_returns.loc[raw_returns.index >= cutoff]
    subsector_idx = subsector_idx.loc[subsector_idx.index >= cutoff]

    all_results = []

    for ticker in tickers:
        cat = ticker_cat.get(ticker)
        if cat is None or cat not in subsector_idx.columns:
            continue

        y = raw_returns[ticker]
        x = subsector_idx[cat]

        common_idx = y.dropna().index.intersection(x.dropna().index)
        if len(common_idx) < ROLLING_WINDOW:
            continue

        y_aligned = y.loc[common_idx]
        x_aligned = x.loc[common_idx]

        rolling = rolling_ols(y_aligned, x_aligned, ROLLING_WINDOW)
        cal_r2 = ticker_cal_r2.get(ticker, np.nan)
        rolling["ticker"] = ticker
        rolling["index"] = index
        rolling["category"] = cat
        rolling["calibrated_r2"] = cal_r2
        rolling["relative_r2"] = rolling["r_squared"] / cal_r2 if cal_r2 > 0 else np.nan
        rolling.index.name = "date"
        rolling = rolling.reset_index()

        all_results.append(rolling)

    if not all_results:
        return pd.DataFrame()

    return pd.concat(all_results, ignore_index=True)


def link_trades_to_stability(
    rolling_df: pd.DataFrame,
    trades_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    For each trade, look up rolling R² and relative R² at entry date
    for Co1 and Co2.
    """
    r2_lookup = {}
    for _, row in rolling_df.iterrows():
        key = (row["index"], row["ticker"], row["date"])
        r2_lookup[key] = (row["r_squared"], row["relative_r2"])

    results = []

    for _, trade in trades_df.iterrows():
        entry_date = trade["date"]
        if pd.isna(entry_date):
            continue

        row = {
            "pair": trade["pair"],
            "index": trade["index"],
            "tail": trade["tail"],
            "entry_date": entry_date,
            "forward_15d_return": trade["forward_15d_return"],
            "cdf_value": trade["cdf_value"],
            "co1_tstat_entry": trade["co1_tstat"],
            "cum_15d_alpha": trade["cum_15d_alpha"],
        }

        for leg, ticker in [("co1", trade["co1"]), ("co2", trade["co2"])]:
            key = (trade["index"], ticker, entry_date)
            r2_vals = r2_lookup.get(key)
            row[f"{leg}_ticker"] = ticker
            if r2_vals is not None:
                row[f"{leg}_r2"] = r2_vals[0]
                row[f"{leg}_relative_r2"] = r2_vals[1]
            else:
                row[f"{leg}_r2"] = np.nan
                row[f"{leg}_relative_r2"] = np.nan

        results.append(row)

    return pd.DataFrame(results)


# ---------------------------------------------------------------------------
# Outcome metrics
# ---------------------------------------------------------------------------
def _metrics(returns: pd.Series, n_baseline: int) -> dict:
    """Compute outcome metrics for a series of trade returns."""
    n = len(returns)
    mean = returns.mean()
    std = returns.std()
    return {
        "n_trades": n,
        "pct_retained": n / n_baseline * 100 if n_baseline > 0 else 0.0,
        "mean_return": mean,
        "median_return": returns.median(),
        "win_rate": (returns > 0).mean() * 100,
        "sharpe": mean / std if std > 0 else np.nan,
        "std_return": std,
        "worst_return": returns.min(),
        "best_return": returns.max(),
    }


def compute_overall_summary(trade_linkage: pd.DataFrame) -> pd.DataFrame:
    """
    For each filter percentile, compute metrics overall.
    Filter = keep trades where co1_relative_r2 >= percentile cutoff
    (i.e. top N% means keeping the highest relative R² trades).
    """
    valid = trade_linkage.dropna(subset=["forward_15d_return", "co1_relative_r2"])
    n_baseline = len(valid)
    rows = []

    rows.append({"filter": "Baseline (no filter)", **_metrics(valid["forward_15d_return"], n_baseline)})

    for pct in FILTER_PERCENTILES:
        cutoff = np.percentile(valid["co1_relative_r2"], 100 - pct)
        filtered = valid[valid["co1_relative_r2"] >= cutoff]
        rows.append({"filter": f"Top {pct}%", "rr2_cutoff": cutoff, **_metrics(filtered["forward_15d_return"], n_baseline)})

    return pd.DataFrame(rows)


def compute_by_index(trade_linkage: pd.DataFrame) -> pd.DataFrame:
    """Same threshold sweep broken down by index."""
    valid = trade_linkage.dropna(subset=["forward_15d_return", "co1_relative_r2"])
    rows = []

    for index in INDICES:
        idx_df = valid[valid["index"] == index]
        n_baseline = len(idx_df)
        if n_baseline == 0:
            continue

        rows.append({"index": index, "filter": "Baseline", **_metrics(idx_df["forward_15d_return"], n_baseline)})

        for pct in FILTER_PERCENTILES:
            cutoff = np.percentile(idx_df["co1_relative_r2"], 100 - pct)
            filtered = idx_df[idx_df["co1_relative_r2"] >= cutoff]
            rows.append({"index": index, "filter": f"Top {pct}%", "rr2_cutoff": cutoff, **_metrics(filtered["forward_15d_return"], n_baseline)})

    return pd.DataFrame(rows)


def compute_by_year(trade_linkage: pd.DataFrame) -> pd.DataFrame:
    """Annual breakdown for baseline and each threshold."""
    valid = trade_linkage.dropna(subset=["forward_15d_return", "co1_relative_r2"]).copy()
    valid["year"] = valid["entry_date"].dt.year
    rows = []

    for index in ["ALL"] + INDICES:
        if index == "ALL":
            subset = valid
        else:
            subset = valid[valid["index"] == index]

        for year, yr_df in subset.groupby("year"):
            n_baseline = len(yr_df)
            if n_baseline < 10:
                continue

            rows.append({"index": index, "year": int(year), "filter": "Baseline", **_metrics(yr_df["forward_15d_return"], n_baseline)})

            for pct in FILTER_PERCENTILES:
                cutoff = np.percentile(yr_df["co1_relative_r2"], 100 - pct)
                filtered = yr_df[yr_df["co1_relative_r2"] >= cutoff]
                if len(filtered) > 0:
                    rows.append({"index": index, "year": int(year), "filter": f"Top {pct}%", **_metrics(filtered["forward_15d_return"], n_baseline)})

    return pd.DataFrame(rows)


def compute_deviation_summary(rolling_df: pd.DataFrame) -> pd.DataFrame:
    """Per-ticker summary of R² metrics across the full history."""
    if rolling_df.empty:
        return pd.DataFrame()

    summary = (
        rolling_df.groupby(["index", "ticker", "category"])
        .agg(
            calibrated_r2=("calibrated_r2", "first"),
            mean_rolling_r2=("r_squared", "mean"),
            std_rolling_r2=("r_squared", "std"),
            mean_relative_r2=("relative_r2", "mean"),
            std_relative_r2=("relative_r2", "std"),
            pct_r2_below_half=("relative_r2", lambda x: (x < 0.5).mean() * 100),
            n_observations=("r_squared", "count"),
        )
        .reset_index()
    )

    return summary.sort_values(["index", "mean_relative_r2"], ascending=[True, True])


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------
def _apply_dark_style():
    """Set dark background publication-quality style."""
    plt.style.use("dark_background")
    plt.rcParams.update({
        "figure.facecolor": "#1a1a2e",
        "axes.facecolor": "#16213e",
        "axes.edgecolor": "#e0e0e0",
        "axes.labelcolor": "#e0e0e0",
        "text.color": "#e0e0e0",
        "xtick.color": "#e0e0e0",
        "ytick.color": "#e0e0e0",
        "grid.color": "#2a2a4a",
        "grid.alpha": 0.5,
        "font.size": 11,
        "axes.titlesize": 13,
        "axes.labelsize": 11,
    })


def generate_charts(
    overall: pd.DataFrame,
    by_index: pd.DataFrame,
    by_year: pd.DataFrame,
    trade_linkage: pd.DataFrame,
):
    """Generate 4 PNG charts and save to OUTPUT_DIR."""
    _apply_dark_style()
    colors = ["#4cc9f0", "#4895ef", "#4361ee", "#3f37c9", "#7209b7", "#f72585"]

    # --- Chart 1: Overall threshold comparison (bar chart, dual y-axes) ---
    fig, ax1 = plt.subplots(figsize=(10, 6))
    fig.patch.set_facecolor("#1a1a2e")

    labels = overall["filter"].tolist()
    x = np.arange(len(labels))
    mean_ret = overall["mean_return"].values * 100  # to bps-ish %
    win_rate = overall["win_rate"].values

    bars = ax1.bar(x - 0.2, mean_ret, 0.4, color=colors[:len(labels)], alpha=0.85, label="Mean return (%)")
    ax1.set_ylabel("Mean 15d return (%)", color="#4cc9f0")
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, rotation=25, ha="right", fontsize=9)
    ax1.tick_params(axis="y", labelcolor="#4cc9f0")
    ax1.axhline(y=mean_ret[0], color="#4cc9f0", linestyle="--", alpha=0.4, linewidth=0.8)

    ax2 = ax1.twinx()
    ax2.bar(x + 0.2, win_rate, 0.4, color="#f72585", alpha=0.7, label="Win rate (%)")
    ax2.set_ylabel("Win rate (%)", color="#f72585")
    ax2.tick_params(axis="y", labelcolor="#f72585")
    ax2.axhline(y=win_rate[0], color="#f72585", linestyle="--", alpha=0.4, linewidth=0.8)

    ax1.set_title("Relative R² Filter: Threshold Comparison", fontsize=14, fontweight="bold", pad=12)
    ax1.grid(axis="y", alpha=0.3)

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", framealpha=0.3)

    fig.tight_layout()
    fig.savefig(OUTPUT_DIR / "chart_threshold_comparison.png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)

    # --- Chart 2: By index heatmap (mean return improvement) ---
    baseline_by_idx = by_index[by_index["filter"] == "Baseline"].set_index("index")["mean_return"]
    pivot_data = []
    for pct in FILTER_PERCENTILES:
        filt = by_index[by_index["filter"] == f"Top {pct}%"]
        for _, row in filt.iterrows():
            bl = baseline_by_idx.get(row["index"], 0)
            improvement = (row["mean_return"] - bl) * 100  # percentage points
            pivot_data.append({"index": row["index"], "filter": f"Top {pct}%", "improvement": improvement})

    pivot_df = pd.DataFrame(pivot_data)
    if not pivot_df.empty:
        heatmap_data = pivot_df.pivot(index="index", columns="filter", values="improvement")
        heatmap_data = heatmap_data[[f"Top {p}%" for p in FILTER_PERCENTILES]]

        fig, ax = plt.subplots(figsize=(10, 5))
        fig.patch.set_facecolor("#1a1a2e")

        im = ax.imshow(heatmap_data.values, cmap="RdYlGn", aspect="auto")
        ax.set_xticks(np.arange(len(FILTER_PERCENTILES)))
        ax.set_xticklabels([f"Top {p}%" for p in FILTER_PERCENTILES])
        ax.set_yticks(np.arange(len(heatmap_data.index)))
        ax.set_yticklabels(heatmap_data.index)

        for i in range(heatmap_data.shape[0]):
            for j in range(heatmap_data.shape[1]):
                val = heatmap_data.values[i, j]
                text_color = "black" if abs(val) > 0.3 else "#e0e0e0"
                ax.text(j, i, f"{val:+.2f}pp", ha="center", va="center", color=text_color, fontsize=10, fontweight="bold")

        cbar = plt.colorbar(im, ax=ax, shrink=0.8)
        cbar.set_label("Mean return improvement (pp)", color="#e0e0e0")
        cbar.ax.yaxis.set_tick_params(color="#e0e0e0")
        plt.setp(plt.getp(cbar.ax.axes, "yticklabels"), color="#e0e0e0")

        ax.set_title("Mean Return Improvement vs Baseline by Index", fontsize=14, fontweight="bold", pad=12)
        fig.tight_layout()
        fig.savefig(OUTPUT_DIR / "chart_index_heatmap.png", dpi=150, facecolor=fig.get_facecolor())
        plt.close(fig)

    # --- Chart 3: Through time (annual, 5 subplots) ---
    # Find best overall threshold
    non_baseline = overall[overall["filter"] != "Baseline (no filter)"]
    best_filter = "Top 95%"

    by_year_all = by_year[by_year["index"] != "ALL"]
    fig, axes = plt.subplots(2, 3, figsize=(16, 10), sharey=True)
    fig.patch.set_facecolor("#1a1a2e")
    axes = axes.flatten()

    for i, index in enumerate(INDICES):
        ax = axes[i]
        idx_data = by_year_all[by_year_all["index"] == index]

        bl = idx_data[idx_data["filter"] == "Baseline"].sort_values("year")
        ft = idx_data[idx_data["filter"] == best_filter].sort_values("year")

        if not bl.empty:
            ax.plot(bl["year"], bl["mean_return"] * 100, "o-", color="#4cc9f0", label="Baseline", linewidth=2, markersize=5)
        if not ft.empty:
            ax.plot(ft["year"], ft["mean_return"] * 100, "s-", color="#f72585", label=best_filter, linewidth=2, markersize=5)

        ax.axhline(y=0, color="#e0e0e0", linestyle="-", alpha=0.2, linewidth=0.8)
        ax.set_title(index, fontsize=13, fontweight="bold")
        ax.set_xlabel("Year")
        ax.grid(alpha=0.3)
        if i == 0:
            ax.set_ylabel("Mean 15d return (%)")
        ax.legend(fontsize=8, framealpha=0.3)

    # Hide unused subplot
    axes[5].set_visible(False)

    fig.suptitle(f"Annual Mean Return: Baseline vs {best_filter}", fontsize=15, fontweight="bold", y=1.01)
    fig.tight_layout()
    fig.savefig(OUTPUT_DIR / "chart_annual_by_index.png", dpi=150, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)

    # --- Chart 4: Return distribution (KDE, baseline vs best) ---
    valid = trade_linkage.dropna(subset=["forward_15d_return", "co1_relative_r2"])
    cutoff_val = np.percentile(valid["co1_relative_r2"], 100 - int(best_filter.split()[1].rstrip("%")))
    filtered = valid[valid["co1_relative_r2"] >= cutoff_val]

    fig, ax = plt.subplots(figsize=(10, 6))
    fig.patch.set_facecolor("#1a1a2e")

    bins = np.linspace(-0.3, 0.3, 80)
    ax.hist(valid["forward_15d_return"], bins=bins, density=True, alpha=0.4, color="#4cc9f0", label=f"Baseline (n={len(valid):,})")
    ax.hist(filtered["forward_15d_return"], bins=bins, density=True, alpha=0.4, color="#f72585", label=f"{best_filter} (n={len(filtered):,})")

    # KDE overlays
    from scipy.stats import gaussian_kde
    x_kde = np.linspace(-0.3, 0.3, 300)
    for data, color, label in [(valid["forward_15d_return"], "#4cc9f0", None), (filtered["forward_15d_return"], "#f72585", None)]:
        clean = data.dropna()
        if len(clean) > 10:
            kde = gaussian_kde(clean, bw_method=0.15)
            ax.plot(x_kde, kde(x_kde), color=color, linewidth=2)

    ax.axvline(x=0, color="#e0e0e0", linestyle="--", alpha=0.4)
    ax.set_xlabel("15-day forward return")
    ax.set_ylabel("Density")
    ax.set_title(f"Return Distribution: Baseline vs {best_filter}", fontsize=14, fontweight="bold", pad=12)
    ax.legend(fontsize=10, framealpha=0.3)
    ax.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(OUTPUT_DIR / "chart_return_distribution.png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)

    return best_filter


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def run_beta_stability_analysis():
    """
    Full beta stability diagnostic pipeline.
    Returns dict of result DataFrames and saves outputs to data/.
    """
    print("=" * 60)
    print("BETA STABILITY DIAGNOSTIC — Relative R² Focus")
    print("=" * 60)

    # Step 1: Compute rolling metrics for all indices
    print("\n[1/4] Computing rolling R² per ticker...")
    all_rolling = []
    for idx in INDICES:
        print(f"      {idx}...", end=" ", flush=True)
        df = compute_rolling_metrics_for_index(idx)
        print(f"{df['ticker'].nunique()} tickers, {len(df)} observations")
        all_rolling.append(df)

    rolling_df = pd.concat(all_rolling, ignore_index=True)
    print(f"      Total: {rolling_df['ticker'].nunique()} tickers, "
          f"{len(rolling_df)} observations")

    deviation_summary = compute_deviation_summary(rolling_df)
    low_r2 = deviation_summary.nsmallest(10, "mean_relative_r2")
    print("\n      10 tickers with lowest mean relative R²:")
    for _, row in low_r2.iterrows():
        print(f"        {row['index']}/{row['ticker']}: "
              f"mean_rR²={row['mean_relative_r2']:.2f}, "
              f"cal_R²={row['calibrated_r2']:.3f}")

    # Step 2: Link to historical trade universe
    print("\n[2/4] Loading trade triggers and linking to R² metrics...")
    trades_df = load_trade_triggers()
    print(f"      {len(trades_df)} historical trades loaded")

    trade_linkage = link_trades_to_stability(rolling_df, trades_df)
    matched = trade_linkage["co1_relative_r2"].notna().sum()
    print(f"      R² matched: {matched}/{len(trade_linkage)}")

    # Step 3: Outcome analysis
    print("\n[3/4] Computing outcome analysis...")
    overall = compute_overall_summary(trade_linkage)
    by_index_df = compute_by_index(trade_linkage)
    by_year_df = compute_by_year(trade_linkage)

    print("\n      OVERALL SUMMARY:")
    print("      " + "-" * 90)
    print(f"      {'Filter':25s}  {'n':>7s}  {'Retained':>8s}  {'Mean':>8s}  {'Win%':>6s}  {'Sharpe':>7s}")
    print("      " + "-" * 90)
    for _, row in overall.iterrows():
        print(f"      {row['filter']:25s}  {row['n_trades']:7.0f}  "
              f"{row['pct_retained']:7.1f}%  "
              f"{row['mean_return']:+8.4f}  "
              f"{row['win_rate']:5.1f}%  "
              f"{row['sharpe']:7.3f}")

    print("\n      BY INDEX (mean return):")
    print("      " + "-" * 90)
    for index in INDICES:
        idx_rows = by_index_df[by_index_df["index"] == index]
        bl_row = idx_rows[idx_rows["filter"] == "Baseline"]
        bl_mean = bl_row["mean_return"].values[0] if len(bl_row) > 0 else 0
        print(f"      {index}  baseline={bl_mean:+.4f}", end="")
        for pct in FILTER_PERCENTILES:
            filt_row = idx_rows[idx_rows["filter"] == f"Top {pct}%"]
            if len(filt_row) > 0:
                diff = (filt_row["mean_return"].values[0] - bl_mean) * 100
                print(f"  Top{pct}%={diff:+.2f}pp", end="")
        print()

    # Step 4: Charts
    print("\n[4/4] Generating charts...")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    best_filter = generate_charts(overall, by_index_df, by_year_df, trade_linkage)
    print(f"      Best filter by Sharpe: {best_filter}")
    print(f"      Charts saved to {OUTPUT_DIR}/")

    # Save Excel
    print(f"\n{'='*60}")
    print(f"Saving to {OUTPUT_PATH}")

    # Trim trade linkage to relative R² columns only
    linkage_cols = [
        "pair", "index", "tail", "entry_date", "forward_15d_return",
        "cdf_value", "co1_tstat_entry", "cum_15d_alpha",
        "co1_ticker", "co1_r2", "co1_relative_r2",
        "co2_ticker", "co2_r2", "co2_relative_r2",
    ]
    linkage_out = trade_linkage[[c for c in linkage_cols if c in trade_linkage.columns]]

    with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
        overall.to_excel(writer, sheet_name="Overall Summary", index=False)
        by_index_df.to_excel(writer, sheet_name="By Index", index=False)
        by_year_df.to_excel(writer, sheet_name="By Year", index=False)
        linkage_out.to_excel(writer, sheet_name="Trade Linkage", index=False)

    print("Done.")

    return {
        "overall": overall,
        "by_index": by_index_df,
        "by_year": by_year_df,
        "trade_linkage": trade_linkage,
        "deviation_summary": deviation_summary,
        "rolling_df": rolling_df,
    }


# ---------------------------------------------------------------------------
# Jupyter execution
# ---------------------------------------------------------------------------
# To run from Jupyter, paste the following into a cell:
#
#   import sys
#   sys.path.insert(0, '<path-to-diagnostics-directory>')
#   import importlib
#   import beta_stability_analysis as bsa
#   importlib.reload(bsa)
#
#   results = bsa.run_beta_stability_analysis()
#
#   # Access individual result DataFrames:
#   results['overall']         # Overall Summary
#   results['by_index']        # By Index breakdown
#   results['by_year']         # Annual breakdown
#   results['trade_linkage']   # Full trade linkage
#   results['deviation_summary']  # Per-ticker R² summary
#   results['rolling_df']      # Raw rolling metrics
