"""Utility package — cross-cutting helpers used throughout the sync engine.

Contains:

* ``rate_limiter`` — token-bucket / sleep-based rate limiter for the
  Notion API (default 3 requests/second).
* ``logger`` — structured logging setup using the standard library.
"""
from __future__ import annotations
