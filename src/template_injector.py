"""Inject meeting note template blocks from a Notion template page.

Reads the template dynamically — edit it in Notion and the injector adapts.

The template is injected INSIDE the page's ``meeting_notes`` block (under
the human-notes section that the AI Meeting block exposes), not at the
page root. Falls back gracefully if the meeting_notes block has not yet
been created by Notion at the moment the webhook fires — the next cron
tick will retry.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from src.notion_client_wrapper import NotionClientWrapper
from src.sources.single_source import HUMAN_CONTENT_BLOCK_TYPES
from src.transcript_pipeline.fetch_transcript import find_meeting_notes_block

logger = logging.getLogger(__name__)

# Top-level fields present on "read" blocks that must not appear on "create" blocks.
_READ_ONLY_BLOCK_FIELDS = {
    "id", "parent", "created_time", "last_edited_time",
    "created_by", "last_edited_by", "has_children",
    "archived", "in_trash", "request_id",
}

# Fields inside the type-specific data that are read-only.
_READ_ONLY_TYPE_FIELDS = {"is_toggleable"}

# How many times to poll for the meeting_notes block before giving up.
_NOTES_BLOCK_RETRY_ATTEMPTS = 3
_NOTES_BLOCK_RETRY_DELAY_SECONDS = 1.0


def _clean_rich_text(rich_text: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip read-only fields from rich_text items for the create API.

    Mention objects (e.g. user mentions) come back from reads with extra
    fields like ``name``, ``avatar_url``, ``type``, ``person`` that the
    create API rejects.  We keep only ``id`` on user objects.
    """
    cleaned: list[dict[str, Any]] = []
    for rt in rich_text:
        rt_type = rt.get("type", "text")
        item: dict[str, Any] = {"type": rt_type}
        if "annotations" in rt:
            item["annotations"] = rt["annotations"]

        if rt_type == "text":
            item["text"] = rt.get("text", {"content": rt.get("plain_text", "")})
        elif rt_type == "mention":
            mention = rt["mention"]
            if mention.get("type") == "user":
                item["mention"] = {"type": "user", "user": {"id": mention["user"]["id"]}}
            else:
                item["mention"] = mention
        elif rt_type == "equation":
            item["equation"] = rt["equation"]
        cleaned.append(item)
    return cleaned


def _block_to_create_format(
    block: dict[str, Any],
    client: NotionClientWrapper,
) -> dict[str, Any] | None:
    """Convert a read-format Notion block to create-format.

    Skips non-human block types (AI meeting notes, etc.).
    Recursively converts children for blocks like tables and toggles.
    """
    block_type = block.get("type", "")
    if block_type not in HUMAN_CONTENT_BLOCK_TYPES:
        return None

    # Drop read-only fields and null-valued fields. Notion returns optional
    # fields like ``icon`` / ``caption`` as ``null`` on read but the create
    # API rejects null and expects the key to be omitted.
    type_data = {
        k: v for k, v in block.get(block_type, {}).items()
        if k not in _READ_ONLY_TYPE_FIELDS and v is not None
    }

    # Clean rich_text to strip read-only fields from mentions
    if "rich_text" in type_data:
        type_data["rich_text"] = _clean_rich_text(type_data["rich_text"])

    # Recursively fetch and convert children (tables, toggles, columns, etc.)
    if block.get("has_children"):
        children = client.get_block_children(block["id"])
        converted = [_block_to_create_format(c, client) for c in children]
        type_data["children"] = [c for c in converted if c is not None]

    return {
        "object": "block",
        "type": block_type,
        block_type: type_data,
    }


def _extract_heading_marker(
    blocks: list[dict[str, Any]],
) -> tuple[str, str] | None:
    """Return (heading_type, lowered_text) of the first heading in *blocks*."""
    for block in blocks:
        bt = block.get("type", "")
        if bt.startswith("heading_"):
            rich_text = block.get(bt, {}).get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich_text)
            if text.strip():
                return (bt, text.strip().lower())
    return None


def fetch_template(
    client: NotionClientWrapper,
    template_page_id: str,
) -> tuple[list[dict[str, Any]], tuple[str, str] | None]:
    """Fetch the template page blocks and convert to create-format.

    Returns (create_blocks, marker) where *marker* is the first heading's
    (type, lowered_text) used for idempotency checks, or None.
    """
    raw_blocks = client.get_block_children(template_page_id)
    marker = _extract_heading_marker(raw_blocks)

    create_blocks: list[dict[str, Any]] = []
    for block in raw_blocks:
        converted = _block_to_create_format(block, client)
        if converted is not None:
            create_blocks.append(converted)

    logger.info(
        "Loaded template: %d blocks, marker=%s",
        len(create_blocks),
        marker[1] if marker else "none",
    )
    return create_blocks, marker


def page_has_template(
    page_blocks: list[dict[str, Any]],
    marker: tuple[str, str] | None,
) -> bool:
    """Check whether the template's first heading already exists in *page_blocks*."""
    if marker is None:
        return False
    target_type, target_text = marker
    for block in page_blocks:
        if block.get("type") == target_type:
            rich_text = block.get(target_type, {}).get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich_text)
            if text.strip().lower() == target_text:
                return True
    return False


def _find_notes_block_id(
    client: NotionClientWrapper, page_id: str,
) -> str | None:
    """Locate the human-notes container inside the page's meeting_notes block.

    Retries a few times to handle the race where Notion has created the
    page but not yet attached the meeting_notes block.
    """
    for attempt in range(1, _NOTES_BLOCK_RETRY_ATTEMPTS + 1):
        page_blocks = client.get_block_children(page_id)
        mn_block = find_meeting_notes_block(page_blocks)
        if mn_block is not None:
            notes_block_id = (
                mn_block.get("meeting_notes", {})
                .get("children", {})
                .get("notes_block_id")
            )
            if notes_block_id:
                return notes_block_id
            logger.warning(
                "meeting_notes block on page %s missing notes_block_id — skipping",
                page_id,
            )
            return None
        if attempt < _NOTES_BLOCK_RETRY_ATTEMPTS:
            time.sleep(_NOTES_BLOCK_RETRY_DELAY_SECONDS)
    logger.info(
        "No meeting_notes block on page %s after %d attempts — leaving for cron retry",
        page_id, _NOTES_BLOCK_RETRY_ATTEMPTS,
    )
    return None


def inject_notes_section(
    client: NotionClientWrapper,
    page_id: str,
    template_blocks: list[dict[str, Any]],
    marker: tuple[str, str] | None,
) -> bool:
    """Inject template blocks inside the page's meeting_notes block.

    Returns True if injected, False if skipped (template already present,
    meeting_notes block not yet available, or no notes_block_id).
    """
    notes_block_id = _find_notes_block_id(client, page_id)
    if notes_block_id is None:
        return False

    existing_children = client.get_block_children(notes_block_id)
    if page_has_template(existing_children, marker):
        return False

    client.append_block_children(
        block_id=notes_block_id,
        children=template_blocks,
        position={"type": "start"},
    )
    logger.info(
        "Injected template into meeting_notes block of page %s (notes_block=%s)",
        page_id, notes_block_id,
    )
    return True
