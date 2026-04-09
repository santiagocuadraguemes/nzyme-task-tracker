"""CLI entry point: python -m src.transcript_pipeline <page_id> [--verbose] [--context] [--correct] [--extract]"""

from __future__ import annotations

import argparse
import sys

import logfire

from src.transcript_pipeline.fetch_transcript import fetch_transcript
from src.transcript_pipeline.transcript_client import create_transcript_client


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
    args = parser.parse_args()

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

    # Enrich attendees from Google Calendar
    if metadata.get("title") and metadata.get("date"):
        from src.transcript_pipeline.gcal_attendees import get_gcal_attendees

        # Parse org chart names for email→name resolution
        org_names = [
            line.split("Person: ")[1].split(" — ")[0]
            for line in org_chart.splitlines()
            if line.startswith("Person: ")
        ] if org_chart else []

        gcal_attendees = get_gcal_attendees(
            metadata["title"], metadata["date"], org_chart_names=org_names
        )
        if gcal_attendees:
            notion_names = {a["name"].lower() for a in attendees}
            for ga in gcal_attendees:
                if ga["name"].lower() not in notion_names:
                    attendees.append({"id": ga["email"], "name": ga["name"]})
                    print(f"  + Added from Google Calendar: {ga['name']}", file=sys.stderr)
        else:
            print("WARNING: No attendees found from Google Calendar.", file=sys.stderr)

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
