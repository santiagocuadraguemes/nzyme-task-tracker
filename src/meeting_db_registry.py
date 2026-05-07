"""Discover per-member Meeting Notes databases via the Org Chart.

Each active row in the Nzyme Org Chart has a "Meeting Notes DB" URL property
pointing at that member's personal Meeting Notes database. Treating the Org
Chart as the single source of truth means joiners and leavers are managed
entirely in Notion: filling in the URL on an active row brings a member's DB
online; clearing it (or flipping `Active = false`) takes it offline. No code
change or redeploy.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from src.config import SyncConfig
from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)

MEETING_DB_PROPERTY = "Meeting Notes DB"
AUTO_EXTRACT_TASKS_PROPERTY = "Auto-extract Tasks"

# Notion URLs end in a 32-char hex UUID, optionally preceded by a slug + dash.
_NOTION_URL_RE = re.compile(
    r"(?:notion\.so|notion\.site)/(?:[^/?#]+/)?(?:[^/?#]*-)?([0-9a-fA-F]{32})"
)


@dataclass(frozen=True)
class MeetingDB:
    """A per-member Meeting Notes database discovered from the Org Chart."""

    db_id: str          # Hyphenated UUID (Notion API format)
    owner_name: str     # Full name from Org Chart row
    owner_email: str    # Lowercased; empty when the row has no email set
    # When True, run the full transcript pipeline (correct → extract →
    # classify → write). When False, parse `## Action Items` bullets
    # verbatim and resolve assignees deterministically. Defaults to False
    # when the column is missing or unset on the Org Chart row — the MVP
    # deploy expects every member on the literal-notes path.
    auto_extract_tasks: bool = False


def _extract_db_id(url: str) -> str | None:
    """Extract the 32-char hex from a Notion URL, returned as a canonical UUID."""
    if not url:
        return None
    m = _NOTION_URL_RE.search(url)
    if not m:
        return None
    raw = m.group(1).lower()
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def _normalize_db_id(db_id: str) -> str:
    """Strip dashes for stable comparison across Notion API surface forms."""
    return (db_id or "").replace("-", "").lower()


def discover_meeting_dbs(
    client: NotionClientWrapper, org_chart_db_id: str,
) -> list[MeetingDB]:
    """Return one MeetingDB per active Org Chart row with a parseable URL.

    Rows missing the URL or with a URL we can't parse are skipped (logged).
    Duplicate DB URLs across rows are skipped after the first match.
    """
    response = client.query_database(
        database_id=org_chart_db_id,
        filter={"property": "Active", "checkbox": {"equals": True}},
    )

    dbs: list[MeetingDB] = []
    seen_db_ids: set[str] = set()
    for row in response.get("results", []):
        props = row.get("properties", {})

        name = ""
        for prop in props.values():
            if prop.get("type") == "title":
                name = "".join(p.get("plain_text", "") for p in prop.get("title", []))
                break
        name = name.strip()

        url = (props.get(MEETING_DB_PROPERTY) or {}).get("url") or ""
        if not url:
            continue

        db_id = _extract_db_id(url)
        if not db_id:
            logger.warning(
                "Org Chart row '%s': couldn't parse DB ID from URL %r — skipping",
                name or "?", url,
            )
            continue
        norm = _normalize_db_id(db_id)
        if norm in seen_db_ids:
            logger.warning(
                "Org Chart row '%s' points at DB %s which is already claimed by "
                "an earlier row — skipping",
                name or "?", db_id,
            )
            continue
        seen_db_ids.add(norm)

        email = ""
        email_prop = props.get("Email") or {}
        if email_prop.get("email"):
            email = email_prop["email"].strip().lower()

        # Default False: missing column or unset checkbox routes the row
        # through the literal-notes path (MVP-wide setting).
        auto_extract = False
        ae_prop = props.get(AUTO_EXTRACT_TASKS_PROPERTY)
        if isinstance(ae_prop, dict) and ae_prop.get("type") == "checkbox":
            auto_extract = bool(ae_prop.get("checkbox"))

        dbs.append(MeetingDB(
            db_id=db_id,
            owner_name=name,
            owner_email=email,
            auto_extract_tasks=auto_extract,
        ))

    if dbs:
        logger.info(
            "Discovered %d active Meeting Notes DB(s) via Org Chart: %s",
            len(dbs), ", ".join(d.owner_name or "?" for d in dbs),
        )
    else:
        logger.warning(
            "Org Chart returned 0 active rows with a '%s' URL — no DBs to poll",
            MEETING_DB_PROPERTY,
        )
    return dbs


def load_registry(
    config: SyncConfig, client: NotionClientWrapper,
) -> list[MeetingDB]:
    """Return the active registry, honoring the single-DB override.

    When `MEETING_NOTES_DB_ID` is set in config, returns a one-entry registry
    using that DB (owner fields blank). Otherwise discovers from the Org
    Chart at `ORG_CHART_DB_ID`. Raises if neither is configured.
    """
    if config.meeting_notes_db_id:
        # Manual single-DB runs (dev / test) keep auto_extract_tasks=True so
        # the full transcript pipeline is exercised by default. Production
        # never sets MEETING_NOTES_DB_ID, so this branch doesn't affect
        # prod — the discover_meeting_dbs path below honours the dataclass
        # default (False) for missing/unset rows.
        return [MeetingDB(
            db_id=config.meeting_notes_db_id, owner_name="", owner_email="",
            auto_extract_tasks=True,
        )]
    if not config.org_chart_db_id:
        raise RuntimeError(
            "Neither MEETING_NOTES_DB_ID nor ORG_CHART_DB_ID is set — "
            "cannot resolve which Meeting Notes DB(s) to poll.",
        )
    return discover_meeting_dbs(client, config.org_chart_db_id)


def find_owner_for_page(
    registry: list[MeetingDB], page_database_id: str,
) -> MeetingDB | None:
    """Return the registry entry whose db_id matches the page's parent DB."""
    target = _normalize_db_id(page_database_id)
    if not target:
        return None
    for db in registry:
        if _normalize_db_id(db.db_id) == target:
            return db
    return None
