"""Configuration loading and validation.

The pipeline is driven by a single YAML (or JSON) config. This module defines
the schema as a dataclass tree, applies defaults, and validates user input.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


# -- Sub-configurations ------------------------------------------------------


@dataclass
class ChannelConfig:
    """Which channel of a multi-channel image holds Iba1.

    For RGB images, ``mode='rgb'`` and ``index`` selects R/G/B as 0/1/2.
    For multi-channel TIFFs, ``mode='multi'`` and ``index`` selects the channel.
    For single-channel images, ``mode='single'`` ignores ``index``.
    """

    mode: str = "rgb"  # 'rgb' | 'multi' | 'single' | 'auto'
    index: int = 1  # default = green for RGB

    def validate(self) -> None:
        if self.mode not in {"rgb", "multi", "single", "auto"}:
            raise ValueError(f"channel.mode must be rgb|multi|single|auto, got {self.mode!r}")
        if self.index < 0:
            raise ValueError("channel.index must be >= 0")


@dataclass
class PixelSizeConfig:
    """Pixel calibration. ``um_per_px`` overrides metadata if set."""

    um_per_px: Optional[float] = None  # if None, read from TIFF metadata
    require_metadata: bool = False  # if True, fail when metadata is missing

    def validate(self) -> None:
        if self.um_per_px is not None and self.um_per_px <= 0:
            raise ValueError("pixel_size.um_per_px must be > 0 if provided")


@dataclass
class BackgroundConfig:
    """Background correction.

    The radius MUST be substantially larger than the expected soma radius so
    real cell bodies are not removed.
    """

    method: str = "rolling_ball"  # 'rolling_ball' | 'morph_opening' | 'gaussian' | 'none' | 'external'
    radius_px: float = 50.0  # pixels; should be >= 5x soma_radius_px
    external_dir: Optional[str] = None  # directory of pre-background-subtracted images

    def validate(self) -> None:
        if self.method not in {"rolling_ball", "morph_opening", "gaussian", "none", "external"}:
            raise ValueError(f"background.method invalid: {self.method!r}")
        if self.radius_px <= 0:
            raise ValueError("background.radius_px must be > 0")
        if self.method == "external" and not self.external_dir:
            raise ValueError("background.external_dir is required when background.method='external'")


@dataclass
class DenoisingConfig:
    """Mild denoising before soma enhancement.

    Defaults are intentionally light; aggressive blurring merges adjacent
    soma and is to be avoided.
    """

    method: str = "median"  # 'median' | 'gaussian' | 'none'
    median_size_px: int = 3
    gaussian_sigma_px: float = 0.8

    def validate(self) -> None:
        if self.method not in {"median", "gaussian", "none"}:
            raise ValueError(f"denoising.method invalid: {self.method!r}")
        if self.median_size_px < 1:
            raise ValueError("denoising.median_size_px must be >= 1")
        if self.gaussian_sigma_px < 0:
            raise ValueError("denoising.gaussian_sigma_px must be >= 0")


@dataclass
class SomaEnhancementConfig:
    """Suppress thin Iba1+ processes and enhance soma-sized blobs."""

    method: str = "tophat_dog"  # 'tophat_dog' | 'tophat' | 'dog' | 'log' | 'opening'
    soma_radius_px: float = 6.0  # expected microglial soma radius in pixels
    dog_sigma_ratio: float = 1.6  # outer/inner sigma for DoG (Marr–Hildreth-ish)

    def validate(self) -> None:
        if self.method not in {"tophat_dog", "tophat", "dog", "log", "opening"}:
            raise ValueError(f"soma_enhancement.method invalid: {self.method!r}")
        if self.soma_radius_px <= 0:
            raise ValueError("soma_enhancement.soma_radius_px must be > 0")
        if self.dog_sigma_ratio <= 1.0:
            raise ValueError("soma_enhancement.dog_sigma_ratio must be > 1.0")


@dataclass
class SeedDetectionConfig:
    """Candidate soma-center detection.

    ``min_peak_intensity`` is a FIXED absolute threshold on the soma-enhanced
    response. Per-image adaptive thresholds are not used because they inflate
    false positives in microglia-depleted images.
    """

    min_distance_px: float = 8.0
    min_peak_intensity: float = 50.0  # absolute, on the soma-enhanced image
    exclude_border_px: int = 2

    def validate(self) -> None:
        if self.min_distance_px <= 0:
            raise ValueError("seed_detection.min_distance_px must be > 0")
        if self.min_peak_intensity < 0:
            raise ValueError("seed_detection.min_peak_intensity must be >= 0")
        if self.exclude_border_px < 0:
            raise ValueError("seed_detection.exclude_border_px must be >= 0")


@dataclass
class SegmentationConfig:
    """Marker-controlled watershed restricted to soma candidate regions.

    ``soma_mask_intensity`` is an absolute threshold on the background-
    corrected image used to restrict where the watershed can grow. A separate
    threshold on the soma-enhanced image is computed dynamically as a fraction
    of ``min_peak_intensity`` to keep segmentation tight around real soma.
    """

    soma_mask_intensity: float = 30.0  # absolute, on bg-corrected image
    enhanced_mask_fraction: float = 0.25  # fraction of seed.min_peak_intensity
    max_soma_radius_factor: float = 2.5  # cap watershed growth at N x soma_radius

    def validate(self) -> None:
        if self.soma_mask_intensity < 0:
            raise ValueError("segmentation.soma_mask_intensity must be >= 0")
        if not 0 < self.enhanced_mask_fraction <= 1:
            raise ValueError("segmentation.enhanced_mask_fraction in (0,1]")
        if self.max_soma_radius_factor <= 0:
            raise ValueError("segmentation.max_soma_radius_factor must be > 0")


@dataclass
class ObjectFilterConfig:
    """Filters applied to candidate soma regions after segmentation.

    Area and intensity are PRIMARY filters. Circularity/solidity are weak
    auxiliary filters (used only to remove obvious artifacts, never for
    biological classification).
    """

    min_area_um2: float = 15.0
    max_area_um2: float = 200.0
    min_mean_intensity: float = 30.0
    min_peak_intensity: float = 60.0
    min_circularity: Optional[float] = None  # 0..1, e.g., 0.2 — disabled if None
    min_solidity: Optional[float] = None  # 0..1, e.g., 0.5 — disabled if None
    exclude_edge_objects: bool = True
    edge_margin_px: int = 1

    def validate(self) -> None:
        if self.min_area_um2 < 0 or self.max_area_um2 <= self.min_area_um2:
            raise ValueError("object_filter: max_area_um2 must be > min_area_um2 >= 0")
        if self.min_mean_intensity < 0 or self.min_peak_intensity < 0:
            raise ValueError("object_filter: intensity thresholds must be >= 0")
        for name, val in [("min_circularity", self.min_circularity),
                          ("min_solidity", self.min_solidity)]:
            if val is not None and not 0 <= val <= 1:
                raise ValueError(f"object_filter.{name} must be in [0, 1]")


@dataclass
class IntensityConfig:
    """Settings for the Iba1+ area fraction supporting metric."""

    area_fraction_threshold: float = 30.0  # absolute on bg-corrected image
    use_otsu_for_area_fraction: bool = False  # If True, use Otsu instead

    def validate(self) -> None:
        if self.area_fraction_threshold < 0:
            raise ValueError("intensity.area_fraction_threshold must be >= 0")


@dataclass
class QCConfig:
    """QC overlay options."""

    save_overlays: bool = True
    show_rejected: bool = True
    contour_color: str = "lime"
    rejected_color: str = "red"
    roi_color: str = "yellow"
    show_ids: bool = False
    figure_dpi: int = 150
    overlay_format: str = "png"  # 'png' | 'tif'

    def validate(self) -> None:
        if self.figure_dpi <= 0:
            raise ValueError("qc.figure_dpi must be > 0")
        if self.overlay_format not in {"png", "tif", "tiff"}:
            raise ValueError("qc.overlay_format must be png or tif")


@dataclass
class ROIConfig:
    """ROI loading.

    ``directory`` may contain ``.zip`` (multi-ROI) or ``.roi`` files named to
    match images (e.g., ``image_001.tif`` ↔ ``image_001.zip``). If no matching
    ROI is found, behaviour depends on ``fallback_whole_image``.
    """

    directory: Optional[str] = None
    suffix: str = ".zip"  # '.zip' | '.roi'
    fallback_whole_image: bool = True


@dataclass
class ValidationConfig:
    """Optional comparison against manual counts."""

    enabled: bool = False
    manual_counts_csv: Optional[str] = None
    output_subdir: str = "validation"


@dataclass
class OptimizationConfig:
    """Optional grid-search parameter optimization."""

    enabled: bool = False
    manual_counts_csv: Optional[str] = None
    training_subset: Optional[List[str]] = None  # image_ids; None = all in manual CSV
    grids: Dict[str, List[float]] = field(default_factory=dict)
    metric: str = "mae"  # 'mae' | 'rmse' | 'mae_balanced'


@dataclass
class CorrectionsConfig:
    """Optional manual correction table."""

    enabled: bool = False
    corrections_csv: Optional[str] = None
    radius_for_remove_px: float = 6.0  # match removal click to nearest detection


@dataclass
class Config:
    """Top-level pipeline configuration."""

    input_dir: str = "input"
    output_dir: str = "output"
    file_glob: str = "*.tif*"  # matches .tif and .tiff
    parameter_set_id: str = "default_v1"
    metadata_csv: Optional[str] = None  # optional CSV with image_id,group columns
    random_seed: int = 0

    channel: ChannelConfig = field(default_factory=ChannelConfig)
    pixel_size: PixelSizeConfig = field(default_factory=PixelSizeConfig)
    roi: ROIConfig = field(default_factory=ROIConfig)
    background: BackgroundConfig = field(default_factory=BackgroundConfig)
    denoising: DenoisingConfig = field(default_factory=DenoisingConfig)
    soma_enhancement: SomaEnhancementConfig = field(default_factory=SomaEnhancementConfig)
    seed_detection: SeedDetectionConfig = field(default_factory=SeedDetectionConfig)
    segmentation: SegmentationConfig = field(default_factory=SegmentationConfig)
    object_filter: ObjectFilterConfig = field(default_factory=ObjectFilterConfig)
    intensity: IntensityConfig = field(default_factory=IntensityConfig)
    qc: QCConfig = field(default_factory=QCConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    optimization: OptimizationConfig = field(default_factory=OptimizationConfig)
    corrections: CorrectionsConfig = field(default_factory=CorrectionsConfig)

    save_rejected_objects: bool = True

    # ------------------------------------------------------------------

    def validate(self) -> None:
        for sub in [self.channel, self.pixel_size, self.background, self.denoising,
                    self.soma_enhancement, self.seed_detection, self.segmentation,
                    self.object_filter, self.intensity, self.qc]:
            sub.validate()
        if self.background.radius_px < 3 * self.soma_enhancement.soma_radius_px:
            # A warning rather than an error; some imaging setups need a smaller radius.
            # Loggers in the pipeline pick this up via warning when running.
            pass

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# -- Loaders ----------------------------------------------------------------


def _merge_dataclass(target: Any, data: Dict[str, Any]) -> None:
    """Recursively overwrite dataclass fields from a plain dict."""
    if data is None:
        return
    for key, value in data.items():
        if not hasattr(target, key):
            raise ValueError(f"Unknown config key: {key!r} (in {type(target).__name__})")
        current = getattr(target, key)
        # If field itself is a dataclass and value is a dict, recurse.
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge_dataclass(current, value)
        else:
            setattr(target, key, value)


def load_config(path: Path) -> Config:
    """Load a YAML or JSON config file into a validated ``Config`` instance."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        data: Dict[str, Any] = yaml.safe_load(text) or {}
    elif path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        raise ValueError(f"Unsupported config extension: {path.suffix}")

    config = Config()
    _merge_dataclass(config, data)
    config.validate()
    return config


def dump_config(config: Config, path: Path) -> None:
    """Write a Config back out as YAML."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(config.to_dict(), fh, sort_keys=False)
