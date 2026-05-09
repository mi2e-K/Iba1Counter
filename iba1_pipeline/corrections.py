"""Manual correction support.

Reviewers may add or remove cell coordinates after inspecting QC overlays.
The correction CSV format is documented in ``examples/corrections_template.csv``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from .filtering import CandidateObject

logger = logging.getLogger("iba1_pipeline")


@dataclass
class Correction:
    image_id: str
    roi_id: str
    action: str  # 'add' or 'remove'
    x: float  # pixels
    y: float  # pixels
    reason: str = ""
    reviewer: str = ""
    blinded_condition: str = ""


def load_corrections(path: Optional[str]) -> List[Correction]:
    """Load and validate a correction CSV. Returns an empty list if path is None."""
    if path is None:
        return []
    df = pd.read_csv(path)
    required = {"image_id", "roi_id", "action", "x", "y"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Correction CSV missing required columns: {sorted(missing)}")

    cors: List[Correction] = []
    for _, row in df.iterrows():
        action = str(row["action"]).strip().lower()
        if action not in {"add", "remove"}:
            logger.warning("Skipping correction with unknown action %r (image_id=%s)",
                           row["action"], row["image_id"])
            continue
        cors.append(Correction(
            image_id=str(row["image_id"]),
            roi_id=str(row["roi_id"]),
            action=action,
            x=float(row["x"]),
            y=float(row["y"]),
            reason=str(row.get("reason", "")) if not pd.isna(row.get("reason", "")) else "",
            reviewer=str(row.get("reviewer", "")) if not pd.isna(row.get("reviewer", "")) else "",
            blinded_condition=str(row.get("blinded_condition", ""))
                if not pd.isna(row.get("blinded_condition", "")) else "",
        ))
    return cors


def apply_corrections(
    image_id: str,
    roi_id: str,
    accepted: List[CandidateObject],
    corrections: List[Correction],
    radius_for_remove_px: float,
) -> Tuple[List[CandidateObject], int]:
    """Apply add/remove corrections, returning a corrected accepted list.

    'remove': drop the closest accepted detection within ``radius_for_remove_px``.
    'add': insert a synthetic accepted CandidateObject at the given coordinates.

    Synthetic adds carry a negative ``object_id`` and area=0 so downstream
    code can recognise them as manual additions in the per-object CSV.
    """
    relevant = [c for c in corrections if c.image_id == image_id and c.roi_id == roi_id]
    if not relevant:
        return list(accepted), 0

    corrected = list(accepted)
    n_changes = 0

    for cor in relevant:
        if cor.action == "remove" and corrected:
            xs = np.array([c.centroid_rc[1] for c in corrected])
            ys = np.array([c.centroid_rc[0] for c in corrected])
            dists = np.hypot(xs - cor.x, ys - cor.y)
            idx = int(np.argmin(dists))
            if dists[idx] <= radius_for_remove_px:
                logger.debug("Removing detection at (%.1f, %.1f) for %s/%s",
                             corrected[idx].centroid_rc[1], corrected[idx].centroid_rc[0],
                             image_id, roi_id)
                corrected.pop(idx)
                n_changes += 1
            else:
                logger.warning(
                    "Remove correction at (%.1f, %.1f) for %s/%s found no detection within %.1fpx",
                    cor.x, cor.y, image_id, roi_id, radius_for_remove_px,
                )
        elif cor.action == "add":
            synthetic_id = -(len([c for c in corrected if c.object_id < 0]) + 1)
            corrected.append(CandidateObject(
                object_id=synthetic_id,
                seed_index=-1,
                centroid_rc=(float(cor.y), float(cor.x)),
                area_px=0,
                area_um2=0.0,
                mean_intensity=0.0,
                peak_intensity=0.0,
                integrated_intensity=0.0,
                circularity=None,
                solidity=None,
                touches_edge=False,
                inside_roi=True,
                accepted=True,
                rejection_reason="manual_add",
            ))
            n_changes += 1

    return corrected, n_changes
