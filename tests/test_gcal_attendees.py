"""Unit tests for gcal_attendees — service account loading and attendee flattening."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from src.transcript_pipeline import gcal_attendees
from src.transcript_pipeline.gcal_attendees import _flatten_attendees, _load_sa_info


@pytest.fixture(autouse=True)
def _clear_sa_cache():
    """Reset the module-level SA cache between tests."""
    gcal_attendees._SA_INFO_CACHE = None
    yield
    gcal_attendees._SA_INFO_CACHE = None


class TestLoadSaInfo:
    def test_loads_from_file_path(self, tmp_path, monkeypatch):
        sa_data = {"type": "service_account", "client_email": "test@proj.iam.gserviceaccount.com"}
        sa_file = tmp_path / "sa.json"
        sa_file.write_text(json.dumps(sa_data))

        monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_SECRET_ARN", raising=False)
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(sa_file))

        result = _load_sa_info()
        assert result["client_email"] == "test@proj.iam.gserviceaccount.com"

    def test_raises_when_no_source_configured(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_SECRET_ARN", raising=False)
        monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)

        with pytest.raises(RuntimeError, match="No Google service account configured"):
            _load_sa_info()

    def test_prefers_secret_arn_over_file(self, tmp_path, monkeypatch):
        sa_file = tmp_path / "sa.json"
        sa_file.write_text(json.dumps({"client_email": "from-file@x.com"}))

        monkeypatch.setenv(
            "GOOGLE_SERVICE_ACCOUNT_SECRET_ARN",
            "arn:aws:secretsmanager:eu-west-1:123:secret:nzyme-gcal",
        )
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(sa_file))

        fake_secret = json.dumps({"client_email": "from-secret@x.com"})

        class FakeSM:
            def get_secret_value(self, SecretId):
                return {"SecretString": fake_secret}

        def fake_boto3_client(service, region_name=None):
            assert service == "secretsmanager"
            return FakeSM()

        with patch("boto3.client", side_effect=fake_boto3_client):
            result = _load_sa_info()
        assert result["client_email"] == "from-secret@x.com"

    def test_caches_across_calls(self, tmp_path, monkeypatch):
        sa_file = tmp_path / "sa.json"
        sa_file.write_text(json.dumps({"client_email": "cached@x.com"}))
        monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_SECRET_ARN", raising=False)
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(sa_file))

        first = _load_sa_info()
        sa_file.unlink()  # If cache works, second call still succeeds
        second = _load_sa_info()
        assert first is second
        assert second["client_email"] == "cached@x.com"


class TestFlattenAttendees:
    def test_extracts_emails_and_displaynames(self):
        event = {
            "attendees": [
                {"email": "reyes@kiboventures.com", "displayName": "Reyes Rubio"},
                {"email": "alf@kiboventures.com"},  # no displayName
            ],
            "organizer": {
                "email": "reyes@kiboventures.com",  # same as attendee — should dedup
                "displayName": "Reyes Rubio",
            },
        }
        result = _flatten_attendees(event)
        emails = [r["email"] for r in result]
        assert "reyes@kiboventures.com" in emails
        assert "alf@kiboventures.com" in emails
        assert len(result) == 2  # organizer deduped

    def test_falls_back_to_email_prefix_when_no_displayname(self):
        event = {"attendees": [{"email": "alf@kiboventures.com"}]}
        result = _flatten_attendees(event)
        assert result[0]["name"] == "alf"

    def test_includes_organizer_not_in_attendee_list(self):
        event = {
            "attendees": [{"email": "other@example.com"}],
            "organizer": {"email": "organizer@kiboventures.com", "displayName": "The Organizer"},
        }
        result = _flatten_attendees(event)
        emails = [r["email"] for r in result]
        assert "organizer@kiboventures.com" in emails
        assert "other@example.com" in emails

    def test_skips_empty_and_duplicate_emails(self):
        event = {
            "attendees": [
                {"email": "a@x.com"},
                {"email": "a@x.com"},  # dup
                {"email": ""},  # empty
                {"email": "b@x.com"},
            ],
        }
        result = _flatten_attendees(event)
        emails = [r["email"] for r in result]
        assert sorted(emails) == ["a@x.com", "b@x.com"]
