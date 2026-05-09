"""Logging configuration for the Iba1 pipeline."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional


def setup_logger(
    name: str = "iba1_pipeline",
    log_file: Optional[Path] = None,
    level: int = logging.INFO,
) -> logging.Logger:
    """Configure and return a logger that writes to stderr and optionally a file.

    Parameters
    ----------
    name
        Logger name (use a stable name so repeated calls return the same logger).
    log_file
        Optional path for a file handler. The parent directory is created if missing.
    level
        Logging level for both handlers.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    # Clear pre-existing handlers if setup_logger is called more than once
    # (e.g., when a single process runs --optimize then a normal batch).
    if logger.handlers:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler(stream=sys.stderr)
    stream_handler.setFormatter(fmt)
    stream_handler.setLevel(level)
    logger.addHandler(stream_handler)

    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        file_handler.setFormatter(fmt)
        file_handler.setLevel(level)
        logger.addHandler(file_handler)

    return logger
