"""Factory for creating a Notion client with API version 2026-03-11.

The main codebase uses 2025-09-03; the transcript pipeline needs 2026-03-11
for meeting_notes block support. This module creates a separate client instance.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from notion_client import Client as NotionClient

from src.notion_client_wrapper import NotionClientWrapper

TRANSCRIPT_API_VERSION = "2026-03-11"


def create_transcript_client() -> NotionClientWrapper:
    """Create a NotionClientWrapper using API version 2026-03-11."""
    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

    notion = NotionClient(
        auth=os.environ["NOTION_API_TOKEN"],
        notion_version=TRANSCRIPT_API_VERSION,
    )
    return NotionClientWrapper(notion)
