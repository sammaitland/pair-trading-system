# CHANGE_MANUAL.md

**Primary deliverable.** Per-file record of every change made during the
rebuild from private source to public reference repository.

---

## Part 1 — Summary

### What was included, excluded, and restructured

| Category | Count | Notes |
|----------|-------|-------|
| Included in full | 16 modules | Import fixes, docstring rewrites, parameter externalisation |
| Included as interface + ref impl | 5 modules | Beta_Estimator, Pair_Generator, Optimizer, Signal_Scorer, Factor_Shock_Detection |
| Excluded entirely | 4 modules | Scheduler_Template, LAM_Call, Daily_Updater_Call, beta_stability/ diagnostics |
| New files created | ~20 | Interfaces, ref impls, config loader, fixtures, test suite, CI workflow |

### Total file and line counts

| Metric | Before (source) | After (output) |
|--------|-----------------|----------------|
| Python files | 29 | 45 (includes __init__.py files, fixture generator, test suite) |
| Total Python lines | 43,576 | ~30,000 |
| Total files | 32 | ~60 (includes config, fixtures, tests, CI, appendix) |
| Markdown docs | 3 | 4 (README, ARCHITECTURE, CHANGE_MANUAL, ROADMAP) |

### Change categories

| Category | Count |
|----------|-------|
| sys.path removal | 22 locations across 19 files |
| API key removal | 4 instances across 3 files |
| Account ID removal | 2 instances |
| Machine hostname removal | 2 instances |
| Mac TTS notification removal | 2 instances |
| Absolute path elimination | 7+ instances |
| Parameter externalisation | ~165 numeric literals → config.yaml |
| Dead code removal (treasury factor) | 17 instances across 3 files |
| Dead code removal (other) | 24 instances |
| Docstring rewrite | All included modules |
| Version marker removal | All included modules |
| Import restructuring | All included modules |
| Layer reassignment | 4 modules (Portfolio_Management, Trade_Execution, Stop_Loss_Protection, Reconciliation: Helper → execution) |
| Hardcoded exclusion lists → config | 2 (crypto tickers, mREIT tickers) |
| Module split | 1 (Tool_Box → calculations.py + scoring_constants.py + signal interfaces) |
| Bug fix | 1 (Delisting_Handler partial-failure — isolated commit, see Part 2) |
| New interface + ref impl | 5 (signal scorer, factor shock detector, beta estimator, pair generator, optimizer) |
| Pair cache schema + fixture | 1 (synthetic_pairs.py + pair_cache_schema.md) |

---

## Part 2 — Per-File Record

### src/shared/config.py
**Source:** Executive/Config.py
**Status:** included (structure only — all values externalised)
**Line count:** 1,161 → 708

**Changes:**
| # | Type | Location | Change | Reason |
|---|------|----------|--------|--------|
| 1 | rewrite | entire file | Rewrote as YAML-driven config loader | Externalise all values per §5.1 |
| 2 | secret | L998 | Removed ALPHAVANTAGE_API_KEY literal | API key |
| 3 | secret | L999 | Removed commented API key | API key in comment |
| 4 | secret | L375, L380 | Removed ACCOUNT_ID literals | Account identifiers |
| 5 | secret | L38, L42 | Removed machine hostnames | Personal identifiers |
| 6 | path | L35-57 | Removed machine detection block | Machine-specific logic |
| 7 | path | L99-117 | Removed hardcoded ~/Desktop paths | Machine-specific paths |
| 8 | param | All | All ~120 strategy parameters → config.yaml (no values) | §5.1 |
| 9 | param | All | All ~35 operational parameters → config.yaml (with defaults) | §5.4 |
| 10 | dead | L286 | Removed TREASURY_TICKER (dead config) | Treasury factor retired |
| 11 | dead | L1010-1011 | Removed DGS10_DEFAULT_YIELD, DGS10_CACHE_DURATION (dead) | Note: kept as operational params in config.yaml; the treasury *beta factor* is dead but DGS10 fetch for display is live |
| 12 | dead | L643-654 | Removed VGT_MEGACAP_ADJUSTMENT deprecated dict | Replaced by MEGACAP_ADJUSTMENT_CONFIG |
| 13 | dead | L1049 | Removed empty ALPHA_HISTORY dict | Never populated |
| 14 | version | L71-77 | Removed internal version strings and dates | §4.4 |
| 15 | docstring | L1-14 | Rewrote module docstring | §4.3 |
| 16 | compat | L1132-1162 | Removed backward compatibility imports block | Clean package structure |

