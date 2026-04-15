"""Unit tests for the pure GCal matching helpers (no network)."""

from __future__ import annotations

from datetime import datetime

from src.transcript_pipeline.gcal_attendees import (
    MIN_FUZZY_SCORE,
    _event_contains,
    _parse_event_bounds,
    _pick_best_event,
)


def _event(summary: str, start: str | None, end: str | None) -> dict:
    ev: dict = {"summary": summary}
    if start:
        ev["start"] = {"dateTime": start}
    if end:
        ev["end"] = {"dateTime": end}
    return ev


def _all_day_event(summary: str, date: str) -> dict:
    return {"summary": summary, "start": {"date": date}, "end": {"date": date}}


class TestParseEventBounds:
    def test_returns_bounds_for_timed_event(self):
        ev = _event("x", "2026-03-27T10:00:00+01:00", "2026-03-27T11:00:00+01:00")
        bounds = _parse_event_bounds(ev)
        assert bounds is not None
        start, end = bounds
        assert start.hour == 10 and end.hour == 11

    def test_returns_none_for_all_day(self):
        assert _parse_event_bounds(_all_day_event("x", "2026-03-27")) is None

    def test_returns_none_when_end_missing(self):
        assert _parse_event_bounds(_event("x", "2026-03-27T10:00:00+01:00", None)) is None


class TestEventContains:
    def test_inside_interval(self):
        ev = _event("x", "2026-03-27T10:00:00+01:00", "2026-03-27T11:00:00+01:00")
        dt = datetime.fromisoformat("2026-03-27T10:02:00+01:00")
        assert _event_contains(dt, ev) is True

    def test_before_start(self):
        ev = _event("x", "2026-03-27T10:00:00+01:00", "2026-03-27T11:00:00+01:00")
        dt = datetime.fromisoformat("2026-03-27T09:59:00+01:00")
        assert _event_contains(dt, ev) is False

    def test_after_end(self):
        ev = _event("x", "2026-03-27T10:00:00+01:00", "2026-03-27T11:00:00+01:00")
        dt = datetime.fromisoformat("2026-03-27T11:01:00+01:00")
        assert _event_contains(dt, ev) is False

    def test_exact_start_boundary(self):
        ev = _event("x", "2026-03-27T10:00:00+01:00", "2026-03-27T11:00:00+01:00")
        dt = datetime.fromisoformat("2026-03-27T10:00:00+01:00")
        assert _event_contains(dt, ev) is True

    def test_all_day_event_is_not_contained(self):
        dt = datetime.fromisoformat("2026-03-27T10:02:00+01:00")
        assert _event_contains(dt, _all_day_event("OOO", "2026-03-27")) is False


class TestPickBestEvent:
    def test_returns_none_for_empty(self):
        dt = datetime.fromisoformat("2026-03-27T10:00:00+01:00")
        assert _pick_best_event([], "Anything", dt) is None

    def test_prefers_containing_event_over_higher_title_score(self):
        # evA: perfect title but wrong time. evB: drift title but contains timestamp.
        dt = datetime.fromisoformat("2026-03-27T10:02:00+01:00")
        evA = _event(
            "Commercial Weekly - WV",
            "2026-03-27T15:00:00+01:00",
            "2026-03-27T16:00:00+01:00",
        )
        evB = _event(
            "Int.call seguimiento comercial WV",
            "2026-03-27T10:00:00+01:00",
            "2026-03-27T11:00:00+01:00",
        )
        best = _pick_best_event([evA, evB], "Commercial Weekly - WV", dt)
        assert best is evB

    def test_returns_none_when_no_containment_and_low_fuzzy(self):
        dt = datetime.fromisoformat("2026-03-27T10:02:00+01:00")
        ev = _event(
            "Totally unrelated dentist appointment",
            "2026-03-27T15:00:00+01:00",
            "2026-03-27T16:00:00+01:00",
        )
        assert _pick_best_event([ev], "Commercial Weekly - WV", dt) is None

    def test_returns_event_when_no_containment_but_high_fuzzy(self):
        dt = datetime.fromisoformat("2026-03-27T10:02:00+01:00")
        ev = _event(
            "Commercial Weekly WV",
            "2026-03-27T15:00:00+01:00",
            "2026-03-27T16:00:00+01:00",
        )
        best = _pick_best_event([ev], "Commercial Weekly - WV", dt)
        assert best is ev

    def test_picks_highest_fuzzy_among_containing_events(self):
        # Back-to-back meetings both containing the timestamp (unlikely but possible
        # for overlapping recurring events).
        dt = datetime.fromisoformat("2026-03-27T10:30:00+01:00")
        evA = _event(
            "Random standup",
            "2026-03-27T10:00:00+01:00",
            "2026-03-27T11:00:00+01:00",
        )
        evB = _event(
            "Commercial Weekly WV",
            "2026-03-27T10:00:00+01:00",
            "2026-03-27T11:00:00+01:00",
        )
        best = _pick_best_event([evA, evB], "Commercial Weekly - WV", dt)
        assert best is evB

    def test_min_fuzzy_score_is_sixty(self):
        assert MIN_FUZZY_SCORE == 60
