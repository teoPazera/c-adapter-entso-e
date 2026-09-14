"""Five-path Monte Carlo sampling for an S4 availability-driver effect."""

from __future__ import annotations

import numpy as np


def _draw_bounded_normal(rng: np.random.Generator, mean: float, sd: float) -> float:
    return float(np.clip(mean if sd == 0 else rng.normal(mean, sd), 0, 24))


def _draw_size(rng: np.random.Generator, quantiles: dict[str, float]) -> float:
    # Simple triangular approximation anchored at documented p10/p50/p90.
    left, mode, right = (float(quantiles[k]) for k in ("p10", "p50", "p90"))
    if left == right:  # deterministic document-derived magnitude
        return left
    return float(rng.triangular(left, mode, right))


def sample_paths(base: np.ndarray, effect: dict, driver: str, n_paths: int = 5, seed: int = 42) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    paths: list[np.ndarray] = []
    for _ in range(n_paths):
        path = np.asarray(base, dtype=float).copy()
        if effect["direction"] == "none" or rng.random() > effect["p_active"]:
            paths.append(path)
            continue
        start = _draw_bounded_normal(rng, **effect["start_hour"])
        end = _draw_bounded_normal(rng, **effect["end_hour"])
        if end < start:
            start, end = end, start
        hours = np.arange(24)
        active = (hours >= int(np.floor(start))) & (hours < int(np.ceil(end)))
        size = _draw_size(rng, effect["size_mw"])
        if driver == "avail_gen_mw":
            path[active] -= size
        elif driver == "unavail_tx_import_mw":
            path[active] += size
        else:
            raise ValueError(f"Unsupported S4 driver {driver!r}")
        paths.append(path)
    return paths
