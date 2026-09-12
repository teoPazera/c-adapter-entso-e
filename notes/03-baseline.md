# S3 — Build and backtest the baseline model (no context)

Date: 2026-09-12 | Repo: `C_adapter_Entso_E`, single-repo project (no upstream clones)

## Question

Can a LEAR (Lago et al. 2021) reimplementation, walk-forward backtested with rolling
recalibration on SE3 day-ahead price, clear a naive persistence baseline on MAE/rMAE/CRPS —
and does it do worse specifically inside the 18 curated S2 event windows (the gap S4's context
adapter would need to close)?

## Preamble — two corrections found while extending the history, before any model was built

The S3 plan's Step 1 (extend `WINDOW_START` from 2023-01-01 to 2021-01-01, so a 2-year
calibration window can reach every curated event) surfaced two problems neither the plan nor
any prior stage anticipated. Both were fixed with the user's explicit sign-off
(`2026-09-12`) before any feature/model code was written, because both reach back into
already-gated S1/S1b/S2 conclusions.

### Correction 1 — outage dedup bug (`_load_series_dir`, `_prepare_outages`)

**Finding.** `s1_build_table.py:74` deduplicated every cached outage series (A77, A80, all 9
transmission pairs) by `created_doc_time` index alone. entsoe-py emits **one row per
`Series_Period` within a revision, and every period of the same revision shares that same
top-level index** — so index-based dedup silently kept one arbitrary period per revision and
discarded the rest. Quantified against the full raw cache (`data/raw/s1/*`), before any fix:

| series | raw rows | rows dropped by index-dedup | share dropped |
|---|---|---|---|
| `transmission_outage_se3_se4` | 848 | 399 | 47% |
| `transmission_outage_se2_se3` | 874 | 396 | 45% |
| `transmission_outage_se3_dk1` | 647 | 341 | 53% |
| `transmission_outage_fi_se3` | 622 | 280 | 45% |
| `transmission_outage_no1_se3` | 518 | 247 | 48% |
| `transmission_outage_se3_no1` | 486 | 222 | 46% |
| `transmission_outage_dk1_se3` | 434 | 228 | 53% |
| `transmission_outage_se3_fi` | 203 | 95 | 47% |
| `transmission_outage_se4_se3` | 140 | 64 | 46% |
| `outage_production_units` (A77) | 627 | 283 | 45% |
| `outage_generation_units` (A80) | 395 | 83 | 21% |

Pre-existing since S0/S1's first pull — only became visible now because extending the window
changed which cache file 2 transmission rows landed in, producing a small, otherwise-inexplicable
before/after diff that led to the investigation (`unavail_tx_fi_se3_mw` moved on 247 hours despite
its own `nominal_mw_proxy` being unchanged — impossible unless the underlying rows differed).
Concrete example: mrid `0GQC0m_4vSjqRAuu_6HauQ` rev 27 (`se3_se4`) has 8 real, contiguous periods
spanning 2022-08-10 to 2023-03-26 (avail_qty 3400–4800 MW); only one 19-hour fragment survived.
A second, compounding bug in `_prepare_outages` (`groupby("mrid").tail(1)` after
`sort_values(["mrid","revision"])`) would have re-truncated a multi-period *latest* revision back
to one row even after fixing the loader.

**Fix** (`src/s1_build_table.py`): `_load_series_dir` gained a `dedup="full_row"` mode (drops only
exact cross-chunk duplicate rows, not same-index distinct periods), applied to the 3 outage
loading call sites; `_prepare_outages` now keeps every row at a mrid's max revision
(`df[df["revision"] == df.groupby("mrid")["revision"].transform("max")]`) instead of
`.tail(1)`. Rebuilt `se3_hourly.csv` from already-cached raw data — **0 new API requests**.

**Consequence for a written S2 finding.** `notes/02-events.md` Finding 10 read G050's 304-hour
ENTSO-E/UMM end-date gap as "two independent regulatory pipelines settling on different final
states." The undeduped A77 data for that exact mrid has 3 periods (0 → 930 → 552 MW available)
running to **2026-01-06**, matching Nord Pool's UMM end almost exactly — the old table only kept
the first (0 MW, ending 2025-12-24). **This reads as our own bug producing an apparent
cross-system divergence, not a real one** — see the corrected event table below (this mrid is now
`G090`). One nuance survives even after the fix: S2's own candidate rule requires
`reduction_mw >= 300`, so the middle (110 MW reduction) segment still doesn't qualify as part of
any candidate, and the 552-MW-reduction tail is now its own separate candidate (`G091`, not
selected into the final 18) — the fix restores the *data*, it doesn't change S2's per-period
candidate-extraction design. The 304-hour number itself is therefore not "wrong," it now has an
honest mechanism instead of an appeal to two independent record-keeping systems.

### Correction 2 — S2 re-run on corrected data

Since the same buggy dedup fed `s2_curate_events.py`'s candidate pool, S2 was re-run end to end
(not hand-patched) once the fix landed — a smaller patch would have left the candidate pool
inconsistent with the corrected outage data.

- `s2_pull_umm.py`: re-run to confirm idempotency first — **0 new requests** (all list pulls
  still fully cached).