**Unchanged:** The version-awareness architecture (get_version_dir, parse_version_from_model, SUPPORTED_VERSIONS) is preserved — this is a real architectural feature, not internal version noise.

**Flags for operator:**
- DGS10 config values: kept as operational params since Execution_Workflow calls get_today_dgs10_close() for display. Confirm this is correct.
- IGV exposure section (L606-615): retired comment block removed entirely. Confirm nothing references it.

---

### src/shared/config_helper.py
**Source:** Helper/Config_Helper.py
**Status:** included in full
**Line count:** 727 → 657

**Changes:**
| # | Type | Location | Change | Reason |
|---|------|----------|--------|--------|
| 1 | import | L33 | `import Config` → `from src.shared import config` | Package imports |
| 2 | path | All | Config.XXX → config.xxx() function calls | Externalised config |
| 3 | docstring | L1-24 | Rewrote module docstring | §4.3 |
| 4 | status | docstring | Added STATUS line | §4.6 |
| 5 | compat | validate_config() | Removed checks for old attribute names (MAX_BETA, MAX_LEVERAGE, MIN_SHARE_QUANTITY) that don't exist in new config | Config restructuring |
| 6 | compat | print_filter_config_comparison() | Removed references to CDF/2-day threshold constants that were in-code parameters | Config restructuring |

**Unchanged:** All IB connection logic, logging setup, validation framework.

---

### src/shared/constraints.py
**Source:** Helper/Constraints.py
**Status:** included in full
**Line count:** 423 → 425

**Changes:**
| # | Type | Location | Change | Reason |
|---|------|----------|--------|--------|
| 1 | import | L16 | `import Config` → `from src.shared import config` | Package imports |
| 2 | path | All | Config.XXX → config.xxx() calls | Externalised config |
| 3 | docstring | L1-14 | Rewrote module docstring | §4.3 |
| 4 | status | docstring | Added STATUS line | §4.6 |

**Unchanged:** All constraint logic, ticker concentration checks, leverage checks, IGV exposure calculation.

---

### src/shared/fetch_market_data.py
**Source:** Helper/Fetch_Market_Data_V9.py
**Status:** included in full
**Line count:** 1,248 → 908

**Changes:**
| # | Type | Location | Change | Reason |
|---|------|----------|--------|--------|
| 1 | import | various | `import Config` → `from src.shared import config` | Package imports |
| 2 | name | filename | Removed V9 from filename | §4.4 |
| 3 | path | various | Config.XXX → config.xxx() calls | Externalised config |
| 4 | docstring | top | Rewrote module docstring | §4.3 |
| 5 | status | docstring | Added STATUS line | §4.6 |

**Unchanged:** All market data fetching, caching, DGS10 yield retrieval logic.

---

### src/shared/calculations.py
**Source:** Helper/Tool_Box.py (partial — see §6)
**Status:** included (generic calculations only — signal internals excluded)
**Line count:** 5,604 → ~2,990

