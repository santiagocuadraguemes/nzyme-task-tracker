"""Entry point for the Nzyme AI-driven task extraction engine."""
from __future__ import annotations

import argparse
import sys
import time

import logfire
from notion_client import Client as NotionClient

from src.config import SyncConfig, load_config
from src.utils.logger import setup_logging, get_logger
from src.notion_client_wrapper import NotionClientWrapper
from src.pipeline import run_inject_templates, run_sync, _archive_done_tasks

logger = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Nzyme — AI-driven task extraction from Notion meeting notes",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Run continuously: inject templates every WATCH_INTERVAL seconds, "
             "sync every SYNC_INTERVAL seconds",
    )
    parser.add_argument(
        "--inject-templates",
        action="store_true",
        help="Inject meeting note template into new pages (one-shot)",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Run the AI extraction pipeline (one-shot)",
    )
    parser.add_argument(
        "--archive",
        action="store_true",
        help="Run the Done-task archive sweep once (mirrors the weekly Sunday Lambda job).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Log actions but don't write to Notion",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Set log level to DEBUG",
    )
    parser.add_argument(
        "--db-id",
        metavar="NOTION_DB_ID",
        help="Process a single Meeting Notes DB (overrides Org Chart discovery). "
             "Equivalent to setting MEETING_NOTES_DB_ID.",
    )
    parser.add_argument(
        "--correction-model",
        metavar="MODEL",
        help="Override the transcript-correction model for this run. "
             "Provider is auto-detected from the prefix (gemini-* uses GEMINI_API_KEY, "
             "everything else uses OPENAI_API_KEY).",
    )
    parser.add_argument(
        "--extraction-model",
        metavar="MODEL",
        help="Override the task-extraction model for this run.",
    )
    parser.add_argument(
        "--classification-model",
        metavar="MODEL",
        help="Override the task-classification model for this run.",
    )

    auto_group = parser.add_mutually_exclusive_group()
    auto_group.add_argument(
        "--auto-extract-tasks",
        dest="auto_extract_tasks_override",
        action="store_const",
        const=True,
        default=None,
        help="Force the transcript pipeline (correct → extract → classify) "
             "for every page in this run, ignoring the per-member "
             "`Auto-extract Tasks` flag on the Org Chart.",
    )
    auto_group.add_argument(
        "--no-auto-extract-tasks",
        dest="auto_extract_tasks_override",
        action="store_const",
        const=False,
        help="Force the literal-notes path (verbatim titles, deterministic "
             "assignees) for every page in this run, ignoring the per-member "
             "`Auto-extract Tasks` flag on the Org Chart.",
    )
    return parser.parse_args()


def run_watch(config: SyncConfig, client: NotionClientWrapper) -> None:
    """Run continuously, injecting templates and syncing on intervals."""
    logger.info(
        "Watch mode started (templates every %ds, sync every %ds). Ctrl+C to stop.",
        config.watch_interval,
        config.sync_interval,
    )
    last_sync = 0.0

    try:
        while True:
            # Template injection every tick
            try:
                run_inject_templates(config, client)
            except Exception:
                logger.exception("Template injection failed — will retry next tick")

            # Sync extraction on interval
            now = time.monotonic()
            if now - last_sync >= config.sync_interval:
                try:
                    logger.info("Running sync cycle")
                    run_sync(config, client)
                except Exception:
                    logger.exception("Sync failed — will retry next interval")
                last_sync = time.monotonic()

            time.sleep(config.watch_interval)
    except KeyboardInterrupt:
        logger.info("Watch mode stopped")


def main() -> None:
    args = parse_args()
    config = load_config()

    if args.dry_run is not None:
        config = config.model_copy(update={"dry_run": args.dry_run})
    if args.verbose:
        config = config.model_copy(update={"log_level": "DEBUG"})
    if args.db_id:
        config = config.model_copy(update={"meeting_notes_db_id": args.db_id})
    if args.correction_model:
        config = config.model_copy(update={"correction_model": args.correction_model})
    if args.extraction_model:
        config = config.model_copy(update={"extraction_model": args.extraction_model})
    if args.classification_model:
        config = config.model_copy(update={"classification_model": args.classification_model})
    if args.auto_extract_tasks_override is not None:
        config = config.model_copy(
            update={"auto_extract_tasks_override": args.auto_extract_tasks_override},
        )

    setup_logging(config.log_level)

    logfire.configure(token=config.logfire_token, service_name="nzyme")
    logfire.instrument_openai()

    notion = NotionClient(auth=config.notion_api_token, notion_version="2026-03-11")
    client = NotionClientWrapper(notion)

    if args.watch:
        run_watch(config, client)
        return

    if args.archive:
        logger.info("Starting archive sweep (dry_run=%s)", config.dry_run)
        try:
            archived = _archive_done_tasks(
                client,
                config.team_tracker_db_id,
                config.task_archive_db_id,
                grace_days=5,
                dry_run=config.dry_run,
            )
            logger.info("Archive sweep complete: archived=%d", archived)
        except Exception:
            logger.exception("Archive sweep failed")
            sys.exit(1)
        return

    # One-shot mode: if neither flag is set, run both (backwards compatible)
    run_templates = args.inject_templates
    run_extraction = args.sync
    if not run_templates and not run_extraction:
        run_templates = True
        run_extraction = True

    if run_templates:
        logger.info("Starting template injection (dry_run=%s)", config.dry_run)
        try:
            run_inject_templates(config, client)
        except Exception:
            logger.exception("Template injection failed")
            if not run_extraction:
                sys.exit(1)

    if run_extraction:
        logger.info("Starting sync (dry_run=%s)", config.dry_run)
        try:
            run_sync(config, client)
        except Exception:
            logger.exception("Sync failed")
            sys.exit(1)

    logger.info("Done")


if __name__ == "__main__":
    main()
