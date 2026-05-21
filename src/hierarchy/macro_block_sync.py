"""Apply Supabase canonical (``public.hierarchy_rows`` Tier 0) → ``Work area``
select options on every member Meeting Notes DB.

Runs daily 07:00 Madrid as the second sub-sync in the ``hierarchy_sync``
orchestrator, **after** ``canonical_mirror_sync`` (which writes today's
Notion Hierarchy DB state into Supabase). This applier reads the
freshly-updated canonical and reconciles each member DB's ``Work area``
select.

Contract:

  * Every live (``deleted_at IS NULL``) + ``active`` Tier 0 canonical row →
    every member DB has an option whose name is the row's sanitized name.
  * Live + inactive row → option soft-archived as
    ``(archived) <sanitized name>``.
  * Tombstoned (``deleted_at`` set) row → option **removed** from the
    member DB; any pages tagged on it have the ``Work area`` property
    cleared first; the mapping row is DELETEd from Supabase.
  * Options on a member DB that don't correspond to any Tier 0 canonical
    row are left alone (e.g. legacy ``Standup`` / ``1:1`` from before
    Work area existed). Pass-through verbatim.
  * Idempotent: when state already matches, no PATCH is issued.

Sanitization
------------
Notion's API forbids commas in select option names (the comma is the
multi-select separator). The Hierarchy DB / Supabase canonical keep
commas verbatim (humans expect ``"Sourcing, Investing & Divesting
(Dealflow)"`` as a display name). Strip commas only when writing to or
matching against Notion option names — see ``_sanitize_option_name``.

Mapping table
-------------
``public.work_area_option_mappings`` pins
``(hierarchy_page_id, member_db_id) → option_id``. Without the mapping a
Tier 0 rename would look like "name doesn't match anything → CREATE a new
option" while the old option stays orphaned with every page that was
tagged on it.

Notion's ``data_sources.update`` silently no-ops select-option renames
(verified 2026-05-21 via ``scripts/diag_work_area_options.py``). To
achieve a *logical* rename we run the 5-step saga in
``src/hierarchy/_rename_saga.py``: PATCH 1 add new option → query +
migrate every tagged page → PATCH 2 drop old option. Option IDs change
on rename; the mapping table absorbs the churn (back-filled with the
post-saga id after each rename).

Bootstrap
---------
On empty mapping table (or for a fresh ``(page_id, member_db_id)``
pair), match by sanitized name against existing options. Found →
adopt the existing option id; if the current Notion option name still
carries a comma, also queue an in-place rename to its comma-free form
(heals data on first contact). Not found → create a fresh option and
back-fill the mapping from Notion's PATCH response.

Recovery
--------
If the mapping back-fill upsert fails after a successful Notion PATCH,
the next run's bootstrap path heals via sanitized-name match — no
duplicate options created. One ``error`` is recorded with the recovery
note in details.

Sharing: Supabase HTTP helpers (``_supabase_creds``, ``_http``) are
re-used via cross-import from ``canonical_mirror_sync`` to keep
HTTP/auth code in one place.

Non-goals:
  * Touch Tier 1 / Tier 2 (those aren't propagated to member-DB
    ``Work area`` today — only Tier 0 is).
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
from src.meeting_db_registry import discover_meeting_dbs
from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)

SUB_SYNC_NAME = "macro_block_sync"

_TIER_0_VALUE = "0. Macro Work Block"
_WORK_AREA_PROPERTY = "Work area"
_ARCHIVED_PREFIX = "(archived) "
_DETAIL_CAP = 50


def _sanitize_option_name(name: str) -> str:
    """Strip commas (Notion forbids them in select options); collapse whitespace.

    Examples::

        "Sourcing, Investing & Divesting (Dealflow)"
          → "Sourcing Investing & Divesting (Dealflow)"
        "A,B" → "A B"   (inserted space — never runs words together)
        "  trailing  " → "trailing"
    """
    return " ".join(name.replace(",", " ").split())


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class _CanonicalTier0Row:
    notion_page_id: str
    name: str
    active: bool
    deleted_at: str | None


@dataclass
class _Mapping:
    hierarchy_page_id: str
    member_db_id: str
    option_id: str
    option_name: str


@dataclass
class _PlannerResult:
    new_options: list[dict[str, Any]] = field(default_factory=list)
    mapping_writes: list[_Mapping] = field(default_factory=list)
    # Hierarchy page ids whose mapping rows should be DELETEd from Supabase
    # (tombstoned canonical rows whose member-DB option has been dropped).
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


def _load_canonical_tier_0() -> list[_CanonicalTier0Row]:
    """Read every Tier 0 row from ``hierarchy_rows`` — including tombstones."""
    tier_value = urllib.parse.quote(_TIER_0_VALUE)
    raw = _http(
        "GET",
        "/rest/v1/hierarchy_rows?select=notion_page_id,name,active,deleted_at"
        f"&tier=eq.{tier_value}&limit=10000",
    ) or []
    rows = [
        _CanonicalTier0Row(
            notion_page_id=r["notion_page_id"],
            name=r.get("name") or "",
            active=bool(r.get("active")),
            deleted_at=r.get("deleted_at"),
        )
        for r in raw
        if r.get("notion_page_id") and (r.get("name") or "").strip()
    ]
    rows.sort(key=lambda r: r.name)
    return rows


def _load_mappings(member_db_ids: list[str]) -> dict[tuple[str, str], _Mapping]:
    """Return ``{(hierarchy_page_id, member_db_id): _Mapping}``."""
    if not member_db_ids:
        return {}
    in_list = ",".join(f'"{i}"' for i in member_db_ids)
    raw = _http(
        "GET",
        f"/rest/v1/work_area_option_mappings?select=*&member_db_id=in.({in_list})"
        "&limit=10000",
    ) or []
    out: dict[tuple[str, str], _Mapping] = {}
    for r in raw:
        page_id = r.get("hierarchy_page_id")
        mdb_id = r.get("member_db_id")
        if not page_id or not mdb_id:
            continue
        out[(page_id, mdb_id)] = _Mapping(
            hierarchy_page_id=page_id,
            member_db_id=mdb_id,
            option_id=r.get("option_id") or "",
            option_name=r.get("option_name") or "",
        )
    return out


# ---------------------------------------------------------------------------
# Collision detection
# ---------------------------------------------------------------------------


def _desired_sanitized(row: _CanonicalTier0Row) -> str:
    """Compute the desired sanitized option name for a non-tombstoned row.

    Tombstoned rows (``deleted_at IS NOT NULL``) are dropped from the member
    DB entirely — they don't claim a name. The defensive prefix logic stays
    for inactive-but-not-tombstoned rows.
    """
    is_archived_state = (not row.active) and (row.deleted_at is None)
    base = f"{_ARCHIVED_PREFIX}{row.name}" if is_archived_state else row.name
    return _sanitize_option_name(base)


def _detect_sanitized_collisions(
    canonical_rows: list[_CanonicalTier0Row],
) -> dict[str, list[str]]:
    """Return ``{sanitized_name: [page_id, …]}`` for any name with >1 row.

    Tombstoned rows are excluded — they don't claim a name on the member DB.
    """
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
    """Compare option arrays by (id, name) — color and other fields ignored."""
    if len(a) != len(b):
        return False
    for opt_a, opt_b in zip(a, b):
        if opt_a.get("id") != opt_b.get("id"):
            return False
        if opt_a.get("name") != opt_b.get("name"):
            return False
    return True


def _plan_member_db_update(
    canonical_rows: list[_CanonicalTier0Row],
    mappings_for_member: dict[str, _Mapping],
    current_options: list[dict[str, Any]],
    member_db_id: str,
    skip_page_ids: set[str] | None = None,
) -> _PlannerResult:
    """Compute the desired option array for one member DB. Pure — no I/O.

    Args:
        canonical_rows: every Tier 0 row from the canonical snapshot,
            sorted deterministically.
        mappings_for_member: ``{hierarchy_page_id: _Mapping}`` filtered to
            this member DB.
        current_options: the member DB's current ``Work area`` options
            (the full Notion options array).
        member_db_id: ID of the member DB being planned for — embedded in
            ``mapping_writes``.
        skip_page_ids: canonical page_ids excluded by collision detection.

    Returns:
        ``_PlannerResult`` with the proposed ``new_options`` array,
        ``mapping_writes`` to upsert into Supabase, counters, and a
        ``changed`` flag set when ``new_options`` differs from
        ``current_options``.
    """
    result = _PlannerResult()
    skip = skip_page_ids or set()

    # Indices over the current options for fast lookup.
    by_id: dict[str, dict[str, Any]] = {
        opt["id"]: opt for opt in current_options if opt.get("id")
    }
    by_sanitized_name: dict[str, dict[str, Any]] = {
        _sanitize_option_name(opt.get("name", "")): opt
        for opt in current_options
        if opt.get("name")
    }

    # Start with every current option carried through verbatim. Mutate the
    # ones we own; everything else (legacy `Standup` / `1:1`) passes through.
    out: list[dict[str, Any]] = [dict(opt) for opt in current_options]
    out_idx_by_id: dict[str, int] = {
        opt["id"]: i for i, opt in enumerate(out) if opt.get("id")
    }
    # Track which placeholder slots in `out` correspond to fresh creates
    # so the caller can back-fill their ids after the PATCH response.
    pending_creates: list[tuple[int, str]] = []  # (out_index, hierarchy_page_id)
    # Track old option ids that the saga will drop (resume-path or simple
    # bootstrap drops). Filtered out of `out` before returning.
    dropped_old_ids: set[str] = set()

    for row in canonical_rows:
        if row.notion_page_id in skip:
            continue
        if not row.name.strip():
            continue

        mapping = mappings_for_member.get(row.notion_page_id)

        # CASE T — tombstoned canonical row → drop the option entirely from
        # the member DB (rather than the prior "(archived) X" rename).
        # Skip CASE C/D bootstrap paths so we never re-create a tombstoned
        # row's option after dropping it.
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
                # Stale mapping (option already gone) — just clean up.
                result.mapping_deletes.append(row.notion_page_id)
                result.details.append(
                    f"member={member_db_id} row={row.notion_page_id[:8]} "
                    "tombstoned canonical with stale mapping (option already "
                    "gone) — deleting mapping",
                )
            # No mapping → nothing to clean up.
            continue

        desired_sanitized = _desired_sanitized(row)

        # CASE A — mapping exists and option still present in Notion.
        if mapping and mapping.option_id in by_id:
            idx = out_idx_by_id[mapping.option_id]
            current_opt = out[idx]
            current_name = current_opt.get("name", "")
            current_is_archived = current_name.startswith(_ARCHIVED_PREFIX)
            desired_is_archived = desired_sanitized.startswith(_ARCHIVED_PREFIX)

            if current_name == desired_sanitized:
                # In sync — refresh mapping for last_synced_at freshness.
                result.mapping_writes.append(_Mapping(
                    hierarchy_page_id=row.notion_page_id,
                    member_db_id=member_db_id,
                    option_id=mapping.option_id,
                    option_name=desired_sanitized,
                ))
                continue

            # Name change required → emit a rename intent (saga will execute it).
            # Resume detection: an option with the desired name already exists
            # in current_state, distinct from the mapped old option → PATCH 1
            # of the saga has already run on a prior tick.
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
                # Preserve the existing option's color — Work area is NOT
                # canonical-color-driven (unlike Detail / External Org), so
                # the saga must carry the OLD option's color through to the
                # new one or the tag visually "resets" to default.
                desired_color=current_opt.get("color"),
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
                # Drop the OLD option from `out` — the new option (already in
                # current_state, hence in `out`) carries the desired name.
                dropped_old_ids.add(mapping.option_id)
                result.mapping_writes.append(_Mapping(
                    hierarchy_page_id=row.notion_page_id,
                    member_db_id=member_db_id,
                    option_id=existing_new["id"],
                    option_name=desired_sanitized,
                ))
            else:
                # Standard rename: keep the OLD id slot but with the desired
                # name. The I/O layer swaps id→new_id after the saga runs.
                out[idx] = {**current_opt, "name": desired_sanitized}
                result.mapping_writes.append(_Mapping(
                    hierarchy_page_id=row.notion_page_id,
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
            # Do NOT carry the dropped mapping into mapping_writes —
            # the row gets a new option_id below.

        # CASE C — no usable mapping. Try bootstrap-adopt by sanitized name.
        adopted = by_sanitized_name.get(desired_sanitized)
        if adopted is not None and adopted.get("id"):
            adopt_id = adopted["id"]
            idx = out_idx_by_id[adopt_id]
            current_name = adopted.get("name", "")
            if current_name != desired_sanitized:
                # Existing option has a comma (or other whitespace cruft);
                # rename it via saga to its comma-free form. Heals the data.
                result.renames.append(RenameIntent(
                    old_option_id=adopt_id,
                    old_name=current_name,
                    desired_name=desired_sanitized,
                    # Preserve existing option's color (see CASE A note).
                    desired_color=adopted.get("color"),
                    canonical_id=row.notion_page_id,
                    annotation=(
                        f"row={row.notion_page_id[:8]} (adopt+rename)"
                    ),
                ))
                # Keep slot with OLD id but desired name; I/O swaps id post-saga.
                out[idx] = {**adopted, "name": desired_sanitized}
                result.renamed += 1
                if (
                    not current_name.startswith(_ARCHIVED_PREFIX)
                    and desired_sanitized.startswith(_ARCHIVED_PREFIX)
                ):
                    result.archived += 1
                result.details.append(
                    f"member={member_db_id} row={row.notion_page_id[:8]} "
                    f"adopted existing option by sanitized-name match "
                    f"(saga rename {current_name!r} → {desired_sanitized!r})",
                )
                result.mapping_writes.append(_Mapping(
                    hierarchy_page_id=row.notion_page_id,
                    member_db_id=member_db_id,
                    option_id="",  # back-filled from saga
                    option_name=desired_sanitized,
                ))
            else:
                result.details.append(
                    f"member={member_db_id} row={row.notion_page_id[:8]} "
                    "adopted existing option by sanitized-name match",
                )
                result.mapping_writes.append(_Mapping(
                    hierarchy_page_id=row.notion_page_id,
                    member_db_id=member_db_id,
                    option_id=adopt_id,
                    option_name=desired_sanitized,
                ))
            continue

        # CASE D — bootstrap-create. Append a fresh option (no id; Notion
        # assigns one) and queue a mapping placeholder for back-fill.
        out.append({"name": desired_sanitized})
        new_idx = len(out) - 1
        pending_creates.append((new_idx, row.notion_page_id))
        result.created += 1
        if desired_sanitized.startswith(_ARCHIVED_PREFIX):
            result.archived += 1
        result.mapping_writes.append(_Mapping(
            hierarchy_page_id=row.notion_page_id,
            member_db_id=member_db_id,
            option_id="",  # back-filled from PATCH response
            option_name=desired_sanitized,
        ))

    # Filter out options dropped by resume-path renames (the new option is
    # already in `out`; the old one is what the saga will delete).
    if dropped_old_ids:
        out = [o for o in out if not o.get("id") or o["id"] not in dropped_old_ids]

    # Did the array actually change? If not, the caller can skip the PATCH.
    result.changed = not _options_equal(out, current_options)
    result.new_options = out
    # Stash pending-create indices on the result for the I/O layer to
    # consume after the PATCH (so it can recover the assigned ids).
    result._pending_creates = pending_creates  # type: ignore[attr-defined]
    result._dropped_old_ids = dropped_old_ids  # type: ignore[attr-defined]
    return result


# ---------------------------------------------------------------------------
# Mapping back-fill helpers
# ---------------------------------------------------------------------------


def _back_fill_mapping_ids(
    plan: _PlannerResult,
    patched_options: list[dict[str, Any]],
) -> list[_Mapping]:
    """Replace placeholder ``option_id == ""`` entries in ``mapping_writes``
    using the option ids assigned by Notion in the PATCH response.

    Saga-rename placeholders are filled separately by
    ``_apply_saga_results_to_mappings`` BEFORE this function runs — by the
    time we get here, the only remaining empty option_ids should belong to
    pure creates (or failed sagas, which we surface and skip).

    Match by name within the patched options array. Returns the list of
    fully-resolved mappings ready to upsert.
    """
    if not getattr(plan, "_pending_creates", None):
        return list(plan.mapping_writes)

    # Build a name → option_id lookup from the patched response.
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
            # Notion didn't return an id for our created option — surface
            # via details; the next run's bootstrap-adopt will heal.
            plan.details.append(
                f"member={m.member_db_id} row={m.hierarchy_page_id[:8]} "
                f"created option {m.option_name!r} but Notion response had "
                "no matching id — mapping skipped (next run will adopt)",
            )
            continue
        resolved.append(_Mapping(
            hierarchy_page_id=m.hierarchy_page_id,
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

    Saga results are keyed by canonical row id (``hierarchy_page_id``). After
    this runs, mapping_writes entries for completed sagas carry the
    post-saga option_id; pure-create placeholders are left empty for
    ``_back_fill_mapping_ids`` to handle from the final PATCH response.
    """
    if not saga_results:
        return
    for m in plan.mapping_writes:
        if m.option_id:
            continue
        new_id = saga_results.get(m.hierarchy_page_id)
        if new_id:
            m.option_id = new_id




