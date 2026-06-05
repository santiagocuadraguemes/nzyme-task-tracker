"""Tests for the claim-before-post wiring in ``_mirror_meeting_to_affinity``.

The pipeline must win the Supabase claim before calling
``write_to_affinity``, and record the outcome back afterwards. All imports
inside ``_mirror_meeting_to_affinity`` are function-local, so patches target
the source modules (``src.fundraising.state``, ``src.fundraising``,
``src.topic_mirror.route_registry``).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.config import SyncConfig
from src.fundraising.outcome import FundraisingOutcome, FundraisingStatus
from src.pipeline import _mirror_meeting_to_affinity
from src.topic_mirror.route_registry import ACTION_AFFINITY_LP_FUNNEL, Route

PAGE_HEX = "a" * 32
DB_HEX = "b" * 32
PAGE_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

_LP_ROUTE = Route(
    match_property="Macro Work Block",
    match_value="Investor Relations & Fundraising",
    target_db_id="",
    label="LP Funnel rule",
    action=ACTION_AFFINITY_LP_FUNNEL,
)


def _make_config(**overrides) -> SyncConfig:
    defaults = {
        "notion_api_token": "secret_abc",
        "openai_api_key": "sk-abc",
        "meeting_notes_db_id": "db-meetings",
        "team_tracker_db_id": "db-tracker",
        "merged_transcript_extraction_prompt_page_id": "page-merged",
        "fundraising_branch_enabled": True,
        "affinity_api_key": "aff-key",
        "meeting_rules_db_id": "rules-db",
    }
    defaults.update(overrides)
    return SyncConfig(**defaults)


def _run_branch(config: SyncConfig) -> None:
    _mirror_meeting_to_affinity(
        config=config,
        client=MagicMock(),
        page={"properties": {}},
        metadata={"title": "LP X update", "date": "2026-06-04", "url": "https://notion.so/p"},
        attendees=[],
        page_id=PAGE_HEX,
        short_id=PAGE_HEX[:16],
        db_owner="Santiago",
        db_id=DB_HEX,
    )


@patch("src.fundraising.state.record_outcome")
@patch("src.fundraising.state.claim_post")
@patch("src.fundraising.write_to_affinity")
@patch("src.topic_mirror.route_registry.match_routes", return_value=[_LP_ROUTE])
@patch("src.topic_mirror.route_registry.load_routes", return_value=[_LP_ROUTE])
def test_claim_won_posts_and_records(
    mock_load, mock_match, mock_write, mock_claim, mock_record,
):
    mock_claim.return_value = True
    outcome = FundraisingOutcome(
        status=FundraisingStatus.POSTED,
        detail="posted_to=[123]",
        opportunity_ids=[123],
    )
    mock_write.return_value = outcome

    _run_branch(_make_config())

    mock_claim.assert_called_once()
    assert mock_claim.call_args.kwargs["page_id"] == PAGE_UUID
    assert mock_claim.call_args.kwargs["owner_name"] == "Santiago"
    mock_write.assert_called_once()
    # write_to_affinity keeps the hex page id (Notion API form).
    assert mock_write.call_args.kwargs["page_id"] == PAGE_HEX
    mock_record.assert_called_once()
    assert mock_record.call_args.kwargs["page_id"] == PAGE_UUID
    assert mock_record.call_args.kwargs["outcome"] is outcome
    assert mock_record.call_args.kwargs["opportunity_ids"] == [123]


@patch("src.fundraising.state.record_outcome")
@patch("src.fundraising.state.claim_post")
@patch("src.fundraising.write_to_affinity")
@patch("src.topic_mirror.route_registry.match_routes", return_value=[_LP_ROUTE])
@patch("src.topic_mirror.route_registry.load_routes", return_value=[_LP_ROUTE])
def test_claim_lost_skips_post(
    mock_load, mock_match, mock_write, mock_claim, mock_record,
):
    mock_claim.return_value = False

    _run_branch(_make_config())

    mock_claim.assert_called_once()
    mock_write.assert_not_called()
    mock_record.assert_not_called()


@patch("src.fundraising.state.claim_post")
@patch("src.fundraising.write_to_affinity")
def test_dry_run_never_touches_claim(mock_write, mock_claim):
    _run_branch(_make_config(dry_run=True))

    mock_claim.assert_not_called()
    mock_write.assert_not_called()


@patch("src.fundraising.state.claim_post")
@patch("src.fundraising.write_to_affinity")
@patch("src.topic_mirror.route_registry.match_routes", return_value=[])
@patch("src.topic_mirror.route_registry.load_routes", return_value=[_LP_ROUTE])
def test_no_rule_match_never_touches_claim(
    mock_load, mock_match, mock_write, mock_claim,
):
    _run_branch(_make_config())

    mock_claim.assert_not_called()
    mock_write.assert_not_called()
