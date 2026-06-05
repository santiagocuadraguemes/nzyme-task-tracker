"""Tests for the Supabase claim-before-post state (``src/fundraising/state``).

All Supabase I/O goes through the reused ``_http`` helper, patched here at
``src.fundraising.state._http`` (the name imported into state's namespace).
No network.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.fundraising.outcome import FundraisingOutcome, FundraisingStatus
from src.fundraising.state import claim_post, record_outcome

PAGE_ID = "11111111-2222-3333-4444-555555555555"


def _claim(**overrides) -> bool:
    kwargs = {
        "page_id": PAGE_ID,
        "db_id": "99999999-8888-7777-6666-555555555555",
        "owner_name": "Santiago",
        "include_transcript": False,
    }
    kwargs.update(overrides)
    return claim_post(**kwargs)


def _row(status: str, *, claimed_minutes_ago: int = 0, attempts: int = 1) -> dict:
    claimed_at = datetime.now(timezone.utc) - timedelta(minutes=claimed_minutes_ago)
    return {
        "page_id": PAGE_ID,
        "status": status,
        "attempts": attempts,
        "claimed_at": claimed_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# claim_post
# ---------------------------------------------------------------------------


@patch("src.fundraising.state._http")
def test_fresh_claim_won(mock_http):
    mock_http.return_value = [_row("claimed")]
    assert _claim() is True
    assert mock_http.call_count == 1
    method, path = mock_http.call_args.args[:2]
    assert method == "POST"
    assert "on_conflict=page_id" in path
    assert (
        mock_http.call_args.kwargs["prefer"]
        == "resolution=ignore-duplicates,return=representation"
    )
    (body_row,) = mock_http.call_args.kwargs["body"]
    assert body_row["page_id"] == PAGE_ID
    assert body_row["status"] == "claimed"
    assert body_row["attempts"] == 1


@patch("src.fundraising.state._http")
def test_lost_claim_terminal_posted_skips(mock_http):
    mock_http.side_effect = [[], [_row("posted")]]
    assert _claim() is False
    assert mock_http.call_count == 2  # POST + GET, no PATCH


@pytest.mark.parametrize(
    "status", ["skipped_no_external_attendees", "skipped_no_lp_match"],
)
@patch("src.fundraising.state._http")
def test_lost_claim_terminal_skipped_skips(mock_http, status):
    mock_http.side_effect = [[], [_row(status)]]
    assert _claim() is False
    assert mock_http.call_count == 2


@patch("src.fundraising.state._http")
def test_lost_claim_failed_row_reclaimed(mock_http):
    mock_http.side_effect = [[], [_row("failed", attempts=1)], [_row("claimed")]]
    assert _claim() is True
    method, path = mock_http.call_args.args[:2]
    assert method == "PATCH"
    assert f"page_id=eq.{PAGE_ID}" in path
    assert "status=eq.failed" in path
    body = mock_http.call_args.kwargs["body"]
    assert body["status"] == "claimed"
    assert body["attempts"] == 2
    assert body["completed_at"] is None


@patch("src.fundraising.state._http")
def test_lost_claim_failed_reclaim_race_lost(mock_http):
    # Another invocation re-claimed first → conditional PATCH matches 0 rows.
    mock_http.side_effect = [[], [_row("failed")], []]
    assert _claim() is False


@patch("src.fundraising.state._http")
def test_lost_claim_stale_claimed_reclaimed(mock_http):
    mock_http.side_effect = [
        [],
        [_row("claimed", claimed_minutes_ago=60, attempts=1)],
        [_row("claimed")],
    ]
    assert _claim() is True
    method, path = mock_http.call_args.args[:2]
    assert method == "PATCH"
    assert "status=eq.claimed" in path
    assert "claimed_at=lt." in path
    assert mock_http.call_args.kwargs["body"]["attempts"] == 2


@patch("src.fundraising.state._http")
def test_lost_claim_fresh_claimed_skips(mock_http):
    mock_http.side_effect = [[], [_row("claimed", claimed_minutes_ago=1)]]
    assert _claim() is False
    assert mock_http.call_count == 2  # no PATCH


@patch("src.fundraising.state._http")
def test_supabase_down_on_insert_fails_closed(mock_http, caplog):
    mock_http.side_effect = RuntimeError("Supabase POST failed (503)")
    with caplog.at_level("ERROR"):
        assert _claim() is False
    assert "failing closed" in caplog.text


@patch("src.fundraising.state._http")
def test_supabase_down_on_get_fails_closed(mock_http):
    mock_http.side_effect = [[], RuntimeError("Supabase GET failed (503)")]
    assert _claim() is False


@patch("src.fundraising.state._http")
def test_lost_claim_row_vanished_fails_closed(mock_http, caplog):
    mock_http.side_effect = [[], []]  # lost insert, then GET finds nothing
    with caplog.at_level("ERROR"):
        assert _claim() is False
    assert "row not found" in caplog.text


# ---------------------------------------------------------------------------
# record_outcome
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (FundraisingStatus.POSTED, "posted"),
        (
            FundraisingStatus.SKIPPED_NO_EXTERNAL_ATTENDEES,
            "skipped_no_external_attendees",
        ),
        (FundraisingStatus.SKIPPED_NO_LP_MATCH, "skipped_no_lp_match"),
        (FundraisingStatus.FAILED_API_ERROR, "failed"),
    ],
)
@patch("src.fundraising.state._http")
def test_record_outcome_maps_statuses(mock_http, status, expected):
    record_outcome(
        page_id=PAGE_ID,
        outcome=FundraisingOutcome(status=status, detail="d"),
    )
    method, path = mock_http.call_args.args[:2]
    assert method == "PATCH"
    assert f"page_id=eq.{PAGE_ID}" in path
    body = mock_http.call_args.kwargs["body"]
    assert body["status"] == expected
    assert body["completed_at"] is not None


@patch("src.fundraising.state._http")
def test_record_outcome_posted_carries_opportunity_ids(mock_http):
    record_outcome(
        page_id=PAGE_ID,
        outcome=FundraisingOutcome(status=FundraisingStatus.POSTED),
        opportunity_ids=[123, 456],
    )
    assert mock_http.call_args.kwargs["body"]["opportunity_ids"] == [123, 456]


@patch("src.fundraising.state._http")
def test_record_outcome_writeback_failure_never_raises(mock_http, caplog):
    mock_http.side_effect = RuntimeError("Supabase PATCH failed (503)")
    with caplog.at_level("ERROR"):
        record_outcome(
            page_id=PAGE_ID,
            outcome=FundraisingOutcome(status=FundraisingStatus.POSTED),
        )
    assert "failed to record outcome" in caplog.text
