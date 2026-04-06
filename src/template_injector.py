"""Inject meeting note template blocks from a Notion template page.

Reads the template dynamically — edit it in Notion and the injector adapts.
"""
from __future__ import annotations

import logging
from typing import Any

from src.notion_client_wrapper import NotionClientWrapper
from src.sources.single_source import HUMAN_CONTENT_BLOCK_TYPES

logger = logging.getLogger(__name__)

# Top-level fields present on "read" blocks that must not appear on "create" blocks.
_READ_ONLY_BLOCK_FIELDS = {
    "id", "parent", "created_time", "last_edited_time",
    "created_by", "last_edited_by", "has_children",
    "archived", "in_trash", "request_id",
}

# Fields inside the type-specific data that are read-only.
_READ_ONLY_TYPE_FIELDS = {"is_toggleable"}


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

    type_data = {
        k: v for k, v in block.get(block_type, {}).items()
        if k not in _READ_ONLY_TYPE_FIELDS
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
    """Check whether the template's first heading already exists on the page."""
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


def inject_notes_section(
    client: NotionClientWrapper,
    page_id: str,
    template_blocks: list[dict[str, Any]],
    marker: tuple[str, str] | None,
) -> bool:
    """Inject template blocks into a page if missing. Returns True if injected."""
    page_blocks = client.get_block_children(page_id)
    if page_has_template(page_blocks, marker):
        return False

    client.append_block_children(
        block_id=page_id,
        children=template_blocks,
        position={"type": "start"},
    )
    logger.info("Injected template into page %s", page_id)
    return True
