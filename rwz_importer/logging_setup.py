"""Logging configuration for the CLI.

The OWA client logs one line per HTTP request under the ``rwz_importer`` logger;
this wires that to stderr with a readable format. ``--verbose`` drops the level
to DEBUG, and an optional file mirrors everything for an audit trail of a run.
"""

from __future__ import annotations

import logging
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"


def setup_logging(*, verbose: bool = False, logfile: str | Path | None = None) -> None:
    """Configure the ``rwz_importer`` logger (idempotent across calls)."""
    logger = logging.getLogger("rwz_importer")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    stream = logging.StreamHandler()
    stream.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    if logfile:
        file_handler = logging.FileHandler(logfile, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
