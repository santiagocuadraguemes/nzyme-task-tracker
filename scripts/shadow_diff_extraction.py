"""Shadow-diff: run legacy (2-call) and merged (1-call) extractors side-by-side.

Usage:
    ../venv/Scripts/python scripts/shadow_diff_extraction.py \
        --pages page_id_1 page_id_2 page_id_3 \
        --out shadow-diff.json

Or read page IDs one-per-line from a file:
    ../venv/Scripts/python scripts/shadow_diff_extraction.py \
        --pages-file pages.txt --out shadow-diff.json

Output is a JSON list, one entry per page:
    [
      {
        "page_id": "...",
        "meeting_title": "...",
        "meeting_date": "...",
        "transcript_chars": 12345,
        "legacy": {"tasks": [...], "elapsed_s": 12.3},
        "merged": {"tasks": [...], "elapsed_s": 4.1},
        "error": null
      },
      ...
    ]

Both paths run against the same fetched transcript + resolved attendees +
loaded context. Tasks come BEFORE classification (no Notion writes, no
tracker hits). Use the dumped JSON to manually compare task overlap,
assignees, priorities, due_dates, and commitment_type distribution
against the §6 passing thresholds in the design doc.

Each result also includes per-call token counts (input / cached / output)
and — for the merged path — the raw short-key payload (``raw_data``) with
scratch fields preserved. ``scripts/estimate_output_savings.py`` feeds on
``raw_data`` to estimate output-token savings for candidate schema changes
without firing any real API calls.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import logfire
from dotenv import load_dotenv
from notion_client import Client as NotionClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_config  # noqa: E402
from src.notion_client_wrapper import NotionClientWrapper  # noqa: E402
from src.transcript_pipeline.context_loader import (  # noqa: E402
    build_enriched_attendee_str,
    load_org_chart,
    load_org_chart_rows,
    load_terminology,
)
from src.transcript_pipeline.fetch_transcript import fetch_transcript  # noqa: E402
from src.transcript_pipeline.task_extractor import TaskExtractor  # noqa: E402
from src.transcript_pipeline.transcript_corrector import TranscriptCorrector  # noqa: E402
from src.utils.llm_logging import get_tracker, start_tracking  # noqa: E402


def _usage_since(tracker, marker: int) -> dict:
    """Sum prompt/cached/completion tokens for records added since ``marker``."""
    if tracker is None:
        return {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    new_records = tracker.records[marker:]
    return {
        "input_tokens": sum(r.prompt_tokens for r in new_records),
        "cached_input_tokens": sum(r.cached_tokens for r in new_records),
        "output_tokens": sum(r.completion_tokens for r in new_records),
    }


def _make_client() -> NotionClientWrapper:
    import os
    notion = NotionClient(auth=os.environ["NOTION_API_TOKEN"], notion_version="2026-03-11")
    return NotionClientWrapper(notion)


def _resolve_attendees_for_diagnostic(
    client: NotionClientWrapper,
    cfg,
    page_id: str,
    metadata: dict,
    notion_attendees: list[dict],
    governance_attendees: list[dict],
    org_chart_rows: list[dict],
) -> list[dict]:
    """Mirror pipeline._resolve_attendees minus the import cycle."""
    attendees = list(notion_attendees)
    if (
        cfg.gcal_enabled
        and metadata.get("title")
        and metadata.get("date")
    ):
        try:
            from src.pipeline import _resolve_delegated_user
            from src.transcript_pipeline.gcal_attendees import get_gcal_attendees

            created_by_id = (metadata.get("created_by") or {}).get("id", "") or metadata.get("created_by_id", "")
            delegated_user = _resolve_delegated_user(
                client, created_by_id, cfg.gcal_delegated_user_default,
            )
            if delegated_user:
                gcal_attendees = get_gcal_attendees(
                    metadata["title"], metadata["date"], delegated_user,
                )
                if gcal_attendees:
                    attendees = [
                        {"id": ga["email"], "name": ga["name"], "email": ga["email"]}
                        for ga in gcal_attendees
                    ]
                    # Light name enrichment via org chart email match
                    email_to_name = {
                        r["email"]: r["name"]
                        for r in (org_chart_rows or [])
                        if r.get("email") and r.get("name")
                    }
                    attendees = [
                        {**a, "name": email_to_name.get((a.get("email") or "").lower(), a["name"])}
                        for a in attendees
                    ]
        except Exception:
            logging.warning("GCal lookup failed for %s — using Notion attendees", page_id, exc_info=True)

    if not attendees and governance_attendees:
        attendees = [{**g, "email": None} for g in governance_attendees]
    return attendees


def diff_one_page(
    client: NotionClientWrapper,
    cfg,
    page_id: str,
    terminology: str,
    org_chart_text: str,
    org_chart_rows: list[dict],
) -> dict:
    """Run both paths on a single page; return their outputs side-by-side."""
    out: dict = {
        "page_id": page_id,
        "meeting_title": None,
        "meeting_date": None,
        "transcript_chars": 0,
        "legacy": None,
        "merged": None,
        "error": None,
    }

    try:
        transcript_text, notion_attendees, metadata, notes_text, governance_attendees = (
            fetch_transcript(page_id, client, verbose=False)
        )
        out["meeting_title"] = metadata.get("title", "")
        out["meeting_date"] = metadata.get("date", "")
        out["transcript_chars"] = len(transcript_text or "")

        if not transcript_text:
            out["error"] = "no transcript text"
            return out

        attendees = _resolve_attendees_for_diagnostic(
            client, cfg, page_id, metadata,
            notion_attendees, governance_attendees, org_chart_rows,
        )

        enriched_attendee_str = (
            build_enriched_attendee_str(attendees, org_chart_rows)
            if org_chart_rows and attendees else ""
        )

        # Pick Gemini creds (same as production transcript path).
        model = cfg.extraction_model or cfg.gemini_model
        api_key = cfg.gemini_api_key or cfg.openai_api_key
        base_url = cfg.gemini_base_url

        tracker = get_tracker()

        # ---- Legacy: corrector → extractor ----
        correction_model = cfg.correction_model or cfg.gemini_model
        corrector = TranscriptCorrector(
            api_key=api_key, model=correction_model, base_url=base_url,
        )
        legacy_marker = len(tracker.records) if tracker else 0
        t0 = time.perf_counter()
        corrected = corrector.correct(
            transcript_text, terminology, attendees,
            enriched_attendee_str=enriched_attendee_str,
            notes_text=notes_text,
        )
        legacy_extractor = TaskExtractor(api_key=api_key, model=model, base_url=base_url)
        legacy_tasks = legacy_extractor.extract(
            corrected, attendees,
            org_chart=org_chart_text,
            terminology=terminology,
            meeting_title=metadata.get("title", ""),
            meeting_date=metadata.get("date", ""),
            enriched_attendee_str=enriched_attendee_str,
            notes_text=notes_text,
        )
        legacy_elapsed = time.perf_counter() - t0
        out["legacy"] = {
            "tasks": legacy_tasks,
            "elapsed_s": round(legacy_elapsed, 2),
            "corrected_chars": len(corrected or ""),
            **_usage_since(tracker, legacy_marker),
        }

        # ---- Merged: single call ----
        merged_extractor = TaskExtractor(api_key=api_key, model=model, base_url=base_url)
        merged_marker = len(tracker.records) if tracker else 0
        t0 = time.perf_counter()
        merged_tasks = merged_extractor.extract_from_raw(
            transcript_text, attendees,
            org_chart=org_chart_text,
            terminology=terminology,
            meeting_title=metadata.get("title", ""),
            meeting_date=metadata.get("date", ""),
            enriched_attendee_str=enriched_attendee_str,
            notes_text=notes_text,
        )
        merged_elapsed = time.perf_counter() - t0
        out["merged"] = {
            "tasks": merged_tasks,
            "elapsed_s": round(merged_elapsed, 2),
            **_usage_since(tracker, merged_marker),
            # Raw short-key payload (incl. scratch fields). The estimator
            # uses this to reconstruct what the model actually emitted.
            "raw_data": merged_extractor._last_raw_data,
        }
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        logging.exception("Page %s failed", page_id)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--pages", nargs="+", help="Notion page IDs (space-separated)")
    src.add_argument("--pages-file", type=Path, help="File with one page ID per line")
    parser.add_argument("--out", type=Path, default=Path("shadow-diff.json"),
                        help="Output JSON path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    page_ids: list[str] = []
    if args.pages:
        page_ids = list(args.pages)
    else:
        page_ids = [
            line.strip() for line in args.pages_file.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]

    if not page_ids:
        print("ERROR: no page IDs provided", file=sys.stderr)
        sys.exit(1)

    cfg = load_config()
    logfire.configure(token=cfg.logfire_token, service_name="nzyme-shadow-diff")
    logfire.instrument_openai()
    start_tracking()

    client = _make_client()

    # Load shared context once.
    terminology = load_terminology(client, cfg.terminology_db_id) if cfg.terminology_db_id else ""
    org_chart_text = load_org_chart(client, cfg.org_chart_db_id) if cfg.org_chart_db_id else ""
    org_chart_rows = load_org_chart_rows(client, cfg.org_chart_db_id) if cfg.org_chart_db_id else []

    results: list[dict] = []
    for i, pid in enumerate(page_ids, 1):
        logging.info("[%d/%d] %s", i, len(page_ids), pid)
        results.append(
            diff_one_page(client, cfg, pid, terminology, org_chart_text, org_chart_rows)
        )

    args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\nWrote {len(results)} entries to {args.out}")

    # Quick summary
    ok = [r for r in results if r["error"] is None and r["legacy"] and r["merged"]]
    if ok:
        legacy_total = sum(len(r["legacy"]["tasks"]) for r in ok)
        merged_total = sum(len(r["merged"]["tasks"]) for r in ok)
        legacy_time = sum(r["legacy"]["elapsed_s"] for r in ok)
        merged_time = sum(r["merged"]["elapsed_s"] for r in ok)
        print(f"  legacy: {legacy_total} tasks across {len(ok)} pages, {legacy_time:.1f}s total")
        print(f"  merged: {merged_total} tasks across {len(ok)} pages, {merged_time:.1f}s total")

        # Per-path output-token totals + per-page averages. The merged
        # numbers are the ones to watch when ranking schema reductions.
        legacy_out = sum(r["legacy"].get("output_tokens", 0) for r in ok)
        merged_out = sum(r["merged"].get("output_tokens", 0) for r in ok)
        merged_in = sum(r["merged"].get("input_tokens", 0) for r in ok)
        merged_cached = sum(r["merged"].get("cached_input_tokens", 0) for r in ok)
        n = len(ok)
        print(
            f"  legacy output tokens: {legacy_out:,} total, "
            f"{legacy_out / n:.0f} avg/page"
        )
        print(
            f"  merged output tokens: {merged_out:,} total, "
            f"{merged_out / n:.0f} avg/page "
            f"({merged_out / max(merged_total, 1):.0f} avg/task)"
        )
        print(
            f"  merged input tokens : {merged_in:,} total ({merged_cached:,} cached)"
        )
    failures = [r for r in results if r["error"]]
    if failures:
        print(f"  {len(failures)} page(s) failed — see entries with non-null `error`")


if __name__ == "__main__":
    main()
