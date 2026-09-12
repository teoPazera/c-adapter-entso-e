# S2 — Curate a small set of context events

Date: 2026-09-11 | Repo: `C_adapter_Entso_E`, single-repo project (no upstream clones)

> **Errata (2026-09-12, superseding some content below).** A dedup bug in
> `s1_build_table.py` (`_load_series_dir`, `_prepare_outages`) was collapsing
> genuine multi-period outage revisions down to one arbitrary period,
> discarding 40-50% of raw outage rows project-wide. Fixed and
> `data/s2/events.json` / `data/s2/candidates.csv` regenerated 2026-09-12
> ($0, 47 real HTTP requests, 32.3s — the candidate pool grew from 79 to 182
> once dropped periods became visible again). Full mechanism, evidence, and
> the corrected 18-event table are in `notes/03-baseline.md`'s preamble —
> read that first. Specifically superseded: **Finding 10** (G050, now G090 —
> the "two independent pipelines diverge" reading doesn't survive; see the
> corrected explanation in 03-baseline.md), the exact event table below (IDs
> and 6 of 18 events differ), and Findings 6/7/11's counts. Findings 1-5, 8,
> 9 and the join methodology are unaffected — they describe mechanisms, not
> the specific old candidate counts.

## Question

Can 15–20 real SE3 generation/transmission outage events be curated, each with an exact-time
ENTSO-E outage record and at least one matching, time-correct Nord Pool UMM document — spread
across seasons, unplanned/planned, generation/transmission, and the hydro-plausible season —
so S4 has real material to feed an LLM?

## What I did

1. `src/s2_pull_umm.py`: phase-1 list pull — 3 enum tables (corrected paths, see Finding 2),
   6 generation-unit thread lists (`Units=<eic>&IncludeOutdated=false&Limit=500`; the plan's
   5 Vattenfall EICs plus Trängslet, see Finding 1), 4 transmission thread-list pulls
   (`Areas=<SE3 EIC>&MessageTypes=3`, year-chunked 2023–2026). **13 real requests, 9.0s, $0**
   (`data/s2/pull_summary.json`). All list totals were under `Limit=500`, so no paging was
   needed (transmission per-year totals — 212/247/227/216 — match the plan's own live-checked
   figures exactly).
2. `src/s2_curate_events.py`: built ENTSO-E candidates from `outages_se3.csv`
   (`docstatus` null, reduction ≥300 MW, duration 1–60 days → **60 generation candidates**,
   exactly reproducing the plan's pre-computed count) and `transmission_outages_se3.csv`
   (`kept==True`, external pairs, `Unplanned outage`, ≥24 h → **19 transmission candidates**).
   Flagged 2 same-unit/same-pair overlapping-window duplicates (Finding 3), matched the
   remaining candidates to UMM threads, fetched full version history for matches, selected 18
   events by the plan's stratification rule, rendered documents, ran the step-5 sanity checks.
3. Iterated the matching rule four times against real data before it was trustworthy — see
   Finding 4. Total real HTTP requests for the whole stage, counted directly from
   `data/raw/s2/*.json` (134 files, 0 errors): **134**, well under the ≤170 estimate and the
   250 stop-and-ask line. 5.0 MB cached, $0.

## Findings

1. **The plan's 5-EIC generation-unit list was one short.** Of the 60 generation candidates, 59
   are Vattenfall's five nuclear units and 1 is Trängslet (hydro, EIC `46WPU0000000018Q`,
   330 MW) — not in the plan's `GENERATION_UNITS` constant, which only named the five nuclear
   EICs. Added it (1 extra request) rather than let it silently reject as `no_umm_thread`
   without ever actually being queried — a "not found" that would have meant "not searched," not
   "searched and absent." `src/s2_pull_umm.py` `GENERATION_UNITS`.

2. **Two of the plan's stated enum endpoint paths 404 live; the corrected paths are one level
   deeper.** `/unavailabilitytypes` and `/reasoncodes` do not exist; the real paths, found via
   the API's own `/swagger/v1/swagger.json`, are `/infrastructure/unavailabilitytypes` and
   `/infrastructure/reasoncodes` (`/infrastructure/fueltypes` was already correct).
   `unavailabilityType`: `1=Unplanned, 2=Planned` — confirmed against real messages, not
   assumed (a "Valve test" message carried type 2; a "Safety reasons" Konti-Skan failure
   carried type 1).

