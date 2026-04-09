"""Fetch meeting attendees from Google Calendar via OAuth."""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
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
    org_chart_names: list[str] | None = None,
) -> list[dict[str, str]]:
    """Query Google Calendar for a meeting and return its attendees.

    Args:
        meeting_title: Meeting title to search for.
        meeting_date: ISO datetime string from Notion (e.g. "2026-04-08T12:00:00.000+02:00").
        org_chart_names: Optional list of org chart names to help resolve emails to names.

    Returns:
        List of {"email": ..., "name": ...} dicts for all attendees including organizer.
    """
    creds = _get_credentials()
    service = build("calendar", "v3", credentials=creds)

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
    print(f"  Google Calendar attendees for '{event.get('summary')}':", file=sys.stderr)
    for att in gcal_attendees:
        print(f"    {att.get('email')} (displayName: {att.get('displayName', '—')})", file=sys.stderr)
    organizer = event.get("organizer", {})
    print(f"    Organizer: {organizer.get('email')} (displayName: {organizer.get('displayName', '—')})", file=sys.stderr)

    result: list[dict[str, str]] = []
    seen_emails: set[str] = set()

    for att in gcal_attendees:
        email = att.get("email", "")
        if not email or email in seen_emails:
            continue
        seen_emails.add(email)
        display_name = att.get("displayName", "")
        # Try to match email prefix to org chart names
        if not display_name and org_chart_names:
            display_name = _match_email_to_name(email, org_chart_names)
        result.append({"email": email, "name": display_name or email.split("@")[0]})

    # Include organizer if not already in attendee list
    org_email = organizer.get("email", "")
    if org_email and org_email not in seen_emails:
        display_name = organizer.get("displayName", "")
        if not display_name and org_chart_names:
            display_name = _match_email_to_name(org_email, org_chart_names)
        result.append({"email": org_email, "name": display_name or org_email.split("@")[0]})

    return result


def _match_email_to_name(email: str, org_chart_names: list[str]) -> str:
    """Try to match an email prefix to an org chart name.

    Only matches when there's exactly one candidate — ambiguous matches
    (e.g., two Juans) return empty to avoid picking the wrong person.
    """
    prefix = email.split("@")[0].lower()
    candidates: list[str] = []
    for name in org_chart_names:
        name_lower = name.lower()
        first_name = name_lower.split()[0] if name.split() else ""
        if prefix == first_name or prefix in name_lower.replace(" ", ""):
            candidates.append(name)

    if len(candidates) == 1:
        return candidates[0]

    if len(candidates) > 1:
        logger.info(
            "Ambiguous email match: %s matches %d org chart names: %s — using email prefix instead",
            email, len(candidates), candidates,
        )
    return ""
