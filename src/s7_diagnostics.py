"""S7 Step 1: deterministic mechanical audit for the S4 availability seam.

Builds a canonical event-hour panel and checks the invariants that must hold
before interpreting oracle/context scores: time-zone alignment, event-hour
membership, persistence baseline construction, oracle path construction,
driver perturbation signs, and frozen-model feature wiring.

Run:
  .venv/bin/python src/s7_diagnostics.py
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from common import ROOT
from s3_features import day_hour_profile, load_table, persistence_fill
from s3_lear import DayModel, predict_day
from s4_context import driver_for, load_events
from s4_run import event_days

DATA_S3 = ROOT / "data" / "s3"
OUT_DIR = ROOT / "data" / "s7_diagnostics"
TZ = ZoneInfo("Europe/Stockholm")
UTC = ZoneInfo("UTC")
EPS = 1e-8


def _model(day: str) -> DayModel:
    path = DATA_S3 / "models" / "730" / f"{day}.npz"
    if not path.exists():
        raise FileNotFoundError(f"Missing frozen 730-day S3 model: {path}")
    return DayModel.load(path)


def _actuals() -> pd.DataFrame:
    forecast = pd.read_csv(DATA_S3 / "forecasts_test.csv")
    forecast["timestamp"] = pd.to_datetime(forecast["timestamp"], utc=True, errors="coerce")
    forecast = forecast[forecast["timestamp"].notna()].copy()
    local = forecast["timestamp"].dt.tz_convert("Europe/Stockholm")
    forecast["day"] = local.dt.strftime("%Y-%m-%d")
    forecast["hour"] = local.dt.hour
    return forecast[["day", "hour", "y", "point"]].drop_duplicates(["day", "hour"])


def _local_hour_bounds(day: str, hour: int) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """Local delivery-hour interval, returning None for spring-forward gap."""
    naive = pd.Timestamp(day) + pd.Timedelta(hours=int(hour))
    start = naive.tz_localize("Europe/Stockholm", ambiguous=True, nonexistent="NaT")
    if pd.isna(start):
        return None
    end = start + pd.Timedelta(hours=1)
    return start, end


def _event_overlap(start: pd.Timestamp, end: pd.Timestamp, day: str, hour: int) -> tuple[bool, float]:
    bounds = _local_hour_bounds(day, hour)
    if bounds is None:
        return False, 0.0
    h0, h1 = bounds
    overlap = max(pd.Timedelta(0), min(h1, end) - max(h0, start))
    return bool(overlap > pd.Timedelta(0)), float(overlap.total_seconds() / 3600)


def _record_failure(rows: list[dict[str, Any]], check: str, event_id: str, day: str, hour: int | None, detail: str) -> None:
    rows.append({"check": check, "event_id": event_id, "day": day, "hour": hour, "detail": detail})


def build_and_audit() -> tuple[pd.DataFrame, list[dict[str, Any]], dict[str, Any]]:
    table, table_report = load_table()
    actuals = _actuals()
    events = load_events()
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    checks = {name: {"passed": 0, "failed": 0} for name in (
        "baseline_persistence", "oracle_path", "feature_wiring", "event_interval_alignment", "generation_sign", "transmission_sign", "model_coverage"
    )}

    for event in events:
        event_id = str(event["event_id"])
        driver, _ = driver_for(event)
        start = pd.Timestamp(event["realised_start"]).tz_convert("Europe/Stockholm")
        end = pd.Timestamp(event["realised_end"]).tz_convert("Europe/Stockholm")
        for day in event_days(event):
            model_path = DATA_S3 / "models" / "730" / f"{day}.npz"
            if not model_path.exists():
                # S4/S5 score only test-slice delivery days. Some selected
                # events predate that slice, so record the omission rather
                # than fitting a new model and silently changing the frozen
                # experimental population.
                checks["model_coverage"]["failed"] += 1
                _record_failure(failures, "model_coverage", event_id, day, None, f"missing frozen test model: {model_path.name}")
                continue
            checks["model_coverage"]["passed"] += 1
            model = _model(day)
            baseline_driver = persistence_fill(table, day, driver)
            oracle_driver = day_hour_profile(table[driver], day)
            baseline = predict_day(model, table, day)
            oracle = predict_day(model, table, day, exog_override={driver: oracle_driver})
            actual_day = actuals[actuals["day"].eq(day)].set_index("hour")

            relevant_features = [f"{driver}_d_h{h:02d}" for h in range(24)]
            missing = [f for f in relevant_features if f not in model.feature_names]
            if missing:
                checks["feature_wiring"]["failed"] += 1
                _record_failure(failures, "feature_wiring", event_id, day, None, f"missing features: {missing}")
            else:
                checks["feature_wiring"]["passed"] += 1

            expected_base = persistence_fill(table, day, driver)
            if not np.allclose(baseline_driver, expected_base, equal_nan=True):
                checks["baseline_persistence"]["failed"] += 1
                _record_failure(failures, "baseline_persistence", event_id, day, None, "baseline driver differs from persistence_fill")
            else:
                checks["baseline_persistence"]["passed"] += 1

            expected_oracle = day_hour_profile(table[driver], day)
            if not np.allclose(oracle_driver, expected_oracle, equal_nan=True):
                checks["oracle_path"]["failed"] += 1
                _record_failure(failures, "oracle_path", event_id, day, None, "oracle driver differs from day_hour_profile")
            else:
                checks["oracle_path"]["passed"] += 1

            for hour in range(24):
                bounds = _local_hour_bounds(day, hour)
                affected, overlap_hours = _event_overlap(start, end, day, hour)
                actual = actual_day.loc[hour, "y"] if hour in actual_day.index else np.nan
                stored_baseline = actual_day.loc[hour, "point"] if hour in actual_day.index else np.nan
                if bounds is None:
                    if np.isfinite(actual):
                        checks["event_interval_alignment"]["failed"] += 1
                        _record_failure(failures, "event_interval_alignment", event_id, day, hour, "nonexistent local DST hour has realised target")
                    else:
                        checks["event_interval_alignment"]["passed"] += 1
                    hour_start = hour_end = None
                else:
                    hour_start, hour_end = bounds
                    checks["event_interval_alignment"]["passed"] += 1

                delta_driver = oracle_driver[hour] - baseline_driver[hour]
                delta_forecast = oracle[hour] - baseline[hour]
                rows.append({
                    "event_id": event_id,
                    "kind": event["kind"],
                    "driver": driver,
                    "reduction_mw": float(event["reduction_mw"]),
                    "event_start_utc": pd.Timestamp(event["realised_start"]).tz_convert("UTC").isoformat(),
                    "event_end_utc": pd.Timestamp(event["realised_end"]).tz_convert("UTC").isoformat(),
                    "event_start_local": start.isoformat(),
                    "event_end_local": end.isoformat(),
                    "day": day,
                    "hour": hour,
                    "hour_start_local": hour_start.isoformat() if hour_start is not None else None,
                    "hour_end_local": hour_end.isoformat() if hour_end is not None else None,
                    "affected_by_realised_event": affected,
                    "event_overlap_hours": overlap_hours,
                    "actual_price": actual,
                    "stored_s3_baseline_price": stored_baseline,
                    "recomputed_baseline_price": baseline[hour],
                    "oracle_price": oracle[hour],
                    "baseline_driver_persistence": baseline_driver[hour],
                    "oracle_driver_realised": oracle_driver[hour],
                    "oracle_minus_baseline_driver": delta_driver,
                    "oracle_minus_baseline_price": delta_forecast,
                })

            # Sign sanity check at the driver path level. It checks the exact
            # injection convention, not an economic assumption about price.
            shift = float(event["reduction_mw"])
            if driver == "avail_gen_mw":
                injected = baseline_driver - shift
                ok = np.allclose(injected - baseline_driver, -shift)
                check = "generation_sign"
            else:
                injected = baseline_driver + shift
                ok = np.allclose(injected - baseline_driver, shift)
                check = "transmission_sign"
            if ok:
                checks[check]["passed"] += 1
            else:
                checks[check]["failed"] += 1
                _record_failure(failures, check, event_id, day, None, "driver injection has wrong sign")

    panel = pd.DataFrame(rows)
    # Stored S3 forecasts are authoritative for the no-context comparison.
    # Recomputed ones should agree to numerical precision when matching model cache exists.
    comparable = panel[np.isfinite(panel["stored_s3_baseline_price"])].copy()
    mismatch = np.abs(comparable["stored_s3_baseline_price"] - comparable["recomputed_baseline_price"])
    n_mismatch = int((mismatch > 1e-5).sum())
    summary = {
        "n_events": len(events),
        "n_event_days": int(panel[["event_id", "day"]].drop_duplicates().shape[0]),
        "n_event_hours": int(len(panel)),
        "n_realised_affected_hours": int(panel["affected_by_realised_event"].sum()),
        "n_nonfinite_actual_price": int((~np.isfinite(panel["actual_price"])).sum()),
        "stored_vs_recomputed_baseline": {
            "n_comparable": int(len(comparable)),
            "n_abs_diff_gt_1e-5": n_mismatch,
            "max_abs_diff": float(mismatch.max()) if len(mismatch) else None,
        },
        "checks": checks,
        "n_failures": len(failures),
        "table_load_report": table_report,
    }
    return panel, failures, summary



def _matched_normal_profile(table: pd.DataFrame, driver: str, day: str, event_days_set: set[str], lookback_days: int = 28) -> tuple[np.ndarray, int]:
    """Same-weekday, prior non-event-day median profile.

    Restricting to preceding calendar weeks gives a genuinely pre-event
    comparison and avoids using a future realised outcome. It is a normal
    level diagnostic, not an operational forecast reconstruction.
    """
    D = pd.Timestamp(day).tz_localize("Europe/Stockholm")
    candidates = []
    for k in range(1, lookback_days // 7 + 1):
        candidate = D - pd.DateOffset(days=7 * k)
        if candidate.strftime("%Y-%m-%d") in event_days_set:
            continue
        prof = day_hour_profile(table[driver], candidate)
        if np.isfinite(prof).any():
            candidates.append(prof)
    if not candidates:
        return np.full(24, np.nan), 0
    return np.nanmedian(np.asarray(candidates), axis=0), len(candidates)


def run_absorption(panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Compare oracle driver with persistence and a matched normal level."""
    table, _ = load_table()
    events = {str(e["event_id"]): e for e in load_events()}
    all_event_days = set(panel[["event_id", "day"]]["day"].astype(str))
    event_rows: list[dict[str, Any]] = []
    hour_rows: list[dict[str, Any]] = []
    for (event_id, day), group in panel.groupby(["event_id", "day"], sort=True):
        group = group.sort_values("hour").copy()
        event = events[str(event_id)]
        driver = str(group["driver"].iloc[0])
        normal, n_controls = _matched_normal_profile(table, driver, str(day), all_event_days)
        affected = group["affected_by_realised_event"].to_numpy(bool)
        valid = affected & np.isfinite(group["oracle_driver_realised"].to_numpy(float)) & np.isfinite(normal)
        # operational direction: a generation outage lowers availability;
        # an import restriction raises import unavailability.
        operational_sign = -1.0 if driver == "avail_gen_mw" else 1.0
        realised = group["oracle_driver_realised"].to_numpy(float)
        persistence = group["baseline_driver_persistence"].to_numpy(float)
        anomaly_vs_normal = operational_sign * (realised - normal)
        anomaly_vs_persistence = operational_sign * (realised - persistence)
        declared = float(event["reduction_mw"])
        if valid.any():
            median_normal = float(np.nanmedian(anomaly_vs_normal[valid]))
            median_persist = float(np.nanmedian(anomaly_vs_persistence[valid]))
            share_correct_signed_normal = float(np.mean(anomaly_vs_normal[valid] > 0))
            share_correct_signed_persist = float(np.mean(anomaly_vs_persistence[valid] > 0))
            ratio_normal = median_normal / declared if declared else np.nan
            classification = (
                "visible_vs_normal" if share_correct_signed_normal >= 0.75 and median_normal >= 0.1 * declared
                else "weak_or_not_visible_vs_normal"
            )
        else:
            median_normal = median_persist = share_correct_signed_normal = share_correct_signed_persist = ratio_normal = np.nan
            classification = "no_valid_affected_hours"
        event_rows.append({
            "event_id": event_id, "day": day, "kind": event["kind"], "driver": driver,
            "reduction_mw": declared, "n_control_days": n_controls,
            "n_affected_valid_hours": int(valid.sum()),
            "median_operational_anomaly_vs_normal_mw": median_normal,
            "median_operational_anomaly_vs_persistence_mw": median_persist,
            "share_affected_hours_correct_signed_vs_normal": share_correct_signed_normal,
            "share_affected_hours_correct_signed_vs_persistence": share_correct_signed_persist,
            "median_declared_mw_share_visible_vs_normal": ratio_normal,
            "classification_vs_normal": classification,
        })
        for i, row in group.reset_index(drop=True).iterrows():
            hour_rows.append({
                "event_id": event_id, "day": day, "hour": int(row["hour"]), "kind": event["kind"], "driver": driver,
                "affected_by_realised_event": bool(row["affected_by_realised_event"]),
                "baseline_driver_persistence": float(persistence[i]),
                "oracle_driver_realised": float(realised[i]),
                "matched_normal_driver": float(normal[i]),
                "operational_anomaly_vs_normal_mw": float(anomaly_vs_normal[i]),
                "operational_anomaly_vs_persistence_mw": float(anomaly_vs_persistence[i]),
                "reduction_mw": declared, "n_control_days": n_controls,
            })
    event_df = pd.DataFrame(event_rows)
    hour_df = pd.DataFrame(hour_rows)
    affected = hour_df[hour_df["affected_by_realised_event"]].copy()
    summary = {
        "method": "same-weekday median of up to four preceding non-event weeks; diagnostic normal level only",
        "n_event_days": int(len(event_df)),
        "n_affected_hours": int(len(affected)),
        "aggregate": {
            "median_operational_anomaly_vs_normal_mw": float(affected["operational_anomaly_vs_normal_mw"].median()),
            "median_operational_anomaly_vs_persistence_mw": float(affected["operational_anomaly_vs_persistence_mw"].median()),
            "share_correct_signed_vs_normal": float((affected["operational_anomaly_vs_normal_mw"] > 0).mean()),
            "share_correct_signed_vs_persistence": float((affected["operational_anomaly_vs_persistence_mw"] > 0).mean()),
        },
        "event_day_classification_counts": event_df["classification_vs_normal"].value_counts(dropna=False).to_dict(),
    }
    return event_df, hour_df, summary


