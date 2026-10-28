#!/usr/bin/env python3
"""
Cloud version: detects new Séjourné EU Commission events and sends ntfy notifications.
Runs on GitHub Actions every hour — no Mac needed.

Detection strategies (combined, deduplicated):
  1. Aggregate college calendar page — scraped UNFILTERED, then filtered
     client-side by name ("journ"). Does NOT depend on Séjourné appearing in
     the site's commissioner dropdown/facet (that facet has dropped him before
     and the whole oe-list-pages filter/pagination is currently buggy).
  2. EU Transparency Register meetings — his own + his cabinet's meetings with
     interest representatives. Structured, JS-free, currently the only live
     source while the aggregate page's pagination is broken.
  3. The ICS file committed to this repo — preserves history (incl. events the
     Mac script found) so nothing is ever lost between runs.
  4. EC Presscorner — his speeches, statements and read-outs (machine-readable
     JSON API). Catches public-appearance events the agenda/register miss.
  5. EU Transparency Register missions — his official travel (per-host XLSX
     export, parsed with the stdlib). Real "where is he" itinerary data:
     Prague, Washington, Davos… with dates + locations.

Strategies 1–5 feed the ICS. A sixth, non-ICS channel (Google News) sends
"sightings" notifications only — see notify_news().

Design note: strategies 1 and 2 fail in *different* ways — (1) survives the
facet dropping him (name filter), (2) survives the aggregate page going stale.
Together they self-heal: when the EU fixes pagination, (1) auto-resumes full
coverage with zero code changes. Each added source (4, 5, news) is independent
and degrades gracefully — a failure in one prints a warning and returns [].
"""

import hashlib
import html
import io
import json
import os
import re
import time
import unicodedata
import urllib.parse
import zipfile
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────

# Aggregate calendar page — NO server-side commissioner filter on purpose.
# We fetch everyone and match by name client-side, so we never depend on the
# site listing Séjourné in its (frequently broken) facet dropdown.
BASE_URL = (
    "https://commission.europa.eu/about/organisation/college-commissioners"
    "/calendar-items-president-and-commissioners_en"
)

# EU Transparency Register — meeting listings keyed by "host" UUID.
TRANSPARENCY_BASE = "https://ec.europa.eu/transparency-initiative/meetings/meeting.do?host="
TRANSPARENCY_HOSTS = [
    # (label, host UUID, title prefix used for events)
    ("self",    "d8fba42d-8cc3-42c8-b1f1-e07d9b2ee8ea", "Séjourné meets"),
    ("cabinet", "21deeb50-48f9-40a3-9ab0-ac66cdbb2ca2", "Séjourné Cabinet meets"),
    # To track more channels, add cabinet members' host UUIDs here. Find them on
    # the member's Transparency page (…/meeting.do?host=<UUID>); the label/prefix
    # is free-text used only in notifications.
]

# EC Presscorner — speeches / statements / read-outs that mention Séjourné.
PRESSCORNER_API    = "https://ec.europa.eu/commission/presscorner/api/search"
PRESSCORNER_DETAIL = "https://ec.europa.eu/commission/presscorner/detail/en/"
# Event-like doc types only. Deliberately excludes MEX (daily-news round-ups)
# and CLDR (the weekly college calendar) — those merely mention him in passing.
PRESSCORNER_TYPES  = {"SPEECH", "STATEMENT", "READ"}

# Transparency Register missions (official travel) — per-host XLSX export.
MISSIONS_EXPORT = (
    "https://ec.europa.eu/transparency-initiative/meetings"
    "/data/missions/commissioners/export?hostId="
)

# EbS (audiovisual.ec.europa.eu) event-planning — the EC AV service's schedule
# of upcoming covered events, incl. Commissioners' trips abroad. Served by the
# same avsportal Solr API as the photo library (type=EVENTPLANNING); the old
# parrotfish /search endpoint now 404s and silently returned 0 items.
EBS_TZ = ZoneInfo("Europe/Brussels")   # shootstartdate is Brussels local time

# EU Audiovisual Service photo/video library — his team often doesn't put
# things on the official agenda, but the EC photographers still cover them and
# caption each reportage with the exact activity. Past events only (photos are
# taken on the day); fills the gaps in the agenda. Same internal Solr API the
# photo-monitor repo uses: no server-side keyword filter, match client-side.
EUAV_API     = "https://gfdwwnbuul.execute-api.eu-west-1.amazonaws.com/avsportal/avsportal"
EUAV_PHOTO   = "https://audiovisual.ec.europa.eu/en/media/photo/"
EUAV_VIDEO   = "https://audiovisual.ec.europa.eu/en/media/video/"
EUAV_PERSON  = "241448"      # EU AV person id for Stéphane Séjourné (pers_json)
EUAV_TAG     = "[EU_AV]"     # marker in DESCRIPTION; sa-archive / news monitor read it

# Weekly College meeting — his own calendar is rarely updated, so we assume he
# attends every College meeting (the user's explicit choice) and put it on the
# calendar ahead of time. Convention: Wednesday in Brussels; Tuesday in
# Strasbourg during EP plenary weeks (read from the EP open-data API, with a
# hardcoded 2026 fallback). Skipped in August and on days he already has a
# known engagement. Regenerated every run (old copies in the ICS are dropped),
# so a prediction disappears as soon as a real event lands on that day.
# The convention has exceptions (e.g. Tue 2026-10-27 in Brussels), so real
# College dates override it week by week: von der Leyen's entries on the
# aggregate calendar page (forward-looking) and EC photo reportages of the
# meeting (after the fact). A week with a known College date gets that date,
# or nothing if it already happened.
EP_API          = "https://data.europarl.europa.eu/api/v2"
COLLEGE_TAG     = "[EU_AUTO]"   # sa-archive renders this as 🇪🇺 例会·推测
COLLEGE_WEEKS   = 6
COLLEGE_TITLE   = "Weekly College of Commissioners meeting (expected)"
COLLEGE_URL     = "https://commission.europa.eu/about/organisation/college-commissioners_en"
COLLEGE_TITLE_DATED = "Weekly College of Commissioners meeting (date per President's agenda)"
COLLEGE_RE = re.compile(   # not "College of Europe" events
    r"\b(weekly|hebdomadaire)\b.{0,30}\b(college|collège|von der leyen commission)\b"
    r"|\bcollege (of commissioners )?meeting"
    r"|\b(meeting|réunion) (of|du) (the )?(college|collège)\b(?! (of|d.)europe)",
    re.I,
)
# date ISO → (location, source label); filled by scrape_from_website / scrape_from_euav
COLLEGE_DATES: dict = {}
_STRASBOURG_MONDAYS_FALLBACK = {   # EP 2026 calendar
    "2026-01-19", "2026-02-09", "2026-03-09", "2026-04-27", "2026-05-18",
    "2026-06-15", "2026-07-06", "2026-09-14", "2026-10-05", "2026-10-19",
    "2026-11-23", "2026-12-14",
}

# European Parliament committees (eMeeting public backend, found via the SPA):
#   meeting/events/eventsByMonth?language=en&year=&month=&organ=<CODE>
#   OJ/oj?language=en&reference=<ref>&securedContext=false   (agenda; often empty)
# A meeting whose agenda names him is confirmed ([EP_AGENDA]). Meetings of the
# committees covering his portfolio are flagged as possible attendance
# ([EP_MAYBE]) — the user asked for this, accepting false positives.
EMEETING          = "https://emeeting.europarl.europa.eu/emeeting/plmrep/"
EMEETING_PAGE     = "https://emeeting.europarl.europa.eu/emeeting/committee/en/agenda/{d}/{organ}"
EP_FOCUS_COMMITTEES = {"ITRE": "Industry, Research and Energy",
                       "IMCO": "Internal Market and Consumer Protection"}
EP_AGENDA_TAG     = "[EP_AGENDA]"
EP_MAYBE_TAG      = "[EP_MAYBE]"
EP_LOOKAHEAD_DAYS = 28
EP_VENUES         = {"BRU": "European Parliament, Brussels", "STR": "European Parliament, Strasbourg"}

