# S7 — Oracle-ceiling audit and stop decision for driver substitution

Date: 2026-09-14

## Decision

**Stop the LLM day-D availability-driver substitution experiment as a point-price-MAE project.**

The realised-driver oracle is an upper bound for any context/LLM method whose
only action is to estimate the same day-D availability path and inject it
through the frozen S3 LEAR model. Its available MAE headroom is too small on
the retained generation track, while the aggregate transmission attachment is
reliably harmful.

This is a bounded design decision, not a claim that outage documents or LLMs
are intrinsically uninformative. It says that this particular attachment point
cannot support a credible price-point-forecast improvement claim.

## Scope

- Frozen model: S3 730-day LEAR model, no retraining.
- Baseline availability inputs: D-1 12:00 Europe/Stockholm persistence.
- Oracle: replace the selected driver with its realised day-D aggregate path.
- Evaluation population: the 94 selected event-days for which a cached frozen
  S3 test-slice model exists; 2,207 finite-target event-hour observations.
- Primary subset: local delivery hours overlapping the realised outage interval.
- Inference unit: event, not individual hour, using an event-block bootstrap.

The selected-event file includes earlier events outside the S3 test slice; 65
such event-days have no cached frozen test model and were intentionally not
refit or added to the evaluation population.

## Mechanical audit

`src/s7_diagnostics.py` reconstructs the S4/S5 paths and writes outputs under
`data/s7_diagnostics/`.

All substantive invariants pass for the scored population:

| Check | Result |
|---|---:|
| Baseline driver equals D-1 12:00 persistence | 94 / 94 event-days |
| Oracle driver equals the realised day-D profile | 94 / 94 event-days |
| Required day-D driver features exist in frozen models | 94 / 94 event-days |
| Local event-hour/DST interval alignment | 2,256 / 2,256 nominal rows |
| Generation injection lowers `avail_gen_mw` | 40 / 40 event-days |
| Transmission injection raises `unavail_tx_import_mw` | 54 / 54 event-days |
| Recomputed frozen baseline equals stored S3 forecast | 2,207 / 2,207; max absolute difference 1.14e-13 EUR/MWh |

Therefore the negative oracle result is not explained by a timezone,
persistence/oracle-path, driver-sign, feature-wiring, or model-cache mismatch.

## Driver prevalence and baseline headroom

Against a same-weekday median of up to four preceding non-event weeks, the
realised aggregate selected driver moves in the expected operational direction
on 80.5% of realised affected hours. The median correctly signed anomaly is
1,237 MW. Thus most selected outages are visible in the realised aggregate
physical driver relative to a normal level.

However, relative to the actual prediction baseline (D-1-noon persistence),
the median operational difference is 0 MW and only 27.9% of affected hours
have a correctly signed realised-minus-persistence difference. The driver is
often already represented by the persistent state before the delivery-day
forecast is formed, leaving little opportunity for a path substitution to
change the price forecast.

## Frozen-model sensitivity

A local 100-MW operational worsening was injected at the same seam used by S4:
reduce `avail_gen_mw` by 100 MW for generation events; increase
`unavail_tx_import_mw` by 100 MW for transmission events.

Across affected hours:

- median response: +0.070 EUR/MWh per 100 MW;
- 68.7% of hours have the expected positive price sign;
- 47.4% have an absolute response of at least 0.10 EUR/MWh per 100 MW.

The seam is therefore not zero-sensitivity, but it is conditional and not
sign-stable enough to turn a physical availability path into a reliable
residual price correction.

## Attachment-point heterogeneity

The pooled score obscures a material split.

| Realised affected-hours subset | Observations | Baseline MAE | Oracle MAE | Oracle − baseline |
|---|---:|---:|---:|---:|
| Generation | 676 | 21.960 | 21.925 | −0.035 |
| Transmission | 1,231 | 18.911 | 19.003 | +0.092 |
| All scoreable events | 1,907 | 20.313 | 20.363 | +0.050 |

The transmission outcome is robust under event-block resampling:

| Stratum | Events | Estimate, oracle − baseline MAE | 95% event-block bootstrap interval |
|---|---:|---:|---:|
| Transmission, affected | 4 | +0.155 | [+0.048, +0.261] |
| Transmission, affected and driver differs from persistence | 4 | +0.323 | [+0.154, +0.562] |

This rules out using the aggregate `unavail_tx_import_mw` feature as the LLM
target for border-specific transmission events. It is a representation mismatch:
a border- and direction-specific restriction is not adequately represented by a
single aggregate SE3 import-unavailability path.

Generation is not confirmed as a win:

| Stratum | Events | Estimate, oracle − baseline MAE | 95% event-block bootstrap interval | Bootstrap P(oracle better) |
|---|---:|---:|---:|---:|
| Generation, affected | 8 | −0.072 | [−0.316, +0.104] | 0.716 |
| Generation, affected and driver differs | 8 | −0.050 | [−0.336, +0.186] | 0.637 |
| Generation, affected, differs, high sensitivity | 8 | −0.282 | [−0.876, +0.240] | 0.833 |

The wide intervals reflect eight heterogeneous events, not a claim of reliable
improvement.

## Oracle ceiling: practical scale

The key decision metric is the oracle gain on the retained generation track.
An LLM estimating the same realised driver cannot exceed this ceiling.

| Generation stratum | Hours | Baseline MAE | Oracle MAE | Oracle gain | Relative gain |
|---|---:|---:|---:|---:|---:|
| All realised affected hours | 676 | 21.960 | 21.925 | 0.035 EUR/MWh | 0.16% |
| Affected hours where realised driver differs from persistence | 315 | 21.686 | 21.584 | 0.102 EUR/MWh | 0.47% |
| Also high-sensitivity | 133 | 24.933 | 24.619 | 0.314 EUR/MWh | 1.26% |

For all generation affected hours, the oracle changes the forecast by a median
of 0 EUR/MWh. In the driver-different subset, its mean absolute forecast shift
is only 1.115 EUR/MWh. Even on the high-sensitivity subset, the apparent
0.314-EUR/MWh gain is based on 133 hours and its event-block interval crosses
zero.

The practical implication is direct: an imperfect LLM-derived timing and
magnitude path is not expected to turn a 0.04–0.10 EUR/MWh broad-sample oracle
ceiling into a meaningful, robust point-MAE improvement.

## Conclusion and follow-up

Do not spend additional LLM-call budget on the availability-path point-forecast
experiment.

The reusable contribution is the empirical attachment-point result:

1. structured outage context maps visibly into realised aggregate physical
   availability;
2. that physical path has little incremental headroom over the prediction-time
   persistence state for generation events;
3. aggregate transmission substitution is systematically harmful despite a
   realised oracle;
4. therefore numeric driver substitution is not an appropriate universal
   interface between outage documents and a frozen statistical price model.

If later work requires an LLM component, treat it as a different research
question—e.g. uncertainty/scenario extraction, residual correction, or
ensemble reweighting—not as a continuation of this stopped driver-substitution
experiment. A future transmission study would require a new border-specific,
direction-preserving representation before any LLM evaluation.

## Reproduction

```bash
.venv/bin/python src/s7_diagnostics.py
```

Primary outputs:

- `data/s7_diagnostics/mechanical_audit.json`
- `data/s7_diagnostics/driver_absorption_summary.json`
- `data/s7_diagnostics/sensitivity_and_rescore.json`
- `data/s7_diagnostics/event_decomposition_summary.json`
- `data/s7_diagnostics/event_block_bootstrap.json`
- `data/s7_diagnostics/oracle_ceiling.json`
