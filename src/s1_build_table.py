"""S1 + S1b: build the aligned hourly modelling table from cached raw pulls.

Reads every chunk file under data/raw/s1/ (written by s1_pull_history.py),
concatenates and de-duplicates each series, aligns to an explicit
DST-safe hourly index, builds the available-generation-capacity driver from
the outage feeds and (S1b) an hourly transmission-availability driver from
A78 outages in place of NTC (unavailable for SE3, S1 Finding 4), and writes:
  data/s1/se3_hourly.csv
  data/s1/outages_se3.csv
  data/s1/transmission_outages_se3.csv
  data/s1/installed_capacity_se3.csv
  data/s1/installed_capacity_per_unit_se3.csv
  data/s1/ntc_prefb_se3.csv          (S1b; only written if pre-FB NTC data exists)
  data/s1/coverage_report.json

Run: uv run python src/s1_build_table.py
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from common import ROOT

DATA_RAW = ROOT / "data" / "raw" / "s1"
DATA_S1 = ROOT / "data" / "s1"
DATA_S1.mkdir(parents=True, exist_ok=True)

TZ = "Europe/Stockholm"
WINDOW_START = "2021-01-01"
WINDOW_END = "2026-09-12"

# S1b: Nordic flow-based day-ahead coupling's stated go-live date -- see
# notes/01b-gate-close.md Finding 1 for why this doesn't actually explain the
# empty NTC probes (a pre-go-live probe is *also* empty).
NTC_FB_GO_LIVE = "2024-10-30"
EXTERNAL_BORDERS = ["NO_1", "FI", "DK_1"]

BORDERS = ["SE_2", "SE_4", "NO_1", "FI", "DK_1"]
PAIR_KEYS = [
    f"se3_{b.lower().replace('_', '')}" for b in BORDERS
] + [
    f"{b.lower().replace('_', '')}_se3" for b in BORDERS
]
# S1 Finding 9: se3_se2 (the SE3->SE2 direction specifically) has zero rows
# across the whole pull; the reverse, se2_se3, has 305. Per the S1b plan's
# 9-pair column list, it's excluded from the table entirely, not modelled as
# an all-zero column.
EXPORT_PAIR_KEYS = [f"se3_{b.lower().replace('_', '')}" for b in BORDERS if b != "SE_2"]
IMPORT_PAIR_KEYS = [f"{b.lower().replace('_', '')}_se3" for b in BORDERS]

report: dict[str, object] = {}


# ---------------------------------------------------------------- loading

def _load_series_dir(series: str, dedup: str = "index") -> pd.DataFrame:
    """Concatenate every cached *.csv chunk for `series`, UTC tz-aware index,
    sorted.

    dedup="index" (default): drop rows with a duplicate index value, keep
    first. Correct for genuine time series (price, load, wind/solar, hydro,
    ntc) where the index is a timestamp with exactly one legitimate row per
    instant, and adjacent month/year chunks can legitimately re-fetch the
    same boundary timestamp.

    dedup="full_row": drop only EXACT full-row duplicates (every column
    equal), keep first. REQUIRED for the outage series (A77/A80/A78):
    entsoe-py emits one row per Series_Period within a revision, and every
    period of the same revision shares the same top-level document index
    (created_doc_time) -- index-based dedup silently collapses a genuine
    multi-period revision down to one arbitrary period. Found 2026-09-12
    while extending S1's window: adding 2021-2022 raw chunks changed which
    period of a shared-index group sorted first for 2 of 10 transmission
    pairs, which is what made an otherwise-invisible bug visible. Quantified
    against the full cached raw tree: 40-50% of rows in every one of the 9
    transmission pairs and both generation-outage feeds (a77/a80) share an
    index with at least one other row and were being silently dropped.
    Confirmed non-redundant (real, different periods, not accidental
    cross-chunk refetches) by direct inspection -- see notes/03-baseline.md
    preamble finding."""
    d = DATA_RAW / series
    if not d.exists():
        return pd.DataFrame()
    frames = []
    for f in sorted(d.glob("*.csv")):
        df = pd.read_csv(f, index_col=0)
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames)
    df.index = pd.to_datetime(df.index, utc=True, format="ISO8601")
    if dedup == "full_row":
        df = df[~df.duplicated(keep="first")]
    else:
        df = df[~df.index.duplicated(keep="first")]
    df = df.sort_index()
    return df


def _n_empty_chunks(series: str) -> int:
    d = DATA_RAW / series
    return len(list(d.glob("*.empty"))) if d.exists() else 0


def _n_error_chunks(series: str) -> int:
    d = DATA_RAW / series
    return len(list(d.glob("*.error.json"))) if d.exists() else 0


def _expected_index() -> pd.DatetimeIndex:
    return pd.date_range(WINDOW_START, WINDOW_END, freq="h", tz=TZ, inclusive="left")


# ---------------------------------------------------------------- coverage

def _coverage(s: pd.Series, expected_index: pd.DatetimeIndex) -> dict:
    reindexed = s.reindex(expected_index)
    n_expected = len(expected_index)
    is_nan = reindexed.isna()
    n_nan = int(is_nan.sum())
    n_present = n_expected - n_nan

    longest = 0
    longest_start = longest_end = None
    cur = 0
    cur_start = None
    nan_vals = is_nan.to_numpy()
    for i in range(n_expected):
        if nan_vals[i]:
            if cur == 0:
                cur_start = expected_index[i]
            cur += 1
            if cur > longest:
                longest = cur
                longest_start = cur_start
                longest_end = expected_index[i]
        else:
            cur = 0

    valid = reindexed.dropna()
    return {
        "n_expected": n_expected,
        "n_present": n_present,
        "n_nan": n_nan,
        "longest_nan_run_hours": longest,
        "longest_nan_run_start": str(longest_start) if longest_start is not None else None,
        "longest_nan_run_end": str(longest_end) if longest_end is not None else None,
        "first_valid": str(valid.index.min()) if len(valid) else None,
        "last_valid": str(valid.index.max()) if len(valid) else None,
    }


# ---------------------------------------------------------------- resampling (DST-safe)

def _resample_hourly(raw_any_tz: pd.Series, expected_index: pd.DatetimeIndex, how: str = "mean") -> pd.Series:
    """Resample to hourly via UTC bucketing then relabel onto expected_index
    (Europe/Stockholm). Hour-bucket boundaries are identical in UTC and
    Stockholm time since the offset is always a whole number of hours -- only
    the *labels* differ -- so this is equivalent to resampling in local time,
    without local time's DST fall-back ambiguity (see analyze_resolution_switch
    docstring: a local .floor("h")/resample can't disambiguate 2023-10-29
    02:00, which occurs twice)."""
    raw_utc = raw_any_tz.tz_convert("UTC")
    resampler = raw_utc.resample("1h")
    hourly_utc = resampler.mean() if how == "mean" else resampler.ffill()
    expected_utc = expected_index.tz_convert("UTC")
    reindexed = hourly_utc.reindex(expected_utc)
    if how == "ffill":
        reindexed = reindexed.ffill()
    reindexed.index = expected_index
    return reindexed


# ---------------------------------------------------------------- resolution-switch detection

def analyze_resolution_switch(raw_series: pd.Series, display_tz: str = TZ) -> dict:
    """Find the first hour-bucket with >1 raw sub-point (i.e. the switch from
    hourly to sub-hourly native resolution), and flag any bucket that doesn't
    cleanly have 1 point before it / 4 points after it. Used for price,
    load_forecast, solar and wind_onshore -- all four turned out to switch to
    15-minute resolution at some point in the pulled window (verified from
    real data, not just the two S0 sample days).

    Buckets in UTC, not `display_tz`: flooring a Europe/Stockholm-localized
    index to the hour raises on the DST fall-back day (2023-10-29 02:00 local
    occurs twice, so .floor("h") can't disambiguate it). UTC has no DST, so
    the bucketing itself is unambiguous; results are converted to
    `display_tz` only for the human-readable fields below."""
    idx_utc = raw_series.index.tz_convert("UTC")
    counts = raw_series.groupby(idx_utc.floor("h")).size()
    switch_candidates = counts[counts > 1]
    switch_ts = switch_candidates.index.min() if len(switch_candidates) else None
    if switch_ts is not None:
        after = counts[counts.index >= switch_ts]
        before = counts[counts.index < switch_ts]
    else:
        after = pd.Series(dtype=int)
        before = counts
    bad_after = after[after != 4]
    bad_before = before[before != 1]
    return {
        "switch_timestamp_local": str(switch_ts.tz_convert(display_tz)) if switch_ts is not None else None,
        "n_hours_after_switch_total": int(len(after)),
        "n_hours_after_switch_not_4_subpoints": int(len(bad_after)),
        "example_bad_after_switch": [str(pd.Timestamp(t).tz_convert(display_tz)) for t in bad_after.index[:5]],
        "n_hours_before_switch_not_1_subpoint": int(len(bad_before)),
        "example_bad_before_switch": [str(pd.Timestamp(t).tz_convert(display_tz)) for t in bad_before.index[:5]],
    }


# ---------------------------------------------------------------- price

def build_price(expected_index: pd.DatetimeIndex) -> pd.Series:
    raw = _load_series_dir("price")
    if raw.empty:
        report["price"] = {"error": "no data cached"}
        return pd.Series(index=expected_index, dtype=float, name="price_eur_mwh")
    raw_series = raw["price_eur_mwh"]
    resolution = analyze_resolution_switch(raw_series)
    n_negative = int((raw_series < 0).sum())
    hourly = _resample_hourly(raw_series, expected_index, how="mean")
    hourly.name = "price_eur_mwh"
    cov = _coverage(hourly, expected_index)
    report["price"] = {
        "n_raw_points": int(len(raw_series)),
        "n_negative_raw_points": n_negative,
        "resolution_switch": resolution,
        "coverage": cov,
        "n_empty_chunks": _n_empty_chunks("price"),
        "n_error_chunks": _n_error_chunks("price"),
    }
    return hourly


# ---------------------------------------------------------------- load / wind-solar

def build_load_forecast(expected_index: pd.DatetimeIndex) -> pd.Series:
    raw = _load_series_dir("load_forecast")
    if raw.empty or "Forecasted Load" not in raw.columns:
        report["load_forecast"] = {"error": "no data cached or column missing", "columns_found": list(raw.columns)}
        return pd.Series(index=expected_index, dtype=float, name="load_forecast_mw")
    raw_series = raw["Forecasted Load"]
    resolution = analyze_resolution_switch(raw_series)
    hourly = _resample_hourly(raw_series, expected_index, how="mean")
    hourly.name = "load_forecast_mw"
    cov = _coverage(hourly, expected_index)
    report["load_forecast"] = {
        "n_raw_points": int(len(raw_series)),
        "resolution_switch": resolution,
        "value_range": [float(raw_series.min()), float(raw_series.max())],
        "coverage": cov,
        "n_empty_chunks": _n_empty_chunks("load_forecast"),
        "n_error_chunks": _n_error_chunks("load_forecast"),
    }
    return hourly


def build_wind_solar(expected_index: pd.DatetimeIndex) -> tuple[pd.Series, pd.Series]:
    raw = _load_series_dir("wind_solar_forecast")
    cols = list(raw.columns)
    report["wind_solar_forecast_columns_found"] = cols
    out = {}
    for col, outname in [("Solar", "solar_forecast_mw"), ("Wind Onshore", "wind_onshore_forecast_mw")]:
        if raw.empty or col not in raw.columns:
            report[outname] = {"error": f"column '{col}' not found", "columns_found": cols}
            out[outname] = pd.Series(index=expected_index, dtype=float, name=outname)
            continue
        raw_series = raw[col]
        resolution = analyze_resolution_switch(raw_series)
        hourly = _resample_hourly(raw_series, expected_index, how="mean")
        hourly.name = outname
        cov = _coverage(hourly, expected_index)
        n_zero_filled_pre_coverage = 0
        if outname == "solar_forecast_mw":
            # S1b Step 1, user decision 2026-09-12: no monthly raw chunk
            # before 2021-12 carries a 'Solar' column at all (verified:
            # 2021-01..11.csv columns == ['Wind Onshore'] only), so
            # concatenation leaves this stretch NaN, not "fetched but
            # missing". Zero-fill strictly before this series' own first
            # real observation: SE3 solar capacity was genuinely near-zero
            # then (first real month, 2021-12, has max=21.2 MW, mean=1.9 MW,
            # vs. 447.7 MW max by 2022) -- an inferred-zero, not a guess.
            # Only applied before the first valid point, so unrelated,
            # smaller NaN stretches elsewhere (resample edges, <1.2% each,
            # 2022/2025/2026) are left to the model's own NaN policy (S3).
            first_valid = hourly.first_valid_index()
            if first_valid is not None:
                pre_mask = hourly.index < first_valid
                n_zero_filled_pre_coverage = int(hourly[pre_mask].isna().sum())
                hourly.loc[pre_mask] = hourly.loc[pre_mask].fillna(0.0)
        report[outname] = {
            "n_raw_points": int(len(raw_series)),
            "resolution_switch": resolution,
            "value_range": [float(raw_series.min()), float(raw_series.max())],
            "coverage": cov,
            "n_empty_chunks": _n_empty_chunks("wind_solar_forecast"),
            "n_error_chunks": _n_error_chunks("wind_solar_forecast"),
            "n_zero_filled_pre_first_observation": n_zero_filled_pre_coverage,
        }
        out[outname] = hourly
    return out["solar_forecast_mw"], out["wind_onshore_forecast_mw"]


# ---------------------------------------------------------------- NTC

def build_ntc(expected_index: pd.DatetimeIndex) -> dict[str, pd.Series]:
    pull_summary_path = DATA_S1 / "pull_summary.json"
    alive: dict[str, bool] = {}
    if pull_summary_path.exists():
        ps = json.loads(pull_summary_path.read_text(encoding="utf-8"))
        alive = ps.get("ntc_probe_alive", {})
    cols: dict[str, pd.Series] = {}
    ntc_report: dict[str, dict] = {}
    for pair_key in PAIR_KEYS:
        colname = f"ntc_{pair_key}"
        if not alive.get(pair_key, False):
            ntc_report[pair_key] = {"dropped_by_probe": True}
            cols[colname] = pd.Series(index=expected_index, dtype=float, name=colname)
            continue
        raw = _load_series_dir(f"ntc_{pair_key}")
        if raw.empty or "ntc_mw" not in raw.columns:
            ntc_report[pair_key] = {"dropped_by_probe": False, "error": "no data cached despite alive probe", "columns_found": list(raw.columns)}
            cols[colname] = pd.Series(index=expected_index, dtype=float, name=colname)
            continue
        raw_series = raw["ntc_mw"]
        hourly = _resample_hourly(raw_series, expected_index, how="mean")
        hourly.name = colname
        cov = _coverage(hourly, expected_index)
        ntc_report[pair_key] = {
            "dropped_by_probe": False,
            "n_raw_points": int(len(raw_series)),
            "coverage": cov,
            "n_empty_chunks": _n_empty_chunks(f"ntc_{pair_key}"),
            "n_error_chunks": _n_error_chunks(f"ntc_{pair_key}"),
        }
        cols[colname] = hourly
    report["ntc"] = ntc_report
    return cols


# ---------------------------------------------------------------- installed capacity

def build_installed_capacity() -> pd.Series:
    """Returns a Series indexed by local calendar year -> total installed MW
    (summed across all production-type columns for that year's row)."""
    raw = _load_series_dir("installed_capacity_type")
    if raw.empty:
        report["installed_capacity_type"] = {"error": "no data cached"}
        return pd.Series(dtype=float)
    raw_local_years = raw.index.tz_convert(TZ).year
    numeric = raw.apply(pd.to_numeric, errors="coerce")
    by_row_total = numeric.sum(axis=1, skipna=True)
    by_row_total.index = raw_local_years
    # multiple raw rows can map to the same local year (see build-time note on
    # the query_installed_generation_capacity truncate-window quirk); keep the
    # first, they should agree since it's the same published annual figure.
    per_year = by_row_total.groupby(level=0).first()
    n_dupe_years = int((by_row_total.groupby(level=0).size() > 1).sum())
    report["installed_capacity_type"] = {
        "n_raw_rows": int(len(raw)),
        "years_found": [int(y) for y in per_year.index],
        "n_years_with_duplicate_raw_rows": n_dupe_years,
        "totals_by_year_mw": {int(y): float(v) for y, v in per_year.items()},
        "columns_found": list(raw.columns),
        "n_empty_chunks": _n_empty_chunks("installed_capacity_type"),
        "n_error_chunks": _n_error_chunks("installed_capacity_type"),
    }
    # save the full per-type table too, for later reuse
    out = raw.copy()
    out.index = raw_local_years
    out.index.name = "year"
    out = out[~out.index.duplicated(keep="first")]
    out.to_csv(DATA_S1 / "installed_capacity_se3.csv")
    return per_year


def build_installed_capacity_per_unit() -> None:
    """query_installed_generation_capacity_per_unit is indexed by generation
    UNIT (EIC string, e.g. Ringhals 4), not by timestamp -- a unit-registry
    snapshot, not a time series. Verified from real data 2026-09-11 (the
    generic _load_series_dir's pd.to_datetime(index) raised on it). Loaded
    separately here, tagged by query year, since capacity may change between
    years (unit retirement/commissioning) -- a plain dedup-by-EIC across
    years would silently discard that."""
    d = DATA_RAW / "installed_capacity_unit"
    frames = []
    if d.exists():
        for f in sorted(d.glob("*.csv")):
            df = pd.read_csv(f, index_col=0)
            df["query_year"] = f.stem
            frames.append(df)
    raw = pd.concat(frames) if frames else pd.DataFrame()
    by_year = {}
    if not raw.empty and "Installed Capacity [MW]" in raw.columns:
        for year, grp in raw.groupby("query_year"):
            by_year[year] = {
                "n_units": int(len(grp)),
                "total_installed_mw": float(pd.to_numeric(grp["Installed Capacity [MW]"], errors="coerce").sum()),
            }
    report["installed_capacity_unit"] = {
        "n_raw_rows": int(len(raw)),
        "columns_found": list(raw.columns),
        "by_query_year": by_year,
        "n_empty_chunks": _n_empty_chunks("installed_capacity_unit"),
        "n_error_chunks": _n_error_chunks("installed_capacity_unit"),
    }
    if not raw.empty:
        raw.index.name = "unit_id"
        raw.to_csv(DATA_S1 / "installed_capacity_per_unit_se3.csv")


def _registry_reconciliation(kept: pd.DataFrame, merged_outages: pd.DataFrame) -> dict:
    """For each generation-outage row, is its production_resource_id present
    in the (>=min_mw-filtered) registry snapshot for the outage's own start
    year? Expected 0 misses (S1b plan step 2) -- if not, list them."""
    if merged_outages.empty or "production_resource_id" not in merged_outages.columns:
        return {"n_checked": 0, "n_missing": 0, "missing_sample": []}
    ids_by_year: dict[str, set] = {}
    if not kept.empty:
        for yr, grp in kept.groupby("query_year"):
            ids_by_year[yr] = set(grp.index)
    outage_years = pd.to_datetime(merged_outages["start"], utc=True).dt.tz_convert(TZ).dt.year.astype(str)
    missing = []
    for oid, oyear in zip(merged_outages["production_resource_id"], outage_years):
        if oid not in ids_by_year.get(oyear, set()):
            missing.append({"production_resource_id": str(oid), "outage_start_year": oyear})
    return {"n_checked": int(len(merged_outages)), "n_missing": len(missing), "missing_sample": missing[:20]}


def build_installed_registry(merged_outages: pd.DataFrame, min_mw: float = 100.0) -> pd.Series:
    """S1b Step 2, user decision 2026-09-11: installed_mw = per-unit registry
    total per year, units >= min_mw only. This is what now feeds avail_gen_mw
    in the table -- build_installed_capacity()'s per-type total (S1 Finding
    5: only ever available for 2026) still runs for its own report section
    and installed_capacity_se3.csv, but no longer reaches the table. The
    >=100MW threshold matches the smallest nominal_power observed in the
    generation-outage feeds (S1b plan, verified against outages_se3.csv), so
    installed and unavailable describe the same fleet -- confirmed below via
    _registry_reconciliation rather than just assumed."""
    d = DATA_RAW / "installed_capacity_unit"
    frames = []
    if d.exists():
        for f in sorted(d.glob("*.csv")):
            df = pd.read_csv(f, index_col=0)
            df["query_year"] = f.stem
            frames.append(df)
    raw = pd.concat(frames) if frames else pd.DataFrame()
    if raw.empty or "Installed Capacity [MW]" not in raw.columns:
        report["installed_registry"] = {"error": "no data cached"}
        return pd.Series(dtype=float)

    raw["Installed Capacity [MW]"] = pd.to_numeric(raw["Installed Capacity [MW]"], errors="coerce")
    kept = raw[raw["Installed Capacity [MW]"] >= min_mw]
    dropped = raw[raw["Installed Capacity [MW]"] < min_mw]

    per_year = kept.groupby("query_year")["Installed Capacity [MW]"].sum()
    per_year.index = per_year.index.astype(int)

    reconciliation = _registry_reconciliation(kept, merged_outages)

    by_year_report = {}
    for y in sorted(set(raw["query_year"])):
        by_year_report[y] = {
            "n_units_kept": int((kept["query_year"] == y).sum()),
            "n_units_dropped": int((dropped["query_year"] == y).sum()),
            "total_installed_mw": float(kept.loc[kept["query_year"] == y, "Installed Capacity [MW]"].sum()),
        }
    report["installed_registry"] = {
        "min_mw_threshold": min_mw,
        "by_query_year": by_year_report,
        "reconciliation_with_outage_feed": reconciliation,
    }
    return per_year


# ---------------------------------------------------------------- outages

DOCSTATUS_EXCLUDE = {"Cancelled", "Withdrawn"}


def _prepare_outages(df: pd.DataFrame) -> pd.DataFrame:
    """Keep the latest revision per mrid FIRST, then drop mrids whose latest
    state is cancelled/withdrawn -- not the other way round. Filtering
    docstatus before picking "latest" would keep a stale pre-cancellation
    revision alive whenever the newest revision is the cancellation itself
    (rev1 Active, rev2 Cancelled -> old code kept rev1; this keeps neither).
    The plan didn't specify the order; this is the only order under which
    "latest revision" actually means the current truth for that mrid.

    Keeps ALL rows at the mrid's max revision, not just one -- CORRECTED
    2026-09-12 from plain groupby("mrid").tail(1), which silently kept only
    the last of several rows whenever the latest revision itself spans
    multiple Series_Periods (common: found on 2 of 18 S2 events plus the
    01b cross-check mrid). tail(1) assumed one row per mrid; that assumption
    was false as soon as _load_series_dir stopped collapsing those periods
    away first (see its dedup="full_row" docstring).

    Also resets to a clean positional index: created_doc_time (the original
    index) is not guaranteed unique across rows, and the event-level overlap
    check in _drop_a80_overlapping_a77 below needs unambiguous .loc access."""
    if df.empty:
        return df
    df = df.reset_index()
    df["revision"] = pd.to_numeric(df["revision"], errors="coerce")
    df = df.sort_values(["mrid", "revision"])
    max_rev = df.groupby("mrid")["revision"].transform("max")
    df = df[df["revision"] == max_rev]
    if "docstatus" in df.columns:
        df = df[~df["docstatus"].isin(DOCSTATUS_EXCLUDE)]
    if not df.empty:
        df["start"] = pd.to_datetime(df["start"], utc=True)
        df["end"] = pd.to_datetime(df["end"], utc=True)
    return df.reset_index(drop=True)


def _drop_a80_overlapping_a77(a80c: pd.DataFrame, a77c: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Drop only the A80 rows that genuinely time-overlap an A77 row for the
    same production_resource_id; A77 wins the overlap. CORRECTED from an
    earlier unit-level rule ("if a unit appears anywhere in A77, drop ALL of
    its A80 rows") that failed the Oskarshamn-3 cross-check below: verified
    2026-09-11 that real event-level overlap between the two feeds is rare
    (8 of 2983 checked (A80,A77) row-pairs across the 5 units common to both
    feeds, all concentrated in one unit/window) -- the unit-level rule threw
    away real, non-duplicate events like Oskarshamn 3's 2025-03-29 ->
    2025-11-02 outage, which has zero A77 rows anywhere near it in time even
    though the *unit* also has unrelated A77 rows from mid-2026."""
    empty_stats = {"n_common_units": 0, "n_a80_rows_dropped_for_overlap": 0}
    if a80c.empty or a77c.empty or "production_resource_id" not in a80c.columns or "production_resource_id" not in a77c.columns:
        return a80c, empty_stats
    common_ids = set(a80c["production_resource_id"]) & set(a77c["production_resource_id"])
    if not common_ids:
        return a80c, empty_stats
    drop_positions = []
    for uid in common_ids:
        rows80 = a80c[a80c["production_resource_id"] == uid]
        rows77 = a77c[a77c["production_resource_id"] == uid]
        for pos, r80 in rows80.iterrows():
            overlap = ((rows77["start"] < r80["end"]) & (r80["start"] < rows77["end"])).any()
            if overlap:
                drop_positions.append(pos)
    stats = {"n_common_units": len(common_ids), "n_a80_rows_dropped_for_overlap": len(drop_positions)}
    return a80c.drop(index=drop_positions), stats


def _sweep_line_unavailable_mw(df: pd.DataFrame, expected_index: pd.DatetimeIndex) -> pd.Series:
    if df.empty:
        return pd.Series(0.0, index=expected_index)
    drop = (pd.to_numeric(df["nominal_power"], errors="coerce") - pd.to_numeric(df["avail_qty"], errors="coerce")).clip(lower=0).fillna(0.0)
    starts = pd.to_datetime(df["start"], utc=True)
    ends = pd.to_datetime(df["end"], utc=True)
    up = pd.Series(drop.to_numpy(), index=starts.to_numpy())
    down = pd.Series(-drop.to_numpy(), index=ends.to_numpy())
    events = pd.concat([up, down]).groupby(level=0).sum().sort_index()
    cum = events.cumsum()
    expected_utc = expected_index.tz_convert("UTC")
    combined = cum.index.union(expected_utc)
    cum_full = cum.reindex(combined).ffill().fillna(0.0)
    result = cum_full.reindex(expected_utc).ffill().fillna(0.0)
    result.index = expected_index
    return result


def _min_active_value(df: pd.DataFrame, value_col: str, expected_index: pd.DatetimeIndex, fill_no_active: float) -> pd.Series:
    """At each timestamp, the MINIMUM of value_col across all rows with
    start <= t < end; fill_no_active where nothing is active.

    Min, not sum (contrast with _sweep_line_unavailable_mw's cumulative sum):
    overlapping rows on one transmission border/element are readings of the
    same underlying capacity constraint (independent concurrent reports, or a
    stricter one nested inside a looser one), not independent reductions to
    add -- unlike generation outages, where different rows are different
    physical units and genuinely additive."""
    if df.empty:
        return pd.Series(fill_no_active, index=expected_index)
    starts = df["start"].to_numpy()
    ends = df["end"].to_numpy()
    values = pd.to_numeric(df[value_col], errors="coerce").to_numpy()
    breakpoints = np.unique(np.concatenate([starts, ends]))
    seg_mins = []
    for i in range(len(breakpoints) - 1):
        seg_start = breakpoints[i]
        active = (starts <= seg_start) & (ends > seg_start)
        seg_mins.append(values[active].min() if active.any() else fill_no_active)
    seg_series = pd.Series(seg_mins, index=breakpoints[:-1])
    expected_utc = expected_index.tz_convert("UTC")
    combined = seg_series.index.union(expected_utc)
    full = seg_series.reindex(combined).ffill()
    full = full.fillna(fill_no_active)  # before the first breakpoint: nothing active yet
    result = full.reindex(expected_utc)
    result.index = expected_index
    return result


def build_outages(expected_index: pd.DatetimeIndex) -> tuple[pd.Series, pd.Series, pd.DataFrame]:
    a80_raw = _load_series_dir("outage_generation_units", dedup="full_row")
    a77_raw = _load_series_dir("outage_production_units", dedup="full_row")

    revision_check = {}
    for name, raw in [("A80", a80_raw), ("A77", a77_raw)]:
        if raw.empty or "mrid" not in raw.columns:
            revision_check[name] = {"n_rows": int(len(raw))}
            continue
        sizes = raw.groupby("mrid").size()
        revision_check[name] = {
            "n_rows": int(len(raw)),
            "n_distinct_mrid": int(len(sizes)),
            "max_rows_per_mrid": int(sizes.max()) if len(sizes) else 0,
            "n_mrid_with_multiple_rows": int((sizes > 1).sum()),
        }
    report["outage_revision_check"] = revision_check

    a80c = _prepare_outages(a80_raw)
    a77c = _prepare_outages(a77_raw)

    ids_80 = set(a80c["production_resource_id"]) if "production_resource_id" in a80c.columns and not a80c.empty else set()
    ids_77 = set(a77c["production_resource_id"]) if "production_resource_id" in a77c.columns and not a77c.empty else set()
    only_80 = ids_80 - ids_77
    only_77 = ids_77 - ids_80

    unavail_80 = _sweep_line_unavailable_mw(a80c, expected_index)
    unavail_77 = _sweep_line_unavailable_mw(a77c, expected_index)
    correlation = float(unavail_80.corr(unavail_77)) if unavail_80.std() > 0 and unavail_77.std() > 0 else None

    a80_kept, overlap_stats = _drop_a80_overlapping_a77(a80c, a77c)

    report["outage_a80_vs_a77"] = {
        "n_ids_a80": len(ids_80),
        "n_ids_a77": len(ids_77),
        "n_ids_only_a80": len(only_80),
        "n_ids_only_a77": len(only_77),
        "n_ids_both": len(ids_80 & ids_77),
        "ids_only_a80_sample": sorted(only_80)[:20],
        "ids_only_a77_sample": sorted(only_77)[:20],
        "unavail_mw_correlation_a80_vs_a77": correlation,
        "n_common_units": overlap_stats["n_common_units"],
        "n_a80_rows_dropped_for_time_overlap_with_a77": overlap_stats["n_a80_rows_dropped_for_overlap"],
        "decision": "union of A77 and A80, EXCEPT an A80 row is dropped when it genuinely time-overlaps an A77 row for the same production_resource_id (A77 wins that overlap) -- see _drop_a80_overlapping_a77 docstring for why a unit-level exclusion was tried first and rejected",
    }

    merged = pd.concat([a77c, a80_kept], ignore_index=True) if not (a77c.empty and a80_kept.empty) else pd.DataFrame()

    unavail_mw = _sweep_line_unavailable_mw(merged, expected_index)
    unavail_mw.name = "unavail_mw"

    if "plant_type" in merged.columns:
        nuclear = merged[merged["plant_type"] == "Nuclear"]
    else:
        nuclear = merged.iloc[0:0]
    unavail_nuclear_mw = _sweep_line_unavailable_mw(nuclear, expected_index)
    unavail_nuclear_mw.name = "unavail_nuclear_mw"

    report["outage_merge"] = {
        "n_rows_a80_prepared": int(len(a80c)),
        "n_rows_a77_prepared": int(len(a77c)),
        "n_rows_a80_kept_after_overlap_drop": int(len(a80_kept)),
        "n_rows_merged": int(len(merged)),
        "n_rows_nuclear_in_merged": int(len(nuclear)),
        "max_unavail_mw": float(unavail_mw.max()) if len(unavail_mw) else None,
        "max_unavail_nuclear_mw": float(unavail_nuclear_mw.max()) if len(unavail_nuclear_mw) else None,
        "n_empty_chunks_a80": _n_empty_chunks("outage_generation_units"),
        "n_error_chunks_a80": _n_error_chunks("outage_generation_units"),
        "n_empty_chunks_a77": _n_empty_chunks("outage_production_units"),
        "n_error_chunks_a77": _n_error_chunks("outage_production_units"),
    }

    # save the full raw (both sources, all revisions/statuses, tagged) record
    tagged = []
    if not a80_raw.empty:
        t = a80_raw.copy()
        t["source"] = "A80"
        tagged.append(t)
    if not a77_raw.empty:
        t = a77_raw.copy()
        t["source"] = "A77"
        tagged.append(t)
    if tagged:
        pd.concat(tagged).sort_index().to_csv(DATA_S1 / "outages_se3.csv")

    return unavail_mw, unavail_nuclear_mw, merged


def build_transmission_outages() -> dict[str, pd.DataFrame]:
    """Extended for S1b: runs _prepare_outages (reused as-is -- it only
    touches mrid/revision/docstatus/start/end, none of which are A80/A77-
    specific) on each pair, reports the mrid-multiplicity check the plan
    asks for, and tags every raw row in transmission_outages_se3.csv with a
    'kept' flag (True iff its exact (mrid, revision) survived preparation --
    not just 'iff its mrid appears at all', since only the latest revision
    per mrid is actually kept). Returns {pair_key: prepared_df} so
    build_transmission_availability and the cross-check reuse the same
    prepared rows instead of re-deriving them."""
    tagged = []
    prepared_by_pair: dict[str, pd.DataFrame] = {}
    for pair_key in PAIR_KEYS:
        raw = _load_series_dir(f"transmission_outage_{pair_key}", dedup="full_row")
        n_empty = _n_empty_chunks(f"transmission_outage_{pair_key}")
        n_error = _n_error_chunks(f"transmission_outage_{pair_key}")
        mult = {}
        if not raw.empty and "mrid" in raw.columns:
            sizes = raw.groupby("mrid").size()
            mult = {
                "n_distinct_mrid": int(len(sizes)),
                "max_rows_per_mrid": int(sizes.max()),
                "n_mrid_with_multiple_rows": int((sizes > 1).sum()),
            }
        report.setdefault("transmission_outages", {})[pair_key] = {
            "n_rows": int(len(raw)),
            "empty": bool(n_empty),
            "error": bool(n_error),
            **mult,
        }
        if raw.empty:
            continue

        prepared = _prepare_outages(raw)
        prepared_by_pair[pair_key] = prepared

        t = raw.copy()
        t["pair"] = pair_key
        if not prepared.empty and {"mrid", "revision"}.issubset(prepared.columns):
            kept_keys = set(zip(prepared["mrid"], prepared["revision"]))
            t_revision = pd.to_numeric(t["revision"], errors="coerce")
            t["kept"] = [(m, r) in kept_keys for m, r in zip(t["mrid"], t_revision)]
        else:
            t["kept"] = False
        tagged.append(t)
    if tagged:
        pd.concat(tagged).sort_index().to_csv(DATA_S1 / "transmission_outages_se3.csv")
    return prepared_by_pair


def build_transmission_availability(prepared_by_pair: dict[str, pd.DataFrame], expected_index: pd.DatetimeIndex) -> dict[str, pd.Series]:
    """S1b Step 3, user decision 2026-09-11: replaces NTC with an hourly
    transmission-availability driver built from A78 outage reports, since
    NTC does not exist for any SE3 border at any point in the pulled window
    -- S1 Finding 4, and confirmed again by S1b's own pre-flow-based probe
    (notes/01b Finding 1)."""
    pull_summary_path = DATA_S1 / "pull_summary.json"
    ps = json.loads(pull_summary_path.read_text(encoding="utf-8")) if pull_summary_path.exists() else {}
    prefb_alive = ps.get("ntc_probe_alive_prefb", {})

    cols: dict[str, pd.Series] = {}
    pair_report: dict[str, dict] = {}

    for pair_key in PAIR_KEYS:
        if pair_key == "se3_se2":
            pair_report[pair_key] = {
                "excluded_from_table": True,
                "note": "S1 Finding 9: zero rows for this direction across the whole pull (reverse, se2_se3, has 305) -- not modelled as its own column, per the plan's 9-pair list",
            }
            continue

        prepared = prepared_by_pair.get(pair_key, pd.DataFrame())
        colname = f"unavail_tx_{pair_key}_mw"
        nominal = float(pd.to_numeric(prepared["avail_qty"], errors="coerce").max()) if not prepared.empty else None

        ntc_p99 = None
        if prefb_alive.get(pair_key, False):
            ntc_raw = _load_series_dir(f"ntc_{pair_key}")
            if not ntc_raw.empty and "ntc_mw" in ntc_raw.columns:
                ntc_p99 = float(pd.to_numeric(ntc_raw["ntc_mw"], errors="coerce").quantile(0.99))

        pair_report[pair_key] = {
            "excluded_from_table": False,
            "n_kept_rows": int(len(prepared)),
            "nominal_mw_proxy": nominal,
            "nominal_mw_proxy_note": "max avail_qty over this pair's kept A78 rows -- a proxy for normal capacity, not a real nominal-capacity figure",
            "prefb_ntc_available": bool(prefb_alive.get(pair_key, False)),
            "prefb_ntc_p99_mw": ntc_p99,
        }

        if prepared.empty or nominal is None:
            cols[colname] = pd.Series(0.0, index=expected_index, name=colname)
            continue

        min_avail = _min_active_value(prepared, "avail_qty", expected_index, fill_no_active=nominal)
        unavail = (nominal - min_avail).clip(lower=0)
        unavail.name = colname
        cols[colname] = unavail

    report["transmission_availability"] = pair_report
    return cols


def build_ntc_validation(tx_cols: dict[str, pd.Series], expected_index: pd.DatetimeIndex) -> dict:
    """S1b Step 3 last bullet: compare the derived transmission-availability
    driver against real pre-flow-based NTC where it exists. As of this run it
    doesn't exist for any pair (notes/01b Finding 1) -- reported explicitly
    per external pair rather than silently skipped."""
    pull_summary_path = DATA_S1 / "pull_summary.json"
    ps = json.loads(pull_summary_path.read_text(encoding="utf-8")) if pull_summary_path.exists() else {}
    prefb_alive = ps.get("ntc_probe_alive_prefb", {})

    external_pairs = (
        [f"se3_{b.lower().replace('_', '')}" for b in EXTERNAL_BORDERS]
        + [f"{b.lower().replace('_', '')}_se3" for b in EXTERNAL_BORDERS]
    )
    prefb_start = pd.Timestamp(WINDOW_START, tz=TZ)
    prefb_end = pd.Timestamp(NTC_FB_GO_LIVE, tz=TZ)
    prefb_mask = (expected_index >= prefb_start) & (expected_index < prefb_end)

    validation = {}
    for pair_key in external_pairs:
        colname = f"unavail_tx_{pair_key}_mw"
        if not prefb_alive.get(pair_key, False):
            validation[pair_key] = {"prefb_ntc_available": False, "note": "no pre-flow-based NTC data (notes/01b Finding 1)"}
            continue
        ntc_raw = _load_series_dir(f"ntc_{pair_key}")
        if ntc_raw.empty or "ntc_mw" not in ntc_raw.columns or colname not in tx_cols:
            validation[pair_key] = {"prefb_ntc_available": True, "note": "probe alive but no usable NTC data or driver column"}
            continue
        ntc_hourly = _resample_hourly(ntc_raw["ntc_mw"], expected_index, how="mean")
        nominal = report.get("transmission_availability", {}).get(pair_key, {}).get("nominal_mw_proxy")
        if nominal is None:
            validation[pair_key] = {"prefb_ntc_available": True, "note": "no nominal_mw_proxy available for this pair"}
            continue
        derived_avail = nominal - tx_cols[colname]

        ntc_w = ntc_hourly[prefb_mask].dropna()
        derived_w = derived_avail[prefb_mask]
        joined = pd.concat([ntc_w, derived_w], axis=1, join="inner").dropna()
        corr = float(joined.iloc[:, 0].corr(joined.iloc[:, 1])) if len(joined) > 1 else None

        ntc_prefb_all = ntc_hourly[prefb_mask].dropna()
        if len(ntc_prefb_all) > 1:
            p99 = float(ntc_prefb_all.quantile(0.99))
            below_p99 = ntc_hourly[prefb_mask] < p99
            active_a78 = tx_cols[colname][prefb_mask] > 0
            share = float((below_p99 & active_a78).sum() / below_p99.sum()) if below_p99.sum() > 0 else None
        else:
            p99, share = None, None

        validation[pair_key] = {
            "prefb_ntc_available": True,
            "n_overlapping_hours": int(len(joined)),
            "pearson_correlation_ntc_vs_derived_avail": corr,
            "ntc_p99_mw": p99,
            "share_of_below_p99_ntc_hours_with_active_a78_row": share,
        }
    report["ntc_validation"] = validation
    return validation


def transmission_cross_check(prepared_by_pair: dict[str, pd.DataFrame], tx_cols: dict[str, pd.Series]) -> dict:
    """S1b Step 4: the plan's named cross-check -- the longest kept UNPLANNED
    outage on no1_se3 or fi_se3 should be exactly reflected in the
    corresponding unavail_tx_* column throughout its window (nominal minus
    its own avail_qty), unless another overlapping row is more restrictive
    somewhere in that window (a real min-aggregation case, not a bug)."""
    candidates = []
    for pair_key in ["no1_se3", "fi_se3"]:
        prepared = prepared_by_pair.get(pair_key, pd.DataFrame())
        if prepared.empty or "businesstype" not in prepared.columns:
            continue
        unplanned = prepared[prepared["businesstype"] != "Planned maintenance"].copy()
        if unplanned.empty:
            continue
        unplanned["duration_h"] = (unplanned["end"] - unplanned["start"]).dt.total_seconds() / 3600.0
        row = unplanned.loc[unplanned["duration_h"].idxmax()]
        candidates.append((pair_key, row))
    if not candidates:
        return {"found": False, "note": "no unplanned kept rows found on no1_se3 or fi_se3"}

    pair_key, row = max(candidates, key=lambda x: x[1]["duration_h"])
    colname = f"unavail_tx_{pair_key}_mw"
    nominal = report.get("transmission_availability", {}).get(pair_key, {}).get("nominal_mw_proxy")
    expected_unavail = (nominal - float(row["avail_qty"])) if nominal is not None else None

    series = tx_cols.get(colname)
    if series is None or expected_unavail is None:
        return {"found": True, "pair": pair_key, "note": "driver column or nominal missing, cannot verify"}

    window_mask = (series.index >= row["start"]) & (series.index < row["end"])
    window_vals = series[window_mask]
    n_total = int(len(window_vals))
    n_match = int(np.isclose(window_vals.to_numpy(), expected_unavail, atol=1e-6).sum()) if n_total else 0

    return {
        "found": True,
        "pair": pair_key,
        "mrid": str(row["mrid"]),
        "businesstype": str(row.get("businesstype")),
        "start": str(row["start"]),
        "end": str(row["end"]),
        "duration_hours": float(row["duration_h"]),
        "avail_qty_mw": float(row["avail_qty"]),
        "nominal_mw_proxy": nominal,
        "expected_unavail_mw": expected_unavail,
        "n_hours_in_window": n_total,
        "n_hours_matching_exactly": n_match,
        "all_hours_match": n_match == n_total and n_total > 0,
        "note": "if not all match, another overlapping A78 row is more restrictive somewhere in this window (min-aggregation, not a bug)",
    }


def build_ntc_prefb_table() -> None:
    """S1b: pre-flow-based NTC (2023-01-01 to 2024-10-29), kept OUT of the
    main modelling table per the user's decision -- written to its own file,
    only for pairs where the pre-FB probe actually found data. As of this
    run, none did (notes/01b Finding 1)."""
    pull_summary_path = DATA_S1 / "pull_summary.json"
    ps = json.loads(pull_summary_path.read_text(encoding="utf-8")) if pull_summary_path.exists() else {}
    prefb_alive = ps.get("ntc_probe_alive_prefb", {})
    alive_pairs = [pk for pk in PAIR_KEYS if prefb_alive.get(pk, False)]
    report["ntc_prefb"] = {"alive_pairs": alive_pairs, "probe_results": prefb_alive}
    if not alive_pairs:
        report["ntc_prefb"]["file_written"] = False
        report["ntc_prefb"]["note"] = "no pre-flow-based NTC data for any pair; ntc_prefb_se3.csv not created"
        return

    prefb_index = pd.date_range(WINDOW_START, NTC_FB_GO_LIVE, freq="h", tz=TZ, inclusive="left")
    cols = {}
    for pair_key in alive_pairs:
        raw = _load_series_dir(f"ntc_{pair_key}")
        if raw.empty or "ntc_mw" not in raw.columns:
            continue
        hourly = _resample_hourly(raw["ntc_mw"], prefb_index, how="mean")
        hourly.name = f"ntc_{pair_key}"
        cols[f"ntc_{pair_key}"] = hourly
    if cols:
        prefb_table = pd.concat(list(cols.values()), axis=1)
        prefb_table.index.name = "timestamp"
        prefb_table.to_csv(DATA_S1 / "ntc_prefb_se3.csv")
        report["ntc_prefb"]["file_written"] = True
        report["ntc_prefb"]["n_rows"] = len(prefb_table)
    else:
        report["ntc_prefb"]["file_written"] = False


# ---------------------------------------------------------------- hydro

def build_hydro(expected_index: pd.DatetimeIndex) -> pd.Series:
    raw = _load_series_dir("hydro_reservoir")
    if raw.empty:
        report["hydro_reservoir"] = {"error": "no data cached"}
        return pd.Series(index=expected_index, dtype=float, name="hydro_reservoir_fill")
    report["hydro_reservoir_columns_found"] = list(raw.columns)
    if raw.shape[1] >= 1:
        col = raw.columns[0]
        s = pd.to_numeric(raw[col], errors="coerce")
    else:
        s = pd.Series(dtype=float)
    hourly = _resample_hourly(s, expected_index, how="ffill")
    hourly.name = "hydro_reservoir_fill"
    report["hydro_reservoir"] = {
        "source_column_used": raw.columns[0] if raw.shape[1] >= 1 else None,
        "n_raw_points": int(len(raw)),
        "note": "weekly-sourced, forward-filled to hourly",
        "n_empty_chunks": _n_empty_chunks("hydro_reservoir"),
        "n_error_chunks": _n_error_chunks("hydro_reservoir"),
    }
    return hourly


# ---------------------------------------------------------------- sanity checks

def sanity_checks(table: pd.DataFrame) -> dict:
    checks = {}
    if "price_eur_mwh" in table.columns:
        checks["n_negative_price_hours"] = int((table["price_eur_mwh"] < 0).sum())
    if "load_forecast_mw" in table.columns:
        lf = table["load_forecast_mw"].dropna()
        checks["load_forecast_in_5000_20000_mw"] = bool(((lf >= 5000) & (lf <= 20000)).all()) if len(lf) else None
        checks["load_forecast_out_of_range_count"] = int((~((lf >= 5000) & (lf <= 20000))).sum()) if len(lf) else None
    if "wind_onshore_forecast_mw" in table.columns:
        w = table["wind_onshore_forecast_mw"].dropna()
        checks["wind_onshore_min"] = float(w.min()) if len(w) else None
        checks["wind_onshore_max"] = float(w.max()) if len(w) else None
        checks["wind_onshore_negative_count"] = int((w < 0).sum()) if len(w) else None
    if "installed_mw" in table.columns:
        # S1b: now registry-based (build_installed_registry), so unlike the
        # old per-type-only figure this should cover the FULL window, not
        # just 2026 onward (S1 Finding 5).
        checks["installed_mw_n_nan"] = int(table["installed_mw"].isna().sum())
        checks["installed_mw_fully_populated_full_window"] = bool(table["installed_mw"].isna().sum() == 0)
    if "avail_gen_mw" in table.columns:
        ag = table["avail_gen_mw"].dropna()
        checks["avail_gen_mw_negative_count"] = int((ag < 0).sum()) if len(ag) else None
        checks["avail_gen_mw_min"] = float(ag.min()) if len(ag) else None
        checks["avail_gen_mw_min_timestamp"] = str(ag.idxmin()) if len(ag) else None
        checks["avail_gen_mw_no_negative_full_window"] = bool((ag < 0).sum() == 0) if len(ag) else None
    if "unavail_nuclear_mw" in table.columns:
        # Oskarshamn 3's real outage (mrid Nilntk4-ZifAIdxNM5sUkA, verified in
        # the pulled A80 data) runs 2025-03-29 16:33 UTC -> 2025-11-02 07:16
        # UTC. Using a date-only slice from "2025-03-29" would include hours
        # *before* 16:33 that day, when the outage hadn't started yet, and
        # wrongly fail this check on a legitimate pre-outage 0 -- caught by
        # running this against real data 2026-09-11. Buffered a full day
        # inside the true window on each side instead.
        window = table.loc["2025-03-30 12:00":"2025-11-01 12:00", "unavail_nuclear_mw"] if len(table) else pd.Series(dtype=float)
        checks["oskarshamn3_window_min_unavail_nuclear_mw"] = float(window.min()) if len(window) else None
        checks["oskarshamn3_cross_check_pass"] = bool(len(window) and window.min() >= 1400)
    return checks


# ---------------------------------------------------------------- main

def main() -> None:
    expected_index = _expected_index()
    report["expected_index"] = {
        "n_rows": len(expected_index),
        "start": str(expected_index.min()),
        "end": str(expected_index.max()),
    }

    price = build_price(expected_index)
    load_forecast = build_load_forecast(expected_index)
    solar, wind_onshore = build_wind_solar(expected_index)

    # S1b: NTC is no longer in the table (S1 Finding 4 + notes/01b Finding 1).
    # build_ntc still runs so report["ntc"] (the original 2025 probe result)
    # stays in the coverage report; its columns are just not used below.
    build_ntc(expected_index)
    build_ntc_prefb_table()

    build_installed_capacity()  # per-type: report + installed_capacity_se3.csv only (S1 Finding 5)
    build_installed_capacity_per_unit()
    unavail_mw, unavail_nuclear_mw, merged_outages = build_outages(expected_index)
    # S1b Step 2: registry-based installed_mw now feeds the table, not the
    # per-type total above (which S1 Finding 5 showed only covers 2026).
    installed_by_year = build_installed_registry(merged_outages, min_mw=100.0)

    prepared_tx_by_pair = build_transmission_outages()
    tx_cols = build_transmission_availability(prepared_tx_by_pair, expected_index)
    build_ntc_validation(tx_cols, expected_index)
    report["transmission_cross_check"] = transmission_cross_check(prepared_tx_by_pair, tx_cols)

    hydro = build_hydro(expected_index)

    installed_hourly = pd.Series(
        [installed_by_year.get(y, float("nan")) for y in expected_index.year],
        index=expected_index, name="installed_mw",
    )
    avail_gen_mw = (installed_hourly - unavail_mw).rename("avail_gen_mw")

    export_series = [tx_cols[f"unavail_tx_{pk}_mw"] for pk in EXPORT_PAIR_KEYS if f"unavail_tx_{pk}_mw" in tx_cols]
    import_series = [tx_cols[f"unavail_tx_{pk}_mw"] for pk in IMPORT_PAIR_KEYS if f"unavail_tx_{pk}_mw" in tx_cols]
    unavail_tx_import_mw = (
        pd.concat(import_series, axis=1).sum(axis=1) if import_series else pd.Series(0.0, index=expected_index)
    ).rename("unavail_tx_import_mw")
    unavail_tx_export_mw = (
        pd.concat(export_series, axis=1).sum(axis=1) if export_series else pd.Series(0.0, index=expected_index)
    ).rename("unavail_tx_export_mw")

    table = pd.concat(
        [price, load_forecast, solar, wind_onshore,
         installed_hourly, unavail_mw, unavail_nuclear_mw, avail_gen_mw]
        + [tx_cols[c] for c in sorted(tx_cols)]
        + [unavail_tx_import_mw, unavail_tx_export_mw, hydro],
        axis=1,
    )
    table.index.name = "timestamp"
    table.to_csv(DATA_S1 / "se3_hourly.csv")

    # per-column coverage for every column of the assembled table, including
    # the ones (installed_mw, unavail_mw, unavail_nuclear_mw, avail_gen_mw,
    # hydro_reservoir_fill) that build_* above don't already cover inline.
    report["coverage_by_column"] = {
        col: _coverage(table[col], expected_index) for col in table.columns
    }
    report["duplicate_timestamps_in_final_index"] = int(table.index.duplicated().sum())

    checks = sanity_checks(table)
    report["sanity_checks"] = checks

    out = DATA_S1 / "coverage_report.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(f"Wrote {DATA_S1 / 'se3_hourly.csv'} ({len(table)} rows, {len(table.columns)} columns)")
    print(f"Columns: {list(table.columns)}")
    print(f"Wrote {out}")
    print("Sanity checks:")
    for k, v in checks.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
