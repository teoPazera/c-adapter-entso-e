# S1b — Close the S1 gate

Date: 2026-09-11 | Repo: `C_adapter_Entso_E`, single-repo project (no upstream clones)

## Question

S1's gate failed on two of five items: no day-ahead NTC for any SE3 border, and `avail_gen_mw`
only populated from 2026-01-01 (installed capacity per type absent for 2023–2025). Per the
user's 2026-09-11 decisions, replace `installed_mw` with the per-unit registry (≥100 MW), and
replace NTC with an hourly transmission-availability driver from A78 outages. Re-evaluate the
S1 gate.

## What I did

1. Verified the plan's zone codes against installed `entsoe-py==0.8.1` before using them:
   `SE_1`, `SE_2`, `SE_4` and country-level `SE` (`10YSE-1--------K`) all match
   (`.venv/Lib/site-packages/entsoe/mappings.py:137-146`).
2. Added a second NTC probe day, `2024-06-15` (well before the plan's stated 2024-10-30
   flow-based go-live), with its own series key `ntc_prefb_probe_<pair>`
   (`src/s1_pull_history.py:274`), and the A68 diagnostic probe across `SE_1`/`SE_2`/`SE_4`/`SE`
   for 2023–2025 (`src/s1_pull_history.py:293`). Ran the pull: **22 real HTTP requests**, 43.4s,
   $0 — under the ≤42 budget (all 10 pre-FB year-pulls were skipped since every pre-FB probe
   also came back empty, so the actual spend was 10 probes + 12 A68 probes = 22, not the
   worst-case ≤20 extra for year-pulls the budget allowed for). Idempotency and the
   API-key-leak check both re-verified clean after this run (second `s1_pull_history.py` run:
   0 real requests; full-tree grep for the key's first 8 characters outside `.env`: 0 matches).
3. Rewrote `installed_mw` to come from `build_installed_registry()`
   (`src/s1_build_table.py:390`): per-unit registry chunks, filtered to `Installed Capacity [MW]
   >= 100`, summed per query year. `build_installed_capacity()` (the old per-type function)
   still runs and still writes `installed_capacity_se3.csv`, but its output no longer reaches
   the table.
4. Extended `build_transmission_outages()` (`src/s1_build_table.py`) to run `_prepare_outages`
   per pair (reused unmodified — it never referenced A80/A77-specific columns), report an
   mrid-multiplicity check, and tag every raw row in `transmission_outages_se3.csv` with a
   `kept` flag (matched on the exact `(mrid, revision)` pair that survived preparation, not just
   "mrid appears at all" — a mrid can have an old revision dropped and a newer one kept).
5. Built the transmission-availability driver (`build_transmission_availability`,
   `src/s1_build_table.py:683`) via a new min-aggregation sweep (`_min_active_value`,
   `src/s1_build_table.py:514`) — deliberately **min**, not sum, across simultaneously-active
   A78 rows on one border, because overlapping rows there describe the same underlying capacity
   constraint, unlike generation outages on different physical units
   (`_sweep_line_unavailable_mw`), which are genuinely additive.
6. Built the NTC-vs-derived-availability validation (`build_ntc_validation`,
   `src/s1_build_table.py:736`) and the plan's named cross-check
   (`transmission_cross_check`, `src/s1_build_table.py:795`).
7. Caught and fixed one bug while running this against real data: `.dt.tz_convert(TZ).year`
   raised `AttributeError` — `.year` needs another `.dt` after `.tz_convert()` returns a Series,
   not a DatetimeIndex. Fixed to `.dt.tz_convert(TZ).dt.year` (`_registry_reconciliation`,
   `src/s1_build_table.py:382`).
8. Verified the plan's own pre-computed numbers against the rebuilt data rather than assuming
   them: `transmission_outages_se3.csv` now has exactly **1,580 rows, 1,200 kept, 380 dropped**
   — the plan's stated "380 rows are `docstatus == 'Cancelled'`" figure matches exactly.

## Findings

1. **The plan's flow-based-coupling explanation for the missing NTC does not hold up against
   direct testing.** All 10 pre-flow-based probes (2024-06-15, months before the stated
   2024-10-30 go-live) are also empty (`data/s1/coverage_report.json:151-163`,
   `ntc_probe_alive_prefb`, every pair `false`). If day-ahead NTC (A61/A01) had simply stopped
   being published at flow-based go-live, a June-2024 probe should have found real pre-switch
   data — it didn't. **Correction to the plan, stated as such rather than silently accepted**:
   the *why* SE3 never publishes this document type is still open (§ Open Questions); the
   flow-based-coupling date is not it, or not the whole story. `data/s1/ntc_prefb_se3.csv` was
   **not created** — no pair had any pre-FB data to put in it
   (`data/s1/coverage_report.json:151-163`, `file_written: false`).

2. **The A68 gap is bidding-zone-specific, not a Swedish-TSO-wide reporting gap.** The
   diagnostic probe (`data/raw/s1/a68_probe_*/`) found `SE_1`, `SE_2`, `SE_4` all empty for
   2023–2025 — consistent with SE3's own gap (S1 Finding 5) — but the **country-level** zone
   `SE` returns real data for all three years: 2023 (Hydro 16,300 + Nuclear 6,900 + Other 9,000
   + Wind 14,700 MW), 2024 (+ Solar 3,200, Other drops to 6,600, Wind 16,700), 2025 (Solar 5,000,
   Wind 18,000). Not used in the table (per the plan, diagnostic only) — flagged as a real,
   previously-unknown resource for future reference (§ Open Questions).

3. **The ≥100 MW registry threshold reconciles almost perfectly with the outage feed** —
   verified, not assumed (`data/s1/coverage_report.json:289-303`): of 366 generation-outage
   rows, only 1 references a `production_resource_id` not found in that year's filtered
   registry, and that one exception is explained, not a real mismatch — it's Ringhals 4
   (`46WPU0000000014Y`, a 1,134 MW unit, confirmed well above the threshold) in an outage
   starting in **2022**, a year before the registry was ever queried (2023–2026 only), not a
   capacity-threshold miss. **`installed_mw` now has zero NaN across the full 32,399-row window**
   (`coverage_report.json` `sanity_checks.installed_mw_fully_populated_full_window: true`) —
   the registry gives 21–23 units kept per year (2–4 dropped below 100 MW), totals 9,330–10,199
   MW (`coverage_report.json:265-288`), versus the old per-type-only 2026 figure of 16,293 MW
   (S1 Finding 5/8) — these describe different, non-reconcilable fleets, as S1 Finding 8 already
   flagged; the registry fleet is the one that matches the outage data.

4. **`avail_gen_mw` is now populated and non-negative across the full window.** Min 5,315 MW at
   2026-06-09 08:00 (`coverage_report.json` `sanity_checks.avail_gen_mw_min_timestamp`) —
   internally consistent with S1's own max `unavail_mw` of 4,884 MW and 2026's registry total of
   10,199 MW (10,199 − 4,884 = 5,315, exact). Oskarshamn 3 cross-check still passes unchanged
   (1,400 MW floor maintained, `coverage_report.json` `sanity_checks`).

5. **Transmission-availability driver built for 9 of 10 pairs** (`se3_se2` excluded per S1
   Finding 9 — zero rows in either S1 or this run) — `coverage_report.json:379-456`. The
   `nominal_mw_proxy` (max observed `avail_qty` per pair) values are plausible against known
   real Nordic interconnector capacities, for what that informal check is worth (not a
   validated source): `se3_dk1`/`dk1_se3` 715 MW, `se3_fi`/`fi_se3` 1,200 MW, `se3_no1` 2,095 MW
   / `no1_se3` 2,145 MW, `se3_se4` 6,000 MW / `se4_se3` 2,500 MW, `se2_se3` 7,300 MW. All 9
   per-pair columns plus both aggregates (`unavail_tx_import_mw`, `unavail_tx_export_mw`) have
   zero NaN across the full window.

6. **NTC-vs-driver validation: not computable for any of the 6 external pairs**
   (`coverage_report.json:457-482`) — direct consequence of Finding 1. Reported explicitly per
   pair (`"prefb_ntc_available": false`), not silently skipped.

7. **The plan's named cross-check found a real 912-hour unplanned outage on `fi_se3`**
   (`mrid Js0C78G3BJcNPCeDyq2wHg`, 2026-05-24 22:00 → 2026-07-01 22:00 UTC, `avail_qty=300`,
   `nominal_mw_proxy=1200` → expected 900 MW unavailable) and confirmed the driver column
   matches exactly for 678 of 912 hours (`coverage_report.json:483-497`). **Verified, not just
   asserted, that the other 234 hours are correct too**: directly inspected the 8 other A78 rows
   overlapping that window and found several `Planned maintenance` rows with `avail_qty=0`
   (fully unavailable) active 2026-06-22 through 2026-07-01 — exactly the span covering the 234
   mismatched hours. The driver correctly reports the *more* restrictive concurrent state
   (1,200 MW unavailable, not 900) during those hours — the min-aggregation rule working
   as designed, not a bug.

## Open questions

1. **Why does SE3 never publish day-ahead NTC (A61/A01), at any point 2023–2026?** The plan's
   flow-based-coupling hypothesis is refuted by Finding 1. Not investigated further here — would
   need either contacting Svenska kraftnät/ENTSO-E, or trying a different document type (e.g.
   flow-based domain parameters), both new scope.
2. The country-level Swedish A68 data (Finding 2) is a real, available resource not currently
   used anywhere — worth keeping in mind if a Sweden-wide (not SE3-only) capacity figure is ever
   useful, though it would need scaling/allocation logic to attribute to SE3 specifically.
3. `nominal_mw_proxy` is derived entirely from the outage feed's own reported `avail_qty` values,
   not validated against any authoritative interconnector-capacity source. The informal
   plausibility check in Finding 5 is not a substitute for that.

## Proposed `Claude.MD` driver-sentence update

Not edited directly, per the ground rules — proposed here for the user to accept or reject in
`C_adapter_Entso_E/Claude.MD` S1: replace *"available generation capacity (installed minus
declared unavailable), day-ahead NTC on the main interconnectors"* with *"available generation
capacity (installed minus declared unavailable, from the per-unit registry restricted to units
≥100 MW), available transmission capacity per border (nominal capacity proxy minus declared
unavailable, from A78 outage reports — day-ahead NTC is not published for any SE3 border,
S1 Finding 4 / S1b Finding 1)."*

