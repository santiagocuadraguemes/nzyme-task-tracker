"""Rate limiter for the Notion API.

Implements a simple sleep-based rate limiter that ensures the caller
does not exceed a configurable number of requests per second.  The
Notion API enforces a limit of approximately 3 requests per second per
integration token; exceeding this returns HTTP 429 responses.

Key class:
    ``RateLimiter`` — call ``acquire()`` before each API request to
    ensure compliance with the rate limit.

Design notes:
    * A token-bucket algorithm is used internally: tokens are added at a
      steady rate (``max_requests_per_second``) and ``acquire()`` blocks
      (via ``time.sleep``) until a token is available.
    * Thread-safety is *not* required for the initial implementation
      (single-threaded sync loop), but the design should be easy to
      extend with a ``threading.Lock`` if needed later.
"""
from __future__ import annotations


class RateLimiter:
    """Sleep-based rate limiter for API calls.

    Parameters
    ----------
    max_requests_per_second:
        Maximum sustained request rate.  Defaults to ``3.0`` to match
        the Notion API limit.
    """

    def __init__(self, max_requests_per_second: float = 3.0) -> None:
        self._max_rps = max_requests_per_second
        self._min_interval: float = 1.0 / max_requests_per_second
        self._last_request_time: float = 0.0

    def acquire(self) -> None:
        """Block until a request slot is available.

        If called faster than ``max_requests_per_second``, this method
        sleeps for the remaining interval before returning.
        """
        import time

        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.monotonic()

    def reset(self) -> None:
        """Reset the limiter state, allowing the next request immediately."""
        self._last_request_time = 0.0
