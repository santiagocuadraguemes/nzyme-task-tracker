"""Shared 5-step saga that achieves a *logical* rename of a Notion
select / multi-select option.

Background
----------
Notion's ``data_sources.update`` silently no-ops renames of existing select
option attributes — verified 2026-05-21 via
``scripts/diag_work_area_options.py`` against four PATCH variants
(``data_sources.update`` with and without ``color``; legacy
``databases.update`` with and without ``color``). The PATCH returns 200 and
the response echoes the new state, but a fresh retrieve shows the OLD name.

What DOES work via PATCH:
  * Adding new options (omit ``id``; Notion assigns one in the response).
  * Removing options (omit them from the array; Notion drops them).
  * ``pages.update`` to change a page's property value (used by the
    Tracker applier already and works correctly).

Saga
----
To rename option X (id ``opt-x``, name ``X``) to ``Y``:

  1. **PATCH 1** — send the full options array with ``opt-x`` preserved AND a
     new entry appended (no id, just ``name=Y`` + optional ``color``). Notion
     assigns it a fresh id; we read it from the response.
  2. **Query tagged pages** in the member DB:
     * ``select`` → ``{property, select: {equals: X}}``
     * ``multi_select`` → ``{property, multi_select: {contains: X}}``
  3. **Migrate each page** via ``pages.update``:
     * ``select`` → ``{property: {select: {id: new_id}}}``
     * ``multi_select`` → take the page's current array, drop entries where
       ``name == X`` (defensively also matches by id), drop any pre-existing
       ``new_id`` entry, append ``{id: new_id}``, write back. Preserves every
       other tag on the page.
  4. **PATCH 2** — send the options array MINUS ``opt-x``. Notion drops it.
  5. Caller back-fills the per-property mapping with ``new_id``.

Idempotency / mid-saga resume
-----------------------------
The saga is restartable from any failure point. The resume signal is
"an option with ``desired_name`` already exists in ``current_state`` whose id
is not ``old_option_id``" — that means PATCH 1 has already run on a prior
tick. The saga uses the pre-existing new id and skips PATCH 1.

Hard failures (PATCH 1 / page query / any page migration / PATCH 2) raise
``RuntimeError``. The per-applier I/O loop catches and records
``report.errors += 1`` with a detail line — next tick resumes from whatever
state Notion is in.

Scope
-----
Only **name** changes trigger the saga. Color-only changes (Detail /
External Org carry canonical-driven color) continue through the existing
single-PATCH path. The diag only proved name renames are silently no-op'd;
running the saga for a color-only change would churn the option id needlessly.
If color PATCHes turn out to be broken too, the fix is a one-line extension
in each applier (emit the rename intent when ``name_changed OR color_changed``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)

_SUPPORTED_TYPES = frozenset({"select", "multi_select"})


@dataclass
class RenameIntent:
    """One logical rename to execute on one member DB.

    The ``canonical_id`` field is the per-applier canonical key
    (hierarchy_page_id / detail_notion_page_id / deal_id) — opaque to the
    saga, used by the caller to associate the saga's returned ``new_option_id``
    with the right mapping-write row.
    """

    old_option_id: str
    old_name: str
    desired_name: str
    desired_color: str | None = None  # None → omit from PATCH 1
    canonical_id: str = ""             # echoed back, not consumed by saga
    annotation: str = ""               # human label embedded in detail lines


@dataclass
class DropIntent:
    """One option to remove from one member DB.

    Used when a canonical row is tombstoned (``deleted_at IS NOT NULL``) —
    the option is dropped from the member DB entirely rather than archived
    (``(archived) X``). The drop saga clears every tagged page's property
    before dropping the option so no page is left with a dangling reference.
    """

    old_option_id: str
    old_name: str
    canonical_id: str = ""
    annotation: str = ""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def execute_rename_saga(
    *,
    client: NotionClientWrapper,
    member_db_id: str,
    property_name: str,
    property_type: str,
    intent: RenameIntent,
    current_state: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Execute the 5-step saga for one logical rename on one member DB.

    Args:
        client: Notion wrapper (rate-limited).
        member_db_id: Meeting Notes DB whose option list we're modifying.
        property_name: Notion property (e.g. ``Macro Work Block`` / ``Detail`` /
            ``External Org``).
        property_type: ``"select"`` or ``"multi_select"``.
        intent: The rename to execute.
        current_state: Full options array on the member DB at saga start.
            For chained sagas, pass the previous saga's ``post_saga_state``.

    Returns:
        new_option_id: Id of the option carrying ``desired_name`` after the
            saga completes (may be a pre-existing id when resume is detected).
        post_saga_state: Full options array after PATCH 2 — i.e.
            ``current_state`` minus the old option, with the new option
            appended. Chain into the next saga in the same tick.
        detail_lines: Human-readable log lines for the SyncReport.
    """
    if property_type not in _SUPPORTED_TYPES:
        raise ValueError(
            f"execute_rename_saga: unsupported property_type {property_type!r}",
        )

    details: list[str] = []
    label = _label(member_db_id, intent)

    # ---- Step 1: PATCH 1 (add new option) — unless we're resuming ----
    new_option_id = _resolve_or_create_new_option(
        client=client,
        member_db_id=member_db_id,
        property_name=property_name,
        property_type=property_type,
        intent=intent,
        current_state=current_state,
        details=details,
        label=label,
    )

    # ---- Step 2: query tagged pages ----
    try:
        query_response = client.query_database(
            database_id=member_db_id,
            filter=_tagged_pages_filter(
                property_name, property_type, intent.old_name,
            ),
        )
    except Exception as e:
        raise RuntimeError(
            f"{label}: saga step 2 (query pages tagged {intent.old_name!r} "
            f"on {property_name}) failed: {type(e).__name__}: {e}",
        ) from e
    pages = query_response.get("results", []) or []

    # ---- Step 3: migrate each page ----
    if pages:
        details.append(
            f"{label}: migrating {len(pages)} page(s) "
            f"from {intent.old_name!r} → {intent.desired_name!r}",
        )
        for page in pages:
            _migrate_page(
                client=client,
                page=page,
                property_name=property_name,
                property_type=property_type,
                old_option_id=intent.old_option_id,
                old_name=intent.old_name,
                new_option_id=new_option_id,
                label=label,
            )
    else:
        details.append(
            f"{label}: no pages tagged on {intent.old_name!r} — "
            "proceeding to drop",
        )

    # ---- Step 4: PATCH 2 (drop old option) ----
    post_saga_state = _post_saga_state(
        current_state=current_state,
        intent=intent,
        new_option_id=new_option_id,
    )
    try:
        client.update_data_source(
            member_db_id,
            {
                property_name: {
                    property_type: {
                        "options": _options_for_patch(post_saga_state),
                    },
                },
            },
        )
    except Exception as e:
        raise RuntimeError(
            f"{label}: saga step 4 (PATCH 2 drop old option "
            f"{intent.old_name!r}) failed: {type(e).__name__}: {e}",
        ) from e

    details.append(
        f"{label}: saga complete (old_id={_short(intent.old_option_id)} → "
        f"new_id={_short(new_option_id)}, name={intent.desired_name!r})",
    )
    return new_option_id, post_saga_state, details


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _label(member_db_id: str, intent: RenameIntent) -> str:
    parts = [f"member={member_db_id}"]
    if intent.annotation:
        parts.append(intent.annotation)
    return " ".join(parts)


