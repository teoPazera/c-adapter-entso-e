# S1 — Pull the full numerical history

Date: 2026-09-11 | Repo: `C_adapter_Entso_E`, single-repo project (no upstream clones)

**Update 2026-09-11:** the two gate failures below (NTC, `avail_gen_mw` full-window coverage)
are resolved in `notes/01b-gate-close.md` — re-evaluated gate: PASS.

## Question

Pull SE3 day-ahead price plus four drivers (day-ahead load forecast, day-ahead wind/solar
forecast, available generation capacity, day-ahead NTC on the main interconnectors) for
2023-01-01 through present, hourly, and report every gap, frequency change, and missing driver
— per the approved S1 plan (planner-written, executed here) and `Claude.MD` S1.

## What I did

1. Extracted `_redact`, `_redact_fields`, `_infer_freq`, `_stringify_keys` out of `s0_probe.py`
   into `src/common.py`, added `make_client()` and `throttled()` per the plan. Re-ran
   `uv run python src/s0_probe.py` to confirm it still works unchanged after the refactor:
   9.0s, all 8 ENTSO-E + 1 UMM calls succeeded (`data/s0/run_summary.json`, only
   `_meta.elapsed_seconds` differs from the S0 run).
2. Added one thing the plan didn't specify: `common.CountingSession`
   (`src/common.py:50`), a `requests.Session` subclass passed into
   `EntsoePandasClient(session=...)` that counts every *real* HTTP GET. This matters because
   I verified by reading `entsoe/decorators.py` and `entsoe/entsoe.py` (2026-09-11, installed
   `entsoe-py==0.8.1`) that a single client call can fan out into several real HTTP requests
   internally — `query_day_ahead_prices` and the A80/A77 unavailability endpoints stack
   `@year_limited`/`@month_limited` with `@documents_limited(100|200)`, which pages in blocks
   of 100–200 documents per year — so counting our own top-level calls would have understated
   the true request count against the pre-approved 180–260 budget.
3. Wrote `src/s1_pull_history.py`: idempotent per-chunk pull, one file per
   `(series, chunk)` under `data/raw/s1/`, `.csv` on success, `.empty` on
   `NoMatchingDataError`, `.error.json` (redacted) on any other exception — a chunk already on
   disk is skipped on re-run, regardless of which outcome it recorded.
4. **First run**: 264.1s, 199 real HTTP requests. Price (4 chunks), load forecast and
   wind/solar forecast (45 chunks each), installed capacity type/per-unit (4 each), A80/A77
   outages (4 each), hydro reservoir (4) all succeeded. Two things did not, and I diagnosed
   both live before writing either up as a plan-following result:
   - **All 10 NTC probes came back empty**, not just the 2 purely-internal SE3–SE2/SE3–SE4
     borders the plan flagged as `UNVERIFIED`. Checked this was real, not a bug: the identical
     `query_net_transfer_capacity_dayahead` call against `NL→BE` on the same day returns 24
     real points, but the same call against an unrelated non-Nordic pair, `DE_LU→FR`, is
     *also* empty — consistent with SE3 (and other flow-based-coupled borders) simply not
     publishing the classic explicit day-ahead NTC document (A61/A01) at all, not a code or
     parameter bug. See Finding 5.
   - **All 10 transmission-outage (A78) pulls failed with HTTP 400.** The plan's own table
     assumed `entsoe-py`'s `@paginated` decorator would auto-bisect an oversized query for this
     endpoint. Verified false: `query_unavailability_transmission` is decorated only with
     `@paginated` (no `@year_limited`), and a live ~4-year request returns a 400 whose error
     text doesn't match either substring `@paginated`'s `PaginationError` check requires
     (`entsoe/decorators.py:121,128`), so the exception propagates unbisected instead of
     triggering a bisect-and-retry. Confirmed the fix directly: a 1-year window for the same
     pair succeeds (56 rows). Fixed `transmission_outages()` (`src/s1_pull_history.py:252`) to
     year-chunk these calls ourselves, like every other endpoint.
5. **Second run** (after the fix): 77.4s, 40 more real HTTP requests, all succeeded — 9 of 10
   border-pairs returned real transmission-outage data. **Total across both runs: 239 real
   HTTP requests**, inside the pre-approved 180–260 budget (the ~178-top-level-call estimate
   undercounted true requests by the amount flagged in step 2, as expected).
