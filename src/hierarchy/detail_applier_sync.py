"""Apply Supabase canonical (``public.detail_rows``) → ``Detail`` multi-select
options on every member Meeting Notes DB.

Runs daily 07:00 Madrid as the second downstream applier in the
``hierarchy_sync`` orchestrator, after ``detail_canonical_mirror_sync`` (which
writes today's Notion Detail Options Settings DB state into Supabase). This
applier reads the freshly-updated canonical and reconciles each member DB's
``Detail`` multi-select.

Contract:

  * Every live (``deleted_at IS NULL``) + ``active`` canonical row →
    every member DB has an option whose name + color match the canonical.
  * Live + inactive row → option soft-archived as
    ``(archived) <sanitized name>``.
  * Tombstoned (``deleted_at`` set) row → option **removed** from the
    member DB (multi-select); the entry is stripped from every tagged
    page's array first; the mapping row is DELETEd from Supabase.
  * Options on a member DB that don't correspond to any canonical row are
    left alone (pre-existing legacy options pass through verbatim).
  * Idempotent: when state already matches (name + color + id), no PATCH is
    issued.

Differences from ``macro_block_sync``:

  * Property type is ``multi_select`` (not ``select``).
  * Color is canonical-driven — every PATCH carries the desired color from
    ``detail_rows.color``. First run normalizes colors across all member DBs.
  * Mapping table is ``public.detail_option_mappings``, PK
    ``(detail_notion_page_id, member_db_id)``.

Sanitization, recovery, bootstrap-adopt-by-sanitized-name — identical to
``macro_block_sync``. Sanitizer imported from there to keep one source of truth
for the Notion-side comma-stripping rule.

Sharing: Supabase HTTP helpers (``_supabase_creds``, ``_http``) are re-used
via cross-import from ``canonical_mirror_sync``. The 5-step rename saga is
imported from ``_rename_saga``; multi-select migration drops the entry for
the old option and appends the new option id while preserving every other
tag on the page.

Non-goals:
  * Garbage-collect orphan ``(archived) X`` options or mappings for
    departed members.
"""
from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.config import SyncConfig
from src.hierarchy._rename_saga import (
    DropIntent,
    RenameIntent,
    execute_drop_saga,
    execute_rename_saga,
    materialize_final_options,
)
from src.hierarchy.base import SyncReport
from src.hierarchy.canonical_mirror_sync import _http, _supabase_creds
from src.hierarchy.macro_block_sync import _sanitize_option_name
from src.meeting_db_registry import discover_meeting_dbs
from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)

SUB_SYNC_NAME = "detail_applier_sync"

_DETAIL_PROPERTY = "Detail"
_ARCHIVED_PREFIX = "(archived) "
_DETAIL_CAP = 50
_DEFAULT_COLOR = "default"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class _CanonicalDetailRow:
    notion_page_id: str
    name: str
    color: str
    active: bool
    deleted_at: str | None


@dataclass
class _Mapping:
    detail_notion_page_id: str
    member_db_id: str
    option_id: str
    option_name: str


@dataclass
class _PlannerResult:
    new_options: list[dict[str, Any]] = field(default_factory=list)
    mapping_writes: list[_Mapping] = field(default_factory=list)
    # Detail row notion_page_ids whose mapping rows should be DELETEd from
    # Supabase (tombstoned canonical rows whose member-DB option has been
    # dropped).
    mapping_deletes: list[str] = field(default_factory=list)
    renames: list[RenameIntent] = field(default_factory=list)
    drops: list[DropIntent] = field(default_factory=list)
    created: int = 0
    renamed: int = 0
    archived: int = 0
    deleted: int = 0
    errors: int = 0
    details: list[str] = field(default_factory=list)
    changed: bool = False


# ---------------------------------------------------------------------------
# Snapshot loaders
# ---------------------------------------------------------------------------