**Changes:**
| # | Type | Location | Change | Reason |
|---|------|----------|--------|--------|
| 1 | split | entire file | Extracted generic calculations; signal internals excluded | §6 |
| 2 | dead | ~L1018-1127 | Removed _treasury_betas, get_treasury_beta(), all treasury dead code | Treasury factor retired |
| 3 | dead | ~L1170-1177 | Removed get_vgt_beta() deprecated wrapper | Dead backward compat |
| 4 | dead | ~L3285-3356 | Removed check_15day_alpha_variance() (V9 legacy with treasury) | Replaced by _v92 version |
| 5 | dead | ~L3437-3486 | Removed check_2day_deviation() (V9 legacy with treasury) | Replaced by _v92 version |
| 6 | dead | ~L3881-3999+ | Removed apply_primary_filters_with_leniency() (V9 legacy) | Replaced by _v92 version |
| 7 | dead | ~L2897-2930 | Removed calculate_pair_alphas() (V9 legacy) | Replaced by _v92 version |
| 8 | dead | ~L2993-3013 | Removed calculate_15day_alpha_sum() (V9 legacy) | Replaced by _v92 version |
| 9 | dead | ~L3045-3064 | Removed calculate_2day_alpha_sum() (V9 legacy) | Replaced by _v92 version |
| 10 | path | L34 | Removed V92_OUTPUT_DIR hardcoded path | Machine-specific path with username |
| 11 | import | L23-26 | Changed to package-relative imports | Package imports |
| 12 | signal | various | Removed composite scoring, retention filtering, band points | Signal internals excluded per §6 |
| 13 | docstring | top | Rewrote module docstring | §4.3 |
| 14 | status | docstring | Added STATUS line | §4.6 |

**Unchanged:** SubsectorIndexManager, AlphaCache, BetaDataManager (minus treasury dead code), all V9.2 filter functions, data alignment, alpha calculations, trending filter, spread check, earnings check.

**Flags for operator:**
- Scoring function internals (calculate_composite_score, apply_retention_filter_by_tail, get_band_points) are excluded and replaced by the signal interface in src/signals/. Confirm no live code path depends on these directly rather than through the interface.

---

### src/shared/scoring_constants.py
**Source:** NEW (derived from Config.py and Tool_Box.py)
**Status:** included in full

This module is the single-source-of-truth bridge between calibration and live pipelines. It publishes the *shape* and *semantics* of PERCENTILE_BANDS, STABILITY_WEIGHTS, SECONDARY_FILTERS, and STRATEGY_CONFIG — but all *values* come from configuration.

---

### src/execution/delisting_handler.py
**Source:** Helper/Delisting_Handler.py
**Status:** included in full
**Line count:** 992 → 1,022

**Changes:**
| # | Type | Location | Change | Reason |
|---|------|----------|--------|--------|
| 1 | import | L30-32 | Removed sys.path manipulation | §4.2 |
| 2 | import | L30, L34 | Changed to package-relative imports | Package imports |
| 3 | path | various | Config.XXX → config.xxx() calls | Externalised config |
| 4 | docstring | L1-17 | Rewrote module docstring | §4.3 |
| 5 | status | docstring | Added STATUS line | §4.6 |
| 6 | **BUG FIX** | handle_delisting() Steps 5-6 | **REQUIRES OPERATOR REVIEW** — see below | §7 |

**Bug fix detail (§7):**

BEFORE: Steps 5 and 6 of handle_delisting() unconditionally recorded ALL affected trades to completed history and removed ALL from portfolio, regardless of whether individual counterpart closes succeeded or failed. On partial failure, this created untracked, unhedged positions.

AFTER: Record and remove per trade, conditional on that trade's counterpart close succeeding. Failed closes stay in portfolio, retain tracking, and are reported for retry. The operation is idempotent — re-running after a partial failure resolves only outstanding trades.

**Also flagged (not fixed):** The completed-trade history is written by full-file overwrite (`combined.to_excel(...)`) with no backup and no atomic rename, while the portfolio file save is sheet-aware via openpyxl Workbook. The asymmetry looks unintentional.

---

### src/execution/execution_workflow.py
**Source:** Executive/Execution_Workflow.py
**Status:** included in full
**Line count:** 1,295 → 1,277

