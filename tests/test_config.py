import os
from unittest.mock import patch

import pytest

from src.config import SyncConfig, load_config


class TestSyncConfig:
    def test_valid_config(self):
        config = SyncConfig(
            notion_api_token="secret_abc",
            openai_api_key="sk-abc",
            meeting_notes_db_id="db-meetings",
            team_tracker_db_id="db-tracker",
        )
        assert config.openai_model == "gpt-5-mini"
        assert config.gemini_model == "gemini-3-flash-preview"
        assert config.gemini_base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
        assert config.buffer_hours == 2
        assert config.dry_run is False

    def test_missing_required_field_raises(self):
        with pytest.raises(Exception):
            SyncConfig(
                notion_api_token="secret_abc",
                # missing openai_api_key
                meeting_notes_db_id="db-meetings",
                team_tracker_db_id="db-tracker",
            )


class TestLoadConfig:
    @patch.dict(os.environ, {
        "NOTION_API_TOKEN": "secret_abc",
        "OPENAI_API_KEY": "sk-abc",
        "MEETING_NOTES_DB_ID": "db-meetings",
        "TEAM_TRACKER_DB_ID": "db-tracker",
        "BUFFER_HOURS": "3",
        "DRY_RUN": "true",
    }, clear=False)
    def test_load_from_env(self):
        config = load_config()
        assert config.notion_api_token == "secret_abc"
        assert config.openai_api_key == "sk-abc"
        assert config.buffer_hours == 3
        assert config.dry_run is True
