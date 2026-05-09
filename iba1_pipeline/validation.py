"""Validation against manual counts.

Produces:
* ``validation/validation_summary.csv`` — per-image manual vs automated counts.
* ``validation/scatter.png`` — scatter plot with y=x reference.
* ``validation/bland_altman.png`` — Bland–Altman plot.
* ``validation/group_summary.csv`` — group-wise error breakdown if metadata
  contains a ``group`` column.

Crucially, error metrics are reported per group as well as overall: a
detector that works on control images but generates false positives on
microglia-depleted images is unacceptable, and only a per-group view exposes
that.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

logger = logging.getLogger("iba1_pipeline")


def _scatter_plot(manual: np.ndarray, automated: np.ndarray, group: Optional[np.ndarray],
                  output_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(5, 5))
    if group is None:
        ax.scatter(manual, automated, alpha=0.7)
    else:
        for g in np.unique(group):
            sel = group == g
            ax.scatter(manual[sel], automated[sel], alpha=0.7, label=str(g))
        ax.legend(loc="best", fontsize=8)
    lim = max(float(manual.max(initial=0)), float(automated.max(initial=0))) * 1.1 + 1
    ax.plot([0, lim], [0, lim], "k--", linewidth=0.7)
    ax.set_xlim(0, lim)
    ax.set_ylim(0, lim)
    ax.set_xlabel("Manual count")
    ax.set_ylabel("Automated count")
    ax.set_title("Automated vs manual counts")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def _bland_altman_plot(manual: np.ndarray, automated: np.ndarray,
                       group: Optional[np.ndarray], output_path: Path) -> None:
    means = (manual + automated) / 2.0
    diffs = automated - manual  # signed: positive = overcounting
    md = float(diffs.mean())
    sd = float(diffs.std(ddof=1)) if diffs.size > 1 else 0.0
    fig, ax = plt.subplots(figsize=(6, 4.5))
    if group is None:
        ax.scatter(means, diffs, alpha=0.7)
    else:
        for g in np.unique(group):
            sel = group == g
            ax.scatter(means[sel], diffs[sel], alpha=0.7, label=str(g))
        ax.legend(loc="best", fontsize=8)
    ax.axhline(md, color="k", linestyle="-", linewidth=0.8, label=f"mean diff = {md:.2f}")
    ax.axhline(md + 1.96 * sd, color="k", linestyle="--", linewidth=0.6,
               label=f"+1.96 SD = {md + 1.96 * sd:.2f}")
    ax.axhline(md - 1.96 * sd, color="k", linestyle="--", linewidth=0.6,
               label=f"-1.96 SD = {md - 1.96 * sd:.2f}")
    ax.set_xlabel("Mean of manual and automated")
    ax.set_ylabel("Automated - manual")
    ax.set_title("Bland–Altman")
    ax.legend(loc="best", fontsize=7)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def validate_against_manual_counts(
    summary_csv: Path,
    manual_csv: Path,
    output_dir: Path,
) -> Dict[str, float]:
    """Compare automated counts to manual counts, write plots and CSVs.

    Returns a dictionary of overall metrics for use by the optimizer.
    """
    summary_csv = Path(summary_csv)
    manual_csv = Path(manual_csv)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = pd.read_csv(summary_csv)
    manual = pd.read_csv(manual_csv)
    if "manual_count" not in manual.columns:
        raise ValueError(f"Manual CSV must have a 'manual_count' column ({manual_csv})")
    if "image_id" not in manual.columns:
        raise ValueError(f"Manual CSV must have an 'image_id' column ({manual_csv})")
    if "roi_id" not in manual.columns:
        manual = manual.assign(roi_id="whole_image")

    merged = summary.merge(
        manual[["image_id", "roi_id", "manual_count"]],
        on=["image_id", "roi_id"],
        how="inner",
    )
    if merged.empty:
        logger.warning("No rows matched between automated summary and manual counts.")
        return {"n": 0}

    final_count = merged["count_corrected"].fillna(merged["count"]).astype(float)
    manual_count = merged["manual_count"].astype(float)
    diffs = final_count - manual_count

    n = int(len(merged))
    mae = float(np.abs(diffs).mean())
    rmse = float(np.sqrt(np.square(diffs).mean()))
    mse = float(np.square(diffs).mean())
    bias = float(diffs.mean())
    pearson = float(stats.pearsonr(manual_count, final_count)[0]) if n >= 2 else float("nan")
    spearman = float(stats.spearmanr(manual_count, final_count).statistic) if n >= 2 else float("nan")

    metrics: Dict[str, float] = {
        "n": n,
        "mae": mae,
        "rmse": rmse,
        "mse": mse,
        "bias_signed_error": bias,
        "pearson_r": pearson,
        "spearman_r": spearman,
    }

    val_summary = merged[["image_id", "roi_id", "count", "count_corrected",
                          "manual_count", "group"]].copy() if "group" in merged.columns else \
                  merged[["image_id", "roi_id", "count", "count_corrected", "manual_count"]].copy()
    val_summary["final_count"] = final_count.values
    val_summary["error_signed"] = diffs.values
    val_summary["error_abs"] = np.abs(diffs.values)
    val_summary.to_csv(output_dir / "validation_summary.csv", index=False)

    group_arr = merged["group"].astype(str).values if "group" in merged.columns else None
    _scatter_plot(manual_count.values, final_count.values, group_arr,
                  output_dir / "scatter.png")
    _bland_altman_plot(manual_count.values, final_count.values, group_arr,
                       output_dir / "bland_altman.png")

    if group_arr is not None:
        rows = []
        for g in np.unique(group_arr):
            sel = group_arr == g
            d = diffs.values[sel]
            rows.append({
                "group": g,
                "n": int(sel.sum()),
                "mae": float(np.abs(d).mean()),
                "rmse": float(np.sqrt(np.square(d).mean())),
                "bias_signed_error": float(d.mean()),
                "manual_mean": float(manual_count.values[sel].mean()),
                "automated_mean": float(final_count.values[sel].mean()),
            })
        pd.DataFrame(rows).to_csv(output_dir / "group_summary.csv", index=False)

    pd.DataFrame([metrics]).to_csv(output_dir / "overall_metrics.csv", index=False)

    return metrics
