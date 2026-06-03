"""Confidentiality gate in ``mirror_to_topic_dbs``.

The gate runs after route matching: a meeting that matched ≥1 Mirror-to-DB
rule is held back (and ``clone_or_merge`` never called) when it resolves to
confidential, returning ``MirrorStatus.SKIPPED_CONFIDENTIAL``.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.config import SyncConfig
from src.topic_mirror import mirror_to_topic_dbs
from src.topic_mirror.outcome import MirrorAction, MirrorStatus

_TARGET_DB_URL = "https://www.notion.so/dc0e537633cb4e8c9c2b97210878d7d2"


def _make_config() -> SyncConfig:
    return SyncConfig(
        notion_api_token="secret_abc",
        openai_api_key="sk-abc",
        team_tracker_db_id="db-tracker",
        merged_transcript_extraction_prompt_page_id="page-merged",
        topic_mirror_enabled=True,
        meeting_rules_db_id="rules-db",
    )


def _client_with_one_detail_route() -> MagicMock:
    """A client whose Meeting Rules query returns one active Mirror-to-DB
    rule matching ``Detail = "AI & Tech"``."""
    client = MagicMock()
    client.query_database.return_value = {
        "results": [
            {
                "id": "rule-1",
                "properties": {
                    "Match Property": {"type": "select", "select": {"name": "Detail"}},
                    "Match Value": {
                        "type": "rich_text",
                        "rich_text": [{"plain_text": "AI & Tech"}],
                    },
                    "Target DB": {"type": "url", "url": _TARGET_DB_URL},
                    "Active": {"type": "checkbox", "checkbox": True},
                    "Route": {"type": "title", "title": [{"plain_text": "AI & Tech"}]},
                    "Action": {"type": "select", "select": {"name": "Mirror to DB"}},
                },
            },
        ],
    }
    return client


def _source_page(confidential: str | None) -> dict:
    props: dict = {
        "Detail": {
            "type": "multi_select",
            "multi_select": [{"name": "AI & Tech"}],
        },
    }
    if confidential is not None:
        props["Confidential"] = {"type": "select", "select": {"name": confidential}}
    return {"id": "src-page", "properties": props}


def _run(monkeypatch, *, confidential, owner_default):
    """Run the orchestrator with clone_or_merge stubbed; return (outcome, mock)."""
    clone_mock = MagicMock(return_value=MirrorAction.CLONED)
    monkeypatch.setattr("src.topic_mirror.clone_or_merge", clone_mock)
    outcome = mirror_to_topic_dbs(
        config=_make_config(),
        client=_client_with_one_detail_route(),
        source_page=_source_page(confidential),
        metadata={"title": "Sprint sync", "date": "2026-06-01"},
        owner_user_id="user-1",
        owner_name="Santiago",
        owner_default_visibility=owner_default,
    )
    return outcome, clone_mock


class TestConfidentialityGate:
    def test_confidential_meeting_shared_default_is_held_back(self, monkeypatch):
        outcome, clone_mock = _run(
            monkeypatch, confidential="Confidential", owner_default="Shared",
        )
        assert outcome.status == MirrorStatus.SKIPPED_CONFIDENTIAL
        assert "AI & Tech" in outcome.detail  # names the route it was held from
        clone_mock.assert_not_called()

    def test_blank_meeting_private_default_is_held_back(self, monkeypatch):
        outcome, clone_mock = _run(
            monkeypatch, confidential=None, owner_default="Private",
        )
        assert outcome.status == MirrorStatus.SKIPPED_CONFIDENTIAL
        clone_mock.assert_not_called()

    def test_blank_meeting_shared_default_mirrors(self, monkeypatch):
        outcome, clone_mock = _run(
            monkeypatch, confidential=None, owner_default="Shared",
        )
        assert outcome.status == MirrorStatus.POSTED
        clone_mock.assert_called_once()

    def test_shareable_meeting_private_default_mirrors(self, monkeypatch):
        outcome, clone_mock = _run(
            monkeypatch, confidential="Shareable", owner_default="Private",
        )
        assert outcome.status == MirrorStatus.POSTED
        clone_mock.assert_called_once()

    def test_no_matching_route_stays_no_match_not_confidential(self, monkeypatch):
        """A confidential meeting that matches no rule is NO_MATCH, not a
        confidential skip — the gate only fires on would-be mirrors."""
        clone_mock = MagicMock(return_value=MirrorAction.CLONED)
        monkeypatch.setattr("src.topic_mirror.clone_or_merge", clone_mock)
        page = {
            "id": "src-page",
            "properties": {
                "Detail": {"type": "multi_select", "multi_select": [{"name": "Legal DD"}]},
                "Confidential": {"type": "select", "select": {"name": "Confidential"}},
            },
        }
        outcome = mirror_to_topic_dbs(
            config=_make_config(),
            client=_client_with_one_detail_route(),
            source_page=page,
            metadata={"title": "X", "date": "2026-06-01"},
            owner_user_id="user-1",
            owner_name="Santiago",
            owner_default_visibility="Private",
        )
        assert outcome.status == MirrorStatus.NO_MATCH
        clone_mock.assert_not_called()
