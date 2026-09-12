"""S1: pull the full 2023-01-01 -> present numerical history for SE3.

Idempotent: every (series, chunk) outcome -- success, "no matching data", or
error -- is cached under data/raw/s1/<series>/<chunk>.{csv,empty,error.json}.
A re-run skips anything already on disk and makes (close to) zero real HTTP
requests once everything is cached; see the request counter printed at the
end (from an instrumented requests.Session, not just our own call count --
see common.CountingSession for why the two differ).

Run: uv run python src/s1_pull_history.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
from entsoe.exceptions import NoMatchingDataError

from common import ROOT, _redact, make_client, throttled

DATA_RAW = ROOT / "data" / "raw" / "s1"
DATA_RAW.mkdir(parents=True, exist_ok=True)
DATA_S1 = ROOT / "data" / "s1"
DATA_S1.mkdir(parents=True, exist_ok=True)

ZONE_CODE = "SE_3"
TZ = "Europe/Stockholm"

WINDOW_START = "2021-01-01"
WINDOW_END = "2026-09-12"  # exclusive right edge -> captures all of "present" (2026-09-11)

# SE3's five real neighbours, verified against entsoe.mappings.NEIGHBOURS['SE_3']
# (.venv/Lib/site-packages/entsoe/mappings.py:345) on 2026-09-11 -- matches the
# plan's border list exactly, so nothing here is guessed.
BORDERS = ["SE_2", "SE_4", "NO_1", "FI", "DK_1"]
# (pair_key, from_zone, to_zone) for each border, both directions. pair_key
# strips underscores from the neighbour code (SE_2 -> se2) to match the
# plan's stated column naming, e.g. ntc_se3_no1 / ntc_no1_se3.
PAIRS = [
    (f"se3_{b.lower().replace('_', '')}" if direction == "out" else f"{b.lower().replace('_', '')}_se3", frm, to)
    for b in BORDERS
    for direction, (frm, to) in [("out", (ZONE_CODE, b)), ("in", (b, ZONE_CODE))]
]

NTC_PROBE_DAY = "2025-09-15"  # same known-good day as S0's HOURLY_DAY

# S1b: Nordic flow-based day-ahead coupling went live for delivery day 2024-10-30
# (Svenska kraftnat / nordicbalancingmodel.net), after which day-ahead capacity is
# no longer described at border level. S1's only NTC probe was 2025-09-15, after
# the switch, so pre-flow-based NTC (2023-01-01 to 2024-10-29) was never even
# requested. This second, earlier probe day checks whether it exists.
NTC_PROBE_DAY_PREFB = "2024-06-15"
NTC_FB_GO_LIVE = "2024-10-30"

# S1b step 1b: diagnostic only -- is the S1 Finding 5 installed-capacity-per-type
# (A68) gap specific to SE3, or does it affect other Swedish zones / the country
# level too? Changes nothing in the modelling table.
A68_PROBE_ZONES = ["SE_1", "SE_2", "SE_4", "SE"]
A68_PROBE_YEARS = [2023, 2024, 2025]
A68_PROBE_DAY_MMDD = "06-15"

pull_summary: dict[str, object] = {}
t_start = time.time()


# ---------------------------------------------------------------- chunking

def _year_chunks(start: str, end: str, tz: str = TZ) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    s = pd.Timestamp(start, tz=tz)
    e = pd.Timestamp(end, tz=tz)
    chunks = []
    cur = pd.Timestamp(year=s.year, month=1, day=1, tz=tz)
    if cur < s:
        cur = s
    while cur < e:
        nxt = pd.Timestamp(year=cur.year + 1, month=1, day=1, tz=tz)
        chunks.append((cur, min(nxt, e)))
        cur = nxt
    return chunks


def _month_chunks(start: str, end: str, tz: str = TZ) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    s = pd.Timestamp(start, tz=tz)
    e = pd.Timestamp(end, tz=tz)
    chunks = []
    cur = s
    while cur < e:
        nxt = cur + pd.DateOffset(months=1)
        chunks.append((cur, min(nxt, e)))
        cur = nxt
    return chunks


def _year_key(chunk_start: pd.Timestamp) -> str:
    return str(chunk_start.year)


def _month_key(chunk_start: pd.Timestamp) -> str:
    return f"{chunk_start.year}-{chunk_start.month:02d}"


# ---------------------------------------------------------------- cache I/O

def _series_dir(series: str) -> Path:
    d = DATA_RAW / series
    d.mkdir(parents=True, exist_ok=True)
    return d


def _outcome_kind(series: str, key: str) -> str | None:
    d = _series_dir(series)
    if (d / f"{key}.csv").exists():
        return "ok"
    if (d / f"{key}.empty").exists():
        return "empty"
    if (d / f"{key}.error.json").exists():
        return "error"
    return None


def _save_result(series: str, key: str, obj, colname: str | None = None) -> None:
    if isinstance(obj, pd.Series):
        if colname:
            obj = obj.copy()
            obj.name = colname
        df = obj.to_frame()
    else:
        df = obj
    if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None:
        # Normalise to UTC before caching so a mixed CET/CEST offset column
        # (Stockholm crosses DST inside almost every yearly chunk) round-trips
        # unambiguously through pd.read_csv(..., parse_dates=True) later.
        df = df.tz_convert("UTC")
    df.to_csv(_series_dir(series) / f"{key}.csv")


def _save_empty(series: str, key: str) -> None:
    (_series_dir(series) / f"{key}.empty").write_text("NoMatchingDataError", encoding="utf-8")


def _save_error(series: str, key: str, e: Exception) -> None:
    (_series_dir(series) / f"{key}.error.json").write_text(
        json.dumps({"type": type(e).__name__, "error": _redact(str(e))}, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------- runner

def run_chunked(client, series: str, chunks, key_fn, call, colname: str | None = None) -> dict:
    """Call call(client, start, end) once per chunk, caching every outcome
    (ok / empty / error) so a re-run skips it. Updates pull_summary[series]."""
    n_ok = n_empty = n_fail = n_cached = 0
    for start, end in chunks:
        key = key_fn(start)
        kind = _outcome_kind(series, key)
        if kind is not None:
            n_cached += 1
            continue
        try:
            obj = throttled(call, client, start, end)
            n_rows = len(obj)
            _save_result(series, key, obj, colname=colname)
            n_ok += 1
            print(f"  [{series}] {key}: ok ({n_rows} rows)")
        except NoMatchingDataError:
            _save_empty(series, key)
            n_empty += 1
            print(f"  [{series}] {key}: empty")
        except Exception as e:  # noqa: BLE001 -- intentionally broad, see module docstring
            _save_error(series, key, e)
            n_fail += 1
            print(f"  [{series}] {key}: FAILED - {_redact(str(e))[:150]}")
    summary = {
        "n_chunks": len(chunks),
        "n_ok": n_ok,
        "n_empty": n_empty,
        "n_fail": n_fail,
        "n_cached": n_cached,
    }
    pull_summary[series] = summary
    return summary


# ---------------------------------------------------------------- per-series calls

def call_price(client, start, end):
    return client.query_day_ahead_prices(ZONE_CODE, start=start, end=end)


def call_load_forecast(client, start, end):
    return client.query_load_forecast(ZONE_CODE, start=start, end=end, process_type="A01")


def call_wind_solar(client, start, end):
    return client.query_wind_and_solar_forecast(
        ZONE_CODE, start=start, end=end, psr_type=None, process_type="A01"
    )


def call_installed_capacity_type(client, start, end):
    return client.query_installed_generation_capacity(ZONE_CODE, start=start, end=end)


def call_installed_capacity_unit(client, start, end):
    return client.query_installed_generation_capacity_per_unit(ZONE_CODE, start=start, end=end)


def call_outage_generation(client, start, end):
    return client.query_unavailability_of_generation_units(ZONE_CODE, start=start, end=end)


def call_outage_production(client, start, end):
    return client.query_unavailability_of_production_units(ZONE_CODE, start=start, end=end)


def call_hydro(client, start, end):
    return client.query_aggregate_water_reservoirs_and_hydro_storage(ZONE_CODE, start=start, end=end)


def make_installed_capacity_call(zone: str):
    def call(client, start, end):
        return client.query_installed_generation_capacity(zone, start=start, end=end)

    return call


def make_ntc_call(frm: str, to: str):
    def call(client, start, end):
        return client.query_net_transfer_capacity_dayahead(frm, to, start=start, end=end)

    return call


def make_transmission_call(frm: str, to: str):
    def call(client, start, end):
        return client.query_unavailability_transmission(frm, to, start=start, end=end)

    return call


# ---------------------------------------------------------------- border-pair series

def ntc_probe_alive(client) -> dict[str, bool]:
    """One-day probe per border-direction (cached); drop pairs with no data."""
    day_s = pd.Timestamp(NTC_PROBE_DAY, tz=TZ)
    day_e = day_s + pd.Timedelta(days=1)
    alive: dict[str, bool] = {}
    for pair_key, frm, to in PAIRS:
        series = f"ntc_probe_{pair_key}"
        run_chunked(client, series, [(day_s, day_e)], lambda _st: "probe", make_ntc_call(frm, to))
        alive[pair_key] = _outcome_kind(series, "probe") == "ok"
    print(f"  NTC probe results: {alive}")
    pull_summary["ntc_probe_alive"] = alive
    dropped = [k for k, v in alive.items() if not v]
    if dropped:
        print(f"  Dropping NTC year-pulls for pairs with no probe data: {dropped}")
    return alive


def ntc_years(client, alive: dict[str, bool], year_chunks) -> None:
    for pair_key, frm, to in PAIRS:
        if not alive.get(pair_key, False):
            continue
        run_chunked(
            client, f"ntc_{pair_key}", year_chunks, _year_key,
            make_ntc_call(frm, to), colname="ntc_mw",
        )


def ntc_probe_alive_prefb(client) -> dict[str, bool]:
    """S1b: second probe, on a day before the 2024-10-30 flow-based go-live.
    Separate series key (ntc_prefb_probe_<pair>) so it doesn't collide with
    the S1 2025-09-15 probe cache (ntc_probe_<pair>), which stays as-is."""
    day_s = pd.Timestamp(NTC_PROBE_DAY_PREFB, tz=TZ)
    day_e = day_s + pd.Timedelta(days=1)
    alive: dict[str, bool] = {}
    for pair_key, frm, to in PAIRS:
        series = f"ntc_prefb_probe_{pair_key}"
        run_chunked(client, series, [(day_s, day_e)], lambda _st: "probe", make_ntc_call(frm, to))
        alive[pair_key] = _outcome_kind(series, "probe") == "ok"
    print(f"  NTC pre-flow-based probe results ({NTC_PROBE_DAY_PREFB}): {alive}")
    pull_summary["ntc_probe_alive_prefb"] = alive
    dropped = [k for k, v in alive.items() if not v]
    if dropped:
        print(f"  Dropping pre-FB NTC year-pulls for pairs with no probe data: {dropped}")
    return alive


def a68_diagnostic_probe(client) -> dict:
    """S1b step 1b, diagnostic only: probe query_installed_generation_capacity
    for other Swedish zones and the country level, one day per year, to see
    whether S1 Finding 5's 2023-2025 gap is SE3-specific. Writes nothing into
    the modelling table -- report-only, cached under data/raw/s1/a68_probe_<zone>/."""
    results: dict[str, dict[str, str | None]] = {}
    for zone in A68_PROBE_ZONES:
        for year in A68_PROBE_YEARS:
            day = pd.Timestamp(f"{year}-{A68_PROBE_DAY_MMDD}", tz=TZ)
            day_e = day + pd.Timedelta(days=1)
            series = f"a68_probe_{zone}"
            key = str(year)
            run_chunked(client, series, [(day, day_e)], lambda _st, k=key: k, make_installed_capacity_call(zone))
            results.setdefault(zone, {})[key] = _outcome_kind(series, key)
    pull_summary["a68_probe_results"] = results
    print(f"  A68 diagnostic probe results: {results}")
    return results


def transmission_outages(client, year_chunks) -> None:
    """CORRECTED from the plan: the plan assumed entsoe-py's @paginated
    decorator would auto-bisect an oversized (~4-year) query for this
    endpoint. Verified false by direct test 2026-09-11: a ~4-year span
    returns a real HTTP 400, but its error text doesn't match either of the
    two substrings @paginated's PaginationError check requires
    (entsoe/decorators.py), so the exception propagates unbisected and the
    whole pull fails for all 10 pairs. A 1-year window works (tested: 56
    rows for SE3-NO1/2025). Year-chunked here instead, same as the other
    endpoints."""
    for pair_key, frm, to in PAIRS:
        run_chunked(
            client, f"transmission_outage_{pair_key}", year_chunks, _year_key,
            make_transmission_call(frm, to),
        )


# ---------------------------------------------------------------- main

def main() -> None:
    client = make_client()
    year_chunks = _year_chunks(WINDOW_START, WINDOW_END)
    month_chunks = _month_chunks(WINDOW_START, WINDOW_END)
    prefb_year_chunks = _year_chunks(WINDOW_START, NTC_FB_GO_LIVE)

    n_pairs = len(PAIRS)
    est_top_level_calls = (
        len(year_chunks)          # price
        + len(month_chunks) * 2   # load forecast + wind/solar forecast
        + n_pairs                 # NTC probes
        + n_pairs * len(year_chunks)  # NTC years (upper bound; dead pairs get dropped)
        + len(year_chunks) * 2    # installed capacity, type + per-unit
        + len(year_chunks) * 2    # A80 + A77 outages
        + n_pairs * len(year_chunks)  # transmission outages, year-chunked per pair
        + len(year_chunks)        # hydro reservoir
        + n_pairs                 # S1b: pre-FB NTC probes
        + n_pairs * len(prefb_year_chunks)  # S1b: pre-FB NTC years (upper bound)
        + len(A68_PROBE_ZONES) * len(A68_PROBE_YEARS)  # S1b: A68 diagnostic probes
    )

    print(f"Zone: {ZONE_CODE}, timezone {TZ}")
    print(f"Window: {WINDOW_START} to {WINDOW_END} (exclusive)")
    print(f"Year chunks: {len(year_chunks)} -> {[str(c[0].date()) for c in year_chunks]}")
    print(f"Pre-FB year chunks: {len(prefb_year_chunks)} -> {[str(c[0].date()) for c in prefb_year_chunks]}")
    print(f"Month chunks: {len(month_chunks)}")
    print(f"Border pairs (both directions): {n_pairs} -> {[p[0] for p in PAIRS]}")
    print(f"Estimated top-level client calls (upper bound, before dropping dead/cached items): {est_top_level_calls}")
    print("Each top-level call may fan out into several real HTTP requests internally")
    print("(entsoe-py year/month chunking + document-offset pagination, verified by")
    print("reading entsoe/decorators.py) -- the real count is reported at the end from")
    print("an instrumented requests.Session, independent of our own call count.")
    print()

    run_chunked(client, "price", year_chunks, _year_key, call_price, colname="price_eur_mwh")
    run_chunked(client, "load_forecast", month_chunks, _month_key, call_load_forecast)
    run_chunked(client, "wind_solar_forecast", month_chunks, _month_key, call_wind_solar)

    alive = ntc_probe_alive(client)
    ntc_years(client, alive, year_chunks)

    run_chunked(client, "installed_capacity_type", year_chunks, _year_key, call_installed_capacity_type)
    run_chunked(client, "installed_capacity_unit", year_chunks, _year_key, call_installed_capacity_unit)
    run_chunked(client, "outage_generation_units", year_chunks, _year_key, call_outage_generation)
    run_chunked(client, "outage_production_units", year_chunks, _year_key, call_outage_production)
    transmission_outages(client, year_chunks)
    run_chunked(client, "hydro_reservoir", year_chunks, _year_key, call_hydro)

    # S1b: close the S1 gate
    alive_prefb = ntc_probe_alive_prefb(client)
    ntc_years(client, alive_prefb, prefb_year_chunks)
    a68_diagnostic_probe(client)

    elapsed = time.time() - t_start
    pull_summary["_meta"] = {
        "elapsed_seconds": round(elapsed, 1),
        "zone": ZONE_CODE,
        "window": [WINDOW_START, WINDOW_END],
        "n_year_chunks": len(year_chunks),
        "n_month_chunks": len(month_chunks),
        "n_border_pairs": n_pairs,
        "est_top_level_calls_upper_bound": est_top_level_calls,
        "n_real_http_requests_this_run": client.session.n_requests,
    }
    out = DATA_S1 / "pull_summary.json"
    out.write_text(json.dumps(pull_summary, indent=2, default=str), encoding="utf-8")
    print(f"\nDone in {elapsed:.1f}s.")
    print(f"Real HTTP requests made THIS RUN (0 if fully cached): {client.session.n_requests}")
    print(f"Full pull summary written to {out}")


if __name__ == "__main__":
    main()