def _load_canonical_detail_rows() -> list[_CanonicalDetailRow]:
    """Read every row from ``detail_rows`` — including tombstones."""
    raw = _http(
        "GET",
        "/rest/v1/detail_rows?select=notion_page_id,name,color,active,deleted_at"
        "&limit=10000",
    ) or []
    rows = [
        _CanonicalDetailRow(
            notion_page_id=r["notion_page_id"],
            name=r.get("name") or "",
            color=r.get("color") or _DEFAULT_COLOR,
            active=bool(r.get("active")),
            deleted_at=r.get("deleted_at"),
        )
        for r in raw
        if r.get("notion_page_id") and (r.get("name") or "").strip()
    ]
    rows.sort(key=lambda r: r.name)
    return rows


def _load_mappings(member_db_ids: list[str]) -> dict[tuple[str, str], _Mapping]:
    """Return ``{(detail_notion_page_id, member_db_id): _Mapping}``."""
    if not member_db_ids:
        return {}
    in_list = ",".join(f'"{i}"' for i in member_db_ids)
    raw = _http(
        "GET",
        f"/rest/v1/detail_option_mappings?select=*&member_db_id=in.({in_list})"
        "&limit=10000",
    ) or []
    out: dict[tuple[str, str], _Mapping] = {}
    for r in raw:
        page_id = r.get("detail_notion_page_id")
        mdb_id = r.get("member_db_id")
        if not page_id or not mdb_id:
            continue
        out[(page_id, mdb_id)] = _Mapping(
            detail_notion_page_id=page_id,
            member_db_id=mdb_id,
            option_id=r.get("option_id") or "",
            option_name=r.get("option_name") or "",
        )
    return out


# urllib.parse is used by _delete_mappings to quote member_db_id in the
# Supabase DELETE URL.


# ---------------------------------------------------------------------------
# Collision detection
# ---------------------------------------------------------------------------


def _desired_sanitized(row: _CanonicalDetailRow) -> str:
    """Desired sanitized name for a non-tombstoned canonical row.

    Tombstoned rows (``deleted_at IS NOT NULL``) are dropped from the member
    DB entirely — they don't claim a name. Inactive-but-not-tombstoned rows
    still get the ``(archived) X`` rename.
    """
    is_archived_state = (not row.active) and (row.deleted_at is None)
    base = f"{_ARCHIVED_PREFIX}{row.name}" if is_archived_state else row.name
    return _sanitize_option_name(base)


def _detect_sanitized_collisions(
    canonical_rows: list[_CanonicalDetailRow],
) -> dict[str, list[str]]:
    """Tombstoned rows are excluded — they don't claim a name."""
    by_sanitized: dict[str, list[str]] = {}
    for row in canonical_rows:
        if row.deleted_at is not None:
            continue
        desired = _desired_sanitized(row)
        by_sanitized.setdefault(desired, []).append(row.notion_page_id)
    return {n: ids for n, ids in by_sanitized.items() if len(ids) > 1}


# ---------------------------------------------------------------------------
# Pure planner
# ---------------------------------------------------------------------------


