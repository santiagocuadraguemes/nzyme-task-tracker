"""Read-only tool-usage report per member.

For each Org Chart row that has a Meeting Notes DB URL, pulls:
  - Their primary Google Calendar events in the window (DWD impersonation)
  - Their Notion Meeting Notes DB pages in the window
  - Per-page tagging + manual-notes signal

Prints a markdown table. NO writes anywhere.

Run from repo root:
    ../venv/Scripts/python scripts/usage_stats.py
    ../venv/Scripts/python scripts/usage_stats.py --days 30 --include-inactive
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from notion_client import Client as NotionClient
from rapidfuzz import fuzz

from src.meeting_db_registry import discover_meeting_dbs
from src.notion_client_wrapper import NotionClientWrapper

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("usage_stats")

REPO_ROOT = Path(__file__).resolve().parent.parent
SA_FILE = REPO_ROOT / ".secrets" / "service-account.json"
SCOPES = ["https://www.googleapis.com/auth/calendar"]  # only scope on DWD allowlist
TZ = ZoneInfo("Europe/Madrid")

# Out-of-domain members can't be impersonated directly (their domain isn't on
# the SA's DWD allowlist). Instead we impersonate an in-domain proxy who has
# "see all event details" sharing access to their calendar, and read the
# target calendar by its email ID. Mirrors the prod GCAL_PROXY_* mechanism.
PROXY_USER = "mar@kiboventures.com"
PROXY_DOMAINS = {"nzalpha.com"}


def _impersonation_target(email: str) -> tuple[str, str]:
    """Return (user_to_impersonate, calendar_id_to_read) for a member email."""
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    if domain in PROXY_DOMAINS:
        return PROXY_USER, email
    return email, "primary"

# Strip the standard Notion auto-suffix (e.g. " (Mon 12/05 10:00)") before fuzzy match
_TITLE_SUFFIX_RE = re.compile(r"\s+\([^()]*\d[^()]*\)\s*$")

# Heuristics: events that aren't "meetings" in the conversational sense
NOISE_PREFIX_PATTERNS = [
    r"^ooo\b", r"^out of office\b", r"^focus\b", r"^focus time\b",
    r"^holiday\b", r"^vacaciones\b", r"^comida\b", r"^lunch\b",
    r"^travel\b", r"^viaje\b", r"^transit\b", r"^commute\b",
    r"^home\b", r"^office\b", r"^block\b", r"^busy\b",
    r"^almuerzo\b", r"^break\b", r"^do not schedule\b",
]
NOISE_RE = re.compile("|".join(NOISE_PREFIX_PATTERNS), re.IGNORECASE)

# Template tokens that don't count as "manual notes"
TEMPLATE_TOKENS = {
    "action items", "ai meeting notes", "meeting notes", "notes",
    "enter action items here", "next steps", "summary", "meeting summary",
    "attendees", "agenda", "decisions",
}

MIN_MANUAL_NOTE_CHARS = 30
MATCH_FUZZY_THRESHOLD = 60
# Day tolerance when tying a note to a calendar event (note Date / created_time
# can lag the meeting by a day or two for batch-created / transcript notes).
MATCH_DAY_WINDOW = 2


@dataclass
class MemberStats:
    name: str
    email: str
    db_id: str
    active: bool

    cal_total: int = 0
    cal_real: int = 0
    cal_recorded: int = 0          # of cal_real, how many matched a Notion page

    notion_total: int = 0
    notion_matched_cal: int = 0    # Notion pages that matched a calendar event
    notion_only: int = 0           # Notion pages with no calendar match
    notion_tagged: int = 0         # any of Macro Work Block / Detail / External Org set
    notion_tag_mwb: int = 0
    notion_tag_detail: int = 0
    notion_tag_external_org: int = 0
    notion_with_manual_notes: int = 0

    notion_template_injected: int = 0
    notion_processed: int = 0

    notion_unmatched: list[dict] = field(default_factory=list)

    error: str = ""

    @property
    def recording_rate(self) -> str:
        # "Of the real meetings on your calendar, how many did you write a note
        # for." Recorded = every Notion meeting note (each is a real meeting).
        # Capped at 100% — a few notes tie to events filtered as non-"real".
        if not self.cal_real:
            return "-"
        return f"{min(100, round(100 * self.notion_total / self.cal_real))}%"

    @property
    def cal_confirmed_rate(self) -> str:
        # QA cross-check: of the notes, how many tie back to a calendar event.
        if not self.notion_total:
            return "-"
        return f"{100 * self.notion_matched_cal / self.notion_total:.0f}%"

    @property
    def tagging_rate(self) -> str:
        # "Of the meetings you recorded, how many are tagged" — over all notes.
        if not self.notion_total:
            return "-"
        return f"{100 * self.notion_tagged / self.notion_total:.0f}%"

    @property
    def manual_notes_rate(self) -> str:
        if not self.notion_total:
            return "-"
        return f"{100 * self.notion_with_manual_notes / self.notion_total:.0f}%"


def _strip_title_suffix(title: str) -> str:
    return _TITLE_SUFFIX_RE.sub("", title or "").strip()


def _clean_for_match(title: str) -> str:
    t = _strip_title_suffix(title).lower()
    # Strip accents/diacritics so "Reunión" == "Reunion" (titles are EN/ES mixed).
    t = "".join(
        c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c)
    )
    # Drop common separators that the calendar uses but Notion titles may not
    # (and vice-versa): <>, |, /, dashes, colons.
    t = re.sub(r"[<>|/:\-–—]+", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _is_real_meeting(event: dict, person_email: str) -> bool:
    if event.get("status") == "cancelled":
        return False

    summary = (event.get("summary") or "").strip()
    if NOISE_RE.search(summary):
        return False

    start = event.get("start", {})
    end = event.get("end", {})
    # All-day blocks have "date" instead of "dateTime"
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


def _load_sa_info() -> dict:
    with SA_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def _build_calendar(sa_info: dict, delegated_user: str):
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=SCOPES, subject=delegated_user,
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _list_calendar_events(service, time_min_iso: str, time_max_iso: str,
                          calendar_id: str = "primary") -> list[dict]:
    events: list[dict] = []
    page_token: str | None = None
    while True:
        resp = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min_iso,
            timeMax=time_max_iso,
            singleEvents=True,
            orderBy="startTime",
            maxResults=2500,
            pageToken=page_token,
        ).execute()
        events.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return events


def _event_start_dt(event: dict) -> datetime | None:
    dt_str = event.get("start", {}).get("dateTime")
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str)
    except ValueError:
        return None


def _list_notion_pages(client: NotionClientWrapper, db_id: str,
                       window_start_utc: datetime, window_end_utc: datetime) -> list[dict]:
    """Pages whose **meeting Date** falls in the window.

    We window by the `Date` property (the date the meeting happened), NOT
    `created_time`: notes are often back-filled days/weeks after the meeting,
    and someone typing up an old meeting today must not be counted as a meeting
    "in the last 30 days" (it also can never match a calendar event in the
    window). Date-windowing keeps the recorded count and the calendar pull on
    the same real-world period."""
    resp = client.query_database(
        database_id=db_id,
        filter={
            "and": [
                {"property": "Date", "date": {"on_or_after": window_start_utc.isoformat()}},
                {"property": "Date", "date": {"before": window_end_utc.isoformat()}},
            ],
        },
        sorts=[{"property": "Date", "direction": "ascending"}],
    )
    return resp.get("results", [])


# Pages that aren't real meetings: leftover template stubs and test scratch.
_JUNK_TITLE_RE = re.compile(
    r"^\s*(template\s*\d*|test(\s+\w+)*|untitled|nueva página|new page)\s*$",
    re.IGNORECASE,
)


def _is_junk_page(page: dict) -> bool:
    return bool(_JUNK_TITLE_RE.match(_page_title(page) or ""))


def _page_title(page: dict) -> str:
    props = page.get("properties", {})
    for p in props.values():
        if p.get("type") == "title":
            return "".join(rt.get("plain_text", "") for rt in p.get("title", []))
    return ""


def _page_date(page: dict) -> datetime | None:
    """Prefer Date property; fall back to created_time."""
    date_prop = (page.get("properties", {}).get("Date") or {}).get("date") or {}
    start = date_prop.get("start")
    if start:
        try:
            return datetime.fromisoformat(start)
        except ValueError:
            pass
    ct = page.get("created_time")
    if ct:
        try:
            return datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _page_tag_flags(page: dict) -> tuple[bool, bool, bool]:
    """Return (has_macro_work_block, has_detail, has_external_org)."""
    props = page.get("properties", {})
    mwb = (props.get("Macro Work Block") or {}).get("select")
    has_mwb = bool(mwb and mwb.get("name"))
    det = (props.get("Detail") or {}).get("multi_select") or []
    has_det = bool(det)
    eo = (props.get("External Org") or {}).get("select")
    has_eo = bool(eo and eo.get("name"))
    return has_mwb, has_det, has_eo


def _page_has_tags(page: dict) -> bool:
    return any(_page_tag_flags(page))


def _page_processed_template(page: dict) -> tuple[bool, bool]:
    props = page.get("properties", {})
    proc = (props.get("Processed") or {}).get("checkbox", False)
    tinj = (props.get("Template Injected") or {}).get("checkbox", False)
    return bool(proc), bool(tinj)


_AI_BLOCK_TYPES = ("meeting_notes", "transcription")
_MAX_RECURSE_DEPTH = 4


def _extract_human_text_from_blocks(blocks: list[dict]) -> str:
    """Concatenate plain_text from top-level human-content blocks."""
    parts: list[str] = []
    for b in blocks:
        btype = b.get("type", "")
        if btype in _AI_BLOCK_TYPES:
            continue
        data = b.get(btype) or {}
        rich = data.get("rich_text") or []
        text = "".join(rt.get("plain_text", "") for rt in rich)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _recurse_text(client: NotionClientWrapper, block_id: str, depth: int = 0) -> str:
    """Recurse through a block tree collecting human plain_text."""
    if depth > _MAX_RECURSE_DEPTH:
        return ""
    try:
        children = client.get_block_children(block_id)
    except Exception:  # noqa: BLE001
        return ""
    parts: list[str] = []
    for c in children:
        btype = c.get("type", "")
        if btype in _AI_BLOCK_TYPES:
            continue
        data = c.get(btype) or {}
        rich = data.get("rich_text") or []
        text = "".join(rt.get("plain_text", "") for rt in rich)
        if text:
            parts.append(text)
        if c.get("has_children"):
            parts.append(_recurse_text(client, c["id"], depth + 1))
    return "\n".join(p for p in parts if p)


def _meeting_notes_text(client: NotionClientWrapper, blocks: list[dict]) -> str:
    """Text written inside the AI Meeting Notes / Transcription block, if any."""
    parts: list[str] = []
    for b in blocks:
        btype = b.get("type")
        if btype not in _AI_BLOCK_TYPES:
            continue
        notes_block_id = (
            (b.get(btype) or {}).get("children", {}).get("notes_block_id")
        )
        if not notes_block_id:
            continue
        # Recurse from the notes_block_id container all the way down
        parts.append(_recurse_text(client, notes_block_id))
    return "\n".join(p for p in parts if p)


def _has_manual_notes(text: str) -> bool:
    """True if the human-block text contains substantive content beyond template."""
    if not text:
        return False
    # Tokenize lines; drop ones that are pure template tokens
    keep: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lower()
        if not line:
            continue
        if line in TEMPLATE_TOKENS:
            continue
        # Drop pure-punctuation / single bullet artifacts
        if re.fullmatch(r"[\W_]+", line):
            continue
        keep.append(line)
    body = " ".join(keep)
    return len(body) >= MIN_MANUAL_NOTE_CHARS


def _fetch_page_children_text(client: NotionClientWrapper, page_id: str) -> str:
    """Top-level human blocks + any text inside the AI Meeting Notes notes section."""
    blocks = client.get_block_children(page_id)
    top = _extract_human_text_from_blocks(blocks)
    inside = _meeting_notes_text(client, blocks)
    return top + "\n" + inside


def _title_score(ptitle: str, summary: str) -> int:
    """Fuzzy title similarity, with a containment boost.

    token_set_ratio already rewards subset overlap, but explicitly boost the
    case where one cleaned title contains the other ("acme" ⊂ "kibo acme intro
    call") so renamed / shortened Notion titles still match their invite.
    """
    if not ptitle or not summary:
        return 0
    # Notion titles are often far more verbose than the calendar invite
    # ("Nzyme. Fundraising. SYZ Capital. Portugal" vs "Ext.call SYZ Capital -
    # Nzyme re: Portugal"). partial_token_set_ratio rewards the shared core
    # without penalising the extra words, so take the strongest signal.
    base = max(
        fuzz.token_set_ratio(ptitle, summary),
        fuzz.partial_token_set_ratio(ptitle, summary),
    )
    if ptitle in summary or summary in ptitle:
        base = max(base, 90)
    return int(base)


def _match_pages_to_events(
    pages: list[dict], events: list[dict]
) -> tuple[set[str], set[str], list[dict]]:
    """Return (matched_page_ids, matched_event_ids, unmatched_debug).

    Greedy: for each Notion page (sorted by date) pick the best calendar event
    within ±MATCH_DAY_WINDOW days whose title score >= threshold; once chosen,
    that event is consumed. `unmatched_debug` lists notes that found no event,
    with the best near-miss, so we can tell true orphans from matcher failures.
    """
    matched_pages: set[str] = set()
    matched_events: set[str] = set()
    unmatched: list[dict] = []

    # Index events by local-date string for fast day lookup
    events_by_day: dict[str, list[dict]] = {}
    for e in events:
        start_dt = _event_start_dt(e)
        if not start_dt:
            continue
        day_local = start_dt.astimezone(TZ).date().isoformat()
        events_by_day.setdefault(day_local, []).append(e)

    deltas = [0]
    for d in range(1, MATCH_DAY_WINDOW + 1):
        deltas.extend([-d, d])

    for page in pages:
        pid = page["id"]
        pdate = _page_date(page)
        ptitle = _clean_for_match(_page_title(page))

        candidates: list[dict] = []
        if pdate:
            day_local = pdate.astimezone(TZ).date()
            for delta in deltas:
                d = (day_local + timedelta(days=delta)).isoformat()
                for e in events_by_day.get(d, []):
                    if e.get("id") in matched_events:
                        continue
                    candidates.append(e)

        if candidates and ptitle:
            best = max(candidates, key=lambda e: _title_score(ptitle, _clean_for_match(e.get("summary") or "")))
            best_score = _title_score(ptitle, _clean_for_match(best.get("summary") or ""))
            if best_score >= MATCH_FUZZY_THRESHOLD:
                matched_pages.add(pid)
                matched_events.add(best["id"])
                continue
            unmatched.append({
                "title": _page_title(page),
                "date": pdate.astimezone(TZ).date().isoformat() if pdate else "?",
                "best_score": best_score,
                "best_event": best.get("summary") or "",
            })
        else:
            unmatched.append({
                "title": _page_title(page),
                "date": pdate.astimezone(TZ).date().isoformat() if pdate else "?",
                "best_score": 0,
                "best_event": "(no calendar event near this day)",
            })

    return matched_pages, matched_events, unmatched


def collect_stats(member, sa_info: dict, notion: NotionClientWrapper,
                  window_start_utc: datetime, window_end_utc: datetime) -> MemberStats:
    stats = MemberStats(
        name=member.owner_name or "?",
        email=member.owner_email or "",
        db_id=member.db_id,
        active=member.active,
    )

    # --- Calendar ---
    try:
        if not stats.email:
            stats.error = "no email"
        else:
            impersonate, calendar_id = _impersonation_target(stats.email)
            cal = _build_calendar(sa_info, impersonate)
            events = _list_calendar_events(
                cal, window_start_utc.isoformat(), window_end_utc.isoformat(),
                calendar_id=calendar_id,
            )
            stats.cal_total = len(events)
            all_events = events
            real_events = [e for e in events if _is_real_meeting(e, stats.email)]
            stats.cal_real = len(real_events)
    except Exception as exc:  # noqa: BLE001
        stats.error = f"calendar: {exc!r}"
        all_events = []
        real_events = []

    # --- Notion pages ---
    try:
        pages = _list_notion_pages(notion, stats.db_id, window_start_utc, window_end_utc)
    except Exception as exc:  # noqa: BLE001
        stats.error = (stats.error + "; " if stats.error else "") + f"notion: {exc!r}"
        pages = []

    # Drop template stubs / test scratch — they aren't real meetings.
    pages = [p for p in pages if not _is_junk_page(p)]

    stats.notion_total = len(pages)

    # --- Matching (QA cross-check only — does NOT gate the recorded count) ---
    # Every Notion note is a recorded meeting. We still tie each note back to a
    # calendar event purely to confirm the ~99% correspondence and surface true
    # orphans. Match against ALL events (not just "real" ones) so the noise /
    # attendee filter can't spuriously drop a legitimate note's event.
    matched_pages, matched_events, unmatched = _match_pages_to_events(pages, all_events)
    stats.notion_matched_cal = len(matched_pages)
    stats.notion_only = stats.notion_total - stats.notion_matched_cal
    stats.notion_unmatched = unmatched
    stats.cal_recorded = len(matched_events)

    for page in pages:
        proc, tinj = _page_processed_template(page)
        if proc:
            stats.notion_processed += 1
        if tinj:
            stats.notion_template_injected += 1
        # Tagging + manual notes count over EVERY recorded note (a note that
        # failed the calendar cross-check is still a recorded meeting).
        has_mwb, has_det, has_eo = _page_tag_flags(page)
        if has_mwb or has_det or has_eo:
            stats.notion_tagged += 1
        if has_mwb:
            stats.notion_tag_mwb += 1
        if has_det:
            stats.notion_tag_detail += 1
        if has_eo:
            stats.notion_tag_external_org += 1
        try:
            text = _fetch_page_children_text(notion, page["id"])
            if _has_manual_notes(text):
                stats.notion_with_manual_notes += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("page content fetch failed for %s: %r", page.get("id"), exc)

    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30,
                        help="Window length in days (default 30, ending today)")
    parser.add_argument("--include-inactive", action="store_true",
                        help="Include Org Chart rows with Active=false (default: included; flag is for clarity)")
    parser.add_argument("--only-active", action="store_true",
                        help="Restrict to Active=true rows")
    parser.add_argument("--debug-unmatched", action="store_true",
                        help="Print each Notion note that did NOT tie to a calendar event")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")

    token = os.environ["NOTION_API_TOKEN"]
    org_chart_db_id = os.environ["ORG_CHART_DB_ID"]
    os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_FILE", str(SA_FILE))

    notion = NotionClientWrapper(NotionClient(auth=token))
    members = discover_meeting_dbs(notion, org_chart_db_id, include_inactive=not args.only_active)

    if not args.only_active:
        # discover_meeting_dbs already returns inactive ones too when include_inactive=True.
        pass

    # Window: last N days, ending at today 00:00 Madrid (exclusive end is now)
    now_madrid = datetime.now(TZ)
    window_end = now_madrid  # up to "now"
    window_start = window_end - timedelta(days=args.days)
    window_start_utc = window_start.astimezone(timezone.utc)
    window_end_utc = window_end.astimezone(timezone.utc)

    print(f"Window: {window_start.isoformat()} -> {window_end.isoformat()} ({args.days}d, Europe/Madrid)")
    print(f"Members with a Meeting Notes DB: {len(members)}\n")

    sa_info = _load_sa_info()

    all_stats: list[MemberStats] = []
    for m in sorted(members, key=lambda x: x.owner_name or ""):
        print(f"  … {m.owner_name} ({m.owner_email})", flush=True)
        s = collect_stats(m, sa_info, notion, window_start_utc, window_end_utc)
        all_stats.append(s)

    # --- Render ---
    # Recorded = every Notion meeting note (each is a real meeting). Recording
    # rate = recorded / real calendar meetings. "Cal ✓" is a QA cross-check:
    # what fraction of notes still tie back to a calendar event.
    print()
    print("| Member | Act | Real meetings (cal) | Recorded (Notion) | Recording rate | Cal match | Tagged (of rec.) | MWB | Detail | ExtOrg | Manual notes | Tmpl inj | Err |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    totals = {k: 0 for k in ("cal_total", "cal_real", "notion_matched_cal",
                              "notion_total", "notion_tagged",
                              "notion_tag_mwb", "notion_tag_detail", "notion_tag_external_org",
                              "notion_with_manual_notes", "notion_template_injected")}
    for s in all_stats:
        print(
            f"| {s.name} | {'Y' if s.active else 'n'} "
            f"| {s.cal_real} "
            f"| {s.notion_total} "
            f"| {s.recording_rate} "
            f"| {s.cal_confirmed_rate} "
            f"| {s.notion_tagged} ({s.tagging_rate}) "
            f"| {s.notion_tag_mwb} "
            f"| {s.notion_tag_detail} "
            f"| {s.notion_tag_external_org} "
            f"| {s.notion_with_manual_notes} ({s.manual_notes_rate}) "
            f"| {s.notion_template_injected} "
            f"| {'Y' if s.error else ''} |",
        )
        for k in totals:
            totals[k] += getattr(s, k)
    print()
    for s in all_stats:
        if s.error:
            print(f"  err [{s.name}]: {s.error}")

    print()
    print("Totals:")
    print(f"  Real calendar meetings:         {totals['cal_real']} (of {totals['cal_total']} raw events)")
    print(f"  Recorded meetings (Notion notes): {totals['notion_total']}"
          f" ({min(100, round(100 * totals['notion_total'] / max(1, totals['cal_real'])))}% of real meetings)")
    print(f"  Of recorded, tie to a calendar event (QA): {totals['notion_matched_cal']}"
          f" ({100 * totals['notion_matched_cal'] / max(1, totals['notion_total']):.0f}%)")
    print(f"  Recorded meetings tagged:       {totals['notion_tagged']}"
          f" ({100 * totals['notion_tagged'] / max(1, totals['notion_total']):.0f}% of recorded)")
    print(f"  Recorded meetings w/ manual notes: {totals['notion_with_manual_notes']}"
          f" ({100 * totals['notion_with_manual_notes'] / max(1, totals['notion_total']):.0f}% of recorded)")
    print(f"  Notion pages w/ template inj.:  {totals['notion_template_injected']}"
          f" ({100 * totals['notion_template_injected'] / max(1, totals['notion_total']):.0f}%)")

    if args.debug_unmatched:
        print()
        print("Notes that did NOT tie to a calendar event (QA — expected ~1%):")
        for s in all_stats:
            if not s.notion_unmatched:
                continue
            print(f"\n  [{s.name}] {len(s.notion_unmatched)} unmatched of {s.notion_total}:")
            for u in s.notion_unmatched:
                print(f"    - {u['date']}  \"{u['title']}\"  (best {u['best_score']}: \"{u['best_event']}\")")


if __name__ == "__main__":
    main()