## Implications for S2 and S3

- **Both S1 gate failures are resolved with real, verified drivers**, not by narrowing the
  window: `avail_gen_mw` and all `unavail_tx_*` columns now cover the full 2023-01-01 to
  2026-09-11 window with zero gaps. S3's backtest window is no longer constrained by driver
  availability the way S1 left it.
- **The transmission driver is a proxy, and S2/S4 should treat it as such**: `nominal_mw_proxy`
  is inferred from the outage feed itself, not an independent capacity figure, because no real
  NTC exists to calibrate against (Open Question 3). Any S4 result that leans on
  `unavail_tx_*` accuracy should flag this as an unvalidated proxy, not a measured capacity.
- **S2's event curation now has two additional real, right-sized event sources**: the 912-hour
  `fi_se3` unplanned outage (Finding 7) is exactly the kind of long, clean, single-cause event
  the curation criteria want, and the 9-pair `unavail_tx_*`/`transmission_outages_se3.csv`
  (now with the `kept` flag) gives a much richer transmission-event pool than S1 had before this
  stage.

## Gate re-evaluation

**PASS.** All five re-evaluation criteria met, two with a named, explained caveat (not hidden):

- Price, load forecast: **PASS** unchanged from S1.
- Wind/solar forecast: **PASS with the same named exception as S1** (48h gap coinciding with
  its own resolution switch, Finding 3 of `01-history-pull.md`).
- `avail_gen_mw` hourly over the full window, no unexplained gap: **PASS** — 0 NaN, never
  negative (Finding 4).
- `unavail_tx_*` hourly over the full window: **PASS** — 0 NaN across all 9 pair columns and
  both aggregates (Finding 5).
- Registry reconciles with the outage fleet, 0 missing per covered year: **PASS with a named
  caveat** — 365/366 reconcile exactly; the 1 exception is an outage starting in 2022, a year
  outside the registry's own 2023–2026 pull, not a genuine mismatch within a covered year
  (Finding 3).
- A78 driver passes its named cross-check: **PASS** — matches exactly except where verified
  correct to diverge (Finding 7).
- Pre-FB NTC comparison numbers or why they couldn't be computed, stated in the note: **PASS**
  — Finding 1 and 6 state plainly that no pre-FB NTC data exists for any pair.

NTC itself is no longer a gate item, per the user's decision. Ready for direction on S2.
