"""Entry point for the Nzyme AI-driven task extraction engine."""
from __future__ import annotations

import argparse
import sys

from notion_client import Client as NotionClient

from src.config import load_config
from src.utils.logger import setup_logging, get_logger
from src.notion_client_wrapper import NotionClientWrapper
from src.pipeline import run_sync

logger = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Nzyme — AI-driven task extraction from Notion meeting notes",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Log extracted tasks but don't write to Notion",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Set log level to DEBUG",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()

    if args.dry_run is not None:
        config = config.model_copy(update={"dry_run": args.dry_run})
    if args.verbose:
        config = config.model_copy(update={"log_level": "DEBUG"})

    setup_logging(config.log_level)
    logger.info("Starting Nzyme sync (dry_run=%s)", config.dry_run)

    notion = NotionClient(auth=config.notion_api_token)
    client = NotionClientWrapper(notion)

    try:
        run_sync(config, client)
    except Exception:
        logger.exception("Sync failed")
        sys.exit(1)

    logger.info("Sync complete")


if __name__ == "__main__":
    main()
