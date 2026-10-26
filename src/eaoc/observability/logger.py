"""
EAOC Observability Centralized Logger Module
Provides configure_logging() initializer and get_logger() accessor.
"""

import logging
import sys
from typing import Optional
from eaoc.config import get_settings
from eaoc.observability.formatter import ConsoleDevelopmentFormatter, StructuredJsonFormatter

_logging_configured: bool = False


def configure_logging(force_reconfigure: bool = False) -> None:
    """
    Configures application-wide logging using settings.LOG_LEVEL and settings.ENVIRONMENT.
    Sets StructuredJsonFormatter for production/testing and ConsoleDevelopmentFormatter for development.
    """
    global _logging_configured
    if _logging_configured and not force_reconfigure:
        return

    settings = get_settings()
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Clear existing handlers to prevent duplicate logs
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)

    if settings.is_development() and not settings.is_testing():
        console_handler.setFormatter(ConsoleDevelopmentFormatter())
    else:
        console_handler.setFormatter(StructuredJsonFormatter())

    root_logger.addHandler(console_handler)

    # Silence overly noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    _logging_configured = True


def get_logger(name: str = "eaoc") -> logging.Logger:
    """
    Returns a configured logger instance for the given component module name.
    Ensures global logging is initialized.
    """
    if not _logging_configured:
        configure_logging()
    return logging.getLogger(name)