# Council of the EU — consilium.europa.eu blocks bots, but the rotating
# presidency site (same CMS, no bot wall) lists every Council meeting. The
# Competitiveness Council (internal market & industry) is his formation, so we
# assume he attends ([COUNCIL_AUTO]). The site changes every semester: add the
# next presidency here (an unreachable host just logs and is skipped).
PRESIDENCY_SITES = [
    ("2026-07-01", "2026-12-31", "https://irish-presidency.consilium.europa.eu"),
    ("2027-01-01", "2027-06-30", "https://lithuanian-presidency.consilium.europa.eu"),
]
COUNCIL_TAG       = "[COUNCIL_AUTO]"
COUNCIL_PATTERN   = re.compile(r"competitiveness[- ](?:council|ministers)", re.I)
# The research & space configuration belongs to other Commissioners.
COUNCIL_EXCLUDE   = re.compile(r"research|space", re.I)
COUNCIL_LOOKAHEAD_DAYS = 120

# Supabase `schedule` table (sa-archive): his engagements already known from
# the Paris Playbook, manual entries, and the news monitor ([NEWS_AUTO]).
# Needs SUPABASE_URL + SUPABASE_KEY (read-only anon key) as Actions secrets;
# skipped silently without them.
SUPABASE_URL      = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY      = os.environ.get("SUPABASE_KEY", "")
SS_PERSON         = "Stéphane Séjourné"
_FR_NUM = {"deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7,
           "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}

# Sources hit with many requests run at most every 6h; their events persist in
# the ICS between scans.
SLOW_SCAN_HOURS   = 6
SLOW_SCAN_KEY     = "_meta|slow_scan"

PREDICTED_TAGS    = ("[EU_AUTO]", "[EP_MAYBE]", "[COUNCIL_AUTO]")

# Google News — broad "sightings" feed. Notify-only; NOT written to the ICS so
# it never pollutes the subscribable calendar with non-event chatter.
# One feed per Google News edition: each edition only returns articles in its
# own language, and his trips are often reported only in non-EN/FR press (the
# 2026-10-22/23 Australia visit was on ANSA, in Italian, and nowhere else).
# The last feed is a dedicated ANSA net (ansa.it/europa tracks his trips).
_GNEWS = "https://news.google.com/rss/search?q={q}&{ed}"
# Non-English editions query the FULL name: in French, bare "séjourné" is the
# past participle of *séjourner* (to stay), which flooded alerts with tourism
# stories ("Des Québécois ont séjourné au village").
GOOGLE_NEWS_FEEDS = [
    _GNEWS.format(q="%22S%C3%A9journ%C3%A9%22+when:2d", ed="hl=en-US&gl=US&ceid=US:en"),
] + [
    _GNEWS.format(q="%22St%C3%A9phane+S%C3%A9journ%C3%A9%22+when:2d", ed=ed) for ed in (
        "hl=fr&gl=FR&ceid=FR:fr",
        "hl=it&gl=IT&ceid=IT:it",
        "hl=de&gl=DE&ceid=DE:de",
        "hl=es&gl=ES&ceid=ES:es",
    )
] + [_GNEWS.format(q="S%C3%A9journ%C3%A9+site:ansa.it",
                   ed="hl=it&gl=IT&ceid=IT:it")]
# Google News RSS is sorted by relevance, not date, and keeps resurfacing old
# articles (sometimes with a re-worded title). Only articles published within
# this window notify; older ones are recorded silently.
NEWS_MAX_AGE_HOURS = 48

NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "ss-calendar-update")
STATE_FILE  = Path("sync_state.json")
ICS_FILE    = Path("sejourn.ics")
DAYS_BACK   = 30            # rolling window for fresh events worth notifying on
# Missions are published with a 2–4 month lag (bi-monthly, per Art. 6(2) Code of
# Conduct), so a freshly *published* mission is always old by the 30-day window.
# Use a wider window for them so new batches still notify — but not so wide it
# replays years of backfilled history on first run.
MISSION_NOTIFY_DAYS = 150
MAX_PAGES   = 60           # safety cap; real stop is the date window / dup detection

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Cache-Control": "no-cache, no-store",
    "Pragma": "no-cache",
}

MONTH_MAP = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4,  "May": 5,  "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# Per-source presentation (emoji + short tag) used in notifications.
SOURCE_BADGE = {
    "calendar":     "🏛",
    "transparency": "🤝",
    "ics":          "🏛",
    "press":        "🎤",
    "mission":      "✈️",
    "ebs":          "📺",
    "euav":         "📷",
    "college":      "🇪🇺",
    "ep":           "🏛️",
    "council":      "🇪🇺",
    "known":        "📌",
}


# ── Name filter ─────────────────────────────────────────────────────────────────

def mentions_sejourne(text: str) -> bool:
    """True iff the text names Séjourné.

    Accent-insensitive match on 'sejourn' — matches "Séjourné" / "Sejourne"
    while NOT firing on unrelated words that merely contain "journ"
    (journalist, journal, journey…). A bare "journ" substring used to leak
    e.g. "Institute of Maltese Journalists" into the calendar.
    """
    norm = (unicodedata.normalize("NFKD", text)
            .encode("ascii", "ignore").decode("ascii").lower())
    return "sejourn" in norm


# ── Strategy 1: aggregate calendar page (all commissioners, name-filtered) ──────

def fetch_page(page: int) -> BeautifulSoup:
    url = f"{BASE_URL}?page={page}"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def parse_page(soup: BeautifulSoup) -> tuple[list, list, tuple, list]:
    """Returns (sejourn_events, all_dates, page_signature, college) from a page.

    college lists (date ISO, location) of College meeting entries (usually on
    von der Leyen's agenda) — used to date the weekly meeting, see COLLEGE_DATES.

    page_signature is the tuple of every title on the page — used to detect
    when ?page= isn't actually advancing (the site currently returns the same
    20 rows for every page number).
    """
    sejourn_events = []
    all_dates = []
    all_titles = []
    college = []

    for article in soup.select("article.ecl-content-item--inline"):
        time_el = article.select_one("time.ecl-content-item__date")
        if not time_el:
            continue
        day   = time_el.select_one(".ecl-date-block__day")
        month = time_el.select_one(".ecl-date-block__month")
        year  = time_el.select_one(".ecl-date-block__year")
        if not (day and month and year):
            continue
        try:
            # day cell can be a range like "19-25" → take the start day
            day_num = int(day.get_text(strip=True).split("-")[0])
            event_date = date(
                int(year.get_text(strip=True)),
                MONTH_MAP[month.get_text(strip=True)],
                day_num,
            )
        except (KeyError, ValueError):
            continue

        all_dates.append(event_date.isoformat())

        title_el    = article.select_one(".ecl-content-block__title")
        location_el = article.select_one(".ecl-content-block__secondary-meta-label")
        title = title_el.get_text(strip=True) if title_el else ""
        all_titles.append(title)

        if COLLEGE_RE.search(title):
            college.append((event_date.isoformat(),
                            location_el.get_text(strip=True) if location_el else ""))

        # Client-side filter: only Séjourné's events.
        if not mentions_sejourne(title):
            continue

        classes = time_el.get("class", [])
        status = ("past" if "ecl-date-block--past" in classes
                  else "ongoing" if "ecl-date-block--ongoing" in classes
                  else "upcoming")

        sejourn_events.append({
            "title":    title,
            "date":     event_date.isoformat(),
            "location": (location_el.get_text(strip=True) if location_el else ""),
            "status":   status,
            "source":   "calendar",
            "subject":  "",
            "url":      "",
        })

    return sejourn_events, all_dates, tuple(all_titles), college


