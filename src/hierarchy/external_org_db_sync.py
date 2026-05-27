"""Mirror Supabase ``ReportingNz_deals`` into the single ``🏢 External Orgs``
Settings database.

Replaces the old per-member ``External Org`` select fan-out (and the Hierarchy
linkage). Instead of pushing deals into the ``External Org`` dropdown on every
member Meeting Notes DB, this sub-sync maintains ONE database in the Nzyme
Settings page whose rows mirror the deal pipeline.

Model:
  * One row per deal, keyed by the Supabase deal UUID stored in the ``Deal ID``
    rich-text property — the row's stable identity, so no mapping table is
    needed.
  * Deals in one of the 4 tracked stages get a row created. Rows are **never**
    deleted: once a row exists its ``Name`` / ``Stage`` are kept current even
    after the deal leaves the tracked stages.
  * Stage drives the option color (Portfolio → orange, dealflow → blue) and the
    dropdown's display order (declared option order = priority). Colors live in
    the DB schema; this module only writes the stage NAME — Notion auto-creates
    an option (default color) for any stage outside the tracked four. Stage
    names are comma-stripped via ``_sanitize_option_name`` because Notion
    forbids commas in select option names.
  * Rows without a ``Deal ID`` are left untouched (manual rows, if any).

Sharing: Supabase HTTP helpers via cross-import from ``canonical_mirror_sync``;
the select-name sanitizer from ``macro_block_sync``.

Cost: zero LLM. Notion + Supabase REST only. Per tick: one paginated query of
the External Orgs DB + one create/update page call per changed row.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.config import SyncConfig
from src.hierarchy.base import SyncReport
from src.hierarchy.canonical_mirror_sync import _http, _supabase_creds
from src.hierarchy.macro_block_sync import _sanitize_option_name
from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)

SUB_SYNC_NAME = "external_org_db_sync"

_NAME_PROP = "Name"
_STAGE_PROP = "Stage"
_DEAL_ID_PROP = "Deal ID"
_DETAIL_CAP = 50

# Stage taxonomy (hard-coded — revisit when the Sales team changes it). Declared
# in priority order; the DB schema mirrors this order + color so a Stage-sorted
# view reads Portfolio → DD phase → Working → Under analysis.
_STAGE_PORTFOLIO = "Portfolio"
_STAGE_DD_PHASE = "DD phase"
_STAGE_WORKING = "Working on a deal (significant effort)"
_STAGE_UNDER_ANALYSIS = "Under analysis (team assigned, moderate effort)"

_TRACKED_STAGES: tuple[str, ...] = (
    _STAGE_PORTFOLIO,
    _STAGE_DD_PHASE,
    _STAGE_WORKING,
    _STAGE_UNDER_ANALYSIS,
)

# Stage → option color, applied once when the DB schema is set up via Notion
# MCP (kept here as the source-of-truth reference; the module itself never
# writes colors — Notion preserves the schema color on each value write).
_STAGE_TO_COLOR: dict[str, str] = {
    _sanitize_option_name(_STAGE_PORTFOLIO): "orange",
    _sanitize_option_name(_STAGE_DD_PHASE): "blue",
    _sanitize_option_name(_STAGE_WORKING): "blue",
    _sanitize_option_name(_STAGE_UNDER_ANALYSIS): "blue",
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class _Deal:
    deal_id: str  # uuid from ReportingNz_deals.id
    name: str
    stage: str


@dataclass
class _Row:
    page_id: str
    deal_id: str
    name: str
    stage: str


@dataclass
class _PlanItem:
    kind: str  # "create" | "update"
    deal: _Deal
    page_id: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plain_text(items: list[dict[str, Any]] | None) -> str:
    """Join Notion rich-text / title fragments into a plain string."""
    return "".join(rt.get("plain_text", "") for rt in (items or [])).strip()


# ---------------------------------------------------------------------------
# Snapshot loaders
# ---------------------------------------------------------------------------


def _load_deals() -> list[_Deal]:
    """Read every active deal from ``ReportingNz_deals`` (id, name, stage)."""
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
    client: NotionClientWrapper, db_id: str,
) -> dict[str, _Row]:
    """Return ``{deal_id: _Row}`` for every row carrying a ``Deal ID``."""
    response = client.query_database(database_id=db_id)
    out: dict[str, _Row] = {}
    for page in response.get("results", []):
        props = page.get("properties") or {}
        deal_id = _plain_text(props.get(_DEAL_ID_PROP, {}).get("rich_text"))
        if not deal_id:
            continue  # manual / un-keyed row — leave it alone
        name = _plain_text(props.get(_NAME_PROP, {}).get("title"))
        sel = props.get(_STAGE_PROP, {}).get("select")
        stage = (sel or {}).get("name", "") if sel else ""
        out[deal_id] = _Row(
            page_id=page["id"], deal_id=deal_id, name=name, stage=stage,
        )
    return out


# ---------------------------------------------------------------------------
# Pure planner
# ---------------------------------------------------------------------------


def _plan(deals: list[_Deal], existing: dict[str, _Row]) -> list[_PlanItem]:
    """Compute create/update actions. Pure — no I/O.

      * No row + deal in a tracked stage → create.
      * No row + deal outside tracked stages → skip (don't create).
      * Row exists → update if name or stage drifted (rows are never deleted,
        so a deal that left the tracked stages still has its Stage refreshed).
    """
    plan: list[_PlanItem] = []
    for deal in deals:
        row = existing.get(deal.deal_id)
        if row is None:
            if deal.stage in _TRACKED_STAGES:
                plan.append(_PlanItem(kind="create", deal=deal))
            continue
        # ``row.stage`` was written sanitized last tick, so compare against the
        # sanitized desired value to avoid a perpetual no-op rewrite loop.
        if row.name != deal.name or row.stage != _sanitize_option_name(deal.stage):
            plan.append(
                _PlanItem(kind="update", deal=deal, page_id=row.page_id),
            )
    return plan


def _props_for(deal: _Deal) -> dict[str, Any]:
    """Build the Notion page-property payload for a deal row.

    Stage is comma-stripped (Notion forbids commas in select option names);
    Name is stored verbatim (it's a title, commas are fine).
    """
    stage = _sanitize_option_name(deal.stage)
    return {
        _NAME_PROP: {"title": [{"text": {"content": deal.name}}]},
        _STAGE_PROP: (
            {"select": {"name": stage}} if stage else {"select": None}
        ),
        _DEAL_ID_PROP: {"rich_text": [{"text": {"content": deal.deal_id}}]},
    }


# ---------------------------------------------------------------------------
# I/O sync
# ---------------------------------------------------------------------------


def sync(client: NotionClientWrapper, config: SyncConfig) -> SyncReport:
    """Mirror ReportingNz_deals → the ``🏢 External Orgs`` Settings DB."""
    report = SyncReport(name=SUB_SYNC_NAME)

    db_id = config.external_orgs_db_id
    if not db_id:
        report.errors += 1
        report.details.append(
            "EXTERNAL_ORGS_DB_ID not configured — cannot sync External Orgs DB",
        )
        logger.warning(
            "external_org_db_sync: EXTERNAL_ORGS_DB_ID not configured — skipping",
        )
        return report

    try:
        _supabase_creds()
    except RuntimeError as e:
        report.errors += 1
        report.details.append(f"Supabase not configured: {e}")
        logger.warning("external_org_db_sync: %s", e)
        return report

    try:
        deals = _load_deals()
    except Exception as e:  # noqa: BLE001
        report.errors += 1
        report.details.append(
            f"ReportingNz_deals fetch failed: {type(e).__name__}: {e}",
        )
        logger.exception("external_org_db_sync: ReportingNz_deals fetch failed")
        return report

    try:
        existing = _load_existing_rows(client, db_id)
    except Exception as e:  # noqa: BLE001
        report.errors += 1
        report.details.append(
            f"External Orgs DB query failed: {type(e).__name__}: {e}",
        )
        logger.exception("external_org_db_sync: External Orgs DB query failed")
        return report

    plan = _plan(deals, existing)

    if config.dry_run:
        creates = sum(1 for p in plan if p.kind == "create")
        updates = sum(1 for p in plan if p.kind == "update")
        report.created += creates
        report.edited += updates
        report.details.append(
            f"would create={creates} update={updates} (dry-run)",
        )
        logger.info(
            "external_org_db_sync: DRY RUN would create=%d update=%d "
            "(deals=%d existing rows=%d)",
            creates, updates, len(deals), len(existing),
        )
        return report

    for item in plan:
        deal = item.deal
        try:
            if item.kind == "create":
                client.create_page(db_id, _props_for(deal))
                report.created += 1
                report.details.append(
                    f"created {deal.name!r} (stage={deal.stage!r})",
                )
            else:
                client.update_page(item.page_id, _props_for(deal))
                report.edited += 1
                report.details.append(
                    f"updated {deal.name!r} (stage={deal.stage!r})",
                )
        except Exception as e:  # noqa: BLE001
            report.errors += 1
            report.details.append(
                f"deal {deal.deal_id[:8]} ({deal.name!r}) {item.kind} failed: "
                f"{type(e).__name__}: {e}",
            )
            logger.exception(
                "external_org_db_sync: %s failed for deal %s (%s)",
                item.kind, deal.deal_id, deal.name,
            )

    logger.info(
        "external_org_db_sync: created=%d edited=%d errors=%d "
        "(deals=%d existing rows=%d)",
        report.created, report.edited, report.errors, len(deals), len(existing),
    )

    if len(report.details) > _DETAIL_CAP:
        truncated = len(report.details) - _DETAIL_CAP
        report.details = report.details[:_DETAIL_CAP]
        report.details.append(f"… (+{truncated} more details truncated)")

    return report


__all__ = ["SUB_SYNC_NAME", "sync"]