**Changes:**
| # | Type | Location | Change | Reason |
|---|------|----------|--------|--------|
| 1 | import | L69-70, L84-85 | Removed sys.path manipulation | §4.2 |
| 2 | import | various | Changed to package-relative imports | Package imports |
| 3 | path | various | Config.XXX → config.xxx() calls | Externalised config |
| 4 | docstring | top | Rewrote module docstring | §4.3 |
| 5 | status | docstring | Added STATUS line | §4.6 |
| 6 | version | various | Removed internal version strings | §4.4 |

**Unchanged:** All workflow logic, stage sequencing, error handling.

---

### src/execution/trade_execution.py
**Source:** Helper/Trade_Execution.py
**Status:** included in full
**Line count:** 2,848 → 2,733

**Changes:**
| # | Type | Location | Change | Reason |
|---|------|----------|--------|--------|
| 1 | import | various | Changed to package-relative imports | Package imports |
| 2 | path | various | Config.XXX → config.xxx() calls | Externalised config |
| 3 | docstring | top | Rewrote module docstring | §4.3 |
| 4 | status | docstring | Added STATUS line | §4.6 |

**Unchanged:** All order placement, execution, aggregation logic.

---

### src/execution/portfolio_management.py
**Source:** Helper/Portfolio_Management.py
**Status:** included (constraint enforcement and greedy selection in full; position sizing behind interface)
**Line count:** 5,559 → 5,519

**Changes:**
| # | Type | Location | Change | Reason |
|---|------|----------|--------|--------|
| 1 | import | various | Changed to package-relative imports | Package imports |
| 2 | path | various | Config.XXX → config.xxx() calls | Externalised config |
| 3 | docstring | top | Rewrote module docstring | §4.3 |
| 4 | status | docstring | Added STATUS line | §4.6 |

**Unchanged:** All portfolio loading, evaluation, constraint enforcement, greedy selection logic.

---

### src/execution/reconciliation.py
**Source:** Helper/Reconciliation.py
**Status:** included in full
**Line count:** 495 → 493

**Changes:**
| # | Type | Location | Change | Reason |
|---|------|----------|--------|--------|
| 1 | import | various | Changed to package-relative imports | Package imports |
| 2 | path | various | Config.XXX → config.xxx() calls | Externalised config |
| 3 | docstring | top | Rewrote module docstring | §4.3 |
| 4 | status | docstring | Added STATUS line | §4.6 |

**Unchanged:** All reconciliation logic, position inversion detection.

---

### src/execution/stop_loss_protection.py
**Source:** Helper/Stop_Loss_Protection.py
**Status:** included in full
**Line count:** 712 → 707

**Changes:**
| # | Type | Location | Change | Reason |
|---|------|----------|--------|--------|
| 1 | import | L25-26 | Removed sys.path manipulation | §4.2 |
| 2 | import | various | Changed to package-relative imports | Package imports |
| 3 | path | various | Config.XXX → config.xxx() calls | Externalised config |
| 4 | docstring | top | Rewrote module docstring | §4.3 |
| 5 | status | docstring | Added STATUS line | §4.6 |

**Unchanged:** All stop loss logic, orphan detection, short squeeze protection.

---

### src/execution/daily_data_capture.py
**Source:** Helper/Daily_Data_Capture.py
**Status:** included in full
**Line count:** 2,428 → 2,405

**Changes:**
| # | Type | Location | Change | Reason |
|---|------|----------|--------|--------|
| 1 | import | L57-58 | Removed sys.path manipulation | §4.2 |
| 2 | import | various | Changed to package-relative imports | Package imports |
| 3 | path | various | Config.XXX → config.xxx() calls | Externalised config |
| 4 | docstring | top | Rewrote module docstring | §4.3 |
| 5 | status | docstring | Added STATUS line | §4.6 |

**Unchanged:** All data capture logic, earnings calendar, analyst archive.

---

### src/implementation/pre_filter.py
**Source:** Implementation/Pre_Filter.py
**Status:** included in full
**Line count:** 1,127 → 1,057

