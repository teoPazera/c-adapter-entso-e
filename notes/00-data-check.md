# S0 — Confirm the data exists and is usable

Date: 2026-09-11 | Repo: `C_adapter_Entso_E`, first commit made at end of this stage

## Question

Do the five raw materials for the CAF-via-context-adapter benchmark exist and look usable:
one sample each of day-ahead price, day-ahead load forecast, day-ahead wind/solar forecast,
one outage record, and one REMIT/UMM market message — and is the message text actually in
English?

## What I did

1. `uv init` + `uv add entsoe-py pandas python-dotenv requests` in `C_adapter_Entso_E/`.
   Installed: `entsoe-py==0.8.1`, `pandas==3.0.5` (both newer than assumed in the plan, but
   the plan's method names all exist unchanged in this version — verified by introspection
   before writing any pull code). Removed the `main.py`/`README.md` boilerplate `uv init`
   creates by default — unused.
2. Verified `.env` loads correctly (`ENTSOE_E_API_KEY`, 36 chars, no quoting/whitespace
   issues) without ever printing the key.
3. Verified Swedish zone codes and method signatures against the installed `entsoe-py`
   package directly: `SE_3` → `10Y1001A1001A46L`; all five target methods exist with the
   signatures the plan assumed.
4. Probed the Nord Pool UMM API (unauthenticated) to establish real filter semantics before
   writing the pull script: `areas=<EIC>` genuinely filters; `publicationStartDate`/
   `publicationStopDate` genuinely filters by publication date; `eventStartDate`/
   `eventStopDate` filters on whether *any* time period in the message overlaps the window
   (loose, not a tight per-event filter); `skip` pages correctly.
5. Wrote `src/s0_probe.py` and ran it. **First run: the ENTSO-E API key returned
   `401 Unauthorized` on every one of 8 calls** (documented as a blocking FAIL in the
   original version of this note). You fixed the key's activation; **second run (after the
   fix): all 8 ENTSO-E calls plus 1 UMM bulk call succeeded, wall-clock 12.4 s.**
6. **Caught and fixed a credential leak in my own script.** `entsoe-py` embeds the API key
   in its own exception messages (`...&securityToken=<key>&...` inside the URL it reports on
   HTTP errors). My first run wrote that verbatim into `data/s0/run_summary.json`. Nothing
   had been committed yet, but I deleted the tainted file, added a redaction pass
   (`_redact`/`_redact_fields` in `src/s0_probe.py`, regex-strips `securityToken=...` from
   any string before it reaches a print or a saved file) and re-ran. Verified after the fix:
   grepping the whole working tree (excluding `.git`/`.venv`) for the key's first 8
   characters returns only `.env` itself (gitignored) — no leaked fragment anywhere else.
7. **Fixed a `TypeError` on JSON serialization.** `pandas.Timestamp` objects were used as
   dict keys in the per-sample `first_3` preview (from `.to_dict()` /
   `.to_dict(orient="index")` on a `DatetimeIndex`-indexed frame), which `json.dumps` cannot
   serialize as object keys. Fixed by stringifying keys before building the summary
   (`_stringify_keys`).
8. **Investigated an outage↔UMM message matching bug**, described in Finding 4 below — found
   it was not a bug in my code but a real property of the two data sources.

## Findings

1. **All five raw materials exist and are pullable with a working, activated ENTSO-E
   token.** Day-ahead price (SE3, 2 sample days), day-ahead load forecast (SE3, 2 days),
   day-ahead wind/solar forecast (SE3, 2 days), 3 real generation-unit outage rows (SE3,
   September 2025), and 44 real UMM market messages (SE3, September 2025) were all
   retrieved and saved under `data/s0/` (`data/s0/run_summary.json` has the full structured
   record of every pull).

2. **The hourly→15-minute switch is real and confirmed exactly where expected, but it is
   price-specific, not benchmark-wide:**

   | Series | 2025-09-15 (pre-switch) | 2025-10-15 (post-switch) |
   |---|---|---|
   | Day-ahead price | 25 points, 60 min resolution | 97 points, 15 min resolution |
   | Day-ahead load forecast | 24 rows, 60 min resolution | 24 rows, **still 60 min** |
   | Day-ahead wind/solar forecast | 24 rows, 60 min resolution | 24 rows, **still 60 min** |

   Price moved to 15-minute settlement on 2025-10-01 as the plan expected. **Load forecast
   and wind/solar forecast did not move — both stayed hourly on the post-switch sample day.**
   This is the plan's own hypothesized outcome ("load forecast may stay hourly after the
   switch") confirmed with real data, not assumed. **This is load-bearing for S1**: the three
   series will not share a native index frequency across the whole 2023–present window, and
   any resampling/alignment choice (upsample load & wind/solar to 15 min, or downsample price
   to hourly) needs to be made explicitly, not discovered by an alignment bug later.

   Minor, explainable oddity: price sample days return 25 and 97 points, not the plan's
   expected 24 and 96 — `entsoe-py` returns a closed interval on both ends for
   `query_day_ahead_prices` (includes the `00:00` point of the *next* day), while
   `query_load_forecast` and `query_wind_and_solar_forecast` return a half-open interval
   (exactly 24 rows for one day, no boundary duplicate). Not a bug, just an
   inconsistency between the two query types worth knowing before building an S1 loader —
   trim the final row from price pulls, or don't assume all three series return the same row
   count for the same day range.