6. Wrote `src/s1_build_table.py`: loads every cached chunk, aligns to an explicit
   `pd.date_range(freq="h", tz="Europe/Stockholm", inclusive="left")` index (DST-safe by
   construction), builds `avail_gen_mw` via a sweep-line over outage intervals, and writes
   `data/s1/se3_hourly.csv` plus a full per-column coverage report. Building this surfaced
   three real bugs, all caught by actually running it against the real pulled data rather than
   trusting the design on paper — see Findings 3, 6, 9.
7. Verified idempotency: a third `s1_pull_history.py` run made **0** real HTTP requests in
   0.0s (reported)/0.85s (wall, incl. Python startup). Verified no credential leak: `git grep`
   and a plain recursive `grep` (which also covers the gitignored `data/raw/s1/` cache) for the
   API key's first 8 characters both return zero matches outside `.env`.

## Findings

1. **The hourly table exists and is well-formed**: `data/s1/se3_hourly.csv`, 32,399 rows,
   2023-01-01 00:00+01:00 → 2026-09-11 23:00+02:00, 19 columns, 0 duplicate timestamps
   (`data/s1/coverage_report.json:502`). 3 of the plan's 4 drivers (load forecast, wind/solar
   forecast, and — with a major caveat — available capacity) are present; NTC is not (Finding
   5). `data/s1/coverage_report.json:310-501` has the per-column breakdown behind every number
   below.

2. **Day-ahead price**: 99.99% coverage (32,397/32,399; longest gap 1 hour,
   `data/s1/coverage_report.json:21-30`). Switches from 60-min to 15-min resolution at exactly
   **2025-10-01 00:00 local** (`coverage_report.json:11`) — confirms S0 Finding 2 precisely.
   1,491 negative hourly-mean price hours, kept (not dropped) per the plan's instruction —
   real, not a data-quality problem in SE3.

3. **Load forecast and wind/solar forecast also switch to 15-minute resolution — later than
   price, and later than S0's 2-day sample could show.** Load forecast switches at
   **2025-12-02 00:00 local** (`coverage_report.json:37`); wind/solar switches at
   **2025-11-25 00:00 local** (`coverage_report.json:68`) — both series switch on different
   dates from each other and from price, roughly two months after it. This corrects the S0 note
   (`00-data-check.md` Finding 2), which correctly reported both series still hourly on its
   2025-10-15 sample day — that day simply preceded either switch. Coverage: load forecast
   99.93% (24-hour gap exactly on its switch day, 2025-12-04); wind/solar 99.56% (48-hour gap
   2025-11-01→11-02, also at its switch). Both gaps are explained by the transition, not
   unexplained data loss. Caught the DST bug that made this analysis possible at
   `src/s1_build_table.py:118` and `:139` — see Finding 6.

4. **Day-ahead NTC is not available for any of SE3's five borders, in either direction** — not
   just the two purely-internal ones (SE3–SE2, SE3–SE4) the plan flagged as `UNVERIFIED`, but
   also the three external interconnectors (NO1, FI, DK1). All 10 probes empty
   (`coverage_report.json:119-149`), all 10 `ntc_*` columns 100% NaN in the final table
   (`coverage_report.json:351-450`). Verified this is real (§ What I did, step 4): the same
   query against `NL→BE` returns real data; against `DE_LU→FR` it's empty like every SE3 pair.
   **The NTC driver as specified cannot be built for SE3 from this ENTSO-E endpoint.**

5. **Installed generation capacity by production type is only available for 2026, not
   2023–2025.** `coverage_report.json:151-168`: 3 of 4 year-chunks return
   `NoMatchingDataError`; only 2026 returns one row (Hydro Water Reservoir 2,595 MW + Nuclear
   6,800 MW + Other 3,351 MW + Wind Onshore 3,547 MW = 16,293 MW). Verified not a chunking
   artifact by direct single-day per-year probes outside the normal chunk boundaries — 2023,
   2024, 2025 all genuinely return no data from ENTSO-E for this document type.
   **Consequence: `installed_mw` and `avail_gen_mw` — the driver this whole benchmark is
   about — are only populated 2026-01-01 onward: 6,095/32,399 hours, 18.8% of the pulled
   window** (`coverage_report.json:451-460,481-490`). This is the single most consequential
   finding in this stage.

