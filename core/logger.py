from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from rich.logging import RichHandler


def get_logger(name: str = "rpa", log_dir: Path | None = None) -> logging.Logger:
    """Configure a logger with Rich console output and daily rotating file handler."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False

    rich_handler = RichHandler(
        rich_tracebacks=True,
        show_path=False,
        markup=False,
    )
    rich_handler.setLevel(logging.INFO)
    logger.addHandler(rich_handler)

    if log_dir is None:
        log_dir = Path(__file__).resolve().parent.parent / "storage" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = TimedRotatingFileHandler(
        log_dir / "rpa.log",
        when="midnight",
        interval=1,
        backupCount=14,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    )
    logger.addHandler(file_handler)
    return logger
