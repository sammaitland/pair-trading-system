# Decision: DGS10 Treasury Yield Removal

## What DGS10 Was

DGS10 (the 10-year US Treasury yield) was used in the original two-factor
alpha model. Each stock's alpha was calculated as:

```
alpha = stock_return - (subsector_beta * index_return) - (treasury_beta * dgs10_return)
```

The treasury beta captured interest-rate sensitivity, allowing the alpha
signal to isolate stock-specific returns from both sector movements and
rate-driven movements.

## Why It Was Removed

The V9.2 transition to a single-factor model (sub-sector beta only) made
the treasury factor redundant. The single-factor model was adopted because:

- Treasury betas were unstable across calibration windows
- The two-factor model added complexity without measurable improvement in
  out-of-sample performance
- Sub-sector indices (from dynamic clustering) already captured most of
  the rate sensitivity implicitly through sector composition

## What Was Still Using It

After the V9.2 transition, DGS10 remained in the codebase as a vestigial
artifact:

- **Execution_Workflow** fetched DGS10 at startup and passed it through
  the pipeline
- **Trade_Execution** stored "Treasury at Initiation" in every trade record
- **Portfolio_Management** accepted a `current_dgs10` parameter in
  `update_live_alpha_returns()` and had a legacy code path for V9 trades
  that used the two-factor calculation
- **Fetch_Market_Data** had four functions dedicated to DGS10 fetching
  (yfinance ^TNX, IBKR ZN futures, with fallback chains)
- **Trade_Execution** had a `get_live_dgs10_price()` function with its
  own caching layer
- **Config** had `dgs10_default_yield` and `dgs10_cache_duration` settings

None of these values fed into any calculation in the V9.2+ code path.
The data was fetched, passed around, stored, and ignored.

## What Was Removed

- All DGS10 fetch functions (4 in fetch_market_data, 1 in trade_execution)
- DGS10 caching infrastructure in trade_execution
- `current_dgs10` parameter from `update_live_alpha_returns()`
- `dgs10_price` parameter from trade execution function signatures
- "Treasury at Initiation" field from trade record creation
- Legacy V9 two-factor alpha calculation path
- DGS10 config fields (`dgs10_default_yield`, `dgs10_cache_duration`)
- DGS10 display lines in config summary printing

## Impact

None on V9.2+ trades. The single-factor model path was already the only
active code path. Legacy V9 trades (if any remain in a portfolio during
version transition) will have their alpha calculated using the single-factor
model, which is a minor numerical difference that resolves naturally as
those trades terminate.
