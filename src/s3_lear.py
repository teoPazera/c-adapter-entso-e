"""S3 Step 3: LEAR (Lago et al. 2021) reimplementation with scikit-learn.

24 independent LassoLarsIC(criterion="aic") models per delivery day D, one
per hour, fit on a rolling window of `window_days` calibration days ending
D-1. Frozen-f interface: fit_day/predict_day/DayModel.save/load. S4 and S5
reuse a saved DayModel unchanged; they only ever vary predict_day's
exog_override.

Run nothing directly -- this is a library module.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from joblib import Parallel, delayed
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import Lasso, LassoLarsIC

# 541 features on a p>n-ish window drives LARS deep into a near-collinear
# region on many (day, hour) fits -- expected given the feature set's
# design (many lagged/correlated columns), not a sign of a bad fit; AIC
# still stops at a sensible point. Silenced at import time so it also
# applies inside joblib's worker processes (each re-imports this module).
warnings.filterwarnings("ignore", category=ConvergenceWarning)

from s3_features import (
    AVAILABILITY_COLS,
    build_feature_row,
    build_target_row,
    to_local_day,
    persistence_fill,
)

MIN_VALID_ROWS_PER_HOUR = 10  # below this, leave a zero model (predicts the scaled median)

COEF_BLOWUP_THRESHOLD = 1e3
# Observed on the real S3 test/validation runs (notes/03-baseline.md Finding 11): every
# well-behaved per-hour fit has max|coef| <= 1.64; every LARS numerical blowup found by an
# exhaustive scan of all 1,531 cached models has max|coef| >= 9e10 -- nothing in between, so
# any threshold in this range separates them cleanly. Root cause: a near-duplicate pair of
# unavail_tx_import_mw night-hour (h02/h03/h04) lag columns, identical on ~99.5% of
# calibration days, drives LARS's active set into a degenerate +c/-c pair that "fits" the
# rare days where the two columns differ -- not a real Lasso optimum at any finite alpha (the
# L1 penalty on a 1e12 coefficient would be astronomical).


@dataclass
class DayModel:
    D: str  # ISO date string -- the delivery day this model forecasts
    window_days: int
    feature_names: list[str]
    coef: np.ndarray  # (24, F)
    intercept: np.ndarray  # (24,)
    x_median: np.ndarray  # (F,)
    x_mad: np.ndarray  # (F,)
    y_median: np.ndarray  # (24,)
    y_mad: np.ndarray  # (24,)
    n_calibration_days: int
    n_ffill_feature_nan: int
    n_bfill_feature_nan: int
    n_degenerate_hours: int  # hours with < MIN_VALID_ROWS_PER_HOUR valid targets
    n_numerical_failure_hours: int  # hours where LassoLarsIC itself raised (degenerate active set); zero model used
    n_lars_blowup_hours: int  # hours where LassoLarsIC returned without raising but max|coef| > COEF_BLOWUP_THRESHOLD; coordinate-descent refit used instead
    noise_variance_method: str  # "ols" (sklearn default, n>p+1) or "ridge_fallback" (n<=p+1)

    def save(self, path: Path) -> None:
        """Writes to a temp file, then atomically renames into place.
        Without this, a process killed mid-write (this project has hit
        LARS crashes and a Windows/joblib BrokenProcessPool this session
        alone) can leave a truncated/corrupt .npz at the real path, which
        the resume logic's `_model_path(...).exists()` check would then
        trust and load as if it were a complete, valid fit."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        with open(tmp_path, "wb") as f:
            np.savez(
                f,
                D=self.D,
                window_days=self.window_days,
                feature_names=np.array(self.feature_names),
                coef=self.coef.astype(np.float32),
                intercept=self.intercept.astype(np.float32),
                x_median=self.x_median.astype(np.float32),
                x_mad=self.x_mad.astype(np.float32),
                y_median=self.y_median.astype(np.float32),
                y_mad=self.y_mad.astype(np.float32),
                n_calibration_days=self.n_calibration_days,
                n_ffill_feature_nan=self.n_ffill_feature_nan,
                n_bfill_feature_nan=self.n_bfill_feature_nan,
                n_degenerate_hours=self.n_degenerate_hours,
                n_numerical_failure_hours=self.n_numerical_failure_hours,
                n_lars_blowup_hours=self.n_lars_blowup_hours,
                noise_variance_method=self.noise_variance_method,
            )
        os.replace(tmp_path, path)

    @staticmethod
    def load(path: Path) -> "DayModel":
        z = np.load(path, allow_pickle=False)
        return DayModel(
            D=str(z["D"]),
            window_days=int(z["window_days"]),
            feature_names=list(z["feature_names"]),
            coef=z["coef"].astype(np.float64),
            intercept=z["intercept"].astype(np.float64),
            x_median=z["x_median"].astype(np.float64),
            x_mad=z["x_mad"].astype(np.float64),
            y_median=z["y_median"].astype(np.float64),
            y_mad=z["y_mad"].astype(np.float64),
            n_calibration_days=int(z["n_calibration_days"]),
            n_ffill_feature_nan=int(z["n_ffill_feature_nan"]),
            n_bfill_feature_nan=int(z["n_bfill_feature_nan"]),
            n_degenerate_hours=int(z["n_degenerate_hours"]),
            n_numerical_failure_hours=int(z["n_numerical_failure_hours"]),
            n_lars_blowup_hours=int(z["n_lars_blowup_hours"]) if "n_lars_blowup_hours" in z.files else 0,
            noise_variance_method=str(z["noise_variance_method"]),
        )


