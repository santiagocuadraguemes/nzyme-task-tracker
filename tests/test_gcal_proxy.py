"""Tests for out-of-domain GCal proxy impersonation.

Domain-wide delegation can't impersonate users outside the Workspace domain
(e.g. nzalpha.com). For those owners we impersonate an in-domain proxy and read
the owner's calendar by id instead of the proxy's "primary".
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from src.pipeline import _gcal_impersonation_target
from src.transcript_pipeline import gcal_attendees


def _cfg(proxy_user, proxy_domains):
    return SimpleNamespace(
        gcal_proxy_delegated_user=proxy_user,
        gcal_proxy_domains=frozenset(proxy_domains),
    )


class TestImpersonationTarget:
    def test_in_domain_owner_reads_own_primary(self):
        cfg = _cfg("mar@kiboventures.com", {"nzalpha.com"})
        impersonate, calendar_id = _gcal_impersonation_target(
            "guillermo@kiboventures.com", cfg,
        )
        assert impersonate == "guillermo@kiboventures.com"
        assert calendar_id == "primary"

    def test_out_of_domain_owner_uses_proxy_and_owner_calendar(self):
        cfg = _cfg("mar@kiboventures.com", {"nzalpha.com"})
        impersonate, calendar_id = _gcal_impersonation_target(
            "sakhee.joisher@nzalpha.com", cfg,
        )
        assert impersonate == "mar@kiboventures.com"
        assert calendar_id == "sakhee.joisher@nzalpha.com"

    def test_domain_match_is_case_insensitive(self):
        cfg = _cfg("mar@kiboventures.com", {"nzalpha.com"})
        impersonate, calendar_id = _gcal_impersonation_target(
            "Alvaro.Fresnillo@NzAlpha.com", cfg,
        )
        assert impersonate == "mar@kiboventures.com"
        assert calendar_id == "Alvaro.Fresnillo@NzAlpha.com"

    def test_out_of_domain_without_proxy_falls_back_to_self(self):
        # Domain listed but no proxy configured → behave as before.
        cfg = _cfg(None, {"nzalpha.com"})
        impersonate, calendar_id = _gcal_impersonation_target(
            "sakhee.joisher@nzalpha.com", cfg,
        )
        assert impersonate == "sakhee.joisher@nzalpha.com"
        assert calendar_id == "primary"

    def test_no_proxy_domains_configured(self):
        cfg = _cfg("mar@kiboventures.com", set())
        impersonate, calendar_id = _gcal_impersonation_target(
            "sakhee.joisher@nzalpha.com", cfg,
        )
        assert impersonate == "sakhee.joisher@nzalpha.com"
        assert calendar_id == "primary"


class _FakeList:
    def __init__(self, items):
        self._items = items

    def execute(self):
        return {"items": self._items}


class _FakeEvents:
    def __init__(self, recorder):
        self._recorder = recorder

    def list(self, **kwargs):
        self._recorder.append(kwargs)
        return _FakeList([])


class _FakeService:
    def __init__(self, recorder):
        self._recorder = recorder

    def events(self):
        return _FakeEvents(self._recorder)


class TestGetGcalAttendeesCalendarId:
    def test_calendar_id_forwarded_to_list(self):
        recorder: list[dict] = []
        with patch.object(
            gcal_attendees, "_build_calendar_service",
            return_value=_FakeService(recorder),
        ):
            gcal_attendees.get_gcal_attendees(
                "Ext. Poseidon | Deep dive",
                "2026-05-29T14:00:00+02:00",
                "mar@kiboventures.com",
                calendar_id="sakhee.joisher@nzalpha.com",
            )
        assert recorder, "expected at least one events().list call"
        assert all(c["calendarId"] == "sakhee.joisher@nzalpha.com" for c in recorder)

    def test_calendar_id_defaults_to_primary(self):
        recorder: list[dict] = []
        with patch.object(
            gcal_attendees, "_build_calendar_service",
            return_value=_FakeService(recorder),
        ):
            gcal_attendees.get_gcal_attendees(
                "Ext. Poseidon | Deep dive",
                "2026-05-29T14:00:00+02:00",
                "guillermo@kiboventures.com",
            )
        assert all(c["calendarId"] == "primary" for c in recorder)