def scrape_from_website() -> list:
    cutoff = (date.today() - timedelta(days=DAYS_BACK)).isoformat()
    print(f"[Calendar] Scraping aggregate page (all commissioners) from {cutoff} onward…")
    found    = []
    seen     = set()
    prev_sig = None

    for page in range(MAX_PAGES):
        if page > 0:
            time.sleep(0.8)
        try:
            sejourn_evs, all_dates, sig, college = parse_page(fetch_page(page))
        except requests.RequestException as e:
            print(f"[Calendar] Page {page+1} failed: {e}")
            break

        if not all_dates:
            break

        # Pagination not advancing (site bug: every ?page= returns the same rows).
        if page > 0 and sig == prev_sig:
            print(f"[Calendar] Page {page+1} identical to previous — pagination "
                  f"not advancing, stopping.")
            break
        prev_sig = sig

        for d, loc in college:
            if d not in COLLEGE_DATES:
                COLLEGE_DATES[d] = (loc, "agenda")
                print(f"[Calendar] College meeting on agenda: {d} {loc}".rstrip())

        for ev in sejourn_evs:
            k = f"{ev['date']}|{ev['title']}"
            if ev["date"] >= cutoff and k not in seen:
                seen.add(k)
                found.append(ev)

        # Stop once the oldest event on this page predates our window.
        if min(all_dates) < cutoff:
            break

    print(f"[Calendar] Found {len(found)} Séjourné events.")
    return found


# ── Strategy 2: EU Transparency Register meetings ──────────────────────────────

def _parse_transparency_host(label: str, host: str, prefix: str) -> list:
    url = TRANSPARENCY_BASE + host
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[Transparency:{label}] fetch failed: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    today  = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=DAYS_BACK)).isoformat()
    events = []

    for tr in soup.select("table tr"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue
        date_txt = cells[0].get_text(" ", strip=True)
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})", date_txt)
        if not m:
            continue
        dd, mm, yyyy = m.groups()
        event_date = f"{yyyy}-{mm}-{dd}"

        location = cells[1].get_text(" ", strip=True)
        org      = cells[2].get_text(" ", strip=True)
        subject  = cells[3].get_text(" ", strip=True)

        # Minutes PDF link, if present.
        link = ""
        a = tr.find("a", href=True)
        if a:
            href = a["href"]
            link = href if href.startswith("http") else \
                "https://ec.europa.eu/transparency-initiative/meetings/" + href.lstrip("/")

        status = ("upcoming" if event_date > today
                  else "ongoing" if event_date == today
                  else "past")

        events.append({
            "title":    f"{prefix} {org}".strip(),
            "date":     event_date,
            "location": location,
            "status":   status,
            "source":   "transparency",
            "subject":  subject,
            "url":      link,
        })

    # Only keep the rolling window for notification purposes; ICS history is
    # preserved separately via strategy 3.
    fresh = [e for e in events if e["date"] >= cutoff]
    print(f"[Transparency:{label}] {len(events)} meetings total, "
          f"{len(fresh)} within last {DAYS_BACK} days.")
    return events


def scrape_from_transparency() -> list:
    out = []
    for label, host, prefix in TRANSPARENCY_HOSTS:
        out.extend(_parse_transparency_host(label, host, prefix))
    return out


# ── Strategy 3: read ICS file committed to this repo (history) ──────────────────

def scrape_from_ics() -> list:
    """Read the ICS file in this repo — preserves full history across runs."""
    if not ICS_FILE.exists():
        print("[ICS] sejourn.ics not found, skipping.")
        return []

    today  = date.today().isoformat()
    events = []

    content = ICS_FILE.read_text(encoding="utf-8")
    for block in re.split(r"BEGIN:VEVENT", content):
        if "END:VEVENT" not in block:
            continue
        summary  = ""
        dtstart  = ""        # all-day  YYYYMMDD
        location = ""
        start_dt = ""        # timed    YYYYMMDDTHHMMSSZ
        end_dt   = ""
        dtend    = ""        # all-day exclusive end YYYYMMDD
        desc     = ""
        url      = ""
        for line in re.split(r"\r\n|\n", block):
            if line.startswith("SUMMARY:"):
                summary = line[8:].replace("\\,", ",").replace("\\n", "\n").strip()
            elif line.startswith("DTSTART;VALUE=DATE:"):
                dtstart = line[19:].strip()
            elif line.startswith("DTSTART:"):            # timed (EbS) event
                start_dt = line[8:].strip()
            elif line.startswith("DTEND:"):
                end_dt = line[6:].strip()
            elif line.startswith("DTEND;VALUE=DATE:"):
                dtend = line[17:].strip()
            elif line.startswith("LOCATION:"):
                location = line[9:].replace("\\,", ",").strip()
            elif line.startswith("DESCRIPTION:") and not desc:
                desc = line[12:].strip()      # first one = event's, not an alarm's
            elif line.startswith("URL:"):
                url = line[4:].strip()

        # Derive the calendar date from whichever DTSTART form is present.
        if re.match(r"\d{8}T\d{6}", start_dt):
            event_date = f"{start_dt[:4]}-{start_dt[4:6]}-{start_dt[6:8]}"
        elif len(dtstart) == 8:
            event_date = f"{dtstart[:4]}-{dtstart[4:6]}-{dtstart[6:8]}"
        else:
            continue
        if not summary or not mentions_sejourne(summary):
            continue

        status = ("upcoming" if event_date > today
                  else "ongoing" if event_date == today
                  else "past")

        ev = {
            "title":    summary,
            "date":     event_date,
            "location": location,
            "status":   status,
            "source":   "ics",
            "subject":  "",
            "url":      url,
            "ics_desc": desc,     # already ICS-escaped; re-emitted verbatim
        }
        if len(dtend) == 8 and len(dtstart) == 8 and dtend > dtstart:
            # multi-day all-day (missions): exclusive DTEND → inclusive date_end
            ev["date_end"] = (datetime.strptime(dtend, "%Y%m%d")
                              - timedelta(days=1)).strftime("%Y-%m-%d")
        if start_dt:                      # preserve timed events' clock times
            ev["start_dt"] = start_dt
            ev["end_dt"]   = end_dt
        events.append(ev)

    print(f"[ICS] Found {len(events)} Séjourné events in sejourn.ics.")
    return events


# ── Strategy 4: EC Presscorner speeches / statements ───────────────────────────

def scrape_from_presscorner() -> list:
    """His speeches, statements and read-outs from the Presscorner JSON API.

    The search returns anything *mentioning* him (incl. other commissioners'
    speeches and daily-news round-ups), so we keep only event-like doc types
    and re-apply the "journ" name filter on the title.
    """
    cutoff = (date.today() - timedelta(days=DAYS_BACK)).isoformat()
    today  = date.today().isoformat()
    try:
        resp = requests.get(
            PRESSCORNER_API,
            params={"text": "Séjourné", "pagesize": 60},
            headers=HEADERS, timeout=30,
        )
        resp.raise_for_status()
        items = resp.json().get("docuLanguageListResources", []) or []
    except (requests.RequestException, ValueError) as e:
        print(f"[Presscorner] fetch failed: {e}")
        return []

    events = []
    seen   = set()
    for it in items:
        if it.get("languageCode") != "EN":
            continue
        code = (it.get("docutype") or {}).get("code", "")
        if code not in PRESSCORNER_TYPES:
            continue
        title = (it.get("title") or "").strip()
        if not mentions_sejourne(title):
            continue
        ev_date = (it.get("eventDate") or "")[:10]
        if not re.match(r"\d{4}-\d{2}-\d{2}$", ev_date):
            continue
        k = f"{ev_date}|{title}"
        if k in seen:
            continue
        seen.add(k)

        ref = (it.get("refCode") or "").lower().replace("/", "_")
        url = PRESSCORNER_DETAIL + ref if ref else ""
        events.append({
            "title":    title,
            "date":     ev_date,
            "location": "",
            # Published docs are records, not reminders → never "upcoming".
            "status":   "ongoing" if ev_date == today else "past",
            "source":   "press",
            "subject":  (it.get("docutype") or {}).get("description", ""),
            "url":      url,
        })

    fresh = sum(1 for e in events if e["date"] >= cutoff)
    print(f"[Presscorner] {len(events)} speech/statement items "
          f"({fresh} within last {DAYS_BACK} days).")
    return events