3. **The generation-unit outage record for SE3/September 2025 is small and real: 3 rows**,
   not a large set. Columns: `avail_qty, biddingzone_domain, businesstype, curvetype,
   docstatus, end, mrid, nominal_power, plant_type, production_resource_id,
   production_resource_location, production_resource_name, production_resource_psr_name,
   pstn, qty_uom, resolution, revision, start` (`data/s0/outage_all_rows.csv`). All three are
   `"Planned maintenance"` with `avail_qty=0` (full unavailability), long-duration:

   | Unit | EIC | Window | Revision |
   |---|---|---|---|
   | Värtaverket (KVV8, biomass) | `46WPU0000000023X` | 2025-09-01 → 2025-09-05 | 3 |
   | Oskarshamn 3 (nuclear) | `46WPU0000000061P` | 2025-03-29 → 2025-11-02 | 16 |
   | Värtaverket (biomass, different capacity figure) | `46WPU0000000023X` | 2024-07-29 → 2026-03-31 | 13 |

   `production_resource_id` is the genuine EIC-format identifier (`46W...` prefix); `mrid`
   is an ENTSO-E-internal opaque instance ID, not an EIC — my first matching attempt used
   `mrid` by mistake (see Finding 4).

4. **Real finding, not a bug: ENTSO-E's outage feed and Nord Pool's UMM/REMIT feed do not
   share an identifier, and a same-calendar-month join between them did not resolve for
   either outage candidate tried.** Investigation, in order:
   - First attempt matched on `mrid` (wrong field — an ENTSO-E-internal ID, not an EIC) and
     on a `"name"` column that doesn't exist in the outage schema (real column is
     `production_resource_name`). Fixed both; re-ran with the correct
     `production_resource_id` (EIC) and `production_resource_name` — still no match against
     the 44 UMM messages published in September 2025 for SE3.
   - Widened to an event-date-overlap search (`eventStartDate`/`eventStopDate` = Aug 1 –
     Sep 10 2025) across 162 messages: still no match by EIC or name.
   - Searched Nord Pool's UMM history with **no date filter at all**, newest-first, for the
     two target EICs directly: both **do** exist in Nord Pool's system —
     `46WPU0000000061P` (Oskarshamn 3) and `46WPU0000000023X` (Värtaverket) each have at
     least one real message thread — but the *newest* message mentioning each EIC (versions
     2 and 9 respectively) is a **different, more recent outage event** (publication dates in
     2026-06 and 2026-07), not the March/September-2025 event from the ENTSO-E row. A
     follow-up event-date search targeted directly at the Oskarshamn 3 window
     (2025-03-20 to 2025-04-05, 100 messages returned) still didn't surface a message
     containing that EIC.
   - Evidence saved: `data/s0/umm_eic_existence_check.json` (documents both EICs' newest
     known UMM message, with an explicit note that these are *not* verified to be the same
     event as the ENTSO-E rows).

   **Interpretation:** Nord Pool's UMM system appears to key each announced outage *event*
   under its own `messageId` (with `version` counting revisions to that one event), and a
   single generating unit accumulates many distinct `messageId`s over its operational life —
   one per outage occurrence. There is no field on either side (ENTSO-E's `mrid` vs. UMM's
   `messageId`/`acerRssMessageIds`) that cross-references the other system. ENTSO-E outage
   publication (EU Regulation 543/2013) and REMIT/UMM publication (REMIT Implementing
   Regulation, Art. 4) are two independently-run regulatory reporting pipelines describing
   overlapping physical events, not one system with two views. **Consequence for S2/S4: an
   outage↔document join must be built as a fuzzy match (unit name + capacity drop + rough
   time overlap, likely with human-in-the-loop confirmation during S2 event curation), not
   as an exact-key join.** This is worth knowing now, before S2 assumes an automatic match is
   possible.

   `data/s0/umm_message_matched.json` therefore holds a **fallback, not a verified match**: the
   first UMM message in the September-2025/SE3 pull with non-empty `remarks`. It is a real,
   usable document sample for the "does a message look like this" question, but it is not
   paired with `data/s0/outage_row.json`'s specific event.

