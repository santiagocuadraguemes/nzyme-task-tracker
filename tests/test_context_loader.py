"""Unit tests for context_loader Org Chart matching and email-first resolution."""

from __future__ import annotations

from src.transcript_pipeline.context_loader import (
    _match_attendee_to_org,
    build_enriched_attendee_str,
)


def _row(
    name: str,
    email: str = "",
    seniority: str = "",
    department: str = "",
    role: str = "",
    topics: list[str] | None = None,
) -> dict:
    return {
        "name": name,
        "email": email,
        "seniority": seniority,
        "department": department,
        "role": role,
        "topics": topics or [],
    }


class TestMatchAttendeeToOrg:
    def test_email_match_wins_over_name_match(self):
        rows = [
            _row("Santiago Cuadra Güemes", email="santiago@kiboventures.com"),
            _row("Someone Else Entirely", email="else@kiboventures.com"),
        ]
        # Even with an ambiguous name, email is the reliable key
        match = _match_attendee_to_org(
            "santi", rows, attendee_email="santiago@kiboventures.com",
        )
        assert match is not None
        assert match["name"] == "Santiago Cuadra Güemes"

    def test_email_match_is_case_insensitive(self):
        rows = [_row("Reyes Rubio", email="reyes@kiboventures.com")]
        match = _match_attendee_to_org(
            "someone", rows, attendee_email="REYES@kiboventures.com",
        )
        assert match is not None
        assert match["name"] == "Reyes Rubio"

    def test_falls_back_to_name_when_no_email(self):
        rows = [_row("Juan Perez", email="juan@kiboventures.com")]
        match = _match_attendee_to_org("Juan Perez", rows, attendee_email=None)
        assert match is not None
        assert match["name"] == "Juan Perez"

    def test_falls_back_to_name_when_email_unknown(self):
        rows = [_row("Juan Perez", email="juan@kiboventures.com")]
        # Email not in org chart → name substring matching should still succeed
        match = _match_attendee_to_org(
            "Juan Perez", rows, attendee_email="external@other.com",
        )
        assert match is not None
        assert match["name"] == "Juan Perez"

    def test_external_attendee_returns_none(self):
        rows = [_row("Santiago Cuadra Güemes", email="santiago@kiboventures.com")]
        match = _match_attendee_to_org(
            "Pablo Campos", rows, attendee_email="pablo.campos@oliverwyman.com",
        )
        assert match is None

    def test_name_substring_matching_still_works(self):
        """Regression: pre-existing name-substring logic must still work."""
        rows = [_row("Santiago Cuadra Güemes", email="")]
        match = _match_attendee_to_org("Santiago Cuadra", rows, attendee_email=None)
        assert match is not None


class TestBuildEnrichedAttendeeStr:
    def test_uses_org_chart_name_over_email_prefix(self):
        rows = [
            _row(
                "Reyes Rubio",
                email="reyes@kiboventures.com",
                seniority="Co-founding Partner",
                department="Investment",
                role="Managing Partner & CIO",
            ),
        ]
        attendees = [
            {"id": "reyes@kiboventures.com", "name": "reyes", "email": "reyes@kiboventures.com"},
        ]
        result = build_enriched_attendee_str(attendees, rows)
        # Full name from org chart should replace email-prefix placeholder
        assert "Reyes Rubio" in result
        assert "Co-founding Partner" in result

    def test_external_attendee_passes_through(self):
        rows = [_row("Santiago Cuadra Güemes", email="santiago@kiboventures.com")]
        attendees = [
            {
                "id": "pablo.campos@oliverwyman.com",
                "name": "pablo.campos",
                "email": "pablo.campos@oliverwyman.com",
            },
        ]
        result = build_enriched_attendee_str(attendees, rows)
        assert "pablo.campos" in result
        # No org chart annotation for external
        assert "[" not in result