# ── Strategy 5: EU Transparency Register missions (official travel) ─────────────

def _parse_missions_xlsx(content: bytes) -> list:
    """Parse the missions XLSX export into a list of row dicts (stdlib only).

    Columns (per the export): A=Date from, B=Date to, C=Location, D=Purpose,
    E=Context, F–I costs, J=Comments.
    """
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    z  = zipfile.ZipFile(io.BytesIO(content))
    sroot = ET.fromstring(z.read("xl/sharedStrings.xml"))
    shared = ["".join(t.text or "" for t in si.iter(ns + "t"))
              for si in sroot.findall(ns + "si")]
    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))

    rows = []
    for row in sheet.iter(ns + "row"):
        cells = {}
        for c in row.findall(ns + "c"):
            col = re.match(r"[A-Z]+", c.get("r", "")).group()
            v = c.find(ns + "v")
            if v is None:
                continue
            cells[col] = shared[int(v.text)] if c.get("t") == "s" else v.text
        rows.append(cells)
    return rows


def scrape_from_missions() -> list:
    # Missions are the commissioner's personal travel → only the "self" host.
    host = next((h for lbl, h, _ in TRANSPARENCY_HOSTS if lbl == "self"), None)
    if not host:
        return []
    try:
        resp = requests.get(MISSIONS_EXPORT + host, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        rows = _parse_missions_xlsx(resp.content)
    except (requests.RequestException, zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        print(f"[Missions] fetch/parse failed: {e}")
        return []

    today  = date.today().isoformat()
    events = []
    for r in rows:
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})", (r.get("A") or "").strip())
        if not m:                       # skips title + header + any cost-only rows
            continue
        dd, mm, yyyy = m.groups()
        d_from   = f"{yyyy}-{mm}-{dd}"
        location = (r.get("C") or "").strip()
        purpose  = " ".join((r.get("D") or "").split())

        d_end = ""
        m2 = re.match(r"(\d{2})/(\d{2})/(\d{4})", (r.get("B") or "").strip())
        if m2:
            dd2, mm2, yyyy2 = m2.groups()
            d_end = f"{yyyy2}-{mm2}-{dd2}"

        status = ("upcoming" if d_from > today
                  else "ongoing" if d_from == today
                  else "past")

        events.append({
            "title":    f"Mission: {location}" if location else "Mission",
            "date":     d_from,
            "date_end": d_end,
            "location": location,
            "status":   status,
            "source":   "mission",
            "subject":  purpose,
            "url":      "",
        })

    print(f"[Missions] {len(events)} missions parsed.")
    return events


# ── Strategy 6: EbS audiovisual event-planning (timed, forward-looking) ─────────

def _ebs_parse(raw: str):
    """'20261026 11:15' (Brussels local) → (date ISO, UTC ICS datetime or '' if 00:00)."""
    m = re.match(r"(\d{4})(\d{2})(\d{2})(?:\s+(\d{2}):(\d{2}))?", str(raw or ""))
    if not m:
        return "", ""
    y, mo, d, hh, mm = m.groups()
    day = f"{y}-{mo}-{d}"
    if not hh or (hh, mm) == ("00", "00"):        # whole-day item (e.g. a trip)
        return day, ""
    local = datetime(int(y), int(mo), int(d), int(hh), int(mm), tzinfo=EBS_TZ)
    return day, local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def scrape_from_ebs() -> list:
    """Upcoming events the EC AV service plans to cover, filtered to Séjourné.

    Matched by his tagged person id first — reliable, no journalist-style false
    positives — then by his name in title/summary.
    """
    try:
        resp = requests.get(EUAV_API, params={
            "wt": "json", "index": 1, "pagesize": 200, "type": "EVENTPLANNING",
        }, headers={**HEADERS, "Accept": "application/json"}, timeout=30)
        resp.raise_for_status()
        docs = resp.json().get("response", {}).get("docs", []) or []
    except (requests.RequestException, ValueError) as e:
        print(f"[EbS] fetch failed: {e}")
        return []

    today  = date.today().isoformat()
    events = []
    for doc in docs:
        title   = _euav_text(doc.get("titles_json"))
        summary = _euav_text(doc.get("summary_json"))
        tagged  = any(str(p.get("id", "")) == EUAV_PERSON
                      for p in (doc.get("pers_json") or []) if isinstance(p, dict))
        if not (tagged or mentions_sejourne(title + " " + summary)):
            continue

        ev_date, start_dt = _ebs_parse(doc.get("shootstartdate"))
        if not ev_date:
            continue
        end_date, end_dt = _ebs_parse(doc.get("shootenddate"))
        location = ", ".join(
            t for t in (_euav_text(loc.get("value"))
                        for loc in (doc.get("location_json") or []) if isinstance(loc, dict))
            if t
        )
        ref = str(doc.get("ref", ""))
        ev = {
            "title":    (title or "EC event")[:200],
            "date":     ev_date,
            "location": location,
            "status":   "upcoming" if ev_date > today
                        else "ongoing" if ev_date == today else "past",
            "source":   "ebs",
            "subject":  summary[:300],
            "url":      (EUAV_PHOTO + ref) if ref else "",
        }
        if start_dt:
            ev["start_dt"] = start_dt
            ev["end_dt"]   = end_dt or start_dt
        elif end_date and end_date > ev_date:     # multi-day trip
            ev["date_end"] = end_date
        events.append(ev)

    print(f"[EbS] {len(events)} planned events tagged Séjourné "
          f"(of {len(docs)} total upcoming).")
    return events


# ── Strategy 7: EU Audiovisual photo/video library ─────────────────────────────

def _euav_text(field) -> str:
    if isinstance(field, dict):
        raw = field.get("EN") or field.get("FR") or next(iter(field.values()), "")
    else:
        raw = field or ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(str(raw)))).strip()


def scrape_from_euav() -> list:
    """Activities captured by EC photographers/camera crews (REPORTAGE/PHOTO/VIDEO).

    Matched by his tagged person id OR his name in title/summary. One event per
    reportage/video (PHOTO items collapse onto their parent reportage ref).
    """
    by_ref = {}
    for media_type in ("REPORTAGE", "PHOTO", "VIDEO"):
        try:
            resp = requests.get(EUAV_API, params={
                "fl": "type,ref,doc_ref,titles_json,summary_json,shootstartdate,pers_json",
                "hasMedia": 1, "wt": "json", "index": 1,
                "pagesize": 100, "type": media_type,
            }, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            docs = resp.json().get("response", {}).get("docs", []) or []
        except (requests.RequestException, ValueError) as e:
            print(f"[EU AV:{media_type}] fetch failed: {e}")
            continue

        for doc in docs:
            title   = _euav_text(doc.get("titles_json"))
            summary = _euav_text(doc.get("summary_json"))
            # Photos of the College meeting (whoever is pictured) date that week's meeting.
            cm = re.match(r"(\d{4})(\d{2})(\d{2})", str(doc.get("shootstartdate", "")))
            if cm and media_type != "VIDEO" and COLLEGE_RE.search(title):
                COLLEGE_DATES.setdefault("-".join(cm.groups()), ("", "photos"))
            tagged  = any(str(p.get("id", "")) == EUAV_PERSON
                          for p in (doc.get("pers_json") or []) if isinstance(p, dict))
            if not (tagged or mentions_sejourne(title + " " + summary)):
                continue
            m = re.match(r"(\d{4})(\d{2})(\d{2})", str(doc.get("shootstartdate", "")))
            if not m or not title:
                continue
            ref = doc.get("doc_ref") or str(doc.get("ref", "")).split("/")[0]
            if not ref or ref in by_ref:
                continue
            by_ref[ref] = {
                "title":    title[:200],
                "date":     "-".join(m.groups()),
                "location": "",
                "status":   "past",
                "source":   "euav",
                "subject":  "",
                "url":      (EUAV_VIDEO if media_type == "VIDEO" else EUAV_PHOTO) + ref,
            }

    today  = date.today().isoformat()
    events = []
    for ev in by_ref.values():
        if ev["date"] >= today:
            ev["status"] = "ongoing" if ev["date"] == today else "upcoming"
        events.append(ev)
    print(f"[EU AV] {len(events)} photo/video reportages featuring Séjourné.")
    return events


# ── Strategy 8: assumed weekly College meeting ─────────────────────────────────

def strasbourg_week_mondays() -> set:
    """Mondays (ISO) of EP Strasbourg plenary weeks: weeks with a Monday or
    Tuesday sitting (Brussels mini-plenaries are Wed–Thu only)."""
    mondays = set()
    try:
        for year in {date.today().year, (date.today() + timedelta(weeks=COLLEGE_WEEKS)).year}:
            resp = requests.get(f"{EP_API}/meetings", params={
                "year": year, "format": "application/ld+json", "limit": 200,
            }, headers={**HEADERS, "Accept": "application/ld+json"}, timeout=30)
            resp.raise_for_status()
            for m in resp.json().get("data", []):
                d = datetime.strptime(m["activity_date"][:10], "%Y-%m-%d").date()
                if d.weekday() in (0, 1):
                    mondays.add((d - timedelta(days=d.weekday())).isoformat())
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"[College] EP plenary calendar fetch failed ({e}) — using 2026 fallback.")
        return set(_STRASBOURG_MONDAYS_FALLBACK)
    print(f"[College] {len(mondays)} Strasbourg plenary weeks from EP API.")
    return mondays