def _short(opt_id: str) -> str:
    """Truncate an opaque Notion option id for detail-line readability."""
    return opt_id[:8] if opt_id else ""


def _resolve_or_create_new_option(
    *,
    client: NotionClientWrapper,
    member_db_id: str,
    property_name: str,
    property_type: str,
    intent: RenameIntent,
    current_state: list[dict[str, Any]],
    details: list[str],
    label: str,
) -> str:
    """Return the id of the option that will carry ``desired_name``.

    Resume detection: if ``current_state`` already contains an option named
    ``desired_name`` whose id is not ``old_option_id``, PATCH 1 has already
    run on a prior tick. Reuse that id and skip PATCH 1.

    Otherwise, run PATCH 1 (append a new option entry with no id) and read
    the assigned id from the response.
    """
    # Resume path
    for opt in current_state:
        if opt.get("name") == intent.desired_name and opt.get("id") and opt[
            "id"] != intent.old_option_id:
            details.append(
                f"{label}: resume detected — reusing existing "
                f"new_id={_short(opt['id'])} for {intent.desired_name!r} "
                "(PATCH 1 skipped)",
            )
            return opt["id"]

    # PATCH 1 — append a new option
    patch1_array = list(_options_for_patch(current_state))
    new_entry: dict[str, Any] = {"name": intent.desired_name}
    if intent.desired_color:
        new_entry["color"] = intent.desired_color
    patch1_array.append(new_entry)
    try:
        response = client.update_data_source(
            member_db_id,
            {
                property_name: {
                    property_type: {"options": patch1_array},
                },
            },
        )
    except Exception as e:
        raise RuntimeError(
            f"{label}: saga step 1 (PATCH 1 add new option "
            f"{intent.desired_name!r}) failed: {type(e).__name__}: {e}",
        ) from e

    new_id = _extract_new_option_id(
        response=response,
        property_name=property_name,
        property_type=property_type,
        desired_name=intent.desired_name,
        pre_existing_ids={opt.get("id") for opt in current_state if opt.get("id")},
    )
    if new_id is None:
        # Fall back to a fresh retrieve in case the PATCH response shape was
        # malformed or truncated.
        try:
            refreshed = client.retrieve_data_source(member_db_id)
        except Exception as e:
            raise RuntimeError(
                f"{label}: saga step 1 returned no parseable new option id "
                f"and re-fetch failed: {type(e).__name__}: {e}",
            ) from e
        new_id = _extract_new_option_id(
            response=refreshed,
            property_name=property_name,
            property_type=property_type,
            desired_name=intent.desired_name,
            pre_existing_ids={
                opt.get("id") for opt in current_state if opt.get("id")
            },
        )
        if new_id is None:
            raise RuntimeError(
                f"{label}: saga step 1 succeeded but no option named "
                f"{intent.desired_name!r} appeared in the response or "
                "subsequent retrieve — cannot proceed",
            )
        details.append(
            f"{label}: PATCH 1 response missing new option id — "
            f"recovered via re-fetch (new_id={_short(new_id)})",
        )
    return new_id