3. **The ENTSO-E side has real same-window duplicates the plan's own dedup mechanism was built
   to catch — found 2, one of each kind.** Generic rule: within a group sharing
   `production_resource_id` (generation) or `pair` (transmission), any two candidates whose
   `[start,end]` windows overlap are collapsed to the larger-`reduction_mw` one, the other
   marked `duplicate_of_<event_id>` and excluded from UMM search.
   - `G037` (Forsmark block 2, 2025-03-17 17:30 → 2025-03-19 22:59, 621 MW, mrid
     `pJooek-TiZG169kz8yHLdg`) overlaps `G038`'s window almost entirely (same start, 1 day
     longer end, mrid `4VeFxvynTyX4l5mO0qHRPQ`) — two ENTSO-E mrids for what reads as one real
     trip, not a revision-of-the-same-mrid (S1 Finding 7's pattern, recurring).
   - `T003` (`se3_fi`, 2023-07-29 23:50 → 2023-08-22 07:00, mrid `0RAPBDDRl-vn27oy7WpVFw`)
     tail-overlaps `T005` (2023-08-20 21:59 → 2023-08-24 13:00) by ~1.4 days out of `T005`'s
     3.65-day span — a borderline case (partial tail overlap, not near-total like `G037`/`G038`)
     but resolved by the same rule rather than inventing a separate partial-overlap threshold.

4. **Matching an ENTSO-E outage window to a UMM thread by simple date-overlap is not reliable
   enough on its own — found and fixed through four iterations against real data, kept here so
   the final rule doesn't read as obvious in hindsight.** The final rule (`gather_versions`,
   `src/s2_curate_events.py`): a thread is admitted only if *some* individual `timePeriod` in
   *some* version overlaps the candidate by ≥50% of the shorter side and isn't more than 3×
   longer than the candidate; among admitted threads, the ONE kept is whichever thread's own
   first-ever `publicationDate` is closest to the candidate's `start` — not a merge of every
   thread that overlaps. Each earlier attempt broke on a real case:
   - **Matching on each thread's *latest* version alone** (cheap, uses only the already-cached
     list pull) lets a long-running thread that was later revised to a wide window swallow an
     unrelated short candidate purely by containment. Found on `G050` (Forsmark block 1,
     2025-12-22 → 2025-12-24, 1040 MW): thread `98e6af81`'s *latest* version spans
     2025-12-19 → 2026-01-07 (18.9 days) purely because of its own unrelated revision history,
     which fully contains `G050`'s 1.6-day window.
   - **Falling back to each thread's *first* version** breaks the other way: `T015`'s genuine
     match (`7d0432de`) opens with two versions whose `timePeriods` are empty
     ("Preliminary capacities, will depend on load flow conditions" — no dates yet) and only
     states real dates from v3 onward.
   - **Checking every period of every version, admitting on any pass,** re-admits spurious
     matches: `717c89cd`, a `fi_se3` capacity-restriction thread whose *envelope* runs
     2024-02-26 → 2024-12-19 (297 days) at a constant 400 MW, turns out to be built from many
     individual short periods internally, one of which coincidentally overlaps `T011`'s
     9-day window well enough to pass a per-period check.
   - **What actually separates a genuine match from a coincidental one is *when the thread was
     created*, not its dates.** A message about a specific incident is opened at or near that
     incident's real start (immediately, for unplanned; up to years ahead for planned nuclear
     refuelling — see Finding 6); a persistent structural-restriction thread was opened long
     before any candidate that later happens to fall inside it. Picking the admitted thread
     closest-published-to-candidate-start resolved all four cases at once: `T011→f2578225`,
     `T010→e713d6b1`, `T015→7d0432de`, `G050→d102a701`, each now matching the candidate's end
     to the hour or better.
   - One UMM message can also **bundle temporally disjoint periods**: the Konti-Skan `DK1`
     message (`61e15095`) v1 combines a real 14.6-day restriction with three unrelated
     15–30-minute wrap-up periods two weeks later — collapsing a version's periods into one
     min/max envelope (needed for display) is wrong for matching, which now checks each period
     independently.
   - A real selection-code bug, found the same way: an early `available(pool)` snapshot was
     computed once before a stratum's fill-loop, so per-unit-cap and already-selected checks
     didn't see updates the same loop made — stratum B put 3 of 3 planned events on Ringhals 4
     before this was fixed to check freshly on every row.

