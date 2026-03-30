"""Structured logging setup for the Nzyme Task Tracker.

Configures Python's standard ``logging`` library with a structured format
that includes timestamps, log level, and module name in every message.

Key functions:
    ``setup_logging``  — one-time initialisation of the root logger.
    ``get_logger``     — returns a named child logger for a specific module.

Design notes:
    * Uses only the standard library (no external dependencies like
      ``structlog``) to keep the dependency footprint small.
    * The format string includes ISO-8601 timestamps and the logger name
      so that log output is easy to search and correlate.
    * ``setup_logging`` is idempotent — calling it multiple times does not
      add duplicate handlers.
"""
from __future__ import annotations

import logging


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure the root logger and return it.

    Sets up a ``StreamHandler`` with a structured format if one has not
    already been attached.

    Parameters
    ----------
    level:
        Logging level name (e.g. ``"DEBUG"``, ``"INFO"``).

    Returns
    -------
    logging.Logger
        The root logger, configured and ready to use.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not root.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)

    return root


def get_logger(name: str) -> logging.Logger:
    """Return a named logger.

    Parameters
    ----------
    name:
        Logger name, typically ``__name__`` of the calling module.

    Returns
    -------
    logging.Logger
        A child logger that inherits the root configuration.
    """
    return logging.getLogger(name)
