"""Object filtering for soma candidates.

Filters are interpretable and biologically conservative:

* Area (in µm²) and intensity are PRIMARY filters.
* Circularity and solidity are weak auxiliary filters used only to remove
  obvious artifacts. Disabled by default because real Iba1+ soma can be
  irregular when partially cropped or surrounded by dense processes.
* Edge-touching and outside-ROI objects can be excluded or flagged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
from skimage import measure

from .config import ObjectFilterConfig

logger = logging.getLogger("iba1_pipeline")


@dataclass
class CandidateObject:
    """A measured candidate, before accept/reject decisions are applied."""

    object_id: int
    seed_index: int  # index into the seed array, used for traceability
    centroid_rc: tuple  # (row, col) in pixels
    area_px: int
    area_um2: float
    mean_intensity: float
    peak_intensity: float
    integrated_intensity: float
    circularity: Optional[float]
    solidity: Optional[float]
    touches_edge: bool
    inside_roi: bool
    accepted: bool = False
    rejection_reason: str = ""


def _circularity(area: float, perimeter: float) -> Optional[float]:
    """4πA/P². Returns None if perimeter is zero."""
    if perimeter <= 0:
        return None
    return float(4 * np.pi * area / (perimeter ** 2))


def _solidity(filled_area: float, convex_area: float) -> Optional[float]:
    if convex_area <= 0:
        return None
    return float(filled_area / convex_area)


def _touches_edge(bbox: tuple, shape: tuple, margin: int) -> bool:
    minr, minc, maxr, maxc = bbox
    h, w = shape
    return (minr <= margin) or (minc <= margin) or (maxr >= h - margin) or (maxc >= w - margin)


def measure_objects(
    labels: np.ndarray,
    intensity_image: np.ndarray,
    roi_mask: np.ndarray,
    pixel_size_um: float,
    edge_margin_px: int,
) -> List[CandidateObject]:
    """Measure all labelled regions in ``labels``. Pure measurement, no filtering yet."""
    if labels.max() == 0:
        return []

    props = measure.regionprops(labels, intensity_image=intensity_image)
    objects: List[CandidateObject] = []
    px_um2 = float(pixel_size_um) ** 2

    for p in props:
        area_px = int(p.area)
        circularity = _circularity(p.area, p.perimeter)
        try:
            solidity = float(p.solidity)
        except Exception:
            solidity = None
        # Determine if centroid sits inside the ROI mask
        cr, cc = p.centroid
        inside = bool(roi_mask[int(round(cr)), int(round(cc))]) if roi_mask is not None else True
        peak = float(np.max(p.intensity_image[p.image]))

        objects.append(
            CandidateObject(
                object_id=int(p.label),
                seed_index=int(p.label) - 1,
                centroid_rc=(float(cr), float(cc)),
                area_px=area_px,
                area_um2=area_px * px_um2,
                mean_intensity=float(p.mean_intensity),
                peak_intensity=peak,
                integrated_intensity=float(p.mean_intensity * area_px),
                circularity=circularity,
                solidity=solidity,
                touches_edge=_touches_edge(p.bbox, labels.shape, edge_margin_px),
                inside_roi=inside,
            )
        )
    return objects


def filter_objects(
    candidates: List[CandidateObject],
    cfg: ObjectFilterConfig,
) -> List[CandidateObject]:
    """Apply filters and tag each candidate as accepted or rejected (in place).

    Returns the same list with the ``accepted`` and ``rejection_reason`` fields
    populated. Use ``[c for c in result if c.accepted]`` to obtain the accepted
    set; the full list with rejection reasons is suitable for QC output.
    """
    for c in candidates:
        reasons = []
        if not c.inside_roi:
            reasons.append("outside_roi")
        if cfg.exclude_edge_objects and c.touches_edge:
            reasons.append("edge")
        if c.area_um2 < cfg.min_area_um2:
            reasons.append(f"area<{cfg.min_area_um2:.1f}")
        if c.area_um2 > cfg.max_area_um2:
            reasons.append(f"area>{cfg.max_area_um2:.1f}")
        if c.mean_intensity < cfg.min_mean_intensity:
            reasons.append(f"mean<{cfg.min_mean_intensity:.1f}")
        if c.peak_intensity < cfg.min_peak_intensity:
            reasons.append(f"peak<{cfg.min_peak_intensity:.1f}")
        if cfg.min_circularity is not None and c.circularity is not None:
            if c.circularity < cfg.min_circularity:
                reasons.append(f"circ<{cfg.min_circularity:.2f}")
        if cfg.min_solidity is not None and c.solidity is not None:
            if c.solidity < cfg.min_solidity:
                reasons.append(f"sol<{cfg.min_solidity:.2f}")

        c.accepted = not reasons
        c.rejection_reason = ";".join(reasons)
    return candidates