**Changes:**
| # | Type | Location | Change | Reason |
|---|------|----------|--------|--------|
| 1 | import | L22 | Removed sys.path manipulation | §4.2 |
| 2 | import | L27-48 | Changed to package-relative imports | Package imports |
| 3 | import | L49 | Factor_Shock_Detection → reference_factor_shock | §3 — interface + no-op |
| 4 | path | L55-60 | Config.XXX → config.xxx() calls | Externalised config |
| 5 | exclusion | L76-122 | CRYPTO_TICKERS, MREIT_TICKERS → config.crypto_tickers(), config.mreit_tickers() | §5.3 |
| 6 | param | L898 | max_spread=0.004 → config reference | Inline spread threshold |
| 7 | platform | L1123 | Removed os.system('say ...') | Mac TTS notification |
| 8 | debug | L516-523 | Removed DEBUG print statements | Internal debug output |
| 9 | version | various | Removed _print_version_banner() and internal version strings | §4.4 |
| 10 | docstring | top | Rewrote module docstring | §4.3 |
| 11 | status | docstring | Added STATUS line | §4.6 |

**Unchanged:** All filter logic, pair processing, sum deviation calculation, factor shock suppression (now via interface).

---

### src/implementation/lam.py
**Source:** Implementation/LAM.py
**Status:** included (pipeline stage and contracts in full; scoring internals from shared toolbox)
**Line count:** 2,124 → 2,085

**Changes:**
| # | Type | Location | Change | Reason |
|---|------|----------|--------|--------|
| 1 | import | various | Changed to package-relative imports | Package imports |
| 2 | path | various | Config.XXX → config.xxx() calls | Externalised config |
| 3 | platform | L2121 | Removed os.system('say ...') | Mac TTS notification |
| 4 | docstring | top | Rewrote module docstring | §4.3 |
| 5 | status | docstring | Added STATUS line | §4.6 |

**Unchanged:** Pipeline stage logic, filter application, pair evaluation.

---

### src/calibration/universe_determination.py
**Source:** Calibration/Universe_Determination.py
**Status:** included in full
**Line count:** 1,131 → 1,126

**Changes:**
| # | Type | Location | Change | Reason |
|---|------|----------|--------|--------|
| 1 | import | L18 | Removed sys.path manipulation | §4.2 |
| 2 | secret | L25 | Removed API_KEY literal | API key |
| 3 | import | various | Changed to package-relative imports | Package imports |
| 4 | exclusion | L47-68 | CRYPTO_TICKERS, MREIT_EXCLUSION → config references | §5.3 |
| 5 | docstring | top | Rewrote module docstring | §4.3 |
| 6 | status | docstring | Added STATUS line | §4.6 |

**Unchanged:** All universe determination logic, ETF holdings parsing, megacap detection, category classification.

---

### Calibration modules (remaining)

All calibration modules received the same standard transformations:
- sys.path removal
- Import restructuring to package-relative
- Config references updated
- Module docstring rewritten
- STATUS line added
- API keys removed where present

Modules: historical_earnings_fetch.py, iv_generation.py, intraday_data_fetch.py, metrics_calculator.py, parameters_extraction.py, percentile_saver.py

---

### Signal interfaces and reference implementations

All NEW files:
- `src/signals/scoring_interface.py` — abstract signal scorer interface
- `src/signals/reference_scorer.py` — naive z-score scorer (reference only)
- `src/signals/factor_shock_interface.py` — abstract factor shock detector interface
- `src/signals/reference_factor_shock.py` — no-op detector (reference only)
- `src/calibration/beta_estimator_interface.py` — abstract beta estimator interface
- `src/calibration/reference_beta_estimator.py` — simple OLS estimator (reference only)
- `src/calibration/pair_generator_interface.py` — abstract pair generator interface
- `src/calibration/reference_pair_generator.py` — synthetic pair generator (reference only)
- `src/calibration/optimizer_interface.py` — abstract optimizer interface
- `src/calibration/reference_optimizer.py` — pass-through optimizer consuming shared scoring_constants and constraints (reference only)

