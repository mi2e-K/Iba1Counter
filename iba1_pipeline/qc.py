"""QC overlay generation.

Produces one annotated image per (image, ROI) showing:
* contrast-stretched Iba1 channel,
* ROI boundary,
* accepted detections (filled contours + center markers),
* optionally rejected candidates in a different style,
* image name, count annotation, and a scale bar when pixel size is known.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

import matplotlib

# Use a non-interactive backend so the pipeline runs headless on servers/CI.
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Circle, Polygon as MplPolygon, Rectangle
from skimage import measure

from .config import QCConfig
from .filtering import CandidateObject

logger = logging.getLogger("iba1_pipeline")


def _stretch_for_display(image: np.ndarray, pct: tuple = (1, 99.5)) -> np.ndarray:
    lo, hi = np.percentile(image, pct)
    if hi <= lo:
        return np.zeros_like(image, dtype=np.float32)
    img = (image.astype(np.float32) - lo) / (hi - lo)
    return np.clip(img, 0, 1)


def _roi_outline(mask: np.ndarray) -> List[np.ndarray]:
    """Return list of (N, 2) arrays of (x, y) outline coords for the ROI mask."""
    contours = measure.find_contours(mask.astype(float), 0.5)
    return [np.column_stack([c[:, 1], c[:, 0]]) for c in contours]


def _object_outlines(labels: np.ndarray, accepted_ids: set) -> List[np.ndarray]:
    """Return list of (N, 2) arrays of (x, y) outline coords for accepted labels."""
    if labels.max() == 0:
        return []
    bin_mask = np.isin(labels, list(accepted_ids))
    if not bin_mask.any():
        return []
    contours = measure.find_contours(bin_mask.astype(float), 0.5)
    return [np.column_stack([c[:, 1], c[:, 0]]) for c in contours]


def _add_scale_bar(ax, pixel_size_um: Optional[float], image_width_px: int) -> None:
    if not pixel_size_um or pixel_size_um <= 0:
        return
    # Pick a "nice" length covering ~15% of image width
    target_um = pixel_size_um * image_width_px * 0.15
    nice = [10, 20, 25, 50, 100, 200, 250, 500, 1000]
    bar_um = min(nice, key=lambda x: abs(x - target_um))
    bar_px = bar_um / pixel_size_um
    pad = 20
    rect = Rectangle(
        (image_width_px - bar_px - pad, image_width_px * 0 + pad),
        width=bar_px,
        height=4,
        linewidth=0,
        facecolor="white",
    )
    ax.add_patch(rect)
    ax.text(
        image_width_px - bar_px / 2 - pad,
        pad + 18,
        f"{bar_um:g} µm",
        color="white",
        ha="center",
        va="top",
        fontsize=8,
    )


def generate_qc_overlay(
    image: np.ndarray,
    roi_mask: np.ndarray,
    labels: np.ndarray,
    candidates: List[CandidateObject],
    image_name: str,
    roi_name: str,
    accepted_count: int,
    cfg: QCConfig,
    output_path: Path,
    pixel_size_um: Optional[float] = None,
) -> None:
    """Render and save the QC overlay PNG/TIFF for a single (image, ROI)."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    h, w = image.shape
    fig, ax = plt.subplots(figsize=(w / 200.0, h / 200.0), dpi=cfg.figure_dpi)
    ax.imshow(_stretch_for_display(image), cmap="gray", interpolation="nearest")

    # ROI outline
    for pts in _roi_outline(roi_mask):
        ax.plot(pts[:, 0], pts[:, 1], color=cfg.roi_color, linewidth=1.0)

    # Accepted contours
    accepted_ids = {c.object_id for c in candidates if c.accepted}
    for pts in _object_outlines(labels, accepted_ids):
        ax.plot(pts[:, 0], pts[:, 1], color=cfg.contour_color, linewidth=0.8)

    # Accepted centers
    accepted = [c for c in candidates if c.accepted]
    if accepted:
        ys = [c.centroid_rc[0] for c in accepted]
        xs = [c.centroid_rc[1] for c in accepted]
        ax.plot(xs, ys, "o", markersize=2.5, markeredgecolor=cfg.contour_color,
                markerfacecolor="none", linewidth=0.5)
        if cfg.show_ids:
            for c in accepted:
                ax.text(c.centroid_rc[1], c.centroid_rc[0], str(c.object_id),
                        color=cfg.contour_color, fontsize=5,
                        ha="center", va="center")

    # Rejected (X markers)
    if cfg.show_rejected:
        rejected = [c for c in candidates if not c.accepted]
        if rejected:
            ys = [c.centroid_rc[0] for c in rejected]
            xs = [c.centroid_rc[1] for c in rejected]
            ax.plot(xs, ys, "x", markersize=3, color=cfg.rejected_color,
                    markeredgewidth=0.6)

    _add_scale_bar(ax, pixel_size_um, w)

    title = f"{image_name} | ROI: {roi_name} | n = {accepted_count}"
    ax.set_title(title, fontsize=8, color="black")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_xlim(0, w)
    ax.set_ylim(h, 0)
    fig.tight_layout(pad=0.5)

    fmt = cfg.overlay_format.lower().lstrip(".")
    if fmt == "tiff":
        fmt = "tif"
    save_path = output_path.with_suffix(f".{fmt}")
    fig.savefig(save_path, dpi=cfg.figure_dpi, bbox_inches="tight")
    plt.close(fig)


