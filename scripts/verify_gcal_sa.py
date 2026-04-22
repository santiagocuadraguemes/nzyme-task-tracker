"""Verify service-account + Domain-Wide Delegation access for Calendar + People APIs.

Simulates what Lambda would do:
  1. Load service-account.json (in Lambda: fetched from S3/Secrets Manager at cold start)
  2. Authenticate as the service account, impersonating a Workspace user
  3. List that user's calendar events (what the extractor needs to match meetings)
  4. Read attendees from one event (the actual payload Lambda consumes)
  5. Probe People API directory access (optional name enrichment)

Run:
    ../venv/Scripts/python scripts/verify_gcal_sa.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

SA_FILE_ENV = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
SA_FILE = Path(SA_FILE_ENV) if SA_FILE_ENV else PROJECT_ROOT / ".secrets" / "service-account.json"
DELEGATED_USER = os.getenv("GCAL_DELEGATED_USER_DEFAULT", "santiago@kiboventures.com")

CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_FULL_SCOPE = "https://www.googleapis.com/auth/calendar"
DIRECTORY_SCOPE = "https://www.googleapis.com/auth/directory.readonly"

# Peer's demo used the full calendar scope; readonly may not be on the DWD
# allowlist even if full is. Try readonly first, fall back to full.
CALENDAR_SCOPES_TO_TRY = [CALENDAR_READONLY_SCOPE, CALENDAR_FULL_SCOPE]


def _build_calendar_service(scopes: list[str], subject: str):
    creds = service_account.Credentials.from_service_account_file(
        str(SA_FILE), scopes=scopes, subject=subject,
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _build_people_service(scopes: list[str], subject: str):
    creds = service_account.Credentials.from_service_account_file(
        str(SA_FILE), scopes=scopes, subject=subject,
    )
    return build("people", "v1", credentials=creds, cache_discovery=False)


def test_calendar_list(subject: str) -> dict | None:
    """Step 1+2+3: auth + list events in a recent window.

    Tries readonly scope first, falls back to full calendar scope.
    """
    print(f"\n[1/4] Calendar list - impersonating {subject}")
    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=7)).isoformat()
    time_max = (now + timedelta(days=7)).isoformat()

    for scope in CALENDAR_SCOPES_TO_TRY:
        scope_label = scope.rsplit("/", 1)[-1]
        print(f"  Trying scope: {scope_label}")
        try:
            service = _build_calendar_service([scope], subject)
            result = service.events().list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                maxResults=25,
                singleEvents=True,
                orderBy="startTime",
            ).execute()
        except Exception as err:
            err_text = str(err)
            if "unauthorized_client" in err_text:
                print(f"    -> {scope_label} not on DWD allowlist, trying next")
                continue
            print(f"  FAIL: {err}")
            return None

        events = result.get("items", [])
        print(f"  OK with scope '{scope_label}' - {len(events)} events in +/- 7 day window")
        for e in events[:5]:
            start = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date")
            print(f"    - {e.get('summary', '(no title)')}  @ {start}")
        return {"service": service, "events": events, "scope": scope}

    print("  FAIL - no calendar scope on DWD allowlist")
    return None


def test_attendee_read(events: list[dict]) -> bool:
    """Step 4: pick one event and show its attendees (the Lambda payload)."""
    print("\n[2/4] Attendee read - simulating what the extractor consumes")
    candidate = None
    for e in events:
        if e.get("attendees"):
            candidate = e
            break
    if not candidate:
        print("  SKIP - no event with attendees in the window (not a failure)")
        return True

    print(f"  Event: {candidate.get('summary', '(no title)')}")
    print(f"  Organizer: {candidate.get('organizer', {}).get('email', '?')}")
    print("  Attendees:")
    for a in candidate.get("attendees", []):
        email = a.get("email", "?")
        name = a.get("displayName", "")
        resp = a.get("responseStatus", "?")
        print(f"    - {email:45s}  name={name or '(none)':25s}  rsvp={resp}")
    return True


def test_people_directory(subject: str) -> bool:
    """Step 5: probe People API - needed only for directory name enrichment."""
    print(f"\n[3/4] People API directory lookup - impersonating {subject}")
    try:
        service = _build_people_service([DIRECTORY_SCOPE], subject)
        result = service.people().listDirectoryPeople(
            readMask="names,emailAddresses",
            sources=["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"],
            pageSize=5,
        ).execute()
    except RefreshError as err:
        print(f"  FAIL at token exchange: {err}")
        print("  -> directory.readonly scope is NOT on the DWD allowlist.")
        print("     Admin must add it in Workspace Admin > Security > API Controls")
        print("     > Domain-wide Delegation > edit the service account's client ID.")
        return False
    except HttpError as err:
        body = err.content.decode() if err.content else "(empty)"
        print(f"  FAIL at list: HTTP {err.resp.status} - {err.reason}")
        print(f"  Body: {body[:400]}")
        if "accessNotConfigured" in body or "has not been used" in body:
            print("  -> People API is not enabled in the GCP project")
        elif "unauthorized_client" in body or "invalid_grant" in body:
            print("  -> directory.readonly scope not authorized via DWD")
        return False
    except Exception as err:
        print(f"  FAIL (unexpected): {type(err).__name__}: {err}")
        return False

    people = result.get("people", [])
    print(f"  OK - directory returned {len(people)} entries")
    for p in people[:3]:
        names = p.get("names", [])
        emails = p.get("emailAddresses", [])
        display = names[0].get("displayName", "?") if names else "?"
        email = emails[0].get("value", "?") if emails else "?"
        print(f"    - {email:45s}  {display}")
    return True


def test_cross_user(first_events: list[dict], working_scope: str) -> bool:
    """Bonus: prove DWD works for a *different* user (Lambda will do this)."""
    print("\n[4/4] Cross-user impersonation - proving DWD works org-wide")
    other = None
    for e in first_events:
        for a in e.get("attendees", []):
            email = a.get("email", "")
            if email.endswith("@kiboventures.com") and email != DELEGATED_USER:
                other = email
                break
        if other:
            break
    if not other:
        print("  SKIP - no other @kiboventures.com attendee found in recent events")
        return True

    print(f"  Attempting to read {other}'s calendar with scope {working_scope.rsplit('/', 1)[-1]}...")
    try:
        service = _build_calendar_service([working_scope], other)
        result = service.events().list(
            calendarId="primary", maxResults=1, singleEvents=True,
        ).execute()
        count = len(result.get("items", []))
        print(f"  OK - successfully impersonated {other}, got {count} event(s)")
        return True
    except HttpError as err:
        print(f"  FAIL: HTTP {err.resp.status} - {err.reason}")
        return False
    except Exception as err:
        print(f"  FAIL at auth: {err}")
        return False


def main() -> int:
    print(f"service-account.json: {SA_FILE}")
    print(f"Exists: {SA_FILE.exists()}")
    if not SA_FILE.exists():
        return 1

    cal_result = test_calendar_list(DELEGATED_USER)
    if cal_result is None:
        print("\nCalendar access failed - cannot proceed. See error above.")
        return 1

    attendee_ok = test_attendee_read(cal_result["events"])
    people_ok = test_people_directory(DELEGATED_USER)
    cross_ok = test_cross_user(cal_result["events"], cal_result["scope"])

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Calendar list+attendees (critical):  {'PASS' if attendee_ok else 'FAIL'}")
    print(f"  People API directory (optional):     {'PASS' if people_ok else 'FAIL'}")
    print(f"  Cross-user impersonation (critical): {'PASS' if cross_ok else 'FAIL'}")

    if attendee_ok and cross_ok:
        print("\nVerdict: service account is ready for Lambda use.")
        if not people_ok:
            print("Note: directory lookup unavailable - fall back to Calendar's own")
            print("      attendee.displayName instead of People API enrichment.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
