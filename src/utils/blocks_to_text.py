"""Convert Notion block arrays to plain text with structure preserved."""
from __future__ import annotations

from typing import Any


def _extract_plain_text(rich_text: list[dict[str, Any]]) -> str:
    return "".join(rt.get("plain_text", "") for rt in rich_text)


def blocks_to_text(
    blocks: list[dict[str, Any]],
    client: Any | None = None,
    indent: int = 0,
) -> str:
    """Convert a list of Notion blocks to plain text.

    Parameters
    ----------
    blocks:
        List of Notion block objects.
    client:
        Optional NotionClientWrapper for fetching children of nested blocks.
    indent:
        Current indentation level (for nested content).
    """
    lines: list[str] = []
    prefix = "  " * indent

    for block in blocks:
        block_type = block.get("type", "")
        block_data = block.get(block_type, {})
        rich_text = block_data.get("rich_text", [])
        text = _extract_plain_text(rich_text)

        if block_type == "heading_1":
            lines.append(f"# {text}")
        elif block_type == "heading_2":
            lines.append(f"## {text}")
        elif block_type == "heading_3":
            lines.append(f"### {text}")
        elif block_type == "bulleted_list_item":
            lines.append(f"{prefix}- {text}")
        elif block_type == "numbered_list_item":
            lines.append(f"{prefix}1. {text}")
        elif block_type == "to_do":
            checked = block_data.get("checked", False)
            marker = "[x]" if checked else "[ ]"
            lines.append(f"{prefix}- {marker} {text}")
        elif block_type == "divider":
            lines.append("---")
        elif block_type in ("paragraph", "callout", "quote", "toggle"):
            if text:
                lines.append(f"{prefix}{text}")

        # Recurse into children
        if block.get("has_children") and client:
            children = client.get_block_children(block["id"])
            child_text = blocks_to_text(children, client, indent + 1)
            if child_text:
                lines.append(child_text)

    return "\n".join(lines)