def _upsert_mappings(mappings: list[_Mapping]) -> None:
    """POST upsert into ``work_area_option_mappings``."""
    if not mappings:
        return
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    body = [
        {
            "hierarchy_page_id": m.hierarchy_page_id,
            "member_db_id": m.member_db_id,
            "option_id": m.option_id,
            "option_name": m.option_name,
            "last_synced_at": now_iso,
        }
        for m in mappings
    ]
    _http(
        "POST",
        "/rest/v1/work_area_option_mappings?on_conflict=hierarchy_page_id,member_db_id",
        body=body,
        prefer="resolution=merge-duplicates,return=minimal",
    )


def _delete_mappings(
    member_db_id: str,
    hierarchy_page_ids: list[str],
) -> None:
    """DELETE mapping rows from Supabase for tombstoned canonical rows.

    Scoped to a single member DB to keep the saga's per-member error
    isolation. Idempotent — re-running with no remaining rows is a no-op.
    """
    if not hierarchy_page_ids:
        return
    page_list = ",".join(f'"{i}"' for i in hierarchy_page_ids)
    _http(
        "DELETE",
        f"/rest/v1/work_area_option_mappings?"
        f"member_db_id=eq.{urllib.parse.quote(member_db_id)}"
        f"&hierarchy_page_id=in.({page_list})",
    )


