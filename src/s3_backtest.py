"""S3 Step 5: walk-forward backtest runner.

Two phases per slice: (A) fit/load one DayModel per calendar day, in
PARALLEL across days (each worker does one day's 24 sequential per-hour
fits) -- day-level parallelism, not the hour-level parallelism inside
fit_day, because a fresh joblib pool per fit_day call (24 tiny tasks) is
dominated by process-pool startup overhead (measured: n_jobs=-1 inside
fit_day was SLOWER than n_jobs=1 for the 1-year window). (B) walk forward
SEQUENTIALLY through days in chronological order, predicting and updating
the per-hour rolling-residual quantile trackers -- this phase must be
sequential since it is stateful (day t's quantile forecast may only see
residuals from days < t).

Run:
  uv run python s3_backtest.py --slice validation
  uv run python s3_backtest.py --slice test --window 730
  uv run python s3_backtest.py --slice all
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from joblib import Parallel, delayed

from common import ROOT
from s3_features import day_hour_profile, load_table, to_local_day
from s3_lear import DayModel, fit_day, predict_day
from s3_metrics import (
    QUANTILE_GRID,
    RollingResidualQuantiles,
    naive_lag24_forecast,
    naive_lago_forecast,
    summarize_period,
)

DATA_S3 = ROOT / "data" / "s3"
MODELS_DIR = DATA_S3 / "models"

VALIDATION_RANGE = ("2023-01-01", "2023-06-30")
TEST_RANGE = ("2023-07-01", "2026-09-11")
WINDOWS = [365, 730]


def _hour_label(day: pd.Timestamp, h: int, tz: str = "Europe/Stockholm") -> pd.Timestamp:
    """The h-th hour-of-day label for local calendar date `day`, DST-aware.

    CORRECTED 2026-09-12 (found by independent review, after Findings 1-3
    above had already fixed which day each iteration represents but missed
    that labeling hours *within* a day the same way -- `day +
    Timedelta(hours=h)` -- breaks on the transition day itself: h=23 of a
    23-hour spring-forward day is the 24th absolute hour past midnight,
    which is actually the NEXT day's 00:00. Verified directly: for
    2024-03-31 (spring-forward), h=2 -> NaT (that wall-clock hour never
    occurred) and all other h stay on 2024-03-31; for 2024-10-27
    (fall-back), all h 0-23 stay on 2024-10-27 with h=2 labeling only the
    first of its two real occurrences (consistent with day_hour_profile's
    own first-occurrence convention). Operates in naive wall-clock time
    then localizes, rather than adding an absolute Timedelta to a tz-aware
    Timestamp."""
    return (day.tz_localize(None) + pd.Timedelta(hours=h)).tz_localize(
        tz, ambiguous=True, nonexistent="NaT"
    )


def _date_range(start: str, end: str) -> list[pd.Timestamp]:
    """CORRECTED 2026-09-12, two independent bugs in the original tz-aware-
    Timestamp-arithmetic version:

    (1) (e - s).days undercounts by 1 whenever the range's net DST shift is
    negative (more spring-forwards than fall-backs inside it) -- e.g.
    2023-01-01 (+01:00) to 2023-06-30 (+02:00) elapses 179 days 23 hours,
    not a clean 180, so `.days` truncates to 179 and the range silently
    drops its own last day. Found when the just-completed validation run
    produced 180 days instead of 181 (2023-06-30 missing, no error).
    Counting on plain calendar .date() objects is immune to the UTC offset.

    (2) `s + pd.Timedelta(days=k)` is ABSOLUTE-duration arithmetic, not
    calendar arithmetic: once k crosses a spring-forward, every later day
    lands at 01:00 local instead of 00:00 (verified directly: day 85 in a
    2023-01-01-based range prints "2023-03-27 01:00:00+02:00"), silently
    self-correcting back to 00:00 only after the matching autumn fall-back.
    Every function that consumes a `day` argument (day_hour_profile,
    fit_day, predict_day) re-normalizes it to local midnight on entry, so
    this never corrupted a computed value -- but run_slice's
    `row["timestamp"] = D + pd.Timedelta(hours=h)` used the RAW, un-
    renormalized D directly, so for roughly 7 months of every year the
    *label* on an otherwise-correct row was off by 1 hour (see the note's
    caveat on forecasts_val_2y.csv / forecasts_test.csv, produced before
    this fix). `pd.DateOffset(days=k)` is calendar/DST-aware and verified
    to stay at local midnight across both transition directions."""
    s = to_local_day(start)
    e = to_local_day(end)
    n = (e.date() - s.date()).days + 1
    return [s + pd.DateOffset(days=k) for k in range(n)]


def _model_path(window_days: int, D: pd.Timestamp) -> Path:
    return MODELS_DIR / str(window_days) / f"{D.date()}.npz"


def _fit_and_save(table: pd.DataFrame, D: pd.Timestamp, window_days: int) -> None:
    """Fit and save inside the SAME worker task, so a day's result survives
    on disk as soon as that one task finishes -- not only once the entire
    Parallel(...) batch below returns. Required for the plan's own resume
    requirement (S3 Step 5: "if the test run crashes midway, resume from
    the last saved DayModel"); the earlier fit-all-then-save-all version
    would have lost every completed fit in an interrupted ~1170-day test
    run, not just the ones in flight."""
    model = fit_day(table, D, window_days=window_days, n_jobs=1)
    model.save(_model_path(window_days, D))


def _refit_days(days: list[pd.Timestamp], recalib_every: int) -> list[pd.Timestamp]:
    return [D for i, D in enumerate(days) if i % recalib_every == 0]


def _fit_phase(
    table: pd.DataFrame, days: list[pd.Timestamp], window_days: int, recalib_every: int, n_jobs: int
) -> dict[pd.Timestamp, DayModel]:
    """Returns {refit_day: DayModel} for every day where i % recalib_every == 0.
    Days in between reuse the most recent refit day's model (resolved in the
    walk-forward phase, not here)."""
    refit_days = _refit_days(days, recalib_every)
    to_fit = [D for D in refit_days if not _model_path(window_days, D).exists()]
    print(f"  [{window_days}d] {len(refit_days)} refit days, {len(to_fit)} not yet cached, n_jobs={n_jobs}")

    if to_fit:
        Parallel(n_jobs=n_jobs, verbose=1)(
            delayed(_fit_and_save)(table, D, window_days) for D in to_fit
        )

    return {D: DayModel.load(_model_path(window_days, D)) for D in refit_days}


def run_slice(
    table: pd.DataFrame,
    days: list[pd.Timestamp],
    window_days: int,
    recalib_every: int = 1,
    n_jobs: int = -1,
) -> pd.DataFrame:
    t0 = time.time()
    models_by_refit_day = _fit_phase(table, days, window_days, recalib_every, n_jobs)
    fit_elapsed = time.time() - t0
    print(f"  [{window_days}d] fit phase: {fit_elapsed:.1f}s for {len(days)} days")

    model_rq = RollingResidualQuantiles()
    naive_rq = RollingResidualQuantiles()

    rows = []
    current_model: DayModel | None = None
    current_refit_day: pd.Timestamp | None = None
    for i, D in enumerate(days):
        if i % recalib_every == 0:
            current_model = models_by_refit_day[D]
            current_refit_day = D
        assert current_model is not None
        D = to_local_day(D)  # defense in depth: guarantee local midnight for the timestamp label below, independent of what _date_range handed in

        # day_hour_profile always returns a 24-slot, hour-of-day-indexed
        # array (NaN for the skipped hour on a 23-hour DST spring-forward
        # day) -- used for y too so every column aligns the same way.
        y = day_hour_profile(table["price_eur_mwh"], D)
        point = predict_day(current_model, table, D)
        naive = naive_lago_forecast(table, D)
        naive_lag24 = naive_lag24_forecast(table, D)

        for h in range(24):
            row = {
                "timestamp": _hour_label(D, h),
                "y": y[h],
                "point": point[h],
                "naive": naive[h],
                "naive_lag24": naive_lag24[h],
                "window_days": window_days,
                "refit_day": str(current_refit_day.date()),
            }
            q = model_rq.quantiles_for(h, point[h])
            nq = naive_rq.quantiles_for(h, naive[h])
            if q is not None:
                for tau in QUANTILE_GRID:
                    row[f"q{round(tau * 100):02d}"] = q[tau]
            if nq is not None:
                for tau in QUANTILE_GRID:
                    row[f"naive_q{round(tau * 100):02d}"] = nq[tau]
            rows.append(row)

            model_rq.update(h, y[h], point[h])
            naive_rq.update(h, y[h], naive[h])

    df = pd.DataFrame(rows)
    print(f"  [{window_days}d] total slice time: {time.time() - t0:.1f}s ({len(days)} days, {len(df)} rows)")
    return df


def aggregate_model_diagnostics(window_days: int, refit_days: list[pd.Timestamp]) -> dict:
    """Sums each saved DayModel's per-fit diagnostics (ffill/bfill NaN
    counts, degenerate-hour count, LARS numerical-failure count) across
    the refit days belonging to THIS phase/window. These were being
    recorded per model but never rolled up anywhere a reader would
    actually see them.

    CORRECTED 2026-09-12: previously globbed every *.npz under
    data/s3/models/<window_days>/ instead of taking an explicit day
    list. Validation and test cache models under the same
    models/<window_days>/ directory, keyed only by calendar date with
    no phase tag -- so once both phases' models exist on disk (e.g.
    after unzipping a full cache), an unscoped glob during the
    validation phase silently included test's models too (and vice
    versa), inflating n_models/n_lars_blowup_hours/etc. to the
    combined total rather than this phase's own contribution. Found by
    A5 fresh-clone verification: a rerun's window_selection.json
    (n_models=1350 for the 730d window) didn't match the committed one
    (n_models=1338) even though metrics.json matched exactly -- the
    committed window_selection.json was a stale snapshot from before
    the on-disk cache reached its final state."""
    paths = [_model_path(window_days, D) for D in refit_days]
    totals = {
        "n_models": len(paths),
        "n_ffill_feature_nan": 0,
        "n_bfill_feature_nan": 0,
        "n_degenerate_hours": 0,
        "n_numerical_failure_hours": 0,
        "n_lars_blowup_hours": 0,
        "n_ridge_fallback_models": 0,
        "max_abs_coef": 0.0,
    }
    for p in paths:
        m = DayModel.load(p)
        totals["n_ffill_feature_nan"] += m.n_ffill_feature_nan
        totals["n_bfill_feature_nan"] += m.n_bfill_feature_nan
        totals["n_degenerate_hours"] += m.n_degenerate_hours
        totals["n_numerical_failure_hours"] += m.n_numerical_failure_hours
        totals["n_lars_blowup_hours"] += m.n_lars_blowup_hours
        totals["max_abs_coef"] = max(totals["max_abs_coef"], float(np.abs(m.coef).max()))
        if m.noise_variance_method == "ridge_fallback":
            totals["n_ridge_fallback_models"] += 1
    return totals


SEASON_MAP = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
}


def build_metrics_tables(df: pd.DataFrame, events_path: Path) -> dict:
    """Overall / per-year / per-season / inside-vs-outside-event-window
    breakdowns, per S3 Step 4's spec. Event windows come from
    data/s2/events.json's realised_start/realised_end (UTC); a row is
    "inside" if its hourly timestamp falls in [start, end] of ANY of the
    18 curated events."""
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    local = df["timestamp"].dt.tz_convert("Europe/Stockholm")
    df["year"] = local.dt.year
    df["season"] = local.dt.month.map(SEASON_MAP)

    tables: dict = {"overall": summarize_period(df)}
    tables["by_year"] = {str(y): summarize_period(g) for y, g in df.groupby("year")}
    tables["by_season"] = {s: summarize_period(g) for s, g in df.groupby("season")}

    events = json.loads(events_path.read_text(encoding="utf-8"))["events"]
    intervals = [
        (pd.Timestamp(e["realised_start"]), pd.Timestamp(e["realised_end"])) for e in events
    ]

    def _inside(ts: pd.Timestamp) -> bool:
        return any(s <= ts <= e for s, e in intervals)

    inside_mask = df["timestamp"].apply(_inside)
    tables["inside_events"] = summarize_period(df[inside_mask])
    tables["outside_events"] = summarize_period(df[~inside_mask])
    tables["n_rows_inside_events"] = int(inside_mask.sum())
    tables["n_rows_outside_events"] = int((~inside_mask).sum())
    tables["n_events"] = len(events)
    return tables


def _run_validation(table: pd.DataFrame, n_jobs: int, recalib_every: int) -> None:
    days = _date_range(*VALIDATION_RANGE)
    results = {}
    for window_days in WINDOWS:
        print(f"=== validation, window={window_days}d ===")
        df = run_slice(table, days, window_days, recalib_every=recalib_every, n_jobs=n_jobs)
        df.to_csv(DATA_S3 / f"forecasts_val_{window_days // 365}y.csv", index=False)
        summary = summarize_period(df)
        summary["model_diagnostics"] = aggregate_model_diagnostics(window_days, _refit_days(days, recalib_every))
        results[window_days] = summary
        print(f"  summary: {json.dumps(summary, default=str)}")

    winner = min(results, key=lambda w: results[w]["mae_model"])
    out = {
        "windows": {str(w): r for w, r in results.items()},
        "chosen_window_days": winner,
        "rule": "lower validation MAE",
        "caveat": "window comparison is confounded with noise_variance_method: "
        "the 1-year window always uses the ridge fallback (n<=p+1), the "
        "2-year window always uses sklearn's own OLS estimator (n>p+1) -- "
        "see model_diagnostics.n_ridge_fallback_models per window.",
    }
    (DATA_S3 / "window_selection.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"Chosen window: {winner} days. Wrote {DATA_S3 / 'window_selection.json'}")


def _run_test(table: pd.DataFrame, window: int | None, n_jobs: int, recalib_every: int) -> None:
    window_days = window
    if window_days is None:
        sel_path = DATA_S3 / "window_selection.json"
        if not sel_path.exists():
            raise SystemExit("No --window given and data/s3/window_selection.json does not exist. Run --slice validation first.")
        window_days = json.loads(sel_path.read_text(encoding="utf-8"))["chosen_window_days"]
        print(f"Using chosen window from window_selection.json: {window_days} days")

    days = _date_range(*TEST_RANGE)
    print(f"=== test, window={window_days}d, {len(days)} days ===")
    df = run_slice(table, days, window_days, recalib_every=recalib_every, n_jobs=n_jobs)
    df.to_csv(DATA_S3 / "forecasts_test.csv", index=False)
    tables = build_metrics_tables(df, ROOT / "data" / "s2" / "events.json")
    tables["window_days"] = window_days
    tables["model_diagnostics"] = aggregate_model_diagnostics(window_days, _refit_days(days, recalib_every))
    print(f"Test overall summary: {json.dumps(tables['overall'], default=str, indent=2)}")
    (DATA_S3 / "metrics.json").write_text(json.dumps(tables, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {DATA_S3 / 'metrics.json'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice", choices=["validation", "test", "all"], required=True)
    ap.add_argument("--window", type=int, choices=WINDOWS, default=None)
    ap.add_argument("--n-jobs", type=int, default=-1)
    ap.add_argument("--recalib-every", type=int, default=1)
    args = ap.parse_args()

    table, ffill_report = load_table()
    print("ffill report:", json.dumps(ffill_report))

    if args.slice in ("validation", "all"):
        _run_validation(table, args.n_jobs, args.recalib_every)

    if args.slice in ("test", "all"):
        _run_test(table, args.window, args.n_jobs, args.recalib_every)


if __name__ == "__main__":
    main()
