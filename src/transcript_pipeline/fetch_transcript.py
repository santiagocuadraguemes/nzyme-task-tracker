"""Core logic for fetching raw transcripts from Notion meeting_notes blocks."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from src.notion_client_wrapper import NotionClientWrapper
from src.utils.blocks_to_text import blocks_to_text


def find_meeting_notes_block(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the first meeting_notes block in a list of blocks."""
    for block in blocks:
        if block.get("type") == "meeting_notes":
            return block
    return None


def extract_transcript_block_id(mn_block: dict[str, Any]) -> str | None:
    """Extract the transcript_block_id from a meeting_notes block.

    Returns None when transcription is paused/disabled and the key is absent.
    Callers route those pages through the notes-only fallback in `pipeline.py`.
    """
    children = (mn_block or {}).get("meeting_notes", {}).get("children") or {}
    block_id = children.get("transcript_block_id")
    return block_id if isinstance(block_id, str) and block_id else None


def fetch_notes_text(
    mn_block: dict[str, Any],
    client: NotionClientWrapper,
) -> str:
    """Fetch human-written notes from the meeting_notes block.

    Returns empty string if no notes block exists or notes are empty.
    """
    notes_block_id = (
        mn_block.get("meeting_notes", {}).get("children", {}).get("notes_block_id")
    )
    if not notes_block_id:
        return ""
    notes_blocks = client.get_block_children(notes_block_id)
    if not notes_blocks:
        return ""
    return blocks_to_text(notes_blocks, client)


def extract_attendee_ids(mn_block: dict[str, Any]) -> list[str]:
    """Extract attendee user IDs from a meeting_notes block.

    Returns an empty list if calendar_event or attendees are missing.
    """
    try:
        attendees = mn_block["meeting_notes"]["calendar_event"]["attendees"]
        # Attendees may be plain user ID strings or dicts with a user_id key
        ids = []
        for a in attendees:
            if isinstance(a, str):
                ids.append(a)
            elif isinstance(a, dict):
                ids.append(a.get("user_id") or a.get("id", str(a)))
            else:
                ids.append(str(a))
        return ids
    except (KeyError, TypeError):
        return []


def build_user_lookup(client: NotionClientWrapper) -> dict[str, str]:
    """Fetch all workspace users and return a {user_id: display_name} map."""
    users = client.list_users()
    lookup: dict[str, str] = {}
    for user in users:
        uid = user.get("id", "")
        name = user.get("name") or user.get("person", {}).get("email", uid)
        lookup[uid] = name
    return lookup


def extract_ai_summary(page: dict[str, Any]) -> str:
    """Read the Notion-AI-generated 'AI Summary' page property as plain text.

    Returns "" when the property is missing or empty. The property is a
    rich_text auto-fill populated by Notion AI from the meeting transcript.
    """
    prop = page.get("properties", {}).get("AI Summary", {})
    if prop.get("type") != "rich_text":
        return ""
    parts = prop.get("rich_text", []) or []
    return "".join(p.get("plain_text", "") for p in parts).strip()


def extract_governance_attendees(page: dict[str, Any]) -> list[dict[str, str]]:
    """Read the 'Governance: Edit & View Access' people property from a page.

    Used as a last-resort attendee fallback when Google Calendar finds no event
    and Notion's meeting_notes block has no attendees. People entries on this
    property already carry `id` and `name` inline, so no user-lookup call is
    needed.
    """
    prop = page.get("properties", {}).get("Governance: Edit & View Access", {})
    people = prop.get("people", []) if prop.get("type") == "people" else []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for person in people:
        uid = person.get("id", "")
        if not uid or uid in seen:
            continue
        seen.add(uid)
        name = person.get("name") or person.get("person", {}).get("email", uid)
        result.append({"id": uid, "name": name})
    return result