def _extract_new_option_id(
    *,
    response: dict[str, Any],
    property_name: str,
    property_type: str,
    desired_name: str,
    pre_existing_ids: set[str],
) -> str | None:
    """Find the freshly-assigned option id in a PATCH/retrieve response.

    The "new" entry is the one whose name matches ``desired_name`` AND whose
    id was NOT present in ``current_state`` before PATCH 1.
    """
    options = (
        ((response or {}).get("properties") or {})
        .get(property_name, {})
        .get(property_type, {})
        .get("options")
    ) or []
    for opt in options:
        opt_id = opt.get("id")
        if not opt_id:
            continue
        if opt.get("name") == desired_name and opt_id not in pre_existing_ids:
            return opt_id
    return None


def _tagged_pages_filter(
    property_name: str,
    property_type: str,
    old_name: str,
) -> dict[str, Any]:
    """Build the Notion query filter for pages tagged with ``old_name``."""
    if property_type == "select":
        return {"property": property_name, "select": {"equals": old_name}}
    # multi_select
    return {"property": property_name, "multi_select": {"contains": old_name}}


def _migrate_page(
    *,
    client: NotionClientWrapper,
    page: dict[str, Any],
    property_name: str,
    property_type: str,
    old_option_id: str,
    old_name: str,
    new_option_id: str,
    label: str,
) -> None:
    """Move ``page`` from the old option to the new option for ``property_name``.

    Wraps the failure in a ``RuntimeError`` carrying enough context for the
    SyncReport detail line; the caller propagates as ``report.errors += 1``.
    """
    page_id = page.get("id")
    if not page_id:
        raise RuntimeError(
            f"{label}: saga step 3 page in query response has no id: {page!r}",
        )

    if property_type == "select":
        new_properties = {property_name: {"select": {"id": new_option_id}}}
    else:
        # multi_select — preserve every other tag on the page.
        current_array = (
            (page.get("properties") or {})
            .get(property_name, {})
            .get("multi_select")
        ) or []
        rebuilt: list[dict[str, Any]] = []
        for entry in current_array:
            entry_id = entry.get("id")
            entry_name = entry.get("name")
            # Drop the old option (match by id and name defensively).
            if entry_id == old_option_id or entry_name == old_name:
                continue
            # Drop any pre-existing new-id entry to avoid duplicates after
            # the append below.
            if entry_id == new_option_id:
                continue
            # Preserve other tags by id (smallest payload Notion needs).
            if entry_id:
                rebuilt.append({"id": entry_id})
        rebuilt.append({"id": new_option_id})
        new_properties = {property_name: {"multi_select": rebuilt}}

    try:
        client.update_page(page_id=page_id, properties=new_properties)
    except Exception as e:
        raise RuntimeError(
            f"{label}: saga step 3 (migrate page {page_id} from "
            f"{old_name!r} → new_id={_short(new_option_id)}) failed: "
            f"{type(e).__name__}: {e}",
        ) from e