def is_predicted(e: dict) -> bool:
    text = e.get("ics_desc", "") + " " + e.get("subject", "")
    return e.get("source") in ("college", "council") or any(t in text for t in PREDICTED_TAGS)


def predict_college_meetings(known: list) -> list:
    busy = set()
    for e in known:
        if is_predicted(e):
            continue
        d = date.fromisoformat(e["date"])
        end = date.fromisoformat(e.get("date_end") or e["date"])
        while d <= end:
            busy.add(d.isoformat())
            d += timedelta(days=1)
    stras = strasbourg_week_mondays()
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    out = []
    for w in range(COLLEGE_WEEKS + 1):
        wk_mon = monday + timedelta(weeks=w)
        if wk_mon.isoformat() in stras:
            meet, loc = wk_mon + timedelta(days=1), "Strasbourg"
        else:
            meet, loc = wk_mon + timedelta(days=2), "Brussels (Berlaymont)"
        title = COLLEGE_TITLE
        # A known College date this week (agenda or photos) beats the convention.
        week = [d for d in COLLEGE_DATES
                if wk_mon.isoformat() <= d < (wk_mon + timedelta(days=7)).isoformat()]
        if week:
            d0 = min(week)
            if d0 != meet.isoformat():
                print(f"[College] week of {wk_mon}: meeting on {d0} "
                      f"({COLLEGE_DATES[d0][1]}), not {meet}.")
            meet  = date.fromisoformat(d0)
            loc   = COLLEGE_DATES[d0][0] or loc
            title = COLLEGE_TITLE_DATED
        d = meet.isoformat()
        if meet < today or meet.month == 8 or d in busy:
            continue
        out.append({
            "title":    title,
            "date":     d,
            "location": loc,
            "status":   "ongoing" if meet == today else "upcoming",
            "source":   "college",
            "subject":  "Assumed: he attends every weekly College meeting",
            "url":      COLLEGE_URL,
        })
    print(f"[College] {len(out)} expected College meetings in the next {COLLEGE_WEEKS} weeks.")
    return out


# ── Strategy 9: European Parliament committee meetings ─────────────────────────

def _slow_scan_due() -> bool:
    try:
        last = json.loads(STATE_FILE.read_text()).get(SLOW_SCAN_KEY, "") if STATE_FILE.exists() else ""
        return not last or datetime.now() - datetime.fromisoformat(last) >= timedelta(hours=SLOW_SCAN_HOURS)
    except (ValueError, OSError):
        return True


def _months_ahead(days: int) -> list:
    out, d, end = [], date.today().replace(day=1), date.today() + timedelta(days=days)
    while d <= end:
        out.append((d.year, d.month))
        d = (d + timedelta(days=32)).replace(day=1)
    return out


_OJ_BUDGET = [200]   # max agenda requests per scan


def _ep_agenda_mention(refs: list) -> str:
    """Agenda text around his name for the first ref whose OJ mentions him."""
    for ref in refs:
        if _OJ_BUDGET[0] <= 0:
            return ""
        _OJ_BUDGET[0] -= 1
        try:
            r = requests.get(EMEETING + "OJ/oj", params={
                "language": "en", "reference": ref, "securedContext": "false"},
                headers={**HEADERS, "Accept": "application/json"}, timeout=20)
        except requests.RequestException:
            continue
        txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>|\\n|[\"{}\[\]]", " ", r.text or "")))
        m = re.search(r"s[ée]journ", txt, re.I)
        if m:
            return txt[max(0, m.start() - 120): m.start() + 80].strip()
    return ""


def scrape_from_ep_committees() -> list:
    try:
        resp = requests.get(EMEETING + "organs/committees", params={"language": "EN"},
                            headers={**HEADERS, "Accept": "application/json"}, timeout=30)
        resp.raise_for_status()
        organs = [o["code"] for o in resp.json() if o.get("code")]
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"[EP] committee list failed: {e}")
        return []

    today = date.today()
    horizon = today + timedelta(days=EP_LOOKAHEAD_DAYS)
    meetings = {}     # (organ, date) -> {refs, venue, title}
    for organ in organs:
        for y, mth in _months_ahead(EP_LOOKAHEAD_DAYS):
            try:
                r = requests.get(EMEETING + "meeting/events/eventsByMonth", params={
                    "language": "en", "year": y, "month": mth, "organ": organ},
                    headers={**HEADERS, "Accept": "application/json"}, timeout=30)
                r.raise_for_status()
                items = r.json() or []
            except (requests.RequestException, ValueError):
                continue
            for it in items:
                try:
                    d = datetime.fromtimestamp(int(it["start"]) / 1000, tz=timezone.utc).date()
                except (KeyError, ValueError, TypeError):
                    continue
                if not (today <= d <= horizon):
                    continue
                m = meetings.setdefault((organ, d.isoformat()), {"refs": [], "venue": it.get("venue", "")})
                for k in ("reference", "meetingReference"):
                    if it.get(k) and it[k] not in m["refs"]:
                        m["refs"].append(it[k])

    events, n_conf = [], 0
    for (organ, d), m in sorted(meetings.items()):
        mention = _ep_agenda_mention(m["refs"])
        if not mention and organ not in EP_FOCUS_COMMITTEES:
            continue
        loc = EP_VENUES.get(m["venue"], "European Parliament")
        if mention:
            n_conf += 1
            title, subj = f"EP {organ} committee: exchange with Séjourné", f"{EP_AGENDA_TAG} {mention}"
        else:
            title = f"EP {organ} committee meeting ({EP_FOCUS_COMMITTEES[organ]}) — possible attendance"
            subj  = f"{EP_MAYBE_TAG} His portfolio committee; attendance not confirmed"
        events.append({
            "title": title, "date": d, "location": loc,
            "status": "ongoing" if d == today.isoformat() else "upcoming",
            "source": "ep", "subject": subj,
            "url": EMEETING_PAGE.format(d=d.replace("-", ""), organ=organ),
        })
    print(f"[EP] {len(meetings)} committee meeting-days scanned → "
          f"{n_conf} naming Séjourné, {len(events) - n_conf} possible (ITRE/IMCO).")
    return events


# ── Strategy 10: Competitiveness Council (rotating presidency site) ────────────

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}