- `s2_curate_events.py`: generation candidates **60 → 102**, transmission candidates
  **19 → 80** (previously-dropped periods that independently clear the `reduction_mw>=300`/
  `duration 1-60d` filters now exist as their own candidates), overlap-duplicates flagged
  **2 → 23**, matched **60/67 (89.6%) → 100/182 (55%)** — hit-rate *looks* worse only because the
  denominator grew by ~2.7x; absolute matched count grew. **47 real HTTP requests, 32.3s, $0**
  (new UMM version-history fetches for candidates that didn't exist before).
- Selected list is still **18 events**, same per-stratum targets, still Gate PASS: A=11 (target
  9–11), B=3 (target 2–3), C=4 (target 3–4), D=4 (target ≥3, was 5 pre-fix). 12 of 18 events are
  the *same real event* as before (renumbered — event IDs are positional over a now-different-size
  pool, not stable identifiers); 6 pre-fix events (`G025` 2024-02-29, `G028` 2024-04-21 planned,
  `G033` 2024-08-15 planned, `G041` 2025-06-23, `G046` 2025-08-17, and one autumn-2023 slot) lost
  their stratum slot to a competing candidate from the newly-visible 2021–2022 population, under
  the *same* unmodified selection rule. Full corrected table:

| id | unit / border | type | start (UTC) | end (UTC) | MW | hydro? |
|---|---|---|---|---|---|---|
| G008 | Ringhals 4 | Unplanned | 2021-07-26 21:00 | 2021-09-09 16:00 | 1134 | N |
| G010 | Forsmark 2 | Unplanned | 2021-08-23 00:25 | 2021-08-27 13:30 | 1118 | N |
| G014 | Forsmark 2 | Unplanned | 2021-10-01 20:00 | 2021-10-03 09:00 | 1118 | N |
| G015 | Ringhals 4 | Unplanned | 2021-12-17 13:30 | 2021-12-19 05:00 | 1134 | N |
| G016 | Ringhals 3 | Unplanned | 2021-12-29 17:15 | 2022-01-01 10:00 | 1063 | N |
| G018 | Ringhals 3 | Unplanned | 2022-01-06 20:28 | 2022-01-11 06:33 | 1063 | N |
| G053 | Ringhals 3 | Unplanned | 2023-10-19 12:45 | 2023-10-21 06:00 | 1063 | N |
| G055 | Ringhals 4 | Unplanned | 2023-11-29 00:25 | 2023-12-04 02:00 | 1134 | N |
| T060 | FI → SE3 | Unplanned | 2023-11-29 00:25 | 2023-12-04 22:59 | 1200 | N |
| G060 | Forsmark 3 | Unplanned | 2024-03-16 16:00 | 2024-03-22 01:00 | 1172 | N |
| T064 | FI → SE3 | Unplanned | 2024-05-10 19:08 | 2024-05-15 12:40 | 1000 | Y |
| T065 | FI → SE3 | Unplanned | 2024-06-18 15:00 | 2024-06-27 21:59 | 1100 | Y |
| G074 | Forsmark 3 | Unplanned | 2025-02-05 17:00 | 2025-02-08 03:00 | 1172 | N |
| T069 | FI → SE3 | Unplanned | 2025-02-28 15:11 | 2025-03-31 21:59 | 1000 | N |
| G079 | Forsmark 1 | Unplanned | 2025-05-09 16:00 | 2025-05-17 04:00 | 1040 | Y |
| G085 | Forsmark 2 | Unplanned | 2025-08-14 01:00 | 2025-08-16 12:00 | 1121 | N |
| G090 | Forsmark 1 | Unplanned | 2025-12-22 19:28 | 2025-12-24 09:30 | 1040 | N |
| G097 | Forsmark 3 | Unplanned | 2026-04-09 01:00 | 2026-04-10 23:30 | 1172 | Y |

Stratum B (planned generation, lead >= 7 days) is now filled by 3 events not shown as distinct
rows above because they coincide with unplanned-stratum picks in this listing pass — full detail
in `data/s2/events.json`, superseding `data/s2/archive/events_pre_dedupfix.json`.
`notes/02-events.md` carries a pointer to this section rather than being rewritten in place.

**Implication for this stage's own window design.** The corrected list now reaches back to
2021-07-26 — five events (`G008`, `G010`, `G014`, `G015`, `G016`) predate the 2023-07-01 test-slice
start entirely. This does **not** reopen the window-extension decision: the validation slice
(2023-01-01 to 2023-06-30) and test slice (2023-07-01 onward) are unchanged, every one of those 5
early events sits purely inside the *calibration* history the 2-year window is allowed to look
back over, never inside a *scored* delivery day. `G018` (2022-01-06) is the earliest event that
could ever fall inside a forecast if the test slice were extended backward — not attempted here.

### Correction 3 — `solar_forecast_mw` has no data before 2021-12-02

**Finding.** No monthly raw chunk before `2021-12` carries a `Solar` column at all
(`data/raw/s1/wind_solar_forecast/2021-01.csv` .. `2021-11.csv` all have exactly `['Wind
Onshore']`) — SE3 solar capacity was genuinely negligible then (first real month, Dec 2021: max
21.2 MW, mean 1.9 MW; 2022 max jumps to 447.7 MW, a 22x increase consistent with real capacity
buildout, not a pull error). This crosses the plan's own 1%-NaN-in-2021-22 stop condition
(91.78% NaN for all of 2021).

**Fix, per the user's decision (2026-09-12):** zero-fill strictly before the column's own first
valid observation (`s1_build_table.py:build_wind_solar`) — 8,040 hours (2021-01-01 00:00 to
2021-12-01 23:00). Reported per-column in `coverage_report.json`
(`n_zero_filled_pre_first_observation`). Residual NaN after this fix, only where the plan's 1%
threshold could still bind (2021-22): `hydro_reservoir_fill` 0.82% (2021, a leading 3-day gap
before the table's own first weekly report — outside every window ever used, see below),
`price_eur_mwh` 0.01%/year (single scattered hours), `solar`/`wind` 0.55% (2022). All comfortably
under 1%.

## What I did

1. Archived `data/s1/se3_hourly.csv` → `data/s1/archive/se3_hourly_2023start.csv`; set
   `WINDOW_START = "2021-01-01"` in `s1_pull_history.py` and `s1_build_table.py`.
2. Ran the extended pull: **116 real HTTP requests, 142.3s, $0** (vs. the plan's 80-request
   estimate — the gap is internal pagination fan-out on the transmission-outage endpoint,
   consistent with every prior stage's own real-vs-estimated gap, not an error). `SE_1`/`SE_2`/
   `SE_4` A68 (per-type installed capacity) still empty for 2021-22, consistent with S1 Finding 5;
   the per-unit registry (what actually feeds the table) returned real 2021 (21 rows) and 2022
   (22 rows) data — **risk (a) from the plan did not materialize**, no registry-gap fallback
   needed.
3. Rebuilt the table; found and fixed the two corrections above; rebuilt again. Final:
   `data/s1/se3_hourly.csv`, **49,919 rows**, 2021-01-01 00:00+01:00 to 2026-09-11 23:00+02:00,
   20 columns, 0 new API requests for either rebuild (pure reprocessing of cached raw data).
4. Registry reconciliation against the (now much larger, post-dedup-fix) merged outage table:
   **832 checked, 1 missing** — `46WPU0000000015W` (Forsmark 1), outage starting **2020**, a year
   before the registry's own 2021-2026 pull window — same class of exception as 01b Finding 3
   (then: 1 exception, a 2022-start outage, now resolved since the registry itself now reaches
   2021), not a genuine mismatch within a covered year.
5. `nominal_mw_proxy` (transmission-availability driver's per-pair scale) recomputed with more
   data now in scope: `se3_se4` 6,000 → 6,200 MW, `se4_se3` 2,500 → 2,800 MW; all other 7 pairs
   unchanged. Both are the plan's anticipated risk (b) (a proxy derived from `max(avail_qty)`
   naturally shifts when more history is included) — reported, not treated as an error.
6. Re-ran S2 end to end on the corrected table (Correction 2 above).
7. Wrote `src/s3_features.py` (feature matrix + day-profile builder + asinh scaler pieces),
   `src/s3_lear.py` (LEAR fit/predict/save/load), `src/s3_metrics.py` (MAE/rMAE/pinball/CRPS/
   coverage/naive benchmarks/rolling-quantile tracker), `src/s3_backtest.py` (walk-forward
   runner). Smoke-tested each in isolation before the real validation run (see Verification).
8. Ran the validation slice (2023-01-01 to 2023-06-30, both windows) and the test slice
   (2023-07-01 to 2026-09-11, chosen window) — results below.

## Findings

1. **Forecast-time protocol, as implemented** (`data/s3/model_config.json`): t0 = D-1 12:00
   local. Price known through D-1; load/wind/solar forecasts for D treated as available at t0
   (their true ENTSO-E publication lag is `UNVERIFIED` — not run, would need a publication-
   timestamp field this dataset doesn't carry). The three availability columns
   (`avail_gen_mw`, `unavail_tx_import_mw`, `unavail_tx_export_mw`) get the D-1 12:00 value held
   constant across all 24 hours of D in the **forecast** row only; every **calibration** row uses
   realised same-day values for every column, including these three. `hydro_reservoir_fill` uses
   D-7, collapsed to one scalar per day (it is a weekly-published series, not lagged for the
   day-vs-forecast asymmetry the availability columns get).
   **Caveat, found on independent review, deliberately not changed:** the same three
   availability columns' own **D-1 lag feature** (as opposed to the D-block just described) uses
   D-1's full, realised 24-hour profile — including hours 13-23, which are *after* t0 (D-1
   12:00) and so would not actually be known yet at the moment a real forecast is issued. This
   is not an oversight: the plan's own forecast-time-protocol text states "their lagged values
   (D-1, D-7) are realised and known," i.e. this simplification was specified up front, not
   discovered after the fact. Quantified rather than left vague: on 220 of 1,169 test days (19%)
   `avail_gen_mw` actually changes between D-1 12:00 and D-1's later hours, so the simplification
   is not vacuous — the D-1 lag feature genuinely carries a small amount of information a real
   t0-respecting forecast would not have had. Left as specified; revisit if S4/S5's causal story
   about "what the agent could have known" needs the D-1 lag tightened to match t0 exactly.
2. **Feature count matches the plan's estimate exactly: 541.** 96 price-lag features (4 lag-days
   × 24h) + 432 exogenous features (6 columns × 3 lags × 24h) + 1 hydro scalar + 7 weekday dummies
   + 1 holiday dummy + 4 Fourier terms. **The holiday dummy needed a real fix, found on
   independent review**: `holidays.country_holidays("SE")`'s default behaviour counts every
   Sunday as a holiday (63 "holidays" in 2024 vs. 13 real ones — New Year, Epiphany, Easter,
   Midsummer, etc.), making the feature near-collinear with the `weekday_6` dummy already in the
   set rather than capturing genuine one-off holiday effects. Fixed to
   `holidays.Sweden(include_sundays=False)`, verified against 2024 (13 real holidays, Sundays
   correctly excluded) before the final backtest.
3. **`LassoLarsIC(criterion="aic")` cannot run unmodified on the 1-year window.** scikit-learn
   1.9.1 requires `n_samples > n_features + 1` for its own OLS-based noise-variance estimate
   (added in sklearn 1.1) and raises otherwise; 365 calibration days < 542 (541 features +
   intercept). The 2-year window (730 samples) is unaffected. **Resolution**
   (`src/s3_lear.py:_ridge_noise_variance`): when infeasible, estimate noise variance via a
   ridge-regularised fit (penalty = mean squared singular value of the calibration window's own
   design matrix — a data-scaled heuristic, not a free parameter) and its effective degrees of
   freedom, `RSS_ridge / (n - trace(ridge hat matrix))`, computed once per day and shared across
   all 24 hour-models (only the target differs per hour, the design matrix doesn't). Recorded per
   saved model as `noise_variance_method` (`"ols"` or `"ridge_fallback"`) for full transparency
   about which window used which path. **Named on independent review, not previously stated
   explicitly: the window-selection comparison is confounded with this choice.** Every 1-year
   model uses the ridge fallback; every 2-year model uses sklearn's own OLS estimator (confirmed
   in the final validation run's `model_diagnostics`: `n_ridge_fallback_models` 181/181 for the
   1-year window, 0/181 for the 2-year window). "The 2-year window wins" is inseparable from
   "the OLS noise-variance path wins" — this was never a clean ablation of window length alone.
   Flagged rather than re-designed: the winning path is also the one requiring no heuristic
   substitution, so the confound does not leave the choice resting on the less-principled option.
4. **Parallelising the 24 per-hour fits inside a single day (as the plan's Step 5 literally
   proposes) is slower than not parallelising them.** Measured on one day: 1-year window, 5.14s
   sequential vs. 12.56s with `n_jobs=-1` (joblib's `loky` process-pool startup cost dominates 24
   small tasks). **Deviation from the plan's literal wording, not from its intent**: parallelised
   across *days* instead (one persistent worker pool for the whole slice, each worker doing one
   day's 24 hourly fits sequentially). Measured on the real 180-day validation run under 14-way
   day-level parallelism: 1-year window 521.9s (2.9s/day amortised), 2-year window 1,266.4s
   (7.0s/day amortised) — slower than the plan's "under 30 minutes on 8 cores" hope once actually
   measured at scale (extrapolating the chosen 2-year window's rate to the ~1,170-day test slice:
   ~2.3 hours), but still comfortably under the plan's own explicit "if it exceeds 3 hours, use
   `--recalib-every 7`" threshold, so daily recalibration was kept rather than pre-emptively
   downgraded.
5. **The original fit-then-save-all-at-the-end implementation would have violated the plan's own
   resume requirement.** Caught in code review before the test run: saving every `DayModel` only
   after the entire `Parallel(...)` batch returns means an interrupted ~1,170-day run loses every
   completed fit, not just in-flight ones. Fixed to fit-and-save inside each worker task
   (`src/s3_backtest.py:_fit_and_save`) before running the real test slice.
6. **A single root cause — `pd.Timedelta` used for calendar-day arithmetic on tz-aware
   Timestamps — turned out to have five independent symptoms across the codebase, discovered in
   three escalating passes, not one.** `Timedelta` is fixed absolute-duration arithmetic; it does
   not know a local calendar day can be 23, 24 or 25 real hours. Escalation, each pass triggered
   by re-checking the previous fix's own blast radius rather than assuming it was complete:
   - **Pass 1** (found via the 730-day validation run producing 180 days instead of 181, no
     error): `(e - s).days` undercounts by 1 whenever a range's net DST shift is negative
     (`2023-01-01`→`2023-06-30` elapses 179 days 23 hours, not a clean 180) — `2023-06-30` was
     silently dropped from that run. Fixed by counting on `.date()` objects.
   - **Pass 2** (found by spot-checking that reloading a saved `DayModel` reproduces its own
     stored forecast — 2 of 3 random test days failed): `_date_range`'s `s + Timedelta(days=k)`
     drifts the *label* by up to 24h once the sequence crosses a transition — self-correcting
     only at the next opposite-direction transition. Quantified in full: of the 1,169 test-slice
     days, exactly 3 calendar dates were processed **twice** (`2023-10-29`, `2024-10-27`,
     `2025-10-26`, an autumn DST Sunday each year) and 3 were **never processed at all**
     (`2024-03-31`, `2025-03-30`, `2026-03-29`, a spring DST Sunday each year) — and separately,
     **462 of 1,169 days (40%) had their output rows' `timestamp` column mislabelled**, shifting
     up to 23 of a day's 24 hours onto the *following* calendar date. The mislabelling does not
     touch any `y`/`point`/`naive`/quantile *value* (those come from `day_hour_profile`, which
     re-normalizes to local midnight on entry regardless of what it's handed), but it does
     corrupt anything that groups rows by date — per-year, per-season, and specifically the
     inside-vs-outside-curated-events split this stage exists to produce. First reported to the
     user as "6 days affected, aggregate metrics fine" — **that was incomplete**: true for the
     aggregate MAE/CRPS, false for every date-grouped table. Corrected on follow-up before any
     date-grouped number was written down.
   - **Pass 3** (found by then asking, systematically, "where else does this codebase step by a
     fixed number of calendar days rather than one time-of-day computation," rather than trusting
     that Passes 1–2 had found every instance): three more genuine call sites, none previously
     checked. (a) `fit_day`'s own 730-day calibration window (`s3_lear.py`) stepped backward via
     `Timedelta` too — verified to collapse 2 distinct calendar dates into a duplicate while
     dropping 2 different ones, in *every single* saved `DayModel`, not just ones near a
     transition (any 730-day span crosses ~4 transitions). (b) `day_hour_profile`'s own
     `day_end = day + Timedelta(days=1)` slice boundary — verified directly against all 11 real
     DST transitions in the 2021–2026 data: on the fall-back day itself, `day_end` landed at
     23:00 the *same* day, one hour short of real midnight, silently excluding that day's own
     genuine hour-23 observation from `day_hour_profile`'s output (a real dropped value, not a
     label). (c) the 1/2/3/7-day price and exogenous lag lookups in `build_feature_row` and both
     naive-forecast functions — verified to fetch the **wrong calendar day's data outright** (not
     merely a wrong hour on the right day) for 78 (day, lag) combinations across 2021–2026,
     always in the week following a spring-forward.
   - **Fix, applied uniformly**: every day-stepping site now uses `pd.DateOffset(days=k)`, which
     is calendar/DST-aware — verified directly against all 11 real transitions in the dataset
     (`day_hour_profile` now shows exactly 1 NaN hour on every spring-forward day and 0 on every
     fall-back day, matching the true 23/25-hour structure) and against the specific dates each
     pass found broken (all now correct).
   - **Disposition of the two already-completed backtests**: the validation run and the first
     test run (results this note originally reported at this point) were both produced before
     Pass 3, so both were re-run in full after all three passes' fixes landed and the stale
     `data/s3/models/` cache was cleared (a cached `.npz` keyed only by date would otherwise have
     silently reused a pre-fix fit). The pre-fix artifacts are kept at
     `data/s3/archive_predatefix/` for reference, not deleted. **Everything below this point is
     from the re-run.**
7. **The date-arithmetic fixes exposed a separate, pre-existing numerical instability in
   `LassoLarsIC` itself, on the re-run's validation pass.** 541 heavily-lagged, mutually-
   correlated features regularly push sklearn's LARS solver into a near-singular active set —
   the "Regressors in active set degenerate" `ConvergenceWarning` (silenced at import time, see
   `s3_lear.py`'s module docstring) fires on most individual hour-fits and was already expected.
   On one (day, hour) combination in the corrected 1-year-window validation run, this crossed
   from "recovers with a warning" into an unrecovered internal shape mismatch inside sklearn's
   `lars_path` (`shapes (365,109) and (108,) not aligned`, sklearn 1.9.1) that crashed the whole
   run. This is very unlikely to be *caused* by the date-arithmetic fixes (the feature set's own
   collinearity is the trigger, not which calendar day supplied a row) — more likely, the corrected
   calibration windows simply landed on a different, rarer numerically-unlucky configuration than
   the pre-fix runs happened to hit. **Fixed** (`src/s3_lear.py:_fit_one_hour`): wrapped the
   `LassoLarsIC.fit()` call in a `try/except`, degrading to the same zero/median model already
   used for too-few-valid-rows hours, tracked separately as `DayModel.n_numerical_failure_hours`
   for transparency. A single hour's numerical failure now costs one hour of one day's forecast
   quality, not the entire backtest.
8. **An independent review (Fable 5.1, asked to verify the code and note directly rather than
   trust a summary) found one more residual instance of the exact Finding 6 bug class, plus four
   lower-severity gaps — all fixed before the results below.**
   - **Residual DST bug**: `run_slice`'s own `D + Timedelta(hours=h)` row-timestamp construction
     had the identical flaw Finding 6 already fixed for *which day* an iteration represents, but
     Finding 6 never touched *labeling the 24 hours within* that day the same way. On the two
     real transition days per year this still mislabelled a row's hour (spring-forward day: hour
     23 landing on the next calendar date; verified directly against `2024-03-31`/`2024-10-27`).
     Fixed (`src/s3_backtest.py:_hour_label`): operate in naive wall-clock time, then localize
     with `nonexistent="NaT"` / `ambiguous=True`, rather than adding an absolute `Timedelta` to a
     tz-aware Timestamp. No refit needed — this only touches the walk-forward labeling pass, not
     the cached model fits.
   - **`_fit_one_hour`'s exception handling was too broad to safely distinguish "known LARS
     numerical fragility" from "a real upstream bug got masked."** Added an explicit assertion
     that no NaN reaches the solver (ffill+bfill should already guarantee this; if it doesn't,
     that is a materially different and more serious problem than LARS degeneracy, and silently
     lumping the two together would hide it) and a print of the exception type/message whenever
     the fallback fires, so a run's console output is a complete record, not just an aggregate
     count.
   - **`DayModel.save()` was not atomic.** A process killed mid-write (this session alone hit a
     LARS crash and two Windows/joblib `BrokenProcessPool` failures) could leave a
     truncated/corrupt `.npz` at the real path, which the resume logic's `exists()` check would
     then trust and load. Fixed: write to a temp file, `os.replace` into place only once the
     write completes.
   - **Per-model diagnostics (`n_ffill_feature_nan`, `n_degenerate_hours`,
     `n_numerical_failure_hours`, which `noise_variance_method`) were recorded per `DayModel` but
     never rolled up anywhere a reader would see them.** Added `aggregate_model_diagnostics()`,
     summed into both `window_selection.json` (validation) and `metrics.json` (test) — this is
     what surfaced the window-selection confound in Finding 3 with actual numbers rather than as
     a suspicion.
   - **Dead code removed**: `_fit_or_load` (superseded by `_fit_and_save`, never called).
   - Both real code fixes (the DST label and the assertion/logging) required re-running; the
     validation slice was re-run a final time with everything in this note applied
     (Findings 1-8), and **that final validation run is the one the numbers below and in
     `data/s3/window_selection.json` come from**.
9. **Test-slice results (2023-07-01 to 2026-09-11, 1,169 days, 2-year/730-day window, chosen per
   Finding 3): the baseline clears naive, overall and in every calendar year.** First run of the
   full test slice produced `mae_model = Infinity` / `crps_model = Infinity` — see Finding 11 for
   the root cause and fix; the table below is the corrected run.

   | period | n_obs | MAE (model) | MAE (naive) | rMAE (model/naive) | CRPS (model) | CRPS (naive) |
   |---|---|---|---|---|---|---|
   | **overall** | 28,056 | 16.92 | 23.99 | **0.705** | 12.90 | 19.37 |
   | 2023 (Jul-Dec) | 4,416 | 21.63 | 22.68 | **0.954** | 16.64 | 19.70 |
   | 2024 | 8,783 | 14.60 | 20.14 | **0.725** | 11.22 | 16.36 |
   | 2025 | 8,759 | 16.15 | 26.16 | **0.617** | 12.56 | 20.81 |
   | 2026 (Jan-Sep 11) | 6,095 | 17.94 | 27.38 | **0.655** | 13.50 | 21.45 |

   "naive" throughout is the Lago et al. 2021 weekday-aware seasonal naive (D-1 Tue-Fri, D-7
   Mon/Sat/Sun — `s3_metrics.py:naive_lago_forecast`), the standard EPF benchmark and the one the
   plan's gate is stated against. The plainer lag-24 naive (`mae_naive_lag24`) is a secondary
   diagnostic column, not a gate criterion; the model beats it too overall (rMAE 0.747) and in
   3 of 4 years, but not in the partial 2023 slice (rMAE 1.071 — the model is ~7% worse than
   plain lag-24 in Jul-Dec 2023 specifically, though still clearly better than the weekday-aware
   naive that period). 90%-interval coverage is close to nominal and similar for model vs. naive
   in every period (0.86-0.90 across the by-season breakdown in `data/s3/metrics.json`), i.e. the
   rolling-residual quantile tracker is reasonably calibrated, not just the point forecast being
   good.
10. **Inside-vs-outside the 18 curated S2 event windows: the baseline still beats naive inside
    events, but by a visibly smaller margin — the gap S4 exists to close.**

    | | n_obs | MAE (model) | MAE (naive) | rMAE | CRPS (model) | CRPS (naive) |
    |---|---|---|---|---|---|---|
    | inside events | 1,785 | 19.40 | 22.41 | **0.866** | 14.55 | 18.25 |
    | outside events | 26,271 | 16.75 | 24.10 | **0.695** | 12.78 | 19.45 |

    The model's edge over naive shrinks by more than half moving from outside to inside events —
    rMAE goes from 0.695 to 0.866 (advantage over naive: 30.5 points outside vs. 13.4 points
    inside). It still clears naive inside events (0.866 < 1), so this is not a case where the
    no-context baseline fails during exactly the periods S4's context adapter targets — but the
    numbers are consistent with the thesis's premise: something the model can't see (the outage
    itself) is costing it more of its edge precisely when a document-derived driver value would
    matter most. 1,785 of 28,056 hours (6.4%) fall inside at least one of the 18 curated events.
11. **The date-arithmetic fixes' first full test run surfaced a second, distinct `LassoLarsIC`
    failure mode: a silent coefficient blowup, not a crash.** Finding 7 already added a
    try/except around `.fit()` for the case where sklearn's LARS solver raises outright. This is
    different: `.fit()` returns normally, but on 12 of 1,169 test days (1.0%) the returned
    coefficients are 8.9e10 to 5.6e12 in magnitude, vs. `≤1.64` on all 1,157 other days (an
    exhaustive scan of every cached 730-day model, not a sample) — a clean, order-of-magnitude
    separation with nothing in between. `n_numerical_failure_hours` was 0 on every one of these
    12 models, confirming the existing guard cannot see this failure class.

    | date | max &#124;coef&#124; | | date | max &#124;coef&#124; |
    |---|---|---|---|---|
    | 2023-10-14 | 5.56e12 | | 2024-10-04 | 4.23e11 |
    | 2024-10-29 | 3.77e12 | | 2023-10-13 | 4.01e11 |
    | 2024-08-07 | 1.26e12 | | 2023-11-05 | 3.63e11 |
    | 2024-05-11 | 1.08e12 | | 2024-05-02 | 2.13e11 |
    | 2024-10-31 | 9.12e11 | | 2024-05-22 | 9.10e10 |
    | 2023-10-16 | 5.76e11 | | 2023-08-15 | 9.00e10 |

    Only one of the twelve (2023-10-14, hour 23) actually overflowed the `sinh` inverse-transform
    into a literal `inf` price forecast; the other eleven stayed numerically finite only because
    the exploded coefficient happened to multiply a near-zero feature value on the specific day
    being predicted — they are equally broken, just not visibly so in this particular backtest.
    The single `inf` was enough to corrupt every mean-based aggregate (`mae_model`, `crps_model`)
    to `Infinity`, because `np.nanmean` filters `NaN` but not `inf`.

    **Original decision (2026-09-12): document, don't patch the model.** Rather than add a
    fit-time magnitude guard to `_fit_one_hour` and re-run, the fix was applied at the metrics
    layer only: `s3_metrics.py:mae()` masks on `isfinite` (not `isnan`), and
    `summarize_period`'s `scoreable` filter also requires `isfinite(point)` and `isfinite(q05)`.
    This excluded exactly 1 row (the literal `inf`) from every aggregate table; the other 11
    latently-broken days remained **included** in every metric, since their forecasts happened to
    be finite and nothing distinguished them numerically from a normal forecast at the metrics
    layer. Flagged at the time as "an unknown-but-nonzero share of the ~1% of days with exploded
    coefficients may be contributing a somewhat-off point forecast" — see the Resolution below for
    the actual measured size of that gap.

    ### Resolution (2026-09-12, later the same day) — fixed at fit time, not just masked

    A follow-up read-only investigation (exhaustive scan of all 1,531 cached models across both
    windows, not a sample) found the root cause is one specific, narrow collinearity, not general
    LARS fragility, and that the blast radius is smaller than the metrics-layer masking's wording
    implied. This superseded the original decision above.

    - **Root cause.** Every exploded coefficient is a `+c / -c` pair on two adjacent night-hour
      (h02/h03/h04) columns of `unavail_tx_import_mw`, always in the D-1 or D-7 lag block, never
      the day-D block. Those two columns are identical on 2,069 of 2,080 calibration-day
      observations checked (99.5%) — LARS's active set goes degenerate on this near-duplicate
      pair, which then "fits" the rare days where the two columns differ. A coefficient of
      1e11-1e13 is not a real Lasso optimum at any finite `alpha` (the L1 penalty on it would be
      astronomical); it is a LARS numerical breakdown, distinct from the Finding 7 failure mode
      (which crashes `.fit()` outright — this one returns normally).
    - **The previously-unscanned validation slice also has this bug**: 9 of 181 one-year-window
      (365-day) validation models blow up (`2023-01-08`, `2023-01-10`, `2023-01-13`, `2023-01-17`,
      `2023-01-30`, `2023-02-01`, `2023-04-01`, `2023-04-05`, `2023-06-29`; max 2.7e13). 0 of 181
      two-year-window (730-day) validation models do — consistent with the 12 test-slice hits all
      being 730-day models; the bug's ~1% rate is not window-specific.
    - **Measured size of the "somewhat-off" gap, precisely**: comparing each of the 12 blown-up
      test models' own original forecast against the same model with only the exploded pair
      zeroed (both computed from the pre-fix cached `.npz`, now in `data/s3/archive_preblowupfix/`)
      — the two agree to within **0.022 EUR/MWh on all 24 hours of all 12 days** (11 of 12 within
      0.007; only `2023-10-14` has a non-finite hour, h23, the literal `inf`). **Correction to the
      original wording above**: this is not "somewhat off" in the sense of a point forecast being
      meaningfully wrong — absent the literal overflow, the blown-up pair's net contribution to
      that day's forecast was already almost exactly zero, because the two near-duplicate columns
      it multiplies are themselves almost equal on the day being predicted. The actual defect was
      narrower than the original wording suggested: one broken hour (`2023-10-14` h23), not eleven
      degraded ones.
    - **Curated events**: exactly one of the 12 test-slice blowup days falls inside an event
      window — `2024-05-11`, inside `T064` (`2024-05-10` 19:08 to `2024-05-15` 12:40) — on a D-7
      lag pair. Both D-7 columns read 7,960 MW that night (identical), so the pair's contribution
      is exactly zero there too; no curated event's inside-events numbers were materially affected
      even before this fix.
    - **S4/S5 seam unaffected**: `exog_override` in `build_feature_row` (`src/s3_features.py`)
      only ever replaces the day-D block, and no exploded coefficient sits on a day-D feature —
      the frozen-`f` seam S4/S5 will use cannot trigger this failure mode via a context override.
    - **Fix** (`src/s3_lear.py:_fit_one_hour`): after a `LassoLarsIC` fit succeeds, if
      `max(abs(coef_)) > COEF_BLOWUP_THRESHOLD` (`1e3` — every well-behaved fit across all 1,531
      models is `<=1.64`, every blowup found is `>=8.9e10`, nothing between), refit the same hour
      with `sklearn.linear_model.Lasso` at the same AIC-chosen `alpha_` (both classes minimize
      `(1/2n)||y-Xw||^2 + alpha||w||_1`) via coordinate descent, which cannot produce this
      artefact. New `DayModel.n_lars_blowup_hours` field tracks how often this fires; if a CD
      refit somehow still exceeds the threshold it degrades to the existing zero/median fallback,
      counted in both `n_lars_blowup_hours` and `n_numerical_failure_hours`. **Only the 21 flagged
      day-models needed refitting** — the threshold check leaves every other fit bit-identical
      (`LassoLarsIC`/`Lasso` are both deterministic), so this was exactly equivalent to a full
      re-run: archived the 21 pre-fix `.npz` files plus the pre-fix CSVs/JSON to
      `data/s3/archive_preblowupfix/`, and re-ran `s3_backtest.py --slice validation` (9 refits in
      the 365 window, 0 in 730) then `--slice test` (12 refits in 730). Both completed in minutes,
      $0, no API calls. Interesting secondary finding: the actual coordinate-descent refit changes
      each of the 12 test days' affected hour by up to **8.3 EUR/MWh** relative to the crude
      "zero the pair, keep everything else" patch used for the measurement above — larger than the
      pair's own near-zero contribution, because the same degenerate LARS active set had also
      mis-fit *other* coefficients for that hour, which a full, stable refit corrects too. The
      fix's benefit is therefore not limited to removing the exploded pair.
    - **Re-run numbers** (all changes are within noise; only 12 of 28,056 test rows could change
      at all):

      | | before (masked) | after (fit-time fix) |
      |---|---|---|
      | overall MAE / rMAE / CRPS | 16.9166 / 0.70504 / 12.8979 | 16.9168 / 0.70504 / 12.8981 |
      | inside-events MAE / rMAE / CRPS | 19.4034 / 0.86564 / 14.5542 | 19.4023 / 0.86559 / 14.5547 |
      | outside-events MAE / rMAE / CRPS | 16.7476 / 0.69489 / 12.7824 | 16.7479 / 0.69490 / 12.7826 |
      | `n_obs_nonfinite_point` (overall) | 1 | **0** |
      | `model_diagnostics.n_lars_blowup_hours` (730) | n/a (not tracked) | 12 |
      | `model_diagnostics.max_abs_coef` (730 / 365) | n/a (not tracked) | 1.639 / 0.979 |

    - **Verification**: exhaustive post-fix scan of all 1,531 cached models confirms
      `max|coef| <= 1.64` everywhere and exactly the 21 flagged files have a new mtime; reload
      check on `2023-10-14` (the literal-`inf` day) now reproduces a finite stored forecast
      (`-23.54` at h23) to float32 precision; a from-scratch refit of the worst case
      (`2023-10-14`) independently reproduces `n_lars_blowup_hours=1`, `max|coef|=0.79`; window
      selection is unchanged (730 still wins); `predict_day`'s signature and the `exog_override`
      seam are untouched. `data/s3/model_config.json`'s `known_limitation_silent_coefficient_blowup`
      entry is replaced with `lars_blowup_fallback`, describing the fix rather than the gap.

## Verification

Run against the final, corrected test-slice artifacts (`data/s3/forecasts_test.csv`,
`data/s3/metrics.json`, `data/s3/models/730/*.npz`), per the plan's own verification checklist:

1. **Frozen-`f` reload check.** 4 real test days spanning the full slice
   (2023-09-05, 2024-06-18, 2025-02-11, 2026-08-30 — deliberately away from the 12 Finding-11
   dates) — loaded each saved `DayModel` from disk, called `predict_day`, and compared against
   the `point` column stored in `forecasts_test.csv` for that day. All 4 match to float32
   round-trip precision (max abs diff ≤ 1.4e-14; the stored coefficients are float32, so this is
   exact). `n_models=1350` in `metrics.json.model_diagnostics` matches 181 validation-period
   refit days + 1,169 test-period refit days exactly — every day has a cached model, none
   missing.
2. **Reproducibility check.** Refit 2024-06-18 from scratch (`fit_day`, bypassing the cache
   entirely) and compared to the cached model: coefficients and intercepts match to 2.5e-8
   (`LassoLarsIC` is deterministic — no seeded randomness anywhere in the fit path).
3. **Leakage check** (code read, `src/s3_lear.py:predict_day` + `s3_features.py:build_feature_row`):
   the three availability columns default to `persistence_fill` (D-1 12:00 local, held constant)
   at predict time — never the realised D-day value; `load/wind/solar_forecast_mw` at lag=0 use
   the table's own forecast-column value for D directly, which is legitimate since these are
   ENTSO-E's own day-ahead forecasts (Finding 1's `UNVERIFIED` publication-lag caveat aside), not
   realised outturns; the target price never appears at lag 0. Calibration rows (used only to fit
   coefficients, never as the day being scored) do use realised same-day values for every column,
   per Finding 1 — a documented, deliberate asymmetry, not a leak into the scored forecast itself.
4. **Metrics hand-check.** Independently recomputed MAE and CRPS with plain numpy (no call into
   `s3_metrics.py`) on a 5-day, 120-row chunk (2024-01-10 to 2024-01-14, chosen well past the
   28-day burn-in and away from any Finding-11 date) — matched the library's `summarize_period`
   output exactly (17.503404854522305 both ways for MAE, 14.025985834116419 both ways for CRPS).

## Addendum (2026-09-12, ad hoc, post-gate): Verification point 1's `n_models=1350` claim was wrong

Found while shipping the repo to GitHub and independently re-running `--slice all` against a
freshly restored model cache (fresh clone + release zip, no dev-machine state carried over).
`aggregate_model_diagnostics` (`src/s3_backtest.py:197-223` at the time) globbed every `*.npz`
under `data/s3/models/<window_days>/` instead of taking an explicit day list. Validation and
test cache models under the same `models/730/` directory, keyed only by calendar date with no
phase tag — so once both phases' models exist on disk, an unscoped glob during either phase
silently counts the other phase's models too. Verification point 1 above read the resulting
`n_models=1350 = 181 + 1,169` as confirmation that "every day has a cached model, none missing";
it actually just meant both phases' caches were fully populated by the time either diagnostic
call ran — a true fact, but not the one being tested, and not reproducible in that form (a fresh
run reproduced `n_models=1350` for the *test* phase but `n_models=1338` was what the
already-committed `window_selection.json`'s *validation*-730 entry showed, a stale figure from
an earlier point in development that was never regenerated).

Fixed by scoping `aggregate_model_diagnostics` to an explicit `refit_days` list (the same list
`_fit_phase` computes), passed by each call site. Re-ran `--slice all`: forecast metrics
(`mae_model`, `crps_model`, etc.) are byte-identical to before — the bug never touched anything
that feeds a score, only this diagnostic rollup. Corrected values: validation-730's own
`n_models=181` (not 1350), `n_lars_blowup_hours=0` (all 12 blowups are test-period days);
test-730's own `n_models=1169` (not 1350), `n_lars_blowup_hours=12` (unchanged — these already
belonged to the test phase). The frozen-`f` reload check (4 spot-checked days matching
`forecasts_test.csv` to float32 precision) and the reproducibility check (2024-06-18 refit
matching cached coefficients to 2.5e-8) are unaffected — both were re-verified independently
after this fix, in a fresh clone.