Each reference implementation is marked `STATUS: reference implementation — not deployed` and is deliberately simplistic. The reference optimizer explicitly imports `scoring_constants` and `constraints` from the shared layer to demonstrate the calibration/live parity invariant.

---

## Part 3 — Files Excluded

| Source File | Reason | Replacement | ROADMAP Reference |
|-------------|--------|-------------|-------------------|
| Calibration/Pair_Generator.py | Pair selection filters are proprietary | Interface (pair_generator_interface.py) + reference impl (reference_pair_generator.py) + synthetic fixture (fixtures/synthetic_pairs.py) | ROADMAP.md — Pair_Generator |
| Helper/Factor_Shock_Detection.py | Factor shock model is proprietary | Interface (factor_shock_interface.py) + no-op ref impl (reference_factor_shock.py) | ROADMAP.md — Factor_Shock_Detection |
| Helper/Tool_Box.py (signal internals) | Scoring internals are proprietary | Signal interface (scoring_interface.py) + naive ref impl (reference_scorer.py) | ROADMAP.md — Tool_Box |
| Executive/Scheduler_Template.py | Incomplete — all code commented out | None | ROADMAP.md — Scheduler_Template |
| Executive/LAM_Call.py | Notebook entry point, superseded | None | ROADMAP.md — LAM_Call |
| Executive/Daily_Updater_Call.py | Notebook entry point, superseded | None | ROADMAP.md — Daily_Updater_Call |
| Diagnostics/beta_stability/ | Analysis complete, pending integration | Appendix in docs/appendix/ | ROADMAP.md — beta_stability |

---

## Part 4 — Values Extracted

Complete table of every value removed from source code and its new location in `config.example.yaml`:

| Source File | Source Line | Value | New Config Field | Classification |
|-------------|-----------|-------|-----------------|----------------|
| Config.py | 518 | MAX_PORTFOLIO_BETA | risk.max_portfolio_beta | strategy |
| Config.py | 519 | MIN_PORTFOLIO_BETA | risk.min_portfolio_beta | strategy |
| Config.py | 520 | TARGET_PORTFOLIO_BETA | risk.target_portfolio_beta | strategy |
| Config.py | 524 | BASE_TRADE_SIZE | sizing.base_trade_size | strategy |
| Config.py | 525 | MAX_LONG_TICKER_CONCENTRATION | risk.max_long_ticker_concentration | strategy |
| Config.py | 526 | MAX_SHORT_TICKER_CONCENTRATION | risk.max_short_ticker_concentration | strategy |
| Config.py | 694 | MAX_ACCOUNT_LEVERAGE | risk.max_account_leverage | strategy |
| Config.py | 699 | MARGIN_SAFETY_BUFFER | risk.margin_safety_buffer | strategy |
| Config.py | 703 | EMERGENCY_LEVERAGE_THRESHOLD | risk.emergency_leverage_threshold | strategy |
| Config.py | 302 | cdf_adjustment | prefilter.cdf_adjustment | strategy |
| Config.py | 303 | two_day_reduction | prefilter.two_day_reduction | strategy |
| Config.py | 304 | sum_dev_neutral_zone | prefilter.sum_dev_neutral_zone | strategy |
| Config.py | 317 | PREFILTER_MAX_SPREAD_BPS | spreads.prefilter_max_spread_bps | strategy |
| Config.py | 322 | MAX_SPREAD_BPS | spreads.max_spread_bps | strategy |
| Config.py | 331-333 | PRIORITY_WEIGHTS | priority_weights.* | strategy |
| Config.py | 827-887 | STRATEGY_CONFIG | strategy_config.* | strategy |
| Config.py | 896-917 | INDEX_BIASES | index_biases.* | strategy |
| Config.py | 924-933 | SECONDARY_FILTER_CONFIG | secondary_signals.* | strategy |
| Config.py | 936-942 | STABILITY_WEIGHTS | secondary_signals.stability_weights | strategy |
| Config.py | 958-982 | EARLY_EXIT_CONFIG | early_exit.* | strategy |
| Config.py | 772 | MAX_HOLDING_DAYS | exits.max_holding_days | strategy |
| Config.py | 682 | STOP_LOSS_ALPHA_THRESHOLD | stop_loss.alpha_threshold | strategy |
| Config.py | 730-736 | TREND thresholds | trending_filter.* | strategy |
| Config.py | 588-596 | FACTOR_EXPOSURE_LIMITS | factor_exposure.* | strategy |
| Config.py | 998 | ALPHAVANTAGE_API_KEY | api_keys.alphavantage | secret |
| Config.py | 375, 380 | ACCOUNT_ID | ibkr.account_id | secret |
| Config.py | 387 | MAX_RETRIES | ibkr.max_retries | operational |
| Config.py | 388 | ORDER_TIMEOUT | ibkr.order_timeout_seconds | operational |
| Config.py | 445-456 | Limit order settings | limit_orders.* | operational |
| Config.py | 796-803 | Exit limit settings | exit_limits.* | operational |
| Config.py | 1006-1015 | Market data settings | market_data.* | operational |
| Config.py | 1072 | RECONCILIATION_TOLERANCE_USD | reconciliation.tolerance_usd | operational |
| Pre_Filter.py | 76-122 | CRYPTO_TICKERS, MREIT_TICKERS | exclusion_lists.* | structural |
| Tool_Box.py | 34 | V92_OUTPUT_DIR | (removed — use config.get_version_dir()) | path |
| Universe_Determination.py | 25 | API_KEY | api_keys.alphavantage | secret |
| IV_Generation.py | 99 | api_key | api_keys.alphavantage | secret |

