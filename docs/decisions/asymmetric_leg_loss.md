# Asymmetric Leg Loss Invariant

The system handles the same underlying hazard — one leg of a pair being
removed or corrupted while the other stays live — in four places.

## Call Sites

1. **Stop-out orphans** — `stop_loss_protection.py`: when a stop loss fires
   on one leg, the counterpart must be closed.
   TODO(sam): exact function and line reference

2. **Delisting orphans** — `delisting_handler.py`: when a ticker is delisted
   or acquired, the counterpart legs must be closed.
   TODO(sam): exact function and line reference

3. **Position inversions** — `reconciliation.py`: when reconciliation detects
   a position that has inverted (wrong sign), both legs must be unwound.
   TODO(sam): exact function and line reference

4. **Failed closes** — `delisting_handler.py`: when a counterpart close fails
   during delisting handling, the failed leg must remain tracked.
   TODO(sam): exact function and line reference

## Invariant Statement

TODO(sam): State the invariant — what must be true after any of these four
events. What guarantees must the system provide about the remaining live leg.

## Rationale

TODO(sam): Why this invariant exists, what happens if it is violated, and
the historical context that motivated it.
