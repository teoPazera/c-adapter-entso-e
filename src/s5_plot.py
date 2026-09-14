"""Create S5 visual comparisons from the completed context-adapter run."""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from common import ROOT

DATA_S4 = ROOT / "data" / "s4"
OUT = DATA_S4 / "s5_comparison.png"


def main() -> None:
    metrics = json.loads((DATA_S4 / "s5_metrics.json").read_text())
    by_event = pd.read_csv(DATA_S4 / "s5_by_event.csv")

    conditions = ["no_context", "context", "oracle"]
    labels = ["No context", "Context\nadapter", "Oracle\ndriver"]
    colors = ["#5B6573", "#1F77B4", "#2CA25F"]
    maes = [metrics[c]["mae"] for c in conditions]

    fig, (ax0, ax1) = plt.subplots(
        1, 2, figsize=(14, 6), gridspec_kw={"width_ratios": [0.85, 1.55]}
    )
    fig.patch.set_facecolor("white")

    bars = ax0.bar(labels, maes, color=colors, width=0.65)
    ax0.set_title("Overall hourly MAE", loc="left", fontweight="bold")
    ax0.set_ylabel("MAE (EUR/MWh)")
    ax0.set_ylim(0, max(maes) * 1.18)
    ax0.grid(axis="y", alpha=0.22)
    ax0.spines[["top", "right"]].set_visible(False)
    for bar, value in zip(bars, maes):
        ax0.text(bar.get_x() + bar.get_width() / 2, value + 0.18, f"{value:.2f}", ha="center", va="bottom", fontweight="bold")
    ax0.text(
        0.02, -0.23,
        f"Context vs. no-context: {metrics['paired']['context_minus_no_context_mae']:+.3f} EUR/MWh\n"
        f"Oracle vs. no-context: {metrics['paired']['oracle_minus_no_context_mae']:+.3f} EUR/MWh",
        transform=ax0.transAxes, fontsize=9, color="#46505A"
    )

    plot = by_event.sort_values("context_minus_no_context_mae")
    y = np.arange(len(plot))
    deltas = plot["context_minus_no_context_mae"].to_numpy()
    bar_colors = np.where(deltas < 0, "#2CA25F", "#D95F5F")
    ax1.barh(y, deltas, color=bar_colors, height=0.68)
    ax1.axvline(0, color="#25303B", linewidth=1)
    ax1.set_yticks(y, plot["event_id"])
    ax1.invert_yaxis()
    ax1.set_title("Context-adapter MAE change by event", loc="left", fontweight="bold")
    ax1.set_xlabel("Context MAE − no-context MAE (EUR/MWh); lower is better")
    ax1.grid(axis="x", alpha=0.22)
    ax1.spines[["top", "right", "left"]].set_visible(False)
    for yi, value in zip(y, deltas):
        align = "left" if value >= 0 else "right"
        x = value + (0.025 if value >= 0 else -0.025)
        ax1.text(x, yi, f"{value:+.2f}", va="center", ha=align, fontsize=8)

    fig.suptitle(
        "S5: Frozen S3 baseline vs. LLM context adapter vs. realized-driver oracle\n"
        "12 events, 92 event-days, 2,207 scoreable hourly observations",
        x=0.05, ha="left", fontsize=14, fontweight="bold"
    )
    fig.text(
        0.05, 0.012,
        "Context forecasts average five temperature-0.4 Nano-derived driver paths. "
        "Negative event bars indicate improvement over no-context.",
        fontsize=9, color="#46505A"
    )
    plt.tight_layout(rect=[0, 0.06, 1, 0.9])
    fig.savefig(OUT, dpi=200, bbox_inches="tight")
    print(OUT)


if __name__ == "__main__":
    main()