# ---------------------------------------------------------------------------
# I/O sync
# ---------------------------------------------------------------------------


def sync(client: NotionClientWrapper, config: SyncConfig) -> SyncReport:
    """Apply Supabase canonical Tier 0 → member-DB ``Work area`` options."""
    report = SyncReport(name=SUB_SYNC_NAME)

    if not config.org_chart_db_id:
        report.errors += 1
        report.details.append(
            "ORG_CHART_DB_ID not configured — cannot enumerate member DBs",
        )
        logger.warning(
            "macro_block_sync: ORG_CHART_DB_ID not configured — skipping",
        )
        return report

    try:
        _supabase_creds()
    except RuntimeError as e:
        report.errors += 1
        report.details.append(f"Supabase not configured: {e}")
        logger.warning("macro_block_sync: %s", e)
        return report

    # ----- Canonical snapshot -----
    try:
        canonical_rows = _load_canonical_tier_0()
    except Exception as e:  # noqa: BLE001
        report.errors += 1
        report.details.append(
            f"canonical Tier 0 snapshot failed: {type(e).__name__}: {e}",
        )
        logger.exception("macro_block_sync: canonical snapshot failed")
        return report

    if not canonical_rows:
        # Benign — likely `canonical_mirror_sync` hasn't run yet OR the
        # table is empty. NOT an error; just a warning.
        logger.warning(
            "macro_block_sync: canonical Tier 0 set is empty — did "
            "canonical_mirror_sync run yet? Skipping with no writes.",
        )
        report.details.append(
            "canonical Tier 0 empty — did canonical_mirror_sync run yet?",
        )
        return report

    # ----- Collision detection (before per-member planning) -----
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
            "macro_block_sync: sanitized-name collision %r on pages %s",
            sanitized, [pid[:8] for pid in page_ids],
        )

    # ----- Discover member DBs -----
    try:
        member_dbs = discover_meeting_dbs(client, config.org_chart_db_id)
    except Exception as e:  # noqa: BLE001
        report.errors += 1
        report.details.append(
            f"member DB discovery failed: {type(e).__name__}: {e}",
        )
        logger.exception("macro_block_sync: member DB discovery failed")
        return report

    # ----- Load all mappings for this run's member DBs in one round-trip -----
    try:
        mappings_by_pair = _load_mappings([m.db_id for m in member_dbs])
    except Exception as e:  # noqa: BLE001
        report.errors += 1
        report.details.append(
            f"mapping load failed: {type(e).__name__}: {e}",
        )
        logger.exception("macro_block_sync: mapping load failed")
        return report

    # ----- Per-member planning + apply -----
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
                "macro_block_sync: retrieve_data_source failed for %s (%s)",
                owner_label, member_db.db_id,
            )
            continue

        props = (ds.get("properties") or {})
        work_area_prop = props.get(_WORK_AREA_PROPERTY)
        if not work_area_prop or work_area_prop.get("type") != "select":
            report.errors += 1
            msg = (
                f"{owner_label}: no '{_WORK_AREA_PROPERTY}' select property — "
                "rename `Meeting type` → `Work area` first"
            )
            report.details.append(msg)
            logger.warning("macro_block_sync: %s", msg)
            continue

        current_options = work_area_prop.get("select", {}).get("options", []) or []
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
                "macro_block_sync: %s already in sync (%d options)",
                owner_label, len(current_options),
            )
            continue

        if config.dry_run:
            logger.info(
                "macro_block_sync: DRY RUN %s would create=%d rename=%d "
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
                    property_name=_WORK_AREA_PROPERTY,
                    property_type="select",
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
                    "macro_block_sync: saga failed for %s (%s) %s → %s",
                    owner_label, member_db.db_id,
                    intent.old_name, intent.desired_name,
                )
                continue
            saga_results[intent.canonical_id] = new_id
            for d in saga_details:
                report.details.append(f"{owner_label}: {d}")

        # ----- Live: run drop sagas (tombstoned canonical rows) -----
        # Tracks which canonical_ids dropped successfully — only those
        # mappings get DELETEd from Supabase below.
        dropped_canonical_ids: set[str] = set()
        for drop_intent in plan.drops:
            try:
                current_state, drop_details = execute_drop_saga(
                    client=client,
                    member_db_id=member_db.db_id,
                    property_name=_WORK_AREA_PROPERTY,
                    property_type="select",
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
                    "macro_block_sync: drop saga failed for %s (%s) %s",
                    owner_label, member_db.db_id, drop_intent.old_name,
                )
                continue
            dropped_canonical_ids.add(drop_intent.canonical_id)
            for d in drop_details:
                report.details.append(f"{owner_label}: {d}")

        # ----- Live: fill mapping_writes from saga results -----
        _apply_saga_results_to_mappings(plan, saga_results)

        # ----- Live: final PATCH (creates / drops / legacy) -----
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
                        _WORK_AREA_PROPERTY: {
                            "select": {"options": final_desired},
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
                    "macro_block_sync: final PATCH failed for %s (%s)",
                    owner_label, member_db.db_id,
                )
            else:
                patched_options = (
                    ((patch_response or {}).get("properties", {})
                     .get(_WORK_AREA_PROPERTY, {})
                     .get("select", {})
                     .get("options")) or []
                )
                if not patched_options:
                    try:
                        ds_after = client.retrieve_data_source(member_db.db_id)
                        patched_options = (
                            ((ds_after.get("properties") or {})
                             .get(_WORK_AREA_PROPERTY, {})
                             .get("select", {})
                             .get("options")) or []
                        )
                    except Exception as e:  # noqa: BLE001
                        report.details.append(
                            f"{owner_label}: re-fetch after PATCH failed: "
                            f"{type(e).__name__}: {e} — mapping back-fill "
                            "skipped (next run will adopt by name match)",
                        )
                        patched_options = []

        # Back-fill pure-create mapping placeholders from the final PATCH
        # response. Saga renames are already filled by
        # _apply_saga_results_to_mappings; anything still empty (failed
        # saga / failed create) is dropped to avoid writing empty ids.
        resolved_mappings = _back_fill_mapping_ids(plan, patched_options)
        resolved_mappings = [m for m in resolved_mappings if m.option_id]

        # ----- Live: upsert mappings -----
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
                "macro_block_sync: mapping back-fill failed for %s (%s)",
                owner_label, member_db.db_id,
            )

        # ----- Live: delete mappings for tombstoned canonical rows -----
        # Restricted to canonical_ids whose drop saga succeeded — leaving a
        # mapping for a still-existing option would be worse than leaving a
        # stale mapping after a failed drop (which the next tick will retry).
        # Mappings for "stale mapping with no live option" cases (no drop
        # saga needed) are included verbatim from plan.mapping_deletes.
        successful_deletes = [
            cid for cid in plan.mapping_deletes
            if (
                # Either we ran a successful drop saga,
                cid in dropped_canonical_ids
                # or there was nothing to drop (no DropIntent for this id).
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
                    "macro_block_sync: mapping delete failed for %s (%s)",
                    owner_label, member_db.db_id,
                )

        report.created += plan.created
        report.renamed += plan.renamed
        report.archived += plan.archived
        report.deleted += plan.deleted
        for d in plan.details:
            report.details.append(d)
        logger.info(
            "macro_block_sync: %s created=%d renamed=%d archived=%d deleted=%d",
            owner_label, plan.created, plan.renamed, plan.archived, plan.deleted,
        )

    # Cap details so big runs don't bloat the log line.
    if len(report.details) > _DETAIL_CAP:
        truncated = len(report.details) - _DETAIL_CAP
        report.details = report.details[:_DETAIL_CAP]
        report.details.append(f"… (+{truncated} more details truncated)")

    return report


__all__ = ["SUB_SYNC_NAME", "sync"]
