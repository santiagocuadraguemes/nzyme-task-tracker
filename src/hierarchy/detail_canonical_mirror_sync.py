"""Mirror the Notion ``Detail Options`` Settings DB → Supabase canonical state.

Runs daily 07:00 Madrid as part of the ``hierarchy_sync`` orchestrator,
between the Hierarchy DB mirror and the downstream appliers. One-way write:
Notion is the editing surface; ``public.detail_rows`` is the canonical source
of truth that ``detail_applier_sync`` consumes to propagate options into every
member Meeting Notes DB's ``Detail`` multi-select.

Per tick:

  1. Snapshot the Notion Detail Options Settings DB.
  2. Load every row from ``public.detail_rows`` (including tombstones).
  3. Diff by ``notion_page_id`` (stable across Notion-side renames):
       - created     — row in Notion, not in canonical
       - edited      — row in both; one or more mirrored fields differ
       - reactivated — row in Notion + canonical has ``deleted_at`` set
       - deleted     — row in canonical (live) but missing from Notion
       - unchanged   — row in both; identical fields
  4. Apply: upsert created/edited/reactivated, tombstone deleted, bump
     ``last_seen_at`` on unchanged.
  5. Append one row to ``public.detail_sync_runs`` with counts + structured
     JSONB change log.

Skips with a benign warning (``errors=0``) when ``DETAIL_OPTIONS_DB_ID`` is
unset — Detail is an optional feature, and the deploy may not have the
Settings DB created yet. Same posture for an empty Notion snapshot (rare,
treated as a heartbeat).

Non-goals:
  * Propagating Detail rows into member-DB multi-select options. That's
    ``detail_applier_sync``'s job — this module only mirrors the canonical.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from src.config import SyncConfig
from src.hierarchy.base import SyncReport
from src.hierarchy.canonical_mirror_sync import (
    _http,
    _in_filter,
    _read_rich_text,  # noqa: F401  (re-exported for symmetry; unused locally)
    _read_select_name,
    _read_title,
    _supabase_creds,
)
from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)

SUB_SYNC_NAME = "detail_canonical_mirror_sync"

# Fields mirrored from Notion → Supabase. Set-based equality; order preserved
# for readability of change-log JSON.
_MIRRORED_FIELDS = (
    "name",
    "color",
    "parent_hierarchy_page_id",
    "active",
)

# Cap the JSONB change log on the audit row.
_CHANGES_JSON_CAP = 500

# Property names on the Notion Detail Options Settings DB.
_PROP_COLOR = "Color"
_PROP_PARENT = "Parent Work area"
_PROP_ACTIVE = "Active"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class _NotionRow:
    """Normalized snapshot of one Detail Options Settings DB row."""

    notion_page_id: str
    name: str
    color: str
    parent_hierarchy_page_id: str | None
    active: bool

    def as_record(self) -> dict[str, Any]:
        return {f: getattr(self, f) for f in _MIRRORED_FIELDS} | {
            "notion_page_id": self.notion_page_id,
        }


Op = Literal["created", "edited", "deleted", "reactivated", "unchanged"]


@dataclass
class _Change:
    notion_page_id: str
    op: Op
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    field_diff: dict[str, dict[str, Any]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Notion snapshot loader
# ---------------------------------------------------------------------------


def _read_relation_first(prop: dict[str, Any] | None) -> str | None:
    rels = (prop or {}).get("relation") or []
    return rels[0]["id"] if rels else None


def _load_notion_snapshot(
    client: NotionClientWrapper, settings_db_id: str,
) -> list[_NotionRow]:
    response = client.query_database(database_id=settings_db_id)
    rows: list[_NotionRow] = []
    for page in response.get("results", []):
        page_id = page.get("id") or ""
        if not page_id:
            continue
        props = page.get("properties") or {}
        name = ""
        for prop in props.values():
            if prop.get("type") == "title":
                name = _read_title(prop)
                break
        if not name:
            logger.warning(
                "detail_canonical_mirror_sync: page %s has empty Name — skipping",
                page_id[:8],
            )
            continue
        color = _read_select_name(props.get(_PROP_COLOR)) or "default"
        rows.append(
            _NotionRow(
                notion_page_id=page_id,
                name=name,
                color=color,
                parent_hierarchy_page_id=_read_relation_first(
                    props.get(_PROP_PARENT),
                ),
                active=bool((props.get(_PROP_ACTIVE) or {}).get("checkbox", False)),
            ),
        )
    return rows


# ---------------------------------------------------------------------------
# Supabase snapshot loader
# ---------------------------------------------------------------------------


def _load_supabase_snapshot() -> dict[str, dict[str, Any]]:
    rows = _http("GET", "/rest/v1/detail_rows?select=*&limit=10000") or []
    return {r["notion_page_id"]: r for r in rows}


# ---------------------------------------------------------------------------
# Pure diff
# ---------------------------------------------------------------------------


def _normalize_for_compare(field_name: str, value: Any) -> Any:
    """Coerce semantically-equivalent Notion/Postgres values for diffing."""
    if field_name == "color" and value is None:
        return "default"
    return value


def _extract_canonical_record(canonical: dict[str, Any]) -> dict[str, Any]:
    return {
        f: _normalize_for_compare(f, canonical.get(f)) for f in _MIRRORED_FIELDS
    }


def _compute_changes(
    notion_rows: list[_NotionRow],
    supabase_snapshot: dict[str, dict[str, Any]],
) -> list[_Change]:
    changes: list[_Change] = []
    notion_ids: set[str] = set()

    for row in notion_rows:
        notion_ids.add(row.notion_page_id)
        canonical = supabase_snapshot.get(row.notion_page_id)
        after = row.as_record()

        if canonical is None:
            changes.append(_Change(
                notion_page_id=row.notion_page_id, op="created", after=after,
            ))
            continue

        if canonical.get("deleted_at") is not None:
            changes.append(_Change(
                notion_page_id=row.notion_page_id,
                op="reactivated",
                before=_extract_canonical_record(canonical),
                after=after,
            ))
            continue

        field_diff: dict[str, dict[str, Any]] = {}
        for f in _MIRRORED_FIELDS:
            current = _normalize_for_compare(f, canonical.get(f))
            desired = getattr(row, f)
            if current != desired:
                field_diff[f] = {"before": current, "after": desired}

        if field_diff:
            changes.append(_Change(
                notion_page_id=row.notion_page_id,
                op="edited",
                before=_extract_canonical_record(canonical),
                after=after,
                field_diff=field_diff,
            ))
        else:
            changes.append(_Change(
                notion_page_id=row.notion_page_id, op="unchanged",
            ))

    # Deletion = canonical row not in Notion snapshot AND not already tombstoned.
    for page_id, canonical in supabase_snapshot.items():
        if page_id in notion_ids:
            continue
        if canonical.get("deleted_at") is not None:
            continue
        changes.append(_Change(
            notion_page_id=page_id,
            op="deleted",
            before=_extract_canonical_record(canonical),
        ))

    return changes


# ---------------------------------------------------------------------------
# I/O sync
# ---------------------------------------------------------------------------


def _build_upsert_record(change: _Change, *, now_iso: str) -> dict[str, Any]:
    assert change.after is not None
    record = dict(change.after)
    record["last_seen_at"] = now_iso
    if change.op in ("created", "edited", "reactivated"):
        record["last_changed_at"] = now_iso
    if change.op == "reactivated":
        record["deleted_at"] = None
    return record


def sync(client: NotionClientWrapper, config: SyncConfig) -> SyncReport:
    """Mirror Notion Detail Options Settings DB → Supabase canonical state."""
    report = SyncReport(name=SUB_SYNC_NAME)

    settings_db_id = getattr(config, "detail_options_db_id", None)
    if not settings_db_id:
        # Optional feature — empty config is a benign warning, not an error.
        report.details.append(
            "DETAIL_OPTIONS_DB_ID not configured — skipping with no writes",
        )
        logger.warning(
            "detail_canonical_mirror_sync: DETAIL_OPTIONS_DB_ID not configured "
            "— skipping",
        )
        return report

    try:
        _supabase_creds()
    except RuntimeError as e:
        report.errors += 1
        report.details.append(f"Supabase not configured: {e}")
        logger.warning("detail_canonical_mirror_sync: %s", e)
        return report

    try:
        notion_rows = _load_notion_snapshot(client, settings_db_id)
    except Exception as e:  # noqa: BLE001
        report.errors += 1
        report.details.append(
            f"Notion snapshot failed: {type(e).__name__}: {e}",
        )
        logger.exception("detail_canonical_mirror_sync: Notion snapshot failed")
        return report

    try:
        supabase_snapshot = _load_supabase_snapshot()
    except Exception as e:  # noqa: BLE001
        report.errors += 1
        report.details.append(
            f"Supabase snapshot failed: {type(e).__name__}: {e}",
        )
        logger.exception(
            "detail_canonical_mirror_sync: Supabase snapshot failed",
        )
        return report

    changes = _compute_changes(notion_rows, supabase_snapshot)
    now_iso = datetime.now(tz=timezone.utc).isoformat()

    upserts: list[dict[str, Any]] = []
    deleted_ids: list[str] = []
    unchanged_ids: list[str] = []

    for ch in changes:
        if ch.op in ("created", "edited", "reactivated"):
            upserts.append(_build_upsert_record(ch, now_iso=now_iso))
            if ch.op == "created":
                report.created += 1
            elif ch.op == "edited":
                report.edited += 1
            else:
                report.reactivated += 1
        elif ch.op == "deleted":
            deleted_ids.append(ch.notion_page_id)
            report.deleted += 1
        elif ch.op == "unchanged":
            unchanged_ids.append(ch.notion_page_id)

    if config.dry_run:
        report.details.append(
            f"dry-run — would upsert={len(upserts)} delete={len(deleted_ids)} "
            f"touch={len(unchanged_ids)}",
        )
        logger.info(
            "detail_canonical_mirror_sync: DRY RUN created=%d edited=%d "
            "deleted=%d reactivated=%d unchanged=%d",
            report.created, report.edited, report.deleted, report.reactivated,
            len(unchanged_ids),
        )
        for ch in changes:
            if ch.op == "edited":
                logger.info(
                    "detail_canonical_mirror_sync: DRY RUN edited page=%s diff=%s",
                    ch.notion_page_id[:8], list(ch.field_diff.keys()),
                )
            elif ch.op in ("created", "deleted", "reactivated"):
                logger.info(
                    "detail_canonical_mirror_sync: DRY RUN %s page=%s",
                    ch.op, ch.notion_page_id[:8],
                )
        return report

    # ---- Apply writes ----
    if upserts:
        try:
            _http(
                "POST",
                "/rest/v1/detail_rows?on_conflict=notion_page_id",
                body=upserts,
                prefer="resolution=merge-duplicates,return=minimal",
            )
        except Exception as e:  # noqa: BLE001
            report.errors += 1
            report.details.append(
                f"upsert failed ({len(upserts)} rows): {type(e).__name__}: {e}",
            )
            logger.exception("detail_canonical_mirror_sync: upsert failed")
            return report

    if deleted_ids:
        try:
            _http(
                "PATCH",
                f"/rest/v1/detail_rows?notion_page_id=in.{_in_filter(deleted_ids)}",
                body={"deleted_at": now_iso},
            )
        except Exception as e:  # noqa: BLE001
            report.errors += 1
            report.details.append(
                f"tombstone PATCH failed ({len(deleted_ids)} ids): "
                f"{type(e).__name__}: {e}",
            )
            logger.exception(
                "detail_canonical_mirror_sync: tombstone PATCH failed",
            )

    if unchanged_ids:
        try:
            _http(
                "PATCH",
                f"/rest/v1/detail_rows?notion_page_id=in.{_in_filter(unchanged_ids)}",
                body={"last_seen_at": now_iso},
            )
        except Exception as e:  # noqa: BLE001
            report.details.append(
                f"last_seen_at heartbeat failed ({len(unchanged_ids)} ids): "
                f"{type(e).__name__}: {e}",
            )
            logger.warning(
                "detail_canonical_mirror_sync: last_seen_at heartbeat failed: %s",
                e,
            )

    # ---- Audit row ----
    changes_payload = [
        {
            "page_id": ch.notion_page_id,
            "op": ch.op,
            "before": ch.before,
            "after": ch.after,
            "field_diff": ch.field_diff or None,
        }
        for ch in changes
        if ch.op != "unchanged"
    ]
    if len(changes_payload) > _CHANGES_JSON_CAP:
        truncated = len(changes_payload) - _CHANGES_JSON_CAP
        changes_payload = changes_payload[:_CHANGES_JSON_CAP]
        changes_payload.append({"op": "_truncated", "count": truncated})

    try:
        _http(
            "POST",
            "/rest/v1/detail_sync_runs",
            body={
                "rows_created": report.created,
                "rows_edited": report.edited,
                "rows_deleted": report.deleted,
                "rows_reactivated": report.reactivated,
                "rows_unchanged": len(unchanged_ids),
                "errors": report.errors,
                "changes": changes_payload,
            },
            prefer="return=minimal",
        )
    except Exception as e:  # noqa: BLE001
        report.details.append(
            f"audit row insert failed: {type(e).__name__}: {e}",
        )
        logger.warning(
            "detail_canonical_mirror_sync: audit row insert failed: %s", e,
        )

    logger.info(
        "detail_canonical_mirror_sync: created=%d edited=%d deleted=%d "
        "reactivated=%d unchanged=%d errors=%d",
        report.created, report.edited, report.deleted, report.reactivated,
        len(unchanged_ids), report.errors,
    )
    return report


__all__ = ["SUB_SYNC_NAME", "sync"]
