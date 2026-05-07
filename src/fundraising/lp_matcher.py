"""Resolve LP Funnel list_entry_ids from meeting attendees.

The LP Funnel (list 168609) is an *opportunity* list — each list entry's
``entity.id`` IS an opportunity id. We match attendees by pulling
``opportunity_ids`` off the Affinity person record and intersecting with the
funnel's opportunity index.

Strategy:
    1. Extract external attendee emails (drop kiboventures.com).
    2. For each email: search Affinity persons (with ``with_opportunities``);
       intersect their ``opportunity_ids`` with the LP Funnel index.
    3. Domain fallback — search persons by ``@domain`` and intersect the same
       way (catches LPs whose primary contact wasn't invited).
    4. Return *every* matching ``list_entry_id``. Two LPs in the same
       meeting → both get the note. The caller decides what to do with the
       empty-list case (no external attendees vs no LP match).
"""
from __future__ import annotations

import logging
from typing import Any

from src.affinity_client import AffinityClient, AffinityError

logger = logging.getLogger(__name__)

INTERNAL_DOMAINS = frozenset({"kiboventures.com"})


def extract_external_emails(attendees: list[dict[str, Any]]) -> list[str]:
    """Return external attendee emails (lowercased, kiboventures stripped).

    Public so the orchestrator can distinguish ``no external attendees`` from
    ``no LP match`` when shaping the outcome.
    """
    return _drop_internal(_extract_emails(attendees))


def _extract_emails(attendees: list[dict[str, Any]]) -> list[str]:
    """Pull email addresses from pipeline attendee dicts."""
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
    """Return ``{opportunity_id: list_entry_id}`` for the LP Funnel."""
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


def resolve_lp_list_entries(
    client: AffinityClient,
    *,
    attendees: list[dict[str, Any]],
    lp_entity_index: dict[int, int],
) -> list[int]:
    """Return every matching list_entry_id (sorted, deduped).

    Empty list = no LP match. Multi-LP meetings (e.g. an inter-LP intro) get
    the same note posted to each match.
    """
    emails = extract_external_emails(attendees)
    if not emails:
        logger.info("LP match skipped: no external attendee emails")
        return []

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
        return []

    matches = sorted(candidate_entries)
    if len(matches) == 1:
        logger.info("LP match: single LP found (list_entry_id=%d) via %s", matches[0], candidate_logs)
    else:
        logger.info(
            "LP match: %d LPs found (list_entry_ids=%s) — note will be posted to each. via %s",
            len(matches), matches, candidate_logs,
        )
    return matches


__all__ = [
    "build_lp_entity_index",
    "extract_external_emails",
    "resolve_lp_list_entries",
]
