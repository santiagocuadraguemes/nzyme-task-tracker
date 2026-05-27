"""Run the merged extractor under ONE candidate schema on the pinned corpus.

Used to validate an output-token reduction with real Gemini calls AFTER the
offline estimator (``estimate_output_savings.py``) ranks candidates cheaply.

Usage:
    ../venv/Scripts/python scripts/compare_candidate.py \
        --candidate no-sr \
        --pages-file pages-corpus.txt \
        --out cand-no-sr.json

Output is in the same shape as ``shadow-diff.json`` but only contains the
``merged`` block (no legacy path). Pair it with ``compare_runs.py`` to
diff against the baseline.

Free-tier note: this runs ONE real Gemini call per page. For a 10-page
corpus that's 10 calls — well under daily free-tier quotas.

Available candidates (see src/transcript_pipeline/schemas.py):
    baseline    — current schema, control run
    no-sr       — drop speaker_reasoning per task
    no-scratch  — drop domain_corrections + speaker_resolutions
    combined    — drop sr + a + scratch fields
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# Enable prompt+completion content capture on the Google Gen AI OTel
# instrumentation (off by default for PII reasons). Must be set before
# `logfire.instrument_google_genai()` is called below.
os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")

import logfire
from dotenv import load_dotenv
from notion_client import Client as NotionClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

from src.config import load_config  # noqa: E402
from src.notion_client_wrapper import NotionClientWrapper  # noqa: E402
from src.transcript_pipeline.context_loader import (  # noqa: E402
    build_enriched_attendee_str,
    load_org_chart,
    load_org_chart_rows,
    load_terminology,
)
from src.transcript_pipeline.fetch_transcript import fetch_transcript  # noqa: E402
from src.transcript_pipeline.schemas import CANDIDATE_SCHEMAS  # noqa: E402
from src.transcript_pipeline.task_extractor import (  # noqa: E402
    TaskExtractor,
    set_response_schema_override,
)
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

            created_by_id = (
                (metadata.get("created_by") or {}).get("id", "")
                or metadata.get("created_by_id", "")
            )
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
            logging.warning(
                "GCal lookup failed for %s — using Notion attendees",
                page_id, exc_info=True,
            )

    if not attendees and governance_attendees:
        attendees = [{**g, "email": None} for g in governance_attendees]
    return attendees


def _make_client() -> NotionClientWrapper:
    import os
    notion = NotionClient(auth=os.environ["NOTION_API_TOKEN"], notion_version="2026-03-11")
    return NotionClientWrapper(notion)


def run_one_page(client, cfg, page_id, terminology, org_chart_text, org_chart_rows, system_prompt):
    out = {
        "page_id": page_id,
        "meeting_title": None,
        "meeting_date": None,
        "transcript_chars": 0,
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

        model = cfg.extraction_model or cfg.gemini_model
        api_key = cfg.gemini_api_key or cfg.openai_api_key
        base_url = cfg.gemini_base_url

        tracker = get_tracker()
        extractor = TaskExtractor(api_key=api_key, model=model, base_url=base_url)
        marker = len(tracker.records) if tracker else 0
        t0 = time.perf_counter()
        tasks = extractor.extract_from_raw(
            transcript_text, attendees,
            system_prompt=system_prompt,
            org_chart=org_chart_text,
            terminology=terminology,
            meeting_title=metadata.get("title", ""),
            meeting_date=metadata.get("date", ""),
            enriched_attendee_str=enriched_attendee_str,
            notes_text=notes_text,
        )
        out["merged"] = {
            "tasks": tasks,
            "elapsed_s": round(time.perf_counter() - t0, 2),
            **_usage_since(tracker, marker),
            "raw_data": extractor._last_raw_data,
        }
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        logging.exception("Page %s failed", page_id)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--candidate", required=True, choices=list(CANDIDATE_SCHEMAS),
                        help="Schema variant to test")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--pages", nargs="+", help="Notion page IDs")
    src.add_argument("--pages-file", type=Path, help="File with one page ID per line")
    parser.add_argument("--out", type=Path, required=True, help="Output JSON path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    page_ids: list[str] = (
        list(args.pages) if args.pages
        else [ln.strip() for ln in args.pages_file.read_text().splitlines()
              if ln.strip() and not ln.startswith("#")]
    )
    if not page_ids:
        sys.exit("ERROR: no page IDs provided")

    cfg = load_config()
    from src.utils.llm_logging import allow_gen_ai_content
    logfire.configure(
        token=cfg.logfire_token,
        service_name="nzyme-candidate-cmp",
        scrubbing=logfire.ScrubbingOptions(callback=allow_gen_ai_content),
    )
    logfire.instrument_openai()
    logfire.instrument_google_genai()
    start_tracking()

    # Wire the candidate schema. Production paths are untouched — this
    # only affects calls made from this script's process.
    set_response_schema_override(CANDIDATE_SCHEMAS[args.candidate])
    logging.info("Candidate schema override: %s", args.candidate)

    client = _make_client()
    terminology = load_terminology(client, cfg.terminology_db_id) if cfg.terminology_db_id else ""
    org_chart_text = load_org_chart(client, cfg.org_chart_db_id) if cfg.org_chart_db_id else ""
    org_chart_rows = load_org_chart_rows(client, cfg.org_chart_db_id) if cfg.org_chart_db_id else []

    # System prompt is required by extract_from_raw — load from Notion playbook.
    from src.pipeline import _fetch_page_text
    system_prompt = _fetch_page_text(
        client, cfg.merged_transcript_extraction_prompt_page_id,
    )
    if not system_prompt.strip():
        sys.exit(
            "ERROR: Merged transcript extraction prompt page is empty. "
            "Populate it in Notion before running compare_candidate.py."
        )

    results: list[dict] = []
    for i, pid in enumerate(page_ids, 1):
        logging.info("[%d/%d] %s", i, len(page_ids), pid)
        results.append(
            run_one_page(client, cfg, pid, terminology, org_chart_text, org_chart_rows, system_prompt)
        )

    args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\nWrote {len(results)} entries to {args.out}")

    ok = [r for r in results if r["error"] is None and r["merged"]]
    if ok:
        n = len(ok)
        total_tasks = sum(len(r["merged"]["tasks"]) for r in ok)
        out_tokens = sum(r["merged"].get("output_tokens", 0) for r in ok)
        print(
            f"  candidate={args.candidate}: {total_tasks} tasks, "
            f"{out_tokens:,} output tokens total ({out_tokens / n:.0f} avg/page, "
            f"{out_tokens / max(total_tasks, 1):.0f} avg/task)"
        )
    fail = [r for r in results if r["error"]]
    if fail:
        print(f"  {len(fail)} page(s) failed — see entries with non-null `error`")


if __name__ == "__main__":
    main()
