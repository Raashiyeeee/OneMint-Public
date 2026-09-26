"""
Structured logging configuration.

Usage:
    from app.utils.logging import setup_logging
    setup_logging("INFO")
"""
from __future__ import annotations

import logging
import sys
from typing import Optional


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> None:
    """
    Configure structured logging.

    Parameters
    ----------
    level : str
        Python logging level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    log_file : str, optional
        If provided, also write logs to this file path.
    """
    fmt = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%SZ"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        datefmt=datefmt,
        handlers=handlers,
    )

    # Quieten noisy third-party libraries
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
