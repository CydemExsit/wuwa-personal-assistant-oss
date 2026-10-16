"""Small rotating local log with deliberately metadata-only messages."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOGGER_NAME = "wuwa_assistant"
_HANDLER_MARKER = "_wuwa_local_handler"


def configure_local_logging(logs_dir: Path, *, app_version: str) -> logging.Logger:
    """Configure one UTF-8 rotating log without recording user content or images."""

    logs_dir.mkdir(parents=True, exist_ok=True)
    target = (logs_dir / "application.log").resolve()
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in tuple(logger.handlers):
        if not getattr(handler, _HANDLER_MARKER, False):
            continue
        current = Path(getattr(handler, "baseFilename", "")).resolve()
        if current == target:
            logger.info("application_start version=%s", app_version)
            return logger
        logger.removeHandler(handler)
        handler.close()

    handler = RotatingFileHandler(
        target,
        maxBytes=1_000_000,
        backupCount=2,
        encoding="utf-8",
    )
    setattr(handler, _HANDLER_MARKER, True)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S")
    )
    logger.addHandler(handler)
    logger.info("application_start version=%s", app_version)
    return logger


__all__ = ["configure_local_logging"]
