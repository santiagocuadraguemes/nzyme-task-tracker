"""Resolve a single LP Funnel list_entry_id from meeting attendees.

The LP Funnel (list 168609) is an *opportunity* list — each list entry's
``entity.id`` IS an opportunity id. We match attendees by pulling
``opportunity_ids`` off the Affinity person record and intersecting with the
funnel's opportunity index.

Strategy (stop at first confident match):
    1. Extract external attendee emails (drop kiboventures.com).
    2. For each email: search Affinity persons (with ``with_opportunities``);
       intersect their ``opportunity_ids`` with the LP Funnel index.
    3. Domain fallback — search persons by ``@domain`` and intersect the same
       way (catches LPs whose primary contact wasn't invited).
    4. Return the matching ``list_entry_id`` ONLY if exactly one distinct LP
       matches. On 0 or >1, log and return None (caller skips the Affinity
       write).
"""
from __future__ import annotations

import logging
from typing import Any

from src.affinity_client import AffinityClient, AffinityError

logger = logging.getLogger(__name__)

INTERNAL_DOMAINS = frozenset({"kiboventures.com"})


def _extract_emails(attendees: list[dict[str, Any]]) -> list[str]:
    """Pull email addresses from pipeline attendee dicts.

    Handles both GCal-resolved attendees (``id`` is an email) and raw
    ``email`` fields if a future attendee source supplies them.
    """
    emails: list[str] = []
    for att in attendees or []:
        candidate = att.get("email") or att.get("id") or ""
        if "@" in candidate:
            emails.append(candidate.lower())
    return emails


def _drop_internal(emails: list[str]) -> list[str]:
    out: list[str] = []
    for email in emails:
        domain = email.rsplit("@", 1)[-1]
        if domain not in INTERNAL_DOMAINS:
            out.append(email)
    return out


def _unique_domains(emails: list[str]) -> list[str]:
    seen: list[str] = []
    for email in emails:
        domain = email.rsplit("@", 1)[-1]
        if domain and domain not in seen:
            seen.append(domain)
    return seen


def build_lp_entity_index(
    client: AffinityClient, list_id: int,
) -> dict[int, int]:
    """Return ``{opportunity_id: list_entry_id}`` for the LP Funnel.

    Each list entry on an opportunity list has ``entity.id`` set to the
    opportunity id. That's the key we match attendee ``opportunity_ids``
    against.
    """
    entries = client.list_list_entries(list_id)
    index: dict[int, int] = {}
    for entry in entries:
        entry_id = entry.get("id")
        entity = entry.get("entity") or {}
        opportunity_id = entity.get("id") or entry.get("entity_id")
        if entry_id is not None and opportunity_id is not None:
            index[opportunity_id] = entry_id
    logger.info(
        "LP Funnel index built: %d opportunities → %d list entries",
        len(index), len({v for v in index.values()}),
    )
    return index


def _match_via_persons(
    persons: list[dict[str, Any]],
    lp_entity_index: dict[int, int],
    *,
    log_prefix: str,
) -> tuple[set[int], list[str]]:
    """Intersect each person's ``opportunity_ids`` with the LP Funnel index."""
    entries: set[int] = set()
    logs: list[str] = []
    for person in persons:
        for opp_id in person.get("opportunity_ids") or []:
            entry_id = lp_entity_index.get(opp_id)
            if entry_id is not None:
                entries.add(entry_id)
                logs.append(f"{log_prefix}→opp:{opp_id}→entry:{entry_id}")
    return entries, logs


def resolve_lp_list_entry(
    client: AffinityClient,
    *,
    attendees: list[dict[str, Any]],
    lp_entity_index: dict[int, int],
) -> int | None:
    """Return the list_entry_id for exactly one matching LP, else None."""
    emails = _drop_internal(_extract_emails(attendees))
    if not emails:
        logger.info("LP match skipped: no external attendee emails")
        return None

    candidate_entries: set[int] = set()
    candidate_logs: list[str] = []

    # Phase 1: email → person → opportunity_ids ∩ LP Funnel
    for email in emails:
        try:
            persons = client.search_persons(email)
        except AffinityError:
            logger.warning("Affinity person search failed for %s", email, exc_info=True)
            continue
        entries, logs = _match_via_persons(
            persons, lp_entity_index, log_prefix=f"email:{email}",
        )
        if entries:
            candidate_entries |= entries
            candidate_logs.extend(logs)
            logger.info(
                "LP match: email %s → LP entries %s", email, sorted(entries),
            )
        else:
            logger.info("LP match: email %s → no LP match (ignored)", email)

    # Phase 2: domain → persons @ that domain → opportunity_ids ∩ LP Funnel
    if not candidate_entries:
        for domain in _unique_domains(emails):
            term = f"@{domain}"
            try:
                persons = client.search_persons(term)
            except AffinityError:
                logger.warning("Affinity person search failed for %s", term, exc_info=True)
                continue
            entries, logs = _match_via_persons(
                persons, lp_entity_index, log_prefix=f"domain:{domain}",
            )
            if entries:
                candidate_entries |= entries
                candidate_logs.extend(logs)

    if not candidate_entries:
        logger.info(
            "LP match: no candidates for emails=%s (not on the LP Funnel)", emails,
        )
        return None
    if len(candidate_entries) > 1:
        logger.warning(
            "LP match: %d candidate entries for emails=%s — skipping write. Matches: %s",
            len(candidate_entries), emails, candidate_logs,
        )
        return None

    match = next(iter(candidate_entries))
    logger.info("LP match: single LP found (list_entry_id=%d) via %s", match, candidate_logs)
    return match


__all__ = ["resolve_lp_list_entry", "build_lp_entity_index"]
