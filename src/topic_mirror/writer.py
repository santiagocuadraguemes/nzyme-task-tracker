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
from src.transcript_pipeline.fetch_transcript import (
    extract_attendee_ids,
    find_meeting_notes_block,
    strip_title_datetime,
)

logger = logging.getLogger(__name__)

# How long to wait for the async clone to populate its meeting_notes block
# before giving up on appending a subsequent contributor's notes. The clone
# returns immediately but Notion fills the meeting_notes block over ~5-10 s.
_NOTES_BLOCK_POLL_ATTEMPTS = 6
_NOTES_BLOCK_POLL_DELAY_SECONDS = 2.0


def _normalize_title(title: str) -> str:
    """Strip the trailing ISO datetime + Notion's ``(N)`` suffix, lower-case.

    Stripping the datetime makes dedup robust across the title-cleanup change:
    a mirror stored before the cleanup (title carries the ISO suffix) still
    matches a freshly-cleaned source title, and vice-versa.
    """
    title = strip_title_datetime(title)
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
    source_page: dict,
    source_title: str,
    owner_user_id: str,
    internal_attendee_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Properties to set on the new mirror page at clone time.

    Empirically (verified 2026-05-18 against API 2026-03-11) the
    ``template_id`` mechanism clones the page BODY — meeting_notes block,
    transcript, AI Summary block, notes — but does NOT carry over the
    source's database property VALUES. So every property the target DB
    expects must be re-passed here, even if the destination column exists.

    Returns the FULL set of source-side properties; ``_filter_to_target_schema``
    drops the ones the target DB doesn't declare before the actual clone call.
    Notion 2026-03-11 rejects unknown property names on ``pages.create`` (e.g.
    ``"External Org is not a property that exists"``) — silent-drop is no
    longer the default, so we filter explicitly.

    ``owner_user_id`` is the Notion user UUID of the first contributor —
    written to the ``Owner`` people property. When empty, Owner is left
    unset rather than failing the clone.
    """
    src_props = source_page.get("properties") or {}

    # Notion auto-names meetings "<GCal title> <ISO datetime>" (e.g. "... modelo
    # 2026-05-29T14:00:00.000+02:00"). Strip the trailing datetime so the mirror
    # shows a clean title instead of the raw timestamp.
    clean_title = strip_title_datetime(source_title) or "(untitled)"
    properties: dict[str, Any] = {
        "Meeting": {"title": [{"type": "text", "text": {"content": clean_title}}]},
    }

    if owner_user_id:
        properties["Owner"] = {"people": [{"id": owner_user_id}]}

    # Internal attendees (people) — the meeting's Notion-member attendees.
    # Dropped by _filter_to_target_schema on DBs that don't declare the column.
    if internal_attendee_ids:
        properties["Internal attendees"] = {
            "people": [{"id": uid} for uid in internal_attendee_ids],
        }

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


# Notion property write-shape type keys we may emit from _build_clone_properties.
# Used to detect a write-value's type without baking it into each property's
# build site.
_WRITE_TYPE_KEYS = frozenset({
    "title", "rich_text", "select", "multi_select", "status", "people",
    "date", "url", "checkbox", "number", "relation", "files", "email",
    "phone_number",
})


def _write_value_type(write_value: dict[str, Any]) -> str | None:
    """Return the type key from a Notion write-shape property value.

    Example: ``{"select": {"name": "AI"}}`` → ``"select"``.
    Returns None when the shape isn't a recognised write value.
    """
    for k in write_value:
        if k in _WRITE_TYPE_KEYS:
            return k
    return None


def _filter_to_target_schema(
    properties: dict[str, Any], target_schema: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Drop properties that don't exist on *target_schema* or whose type
    doesn't match.

    The Meeting Mirrors landing DBs declare a narrow subset of the source's
    columns on purpose (see ``docs/meeting-mirrors.md``). Notion's API used
    to silently drop unknown property names on ``pages.create``, but the
    ``template_id`` clone path against API ``2026-03-11`` now rejects them
    (e.g. ``"External Org is not a property that exists"``), so we filter
    here.

    Returns (kept, dropped_names) so the caller can log what was discarded.
    """
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for name, value in properties.items():
        target = target_schema.get(name)
        if target is None:
            dropped.append(name)
            continue
        value_type = _write_value_type(value)
        target_type = target.get("type")
        if value_type and target_type and value_type != target_type:
            # Same name, different type — safer to drop than to send a value
            # Notion will reject. Caller logs it.
            dropped.append(f"{name}(type:{value_type}!={target_type})")
            continue
        kept[name] = value
    return kept, dropped


