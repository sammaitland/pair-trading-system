# Roadmap

Modules and features excluded from this repository, listed with their intended
purpose. This is where unfinished work lives instead of in the repository as
half-built files.

## Excluded Modules

| Module | Purpose | Status |
|--------|---------|--------|
| Pair_Generator | Generate the calibrated pair universe with rolling beta support, walk-forward bubble exclusions, and BIC-based filtering | Excluded — pair selection filters are out of scope. Schema and synthetic fixture provided. |
| Factor_Shock_Detection | Multi-factor regression pipeline for detecting sector-level regime changes and suppressing affected pairs | Excluded — interface and no-op reference implementation provided |
| Tool_Box (signal internals) | Composite scoring, secondary signal evaluation, retention filtering | Excluded — interface and naive reference implementation provided |
| Scheduler_Template | APScheduler-based automation for scheduling pipeline stages | Excluded — incomplete, all code commented out. Workflows currently run manually. |
| LAM_Call | Jupyter notebook entry point for LAM analytics | Excluded — superseded by package entry point |
| Daily_Updater_Call | Jupyter notebook entry point for daily data capture | Excluded — superseded by package entry point |
| beta_stability/ diagnostics | Ticker-level R-squared regime change detection for beta stability | Excluded — analysis complete, pending integration as scoring weight in pair generator |

## Excluded Features

| Feature | Description | Status |
|---------|-------------|--------|
| IGV Exposure Management | Pre-trade IGV/software exposure constraint and options put hedge | Retired in V9.4 — now managed via factor shock framework |
| SES (Sophisticated Exit Strategy) | Model-based exit triggers | Removed in V9 — disabled flag remains |
| Treasury Beta Factor | Two-factor alpha model using DGS10 treasury yield | Retired — single-factor (sub-sector only) model adopted in V9.2 |
