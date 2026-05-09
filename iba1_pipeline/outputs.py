"""CSV outputs and parameter logging.

Three CSVs are produced per batch:

* ``image_summary.csv``: one row per (image, ROI), with count, density, and
  supporting metrics.
* ``per_object.csv``: one row per detected candidate (accepted or rejected).
* ``parameters.yaml``: a frozen copy of the config used, plus package
  versions and a timestamp.
"""

from __future__ import annotations

import datetime as dt
import importlib
import logging
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import yaml

from .config import Config
from .filtering import CandidateObject

logger = logging.getLogger("iba1_pipeline")


@dataclass
class ImageROIResult:
    """Per-(image, ROI) record produced by the pipeline."""

    image_id: str
    image_path: str
    roi_id: str
    group: Optional[str]
    parameter_set_id: str
    pixel_size_um: float
    pixel_size_source: str
    count: int
    count_corrected: Optional[int]
    roi_area_mm2: float
    density_cells_per_mm2: float
    density_corrected: Optional[float]
    iba1_area_fraction: float
    roi_mean_intensity: float
    roi_integrated_intensity: float
    n_rejected: int
    qc_status: str = "not_reviewed"


# ---------------------------------------------------------------------------


def _package_versions() -> Dict[str, str]:
    versions: Dict[str, str] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    for pkg in [
        "numpy", "pandas", "scipy", "skimage", "tifffile",
        "imageio", "matplotlib", "roifile", "yaml",
    ]:
        try:
            mod = importlib.import_module(pkg)
            versions[pkg] = getattr(mod, "__version__", "unknown")
        except Exception:
            versions[pkg] = "not_installed"
    return versions


def write_parameter_log(config: Config, output_dir: Path) -> Path:
    """Write a frozen parameter log including package versions and timestamp."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "parameter_set_id": config.parameter_set_id,
        "package_versions": _package_versions(),
        "config": config.to_dict(),
    }
    out = output_dir / "parameters.yaml"
    with out.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, sort_keys=False)
    return out


def write_image_summary_csv(
    results: List[ImageROIResult],
    output_dir: Path,
    filename: str = "image_summary.csv",
) -> Path:
    """Write the per-image, per-ROI summary CSV."""
    rows = [r.__dict__ for r in results]
    df = pd.DataFrame(rows, columns=[
        "image_id", "image_path", "roi_id", "group", "parameter_set_id",
        "pixel_size_um", "pixel_size_source",
        "count", "count_corrected",
        "roi_area_mm2",
        "density_cells_per_mm2", "density_corrected",
        "iba1_area_fraction", "roi_mean_intensity", "roi_integrated_intensity",
        "n_rejected", "qc_status",
    ])
    out = Path(output_dir) / filename
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    return out


def write_per_object_csv(
    image_id: str,
    roi_id: str,
    candidates: List[CandidateObject],
    pixel_size_um: float,
    output_path: Path,
    save_rejected: bool,
) -> None:
    """Append per-object rows for one (image, ROI) to a CSV.

    The file is written incrementally so very long batches don't have to keep
    everything in memory at once.
    """
    if not candidates:
        return
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for c in candidates:
        if not save_rejected and not c.accepted:
            continue
        rows.append({
            "image_id": image_id,
            "roi_id": roi_id,
            "object_id": c.object_id,
            "x_centroid_px": c.centroid_rc[1],
            "y_centroid_px": c.centroid_rc[0],
            "x_centroid_um": c.centroid_rc[1] * pixel_size_um,
            "y_centroid_um": c.centroid_rc[0] * pixel_size_um,
            "area_px": c.area_px,
            "area_um2": c.area_um2,
            "mean_intensity": c.mean_intensity,
            "peak_intensity": c.peak_intensity,
            "integrated_intensity": c.integrated_intensity,
            "circularity": c.circularity,
            "solidity": c.solidity,
            "touches_edge": c.touches_edge,
            "inside_roi": c.inside_roi,
            "accepted_or_rejected": "accepted" if c.accepted else "rejected",
            "rejection_reason": c.rejection_reason,
        })
    df = pd.DataFrame(rows)
    header = not output_path.exists()
    df.to_csv(output_path, mode="a", header=header, index=False)