6. **Three real bugs, each only found by running the pipeline against real data:**
   - **DST fall-back ambiguity.** `.floor("h")`/`.resample("1h")` on a `Europe/Stockholm`
     tz-aware index raises `ValueError: Cannot infer dst time from 2023-10-29 02:00:00` — that
     local hour occurs twice when clocks go back. Fixed by bucketing in UTC throughout
     (`src/s1_build_table.py:118` `_resample_hourly`, `:139` `analyze_resolution_switch`); UTC
     and Stockholm hour-bucket boundaries coincide (the offset is always a whole number of
     hours), so this changes nothing about the result, only removes the ambiguity.
   - **`installed_capacity_unit` is indexed by generation-unit EIC, not by timestamp** — a
     unit-registry snapshot (Ringhals 4, Forsmark blocks, …), not a time series. The generic
     datetime-index loader crashed on it (`DateParseError: ... unable to parse:
     46WPU0000000014Y`). Given its own loader (`src/s1_build_table.py:321`), tagged by query
     year since capacity can change between years. Saved to
     `data/s1/installed_capacity_per_unit_se3.csv` (93 rows across 4 years).
   - **The A80/A77 double-counting rule was too coarse and silently deleted a real outage.**
     My first implementation excluded *all* of a unit's A80 rows if that unit appeared
     *anywhere* in A77 ("prefer A77, unit-level"). This is one reasonable reading of the plan's
     "if the same unit appears in both, use A77 as primary" instruction, but it failed the
     plan's own Oskarshamn-3 cross-check (`unavail_nuclear_mw` dropped to 0 inside the known
     outage window). Diagnosed directly: of the 5 units common to both feeds, only 1 has any
     genuine time-overlapping event between the two feeds (8 of 2,983 checked (A80,A77)
     row-pairs, `coverage_report.json:230-233`); the other 4 — including Oskarshamn 3 — report
     entirely disjoint real events. Fixed to an event-level rule
     (`src/s1_build_table.py:387` `_drop_a80_overlapping_a77`): drop only the specific A80 rows
     that genuinely time-overlap an A77 row for the same unit (8 of 164 dropped). Also
     reordered `_prepare_outages` (`src/s1_build_table.py:361`) to pick the latest revision per
     `mrid` *before* filtering cancelled/withdrawn status, not after — the reverse order can
     resurrect a stale pre-cancellation revision when the newest revision is itself the
     cancellation.

7. **Outage revision behaviour, resolved** (plan's flagged `UNVERIFIED` item): entsoe-py
   returns **exactly one row per `mrid`** — 174/174 distinct in A80, 234/234 in A77, zero
   `mrid`s with more than one row across all 408 real rows spanning 3.75 years
   (`coverage_report.json:201-214`), even though individual rows carry revision numbers up to
   16. Not exhaustively provable (no `mrid` recurred to test directly against), but the
   complete absence of any multi-row `mrid` is strong evidence the feed is **latest-revision-
   only**. `docstatus` did take a non-null value in real data — "Cancelled" appears at least
   twice — partially answering S0 Open Question 3; "Withdrawn" was not observed but not
   exhaustively checked.

8. **Installed capacity per unit (2023–2025 available) does not reconcile with installed
   capacity per type (2026-only)**: per-unit 2026 total is 10,456.2 MW (27 units,
   `coverage_report.json:193-196`); per-type 2026 total is 16,293 MW (Finding 5) — a ~5,837 MW
   gap. These are two different ENTSO-E datasets with evidently different coverage/completeness
   criteria, not cross-checkable against each other from what's in this pull. Flagged, not
   resolved — see Open Questions.

9. **Transmission outages (A78): 9 of 10 border-pairs have real data** after the pagination fix
   (Finding on process, § What I did step 4) — row counts from 53 (`se4_se3`) to 305
   (`se2_se3`), saved to `data/s1/transmission_outages_se3.csv`
   (`coverage_report.json:248-298`). One asymmetry: `se3_se2` (SE3→SE2 direction) is empty in
   all 4 years while the reverse `se2_se3` has 305 rows. Reported as observed; not investigated
   further (plausibly just which direction ENTSO-E files the border-element document under, not
   a data gap — the border itself clearly has real outage activity via the reverse direction).

10. **Oskarshamn 3 cross-check: PASS**, after the Finding 6 fix —
    `unavail_nuclear_mw` never drops below 1,400 MW across a safely-interior slice of the known
    2025-03-29 16:33 → 2025-11-02 07:16 UTC outage window (`coverage_report.json:512-513`).
    Note for future stages: the plan's suggested check window ("2025-03-29" to "2025-11-01",
    date-only) is itself imprecise — a date-only slice includes hours on 2025-03-29 *before*
    16:33, when the outage legitimately hadn't started, which reads as 0 and would fail the
    check even with a fully correct pipeline. The implemented check
    (`src/s1_build_table.py` `sanity_checks()`) buffers a full day inside the real window on
    each side instead of using the plan's literal date strings.

