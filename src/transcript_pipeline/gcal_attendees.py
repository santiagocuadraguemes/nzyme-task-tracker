"""Fetch meeting attendees from Google Calendar via OAuth."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/directory.readonly",
]
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
TOKEN_FILE = PROJECT_ROOT / "token.json"

MIN_FUZZY_SCORE = 60


def _get_credentials() -> Credentials:
    """Load or create Google OAuth credentials.

    First run opens a browser for consent and stores token.json locally.
    Subsequent runs auto-refresh the token.
    """
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"Google OAuth credentials not found at {CREDENTIALS_FILE}. "
                    f"Download from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return creds


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
    rapidfuzz token_set_ratio against the cleaned title. If no event contains
    the timestamp, still returns the highest-scoring event if its score is at
    least MIN_FUZZY_SCORE; otherwise returns None.
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


def _flatten_attendees(
    event: dict[str, Any],
    directory: dict[str, str],
) -> list[dict[str, str]]:
    """Resolve an event's attendees + organizer to {email, name} dicts."""
    result: list[dict[str, str]] = []
    seen_emails: set[str] = set()

    for att in event.get("attendees", []):
        email = att.get("email", "")
        if not email or email in seen_emails:
            continue
        seen_emails.add(email)
        display_name = att.get("displayName", "") or directory.get(email.lower(), "")
        result.append({"email": email, "name": display_name or email.split("@")[0]})

    organizer = event.get("organizer", {})
    org_email = organizer.get("email", "")
    if org_email and org_email not in seen_emails:
        display_name = organizer.get("displayName", "") or directory.get(
            org_email.lower(), ""
        )
        result.append(
            {"email": org_email, "name": display_name or org_email.split("@")[0]}
        )

    return result


def get_gcal_attendees(
    meeting_title: str,
    meeting_date: str,
) -> list[dict[str, str]]:
    """Query Google Calendar for a meeting and return its attendees.

    Matches in two passes:
    1. Keyword search (q=<title>) with the current ±12 h window, best-ranked.
    2. Time-containment fallback: re-query without q=, pick the event whose
       [start, end] contains the Notion timestamp. Title drift between Notion
       and GCal (e.g. "Commercial Weekly - WV" vs "Int.call seguimiento
       comercial WV") is handled because matching falls through to time.

    Uses Google Workspace directory (People API) to resolve emails to full names.

    Args:
        meeting_title: Meeting title to search for.
        meeting_date: ISO datetime string from Notion (e.g. "2026-04-08T12:00:00.000+02:00").

    Returns:
        List of {"email": ..., "name": ...} dicts for all attendees including organizer.
    """
    from src.transcript_pipeline.fetch_transcript import strip_title_datetime

    creds = _get_credentials()
    service = build("calendar", "v3", credentials=creds)
    directory = _build_directory_lookup(creds)

    cleaned_title = strip_title_datetime(meeting_title)
    notion_dt = datetime.fromisoformat(meeting_date)
    time_min = (notion_dt - timedelta(hours=12)).isoformat()
    time_max = (notion_dt + timedelta(hours=12)).isoformat()

    # Pass 1: keyword search
    logger.info(
        "Searching Google Calendar (keyword) for '%s' around %s",
        cleaned_title,
        meeting_date,
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
        return _flatten_attendees(match, directory)

    # Pass 2: time-containment fallback
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
        return _flatten_attendees(match, directory)

    considered = [
        f"'{e.get('summary')}' @ {e.get('start', {}).get('dateTime') or e.get('start', {}).get('date')}"
        for e in pass2
    ]
    logger.warning(
        "No Google Calendar event matched '%s' at %s. Considered: %s",
        cleaned_title,
        notion_dt,
        considered,
    )
    return []


def _build_directory_lookup(creds: Credentials) -> dict[str, str]:
    """Fetch Google Workspace directory and return {email: full_name} map."""
    service = build("people", "v1", credentials=creds)
    lookup: dict[str, str] = {}
    page_token: str | None = None

    while True:
        result = (
            service.people()
            .listDirectoryPeople(
                readMask="names,emailAddresses",
                sources=["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"],
                pageSize=200,
                pageToken=page_token,
            )
            .execute()
        )

        for person in result.get("people", []):
            names = person.get("names", [])
            emails = person.get("emailAddresses", [])
            if not names or not emails:
                continue
            full_name = names[0].get("displayName", "")
            if not full_name:
                continue
            for email_entry in emails:
                email = email_entry.get("value", "").lower()
                if email:
                    lookup[email] = full_name

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    logger.info("Loaded %d people from Google Workspace directory", len(lookup))
    return lookup