def _first_date(text: str):
    m = re.search(r"\b(\d{1,2})(?:\s*[-–]\s*\d{1,2})?\s+(January|February|March|April|May|June|July|"
                  r"August|September|October|November|December)\s+(20\d\d)", text)
    if not m:
        return None
    return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))


def scrape_from_council() -> list:
    today   = date.today()
    horizon = today + timedelta(days=COUNCIL_LOOKAHEAD_DAYS)
    events  = []
    for start, end, site in PRESIDENCY_SITES:
        if end < today.isoformat() or start > horizon.isoformat():
            continue
        links, page = set(), 1
        while page <= 15:
            try:
                r = requests.get(f"{site}/en/events/", params={
                    "StartDate": max(start, today.isoformat()), "EndDate": min(end, horizon.isoformat()),
                    "page": page}, headers=HEADERS, timeout=30)
                r.raise_for_status()
            except requests.RequestException as e:
                print(f"[Council] {site} page {page} failed: {e}")
                break
            found = set(re.findall(r'href="(/en/events/[^"#?]+/)"', r.text)) - links
            if not found:
                break
            links |= found
            page += 1
        for link in sorted(l for l in links if COUNCIL_PATTERN.search(l.replace("-", " ")) or "competitiveness" in l):
            try:
                pg = requests.get(site + link, headers=HEADERS, timeout=30).text
            except requests.RequestException:
                continue
            tm = re.search(r"<title>(.*?)</title>", pg, re.S)
            title = html.unescape(re.sub(r"\s*[|–-]\s*[^|–-]*presidency.*$", "", tm.group(1), flags=re.I).strip()) if tm else ""
            if not COUNCIL_PATTERN.search(title) or COUNCIL_EXCLUDE.search(title):
                continue
            d = _first_date(re.sub(r"<[^>]+>", " ", pg))
            if not d or not (today <= d <= horizon):
                continue
            events.append({
                "title": f"{title} (expected)", "date": d.isoformat(), "location": "",
                "status": "ongoing" if d == today else "upcoming",
                "source": "council",
                "subject": f"{COUNCIL_TAG} His Council formation; attendance assumed",
                "url": site + link,
            })
    print(f"[Council] {len(events)} Competitiveness Council meetings ahead.")
    return events


# ── Strategy 11: known engagements from the sa-archive Supabase table ──────────

def _trip_days(text: str) -> int:
    """'visite de quatre jours' / 'four-day visit' / '3 jours' → 4 / 4 / 3."""
    m = re.search(r"\b(\d|deux|trois|quatre|cinq|six|sept|two|three|four|five|six|seven)"
                  r"(?:\s+|-)(?:jours|days?)\b", text, re.I)
    if not m:
        return 1
    v = m.group(1).lower()
    return int(v) if v.isdigit() else _FR_NUM.get(v, 1)


def scrape_from_supabase() -> list:
    if not (SUPABASE_URL and SUPABASE_KEY):
        print("[Supabase] no SUPABASE_URL/SUPABASE_KEY — skipping known engagements.")
        return []
    since = (date.today() - timedelta(days=DAYS_BACK)).isoformat()
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/schedule", headers={
            "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, params={
            "select": "event_date,title,location,description,source_url",
            "person": f"eq.{SS_PERSON}", "event_date": f"gte.{since}",
            "order": "event_date.asc", "limit": "200"}, timeout=20)
        r.raise_for_status()
        rows = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[Supabase] fetch failed: {e}")
        return []
    today, events = date.today().isoformat(), []
    for row in rows:
        d, title = str(row.get("event_date") or "")[:10], (row.get("title") or "").strip()
        if not re.match(r"\d{4}-\d{2}-\d{2}$", d) or not title:
            continue
        ev = {
            "title": title, "date": d, "location": (row.get("location") or "").strip(),
            "status": "upcoming" if d > today else "ongoing" if d == today else "past",
            "source": "known",
            "subject": "Known engagement (" + re.sub(r"\s+", " ", (row.get("description") or "sa-archive")).strip()[:80] + ")",
            "url": row.get("source_url") or "",
        }
        n = _trip_days(title + " " + (row.get("description") or ""))
        if n > 1:
            ev["date_end"] = (date.fromisoformat(d) + timedelta(days=n - 1)).isoformat()
        events.append(ev)
    print(f"[Supabase] {len(events)} known engagements of his (Playbook / manual / news).")
    return events


# ── Cross-source dedup ──────────────────────────────────────────────────────────

# Transparency events are titled "Séjourné meets <org>"; the aggregate agenda
# titles the same meeting differently ("Executive Vice-President Séjourné meets
# Mr X, President of <org>"). When both describe the same day+org, keep the
# richer agenda one and drop the Transparency duplicate.
_TRANSPARENCY_PREFIXES = ("Séjourné Cabinet meets", "Séjourné meets")
_DEDUP_STOPWORDS = {
    "france", "paris", "french", "europe", "european", "union", "brussels",
    "bruxelles", "national", "federation", "fédération", "chambers", "commerce",
    "industry", "industrie", "minister", "ministre", "president", "président",
    "groupe", "association", "company", "group",
}


def _is_transparency_style(title: str) -> bool:
    return any(title.startswith(p) for p in _TRANSPARENCY_PREFIXES)


def _org_signature(title: str) -> tuple:
    """(primary org phrase, distinctive tokens) for a Transparency-style title."""
    t = title
    for p in _TRANSPARENCY_PREFIXES:
        if t.startswith(p):
            t = t[len(p):].strip()
            break
    primary = t.split(" (")[0].strip().lower()
    tokens = {
        w for w in re.findall(r"[0-9a-zàâäéèêëîïôöùûüç]+", t.lower())
        if len(w) >= 5 and w not in _DEDUP_STOPWORDS
    }
    return primary, tokens


def drop_transparency_duplicates(events: list) -> list:
    # Anchors = the official-agenda-style events, grouped by date.
    anchors_by_date = {}
    for e in events:
        if not _is_transparency_style(e["title"]):
            anchors_by_date.setdefault(e["date"], []).append(e["title"].lower())

    out = []
    for e in events:
        if _is_transparency_style(e["title"]):
            primary, tokens = _org_signature(e["title"])
            anchors = anchors_by_date.get(e["date"], [])
            dup = (primary and any(primary in a for a in anchors)) or \
                  any(tok in a for tok in tokens for a in anchors)
            if dup:
                print(f"[dedup] drop Transparency dup of agenda event: "
                      f"{e['date']} {e['title']}")
                continue
        out.append(e)
    return out


_EUAV_STOPWORDS = _DEDUP_STOPWORDS | {
    "sejourne", "stephane", "executive", "vice", "commission", "commissioner",
    "meets", "meeting", "visit", "participation", "with", "from", "their",
    "prosperity", "industrial", "strategy", "other",
}


def _title_tokens(title: str) -> set:
    t = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    return {w for w in re.findall(r"[a-z0-9]+", t)
            if len(w) >= 4 and w not in _EUAV_STOPWORDS}


def drop_euav_duplicates(events: list) -> list:
    """Drop a photo-library event when another event the same day is clearly the
    same activity (≥2 shared distinctive words). Photo captions only fill gaps;
    the agenda/meeting entry is the canonical one. Also collapses a reportage and
    its video of the same activity."""
    def is_photo(e):
        return e.get("source") == "euav" or EUAV_TAG in e.get("ics_desc", "")

    out, kept_by_date = [], {}
    # Non-photo events first so they're the anchors.
    for e in sorted(events, key=is_photo):
        if is_photo(e):
            toks = _title_tokens(e["title"])
            if any(len(toks & other) >= 2 for other in kept_by_date.get(e["date"], [])):
                print(f"[dedup] drop EU AV dup: {e['date']} {e['title'][:70]}")
                continue
        kept_by_date.setdefault(e["date"], []).append(_title_tokens(e["title"]))
        out.append(e)
    return out


SLOW_SCAN_RAN = False