def _post_saga_state(
    *,
    current_state: list[dict[str, Any]],
    intent: RenameIntent,
    new_option_id: str,
) -> list[dict[str, Any]]:
    """Compute the options array Notion will have after PATCH 2 completes.

    Drops the old option; ensures the new option (with ``new_option_id`` and
    desired attributes) is present exactly once. **Preserves the position**
    of the old option (the new option lands where the old one was) so the
    array stays comparable with the planner's ``new_options`` (which keeps
    the OLD id in place until the I/O layer swaps it). Without this, the
    applier would issue a redundant final PATCH every saga tick just to
    re-sort the array.

    Two scenarios:

    * **Non-resume** (new option not in ``current_state``): replace the OLD
      entry with the new entry at the same position.
    * **Resume** (new option already present): drop the OLD entry; normalize
      the existing new entry to carry the desired name + color.
    """
    new_already_present = any(
        opt.get("id") == new_option_id for opt in current_state
    )

    def _normalized_new(existing: dict[str, Any] | None) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "id": new_option_id,
            "name": intent.desired_name,
        }
        if intent.desired_color:
            entry["color"] = intent.desired_color
        elif existing and existing.get("color"):
            entry["color"] = existing["color"]
        return entry

    out: list[dict[str, Any]] = []
    for opt in current_state:
        opt_id = opt.get("id")
        if opt_id == intent.old_option_id:
            if not new_already_present:
                # Replace OLD with NEW at the same position.
                out.append(_normalized_new(existing=None))
            # Else (resume): drop the old; the existing new entry will land
            # at its own position later in this loop.
            continue
        if opt_id == new_option_id:
            out.append(_normalized_new(existing=opt))
            continue
        out.append(dict(opt))
    return out