def generate_qc_clean_overlay(
    corrected: np.ndarray,
    roi_mask: np.ndarray,
    candidates: List[CandidateObject],
    image_name: str,
    roi_name: str,
    accepted_count: int,
    cfg: QCConfig,
    output_path: Path,
) -> None:
    """Render a minimalist QC overlay for visual verification.

    Differs from ``generate_qc_overlay``:
    * Background is the BG-corrected GREEN channel, displayed with a
      black->green LUT so it looks like the original fluorescence channel
      (not grayscale).
    * Accepted detections are marked as semi-transparent grey filled circles
      sized 1.5x the detected region's equivalent radius (with a floor) so
      they're clearly visible at typical viewing zoom.
    * Rejected candidates are NOT shown.
    * No scale bar.
    * ROI outline is drawn in the same yellow as the main overlay.
    """
    from matplotlib.colors import LinearSegmentedColormap

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Black -> bright green LUT mimicking ImageJ's "Green" channel display.
    green_cmap = LinearSegmentedColormap.from_list(
        "iba1_green", [(0.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
    )

    h, w = corrected.shape
    fig, ax = plt.subplots(figsize=(w / 200.0, h / 200.0), dpi=cfg.figure_dpi)
    ax.imshow(_stretch_for_display(corrected), cmap=green_cmap,
              interpolation="nearest")

    # ROI outline only (no scale bar / rejected markers / contours)
    for pts in _roi_outline(roi_mask):
        ax.plot(pts[:, 0], pts[:, 1], color=cfg.roi_color, linewidth=0.7, alpha=0.7)

    # Accepted detections as semi-transparent grey circles. Sized 1.5x the
    # measured equivalent radius to remain visible after matplotlib's display
    # downscale; floored at ~5 px so even the smallest detections are seen.
    for c in candidates:
        if not c.accepted:
            continue
        if c.area_px > 0:
            r = float(np.sqrt(c.area_px / np.pi)) * 1.5
        else:
            r = 5.0  # manual additions have no measured area
        r = max(5.0, r)
        ax.add_patch(Circle(
            (c.centroid_rc[1], c.centroid_rc[0]),
            radius=r,
            facecolor="none",   # outline only -- underlying soma stays visible
            edgecolor="0.85",   # light grey, readable on the green LUT
            linewidth=0.8,
            alpha=0.7,
        ))

    # The clean overlay does not show the ROI id in the title; the filename
    # also drops the ROI suffix (see batch.py). Keeps the visual minimal.
    ax.set_title(f"{image_name} | n = {accepted_count}",
                 fontsize=8, color="black")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_xlim(0, w)
    ax.set_ylim(h, 0)
    fig.tight_layout(pad=0.5)

    fmt = cfg.overlay_format.lower().lstrip(".")
    if fmt == "tiff":
        fmt = "tif"
    save_path = output_path.with_suffix(f".{fmt}")
    fig.savefig(save_path, dpi=cfg.figure_dpi, bbox_inches="tight")
    plt.close(fig)
