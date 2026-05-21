"""Mirror the Notion Hierarchy DB → Supabase canonical state.

Runs daily 07:00 Madrid as part of the ``hierarchy_sync`` orchestrator.
One-way write: Notion is the editing surface; Supabase is the canonical
source of truth that diff-driven downstream syncs (PR2+) will consume.

Per tick:

  1. Snapshot the Notion Hierarchy DB.
  2. Load every row from ``public.hierarchy_rows`` (including tombstones).
  3. Diff by ``notion_page_id`` (stable across Notion-side renames):
       - created     — row in Notion, not in canonical
       - edited      — row in both; one or more mirrored fields differ
       - reactivated — row in Notion + canonical has ``deleted_at`` set
       - deleted     — row in canonical (live) but missing from Notion
       - unchanged   — row in both; identical fields
  4. Apply: upsert created/edited/reactivated, tombstone deleted, bump
     ``last_seen_at`` on unchanged.
  5. Append one row to ``public.hierarchy_sync_runs`` with counts + a
     structured JSONB change log so "what changed in the Hierarchy DB
     last week" is one SQL query.

Non-goals (handled in later PRs):
- Propagating changes to member-DB ``Work area`` options or Tracker
  ``[DETAILS INSIDE]`` rows. ``macro_block_sync`` still owns the Work-area
  propagation by its own snapshot-vs-current diffing; that's untouched in
  this PR.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from src.config import SyncConfig
from src.hierarchy.base import SyncReport
from src.notion_client_wrapper import NotionClientWrapper

logger = logging.getLogger(__name__)

SUB_SYNC_NAME = "canonical_mirror_sync"

# Fields mirrored from Notion → Supabase. Order is preserved when building
# change-log JSON for readability; equality is set-based.
_MIRRORED_FIELDS = (
    "name",
    "tier",
    "active",
    "parent_notion_page_id",
    "tracker_node_page_id",
    "notes",
)

# Cap the JSONB change log on the audit row so a bootstrap run that flags
# every row as 'created' doesn't bloat the row to absurd sizes. The full
# event set is still visible in CloudWatch logs via the structured details.
_CHANGES_JSON_CAP = 500


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class _NotionRow:
    """Normalized snapshot of one Hierarchy DB row."""

    notion_page_id: str
    name: str
    tier: str
    active: bool
    parent_notion_page_id: str | None
    tracker_node_page_id: str | None
    notes: str

    def as_record(self) -> dict[str, Any]:
        """Mirrored-field dict — what gets compared and what gets upserted."""
        return {f: getattr(self, f) for f in _MIRRORED_FIELDS} | {
            "notion_page_id": self.notion_page_id,
        }


Op = Literal["created", "edited", "deleted", "reactivated", "unchanged"]


@dataclass
class _Change:
    notion_page_id: str
    op: Op
    before: dict[str, Any] | None = None  # canonical state (None for created)
    after: dict[str, Any] | None = None   # Notion state (None for deleted)
    field_diff: dict[str, dict[str, Any]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Notion snapshot loader
# ---------------------------------------------------------------------------


def _read_title(prop: dict[str, Any]) -> str:
    items = prop.get("title") or []
    return "".join(rt.get("plain_text", "") for rt in items).strip()


def _read_select_name(prop: dict[str, Any] | None) -> str:
    sel = (prop or {}).get("select")
    return (sel or {}).get("name", "") or ""


def _read_relation_first(prop: dict[str, Any] | None) -> str | None:
    rels = (prop or {}).get("relation") or []
    return rels[0]["id"] if rels else None


def _read_rich_text(prop: dict[str, Any] | None) -> str:
    items = (prop or {}).get("rich_text") or []
    return "".join(rt.get("plain_text", "") for rt in items).strip()


def _load_notion_snapshot(
    client: NotionClientWrapper, hierarchy_db_id: str,
) -> list[_NotionRow]:
    response = client.query_database(database_id=hierarchy_db_id)
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
                "canonical_mirror_sync: page %s has empty Name — skipping",
                page_id[:8],
            )
            continue
        rows.append(
            _NotionRow(
                notion_page_id=page_id,
                name=name,
                tier=_read_select_name(props.get("Tier")),
                active=bool((props.get("Active") or {}).get("checkbox", False)),
                parent_notion_page_id=_read_relation_first(props.get("Parent item")),
                tracker_node_page_id=_read_relation_first(props.get("Tracker Node")),
                notes=_read_rich_text(props.get("Notes")),
            ),
        )
    return rows


# ---------------------------------------------------------------------------
# Supabase I/O (stdlib only — matches src/supabase_writer.py)
# ---------------------------------------------------------------------------


def _supabase_creds() -> tuple[str, str]:
    """Resolve Supabase URL + service-role key from env.

    Accepts ``SUPABASE_SERVICE_ROLE_KEY`` (explicit) or ``SUPABASE_KEY``
    (short — common in .env). Must be a service-role key — RLS bypass is
    required because ``hierarchy_rows`` / ``hierarchy_sync_runs`` are
    RLS-enabled with no policies.
    """
    url = os.environ.get("SUPABASE_URL")
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
    )
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) "
            "must be set for canonical_mirror_sync.",
        )
    return url.rstrip("/"), key


def _http(method: str, path: str, body: Any = None, prefer: str = "return=minimal") -> Any:
    url, key = _supabase_creds()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{url}{path}", data=data, method=method, headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
            if not payload:
                return None
            try:
                return json.loads(payload.decode("utf-8"))
            except json.JSONDecodeError:
                return None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Supabase {method} {path} failed ({e.code}): {detail}",
        ) from e


def _load_supabase_snapshot() -> dict[str, dict[str, Any]]:
    """Return ``{notion_page_id: row}`` for every row in ``hierarchy_rows``.

    Includes tombstoned rows (``deleted_at`` is not null) — reactivation
    detection needs them.
    """
    rows = _http("GET", "/rest/v1/hierarchy_rows?select=*&limit=10000") or []
    return {r["notion_page_id"]: r for r in rows}


# ---------------------------------------------------------------------------
# Pure diff function
# ---------------------------------------------------------------------------


def _normalize_for_compare(field_name: str, value: Any) -> Any:
    """Coerce semantically-equivalent Notion/Postgres values for diffing.

    Notion's empty rich-text comes back as ``""``; PostgREST returns the
    column as ``None`` when never set. Without normalization the diff
    would flag spurious 'edited' events on every run after a bootstrap
    where ``notes`` was empty.
    """
    if field_name == "notes" and value is None:
        return ""
    return value


def _extract_canonical_record(canonical: dict[str, Any]) -> dict[str, Any]:
    """Pick only the mirrored fields out of a canonical row."""
    return {
        f: _normalize_for_compare(f, canonical.get(f)) for f in _MIRRORED_FIELDS
    }


def _compute_changes(
    notion_rows: list[_NotionRow],
    supabase_snapshot: dict[str, dict[str, Any]],
) -> list[_Change]:
    """Pure diff: Notion snapshot + canonical snapshot → list of ops."""
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


def _build_upsert_record(
    change: _Change, *, now_iso: str,
) -> dict[str, Any]:
    """Build a single Postgres-bound row for the upsert batch."""
    assert change.after is not None  # by construction
    record = dict(change.after)
    record["last_seen_at"] = now_iso
    if change.op in ("created", "edited", "reactivated"):
        record["last_changed_at"] = now_iso
    if change.op == "reactivated":
        record["deleted_at"] = None
    return record


def _in_filter(ids: list[str]) -> str:
    """PostgREST `in.()` filter from a list of ids."""
    return "(" + ",".join(f'"{i}"' for i in ids) + ")"


def sync(client: NotionClientWrapper, config: SyncConfig) -> SyncReport:
    """Mirror Notion Hierarchy DB → Supabase canonical state."""
    report = SyncReport(name=SUB_SYNC_NAME)

    hierarchy_db_id = getattr(config, "hierarchy_db_id", None)
    if not hierarchy_db_id:
        report.errors += 1
        report.details.append(
            "HIERARCHY_DB_ID not configured — skipping canonical_mirror_sync",
        )
        logger.warning(
            "canonical_mirror_sync: HIERARCHY_DB_ID not configured — skipping",
        )
        return report

    try:
        _supabase_creds()
    except RuntimeError as e:
        report.errors += 1
        report.details.append(f"Supabase not configured: {e}")
        logger.warning("canonical_mirror_sync: %s", e)
        return report

    try:
        notion_rows = _load_notion_snapshot(client, hierarchy_db_id)
    except Exception as e:  # noqa: BLE001
        report.errors += 1
        report.details.append(f"Notion snapshot failed: {type(e).__name__}: {e}")
        logger.exception("canonical_mirror_sync: Notion snapshot failed")
        return report

    try:
        supabase_snapshot = _load_supabase_snapshot()
    except Exception as e:  # noqa: BLE001
        report.errors += 1
        report.details.append(f"Supabase snapshot failed: {type(e).__name__}: {e}")
        logger.exception("canonical_mirror_sync: Supabase snapshot failed")
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
            f"touch={len(unchanged_ids)}"
        )
        logger.info(
            "canonical_mirror_sync: DRY RUN created=%d edited=%d deleted=%d "
            "reactivated=%d unchanged=%d",
            report.created, report.edited, report.deleted, report.reactivated,
            len(unchanged_ids),
        )
        for ch in changes:
            if ch.op == "edited":
                logger.info(
                    "canonical_mirror_sync: DRY RUN edited page=%s diff=%s",
                    ch.notion_page_id[:8], list(ch.field_diff.keys()),
                )
            elif ch.op in ("created", "deleted", "reactivated"):
                logger.info(
                    "canonical_mirror_sync: DRY RUN %s page=%s",
                    ch.op, ch.notion_page_id[:8],
                )
        return report

    # ---- Apply writes ----
    if upserts:
        try:
            _http(
                "POST",
                "/rest/v1/hierarchy_rows?on_conflict=notion_page_id",
                body=upserts,
                prefer="resolution=merge-duplicates,return=minimal",
            )
        except Exception as e:  # noqa: BLE001
            report.errors += 1
            report.details.append(
                f"upsert failed ({len(upserts)} rows): {type(e).__name__}: {e}"
            )
            logger.exception("canonical_mirror_sync: upsert failed")
            # Bail before downstream writes; next run retries the full diff.
            return report

    if deleted_ids:
        try:
            _http(
                "PATCH",
                f"/rest/v1/hierarchy_rows?notion_page_id=in.{_in_filter(deleted_ids)}",
                body={"deleted_at": now_iso},
            )
        except Exception as e:  # noqa: BLE001
            report.errors += 1
            report.details.append(
                f"tombstone PATCH failed ({len(deleted_ids)} ids): "
                f"{type(e).__name__}: {e}"
            )
            logger.exception("canonical_mirror_sync: tombstone PATCH failed")

    if unchanged_ids:
        try:
            _http(
                "PATCH",
                f"/rest/v1/hierarchy_rows?notion_page_id=in.{_in_filter(unchanged_ids)}",
                body={"last_seen_at": now_iso},
            )
        except Exception as e:  # noqa: BLE001
            # Heartbeat — never escalate to a hard error.
            report.details.append(
                f"last_seen_at heartbeat failed ({len(unchanged_ids)} ids): "
                f"{type(e).__name__}: {e}"
            )
            logger.warning(
                "canonical_mirror_sync: last_seen_at heartbeat failed: %s", e,
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
            "/rest/v1/hierarchy_sync_runs",
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
        # Audit-row failure shouldn't fail the sync — the canonical state
        # is the source of truth; the audit log is convenience.
        report.details.append(f"audit row insert failed: {type(e).__name__}: {e}")
        logger.warning(
            "canonical_mirror_sync: audit row insert failed: %s", e,
        )

    logger.info(
        "canonical_mirror_sync: created=%d edited=%d deleted=%d "
        "reactivated=%d unchanged=%d errors=%d",
        report.created, report.edited, report.deleted, report.reactivated,
        len(unchanged_ids), report.errors,
    )
    return report


__all__ = ["SUB_SYNC_NAME", "sync"]
