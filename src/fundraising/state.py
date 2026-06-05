"""Supabase claim-before-post state for the fundraising → Affinity branch.

One row per meeting page in ``public.affinity_meeting_posts`` (Neo project),
keyed by the canonical page UUID so it joins ``meeting_transcripts.page_id``.
The branch must win an atomic claim (``claim_post``) before posting the LP
note, and writes the terminal outcome back afterwards (``record_outcome``).

Claim semantics:
- Insert with ``Prefer: resolution=ignore-duplicates,return=representation``
  is the atomic primitive — a non-empty response means this invocation owns
  the post.
- ``posted`` / ``skipped_*`` rows are terminal: never re-posted.
- ``failed`` rows and stale ``claimed`` rows (older than
  ``STALE_CLAIM_MINUTES`` — a crashed run) are re-claimed via a conditional
  PATCH whose WHERE filter is the server-side concurrency guard.
- **Fail closed**: any Supabase error → log ERROR → no claim → no post. The
  next pipeline retry of the page tries again. Never post without a claim.

Re-uses the Supabase HTTP helpers from ``canonical_mirror_sync`` (stdlib
urllib, service-role key from env) — the established convention across the
hierarchy sub-syncs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from src.fundraising.outcome import FundraisingOutcome, FundraisingStatus
from src.hierarchy.canonical_mirror_sync import _http

logger = logging.getLogger(__name__)

# A 'claimed' row older than this is treated as a crashed run and re-claimed.
STALE_CLAIM_MINUTES = 45

_TABLE = "/rest/v1/affinity_meeting_posts"

_STATUS_MAP: dict[FundraisingStatus, str] = {
    FundraisingStatus.POSTED: "posted",
    FundraisingStatus.SKIPPED_NO_EXTERNAL_ATTENDEES: "skipped_no_external_attendees",
    FundraisingStatus.SKIPPED_NO_LP_MATCH: "skipped_no_lp_match",
    FundraisingStatus.FAILED_API_ERROR: "failed",
}

_TERMINAL_STATUSES = frozenset(
    {"posted", "skipped_no_external_attendees", "skipped_no_lp_match"},
)

# Keep `detail` audit-friendly without ballooning rows on pathological errors.
_DETAIL_MAX_CHARS = 2000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_ts(value: Any) -> datetime | None:
    """Parse a PostgREST timestamptz string; None on anything unexpected."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _reclaim(page_id: str, filters: str, attempts: int) -> bool:
    """Conditional re-claim PATCH. The WHERE filter (``filters``) is the
    concurrency guard — PostgREST applies it server-side, so the row
    transitions exactly once under a race. Returns True iff we won."""
    rows = _http(
        "PATCH",
        f"{_TABLE}?page_id=eq.{page_id}&{filters}",
        body={
            "status": "claimed",
            "claimed_at": _iso(_now()),
            "attempts": attempts + 1,
            "completed_at": None,
        },
        prefer="return=representation",
    )
    return bool(rows)


def claim_post(
    *,
    page_id: str,
    db_id: str | None,
    owner_name: str,
    include_transcript: bool,
) -> bool:
    """Atomically claim the right to post this page to Affinity.

    Returns True iff THIS invocation now owns the post (caller may proceed
    to ``write_to_affinity``). False when the row is terminal (already
    posted / skipped), freshly claimed by another invocation, or Supabase
    is unreachable (fail closed). Never raises.
    """
    try:
        # Insert-claim: non-empty response = we created the row = we own it.
        inserted = _http(
            "POST",
            f"{_TABLE}?on_conflict=page_id",
            body=[{
                "page_id": page_id,
                "db_id": db_id,
                "owner_name": owner_name or None,
                "status": "claimed",
                "include_transcript": include_transcript,
                "claimed_at": _iso(_now()),
                "attempts": 1,
            }],
            prefer="resolution=ignore-duplicates,return=representation",
        )
        if inserted:
            return True

        # Lost the insert — a row exists. Decide by its status.
        rows = _http("GET", f"{_TABLE}?page_id=eq.{page_id}&select=*")
        if not rows:
            # Row vanished between insert and GET (manual delete?) — be
            # conservative and skip; the next pipeline retry re-claims.
            logger.error(
                "affinity claim: page=%s lost insert but row not found — skipping",
                page_id,
            )
            return False
        row = rows[0]
        status = row.get("status")
        attempts = int(row.get("attempts") or 1)

        if status in _TERMINAL_STATUSES:
            logger.info(
                "affinity claim: page=%s already %s — terminal, skipping",
                page_id, status,
            )
            return False

        if status == "failed":
            won = _reclaim(page_id, "status=eq.failed", attempts)
            if won:
                logger.info(
                    "affinity claim: page=%s re-claimed failed row (attempt %d)",
                    page_id, attempts + 1,
                )
            return won

        if status == "claimed":
            claimed_at = _parse_ts(row.get("claimed_at"))
            cutoff = _now() - timedelta(minutes=STALE_CLAIM_MINUTES)
            if claimed_at is not None and claimed_at >= cutoff:
                logger.info(
                    "affinity claim: page=%s claimed by another invocation "
                    "at %s — skipping",
                    page_id, row.get("claimed_at"),
                )
                return False
            # Stale (or unparseable timestamp): treat as a crashed run.
            won = _reclaim(
                page_id,
                f"status=eq.claimed&claimed_at=lt.{_iso(cutoff)}",
                attempts,
            )
            if won:
                logger.warning(
                    "affinity claim: page=%s re-claimed stale claim from %s "
                    "(attempt %d) — prior run likely crashed mid-post; a "
                    "duplicate note is possible",
                    page_id, row.get("claimed_at"), attempts + 1,
                )
            return won

        logger.error(
            "affinity claim: page=%s row has unknown status %r — skipping",
            page_id, status,
        )
        return False
    except Exception:
        # Fail closed: no confirmed claim → no post this run.
        logger.exception(
            "affinity claim: Supabase error for page=%s — failing closed "
            "(no Affinity post this run)",
            page_id,
        )
        return False


def record_outcome(
    *,
    page_id: str,
    outcome: FundraisingOutcome,
    opportunity_ids: list[int] | None = None,
) -> None:
    """Write the terminal status back to the claimed row. Never raises.

    On write-back failure the row stays ``claimed``; after
    ``STALE_CLAIM_MINUTES`` a later run may re-claim and re-post — the one
    residual duplicate window, accepted by design.
    """
    try:
        _http(
            "PATCH",
            f"{_TABLE}?page_id=eq.{page_id}",
            body={
                "status": _STATUS_MAP[outcome.status],
                "detail": (outcome.detail or "")[:_DETAIL_MAX_CHARS] or None,
                "completed_at": _iso(_now()),
                "opportunity_ids": opportunity_ids or None,
            },
        )
    except Exception:
        logger.exception(
            "affinity claim: failed to record outcome page=%s status=%s — "
            "row stays 'claimed'; a stale-claim retry after %d min may "
            "duplicate the note",
            page_id, outcome.status.value, STALE_CLAIM_MINUTES,
        )


__all__ = ["STALE_CLAIM_MINUTES", "claim_post", "record_outcome"]