def _ridge_noise_variance(X: np.ndarray, Y: np.ndarray) -> np.ndarray:
    """Per-hour noise-variance estimate for LassoLarsIC's AIC/BIC criterion
    when n_samples <= n_features (+1) -- sklearn's own default estimator is
    plain OLS residual variance, which needs n > p+1 and raises otherwise
    (added sklearn 1.1; hit here by the 1-year window: 365 samples <
    542 = 541 features + intercept). Standard shrinkage substitute: ridge-
    regress each hour's target on X with a data-scaled penalty (lambda =
    mean squared singular value of X, a common unit-scale heuristic), then
    use the ridge fit's own effective degrees of freedom (trace of its hat
    matrix, sum(s^2/(s^2+lambda))) in place of the OLS df in the classic
    RSS/(n-df) variance estimator. X's SVD is computed once per day and
    reused across all 24 hours (only Y differs per hour) -- this is the
    only reason fit_day computes it up front rather than inside
    _fit_one_hour. A handful of NaN targets (DST spring-forward days) are
    median-filled here ONLY for this auxiliary variance estimate; the real
    per-hour fit below still drops them via its own `valid` mask."""
    n = X.shape[0]
    col_median = np.nanmedian(Y, axis=0, keepdims=True)
    Y_filled = np.where(np.isnan(Y), col_median, Y)
    U, s, _ = np.linalg.svd(X, full_matrices=False)
    lam = float(np.mean(s**2))
    d = s**2 / (s**2 + lam)
    df_ridge = float(np.sum(d))
    denom = max(n - df_ridge, 1.0)
    fitted = U @ (d[:, None] * (U.T @ Y_filled))
    rss = np.sum((Y_filled - fitted) ** 2, axis=0)
    return np.maximum(rss / denom, 1e-8)


