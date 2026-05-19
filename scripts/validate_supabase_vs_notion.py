"""Spot-check meeting_transcripts rows against live Notion.

For each page_id passed on the command line (or sampled from Supabase),
re-extract the row from Notion using the same code path as the backfill
script, then diff against what's currently stored in Supabase.

Usage:
    python scripts/validate_supabase_vs_notion.py <page_id> [<page_id>...]
    python scripts/validate_supabase_vs_notion.py --random 5
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import urllib.parse
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from notion_client import Client as NotionClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.meeting_db_registry import discover_meeting_dbs, MeetingDB, _extract_db_id
from src.meeting_row import extract_row
from src.notion_client_wrapper import NotionClientWrapper


def supabase_get(page_ids: list[str], url: str, key: str) -> dict[str, dict]:
    """Fetch rows by page_id list from Supabase via PostgREST."""
    if not page_ids:
        return {}
    in_list = "(" + ",".join(f'"{p}"' for p in page_ids) + ")"
    qs = urllib.parse.urlencode({
        "select": "page_id,title,meeting_type,detail,meeting_start,"
                  "transcript,notes,notion_summary,task_page_ids,"
                  "owner_name,last_edited_time",
        "page_id": f"in.{in_list}",
    })
    req = urllib.request.Request(
        f"{url.rstrip('/')}/rest/v1/meeting_transcripts?{qs}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        rows = json.loads(resp.read().decode("utf-8"))
    return {r["page_id"]: r for r in rows}


def sample_random(url: str, key: str, n: int) -> list[str]:
    """Pull up to 500 page_ids from Supabase, sample n at random."""
    qs = urllib.parse.urlencode({"select": "page_id", "limit": "500"})
    req = urllib.request.Request(
        f"{url.rstrip('/')}/rest/v1/meeting_transcripts?{qs}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        rows = json.loads(resp.read().decode("utf-8"))
    ids = [r["page_id"] for r in rows]
    random.shuffle(ids)
    return ids[:n]


def _digest(s: str | None) -> str:
    if not s:
        return "ø"
    return f"{len(s):>6}  md5={hashlib.md5(s.encode()).hexdigest()[:8]}"


def _norm_ts(v):
    """Parse a Notion/Postgres timestamp into a UTC datetime for instant-equality."""
    if not v:
        return None
    from datetime import datetime
    # Postgres returns "...+00:00", Notion "...+02:00" or "Z" or with ".000".
    s = str(v).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).astimezone(tz=None).utctimetuple()
    except ValueError:
        return s  # fall back to string equality


def compare(supa: dict, fresh: dict) -> tuple[bool, list[str]]:
    """Return (matches, diff_messages)."""
    diffs: list[str] = []
    for field in ("title", "meeting_type", "detail"):
        a = supa.get(field)
        b = fresh.get(field)
        if str(a) != str(b):
            diffs.append(f"    {field}:  supa={a!r}  notion={b!r}")
    # Date-only vs datetime: Postgres timestamptz forces midnight UTC for
    # all-day events, while Notion returns "YYYY-MM-DD". Compare just the
    # date portion when meeting_is_datetime is false.
    sa_raw = supa.get("meeting_start") or ""
    sb_raw = fresh.get("meeting_start") or ""
    if not fresh.get("meeting_is_datetime"):
        if sa_raw[:10] != sb_raw[:10]:
            diffs.append(
                f"    meeting_start (date):  supa={sa_raw!r}  notion={sb_raw!r}",
            )
    else:
        if _norm_ts(sa_raw) != _norm_ts(sb_raw):
            diffs.append(
                f"    meeting_start:  supa={sa_raw!r}  notion={sb_raw!r}",
            )
    for field in ("transcript", "notes", "notion_summary"):
        a = supa.get(field) or ""
        b = fresh.get(field) or ""
        if a == b:
            continue
        # Length diff likely means the page edited after backfill — flag it.
        diffs.append(
            f"    {field}:  supa={_digest(a)}  notion={_digest(b)}",
        )
    sa = set(supa.get("task_page_ids") or [])
    sb = set(fresh.get("task_page_ids") or [])
    if sa != sb:
        only_supa = sa - sb
        only_notion = sb - sa
        if only_supa or only_notion:
            diffs.append(
                f"    task_page_ids:  supa_only={list(only_supa)}  "
                f"notion_only={list(only_notion)}",
            )
    return (not diffs, diffs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("page_ids", nargs="*",
                        help="Specific page IDs to validate")
    parser.add_argument("--random", type=int, default=None,
                        help="Sample N random page IDs from Supabase")
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    sb_url = os.environ["SUPABASE_URL"]
    sb_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ["SUPABASE_KEY"]
    notion_token = os.environ["NOTION_API_TOKEN"]
    org_chart_db_id = os.environ["ORG_CHART_DB_ID"]

    if args.random:
        page_ids = sample_random(sb_url, sb_key, args.random)
    else:
        page_ids = args.page_ids
    if not page_ids:
        sys.exit("No page IDs to validate. Pass page IDs or --random N.")

    print(f"Validating {len(page_ids)} pages\n")

    client = NotionClientWrapper(
        NotionClient(auth=notion_token, notion_version="2026-03-11"),
    )
    registry = discover_meeting_dbs(client, org_chart_db_id)
    by_db = {db.db_id.replace("-", "").lower(): db for db in registry}

    supa_rows = supabase_get(page_ids, sb_url, sb_key)

    n_ok = n_diff = n_missing = 0
    for pid in page_ids:
        print(f"━━━ {pid} ━━━")
        supa = supa_rows.get(pid)
        if not supa:
            print("  ✗ Not present in Supabase\n")
            n_missing += 1
            continue
        page = client.get_page(pid)
        parent_db = (
            page.get("parent", {}).get("database_id")
            or page.get("parent", {}).get("data_source_id", "")
        )
        owner = by_db.get((parent_db or "").replace("-", "").lower())
        if owner is None:
            owner = MeetingDB(
                db_id=_extract_db_id(f"notion.so/{parent_db}") or parent_db,
                owner_name="?", owner_email="",
            )
        fresh = extract_row(page, owner, client)
        ok, diffs = compare(supa, fresh)
        if ok:
            print(f"  ✓ MATCH  '{supa['title'][:60]}'  "
                  f"t={len(supa.get('transcript') or '')} "
                  f"s={len(supa.get('notion_summary') or '')} "
                  f"n={len(supa.get('notes') or '')}\n")
            n_ok += 1
        else:
            print(f"  ✗ DIFF   '{supa['title'][:60]}'")
            for d in diffs:
                print(d)
            print()
            n_diff += 1

    print(f"\nSummary: {n_ok} match, {n_diff} differ, {n_missing} missing")
    return 0 if n_diff == 0 and n_missing == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
