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


def extract_transcript_block_id(mn_block: dict[str, Any]) -> str:
    """Extract the transcript_block_id from a meeting_notes block.

    Raises ValueError if the expected structure is missing.
    """
    try:
        return mn_block["meeting_notes"]["children"]["transcript_block_id"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"meeting_notes block missing transcript_block_id. "
            f"Block structure: {json.dumps(mn_block.get('meeting_notes', {}), indent=2)}"
        ) from exc


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


def extract_page_metadata(page: dict[str, Any]) -> dict[str, str]:
    """Extract meeting title and date from a Notion page's properties.

    Returns:
        {"title": "Meeting title", "date": "2026-04-09"} with empty strings as fallbacks.
    """
    props = page.get("properties", {})
    # Title — "Meeting" property (title type)
    title = ""
    title_prop = props.get("Meeting", {})
    title_parts = title_prop.get("title", [])
    if title_parts:
        title = "".join(t.get("plain_text", "") for t in title_parts)

    # Date — try "Date" property first, fall back to page created_time
    date = ""
    date_prop = props.get("Date", {})
    date_val = date_prop.get("date") or {}
    date = date_val.get("start", "")
    if not date:
        date = page.get("created_time", "")

    return {"title": title, "date": date}


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
) -> tuple[str, list[dict[str, str]], dict[str, str]]:
    """Fetch raw transcript text, resolved attendees, and page metadata.

    Returns:
        (transcript_text, attendees, metadata) where attendees is a list of
        {"id": user_id, "name": display_name} dicts, and metadata is
        {"title": ..., "date": ...}.
    """
    # 1. Get page metadata (title, date)
    page = client.get_page(page_id)
    metadata = extract_page_metadata(page)

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
    transcript_blocks = client.get_block_children(transcript_block_id)

    if not transcript_blocks:
        print("WARNING: Transcript block has no children (recording may not be processed yet).", file=sys.stderr)
        return ("", [], metadata)

    transcript_text = blocks_to_text(transcript_blocks, client)

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

    return (transcript_text, attendees, metadata)