def _ensure_select_options_on_target(
    client: NotionClientWrapper,
    target_db_id: str,
    target_schema: dict[str, Any],
    properties_to_clone: dict[str, Any],
    source_props: dict[str, Any],
    route_label: str,
) -> None:
    """For each select / multi_select being cloned, add any option names
    missing from the target DB's schema — preserving the source's color.

    Notion's ``data_sources.update`` PATCH replaces the full options list,
    so we re-send existing options (with their ``id`` so they're preserved)
    plus the new option entries (no ``id`` = create). Source colors come
    from the source page's property response, which carries
    ``{name, id, color}`` inline for each selected option.

    Failures are logged but not raised — if the PATCH fails the subsequent
    clone will surface the real error (unknown option value) and the route
    is marked failed by the caller. Silent fallback isn't acceptable here
    (we'd lose data); loud-and-keep-going is.
    """
    patch_body: dict[str, Any] = {}

    for name, write_value in properties_to_clone.items():
        ptype = _write_value_type(write_value)
        if ptype not in {"select", "multi_select"}:
            continue
        target_prop = target_schema.get(name) or {}
        if target_prop.get("type") != ptype:
            continue

        existing_options = (target_prop.get(ptype) or {}).get("options", []) or []
        existing_names = {o.get("name") for o in existing_options if o.get("name")}

        # Pull desired option names + colors from the SOURCE page's property
        # response — that's where the color info lives. The write_value only
        # carries ``{name: ...}`` (no id, no color) because we built it.
        src_prop = source_props.get(name) or {}
        if ptype == "select":
            src_options = [src_prop.get("select")] if src_prop.get("select") else []
        else:
            src_options = src_prop.get("multi_select") or []

        new_entries: list[dict[str, Any]] = []
        seen_new: set[str] = set()
        for opt in src_options:
            if not opt:
                continue
            opt_name = opt.get("name")
            if not opt_name or opt_name in existing_names or opt_name in seen_new:
                continue
            entry: dict[str, Any] = {"name": opt_name}
            color = opt.get("color")
            if color:
                entry["color"] = color
            new_entries.append(entry)
            seen_new.add(opt_name)

        if not new_entries:
            continue

        full_options: list[dict[str, Any]] = []
        for o in existing_options:
            preserved: dict[str, Any] = {"name": o["name"]}
            if o.get("id"):
                preserved["id"] = o["id"]
            if o.get("color"):
                preserved["color"] = o["color"]
            full_options.append(preserved)
        full_options.extend(new_entries)

        patch_body[name] = {ptype: {"options": full_options}}
        logger.info(
            "Mirror %s: adding %d missing %s option(s) to target.%s: %s",
            route_label, len(new_entries), ptype, name,
            ", ".join(e["name"] for e in new_entries),
        )

    if not patch_body:
        return

    try:
        client.update_data_source(target_db_id, patch_body)
    except APIResponseError as e:
        logger.warning(
            "Mirror %s: failed to add missing select options to target DB %s: %s — "
            "clone will likely fail on those values",
            route_label, target_db_id[:8], e,
        )


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
    ``Task - Relation``) intentionally don't exist on the target DB.
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
    return _read_people_ids(mirror_page, "Owner")


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
    """Build the ``<Name>'s notes`` H3 label block (create-format).

    Blue background so each contributor's section is visually demarcated
    inside the shared notes container.
    """
    return {
        "object": "block",
        "type": "heading_3",
        "heading_3": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {"content": f"{contributor}'s notes"},
                },
            ],
            "color": "blue_background",
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


def _label_first_contributor_notes(
    client: NotionClientWrapper, mirror_page_id: str, contributor: str,
) -> bool:
    """Prepend ``<Name>'s notes`` (blue H3) at the top of the first
    contributor's notes container.

    The ``template_id`` clone carries the first contributor's entire notes
    container (Action Items + Notes + written notes) into the mirror unlabeled.
    We prepend their label H3 at the very top of that container — above their
    whole section — mirroring how the merge path appends later contributors'
    ``<Name>'s notes`` sections below. ``position: start`` doesn't depend on
    the notes heading text (which a member may rename, e.g. "Notes [TEST]"), so
    it's robust to template drift.

    Best-effort: the clone is async, so we poll until the notes container has
    populated before prepending. Returns False if it hasn't within the poll
    budget — the notes are still present, just unlabeled.
    """
    notes_block_id = _find_mirror_notes_block_id(client, mirror_page_id)
    if not notes_block_id:
        logger.warning(
            "Mirror %s has no notes_block_id after %d polls — skipping first "
            "contributor label", mirror_page_id[:8], _NOTES_BLOCK_POLL_ATTEMPTS,
        )
        return False

    label = _build_contributor_heading(contributor)
    for attempt in range(1, _NOTES_BLOCK_POLL_ATTEMPTS + 1):
        # Wait for the async clone to populate the container so we prepend
        # above real content rather than into an empty block mid-populate.
        if client.get_block_children(notes_block_id):
            client.append_block_children(
                block_id=notes_block_id, children=[label],
                position={"type": "start"},
            )
            return True
        if attempt < _NOTES_BLOCK_POLL_ATTEMPTS:
            time.sleep(_NOTES_BLOCK_POLL_DELAY_SECONDS)

    logger.warning(
        "Mirror %s notes container still empty after %d polls — leaving first "
        "contributor notes unlabeled", mirror_page_id[:8], _NOTES_BLOCK_POLL_ATTEMPTS,
    )
    return False


