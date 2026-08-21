# Pair Cache Schema

The pair generator produces an Excel file containing the calibrated pair
universe. Downstream modules (Pre_Filter, LAM) consume this as their
candidate set.

## File format

`{VERSION}_Parameters.xlsx` with the following sheets:

### Sheet: Pairs

| Column | Type | Description |
|--------|------|-------------|
| Tag | str | Unique pair identifier (e.g. `VGT_AAPL_MSFT_L`) |
| Pair | str | Human-readable pair name (e.g. `AAPL/MSFT`) |
| Co1 | str | First ticker |
| Co2 | str | Second ticker |
| Index | str | Sector ETF (VGT, VFH, VIS, VHT, VCR) |
| Tail | str | `L` (lower) or `U` (upper) |
| EMA_Multiplier | float | Index-specific EMA multiplier for 2-day deviation |
| CDF_Threshold | float | Index-specific CDF threshold for 15-day alpha variance |

### Sheet: Tickers

| Column | Type | Description |
|--------|------|-------------|
| Ticker | str | Stock ticker symbol |
| Index | str | Assigned sector ETF |
| SubSector_Beta | float | Beta to sub-sector index |
| Treasury_Beta | float | Beta to DGS10 (always 0 in single-factor model) |
| VO_Beta | float | Beta to broad market (Vanguard Mid-Cap) |

### Sheet: 15Day_Cumulative_Stats

| Column | Type | Description |
|--------|------|-------------|
| Tag | str | Pair identifier |
| Mean | float | Mean 15-day cumulative alpha |
| StdDev | float | Std dev of 15-day cumulative alpha |
| Skew | float | Skewness |
| Kurtosis | float | Kurtosis |

### Sheet: Sum_Deviation_Params

| Column | Type | Description |
|--------|------|-------------|
| Parameter | str | Parameter name |
| Value | float | Parameter value |

Key parameter: `Sum Deviation StdDev` — the global standard deviation used
for CDF conversion of sum deviation values.
