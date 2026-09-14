"""S5 comparison tables and compact visualization data for S4 forecasts."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from common import ROOT

DATA_S4 = ROOT / "data" / "s4"
DATA_S3 = ROOT / "data" / "s3"


def summarize(df: pd.DataFrame) -> dict:
    error = df.point - df.y
    return {
        "n_obs": int(len(df)),
        "mae": float(error.abs().mean()),
        "rmse": float(np.sqrt(np.mean(error**2))),
        "bias": float(error.mean()),
    }


def main() -> None:
    forecasts = pd.read_csv(DATA_S4 / "forecasts_s4.csv")
    baseline = pd.read_csv(DATA_S3 / "forecasts_test.csv")
    # forecasts_test spans CET/CEST offsets; normalise mixed-offset strings to UTC
    # before deriving the local Stockholm delivery date/hour used by S4.
    baseline["timestamp"] = pd.to_datetime(baseline["timestamp"], utc=True, errors="coerce")
    baseline = baseline[baseline["timestamp"].notna()].copy()
    stockholm = baseline["timestamp"].dt.tz_convert("Europe/Stockholm")
    baseline["day"] = stockholm.dt.strftime("%Y-%m-%d")
    baseline["hour"] = stockholm.dt.hour
    merged = forecasts.merge(baseline[["day", "hour", "y"]], on=["day", "hour"], how="left", validate="many_to_one")
    merged = merged[np.isfinite(merged.y)].copy()
    summary = {condition: summarize(group) for condition, group in merged.groupby("condition")}
    wide = merged.pivot_table(index=["event_id", "day", "hour", "y"], columns="condition", values="point", aggfunc="first").dropna()
    abs_err = pd.DataFrame({c: (wide[c] - wide.index.get_level_values("y")).abs() for c in wide.columns})
    summary["paired"] = {
        "context_minus_no_context_mae": float(abs_err.context.mean() - abs_err.no_context.mean()),
        "oracle_minus_no_context_mae": float(abs_err.oracle.mean() - abs_err.no_context.mean()),
        "context_wins_hours": int((abs_err.context < abs_err.no_context).sum()),
        "no_context_wins_hours": int((abs_err.no_context < abs_err.context).sum()),
        "ties_hours": int((abs_err.no_context == abs_err.context).sum()),
    }
    by_event=[]
    for event_id, group in merged.groupby("event_id"):
        row={"event_id":event_id}
        for condition, sub in group.groupby("condition"):
            row[f"mae_{condition}"]=float((sub.point-sub.y).abs().mean())
        row["context_minus_no_context_mae"]=row["mae_context"]-row["mae_no_context"]
        row["oracle_minus_no_context_mae"]=row["mae_oracle"]-row["mae_no_context"]
        by_event.append(row)
    by_event_df=pd.DataFrame(by_event).sort_values("context_minus_no_context_mae")
    by_event_df.to_csv(DATA_S4 / "s5_by_event.csv", index=False)
    merged.to_csv(DATA_S4 / "s5_hourly_comparison.csv", index=False)
    (DATA_S4 / "s5_metrics.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
