"""S6: random-path controls and diagnostic decompositions for the S4 adapter.

This script asks a sharper question than S5 alone:

* Does any random perturbation of the same future availability-driver slot
  improve prices as much as (or more than) the document-conditioned LLM?
* Does the frozen model have enough sensitivity to that slot for an adapter
  to possibly help?
* Does an oracle that replaces the driver with its realised path have room to
  beat persistence at all?

The random controls deliberately never read UMM text or realised outcomes.
They draw outage-like paths from distributions matched to each LLM path's
marginal perturbation (driver, active-hour count and MW magnitude), then use
identical frozen S3 models and S5 scoring.  They are therefore a null test of
whether the LLM's document-conditioned placement/timing adds information.

Run:
  .venv/bin/python src/s6_random_perturbation.py --random-draws 2000
"""
from __future__ import annotations

import argparse
import json
from math import erfc
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from common import ROOT
from s3_features import day_hour_profile, load_table, persistence_fill
from s3_lear import DayModel, predict_day
from s4_context import driver_for, event_by_id

DATA_S3 = ROOT / "data" / "s3"
DATA_S4 = ROOT / "data" / "s4"
EPS = 1e-10


def _model(day: str) -> DayModel:
    path = DATA_S3 / "models" / "730" / f"{day}.npz"
    if not path.exists():
        raise FileNotFoundError(f"Missing frozen S3 model: {path}")
    return DayModel.load(path)


def _load_actuals() -> pd.DataFrame:
    baseline = pd.read_csv(DATA_S3 / "forecasts_test.csv")
    baseline["timestamp"] = pd.to_datetime(baseline["timestamp"], utc=True, errors="coerce")
    baseline = baseline[baseline["timestamp"].notna()].copy()
    local = baseline["timestamp"].dt.tz_convert("Europe/Stockholm")
    baseline["day"] = local.dt.strftime("%Y-%m-%d")
    baseline["hour"] = local.dt.hour
    return baseline[["day", "hour", "y"]]


def _parse_path_points(value: Any) -> np.ndarray:
    if not isinstance(value, str) or not value:
        return np.empty(0, dtype=float)
    parsed = json.loads(value)
    return np.asarray(parsed, dtype=float)


def _context_adjustments(forecasts: pd.DataFrame) -> pd.DataFrame:
    wide = forecasts.pivot_table(
        index=["event_id", "day", "hour"], columns="condition", values="point", aggfunc="first"
    ).reset_index()
    context_rows = forecasts[forecasts["condition"] == "context"].copy()
    context_rows = context_rows.merge(
        wide[["event_id", "day", "hour", "no_context", "context"]],
        on=["event_id", "day", "hour"],
        how="left",
        validate="one_to_one",
    )
    context_rows["context_delta"] = context_rows["context"] - context_rows["no_context"]
    return context_rows


