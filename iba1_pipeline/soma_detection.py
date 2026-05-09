"""Soma enhancement, seed detection, and marker-controlled segmentation.

This module implements the soma-oriented detection strategy:

1. Suppress thin Iba1+ processes via morphological opening at a sub-soma scale.
2. Compute a blob-response image (DoG / LoG) at the expected soma scale.
3. Detect candidate centers as local maxima with a FIXED absolute threshold —
   never per-image adaptive — to avoid inflating false positives in
   microglia-depleted images.
4. Segment soma regions with marker-controlled watershed restricted to a soma
   mask derived from the process-suppressed image, then cap each region by
   distance from its seed to keep contours tight.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy import ndimage as ndi
from skimage import morphology
from skimage.feature import peak_local_max
from skimage.segmentation import watershed

from .config import (
    SeedDetectionConfig,
    SegmentationConfig,
    SomaEnhancementConfig,
)

logger = logging.getLogger("iba1_pipeline")


# ---------------------------------------------------------------------------
# Soma-scale enhancement
# ---------------------------------------------------------------------------


@dataclass
class EnhancementResult:
    """Outputs of the enhancement step.

    ``opened`` is the process-suppressed image (used for the soma mask).
    ``response`` is the blob-response image (used for peak detection).
    """

    opened: np.ndarray
    response: np.ndarray


def _process_suppressed(image: np.ndarray, soma_radius_px: float) -> np.ndarray:
    """Grayscale opening with a disk smaller than the soma but larger than processes.

    Choice of radius: small enough that the soma survives (radius < soma_radius),
    large enough that thin processes (~few pixels wide) are removed.
    """
    r = max(2, int(round(soma_radius_px * 0.4)))
    selem = morphology.disk(r)
    return ndi.grey_opening(image, footprint=selem).astype(np.float32)


def _dog(image: np.ndarray, sigma_inner: float, sigma_outer: float) -> np.ndarray:
    """Difference of Gaussians. Returns positive response at bright blobs."""
    g1 = ndi.gaussian_filter(image, sigma=sigma_inner)
    g2 = ndi.gaussian_filter(image, sigma=sigma_outer)
    return (g1 - g2).astype(np.float32)


def _log(image: np.ndarray, sigma: float) -> np.ndarray:
    """Scale-normalized Laplacian of Gaussian; positive at bright blobs."""
    response = -ndi.gaussian_laplace(image, sigma=sigma) * (sigma ** 2)
    return response.astype(np.float32)


def enhance_soma(image: np.ndarray, cfg: SomaEnhancementConfig) -> EnhancementResult:
    """Suppress processes and produce a soma-blob response image.

    Parameters
    ----------
    image
        Background-corrected, denoised Iba1 image (float32).
    cfg
        Soma enhancement configuration.
    """
    soma_r = float(cfg.soma_radius_px)
    sigma_inner = soma_r / np.sqrt(2.0)
    sigma_outer = sigma_inner * cfg.dog_sigma_ratio

    opened = _process_suppressed(image, soma_r)

    method = cfg.method
    if method == "tophat_dog":
        # Opening removes processes; DoG on the opened image localizes blobs.
        response = _dog(opened, sigma_inner, sigma_outer)
    elif method == "tophat":
        # Synonymous with the older naming; same path.
        response = opened
    elif method == "opening":
        response = opened
    elif method == "dog":
        response = _dog(image, sigma_inner, sigma_outer)
    elif method == "log":
        response = _log(image, sigma_inner)
    else:
        raise ValueError(f"Unknown soma_enhancement.method: {method}")

    np.clip(response, 0, None, out=response)
    return EnhancementResult(opened=opened, response=response)


# ---------------------------------------------------------------------------
# Seed detection
# ---------------------------------------------------------------------------


def detect_candidate_seeds(
    response: np.ndarray,
    roi_mask: np.ndarray,
    cfg: SeedDetectionConfig,
) -> np.ndarray:
    """Return candidate soma seed coordinates as an (N, 2) row,col array.

    Uses ``peak_local_max`` with a FIXED absolute intensity threshold. The
    threshold is intentionally not Otsu/adaptive: per-image rescaling inflates
    false positives in microglia-depleted images where true signal is sparse.
    """
    if response.shape != roi_mask.shape:
        raise ValueError("response and roi_mask shape mismatch")

    if not roi_mask.any():
        return np.empty((0, 2), dtype=int)

    coords = peak_local_max(
        response,
        min_distance=max(1, int(round(cfg.min_distance_px))),
        threshold_abs=float(cfg.min_peak_intensity),
        labels=roi_mask.astype(np.uint8),
        exclude_border=int(cfg.exclude_border_px),
    )
    return coords  # shape (N, 2): row, col


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


def _seeds_to_marker_label(coords: np.ndarray, shape: tuple) -> np.ndarray:
    """Convert seed coordinates into a label image where each seed is a unique label."""
    markers = np.zeros(shape, dtype=np.int32)
    for i, (r, c) in enumerate(coords, start=1):
        markers[int(r), int(c)] = i
    return markers


def segment_soma_candidates(
    bg_corrected: np.ndarray,
    enhancement: EnhancementResult,
    seeds: np.ndarray,
    roi_mask: np.ndarray,
    seg_cfg: SegmentationConfig,
    soma_radius_px: float,
    seed_min_peak: float,
) -> np.ndarray:
    """Marker-controlled watershed restricted to soma candidate regions.

    The mask combines:
      - intensity threshold on the bg-corrected image (so dim background is
        excluded), and
      - intensity threshold on the process-suppressed image (so thin
        processes are excluded by construction).

    Each output region is also distance-capped to ``max_soma_radius_factor *
    soma_radius_px`` so watershed cannot grow a soma blob into nearby processes.

    Returns a labeled image (int32) where 0 = background; each label
    corresponds to one seed in ``seeds`` (label index = seed_index + 1).
    """
    if seeds.size == 0:
        return np.zeros(bg_corrected.shape, dtype=np.int32)

    enhanced_thr = max(0.0, seed_min_peak * seg_cfg.enhanced_mask_fraction)
    soma_mask = (
        (bg_corrected >= seg_cfg.soma_mask_intensity)
        & (enhancement.opened >= enhanced_thr)
        & roi_mask
    )

    # Ensure each seed pixel is inside the mask so watershed actually returns
    # that label region. Without this, a seed found at a strong DoG peak that
    # happens to sit on a slightly dim pixel could be silently dropped.
    seed_mask = np.zeros_like(soma_mask)
    seed_mask[seeds[:, 0], seeds[:, 1]] = True
    soma_mask = soma_mask | seed_mask

    markers = _seeds_to_marker_label(seeds, bg_corrected.shape)

    # Watershed on the negative of the corrected image: peaks become basins.
    labels = watershed(-bg_corrected, markers=markers, mask=soma_mask, watershed_line=False)

    # Distance cap from each seed.
    inv_seed = np.logical_not(seed_mask)
    dist_to_seed = ndi.distance_transform_edt(inv_seed)
    max_radius = seg_cfg.max_soma_radius_factor * soma_radius_px
    labels[dist_to_seed > max_radius] = 0

    return labels.astype(np.int32)
