"""Настройка логирования. Всё пишется в stderr: stdout занят JSON-выводом CLI и MCP-протоколом."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from .config import LoggingConfig

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(cfg: LoggingConfig, level: str | None = None) -> None:
    logger = logging.getLogger("web_scraper")
    logger.setLevel((level or cfg.level).upper())
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    formatter = logging.Formatter(_FORMAT)
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(formatter)
    logger.addHandler(stderr)

    if cfg.file:
        Path(cfg.file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(cfg.file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    # httpx на INFO логирует каждый запрос — это шум рядом с нашими логами.
    logging.getLogger("httpx").setLevel(logging.WARNING)
