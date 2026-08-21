# Architecture

TODO(sam): This document describes the system architecture, pipeline stages,
data flow, and the invariants that enforce calibration-live parity.

## Pipeline Overview

TODO(sam): High-level diagram of the full pipeline from universe determination
through to trade execution and reconciliation.

## Calibration Pipeline

TODO(sam): Stages, data flow, outputs. How calibrated parameters flow into
the live pipeline.

## Live Pipeline

TODO(sam): Stages (pre-filter → LAM → portfolio management → execution →
reconciliation), timing, data dependencies.

## Module Interactions

TODO(sam): Which modules call which, data contracts between them.

## Data Contracts

TODO(sam): The key data structures that flow between modules (pair cache,
parameters file, portfolio file).

## The Single-Source-of-Truth Invariant

TODO(sam): How scoring constants, filter thresholds, and constraint parameters
are shared between calibration and live pipelines to prevent divergence.

## Configuration Architecture

TODO(sam): How config.yaml drives all behaviour, why no values are hardcoded,
how version-specific directories work.

## Error Handling and Degradation

TODO(sam): How the system handles partial failures (factor shock unavailable,
market data missing, order timeout).
