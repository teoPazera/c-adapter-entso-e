"""S3 Step 4: MAE, rMAE, pinball/CRPS, coverage, naive benchmarks.

CRPS uses the pinball-loss identity CRPS = 2 * mean_over_tau(pinball_tau),
approximated on a 19-point grid (tau = 0.05..0.95), not CiK's own O(k^2 n^3)
sample-based estimator (S2a Finding, thesis/ notes -- not reused here, this
is a plain point-quantile forecast, not a sample-array forecast).

Quantile forecasts are point_forecast + the empirical tau-quantile of a
per-hour-of-day rolling window of trailing residuals (see
RollingResidualQuantiles). This is deliberately walk-forward / no-lookahead:
a day's quantile forecast only ever reads residuals from strictly earlier
days; the caller must call quantiles_for() before update() for the same
observation.

Run nothing directly -- this is a library module.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

QUANTILE_GRID = [round(0.05 * i, 2) for i in range(1, 20)]  # 0.05, 0.10, ..., 0.95
ROLLING_MAX_DAYS = 90
ROLLING_MIN_DAYS = 28


def mae(y_true, y_pred) -> float:
    """isfinite, not isnan: a LassoLarsIC numerical blowup (Finding 11) can
    produce a finite-but-garbage or literal-inf y_pred without ever
    surfacing as NaN. isnan alone let a single inf row poison this whole
    aggregate to Infinity on the first real test run -- caught, not
    silently patched (see notes/03-baseline.md Finding 11)."""
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    mask = np.isfinite(y_true) & np.isfinite(y_pred)
    if not mask.any():
        return float("nan")
    return float(np.mean(np.abs(y_true[mask] - y_pred[mask])))


def rmae(mae_model: float, mae_naive: float) -> float | None:
    if mae_naive is None or mae_naive == 0 or np.isnan(mae_naive):
        return None
    return mae_model / mae_naive


def pinball_loss_scalar(y_true: float, q_pred: float, tau: float) -> float:
    diff = y_true - q_pred
    return max(tau * diff, (tau - 1) * diff)


def crps_from_quantiles(y_true: float, quantile_preds: dict[float, float]) -> float:
    """CRPS approx for one observation given its full quantile-grid forecast."""
    losses = [pinball_loss_scalar(y_true, quantile_preds[t], t) for t in QUANTILE_GRID]
    return 2.0 * float(np.mean(losses))


def coverage(y_true, q_low, q_high) -> float:
    """Fraction of y_true within [q_low, q_high] (e.g. the 90% interval:
    q05 <= y <= q95)."""
    y_true = np.asarray(y_true, dtype=float)
    q_low = np.asarray(q_low, dtype=float)
    q_high = np.asarray(q_high, dtype=float)
    mask = ~(np.isnan(y_true) | np.isnan(q_low) | np.isnan(q_high))
    if not mask.any():
        return float("nan")
    inside = (y_true[mask] >= q_low[mask]) & (y_true[mask] <= q_high[mask])
    return float(np.mean(inside))


class RollingResidualQuantiles:
    """Per-hour-of-day rolling buffer of (actual - point_forecast) residuals.

    quantiles_for(hour, point_forecast) returns None until ROLLING_MIN_DAYS
    residuals have been observed for that hour (burn-in); otherwise returns
    {tau: point_forecast + empirical_quantile(residuals[-ROLLING_MAX_DAYS:], tau)}.
    Call quantiles_for() to score a day BEFORE calling update() with that
    same day's realised residual, or the quantile forecast leaks the answer.
    """

    def __init__(self, max_days: int = ROLLING_MAX_DAYS, min_days: int = ROLLING_MIN_DAYS):
        self.max_days = max_days
        self.min_days = min_days
        self.history: dict[int, list[float]] = {h: [] for h in range(24)}

    def is_ready(self, hour: int) -> bool:
        return len(self.history[hour]) >= self.min_days

    def quantiles_for(self, hour: int, point_forecast: float) -> dict[float, float] | None:
        if not self.is_ready(hour):
            return None
        resid = np.asarray(self.history[hour][-self.max_days:], dtype=float)
        return {tau: point_forecast + float(np.quantile(resid, tau)) for tau in QUANTILE_GRID}

    def update(self, hour: int, actual: float, point_forecast: float) -> None:
        if np.isnan(actual) or np.isnan(point_forecast):
            return
        self.history[hour].append(actual - point_forecast)


def naive_lago_forecast(table: pd.DataFrame, D, tz: str = "Europe/Stockholm") -> np.ndarray:
    """Lago et al. 2021's standard EPF naive: price of D-1 on Tue-Fri, price
    of D-7 on Mon/Sat/Sun (captures the weekly seasonality a plain lag-24
    misses across the weekend boundary)."""
    from s3_features import day_hour_profile, to_local_day

    D = to_local_day(D, tz=tz)
    lag_days = 7 if D.weekday() in (0, 5, 6) else 1  # Mon=0, Sat=5, Sun=6
    # DateOffset, not Timedelta -- see s3_features.day_hour_profile's docstring
    return day_hour_profile(table["price_eur_mwh"], D - pd.DateOffset(days=lag_days))


def naive_lag24_forecast(table: pd.DataFrame, D, tz: str = "Europe/Stockholm") -> np.ndarray:
    from s3_features import day_hour_profile, to_local_day

    D = to_local_day(D, tz=tz)
    return day_hour_profile(table["price_eur_mwh"], D - pd.DateOffset(days=1))


def summarize_period(df: pd.DataFrame) -> dict:
    """df: one row per (day,hour) with columns y, point, naive, naive_lag24,
    and (only for rows past burn-in) q05..q95 / naive_q05..naive_q95.
    CRPS/coverage are computed only over rows where the model's q05 is not
    NaN (i.e. past the rolling-quantile burn-in)."""
    out: dict = {"n_obs": int(len(df))}
    out["mae_model"] = mae(df["y"], df["point"])
    out["mae_naive"] = mae(df["y"], df["naive"])
    out["mae_naive_lag24"] = mae(df["y"], df["naive_lag24"])
    out["rmae_vs_naive"] = rmae(out["mae_model"], out["mae_naive"])
    out["rmae_vs_naive_lag24"] = rmae(out["mae_model"], out["mae_naive_lag24"])

    q_col = "q05"
    # Also require y notna: a 23-hour DST spring-forward day leaves y NaN
    # for the skipped hour while point/q05..q95 are still populated (the
    # model always emits 24 predictions regardless of which local hours
    # exist that day) -- without this, a single such row poisons
    # np.mean(crps_*) to NaN for the whole period, silently. mae() already
    # guards against this via its own NaN mask; this mirrors that here.
    #
    # isfinite(point)/isfinite(q_col), not just notna(): a LassoLarsIC
    # numerical blowup (Finding 11) produces a finite garbage or literal-inf
    # point forecast that RollingResidualQuantiles then propagates into
    # every quantile column (point_forecast + residual quantile). notna()
    # alone does not catch inf, so it silently let one exploded day through
    # scoreable, poisoning crps_model to Infinity on the first real test
    # run. Excluded here rather than patched at the model level (S3 gate
    # decision -- see the note): the day's DayModel still exists and still
    # reloads to its stored (garbage) forecast, it is just not scored.
    out["n_obs_nonfinite_point"] = int((~np.isfinite(df["point"])).sum()) if "point" in df.columns else 0
    scoreable = (
        df[df["y"].notna() & np.isfinite(df["point"]) & np.isfinite(df[q_col])]
        if q_col in df.columns
        else df.iloc[0:0]
    )
    out["n_obs_scoreable_crps"] = int(len(scoreable))
    if len(scoreable):
        crps_model = [
            crps_from_quantiles(row["y"], {t: row[f"q{round(t * 100):02d}"] for t in QUANTILE_GRID})
            for _, row in scoreable.iterrows()
        ]
        crps_naive = [
            crps_from_quantiles(row["y"], {t: row[f"naive_q{round(t * 100):02d}"] for t in QUANTILE_GRID})
            for _, row in scoreable.iterrows()
        ]
        # np.nanmean, not np.mean: the model and naive rolling-quantile
        # trackers accumulate residuals independently (different residual
        # definitions), so one can reach its burn-in a few days after the
        # other, or a single upstream NaN forecast (naive_lago_forecast on
        # a day whose D-7 lookup was corrupted by the pre-fix _date_range
        # drift bug -- Finding 6) can leave one tracker's quantile columns
        # NaN on a row where the other's are already populated. Found on
        # the real test run: 3 of 27,378 scoreable rows had a NaN
        # naive_q05, which silently zeroed out crps_naive entirely under
        # plain np.mean. nanmean drops exactly those rows from THIS
        # metric's own denominator instead of poisoning it.
        out["n_crps_model_nan_rows"] = int(np.sum(np.isnan(crps_model)))
        out["n_crps_naive_nan_rows"] = int(np.sum(np.isnan(crps_naive)))
        out["crps_model"] = float(np.nanmean(crps_model))
        out["crps_naive"] = float(np.nanmean(crps_naive))
        out["coverage90_model"] = coverage(scoreable["y"], scoreable["q05"], scoreable["q95"])
        out["coverage90_naive"] = coverage(scoreable["y"], scoreable["naive_q05"], scoreable["naive_q95"])
    else:
        out["crps_model"] = out["crps_naive"] = None
        out["coverage90_model"] = out["coverage90_naive"] = None
    return out
