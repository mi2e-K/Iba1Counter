"""Parameter optimization via grid search against manual counts.

Optimization is intentionally simple — exhaustive grid search over a few
critical parameters (provided via ``optimization.grids`` in the config).
For each parameter combination we run the pipeline on the training subset,
compare to manual counts, and pick the configuration that minimises the
chosen error metric. The resulting parameters are written to disk and can
then be plugged back into the main config for batch processing.

Two error metrics are supported:

* ``mae`` — mean absolute error across the training subset.
* ``mae_balanced`` — average of per-group MAE; this prevents control images
  (often higher counts) from dominating the optimization and missing
  overcounting in microglia-depleted groups.
"""

from __future__ import annotations

import copy
import itertools
import logging
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml

from .batch import run_batch
from .config import Config, dump_config

logger = logging.getLogger("iba1_pipeline")


# Mapping from grid key to (path inside Config) so the optimizer can set the
# right field. Add to this map if new grids are introduced.
_GRID_KEY_PATHS: Dict[str, Tuple[str, ...]] = {
    "soma_radius_px": ("soma_enhancement", "soma_radius_px"),
    "min_peak_intensity": ("seed_detection", "min_peak_intensity"),
    "min_distance_px": ("seed_detection", "min_distance_px"),
    "background_radius_px": ("background", "radius_px"),
    "min_area_um2": ("object_filter", "min_area_um2"),
    "max_area_um2": ("object_filter", "max_area_um2"),
    "min_mean_intensity": ("object_filter", "min_mean_intensity"),
    "min_circularity": ("object_filter", "min_circularity"),
    "min_solidity": ("object_filter", "min_solidity"),
}


def _set_path(config: Config, path: Tuple[str, ...], value) -> None:
    obj = config
    for key in path[:-1]:
        obj = getattr(obj, key)
    setattr(obj, path[-1], value)


def _balanced_mae(errors: np.ndarray, groups: np.ndarray) -> float:
    """Average of per-group MAEs."""
    if len(errors) == 0:
        return float("inf")
    if groups is None or len(np.unique(groups)) <= 1:
        return float(np.abs(errors).mean())
    parts: List[float] = []
    for g in np.unique(groups):
        sel = groups == g
        if sel.any():
            parts.append(float(np.abs(errors[sel]).mean()))
    return float(np.mean(parts))


def _compute_error(summary_csv: Path, manual_csv: Path, metric: str) -> float:
    summary = pd.read_csv(summary_csv)
    manual = pd.read_csv(manual_csv)
    if "roi_id" not in manual.columns:
        manual["roi_id"] = "whole_image"
    merged = summary.merge(
        manual[["image_id", "roi_id", "manual_count"]],
        on=["image_id", "roi_id"],
        how="inner",
    )
    if merged.empty:
        return float("inf")
    final = merged["count_corrected"].fillna(merged["count"]).astype(float).values
    truth = merged["manual_count"].astype(float).values
    err = final - truth
    if metric == "rmse":
        return float(math.sqrt(float(np.mean(err ** 2))))
    if metric == "mae_balanced":
        groups = merged["group"].astype(str).values if "group" in merged.columns else None
        return _balanced_mae(err, groups)
    return float(np.abs(err).mean())


def _filter_to_subset(config: Config, training_subset: Optional[List[str]]) -> Config:
    """Return a copy of config restricted to the training subset.

    The simplest way to restrict the run is to set ``file_glob`` to a list-like
    pattern. Since ``find_images`` only takes a single glob, we instead create
    a temporary input directory of symlinks. Here, we keep things simple by
    leaving the input directory alone but post-filtering after the run via the
    manual_count CSV; a missing image_id in manual just means no contribution
    to the metric.
    """
    return config  # post-filtering handled by the merge against manual CSV


@dataclass
class OptimizationResult:
    best_combo: Dict[str, float]
    best_metric: float
    metric_name: str
    all_combos: List[Dict[str, float]]


def run_optimization(config: Config, base_output_dir: Path) -> OptimizationResult:
    """Exhaustive grid search over ``config.optimization.grids``.

    For each combination, runs the pipeline into a per-trial output subdir,
    computes the chosen error metric against the manual counts, and tracks
    the best combination. Writes ``optimization_results.csv`` and a
    ``best_config.yaml`` file in ``base_output_dir / 'optimization'``.
    """
    opt_cfg = config.optimization
    if not opt_cfg.enabled:
        raise ValueError("optimization.enabled must be True to run --optimize")
    if not opt_cfg.manual_counts_csv:
        raise ValueError("optimization.manual_counts_csv is required for --optimize")
    if not opt_cfg.grids:
        raise ValueError("optimization.grids is empty; specify at least one parameter grid")

    out_dir = Path(base_output_dir) / "optimization"
    out_dir.mkdir(parents=True, exist_ok=True)
    manual_csv = Path(opt_cfg.manual_counts_csv)
    metric_name = opt_cfg.metric

    # Validate grid keys
    for key in opt_cfg.grids:
        if key not in _GRID_KEY_PATHS:
            raise ValueError(
                f"Unknown grid key {key!r}. Supported keys: {sorted(_GRID_KEY_PATHS)}"
            )

    keys = list(opt_cfg.grids.keys())
    combos = list(itertools.product(*[opt_cfg.grids[k] for k in keys]))
    logger.info("Optimization grid: %d combinations across %s", len(combos), keys)

    results: List[Dict[str, float]] = []
    best_metric = float("inf")
    best_combo: Optional[Dict[str, float]] = None

    for i, vals in enumerate(combos):
        combo = dict(zip(keys, vals))
        trial_cfg = copy.deepcopy(config)
        trial_cfg.parameter_set_id = f"{config.parameter_set_id}__opt{i:04d}"
        for k, v in combo.items():
            _set_path(trial_cfg, _GRID_KEY_PATHS[k], v)
        trial_cfg.optimization.enabled = False  # avoid recursion
        trial_cfg.qc.save_overlays = False  # speed
        trial_cfg.output_dir = str(out_dir / f"trial_{i:04d}")
        try:
            trial_cfg.validate()
        except Exception as exc:
            logger.warning("Skipping invalid combo %s: %s", combo, exc)
            continue

        try:
            summary_csv = run_batch(trial_cfg)
            err = _compute_error(summary_csv, manual_csv, metric_name)
        except Exception as exc:
            logger.warning("Trial %d failed: %s", i, exc)
            err = float("inf")

        record = dict(combo)
        record["metric"] = err
        record["trial"] = i
        results.append(record)

        if err < best_metric:
            best_metric = err
            best_combo = combo
            logger.info("New best %s = %.4f at %s", metric_name, err, combo)

    res_df = pd.DataFrame(results).sort_values("metric")
    res_df.to_csv(out_dir / "optimization_results.csv", index=False)

    if best_combo is None:
        raise RuntimeError("Optimization produced no valid results")

    # Write a config with the best parameters baked in
    best_cfg = copy.deepcopy(config)
    best_cfg.parameter_set_id = f"{config.parameter_set_id}__optimized"
    for k, v in best_combo.items():
        _set_path(best_cfg, _GRID_KEY_PATHS[k], v)
    best_cfg.optimization.enabled = False
    dump_config(best_cfg, out_dir / "best_config.yaml")

    return OptimizationResult(
        best_combo=best_combo,
        best_metric=best_metric,
        metric_name=metric_name,
        all_combos=results,
    )
