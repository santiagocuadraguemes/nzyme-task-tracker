"""Copy a contributor's notes tab verbatim from their source page.

When a second member tags an already-mirrored meeting, we don't re-clone the
whole page — we copy the entire contents of their AI-meeting-block notes
container (Action Items section, Notes heading, written notes, everything) and
append it to the mirror under a ``<Name>'s notes`` H3, so each contributor's
notes are reproduced literally with their original structure and colors.

The blocks have to be converted to create-format (drop read-only fields,
clean up mentions) before they can be appended. We reuse the converter from
``template_injector`` so the conversion rules stay in one place — it preserves
block types and the ``color`` field, dropping only read-only metadata.
"""
from __future__ import annotations

import logging
from typing import Any

from src.notion_client_wrapper import NotionClientWrapper
from src.template_injector import _block_to_create_format
from src.transcript_pipeline.fetch_transcript import find_meeting_notes_block


logger = logging.getLogger(__name__)


def _notes_block_id(client: NotionClientWrapper, source_page_id: str) -> str | None:
    """Return the source page's ``meeting_notes.children.notes_block_id``."""
    blocks = client.get_block_children(source_page_id)
    mn_block = find_meeting_notes_block(blocks)
    if mn_block is None:
        logger.debug("Source page %s has no meeting_notes block", source_page_id)
        return None
    notes_block_id = (
        mn_block.get("meeting_notes", {})
        .get("children", {})
        .get("notes_block_id")
    )
    if not notes_block_id:
        logger.debug(
            "Source page %s meeting_notes has no notes_block_id", source_page_id,
        )
        return None
    return notes_block_id


def fetch_notes_blocks_for_clone(
    client: NotionClientWrapper, source_page_id: str,
) -> list[dict[str, Any]]:
    """Fetch a contributor's ENTIRE notes container as create-format blocks.

    Literal copy: every child of the source's ``notes_block_id`` — the
    ``## Action Items`` section, the ``## Notes`` heading, the written notes —
    is reproduced, preserving block types and ``color``. Nothing is sliced or
    dropped (beyond read-only metadata and non-human block types that
    ``_block_to_create_format`` filters).

    Returns an empty list when the source has no ``meeting_notes`` block, no
    ``notes_block_id``, or an empty notes container.
    """
    notes_block_id = _notes_block_id(client, source_page_id)
    if not notes_block_id:
        return []

    notes_children = client.get_block_children(notes_block_id)
    if not notes_children:
        return []

    create_blocks: list[dict[str, Any]] = []
    for block in notes_children:
        converted = _block_to_create_format(block, client)
        if converted is not None:
            create_blocks.append(converted)
    return create_blocks


__all__ = ["fetch_notes_blocks_for_clone"]
