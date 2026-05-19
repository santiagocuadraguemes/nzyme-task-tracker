"""Count "real" meetings per person over the last 14 calendar days.

Reads the service-account JSON at .secrets/service-account.json (DWD), impersonates
each target user, queries primary calendar, filters out noise, and prints a markdown
table grouped by tier + a per-tier "average of averages".

Run from repo root:
    ../venv/Scripts/python scripts/meeting_count_per_person.py
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

SA_FILE = Path(__file__).resolve().parent.parent / ".secrets" / "service-account.json"
SCOPES = ["https://www.googleapis.com/auth/calendar"]
TZ = ZoneInfo("Europe/Madrid")

TIERS: dict[str, list[str]] = {
    "Partners / Directors": [
        "juan@kiboventures.com",
        "vicente@kiboventures.com",
        "reyes@kiboventures.com",
    ],
    "Associates / VPs": [
        "alf@kiboventures.com",
        "gpa@kiboventures.com",
        "jaimegervas@kiboventures.com",
    ],
    "Interns / Junior Analysts": [
        "santiago@kiboventures.com",
        "jaaz@kiboventures.com",
        "nacho@kiboventures.com",
    ],
}

NOISE_PREFIX_PATTERNS = [
    r"^ooo\b",
    r"^out of office\b",
    r"^focus\b",
    r"^holiday\b",
    r"^vacaciones\b",
    r"^comida\b",
    r"^lunch\b",
]
NOISE_RE = re.compile("|".join(NOISE_PREFIX_PATTERNS), re.IGNORECASE)


@dataclass
class PersonStats:
    email: str
    total: int
    workdays: int

    @property
    def per_workday(self) -> float:
        return self.total / self.workdays if self.workdays else 0.0


def load_sa_info() -> dict:
    with SA_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def build_service(sa_info: dict, delegated_user: str):
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=SCOPES, subject=delegated_user,
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def workday_count(start: date, end_exclusive: date) -> int:
    """Count Mon-Fri days in [start, end_exclusive)."""
    n = 0
    d = start
    while d < end_exclusive:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def is_real_meeting(event: dict, person_email: str) -> bool:
    if event.get("status") == "cancelled":
        return False

    summary = (event.get("summary") or "").strip()
    if NOISE_RE.search(summary):
        return False

    # all-day events have "date" instead of "dateTime"
    start = event.get("start", {})
    end = event.get("end", {})
    if "date" in start or "date" in end:
        return False
    if "dateTime" not in start or "dateTime" not in end:
        return False

    try:
        start_dt = datetime.fromisoformat(start["dateTime"])
        end_dt = datetime.fromisoformat(end["dateTime"])
    except ValueError:
        return False
    if end_dt <= start_dt:
        return False

    attendees = event.get("attendees", []) or []
    real_attendees = [a for a in attendees if not a.get("resource")]
    other_emails = {
        (a.get("email") or "").lower()
        for a in real_attendees
        if (a.get("email") or "").lower() != person_email.lower()
    }
    other_emails.discard("")
    if len(other_emails) < 1:
        return False

    for a in real_attendees:
        if (a.get("email") or "").lower() == person_email.lower():
            if a.get("responseStatus") == "declined":
                return False
            break

    return True


def list_events(service, time_min_iso: str, time_max_iso: str) -> list[dict]:
    events: list[dict] = []
    page_token: str | None = None
    while True:
        resp = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min_iso,
                timeMax=time_max_iso,
                singleEvents=True,
                orderBy="startTime",
                maxResults=2500,
                pageToken=page_token,
            )
            .execute()
        )
        events.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return events


def analyze_person(sa_info: dict, email: str, window_start: datetime, window_end: datetime) -> PersonStats:
    service = build_service(sa_info, email)
    events = list_events(service, window_start.isoformat(), window_end.isoformat())
    real = [e for e in events if is_real_meeting(e, email)]
    workdays = workday_count(window_start.date(), window_end.date())
    return PersonStats(email=email, total=len(real), workdays=workdays)


def main() -> None:
    sa_info = load_sa_info()

    now_madrid = datetime.now(TZ)
    window_end = datetime.combine(now_madrid.date(), time(0, 0), tzinfo=TZ)
    window_start = window_end - timedelta(days=14)

    print(f"Window (Europe/Madrid): {window_start.date()} -> {window_end.date()} (exclusive)")
    print(f"Workdays in window: {workday_count(window_start.date(), window_end.date())}\n")

    print("| Tier | Persona | Reuniones totales (14d) | Días laborables analizados | Media reuniones / día laborable |")
    print("|---|---|---|---|---|")

    tier_avgs: dict[str, list[float]] = {}
    for tier, emails in TIERS.items():
        tier_avgs[tier] = []
        for email in emails:
            try:
                stats = analyze_person(sa_info, email, window_start, window_end)
                tier_avgs[tier].append(stats.per_workday)
                print(
                    f"| {tier} | {email} | {stats.total} | {stats.workdays} | {stats.per_workday:.2f} |"
                )
            except Exception as exc:  # noqa: BLE001
                print(f"| {tier} | {email} | ERROR | - | {exc!r} |")

    print()
    print("Resumen por tier (media de las medias):")
    for tier, avgs in tier_avgs.items():
        if avgs:
            mean_of_means = sum(avgs) / len(avgs)
            print(f"  {tier}: {mean_of_means:.2f} reuniones / día laborable")
        else:
            print(f"  {tier}: (sin datos)")


if __name__ == "__main__":
    main()
