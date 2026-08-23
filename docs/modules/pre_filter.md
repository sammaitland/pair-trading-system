# Pre-Filter

Reduces the candidate pair universe before the live analytics module runs, so that
signal generation, portfolio construction and execution all complete before the
market close.

---

## The problem

The live analytics module (LAM) evaluates every pair in the calibrated universe
against the full trading rule set. Its runtime scales with the number of pairs and
is dominated by per-pair alpha and deviation calculations.

Three constraints collide:

1. **Signal quality improves later in the day.** The deviation measures that drive
   entry decisions are computed from the current day's returns. The closer to the
   close they are computed, the more of the day's information they contain.
2. **Execution needs slack.** Portfolio management must size positions against
   leverage, beta and concentration constraints, and the execution layer must
   place and fill both legs of every pair. Both need guaranteed time before the
   close.
3. **LAM on the full universe does not fit.** Running LAM across every calibrated
   pair takes longer than the window between the earliest acceptable signal time
   and the latest acceptable execution start.

The system therefore needs to reduce the pair count *before* LAM runs, using
cheaper computation, while accepting that the reduction step consumes part of the
same budget it is trying to protect.

**Therefore:** the pre-filter runs on a fixed schedule ahead of LAM, applies a
subset of the trading rules at lower computational cost, and emits an `Active`
flag per pair. LAM reads only the active set.

> `TODO(sam): insert the actual figures — full-universe LAM runtime, post-filter
> runtime, typical reduction ratio, and the wall-clock budget for each stage. The
> argument above is materially stronger with numbers attached.`

---

## The design decision that matters: asymmetric error cost

The pre-filter is an approximate screen sitting in front of an exact one. It makes
two kinds of mistake, and they are not equally expensive:

| Error | Consequence |
| --- | --- |
| **False negative** — a tradeable pair is dropped | Unrecoverable. LAM never sees the pair, so the opportunity is lost silently and leaves no trace in the logs. |
| **False positive** — an untradeable pair is admitted | Recoverable. LAM applies the exact rules and rejects it. The cost is runtime, which is the resource the pre-filter exists to conserve. |

A screen tuned to agree with LAM as closely as possible would minimise total
disagreement, treating both errors alike. That is the wrong objective here.

**Therefore:** every threshold the pre-filter shares with LAM is applied in a
deliberately loosened form. The loosening is configured as an explicit *leniency*
set rather than baked into the filter logic, so the bias is visible, tunable and
auditable rather than implicit in a set of magic numbers.

The leniency set covers:

- a widening adjustment applied to percentile thresholds
- a reduction applied to multi-day deviation requirements
- an exclusion band around the neutral region of the deviation distribution
- toggles allowing individual filters to be skipped entirely when their input data
  is not yet available at pre-filter time

The exclusion band deserves separate note: it is the one place where the pre-filter
*removes* pairs on signal grounds rather than data-quality grounds. Pairs whose
deviation sits near the centre of the distribution are excluded because they cannot
plausibly reach an entry threshold by the close. This is a bet on the deviation not
moving far in the remaining session, and it is the single most aggressive assumption
in the module.

> **Open item.** The false-negative rate is currently unmeasured. The correct test
> is to run LAM on the full universe on a sample of days and count pairs LAM would
> have traded that the pre-filter dropped. Until that is done, the leniency
> parameters are justified by reasoning rather than evidence.

---

## Cost-ordered filter cascade

Filters are applied in ascending order of computational cost, with early exit on
first failure. A pair that fails a set-membership test never reaches an alignment
or deviation calculation.

1. **Data availability** — historical series present for both legs and the index
2. **Classification availability** — both tickers mapped to a sub-sector category
3. **Set-membership exclusions** — market capitalisation floor, delisted tickers,
   structural exclusion lists
4. **Primary rule filters** — the shared filter block, applied with leniency
5. **Trend filters** — per-ticker directional screens
6. **Deviation calculation and banding** — the expensive step
7. **Suppression overlay** — applied only to pairs that have passed everything else

**Therefore:** the ordering is not stylistic. Step 6 is roughly an order of
magnitude more expensive per pair than steps 1–3, and steps 1–3 eliminate a
substantial fraction of the universe on most days. Reordering the cascade would
change total runtime materially without changing the output.

The suppression overlay at step 7 is ordered last for a different reason: it is the
only filter whose inputs come from a separate detection pipeline that may be
unavailable. Placing it last means its failure cannot prevent the rest of the
cascade from producing a result.

---

## Caching: the main runtime lever

Pair count grows quadratically in ticker count; the per-ticker quantities the
deviation calculation needs do not.

The deviation measure for a pair is built from per-ticker residuals against each
ticker's own sub-sector index. Those residuals depend only on the ticker, not on
its counterpart. Computing them inside the pair loop recomputes each ticker's
residual series once per pair it appears in.

**Therefore:** residuals are computed once per ticker in a populate step before the
pair loop begins, cached, and read during the loop. The pair loop reduces to a
combination of two cached series. The cache is explicitly reset at the start of each
run rather than persisted, because a stale residual series would silently produce
yesterday's signal with today's timestamp — see below.

---

## The parity problem: constructing today's index return live

The sub-sector indices are constructed during calibration from constituent
returns. At pre-filter time the current day's index value does not yet exist in
the calibration output, because calibration runs on completed sessions.

The residual calculation aligns each ticker's return series against its index
return series and operates on the intersection of their dates. If today is absent
from the index series, the intersection silently excludes today — and the
calculation succeeds, returns a plausible number, and reports a signal computed
from data ending yesterday.

This is the dangerous shape of failure: no exception, no warning, output that
looks correct.