11. **All other sanity checks pass** (`coverage_report.json:503-514`): `avail_gen_mw` never
    negative (min 11,409 MW, within its 2026-only valid range); load forecast within
    [5,516, 15,827] MW (inside the expected 5,000–20,000 band); wind onshore within
    [19.3, 3,850.18] MW, never negative.

12. **Cost and size**: 239 real HTTP requests, ~341s combined wall-clock across both runs, $0
    (ENTSO-E API is free; no paid call was made). `data/raw/s1/` (gitignored raw cache) = 6.6MB;
    `data/s1/` (committable outputs) = 3.3MB, `se3_hourly.csv` itself 3.0MB — all within the
    plan's "well under 5MB" / "few MB" expectations. Idempotency verified: a third pull run
    made 0 real HTTP requests in under 1 second. No credential leak anywhere in the tree,
    tracked or gitignored (verified by grep, not assumed).

## Open questions

1. Which installed-capacity source should be `installed_mw` for 2023–2025: leave `avail_gen_mw`
   NaN before 2026 (current, honest default), or substitute the per-unit total (Finding 8) —
   which is available for the full window but ~36% lower than the per-type 2026 figure and of
   unclear completeness? Needs a decision before S3 can pick a backtest window that uses
   `avail_gen_mw`.
2. Whether an alternative ENTSO-E product (e.g. flow-based domain parameters) could substitute
   for the missing day-ahead NTC (Finding 4) — not investigated; would be new scope beyond this
   stage's plan.
3. The `se3_se2`/`se2_se3` transmission-outage asymmetry (Finding 9) — low priority, not
   investigated further.
4. `docstatus` value inventory (S0 Open Question 3) is now partially answered (Cancelled
   observed) but not exhaustively enumerated across all 408 outage rows.

## Implications for S2 and S3

- **S3's usable backtest window is smaller than "2023-01 to present" for any model that wants
  `avail_gen_mw` as a driver from day one**: that column is only reliable from 2026-01-01
  (8.5 months), not the full 3.75 years price/load/wind-solar have. If S3 needs the longer
  window, it must either run without `avail_gen_mw` for 2023–2025, or Open Question 1 gets
  resolved first.
- **NTC is off the table as a driver for S3/S4 as currently specified** (Finding 4) — the
  top-level `Claude.MD` S1 driver list needs revising; the mechanism this benchmark tests
  (reading documents to fill a future-driver slot) should target the availability/outage
  driver, which has rich real data, not NTC, which has none for SE3.
- **S2's event curation should source from the corrected, merged outage set**
  (`data/s1/outages_se3.csv`, tagged `A80`/`A77`), not either feed alone — Finding 6 shows a
  naive single-source or unit-level-merged read can silently miss a real event like Oskarshamn
  3. Good real material exists for the 15–20 curated events: 174 A80 + 234 A77 generation-side
  rows, plus 9 of 10 transmission-outage border-pairs with real data (Finding 9).

## Gate

**FAIL, two of five plan requirements not met** — named precisely, not shrunk or papered over:

- Day-ahead price: **PASS** — exists, hourly, 99.99% coverage, resolution switch verified
  exactly (Finding 2).
- Load forecast: **PASS** — exists, hourly, 99.93% coverage, resolution switch verified, one
  explained 24h gap (Finding 3).
- Wind/solar forecast: **PASS with a named exception** — exists, hourly, 99.56% coverage,
  resolution switch verified, one explained 48h gap (>24h, but named and understood — the
  series' own resolution-switch date, Finding 3).
- **NTC on at least the three external borders: FAIL.** Zero of SE3's five borders — internal
  or external — return day-ahead NTC data (Finding 4). This is not a bug; it appears to be a
  real characteristic of how SE3 (and other flow-based-coupled zones) publish capacity data,
  verified against a working continental pair and a matching-empty continental pair.
- **Available-capacity driver over the full window: FAIL.** `avail_gen_mw` exists as a column
  and is correct where populated (Oskarshamn cross-check passes, never negative), but covers
  only 18.8% of the window (2026 onward), not the full 2023–2026 span (Finding 5).

Per the ground rules, reporting this as-is rather than narrowing the window or dropping NTC
silently to force a clean pass. The note above names every gap, every frequency change, and
both missing/incomplete drivers, with `file:line` evidence for each. Waiting for direction on
Open Question 1 (and whether to accept the NTC gap or investigate an alternative source) before
S2.
