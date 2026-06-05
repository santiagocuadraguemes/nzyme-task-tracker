"""Tests for `extract_row` (Notion page → meeting_transcripts row).

Covers the 2026-06-04 mirror fixes: `Macro Work Block` property (renamed
from `Meeting type`), multi-select `Detail`, and the gated GCal attendee
resolution for Affinity-rule-matching meetings.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.meeting_db_registry import MeetingDB
from src.meeting_row import extract_row

DB_HEX = "b" * 32
PAGE_HEX = "a" * 32

_OWNER = MeetingDB(db_id=DB_HEX, owner_name="Santiago", owner_email="s@kibo.vc")


def _page(
    *,
    macro_work_block: str | None = None,
    legacy_meeting_type: str | None = None,
    detail_multi: list[str] | None = None,
) -> dict:
    props: dict = {
        "Meeting": {
            "type": "title",
            "title": [{"plain_text": "LP X update"}],
        },
        "Date": {
            "type": "date",
            "date": {"start": "2026-06-04T10:00:00.000+02:00", "end": None},
        },
    }
    if macro_work_block is not None:
        props["Macro Work Block"] = {
            "type": "select", "select": {"name": macro_work_block},
        }
    if legacy_meeting_type is not None:
        props["Meeting type"] = {
            "type": "select", "select": {"name": legacy_meeting_type},
        }
    if detail_multi is not None:
        props["Detail"] = {
            "type": "multi_select",
            "multi_select": [{"name": n} for n in detail_multi],
        }
    return {
        "id": PAGE_HEX,
        "created_time": "2026-06-04T08:00:00.000Z",
        "last_edited_time": "2026-06-04T11:00:00.000Z",
        "created_by": {"id": "u1"},
        "properties": props,
    }


def _client() -> MagicMock:
    client = MagicMock()
    client.get_block_children.return_value = []  # no meeting_notes block
    return client


def test_reads_macro_work_block_property():
    row = extract_row(_page(macro_work_block="Investor Relations & Fundraising"),
                      _OWNER, _client())
    assert row["macro_work_block"] == "Investor Relations & Fundraising"
    assert "meeting_type" not in row


def test_falls_back_to_legacy_meeting_type_property():
    row = extract_row(_page(legacy_meeting_type="Team sync"), _OWNER, _client())
    assert row["macro_work_block"] == "Team sync"


def test_detail_multi_select_joined():
    row = extract_row(_page(detail_multi=["AI & Tech", "Hiring"]), _OWNER, _client())
    assert row["detail"] == "AI & Tech, Hiring"


def test_detail_absent_is_none():
    row = extract_row(_page(), _OWNER, _client())
    assert row["detail"] is None


@patch("src.pipeline._resolve_attendees")
def test_attendees_resolved_for_every_meeting_when_enabled(mock_resolve):
    mock_resolve.return_value = [
        {"id": "lp@fund.com", "name": "LP", "email": "LP@Fund.com"},
        {"id": "s@kibo.vc", "name": "Santiago", "email": "s@kibo.vc"},
        {"id": "dup", "name": "Dup", "email": "lp@fund.com"},
        {"id": "no-email", "name": "X", "email": None},
    ]
    # Any meeting — no Macro Work Block gating.
    row = extract_row(
        _page(macro_work_block="Team sync"),
        _OWNER, _client(),
        config=MagicMock(),
        resolve_attendees=True,
    )
    # Lower-cased, de-duped, email-less entries dropped.
    assert row["attendee_emails"] == ["lp@fund.com", "s@kibo.vc"]
    mock_resolve.assert_called_once()


@patch("src.pipeline._resolve_attendees")
def test_attendees_not_resolved_when_disabled(mock_resolve):
    row = extract_row(
        _page(macro_work_block="Investor Relations & Fundraising"),
        _OWNER, _client(),
        config=MagicMock(),
        resolve_attendees=False,
    )
    assert row["attendee_emails"] is None
    mock_resolve.assert_not_called()


@patch("src.pipeline._resolve_attendees")
def test_attendees_not_resolved_without_config(mock_resolve):
    row = extract_row(
        _page(macro_work_block="Investor Relations & Fundraising"),
        _OWNER, _client(),
        resolve_attendees=True,
    )
    assert row["attendee_emails"] is None
    mock_resolve.assert_not_called()


@patch("src.pipeline._resolve_attendees")
def test_attendee_resolution_failure_soft_fails(mock_resolve):
    mock_resolve.side_effect = RuntimeError("GCal exploded")
    row = extract_row(
        _page(macro_work_block="Investor Relations & Fundraising"),
        _OWNER, _client(),
        config=MagicMock(),
        resolve_attendees=True,
    )
    assert row["attendee_emails"] is None  # row still produced
    assert row["macro_work_block"] == "Investor Relations & Fundraising"
