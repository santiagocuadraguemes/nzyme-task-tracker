"""Tests for the fundraising → Affinity orchestrator state machine.

Verifies that ``write_to_affinity`` returns the right ``FundraisingOutcome``
for every reachable branch, including the multi-LP path. Network is fully
mocked.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.affinity_client import AffinityError
from src.config import SyncConfig
from src.fundraising import _strip_template_scaffolding, write_to_affinity
from src.fundraising.outcome import FundraisingStatus


class TestStripTemplateScaffolding:
    def test_untouched_template_yields_empty(self):
        raw = (
            "## Action Items\n"
            "- [List action items here - tag the owner when possible]\n"
            "- \n"
            "### Notes"
        )
        assert _strip_template_scaffolding(raw) == ""

    def test_empty_input_yields_empty(self):
        assert _strip_template_scaffolding("") == ""

    def test_real_action_items_preserved_verbatim(self):
        raw = (
            "## Action Items\n"
            "- [ ] Send NDA to Francesco\n"
            "### Notes\n"
            "They want Portugal exposure."
        )
        out = _strip_template_scaffolding(raw)
        assert "- [ ] Send NDA to Francesco" in out
        assert "They want Portugal exposure." in out
        # Headings dropped.
        assert "Action Items" not in out
        assert "### Notes" not in out

    def test_freeform_notes_without_markers_preserved(self):
        assert _strip_template_scaffolding("Just a quick note.") == "Just a quick note."


def _make_config(**overrides) -> SyncConfig:
    defaults = {
        "notion_api_token": "secret_abc",
        "openai_api_key": "sk-abc",
        "meeting_notes_db_id": "db-meetings",
        "team_tracker_db_id": "db-tracker",
        "merged_transcript_extraction_prompt_page_id": "page-merged",
        "meeting_template_page_id": "tmpl-123",
        "fundraising_branch_enabled": True,
        "affinity_api_key": "aff-key",
    }
    defaults.update(overrides)
    return SyncConfig(**defaults)


def _basic_metadata():
    return {
        "title": "LP X update",
        "date": "2026-04-17",
        "created_by": {"id": "u1", "name": "Santiago"},
        "url": "https://www.notion.so/p",
    }


def _make_notion_client(*, ai_summary: str = "", user_notes: str = ""):
    """Build a MagicMock NotionClientWrapper that returns canned summary/notes."""
    nc = MagicMock()
    nc.get_page.return_value = {
        "properties": {
            "AI Summary": {
                "type": "rich_text",
                "rich_text": [{"plain_text": ai_summary}] if ai_summary else [],
            }
        }
    }
    # Empty meeting_notes block list is enough — the orchestrator's
    # block-fetch path is patched out in tests that need user_notes.
    nc.get_block_children.return_value = []
    nc._user_notes = user_notes  # used by tests that patch fetch_notes_text
    return nc


def test_returns_skipped_no_external_attendees_when_only_kibo_emails():
    config = _make_config()
    outcome = write_to_affinity(
        config=config,
        tasks=[],
        metadata=_basic_metadata(),
        attendees=[
            {"id": "santiago@kiboventures.com", "name": "Santiago"},
            {"id": "notion-uid-no-email", "name": "Notion User"},
        ],
        notion_url="https://notion.so/p",
        page_id="page-1",
        notion_client=_make_notion_client(),
    )
    assert outcome.status == FundraisingStatus.SKIPPED_NO_EXTERNAL_ATTENDEES
    assert "external" in outcome.detail.lower()


def test_failed_when_no_api_key_configured():
    config = _make_config(affinity_api_key="")
    outcome = write_to_affinity(
        config=config,
        tasks=[],
        metadata=_basic_metadata(),
        attendees=[{"id": "lp@example.com", "name": "LP"}],
        notion_url="https://notion.so/p",
        page_id="page-1",
        notion_client=_make_notion_client(),
    )
    assert outcome.status == FundraisingStatus.FAILED_API_ERROR
    assert "AFFINITY_API_KEY" in outcome.detail


@patch("src.fundraising.AffinityClient")
def test_skipped_no_lp_match(mock_client_cls):
    """External attendees present but none on the LP Funnel."""
    config = _make_config()
    fake = MagicMock()
    fake.list_list_entries.return_value = [
        {"id": 999, "entity": {"id": 900}},
    ]
    fake.search_persons.return_value = []  # nobody matches
    mock_client_cls.return_value.__enter__.return_value = fake

    outcome = write_to_affinity(
        config=config,
        tasks=[],
        metadata=_basic_metadata(),
        attendees=[{"id": "lp@example.com", "name": "LP"}],
        notion_url="https://notion.so/p",
        page_id="page-1",
        notion_client=_make_notion_client(),
    )
    assert outcome.status == FundraisingStatus.SKIPPED_NO_LP_MATCH


@patch("src.fundraising.fetch_notes_text")
@patch("src.fundraising.find_meeting_notes_block")
@patch("src.fundraising.AffinityClient")
def test_posted_when_single_lp_match_and_create_note_succeeds(
    mock_client_cls, mock_find_mn, mock_fetch_notes,
):
    config = _make_config()
    fake = MagicMock()
    fake.list_list_entries.return_value = [
        {"id": 1001, "entity": {"id": 900}},
    ]
    fake.search_persons.return_value = [{"id": 1, "opportunity_ids": [900]}]
    mock_client_cls.return_value.__enter__.return_value = fake
    mock_find_mn.return_value = {"meeting_notes": {"children": {}}}
    mock_fetch_notes.return_value = "User wrote some notes here."

    outcome = write_to_affinity(
        config=config,
        tasks=[],
        metadata=_basic_metadata(),
        attendees=[{"id": "lp@example.com", "name": "LP"}],
        notion_url="https://notion.so/p",
        page_id="page-1",
        notion_client=_make_notion_client(ai_summary="Notion's auto summary."),
    )
    assert outcome.status == FundraisingStatus.POSTED
    assert "900" in outcome.detail  # opportunity id appears in detail
    # Composed body contains both sections
    assert "User wrote some notes here." in outcome.summary
    assert "Notion's auto summary." in outcome.summary
    assert "Manual notes:" in outcome.summary
    assert "Summary:" in outcome.summary
    fake.create_note.assert_called_once()


@patch("src.fundraising.fetch_notes_text")
@patch("src.fundraising.find_meeting_notes_block")
@patch("src.fundraising.AffinityClient")
def test_posted_when_multiple_lps_in_meeting(
    mock_client_cls, mock_find_mn, mock_fetch_notes,
):
    """Both LPs in the funnel get the same note attached."""
    config = _make_config()
    fake = MagicMock()
    fake.list_list_entries.return_value = [
        {"id": 1001, "entity": {"id": 900}},
        {"id": 1002, "entity": {"id": 901}},
    ]

    def persons_side_effect(term):
        if term == "a@fundx.com":
            return [{"id": 1, "opportunity_ids": [900]}]
        return [{"id": 2, "opportunity_ids": [901]}]

    fake.search_persons.side_effect = persons_side_effect
    mock_client_cls.return_value.__enter__.return_value = fake
    mock_find_mn.return_value = {"meeting_notes": {"children": {}}}
    mock_fetch_notes.return_value = ""

    outcome = write_to_affinity(
        config=config,
        tasks=[],
        metadata=_basic_metadata(),
        attendees=[
            {"id": "a@fundx.com", "name": "A"},
            {"id": "b@fundy.com", "name": "B"},
        ],
        notion_url="https://notion.so/p",
        page_id="page-1",
        notion_client=_make_notion_client(ai_summary="Cross-LP intro."),
    )
    assert outcome.status == FundraisingStatus.POSTED
    assert fake.create_note.call_count == 2
    # Detail records both opportunity ids
    assert "900" in outcome.detail
    assert "901" in outcome.detail


@patch("src.fundraising.fetch_notes_text")
@patch("src.fundraising.find_meeting_notes_block")
@patch("src.fundraising.AffinityClient")
def test_failed_api_error_when_create_note_raises(
    mock_client_cls, mock_find_mn, mock_fetch_notes,
):
    config = _make_config()
    fake = MagicMock()
    fake.list_list_entries.return_value = [
        {"id": 1001, "entity": {"id": 900}},
    ]
    fake.search_persons.return_value = [{"id": 1, "opportunity_ids": [900]}]
    fake.create_note.side_effect = AffinityError(500, "boom", "/notes")
    mock_client_cls.return_value.__enter__.return_value = fake
    mock_find_mn.return_value = {"meeting_notes": {"children": {}}}
    mock_fetch_notes.return_value = ""

    outcome = write_to_affinity(
        config=config,
        tasks=[],
        metadata=_basic_metadata(),
        attendees=[{"id": "lp@example.com", "name": "LP"}],
        notion_url="https://notion.so/p",
        page_id="page-1",
        notion_client=_make_notion_client(ai_summary="x"),
    )
    assert outcome.status == FundraisingStatus.FAILED_API_ERROR
    assert "boom" in outcome.detail


@patch("src.fundraising.fetch_notes_text")
@patch("src.fundraising.find_meeting_notes_block")
@patch("src.fundraising.AffinityClient")
def test_failed_when_one_of_many_lps_fails(
    mock_client_cls, mock_find_mn, mock_fetch_notes,
):
    """Partial failure → outcome=Failed; retry sweep re-runs the whole batch."""
    config = _make_config()
    fake = MagicMock()
    fake.list_list_entries.return_value = [
        {"id": 1001, "entity": {"id": 900}},
        {"id": 1002, "entity": {"id": 901}},
    ]

    def persons_side_effect(term):
        if term == "a@fundx.com":
            return [{"id": 1, "opportunity_ids": [900]}]
        return [{"id": 2, "opportunity_ids": [901]}]

    fake.search_persons.side_effect = persons_side_effect

    def flaky_note(content, content_type, opportunity_ids, **kwargs):
        if opportunity_ids[0] == 901:
            raise AffinityError(500, "boom", "/notes")
        return {"id": 7000}

    fake.create_note.side_effect = flaky_note
    mock_client_cls.return_value.__enter__.return_value = fake
    mock_find_mn.return_value = {"meeting_notes": {"children": {}}}
    mock_fetch_notes.return_value = ""

    outcome = write_to_affinity(
        config=config,
        tasks=[],
        metadata=_basic_metadata(),
        attendees=[
            {"id": "a@fundx.com", "name": "A"},
            {"id": "b@fundy.com", "name": "B"},
        ],
        notion_url="https://notion.so/p",
        page_id="page-1",
        notion_client=_make_notion_client(ai_summary="x"),
    )
    assert outcome.status == FundraisingStatus.FAILED_API_ERROR
    # Detail records which opportunity succeeded and which failed
    assert "900" in outcome.detail
    assert "901" in outcome.detail


@patch("src.fundraising.blocks_to_text")
@patch("src.fundraising.fetch_notes_text")
@patch("src.fundraising.find_meeting_notes_block")
@patch("src.fundraising.AffinityClient")
def test_transcript_included_when_rule_asks_for_it(
    mock_client_cls, mock_find_mn, mock_fetch_notes, mock_blocks_to_text,
):
    """include_transcript=True → raw transcript fetched from the
    meeting_notes block and rendered in the posted note."""
    config = _make_config()
    fake = MagicMock()
    fake.list_list_entries.return_value = [
        {"id": 1001, "entity": {"id": 900}},
    ]
    fake.search_persons.return_value = [{"id": 1, "opportunity_ids": [900]}]
    mock_client_cls.return_value.__enter__.return_value = fake
    mock_find_mn.return_value = {
        "meeting_notes": {"children": {"transcript_block_id": "tb-1"}},
    }
    mock_fetch_notes.return_value = ""
    mock_blocks_to_text.return_value = "Speaker 1: hola\nSpeaker 2: buenas"

    nc = _make_notion_client(ai_summary="Resumen.")
    nc.get_block_children.side_effect = (
        lambda bid: [{"type": "paragraph"}] if bid == "tb-1" else []
    )

    outcome = write_to_affinity(
        config=config,
        tasks=[],
        metadata=_basic_metadata(),
        attendees=[{"id": "lp@example.com", "name": "LP"}],
        notion_url="https://notion.so/p",
        page_id="page-1",
        notion_client=nc,
        include_transcript=True,
    )
    assert outcome.status == FundraisingStatus.POSTED
    content = fake.create_note.call_args.kwargs["content"]
    assert "Full transcript" in content
    assert "Speaker 1: hola" in content


@patch("src.fundraising.fetch_notes_text")
@patch("src.fundraising.find_meeting_notes_block")
@patch("src.fundraising.AffinityClient")
def test_transcript_rule_degrades_when_page_has_no_transcript(
    mock_client_cls, mock_find_mn, mock_fetch_notes,
):
    """Transcription paused/not processed → note still posts, without the
    Full transcript section."""
    config = _make_config()
    fake = MagicMock()
    fake.list_list_entries.return_value = [
        {"id": 1001, "entity": {"id": 900}},
    ]
    fake.search_persons.return_value = [{"id": 1, "opportunity_ids": [900]}]
    mock_client_cls.return_value.__enter__.return_value = fake
    # No transcript_block_id key → transcription disabled.
    mock_find_mn.return_value = {"meeting_notes": {"children": {}}}
    mock_fetch_notes.return_value = ""

    outcome = write_to_affinity(
        config=config,
        tasks=[],
        metadata=_basic_metadata(),
        attendees=[{"id": "lp@example.com", "name": "LP"}],
        notion_url="https://notion.so/p",
        page_id="page-1",
        notion_client=_make_notion_client(ai_summary="x"),
        include_transcript=True,
    )
    assert outcome.status == FundraisingStatus.POSTED
    content = fake.create_note.call_args.kwargs["content"]
    assert "Full transcript" not in content


@patch("src.fundraising.blocks_to_text")
@patch("src.fundraising.fetch_notes_text")
@patch("src.fundraising.find_meeting_notes_block")
@patch("src.fundraising.AffinityClient")
def test_rejected_transcript_note_posts_fallback_and_flags_detail(
    mock_client_cls, mock_find_mn, mock_fetch_notes, mock_blocks_to_text,
):
    """Failing handler end-to-end: full note rejected → fallback posted →
    outcome=Posted with transcript_omitted_for in the detail."""
    config = _make_config()
    fake = MagicMock()
    fake.list_list_entries.return_value = [
        {"id": 1001, "entity": {"id": 900}},
    ]
    fake.search_persons.return_value = [{"id": 1, "opportunity_ids": [900]}]

    def reject_full(content, content_type, opportunity_ids, **kwargs):
        if "Speaker 1" in content:
            raise AffinityError(413, "payload too large", "/notes")
        return {"id": 9000}

    fake.create_note.side_effect = reject_full
    mock_client_cls.return_value.__enter__.return_value = fake
    mock_find_mn.return_value = {
        "meeting_notes": {"children": {"transcript_block_id": "tb-1"}},
    }
    mock_fetch_notes.return_value = ""
    mock_blocks_to_text.return_value = "Speaker 1: hola"

    nc = _make_notion_client(ai_summary="x")
    nc.get_block_children.side_effect = (
        lambda bid: [{"type": "paragraph"}] if bid == "tb-1" else []
    )

    outcome = write_to_affinity(
        config=config,
        tasks=[],
        metadata=_basic_metadata(),
        attendees=[{"id": "lp@example.com", "name": "LP"}],
        notion_url="https://notion.so/p",
        page_id="page-1",
        notion_client=nc,
        include_transcript=True,
    )
    assert outcome.status == FundraisingStatus.POSTED
    assert "transcript_omitted_for=[900]" in outcome.detail
    assert fake.create_note.call_count == 2


@patch("src.fundraising.AffinityClient")
def test_unexpected_exception_caught_as_failed(mock_client_cls):
    """A generic exception (e.g. JSON decode) shouldn't escape."""
    config = _make_config()
    mock_client_cls.return_value.__enter__.side_effect = RuntimeError("totally unexpected")

    outcome = write_to_affinity(
        config=config,
        tasks=[],
        metadata=_basic_metadata(),
        attendees=[{"id": "lp@example.com", "name": "LP"}],
        notion_url="https://notion.so/p",
        page_id="page-1",
        notion_client=_make_notion_client(),
    )
    assert outcome.status == FundraisingStatus.FAILED_API_ERROR
    assert "RuntimeError" in outcome.detail
    assert "totally unexpected" in outcome.detail