Note: This table covers the primary extractions. The full set of ~165 parameters flagged in the audit is covered by the comprehensive config.example.yaml schema — every field there corresponds to a value that was previously hardcoded.

---

## Part 5 — DECISIONS REQUIRED

All decisions resolved.

| # | Question | Decision | Commit |
|---|----------|----------|--------|
| 1 | Include beta_stability/ diagnostics? | **Included as appendix** in docs/appendix/, paths sanitised | `b96d4c4` |
| 2 | DGS10 config values — still used? | **Removed entirely**. Vestigial artifact of retired treasury factor. See docs/decisions/dgs10_removal.md | `b96d4c4` |
| 3 | Operator-written module docs? | **Provided and included** — pre_filter.md and delisting_handler.md replace generated stubs. Sanitisation notes sections removed. | `6b12315` |
| 4 | Legacy backward-compat aliases? | **Removed**. All call sites updated to canonical function names. | `b96d4c4` |
| 5 | Circular dependency PM ↔ TE? | **Refactored**. TE no longer imports PM. Portfolio recording moved to EW. See docs/decisions/circular_dependency_fix.md | `5024fe0` |

---

## Part 6 — Discrepancies Found

| # | File | Line | Discrepancy |
|---|------|------|-------------|
| 1 | Config.py L1 | docstring says "config.py" (lowercase) but filename is Config.py (Title_Case) | Naming inconsistency — resolved by snake_case rename |
| 2 | Config.py L322 | Comment says "25 Basis points" but value is 22 | Comment does not match value |
| 3 | Config.py L445 | Comment says "$0.02 buffer" but value is 0.01 | Comment does not match value |
| 4 | Config.py L898-903 vs L896 | INDEX_BIASES comments describe % penalties/boosts that don't match the numeric values | Comments appear to be from earlier tuning |
| 5 | Config.py L552-553 | Comments say "40%" and "35%" but values are 0.20 and 0.18 (20% and 18%) | Comments don't match values |
| 6 | Config.py L563 | Comment says "40% of total dollar beta" but value is 0.22 (22%) | Comment doesn't match value |
| 7 | Delisting_Handler.py list_potential_delistings() L851 | References Config.TWS_PORT and Config.CLIENT_ID which don't exist in Config | Dead references to removed config attributes |
| 8 | Config_Helper.py validate_config() L454-468 | References Config.MAX_BETA, Config.MAX_LEVERAGE, Config.MIN_SHARE_QUANTITY which don't exist | Dead references to old config structure |
| 9 | Config_Helper.py print_config_summary() L632 | References Config.MAX_LEVERAGE which doesn't exist | Dead reference |
| 10 | Source docstrings describe V9.4C | But Config.ACTIVE_VERSION is V9.3 | Version mismatch — V9.4 may be in-progress |
| 11 | Scheduler_Template.py L17-18 | sys.path references V9.4C but rest of codebase is V9.3 | Version mismatch in inactive template |