5. **English check, on the 44 real UMM messages actually available for SE3/September 2025**
   (not the plan's target of 100–200 — that's simply how many exist for one zone/month by
   publication date; a bigger sample means widening the window, not re-running this pull):
   - 0 of 44 have both empty `remarks` and empty `unavailabilityReason`.
   - Heuristic (Swedish diacritics + function-word counting) marks 40/44 (90.9%) as English,
     4/44 as containing Swedish-language content.
   - Manual check of the one borderline example shows it's actually English prose that only
     tripped the heuristic on the Swedish proper noun "Kraftnät": *"Missing heartbeat for the
     mFRR EAM market, which may result in Svenska Kraftnät not activating Vattenfall's mFRR
     EAM bids. Reson and duration unknown"* — so 90.9% is a lower bound.
   - `langdetect` was not installed; this result is heuristic-only, not library-verified.
   - Verbatim English example: `"Summer maintenance stop"`.
   - Verbatim example from the real Oskarshamn 3 message found in Finding 4 (a different,
     2026 event, shown here only as an additional real-text sample, not the matched pair):
     `unavailabilityReason: "Additional measures in the turbine."`, `remarks: "Full power
     will be reached after approximately 3 days. New event stop."` — plainly English, and
     illustrates the revision mechanic directly: `version: 2`, i.e. this exact text is a
     *correction* to an earlier version of the same message.

6. **entsoe-py/pandas versions are newer than the plan assumed** (`entsoe-py==0.8.1`,
   `pandas==3.0.5`). All plan-referenced method names and the Sweden zone-code mapping exist
   unchanged, verified by direct introspection, so the version difference did not silently
   invalidate anything.

## Does the English assumption hold?

**Yes.** 40/44 (90.9%) heuristic-English, with the one flagged exception shown by manual
inspection to actually be English (heuristic false-positive on a Swedish proper noun). True
rate is at least 90.9%, plausibly higher. Not fully verified: no `langdetect`-confirmed
number yet (would cost nothing, just wasn't added this run), and the sample is 44 messages
(everything that exists for SE3/September 2025 by publication date), not the 100–200 the
plan hoped for.

## Open questions

1. **UMM `messageType`/`unavailabilityType` integer code meanings** — still not decoded
   (deprioritized once the outage↔UMM matching problem turned out to be structural rather
   than a filter-naming issue).
2. **How S2 should actually pair a curated outage event with its document(s).** Finding 4
   means "search UMM by unit name/EIC within the event month" is not a reliable recipe. A
   real recipe probably needs: (a) a wider, undated EIC search plus manual inspection of
   which `messageId` thread's time periods actually overlap the target event, or (b) sourcing
   documents a different way (e.g. Nord Pool's own web UI export, or ENTSO-E's own "urgent
   market message" mirror, if one exists) — not resolved here, flagged for S2.
3. Whether `docstatus` (`None` for all 3 rows returned) ever takes a non-null value for SE3,
   and whether that matters for filtering active vs. withdrawn outages at scale — not
   observed in this small sample.

## Implications for S1

- **The three numerical series will not share one native resolution across 2023–present.**
  Price moves to 15-minute on 2025-10-01; load forecast and wind/solar forecast do not. S1's
  loader needs an explicit resampling/alignment decision stated up front, not discovered
  as a bug later.
- **Watch the boundary-row inconsistency** between `query_day_ahead_prices` (closed interval,
  one extra row per day pulled) and the forecast queries (half-open, exact row count) when
  writing the S1 pull loop over a long date range — an off-by-one here compounds silently
  over two years of data.
- **S2's event curation cannot assume an automatic outage→document join.** Budget human
  review time for confirming each of the 15–20 curated events actually has a genuine,
  time-matched UMM document — Finding 4 shows this doesn't fall out of name/EIC/date
  matching alone even for two real, well-known Swedish generating units.

## Gate: PASS with one named exception

- All five raw material types exist and were retrieved with real, inspectable data: **PASS.**
- Price shows 60 min then 15 min resolution across the switch: **PASS**, exactly as
  hypothesized, plus the useful side-finding that load/wind-solar forecasts do not switch.
- English-check reports a clear majority of non-empty messages in English: **PASS** (90.9%,
  likely higher).
- **One outage matched to one UMM message on the same UTC window: FAIL**, named precisely —
  not from a code bug (that was found and fixed first), but from a real structural property
  of the two source systems (Finding 4). `data/s0/umm_message_matched.json` is a real UMM
  document sample, not a verified pair with `data/s0/outage_row.json`.

Per the ground rules, I'm reporting this exception rather than forcing a fabricated match or
silently swapping in a different, better-matching zone/date to make the criterion pass.
Everything else about the raw materials checks out; the join strategy for S2 needs to be
designed with Finding 4 in mind rather than assumed to be free.