def _summary_mae(frame: pd.DataFrame, mask: pd.Series) -> dict[str, Any]:
    sub = frame.loc[mask & np.isfinite(frame["actual_price"])].copy()
    if sub.empty:
        return {"n_obs": 0, "mae_baseline": None, "mae_oracle": None, "oracle_minus_baseline_mae": None,
                "oracle_better_hours": 0, "baseline_better_hours": 0, "ties_hours": 0}
    base_error = np.abs(sub["recomputed_baseline_price"] - sub["actual_price"])
    oracle_error = np.abs(sub["oracle_price"] - sub["actual_price"])
    return {
        "n_obs": int(len(sub)),
        "mae_baseline": float(base_error.mean()),
        "mae_oracle": float(oracle_error.mean()),
        "oracle_minus_baseline_mae": float(oracle_error.mean() - base_error.mean()),
        "oracle_better_hours": int((oracle_error < base_error).sum()),
        "baseline_better_hours": int((base_error < oracle_error).sum()),
        "ties_hours": int((base_error == oracle_error).sum()),
    }


def run_sensitivity_and_rescore(panel: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Local 100-MW response of frozen LEAR plus pre-specified nested scores."""
    table, _ = load_table()
    delta_mw = 100.0
    pieces: list[pd.DataFrame] = []
    for (event_id, day), group in panel.groupby(["event_id", "day"], sort=True):
        group = group.sort_values("hour").copy()
        model = _model(str(day))
        driver = str(group["driver"].iloc[0])
        baseline_driver = group["baseline_driver_persistence"].to_numpy(float)
        # Operational perturbation: lower generation availability or increase
        # import unavailability, in both cases representing a 100 MW worsening.
        operational_delta = -delta_mw if driver == "avail_gen_mw" else delta_mw
        perturbed = baseline_driver + operational_delta
        perturbed_forecast = predict_day(model, table, str(day), exog_override={driver: perturbed})
        group["sensitivity_delta_mw"] = delta_mw
        group["sensitivity_operational_price_change_per_100mw"] = perturbed_forecast - group["recomputed_baseline_price"].to_numpy(float)
        group["oracle_driver_differs_from_persistence"] = np.abs(group["oracle_minus_baseline_driver"].to_numpy(float)) > EPS
        pieces.append(group)
    out = pd.concat(pieces, ignore_index=True)
    affected = out["affected_by_realised_event"]
    driver_diff = out["oracle_driver_differs_from_persistence"]
    sensitivity = out["sensitivity_operational_price_change_per_100mw"]
    high = np.abs(sensitivity) >= 0.10  # 0.10 EUR/MWh per 100 MW, declared before inspecting results
    masks = {
        "all_scored_event_hours": pd.Series(True, index=out.index),
        "realised_affected_hours": affected,
        "affected_hours_driver_differs_from_persistence": affected & driver_diff,
        "affected_high_sensitivity_hours": affected & high,
        "affected_driver_differs_and_high_sensitivity": affected & driver_diff & high,
    }
    scores = {name: _summary_mae(out, mask) for name, mask in masks.items()}
    finite_sens = out[np.isfinite(sensitivity)].copy()
    summary = {
        "method": "frozen-model local finite difference: 100 MW operational worsening of the selected driver path",
        "sensitivity_delta_mw": delta_mw,
        "predeclared_high_sensitivity_threshold_eur_per_mwh_per_100mw": 0.10,
        "sensitivity": {
            "n_hours": int(len(finite_sens)),
            "median_operational_price_change_per_100mw": float(finite_sens["sensitivity_operational_price_change_per_100mw"].median()),
            "mean_operational_price_change_per_100mw": float(finite_sens["sensitivity_operational_price_change_per_100mw"].mean()),
            "share_correct_price_sign_positive": float((finite_sens["sensitivity_operational_price_change_per_100mw"] > 0).mean()),
            "share_abs_ge_threshold": float((np.abs(finite_sens["sensitivity_operational_price_change_per_100mw"]) >= 0.10).mean()),
        },
        "sensitivity_on_affected_hours": {
            "n_hours": int((affected & np.isfinite(sensitivity)).sum()),
            "median_operational_price_change_per_100mw": float(out.loc[affected, "sensitivity_operational_price_change_per_100mw"].median()),
            "share_correct_price_sign_positive": float((out.loc[affected, "sensitivity_operational_price_change_per_100mw"] > 0).mean()),
            "share_abs_ge_threshold": float((np.abs(out.loc[affected, "sensitivity_operational_price_change_per_100mw"]) >= 0.10).mean()),
        },
        "nested_oracle_scores": scores,
    }
    return out, summary


def run_event_decomposition(sensitivity_panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Event/day error decomposition and descriptive residual context.

    This is deliberately attribution, not a causal regression: the selected
    event count is small and other system drivers co-move with outages.
    """
    table, _ = load_table()
    df = sensitivity_panel.copy()
    df["baseline_abs_error"] = np.abs(df["recomputed_baseline_price"] - df["actual_price"])
    df["oracle_abs_error"] = np.abs(df["oracle_price"] - df["actual_price"])
    df["oracle_minus_baseline_abs_error"] = df["oracle_abs_error"] - df["baseline_abs_error"]
    df["baseline_residual"] = df["actual_price"] - df["recomputed_baseline_price"]
    daily_rows: list[dict[str, Any]] = []
    for (event_id, day), g in df.groupby(["event_id", "day"], sort=True):
        g = g.sort_values("hour").copy()
        valid = np.isfinite(g["actual_price"])
        affected = valid & g["affected_by_realised_event"].to_numpy(bool)
        # Exact day-D realised profiles are used only to characterize what
        # co-moved on these event days, never as a forecast claim.
        profile = {col: day_hour_profile(table[col], str(day)) for col in (
            "load_forecast_mw", "wind_onshore_forecast_mw", "solar_forecast_mw",
            "avail_gen_mw", "unavail_tx_import_mw", "unavail_tx_export_mw"
        )}
        hydro = day_hour_profile(table["hydro_reservoir_fill"], str(day))
        row = {
            "event_id": event_id, "day": day, "kind": g["kind"].iloc[0], "driver": g["driver"].iloc[0],
            "n_scored_hours": int(valid.sum()), "n_affected_scored_hours": int(affected.sum()),
            "baseline_mae_all": float(g.loc[valid, "baseline_abs_error"].mean()),
            "oracle_mae_all": float(g.loc[valid, "oracle_abs_error"].mean()),
            "oracle_minus_baseline_mae_all": float(g.loc[valid, "oracle_minus_baseline_abs_error"].mean()),
            "baseline_mae_affected": float(g.loc[affected, "baseline_abs_error"].mean()) if affected.any() else np.nan,
            "oracle_mae_affected": float(g.loc[affected, "oracle_abs_error"].mean()) if affected.any() else np.nan,
            "oracle_minus_baseline_mae_affected": float(g.loc[affected, "oracle_minus_baseline_abs_error"].mean()) if affected.any() else np.nan,
            "mean_baseline_residual_affected": float(g.loc[affected, "baseline_residual"].mean()) if affected.any() else np.nan,
            "mean_oracle_forecast_shift_affected": float(g.loc[affected, "oracle_minus_baseline_price"].mean()) if affected.any() else np.nan,
            "share_correct_sensitivity_sign_affected": float((g.loc[affected, "sensitivity_operational_price_change_per_100mw"] > 0).mean()) if affected.any() else np.nan,
            "median_abs_sensitivity_per_100mw_affected": float(np.abs(g.loc[affected, "sensitivity_operational_price_change_per_100mw"]).median()) if affected.any() else np.nan,
            "mean_load_forecast_mw_affected": float(np.nanmean(profile["load_forecast_mw"][affected])) if affected.any() else np.nan,
            "mean_wind_forecast_mw_affected": float(np.nanmean(profile["wind_onshore_forecast_mw"][affected])) if affected.any() else np.nan,
            "mean_solar_forecast_mw_affected": float(np.nanmean(profile["solar_forecast_mw"][affected])) if affected.any() else np.nan,
            "mean_other_import_unavail_mw_affected": float(np.nanmean(profile["unavail_tx_import_mw"][affected])) if affected.any() else np.nan,
            "mean_other_export_unavail_mw_affected": float(np.nanmean(profile["unavail_tx_export_mw"][affected])) if affected.any() else np.nan,
            "mean_hydro_reservoir_fill_affected": float(np.nanmean(hydro[affected])) if affected.any() else np.nan,
        }
        daily_rows.append(row)
    daily = pd.DataFrame(daily_rows)
    event = daily.groupby(["event_id", "kind", "driver"], as_index=False).agg(
        n_event_days=("day", "size"),
        n_affected_scored_hours=("n_affected_scored_hours", "sum"),
        baseline_mae_affected=("baseline_mae_affected", "mean"),
        oracle_mae_affected=("oracle_mae_affected", "mean"),
        oracle_minus_baseline_mae_affected=("oracle_minus_baseline_mae_affected", "mean"),
        mean_baseline_residual_affected=("mean_baseline_residual_affected", "mean"),
        mean_oracle_forecast_shift_affected=("mean_oracle_forecast_shift_affected", "mean"),
        share_correct_sensitivity_sign_affected=("share_correct_sensitivity_sign_affected", "mean"),
        median_abs_sensitivity_per_100mw_affected=("median_abs_sensitivity_per_100mw_affected", "mean"),
    ).sort_values("oracle_minus_baseline_mae_affected")
    kind = daily.groupby("kind").agg(
        n_event_days=("day", "size"), n_affected_scored_hours=("n_affected_scored_hours", "sum"),
        baseline_mae_affected=("baseline_mae_affected", "mean"), oracle_mae_affected=("oracle_mae_affected", "mean"),
        oracle_minus_baseline_mae_affected=("oracle_minus_baseline_mae_affected", "mean"),
        mean_baseline_residual_affected=("mean_baseline_residual_affected", "mean"),
        mean_oracle_forecast_shift_affected=("mean_oracle_forecast_shift_affected", "mean"),
    ).reset_index().to_dict("records")
    summary = {
        "interpretation": "descriptive event/day decomposition; co-moving driver columns are not causal attribution",
        "by_kind": kind,
        "event_oracle_better_on_affected_hours": int((event["oracle_minus_baseline_mae_affected"] < 0).sum()),
        "event_oracle_worse_on_affected_hours": int((event["oracle_minus_baseline_mae_affected"] > 0).sum()),
        "event_oracle_tied_on_affected_hours": int((event["oracle_minus_baseline_mae_affected"] == 0).sum()),
    }
    return daily, event, summary


def _event_block_bootstrap(df: pd.DataFrame, mask: pd.Series, n_draws: int = 10_000, seed: int = 20260914) -> dict[str, Any]:
    """Bootstrap MAE difference by event, preserving within-event dependence."""
    sub = df.loc[mask & np.isfinite(df["actual_price"])].copy()
    sub = sub[np.isfinite(sub["recomputed_baseline_price"]) & np.isfinite(sub["oracle_price"])].copy()
    if sub.empty:
        return {"n_events": 0, "n_obs": 0, "estimate": None, "ci95": [None, None], "p_bootstrap_oracle_better": None}
    per_event = sub.groupby("event_id").apply(
        lambda g: float(np.abs(g["oracle_price"] - g["actual_price"]).mean() - np.abs(g["recomputed_baseline_price"] - g["actual_price"]).mean()),
        include_groups=False,
    )
    values = per_event.to_numpy(float)
    rng = np.random.default_rng(seed)
    draws = values[rng.integers(0, len(values), size=(n_draws, len(values)))].mean(axis=1)
    return {
        "n_events": int(len(values)),
        "n_obs": int(len(sub)),
        "estimate": float(values.mean()),
        "ci95": [float(np.quantile(draws, 0.025)), float(np.quantile(draws, 0.975))],
        "p_bootstrap_oracle_better": float(np.mean(draws < 0)),
        "per_event_mae_difference": {str(k): float(v) for k, v in per_event.items()},
    }


def run_bootstrap(sensitivity_panel: pd.DataFrame) -> dict[str, Any]:
    """Pre-specified event-block uncertainty for kind and driver-difference strata."""
    df = sensitivity_panel.copy()
    affected = df["affected_by_realised_event"]
    differs = df["oracle_driver_differs_from_persistence"]
    high = np.abs(df["sensitivity_operational_price_change_per_100mw"]) >= 0.10
    strata = {
        "generation_affected": affected & df["kind"].eq("generation"),
        "generation_affected_driver_differs": affected & df["kind"].eq("generation") & differs,
        "generation_affected_driver_differs_high_sensitivity": affected & df["kind"].eq("generation") & differs & high,
        "transmission_affected": affected & df["kind"].eq("transmission"),
        "transmission_affected_driver_differs": affected & df["kind"].eq("transmission") & differs,
        "all_affected": affected,
    }
    return {
        "method": "nonparametric event-block bootstrap of each event's within-event MAE difference (oracle minus baseline); 10,000 resamples, seed 20260914",
        "strata": {name: _event_block_bootstrap(df, mask) for name, mask in strata.items()},
    }


def run_oracle_ceiling(sensitivity_panel: pd.DataFrame) -> dict[str, Any]:
    """Quantify the practical oracle headroom on the retained generation track."""
    df = sensitivity_panel.copy()
    valid = np.isfinite(df["actual_price"]) & np.isfinite(df["recomputed_baseline_price"]) & np.isfinite(df["oracle_price"])
    affected_generation = valid & df["affected_by_realised_event"] & df["kind"].eq("generation")
    differs = df["oracle_driver_differs_from_persistence"]
    high = np.abs(df["sensitivity_operational_price_change_per_100mw"]) >= 0.10
    strata = {
        "generation_affected": affected_generation,
        "generation_affected_driver_differs": affected_generation & differs,
        "generation_affected_driver_differs_high_sensitivity": affected_generation & differs & high,
    }
    out: dict[str, Any] = {"interpretation": "oracle is an upper bound for any context path that only estimates the same realised driver; values are descriptive absolute-error quantities"}
    for name, mask in strata.items():
        sub = df.loc[mask].copy()
        base_err = np.abs(sub["recomputed_baseline_price"] - sub["actual_price"])
        oracle_err = np.abs(sub["oracle_price"] - sub["actual_price"])
        gain = base_err - oracle_err
        shifts = np.abs(sub["oracle_minus_baseline_price"])
        out[name] = {
            "n_events": int(sub["event_id"].nunique()),
            "n_hours": int(len(sub)),
            "baseline_mae": float(base_err.mean()),
            "oracle_mae": float(oracle_err.mean()),
            "oracle_mae_gain": float(gain.mean()),
            "oracle_mae_gain_pct_of_baseline": float(100 * gain.mean() / base_err.mean()),
            "oracle_wins_hours": int((gain > 0).sum()),
            "oracle_loses_hours": int((gain < 0).sum()),
            "ties_hours": int((gain == 0).sum()),
            "oracle_win_rate": float((gain > 0).mean()),
            "median_abs_oracle_forecast_shift": float(shifts.median()),
            "mean_abs_oracle_forecast_shift": float(shifts.mean()),
            "p95_abs_oracle_forecast_shift": float(np.quantile(shifts, .95)),
            "max_abs_oracle_forecast_shift": float(shifts.max()),
            "median_abs_error_reduction_when_oracle_wins": float(gain[gain > 0].median()) if (gain > 0).any() else None,
            "mean_abs_error_reduction_when_oracle_wins": float(gain[gain > 0].mean()) if (gain > 0).any() else None,
        }
    return out

def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    panel, failures, summary = build_and_audit()
    panel.to_csv(OUT_DIR / "event_hour_panel.csv", index=False)
    pd.DataFrame(failures, columns=["check", "event_id", "day", "hour", "detail"]).to_csv(OUT_DIR / "failures.csv", index=False)
    (OUT_DIR / "mechanical_audit.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    absorption_by_day, absorption_by_hour, absorption_summary = run_absorption(panel)
    absorption_by_day.to_csv(OUT_DIR / "driver_absorption_by_day.csv", index=False)
    absorption_by_hour.to_csv(OUT_DIR / "driver_absorption_by_hour.csv", index=False)
    (OUT_DIR / "driver_absorption_summary.json").write_text(json.dumps(absorption_summary, indent=2), encoding="utf-8")
    sensitivity_panel, sensitivity_summary = run_sensitivity_and_rescore(panel)
    sensitivity_panel.to_csv(OUT_DIR / "sensitivity_hour_panel.csv", index=False)
    (OUT_DIR / "sensitivity_and_rescore.json").write_text(json.dumps(sensitivity_summary, indent=2), encoding="utf-8")
    decomposition_by_day, decomposition_by_event, decomposition_summary = run_event_decomposition(sensitivity_panel)
    decomposition_by_day.to_csv(OUT_DIR / "event_decomposition_by_day.csv", index=False)
    decomposition_by_event.to_csv(OUT_DIR / "event_decomposition_by_event.csv", index=False)
    (OUT_DIR / "event_decomposition_summary.json").write_text(json.dumps(decomposition_summary, indent=2), encoding="utf-8")
    bootstrap_summary = run_bootstrap(sensitivity_panel)
    (OUT_DIR / "event_block_bootstrap.json").write_text(json.dumps(bootstrap_summary, indent=2), encoding="utf-8")
    ceiling_summary = run_oracle_ceiling(sensitivity_panel)
    (OUT_DIR / "oracle_ceiling.json").write_text(json.dumps(ceiling_summary, indent=2), encoding="utf-8")
    print(json.dumps({"mechanical": summary, "driver_absorption": absorption_summary, "sensitivity_and_rescore": sensitivity_summary, "event_decomposition": decomposition_summary, "event_block_bootstrap": bootstrap_summary, "oracle_ceiling": ceiling_summary}, indent=2))
    substantive = [f for f in failures if f["check"] != "model_coverage"]
    if substantive:
        raise SystemExit(f"Mechanical audit completed with {len(substantive)} substantive failure(s); see {OUT_DIR / 'failures.csv'}")


if __name__ == "__main__":
    main()
