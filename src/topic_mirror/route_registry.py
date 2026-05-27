"""Meeting Rules registry (was: Topic Mirror Routes).

Reads the Meeting Rules Notion DB once per pipeline tick and exposes:

  - ``Route`` — one rule (Match Property, Match Value, Action, optional target DB).
  - ``load_routes`` — fetch every active row from the DB.
  - ``match_routes`` — given a meeting page's properties, return the
    subset of routes whose Match Property/Match Value the page satisfies.

A single page can match several routes (e.g. ``Detail=["AI & Tech",
"Legal DD"]`` plus ``Macro Work Block="LPs & Fundraising"``). Each
consumer filters the matched list by ``action`` and runs its own
operation:

  - ``Mirror to DB`` (default) → consumed by ``src.topic_mirror``.
  - ``Fire Affinity LP Funnel`` → consumed by the Fundraising branch in
    ``src.pipeline``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)

# Match Property values defined in the Meeting Rules DB schema.
MATCH_MACRO_WORK_BLOCK = "Macro Work Block"
MATCH_DETAIL = "Detail"
MATCH_EXTERNAL_ORG = "External Org"

_VALID_MATCH_PROPERTIES = frozenset({MATCH_MACRO_WORK_BLOCK, MATCH_DETAIL, MATCH_EXTERNAL_ORG})

# Action values defined in the Meeting Rules DB schema. Rows with an unset
# Action cell default to ACTION_MIRROR_TO_DB (back-compat for rows that
# pre-date the schema change).
ACTION_MIRROR_TO_DB = "Mirror to DB"
ACTION_AFFINITY_LP_FUNNEL = "Fire Affinity LP Funnel"

_VALID_ACTIONS = frozenset({ACTION_MIRROR_TO_DB, ACTION_AFFINITY_LP_FUNNEL})

# A Notion page/DB ID is 32 hex characters; URLs may have dashes or trailing
# query strings. Pull the last 32-char hex run from the URL.
_NOTION_ID_PATTERN = re.compile(r"([0-9a-fA-F]{32})")


@dataclass(frozen=True)
class Route:
    match_property: str   # "Macro Work Block" | "Detail" | "External Org"
    match_value: str      # e.g. "AI & Tech"
    target_db_id: str     # 32-char hex DB id; "" when action != Mirror to DB
    label: str            # human-readable label for logs (e.g. "Detail:AI & Tech")
    action: str = ACTION_MIRROR_TO_DB   # what consumer runs this rule


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
    """Read every active row from the Meeting Rules DB.

    Skips rows where ``Active`` is unchecked, Match Property is unknown,
    Match Value is empty, Action is unknown, or — for ``Mirror to DB``
    actions specifically — the Target DB URL doesn't parse. Other actions
    don't need a Target DB; the cell is ignored.

    Logs the skip reason so misconfigured rows surface in CloudWatch.
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
                "Meeting Rules row %s skipped: Match Property %r not in %s",
                page.get("id", "?")[:8], match_prop_name, sorted(_VALID_MATCH_PROPERTIES),
            )
            continue

        match_value = _read_rich_text(props.get("Match Value", {}))
        if not match_value:
            logger.info(
                "Meeting Rules row %s skipped: Match Value is empty",
                page.get("id", "?")[:8],
            )
            continue

        # Action defaults to Mirror to DB when the cell is empty — back-compat
        # for rows that pre-date the `Action` column.
        action_sel = props.get("Action", {}).get("select") or {}
        action = action_sel.get("name", "") or ACTION_MIRROR_TO_DB
        if action not in _VALID_ACTIONS:
            logger.info(
                "Meeting Rules row %s skipped: Action %r not in %s",
                page.get("id", "?")[:8], action, sorted(_VALID_ACTIONS),
            )
            continue

        target_url = (props.get("Target DB", {}).get("url") or "").strip()
        target_db_id = _extract_db_id_from_url(target_url) or ""
        if action == ACTION_MIRROR_TO_DB and not target_db_id:
            logger.warning(
                "Meeting Rules row %s skipped: Action='%s' needs a parseable "
                "Target DB URL (got %r)",
                page.get("id", "?")[:8], action, target_url,
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
                action=action,
            )
        )

    logger.debug("Loaded %d active meeting rule(s)", len(routes))
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
    "ACTION_AFFINITY_LP_FUNNEL",
    "ACTION_MIRROR_TO_DB",
    "MATCH_DETAIL",
    "MATCH_EXTERNAL_ORG",
    "MATCH_MACRO_WORK_BLOCK",
    "Route",
    "load_routes",
    "match_routes",
]