def _options_for_patch(state: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project the options array into the minimal shape PATCH needs.

    Notion matches existing entries by ``id``; we pass ``name`` + ``color``
    along for readability of the request body, even though Notion ignores
    them on rename. Entries without ``id`` (intentional creates) pass through
    untouched.
    """
    out: list[dict[str, Any]] = []
    for opt in state:
        entry: dict[str, Any] = {}
        if opt.get("id"):
            entry["id"] = opt["id"]
        if opt.get("name") is not None:
            entry["name"] = opt["name"]
        if opt.get("color"):
            entry["color"] = opt["color"]
        out.append(entry)
    return out


def execute_drop_saga(
    *,
    client: NotionClientWrapper,
    member_db_id: str,
    property_name: str,
    property_type: str,
    intent: DropIntent,
    current_state: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Remove one option from a member DB.

    Steps:

      1. Query pages tagged on ``intent.old_name``.
      2. Clear each page's property (select → ``None``; multi_select →
         remove the entry while preserving every other tag).
      3. PATCH the data source with ``current_state`` minus the dropped
         option.

    Notion *does* cascade-clear tags when an option is removed from the
    schema, but doing it explicitly here keeps the audit trail intact and
    avoids surprise activity-log entries on every tagged page.

    Returns ``(post_state, detail_lines)``. Raises ``RuntimeError`` on hard
    failure — caller surfaces as ``report.errors += 1``; next tick resumes.
    """
    if property_type not in _SUPPORTED_TYPES:
        raise ValueError(
            f"execute_drop_saga: unsupported property_type {property_type!r}",
        )

    details: list[str] = []
    label = " ".join(
        p for p in (f"member={member_db_id}", intent.annotation) if p
    )

    # Step 1: query tagged pages.
    try:
        query_response = client.query_database(
            database_id=member_db_id,
            filter=_tagged_pages_filter(
                property_name, property_type, intent.old_name,
            ),
        )
    except Exception as e:
        raise RuntimeError(
            f"{label}: drop step 1 (query pages tagged "
            f"{intent.old_name!r} on {property_name}) failed: "
            f"{type(e).__name__}: {e}",
        ) from e
    pages = query_response.get("results", []) or []

    # Step 2: clear each page.
    if pages:
        details.append(
            f"{label}: clearing {property_name} tag from {len(pages)} "
            f"page(s) before dropping option {intent.old_name!r}",
        )
        for page in pages:
            _clear_page_tag(
                client=client,
                page=page,
                property_name=property_name,
                property_type=property_type,
                old_option_id=intent.old_option_id,
                old_name=intent.old_name,
                label=label,
            )
    else:
        details.append(
            f"{label}: no pages tagged on {intent.old_name!r} — "
            "proceeding straight to drop",
        )

    # Step 3: PATCH to drop the option.
    post_state = [
        dict(opt)
        for opt in current_state
        if opt.get("id") != intent.old_option_id
    ]
    try:
        client.update_data_source(
            member_db_id,
            {
                property_name: {
                    property_type: {
                        "options": _options_for_patch(post_state),
                    },
                },
            },
        )
    except Exception as e:
        raise RuntimeError(
            f"{label}: drop step 3 (PATCH drop option "
            f"{intent.old_name!r}) failed: {type(e).__name__}: {e}",
        ) from e

    details.append(
        f"{label}: drop complete "
        f"(old_id={_short(intent.old_option_id)}, name={intent.old_name!r})",
    )
    return post_state, details


def _clear_page_tag(
    *,
    client: NotionClientWrapper,
    page: dict[str, Any],
    property_name: str,
    property_type: str,
    old_option_id: str,
    old_name: str,
    label: str,
) -> None:
    """Clear the tag for ``old_option_id`` from one page.

    select → property set to ``None``.
    multi_select → entry for the old option removed while every other tag
    is preserved.
    """
    page_id = page.get("id")
    if not page_id:
        raise RuntimeError(
            f"{label}: drop saga page in query response has no id: {page!r}",
        )

    if property_type == "select":
        new_properties = {property_name: {"select": None}}
    else:
        current_array = (
            (page.get("properties") or {})
            .get(property_name, {})
            .get("multi_select")
        ) or []
        rebuilt: list[dict[str, Any]] = []
        for entry in current_array:
            entry_id = entry.get("id")
            entry_name = entry.get("name")
            if entry_id == old_option_id or entry_name == old_name:
                continue
            if entry_id:
                rebuilt.append({"id": entry_id})
        new_properties = {property_name: {"multi_select": rebuilt}}

    try:
        client.update_page(page_id=page_id, properties=new_properties)
    except Exception as e:
        raise RuntimeError(
            f"{label}: drop saga step 2 (clear page {page_id} tag for "
            f"{old_name!r}) failed: {type(e).__name__}: {e}",
        ) from e


def materialize_final_options(
    *,
    new_options: list[dict[str, Any]],
    renames: list[RenameIntent],
    saga_results: dict[str, str],
) -> list[dict[str, Any]]:
    """Project a planner's ``new_options`` into the final array Notion holds.

    The planner keeps the OLD option id on entries it intends to rename
    (because at planning time the saga's new id doesn't exist yet). After
    the sagas run, the I/O layer calls this helper to swap each completed
    saga's old id for its new id. Entries the saga didn't touch (creates,
    unchanged, color-only, legacy passthrough) carry through verbatim.

    Saga results are keyed by ``canonical_id`` — the per-applier opaque
    identifier (hierarchy_page_id / detail_notion_page_id / deal_id).
    """
    if not saga_results:
        return [dict(opt) for opt in new_options]

    old_to_new: dict[str, str] = {}
    for intent in renames:
        new_id = saga_results.get(intent.canonical_id)
        if new_id:
            old_to_new[intent.old_option_id] = new_id

    out: list[dict[str, Any]] = []
    for opt in new_options:
        opt_id = opt.get("id")
        if opt_id and opt_id in old_to_new:
            out.append({**opt, "id": old_to_new[opt_id]})
        else:
            out.append(dict(opt))
    return out


__all__ = [
    "DropIntent",
    "RenameIntent",
    "execute_drop_saga",
    "execute_rename_saga",
    "materialize_final_options",
]
