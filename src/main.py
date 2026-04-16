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
from src.pipeline import run_inject_templates, run_sync

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

    setup_logging(config.log_level)

    logfire.configure(token=config.logfire_token, service_name="nzyme")
    logfire.instrument_openai()

    notion = NotionClient(auth=config.notion_api_token, notion_version="2026-03-11")
    client = NotionClientWrapper(notion)

    if args.watch:
        run_watch(config, client)
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
