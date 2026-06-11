"""Tests for `extract_page_metadata` — the title must be located by property
TYPE (== "title"), not a hardcoded "Meeting" name, so DBs that name their title
property differently ("Note" in Álvaro Lozano's, "Título" in Jaime Gervás's)
still yield a title rather than an empty string.
"""
from __future__ import annotations

from src.transcript_pipeline.fetch_transcript import extract_page_metadata


def _page(title_prop_name: str, title_text: str) -> dict:
    return {
        "properties": {
            title_prop_name: {
                "type": "title",
                "title": [{"plain_text": title_text}],
            },
            "Date": {"type": "date", "date": {"start": "2026-04-09"}},
        },
        "created_time": "2026-04-08T08:00:00.000Z",
        "created_by": {"id": "u1", "name": "Santiago"},
    }


def test_title_from_standard_meeting_property():
    meta = extract_page_metadata(_page("Meeting", "LP X update"))
    assert meta["title"] == "LP X update"
    assert meta["date"] == "2026-04-09"


def test_title_found_by_type_when_named_note():
    meta = extract_page_metadata(_page("Note", "Revisión Modelo"))
    assert meta["title"] == "Revisión Modelo"


def test_title_found_by_type_when_named_titulo():
    meta = extract_page_metadata(_page("Título", "Catch up Lavanderías"))
    assert meta["title"] == "Catch up Lavanderías"


def test_title_empty_when_no_title_property():
    page = {
        "properties": {"Date": {"type": "date", "date": {"start": "2026-04-09"}}},
        "created_time": "2026-04-08T08:00:00.000Z",
        "created_by": {},
    }
    meta = extract_page_metadata(page)
    assert meta["title"] == ""
    # Date still resolves from the Date property.
    assert meta["date"] == "2026-04-09"


def test_date_falls_back_to_created_time():
    page = _page("Meeting", "Sync")
    page["properties"].pop("Date")
    meta = extract_page_metadata(page)
    assert meta["date"] == "2026-04-08T08:00:00.000Z"