def extract_page_metadata(page: dict[str, Any]) -> dict[str, str]:
    """Extract meeting title and date from a Notion page's properties.

    Returns:
        {"title": "Meeting title", "date": "2026-04-09"} with empty strings as fallbacks.
    """
    props = page.get("properties", {})
    # Title — located by property TYPE, not name. Every Notion DB has exactly
    # one property of type "title", but its NAME varies ("Meeting" in standard
    # member DBs, "Note" in Álvaro Lozano's, "Título" in Jaime Gervás's).
    # Matches `meeting_row._title_text`.
    title = ""
    for prop in props.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            title = "".join(
                t.get("plain_text", "") for t in prop.get("title", []) or []
            )
            break

    # Date — try "Date" property first, fall back to page created_time
    date = ""
    date_prop = props.get("Date", {})
    date_val = date_prop.get("date") or {}
    date = date_val.get("start", "")
    if not date:
        date = page.get("created_time", "")

    # Creator — for assignee fallback
    creator = page.get("created_by", {})
    created_by_id = creator.get("id", "")
    created_by_name = creator.get("name", "")

    return {
        "title": title,
        "date": date,
        "created_by_id": created_by_id,
        "created_by_name": created_by_name,
    }


def strip_title_datetime(title: str) -> str:
    """Strip trailing ISO datetime suffix from a Notion meeting title.

    Notion titles include the event datetime (e.g. "NzX SteerCo 2026-04-08T12:00:00.000+02:00")
    but Google Calendar titles don't. Strip the trailing ISO pattern so GCal search works.
    """
    return re.sub(r"\s+\d{4}-\d{2}-\d{2}(T\S+)?$", "", title).strip()


def fetch_transcript(
    page_id: str,
    client: NotionClientWrapper,
    verbose: bool = False,
) -> tuple[str, list[dict[str, str]], dict[str, str], str, list[dict[str, str]]]:
    """Fetch raw transcript text, resolved attendees, page metadata, notes, and
    governance-access attendees.

    Returns:
        (transcript_text, attendees, metadata, notes_text, governance_attendees).
        `governance_attendees` comes from the page's "Governance: Edit & View
        Access" people property and is meant as a last-resort fallback when
        neither GCal nor the meeting_notes block provides attendees.
    """
    # 1. Get page metadata (title, date) + governance attendees
    page = client.get_page(page_id)
    metadata = extract_page_metadata(page)
    governance_attendees = extract_governance_attendees(page)

    # 2. Get all blocks on the page
    blocks = client.get_block_children(page_id)

    # 2. Find the meeting_notes block
    mn_block = find_meeting_notes_block(blocks)
    if mn_block is None:
        print(
            f"ERROR: No meeting_notes block found on page {page_id}.\n"
            f"Found block types: {[b.get('type') for b in blocks]}",
            file=sys.stderr,
        )
        sys.exit(1)

    if verbose:
        print("=== RAW MEETING_NOTES BLOCK ===", file=sys.stderr)
        print(json.dumps(mn_block, indent=2, default=str), file=sys.stderr)
        print("", file=sys.stderr)

    # 3. Extract and fetch transcript
    transcript_block_id = extract_transcript_block_id(mn_block)
    if transcript_block_id is None:
        print(
            f"ERROR: meeting_notes block on page {page_id} has no transcript_block_id "
            f"(transcription paused/disabled). Block structure:\n"
            f"{json.dumps(mn_block.get('meeting_notes', {}), indent=2)}",
            file=sys.stderr,
        )
        sys.exit(1)
    transcript_blocks = client.get_block_children(transcript_block_id)

    if not transcript_blocks:
        print("WARNING: Transcript block has no children (recording may not be processed yet).", file=sys.stderr)
        return ("", [], metadata, "", governance_attendees)

    transcript_text = blocks_to_text(transcript_blocks, client)

    # 3b. Fetch human-written notes (optional — some meetings have none)
    notes_text = fetch_notes_text(mn_block, client)

    # 4. Resolve attendees
    attendee_ids = extract_attendee_ids(mn_block)
    if attendee_ids:
        user_lookup = build_user_lookup(client)
        attendees = [
            {"id": uid, "name": user_lookup.get(uid, uid)}
            for uid in attendee_ids
        ]
    else:
        attendees = []

    return (transcript_text, attendees, metadata, notes_text, governance_attendees)
