"""Background correction and denoising.

The background-corrected image is the canonical representation passed to
intensity measurements and to the soma-enhancement step. Operations are
performed in float32 to preserve precision while limiting memory.

When ``background.method == 'external'``, the batch layer loads an already
background-subtracted image from ``background.external_dir`` and this module
only applies the denoising step.
"""

from __future__ import annotations

import logging

import numpy as np
from scipy import ndimage as ndi
from skimage import filters, morphology, restoration

from .config import BackgroundConfig, DenoisingConfig

logger = logging.getLogger("iba1_pipeline")


def to_float32(image: np.ndarray) -> np.ndarray:
    """Cast to float32 without rescaling so original intensities are preserved."""
    return image.astype(np.float32, copy=False)


def estimate_background(image: np.ndarray, cfg: BackgroundConfig) -> np.ndarray:
    """Return an estimate of the slowly varying background for ``image``."""
    img = to_float32(image)
    radius = float(cfg.radius_px)
    method = cfg.method

    if method == "rolling_ball":
        # scikit-image's rolling_ball is robust but slow on large images; use
        # a downsample-then-upsample trick is unnecessary at typical sizes.
        bg = restoration.rolling_ball(img, radius=radius)
        return bg.astype(np.float32)

    if method == "morph_opening":
        # Grayscale opening with a large disk approximates a smooth background.
        selem = morphology.disk(int(round(radius)))
        return ndi.grey_opening(img, footprint=selem).astype(np.float32)

    if method == "gaussian":
        # Heavily smoothed copy as background. sigma chosen so support ~ radius.
        sigma = radius / 2.0
        return ndi.gaussian_filter(img, sigma=sigma).astype(np.float32)

    if method == "none":
        return np.zeros_like(img, dtype=np.float32)

    if method == "external":
        return np.zeros_like(img, dtype=np.float32)

    raise ValueError(f"Unknown background method: {method}")


def correct_background(image: np.ndarray, cfg: BackgroundConfig) -> np.ndarray:
    """Subtract the estimated background and clip negatives to zero."""
    img = to_float32(image)
    if cfg.method in {"none", "external"}:
        return img.copy()
    bg = estimate_background(img, cfg)
    corrected = img - bg
    np.clip(corrected, 0, None, out=corrected)
    return corrected


def denoise_image(image: np.ndarray, cfg: DenoisingConfig) -> np.ndarray:
    """Apply mild denoising. Aggressive smoothing is intentionally avoided."""
    img = to_float32(image)
    if cfg.method == "median":
        size = max(1, int(cfg.median_size_px))
        if size <= 1:
            return img.copy()
        # Footprint must be odd for median consistency
        if size % 2 == 0:
            size += 1
        return ndi.median_filter(img, size=size).astype(np.float32)
    if cfg.method == "gaussian":
        sigma = max(0.0, float(cfg.gaussian_sigma_px))
        if sigma == 0:
            return img.copy()
        return ndi.gaussian_filter(img, sigma=sigma).astype(np.float32)
    if cfg.method == "none":
        return img.copy()
    raise ValueError(f"Unknown denoising method: {cfg.method}")


def preprocess(image: np.ndarray, bg_cfg: BackgroundConfig,
               denoise_cfg: DenoisingConfig) -> np.ndarray:
    """Full preprocessing chain: bg correction then denoising."""
    corrected = correct_background(image, bg_cfg)
    corrected = denoise_image(corrected, denoise_cfg)
    return corrected
