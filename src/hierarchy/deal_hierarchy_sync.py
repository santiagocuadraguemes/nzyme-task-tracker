"""Mirror Supabase ``ReportingNz_deals`` → rows in the Notion **Hierarchy DB**.

This is the *producer* that turns the deal pipeline into first-class hierarchy
nodes, so the rest of the existing machinery (``canonical_mirror_sync`` →
``hierarchy_rows`` → ``tracker_applier_sync`` → ``[DETAILS INSIDE]`` tracker
nodes) materialises a fileable node per tracked deal **for free**. It MUST run
first in the orchestrator, before ``canonical_mirror_sync`` snapshots the
Hierarchy DB.

Model:
  * One Hierarchy row per tracked deal, keyed by the Supabase deal UUID stored
    in the ``Deal ID`` rich-text property — the row's stable identity. Rows
    **without** a ``Deal ID`` are hand-made nodes and are NEVER touched.
  * Stage → destination (verified against live Supabase 2026-06-02):

      +-------------------------------------------------+-----------------------------+---------------+
      | Stage                                           | Parent anchor               | Tier          |
      +-------------------------------------------------+-----------------------------+---------------+
      | Portfolio                                       | Value Creation for Portfolio| 1. Project    |
      | DD phase                                        | Dealflow - Main Opportunities| 2. Workstream|
      | Working on a deal (significant effort)          | Dealflow - Main Opportunities| 2. Workstream|
      | Under analysis (team assigned, moderate effort) | Dealflow - Main Opportunities| 2. Workstream|
      +-------------------------------------------------+-----------------------------+---------------+

  * **Soft-archive on stage exit**: when a deal leaves the tracked stages (or
    disappears from the live snapshot entirely, e.g. ``is_active=false`` in
    Affinity), its row is set ``Active=false`` — the downstream
    ``tracker_applier_sync`` then renames the matching tracker node to
    ``(archived) X``. Name / Tier / Parent are left untouched. Re-entry into a
    tracked stage flips ``Active`` back to ``true`` and refreshes the row.
    Rows are **never** deleted.

Sharing: Supabase HTTP helpers via cross-import from ``canonical_mirror_sync``;
the Notion property readers (``_read_title`` etc.) from the same module.

Cost: zero LLM. Notion + Supabase REST only. Per tick: one ``ReportingNz_deals``
query + one paginated Hierarchy DB query + one create/update page call per
changed row.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.config import SyncConfig
from src.hierarchy.base import SyncReport
from src.hierarchy.canonical_mirror_sync import (
    _http,
    _read_relation_first,
    _read_rich_text,
    _read_select_name,
    _read_title,
    _supabase_creds,
)
from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)

SUB_SYNC_NAME = "deal_hierarchy_sync"

_NAME_PROP = "Name"
_TIER_PROP = "Tier"
_ACTIVE_PROP = "Active"
_PARENT_PROP = "Parent item"
_DEAL_ID_PROP = "Deal ID"
_DETAIL_CAP = 50

# Stage taxonomy (hard-coded — revisit when the deal team changes it).
_STAGE_PORTFOLIO = "Portfolio"
_STAGE_DD_PHASE = "DD phase"
_STAGE_WORKING = "Working on a deal (significant effort)"
_STAGE_UNDER_ANALYSIS = "Under analysis (team assigned, moderate effort)"

_DEALFLOW_STAGES: tuple[str, ...] = (
    _STAGE_DD_PHASE,
    _STAGE_WORKING,
    _STAGE_UNDER_ANALYSIS,
)
_TRACKED_STAGES: tuple[str, ...] = (_STAGE_PORTFOLIO, *_DEALFLOW_STAGES)

# Hierarchy DB anchor pages (Notion page ids) — confirmed live 2026-06-02.
_ANCHOR_PORTFOLIO = "c3a645bf-edae-4176-9373-4b0f958f3c72"  # "Value Creation for Portfolio" (Tier 0)
_ANCHOR_DEALFLOW = "009aebf3-8d24-4862-b67f-0978390db56b"   # "Dealflow - Main Opportunities" (Tier 1)

_TIER_PROJECT = "1. Project"      # Portfolio companies land here (under a Tier 0 block)
_TIER_WORKSTREAM = "2. Workstream"  # Dealflow opportunities land here (under a Tier 1 project)

_ANCHORS = frozenset({_ANCHOR_PORTFOLIO, _ANCHOR_DEALFLOW})


def _norm(name: str) -> str:
    """Normalize a row/deal name for adoption matching (case + whitespace)."""
    return " ".join((name or "").split()).casefold()


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class _Deal:
    deal_id: str  # uuid from ReportingNz_deals.id
    name: str
    stage: str

    @property
    def tracked(self) -> bool:
        return self.stage in _TRACKED_STAGES

    @property
    def desired_parent(self) -> str:
        return _ANCHOR_PORTFOLIO if self.stage == _STAGE_PORTFOLIO else _ANCHOR_DEALFLOW

    @property
    def desired_tier(self) -> str:
        return _TIER_PROJECT if self.stage == _STAGE_PORTFOLIO else _TIER_WORKSTREAM


@dataclass
class _Row:
    page_id: str
    deal_id: str
    name: str
    tier: str
    active: bool
    parent_id: str | None


@dataclass
class _PlanItem:
    kind: str  # "create" | "edited" | "reactivated" | "archived"
    page_id: str | None
    properties: dict[str, Any]
    label: str  # human-readable, for detail lines


# ---------------------------------------------------------------------------
# Snapshot loaders
# ---------------------------------------------------------------------------


def _load_deals() -> list[_Deal]:
    """Read every active deal from ``ReportingNz_deals`` (id, name, stage).

    Loads ALL stages — the planner needs to see deals that left the tracked
    set to soft-archive them.
    """
    raw = _http(
        "GET",
        "/rest/v1/ReportingNz_deals?select=id,name,stage&is_active=eq.true"
        "&limit=10000",
    ) or []
    deals: list[_Deal] = []
    for r in raw:
        deal_id = r.get("id")
        name = (r.get("name") or "").strip()
        stage = r.get("stage") or ""
        if not deal_id or not name:
            continue
        deals.append(_Deal(deal_id=deal_id, name=name, stage=stage))
    return deals


def _load_existing_rows(
    client: NotionClientWrapper, hierarchy_db_id: str,
) -> tuple[dict[str, _Row], dict[str, _Row]]:
    """Snapshot the Hierarchy DB into two buckets:

    * ``owned`` — ``{deal_id: _Row}`` for rows that already carry a ``Deal ID``
      (created/adopted by a previous run of this sync).
    * ``adoptable`` — ``{normalized_name: _Row}`` for hand-made rows (empty
      ``Deal ID``) that sit directly under one of the two deal anchors. These
      are the curated company rows a tracked deal should ADOPT (stamp its
      ``Deal ID`` onto) rather than duplicate. Restricting to anchor children
      avoids matching unrelated same-named nodes elsewhere in the tree.
    """
    response = client.query_database(database_id=hierarchy_db_id)
    owned: dict[str, _Row] = {}
    adoptable: dict[str, _Row] = {}
    for page in response.get("results", []):
        props = page.get("properties") or {}
        # Title is found by name (writer addresses it as "Name"); fall back to
        # the first title-typed prop for resilience.
        name = _read_title(props.get(_NAME_PROP) or {})
        if not name:
            for prop in props.values():
                if prop.get("type") == "title":
                    name = _read_title(prop)
                    break
        row = _Row(
            page_id=page["id"],
            deal_id=_read_rich_text(props.get(_DEAL_ID_PROP)),
            name=name,
            tier=_read_select_name(props.get(_TIER_PROP)),
            active=bool((props.get(_ACTIVE_PROP) or {}).get("checkbox", False)),
            parent_id=_read_relation_first(props.get(_PARENT_PROP)),
        )
        if row.deal_id:
            owned[row.deal_id] = row
        elif row.parent_id in _ANCHORS and row.name:
            adoptable.setdefault(_norm(row.name), row)
    return owned, adoptable


# ---------------------------------------------------------------------------
# Property payload builders
# ---------------------------------------------------------------------------


def _create_props(deal: _Deal) -> dict[str, Any]:
    return {
        _NAME_PROP: {"title": [{"text": {"content": deal.name}}]},
        _TIER_PROP: {"select": {"name": deal.desired_tier}},
        _ACTIVE_PROP: {"checkbox": True},
        _PARENT_PROP: {"relation": [{"id": deal.desired_parent}]},
        _DEAL_ID_PROP: {"rich_text": [{"text": {"content": deal.deal_id}}]},
    }


def _stamp_deal_id_props(deal: _Deal) -> dict[str, Any]:
    """Adoption payload: stamp the Deal ID onto an existing hand-made row.

    Only the ``Deal ID`` is written — name/tier/parent are left exactly as the
    team curated them. This is the key fix: the writer adopts the existing row
    instead of creating a duplicate.
    """
    return {_DEAL_ID_PROP: {"rich_text": [{"text": {"content": deal.deal_id}}]}}


# ---------------------------------------------------------------------------
# Pure planner
# ---------------------------------------------------------------------------


def _plan(
    deals: list[_Deal],
    owned: dict[str, _Row],
    adoptable: dict[str, _Row],
) -> list[_PlanItem]:
    """Compute adopt/create/soft-archive actions. Pure — no I/O.

      * tracked deal, already owned (Deal ID stamped) → only toggle ``Active``
        (reactivate if it was archived). Name/tier/parent are NEVER rewritten —
        the hierarchy is human-curated; the sync must not re-home or rename.
      * tracked deal, not owned, but a hand-made anchor-child shares its name →
        ADOPT that row (stamp its Deal ID). No duplicate created.
      * tracked deal, not owned, no name match → create a new row.
      * untracked deal (or a deal that vanished from the snapshot), owned row
        still active → soft-archive (``Active=false``). Idempotent.
      * untracked deal, no owned row → skip.
    """
    plan: list[_PlanItem] = []
    snapshot_ids: set[str] = set()

    for deal in deals:
        snapshot_ids.add(deal.deal_id)
        row = owned.get(deal.deal_id)

        if row is None:
            if not deal.tracked:
                continue
            adopt = adoptable.get(_norm(deal.name))
            if adopt is not None:
                plan.append(_PlanItem(
                    kind="adopted",
                    page_id=adopt.page_id,
                    properties=_stamp_deal_id_props(deal),
                    label=f"{deal.name!r} → existing row {adopt.page_id[:8]}",
                ))
            else:
                plan.append(_PlanItem(
                    kind="create",
                    page_id=None,
                    properties=_create_props(deal),
                    label=f"{deal.name!r} (stage={deal.stage!r})",
                ))
            continue

        # Owned row exists → only flip Active; never rename/re-home.
        if deal.tracked:
            if row.active is not True:
                plan.append(_PlanItem(
                    kind="reactivated",
                    page_id=row.page_id,
                    properties={_ACTIVE_PROP: {"checkbox": True}},
                    label=f"{row.name!r} (back in tracked stages)",
                ))
        elif row.active is not False:
            plan.append(_PlanItem(
                kind="archived",
                page_id=row.page_id,
                properties={_ACTIVE_PROP: {"checkbox": False}},
                label=f"{row.name!r} (left tracked stages, stage={deal.stage!r})",
            ))

    # Owned rows whose deal disappeared from the live snapshot entirely
    # (e.g. is_active=false in Affinity) → soft-archive too.
    for deal_id, row in owned.items():
        if deal_id in snapshot_ids:
            continue
        if row.active is not False:
            plan.append(_PlanItem(
                kind="archived",
                page_id=row.page_id,
                properties={_ACTIVE_PROP: {"checkbox": False}},
                label=f"{row.name!r} (deal absent from snapshot)",
            ))

    return plan


# ---------------------------------------------------------------------------
# I/O sync
# ---------------------------------------------------------------------------


def sync(client: NotionClientWrapper, config: SyncConfig) -> SyncReport:
    """Mirror ReportingNz_deals → rows in the Notion Hierarchy DB."""
    report = SyncReport(name=SUB_SYNC_NAME)

    hierarchy_db_id = config.hierarchy_db_id
    if not hierarchy_db_id:
        report.errors += 1
        report.details.append(
            "HIERARCHY_DB_ID not configured — cannot sync deal hierarchy rows",
        )
        logger.warning(
            "deal_hierarchy_sync: HIERARCHY_DB_ID not configured — skipping",
        )
        return report

    try:
        _supabase_creds()
    except RuntimeError as e:
        report.errors += 1
        report.details.append(f"Supabase not configured: {e}")
        logger.warning("deal_hierarchy_sync: %s", e)
        return report

    try:
        deals = _load_deals()
    except Exception as e:  # noqa: BLE001
        report.errors += 1
        report.details.append(
            f"ReportingNz_deals fetch failed: {type(e).__name__}: {e}",
        )
        logger.exception("deal_hierarchy_sync: ReportingNz_deals fetch failed")
        return report

    try:
        owned, adoptable = _load_existing_rows(client, hierarchy_db_id)
    except Exception as e:  # noqa: BLE001
        report.errors += 1
        report.details.append(
            f"Hierarchy DB query failed: {type(e).__name__}: {e}",
        )
        logger.exception("deal_hierarchy_sync: Hierarchy DB query failed")
        return report

    plan = _plan(deals, owned, adoptable)

    def _count(kind: str) -> int:
        return sum(1 for p in plan if p.kind == kind)

    if config.dry_run:
        # adopted/reactivated are both edits to an existing row.
        report.created += _count("create")
        report.edited += _count("adopted")
        report.reactivated += _count("reactivated")
        report.archived += _count("archived")
        report.details.append(
            f"would create={_count('create')} adopt={_count('adopted')} "
            f"reactivate={_count('reactivated')} archive={_count('archived')} "
            "(dry-run)",
        )
        logger.info(
            "deal_hierarchy_sync: DRY RUN create=%d adopt=%d reactivate=%d "
            "archive=%d (deals=%d owned=%d adoptable=%d)",
            _count("create"), _count("adopted"), _count("reactivated"),
            _count("archived"), len(deals), len(owned), len(adoptable),
        )
        return report

    for item in plan:
        try:
            if item.kind == "create":
                client.create_page(hierarchy_db_id, item.properties)
                report.created += 1
            else:
                client.update_page(item.page_id, item.properties)
                if item.kind == "adopted":
                    report.edited += 1
                elif item.kind == "reactivated":
                    report.reactivated += 1
                else:  # archived
                    report.archived += 1
            report.details.append(f"{item.kind} {item.label}")
        except Exception as e:  # noqa: BLE001
            report.errors += 1
            report.details.append(
                f"{item.kind} {item.label} failed: {type(e).__name__}: {e}",
            )
            logger.exception(
                "deal_hierarchy_sync: %s failed for %s", item.kind, item.label,
            )

    logger.info(
        "deal_hierarchy_sync: created=%d adopted=%d reactivated=%d archived=%d "
        "errors=%d (deals=%d owned=%d adoptable=%d)",
        report.created, report.edited, report.reactivated, report.archived,
        report.errors, len(deals), len(owned), len(adoptable),
    )

    if len(report.details) > _DETAIL_CAP:
        truncated = len(report.details) - _DETAIL_CAP
        report.details = report.details[:_DETAIL_CAP]
        report.details.append(f"… (+{truncated} more details truncated)")

    return report


__all__ = ["SUB_SYNC_NAME", "sync"]