5. **S0 Finding 4 is corrected, not just extended.** S0 concluded "no field cross-references
   the two systems" and recommended a fuzzy join with human-in-the-loop confirmation
   (`00-data-check.md` Finding 4). That is too pessimistic: `production_resource_id` (ENTSO-E)
   and `productionUnits[].eic` (UMM) are the *same* EIC and give an exact match — this stage's
   join is EIC-exact throughout, no fuzzy text matching anywhere. S0's own attempt at exactly
   this join found nothing only because it (a) searched a single-month publication window
   instead of the unit's full thread history, and (b) tested it on Oskarshamn 3 and Värtaverket
   specifically — and Oskarshamn 3's near-total absence from Nord Pool UMM (Finding 7) is now
   independently reconfirmed, not an artifact of S0's search. The join mechanism was sound; two
   of the three units S0 tried it on were genuinely bad examples.

6. **OKG (Oskarshamn 3) exclusion, with counts.** 10 of the 60 generation candidates are OKG
   (`46WPU0000000061P`); none were searched against UMM (no request made), all 10 rejected as
   `okg_unit`, per the plan's live check that OKG has no Nord Pool UMM thread with an event in
   2018–2025. Example: `G002`, 2023-03-03 22:00 → 2023-03-10 21:43, 1400 MW.

7. **Join hit rate over all 79 candidates** (`data/s2/candidates.csv`):

   | | total | ENTSO-E-side duplicate | OKG (not searched) | searched | matched |
   |---|---|---|---|---|---|
   | generation | 60 | 1 | 10 | 49 | 42 (85.7%) |
   | transmission | 19 | 1 | 0 | 18 | 18 (100%) |
   | **total** | **79** | **2** | **10** | **67** | **60 (89.6%)** |

   Three rejected examples, with reasons, beyond the OKG one in Finding 6:
   - `G013` (Ringhals 3, 2023-08-01 19:00 → 2023-08-03 15:00, 531 MW) — `overlap_below_50pct`:
     Ringhals 3 has real UMM threads, none with a period overlapping this specific window
     enough.
   - `G037` — `duplicate_of_G038` (Finding 3).
   - `T003` — `duplicate_of_T005` (Finding 3).

8. **Advance notice for planned nuclear maintenance runs to *years*, not weeks, and is
   repeatedly revised.** `G033` (Ringhals 4, 2024-08-15 → 2024-09-15) was first announced
   2021-02-22 (3.5 years ahead) and revised 10 more times before the final dates; `G014`
   (same unit, 2023-08-02 → 2023-09-24) similarly opened ~3.5 years ahead; `G028` (Forsmark
   block 2) opened 2019-12-16, almost 4 years ahead, and took 19 versions to reach its exact
   final window (`data/s2/documents/G028/`). This matters for the plan's **C1 design
   correction** (§ below): the *early* structured `timePeriods` for these threads are
   preliminary and wrong for years at a stretch — an LLM reading the *remarks* text explaining
   why dates keep moving is doing something the final structured record alone cannot show,
   even though the final record eventually converges to the exact truth.

9. **7 of 18 selected events have no document version published before `realised_start`.**
   All 7 are `Unplanned outage` (`G050`, `G022`, `G020`, `G017`, `T006`, `T010`, `T015`); the
   first report lands 3 to 112 minutes *after* the real start in every case — immediate,
   reactive reporting, which is what "unplanned" means. This is a real property of the data,
   not a matching defect: the plan's step-5 check ("≥1 version published before
   `realised_start`") reads as written for advance-notice events and doesn't quite fit
   reactive ones. Flagging rather than silently forcing a pre-start version by picking a worse
   match.

10. **One genuine, explained end-date discrepancy survives.** `G050`'s single matched thread
    (`d102a701`) is the correct, uniquely-matched document (Finding 4), but its real incident
    (a turbine failure with repeated restart attempts, `data/s2/documents/G050/`) ran until
    2026-01-06 in Nord Pool's telling — 304 hours (12.7 days) past this specific ENTSO-E mrid's
    stated end of 2025-12-24 09:30. Read as the two independent regulatory pipelines (S0
    Finding 4) settling on different final states for the same real event, not as a wrong
    match — the step-5 sanity check (`n_events_end_mismatch_gt_6h`) is descriptive by the
    plan's own wording ("Count of...") and this is its one true positive.

