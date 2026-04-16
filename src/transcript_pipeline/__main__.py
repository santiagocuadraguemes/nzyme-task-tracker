"""CLI entry point: python -m src.transcript_pipeline <page_id> [--verbose] [--context] [--correct] [--extract] [--write] [--dry-run] [--gcal]"""

from __future__ import annotations

import argparse
import logging
import sys

import logfire

from dotenv import load_dotenv
from notion_client import Client as NotionClient
from pathlib import Path

from src.notion_client_wrapper import NotionClientWrapper
from src.transcript_pipeline.fetch_transcript import fetch_transcript


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

    # 3. Google Calendar lookup (People API resolves emails → names)
    print("\n=== GCAL SEARCH ===")
    print(f'  Query:  "{clean_title}"')
    print(f"  Window: ±12h around {date}")

    gcal_attendees = get_gcal_attendees(clean_title, date)

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
    parser.add_argument("--extract", action="store_true", help="Extract tasks from corrected transcript (implies --correct)")
    parser.add_argument("--write", action="store_true", help="Write extracted tasks to Team Task Tracker (implies --extract)")
    parser.add_argument("--dry-run", action="store_true", help="Log tasks that would be written without creating them (requires --write)")
    parser.add_argument("--openai", action="store_true", help="Force OpenAI endpoint for correction + extraction")
    parser.add_argument("--classifier-openai", action="store_true", help="Force OpenAI endpoint for classification (defaults to --openai)")
    parser.add_argument("--gcal", action="store_true", help="Test GCal attendee lookup only (no transcript fetch)")
    args = parser.parse_args()

    if args.gcal:
        _run_gcal_test(args)
        return

    # --write routes through the unified pipeline
    if args.write:
        _run_write_mode(args)
        return

    # --extract implies --correct, --correct implies --context
    if args.extract:
        args.correct = True
    load_context = args.context or args.correct

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
    if metadata.get("title") and metadata.get("date"):
        from src.transcript_pipeline.gcal_attendees import get_gcal_attendees

        gcal_attendees = get_gcal_attendees(metadata["title"], metadata["date"])
        if gcal_attendees:
            attendees = [{"id": ga["email"], "name": ga["name"]} for ga in gcal_attendees]

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
    if args.openai:
        overrides["openai_base_url"] = "https://api.openai.com/v1"
    if overrides:
        cfg = cfg.model_copy(update=overrides)

    logfire.configure(token=cfg.logfire_token, service_name="nzyme-transcript")
    logfire.instrument_openai()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    client = _create_client()

    from src.pipeline import run_sync_for_page

    print(f"Processing page {args.page_id} via unified pipeline...")
    if args.dry_run:
        print("(dry-run mode — no writes)")
    print()

    run_sync_for_page(cfg, client, args.page_id, use_gcal=True, force=True)

    print()
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
