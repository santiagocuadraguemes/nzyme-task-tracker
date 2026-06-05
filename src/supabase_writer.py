"""PostgREST upsert client for the `meeting_transcripts` table.

Stdlib-only — runs in Lambda without adding the `supabase` Python SDK.
Uses urllib because PostgREST is just a REST endpoint; no smart-client
features (subscriptions, RPC, auth flows) are needed for backend writes.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)


def _credentials() -> tuple[str, str]:
    """Read SUPABASE_URL and a service-role key from env.

    Accept either SUPABASE_SERVICE_ROLE_KEY (explicit, preferred) or
    SUPABASE_KEY (short — common in .env files). Must be a service-role
    key — RLS bypass is required because the table is RLS-enabled with no
    policies.
    """
    url = os.environ.get("SUPABASE_URL")
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
    )
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) "
            "must be set in the environment.",
        )
    return url, key


def upsert_meetings(rows: list[dict[str, Any]]) -> None:
    """POST rows to `meeting_transcripts` with on_conflict=page_id.

    Rows may omit columns to leave them untouched on merge (e.g.
    ``attendee_emails`` is popped when resolution didn't run, so an upsert
    never NULLs out previously stored emails). PostgREST requires uniform
    keys within one POST, so rows are grouped by key shape and sent in one
    request per shape.
    """
    if not rows:
        return
    url, key = _credentials()
    endpoint = (
        f"{url.rstrip('/')}/rest/v1/meeting_transcripts?on_conflict=page_id"
    )
    groups: dict[frozenset[str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(frozenset(row), []).append(row)
    for group in groups.values():
        body = json.dumps(group, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Supabase upsert failed ({e.code}): {detail}",
            ) from e


def fetch_max_last_edited(db_ids: list[str]) -> dict[str, str]:
    """Return {db_id: max(last_edited_time)} for the given DB IDs.

    Used as the incremental-sync checkpoint per Meeting Notes DB. DBs with
    no rows yet are absent from the result — caller should treat as
    "from the beginning of time".
    """
    if not db_ids:
        return {}
    url, key = _credentials()
    in_list = "(" + ",".join(f'"{d}"' for d in db_ids) + ")"
    qs = urllib.parse.urlencode({
        "select": "db_id,last_edited_time",
        "db_id": f"in.{in_list}",
        "order": "last_edited_time.desc",
        # PostgREST limits the response; we sort newest first and take
        # the first hit per db_id below.
        "limit": "1000",
    })
    req = urllib.request.Request(
        f"{url.rstrip('/')}/rest/v1/meeting_transcripts?{qs}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Supabase checkpoint read failed ({e.code}): {detail}",
        ) from e

    out: dict[str, str] = {}
    for r in rows:
        d = r.get("db_id")
        t = r.get("last_edited_time")
        if not d or not t:
            continue
        # Keep the largest (rows are pre-sorted desc, so the first wins).
        if d not in out:
            out[d] = t
    return out