11. **Per-stratum fill, all within target, no shortfall to name:**

    | stratum | target | selected |
    |---|---|---|
    | (a) unplanned generation | 9–11 | 11 (≥2/season ✓; 10 of 11 `revision_surprise` ✓; ≤3/unit ✓) |
    | (b) planned generation, lead ≥7 days | 2–3 | 3 |
    | (c) unplanned transmission | 3–4 | 4 |
    | (d) `hydro_plausible` (Apr–Jun start) | ≥3 | 5 |

    **Judgment call, flagged rather than silently resolved:** stratum (c)'s 4 selected events
    are all `fi_se3` (`T006`, `T010`, `T011`, `T015`) — the plan's "at most 3 per unit" cap is
    stated only inside bullet (a); (c)'s rule is pure `reduction_mw` ranking with a ≥300 MW
    preference, and `fi_se3` genuinely holds the 4 largest transmission reductions in the
    dataset (1200/1100/1000/1000 MW vs. the next-best 715 MW on `dk1_se3`). Applying (a)'s cap
    to (c) would have overruled the plan's own stated ranking rule; not done, reported instead.
    `dk1_se3`, `se3_no1`, `se3_fi` events exist in `candidates.csv` if cross-border diversity
    is wanted over the top-4-by-size rule.

## Full event table

18 events, chronological. `n_v` = UMM versions kept for the matched thread; `lead` = candidate
start minus the thread's first publication (negative = published after start); `surprise` =
`revision_surprise`; `hydro?` = `hydro_plausible`.

| id | unit / border | type | start (UTC) | end (UTC) | MW | n_v | lead | surprise | hydro? |
|---|---|---|---|---|---|---|---|---|---|
| G014 | Ringhals 4 | Planned | 2023-08-02 03:00 | 2023-09-24 18:00 | 1134 | 9 | 1273.7d | Y | N |
| G017 | Forsmark block 1 | Unplanned | 2023-09-06 11:22 | 2023-09-09 06:30 | 988 | 5 | -21min | Y | N |
| G020 | Ringhals 3 | Unplanned | 2023-10-19 12:45 | 2023-10-21 06:00 | 1063 | 4 | -35min | Y | N |
| G022 | Ringhals 4 | Unplanned | 2023-11-29 00:25 | 2023-12-04 02:00 | 1134 | 4 | -38min | Y | N |
| T006 | FI → SE3 | Unplanned | 2023-11-29 00:25 | 2023-12-04 22:59 | 1200 | 7 | -63min | Y | N |
| G025 | Forsmark block 2 | Unplanned | 2024-02-29 23:00 | 2024-03-04 20:00 | 1121 | 4 | 779min | N | N |
| G026 | Forsmark block 3 | Unplanned | 2024-03-16 16:00 | 2024-03-22 01:00 | 1172 | 3 | 31.3d | Y | N |
| G028 | Forsmark block 2 | Planned | 2024-04-21 10:00 | 2024-05-23 10:00 | 1121 | 7 | 1223.9d | Y | Y |
| T010 | FI → SE3 | Unplanned | 2024-05-10 19:08 | 2024-05-15 12:40 | 1000 | 8 | -32min | Y | Y |
| T011 | FI → SE3 | Unplanned | 2024-06-18 15:00 | 2024-06-27 21:59 | 1100 | 2 | 1397min | Y | Y |
| G033 | Ringhals 4 | Planned | 2024-08-15 01:00 | 2024-09-15 22:00 | 1134 | 11 | 1269.5d | Y | N |
| G035 | Forsmark block 3 | Unplanned | 2025-02-05 17:00 | 2025-02-08 03:00 | 1172 | 5 | 1.4d | Y | N |
| T015 | FI → SE3 | Unplanned | 2025-02-28 15:11 | 2025-03-31 21:59 | 1000 | 4 | -3min | Y | N |
| G041 | Ringhals 3 | Unplanned | 2025-06-23 18:00 | 2025-06-24 21:00 | 770 | 2 | 32min | Y | Y |
| G045 | Forsmark block 2 | Unplanned | 2025-08-14 01:00 | 2025-08-16 12:00 | 1121 | 6 | 728min | Y | N |
| G046 | Forsmark block 1 | Unplanned | 2025-08-17 11:30 | 2025-08-28 13:00 | 1040 | 10 | 1.8d | Y | N |
| G050 | Forsmark block 1 | Unplanned | 2025-12-22 19:28 | 2025-12-24 09:30 | 1040 | 6 | -112min | Y | N |
| G056 | Forsmark block 3 | Unplanned | 2026-04-09 01:00 | 2026-04-10 23:30 | 1172 | 6 | 657min | Y | Y |