# ── Merge all sources ───────────────────────────────────────────────────────────

def collect_all_events() -> list:
    web_events     = scrape_from_website()
    reg_events     = scrape_from_transparency()
    press_events   = scrape_from_presscorner()
    mission_events = scrape_from_missions()
    ebs_events     = scrape_from_ebs()
    euav_events    = scrape_from_euav()
    known_events   = scrape_from_supabase()
    global SLOW_SCAN_RAN
    SLOW_SCAN_RAN  = _slow_scan_due()
    slow_events    = (scrape_from_ep_committees() + scrape_from_council()) if SLOW_SCAN_RAN else []
    if not SLOW_SCAN_RAN:
        print(f"[EP/Council] scanned < {SLOW_SCAN_HOURS}h ago — keeping ICS copies.")
    ics_events     = scrape_from_ics()
    if SLOW_SCAN_RAN:
        # Fresh scan replaces last scan's possible/expected entries.
        ics_events = [e for e in ics_events
                      if EP_MAYBE_TAG not in e.get("ics_desc", "")
                      and COUNCIL_TAG not in e.get("ics_desc", "")]

    # Merge, deduplicate by (date, title). Live sources win over the ICS copy
    # so that subject/url/source fields stay populated. EbS comes before the ICS
    # copy so its precise start/end times survive the merge.
    merged = {}
    for ev in (web_events + reg_events + press_events + mission_events
               + ebs_events + euav_events + known_events + slow_events + ics_events):
        k = f"{ev['date']}|{ev['title']}"
        if k not in merged:
            merged[k] = ev

    out = drop_transparency_duplicates(list(merged.values()))
    out = drop_euav_duplicates(out)
    # Predictions are regenerated each run: drop last run's copies first.
    out = [e for e in out if COLLEGE_TAG not in e.get("ics_desc", "")]
    out += predict_college_meetings(out)
    out.sort(key=lambda e: e["date"], reverse=True)
    print(f"[Total] {len(out)} unique Séjourné events after merging + dedup.")
    return out


# ── ICS generator ──────────────────────────────────────────────────────────────

def generate_ics(events: list) -> str:
    now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Sejourn EU Commission Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Séjourné - EC Calendar",
        "X-WR-TIMEZONE:Europe/Brussels",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]
    for ev in events:
        dt    = ev["date"].replace("-", "")
        # Three event shapes:
        #   • timed (EbS): start_dt/end_dt are UTC datetimes → DTSTART/DTEND with time
        #   • multi-day all-day (missions): inclusive date_end → exclusive DTEND +1 day
        #   • single-day all-day (default): DTEND = DTSTART
        timed = bool(ev.get("start_dt"))
        if timed:
            start_line = f"DTSTART:{ev['start_dt']}"
            end_line   = f"DTEND:{ev.get('end_dt') or ev['start_dt']}"
        else:
            end = ev.get("date_end")
            if end and end >= ev["date"]:
                end_dt = (datetime.strptime(end, "%Y-%m-%d")
                          + timedelta(days=1)).strftime("%Y%m%d")
            else:
                end_dt = dt
            start_line = f"DTSTART;VALUE=DATE:{dt}"
            end_line   = f"DTEND;VALUE=DATE:{end_dt}"
        # Deterministic UID so the same event keeps one identity across runs
        # (Python's str hash is salted per-process, which would churn UIDs and
        # make subscriber calendars re-create / re-alert the same event).
        digest = hashlib.md5(f"{ev['date']}|{ev['title']}".encode("utf-8")).hexdigest()[:10]
        uid    = f"{ev['date']}-{digest}@sejourn-eu"
        title = ev["title"].replace(",", "\\,").replace("\n", "\\n")
        loc   = ev.get("location", "").replace(",", "\\,")

        # Build a DESCRIPTION from subject / source / minutes link when present.
        desc_parts = []
        if ev.get("subject"):
            desc_parts.append(ev["subject"])
        if ev.get("source") == "transparency":
            desc_parts.append("Source: EU Transparency Register")
        elif ev.get("source") == "mission":
            desc_parts.append("Source: EU Transparency Register (mission)")
        elif ev.get("source") == "press":
            desc_parts.append("Source: EC Presscorner")
        elif ev.get("source") == "ebs":
            desc_parts.append("Source: EC Audiovisual (EbS) — planned coverage")
        elif ev.get("source") == "euav":
            desc_parts.append(f"{EUAV_TAG} Source: EC Audiovisual photo/video library")
        elif ev.get("source") == "ep":
            desc_parts.append("Source: European Parliament eMeeting")
        elif ev.get("source") == "council":
            desc_parts.append("Source: Council presidency calendar")
        elif ev.get("source") == "known":
            desc_parts.append("Source: sa-archive schedule (Playbook / manual / news)")
        elif ev.get("source") == "college":
            desc_parts.append(f"{COLLEGE_TAG} Expected — not on his official calendar")
        if ev.get("url"):
            desc_parts.append(ev["url"])
        description = "\\n".join(p.replace(",", "\\,") for p in desc_parts)
        if ev.get("source") == "ics" and ev.get("ics_desc"):
            description = ev["ics_desc"]

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now}",
            start_line,
            end_line,
            f"SUMMARY:{title}",
            f"LOCATION:{loc}",
        ]
        if description:
            lines.append(f"DESCRIPTION:{description}")
        if ev.get("url"):
            lines.append(f"URL:{ev['url']}")
        lines.append("STATUS:CONFIRMED")

        if ev["status"] in ("upcoming", "ongoing"):
            lines += [
                "BEGIN:VALARM",
                "TRIGGER:-P1D",
                "ACTION:DISPLAY",
                f"DESCRIPTION:Tomorrow: {title[:50]}",
                "END:VALARM",
                "BEGIN:VALARM",
                "TRIGGER:-PT1H",
                "ACTION:DISPLAY",
                f"DESCRIPTION:In 1 hour: {title[:50]}",
                "END:VALARM",
            ]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


# ── State & notification ───────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def event_key(ev: dict) -> str:
    return f"{ev['date']}|{ev['title']}"


def _fmt_event_line(e: dict) -> str:
    badge = SOURCE_BADGE.get(e.get("source", ""), "•")
    # Full title: EC titles carry the useful detail at the end (dates, org names),
    # and ntfy wraps long lines fine.
    line  = f"{badge} {e['date']}  {e['title']}"
    extra = e.get("location") or re.sub(r"\[[A-Z_]+\]\s*", "", e.get("subject", ""))
    if extra:
        line += f"\n     {extra[:120]}"
    return line


def ntfy_post(title: str, message: str, priority: int, tags: list,
              click: str = "", actions: list = None) -> bool:
    """Publish via ntfy's JSON API.

    HTTP headers are latin-1, so a 'Title' header with é arrives as 'S�journ�';
    the JSON body is UTF-8 end to end.
    """
    try:
        resp = requests.post(
            "https://ntfy.sh/",
            json={"topic": NTFY_TOPIC, "title": title, "message": message,
                  "priority": priority, "tags": tags,
                  **({"click": click} if click else {}),
                  **({"actions": actions} if actions else {})},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"ntfy publish failed: {e}")
        return False


def _news_title_key(title: str) -> str:
    """Normalised title: drop the ' - Source' suffix, accents, case, punctuation,
    so the same story re-listed by Google News with small edits dedups."""
    t = re.sub(r"\s+[-–—|]\s+[^-–—|]{1,60}$", "", title)
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return "news|" + hashlib.md5(t.encode("utf-8")).hexdigest()[:12]


