"""CLI entry point: python -m src.transcript_pipeline <page_id> [--verbose] [--context] [--correct] [--extract] [--gcal]"""

from __future__ import annotations

import argparse
import logging
import sys

import logfire

from src.transcript_pipeline.fetch_transcript import fetch_transcript
from src.transcript_pipeline.transcript_client import create_transcript_client


def _run_gcal_test(args: argparse.Namespace) -> None:
    """Test GCal attendee lookup in isolation — no transcript fetch."""
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    from src.transcript_pipeline.fetch_transcript import (
        build_user_lookup,
        extract_attendee_ids,
        extract_page_metadata,
        find_meeting_notes_block,
        strip_title_datetime,
    )
    from src.transcript_pipeline.gcal_attendees import get_gcal_attendees

    client = create_transcript_client()

    # 1. Page metadata
    page = client.get_page(args.page_id)
    metadata = extract_page_metadata(page)
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
    print(f"\n=== GCAL SEARCH ===")
    print(f'  Query:  "{clean_title}"')
    print(f"  Window: ±12h around {date}")

    gcal_attendees = get_gcal_attendees(clean_title, date)

    print(f"\n=== GCAL ATTENDEES ({len(gcal_attendees)}) ===")
    if gcal_attendees:
        for ga in gcal_attendees:
            print(f"  - {ga['name']}  <{ga['email']}>")
    else:
        print("  (none found)")

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
    else:
        print(f"\n=== RESULT: Falling back to Notion ({len(notion_attendees)} attendees) ===")
        print("  No GCal event found — Notion attendees will be used.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch and optionally correct a meeting transcript from Notion.",
    )
    parser.add_argument("page_id", help="Notion page ID containing a meeting_notes block")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print raw API response for debugging")
    parser.add_argument("--context", "-c", action="store_true", help="Also load terminology + org chart context")
    parser.add_argument("--correct", action="store_true", help="Run LLM correction on the transcript (implies --context)")
    parser.add_argument("--model", type=str, default=None, help="Override LLM model (e.g., gpt-4o-mini)")
    parser.add_argument("--extract", action="store_true", help="Extract tasks from corrected transcript (implies --correct)")
    parser.add_argument("--openai", action="store_true", help="Force OpenAI endpoint (ignore OPENAI_BASE_URL)")
    parser.add_argument("--gcal", action="store_true", help="Test GCal attendee lookup only (no transcript fetch)")
    args = parser.parse_args()

    if args.gcal:
        _run_gcal_test(args)
        return

    # --extract implies --correct, --correct implies --context
    if args.extract:
        args.correct = True
    load_context = args.context or args.correct

    client = create_transcript_client()
    transcript_text, attendees, metadata = fetch_transcript(args.page_id, client, verbose=args.verbose)

    # Load context from Notion DBs (needed for GCal name resolution + corrector + extractor)
    terminology = ""
    org_chart = ""
    cfg = None
    if load_context:
        from src.config import load_config
        from src.transcript_pipeline.context_loader import load_org_chart, load_terminology

        cfg = load_config()

        if cfg.terminology_db_id:
            terminology = load_terminology(client, cfg.terminology_db_id)
        if cfg.org_chart_db_id:
            org_chart = load_org_chart(client, cfg.org_chart_db_id)

    # Use GCal as authoritative attendee source (falls back to Notion)
    if metadata.get("title") and metadata.get("date"):
        from src.transcript_pipeline.gcal_attendees import get_gcal_attendees

        gcal_attendees = get_gcal_attendees(metadata["title"], metadata["date"])
        if gcal_attendees:
            attendees = [{"id": ga["email"], "name": ga["name"]} for ga in gcal_attendees]

    # Print meeting info
    if metadata.get("title"):
        print(f"=== MEETING: {metadata['title']} ({metadata.get('date', '?')}) ===")
    print("=== ATTENDEES ===")
    if attendees:
        for a in attendees:
            print(f"  - {a['name']}")
    else:
        print("  (none found)")

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
        # --openai forces the OpenAI endpoint (useful when OPENAI_BASE_URL points to Gemini)
        base_url = "https://api.openai.com/v1" if args.openai else cfg.openai_base_url

        corrector = TranscriptCorrector(
            api_key=cfg.openai_api_key,
            model=model,
            base_url=base_url,
        )

        print("Correcting transcript with", model, "...", file=sys.stderr)
        corrected = corrector.correct(transcript_text, terminology, org_chart, attendees)

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
                api_key=cfg.openai_api_key,
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
                    print()
                    print(f"  {i}. [{priority}] {title}  (confidence: {confidence})")
                    print(f"     Assignee: {assignee}")
                    print(f"     Due: {due}")
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


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