Per-unit counts across the full 18: Forsmark block 3 ×3, Forsmark block 1 ×3, Forsmark block 2
×3, Ringhals 4 ×3, Ringhals 3 ×2, `fi_se3` ×4 (Finding 11).

## Design corrections carried forward from the plan — for the user's decision, not acted on

- **C1 (S3/S5).** The plan's own worry — that structured `timePeriods` already give the future
  outage path, so an LLM "win" might just be re-deriving structured data — is sharpened by
  Finding 8: for planned events specifically, the *early* structured record is wrong for years
  and only the free-text remarks explain why (e.g. "Correction 29.4" repeated 8 times on one
  `fi_se3` thread in 2024). The four-condition comparison (naive / structured schedule / LLM /
  oracle) the plan proposed stays a live option.
- **C2 (S3).** Confirmed still relevant: `hydro_reservoir_fill` and seasonality belong in the
  S3 baseline regardless of which events are used, per the original feasibility analysis.

## Open questions

1. Whether stratum (c)'s `fi_se3`-only result (Finding 11) is acceptable, or whether the user
   wants cross-border diversity traded against the plan's stated ranking rule.
2. Whether the 7 pre-start-version-free unplanned events (Finding 9) should be usable in S4 at
   all, given the plan's S4 forecast-origin design (`t0_candidates.day_ahead` vs. `announcement`)
   — a day-ahead origin sees nothing for these regardless, which may be exactly the point for
   an unplanned-event condition, but wasn't stated as a design choice yet.
3. `T015`'s matched thread (`7d0432de`) is one of 8 UMM threads that legitimately overlap its
   31-day candidate window (Finding 4's per-period logic admits several genuinely short,
   non-background restrictions on this busy border) — only the single closest-published one is
   kept per event by design; the other 7 are visible in `candidates.csv`'s pre-filter stage but
   not persisted anywhere. Worth a second look if S4 wants "everything active during the
   window," not "the one primary document."

## Implications for the thesis

- **The join is exact, not fuzzy — reverses S0's stated plan for S2/S4 and removes a
  human-in-the-loop step S0 said would be needed** (Finding 5). Any later stage that assumed
  manual confirmation of the outage↔document pairing can drop that assumption.
- **Matching real outage windows to real document threads needed four real iterations to get
  right, and the failure mode each time was a different kind of date-arithmetic coincidence,
  not a data-quality problem** (Finding 4). If S9's Zurich schema ever needs an analogous
  join (internal document ↔ internal forecasting-driver event), budget real iteration time for
  this, not a one-shot date-overlap rule.
- **Planned nuclear maintenance is announced years ahead and revised repeatedly before landing
  on the truth** (Finding 8) — this is a genuine, options-rich case for the "does an LLM reading
  the revision history add value beyond the latest structured record" question C1 raises, and
  argues for keeping the full version sequence (already captured in `events.json`) rather than
  only the latest version when S4 builds its context blob.

## Gate

**PASS.** 18 events in `data/s2/events.json` (within 15–20), each with zone, realised start/end
(UTC), a reduction size, and ≥1 UMM document version (0 of 18 have zero versions); strata (a)–(d)
all filled inside target with no shortfall to name (Finding 11); join hit rate and OKG exclusion
stated with counts (Findings 6–7). Two named, evidenced caveats, neither a failed gate criterion:
`G050`'s 304-hour end discrepancy is a genuine two-pipeline divergence on a correctly-matched
single document (Finding 10), and stratum (c) concentrated on one border by the plan's own
ranking rule (Finding 11). Second run of `s2_pull_umm.py` and repeated runs of
`s2_curate_events.py` made 0 new requests (full idempotency verified across ~10 iterations while
fixing the matching logic). Ready for direction on S3.
