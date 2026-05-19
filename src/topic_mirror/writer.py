"""Mirror writer: clone first-contributor pages, merge notes for the rest.

First contributor → Notion ``POST /v1/pages`` with
``template: {type: 'template_id', template_id: <src>}`` — clones the
AI-managed ``meeting_notes`` block (transcript, AI Summary, attendees,
the contributor's ``## Notes``) into the target DB. Verified empirically
in ``scripts/replicate_meeting.py``.

Subsequent contributors → query the target DB for an existing mirror
matching the meeting's title + date. If the contributor isn't already
in ``Contributors``, fetch their ``## Notes`` content from THEIR source
page, append a ``### <Name>'s Notes`` heading inside the mirror's
notes_block_id, then add them to ``Contributors``.

Known design trade-off (Option B with append-only):
  - Notion's API has no atomic prepend, so the FIRST contributor's notes
    stay unlabeled inside ``## Notes`` (they are the only thing there at
    clone time). Second-and-later contributors get labeled ``### <Name>'s
    Notes`` H3 sections appended to the same notes_block_id container.
  - The asymmetry is intentional. Symmetric labeling would require
    deleting + re-creating the AI-cloned content, which is destructive.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from notion_client import APIResponseError

from src.notion_client_wrapper import NotionClientWrapper
from src.topic_mirror.notes_extractor import fetch_notes_blocks_for_clone
from src.topic_mirror.outcome import MirrorAction
from src.topic_mirror.route_registry import Route
from src.transcript_pipeline.fetch_transcript import find_meeting_notes_block

logger = logging.getLogger(__name__)

# How long to wait for the async clone to populate its meeting_notes block
# before giving up on appending a subsequent contributor's notes. The clone
# returns immediately but Notion fills the meeting_notes block over ~5-10 s.
_NOTES_BLOCK_POLL_ATTEMPTS = 6
_NOTES_BLOCK_POLL_DELAY_SECONDS = 2.0


def _normalize_title(title: str) -> str:
    """Strip Notion's duplicate ``(N)`` suffix and lower-case for matching.

    Mirrors the normalization used by ``_meeting_fingerprint`` in pipeline.py.
    """
    return re.sub(r"\s*\(\d+\)\s*$", "", title).strip().lower()


def _date_only(date_str: str) -> str:
    """Return the YYYY-MM-DD prefix of an ISO date or datetime string."""
    return date_str[:10] if date_str and len(date_str) >= 10 else ""


def _source_date_value(source_page: dict) -> dict | None:
    """Read the source page's Date property as a write-shape dict.

    Falls back to ``created_time`` (date-only) when the Date property is
    empty — same fallback the rest of the pipeline uses for the meeting
    fingerprint and GCal lookup.
    """
    prop = (source_page.get("properties") or {}).get("Date", {})
    if prop.get("type") == "date":
        d = prop.get("date") or None
        if d and d.get("start"):
            out: dict[str, Any] = {"start": d["start"]}
            if d.get("end"):
                out["end"] = d["end"]
            if d.get("time_zone"):
                out["time_zone"] = d["time_zone"]
            return out
    created_time = source_page.get("created_time", "")
    if created_time:
        return {"start": _date_only(created_time)}
    return None


def _title_plain_text(prop: dict) -> str:
    if not prop or prop.get("type") != "title":
        return ""
    return "".join(p.get("plain_text", "") for p in prop.get("title", []) or [])


def find_existing_mirror(
    client: NotionClientWrapper,
    target_db_id: str,
    source_title: str,
    source_date: str,
) -> dict | None:
    """Find a mirror page in *target_db_id* whose title+date matches the source.

    Notion's date filter ``equals`` compares date-only even when the stored
    value is a datetime, so passing the YYYY-MM-DD prefix works for both
    representations. We filter on date first to narrow the result set, then
    normalize and compare titles in Python (Notion has no fuzzy/string-
    normalised title filter).
    """
    date_filter = _date_only(source_date)
    if not date_filter:
        # Without a date we can't safely dedup — fall back to scanning by
        # title only. This would be expensive on a large mirror DB, so log
        # a warning so we notice if it happens in practice.
        logger.warning(
            "find_existing_mirror called with empty source_date — "
            "scanning entire target_db %s by title", target_db_id[:8],
        )
        response = client.query_database(database_id=target_db_id)
    else:
        response = client.query_database(
            database_id=target_db_id,
            filter={"property": "Date", "date": {"equals": date_filter}},
        )

    target = _normalize_title(source_title)
    for page in response.get("results", []):
        title = _title_plain_text((page.get("properties") or {}).get("Meeting", {}))
        if _normalize_title(title) == target:
            return page
    return None


def _build_clone_properties(
    source_page: dict, source_title: str, owner_user_id: str,
) -> dict[str, Any]:
    """Properties to set on the new mirror page at clone time.

    Empirically (verified 2026-05-18 against API 2026-03-11) the
    ``template_id`` mechanism clones the page BODY — meeting_notes block,
    transcript, AI Summary block, notes — but does NOT carry over the
    source's database property VALUES. So every property the target DB
    expects must be re-passed here, even if the destination column exists.
    Properties absent from the destination schema are still silently
    dropped, which is what lets us omit pipeline-control columns
    (``Processed``, ``Processing``, ``Task - Relation``, etc.).

    ``owner_user_id`` is the Notion user UUID of the first contributor —
    written to the ``Owner`` people property. When empty, Owner is left
    unset rather than failing the clone.
    """
    src_props = source_page.get("properties") or {}

    properties: dict[str, Any] = {
        "Meeting": {"title": [{"type": "text", "text": {"content": source_title or "(untitled)"}}]},
    }

    if owner_user_id:
        properties["Owner"] = {"people": [{"id": owner_user_id}]}

    date_val = _source_date_value(source_page)
    if date_val:
        properties["Date"] = {"date": date_val}

    source_url = source_page.get("url") or (
        f"https://www.notion.so/{source_page.get('id', '').replace('-', '')}"
    )
    if source_url:
        properties["Primary Source URL"] = {"url": source_url}

    # Meeting type (select) — single value.
    mt = (src_props.get("Meeting type") or {}).get("select")
    if mt and mt.get("name"):
        properties["Meeting type"] = {"select": {"name": mt["name"]}}

    # Detail (multi_select) — zero or more values.
    detail_items = (src_props.get("Detail") or {}).get("multi_select") or []
    detail_names = [it["name"] for it in detail_items if it.get("name")]
    if detail_names:
        properties["Detail"] = {"multi_select": [{"name": n} for n in detail_names]}

    # External Org (select).
    eo = (src_props.get("External Org") or {}).get("select")
    if eo and eo.get("name"):
        properties["External Org"] = {"select": {"name": eo["name"]}}

    # AI Summary (rich_text) — auto-populated by Notion AI on the source.
    # Re-passed so the mirror shows a value immediately; Notion AI may
    # later regenerate it from the cloned meeting_notes block.
    ai_summary_items = (src_props.get("AI Summary") or {}).get("rich_text") or []
    if ai_summary_items:
        properties["AI Summary"] = {
            "rich_text": [
                {"type": "text", "text": {"content": rt.get("plain_text", "")}}
                for rt in ai_summary_items
            ],
        }

    # Governance: Edit & View Access (people) — copied straight from source
    # so the mirror inherits the same access list. People IDs are stable
    # workspace-wide, so the same user can be written into multiple DBs.
    gov_people = (src_props.get("Governance: Edit & View Access") or {}).get("people") or []
    gov_ids = [p["id"] for p in gov_people if p.get("id")]
    if gov_ids:
        properties["Governance: Edit & View Access"] = {
            "people": [{"id": pid} for pid in gov_ids],
        }

    return properties


def _clone_into_target(
    client: NotionClientWrapper,
    source_page: dict,
    target_db_id: str,
    properties: dict[str, Any],
) -> dict:
    """Call ``pages.create`` with ``template: {type: 'template_id', ...}``.

    Properties not declared on the target schema are silently dropped by
    Notion — that's the entire reason this feature works. Pipeline-control
    columns (``Processed``, ``Processing``, ``Template Injected``,
    ``Task - Relation``, ``LP Emails``) intentionally don't exist on the
    target DB.
    """
    template = {"type": "template_id", "template_id": source_page["id"]}
    return client._call_with_retry(
        client._client.pages.create,
        parent={"database_id": target_db_id},
        properties=properties,
        template=template,
    )


def _read_owner_ids(mirror_page: dict) -> list[str]:
    """Return the list of Notion user UUIDs currently in the mirror's Owner."""
    prop = (mirror_page.get("properties") or {}).get("Owner", {}) or {}
    if prop.get("type") != "people":
        return []
    return [p["id"] for p in prop.get("people") or [] if p.get("id")]