def _fit_one_hour(
    X_scaled: np.ndarray, y_scaled_h: np.ndarray, noise_variance: float | None, context: str = ""
) -> tuple[np.ndarray, float, bool, bool, bool]:
    valid = ~np.isnan(y_scaled_h)
    if valid.sum() < MIN_VALID_ROWS_PER_HOUR:
        return np.zeros(X_scaled.shape[1]), 0.0, True, False, False
    # X_scaled should never contain NaN here -- fit_day ffills then bfills
    # the whole window before this is called. Assert rather than let it
    # silently fall into the except below: a real NaN reaching here means
    # the ffill/bfill safety net itself has a gap, which is a different and
    # more serious problem than LARS's known numerical fragility, and
    # broadening the except to swallow it too would hide that distinction.
    assert not np.isnan(X_scaled[valid]).any(), f"NaN reached _fit_one_hour after ffill/bfill ({context})"
    model = LassoLarsIC(criterion="aic", noise_variance=noise_variance)
    try:
        model.fit(X_scaled[valid], y_scaled_h[valid])
    except (ValueError, np.linalg.LinAlgError) as e:
        # 541 heavily-lagged, highly-correlated features regularly push
        # sklearn's LARS solver into a near-singular active set (the
        # "Regressors in active set degenerate" ConvergenceWarning,
        # silenced at import time, fires on most fits) -- on rare
        # (day, hour) combinations this crosses from "recovers with a
        # warning" to an unrecovered internal shape mismatch inside
        # lars_path (seen directly: "shapes (365,109) and (108,) not
        # aligned", sklearn 1.9.1). Degrade the same way as too-few-rows
        # (a zero/median model for this one hour) rather than lose an
        # entire day's fit -- or, in a day-level-parallel backtest, crash
        # the whole multi-hour run over one hour's numerical instability.
        # Printed (not silent) so a run's console output is a record of
        # every occurrence, not just the aggregate count.
        print(f"  [_fit_one_hour] numerical failure {context}: {type(e).__name__}: {e}")
        return np.zeros(X_scaled.shape[1]), 0.0, False, True, False

    if np.abs(model.coef_).max() > COEF_BLOWUP_THRESHOLD:
        # LARS returned without raising, but the active set is degenerate
        # (see COEF_BLOWUP_THRESHOLD's docstring) -- refit the SAME
        # (alpha, objective) with coordinate descent, which cannot produce
        # this artefact. LassoLarsIC and Lasso both minimize
        # (1/2n)||y-Xw||^2 + alpha||w||_1; model.alpha_ is the AIC-chosen
        # alpha from the LARS path, reused as-is rather than re-selected.
        print(
            f"  [_fit_one_hour] LARS coefficient blowup {context}: "
            f"max|coef|={np.abs(model.coef_).max():.3e}, refitting with coordinate descent "
            f"(alpha={model.alpha_:.6g})"
        )
        cd_model = Lasso(alpha=model.alpha_, fit_intercept=True, max_iter=50_000, tol=1e-6)
        cd_model.fit(X_scaled[valid], y_scaled_h[valid])
        if np.abs(cd_model.coef_).max() > COEF_BLOWUP_THRESHOLD:
            print(
                f"  [_fit_one_hour] coordinate-descent refit still exceeds threshold {context}: "
                f"max|coef|={np.abs(cd_model.coef_).max():.3e}, degrading to zero model"
            )
            return np.zeros(X_scaled.shape[1]), 0.0, False, True, True
        return cd_model.coef_, float(cd_model.intercept_), False, False, True

    return model.coef_, float(model.intercept_), False, False, False


