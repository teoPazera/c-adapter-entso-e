## Part B — company device: S4 context adapter design  `[execute after Part A, on the new machine]`

Goal (per `Claude.MD` S4): for each curated event, an LLM reads the UMM document version(s)
actually published before forecast time, emits a *distribution* over the availability driver's
effect, K sampled driver paths are pushed through the frozen S3 `DayModel`s via the existing
`predict_day(..., exog_override=...)` seam, and the resulting forecast distributions are
averaged. Gate: one worked example end to end before running all 18 events.

### B1. Fixed design choices (state them in `notes/04-context-adapter.md`, revise only with reason)
- **Forecast origin.** t0 = D-1 12:00 local, same as S3. Delivery days per event: every local
  calendar day whose 24 h intersect `[realised_start, realised_end]`, plus `post_days` after
  the end (default 0; 3 for `hydro_plausible` events so the "effect outlasts the document"
  hypothesis is testable). Events pre-2023-07-01 (G008, G010, G014, G015, G016, G018) have no
  test-slice model; fit them on demand with `fit_day` (frozen-f rule holds: same code, same
  window) and cache to `data/s3/models/730/`.
- **Document availability.** For each matched thread, the latest version with
  `publicationDate <= t0`. Days with no version yet (unplanned events announced after t0,
  02-events open question 2) are the built-in no-information control: the context condition
  must reduce to the baseline by construction, and the note reports how many event-days that
  is. Full revision history up to t0 is passed, not just the latest (02-events Implication 3).
- **Driver mapping.** generation events -> `avail_gen_mw` (subtract effect); the four
  transmission events are all FI->SE3 imports -> `unavail_tx_import_mw` (add effect). Only the
  day-D block is overridden; D-1/D-7 lags stay realised, exactly as the baseline.
- **Effect distribution the LLM must return** (strict JSON, validated): `p_active` (prob. the
  unit is out at all on D), `start_hour` and `end_hour` on D as discrete distributions or
  {mean, sd} clipped to [0, 24], `size_mw` as {p10, p50, p90}, `direction` in {reduce,
  none}. K = 100 paths sampled from it; each path is the persistence fill with the effect
  applied over its sampled active hours.
- **Aggregation.** Point = mean over K path forecasts. Quantiles = mixture of the K
  per-path distributions, each = path point + the S3 residual quantile offsets for that
  (day, hour) taken from `forecasts_test.csv` (`q_tau - point`). Report both the mixture and
  the point-plus-offsets variant so S5 can show whether the path spread matters.
- **Conditions produced per event-day** (S5 consumes all three): `no_context` (persistence
  fill, identical to `forecasts_test.csv`), `context` (above), `oracle` (`day_hour_profile`
  of the realised driver for D as the override).
- **Cost discipline.** One LLM call per (event, delivery day), cached to
  `data/s4/llm_cache/<sha256(prompt)>.json` so reruns are $0 and paths are re-sampleable
  under a fixed seed. Estimate before the first real call: ~18 events x ~4 days x ~3k
  tokens ~ 250k tokens, expected under $2 on any mid-tier model; ask before running.

### B2. Files
- `src/llm_client.py` — `Protocol` with `complete_json(system, user) -> dict`; `MockClient`
  (returns a fixed valid effect JSON so the whole pipeline runs at $0); one real adapter for
  the provider chosen there, selected by env var. If Anthropic: load the `claude-api` skill
  first for model IDs and pricing.
- `src/s4_context.py` — event/day enumeration, version selection at t0, prompt rendering from
  the already-rendered `data/s2/documents/<event_id>/*_v<k>.txt` files, JSON schema
  validation, cache.
- `src/s4_paths.py` — effect distribution -> K driver paths (seeded), persistence-fill base.
- `src/s4_run.py` — `--event G074 --day 2025-02-06` worked example; `--all` for the 18
  events; writes `data/s4/forecasts_s4.csv` (one row per event x day x hour x condition x
  variant) and `data/s4/run_summary.json` (calls, tokens, cost, cache hits).
- `notes/04-context-adapter.md` — worked example with the prompt, the returned JSON, the
  sampled paths, and the three forecasts side by side; then the full run.

### B3. Gate for S4
The G074 (Forsmark 3, 2025-02-05..08, 5 versions incl. a revision the day before) worked
example runs end to end: document selection picks version 1 for D=2025-02-06 and version 2
for D=2025-02-07 (publication timestamps in `events.json`), the effect JSON validates, K paths
reproduce under the seed, and the three conditions' forecasts differ in the expected direction
(context between no-context and oracle). Stop and report before `--all`.

## Verification (Part B)
- `MockClient` run of `--all` completes with $0 and produces the full CSV shape.
- `no_context` rows equal `forecasts_test.csv` rows for the same (day, hour) to 1e-9.
- `oracle` rows for a non-event day (override = persistence fill) equal `no_context`.
- Cache: second real run reports 0 new calls.