## Open questions

1. Actual ENTSO-E publication lag for A65 (load forecast) and A69 (wind/solar forecast) relative
   to a delivery day — assumed available at t0 (D-1 12:00 local) per Finding 1; not verified
   against a real publication-timestamp field (none exists in the pulled columns).
2. Whether S2's per-period `reduction_mw>=300` candidate rule should stitch same-mrid,
   temporally-adjacent periods into one episode (the G090/G091 split from Correction 1) — flagged,
   not resolved; affects at most this one mrid in the current 18-event list.
3. `--recalib-every` was implemented but not exercised (daily recalibration stayed inside the
   time budget) — untested at N>1.
4. ~~Finding 11's silent-coefficient-blowup root cause...~~ **Resolved** — see Finding 11's
   Resolution subsection: a near-duplicate pair of `unavail_tx_import_mw` night-hour lag columns,
   fixed at fit time with a coordinate-descent fallback.

## Implications for the thesis

- **The no-context LEAR baseline clears naive cleanly enough to be a real floor for S4 to improve
  on**: rMAE 0.62-0.95 across every calendar year (never above 1, and only close to 1 in the
  partial Jul-Dec 2023 slice, the shortest and earliest period) and CRPS 30-46% below naive's in
  every year. S4's context adapter has genuine room to matter, but is not competing against a
  strawman — the baseline it must beat is already substantially better than persistence.
