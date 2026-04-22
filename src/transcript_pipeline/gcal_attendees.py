"""Fetch meeting attendees from Google Calendar via a service account with DWD.

Authentication: a Google Cloud service account with Domain-Wide Delegation
authorized for the `https://www.googleapis.com/auth/calendar` scope. The
service account impersonates a specific Workspace user (the "delegated user")
per call, which is typically the meeting page's Notion creator.

Credentials can come from:
  - Local dev: a JSON file at `GOOGLE_SERVICE_ACCOUNT_FILE`.
  - Lambda:    a secret in AWS Secrets Manager whose ARN is
               `GOOGLE_SERVICE_ACCOUNT_SECRET_ARN`. The secret value is the
               raw service-account.json string.

Name resolution no longer uses the People API (`directory.readonly` is not on
the DWD allowlist). Callers resolve attendee names downstream by matching
emails against the Notion Org Chart.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
MIN_FUZZY_SCORE = 60

_SA_INFO_CACHE: dict[str, Any] | None = None


def _load_sa_info() -> dict[str, Any]:
    """Load service-account JSON from file or Secrets Manager; cache for warm reuse."""
    global _SA_INFO_CACHE
    if _SA_INFO_CACHE is not None:
        return _SA_INFO_CACHE

    secret_arn = os.getenv("GOOGLE_SERVICE_ACCOUNT_SECRET_ARN")
    if secret_arn:
        import boto3

        region = os.getenv("AWS_REGION", "eu-west-1")
        logger.debug("Loading service account from Secrets Manager (region=%s)", region)
        sm = boto3.client("secretsmanager", region_name=region)
        resp = sm.get_secret_value(SecretId=secret_arn)
        raw = resp.get("SecretString") or resp["SecretBinary"].decode()
        _SA_INFO_CACHE = json.loads(raw)
        return _SA_INFO_CACHE

    file_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    if not file_path:
        raise RuntimeError(
            "No Google service account configured. Set GOOGLE_SERVICE_ACCOUNT_FILE "
            "(local) or GOOGLE_SERVICE_ACCOUNT_SECRET_ARN (Lambda)."
        )
    logger.debug("Loading service account from file: %s", file_path)
    with open(file_path, "r", encoding="utf-8") as f:
        _SA_INFO_CACHE = json.load(f)
    return _SA_INFO_CACHE


def _build_calendar_service(delegated_user: str):
    """Create a Calendar API client impersonating `delegated_user`."""
    sa_info = _load_sa_info()
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=SCOPES, subject=delegated_user,
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _parse_event_bounds(event: dict[str, Any]) -> tuple[datetime, datetime] | None:
    """Return (start, end) as tz-aware datetimes, or None for all-day/missing bounds."""
    start_dt = event.get("start", {}).get("dateTime")
    end_dt = event.get("end", {}).get("dateTime")
    if not start_dt or not end_dt:
        return None
    try:
        return (datetime.fromisoformat(start_dt), datetime.fromisoformat(end_dt))
    except ValueError:
        return None


def _event_contains(notion_dt: datetime, event: dict[str, Any]) -> bool:
    """True if notion_dt falls within the event's [start, end] interval."""
    bounds = _parse_event_bounds(event)
    if bounds is None:
        return False
    start, end = bounds
    return start <= notion_dt <= end


def _pick_best_event(
    events: list[dict[str, Any]],
    cleaned_title: str,
    notion_dt: datetime,
) -> dict[str, Any] | None:
    """Pick the best GCal event for the given Notion meeting.

    Prefers events whose [start, end] contains notion_dt. Ties broken by
    rapidfuzz token_set_ratio against the cleaned title.
    """
    if not events:
        return None

    containing = [e for e in events if _event_contains(notion_dt, e)]
    pool = containing if containing else events

    def score(event: dict[str, Any]) -> float:
        summary = (event.get("summary") or "").lower()
        return fuzz.token_set_ratio(cleaned_title.lower(), summary)

    best = max(pool, key=score)
    if containing:
        return best
    return best if score(best) >= MIN_FUZZY_SCORE else None


def _flatten_attendees(event: dict[str, Any]) -> list[dict[str, str]]:
    """Resolve an event's attendees + organizer to {email, name} dicts.

    `name` is populated from the Calendar event's own `displayName` when
    present (often empty). Downstream code enriches missing names via the
    Notion Org Chart email lookup.
    """
    result: list[dict[str, str]] = []
    seen_emails: set[str] = set()

    for att in event.get("attendees", []):
        email = att.get("email", "")
        if not email or email in seen_emails:
            continue
        seen_emails.add(email)
        display_name = att.get("displayName", "") or email.split("@")[0]
        result.append({"email": email, "name": display_name})

    organizer = event.get("organizer", {})
    org_email = organizer.get("email", "")
    if org_email and org_email not in seen_emails:
        display_name = organizer.get("displayName", "") or org_email.split("@")[0]
        result.append({"email": org_email, "name": display_name})

    return result


def get_gcal_attendees(
    meeting_title: str,
    meeting_date: str,
    delegated_user: str,
) -> list[dict[str, str]]:
    """Query Google Calendar for a meeting and return its attendees.

    Args:
        meeting_title: Meeting title to search for.
        meeting_date: ISO datetime string from Notion (e.g.
            "2026-04-08T12:00:00.000+02:00").
        delegated_user: Workspace email to impersonate. The service account
            searches this user's primary calendar.

    Returns:
        List of {"email": ..., "name": ...} dicts for all attendees including
        organizer. Names are best-effort from Calendar's `displayName` field;
        callers should enrich via the Notion Org Chart.
    """
    from src.transcript_pipeline.fetch_transcript import strip_title_datetime

    service = _build_calendar_service(delegated_user)

    cleaned_title = strip_title_datetime(meeting_title)
    notion_dt = datetime.fromisoformat(meeting_date)
    time_min = (notion_dt - timedelta(hours=12)).isoformat()
    time_max = (notion_dt + timedelta(hours=12)).isoformat()

    logger.debug(
        "Searching Google Calendar (keyword) for '%s' around %s (as %s)",
        cleaned_title, meeting_date, delegated_user,
    )
    pass1 = (
        service.events()
        .list(
            calendarId="primary",
            q=cleaned_title,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
        .get("items", [])
    )
    match = _pick_best_event(pass1, cleaned_title, notion_dt)
    if match is not None:
        logger.info(
            "Matched by keyword: '%s' (%s)",
            match.get("summary"),
            match.get("start", {}).get("dateTime"),
        )
        return _flatten_attendees(match)

    logger.info(
        "No keyword match; falling back to time-range scan for '%s'", cleaned_title
    )
    pass2 = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
        .get("items", [])
    )
    containing = [e for e in pass2 if _event_contains(notion_dt, e)]
    match = _pick_best_event(containing, cleaned_title, notion_dt)
    if match is not None:
        logger.info(
            "Matched by time containment: '%s' (%s)",
            match.get("summary"),
            match.get("start", {}).get("dateTime"),
        )
        return _flatten_attendees(match)

    considered = [
        f"'{e.get('summary')}' @ {e.get('start', {}).get('dateTime') or e.get('start', {}).get('date')}"
        for e in pass2
    ]
    logger.warning(
        "No Google Calendar event matched '%s' at %s. Considered: %s",
        cleaned_title, notion_dt, considered,
    )
    return []
