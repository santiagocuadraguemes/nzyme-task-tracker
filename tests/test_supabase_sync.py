"""Tests for the Notion → Supabase mirror sync orchestration.

Covers the 2026-06-04 fixes: the registry now includes inactive members
(fundraising meetings live in partners' DBs), GCal attendee resolution runs
for every meeting, and a None attendee_emails never overwrites previously
stored emails.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.meeting_db_registry import MeetingDB
from src.supabase_sync import _sync_db, run_full, run_incremental

_DB = MeetingDB(db_id="b" * 32, owner_name="Santiago", owner_email="s@kibo.vc")


@patch("src.supabase_sync.sync_incremental", return_value=0)
@patch("src.supabase_sync.load_registry", return_value=[])
def test_run_incremental_includes_inactive_members(mock_load, mock_sync):
    run_incremental(MagicMock(), MagicMock())
    assert mock_load.call_args.kwargs["include_inactive"] is True


@patch("src.supabase_sync.sync_full", return_value=0)
@patch("src.supabase_sync.load_registry", return_value=[])
def test_run_full_includes_inactive_members(mock_load, mock_sync):
    run_full(MagicMock(), MagicMock())
    assert mock_load.call_args.kwargs["include_inactive"] is True


@patch("src.supabase_sync.upsert_meetings")
@patch("src.supabase_sync.extract_row")
@patch("src.supabase_sync._query_pages_since", return_value=[{"id": "a" * 32}])
def test_attendee_resolution_enabled_when_config_present(
    mock_query, mock_extract, mock_upsert,
):
    mock_extract.return_value = {"page_id": "p", "attendee_emails": ["a@b.com"]}
    _sync_db(_DB, MagicMock(), None, config=MagicMock())
    assert mock_extract.call_args.kwargs["resolve_attendees"] is True
    # Emails present → key kept in the upsert payload.
    assert mock_upsert.call_args.args[0][0]["attendee_emails"] == ["a@b.com"]


@patch("src.supabase_sync.upsert_meetings")
@patch("src.supabase_sync.extract_row")
@patch("src.supabase_sync._query_pages_since", return_value=[{"id": "a" * 32}])
def test_none_attendee_emails_popped_to_avoid_null_overwrite(
    mock_query, mock_extract, mock_upsert,
):
    mock_extract.return_value = {"page_id": "p", "attendee_emails": None}
    _sync_db(_DB, MagicMock(), None, config=MagicMock())
    assert "attendee_emails" not in mock_upsert.call_args.args[0][0]
