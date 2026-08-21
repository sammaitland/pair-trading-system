# Decision: Breaking the Portfolio_Management / Trade_Execution Circular Dependency

## Problem

`Portfolio_Management` and `Trade_Execution` import each other at module level:

```
Trade_Execution (L33):   import Portfolio_Management as pm
Portfolio_Management (L32-33):  import Trade_Execution
                                from Trade_Execution import calculate_weighted_spread, ...
```

This is a circular import. It works in production only because of Python's
partial-module-loading behaviour and a specific load order — `Portfolio_Management`
is always imported first (by `Execution_Workflow`), so by the time
`Trade_Execution` tries to reference `pm.add_trade_dates()` at runtime, the PM
module is fully loaded.

The workaround has two visible symptoms:

1. **Lazy imports inside PM function bodies** — `connect_ibkr` (L1272),
   `fetch_market_data` (L3821), and `get_account_summary_values` (L4648) are
   imported inside functions with `from Trade_Execution import ...` because
   importing them at module level failed under some code paths.

2. **Duplicate import** — `fetch_market_data` appears both at module level (L33)
   and as a lazy import (L3821), suggesting the lazy import was added after the
   module-level one caused an error in a specific call path.

The dependency is fragile: if any code path imports `Trade_Execution` before
`Portfolio_Management`, the partially-loaded PM module won't have the attributes
TE needs, and the import fails.

## Root cause

`Trade_Execution` writes to the portfolio after executing trades. Specifically,
at the end of both `execute_trades_in_batches()` and
`execute_trades_with_aggregation()`, it calls:

```python
executed_trades = pm.add_trade_dates(executed_trades)
portfolio_df = pm.append_executed_trades(portfolio_df, executed_trades, parameters_df)
```

This is a responsibility leak — trade execution should not be writing to the
portfolio. The orchestrator (`Execution_Workflow`) already manages the portfolio
DataFrame and passes it to TE. TE should return the execution results and let
the orchestrator handle recording.

## Fix

### Principle

Trade_Execution returns execution results. Execution_Workflow records them to the
portfolio via Portfolio_Management. Neither TE nor PM imports the other.

### Step-by-step changes

#### 1. Trade_Execution: remove PM dependency

In `execute_trades_in_batches()` and `execute_trades_with_aggregation()`:

**Before** (at the end of each function):
```python
successful_pairs = successful['Pair'].tolist()

if successful_pairs:
    executed_trades = evaluated_trades_df[
        evaluated_trades_df['Pair'].isin(successful_pairs)
    ].copy()
    executed_trades = pm.add_trade_dates(executed_trades)
    portfolio_df = pm.append_executed_trades(
        portfolio_df, executed_trades, parameters_df
    )
    logger.info(f"\n  Added {len(executed_trades)} trades to portfolio")

return portfolio_df, execution_summary_df
```

**After**:
```python
return execution_summary_df
```

The function no longer accepts `portfolio_df` or `parameters_df` as arguments
(remove from signature), and no longer returns a modified portfolio. It returns
only the execution summary.

Remove the module-level import:
```python
# DELETE: import Portfolio_Management as pm
```

#### 2. Execution_Workflow: take over portfolio recording

**Before** (around L1014-1023):
```python
portfolio_df, execution_summary = await execute_trades_with_aggregation(
    evaluated_trades_df, portfolio_df, parameters_df,
    ib, simple_prices, index_price, dgs10_price
)
```

**After**:
```python
execution_summary = await execute_trades_with_aggregation(
    evaluated_trades_df,
    ib, simple_prices, index_price, dgs10_price
)

# Record successful trades to portfolio
successful = execution_summary[
    execution_summary['Status'].isin(['Executed', 'Partial'])
]
if not successful.empty:
    successful_pairs = successful['Pair'].tolist()
    executed_trades = evaluated_trades_df[
        evaluated_trades_df['Pair'].isin(successful_pairs)
    ].copy()
    executed_trades = portfolio_management.add_trade_dates(executed_trades)
    portfolio_df = portfolio_management.append_executed_trades(
        portfolio_df, executed_trades, parameters_df
    )
    logger.info(f"  Added {len(executed_trades)} trades to portfolio")
```

Apply the same pattern for the `execute_trades_in_batches` call path.

#### 3. Portfolio_Management: remove TE dependency at module level

**Before** (L32-33):
```python
import Trade_Execution
from Trade_Execution import calculate_weighted_spread, fetch_market_data, get_account_summary_values
```

**After** — remove both lines. Replace with lazy imports only where needed:

- `calculate_weighted_spread` — used in `evaluate_trades()` for spread
  calculation during trade evaluation. Move to lazy import inside that function.
- `fetch_market_data` — already has a lazy import at L3821. Remove the
  module-level one; keep only the lazy import.
- `get_account_summary_values` — already has a lazy import at L4648. Remove
  the module-level one.
- `connect_ibkr` — already lazy at L1272. No change needed.

The lazy imports in PM are acceptable because they are inside function bodies
that are only called at runtime, well after both modules are fully loaded. They
exist for a different reason than the circular dependency — they avoid importing
IBKR-dependent code in contexts where IBKR isn't available.

#### 4. Verify no remaining cross-imports

After the changes:
- `Trade_Execution` imports: Config, Constraints, Config_Helper, Tool_Box, Fetch_Market_Data — no PM
- `Portfolio_Management` imports: Config, Config_Helper, Constraints, Tool_Box — no TE at module level; lazy TE imports inside 4 function bodies
- `Execution_Workflow` imports both and orchestrates between them

## Testing

1. Import `Trade_Execution` first (before `Portfolio_Management`) — should succeed
2. Import `Portfolio_Management` first — should succeed
3. Import both in either order — should succeed
4. Run the full workflow — execution results should be identical
5. Run `evaluate_trades()` without IBKR connection — lazy imports should handle gracefully

## Risk

Low. The change moves 8 lines of portfolio-update logic from Trade_Execution to
Execution_Workflow, where the same data is already available. No trade logic,
order placement, or portfolio evaluation logic changes. The only behavioural
difference is *where* the portfolio DataFrame gets updated — the *what* is identical.
