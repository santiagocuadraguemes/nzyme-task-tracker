"""Configuration for the Nzyme AI-driven task extraction engine."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field


class SyncConfig(BaseModel):
    """Validated runtime configuration."""

    notion_api_token: str = Field(..., description="Notion integration token")
    openai_api_key: str = Field(..., description="OpenAI API key")
    openai_model: str = Field("gpt-4.1", description="OpenAI model name")
    # TEMPORARY: base_url allows using Gemini's OpenAI-compatible endpoint for testing.
    # Remove once we switch back to OpenAI (target model: gpt-5-mini).
    openai_base_url: str | None = Field(None, description="Custom base URL for OpenAI-compatible APIs")
    meeting_notes_db_id: str = Field(..., description="Meeting Notes DB ID")
    team_tracker_db_id: str = Field(..., description="Team Task Tracker DB ID")
    playbook_page_id: str = Field(..., description="Playbook Notion page ID")
    buffer_hours: int = Field(2, description="Hours to wait after meeting date")
    logfire_token: str | None = Field(None, description="Logfire write token for LLM observability")
    log_level: str = Field("INFO", description="Logging level")
    dry_run: bool = Field(False, description="Log tasks but don't write to Notion")


def load_config() -> SyncConfig:
    """Build a validated SyncConfig from environment variables."""
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    return SyncConfig(
        notion_api_token=os.environ["NOTION_API_TOKEN"],
        openai_api_key=os.environ["OPENAI_API_KEY"],
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1"),
        openai_base_url=os.getenv("OPENAI_BASE_URL"),  # TEMPORARY: for Gemini testing
        meeting_notes_db_id=os.environ["MEETING_NOTES_DB_ID"],
        team_tracker_db_id=os.environ["TEAM_TRACKER_DB_ID"],
        playbook_page_id=os.environ["PLAYBOOK_PAGE_ID"],
        logfire_token=os.getenv("LOGFIRE_TOKEN"),
        buffer_hours=int(os.getenv("BUFFER_HOURS", "2")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        dry_run=os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes"),
    )
