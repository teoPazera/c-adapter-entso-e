# C_adapter_Entso_E

Does an LLM that reads real grid-outage and market documents improve a
frozen statistical forecasting model, by adjusting the model's future
driver inputs instead of retraining the model itself? Proof of concept on
public Swedish (SE3) electricity day-ahead price data. Full framing and
per-stage ground rules: [Claude.MD](Claude.MD).

Working notes, one file per stage: [notes/](notes/).

## Status

S0–S3 complete and gated. S3 = walk-forward LEAR baseline vs. naive,
recalibrated daily, over 2023-01 (validation) through 2026-09 (test).
Current numbers (`data/s3/metrics.json`, 730-day window): overall
`mae_model=16.92` vs. `mae_naive=23.99` (`rmae=0.705`), `n_models=1350`,
`max_abs_coef=1.639`. Next stage: S4, the context adapter.

## Setup

```powershell
git clone <this repo's URL>
cd C_adapter_Entso_E
uv sync
```

Pins from `uv.lock`, Python version from `.python-version` (3.12).

### Fast path: skip refitting 1,531 models

The model cache (`data/s3/models/`), the raw API cache (`data/raw/`), and
two pre-fix diagnostic archives (`data/s3/archive_*/`) are not tracked in
git — 261 MB combined, and deterministically reproducible (see
`notes/03-baseline.md`, Verification 2: an isolated refit matches the
cached model to 2.5e-8). They ship instead as a GitHub Release asset.

Download the latest `s3_cache_*.zip` from the repo's Releases page and
unzip it at the repo root — it restores `data/s3/models/`, `data/raw/`,
and `data/s3/archive_*/` in place.

Without the zip, the same commands below still work; they just refit
everything first (~30 min for validation, ~2.3 h for test, $0 — no LLM
calls in S0–S3).

## Run it

```powershell
cd src
uv run python s3_backtest.py --slice all
```

This runs validation (both the 365-day and 730-day recalibration windows,
picks the lower-MAE one) and then test with the chosen window. With the
cache present it's prediction-only (the fit phase prints
`0 not yet cached` and finishes in seconds); without it, it refits.
Expect the test summary to match `data/s3/metrics.json`:
`n_models=1350`, `max_abs_coef≈1.639`, `n_lars_blowup_hours=12`,
`mae_model≈16.92`.

`--slice validation` and `--slice test --window {365,730}` also work
individually — see the module docstring in `src/s3_backtest.py`.

## Stage map

| Script | Produces | Needs `ENTSOE_E_API_KEY`? |
|---|---|---|
| `src/s0_probe.py` | `data/s0/` — one sample of each raw data source | Yes |
| `src/s1_pull_history.py` | `data/raw/s1/` — cached raw API pulls, 2023-01→present | Yes |
| `src/s1_build_table.py` | `data/s1/se3_hourly.csv` + driver tables, from the S1 cache | No (reads `data/raw/s1/`) |
| `src/s2_pull_umm.py` | `data/raw/s2/` — cached Nord Pool UMM message pulls | No (Nord Pool UMM API is unauthenticated) |
| `src/s2_curate_events.py` | `data/s2/events.json`, `data/s2/documents/` — 18 curated events | No (reads `data/s1/` + `data/raw/s2/`) |
| `src/s3_backtest.py` | `data/s3/forecasts_*.csv`, `data/s3/metrics.json`, `data/s3/models/` | No (reads `data/s1/`, `data/s2/`) |

Only S0–S2's *pull* scripts touch a paid/rate-limited API. Copy
`.env.example` to `.env` and fill in the key only if you need to re-run
those; everything from S3 onward runs entirely off the already-pulled
data checked into (or restored into) this repo.

## Reference

- [Claude.MD](Claude.MD) — the operating rules and staged plan this
  project follows.
- [notes/](notes/) — one file per stage, findings with `file:line`
  evidence, gate results.
