# Roadmap

Modules and features excluded from this repository, listed with their intended
purpose. Where a module is architecturally important, an interface and reference
implementation are provided so the surrounding pipeline can be exercised.

## Proprietary Modules (interface + reference implementation provided)

| Module | Purpose | Status |
|--------|---------|--------|
| Pair_Generator | Generate the calibrated pair universe with rolling beta support, walk-forward bubble exclusions, and BIC-based filtering | Interface and reference implementation provided. Synthetic fixture generator in fixtures/. |
| Optimizer (objective function) | Walk-forward optimisation over EMA/CDF parameter space using canonical scoring rules | Interface and reference implementation provided. Reference optimizer consumes shared scoring_constants and constraints. |
| Factor_Shock_Detection | Multi-factor regression pipeline for detecting sector-level regime changes and suppressing affected pairs | Interface and no-op reference implementation provided. |
| Tool_Box (signal internals) | Composite scoring, secondary signal evaluation, retention filtering | Interface and naive reference implementation provided. |

## Excluded Modules (no replacement needed)

| Module | Purpose | Status |
|--------|---------|--------|
| Scheduler_Template | APScheduler-based automation for scheduling pipeline stages | Excluded — incomplete, all code commented out. Workflows run on-demand. |
| LAM_Call | Jupyter notebook entry point for LAM analytics | Excluded — superseded by package structure. |
| Daily_Updater_Call | Jupyter notebook entry point for daily data capture | Excluded — superseded by package structure. |
| beta_stability/ diagnostics | Ticker-level R-squared regime change detection for beta stability | Analysis complete, included as appendix in docs/appendix/. |

## Excluded Features

| Feature | Description | Status |
|---------|-------------|--------|
| IGV Exposure Management | Pre-trade IGV/software exposure constraint and options put hedge | Retired in V9.4 — now managed via factor shock framework. |
| SES (Sophisticated Exit Strategy) | Model-based exit triggers | Removed in V9 — disabled flag remains. |
| Treasury Beta Factor | Two-factor alpha model using DGS10 treasury yield | Retired — single-factor (sub-sector only) model adopted in V9.2. |

## Pending

| Item | Description |
|------|-------------|
| Module-level documentation | Per-module deep-dive docs (design decisions, known weaknesses, interface contracts) are planned but not yet written. |
| LICENSE | Licence file to be added before public release. |
