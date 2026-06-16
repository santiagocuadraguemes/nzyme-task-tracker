"""Meeting attendee resolution (GCal → Notion → governance fallback chain).

Carved out of ``src.pipeline`` when task extraction moved to the standalone
``nzyme-task-extraction`` project (2026-06-15). The Notion → Supabase sync
path (``src.meeting_row.extract_row``) is the sole in-repo consumer; it calls
``_resolve_attendees`` to populate the ``attendee_emails`` mirror column.

These helpers only depend on the kept transcript-pipeline modules
(``fetch_transcript`` for attendee/governance extraction, ``gcal_attendees``
for the Google Calendar lookup) — none of the deleted extraction code.
"""
from __future__ import annotations

import logging

from src.config import SyncConfig
from src.notion_client_wrapper import NotionClientWrapper
from src.transcript_pipeline.fetch_transcript import (
    extract_attendee_ids,
    extract_governance_attendees,
)

logger = logging.getLogger(__name__)


def _resolve_delegated_user(
    client: NotionClientWrapper,
    created_by_id: str,
    default_email: str | None,
) -> str | None:
    """Resolve the meeting page's Notion creator to a Workspace email.

    Returns the creator's email when retrievable, else `default_email`.
    The returned email is used to impersonate a Workspace user when calling
    Google Calendar via the service account.
    """
    if created_by_id:
        try:
            user = client.retrieve_user(created_by_id)
            email = (user.get("person") or {}).get("email") or ""
            if email:
                return email.strip().lower()
            logger.info(
                "Notion user %s has no email (bot or integration-gated); using default %s",
                created_by_id, default_email,
            )
        except Exception:
            logger.warning(
                "Failed to retrieve Notion user %s; using default %s",
                created_by_id, default_email, exc_info=True,
            )
    return default_email


def _gcal_impersonation_target(
    delegated_user: str, config: SyncConfig,
) -> tuple[str, str]:
    """Resolve ``(impersonate_email, calendar_id)`` for a GCal lookup.

    Domain-wide delegation can only impersonate in-domain users. When the
    meeting owner's email is in an out-of-domain set (``gcal_proxy_domains``)
    and a proxy is configured, impersonate the in-domain proxy and read the
    owner's calendar by id (the proxy must have "see all event details" access).
    Everyone else impersonates themselves and reads their own ``"primary"``.
    """
    domain = delegated_user.rsplit("@", 1)[-1].lower()
    if domain in config.gcal_proxy_domains and config.gcal_proxy_delegated_user:
        return config.gcal_proxy_delegated_user, delegated_user
    return delegated_user, "primary"


def _enrich_attendee_names(
    attendees: list[dict[str, str]],
    org_chart_rows: list[dict] | None,
) -> list[dict[str, str]]:
    """Replace email-prefix placeholder names with Org Chart full names when available."""
    if not org_chart_rows:
        return attendees
    email_to_name = {
        row["email"]: row["name"]
        for row in org_chart_rows
        if row.get("email") and row.get("name")
    }
    enriched: list[dict[str, str]] = []
    for att in attendees:
        email = (att.get("email") or "").lower()
        if email and email in email_to_name:
            enriched.append({**att, "name": email_to_name[email]})
        else:
            enriched.append(att)
    return enriched


def _resolve_attendees(
    client: NotionClientWrapper,
    config: SyncConfig,
    mn_block: dict | None,
    page: dict,
    metadata: dict,
    *,
    org_chart_rows: list[dict] | None = None,
) -> list[dict[str, str]]:
    """Resolve meeting attendees via the priority chain.

    Priority:
    1. Google Calendar (when `config.gcal_enabled`) — impersonates the page's
       Notion creator via service account; falls back to default delegated
       user if the creator's email can't be resolved.
    2. Notion meeting_notes.calendar_event.attendees.
    3. Page's "Governance: Edit & View Access" people property.

    Returns list of {"id", "name", optional "email"} dicts. Notion-source
    attendees now also carry `email` (pulled from the workspace user
    record) so the org-chart enrichment step works regardless of whether
    GCal returned an event.
    """
    attendees: list[dict[str, str]] = []

    # Source 2: Notion meeting_notes attendees
    if mn_block is not None:
        attendee_ids = extract_attendee_ids(mn_block)
        if attendee_ids:
            workspace_users = {u.get("id"): u for u in client.list_users()}
            for uid in attendee_ids:
                u = workspace_users.get(uid, {})
                person = u.get("person") or {}
                email = (person.get("email") or "").strip().lower() or None
                name = u.get("name") or email or uid
                attendees.append({"id": uid, "name": name, "email": email})
            # Email-based canonical-name lookup against the Org Chart — same
            # step the GCal branch already runs. Without this, Notion display
            # names like "reyes" (lowercase email prefix) never resolve to
            # the org chart's "Reyes Rubio" entry.
            if org_chart_rows:
                attendees = _enrich_attendee_names(attendees, org_chart_rows)

    # Source 1: Google Calendar (overrides Notion when an event matches)
    gcal_ready = config.gcal_enabled and metadata.get("title") and metadata.get("date")
    if gcal_ready:
        try:
            from src.transcript_pipeline.gcal_attendees import get_gcal_attendees

            created_by_id = (metadata.get("created_by") or {}).get("id", "")
            delegated_user = _resolve_delegated_user(
                client, created_by_id, config.gcal_delegated_user_default,
            )
            if not delegated_user:
                logger.warning(
                    "No Workspace user to impersonate for GCal lookup — skipping",
                )
            else:
                # Domain-wide delegation can't impersonate out-of-domain owners
                # (e.g. nzalpha.com) — those are read via an in-domain proxy
                # against the owner's calendar id. In-domain owners read their
                # own "primary".
                impersonate, calendar_id = _gcal_impersonation_target(
                    delegated_user, config,
                )
                logger.info(
                    "GCal lookup for owner=%s (impersonating %s, calendar=%s)",
                    delegated_user, impersonate, calendar_id,
                )
                gcal_attendees = get_gcal_attendees(
                    metadata["title"], metadata["date"], impersonate,
                    calendar_id=calendar_id,
                )
                if gcal_attendees:
                    attendees = [
                        {"id": ga["email"], "name": ga["name"], "email": ga["email"]}
                        for ga in gcal_attendees
                    ]
                    attendees = _enrich_attendee_names(attendees, org_chart_rows)
                    logger.info("GCal attendees resolved: %d", len(attendees))
        except Exception:
            logger.warning("GCal lookup failed — using Notion attendees", exc_info=True)
    else:
        reasons = []
        if not config.gcal_enabled:
            reasons.append("gcal_enabled=False")
        if not metadata.get("title"):
            reasons.append("no title")
        if not metadata.get("date"):
            reasons.append("no date")
        logger.debug("GCal lookup skipped: %s", ", ".join(reasons))

    # Source 3: Governance fallback
    if not attendees:
        governance = extract_governance_attendees(page)
        if governance:
            attendees = [{**g, "email": None} for g in governance]
            logger.debug(
                "Using governance-access fallback (%d attendees)", len(attendees),
            )

    return attendees
