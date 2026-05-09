"""Batch orchestration for the Iba1 microglia pipeline.

This module wires together the per-step modules and runs the full pipeline
across a directory of images.
"""

from __future__ import annotations

import logging
import random
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from .config import Config, PixelSizeConfig
from .corrections import apply_corrections, load_corrections
from .filtering import filter_objects, measure_objects
from .io_utils import find_images, load_image, load_metadata, load_rois_for_image
from .measurements import measure_roi_intensity
from .outputs import (
    ImageROIResult,
    write_image_summary_csv,
    write_parameter_log,
    write_per_object_csv,
)
from .preprocessing import denoise_image, preprocess
from .qc import generate_qc_clean_overlay, generate_qc_overlay
from .soma_detection import (
    detect_candidate_seeds,
    enhance_soma,
    segment_soma_candidates,
)

logger = logging.getLogger("iba1_pipeline")

_IMAGE_EXTENSIONS = {".tif", ".tiff", ".png", ".jpg", ".jpeg"}


def _set_seed(seed: int) -> None:
    """Fix random seeds for any stochastic dependencies."""
    random.seed(seed)
    np.random.seed(seed)


def _find_external_background_image(image_path: Path, external_dir: str) -> Path:
    """Find a pre-background-subtracted image matching ``image_path`` by name/stem."""
    directory = Path(external_dir)
    if not directory.exists():
        raise FileNotFoundError(f"background.external_dir does not exist: {directory}")

    direct = directory / image_path.name
    if direct.exists():
        return direct

    candidates = sorted(
        p for p in directory.iterdir()
        if p.is_file()
        and p.stem == image_path.stem
        and p.suffix.lower() in _IMAGE_EXTENSIONS
    )
    if not candidates:
        raise FileNotFoundError(
            f"No external background-corrected image found for {image_path.name} "
            f"in {directory}. Expected same filename or same stem."
        )
    if len(candidates) > 1:
        logger.warning(
            "Multiple external background images matched %s in %s; using %s",
            image_path.stem, directory, candidates[0].name,
        )
    return candidates[0]


def _prepare_detection_image(raw: np.ndarray, image_path: Path, loaded, config: Config) -> np.ndarray:
    """Return the image used for detection after background correction + denoising.

    Normal modes estimate the background in Python. ``background.method='external'``
    loads an already background-subtracted image from ``background.external_dir``
    and only applies the configured denoising step.
    """
    if config.background.method != "external":
        return preprocess(raw, config.background, config.denoising)

    external_path = _find_external_background_image(
        image_path, config.background.external_dir or "",
    )
    external_loaded = load_image(
        external_path,
        config.channel,
        PixelSizeConfig(um_per_px=loaded.pixel_size_um, require_metadata=False),
    )
    corrected = external_loaded.image.astype(np.float32)
    if corrected.shape != raw.shape:
        raise ValueError(
            f"External background image shape mismatch for {image_path.name}: "
            f"{corrected.shape} vs raw Iba1 channel {raw.shape}"
        )
    logger.info("Using external background-corrected image for %s: %s",
                image_path.name, external_path)
    return denoise_image(corrected, config.denoising)


