"""S3 Step 3: feature matrix builder, shared by S3/S4/S5.

Builds one row of ~541 features per delivery day D (price lags D-1/D-2/D-3/D-7
x24h, 6 exogenous columns x {D,D-1,D-7} x24h, hydro D-7, weekday/holiday/
Fourier calendar terms) and the 24-hour target vector for D. See
notes/03-baseline.md and data/s3/model_config.json for the full spec.

Run nothing directly -- this is a library module.
"""

from __future__ import annotations

from pathlib import Path

import holidays
import numpy as np
import pandas as pd

from common import ROOT

TZ = "Europe/Stockholm"
DATA_S1 = ROOT / "data" / "s1"

TARGET_COL = "price_eur_mwh"
EXOG_COLS = [
    "load_forecast_mw",
    "wind_onshore_forecast_mw",
    "solar_forecast_mw",
    "avail_gen_mw",
    "unavail_tx_import_mw",
    "unavail_tx_export_mw",
]
AVAILABILITY_COLS = ["avail_gen_mw", "unavail_tx_import_mw", "unavail_tx_export_mw"]
PRICE_LAG_DAYS = [1, 2, 3, 7]
EXOG_LAG_DAYS = [0, 1, 7]
HYDRO_COL = "hydro_reservoir_fill"
HYDRO_LAG_DAYS = 7
N_FOURIER_HARMONICS = 2
FFILL_COLS = [TARGET_COL] + EXOG_COLS + [HYDRO_COL]

SE_HOLIDAYS = holidays.Sweden(include_sundays=False)
# CORRECTED 2026-09-12: holidays.country_holidays("SE") (and Sweden() with
# its own default) counts every Sunday as a holiday -- 63 "holidays" in
# 2024 vs. the 13 real ones -- making the dummy near-collinear with the
# weekday_6 dummy already in the feature set rather than capturing genuine
# one-off holiday effects. include_sundays=False verified to give the
# real 13.


def to_local_day(D, tz: str = TZ) -> pd.Timestamp:
    """Normalize D (a date string, naive Timestamp, or already-tz-aware
    Timestamp/DatetimeIndex element) to a local-midnight, tz-aware
    Timestamp. pd.Timestamp(D, tz=tz) raises if D already carries tzinfo
    (e.g. a value pulled straight from table.index), so this is the one
    place every day-taking function in s3_features/s3_lear/s3_metrics routes
    through."""
    ts = pd.Timestamp(D)
    ts = ts.tz_localize(tz) if ts.tzinfo is None else ts.tz_convert(tz)
    return ts.normalize()


def load_table() -> tuple[pd.DataFrame, dict]:
    """Load se3_hourly.csv, tz-aware Europe/Stockholm index, and forward-fill
    the columns features are built from. ffill is applied here (source-table
    level, using the FULL history), not on a per-window assembled feature
    matrix -- ffill-ing only within a rolling window would leave a NaN at the
    window's own first row unfixed whenever the true prior value exists just
    outside the window. Reports before/after NaN counts per column; the
    residual after ffill is expected to be ~0 except for genuine DST
    spring-forward hours (which are absent rows, not NaN, and are handled at
    the day-profile level instead -- see day_hour_profile)."""
    df = pd.read_csv(DATA_S1 / "se3_hourly.csv", index_col=0)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(TZ)
    df = df.sort_index()
    nan_before = {c: int(df[c].isna().sum()) for c in FFILL_COLS}
    df[FFILL_COLS] = df[FFILL_COLS].ffill()
    nan_after = {c: int(df[c].isna().sum()) for c in FFILL_COLS}
    report = {"nan_before_ffill": nan_before, "nan_after_ffill": nan_after}
    return df, report


