"""Minimal stdlib Supabase PostgREST client (service-role key).

Generic ``_http`` + ``_supabase_creds`` helpers, formerly defined in
``src/hierarchy/canonical_mirror_sync.py``. Lifted into this shared module when
the Hierarchy appliers were carved out to the standalone ``nzyme-housekeeping``
Lambda (2026-06-11) — ``config_mirror_sync`` still needs the generic helper, so
it lives here rather than in the deleted ``hierarchy`` package.

Uses urllib (no smart client) because PostgREST is just a REST endpoint.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


def _supabase_creds() -> tuple[str, str]:
    """Resolve Supabase URL + service-role key from env.

    Accepts ``SUPABASE_SERVICE_ROLE_KEY`` (explicit) or ``SUPABASE_KEY``
    (short — common in .env). Must be a service-role key — RLS bypass is
    required because the mirror tables are RLS-enabled with no policies.
    """
    url = os.environ.get("SUPABASE_URL")
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
    )
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) "
            "must be set for Supabase REST access.",
        )
    return url.rstrip("/"), key


def _http(method: str, path: str, body: Any = None, prefer: str = "return=minimal") -> Any:
    url, key = _supabase_creds()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{url}{path}", data=data, method=method, headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
            if not payload:
                return None
            try:
                return json.loads(payload.decode("utf-8"))
            except json.JSONDecodeError:
                return None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Supabase {method} {path} failed ({e.code}): {detail}",
        ) from e
