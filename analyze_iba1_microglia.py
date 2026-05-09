"""Command-line entry point for the Iba1 microglia quantification pipeline.

Usage
-----

Standard batch run:
    python analyze_iba1_microglia.py --config config.yaml

Process a single image (everything else respects the config):
    python analyze_iba1_microglia.py --config config.yaml --single-image path/to/image.tif

Run validation against manual counts (config.validation must be enabled):
    python analyze_iba1_microglia.py --config config.yaml --validate

Run parameter optimization (config.optimization must be enabled):
    python analyze_iba1_microglia.py --config config.yaml --optimize
"""

from __future__ import annotations

import argparse
import logging
import sys
from copy import deepcopy
from pathlib import Path

from iba1_pipeline.batch import run_batch
from iba1_pipeline.config import load_config
from iba1_pipeline.logging_utils import setup_logger
from iba1_pipeline.optimization import run_optimization
from iba1_pipeline.validation import validate_against_manual_counts


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Iba1+ microglia quantification (semi-automated Fiji + Python pipeline).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--config", required=True, type=Path,
                   help="Path to YAML/JSON pipeline configuration file.")
    p.add_argument("--validate", action="store_true",
                   help="After the batch run, validate against manual counts.")
    p.add_argument("--optimize", action="store_true",
                   help="Run parameter optimization (overrides --validate).")
    p.add_argument("--single-image", type=Path, default=None,
                   help="If set, process only this image. The config's input_dir is "
                        "temporarily overridden to the image's parent directory.")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Logging level for stderr and the log file.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    config = load_config(args.config)

    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger(
        log_file=output_dir / "run.log",
        level=getattr(logging, args.log_level),
    )

    logger.info("Loaded config from %s (parameter_set_id=%s)",
                args.config, config.parameter_set_id)

    if args.optimize:
        if not config.optimization.enabled:
            logger.error("--optimize given but optimization.enabled is False in config.")
            return 2
        result = run_optimization(config, base_output_dir=output_dir)
        logger.info("Optimization complete. Best %s=%.4f at %s",
                    result.metric_name, result.best_metric, result.best_combo)
        logger.info("Best config written to %s",
                    output_dir / "optimization" / "best_config.yaml")
        return 0

    if args.single_image is not None:
        single = args.single_image.resolve()
        if not single.exists():
            logger.error("Image file not found: %s", single)
            return 2
        # Run on that single image only; configure a one-image glob.
        cfg = deepcopy(config)
        cfg.input_dir = str(single.parent)
        cfg.file_glob = single.name
        run_batch(cfg)
    else:
        run_batch(config)

    if args.validate:
        if not config.validation.enabled:
            logger.error("--validate given but validation.enabled is False in config.")
            return 2
        if not config.validation.manual_counts_csv:
            logger.error("validation.manual_counts_csv is empty in config.")
            return 2
        summary_csv = output_dir / "image_summary.csv"
        manual_csv = Path(config.validation.manual_counts_csv)
        val_dir = output_dir / config.validation.output_subdir
        metrics = validate_against_manual_counts(summary_csv, manual_csv, val_dir)
        logger.info("Validation: %s", metrics)

    return 0


if __name__ == "__main__":
    sys.exit(main())
