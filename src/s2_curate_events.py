"""S2 step 2: join ENTSO-E candidate outages to Nord Pool UMM documents,
stratify-select 15-20 curated events, render their documents, and write the
report tables.

Reads: data/s1/outages_se3.csv, data/s1/transmission_outages_se3.csv,
       data/s1/se3_hourly.csv (S1 pull, already on disk)
       data/raw/s2/*.json (S2 phase-1 UMM pull, via s2_pull_umm)
Writes: data/s2/candidates.csv, data/s2/events.json,
        data/s2/documents/<event_id>/<messageId>_v<k>.{json,txt},
        data/s2/pull_summary.json (phase 2 request count appended)

Matching is done against each thread's LATEST version only (the data
s2_pull_umm's phase-1 pull already has cached, at $0) rather than the plan's
literal "any-version" wording, which would require fetching full version
history for every one-of-hundreds thread near a unit before knowing which
ones matter -- see notes/02-events.md for why this is the budget-feasible
reading. Full version history (a real request per matched thread) is fetched
only for non-duplicate, non-OKG candidates that pass the latest-version
overlap match.

Run: uv run python src/s2_curate_events.py
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

import pandas as pd

import s2_pull_umm as umm
from common import ROOT

DATA_S1 = ROOT / "data" / "s1"
DATA_S2 = ROOT / "data" / "s2"
DOCS_DIR = DATA_S2 / "documents"
DATA_S2.mkdir(parents=True, exist_ok=True)

TZ = "Europe/Stockholm"

EXTERNAL_PAIR_EICS = {
    "no1_se3": (umm.BORDER_EICS["no1"], umm.SE3_EIC),
    "fi_se3": (umm.BORDER_EICS["fi"], umm.SE3_EIC),
    "dk1_se3": (umm.BORDER_EICS["dk1"], umm.SE3_EIC),
    "se3_no1": (umm.SE3_EIC, umm.BORDER_EICS["no1"]),
    "se3_fi": (umm.SE3_EIC, umm.BORDER_EICS["fi"]),
    "se3_dk1": (umm.SE3_EIC, umm.BORDER_EICS["dk1"]),
}
# nominal_mw_proxy per external pair, reused from S1b's already-verified
# figure (data/s1/coverage_report.json "transmission_availability") rather
# than recomputed here -- same number, one source of truth.
_COV = json.loads((DATA_S1 / "coverage_report.json").read_text(encoding="utf-8"))
NOMINAL_MW_PROXY = {
    k: v.get("nominal_mw_proxy")
    for k, v in _COV["transmission_availability"].items()
    if k in EXTERNAL_PAIR_EICS
}

MAX_TOTAL_REQUESTS = 250  # plan's stated stop-and-ask threshold (S2 ground rules)

OVERLAP_MIN_FRAC = 0.5
# A thread whose FIRST version's own duration is more than this many times
# the candidate's duration is treated as a long-running background
# restriction the candidate happens to fall inside, not a document ABOUT
# the candidate -- >=50%-of-shorter-interval alone doesn't catch this,
# because a long thread trivially contains a short candidate at 100%. Found
# live 2026-09-11 on fi_se3 (T010/T011/T015 each pulled in 3-4 multi-week-
# to-multi-month capacity-restriction threads this way); the two real
# working examples (f2578225 for T011, d102a701 for G050, e713d6b1 for T010)
# all have first-version duration within 1.4x of their candidate's duration,
# so 3x leaves clear margin. See notes/02-events.md.
FIRST_VERSION_DURATION_RATIO_MAX = 3.0
REVISION_SURPRISE_STOP_HOURS = 6.0
REVISION_SURPRISE_MW = 100.0

TARGET_A = (9, 11)   # unplanned generation
TARGET_B = (2, 3)    # planned generation, lead >= 7 days
TARGET_C = (3, 4)    # unplanned transmission
MIN_HYDRO_PLAUSIBLE = 3
PER_UNIT_CAP = 3
MIN_REVISION_SURPRISE_A = 3
PLANNED_LEAD_MIN_HOURS = 7 * 24


# ---------------------------------------------------------------- season / hydro

def _season(ts: pd.Timestamp) -> str:
    m = ts.month
    if m in (12, 1, 2):
        return "winter"
    if m in (3, 4, 5):
        return "spring"
    if m in (6, 7, 8):
        return "summer"
    return "autumn"


def _hydro_plausible(ts: pd.Timestamp) -> bool:
    return ts.month in (4, 5, 6)


# ---------------------------------------------------------------- ENTSO-E candidate loading

def load_generation_candidates() -> pd.DataFrame:
    df = pd.read_csv(DATA_S1 / "outages_se3.csv")
    kept = df[df["docstatus"].isna()].copy()
    # start/end carry mixed CET/CEST offsets (Stockholm crosses DST inside
    # the window) -- utc=True is required to parse them into one comparable
    # column; start_local/end_local (Stockholm wall-clock) are kept
    # separately for season/hydro_plausible/day_ahead, which are calendar
    # concepts, not UTC-instant ones.
    kept["start"] = pd.to_datetime(kept["start"], utc=True)
    kept["end"] = pd.to_datetime(kept["end"], utc=True)
    kept["start_local"] = kept["start"].dt.tz_convert(TZ)
    kept["end_local"] = kept["end"].dt.tz_convert(TZ)
    kept["duration_days"] = (kept["end"] - kept["start"]).dt.total_seconds() / 86400
    kept["reduction_mw"] = (kept["nominal_power"] - kept["avail_qty"]).clip(lower=0)
    sel = kept[(kept["reduction_mw"] >= 300) & (kept["duration_days"].between(1, 60))].copy()
    sel = sel.sort_values("start").reset_index(drop=True)
    sel["event_id"] = [f"G{idx + 1:03d}" for idx in sel.index]
    sel["kind"] = "generation"
    sel["unit_or_pair"] = sel["production_resource_id"]
    sel["unit_name"] = sel["production_resource_name"]
    sel["entsoe_source"] = sel["source"]
    return sel


def load_transmission_candidates() -> pd.DataFrame:
    df = pd.read_csv(DATA_S1 / "transmission_outages_se3.csv")
    ext = df[(df["kept"] == True) & (df["pair"].isin(EXTERNAL_PAIR_EICS))].copy()  # noqa: E712
    ext["start"] = pd.to_datetime(ext["start"], utc=True)
    ext["end"] = pd.to_datetime(ext["end"], utc=True)
    ext["start_local"] = ext["start"].dt.tz_convert(TZ)
    ext["end_local"] = ext["end"].dt.tz_convert(TZ)
    ext["duration_days"] = (ext["end"] - ext["start"]).dt.total_seconds() / 86400
    unpl = ext[(ext["businesstype"] == "Unplanned outage") & (ext["duration_days"] >= 1)].copy()
    unpl = unpl[(unpl["end"] - unpl["start"]) >= pd.Timedelta(hours=24)]
    unpl["nominal_mw_proxy"] = unpl["pair"].map(NOMINAL_MW_PROXY)
    unpl["reduction_mw"] = (unpl["nominal_mw_proxy"] - unpl["avail_qty"]).clip(lower=0)
    unpl = unpl.sort_values("start").reset_index(drop=True)
    unpl["event_id"] = [f"T{idx + 1:03d}" for idx in unpl.index]
    unpl["kind"] = "transmission"
    unpl["unit_or_pair"] = unpl["pair"]
    unpl["unit_name"] = None  # filled from the matched UMM payload's own "name" field
    unpl["entsoe_source"] = "A78"
    unpl["production_resource_id"] = None
    return unpl


def _dedupe_overlaps(df: pd.DataFrame, group_col: str) -> pd.DataFrame:
    """Mark the smaller-reduction row of any pair that shares group_col and
    has a temporally overlapping [start,end] window as duplicate_of the
    larger. Generic rule (documented in notes/02-events.md): applied
    identically to both a same-mrid-family revision-under-a-new-mrid case
    (generation, Forsmark block 2, 2025-03) and a borderline tail-overlap
    case (transmission, se3_fi, 2023-07/08) -- the plan gave no separate
    tie-break rule for partial vs near-total overlap, so one rule covers
    both rather than inventing an ad hoc threshold."""
    df = df.copy()
    df["duplicate_of"] = None
    for _, idxs in df.groupby(group_col).groups.items():
        if len(idxs) < 2:
            continue
        ordered = df.loc[list(idxs)].sort_values("reduction_mw", ascending=False).index.tolist()
        kept_rows: list[int] = []
        for idx in ordered:
            row = df.loc[idx]
            hit = None
            for kidx in kept_rows:
                krow = df.loc[kidx]
                if row["start"] < krow["end"] and krow["start"] < row["end"]:
                    hit = kidx
                    break
            if hit is not None:
                df.at[idx, "duplicate_of"] = df.at[hit, "event_id"]
            else:
                kept_rows.append(idx)
    return df


# ---------------------------------------------------------------- UMM matching (latest version only)

def _overlap_frac(a_start, a_end, b_start, b_end) -> float:
    ov_start = max(a_start, b_start)
    ov_end = min(a_end, b_end)
    overlap = (ov_end - ov_start).total_seconds()
    if overlap <= 0:
        return 0.0
    shorter = min((a_end - a_start).total_seconds(), (b_end - b_start).total_seconds())
    return overlap / shorter if shorter > 0 else 0.0


def _period_bounds(sub: dict | None):
    """Collapsed min/max envelope across all of sub's timePeriods -- fine as
    a DISPLAY summary (build_version_record), but not for matching: one UMM
    message can bundle several temporally disjoint periods (found live on a
    Konti-Skan message whose v1 combined a real 14-day restriction with
    three unrelated 15-30 minute wrap-up periods two weeks later), and the
    envelope makes the version look far longer than any single period
    actually is. Matching in gather_versions instead checks each timePeriod
    independently against the candidate window."""
    if not sub or not sub.get("timePeriods"):
        return None
    periods = sub["timePeriods"]
    starts = [pd.Timestamp(p["eventStart"]) for p in periods]
    stops = [pd.Timestamp(p["eventStop"]) for p in periods]
    unavail = max((p.get("unavailableCapacity") or 0) for p in periods)
    return min(starts), max(stops), unavail


def _gen_subentry(item: dict, eic: str) -> dict | None:
    for pu in item.get("productionUnits", []):
        if pu.get("eic") == eic:
            return pu
    return None


def _tx_subentry(item: dict, from_eic: str, to_eic: str) -> dict | None:
    for tu in item.get("transmissionUnits", []):
        if tu.get("inAreaEic") == from_eic and tu.get("outAreaEic") == to_eic:
            return tu
    return None


def match_generation(row, unit_threads: dict) -> tuple[list[str], str | None]:
    eic = row["production_resource_id"]
    if eic == umm.OKG_EIC:
        return [], "okg_unit"
    items = unit_threads.get(eic)
    if items is None:
        return [], "unit_not_pulled"
    a_start = pd.Timestamp(row["start"]).tz_localize(None)
    a_end = pd.Timestamp(row["end"]).tz_localize(None)
    matched = []
    for item in items:
        sub = _gen_subentry(item, eic)
        bounds = _period_bounds(sub)
        if bounds is None:
            continue
        b_start, b_end = bounds[0].tz_localize(None), bounds[1].tz_localize(None)
        if _overlap_frac(a_start, a_end, b_start, b_end) >= OVERLAP_MIN_FRAC:
            matched.append(item["messageId"])
    if matched:
        return matched, None
    return [], "overlap_below_50pct"


def match_transmission(row, tx_items: list[dict]) -> tuple[list[str], str | None]:
    pair = row["unit_or_pair"]
    from_eic, to_eic = EXTERNAL_PAIR_EICS[pair]
    a_start = pd.Timestamp(row["start"]).tz_localize(None)
    a_end = pd.Timestamp(row["end"]).tz_localize(None)
    any_for_pair = False
    matched = []
    for item in tx_items:
        sub = _tx_subentry(item, from_eic, to_eic)
        if sub is None:
            continue
        any_for_pair = True
        bounds = _period_bounds(sub)
        if bounds is None:
            continue
        b_start, b_end = bounds[0].tz_localize(None), bounds[1].tz_localize(None)
        if _overlap_frac(a_start, a_end, b_start, b_end) >= OVERLAP_MIN_FRAC:
            matched.append(item["messageId"])
    if matched:
        return matched, None
    return [], ("overlap_below_50pct" if any_for_pair else "no_umm_thread")


# ---------------------------------------------------------------- version history + derived fields

def _thread_versions(message_id: str) -> list[dict]:
    if umm.request_count + 1 > MAX_TOTAL_REQUESTS:
        raise RuntimeError(f"Would exceed MAX_TOTAL_REQUESTS={MAX_TOTAL_REQUESTS} -- stopping, per plan's stop-and-ask rule")
    return umm.fetch_message_versions(message_id)


def build_version_record(item: dict, sub: dict | None) -> dict:
    bounds = _period_bounds(sub)
    return {
        "messageId": item["messageId"],
        "version": item.get("version"),
        "publicationDate": item.get("publicationDate"),
        "event_start": bounds[0].isoformat() if bounds else None,
        "event_stop": bounds[1].isoformat() if bounds else None,
        "unavailable_mw": bounds[2] if bounds else None,
        "remarks": item.get("remarks"),
    }


def gather_versions(row, sub_fn) -> tuple[list[dict], bool, list[str]]:
    """Fetch full version history for every thread the cheap latest-version
    pre-filter matched, then pick the SINGLE BEST thread and return only its
    version history -- not a merge of every thread that passes some overlap
    test.

    This design replaced three progressively more elaborate merge-based
    attempts during live curation on 2026-09-11 (matching latest-version
    envelopes only; re-validating against each thread's first version;
    re-validating every version's individual timePeriods), each of which
    fixed one concrete false positive but reopened another on fi_se3 -- a
    high-traffic border where dozens of threads are simultaneously "live"
    (see notes/02-events.md for the T010/T011/T015/G050/Konti-Skan cases
    walked through in order). The recurring problem was date-arithmetic
    alone: a persistent structural capacity-restriction thread can contain
    an individual timePeriod that overlaps a short candidate's window about
    as well as the genuine incident report does, so no overlap-fraction or
    duration-ratio threshold cleanly separates them.

    What does separate them: WHEN the thread was first published relative
    to when the candidate outage actually started. A message written about
    a specific incident is created at or near that incident's start (at
    latest a handful of days ahead for a planned one); a long-running
    structural restriction's thread was typically opened long before any
    particular candidate that later falls inside it. So: admit only threads
    with >=1 timePeriod (any version) overlapping the candidate by
    >=OVERLAP_MIN_FRAC of the shorter side and not disproportionately
    longer (FIRST_VERSION_DURATION_RATIO_MAX guards the swallow case), then
    among admitted threads keep only the ONE whose own first-ever
    publicationDate is closest to the candidate's start. One thread per
    event, not a merge, keeps the curated document set clean and the
    end-mismatch/lead-time fields unambiguous.

    Returns (that thread's version records sorted by publicationDate;
    revision_surprise within it; a single-element (or empty)
    validated_thread_ids list -- empty means the caller must re-reject the
    candidate)."""
    a_start = pd.Timestamp(row["start"]).tz_localize(None)
    a_end = pd.Timestamp(row["end"]).tz_localize(None)
    candidate_duration_s = (a_end - a_start).total_seconds()

    best_thread_id = None
    best_closeness_s = None
    best_versions_sorted = None
    for message_id in row["matched_thread_ids"]:
        versions = _thread_versions(message_id)
        versions_sorted = sorted(versions, key=lambda v: v.get("version", 0))
        if not versions_sorted:
            continue
        passed = False
        for v in versions_sorted:
            sub = sub_fn(v)
            if not sub or not sub.get("timePeriods"):
                continue
            for p in sub["timePeriods"]:
                p_start = pd.Timestamp(p["eventStart"]).tz_localize(None)
                p_end = pd.Timestamp(p["eventStop"]).tz_localize(None)
                shorter_frac = _overlap_frac(a_start, a_end, p_start, p_end)
                dur_s = (p_end - p_start).total_seconds()
                if shorter_frac >= OVERLAP_MIN_FRAC and (
                    candidate_duration_s <= 0 or dur_s <= FIRST_VERSION_DURATION_RATIO_MAX * candidate_duration_s
                ):
                    passed = True
                    break
            if passed:
                break
        if not passed:
            continue
        first_pub = pd.Timestamp(versions_sorted[0]["publicationDate"]).tz_localize(None)
        closeness_s = abs((first_pub - a_start).total_seconds())
        if best_closeness_s is None or closeness_s < best_closeness_s:
            best_thread_id, best_closeness_s, best_versions_sorted = message_id, closeness_s, versions_sorted

    if best_thread_id is None:
        return [], False, []

    all_records: list[dict] = []
    surprise = False
    prev_bounds = None
    for item in best_versions_sorted:
        sub = sub_fn(item)
        all_records.append(build_version_record(item, sub))
        bounds = _period_bounds(sub)
        if bounds is not None and prev_bounds is not None:
            stop_delta_h = abs((bounds[1] - prev_bounds[1]).total_seconds()) / 3600
            mw_delta = abs(bounds[2] - prev_bounds[2])
            if stop_delta_h > REVISION_SURPRISE_STOP_HOURS or mw_delta > REVISION_SURPRISE_MW:
                surprise = True
        if bounds is not None:
            prev_bounds = bounds
    all_records.sort(key=lambda r: r["publicationDate"] or "")
    return all_records, surprise, [best_thread_id]


# ---------------------------------------------------------------- selection

def select_events(accepted: pd.DataFrame) -> tuple[list[str], dict]:
    log: dict[str, object] = {}
    unit_cap = Counter()
    selected: list[str] = []

    def take(event_id: str, unit_key: str):
        selected.append(event_id)
        unit_cap[unit_key] += 1

    def take_up_to(pool: pd.DataFrame, n_max: int, cap: int = PER_UNIT_CAP) -> int:
        """Iterate pool (already sorted) taking rows not yet selected and
        under the per-unit cap, checked FRESH each row -- not against a
        snapshot computed once before the loop. A snapshot-filtered pool
        does not see unit_cap/selected updates made by take() during its own
        iteration, which can silently blow the per-unit cap (found live
        2026-09-11: stratum B put 3 of 3 planned events on one unit because
        the old `available(pool)` snapshot was computed before that
        stratum's own loop started taking rows). Returns how many were
        taken."""
        n_taken = 0
        for _, r in pool.iterrows():
            if n_taken >= n_max:
                break
            if r["event_id"] in selected or unit_cap[r["unit_or_pair"]] >= cap:
                continue
            take(r["event_id"], r["unit_or_pair"])
            n_taken += 1
        return n_taken

    # --- Stratum A: unplanned generation ---
    gen_unpl = accepted[(accepted["kind"] == "generation") & (accepted["businesstype"] == "Unplanned outage")]
    gen_unpl = gen_unpl.sort_values("reduction_mw", ascending=False)

    for season in ["winter", "spring", "summer", "autumn"]:
        take_up_to(gen_unpl[gen_unpl["season"] == season], 2)

    target_a = TARGET_A[1]
    n_a = len([e for e in selected if e.startswith("G")])
    take_up_to(gen_unpl, max(0, target_a - n_a))

    idx_pre = accepted.set_index("event_id")
    a_ids = [e for e in selected if e.startswith("G") and idx_pre.loc[e, "businesstype"] == "Unplanned outage"]
    n_surprise = sum(1 for eid in a_ids if idx_pre.loc[eid, "revision_surprise"])
    if n_surprise < MIN_REVISION_SURPRISE_A:
        surprise_candidates = gen_unpl[
            (gen_unpl["revision_surprise"] == True) & (~gen_unpl["event_id"].isin(selected))  # noqa: E712
        ]
        non_surprise_selected = sorted(
            [e for e in a_ids if not idx_pre.loc[e, "revision_surprise"]],
            key=lambda e: idx_pre.loc[e, "reduction_mw"],
        )
        for _, r in surprise_candidates.iterrows():
            if n_surprise >= MIN_REVISION_SURPRISE_A or not non_surprise_selected:
                break
            drop = non_surprise_selected.pop(0)
            selected.remove(drop)
            unit_cap[idx_pre.loc[drop, "unit_or_pair"]] -= 1
            take(r["event_id"], r["unit_or_pair"])
            n_surprise += 1
    log["stratum_A_unplanned_generation"] = {
        "target": TARGET_A, "n_selected": len([e for e in selected if e.startswith("G")]),
        "n_revision_surprise": n_surprise,
    }

    # --- Stratum B: planned generation, lead >= 7 days ---
    gen_pl = accepted[(accepted["kind"] == "generation") & (accepted["businesstype"] == "Planned maintenance")
                       & (accepted["first_publication_lead_hours"] >= PLANNED_LEAD_MIN_HOURS)]
    gen_pl = gen_pl.sort_values("reduction_mw", ascending=False)
    n_b = take_up_to(gen_pl, TARGET_B[1])
    log["stratum_B_planned_generation"] = {"target": TARGET_B, "n_selected": n_b}

    # --- Stratum C: unplanned transmission, external pairs ---
    tx = accepted[accepted["kind"] == "transmission"].sort_values(
        ["reduction_mw"], ascending=False
    )
    tx_ge300 = tx[tx["reduction_mw"] >= 300]
    tx_lt300 = tx[tx["reduction_mw"] < 300]
    # No per-pair cap: the plan states "at most 3 per unit" only for
    # stratum (a); stratum (c)'s rule is pure reduction_mw ranking with a
    # >=300MW preference. Applying (a)'s cap here would silently overrule
    # that and is worth flagging rather than doing quietly -- see
    # notes/02-events.md for the resulting fi_se3 concentration.
    target_c = TARGET_C[1]
    n_c = take_up_to(tx_ge300, target_c, cap=len(accepted))
    n_c += take_up_to(tx_lt300, target_c - n_c, cap=len(accepted))
    log["stratum_C_unplanned_transmission"] = {"target": TARGET_C, "n_selected": n_c}

    # --- Stratum D check: hydro_plausible among selected ---
    idx = accepted.set_index("event_id")
    n_hydro = sum(1 for eid in selected if bool(idx.loc[eid, "hydro_plausible"]))
    if n_hydro < MIN_HYDRO_PLAUSIBLE:
        hydro_candidates = accepted[(accepted["hydro_plausible"] == True) & (~accepted["event_id"].isin(selected))]  # noqa: E712
        hydro_candidates = hydro_candidates.sort_values("reduction_mw", ascending=False)
        for _, r in hydro_candidates.iterrows():
            if n_hydro >= MIN_HYDRO_PLAUSIBLE:
                break
            if r["kind"] == "generation" and unit_cap[r["unit_or_pair"]] >= PER_UNIT_CAP:
                continue
            same_kind_selected = [
                e for e in selected
                if idx.loc[e, "kind"] == r["kind"] and not bool(idx.loc[e, "hydro_plausible"])
                and idx.loc[e, "businesstype"] == r["businesstype"]
            ]
            if not same_kind_selected:
                continue
            drop = min(same_kind_selected, key=lambda e: idx.loc[e, "reduction_mw"])
            selected.remove(drop)
            unit_cap[idx.loc[drop, "unit_or_pair"]] -= 1
            take(r["event_id"], r["unit_or_pair"])
            n_hydro += 1
    log["stratum_D_hydro_plausible"] = {"target_min": MIN_HYDRO_PLAUSIBLE, "n_selected": n_hydro}

    return selected, log


# ---------------------------------------------------------------- documents

def _enum_text(enums: dict, table: str, value) -> str | None:
    for entry in enums.get(table, []):
        if entry.get("value") == value:
            return entry.get("text")
    return None


def render_documents(event_id: str, versions: list[dict], raw_items_by_msg_version: dict, enums: dict, row, unit_name) -> None:
    out_dir = DOCS_DIR / event_id
    if out_dir.exists():
        # Clear stale files from a previous run's matched thread(s) -- the
        # matching logic changed several times during live curation, and
        # without this an event's directory silently accumulates documents
        # for threads no longer in its matched_thread_ids (found live
        # 2026-09-11: T011's directory still held three superseded threads
        # after matching was fixed down to the single correct one).
        for f in out_dir.iterdir():
            f.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)
    for rec in versions:
        key = (rec["messageId"], rec["version"])
        item = raw_items_by_msg_version.get(key)
        if item is None:
            continue
        (out_dir / f"{rec['messageId']}_v{rec['version']}.json").write_text(
            json.dumps(item, indent=2), encoding="utf-8"
        )
        lines = [
            f"Event: {event_id} ({unit_name}, {row['kind']})",
            f"Message: {rec['messageId']} version {rec['version']}",
            f"Published: {rec['publicationDate']}",
            f"Unavailability type: {_enum_text(enums, 'unavailabilitytypes', item.get('unavailabilityType'))}",
            f"Reason code: {_enum_text(enums, 'reasoncodes', item.get('reasonCode'))}",
            f"Unavailability reason: {item.get('unavailabilityReason')}",
            "",
            "Time periods:",
        ]
        units_field = "productionUnits" if row["kind"] == "generation" else "transmissionUnits"
        for u in item.get(units_field, []):
            lines.append(f"  Unit/border: {u.get('name')}")
            for p in u.get("timePeriods", []):
                lines.append(
                    f"    {p.get('eventStart')} -> {p.get('eventStop')}: "
                    f"unavailable {p.get('unavailableCapacity')} MW, available {p.get('availableCapacity')} MW"
                )
        lines += ["", "Remarks:", item.get("remarks") or "(none)"]
        (out_dir / f"{rec['messageId']}_v{rec['version']}.txt").write_text(
            "\n".join(lines), encoding="utf-8"
        )


# ---------------------------------------------------------------- main

def main() -> None:
    t0 = time.time()
    if DOCS_DIR.exists():
        # Full wipe, not just per-event: which event_ids get selected can
        # itself change between runs (selection logic changed during live
        # curation), so a stale directory for an event no longer selected
        # would otherwise survive indefinitely. Cheap to regenerate --
        # everything here is rebuilt from the data/raw/s2/ cache at $0.
        import shutil
        shutil.rmtree(DOCS_DIR)
    print("Loading ENTSO-E candidates...")
    gen = load_generation_candidates()
    tx = load_transmission_candidates()
    print(f"  generation candidates: {len(gen)}")
    print(f"  transmission candidates: {len(tx)}")

    gen = _dedupe_overlaps(gen, "production_resource_id")
    tx = _dedupe_overlaps(tx, "unit_or_pair")
    n_dup = int(gen["duplicate_of"].notna().sum() + tx["duplicate_of"].notna().sum())
    print(f"  overlap duplicates flagged: {n_dup}")

    common_cols = ["event_id", "kind", "unit_or_pair", "unit_name", "production_resource_id",
                   "entsoe_mrid" if "entsoe_mrid" in gen.columns else "mrid",
                   "start", "end", "reduction_mw", "duration_days", "businesstype",
                   "entsoe_source", "duplicate_of"]
    gen = gen.rename(columns={"mrid": "entsoe_mrid"})
    tx = tx.rename(columns={"mrid": "entsoe_mrid"})
    cand = pd.concat([gen, tx], ignore_index=True, sort=False)
    cand["season"] = cand["start_local"].apply(_season)
    cand["hydro_plausible"] = cand["start_local"].apply(_hydro_plausible)

    print("Fetching UMM thread-list pools (cached, phase-1 data)...")
    unit_threads = umm.fetch_unit_threads()
    tx_items = umm.fetch_transmission_threads()

    print("Matching candidates to UMM threads (latest-version overlap)...")
    matched_ids, reasons = [], []
    for _, row in cand.iterrows():
        if pd.notna(row["duplicate_of"]):
            matched_ids.append([])
            reasons.append(f"duplicate_of_{row['duplicate_of']}")
            continue
        if row["kind"] == "generation":
            m, r = match_generation(row, unit_threads)
        else:
            m, r = match_transmission(row, tx_items)
        matched_ids.append(m)
        reasons.append(r)
    cand["matched_thread_ids"] = matched_ids
    cand["rejection_reason"] = reasons
    cand["accepted"] = cand["rejection_reason"].isna()

    n_accepted = int(cand["accepted"].sum())
    print(f"  pre-filter accepted (latest-version overlap, before first-version re-validation): {n_accepted} of {len(cand)}")
    print(f"  pre-filter rejection reasons: {cand['rejection_reason'].value_counts(dropna=True).to_dict()}")

    enums = umm.fetch_enums()

    print("Fetching full version history + re-validating each thread by its own first version...")
    all_versions_col, surprise_col, lead_col, n_versions_col = [], [], [], []
    validated_ids_col, final_reason_col, final_accepted_col = [], [], []
    raw_items_by_msg_version: dict[tuple[str, int], dict] = {}
    n_reclassified = 0
    for _, row in cand.iterrows():
        if not row["accepted"]:
            all_versions_col.append([])
            surprise_col.append(False)
            lead_col.append(None)
            n_versions_col.append(0)
            validated_ids_col.append([])
            final_reason_col.append(row["rejection_reason"])
            final_accepted_col.append(False)
            continue
        sub_fn = (
            (lambda item, eic=row["production_resource_id"]: _gen_subentry(item, eic))
            if row["kind"] == "generation"
            else (lambda item, p=row["unit_or_pair"]: _tx_subentry(item, *EXTERNAL_PAIR_EICS[p]))
        )
        records, surprise, validated_ids = gather_versions(row, sub_fn)
        for message_id in row["matched_thread_ids"]:
            for item in umm.fetch_message_versions(message_id):
                raw_items_by_msg_version[(item["messageId"], item.get("version"))] = item
        all_versions_col.append(records)
        surprise_col.append(surprise)
        validated_ids_col.append(validated_ids)
        if records:
            first_pub = pd.Timestamp(min(r["publicationDate"] for r in records))
            start_naive = pd.Timestamp(row["start"]).tz_localize(None)
            first_pub_naive = first_pub.tz_localize(None) if first_pub.tzinfo else first_pub
            lead_col.append((start_naive - first_pub_naive).total_seconds() / 3600)
        else:
            lead_col.append(None)
        n_versions_col.append(len(records))
        if validated_ids:
            final_reason_col.append(None)
            final_accepted_col.append(True)
        else:
            n_reclassified += 1
            final_reason_col.append("no_thread_survived_first_version_check")
            final_accepted_col.append(False)

    cand["versions"] = all_versions_col
    cand["revision_surprise"] = surprise_col
    cand["first_publication_lead_hours"] = lead_col
    cand["n_versions"] = n_versions_col
    # matched_thread_ids / accepted / rejection_reason now reflect the
    # first-version-validated set, not the cheap latest-version pre-filter
    # (see gather_versions docstring for why a second pass is needed).
    cand["matched_thread_ids"] = validated_ids_col
    cand["accepted"] = final_accepted_col
    cand["rejection_reason"] = final_reason_col

    print(f"  candidates reclassified rejected after first-version re-validation: {n_reclassified}")
    print(f"  final accepted: {int(cand['accepted'].sum())} of {len(cand)}")
    print(f"  final rejection reasons: {cand['rejection_reason'].value_counts(dropna=True).to_dict()}")
    print(f"  real HTTP requests so far (this run): {umm.request_count}")

    candidates_csv = cand.drop(columns=["versions"]).copy()
    candidates_csv["matched_thread_ids"] = candidates_csv["matched_thread_ids"].apply(lambda x: ";".join(x))
    candidates_csv.to_csv(DATA_S2 / "candidates.csv", index=False)
    print(f"Wrote {DATA_S2 / 'candidates.csv'} ({len(candidates_csv)} rows)")

    accepted = cand[cand["accepted"]].copy()

    print("Selecting curated events...")
    selected_ids, strata_log = select_events(accepted)
    print(f"  selected {len(selected_ids)} events")
    for k, v in strata_log.items():
        print(f"  {k}: {v}")

    idx = accepted.set_index("event_id")
    events_out = []
    for eid in selected_ids:
        row = idx.loc[eid]
        realised_start_utc = pd.Timestamp(row["start"]).tz_convert("UTC")
        realised_end_utc = pd.Timestamp(row["end"]).tz_convert("UTC")
        start_local = pd.Timestamp(row["start_local"])
        day_ahead = (start_local.normalize() - pd.Timedelta(days=1)) + pd.Timedelta(hours=12)
        versions = row["versions"]
        first_pub = min((v["publicationDate"] for v in versions), default=None)
        primary_thread_id = row["matched_thread_ids"][0] if row["matched_thread_ids"] else None
        unit_name = row["unit_name"]
        if row["kind"] == "transmission" and primary_thread_id:
            # Try every version of the primary thread, not just the first
            # (chronologically) one -- some versions omit a leg entirely
            # (found on T015: the earliest version didn't carry the fi_se3
            # transmissionUnits entry the field is named from, only a later
            # revision added it), so the fixed-index lookup silently left
            # unit_name at its None placeholder.
            for v in versions:
                if v["messageId"] != primary_thread_id:
                    continue
                item_v = raw_items_by_msg_version.get((v["messageId"], v["version"]))
                if not item_v:
                    continue
                sub_v = _tx_subentry(item_v, *EXTERNAL_PAIR_EICS[row["unit_or_pair"]])
                if sub_v and sub_v.get("name"):
                    unit_name = sub_v.get("name")
                    break
        events_out.append({
            "event_id": eid,
            "kind": row["kind"],
            "unit_eic|pair": row["unit_or_pair"],
            "unit_name": unit_name,
            "zone": "SE3",
            "entsoe_mrid": row["entsoe_mrid"],
            "entsoe_source": row["entsoe_source"],
            "realised_start": realised_start_utc.isoformat(),
            "realised_end": realised_end_utc.isoformat(),
            "reduction_mw": float(row["reduction_mw"]),
            "businesstype": row["businesstype"],
            "season": row["season"],
            "hydro_plausible": bool(row["hydro_plausible"]),
            "umm_thread_ids": row["matched_thread_ids"],
            "versions": versions,
            "first_publication_lead_hours": row["first_publication_lead_hours"],
            "revision_surprise": bool(row["revision_surprise"]),
            "t0_candidates": {
                "announcement": first_pub,
                "day_ahead": day_ahead.isoformat(),
            },
        })
        render_documents(eid, versions, raw_items_by_msg_version, enums, row, unit_name)

    events_payload = {
        "generated_by": "src/s2_curate_events.py",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_events": len(events_out),
        "strata_counts": strata_log,
        "events": events_out,
    }
    (DATA_S2 / "events.json").write_text(json.dumps(events_payload, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {DATA_S2 / 'events.json'} ({len(events_out)} events)")

    print("Running sanity checks...")
    hourly = pd.read_csv(DATA_S1 / "se3_hourly.csv")
    hourly["timestamp"] = pd.to_datetime(hourly["timestamp"], utc=True)
    hourly = hourly.set_index("timestamp")

    checks = {"n_events_with_pre_start_version": 0, "n_events_window_check_pass": 0,
              "n_events_window_check_fail": [], "n_events_end_mismatch_gt_6h": []}
    for ev in events_out:
        versions = ev["versions"]
        rs = pd.Timestamp(ev["realised_start"])
        has_pre = any(pd.Timestamp(v["publicationDate"]).tz_convert("UTC") < rs for v in versions if v["publicationDate"])
        if has_pre:
            checks["n_events_with_pre_start_version"] += 1

        if ev["kind"] == "generation":
            row0 = idx.loc[ev["event_id"]]
            col = "unavail_nuclear_mw" if row0["businesstype"] and "Nuclear" in str(row0.get("plant_type", "")) else "unavail_mw"
        else:
            col = f"unavail_tx_{ev['unit_eic|pair']}_mw"
        w_start = pd.Timestamp(ev["realised_start"])
        w_end = pd.Timestamp(ev["realised_end"])
        if col in hourly.columns:
            window = hourly.loc[(hourly.index >= w_start) & (hourly.index < w_end), col]
            mean_val = float(window.mean()) if len(window) else float("nan")
            ok = mean_val >= 0.8 * ev["reduction_mw"] if mean_val == mean_val else False
            if ok:
                checks["n_events_window_check_pass"] += 1
            else:
                checks["n_events_window_check_fail"].append({"event_id": ev["event_id"], "col": col, "mean": mean_val, "expected_min": 0.8 * ev["reduction_mw"]})

        re_end = pd.Timestamp(ev["realised_end"])
        # Compare against the PRIMARY (highest-overlap) thread's own latest
        # version, not the max-publicationDate record across every merged
        # thread -- on a busy border several validated threads can be
        # legitimately distinct events, and the one revised most recently
        # is not necessarily the one describing this candidate's own end.
        primary_id = ev["umm_thread_ids"][0] if ev["umm_thread_ids"] else None
        primary_versions = [v for v in versions if v["messageId"] == primary_id] if primary_id else versions
        latest_version = max(primary_versions, key=lambda v: v["publicationDate"] or "", default=None)
        if latest_version and latest_version.get("event_stop"):
            umm_stop = pd.Timestamp(latest_version["event_stop"])
            if umm_stop.tzinfo is None:
                umm_stop = umm_stop.tz_localize("UTC")
            diff_h = abs((umm_stop.tz_convert("UTC") - re_end).total_seconds()) / 3600
            if diff_h > 6:
                checks["n_events_end_mismatch_gt_6h"].append({"event_id": ev["event_id"], "diff_hours": diff_h})

    print(json.dumps(checks, indent=2, default=str))

    elapsed = time.time() - t0
    umm.write_pull_summary({"phase": "phase2_matching_and_versions", "elapsed_s": round(elapsed, 1),
                             "n_candidates": len(cand), "n_accepted": n_accepted, "n_selected": len(selected_ids)})
    print(f"\nDone in {elapsed:.1f}s. Real HTTP requests this run: {umm.request_count}")
    (DATA_S2 / "sanity_checks.json").write_text(json.dumps(checks, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
