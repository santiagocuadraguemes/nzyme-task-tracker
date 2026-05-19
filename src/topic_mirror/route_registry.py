"""Topic Mirror Routes registry.

Reads the Topic Mirror Routes Notion DB once per pipeline tick and exposes:

  - ``Route`` — one routing rule (Match Property, Match Value, target DB).
  - ``load_routes`` — fetch every active row from the DB.
  - ``match_routes`` — given a meeting page's properties, return the
    subset of routes whose Match Property/Match Value the page satisfies.

A single page can match several routes (e.g. ``Detail=["AI & Tech",
"Legal DD"]`` plus ``External Org="White Vega"``); the orchestrator
runs the mirror op for each match independently.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)

# Match Property values defined in the Topic Mirror Routes DB schema.
MATCH_MEETING_TYPE = "Meeting type"
MATCH_DETAIL = "Detail"
MATCH_EXTERNAL_ORG = "External Org"

_VALID_MATCH_PROPERTIES = frozenset({MATCH_MEETING_TYPE, MATCH_DETAIL, MATCH_EXTERNAL_ORG})

# A Notion page/DB ID is 32 hex characters; URLs may have dashes or trailing
# query strings. Pull the last 32-char hex run from the URL.
_NOTION_ID_PATTERN = re.compile(r"([0-9a-fA-F]{32})")


@dataclass(frozen=True)
class Route:
    match_property: str   # "Meeting type" | "Detail" | "External Org"
    match_value: str      # e.g. "AI & Tech"
    target_db_id: str     # 32-char hex DB id (extracted from the Target DB URL)
    label: str            # human-readable label for logs (e.g. "Detail:AI & Tech")


def _extract_db_id_from_url(url: str) -> str | None:
    """Pull the 32-char hex DB id out of a Notion DB URL.

    Notion URLs come in shapes like:
      https://www.notion.so/<workspace>/<title-slug>-<id>?v=<view>
      https://www.notion.so/<id>
      https://www.notion.so/<workspace>/<id>?v=<view>

    The view id (``?v=...``) is also 32 chars, so we strip query strings
    first and grab the LAST 32-char hex run.
    """
    if not url:
        return None
    # Strip query string so the view id doesn't beat the DB id.
    base = url.split("?", 1)[0]
    matches = _NOTION_ID_PATTERN.findall(base)
    return matches[-1] if matches else None


def _read_title(prop: dict[str, Any]) -> str:
    items = prop.get("title") or []
    return "".join(rt.get("plain_text", "") for rt in items).strip()


def _read_rich_text(prop: dict[str, Any]) -> str:
    items = prop.get("rich_text") or []
    return "".join(rt.get("plain_text", "") for rt in items).strip()


def load_routes(client: NotionClientWrapper, db_id: str) -> list[Route]:
    """Read every active row from the Topic Mirror Routes DB.

    Skips rows where ``Active`` is unchecked, Match Property is unknown, or
    Target DB URL doesn't yield a parseable Notion id. Logs (at INFO) the
    skip reason so misconfigured rows surface in CloudWatch.
    """
    response = client.query_database(
        database_id=db_id,
        filter={"property": "Active", "checkbox": {"equals": True}},
    )
    routes: list[Route] = []
    for page in response.get("results", []):
        props = page.get("properties") or {}

        match_prop_sel = props.get("Match Property", {}).get("select") or {}
        match_prop_name = match_prop_sel.get("name", "")
        if match_prop_name not in _VALID_MATCH_PROPERTIES:
            logger.info(
                "Routes DB row %s skipped: Match Property %r not in %s",
                page.get("id", "?")[:8], match_prop_name, sorted(_VALID_MATCH_PROPERTIES),
            )
            continue

        match_value = _read_rich_text(props.get("Match Value", {}))
        if not match_value:
            logger.info(
                "Routes DB row %s skipped: Match Value is empty",
                page.get("id", "?")[:8],
            )
            continue

        target_url = (props.get("Target DB", {}).get("url") or "").strip()
        target_db_id = _extract_db_id_from_url(target_url)
        if not target_db_id:
            logger.warning(
                "Routes DB row %s skipped: Target DB URL %r is not a parseable Notion id",
                page.get("id", "?")[:8], target_url,
            )
            continue

        # Title is human-only; if the user didn't set one, synthesize for logs.
        route_title = _read_title(props.get("Route", {}))
        label = route_title or f"{match_prop_name}:{match_value}"

        routes.append(
            Route(
                match_property=match_prop_name,
                match_value=match_value,
                target_db_id=target_db_id,
                label=label,
            )
        )

    logger.debug("Loaded %d active topic mirror route(s)", len(routes))
    return routes


def _page_values_for(match_property: str, props: dict[str, Any]) -> set[str]:
    """Return the set of tag values the page has for *match_property*.

    Multi-select properties return all selected values; selects return
    a single-value set; missing/unset returns an empty set.
    """
    prop = props.get(match_property, {})
    ptype = prop.get("type")
    if ptype == "multi_select":
        return {(it.get("name") or "").strip() for it in prop.get("multi_select") or []}
    if ptype == "select":
        sel = prop.get("select") or {}
        name = (sel.get("name") or "").strip()
        return {name} if name else set()
    return set()


def match_routes(routes: list[Route], page_properties: dict[str, Any]) -> list[Route]:
    """Return the subset of *routes* whose Match Property/Match Value the page has set."""
    if not routes:
        return []
    cache: dict[str, set[str]] = {}
    matched: list[Route] = []
    for route in routes:
        values = cache.get(route.match_property)
        if values is None:
            values = _page_values_for(route.match_property, page_properties)
            cache[route.match_property] = values
        if route.match_value in values:
            matched.append(route)
    return matched


__all__ = [
    "MATCH_DETAIL",
    "MATCH_EXTERNAL_ORG",
    "MATCH_MEETING_TYPE",
    "Route",
    "load_routes",
    "match_routes",
]
