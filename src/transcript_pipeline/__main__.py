"""CLI entry point: python -m src.transcript_pipeline <page_id> [--verbose] [--context] [--correct] [--extract] [--write] [--dry-run] [--gcal]"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone

import logfire

from dotenv import load_dotenv
from notion_client import Client as NotionClient
from pathlib import Path

from src.notion_client_wrapper import NotionClientWrapper
from src.transcript_pipeline.fetch_transcript import fetch_transcript
from src.utils.llm_logging import get_tracker, print_usage_summary, start_tracking


def _usage_since(tracker, marker: int) -> dict:
    """Sum input/cached/output token counts for records added since marker.

    Mirror of the helper in scripts/shadow_diff_extraction.py — same shape
    so the JSON written by ``--save-run`` is byte-compatible with the rest
    of the measurement harness.
    """
    if tracker is None:
        return {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    new_records = tracker.records[marker:]
    return {
        "input_tokens": sum(r.prompt_tokens for r in new_records),
        "cached_input_tokens": sum(r.cached_tokens for r in new_records),
        "output_tokens": sum(r.completion_tokens for r in new_records),
    }


def _append_run_history(run_dir: Path, entry: dict) -> Path:
    """Append a run entry to ``<run_dir>/<page_id>.json`` (history log).

    One file per meeting. Each run is a new entry appended to the file's
    list. Never deduped — the file grows over time so you can scroll back
    and see how (e.g.) output_tokens evolved as you tweaked the prompt.
    """
    pid = entry["page_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{pid}.json"

    existing: list[dict] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                raise ValueError("not a list")
        except Exception as e:
            print(
                f"Warning: {path} exists but isn't a valid run-list "
                f"({e}); starting a fresh history.",
                file=sys.stderr,
            )
            existing = []

    existing.append(entry)
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _count_entries(path: Path) -> int:
    try:
        return len(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return 0


def _create_client() -> NotionClientWrapper:
    """Create a NotionClientWrapper with API v2026-03-11."""
    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
    import os
    notion = NotionClient(auth=os.environ["NOTION_API_TOKEN"], notion_version="2026-03-11")
    return NotionClientWrapper(notion)


def _run_gcal_test(args: argparse.Namespace) -> None:
    """Test GCal attendee lookup in isolation — no transcript fetch."""
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    from src.transcript_pipeline.fetch_transcript import (
        build_user_lookup,
        extract_attendee_ids,
        extract_governance_attendees,
        extract_page_metadata,
        find_meeting_notes_block,
        strip_title_datetime,
    )
    from src.transcript_pipeline.gcal_attendees import get_gcal_attendees

    client = _create_client()

    # 1. Page metadata + governance attendees (tertiary fallback)
    page = client.get_page(args.page_id)
    metadata = extract_page_metadata(page)
    governance_attendees = extract_governance_attendees(page)
    raw_title = metadata.get("title", "")
    clean_title = strip_title_datetime(raw_title)
    date = metadata.get("date", "")

    print("=== NOTION PAGE ===")
    print(f"  Title (raw):   {raw_title}")
    print(f"  Title (clean): {clean_title}")
    print(f"  Date:          {date}")

    if not clean_title or not date:
        print("\nERROR: Missing title or date — cannot search GCal.")
        sys.exit(1)

    # 2. Notion attendees (from meeting_notes block, no transcript fetch)
    blocks = client.get_block_children(args.page_id)
    mn_block = find_meeting_notes_block(blocks)
    notion_attendees: list[dict[str, str]] = []
    if mn_block:
        attendee_ids = extract_attendee_ids(mn_block)
        if attendee_ids:
            user_lookup = build_user_lookup(client)
            notion_attendees = [
                {"id": uid, "name": user_lookup.get(uid, uid)}
                for uid in attendee_ids
            ]

    print(f"\n=== NOTION ATTENDEES ({len(notion_attendees)}) ===")
    if notion_attendees:
        for a in notion_attendees:
            print(f"  - {a['name']}")
    else:
        print("  (none)")

    # 3. Google Calendar lookup — impersonates the page creator via service account
    print("\n=== GCAL SEARCH ===")
    print(f'  Query:  "{clean_title}"')
    print(f"  Window: ±12h around {date}")

    from src.config import load_config
    from src.pipeline import _resolve_delegated_user

    cfg = load_config()
    created_by_id = metadata.get("created_by_id", "")
    delegated_user = _resolve_delegated_user(
        client, created_by_id, cfg.gcal_delegated_user_default,
    )
    if not delegated_user:
        print("\nERROR: No Workspace user to impersonate. Set GCAL_DELEGATED_USER_DEFAULT.")
        sys.exit(1)
    print(f"  Page creator ID: {created_by_id or '(none)'}")
    print(f"  Impersonating:   {delegated_user}")
    gcal_attendees = get_gcal_attendees(clean_title, date, delegated_user)

    print(f"\n=== GCAL ATTENDEES ({len(gcal_attendees)}) ===")
    if gcal_attendees:
        for ga in gcal_attendees:
            print(f"  - {ga['name']}  <{ga['email']}>")
    else:
        print("  (none found)")

    # 3b. Governance fallback preview
    print(
        f"\n=== GOVERNANCE: EDIT & VIEW ACCESS ({len(governance_attendees)}) ==="
    )
    if governance_attendees:
        for a in governance_attendees:
            print(f"  - {a['name']}")
    else:
        print("  (none)")

    # 4. Summary: which source would the pipeline use?
    if gcal_attendees:
        print(f"\n=== RESULT: GCal is authoritative ({len(gcal_attendees)} attendees) ===")
        print(f"  Notion had {len(notion_attendees)}, GCal has {len(gcal_attendees)}")
        # Show who GCal adds that Notion missed
        notion_names_lower = {a["name"].strip().lower() for a in notion_attendees}
        gcal_only = [
            ga for ga in gcal_attendees
            if ga["name"].strip().lower() not in notion_names_lower
        ]
        if gcal_only:
            print(f"  GCal adds {len(gcal_only)} not in Notion:")
            for ga in gcal_only:
                print(f"    + {ga['name']}  <{ga['email']}>")
    elif notion_attendees:
        print(f"\n=== RESULT: Falling back to Notion meeting_notes ({len(notion_attendees)} attendees) ===")
    elif governance_attendees:
        print(
            f"\n=== RESULT: Falling back to Governance: Edit & View Access "
            f"({len(governance_attendees)} attendees) ==="
        )
    else:
        print("\n=== RESULT: No attendees from any source ===")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch and optionally correct a meeting transcript from Notion.",
    )
    parser.add_argument("page_id", help="Notion page ID containing a meeting_notes block")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print raw API response for debugging")
    parser.add_argument("--context", "-c", action="store_true", help="Also load terminology + org chart context")
    parser.add_argument("--correct", action="store_true", help="Run LLM correction on the transcript (implies --context)")
    parser.add_argument("--model", type=str, default=None, help="Override LLM model for correction + extraction")
    parser.add_argument("--classifier-model", type=str, default=None, help="Override LLM model for classification (defaults to --model)")
    parser.add_argument("--correction-model", type=str, default=None, help="[--write only] Override the transcript-correction model. Provider auto-detected from prefix.")
    parser.add_argument("--extraction-model", type=str, default=None, help="[--write only] Override the task-extraction model. Provider auto-detected from prefix.")
    parser.add_argument("--classification-model", type=str, default=None, help="[--write only] Override the task-classification model. Provider auto-detected from prefix.")
    parser.add_argument("--extract", action="store_true", help="Extract tasks (merged single-call path by default; uses 2-call flow only when --legacy-2call is set)")
    parser.add_argument("--write", action="store_true", help="Write extracted tasks to Team Task Tracker (implies --extract)")
    parser.add_argument("--dry-run", action="store_true", help="Log tasks that would be written without creating them (requires --write)")
    parser.add_argument("--openai", action="store_true", help="Force OpenAI endpoint for correction + extraction")
    parser.add_argument(
        "--openrouter",
        action="store_true",
        help=(
            "Diagnostic only (--extract). Route the merged-extract call through "
            "OpenRouter (https://openrouter.ai). Requires OPENROUTER_API_KEY in "
            ".env and a --model slug like 'deepseek/deepseek-chat-v3.1:free'."
        ),
    )
    parser.add_argument("--classifier-openai", action="store_true", help="Force OpenAI endpoint for classification (defaults to --openai)")
    parser.add_argument("--gcal", action="store_true", help="Test GCal attendee lookup only (no transcript fetch)")
    parser.add_argument(
        "--legacy-2call",
        action="store_true",
        help=(
            "Force the legacy 2-call flow (TranscriptCorrector → TaskExtractor). "
            "Diagnostic only — production path is controlled by TRANSCRIPT_MERGED_EXTRACTION."
        ),
    )
    parser.add_argument(
        "--save-run",
        action="store_true",
        help=(
            "After --extract, append a history entry (tasks + token counts + "
            "raw payload + UTC timestamp + optional --run-note) to "
            "<save-run-dir>/<page_id>.json. Re-runs of the same page append a "
            "NEW entry to the same file so you can scroll back through how "
            "output_tokens evolved as you tweaked the prompt or schema."
        ),
    )
    parser.add_argument(
        "--save-run-dir",
        type=Path,
        default=Path("runs"),
        metavar="DIR",
        help="Directory for --save-run history files (default: ./runs).",
    )
    parser.add_argument(
        "--run-note",
        type=str,
        default=None,
        metavar="TEXT",
        help=(
            "Free-text label saved alongside the run (e.g. 'baseline', "
            "'dropped sr field', 'shortened prompt'). Helps you scan the "
            "history file later."
        ),
    )
    args = parser.parse_args()

    start_tracking()

    if args.gcal:
        _run_gcal_test(args)
        return

    # --write routes through the unified pipeline
    if args.write:
        _run_write_mode(args)
        return

    # Diagnostic modes: --extract uses the merged call by default;
    # --legacy-2call falls back to the old corrector → extractor flow.
    use_merged = args.extract and not args.legacy_2call
    if args.extract and args.legacy_2call:
        args.correct = True
    load_context = args.context or args.correct or use_merged

    if args.dry_run:
        print("Note: --dry-run has no effect without --write", file=sys.stderr)

    client = _create_client()
    transcript_text, attendees, metadata, notes_text, governance_attendees = fetch_transcript(args.page_id, client, verbose=args.verbose)

    # Load context from Notion DBs
    terminology = ""
    org_chart = ""
    org_chart_rows: list[dict] = []
    cfg = None
    if load_context:
        from src.config import load_config
        from src.transcript_pipeline.context_loader import (
            load_org_chart,
            load_org_chart_rows,
            load_terminology,
        )

        cfg = load_config()

        if cfg.terminology_db_id:
            terminology = load_terminology(client, cfg.terminology_db_id)
        if cfg.org_chart_db_id:
            org_chart = load_org_chart(client, cfg.org_chart_db_id)
            org_chart_rows = load_org_chart_rows(client, cfg.org_chart_db_id)

    # Attendee resolution chain
    if metadata.get("title") and metadata.get("date") and cfg and cfg.gcal_enabled:
        from src.pipeline import _resolve_delegated_user
        from src.transcript_pipeline.gcal_attendees import get_gcal_attendees

        created_by_id = metadata.get("created_by_id", "")
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

    if not attendees and governance_attendees:
        attendees = governance_attendees
        print(
            f"Note: no GCal event or meeting_notes attendees; using "
            f"'Governance: Edit & View Access' ({len(attendees)} people)",
            file=sys.stderr,
        )

    # Build enriched attendee string
    enriched_attendee_str = ""
    if org_chart_rows and attendees:
        from src.transcript_pipeline.context_loader import build_enriched_attendee_str

        enriched_attendee_str = build_enriched_attendee_str(attendees, org_chart_rows)

    # Print meeting info
    if metadata.get("title"):
        print(f"=== MEETING: {metadata['title']} ({metadata.get('date', '?')}) ===")
    print("=== ATTENDEES ===")
    if enriched_attendee_str:
        for line in enriched_attendee_str.splitlines():
            print(f"  {line}")
    elif attendees:
        for a in attendees:
            print(f"  - {a['name']}")
    else:
        print("  (none found)")

    if notes_text:
        print("=== HUMAN NOTES ===")
        print(notes_text)

    if load_context and args.context and not args.correct:
        print()
        print("=== TERMINOLOGY CONTEXT ===")
        print(terminology if terminology else "  (no active terms)")
        print()
        print("=== ORG CHART CONTEXT ===")
        print(org_chart if org_chart else "  (no active members)")

    # Merged single-call path (default for --extract without --legacy-2call)
    if use_merged:
        if not cfg:
            from src.config import load_config
            cfg = load_config()
        logfire.configure(token=cfg.logfire_token, service_name="nzyme-transcript")
        logfire.instrument_openai()

        if not transcript_text:
            print("\nERROR: No transcript to extract from.", file=sys.stderr)
            sys.exit(1)

        # Deterministic noise cleanup — mirrors what _process_via_transcript
        # does in the production pipeline, so --extract is a faithful
        # diagnostic of what the LLM actually sees.
        from src.transcript_pipeline.transcript_cleaner import clean as clean_transcript

        cleaned = clean_transcript(transcript_text)
        if cleaned.chars_before:
            print(
                f"Transcript cleaned: {cleaned.chars_before} → {cleaned.chars_after} "
                f"chars ({cleaned.ratio * 100:.0f}% kept)",
                file=sys.stderr,
            )
        transcript_text = cleaned.text

        from src.transcript_pipeline.task_extractor import TaskExtractor

        if args.openrouter:
            if not cfg.openrouter_api_key:
                print(
                    "\nERROR: --openrouter requires OPENROUTER_API_KEY in .env.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if not (args.model or args.extraction_model):
                print(
                    "\nERROR: --openrouter requires --model (e.g. "
                    "'deepseek/deepseek-chat-v3.1:free'). The default Gemini "
                    "model would not route correctly via OpenRouter.",
                    file=sys.stderr,
                )
                sys.exit(1)
            model = args.model or args.extraction_model
            base_url = cfg.openrouter_base_url
            api_key = cfg.openrouter_api_key
        else:
            model = args.model or args.extraction_model or cfg.gemini_model
            base_url = "https://api.openai.com/v1" if args.openai else cfg.gemini_base_url
            api_key = cfg.openai_api_key if args.openai else (cfg.gemini_api_key or cfg.openai_api_key)

        print(f"Merged-extracting tasks with {model} (single call)...", file=sys.stderr)
        extractor = TaskExtractor(api_key=api_key, model=model, base_url=base_url)

        tracker = get_tracker()
        marker = len(tracker.records) if tracker else 0
        t0 = time.perf_counter()
        tasks = extractor.extract_from_raw(
            transcript_text,
            attendees,
            org_chart=org_chart,
            terminology=terminology,
            meeting_title=metadata.get("title", ""),
            meeting_date=metadata.get("date", ""),
            enriched_attendee_str=enriched_attendee_str,
            notes_text=notes_text,
        )
        elapsed = time.perf_counter() - t0

        print()
        print(f"=== EXTRACTED TASKS ({len(tasks)}) ===")
        if tasks:
            for i, t in enumerate(tasks, 1):
                priority = t.get("priority", "?")
                title = t.get("title", "(no title)")
                assignee = t.get("assignee") or "Unassigned"
                due = t.get("due_date") or "—"
                confidence = t.get("confidence", "?")
                commitment = t.get("commitment_type", "?")
                context = t.get("context", "")
                reasoning = t.get("speaker_reasoning", "")
                print()
                print(f"  {i}. [{priority}] {title}  (confidence: {confidence}, commitment: {commitment})")
                print(f"     Assignee: {assignee}")
                print(f"     Due: {due}")
                if reasoning:
                    print(f"     Why: {reasoning}")
                if context:
                    print(f'     Context: "{context}"')
        else:
            print("  (no tasks found)")

        if args.save_run:
            entry = {
                "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "run_note": args.run_note or "",
                "model": model,
                "page_id": args.page_id,
                "meeting_title": metadata.get("title", ""),
                "meeting_date": metadata.get("date", ""),
                "transcript_chars": len(transcript_text or ""),
                "merged": {
                    "tasks": tasks,
                    "elapsed_s": round(elapsed, 2),
                    **_usage_since(tracker, marker),
                    "raw_data": extractor._last_raw_data,
                },
                "error": None,
            }
            path = _append_run_history(args.save_run_dir, entry)
            usage = entry["merged"]
            print(
                f"Saved run #{_count_entries(path)} → {path}  "
                f"(out tokens: {usage['output_tokens']}, "
                f"tasks: {len(tasks)}"
                + (f", note: {args.run_note!r}" if args.run_note else "")
                + ")",
                file=sys.stderr,
            )
        return

    # Run LLM correction
    if args.correct:
        if not cfg:
            from src.config import load_config
            cfg = load_config()
        logfire.configure(token=cfg.logfire_token, service_name="nzyme-transcript")
        logfire.instrument_openai()

        if not transcript_text:
            print("\nERROR: No transcript to correct.", file=sys.stderr)
            sys.exit(1)

        from src.transcript_pipeline.transcript_corrector import TranscriptCorrector

        model = args.model or cfg.openai_model
        base_url = "https://api.openai.com/v1" if args.openai else cfg.openai_base_url
        api_key = cfg.openai_api_key if args.openai else (cfg.gemini_api_key or cfg.openai_api_key)

        corrector = TranscriptCorrector(
            api_key=api_key,
            model=model,
            base_url=base_url,
        )

        print("Correcting transcript with", model, "...", file=sys.stderr)
        corrected = corrector.correct(
            transcript_text, terminology, attendees,
            enriched_attendee_str=enriched_attendee_str,
            notes_text=notes_text,
        )

        if not args.extract:
            print()
            print("=== RAW TRANSCRIPT ===")
            print(transcript_text)
            print()
            print("=== CORRECTED TRANSCRIPT ===")
            print(corrected)

        # Task extraction
        if args.extract:
            from src.transcript_pipeline.task_extractor import TaskExtractor

            print()
            print("Extracting tasks with", model, "...", file=sys.stderr)
            extractor = TaskExtractor(
                api_key=api_key,
                model=model,
                base_url=base_url,
            )
            tasks = extractor.extract(
                corrected,
                attendees,
                org_chart=org_chart,
                terminology=terminology,
                meeting_title=metadata.get("title", ""),
                meeting_date=metadata.get("date", ""),
                enriched_attendee_str=enriched_attendee_str,
                notes_text=notes_text,
            )

            print()
            print(f"=== EXTRACTED TASKS ({len(tasks)}) ===")
            if tasks:
                for i, t in enumerate(tasks, 1):
                    priority = t.get("priority", "?")
                    title = t.get("title", "(no title)")
                    assignee = t.get("assignee") or "Unassigned"
                    due = t.get("due_date") or "\u2014"
                    confidence = t.get("confidence", "?")
                    context = t.get("context", "")
                    reasoning = t.get("speaker_reasoning", "")
                    print()
                    print(f"  {i}. [{priority}] {title}  (confidence: {confidence})")
                    print(f"     Assignee: {assignee}")
                    print(f"     Due: {due}")
                    if reasoning:
                        print(f"     Why: {reasoning}")
                    if context:
                        print(f'     Context: "{context}"')
            else:
                print("  (no tasks found)")
    else:
        print()
        print("=== TRANSCRIPT ===")
        if transcript_text:
            print(transcript_text)
        else:
            print("  (empty — recording may not be processed yet)")


def _run_write_mode(args: argparse.Namespace) -> None:
    """Run the full pipeline for a single page (correct → extract → classify → write).

    Routes through pipeline.run_sync_for_page() to use the unified pipeline
    with transcript-first extraction and all post-processing (dedup, etc.).
    """
    from src.config import load_config

    cfg = load_config()

    # Validate required config
    if not cfg.team_tracker_db_id:
        print("ERROR: --write requires TEAM_TRACKER_DB_ID in .env", file=sys.stderr)
        sys.exit(1)
    if not cfg.classifier_prompt_page_id:
        print("ERROR: --write requires CLASSIFIER_PROMPT_PAGE_ID in .env", file=sys.stderr)
        sys.exit(1)

    # Apply CLI overrides to config
    overrides: dict = {}
    if args.dry_run:
        overrides["dry_run"] = True
    if args.model:
        overrides["openai_model"] = args.model
    if args.correction_model:
        overrides["correction_model"] = args.correction_model
    if args.extraction_model:
        overrides["extraction_model"] = args.extraction_model
    if args.classification_model:
        overrides["classification_model"] = args.classification_model
    if args.openai:
        overrides["openai_base_url"] = "https://api.openai.com/v1"
    if args.legacy_2call:
        overrides["transcript_merged_extraction"] = False
    if overrides:
        cfg = cfg.model_copy(update=overrides)

    logfire.configure(token=cfg.logfire_token, service_name="nzyme-transcript")
    logfire.instrument_openai()

    from src.utils.logger import setup_logging
    setup_logging("DEBUG" if args.verbose else "INFO")

    client = _create_client()

    from src.pipeline import run_sync_for_page

    print(f"Processing page {args.page_id} via unified pipeline...")
    if args.dry_run:
        print("(dry-run mode — no writes)")
    print()

    run_sync_for_page(cfg, client, args.page_id, force=True)

    print()
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        print_usage_summary()
