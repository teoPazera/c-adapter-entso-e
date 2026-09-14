"""Minimal S4 context-adapter runner.

One LLM response is one Monte-Carlo scenario.  For each event/day the runner
asks the model K times at nonzero temperature, turns each returned effect into
one driver path, predicts with the unchanged S3 DayModel, and averages paths.

Examples:
  .venv/bin/python src/s4_run.py --event G074 --day 2025-02-06 --provider openai --paths 5
  .venv/bin/python src/s4_run.py --all --provider openai --paths 5
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from common import ROOT
from llm_client import MockJsonClient, OpenAIJsonClient
from s3_features import day_hour_profile, load_table, persistence_fill
from s3_lear import DayModel, fit_day, predict_day
from s4_context import cached_effect, driver_for, event_by_id, load_events
from s4_paths import sample_paths

DATA_S3 = ROOT / "data" / "s3"
DATA_S4 = ROOT / "data" / "s4"
TZ = ZoneInfo("Europe/Stockholm")
INPUT_USD_PER_MILLION = 0.20
OUTPUT_USD_PER_MILLION = 1.25


def event_days(event: dict[str, Any]) -> list[str]:
    start = datetime.fromisoformat(event["realised_start"]).astimezone(TZ).date()
    end = datetime.fromisoformat(event["realised_end"]).astimezone(TZ).date()
    days=[]
    while start <= end:
        days.append(start.isoformat())
        start += timedelta(days=1)
    return days


def _model(table: pd.DataFrame, day: str) -> DayModel:
    """Load the S3 730-day DayModel, fitting/caching only if pre-test history lacks it."""
    path = DATA_S3 / "models" / "730" / f"{day}.npz"
    if path.exists():
        return DayModel.load(path)
    print(f"  fitting missing 730-day frozen model for {day}", flush=True)
    model = fit_day(table, day, window_days=730, n_jobs=1)
    model.save(path)
    return model


def _cost(usage: dict[str, Any]) -> float:
    return usage.get("prompt_tokens", 0) * INPUT_USD_PER_MILLION / 1_000_000 + usage.get("completion_tokens", 0) * OUTPUT_USD_PER_MILLION / 1_000_000


def _metadata_usage(metadata: dict[str, Any]) -> dict[str, int]:
    usage=metadata.get("token_usage") or {}
    return {"prompt_tokens": int(usage.get("prompt_tokens") or 0), "completion_tokens": int(usage.get("completion_tokens") or 0)}


def run_one(event: dict[str, Any], day: str, client, paths: int, seed: int, force_llm: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    table, _ = load_table()
    model = _model(table, day)
    driver, _ = driver_for(event)
    base = persistence_fill(table, day, driver)
    baseline = predict_day(model, table, day)
    oracle_driver = day_hour_profile(table[driver], day)
    oracle = predict_day(model, table, day, exog_override={driver: oracle_driver})

    path_forecasts=[]
    calls=[]
    # If no document was available by t0, context is intentionally identical
    # to baseline: this is the built-in no-information control and costs $0.
    from s4_context import available_versions
    has_context = bool(available_versions(event, day))
    if has_context:
        for scenario in range(paths):
            effect, metadata, cache_key = cached_effect(client, event, day, force=force_llm, cache_salt=f"scenario={scenario}")
            driver_path = sample_paths(base, effect, driver, n_paths=1, seed=seed + scenario)[0]
            forecast = predict_day(model, table, day, exog_override={driver: driver_path})
            path_forecasts.append(forecast)
            usage=_metadata_usage(metadata)
            calls.append({"scenario": scenario, "cache_key": cache_key, "cache_hit": bool(metadata.get("cache_hit")), "effect": effect, "usage": usage, "cost_usd": _cost(usage)})
        context=np.mean(path_forecasts, axis=0)
    else:
        context=baseline.copy()
    rows=[]
    for hour in range(24):
        rows.extend([
            {"event_id":event["event_id"],"day":day,"hour":hour,"condition":"no_context","variant":"point","point":float(baseline[hour])},
            {"event_id":event["event_id"],"day":day,"hour":hour,"condition":"context","variant":"mean_of_5_llm_paths","point":float(context[hour]),"path_points":json.dumps([float(x[hour]) for x in path_forecasts])},
            {"event_id":event["event_id"],"day":day,"hour":hour,"condition":"oracle","variant":"point","point":float(oracle[hour])},
        ])
    return rows, {
        "event_id": event["event_id"], "day":day, "driver":driver, "has_context":has_context, "n_paths":paths,
        "calls":calls, "max_context_minus_baseline":float(np.max(np.abs(context-baseline))),
        "max_oracle_minus_baseline":float(np.max(np.abs(oracle-baseline))),
    }


def main() -> None:
    ap=argparse.ArgumentParser()
    ap.add_argument("--event")
    ap.add_argument("--day")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--provider", choices=("openai","mock"), default="openai")
    ap.add_argument("--paths",type=int,default=5)
    ap.add_argument("--seed",type=int,default=42)
    ap.add_argument("--force-llm",action="store_true")
    ap.add_argument("--min-interval-seconds",type=float,default=8.0)
    ap.add_argument("--max-retries",type=int,default=8)
    ap.add_argument("--initial-backoff-seconds",type=float,default=20.0)
    ap.add_argument("--start-date", default=None, help="optional inclusive YYYY-MM-DD filter for --all")
    ap.add_argument("--event-day-pause-seconds", type=float, default=0.0, help="cool-down after each checkpointed event-day")
    args=ap.parse_args()
    if args.all == bool(args.event or args.day):
        ap.error("use either --all, or both --event and --day")
    if args.paths < 1: ap.error("--paths must be >= 1")

    client=OpenAIJsonClient(min_interval_seconds=args.min_interval_seconds, max_retries=args.max_retries, initial_backoff_seconds=args.initial_backoff_seconds) if args.provider=="openai" else MockJsonClient()
    jobs=[]
    if args.all:
        for event in load_events():
            jobs.extend((event,day) for day in event_days(event) if args.start_date is None or day >= args.start_date)
    else:
        jobs=[(event_by_id(args.event),args.day)]

    DATA_S4.mkdir(parents=True,exist_ok=True)
    forecasts=DATA_S4 / ("forecasts_s4.csv" if args.all else f"worked_example_{args.event}_{args.day}.csv")
    summary_path=DATA_S4 / ("run_summary.json" if args.all else f"worked_example_{args.event}_{args.day}.json")
    all_rows = pd.read_csv(forecasts).to_dict("records") if forecasts.exists() else []
    completed = {(r["event_id"], r["day"]) for r in all_rows}
    prior = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    runs = prior.get("runs", [])
    runs = [run for run in runs if (run["event_id"], run["day"]) in completed]
    print(f"Resuming with {len(completed)}/{len(jobs)} completed event-days.", flush=True)
    for i,(event,day) in enumerate(jobs,1):
        if (event["event_id"], day) in completed:
            continue
        print(f"[{i}/{len(jobs)}] {event['event_id']} {day}", flush=True)
        rows,run=run_one(event,day,client,args.paths,args.seed, args.force_llm)
        all_rows.extend(rows); runs.append(run)
        pd.DataFrame(all_rows).to_csv(forecasts,index=False)
        partial={"provider":args.provider,"status":"running","n_event_days":len(runs),"n_paths_per_day":args.paths,"runs":runs,"forecasts":str(forecasts.relative_to(ROOT))}
        summary_path.write_text(json.dumps(partial,indent=2),encoding="utf-8")
        if args.event_day_pause_seconds > 0 and i < len(jobs):
            import time
            print(f"  checkpointed; cooling down {args.event_day_pause_seconds:.0f}s", flush=True)
            time.sleep(args.event_day_pause_seconds)

    prompt_tokens=sum(c["usage"]["prompt_tokens"] for run in runs for c in run["calls"])
    completion_tokens=sum(c["usage"]["completion_tokens"] for run in runs for c in run["calls"])
    summary={
        "provider":args.provider,"status":"complete","n_event_days":len(runs),"n_paths_per_day":args.paths,"n_calls":len(runs)*args.paths,
        "cache_hits":sum(c["cache_hit"] for r in runs for c in r["calls"]),"prompt_tokens":prompt_tokens,"completion_tokens":completion_tokens,
        "cost_usd":prompt_tokens*INPUT_USD_PER_MILLION/1_000_000 + completion_tokens*OUTPUT_USD_PER_MILLION/1_000_000,
        "rates_usd_per_million":{"input":INPUT_USD_PER_MILLION,"output":OUTPUT_USD_PER_MILLION},"runs":runs,"forecasts":str(forecasts.relative_to(ROOT)),
    }
    summary_path=DATA_S4 / ("run_summary.json" if args.all else f"worked_example_{args.event}_{args.day}.json")
    summary_path.write_text(json.dumps(summary,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in summary.items() if k not in {"runs"}},indent=2))

if __name__=="__main__": main()