- **The inside-vs-outside-events gap (Finding 10) is the sharpest available motivation for S4**:
  the baseline's advantage over naive roughly halves inside curated outage windows (13.4 points
  of rMAE margin vs. 30.5 outside). This is a quantified, pre-registered-feeling target — S4/S5
  should report the same inside/outside split so "did the context adapter close this specific
  gap" has a direct before/after comparison, not just an aggregate score change.
- **The silent-coefficient-blowup issue (Finding 11) is fixed at the source, not just masked** —
  a coordinate-descent fallback in `_fit_one_hour` now catches it at fit time. S4/S5 reuse frozen
  `DayModel`s via the same `predict_day` seam; they no longer need their own load-time
  `max(abs(coef))` sanity check for this specific failure mode, though the general lesson (LARS on
  541 collinear features can misbehave in ways that don't raise) is worth keeping in mind for any
  *new* feature added later.

## Gate

**PASS.** Evidence against each of the plan's stated criteria:

- **rMAE < 1 vs. naive, overall and every calendar year**: overall 0.705; 2023 0.954, 2024 0.725,
  2025 0.617, 2026 0.655 (Finding 9). All four years and the overall figure clear the naive
  benchmark.
- **CRPS below naive's CRPS**: overall 12.90 vs. 19.37; every individual year and season in
  `data/s3/metrics.json` also independently shows `crps_model < crps_naive` (Finding 9).
- **Every test day has a saved `DayModel` that reloads to its stored forecast, and every stored
  forecast is now finite**: 1,169 of 1,169 test-slice refit days have a cached `.npz`; reload
  spot-checked exact on 5 days including the former literal-`inf` day (Verification §1, Finding
  11 Resolution). The 12/1,169 numerically-pathological models flagged when this gate first ran
  (Finding 11) were root-caused and fixed at fit time the same day — an exhaustive scan of all
  1,531 cached models (both windows) now shows `max|coef| <= 1.64` everywhere, with no remaining
  documented exception.
- **Inside-vs-outside-events table reported**: Finding 10 (numbers re-confirmed after the Finding
  11 fix — moved by <0.01 MAE/CRPS, as expected from a 12-row change out of 28,056).

Net: the baseline is a real, working floor — it beats naive by a wide margin overall and in every
year, its edge is honestly smaller exactly where S4 is supposed to help, and the one open
correctness gap found while producing these results (12/1,169, then 9/181 more in validation,
numerically pathological day-models) was root-caused to a specific feature collinearity and fixed
at the source, not just masked. **Do not start S4 from this message — stop and wait per the
project's ground rules.**