def _find_mirror_notes_block_id(
    client: NotionClientWrapper, mirror_page_id: str,
) -> str | None:
    """Locate the mirror's ``meeting_notes.children.notes_block_id``.

    The template clone is async — Notion populates the meeting_notes block
    over ~5–10 s. We poll for up to roughly 12 seconds before giving up;
    if still missing, the caller logs a warning and skips the append.
    Member 2's notes are then lost for that meeting — acceptable for v1
    given how rare it is for two pages on the same meeting to be processed
    inside the same cron tick.
    """
    for attempt in range(1, _NOTES_BLOCK_POLL_ATTEMPTS + 1):
        try:
            blocks = client.get_block_children(mirror_page_id)
        except APIResponseError as e:
            logger.warning(
                "Failed to read mirror %s blocks (attempt %d/%d): %s",
                mirror_page_id[:8], attempt, _NOTES_BLOCK_POLL_ATTEMPTS, e,
            )
            return None
        mn_block = find_meeting_notes_block(blocks)
        if mn_block is not None:
            notes_block_id = (
                mn_block.get("meeting_notes", {})
                .get("children", {})
                .get("notes_block_id")
            )
            if notes_block_id:
                return notes_block_id
        if attempt < _NOTES_BLOCK_POLL_ATTEMPTS:
            time.sleep(_NOTES_BLOCK_POLL_DELAY_SECONDS)
    return None


