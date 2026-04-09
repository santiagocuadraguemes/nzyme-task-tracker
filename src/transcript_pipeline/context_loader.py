"""Load terminology and org chart context from Notion databases."""

from __future__ import annotations

from typing import Any

from src.notion_client_wrapper import NotionClientWrapper


def _get_text(prop: dict[str, Any]) -> str:
    """Extract plain text from a Notion rich_text property."""
    parts = prop.get("rich_text", [])
    return "".join(p.get("plain_text", "") for p in parts).strip()


def _get_title(prop: dict[str, Any]) -> str:
    """Extract plain text from a Notion title property."""
    parts = prop.get("title", [])
    return "".join(p.get("plain_text", "") for p in parts).strip()


def _get_select(prop: dict[str, Any]) -> str:
    """Extract the name from a Notion select property."""
    sel = prop.get("select")
    return sel["name"] if sel else ""


def _get_multi_select(prop: dict[str, Any]) -> list[str]:
    """Extract names from a Notion multi_select property."""
    return [o["name"] for o in prop.get("multi_select", [])]


def load_terminology(client: NotionClientWrapper, db_id: str) -> str:
    """Load active terms from the Terminology Dictionary and format for LLM context.

    Returns a structured string like:
        Term: Civislend (deal) — Real estate crowdfunding platform
          Phonetic variants: civic lend, civil end, civis lend
    """
    resp = client.query_database(
        database_id=db_id,
        filter={"property": "Active", "checkbox": {"equals": True}},
    )
    rows = resp.get("results", [])
    if not rows:
        return ""

    entries: list[str] = []
    for row in rows:
        props = row.get("properties", {})
        term = _get_title(props.get("Term", {}))
        if not term:
            continue

        category = _get_select(props.get("Category", {}))
        context = _get_text(props.get("Context", {}))
        variants = _get_text(props.get("Phonetic Variants", {}))

        # Header line: Term: Name (category) — context
        header = f"Term: {term}"
        if category:
            header += f" ({category})"
        if context:
            header += f" — {context}"

        lines = [header]
        if variants:
            lines.append(f"  Phonetic variants: {variants}")

        entries.append("\n".join(lines))

    return "\n\n".join(entries)


def load_org_chart(client: NotionClientWrapper, db_id: str) -> str:
    """Load active members from the Org Chart and format for LLM context.

    Returns a structured string like:
        Person: Reyes Rubio — Co-founding Partner, Investment
          Role: Managing Partner & CIO
          Typical topics: deal execution, fundraising, portfolio
    """
    resp = client.query_database(
        database_id=db_id,
        filter={"property": "Active", "checkbox": {"equals": True}},
    )
    rows = resp.get("results", [])
    if not rows:
        return ""

    entries: list[str] = []
    for row in rows:
        props = row.get("properties", {})
        name = _get_title(props.get("Name", {}))
        if not name:
            continue

        seniority = _get_select(props.get("Seniority", {}))
        department = _get_select(props.get("Department", {}))
        role = _get_text(props.get("Role", {}))
        topics = _get_multi_select(props.get("Typical Topics", {}))

        # Header line: Person: Name — Seniority, Department
        header = f"Person: {name}"
        qualifiers = [q for q in (seniority, department) if q]
        if qualifiers:
            header += f" — {', '.join(qualifiers)}"

        lines = [header]
        if role:
            lines.append(f"  Role: {role}")
        if topics:
            lines.append(f"  Typical topics: {', '.join(topics)}")

        entries.append("\n".join(lines))

    return "\n\n".join(entries)
