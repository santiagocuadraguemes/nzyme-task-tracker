"""Logging setup for Nzyme Task Tracker.

Goals:
  * Default INFO output is a short, human-readable trace of what the pipeline
    is doing — one or two lines per phase per meeting.
  * Set ``LOG_LEVEL=DEBUG`` to get the full HTTP / library trace for
    debugging.
  * Works identically in CLI and in AWS Lambda. In Lambda, AWS configures the
    root handler (JSON format), so we do NOT add a second handler — we only
    set levels.
"""
from __future__ import annotations

import logging
import os

# Third-party loggers that flood INFO with one line per HTTP request or
# auth event. Capped at WARNING so a real problem still surfaces.
_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "urllib3",
    "openai",
    "openai._base_client",
    "botocore",
    "boto3",
    "s3transfer",
    "googleapiclient",
    "googleapiclient.discovery",
    "googleapiclient.discovery_cache",
    "google",
    "google.auth",
    "google_auth_httplib2",
)


def _running_in_lambda() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure root logger level and silence noisy third-party loggers.

    Idempotent: safe to call from both module load (Lambda cold start) and
    CLI ``main()``.
    """
    resolved_level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(resolved_level)

    # CLI: add a stream handler with our format. In Lambda, AWS owns the
    # handler (JSON format via template.yaml LoggingConfig), so adding one
    # would double every log line.
    if not _running_in_lambda() and not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        root.addHandler(handler)

    # Always cap noisy loggers — applies whether in CLI or Lambda.
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    return root


def get_logger(name: str) -> logging.Logger:
    """Return a named logger (typically pass ``__name__``)."""
    return logging.getLogger(name)