def _dm_test(loss_diff: np.ndarray) -> dict[str, float | int | None]:
    """Two-sided HAC Diebold-Mariano test of |error| context minus comparator."""
    x = np.asarray(loss_diff, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 10:
        return {"n": n, "mean_loss_diff": None, "dm_stat": None, "p_value": None, "lag": None}
    lag = min(23, int(np.floor(n ** (1 / 3))))
    centered = x - x.mean()
    long_run = float(np.dot(centered, centered) / n)
    for k in range(1, lag + 1):
        gamma = float(np.dot(centered[k:], centered[:-k]) / n)
        long_run += 2 * (1 - k / (lag + 1)) * gamma
    if long_run <= 0:
        stat = float("nan")
        p = float("nan")
    else:
        stat = float(x.mean() / np.sqrt(long_run / n))
        p = float(erfc(abs(stat) / np.sqrt(2)))
    return {"n": n, "mean_loss_diff": float(x.mean()), "dm_stat": stat, "p_value": p, "lag": lag}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--random-draws", type=int, default=2000, help="Monte-Carlo random placements per event-day")
    ap.add_argument("--seed", type=int, default=20260914)
    args = ap.parse_args()
    if args.random_draws < 10:
        ap.error("--random-draws must be at least 10")

    forecasts = pd.read_csv(DATA_S4 / "forecasts_s4.csv")
    if "path_points" not in forecasts:
        raise ValueError("forecasts_s4.csv has no context path_points column")
    actuals = _load_actuals()
    context_rows = _context_adjustments(forecasts)
    table, _ = load_table()
    rng = np.random.default_rng(args.seed)

    daily: list[dict[str, Any]] = []
    random_hourly: list[dict[str, Any]] = []
    for (event_id, day), group in context_rows.groupby(["event_id", "day"], sort=True):
        event = event_by_id(str(event_id))
        driver, _ = driver_for(event)
        baseline = group.sort_values("hour")["no_context"].to_numpy(float)
        context = group.sort_values("hour")["context"].to_numpy(float)
        # We do have a stronger, data-independent random control: random blocks
        # with event-declared magnitude and the realised event-day duration,
        # without documents. This avoids smuggling LLM information into null.
        event_size = float(event.get("reduction_mw") or 0.0)
        start = pd.Timestamp(event["realised_start"]).tz_convert("Europe/Stockholm")
        end = pd.Timestamp(event["realised_end"]).tz_convert("Europe/Stockholm")
        realised_width = int(np.clip(np.ceil((end - start).total_seconds() / 3600), 1, 24))
        signed = -event_size if driver == "avail_gen_mw" else event_size
        random_forecasts = []
        for _ in range(args.random_draws):
            path = base.copy()
            h0 = int(rng.integers(0, 24))
            path[(h0 + np.arange(realised_width)) % 24] += signed
            random_forecasts.append(predict_day(model, table, str(day), exog_override={driver: path}))
        random_forecasts = np.asarray(random_forecasts)
        random_mean = random_forecasts.mean(axis=0)
        y = actuals[actuals["day"].eq(str(day))].set_index("hour").reindex(range(24))["y"].to_numpy(float)
        valid = np.isfinite(y)
        base_mae = float(np.mean(np.abs(baseline[valid] - y[valid])))
        context_mae = float(np.mean(np.abs(context[valid] - y[valid])))
        oracle_mae = float(np.mean(np.abs(oracle[valid] - y[valid])))
        random_maes = np.mean(np.abs(random_forecasts[:, valid] - y[valid]), axis=1)
        random_mean_mae = float(np.mean(np.abs(random_mean[valid] - y[valid])))
        daily.append({
            "event_id": event_id, "day": day, "driver": driver,
            "n_hours": int(valid.sum()), "baseline_mae": base_mae,
            "llm_context_mae": context_mae, "oracle_mae": oracle_mae,
            "random_mean_mae": random_mean_mae,
            "llm_minus_baseline_mae": context_mae - base_mae,
            "oracle_minus_baseline_mae": oracle_mae - base_mae,
            "random_mean_minus_baseline_mae": random_mean_mae - base_mae,
            "random_single_path_mae_mean": float(random_maes.mean()),
            "random_single_path_mae_p05": float(np.quantile(random_maes, .05)),
            "random_single_path_mae_p50": float(np.quantile(random_maes, .50)),
            "random_single_path_mae_p95": float(np.quantile(random_maes, .95)),
            "p_random_single_beats_llm": float(np.mean(random_maes < context_mae)),
            "max_abs_llm_forecast_shift": float(np.max(np.abs(context - baseline))),
            "max_abs_oracle_forecast_shift": float(np.max(np.abs(oracle - baseline))),
            "random_declared_mw": event_size, "random_width_hours": realised_width,
        })
        for h in range(24):
            random_hourly.append({
                "event_id": event_id, "day": day, "hour": h, "y": y[h],
                "no_context": baseline[h], "llm_context": context[h], "oracle": oracle[h],
                "random_mean": random_mean[h],
                "random_single_point_p05": float(np.quantile(random_forecasts[:, h], .05)),
                "random_single_point_p50": float(np.quantile(random_forecasts[:, h], .50)),
                "random_single_point_p95": float(np.quantile(random_forecasts[:, h], .95)),
            })

    daily_df = pd.DataFrame(daily)
    hourly_df = pd.DataFrame(random_hourly)
    hourly_df = hourly_df[np.isfinite(hourly_df["y"])].copy()
    for col in ("no_context", "llm_context", "oracle", "random_mean"):
        hourly_df[f"abs_error_{col}"] = (hourly_df[col] - hourly_df["y"]).abs()

    aggregate = {
        "n_event_days": int(len(daily_df)),
        "n_hours": int(len(hourly_df)),
        "random_draws_per_event_day": args.random_draws,
        "seed": args.seed,
        "mae": {c: float(hourly_df[f"abs_error_{c}"].mean()) for c in ("no_context", "llm_context", "oracle", "random_mean")},
        "mae_difference_vs_baseline": {
            c: float(hourly_df[f"abs_error_{c}"].mean() - hourly_df["abs_error_no_context"].mean())
            for c in ("llm_context", "oracle", "random_mean")
        },
        "dm_abs_error": {
            "llm_context_minus_baseline": _dm_test(hourly_df["abs_error_llm_context"] - hourly_df["abs_error_no_context"]),
            "oracle_minus_baseline": _dm_test(hourly_df["abs_error_oracle"] - hourly_df["abs_error_no_context"]),
            "llm_context_minus_random_mean": _dm_test(hourly_df["abs_error_llm_context"] - hourly_df["abs_error_random_mean"]),
        },
        "event_days": {
            "llm_better_than_baseline": int((daily_df["llm_minus_baseline_mae"] < 0).sum()),
            "oracle_better_than_baseline": int((daily_df["oracle_minus_baseline_mae"] < 0).sum()),
            "random_mean_better_than_baseline": int((daily_df["random_mean_minus_baseline_mae"] < 0).sum()),
        },
        "sensitivity": {
            "median_max_abs_llm_forecast_shift": float(daily_df["max_abs_llm_forecast_shift"].median()),
            "median_max_abs_oracle_forecast_shift": float(daily_df["max_abs_oracle_forecast_shift"].median()),
            "event_days_oracle_exactly_equals_baseline": int((daily_df["max_abs_oracle_forecast_shift"] < EPS).sum()),
        },
        "random_single_path": {
            "median_p_random_single_beats_llm": float(daily_df["p_random_single_beats_llm"].median()),
            "mean_p_random_single_beats_llm": float(daily_df["p_random_single_beats_llm"].mean()),
        },
    }

    daily_df.sort_values(["llm_minus_baseline_mae", "event_id", "day"]).to_csv(DATA_S4 / "s6_random_control_by_day.csv", index=False)
    hourly_df.to_csv(DATA_S4 / "s6_random_control_hourly.csv", index=False)
    (DATA_S4 / "s6_random_control_metrics.json").write_text(json.dumps(aggregate, indent=2), encoding="utf-8")
    print(json.dumps(aggregate, indent=2))


if __name__ == "__main__":
    main()
