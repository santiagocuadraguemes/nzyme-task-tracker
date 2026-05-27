"""Load terminology and org chart context from Notion databases."""

from __future__ import annotations

import unicodedata
from typing import Any

from src.notion_client_wrapper import NotionClientWrapper


def _get_text(prop: dict[str, Any]) -> str:
    """Extract plain text from a Notion rich_text property."""
    parts = prop.get("rich_text", [])
    return "".join(p.get("plain_text", "") for p in parts).strip()


def _get_email(prop: dict[str, Any]) -> str:
    """Extract an email from a Notion property — handles both email and rich_text types."""
    if "email" in prop and prop.get("email"):
        return prop["email"].strip().lower()
    return _get_text(prop).lower()


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
    """Load active members from the Org Chart and format for LLM context."""
    return format_org_chart(load_org_chart_rows(client, db_id))


def format_org_chart(rows: list[dict[str, Any]]) -> str:
    """Format already-loaded org chart rows for LLM context.

    Returns a structured string like:
        Person: Reyes Rubio — Co-founding Partner, Investment
          Role: Managing Partner & CIO
          Typical topics: deal execution, fundraising, portfolio
    """
    if not rows:
        return ""

    entries: list[str] = []
    for row in rows:
        header = f"Person: {row['name']}"
        qualifiers = [q for q in (row["seniority"], row["department"]) if q]
        if qualifiers:
            header += f" — {', '.join(qualifiers)}"

        lines = [header]
        if row["role"]:
            lines.append(f"  Role: {row['role']}")
        if row["topics"]:
            lines.append(f"  Typical topics: {', '.join(row['topics'])}")

        entries.append("\n".join(lines))

    return "\n\n".join(entries)


def load_org_chart_rows(
    client: NotionClientWrapper, db_id: str
) -> list[dict[str, Any]]:
    """Load every member of the Org Chart as structured dicts.

    Returns list of {"name", "email", "seniority", "department", "role",
    "topics", "active"}. ``active`` reflects the Notion ``Active`` checkbox;
    callers that need the Active subset (e.g. ``discover_meeting_dbs`` for
    deciding whose meeting DB to poll) filter on it. Attendee enrichment
    and role annotations DON'T filter — Active is a gate for syncing
    a member's own meetings, not for whether their role is shown when
    they appear as an attendee in someone else's meeting.

    `email` is lowercase (empty string if not set). Used for email-first
    matching of GCal attendees against the org chart.
    """
    resp = client.query_database(database_id=db_id)
    results = resp.get("results", [])
    rows: list[dict[str, Any]] = []
    for r in results:
        props = r.get("properties", {})
        name = _get_title(props.get("Name", {}))
        if not name:
            continue
        active = props.get("Active", {}).get("checkbox", False)
        rows.append({
            "name": name,
            "email": _get_email(props.get("Email", {})),
            "seniority": _get_select(props.get("Seniority", {})),
            "department": _get_select(props.get("Department", {})),
            "role": _get_text(props.get("Role", {})),
            "topics": _get_multi_select(props.get("Typical Topics", {})),
            "active": bool(active),
        })
    return rows


def _normalize(name: str) -> str:
    """Lowercase, strip accents and whitespace for fuzzy name matching."""
    name = name.strip().lower()
    # Decompose accented chars and strip combining marks
    nfkd = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _match_attendee_to_org(
    attendee_name: str,
    org_rows: list[dict[str, Any]],
    attendee_email: str | None = None,
) -> dict[str, Any] | None:
    """Find the best org chart match for an attendee.

    Tries email first (exact, case-insensitive) when available — this is the
    reliable match for GCal attendees. Falls back to name substring matching
    when no email is provided or no email row matches.
    """
    if attendee_email:
        norm_email = attendee_email.strip().lower()
        for row in org_rows:
            if row.get("email") and row["email"] == norm_email:
                return row

    norm_att = _normalize(attendee_name)
    for row in org_rows:
        norm_org = _normalize(row["name"])
        if norm_att == norm_org or norm_att in norm_org or norm_org in norm_att:
            return row
    return None


def build_enriched_attendee_str(
    attendees: list[dict[str, str]],
    org_chart_rows: list[dict[str, Any]],
) -> str:
    """Build a structured attendee string with inline role context.

    Attendee dicts may include an `email` field (set by the GCal source);
    when present it is used as the primary match key against the org chart,
    falling back to name. Returns something like:
        - Santiago Cuadra Güemes [Technology — Head of Technology]
          Typical topics: automation, Notion integration
        - Reyes Rubio [Investment — Managing Partner & CIO]
          Typical topics: deal execution, fundraising
        - External Guest (no org chart match)
    """
    lines: list[str] = []
    for att in attendees:
        name = att["name"]
        email = att.get("email")
        match = _match_attendee_to_org(name, org_chart_rows, attendee_email=email)
        if match:
            qualifiers = [q for q in (match["seniority"], match["department"]) if q]
            annotation = f" [{' — '.join(qualifiers)}]" if qualifiers else ""
            display = match["name"] if match["name"] else name
            lines.append(f"- {display}{annotation}")
            if match["topics"]:
                lines.append(f"  Typical topics: {', '.join(match['topics'])}")
        else:
            lines.append(f"- {name}")
    return "\n".join(lines)
