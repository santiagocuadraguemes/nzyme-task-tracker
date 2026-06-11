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
    external_org: str | None = None,
    confidential: str | None = None,
    created_by: dict | None = None,
    title_prop_name: str = "Meeting",
    title_text: str = "LP X update",
) -> dict:
    props: dict = {
        title_prop_name: {
            "type": "title",
            "title": [{"plain_text": title_text}],
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
    if external_org is not None:
        props["External Org"] = {
            "type": "select", "select": {"name": external_org},
        }
    if confidential is not None:
        props["Confidential"] = {
            "type": "select", "select": {"name": confidential},
        }
    return {
        "id": PAGE_HEX,
        "created_time": "2026-06-04T08:00:00.000Z",
        "last_edited_time": "2026-06-04T11:00:00.000Z",
        "created_by": created_by if created_by is not None else {"id": "u1"},
        "properties": props,
    }


def _client() -> MagicMock:
    client = MagicMock()
    client.get_block_children.return_value = []  # no meeting_notes block
    return client


def test_title_read_from_standard_meeting_property():
    row = extract_row(_page(title_text="LP X update"), _OWNER, _client())
    assert row["title"] == "LP X update"


def test_title_read_by_type_when_property_named_differently():
    # Álvaro Lozano's DB names its title property "Note", not "Meeting".
    # The title must still be extracted (located by type == "title"), not
    # fall back to "(untitled)".
    row = extract_row(
        _page(title_prop_name="Note", title_text="Revisión Modelo"),
        _OWNER, _client(),
    )
    assert row["title"] == "Revisión Modelo"


def test_title_untitled_when_no_title_property():
    page = _page()
    del page["properties"]["Meeting"]
    row = extract_row(page, _OWNER, _client())
    assert row["title"] == "(untitled)"


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


# ---------------------------------------------------------------------------
# Full member-DB replica columns (2026-06-05 — multi-Lambda architecture)
# ---------------------------------------------------------------------------


def test_external_org_and_confidential_mirrored():
    row = extract_row(
        _page(external_org="Citadel", confidential="Shareable"),
        _OWNER, _client(),
    )
    assert row["external_org"] == "Citadel"
    assert row["confidential"] == "Shareable"


def test_external_org_and_confidential_absent_are_none():
    row = extract_row(_page(), _OWNER, _client())
    assert row["external_org"] is None
    assert row["confidential"] is None


def test_created_by_mirrored_and_uuid_normalized():
    row = extract_row(
        _page(created_by={"id": "c" * 32, "name": "Santiago"}),
        _OWNER, _client(),
    )
    assert row["created_by_id"] == (
        "cccccccc-cccc-cccc-cccc-cccccccccccc"
    )
    assert row["created_by_name"] == "Santiago"


def test_created_by_partial_user_without_name():
    # Page payloads carry partial user objects ({"id": ...} only) — the
    # name column stays NULL rather than "".
    row = extract_row(
        _page(created_by={"id": "d" * 32}), _OWNER, _client(),
    )
    assert row["created_by_id"] is not None
    assert row["created_by_name"] is None


def test_created_by_missing_entirely_is_safe():
    row = extract_row(_page(created_by={}), _OWNER, _client())
    assert row["created_by_id"] is None
    assert row["created_by_name"] is None


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