def push_notification(new_events: list):
    if not NTFY_TOPIC or not new_events:
        return

    count    = len(new_events)
    n_mtg    = sum(1 for e in new_events if e.get("source") == "transparency")
    n_press  = sum(1 for e in new_events if e.get("source") == "press")
    n_trip   = sum(1 for e in new_events if e.get("source") == "mission")
    n_ebs    = sum(1 for e in new_events if e.get("source") == "ebs")
    n_photo  = sum(1 for e in new_events if e.get("source") == "euav")
    n_coll   = sum(1 for e in new_events if e.get("source") == "college")
    n_ep     = sum(1 for e in new_events if e.get("source") == "ep")
    n_cncl   = sum(1 for e in new_events if e.get("source") == "council")
    n_known  = sum(1 for e in new_events if e.get("source") == "known")
    n_cal    = count - n_mtg - n_press - n_trip - n_ebs - n_photo - n_coll - n_ep - n_cncl - n_known
    upcoming = any(e["status"] in ("upcoming", "ongoing") for e in new_events)

    # Headline reflects the mix of sources.
    bits = []
    if n_cal:
        bits.append(f"{n_cal} agenda")
    if n_mtg:
        bits.append(f"{n_mtg} meeting{'s' if n_mtg != 1 else ''}")
    if n_press:
        bits.append(f"{n_press} speech{'es' if n_press != 1 else ''}")
    if n_trip:
        bits.append(f"{n_trip} mission{'s' if n_trip != 1 else ''}")
    if n_ebs:
        bits.append(f"{n_ebs} scheduled")
    if n_photo:
        bits.append(f"{n_photo} seen in EC photos")
    if n_coll:
        bits.append(f"{n_coll} College meeting{'s' if n_coll != 1 else ''} (expected)")
    if n_ep:
        bits.append(f"{n_ep} EP committee")
    if n_cncl:
        bits.append(f"{n_cncl} Council (expected)")
    if n_known:
        bits.append(f"{n_known} known (Playbook/news)")
    summary = f"{count} new ({' + '.join(bits)})" if bits else f"{count} new"

    shown   = sorted(new_events, key=lambda e: e["date"])[:10]
    details = "\n\n".join(_fmt_event_line(e) for e in shown)
    if count > 10:
        details += f"\n\n…and {count - 10} more"

    if ntfy_post("Séjourné - EU Commission", summary + "\n\n" + details,
                 priority=4 if upcoming else 3, tags=["calendar", "eu"]):
        print(f"Push notification sent → ntfy:{NTFY_TOPIC}")


# Headline names him as a proper noun: capitalised "Séjourné"/"Sejourne" (or
# all caps). The *séjourner* verb is lowercase mid-sentence ("ont séjourné"),
# so a case-sensitive match drops it while keeping "Séjourné in visita…".
def _split_source(title: str) -> tuple:
    """'Headline - Source' (Google News format) → ('Headline', 'Source')."""
    m = re.match(r"(.*\S)\s+[-–—|]\s+([^-–—|]{1,60})$", title)
    return (m.group(1), m.group(2).strip()) if m else (title, "")


_SEJOURNE_NAME = re.compile(r"\bS(?:[ée]journ[ée]|ÉJOURNÉ|EJOURNE)\b")


def notify_news(state: dict) -> int:
    """Google News 'sightings' channel — notify on new articles mentioning him.

    Deliberately NOT written to the ICS: these are press mentions, not events.
    State keys are namespaced ('news|…') so they never collide with calendar
    events. Returns the number of newly-recorded articles (mutates `state`).
    """
    if not NTFY_TOPIC:
        return 0
    blocks = []
    for feed in GOOGLE_NEWS_FEEDS:
        try:
            resp = requests.get(feed, headers=HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"[News] fetch failed ({feed[-24:]}): {e}")
            continue
        blocks += re.findall(r"<item>(.*?)</item>", resp.text, re.S)[:40]

    now     = datetime.now(timezone.utc)
    new     = []
    n_stale = 0
    for block in blocks:
        tm = re.search(r"<title>(.*?)</title>", block, re.S)
        lm = re.search(r"<link>(.*?)</link>", block, re.S)
        pm = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S)
        title = html.unescape(re.sub(r"<[^>]+>", "", tm.group(1)).strip()) if tm else ""
        link  = re.sub(r"<[^>]+>", "", lm.group(1)).strip() if lm else ""
        if not _SEJOURNE_NAME.search(title):
            continue
        # Old-style key (raw title hash) is still checked so articles already
        # seen before the normalised key existed don't re-notify.
        raw_key = "news|" + hashlib.md5(title.encode("utf-8")).hexdigest()[:12]
        key     = _news_title_key(title)
        if key in state or raw_key in state:
            continue
        state[key] = datetime.now().isoformat()
        try:
            published = parsedate_to_datetime(pm.group(1).strip()) if pm else None
        except (TypeError, ValueError):
            published = None
        # No/unparseable date → treat as stale rather than risk an old alert.
        if published is None or now - published > timedelta(hours=NEWS_MAX_AGE_HOURS):
            n_stale += 1
            continue
        new.append((title, link, published))

    if n_stale:
        print(f"[News] {n_stale} older article(s) recorded without notifying.")
    if not new:
        print("[News] no new articles.")
        return n_stale

    # Numbered headlines with the source underneath. No raw Google News URLs
    # (hundreds of characters, and most ntfy clients don't render markdown
    # links) — each article gets a button instead (ntfy allows 3), and
    # tapping the alert opens the first.
    shown   = new[:6]
    split   = [_split_source(t) for t, _, _ in shown]
    lines = []
    for i, (head, src) in enumerate(split):
        lines.append(f"{i + 1}. {head}")
        if src:
            lines.append(f"   {src}")
    body = "\n".join(lines)
    if len(new) > 6:
        body += f"\n…and {len(new) - 6} more"
    msg = f"{len(new)} news mention{'s' if len(new) != 1 else ''}\n\n" + body
    actions = [{"action": "view", "label": f"{i + 1} · {(src or 'Open')[:20]}",
                "url": link}
               for i, ((_, src), (_, link, _)) in enumerate(zip(split, shown[:3]))]
    if ntfy_post("Séjourné - in the news", msg, priority=2, tags=["newspaper"],
                 click=shown[0][1], actions=actions):
        print(f"[News] notified {len(new)} new article(s).")
    return len(new) + n_stale


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=== Séjourné EU Commission Cloud Sync ===\n")

    events = collect_all_events()

    state         = load_state()
    state_changed = False
    if SLOW_SCAN_RAN:
        state[SLOW_SCAN_KEY] = datetime.now().isoformat()
        state_changed = True

    if not events:
        print("No events found from any source.")
    else:
        new_events = [e for e in events if event_key(e) not in state]

        # Notify only on recent events; older backlog is recorded silently so the
        # ICS/history stays complete without blasting the phone with stale items.
        # Missions get a wider window (they're published months after the fact).
        cutoff      = (date.today() - timedelta(days=DAYS_BACK)).isoformat()
        mis_cutoff  = (date.today() - timedelta(days=MISSION_NOTIFY_DAYS)).isoformat()
        def _is_notify(e):
            return e["date"] >= (mis_cutoff if e.get("source") == "mission" else cutoff)
        # EU AV photo events are calendar/agenda fill-ins only: the photo
        # monitor already pushes an alert for every new photo of him.
        notify_events = [e for e in new_events
                         if _is_notify(e) and e.get("source") != "euav"]
        print(f"\n{len(events)} total  •  {len(new_events)} new "
              f"({len(notify_events)} recent, {len(new_events)-len(notify_events)} backlog) "
              f"•  {len(events)-len(new_events)} unchanged")

        # Always write ICS with the latest merged data.
        ICS_FILE.write_text(generate_ics(events), encoding="utf-8")
        print(f"Written {ICS_FILE} ({len(events)} events)")

        if new_events:
            for e in new_events:
                state[event_key(e)] = datetime.now().isoformat()
            state_changed = True
            if notify_events:
                push_notification(notify_events)
            else:
                print("New events are all backlog — recorded without notifying.")
        else:
            print("No new events — nothing to notify.")

    # Independent 'sightings' channel — never touches the ICS.
    if notify_news(state):
        state_changed = True

    if state_changed:
        save_state(state)
    print("Done ✓")


if __name__ == "__main__":
    main()