---

## Part 7 — Verification Results

### Secret scan (post-sanitisation)

Searched for: API keys, account IDs, machine hostnames, absolute paths with
usernames, sys.path manipulation, os.system('say') calls.

**Result: CLEAN.** No hardcoded credentials, account identifiers, personal
machine names, or absolute user paths found in `src/`. Two path references
remain in `docs/appendix/beta_stability.md` using `{VERSION_DIR}` placeholder
(sanitised from original `~/Desktop/V9/V9.3/`).

### sys.path manipulation
**Result: CLEAN.** Zero occurrences in `src/`.

### Import structure
Missing config accessor functions identified and added. Dead getter functions
(`get_prefilter_config`, `get_lam_config`) that referenced non-existent old
config attributes removed. Config_helper function references (connect_ib_async,
disconnect_ib, setup_logging, get_client_id) corrected from `config.*` to
`ch.*` across 4 files.

### Pipeline stage execution
Not tested — requires IBKR connection and real market data. Synthetic fixture
generator provided at `fixtures/synthetic_pairs.py`.

### gitleaks scan
Not run — gitleaks not available in this environment. Manual scan performed
(see secret scan above). Recommend running `gitleaks detect` before pushing.

---

## Part 8 — Naming Mapping

**Flagged decision — implement last per §4.5.**

| Old Name (Title_Case) | New Name (snake_case) | Layer |
|-----------------------|----------------------|-------|
| Config.py | config.py | shared |
| Config_Helper.py | config_helper.py | shared |
| Constraints.py | constraints.py | shared |
| Fetch_Market_Data_V9.py | fetch_market_data.py | shared |
| Tool_Box.py | calculations.py (split) | shared |
| — | scoring_constants.py (new) | shared |
| Pre_Filter.py | pre_filter.py | implementation |
| LAM.py | lam.py | implementation |
| Execution_Workflow.py | execution_workflow.py | execution |
| Trade_Execution.py | trade_execution.py | execution |
| Portfolio_Management.py | portfolio_management.py | execution |
| Reconciliation.py | reconciliation.py | execution |
| Stop_Loss_Protection.py | stop_loss_protection.py | execution |
| Daily_Data_Capture.py | daily_data_capture.py | execution |
| Delisting_Handler.py | delisting_handler.py | execution |
| Universe_Determination.py | universe_determination.py | calibration |
| Beta_Estimator.py | beta_estimator_interface.py + reference_beta_estimator.py | calibration |
| Historical_Earnings_Fetch.py | historical_earnings_fetch.py | calibration |
| IV_Generation.py | iv_generation.py | calibration |
| Intraday_Data_Fetch.py | intraday_data_fetch.py | calibration |
| Metrics_Calculator.py | metrics_calculator.py | calibration |
| Optimizer.py | optimizer_interface.py | calibration |
| Parameters_Extraction.py | parameters_extraction.py | calibration |
| Percentile_Saver.py | percentile_saver.py | calibration |
| Factor_Shock_Detection.py | factor_shock_interface.py + reference_factor_shock.py | signals |
| — | scoring_interface.py + reference_scorer.py (new) | signals |

Note: Filenames were already created in snake_case. The rename from Title_Case happened as part of the restructuring, not as a separate step. This table serves as the complete old→new mapping.