def _build_contributor_heading(contributor: str) -> dict[str, Any]:
    """Build the ``### <Name>'s Notes`` H3 heading block (create-format)."""
    return {
        "object": "block",
        "type": "heading_3",
        "heading_3": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {"content": f"{contributor}'s Notes"},
                },
            ],
        },
    }


def _append_contributor_notes(
    client: NotionClientWrapper,
    mirror_page_id: str,
    contributor: str,
    notes_blocks: list[dict[str, Any]],
) -> bool:
    """Append ``### <Name>'s Notes`` + their notes content inside notes_block_id.

    Returns True if blocks were appended, False if the mirror's
    notes_block_id couldn't be found (async clone still in flight after
    the poll budget — the contributor is NOT added to Contributors so
    a future re-run can retry).
    """
    notes_block_id = _find_mirror_notes_block_id(client, mirror_page_id)
    if not notes_block_id:
        logger.warning(
            "Mirror %s has no notes_block_id after %d polls — skipping notes append",
            mirror_page_id[:8], _NOTES_BLOCK_POLL_ATTEMPTS,
        )
        return False

    children = [_build_contributor_heading(contributor), *notes_blocks]
    client.append_block_children(block_id=notes_block_id, children=children)
    return True


def _update_owners(
    client: NotionClientWrapper,
    mirror_page_id: str,
    current_ids: list[str],
    new_owner_id: str,
) -> None:
    """Add *new_owner_id* to the mirror's ``Owner`` people property."""
    if not new_owner_id:
        return
    ids = list(dict.fromkeys([*current_ids, new_owner_id]))
    client.update_page(
        page_id=mirror_page_id,
        properties={
            "Owner": {"people": [{"id": uid} for uid in ids]},
        },
    )


def clone_or_merge(
    *,
    client: NotionClientWrapper,
    route: Route,
    source_page: dict,
    source_title: str,
    source_date: str,
    owner_user_id: str,
    owner_name: str,
) -> MirrorAction:
    """Mirror *source_page* into *route.target_db_id*.

    First contributor → ``MirrorAction.CLONED`` (writes ``Owner`` people).
    Subsequent contributor with notes → ``MirrorAction.MERGED``
    (appends ``### <owner_name>'s Notes`` H3 + content inside the mirror's
    notes_block_id, adds ``owner_user_id`` to ``Owner``).
    Owner already in the Owner list, or no notes to merge → ``MirrorAction.NOOP``.

    *owner_user_id* is the Notion user UUID used for the Owner people field;
    *owner_name* is the display name used for the appended heading. Pass
    both: the UUID is the dedup key, but the name is what readers see.

    Raises ``APIResponseError`` only for unexpected failures (the caller
    catches and converts to a failed-route entry in the outcome).
    """
    existing = find_existing_mirror(client, route.target_db_id, source_title, source_date)
    if existing is None:
        properties = _build_clone_properties(source_page, source_title, owner_user_id)
        mirror = _clone_into_target(client, source_page, route.target_db_id, properties)
        logger.info(
            "Cloned page %s → mirror %s (route=%s owner=%s)",
            source_page.get("id", "?")[:8],
            mirror.get("id", "?")[:8],
            route.label,
            owner_name or owner_user_id[:8],
        )
        return MirrorAction.CLONED

    current_owner_ids = _read_owner_ids(existing)
    if owner_user_id and owner_user_id in current_owner_ids:
        logger.debug(
            "Mirror %s already has owner %s (route=%s) — skipping merge",
            existing.get("id", "?")[:8], owner_name or owner_user_id[:8], route.label,
        )
        return MirrorAction.NOOP

    # Pull just this contributor's `## Notes` content from the source page.
    notes_blocks = fetch_notes_blocks_for_clone(client, source_page["id"])
    if not notes_blocks:
        logger.info(
            "Mirror %s: contributor %r has no '## Notes' content to merge "
            "(route=%s); adding to Owner anyway",
            existing.get("id", "?")[:8], owner_name or "?", route.label,
        )
        _update_owners(client, existing["id"], current_owner_ids, owner_user_id)
        return MirrorAction.NOOP

    appended = _append_contributor_notes(
        client, existing["id"], owner_name or "Unknown", notes_blocks,
    )
    if not appended:
        # Mirror not yet populated — don't update Owner so a manual re-run
        # (Processed=false) gets another shot at the merge.
        return MirrorAction.NOOP

    _update_owners(client, existing["id"], current_owner_ids, owner_user_id)
    logger.info(
        "Merged contributor %r notes into mirror %s (route=%s, %d block(s))",
        owner_name or "?", existing.get("id", "?")[:8], route.label, len(notes_blocks),
    )
    return MirrorAction.MERGED


__all__ = [
    "clone_or_merge",
    "find_existing_mirror",
]
