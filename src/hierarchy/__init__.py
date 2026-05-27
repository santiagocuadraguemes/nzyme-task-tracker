"""Hierarchy DB → downstream Notion state sync orchestrator.

Each tick fans out across every registered sub-sync. Failures in one sub-sync
do NOT abort the others — they log loudly and the orchestrator continues.

Add a new sub-sync by writing ``src/hierarchy/<name>_sync.py`` exposing a
top-level ``sync(client, config) -> SyncReport`` and appending it to
``_SUB_SYNCS`` below.
"""
from __future__ import annotations

import logging

from src.config import SyncConfig
from src.hierarchy import (
    canonical_mirror_sync,
    detail_applier_sync,
    detail_canonical_mirror_sync,
    external_org_db_sync,
    macro_block_sync,
    tracker_applier_sync,
)
from src.hierarchy.base import SubSync, SyncReport
from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)


# Order matters: every canonical mirror runs before the applier(s) reading
# it (PR2: hierarchy_rows / PR4: detail_rows). Among appliers, order is
# independent (different Notion targets; different mapping tables).
# external_org_db_sync has no canonical mirror — it reads ReportingNz_deals
# live each tick and mirrors it into the single External Orgs Settings DB
# (no member-DB fan-out).
_SUB_SYNCS: list[SubSync] = [
    canonical_mirror_sync.sync,          # Hierarchy DB → hierarchy_rows
    detail_canonical_mirror_sync.sync,   # Detail Options Settings DB → detail_rows
    macro_block_sync.sync,               # Tier 0 → member-DB Macro Work Block
    detail_applier_sync.sync,            # detail_rows → member-DB Detail
    external_org_db_sync.sync,           # ReportingNz_deals → External Orgs Settings DB
    tracker_applier_sync.sync,           # hierarchy_rows → Team Task Tracker
]


def run_all(
    client: NotionClientWrapper,
    config: SyncConfig,
    only: list[str] | None = None,
) -> list[SyncReport]:
    """Run every registered sub-sync. Returns one SyncReport per sub-sync.

    ``only`` filters by module name (e.g. ``["canonical_mirror_sync"]``).
    Unknown names raise ``ValueError`` rather than silently running nothing.
    """
    selected = _SUB_SYNCS
    if only:
        known = {
            getattr(s, "__module__", "?").rsplit(".", 1)[-1]: s
            for s in _SUB_SYNCS
        }
        missing = [n for n in only if n not in known]
        if missing:
            raise ValueError(
                f"Unknown sub-sync(s): {missing}. Available: {list(known)}",
            )
        selected = [known[n] for n in only]

    reports: list[SyncReport] = []
    for sub_sync in selected:
        sub_name = getattr(sub_sync, "__module__", "?").rsplit(".", 1)[-1]
        try:
            report = sub_sync(client, config)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "hierarchy_sync: name=%s crashed unexpectedly", sub_name,
            )
            report = SyncReport(
                name=sub_name,
                errors=1,
                details=[f"unhandled exception: {type(e).__name__}: {e}"],
            )
        reports.append(report)
        logger.info(
            "hierarchy_sync: name=%s created=%d renamed=%d archived=%d "
            "edited=%d deleted=%d reactivated=%d parent_fixed=%d errors=%d",
            report.name, report.created, report.renamed, report.archived,
            report.edited, report.deleted, report.reactivated,
            report.parent_fixed, report.errors,
        )
    return reports


__all__ = ["SyncReport", "run_all"]
