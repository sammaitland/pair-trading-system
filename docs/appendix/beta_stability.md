# Beta Stability Diagnostic

## Objective

Detect ticker-level beta regime changes that make the calibrated alpha series
unreliable, using **relative R²** as the primary signal.

Complementary to Factor Shock Detection (which operates at the index/factor
level) — beta stability operates at the individual ticker level.

## Hypothesis

When a ticker's rolling R² vs its sub-sector index degrades relative to its
calibrated baseline, the alpha series derived from that relationship becomes
unreliable. Filtering out trades where Co1 has low relative R² should improve
mean returns and win rate.

## Signal: Relative R²

```
relative_r2 = rolling_15d_r2 / calibrated_r2
```

Where `calibrated_r2` comes from `SubSector Beta Summary` (the r_squared
column from the 630-day lookback calibration). A relative R² of 1.0 means
the rolling fit matches calibration; values well below 1.0 indicate the
sub-sector relationship has broken down.

## Methodology

### 1. Rolling R² computation
For each ticker in each index, compute rolling 15-day OLS regression of the
ticker's daily raw return against its sub-sector index return. Extract R².

The 15-day window was chosen over 10-day for better statistical properties
while retaining recency. Daily returns only — intraday data is too noisy for
beta estimation and sub-sector indices have no intraday equivalent.

### 2. Filter threshold sweep
For each of 5 filter levels (keep top 25%, 33%, 50%, 67%, 75% of trades by
Co1 relative R²), compute vs unfiltered baseline:
- Mean 15-day forward return
- Win rate
- Sharpe-like ratio (mean / std)
- Trades retained (% of baseline)

### 3. Breakdowns
- **By index** — each threshold × index combination
- **By year** — annual performance for baseline vs best threshold

### 4. Trade outcome linkage
Join to `trade_triggers_df` from pair cache files (~113K historical backtest
trades). For each trade, record Co1 and Co2 relative R² at entry date and
the subsequent 15-day forward alpha return.

## Data Sources

| Source | Path | What it provides |
|---|---|---|
| Pair cache files | `{VERSION_DIR}/cache/{INDEX}_pair_cache.pkl` | Full historical backtest trade universe |
| SubSector Beta Summary | `{VERSION_DIR}/{INDEX}/{INDEX}_SubSector_Beta_Analysis.xlsx` | Calibrated R², cluster assignments |
| SubSector Indices | Same Excel, sheet `SubSector Indices` | Daily sub-sector index returns |
| Raw Returns Series | Same Excel, sheet `Raw Returns Series` | Daily raw ticker returns |

## Output

### Excel: `data/latest_run.xlsx`
- **Overall Summary** — threshold sweep metrics (overall)
- **By Index** — threshold sweep broken down by index
- **By Year** — annual breakdown for baseline and each threshold
- **Trade Linkage** — each trade with Co1/Co2 relative R² at entry

### Charts: `data/chart_*.png`
1. **Threshold comparison** — bar chart, mean return + win rate dual y-axes
2. **Index heatmap** — mean return improvement by index × threshold
3. **Annual by index** — line chart, baseline vs best threshold (5 subplots)
4. **Return distribution** — overlaid histograms + KDE, baseline vs best

## Conclusions

Two threshold sweeps were run across 108,479 historical backtest trades:
- **Restrictive thresholds**: Top 25%, 33%, 50%, 67%, 75%
- **Lenient thresholds**: Top 80%, 85%, 90%, 95%

### Key findings

1. **Signal is genuine** — monotonic improvement from Q1 (lowest relative R²)
   to Q4 (highest) confirmed across all 108,479 trades. Tickers with higher
   relative R² at entry consistently produce better 15-day forward returns.

2. **VHT shows the strongest and most consistent signal** across all threshold
   levels. VCR shows the weakest signal.

3. **Binary filter is impractical** — Top 25% cuts 75% of trades for only
   ~36 bps improvement. Lenient thresholds (80–95%) deliver only 3–4 bps
   with inconsistent behaviour across indices.

4. **Recommended implementation**: continuous scoring component in
   Pair_Generator evaluation output, not a binary Pre_Filter rule. The signal
   has predictive value but is too weak for a hard cutoff — it should inform
   trade ranking rather than trade exclusion.

5. **Earlier signals discarded**: beta ratio and T-stat instability flags
   were too noisy on a 15-day window. Factor contamination was removed as a
   separate workstream.

## Integration Path

1. ~~Diagnostics: validate signal and derive thresholds~~ — **done**
2. Calibration/Pair_Generator: integrate as scoring weight in evaluation output
3. Implementation/Pre_Filter: not recommended as binary filter — use as
   continuous score only

## Status

**Analysis complete** — deployment approach confirmed. Next step: integrate
relative R² as a scoring weight in Pair_Generator evaluation output.
