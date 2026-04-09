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

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/directory.readonly",
]
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
TOKEN_FILE = PROJECT_ROOT / "token.json"


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


def get_gcal_attendees(
    meeting_title: str,
    meeting_date: str,
) -> list[dict[str, str]]:
    """Query Google Calendar for a meeting and return its attendees.

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

    # Build email→name lookup from Google Workspace directory
    directory = _build_directory_lookup(creds)

    # Strip ISO datetime suffix that Notion appends to meeting titles
    meeting_title = strip_title_datetime(meeting_title)

    # Parse the meeting date and search in a 24-hour window around it
    dt = datetime.fromisoformat(meeting_date)
    time_min = (dt - timedelta(hours=12)).isoformat()
    time_max = (dt + timedelta(hours=12)).isoformat()

    logger.info("Searching Google Calendar for '%s' around %s", meeting_title, meeting_date)

    events_result = (
        service.events()
        .list(
            calendarId="primary",
            q=meeting_title,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    events = events_result.get("items", [])
    if not events:
        logger.warning("No Google Calendar event found for '%s'", meeting_title)
        return []

    # Pick the best match (first result from search)
    event = events[0]
    logger.info("Found event: '%s' (%s)", event.get("summary"), event.get("start", {}).get("dateTime"))

    # Collect attendees
    gcal_attendees = event.get("attendees", [])
    logger.info("Google Calendar attendees for '%s':", event.get("summary"))
    for att in gcal_attendees:
        logger.debug("  %s (displayName: %s)", att.get("email"), att.get("displayName", "—"))
    organizer = event.get("organizer", {})
    logger.debug("  Organizer: %s (displayName: %s)", organizer.get("email"), organizer.get("displayName", "—"))

    result: list[dict[str, str]] = []
    seen_emails: set[str] = set()

    for att in gcal_attendees:
        email = att.get("email", "")
        if not email or email in seen_emails:
            continue
        seen_emails.add(email)
        display_name = att.get("displayName", "") or directory.get(email.lower(), "")
        result.append({"email": email, "name": display_name or email.split("@")[0]})

    # Include organizer if not already in attendee list
    org_email = organizer.get("email", "")
    if org_email and org_email not in seen_emails:
        display_name = organizer.get("displayName", "") or directory.get(org_email.lower(), "")
        result.append({"email": org_email, "name": display_name or org_email.split("@")[0]})

    return result


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