def process_single_image(
    image_path: Path,
    config: Config,
    metadata: Dict[str, Dict[str, str]],
    corrections,
    output_dir: Path,
    per_object_path: Path,
) -> List[ImageROIResult]:
    """Run the full per-image pipeline; returns one result per ROI."""
    image_id = image_path.stem
    group = metadata.get(image_id, {}).get("group") if metadata else None
    logger.info("Processing %s (group=%s)", image_id, group)

    loaded = load_image(image_path, config.channel, config.pixel_size)
    raw = loaded.image.astype(np.float32)
    rois = load_rois_for_image(image_path, raw.shape, config.roi)

    # Preprocess once per image; ROI is applied later.
    corrected = _prepare_detection_image(raw, image_path, loaded, config)
    enh = enhance_soma(corrected, config.soma_enhancement)

    results: List[ImageROIResult] = []
    overlays_dir = output_dir / "qc_overlays"

    for roi in rois:
        roi_id = roi.name
        seeds = detect_candidate_seeds(enh.response, roi.mask, config.seed_detection)
        labels = segment_soma_candidates(
            corrected, enh, seeds, roi.mask,
            config.segmentation,
            soma_radius_px=config.soma_enhancement.soma_radius_px,
            seed_min_peak=config.seed_detection.min_peak_intensity,
        )

        candidates = measure_objects(
            labels=labels,
            intensity_image=corrected,
            roi_mask=roi.mask,
            pixel_size_um=loaded.pixel_size_um,
            edge_margin_px=config.object_filter.edge_margin_px,
        )
        filter_objects(candidates, config.object_filter)
        accepted = [c for c in candidates if c.accepted]
        rejected = [c for c in candidates if not c.accepted]

        # Apply manual corrections if enabled
        n_manual_changes = 0
        corrected_accepted = accepted
        if config.corrections.enabled and corrections:
            corrected_accepted, n_manual_changes = apply_corrections(
                image_id, roi_id, accepted, corrections,
                config.corrections.radius_for_remove_px,
            )

        roi_intens = measure_roi_intensity(
            bg_corrected=corrected,
            raw_image=raw,
            roi_mask=roi.mask,
            pixel_size_um=loaded.pixel_size_um,
            cfg=config.intensity,
        )

        count = len(accepted)
        count_corrected = len(corrected_accepted) if n_manual_changes else None
        density = count / roi_intens.area_mm2 if roi_intens.area_mm2 > 0 else 0.0
        density_corrected = (
            count_corrected / roi_intens.area_mm2
            if (count_corrected is not None and roi_intens.area_mm2 > 0)
            else None
        )

        results.append(ImageROIResult(
            image_id=image_id,
            image_path=str(image_path),
            roi_id=roi_id,
            group=group,
            parameter_set_id=config.parameter_set_id,
            pixel_size_um=loaded.pixel_size_um,
            pixel_size_source=loaded.pixel_size_source,
            count=count,
            count_corrected=count_corrected,
            roi_area_mm2=roi_intens.area_mm2,
            density_cells_per_mm2=density,
            density_corrected=density_corrected,
            iba1_area_fraction=roi_intens.iba1_area_fraction,
            roi_mean_intensity=roi_intens.mean_intensity,
            roi_integrated_intensity=roi_intens.integrated_intensity,
            n_rejected=len(rejected),
        ))

        # Per-object CSV append
        write_per_object_csv(
            image_id=image_id,
            roi_id=roi_id,
            candidates=candidates,
            pixel_size_um=loaded.pixel_size_um,
            output_path=per_object_path,
            save_rejected=config.save_rejected_objects,
        )

        # QC overlay (full: raw + ROI + accepted contours + rejected X + scale bar)
        if config.qc.save_overlays:
            display_count = count_corrected if count_corrected is not None else count
            overlay_path = overlays_dir / f"{image_id}__{roi_id}"
            generate_qc_overlay(
                image=raw,
                roi_mask=roi.mask,
                labels=labels,
                candidates=candidates,
                image_name=image_id,
                roi_name=roi_id,
                accepted_count=display_count,
                cfg=config.qc,
                output_path=overlay_path,
                pixel_size_um=loaded.pixel_size_um,
            )
            # Clean overlay: bg-corrected + ROI + accepted-only grey circles.
            # No rejected markers, no scale bar -- intended for fast visual check.
            # Filename intentionally omits roi_id; if multiple ROIs per image
            # exist, the last one wins (rare for typical single-ROI projects).
            clean_path = overlays_dir / f"{image_id}_qc"
            generate_qc_clean_overlay(
                corrected=corrected,
                roi_mask=roi.mask,
                candidates=candidates,
                image_name=image_id,
                roi_name=roi_id,
                accepted_count=display_count,
                cfg=config.qc,
                output_path=clean_path,
            )

    return results


def run_batch(config: Config) -> Path:
    """Run the pipeline over every image matching the config glob.

    Returns the path to the per-image summary CSV.
    """
    _set_seed(config.random_seed)
    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if (
        config.background.method not in {"none", "external"}
        and config.background.radius_px < 3 * config.soma_enhancement.soma_radius_px
    ):
        logger.warning(
            "background.radius_px (%.1f) is < 3x soma_radius_px (%.1f). Real cell bodies "
            "may be removed during background subtraction.",
            config.background.radius_px, config.soma_enhancement.soma_radius_px,
        )

    images = find_images(Path(config.input_dir), config.file_glob)
    metadata = load_metadata(config.metadata_csv)
    corrections = (
        load_corrections(config.corrections.corrections_csv)
        if config.corrections.enabled else []
    )

    per_object_path = output_dir / "per_object.csv"
    if per_object_path.exists():
        per_object_path.unlink()  # we append, so start fresh

    write_parameter_log(config, output_dir)

    all_results: List[ImageROIResult] = []
    for img_path in images:
        try:
            res = process_single_image(
                img_path, config, metadata, corrections,
                output_dir=output_dir,
                per_object_path=per_object_path,
            )
            all_results.extend(res)
        except Exception as exc:
            logger.exception("Failed to process %s: %s", img_path, exc)

    summary_path = write_image_summary_csv(all_results, output_dir)
    logger.info("Wrote summary to %s (n=%d rows)", summary_path, len(all_results))
    return summary_path
