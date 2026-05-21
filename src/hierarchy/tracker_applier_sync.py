"""Apply Supabase canonical (``public.hierarchy_rows``) → Team Task Tracker
``[DETAILS INSIDE]`` rows.

Runs daily 07:00 Madrid as the third sub-sync in the ``hierarchy_sync``
orchestrator, **after** ``canonical_mirror_sync`` (which writes today's
Notion state into Supabase). This applier reads the freshly-updated
canonical and reconciles the Tracker side.

Contract:
- Every canonical row should have a matching ``[DETAILS INSIDE]`` Tracker
  row paired via ``hierarchy_rows.tracker_node_page_id``. Missing
  mappings are created here on first sight, and the new id is back-filled
  into Supabase (authoritative) and the Notion ``Tracker Node`` relation
  (best-effort, human-readable cache).
- Tracker title tracks canonical ``name``:
  * live (``deleted_at IS NULL``) + ``active`` → ``name``
  * live + inactive (``active=false``)          → ``(archived) name``
  * tombstoned (``deleted_at`` set)             → **Notion-archived**
    (the ``[DETAILS INSIDE]`` row is removed from the tracker entirely,
    Supabase ``tracker_node_page_id`` cleared). Inactive-but-not-tombstoned
    rows still soft-archive in the title to preserve context for the
    classifier.
- Tracker ``Parent item`` tracks the parent canonical row's
  ``tracker_node_page_id`` (or empty for roots).
- When the parent canonical row is tombstoned, the child's ``Parent item``
  is cleared (parent's Tracker page is archived, so the link would point
  at an archived/greyed-out page). Inactive-but-not-tombstoned parents
  still keep the link — their tracker rows exist as ``(archived) X``.

Two-pass create-then-reconcile so parents resolve cleanly even when both
parent and child are created in the same run:

  Pass 1 — create missing Tracker rows (title only, no parent yet);
           PATCH Supabase ``tracker_node_page_id`` (authoritative);
           best-effort Notion ``Tracker Node`` relation write.
  Pass 2 — re-plan against mutated snapshots; apply title + Parent item
           in a single ``update_page`` per Tracker row.

Sharing: Supabase HTTP helpers (``_supabase_creds``, ``_http``) are
re-used via cross-import from ``canonical_mirror_sync`` to keep
HTTP/auth code in one place. The leading underscore signals they remain
module-internal — PR2's ``macro_block_sync`` rewrite will follow the same
pattern.

Non-goals:
- Does not touch ``Status``, ``Assignee``, ``Due Date``, ``Category``,
  or ``Sub-item`` on Tracker rows.
- Does not move existing real tasks parented to a renamed node (parents
  are stored by id; renames don't break child→parent relations).
- Does not garbage-collect orphan ``(archived) X`` rows.
- Does not retire the Notion ``Tracker Node`` relation — it stays as a
  human-readable cache (Supabase is authoritative).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.config import SyncConfig
from src.hierarchy.base import SyncReport
from src.hierarchy.canonical_mirror_sync import _http, _supabase_creds
from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)

SUB_SYNC_NAME = "tracker_applier_sync"

_ARCHIVED_PREFIX = "(archived) "
_DETAILS_INSIDE = "[DETAILS INSIDE]"
_TITLE_CAP = 2000  # Notion title hard cap.
_DETAIL_CAP = 50   # Bound report.details so big runs don't blow up the log.


# ---------------------------------------------------------------------------
# Snapshot dataclasses
# ---------------------------------------------------------------------------


@dataclass
class _CanonicalRow:
    """One row from Supabase ``public.hierarchy_rows``, normalized."""

    notion_page_id: str
    name: str
    tier: str
    active: bool
    parent_notion_page_id: str | None
    tracker_node_page_id: str | None
    deleted_at: str | None  # ISO timestamp string, or None when live


@dataclass
class _PlannerResult:
    """Output of ``_plan_tracker_updates``. Pure data, no I/O."""

    to_create: list[_CanonicalRow] = field(default_factory=list)
    # (tracker_id, patch_payload) — payload contains only changed fields.
    to_update: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # Tombstoned canonical rows whose live tracker page should be
    # Notion-archived. ``(canonical_row, tracker_id)``.
    to_archive: list[tuple[_CanonicalRow, str]] = field(default_factory=list)
    # Tombstoned canonical rows whose ``tracker_node_page_id`` points at a
    # tracker page that's already gone (no longer in the snapshot — usually
    # because it was Notion-archived manually). Mapping in Supabase must be
    # cleared so the row stops being processed.
    to_clear_canonical: list[_CanonicalRow] = field(default_factory=list)
    created: int = 0
    renamed: int = 0
    parent_fixed: int = 0
    archived: int = 0
    deleted: int = 0
    details: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Snapshot loaders
# ---------------------------------------------------------------------------


def _load_canonical_snapshot() -> list[_CanonicalRow]:
    """Read every row from ``hierarchy_rows`` — including tombstones.

    Tombstoned rows still drive a desired Tracker state
    (``(archived) name``), so we cannot pre-filter them at the SQL level.
    """
    raw = _http(
        "GET",
        "/rest/v1/hierarchy_rows?select=notion_page_id,name,tier,active,"
        "parent_notion_page_id,tracker_node_page_id,deleted_at&limit=10000",
    ) or []
    rows = [
        _CanonicalRow(
            notion_page_id=r["notion_page_id"],
            name=r.get("name") or "",
            tier=r.get("tier") or "",
            active=bool(r.get("active")),
            parent_notion_page_id=r.get("parent_notion_page_id"),
            tracker_node_page_id=r.get("tracker_node_page_id"),
            deleted_at=r.get("deleted_at"),
        )
        for r in raw
        if r.get("notion_page_id")
    ]
    # Deterministic order for dry-run logs.
    rows.sort(key=lambda r: (r.tier, r.name))
    return rows


def _read_title(prop: dict[str, Any]) -> str:
    items = prop.get("title") or []
    return "".join(rt.get("plain_text", "") for rt in items).strip()


def _first_relation_id(prop: dict[str, Any] | None) -> str | None:
    rels = (prop or {}).get("relation") or []
    return rels[0]["id"] if rels else None


def _load_tracker_snapshot(
    client: NotionClientWrapper, team_tracker_db_id: str,
) -> dict[str, dict[str, Any]]:
    """Return ``{tracker_id: {"title": str, "parent_id": str | None}}``.

    Filtered to ``Priority = '[DETAILS INSIDE]'``. Notion-archived pages
    are excluded (we never want to PATCH an archived page).
    """
    response = client.query_database(
        database_id=team_tracker_db_id,
        filter={"property": "Priority", "select": {"equals": _DETAILS_INSIDE}},
    )
    out: dict[str, dict[str, Any]] = {}
    for page in response.get("results", []):
        page_id = page.get("id") or ""
        if not page_id:
            continue
        if page.get("archived"):
            logger.warning(
                "tracker_applier_sync: tracker row %s is Notion-archived — skipping",
                page_id[:8],
            )
            continue
        props = page.get("properties") or {}
        title = ""
        for prop in props.values():
            if prop.get("type") == "title":
                title = _read_title(prop)
                break
        out[page_id] = {
            "title": title,
            "parent_id": _first_relation_id(props.get("Parent item")),
        }
    return out


# ---------------------------------------------------------------------------
# Pure planner
# ---------------------------------------------------------------------------


def _desired_title(row: _CanonicalRow) -> str:
    """Compute the Tracker title that should reflect a non-tombstoned row.

    Tombstoned rows have their tracker page Notion-archived entirely, so the
    title doesn't matter for them — the defensive prefix logic stays here for
    inactive-but-not-tombstoned rows.
    """
    is_archived_state = (not row.active) and (row.deleted_at is None)
    base = f"{_ARCHIVED_PREFIX}{row.name}" if is_archived_state else row.name
    return base[:_TITLE_CAP]


def _plan_tracker_updates(
    canonical_rows: list[_CanonicalRow],
    tracker_snapshot: dict[str, dict[str, Any]],
) -> _PlannerResult:
    """Compute the create + update + archive plan. No I/O, no logging side effects."""
    result = _PlannerResult()

    # Defensive skip for rows with empty name — the canonical mirror filters
    # them out at load time, but the planner stays paranoid.
    by_canonical_id: dict[str, _CanonicalRow] = {
        r.notion_page_id: r for r in canonical_rows if r.name
    }

    # Detect duplicate Tracker fan-in so we don't PATCH the same id twice.
    seen_tracker_ids: set[str] = set()

    for row in canonical_rows:
        if not row.name:
            continue

        # CASE T: tombstoned canonical → remove the tracker page entirely.
        # Skip create/update/rename logic so the row's title never lands on
        # Notion as `(archived) X` (the old behavior); the tracker page is
        # archived instead.
        if row.deleted_at is not None:
            if (
                row.tracker_node_page_id
                and row.tracker_node_page_id in tracker_snapshot
            ):
                result.to_archive.append((row, row.tracker_node_page_id))
                result.deleted += 1
            elif row.tracker_node_page_id:
                # Mapping points at a page that's already gone (manually
                # Notion-archived?). Just clear the canonical mapping so we
                # stop processing this row.
                result.to_clear_canonical.append(row)
                result.details.append(
                    f"row={row.notion_page_id[:8]} name={row.name!r} "
                    f"tombstoned canonical with stale tracker_node_page_id "
                    f"{row.tracker_node_page_id[:8]} (page already gone) — "
                    "clearing mapping",
                )
            # else: tombstoned + never had a tracker row → nothing to do.
            continue

        desired_title = _desired_title(row)

        # Desired parent tracker id: parent canonical row's tracker_node_page_id.
        # If the parent is tombstoned, clear the child's Parent item so we
        # don't end up with a relation to a Notion-archived page.
        desired_parent_tracker_id: str | None = None
        if row.parent_notion_page_id:
            parent_row = by_canonical_id.get(row.parent_notion_page_id)
            if parent_row is None:
                result.details.append(
                    f"row={row.notion_page_id[:8]} name={row.name!r} "
                    f"parent_id={row.parent_notion_page_id[:8]} not in canonical "
                    "snapshot — leaving Parent item empty",
                )
            elif parent_row.deleted_at is not None:
                result.details.append(
                    f"row={row.notion_page_id[:8]} name={row.name!r} "
                    f"parent_id={row.parent_notion_page_id[:8]} is tombstoned "
                    "— clearing Parent item",
                )
            else:
                desired_parent_tracker_id = parent_row.tracker_node_page_id

        # CASE A: missing or stale tracker_node_page_id → create.
        if (
            not row.tracker_node_page_id
            or row.tracker_node_page_id not in tracker_snapshot
        ):
            if (
                row.tracker_node_page_id
                and row.tracker_node_page_id not in tracker_snapshot
            ):
                result.details.append(
                    f"row={row.notion_page_id[:8]} name={row.name!r} "
                    f"tracker_node_page_id {row.tracker_node_page_id[:8]} "
                    "not found in Tracker DB — recreating",
                )
            result.to_create.append(row)
            result.created += 1
            # Archived-state title means a fresh (archived) X row at create time.
            if not row.active:
                result.archived += 1
            continue

        # CASE B: Tracker row exists → diff and queue reconcile.
        if row.tracker_node_page_id in seen_tracker_ids:
            result.details.append(
                f"row={row.notion_page_id[:8]} name={row.name!r} duplicate "
                f"fan-in to tracker {row.tracker_node_page_id[:8]} — "
                "skipped (planned by earlier row)",
            )
            continue
        seen_tracker_ids.add(row.tracker_node_page_id)

        current = tracker_snapshot[row.tracker_node_page_id]
        current_title = (current.get("title") or "").strip()
        current_parent_id = current.get("parent_id")

        patch: dict[str, Any] = {}
        if current_title != desired_title:
            patch["Task"] = {"title": [{"text": {"content": desired_title}}]}
            result.renamed += 1
            if (
                not current_title.startswith(_ARCHIVED_PREFIX)
                and desired_title.startswith(_ARCHIVED_PREFIX)
            ):
                result.archived += 1
        if current_parent_id != desired_parent_tracker_id:
            patch["Parent item"] = {
                "relation": (
                    [{"id": desired_parent_tracker_id}]
                    if desired_parent_tracker_id
                    else []
                ),
            }
            result.parent_fixed += 1

        if patch:
            result.to_update.append((row.tracker_node_page_id, patch))

    return result


# ---------------------------------------------------------------------------
# I/O sync
# ---------------------------------------------------------------------------


def _create_payload(row: _CanonicalRow) -> dict[str, Any]:
    return {
        "Task": {"title": [{"text": {"content": _desired_title(row)}}]},
        "Priority": {"select": {"name": _DETAILS_INSIDE}},
    }


def _patch_canonical_tracker_id(notion_page_id: str, tracker_id: str) -> None:
    """PATCH ``hierarchy_rows.tracker_node_page_id`` (authoritative mapping)."""
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    _http(
        "PATCH",
        f"/rest/v1/hierarchy_rows?notion_page_id=eq.{notion_page_id}",
        body={
            "tracker_node_page_id": tracker_id,
            "last_changed_at": now_iso,
        },
    )


def _clear_canonical_tracker_id(notion_page_id: str) -> None:
    """NULL the ``tracker_node_page_id`` for a tombstoned canonical row.

    Called after the tracker page is Notion-archived (or detected as already
    gone). Without this, the next tick would see the stale id and try to
    recreate the tracker row.
    """
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    _http(
        "PATCH",
        f"/rest/v1/hierarchy_rows?notion_page_id=eq.{notion_page_id}",
        body={
            "tracker_node_page_id": None,
            "last_changed_at": now_iso,
        },
    )


def sync(client: NotionClientWrapper, config: SyncConfig) -> SyncReport:
    """Apply Supabase canonical → Tracker ``[DETAILS INSIDE]`` rows."""
    report = SyncReport(name=SUB_SYNC_NAME)

    team_tracker_db_id = getattr(config, "team_tracker_db_id", None)
    if not team_tracker_db_id:
        report.errors += 1
        report.details.append(
            "TEAM_TRACKER_DB_ID not configured — skipping tracker_applier_sync",
        )
        logger.warning(
            "tracker_applier_sync: TEAM_TRACKER_DB_ID not configured — skipping",
        )
        return report

    # hierarchy_db_id is OPTIONAL — only needed for the best-effort Notion
    # `Tracker Node` cache write. Missing → downgrade that write to no-op
    # rather than aborting the whole applier.
    hierarchy_db_id = getattr(config, "hierarchy_db_id", None)

    try:
        _supabase_creds()
    except RuntimeError as e:
        report.errors += 1
        report.details.append(f"Supabase not configured: {e}")
        logger.warning("tracker_applier_sync: %s", e)
        return report

    try:
        canonical_rows = _load_canonical_snapshot()
    except Exception as e:  # noqa: BLE001
        report.errors += 1
        report.details.append(
            f"canonical snapshot failed: {type(e).__name__}: {e}",
        )
        logger.exception("tracker_applier_sync: canonical snapshot failed")
        return report

    if not canonical_rows:
        # Benign — likely `canonical_mirror_sync` hasn't run yet OR the
        # table is empty. NOT an error; just a warning.
        logger.warning(
            "tracker_applier_sync: hierarchy_rows is empty — did "
            "canonical_mirror_sync run yet? Skipping with no writes.",
        )
        report.details.append(
            "canonical empty — did canonical_mirror_sync run yet?",
        )
        return report

    try:
        tracker_snapshot = _load_tracker_snapshot(client, team_tracker_db_id)
    except Exception as e:  # noqa: BLE001
        report.errors += 1
        report.details.append(
            f"tracker snapshot failed: {type(e).__name__}: {e}",
        )
        logger.exception("tracker_applier_sync: tracker snapshot failed")
        return report

    plan = _plan_tracker_updates(canonical_rows, tracker_snapshot)
    for d in plan.details[:_DETAIL_CAP]:
        report.details.append(d)

    # -------- Pass 1: create missing Tracker rows + back-fill --------
    for hier_row in plan.to_create:
        short = hier_row.notion_page_id[:8]
        if config.dry_run:
            sentinel = f"dry-run-create-{hier_row.notion_page_id}"
            tracker_snapshot[sentinel] = {
                "title": _desired_title(hier_row), "parent_id": None,
            }
            hier_row.tracker_node_page_id = sentinel
            report.created += 1
            # Tombstoned rows never reach the create path (CASE T short-
            # circuits in the planner); only inactive-but-not-tombstoned
            # rows are created with the (archived) prefix.
            if not hier_row.active:
                report.archived += 1
            report.details.append(
                f"row={short} name={hier_row.name!r} would create Tracker row (dry-run)",
            )
            logger.info(
                "tracker_applier_sync: DRY RUN row=%s would create Tracker row name=%r",
                short, hier_row.name,
            )
            continue

        try:
            created = client.create_page(
                team_tracker_db_id, _create_payload(hier_row),
            )
        except Exception as e:  # noqa: BLE001
            report.errors += 1
            report.details.append(
                f"row={short} name={hier_row.name!r} create_page failed: "
                f"{type(e).__name__}: {e}",
            )
            logger.exception(
                "tracker_applier_sync: create_page failed for canonical row %s (%r)",
                short, hier_row.name,
            )
            continue

        new_tracker_id = (created or {}).get("id") or ""
        if not new_tracker_id:
            report.errors += 1
            report.details.append(
                f"row={short} name={hier_row.name!r} create_page returned no id",
            )
            logger.error(
                "tracker_applier_sync: create_page for canonical row %s returned no id",
                short,
            )
            continue

        # Authoritative mapping write — failure here LEAKS the Tracker row.
        try:
            _patch_canonical_tracker_id(hier_row.notion_page_id, new_tracker_id)
        except Exception as e:  # noqa: BLE001
            report.errors += 1
            report.details.append(
                f"row={short} name={hier_row.name!r} created Tracker row "
                f"{new_tracker_id[:8]} but Supabase tracker_node_page_id "
                f"back-fill FAILED: {type(e).__name__}: {e} — orphan needs "
                "manual reconciliation (next run will create another row)",
            )
            logger.exception(
                "tracker_applier_sync: Supabase back-fill failed for row %s; "
                "orphan Tracker row id=%s name=%r",
                short, new_tracker_id, hier_row.name,
            )
            # Skip the Notion cache write too — pointless with broken canonical.
            continue

        # Best-effort Notion `Tracker Node` relation write — cache only.
        if hierarchy_db_id:
            try:
                client.update_page(
                    page_id=hier_row.notion_page_id,
                    properties={
                        "Tracker Node": {"relation": [{"id": new_tracker_id}]},
                    },
                )
            except Exception as e:  # noqa: BLE001
                report.details.append(
                    f"row={short} name={hier_row.name!r} Notion Tracker Node "
                    f"cache writeback failed: {type(e).__name__}: {e} "
                    "(Supabase is canonical; cache will heal next run)",
                )
                logger.warning(
                    "tracker_applier_sync: Notion Tracker Node writeback "
                    "failed for row %s: %s", short, e,
                )

        # Update in-memory snapshots so pass 2 can resolve this row as a parent.
        hier_row.tracker_node_page_id = new_tracker_id
        tracker_snapshot[new_tracker_id] = {
            "title": _desired_title(hier_row), "parent_id": None,
        }
        report.created += 1
        if (hier_row.deleted_at is not None) or (not hier_row.active):
            report.archived += 1
        logger.info(
            "tracker_applier_sync: row=%s created Tracker row %s name=%r",
            short, new_tracker_id[:8], hier_row.name,
        )

    # -------- Re-plan parents now that pass 1 mutated the snapshots --------
    reconcile_plan = _plan_tracker_updates(canonical_rows, tracker_snapshot)

    # -------- Pass 2: apply title + parent reconciliation --------
    for tracker_id, payload in reconcile_plan.to_update:
        # Dry-run sentinels never reach Notion.
        if tracker_id.startswith("dry-run-create-"):
            if "Task" in payload:
                report.renamed += 1
                desired = (
                    (payload["Task"].get("title") or [{}])[0]
                    .get("text", {})
                    .get("content", "")
                )
                if desired.startswith(_ARCHIVED_PREFIX):
                    report.archived += 1
            if "Parent item" in payload:
                report.parent_fixed += 1
            report.details.append(
                f"tracker={tracker_id} would patch keys={list(payload.keys())} (dry-run)",
            )
            logger.info(
                "tracker_applier_sync: DRY RUN tracker=%s would patch keys=%s",
                tracker_id, list(payload.keys()),
            )
            continue

        current_title = (
            tracker_snapshot.get(tracker_id, {}).get("title") or ""
        ).strip()
        desired_title = current_title  # default for parent-only patches

        if config.dry_run:
            if "Task" in payload:
                report.renamed += 1
                desired_title = (
                    (payload["Task"].get("title") or [{}])[0]
                    .get("text", {})
                    .get("content", "")
                )
                if (
                    not current_title.startswith(_ARCHIVED_PREFIX)
                    and desired_title.startswith(_ARCHIVED_PREFIX)
                ):
                    report.archived += 1
            if "Parent item" in payload:
                report.parent_fixed += 1
            report.details.append(
                f"tracker={tracker_id[:8]} would patch keys={list(payload.keys())} (dry-run)",
            )
            logger.info(
                "tracker_applier_sync: DRY RUN tracker=%s keys=%s title=%r→%r",
                tracker_id[:8], list(payload.keys()), current_title, desired_title,
            )
            continue

        try:
            client.update_page(page_id=tracker_id, properties=payload)
        except Exception as e:  # noqa: BLE001
            report.errors += 1
            report.details.append(
                f"tracker={tracker_id[:8]} update_page failed: "
                f"{type(e).__name__}: {e}",
            )
            logger.exception(
                "tracker_applier_sync: update_page failed for tracker row %s",
                tracker_id,
            )
            continue

        if "Task" in payload:
            report.renamed += 1
            desired_title = (
                (payload["Task"].get("title") or [{}])[0]
                .get("text", {})
                .get("content", "")
            )
            if (
                not current_title.startswith(_ARCHIVED_PREFIX)
                and desired_title.startswith(_ARCHIVED_PREFIX)
            ):
                report.archived += 1
        if "Parent item" in payload:
            report.parent_fixed += 1
        logger.info(
            "tracker_applier_sync: tracker=%s patched keys=%s",
            tracker_id[:8], list(payload.keys()),
        )

    # -------- Pass 3: archive tracker pages for tombstoned canonical rows --------
    # The tombstoned-but-mapping-points-at-gone-page case (to_clear_canonical)
    # uses the same Supabase clear without the Notion archive call.
    for hier_row, tracker_id in plan.to_archive:
        short = hier_row.notion_page_id[:8]
        if config.dry_run:
            report.deleted += 1
            report.details.append(
                f"row={short} name={hier_row.name!r} would archive "
                f"tracker={tracker_id[:8]} + clear canonical mapping "
                "(dry-run)",
            )
            logger.info(
                "tracker_applier_sync: DRY RUN row=%s would archive "
                "tracker=%s name=%r",
                short, tracker_id[:8], hier_row.name,
            )
            continue

        try:
            client.archive_page(tracker_id)
        except Exception as e:  # noqa: BLE001
            report.errors += 1
            report.details.append(
                f"row={short} name={hier_row.name!r} archive_page "
                f"{tracker_id[:8]} failed: {type(e).__name__}: {e} — "
                "next run retries (canonical mapping kept)",
            )
            logger.exception(
                "tracker_applier_sync: archive_page failed for canonical row "
                "%s (%r) tracker=%s",
                short, hier_row.name, tracker_id,
            )
            continue

        # Clear the canonical mapping ONLY after the archive succeeds —
        # otherwise a transient archive failure would leave the canonical
        # pointing at an archived page with no recovery path.
        try:
            _clear_canonical_tracker_id(hier_row.notion_page_id)
        except Exception as e:  # noqa: BLE001
            report.errors += 1
            report.details.append(
                f"row={short} name={hier_row.name!r} tracker page "
                f"{tracker_id[:8]} archived but Supabase mapping clear "
                f"FAILED: {type(e).__name__}: {e} — next run sees stale "
                "mapping → CASE T detects page already gone → re-clears",
            )
            logger.exception(
                "tracker_applier_sync: canonical mapping clear failed for "
                "tombstoned row %s after archive", short,
            )
            continue

        report.deleted += 1
        logger.info(
            "tracker_applier_sync: row=%s archived Tracker row %s name=%r "
            "(tombstoned canonical)",
            short, tracker_id[:8], hier_row.name,
        )

    for hier_row in plan.to_clear_canonical:
        short = hier_row.notion_page_id[:8]
        if config.dry_run:
            report.details.append(
                f"row={short} name={hier_row.name!r} would clear stale "
                "canonical mapping (tracker page already gone) (dry-run)",
            )
            continue
        try:
            _clear_canonical_tracker_id(hier_row.notion_page_id)
        except Exception as e:  # noqa: BLE001
            report.errors += 1
            report.details.append(
                f"row={short} name={hier_row.name!r} stale canonical "
                f"mapping clear FAILED: {type(e).__name__}: {e}",
            )
            logger.exception(
                "tracker_applier_sync: stale canonical mapping clear failed "
                "for tombstoned row %s", short,
            )

    # Cap details so big runs don't bloat the log line.
    if len(report.details) > _DETAIL_CAP:
        truncated = len(report.details) - _DETAIL_CAP
        report.details = report.details[:_DETAIL_CAP]
        report.details.append(f"… (+{truncated} more details truncated)")

    return report


__all__ = ["SUB_SYNC_NAME", "sync"]
