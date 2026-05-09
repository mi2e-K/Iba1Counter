"""Image loading, channel extraction, ROI loading, and pixel-size handling."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import tifffile
from skimage.draw import polygon as skpolygon

try:
    import roifile  # Reads Fiji/ImageJ .roi and .zip files
except Exception:  # pragma: no cover - import-time guard
    roifile = None  # type: ignore

from .config import ChannelConfig, PixelSizeConfig, ROIConfig

logger = logging.getLogger("iba1_pipeline")


# ---------------------------------------------------------------------------
# Image loading
# ---------------------------------------------------------------------------


@dataclass
class LoadedImage:
    """Container for a loaded image and its calibration info."""

    image: np.ndarray  # 2D array, the Iba1 channel; native dtype preserved
    pixel_size_um: float
    pixel_size_source: str  # 'config' | 'tiff_metadata' | 'fallback'
    path: Path


def _select_channel_axis(arr: np.ndarray) -> Tuple[int, int]:
    """Return (axis, n_channels) for a probable channel axis in a 3-D array.

    Heuristic: pick the smallest axis as the channel axis if the image is 3-D.
    Microscopy convention is YXC (channel last); TIFFs from many devices are
    CYX (channel first). We pick whichever has the smaller length.
    """
    if arr.ndim != 3:
        raise ValueError(f"Expected 3-D array for channel selection, got shape {arr.shape}")
    smallest = int(np.argmin(arr.shape))
    return smallest, arr.shape[smallest]


def extract_iba1_channel(arr: np.ndarray, channel_cfg: ChannelConfig) -> np.ndarray:
    """Extract the Iba1 channel as a 2-D array.

    Parameters
    ----------
    arr
        Raw image array. Accepts 2-D (single channel) or 3-D (multi-channel).
    channel_cfg
        Channel configuration controlling RGB/multi/single/auto behavior.
    """
    if arr.ndim == 2:
        if channel_cfg.mode in {"rgb", "multi"}:
            logger.warning("Image is 2-D; ignoring channel.mode=%s", channel_cfg.mode)
        return arr

    if arr.ndim != 3:
        raise ValueError(f"Unsupported image dimensionality: shape={arr.shape}")

    mode = channel_cfg.mode
    if mode == "auto":
        # Detect: if smallest axis has length 3 or 4, treat as RGB(A); else as multi.
        axis, n_channels = _select_channel_axis(arr)
        if n_channels in {3, 4}:
            mode = "rgb"
        else:
            mode = "multi"
        logger.info("channel.mode=auto resolved to %s (axis=%d, n=%d)", mode, axis, n_channels)

    axis, n_channels = _select_channel_axis(arr)

    if mode == "rgb":
        if channel_cfg.index >= n_channels:
            raise ValueError(
                f"channel.index={channel_cfg.index} out of range for RGB image with "
                f"{n_channels} channels"
            )
        return np.take(arr, channel_cfg.index, axis=axis)

    if mode == "multi":
        if channel_cfg.index >= n_channels:
            raise ValueError(
                f"channel.index={channel_cfg.index} out of range for multi-channel image "
                f"with {n_channels} channels"
            )
        return np.take(arr, channel_cfg.index, axis=axis)

    if mode == "single":
        # Reduce to first channel
        return np.take(arr, 0, axis=axis)

    raise ValueError(f"Unsupported channel.mode={mode!r}")


def _read_pixel_size_from_tiff(path: Path) -> Optional[float]:
    """Try to recover pixel size in micrometres from TIFF metadata.

    Order of preference:
    1. ImageJ metadata (``unit`` + ``spacing`` / X resolution)
    2. Standard TIFF ``XResolution`` + ``ResolutionUnit``
    Returns ``None`` if no usable metadata is found.
    """
    try:
        with tifffile.TiffFile(path) as tif:
            if tif.is_imagej:
                meta = tif.imagej_metadata or {}
                page = tif.pages[0]
                tags = page.tags
                xres = tags.get("XResolution", None)
                unit = meta.get("unit") or meta.get("Unit")
                if xres is not None and unit:
                    num, denom = xres.value
                    if num and denom:
                        per_unit = denom / num  # units per pixel
                        return _to_micrometres(per_unit, unit)
            # Standard TIFF tags
            page = tif.pages[0]
            tags = page.tags
            xres = tags.get("XResolution", None)
            res_unit = tags.get("ResolutionUnit", None)
            if xres is not None and res_unit is not None:
                num, denom = xres.value
                if num and denom:
                    per_unit_pixel = denom / num  # units per pixel (inverse of px/unit)
                    unit_value = res_unit.value
                    # 2 = inch, 3 = cm
                    if unit_value == 2:
                        return per_unit_pixel * 25400.0  # inch -> µm
                    if unit_value == 3:
                        return per_unit_pixel * 10000.0  # cm -> µm
    except Exception as exc:  # pragma: no cover - best effort
        logger.debug("Could not read pixel size from %s: %s", path, exc)
    return None


def _to_micrometres(value: float, unit: str) -> float:
    """Convert a length in the given unit to micrometres."""
    unit = unit.strip().lower().replace("μ", "u")
    if unit in {"um", "micron", "microns", "micrometer", "micrometre", "u"}:
        return float(value)
    if unit in {"nm", "nanometer", "nanometre"}:
        return value / 1000.0
    if unit in {"mm", "millimeter", "millimetre"}:
        return value * 1000.0
    if unit in {"cm", "centimeter", "centimetre"}:
        return value * 10000.0
    if unit in {"inch", "in"}:
        return value * 25400.0
    logger.warning("Unrecognised pixel-size unit %r; assuming micrometres.", unit)
    return float(value)


def load_image(path: Path, channel_cfg: ChannelConfig,
               pixel_cfg: PixelSizeConfig) -> LoadedImage:
    """Load an image and return the Iba1 channel plus pixel calibration."""
    path = Path(path)
    if path.suffix.lower() in {".tif", ".tiff"}:
        raw = tifffile.imread(path)
    else:
        # Fallback for PNG/JPG; lazy import to keep tifffile primary.
        import imageio.v3 as iio
        raw = iio.imread(path)

    iba1 = extract_iba1_channel(raw, channel_cfg)

    # Pixel size resolution
    if pixel_cfg.um_per_px is not None:
        size = pixel_cfg.um_per_px
        source = "config"
    else:
        size = _read_pixel_size_from_tiff(path) if path.suffix.lower() in {".tif", ".tiff"} else None
        if size is None:
            if pixel_cfg.require_metadata:
                raise ValueError(
                    f"Pixel size missing from metadata for {path.name} and require_metadata=True"
                )
            logger.warning(
                "No pixel size found for %s; falling back to 1.0 um/px (results in µm² will be "
                "wrong unless this is corrected via config).", path.name
            )
            size = 1.0
            source = "fallback"
        else:
            source = "tiff_metadata"

    return LoadedImage(image=iba1, pixel_size_um=size, pixel_size_source=source, path=path)


# ---------------------------------------------------------------------------
# ROI loading
# ---------------------------------------------------------------------------


@dataclass
class ROI:
    """A single ROI as a binary mask plus its name."""

    name: str
    mask: np.ndarray  # bool, same shape as image
    polygon_xy: Optional[np.ndarray] = None  # shape (N, 2): x,y in pixels


def _polygon_to_mask(coords_xy: np.ndarray, shape: Tuple[int, int]) -> np.ndarray:
    """Rasterize an XY polygon to a boolean mask of the given image shape (rows, cols)."""
    rr, cc = skpolygon(coords_xy[:, 1], coords_xy[:, 0], shape=shape)
    mask = np.zeros(shape, dtype=bool)
    mask[rr, cc] = True
    return mask


def _roifile_obj_to_polygon(roi: "roifile.ImagejRoi") -> Optional[np.ndarray]:
    """Extract pixel-space (x, y) polygon coords from a roifile ROI object."""
    coords = roi.coordinates() if hasattr(roi, "coordinates") else None
    if coords is not None and len(coords) > 0:
        return np.asarray(coords, dtype=float)
    # Rectangular ROI fallback
    try:
        x, y = roi.left, roi.top
        w, h = roi.width, roi.height
        return np.asarray([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=float)
    except Exception:
        return None


def load_rois_for_image(image_path: Path, image_shape: Tuple[int, int],
                        roi_cfg: ROIConfig) -> List[ROI]:
    """Find and load ROIs matching ``image_path``.

    Lookup order:
    1. ``<roi_dir>/<image_stem><suffix>`` (.zip preferred, then .roi)
    2. If none found and ``fallback_whole_image=True``, return a whole-image ROI.
    """
    rois: List[ROI] = []
    if roi_cfg.directory:
        roi_dir = Path(roi_cfg.directory)
        stem = image_path.stem
        candidates = [roi_dir / f"{stem}.zip", roi_dir / f"{stem}.roi"]
        # Also support image-name-with-suffix
        for c in list(candidates):
            candidates.append(roi_dir / f"{stem}{roi_cfg.suffix}")
        match = next((c for c in candidates if c.exists()), None)
        if match is not None:
            if roifile is None:
                raise RuntimeError(
                    "roifile is not installed but ROI files were provided. "
                    "Install with `pip install roifile`."
                )
            try:
                roi_objs = roifile.ImagejRoi.fromfile(match)
            except Exception as exc:
                raise RuntimeError(f"Failed to read ROI file {match}: {exc}")
            if not isinstance(roi_objs, list):
                roi_objs = [roi_objs]
            for i, ro in enumerate(roi_objs):
                poly = _roifile_obj_to_polygon(ro)
                if poly is None or len(poly) < 3:
                    logger.warning("Skipping non-polygon ROI #%d in %s", i, match.name)
                    continue
                mask = _polygon_to_mask(poly, image_shape)
                if mask.sum() == 0:
                    logger.warning("ROI #%d in %s rasterizes to empty mask", i, match.name)
                    continue
                name = getattr(ro, "name", None) or f"ROI_{i + 1}"
                rois.append(ROI(name=name, mask=mask, polygon_xy=poly))
            if rois:
                return rois
            logger.warning("ROI file %s loaded but produced no valid ROIs.", match.name)

    if roi_cfg.fallback_whole_image:
        logger.info("Using whole-image ROI fallback for %s", image_path.name)
        mask = np.ones(image_shape, dtype=bool)
        return [ROI(name="whole_image", mask=mask, polygon_xy=None)]

    raise FileNotFoundError(f"No ROI file found for image {image_path.name}")


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def find_images(input_dir: Path, file_glob: str) -> List[Path]:
    """Return sorted list of image paths matching ``file_glob`` in ``input_dir``."""
    input_dir = Path(input_dir)
    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")
    images = sorted(input_dir.glob(file_glob))
    if not images:
        logger.warning("No images matched %s in %s", file_glob, input_dir)
    return images


def load_metadata(path: Optional[str]) -> Dict[str, Dict[str, str]]:
    """Optional CSV with at least ``image_id`` column. Returns dict by image_id."""
    if path is None:
        return {}
    import pandas as pd
    df = pd.read_csv(path)
    if "image_id" not in df.columns:
        raise ValueError(f"Metadata CSV {path} must contain an 'image_id' column")
    df = df.set_index("image_id")
    return {idx: row.to_dict() for idx, row in df.iterrows()}
