"""Pull a contributor's ``## Notes`` content from their source page.

When a second member tags an already-mirrored meeting, we don't re-clone the
whole page — we extract just the blocks they wrote under their ``## Notes``
heading and append them to the mirror under a ``### <Name>'s Notes`` H3.

The blocks have to be converted to create-format (drop read-only fields,
clean up mentions) before they can be appended. We reuse the converter
from ``template_injector`` so the conversion rules stay in one place.
"""
from __future__ import annotations

import logging
from typing import Any

from src.notion_client_wrapper import NotionClientWrapper
from src.template_injector import _block_to_create_format
from src.transcript_pipeline.fetch_transcript import find_meeting_notes_block

logger = logging.getLogger(__name__)

# Heading text that demarcates the start of the human-written notes section
# inside the AI Meeting block's notes container. Matched case-insensitively
# so manual capitalisation drift on the template doesn't break things.
_NOTES_HEADING_LABEL = "notes"
_ACTION_ITEMS_HEADING_LABEL = "action items"


def _is_heading_with_text(block: dict[str, Any], lowered_text: str) -> bool:
    btype = block.get("type", "")
    if not btype.startswith("heading_"):
        return False
    rich_text = block.get(btype, {}).get("rich_text", [])
    text = "".join(rt.get("plain_text", "") for rt in rich_text).strip().lower()
    return text == lowered_text


def _slice_after_notes_heading(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the blocks that come after the first ``## Notes`` heading.

    If no such heading exists, returns an empty list — the source hasn't
    been template-injected and we don't know which blocks belong to the
    notes section vs. the action items section.
    """
    after: list[dict[str, Any]] | None = None
    for block in blocks:
        if after is None:
            if _is_heading_with_text(block, _NOTES_HEADING_LABEL):
                after = []
            continue
        # Once we hit a sibling that would clearly belong to a different
        # section (none defined today — only Action Items + Notes exist
        # in the template), we'd stop. For now everything after Notes is
        # treated as notes content.
        after.append(block)
    return after or []


def fetch_notes_blocks_for_clone(
    client: NotionClientWrapper, source_page_id: str,
) -> list[dict[str, Any]]:
    """Fetch a contributor's ``## Notes`` content as create-format blocks.

    Returns an empty list when:
      - The source page has no ``meeting_notes`` block
      - The ``meeting_notes`` block has no ``notes_block_id``
      - The notes container has no ``## Notes`` heading (template
        injection didn't happen; we can't tell notes from action items)
      - There's no content after the ``## Notes`` heading

    All read-only fields are stripped and non-human block types (AI
    notes, etc.) are filtered out by ``_block_to_create_format``.
    """
    blocks = client.get_block_children(source_page_id)
    mn_block = find_meeting_notes_block(blocks)
    if mn_block is None:
        logger.debug("Source page %s has no meeting_notes block", source_page_id)
        return []

    notes_block_id = (
        mn_block.get("meeting_notes", {})
        .get("children", {})
        .get("notes_block_id")
    )
    if not notes_block_id:
        logger.debug(
            "Source page %s meeting_notes has no notes_block_id", source_page_id,
        )
        return []

    notes_children = client.get_block_children(notes_block_id)
    if not notes_children:
        return []

    after_notes = _slice_after_notes_heading(notes_children)
    if not after_notes:
        logger.debug(
            "Source page %s has no '## Notes' heading in notes_block — "
            "cannot isolate contributor notes",
            source_page_id,
        )
        return []

    create_blocks: list[dict[str, Any]] = []
    for block in after_notes:
        converted = _block_to_create_format(block, client)
        if converted is not None:
            create_blocks.append(converted)
    return create_blocks


__all__ = ["fetch_notes_blocks_for_clone"]