def _internal_attendee_ids(
    client: NotionClientWrapper, source_page_id: str,
) -> list[str]:
    """Notion-member attendee UUIDs from a source page's meeting_notes block.

    "Internal" = attendees that resolve to a real workspace member
    (``type == "person"``). External meeting participants aren't Notion users
    and never appear here, so the membership filter is what separates the team
    from any stray guest/bot id. Order-preserving and deduped. Empty when the
    page has no meeting_notes block or no member attendees.
    """
    blocks = client.get_block_children(source_page_id)
    mn_block = find_meeting_notes_block(blocks)
    if mn_block is None:
        return []
    attendee_ids = extract_attendee_ids(mn_block)
    if not attendee_ids:
        return []
    member_ids = {
        u.get("id") for u in client.list_users() if u.get("type") == "person"
    }
    out: list[str] = []
    seen: set[str] = set()
    for uid in attendee_ids:
        if uid in member_ids and uid not in seen:
            seen.add(uid)
            out.append(uid)
    return out


def _read_people_ids(page: dict, prop_name: str) -> list[str]:
    """Return the Notion user UUIDs in a page's *prop_name* people property."""
    prop = (page.get("properties") or {}).get(prop_name, {}) or {}
    if prop.get("type") != "people":
        return []
    return [p["id"] for p in prop.get("people") or [] if p.get("id")]


def _update_internal_attendees(
    client: NotionClientWrapper, mirror_page: dict, new_ids: list[str],
) -> None:
    """Union *new_ids* into the mirror's ``Internal attendees`` people property.

    Graceful no-op when the target DB doesn't declare the column (the property
    is absent from the page) or when there's nothing new to add — so re-runs
    don't issue redundant PATCHes.
    """
    if not new_ids:
        return
    prop = (mirror_page.get("properties") or {}).get("Internal attendees")
    if prop is None or prop.get("type") != "people":
        return
    current = _read_people_ids(mirror_page, "Internal attendees")
    merged = list(dict.fromkeys([*current, *new_ids]))
    if merged == current:
        return
    client.update_page(
        page_id=mirror_page["id"],
        properties={"Internal attendees": {"people": [{"id": uid} for uid in merged]}},
    )


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

    First contributor → ``MirrorAction.CLONED`` (writes ``Owner`` + ``Internal
    attendees`` people, then labels their cloned notes with a ``<Name>'s
    notes`` blue H3). Subsequent contributor with notes → ``MirrorAction.MERGED``
    (appends ``<owner_name>'s notes`` blue H3 + content inside the mirror's
    notes_block_id, unions ``owner_user_id`` into ``Owner`` and the
    contributor's member attendees into ``Internal attendees``).
    Owner already in the Owner list, or no notes to merge → ``MirrorAction.NOOP``
    (``Internal attendees`` is still unioned in either case).

    *owner_user_id* is the Notion user UUID used for the Owner people field;
    *owner_name* is the display name used for the appended heading. Pass
    both: the UUID is the dedup key, but the name is what readers see.

    Raises ``APIResponseError`` only for unexpected failures (the caller
    catches and converts to a failed-route entry in the outcome).
    """
    internal_ids = _internal_attendee_ids(client, source_page["id"])
    existing = find_existing_mirror(client, route.target_db_id, source_title, source_date)
    if existing is None:
        # Retrieve target schema once so we can (a) drop source props the
        # target doesn't declare, and (b) auto-add missing select /
        # multi_select option values before the clone. Notion API 2026-03-11
        # no longer silently drops unknown property names on pages.create
        # under template_id, so this filtering step is load-bearing.
        target_schema = (
            client.retrieve_data_source(route.target_db_id).get("properties") or {}
        )
        properties = _build_clone_properties(
            source_page, source_title, owner_user_id, internal_ids,
        )
        properties, dropped = _filter_to_target_schema(properties, target_schema)
        if dropped:
            logger.info(
                "Mirror %s: dropped %d source prop(s) absent from target schema: %s",
                route.label, len(dropped), ", ".join(dropped),
            )
        _ensure_select_options_on_target(
            client=client,
            target_db_id=route.target_db_id,
            target_schema=target_schema,
            properties_to_clone=properties,
            source_props=source_page.get("properties") or {},
            route_label=route.label,
        )
        mirror = _clone_into_target(client, source_page, route.target_db_id, properties)
        logger.info(
            "Cloned page %s → mirror %s (route=%s owner=%s)",
            source_page.get("id", "?")[:8],
            mirror.get("id", "?")[:8],
            route.label,
            owner_name or owner_user_id[:8],
        )
        # Label the first contributor's cloned notes (best-effort — async clone).
        _label_first_contributor_notes(
            client, mirror["id"], owner_name or "Unknown",
        )
        return MirrorAction.CLONED

    # Union this contributor's member attendees into Internal attendees up
    # front, so it's kept current even when the notes-merge path no-ops.
    _update_internal_attendees(client, existing, internal_ids)

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