def _options_equal(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> bool:
    """Compare option arrays by (id, name, color) — order-sensitive."""
    if len(a) != len(b):
        return False
    for opt_a, opt_b in zip(a, b):
        if opt_a.get("id") != opt_b.get("id"):
            return False
        if opt_a.get("name") != opt_b.get("name"):
            return False
        # default vs None vs missing → all treated as "default"
        ca = opt_a.get("color") or _DEFAULT_COLOR
        cb = opt_b.get("color") or _DEFAULT_COLOR
        if ca != cb:
            return False
    return True


def _plan_member_db_update(
    canonical_rows: list[_CanonicalDetailRow],
    mappings_for_member: dict[str, _Mapping],
    current_options: list[dict[str, Any]],
    member_db_id: str,
    skip_page_ids: set[str] | None = None,
) -> _PlannerResult:
    """Compute the desired option array for one member DB. Pure — no I/O.

    Differences from ``macro_block_sync._plan_member_db_update``:
      * ``color`` is canonical-driven and part of the change comparison.
      * Otherwise identical: CASE A (mapping hit), CASE B (stale mapping),
        CASE C (bootstrap-adopt by sanitized name), CASE D (bootstrap-create).
    """
    result = _PlannerResult()
    skip = skip_page_ids or set()

    by_id: dict[str, dict[str, Any]] = {
        opt["id"]: opt for opt in current_options if opt.get("id")
    }
    by_sanitized_name: dict[str, dict[str, Any]] = {
        _sanitize_option_name(opt.get("name", "")): opt
        for opt in current_options
        if opt.get("name")
    }

    out: list[dict[str, Any]] = [dict(opt) for opt in current_options]
    out_idx_by_id: dict[str, int] = {
        opt["id"]: i for i, opt in enumerate(out) if opt.get("id")
    }
    pending_creates: list[tuple[int, str]] = []
    dropped_old_ids: set[str] = set()

    for row in canonical_rows:
        if row.notion_page_id in skip:
            continue
        if not row.name.strip():
            continue

        mapping = mappings_for_member.get(row.notion_page_id)

        # CASE T — tombstoned canonical row → drop the multi-select option
        # entirely from the member DB. Tagged pages get the entry removed
        # from their array (every other Detail tag preserved) before the
        # PATCH drops the option. Skip CASE C/D so we never re-create.
        if row.deleted_at is not None:
            if mapping and mapping.option_id in by_id:
                opt = by_id[mapping.option_id]
                result.drops.append(DropIntent(
                    old_option_id=mapping.option_id,
                    old_name=opt.get("name", ""),
                    canonical_id=row.notion_page_id,
                    annotation=f"row={row.notion_page_id[:8]} (tombstoned)",
                ))
                dropped_old_ids.add(mapping.option_id)
                result.deleted += 1
                result.mapping_deletes.append(row.notion_page_id)
            elif mapping:
                result.mapping_deletes.append(row.notion_page_id)
                result.details.append(
                    f"member={member_db_id} row={row.notion_page_id[:8]} "
                    "tombstoned canonical with stale mapping (option already "
                    "gone) — deleting mapping",
                )
            continue

        desired_sanitized = _desired_sanitized(row)
        desired_color = row.color or _DEFAULT_COLOR

        # CASE A — mapping exists and option still present in Notion.
        if mapping and mapping.option_id in by_id:
            idx = out_idx_by_id[mapping.option_id]
            current_opt = out[idx]
            current_name = current_opt.get("name", "")
            current_color = current_opt.get("color") or _DEFAULT_COLOR
            current_is_archived = current_name.startswith(_ARCHIVED_PREFIX)
            desired_is_archived = desired_sanitized.startswith(_ARCHIVED_PREFIX)

            name_changed = current_name != desired_sanitized
            color_changed = current_color != desired_color

            if not name_changed and not color_changed:
                # In sync — refresh mapping for last_synced_at freshness.
                result.mapping_writes.append(_Mapping(
                    detail_notion_page_id=row.notion_page_id,
                    member_db_id=member_db_id,
                    option_id=mapping.option_id,
                    option_name=desired_sanitized,
                ))
                continue

            if not name_changed:
                # Color-only change → PATCH it directly (current behaviour).
                # The saga is only used for name changes; color-only PATCHes
                # have not been independently shown to silently no-op.
                out[idx] = {
                    **current_opt,
                    "name": desired_sanitized,
                    "color": desired_color,
                }
                result.renamed += 1
                result.mapping_writes.append(_Mapping(
                    detail_notion_page_id=row.notion_page_id,
                    member_db_id=member_db_id,
                    option_id=mapping.option_id,
                    option_name=desired_sanitized,
                ))
                continue

            # Name change → emit a rename intent (saga executes it).
            existing_new = by_sanitized_name.get(desired_sanitized)
            is_resume = (
                existing_new is not None
                and existing_new.get("id")
                and existing_new["id"] != mapping.option_id
            )

            result.renames.append(RenameIntent(
                old_option_id=mapping.option_id,
                old_name=current_name,
                desired_name=desired_sanitized,
                desired_color=desired_color,
                canonical_id=row.notion_page_id,
                annotation=(
                    f"row={row.notion_page_id[:8]}"
                    + (" (resume)" if is_resume else "")
                ),
            ))
            result.renamed += 1
            if not current_is_archived and desired_is_archived:
                result.archived += 1

            if is_resume:
                dropped_old_ids.add(mapping.option_id)
                # Normalize the pre-existing new entry to carry desired color.
                new_idx = out_idx_by_id[existing_new["id"]]
                out[new_idx] = {
                    **out[new_idx],
                    "name": desired_sanitized,
                    "color": desired_color,
                }
                result.mapping_writes.append(_Mapping(
                    detail_notion_page_id=row.notion_page_id,
                    member_db_id=member_db_id,
                    option_id=existing_new["id"],
                    option_name=desired_sanitized,
                ))
            else:
                out[idx] = {
                    **current_opt,
                    "name": desired_sanitized,
                    "color": desired_color,
                }
                result.mapping_writes.append(_Mapping(
                    detail_notion_page_id=row.notion_page_id,
                    member_db_id=member_db_id,
                    option_id="",  # back-filled from saga
                    option_name=desired_sanitized,
                ))
            continue

        # CASE B — mapping exists but option_id no longer in Notion (manually
        # deleted from the member DB). Drop mapping; fall through to bootstrap.
        if mapping and mapping.option_id not in by_id:
            result.details.append(
                f"member={member_db_id} row={row.notion_page_id[:8]} "
                f"mapped option_id={mapping.option_id[:8]} not in current "
                "options — falling back to bootstrap",
            )

        # CASE C — bootstrap-adopt by sanitized name.
        adopted = by_sanitized_name.get(desired_sanitized)
        if adopted is not None and adopted.get("id"):
            adopt_id = adopted["id"]
            idx = out_idx_by_id[adopt_id]
            current_name = adopted.get("name", "")
            current_color = adopted.get("color") or _DEFAULT_COLOR
            name_changed = current_name != desired_sanitized
            color_changed = current_color != desired_color
            if name_changed:
                # Saga rename (with desired color on the new option).
                result.renames.append(RenameIntent(
                    old_option_id=adopt_id,
                    old_name=current_name,
                    desired_name=desired_sanitized,
                    desired_color=desired_color,
                    canonical_id=row.notion_page_id,
                    annotation=(
                        f"row={row.notion_page_id[:8]} (adopt+rename)"
                    ),
                ))
                out[idx] = {
                    **adopted,
                    "name": desired_sanitized,
                    "color": desired_color,
                }
                result.renamed += 1
                if (
                    not current_name.startswith(_ARCHIVED_PREFIX)
                    and desired_sanitized.startswith(_ARCHIVED_PREFIX)
                ):
                    result.archived += 1
                result.details.append(
                    f"member={member_db_id} row={row.notion_page_id[:8]} "
                    f"adopted existing option by sanitized-name match "
                    f"(saga rename {current_name!r} → {desired_sanitized!r}, "
                    f"color {current_color}→{desired_color})",
                )
                result.mapping_writes.append(_Mapping(
                    detail_notion_page_id=row.notion_page_id,
                    member_db_id=member_db_id,
                    option_id="",  # back-filled from saga
                    option_name=desired_sanitized,
                ))
            elif color_changed:
                # Adopt + color-only PATCH.
                out[idx] = {
                    **adopted,
                    "name": desired_sanitized,
                    "color": desired_color,
                }
                result.renamed += 1
                result.details.append(
                    f"member={member_db_id} row={row.notion_page_id[:8]} "
                    f"adopted existing option (color update only "
                    f"{current_color}→{desired_color})",
                )
                result.mapping_writes.append(_Mapping(
                    detail_notion_page_id=row.notion_page_id,
                    member_db_id=member_db_id,
                    option_id=adopt_id,
                    option_name=desired_sanitized,
                ))
            else:
                result.details.append(
                    f"member={member_db_id} row={row.notion_page_id[:8]} "
                    "adopted existing option by sanitized-name match",
                )
                result.mapping_writes.append(_Mapping(
                    detail_notion_page_id=row.notion_page_id,
                    member_db_id=member_db_id,
                    option_id=adopt_id,
                    option_name=desired_sanitized,
                ))
            continue

        # CASE D — bootstrap-create.
        out.append({"name": desired_sanitized, "color": desired_color})
        new_idx = len(out) - 1
        pending_creates.append((new_idx, row.notion_page_id))
        result.created += 1
        if desired_sanitized.startswith(_ARCHIVED_PREFIX):
            result.archived += 1
        result.mapping_writes.append(_Mapping(
            detail_notion_page_id=row.notion_page_id,
            member_db_id=member_db_id,
            option_id="",
            option_name=desired_sanitized,
        ))

    if dropped_old_ids:
        out = [o for o in out if not o.get("id") or o["id"] not in dropped_old_ids]

    pending_create_names = [
        out[idx]["name"] for idx, _ in pending_creates if idx < len(out)
    ]
    out = _slot_new_options_into_color_clusters(out, pending_create_names)

    result.changed = not _options_equal(out, current_options)
    result.new_options = out
    result._pending_creates = pending_creates  # type: ignore[attr-defined]
    result._dropped_old_ids = dropped_old_ids  # type: ignore[attr-defined]
    return result


def _slot_new_options_into_color_clusters(
    out: list[dict[str, Any]],
    pending_create_names: list[str],
) -> list[dict[str, Any]]:
    """Move each freshly-created option (no id yet, appended at the tail of
    ``out``) to just after the last existing option that shares its color.

    The operator owns the dropdown order on each member DB. Renames /
    color updates / adoptions all leave existing options in their current
    slot — only bootstrap-creates need a position chosen, and the natural
    place is "end of the same-color cluster" so a new blue option lands
    with the other blues rather than at the bottom of the array.

    If no same-color option exists on the member DB yet, the new entry
    stays at the tail (it becomes the start of that color's cluster).
    """
    if not pending_create_names:
        return out

    result = list(out)
    for name in pending_create_names:
        new_idx = next(
            (
                i for i, o in enumerate(result)
                if o.get("name") == name and not o.get("id")
            ),
            None,
        )
        if new_idx is None:
            continue
        new_opt = result[new_idx]
        new_color = new_opt.get("color") or _DEFAULT_COLOR
        last_same_color = -1
        for i, o in enumerate(result):
            if i == new_idx:
                continue
            if (o.get("color") or _DEFAULT_COLOR) == new_color:
                last_same_color = i
        if last_same_color < 0 or last_same_color == new_idx - 1:
            continue
        result.pop(new_idx)
        target = last_same_color + 1 if new_idx > last_same_color else last_same_color
        result.insert(target, new_opt)
    return result


# ---------------------------------------------------------------------------
# Mapping back-fill helpers
# ---------------------------------------------------------------------------


def _back_fill_mapping_ids(
    plan: _PlannerResult,
    patched_options: list[dict[str, Any]],
) -> list[_Mapping]:
    """Replace placeholder ``option_id == ""`` entries from the PATCH response.

    Saga-rename placeholders are filled separately by
    ``_apply_saga_results_to_mappings``; remaining empties are pure creates.
    """
    if not getattr(plan, "_pending_creates", None):
        return list(plan.mapping_writes)

    name_to_id: dict[str, str] = {}
    for opt in patched_options:
        name = opt.get("name", "")
        opt_id = opt.get("id")
        if name and opt_id and name not in name_to_id:
            name_to_id[name] = opt_id

    resolved: list[_Mapping] = []
    for m in plan.mapping_writes:
        if m.option_id:
            resolved.append(m)
            continue
        new_id = name_to_id.get(m.option_name, "")
        if not new_id:
            plan.details.append(
                f"member={m.member_db_id} row={m.detail_notion_page_id[:8]} "
                f"created option {m.option_name!r} but Notion response had "
                "no matching id — mapping skipped (next run will adopt)",
            )
            continue
        resolved.append(_Mapping(
            detail_notion_page_id=m.detail_notion_page_id,
            member_db_id=m.member_db_id,
            option_id=new_id,
            option_name=m.option_name,
        ))
    return resolved


def _apply_saga_results_to_mappings(
    plan: _PlannerResult,
    saga_results: dict[str, str],
) -> None:
    """Fill in option_id on placeholder mappings using saga results.

    Saga results are keyed by canonical row id (``detail_notion_page_id``).
    """
    if not saga_results:
        return
    for m in plan.mapping_writes:
        if m.option_id:
            continue
        new_id = saga_results.get(m.detail_notion_page_id)
        if new_id:
            m.option_id = new_id


def _upsert_mappings(mappings: list[_Mapping]) -> None:
    if not mappings:
        return
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    body = [
        {
            "detail_notion_page_id": m.detail_notion_page_id,
            "member_db_id": m.member_db_id,
            "option_id": m.option_id,
            "option_name": m.option_name,
            "last_synced_at": now_iso,
        }
        for m in mappings
    ]
    _http(
        "POST",
        "/rest/v1/detail_option_mappings?on_conflict=detail_notion_page_id,member_db_id",
        body=body,
        prefer="resolution=merge-duplicates,return=minimal",
    )


def _delete_mappings(
    member_db_id: str,
    detail_notion_page_ids: list[str],
) -> None:
    """DELETE detail_option_mappings rows for tombstoned canonical rows.

    Scoped to one member DB for per-member error isolation. Idempotent.
    """
    if not detail_notion_page_ids:
        return
    page_list = ",".join(f'"{i}"' for i in detail_notion_page_ids)
    _http(
        "DELETE",
        f"/rest/v1/detail_option_mappings?"
        f"member_db_id=eq.{urllib.parse.quote(member_db_id)}"
        f"&detail_notion_page_id=in.({page_list})",
    )


# ---------------------------------------------------------------------------
# I/O sync
# ---------------------------------------------------------------------------


def sync(client: NotionClientWrapper, config: SyncConfig) -> SyncReport:
    """Apply Supabase canonical `detail_rows` → member-DB `Detail` multi-select."""
    report = SyncReport(name=SUB_SYNC_NAME)

    if not config.org_chart_db_id:
        report.errors += 1
        report.details.append(
            "ORG_CHART_DB_ID not configured — cannot enumerate member DBs",
        )
        logger.warning(
            "detail_applier_sync: ORG_CHART_DB_ID not configured — skipping",
        )
        return report

    try:
        _supabase_creds()
    except RuntimeError as e:
        report.errors += 1
        report.details.append(f"Supabase not configured: {e}")
        logger.warning("detail_applier_sync: %s", e)
        return report

    try:
        canonical_rows = _load_canonical_detail_rows()
    except Exception as e:  # noqa: BLE001
        report.errors += 1
        report.details.append(
            f"canonical detail_rows snapshot failed: {type(e).__name__}: {e}",
        )
        logger.exception("detail_applier_sync: canonical snapshot failed")
        return report

    if not canonical_rows:
        # Benign — Detail feature may be uninitialised (Settings DB empty or
        # DETAIL_OPTIONS_DB_ID unset on the previous mirror tick).
        logger.warning(
            "detail_applier_sync: detail_rows is empty — did "
            "detail_canonical_mirror_sync run yet? Skipping with no writes.",
        )
        report.details.append(
            "detail_rows empty — did detail_canonical_mirror_sync run yet?",
        )
        return report

    collisions = _detect_sanitized_collisions(canonical_rows)
    skip_page_ids: set[str] = set()
    for sanitized, page_ids in collisions.items():
        report.errors += 1
        report.details.append(
            f"sanitized-name collision {sanitized!r} on canonical pages "
            f"{[pid[:8] for pid in page_ids]} — skipping all of them",
        )
        skip_page_ids.update(page_ids)
        logger.error(
            "detail_applier_sync: sanitized-name collision %r on pages %s",
            sanitized, [pid[:8] for pid in page_ids],
        )

    try:
        member_dbs = discover_meeting_dbs(client, config.org_chart_db_id)
    except Exception as e:  # noqa: BLE001
        report.errors += 1
        report.details.append(
            f"member DB discovery failed: {type(e).__name__}: {e}",
        )
        logger.exception("detail_applier_sync: member DB discovery failed")
        return report

    try:
        mappings_by_pair = _load_mappings([m.db_id for m in member_dbs])
    except Exception as e:  # noqa: BLE001
        report.errors += 1
        report.details.append(
            f"mapping load failed: {type(e).__name__}: {e}",
        )
        logger.exception("detail_applier_sync: mapping load failed")
        return report

    for member_db in member_dbs:
        owner_label = member_db.owner_name or "?"
        try:
            ds = client.retrieve_data_source(member_db.db_id)
        except Exception as e:  # noqa: BLE001
            report.errors += 1
            report.details.append(
                f"{owner_label}: retrieve_data_source failed: "
                f"{type(e).__name__}: {e}",
            )
            logger.exception(
                "detail_applier_sync: retrieve_data_source failed for %s (%s)",
                owner_label, member_db.db_id,
            )
            continue

        props = (ds.get("properties") or {})
        detail_prop = props.get(_DETAIL_PROPERTY)
        if not detail_prop or detail_prop.get("type") != "multi_select":
            report.errors += 1
            msg = (
                f"{owner_label}: no '{_DETAIL_PROPERTY}' multi_select property"
            )
            report.details.append(msg)
            logger.warning("detail_applier_sync: %s", msg)
            continue

        current_options = (
            detail_prop.get("multi_select", {}).get("options", []) or []
        )
        mappings_for_member = {
            page_id: m
            for (page_id, mdb_id), m in mappings_by_pair.items()
            if mdb_id == member_db.db_id
        }

        plan = _plan_member_db_update(
            canonical_rows=canonical_rows,
            mappings_for_member=mappings_for_member,
            current_options=current_options,
            member_db_id=member_db.db_id,
            skip_page_ids=skip_page_ids,
        )

        if (
            not plan.changed
            and not plan.renames
            and not plan.drops
            and not plan.mapping_deletes
        ):
            logger.debug(
                "detail_applier_sync: %s already in sync (%d options)",
                owner_label, len(current_options),
            )
            continue

        if config.dry_run:
            logger.info(
                "detail_applier_sync: DRY RUN %s would create=%d rename=%d "
                "archived=%d deleted=%d",
                owner_label, plan.created, plan.renamed, plan.archived,
                plan.deleted,
            )
            report.created += plan.created
            report.renamed += plan.renamed
            report.archived += plan.archived
            report.deleted += plan.deleted
            report.details.append(
                f"{owner_label}: would create={plan.created} "
                f"rename={plan.renamed} archived={plan.archived} "
                f"deleted={plan.deleted} (dry-run)",
            )
            continue

        # ----- Live: run rename sagas first -----
        current_state = list(current_options)
        saga_results: dict[str, str] = {}
        for intent in plan.renames:
            try:
                new_id, current_state, saga_details = execute_rename_saga(
                    client=client,
                    member_db_id=member_db.db_id,
                    property_name=_DETAIL_PROPERTY,
                    property_type="multi_select",
                    intent=intent,
                    current_state=current_state,
                )
            except Exception as e:  # noqa: BLE001
                report.errors += 1
                report.details.append(
                    f"{owner_label}: rename saga {intent.old_name!r} → "
                    f"{intent.desired_name!r} failed: "
                    f"{type(e).__name__}: {e}",
                )
                logger.exception(
                    "detail_applier_sync: saga failed for %s (%s) %s → %s",
                    owner_label, member_db.db_id,
                    intent.old_name, intent.desired_name,
                )
                continue
            saga_results[intent.canonical_id] = new_id
            for d in saga_details:
                report.details.append(f"{owner_label}: {d}")

        # ----- Live: run drop sagas (tombstoned canonical rows) -----
        dropped_canonical_ids: set[str] = set()
        for drop_intent in plan.drops:
            try:
                current_state, drop_details = execute_drop_saga(
                    client=client,
                    member_db_id=member_db.db_id,
                    property_name=_DETAIL_PROPERTY,
                    property_type="multi_select",
                    intent=drop_intent,
                    current_state=current_state,
                )
            except Exception as e:  # noqa: BLE001
                report.errors += 1
                report.details.append(
                    f"{owner_label}: drop saga for {drop_intent.old_name!r} "
                    f"failed: {type(e).__name__}: {e}",
                )
                logger.exception(
                    "detail_applier_sync: drop saga failed for %s (%s) %s",
                    owner_label, member_db.db_id, drop_intent.old_name,
                )
                continue
            dropped_canonical_ids.add(drop_intent.canonical_id)
            for d in drop_details:
                report.details.append(f"{owner_label}: {d}")

        # ----- Live: fill mapping_writes from saga results -----
        _apply_saga_results_to_mappings(plan, saga_results)

        # ----- Live: final PATCH (creates / drops / color-only) -----
        final_desired = materialize_final_options(
            new_options=plan.new_options,
            renames=plan.renames,
            saga_results=saga_results,
        )
        patched_options = current_state
        if not _options_equal(final_desired, current_state):
            try:
                patch_response = client.update_data_source(
                    member_db.db_id,
                    {
                        _DETAIL_PROPERTY: {
                            "multi_select": {"options": final_desired},
                        },
                    },
                )
            except Exception as e:  # noqa: BLE001
                report.errors += 1
                report.details.append(
                    f"{owner_label}: final update_data_source failed: "
                    f"{type(e).__name__}: {e}",
                )
                logger.exception(
                    "detail_applier_sync: final PATCH failed for %s (%s)",
                    owner_label, member_db.db_id,
                )
            else:
                patched_options = (
                    ((patch_response or {}).get("properties", {})
                     .get(_DETAIL_PROPERTY, {})
                     .get("multi_select", {})
                     .get("options")) or []
                )
                if not patched_options:
                    try:
                        ds_after = client.retrieve_data_source(member_db.db_id)
                        patched_options = (
                            ((ds_after.get("properties") or {})
                             .get(_DETAIL_PROPERTY, {})
                             .get("multi_select", {})
                             .get("options")) or []
                        )
                    except Exception as e:  # noqa: BLE001
                        report.details.append(
                            f"{owner_label}: re-fetch after PATCH failed: "
                            f"{type(e).__name__}: {e} — mapping back-fill "
                            "skipped (next run will adopt by name match)",
                        )
                        patched_options = []

        resolved_mappings = _back_fill_mapping_ids(plan, patched_options)
        resolved_mappings = [m for m in resolved_mappings if m.option_id]

        try:
            _upsert_mappings(resolved_mappings)
        except Exception as e:  # noqa: BLE001
            report.errors += 1
            report.details.append(
                f"{owner_label}: mapping back-fill failed: "
                f"{type(e).__name__}: {e} — next run will adopt by "
                "sanitized-name match without creating duplicates",
            )
            logger.exception(
                "detail_applier_sync: mapping back-fill failed for %s (%s)",
                owner_label, member_db.db_id,
            )

        # ----- Live: delete mappings for tombstoned canonical rows -----
        # Only DELETE for canonical_ids whose drop saga succeeded (or where
        # no drop was needed — stale mapping with no live option).
        successful_deletes = [
            cid for cid in plan.mapping_deletes
            if (
                cid in dropped_canonical_ids
                or not any(d.canonical_id == cid for d in plan.drops)
            )
        ]
        if successful_deletes:
            try:
                _delete_mappings(member_db.db_id, successful_deletes)
            except Exception as e:  # noqa: BLE001
                report.errors += 1
                report.details.append(
                    f"{owner_label}: mapping delete failed for "
                    f"{len(successful_deletes)} tombstoned row(s): "
                    f"{type(e).__name__}: {e} — next run retries",
                )
                logger.exception(
                    "detail_applier_sync: mapping delete failed for %s (%s)",
                    owner_label, member_db.db_id,
                )

        report.created += plan.created
        report.renamed += plan.renamed
        report.archived += plan.archived
        report.deleted += plan.deleted
        for d in plan.details:
            report.details.append(d)
        logger.info(
            "detail_applier_sync: %s created=%d renamed=%d archived=%d "
            "deleted=%d",
            owner_label, plan.created, plan.renamed, plan.archived, plan.deleted,
        )

    if len(report.details) > _DETAIL_CAP:
        truncated = len(report.details) - _DETAIL_CAP
        report.details = report.details[:_DETAIL_CAP]
        report.details.append(f"… (+{truncated} more details truncated)")

    return report


__all__ = ["SUB_SYNC_NAME", "sync"]
