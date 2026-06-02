"""Apply Supabase ``ReportingNz_deals`` (filtered by stage) → ``External Org``
select options on every member Meeting Notes DB.

Unlike Detail and Work area, External Org has **no Notion editing surface for
the option list itself** — the canonical record already lives in Supabase
(synced from Affinity into ``public."ReportingNz_deals"``). This sub-sync reads
those rows directly each tick, applies a hard-coded stage filter + ordering,
and propagates the result into every active member DB.

Filter + sort + color rules:

  +-------------------------------------------------+--------+----------+
  | Stage                                           | Color  | Priority |
  +-------------------------------------------------+--------+----------+
  | Portfolio                                       | orange | 0        |
  | DD phase                                        | blue   | 1        |
  | Working on a deal (significant effort)          | blue   | 2        |
  | Under analysis (team assigned, moderate effort) | blue   | 3        |
  +-------------------------------------------------+--------+----------+

Within a stage, options are ordered alphabetically by deal name.

Stage transitions OUT of the filter (e.g. Portfolio → Discarded) → existing
option soft-archived as ``(archived) X`` on every member DB. Mapping kept so a
later re-entry into the filter un-archives in place.

Relationship to the Hierarchy DB: the same tracked deal set is written into the
Notion Hierarchy DB by ``deal_hierarchy_sync`` (which runs first in the
orchestrator). That sub-sync owns the hierarchy rows directly — keyed on a
``Deal ID`` property — so this applier no longer name-matches deals to hierarchy
rows; it just fans the deal list out to member-DB ``External Org`` selects.

Sharing: Supabase HTTP helpers via cross-import from ``canonical_mirror_sync``;
sanitization rule from ``macro_block_sync``.

Non-goals:
  * Garbage-collect orphan ``(archived) X`` options or mappings for departed
    members.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.config import SyncConfig
from src.hierarchy._rename_saga import (
    RenameIntent,
    execute_rename_saga,
    materialize_final_options,
)
from src.hierarchy.base import SyncReport
from src.hierarchy.canonical_mirror_sync import _http, _supabase_creds
from src.hierarchy.macro_block_sync import _sanitize_option_name
from src.meeting_db_registry import discover_meeting_dbs
from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)

SUB_SYNC_NAME = "external_org_applier_sync"

_EXTERNAL_ORG_PROPERTY = "External Org"
_ARCHIVED_PREFIX = "(archived) "
_DETAIL_CAP = 50

# Stage taxonomy (hard-coded — revisit when the deal team changes it).
_STAGE_PORTFOLIO = "Portfolio"
_STAGE_DD_PHASE = "DD phase"
_STAGE_WORKING = "Working on a deal (significant effort)"
_STAGE_UNDER_ANALYSIS = "Under analysis (team assigned, moderate effort)"

_ALLOWED_STAGES: tuple[str, ...] = (
    _STAGE_PORTFOLIO,
    _STAGE_DD_PHASE,
    _STAGE_WORKING,
    _STAGE_UNDER_ANALYSIS,
)

_STAGE_PRIORITY: dict[str, int] = {
    s: i for i, s in enumerate(_ALLOWED_STAGES)
}

_STAGE_TO_COLOR: dict[str, str] = {
    _STAGE_PORTFOLIO: "orange",
    _STAGE_DD_PHASE: "blue",
    _STAGE_WORKING: "blue",
    _STAGE_UNDER_ANALYSIS: "blue",
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class _CanonicalDeal:
    deal_id: str  # uuid from ReportingNz_deals.id
    name: str
    stage: str

    @property
    def color(self) -> str:
        return _STAGE_TO_COLOR.get(self.stage, "default")

    @property
    def sort_key(self) -> tuple[int, str]:
        return (_STAGE_PRIORITY.get(self.stage, 99), self.name.lower())


@dataclass
class _Mapping:
    deal_id: str
    member_db_id: str
    option_id: str
    option_name: str


@dataclass
class _PlannerResult:
    new_options: list[dict[str, Any]] = field(default_factory=list)
    mapping_writes: list[_Mapping] = field(default_factory=list)
    renames: list[RenameIntent] = field(default_factory=list)
    created: int = 0
    renamed: int = 0
    archived: int = 0
    errors: int = 0
    details: list[str] = field(default_factory=list)
    changed: bool = False


# ---------------------------------------------------------------------------
# Snapshot loaders
# ---------------------------------------------------------------------------


def _load_canonical_deals() -> list[_CanonicalDeal]:
    """Read ``ReportingNz_deals``, filter by allowed stages, sort by priority+name."""
    # Fetch only is_active rows to keep the payload tight; stage filter in Python.
    raw = _http(
        "GET",
        "/rest/v1/ReportingNz_deals?select=id,name,stage&is_active=eq.true"
        "&limit=10000",
    ) or []
    deals: list[_CanonicalDeal] = []
    for r in raw:
        deal_id = r.get("id")
        name = (r.get("name") or "").strip()
        stage = r.get("stage") or ""
        if not deal_id or not name or stage not in _ALLOWED_STAGES:
            continue
        deals.append(_CanonicalDeal(deal_id=deal_id, name=name, stage=stage))
    deals.sort(key=lambda d: d.sort_key)
    return deals


def _load_mappings(member_db_ids: list[str]) -> dict[tuple[str, str], _Mapping]:
    """Return ``{(deal_id, member_db_id): _Mapping}``."""
    if not member_db_ids:
        return {}
    in_list = ",".join(f'"{i}"' for i in member_db_ids)
    raw = _http(
        "GET",
        f"/rest/v1/external_org_option_mappings?select=*&member_db_id=in.({in_list})"
        "&limit=10000",
    ) or []
    out: dict[tuple[str, str], _Mapping] = {}
    for r in raw:
        deal_id = r.get("deal_id")
        mdb_id = r.get("member_db_id")
        if not deal_id or not mdb_id:
            continue
        out[(deal_id, mdb_id)] = _Mapping(
            deal_id=deal_id,
            member_db_id=mdb_id,
            option_id=r.get("option_id") or "",
            option_name=r.get("option_name") or "",
        )
    return out


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
        ca = opt_a.get("color") or "default"
        cb = opt_b.get("color") or "default"
        if ca != cb:
            return False
    return True


def _finalize_out(
    out: list[dict[str, Any]],
    mapping_writes: list[_Mapping],
    renames: list[RenameIntent],
    canonical_deals: list[_CanonicalDeal],
    legacy_options_with_tags: set[str] | None,
) -> tuple[list[dict[str, Any]], int]:
    """Apply final ordering + legacy cleanup to the options array.

    Buckets every option in ``out`` by ownership and archival state, then
    reorders:

      * **Top**: canonical active options, ordered by stage priority + alpha.
      * **Middle**: bootstrap-create placeholders (no id yet; Notion assigns
        ids on PATCH; their relative order doesn't matter because their
        names already encode stage-priority alpha).
      * **Bottom**: ``(archived) X`` options and surviving legacy options,
        sorted alphabetically (non-archived first, archived after).

    Legacy options whose id is NOT in ``legacy_options_with_tags`` are
    **dropped** (Notion removes them on PATCH). When ``legacy_options_with_tags``
    is ``None`` (tag-check unavailable), every legacy option is kept (still
    moved to the bottom — drop semantics is opt-in via a successful tag-check).

    Saga-target entries (planner-mutated to desired name but still carrying
    their OLD option_id at finalize-time) are treated as canonical-owned via
    the ``renames`` list — the I/O layer swaps the ids post-saga.

    Returns ``(new_out, dropped_count)``.
    """
    owned_ids = {m.option_id for m in mapping_writes if m.option_id}
    # Saga renames carry OLD ids on `out` until the I/O layer swaps them.
    # Treat those as canonical-owned so they don't fall into the legacy
    # bucket and either get dropped or sorted incorrectly.
    for intent in renames:
        if intent.old_option_id:
            owned_ids.add(intent.old_option_id)
    id_to_deal: dict[str, _CanonicalDeal] = {}
    for m in mapping_writes:
        if not m.option_id:
            continue
        for d in canonical_deals:
            if d.deal_id == m.deal_id:
                id_to_deal[m.option_id] = d
                break
    # Map OLD ids on saga-target entries to their canonical deal so the
    # canonical-active sort uses the deal's stage priority + name.
    for intent in renames:
        if not intent.old_option_id:
            continue
        for d in canonical_deals:
            if d.deal_id == intent.canonical_id:
                id_to_deal[intent.old_option_id] = d
                break

    canonical_active: list[dict[str, Any]] = []
    canonical_archived: list[dict[str, Any]] = []
    legacy_kept: list[dict[str, Any]] = []
    legacy_dropped: list[dict[str, Any]] = []
    pending_creates: list[dict[str, Any]] = []

    for opt in out:
        opt_id = opt.get("id")
        name = opt.get("name", "")
        if not opt_id:
            pending_creates.append(opt)
            continue
        if opt_id in owned_ids:
            if name.startswith(_ARCHIVED_PREFIX):
                canonical_archived.append(opt)
            else:
                canonical_active.append(opt)
        else:
            if legacy_options_with_tags is None or opt_id in legacy_options_with_tags:
                legacy_kept.append(opt)
            else:
                legacy_dropped.append(opt)

    def _sort_key(o: dict[str, Any]) -> tuple[int, str]:
        deal = id_to_deal.get(o["id"])
        if deal:
            return deal.sort_key
        return (99, (o.get("name") or "").lower())

    canonical_active.sort(key=_sort_key)

    bottom = canonical_archived + legacy_kept
    bottom.sort(key=lambda o: (
        1 if (o.get("name") or "").startswith(_ARCHIVED_PREFIX) else 0,
        (o.get("name") or "").lower(),
    ))

    new_out = canonical_active + pending_creates + bottom
    return new_out, len(legacy_dropped)


def _plan_member_db_update(
    canonical_deals: list[_CanonicalDeal],
    mappings_for_member: dict[str, _Mapping],
    current_options: list[dict[str, Any]],
    member_db_id: str,
    skip_deal_ids: set[str] | None = None,
    legacy_options_with_tags: set[str] | None = None,
) -> _PlannerResult:
    """Compute the desired ``External Org`` options array. Pure — no I/O.

    Three passes:
      1. Iterate canonical deals (in-filter): CASE A (mapping hit) / CASE B
         (stale mapping) / CASE C (bootstrap-adopt by sanitized name) / CASE D
         (bootstrap-create).
      2. Iterate remaining mappings (deals no longer in canonical → stage
         transition OUT of the filter): rename option to ``(archived) X``.
      3. Finalize: drop legacy options whose id is NOT in
         ``legacy_options_with_tags`` (i.e. options no meeting has ever been
         tagged on); reorder so canonical-active options sit at the top
         (stage priority + alpha) and surviving legacy + archived options
         sink to the bottom.

    When ``legacy_options_with_tags`` is ``None`` (tag-check unavailable),
    no legacy options are dropped — they're still sent to the bottom though.
    """
    result = _PlannerResult()
    skip = skip_deal_ids or set()

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

    handled_deal_ids: set[str] = set()

    # ---- Pass 1: canonical deals ----
    for deal in canonical_deals:
        if deal.deal_id in skip:
            continue
        handled_deal_ids.add(deal.deal_id)

        desired_sanitized = _sanitize_option_name(deal.name)
        desired_color = deal.color
        mapping = mappings_for_member.get(deal.deal_id)

        # CASE A — mapping hit + option present.
        if mapping and mapping.option_id in by_id:
            idx = out_idx_by_id[mapping.option_id]
            current_opt = out[idx]
            current_name = current_opt.get("name", "")
            current_color = current_opt.get("color") or "default"
            current_is_archived = current_name.startswith(_ARCHIVED_PREFIX)

            name_changed = current_name != desired_sanitized
            color_changed = current_color != desired_color

            if not name_changed and not color_changed:
                result.mapping_writes.append(_Mapping(
                    deal_id=deal.deal_id,
                    member_db_id=member_db_id,
                    option_id=mapping.option_id,
                    option_name=desired_sanitized,
                ))
                continue

            if not name_changed:
                # Color-only change → keep current PATCH path.
                out[idx] = {
                    **current_opt,
                    "name": desired_sanitized,
                    "color": desired_color,
                }
                result.renamed += 1
                result.mapping_writes.append(_Mapping(
                    deal_id=deal.deal_id,
                    member_db_id=member_db_id,
                    option_id=mapping.option_id,
                    option_name=desired_sanitized,
                ))
                continue

            # Name change → saga (covers un-archive: (archived) X → X).
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
                canonical_id=deal.deal_id,
                annotation=(
                    f"deal={deal.deal_id[:8]}"
                    + (" (resume)" if is_resume else "")
                ),
            ))
            result.renamed += 1
            if current_is_archived:
                result.details.append(
                    f"member={member_db_id} deal={deal.deal_id[:8]} "
                    "un-archived (came back into filter)",
                )

            if is_resume:
                dropped_old_ids.add(mapping.option_id)
                new_idx = out_idx_by_id[existing_new["id"]]
                out[new_idx] = {
                    **out[new_idx],
                    "name": desired_sanitized,
                    "color": desired_color,
                }
                result.mapping_writes.append(_Mapping(
                    deal_id=deal.deal_id,
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
                    deal_id=deal.deal_id,
                    member_db_id=member_db_id,
                    option_id="",  # back-filled from saga
                    option_name=desired_sanitized,
                ))
            continue

        # CASE B — stale mapping (option_id no longer in Notion).
        if mapping and mapping.option_id not in by_id:
            result.details.append(
                f"member={member_db_id} deal={deal.deal_id[:8]} "
                f"mapped option_id={mapping.option_id[:8]} not in current "
                "options — falling back to bootstrap",
            )

        # CASE C — bootstrap-adopt by sanitized name.
        adopted = by_sanitized_name.get(desired_sanitized)
        if adopted is not None and adopted.get("id"):
            adopt_id = adopted["id"]
            idx = out_idx_by_id[adopt_id]
            current_name = adopted.get("name", "")
            current_color = adopted.get("color") or "default"
            name_changed = current_name != desired_sanitized
            color_changed = current_color != desired_color
            if name_changed:
                # Saga rename (name + maybe color delta).
                result.renames.append(RenameIntent(
                    old_option_id=adopt_id,
                    old_name=current_name,
                    desired_name=desired_sanitized,
                    desired_color=desired_color,
                    canonical_id=deal.deal_id,
                    annotation=f"deal={deal.deal_id[:8]} (adopt+rename)",
                ))
                out[idx] = {
                    **adopted,
                    "name": desired_sanitized,
                    "color": desired_color,
                }
                result.renamed += 1
                result.details.append(
                    f"member={member_db_id} deal={deal.deal_id[:8]} "
                    f"adopted existing option by sanitized-name match "
                    f"(saga rename {current_name!r} → {desired_sanitized!r}, "
                    f"color {current_color}→{desired_color})",
                )
                result.mapping_writes.append(_Mapping(
                    deal_id=deal.deal_id,
                    member_db_id=member_db_id,
                    option_id="",  # back-filled from saga
                    option_name=desired_sanitized,
                ))
            elif color_changed:
                out[idx] = {
                    **adopted,
                    "name": desired_sanitized,
                    "color": desired_color,
                }
                result.renamed += 1
                result.details.append(
                    f"member={member_db_id} deal={deal.deal_id[:8]} "
                    f"adopted existing option (color only "
                    f"{current_color}→{desired_color})",
                )
                result.mapping_writes.append(_Mapping(
                    deal_id=deal.deal_id,
                    member_db_id=member_db_id,
                    option_id=adopt_id,
                    option_name=desired_sanitized,
                ))
            else:
                result.details.append(
                    f"member={member_db_id} deal={deal.deal_id[:8]} "
                    "adopted existing option by sanitized-name match",
                )
                result.mapping_writes.append(_Mapping(
                    deal_id=deal.deal_id,
                    member_db_id=member_db_id,
                    option_id=adopt_id,
                    option_name=desired_sanitized,
                ))
            continue

        # CASE D — bootstrap-create.
        out.append({"name": desired_sanitized, "color": desired_color})
        new_idx = len(out) - 1
        pending_creates.append((new_idx, deal.deal_id))
        result.created += 1
        result.mapping_writes.append(_Mapping(
            deal_id=deal.deal_id,
            member_db_id=member_db_id,
            option_id="",
            option_name=desired_sanitized,
        ))

    # ---- Pass 2: archive deals that fell out of the filter ----
    for deal_id, mapping in mappings_for_member.items():
        if deal_id in handled_deal_ids:
            continue
        if mapping.option_id not in by_id:
            # Option already gone — skip the rename. Mapping kept as-is.
            continue
        idx = out_idx_by_id[mapping.option_id]
        current_opt = out[idx]
        current_name = current_opt.get("name", "")
        if current_name.startswith(_ARCHIVED_PREFIX):
            continue  # already archived
        desired_name = _ARCHIVED_PREFIX + current_name
        desired_color = current_opt.get("color") or "default"

        # Archive is a name change → saga (preserves the option's existing
        # color; only the name flips to (archived) X).
        result.renames.append(RenameIntent(
            old_option_id=mapping.option_id,
            old_name=current_name,
            desired_name=desired_name,
            desired_color=desired_color,
            canonical_id=deal_id,
            annotation=f"deal={deal_id[:8]} (stage-out archive)",
        ))
        out[idx] = {**current_opt, "name": desired_name}
        result.renamed += 1
        result.archived += 1
        result.mapping_writes.append(_Mapping(
            deal_id=deal_id,
            member_db_id=member_db_id,
            option_id="",  # back-filled from saga
            option_name=_sanitize_option_name(desired_name),
        ))
        result.details.append(
            f"member={member_db_id} deal={deal_id[:8]} "
            "stage moved out of filter — archived (saga)",
        )

    if dropped_old_ids:
        out = [o for o in out if not o.get("id") or o["id"] not in dropped_old_ids]
        out_idx_by_id = {
            opt["id"]: i for i, opt in enumerate(out) if opt.get("id")
        }

    # Pass 3: finalize — drop tag-less legacy options, reorder by canonical
    # priority. Skipped when legacy_options_with_tags is None.
    out, dropped = _finalize_out(
        out=out,
        mapping_writes=result.mapping_writes,
        renames=result.renames,
        canonical_deals=canonical_deals,
        legacy_options_with_tags=legacy_options_with_tags,
    )
    if dropped:
        result.details.append(
            f"member={member_db_id} dropped {dropped} legacy option(s) with "
            "no tagged meetings",
        )

    result.changed = not _options_equal(out, current_options)
    result.new_options = out
    result._pending_creates = pending_creates  # type: ignore[attr-defined]
    result._dropped_old_ids = dropped_old_ids  # type: ignore[attr-defined]
    result._dropped_legacy_count = dropped  # type: ignore[attr-defined]
    return result


# ---------------------------------------------------------------------------
# Mapping back-fill helpers
# ---------------------------------------------------------------------------


def _back_fill_mapping_ids(
    plan: _PlannerResult,
    patched_options: list[dict[str, Any]],
) -> list[_Mapping]:
    """Replace placeholder option_ids from the PATCH response.

    Saga-rename placeholders are filled separately by
    ``_apply_saga_results_to_mappings``; only pure-create placeholders remain
    by the time we get here.
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
                f"member={m.member_db_id} deal={m.deal_id[:8]} "
                f"created option {m.option_name!r} but Notion response had "
                "no matching id — mapping skipped (next run will adopt)",
            )
            continue
        resolved.append(_Mapping(
            deal_id=m.deal_id,
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

    Saga results are keyed by canonical row id (``deal_id``).
    """
    if not saga_results:
        return
    for m in plan.mapping_writes:
        if m.option_id:
            continue
        new_id = saga_results.get(m.deal_id)
        if new_id:
            m.option_id = new_id


def _upsert_mappings(mappings: list[_Mapping]) -> None:
    if not mappings:
        return
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    body = [
        {
            "deal_id": m.deal_id,
            "member_db_id": m.member_db_id,
            "option_id": m.option_id,
            "option_name": m.option_name,
            "last_synced_at": now_iso,
        }
        for m in mappings
    ]
    _http(
        "POST",
        "/rest/v1/external_org_option_mappings?on_conflict=deal_id,member_db_id",
        body=body,
        prefer="resolution=merge-duplicates,return=minimal",
    )


# ---------------------------------------------------------------------------
# Tag check (Notion) — which legacy options actually have meetings on them
# ---------------------------------------------------------------------------


def _legacy_options_with_tags(
    *,
    client: NotionClientWrapper,
    member_db_id: str,
    current_options: list[dict[str, Any]],
    canonical_deals: list[_CanonicalDeal],
    mappings_for_member: dict[str, _Mapping],
    report: SyncReport,
    owner_label: str,
) -> set[str] | None:
    """Query Notion for option names actually in use, build the keep-set.

    Returns the set of legacy option_ids that have ≥1 meeting tagged on them
    (those get sent to the bottom, but kept). Options NOT in the returned
    set are dropped by the planner.

    Returns ``None`` on Notion API failure — the planner treats ``None`` as
    "skip cleanup", so every legacy option is kept (still moved to bottom).
    This is a safety choice: a transient tag-check failure must never cause
    bulk option deletion.

    Performance: one paginated query per member, filtered to pages where
    ``External Org`` is non-empty. Cost is bounded by tagged-page count.
    """
    # Identify legacy option ids: present in Notion, not canonical-owned, not
    # going to be adopted by sanitized-name match on this tick.
    canonical_sanitized = {_sanitize_option_name(d.name) for d in canonical_deals}
    canonical_owned_ids = {
        m.option_id for m in mappings_for_member.values() if m.option_id
    }
    legacy_ids_to_check: set[str] = set()
    for opt in current_options:
        opt_id = opt.get("id")
        opt_name = opt.get("name", "")
        if not opt_id or not opt_name:
            continue
        if opt_id in canonical_owned_ids:
            continue
        if _sanitize_option_name(opt_name) in canonical_sanitized:
            continue  # bootstrap-adopt will claim this
        legacy_ids_to_check.add(opt_id)

    if not legacy_ids_to_check:
        return set()  # Nothing legacy → empty keep-set is fine

    # Pull every page with External Org set, project the option NAME.
    try:
        response = client.query_database(
            database_id=member_db_id,
            filter={
                "property": _EXTERNAL_ORG_PROPERTY,
                "select": {"is_not_empty": True},
            },
        )
    except Exception as e:  # noqa: BLE001
        report.details.append(
            f"{owner_label}: tag-check query failed ({type(e).__name__}: {e}) "
            "— legacy options kept defensively",
        )
        logger.warning(
            "external_org_applier_sync: %s tag-check query failed: %s",
            owner_label, e,
        )
        return None

    tagged_names: set[str] = set()
    for page in response.get("results", []):
        sel = (page.get("properties") or {}).get(
            _EXTERNAL_ORG_PROPERTY, {},
        ).get("select")
        if sel and sel.get("name"):
            tagged_names.add(sel["name"])

    # Translate tagged option NAMES → option_ids in our legacy candidate set.
    with_tags: set[str] = set()
    for opt in current_options:
        opt_id = opt.get("id")
        if opt_id in legacy_ids_to_check and opt.get("name") in tagged_names:
            with_tags.add(opt_id)
    return with_tags


# ---------------------------------------------------------------------------
# I/O sync
# ---------------------------------------------------------------------------


def sync(client: NotionClientWrapper, config: SyncConfig) -> SyncReport:
    """Apply ReportingNz_deals (filtered by stage) → member-DB `External Org`."""
    report = SyncReport(name=SUB_SYNC_NAME)

    if not config.org_chart_db_id:
        report.errors += 1
        report.details.append(
            "ORG_CHART_DB_ID not configured — cannot enumerate member DBs",
        )
        logger.warning(
            "external_org_applier_sync: ORG_CHART_DB_ID not configured — skipping",
        )
        return report

    try:
        _supabase_creds()
    except RuntimeError as e:
        report.errors += 1
        report.details.append(f"Supabase not configured: {e}")
        logger.warning("external_org_applier_sync: %s", e)
        return report

    # ----- Canonical deals -----
    try:
        canonical_deals = _load_canonical_deals()
    except Exception as e:  # noqa: BLE001
        report.errors += 1
        report.details.append(
            f"ReportingNz_deals fetch failed: {type(e).__name__}: {e}",
        )
        logger.exception(
            "external_org_applier_sync: ReportingNz_deals fetch failed",
        )
        return report

    if not canonical_deals:
        logger.warning(
            "external_org_applier_sync: no deals in any allowed stage — "
            "skipping with no writes",
        )
        report.details.append(
            "no deals in allowed stages — nothing to apply",
        )
        return report

    # ----- Collision detection (sanitized name across deals) -----
    by_sanitized: dict[str, list[str]] = {}
    for deal in canonical_deals:
        by_sanitized.setdefault(_sanitize_option_name(deal.name), []).append(
            deal.deal_id,
        )
    skip_deal_ids: set[str] = set()
    for sanitized, ids in by_sanitized.items():
        if len(ids) > 1:
            report.errors += 1
            report.details.append(
                f"sanitized-name collision {sanitized!r} on deals "
                f"{[i[:8] for i in ids]} — skipping all of them",
            )
            skip_deal_ids.update(ids)
            logger.error(
                "external_org_applier_sync: sanitized-name collision %r on %s",
                sanitized, [i[:8] for i in ids],
            )

    # ----- Discover member DBs -----
    try:
        member_dbs = discover_meeting_dbs(client, config.org_chart_db_id)
    except Exception as e:  # noqa: BLE001
        report.errors += 1
        report.details.append(
            f"member DB discovery failed: {type(e).__name__}: {e}",
        )
        logger.exception(
            "external_org_applier_sync: member DB discovery failed",
        )
        return report

    try:
        mappings_by_pair = _load_mappings([m.db_id for m in member_dbs])
    except Exception as e:  # noqa: BLE001
        report.errors += 1
        report.details.append(
            f"mapping load failed: {type(e).__name__}: {e}",
        )
        logger.exception("external_org_applier_sync: mapping load failed")
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
                "external_org_applier_sync: retrieve_data_source failed for "
                "%s (%s)", owner_label, member_db.db_id,
            )
            continue

        props = (ds.get("properties") or {})
        ext_prop = props.get(_EXTERNAL_ORG_PROPERTY)
        if not ext_prop or ext_prop.get("type") != "select":
            report.errors += 1
            msg = (
                f"{owner_label}: no '{_EXTERNAL_ORG_PROPERTY}' select property"
            )
            report.details.append(msg)
            logger.warning("external_org_applier_sync: %s", msg)
            continue

        current_options = (
            ext_prop.get("select", {}).get("options", []) or []
        )
        mappings_for_member = {
            deal_id: m
            for (deal_id, mdb_id), m in mappings_by_pair.items()
            if mdb_id == member_db.db_id
        }

        # Tag-check: which legacy options have at least one meeting tagged on
        # them? Lets the planner safely drop the ones nobody has ever used.
        # On failure → set is None and the planner keeps every legacy option
        # (still moved to the bottom). One paginated Notion query per member;
        # results limited to pages where External Org is non-empty.
        legacy_options_with_tags = _legacy_options_with_tags(
            client=client,
            member_db_id=member_db.db_id,
            current_options=current_options,
            canonical_deals=canonical_deals,
            mappings_for_member=mappings_for_member,
            report=report,
            owner_label=owner_label,
        )

        plan = _plan_member_db_update(
            canonical_deals=canonical_deals,
            mappings_for_member=mappings_for_member,
            current_options=current_options,
            member_db_id=member_db.db_id,
            skip_deal_ids=skip_deal_ids,
            legacy_options_with_tags=legacy_options_with_tags,
        )

        # mapping_writes can be non-empty even when the options array is
        # unchanged — bootstrap-adopt by sanitized name (or a CASE A no-op
        # hit) records the (deal_id → option_id) pin without touching the
        # schema. Skipping here would leave those mappings unwritten, and
        # without a mapping the stage-out archive pass (which iterates
        # mappings_for_member) can never find the option to archive. Only
        # skip when nothing at all is pending — including pure mapping writes.
        if not plan.changed and not plan.renames and not plan.mapping_writes:
            logger.debug(
                "external_org_applier_sync: %s already in sync (%d options)",
                owner_label, len(current_options),
            )
            continue

        if config.dry_run:
            logger.info(
                "external_org_applier_sync: DRY RUN %s would create=%d "
                "rename=%d archived=%d",
                owner_label, plan.created, plan.renamed, plan.archived,
            )
            report.created += plan.created
            report.renamed += plan.renamed
            report.archived += plan.archived
            report.details.append(
                f"{owner_label}: would create={plan.created} "
                f"rename={plan.renamed} archived={plan.archived} (dry-run)",
            )
            continue

        # ----- Live: run rename sagas (incl. stage-out archives) first -----
        current_state = list(current_options)
        saga_results: dict[str, str] = {}
        for intent in plan.renames:
            try:
                new_id, current_state, saga_details = execute_rename_saga(
                    client=client,
                    member_db_id=member_db.db_id,
                    property_name=_EXTERNAL_ORG_PROPERTY,
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
                    "external_org_applier_sync: saga failed for "
                    "%s (%s) %s → %s",
                    owner_label, member_db.db_id,
                    intent.old_name, intent.desired_name,
                )
                continue
            saga_results[intent.canonical_id] = new_id
            for d in saga_details:
                report.details.append(f"{owner_label}: {d}")

        _apply_saga_results_to_mappings(plan, saga_results)

        # ----- Live: final PATCH (creates / legacy drops / reorder) -----
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
                        _EXTERNAL_ORG_PROPERTY: {
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
                    "external_org_applier_sync: final PATCH failed for "
                    "%s (%s)", owner_label, member_db.db_id,
                )
            else:
                patched_options = (
                    ((patch_response or {}).get("properties", {})
                     .get(_EXTERNAL_ORG_PROPERTY, {})
                     .get("select", {})
                     .get("options")) or []
                )
                if not patched_options:
                    try:
                        ds_after = client.retrieve_data_source(member_db.db_id)
                        patched_options = (
                            ((ds_after.get("properties") or {})
                             .get(_EXTERNAL_ORG_PROPERTY, {})
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
                "external_org_applier_sync: mapping back-fill failed for "
                "%s (%s)", owner_label, member_db.db_id,
            )

        dropped = getattr(plan, "_dropped_legacy_count", 0)
        report.created += plan.created
        report.renamed += plan.renamed
        report.archived += plan.archived
        report.deleted += dropped
        for d in plan.details:
            report.details.append(d)
        logger.info(
            "external_org_applier_sync: %s created=%d renamed=%d archived=%d "
            "deleted=%d (legacy with no tagged meetings)",
            owner_label, plan.created, plan.renamed, plan.archived, dropped,
        )

    if len(report.details) > _DETAIL_CAP:
        truncated = len(report.details) - _DETAIL_CAP
        report.details = report.details[:_DETAIL_CAP]
        report.details.append(f"… (+{truncated} more details truncated)")

    return report


__all__ = ["SUB_SYNC_NAME", "sync"]
