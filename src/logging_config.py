from __future__ import annotations

import logging
import logging.handlers
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

import structlog


def configure_logging(level: str = "INFO", log_dir: str | Path | None = None) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )

    handlers: list[logging.Handler] = []

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    handlers.append(stream_handler)

    if log_dir is not None:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            log_path / "trading-bot.log",
            when="midnight",
            backupCount=90,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    for h in root_logger.handlers[:]:
        root_logger.removeHandler(h)
    for h in handlers:
        root_logger.addHandler(h)


def get_logger(name: str, **initial_context: Any) -> structlog.stdlib.BoundLogger:
    logger = structlog.get_logger(name)
    if initial_context:
        logger = logger.bind(**initial_context)
    return logger  # type: ignore[return-value]


@contextmanager
def sleeve_log_context(
    logger: structlog.stdlib.BoundLogger, sleeve_id: str
) -> Generator[structlog.stdlib.BoundLogger, None, None]:
    """Bind sleeve_id to all log calls within this context."""
    bound = logger.bind(sleeve_id=sleeve_id)
    try:
        yield bound
    finally:
        pass
