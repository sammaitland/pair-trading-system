# Incident: Delisting Handler Partial-Failure Defect

TODO(sam): Document the delisting handler partial-failure defect and its fix.

## What Happened

TODO(sam): Description of the defect — on partial failure, all trades were
recorded to completed history and removed from portfolio, even when some
counterpart closes failed.

## Root Cause

TODO(sam): The state mutations (record completion, remove from portfolio)
operated on the entire affected set rather than the successfully-closed subset.

## Detection

TODO(sam): How the defect was identified.

## Resolution

TODO(sam): Per-trade conditional recording and removal, with failed closes
remaining in portfolio for retry. Operation made idempotent.

## Prevention

TODO(sam): What changes were made to prevent similar defects.
