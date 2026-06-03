"""One-off diagnostic: can the service account read Vicente's calendar?

Replicates the pipeline's GCal attendee lookup for the 2026-05-28 Access
Capital meeting, impersonating each candidate user, and prints what events +
attendees come back. Read-only. Not part of the pipeline.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build

SA_FILE = ".secrets/service-account.json"
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Notion meeting: "Ext.call Access Cap- Nzyme re: Portugal" @ 2026-05-28 15:00 (GMT+2)
NOTION_DT = datetime.fromisoformat("2026-05-28T15:00:00+02:00")
KEYWORD = "Access Cap"

# Who the pipeline might impersonate: the page creator, else the default.
CANDIDATES = ["vicente@kiboventures.com", "nzyme@kiboventures.com"]


def svc(user: str):
    info = json.load(open(SA_FILE, encoding="utf-8"))
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=SCOPES, subject=user,
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def probe(user: str) -> None:
    print(f"\n{'='*70}\nImpersonating: {user}\n{'='*70}")
    try:
        service = svc(user)
    except Exception as e:
        print(f"  AUTH FAILED building service: {e!r}")
        return

    time_min = (NOTION_DT - timedelta(hours=12)).isoformat()
    time_max = (NOTION_DT + timedelta(hours=12)).isoformat()

    # Pass 1: keyword search (what the pipeline does first)
    try:
        items = (
            service.events().list(
                calendarId="primary", q=KEYWORD,
                timeMin=time_min, timeMax=time_max,
                singleEvents=True, orderBy="startTime",
            ).execute().get("items", [])
        )
    except Exception as e:
        print(f"  CALENDAR READ FAILED: {e!r}")
        return

    print(f"  keyword '{KEYWORD}' search -> {len(items)} event(s)")
    for ev in items:
        _dump(ev)

    # Pass 2: full time-range scan (the pipeline's fallback)
    allev = (
        service.events().list(
            calendarId="primary",
            timeMin=time_min, timeMax=time_max,
            singleEvents=True, orderBy="startTime",
        ).execute().get("items", [])
    )
    print(f"  full ±12h window -> {len(allev)} event(s) total:")
    for ev in allev:
        s = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
        vis = ev.get("visibility", "default")
        natt = len(ev.get("attendees", []) or [])
        print(f"    - '{ev.get('summary')}' @ {s}  visibility={vis} attendees={natt}")


def _dump(ev: dict) -> None:
    s = ev.get("start", {}).get("dateTime")
    print(f"    MATCH: '{ev.get('summary')}' @ {s}  visibility={ev.get('visibility','default')}")
    print(f"      organizer: {ev.get('organizer', {}).get('email')}")
    atts = ev.get("attendees", []) or []
    if not atts:
        print("      attendees: NONE on event")
    for a in atts:
        print(f"      attendee: {a.get('email')}  ({a.get('displayName','')})  resp={a.get('responseStatus')}")


if __name__ == "__main__":
    for u in CANDIDATES:
        probe(u)