def day_hour_profile(series: pd.Series, day) -> np.ndarray:
    """24-length array (hour-of-day 0..23, local wall-clock) for calendar
    date `day` in Europe/Stockholm. DST edge cases: a 23-hour spring-forward
    day leaves the skipped hour NaN (a genuinely absent row, not missing
    data); a 25-hour fall-back day has two timestamps sharing the same
    wall-clock hour label (distinct UTC instants) -- the first (earlier UTC,
    since the table is chronologically sorted) is kept, matching this
    project's existing keep="first" convention for ambiguous-label dedup
    (s1_build_table.py _load_series_dir).

    day_end uses pd.DateOffset, not pd.Timedelta: CORRECTED 2026-09-12,
    found while re-verifying the S3 test run (see s3_backtest.py
    _date_range's docstring for the general bug class). Timedelta is
    absolute-duration arithmetic, so on the fall-back day itself
    `day + Timedelta(days=1)` landed at 23:00 the SAME day (one hour short
    of real midnight), which silently excluded that day's genuine, real
    hour-23 row from day_data -- not a labeling issue, an actual dropped
    observation. DateOffset stays at local midnight on both DST boundaries
    (verified directly against every 2024 transition date)."""
    day = to_local_day(day)
    day_end = day + pd.DateOffset(days=1)
    day_data = series[(series.index >= day) & (series.index < day_end)]
    out = np.full(24, np.nan)
    seen = set()
    for ts, v in day_data.items():
        h = ts.hour
        if h not in seen:
            out[h] = v
            seen.add(h)
    return out


def build_feature_row(
    table: pd.DataFrame, D, exog_override: dict[str, np.ndarray] | None = None
) -> dict[str, float]:
    """One delivery day's feature dict, in a fixed, deterministic key order
    (dict insertion order in Python 3.7+ -- relied on by fit_day/predict_day
    to build the X vector consistently).

    exog_override: {column_name: 24-array} -- replaces ONLY the day-D
    (lag=0) block of the named column before scaling; D-1 and D-7 lags of
    that same column are still read from the table untouched. This is the
    frozen-f seam S4/S5 use (persistence fill at predict time, sampled paths
    for the DR-agent condition, realised values for the oracle condition)."""
    D = to_local_day(D)
    feats: dict[str, float] = {}

    for lag in PRICE_LAG_DAYS:
        day = D - pd.DateOffset(days=lag)  # DateOffset, not Timedelta -- see day_hour_profile's docstring
        prof = day_hour_profile(table[TARGET_COL], day)
        for h in range(24):
            feats[f"price_lag{lag}d_h{h:02d}"] = prof[h]

    for col in EXOG_COLS:
        for lag in EXOG_LAG_DAYS:
            tag = "d" if lag == 0 else f"dminus{lag}d"
            if lag == 0 and exog_override is not None and col in exog_override:
                prof = np.asarray(exog_override[col], dtype=float)
                if prof.shape != (24,):
                    raise ValueError(f"exog_override[{col!r}] must be a 24-array, got shape {prof.shape}")
            else:
                day = D - pd.DateOffset(days=lag)  # DateOffset, not Timedelta -- see day_hour_profile's docstring
                prof = day_hour_profile(table[col], day)
            for h in range(24):
                feats[f"{col}_{tag}_h{h:02d}"] = prof[h]

    hydro_day = D - pd.DateOffset(days=HYDRO_LAG_DAYS)  # DateOffset, not Timedelta -- see day_hour_profile's docstring
    hydro_prof = day_hour_profile(table[HYDRO_COL], hydro_day)
    feats["hydro_reservoir_fill_dminus7d"] = (
        float(np.nanmean(hydro_prof)) if not np.all(np.isnan(hydro_prof)) else np.nan
    )

    weekday = D.weekday()
    for i in range(7):
        feats[f"weekday_{i}"] = 1.0 if weekday == i else 0.0

    feats["is_public_holiday"] = 1.0 if D.date() in SE_HOLIDAYS else 0.0

    doy = D.dayofyear
    for k in range(1, N_FOURIER_HARMONICS + 1):
        feats[f"fourier_sin_{k}"] = float(np.sin(2 * np.pi * k * doy / 365.25))
        feats[f"fourier_cos_{k}"] = float(np.cos(2 * np.pi * k * doy / 365.25))

    return feats


def build_target_row(table: pd.DataFrame, D) -> np.ndarray:
    return day_hour_profile(table[TARGET_COL], D)


def persistence_fill(table: pd.DataFrame, D, col: str) -> np.ndarray:
    """The baseline's forecast-time fill for an availability column: the
    value at D-1 12:00 local (SDAC gate closure), held constant over all 24
    hours of D. Used by predict_day as the default exog_override for
    AVAILABILITY_COLS."""
    D = to_local_day(D)
    t0 = D - pd.Timedelta(hours=12)
    val = table[col].asof(t0)
    return np.full(24, float(val))


def feature_names_reference(table: pd.DataFrame) -> list[str]:
    """Deterministic feature name list, derived by building one row against
    real data (rather than hand-duplicating the loop structure above)."""
    sample_day = table.index[-1].normalize()
    return list(build_feature_row(table, sample_day).keys())