**Therefore:** the module constructs the current day's sub-sector index return
directly from live constituent prices and appends it to the index series before any
alignment occurs, updating both the returns series and the derived price series so
that downstream consumers of either see a consistent view. The append step is
sequenced explicitly between data fetch and cache population, and the ordering
dependency is documented in the code rather than left implicit in call order.

This belongs to a general class: **any quantity that calibration derives from
completed sessions must be reconstructible live, by the same definition, from
partial-session data.** Where it cannot be, the live and calibrated pipelines have
diverged, and the divergence will not announce itself.

---

## Structural exclusions

Two categories of ticker are excluded by explicit list rather than by any
statistical test: crypto-linked equities and mortgage REITs.

The rationale is model misspecification rather than data quality. The system prices
each stock against a single sub-sector factor and treats the residual as
idiosyncratic, mean-reverting alpha. For these two groups that assumption fails in
a specific way: crypto-linked equities carry a dominant exposure to an asset that
is not a constituent of any equity sub-sector index, and mortgage REITs carry a
dominant exposure to rates that the single-factor model no longer captures — the
treasury factor was removed in the move to a single-factor specification.

**Therefore:** their residuals are not idiosyncratic. They are the unmodelled
factor, and they will appear as large, persistent, apparently tradeable deviations
that do not mean-revert on the horizon the strategy assumes. Excluding them is
cheaper and more honest than adding factors to accommodate a small number of names.

> **Known weakness.** Hardcoded lists in module scope do not scale and will drift
> out of date as new names list. The correct form is a configuration-driven
> exclusion set with the rationale attached to each entry, ideally derived from a
> factor-exposure test rather than maintained by hand. Documented here rather than
> fixed because the current list is short and stable.

---

## Degradation policy

Several inputs are optional: the earnings calendar, the delisted-ticker set, the
suppression pipeline, cached market capitalisations. Each is wrapped so that its
absence produces a warning and the run continues.

The reasoning is deadline-driven: a run that aborts produces no active set at all,
and there is no time to diagnose and re-run before the close.

**Therefore:** the module always produces output. But this policy is applied
uniformly, and it should not be. There are two distinct classes of optional input:

- **Efficiency inputs** (market caps, suppression overlay) — absence widens the
  candidate set. LAM still applies the exact rules. The cost is runtime.
- **Risk inputs** (earnings calendar) — absence removes a protection. Pairs are
  admitted through earnings announcements, which is precisely the event the filter
  exists to avoid.

Degrading silently on the second class is not equivalent to degrading on the first.

> **Improvement identified.** Classify optional inputs by class in configuration.
> Efficiency inputs degrade with a warning; risk inputs either abort the run or
> escalate to an alert that requires acknowledgement before execution proceeds.

---

## Auditability

Every pair in the calibrated universe appears in the output, including rejected
ones, with a decision flag and a free-text reason. Rejection reasons are
aggregated into a summary sheet by category.

**Therefore:** the question "why did the system not trade pair X today?" is
answerable from the output file alone, without re-running anything or reading
logs. Given that the pre-filter's expensive error is the invisible one, an output
format that makes every exclusion visible and attributable is a control on the
module's primary failure mode rather than a convenience.

---

## Self-testing the calibration assumption

Deviation values are converted to percentiles using a normal CDF with a globally
calibrated dispersion parameter. That parameter is fitted during calibration and
assumed to hold live.

The module includes a diagnostic that bins the resulting percentiles by decile and
flags any decile whose population departs from the expected uniform share. If the
calibrated dispersion no longer matches the live distribution, the percentiles
concentrate and the flags fire.

**Therefore:** drift in the calibrated distribution surfaces as a visible daily
signal rather than as a slow, unnoticed change in how many pairs pass the threshold.
The diagnostic tests the assumption the module depends on, not just the code that
implements it.

> **Open item.** The check is currently advisory — it prints flags but does not
> gate execution or write a machine-readable drift metric. A recorded per-day
> deviation-from-uniform statistic would turn a visual check into a monitorable
> series and give an objective trigger for recalibration.

---

## Interface

```
run_prefilter(broker_connection) -> DataFrame
```

**Reads**
- calibrated pair parameters and per-pair thresholds
- sub-sector index series and category assignments from calibration output
- live and historical market data for all universe tickers
- optional: earnings calendar, delisted set, cached market caps, suppression status

**Writes**
- one row per calibrated pair: identifiers, tail, index, active flag, rejection
  reason, deviation value, percentile, band
- run summary and rejection-reason aggregation

**Guarantees**
- every input pair appears in the output exactly once
- every inactive pair carries a non-empty reason
- an inactive result is never produced by an unhandled exception; data failures are
  caught and recorded as reasons

**Does not**
- make entry decisions — it produces candidates, not trades
- size positions or check portfolio constraints
- place orders

---

## Sanitisation notes for the public repo

`TODO — remove this section before publishing.`

- Replace the hardcoded local path insertion with package-relative imports.
- Move the inline maximum-spread argument into configuration; it is currently a
  literal at the call site while comparable parameters live in config.
- Leniency values, threshold parameters and the calibrated dispersion parameter
  move to `config.example.yaml` as documented but unset fields.
- The deviation function itself calls into the shared toolbox; replace with the
  documented interface plus a reference implementation.
- Remove the platform-specific completion notification.
- Decide whether the CDF-with-global-dispersion normalisation is disclosable. The
  mechanism is conventional; the fitted parameter is not.

**Code cleanup identified during this write-up:** the percentile and band are
computed inside the cached deviation call and then immediately recomputed from the
raw deviation in the pair loop. One of the two is redundant. Resolve before
publishing — a reviewer will read the duplication as uncertainty about which
implementation is authoritative.
