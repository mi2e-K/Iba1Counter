"""Per-ROI summary statistics and supporting metrics."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from skimage.filters import threshold_otsu

from .config import IntensityConfig


@dataclass
class ROIIntensitySummary:
    """Supporting intensity metrics for a single ROI."""

    area_px: int
    area_mm2: float
    iba1_area_fraction: float
    mean_intensity: float
    integrated_intensity: float


def measure_roi_intensity(
    bg_corrected: np.ndarray,
    raw_image: np.ndarray,
    roi_mask: np.ndarray,
    pixel_size_um: float,
    cfg: IntensityConfig,
) -> ROIIntensitySummary:
    """Compute area, area fraction, mean and integrated Iba1 intensity in the ROI.

    The area fraction is computed on the background-corrected image so it
    reflects above-background Iba1+ pixels rather than absolute brightness.
    Mean and integrated intensity are computed on the RAW image so they
    correspond to instrument intensities (consistent with what an analyst
    would read off the original file).
    """
    if roi_mask.shape != bg_corrected.shape:
        raise ValueError("ROI mask shape must match image shape")

    roi_pixels = int(roi_mask.sum())
    if roi_pixels == 0:
        return ROIIntensitySummary(
            area_px=0, area_mm2=0.0, iba1_area_fraction=0.0,
            mean_intensity=0.0, integrated_intensity=0.0,
        )

    # Convert area to mm²: (px_count * (µm/px)²) / 1e6 = mm²
    area_mm2 = roi_pixels * (pixel_size_um ** 2) / 1.0e6

    if cfg.use_otsu_for_area_fraction:
        roi_vals = bg_corrected[roi_mask]
        if roi_vals.size > 0 and np.ptp(roi_vals) > 0:
            try:
                thr = float(threshold_otsu(roi_vals))
            except Exception:
                thr = float(cfg.area_fraction_threshold)
        else:
            thr = float(cfg.area_fraction_threshold)
    else:
        thr = float(cfg.area_fraction_threshold)

    above = (bg_corrected >= thr) & roi_mask
    area_fraction = float(above.sum()) / float(roi_pixels)

    raw_in_roi = raw_image.astype(np.float64)[roi_mask]
    mean_intensity = float(raw_in_roi.mean())
    integrated_intensity = float(raw_in_roi.sum())

    return ROIIntensitySummary(
        area_px=roi_pixels,
        area_mm2=area_mm2,
        iba1_area_fraction=area_fraction,
        mean_intensity=mean_intensity,
        integrated_intensity=integrated_intensity,
    )