def fit_day(table: pd.DataFrame, D, window_days: int, n_jobs: int = 1) -> DayModel:
    """Fit on the window_days calibration days ending D-1 (D itself is the
    delivery day being forecast, never in its own calibration set). Every
    calibration row uses REALISED (same-day) values for every column,
    including the three availability columns -- only predict_day's forecast
    row uses the persistence fill. This asymmetry is the mechanism
    C_adapter_Entso_E/CLAUDE.md describes.

    Steps backward with pd.DateOffset, not pd.Timedelta: the same absolute-
    duration-vs-calendar-arithmetic bug found in s3_backtest.py's
    _date_range (see its docstring) applies here too -- a fixed-length
    Timedelta chain of ~730 steps crosses ~4 DST transitions and was
    verified to collapse 2 distinct calendar dates into duplicates (while
    silently dropping 2 different dates from the window entirely) for a
    representative D. DateOffset stays at local midnight throughout."""
    D = to_local_day(D)
    cal_days = [D - pd.DateOffset(days=k) for k in range(window_days, 0, -1)]

    rows = [build_feature_row(table, d) for d in cal_days]
    feature_names = list(rows[0].keys())
    X = np.array([[r[name] for name in feature_names] for r in rows], dtype=float)
    Y = np.array([build_target_row(table, d) for d in cal_days], dtype=float)  # (window_days, 24)

    n_nan_before = int(np.isnan(X).sum())
    X_df = pd.DataFrame(X, columns=feature_names)
    X_df = X_df.ffill()
    n_nan_after_ffill = int(X_df.isna().sum().sum())
    X_df = X_df.bfill()  # last resort: only fires if a NaN survives at the window's own first row(s)
    n_nan_after_bfill = int(X_df.isna().sum().sum())
    X = X_df.to_numpy()

    x_median = np.nanmedian(X, axis=0)
    x_mad = np.nanmedian(np.abs(X - x_median), axis=0)
    x_mad = np.where(x_mad < 1e-8, 1.0, x_mad)
    X_scaled = np.arcsinh((X - x_median) / x_mad)

    y_median = np.nanmedian(Y, axis=0)
    y_mad = np.nanmedian(np.abs(Y - y_median), axis=0)
    y_mad = np.where(y_mad < 1e-8, 1.0, y_mad)
    Y_scaled = np.arcsinh((Y - y_median) / y_mad)

    n_samples, n_features = X_scaled.shape
    use_ridge_fallback = n_samples <= n_features + 1  # sklearn's own OLS estimator needs n > p+1
    noise_variances: list[float | None]
    if use_ridge_fallback:
        noise_variances = list(_ridge_noise_variance(X_scaled, Y_scaled))
    else:
        noise_variances = [None] * 24

    results = Parallel(n_jobs=n_jobs)(
        delayed(_fit_one_hour)(X_scaled, Y_scaled[:, h], noise_variances[h], f"D={D.date()} h={h}")
        for h in range(24)
    )
    coef = np.array([r[0] for r in results])
    intercept = np.array([r[1] for r in results])
    n_degenerate = int(sum(r[2] for r in results))
    n_numerical_failure = int(sum(r[3] for r in results))
    n_lars_blowup = int(sum(r[4] for r in results))

    return DayModel(
        D=str(D.date()),
        window_days=window_days,
        feature_names=feature_names,
        coef=coef,
        intercept=intercept,
        x_median=x_median,
        x_mad=x_mad,
        y_median=y_median,
        y_mad=y_mad,
        n_calibration_days=window_days,
        n_ffill_feature_nan=n_nan_before - n_nan_after_ffill,
        n_bfill_feature_nan=n_nan_after_ffill - n_nan_after_bfill,
        n_degenerate_hours=n_degenerate,
        n_numerical_failure_hours=n_numerical_failure,
        n_lars_blowup_hours=n_lars_blowup,
        noise_variance_method="ridge_fallback" if use_ridge_fallback else "ols",
    )


def predict_day(
    model: DayModel, table: pd.DataFrame, D, exog_override: dict[str, np.ndarray] | None = None
) -> np.ndarray:
    """Baseline forecast for day D: availability columns default to the
    persistence fill (D-1 12:00 local, held constant); exog_override can
    replace any of those (or add others) for S4/S5's ablation conditions."""
    D = to_local_day(D)
    override: dict[str, np.ndarray] = {col: persistence_fill(table, D, col) for col in AVAILABILITY_COLS}
    if exog_override:
        override.update(exog_override)

    feats = build_feature_row(table, D, exog_override=override)
    x = np.array([feats[name] for name in model.feature_names], dtype=float)
    nan_mask = np.isnan(x)
    if nan_mask.any():
        x = np.where(nan_mask, model.x_median, x)  # predict-time safety net only

    x_scaled = np.arcsinh((x - model.x_median) / model.x_mad)
    y_scaled = model.coef @ x_scaled + model.intercept
    y = np.sinh(y_scaled) * model.y_mad + model.y_median
    return y
