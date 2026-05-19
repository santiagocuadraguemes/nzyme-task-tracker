"""Meeting Mirrors feature — clone tagged meetings into topic-specific Notion DBs.

The orchestrator ``mirror_to_topic_dbs`` is called by
``src.pipeline.run_sync_for_page`` after the primary task-tracker write
succeeds. Routing is config-as-data: rows in the Topic Mirror Routes DB
map a meeting tag (Meeting type / Detail / External Org) to a target DB.

Cross-DB dedup key = ``normalize(title) + Date.start[:10]``. The first
contributor processed becomes the "primary" — their source page is cloned
via Notion's ``template: {type: 'template_id', template_id: <src>}``
mechanism, which copies the AI-managed ``meeting_notes`` block (transcript,
AI Summary, attendees, their notes). Subsequent contributors do NOT
re-clone; instead their ``## Notes`` content is appended INSIDE the
mirror's ``notes_block_id`` under a ``### <Name>'s Notes`` heading.

Never raises into the caller — every failure mode maps to a
``MirrorOutcome``. The caller logs a structured ``topic mirror outcome:``
line per page so silent failures become grep-able in CloudWatch.
"""
from __future__ import annotations

import logging

from src.config import SyncConfig
from src.notion_client_wrapper import NotionClientWrapper
from src.topic_mirror.outcome import MirrorAction, MirrorOutcome, MirrorStatus
from src.topic_mirror.route_registry import Route, load_routes, match_routes
from src.topic_mirror.writer import clone_or_merge

logger = logging.getLogger(__name__)


def mirror_to_topic_dbs(
    *,
    config: SyncConfig,
    client: NotionClientWrapper,
    source_page: dict,
    metadata: dict,
    owner_user_id: str,
    owner_name: str,
) -> MirrorOutcome:
    """Mirror a single meeting page into every matching topic DB.

    Parameters
    ----------
    source_page:
        Notion page dict (as returned by ``client.get_page``) — read for
        properties so we can match against the route table.
    metadata:
        Output of ``SingleSource.get_page_metadata(page)``. Used for the
        cross-DB dedup key (title + date).
    owner_user_id:
        Notion user UUID written to the mirror's ``Owner`` people property.
        Empty string means Owner is left unset; merge-path dedup falls back
        to skipping (no UUID → can't compare against existing Owner list).
    owner_name:
        Display name used for the appended ``### <Name>'s Notes`` H3
        heading when this contributor is the 2nd/3rd/... to tag the same
        meeting. The Owner UUID is the dedup key; the name is what readers see.
        Falls back to "Unknown" if empty.
    """
    if not config.topic_mirror_enabled:
        return MirrorOutcome(status=MirrorStatus.DISABLED, detail="topic_mirror_enabled=False")

    if not config.topic_mirror_routes_db_id:
        return MirrorOutcome(
            status=MirrorStatus.DISABLED,
            detail="TOPIC_MIRROR_ROUTES_DB_ID not configured",
        )

    try:
        all_routes = load_routes(client, config.topic_mirror_routes_db_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to load topic mirror routes")
        return MirrorOutcome(
            status=MirrorStatus.FAILED,
            detail=f"route registry load failed: {type(e).__name__}: {e}",
        )

    matched = match_routes(all_routes, source_page.get("properties", {}))
    if not matched:
        return MirrorOutcome(
            status=MirrorStatus.NO_MATCH,
            detail=f"page tags matched 0 of {len(all_routes)} active route(s)",
        )

    title = metadata.get("title", "")
    date = metadata.get("date", "")

    actions: list[tuple[str, MirrorAction]] = []
    failures: list[tuple[str, str]] = []
    for route in matched:
        try:
            action = clone_or_merge(
                client=client,
                route=route,
                source_page=source_page,
                source_title=title,
                source_date=date,
                owner_user_id=owner_user_id,
                owner_name=owner_name or "Unknown",
            )
            actions.append((route.label, action))
        except Exception as e:  # noqa: BLE001
            logger.exception("Mirror failed for route %s", route.label)
            failures.append((route.label, f"{type(e).__name__}: {e}"))

    if failures and not actions:
        return MirrorOutcome(
            status=MirrorStatus.FAILED,
            detail="; ".join(f"{r}: {err}" for r, err in failures),
        )

    by_action = {
        MirrorAction.CLONED: [r for r, a in actions if a == MirrorAction.CLONED],
        MirrorAction.MERGED: [r for r, a in actions if a == MirrorAction.MERGED],
        MirrorAction.NOOP: [r for r, a in actions if a == MirrorAction.NOOP],
    }
    summary_parts: list[str] = []
    if by_action[MirrorAction.CLONED]:
        summary_parts.append(f"cloned=[{','.join(by_action[MirrorAction.CLONED])}]")
    if by_action[MirrorAction.MERGED]:
        summary_parts.append(f"merged=[{','.join(by_action[MirrorAction.MERGED])}]")
    if by_action[MirrorAction.NOOP]:
        summary_parts.append(f"noop=[{','.join(by_action[MirrorAction.NOOP])}]")
    if failures:
        summary_parts.append(f"failed=[{','.join(r for r, _ in failures)}]")

    status = (
        MirrorStatus.PARTIAL_FAILURE if failures else MirrorStatus.POSTED
    )
    return MirrorOutcome(status=status, detail=" ".join(summary_parts))


__all__ = [
    "MirrorAction",
    "MirrorOutcome",
    "MirrorStatus",
    "Route",
    "mirror_to_topic_dbs",
]
