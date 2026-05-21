"""Logging setup for StmtForge."""

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from stmtforge.utils.config import load_config, resolve_path


_initialized = False


def _redact_message(message: str) -> str:
    """Redact common personal data patterns from log messages."""
    if not message:
        return message

    message = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[REDACTED_EMAIL]", message)
    message = re.sub(r"(?<!\d)(?:\+91[-\s]?)?[6-9]\d{9}(?!\d)", "[REDACTED_PHONE]", message)
    message = re.sub(r"\b[A-Z]{5}\d{4}[A-Z]\b", "[REDACTED_PAN]", message)
    message = re.sub(r"\b(?:\d[ -]*){12,19}\d\b", "[REDACTED_CARD]", message)
    message = re.sub(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b", "[REDACTED_AADHAAR]", message)
    message = re.sub(r"\b[A-Z]{4}0[A-Z0-9]{6}\b", "[REDACTED_IFSC]", message)
    return message


class _RedactionFilter(logging.Filter):
    """Logging filter to sanitize emitted messages for privacy."""

    def filter(self, record: logging.LogRecord) -> bool:
        sanitized = _redact_message(record.getMessage())
        record.msg = sanitized
        record.args = ()
        # Also redact exception tracebacks
        if record.exc_text:
            record.exc_text = _redact_message(record.exc_text)
        return True


def setup_logging(name: str = "stmtforge") -> logging.Logger:
    """Set up and return a configured logger."""
    global _initialized
    logger = logging.getLogger(name)

    if _initialized:
        return logger

    try:
        config = load_config()
    except FileNotFoundError:
        # No config file yet (e.g. during `stmtforge init`); use defaults
        config = {}

    log_config = config.get("logging", {})
    level = getattr(logging, log_config.get("level", "INFO").upper(), logging.INFO)
    logger.setLevel(level)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler — force UTF-8 on Windows to handle unicode chars (✓, →, etc.)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(_RedactionFilter())
    logger.addHandler(console_handler)

    # File handler (only if config is available)
    log_file = log_config.get("file", "data/stmtforge.log")
    try:
        log_path = resolve_path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=log_config.get("max_bytes", 10 * 1024 * 1024),
            backupCount=log_config.get("backup_count", 5),
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(_RedactionFilter())
        logger.addHandler(file_handler)
    except Exception:
        pass  # Skip file logging if path can't be resolved

    _initialized = True
    return logger


def get_logger(module_name: str) -> logging.Logger:
    """Get a child logger for a specific module."""
    setup_logging()
    return logging.getLogger(f"stmtforge.{module_name}")
