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
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

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

# EbS (audiovisual.ec.europa.eu) event-planning feed — the EC AV service's
# schedule of upcoming covered events. Undocumented AWS backend behind the SPA;
# the only source with precise start/end *times* (everything else is all-day)
# and forward-looking. Filter by tagged personality, so zero name false hits.
EBS_API = ("https://8hwk2cyeyb.execute-api.eu-west-1.amazonaws.com"
           "/parrotfish-prod/search")
EBS_DETAIL = "https://audiovisual.ec.europa.eu/en/event/"

# Google News — broad "sightings" feed. Notify-only; NOT written to the ICS so
# it never pollutes the subscribable calendar with non-event chatter.
GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?"
    "q=%22S%C3%A9journ%C3%A9%22&hl=en-US&gl=US&ceid=US:en"
)

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


def parse_page(soup: BeautifulSoup) -> tuple[list, list, tuple]:
    """Returns (sejourn_events, all_dates, page_signature) from a page.

    page_signature is the tuple of every title on the page — used to detect
    when ?page= isn't actually advancing (the site currently returns the same
    20 rows for every page number).
    """
    sejourn_events = []
    all_dates = []
    all_titles = []

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

    return sejourn_events, all_dates, tuple(all_titles)


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
            sejourn_evs, all_dates, sig = parse_page(fetch_page(page))
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
        for line in re.split(r"\r\n|\n", block):
            if line.startswith("SUMMARY:"):
                summary = line[8:].replace("\\,", ",").replace("\\n", "\n").strip()
            elif line.startswith("DTSTART;VALUE=DATE:"):
                dtstart = line[19:].strip()
            elif line.startswith("DTSTART:"):            # timed (EbS) event
                start_dt = line[8:].strip()
            elif line.startswith("DTEND:"):
                end_dt = line[6:].strip()
            elif line.startswith("LOCATION:"):
                location = line[9:].replace("\\,", ",").strip()

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
            "url":      "",
        }
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

def _ebs_en(entries: list, field: str = "content") -> str:
    """Pick the English string from an EbS [{language, content/title}] list."""
    if not entries:
        return ""
    chosen = next((e for e in entries if e.get("language") == "EN"), entries[0])
    raw = chosen.get(field) or chosen.get("title") or chosen.get("content") or ""
    return html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()


def _ebs_dt(iso: str) -> str:
    """'2026-06-25T07:00:00.000Z' → '20260625T070000Z' (ICS UTC datetime)."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", iso or "")
    return ("".join(m.groups()) + "Z") if m else ""


def scrape_from_ebs() -> list:
    """Upcoming events the EC AV service plans to cover, filtered to Séjourné.

    Filters on the tagged `personalities` (lastname) first — reliable, no
    journalist-style false positives — and falls back to the title text.
    """
    url = EBS_API + "?" + urllib.parse.urlencode({
        "mediaType": "EVENTPLANNING", "pageSize": 100,
    })
    try:
        req = requests.get(url, headers={**HEADERS, "Accept": "application/json"},
                           timeout=30)
        req.raise_for_status()
        items = req.json().get("items", []) or []
    except (requests.RequestException, ValueError) as e:
        print(f"[EbS] fetch failed: {e}")
        return []

    today  = date.today().isoformat()
    events = []
    for it in items:
        people = " ".join(
            f"{p.get('lastname','')} {p.get('title','')}"
            for p in (it.get("personalities") or [])
        )
        title = _ebs_en(it.get("titles"))
        if not (mentions_sejourne(people) or mentions_sejourne(title)):
            continue

        start_iso = it.get("startDateTime") or ""
        ev_date   = start_iso[:10]
        if not re.match(r"\d{4}-\d{2}-\d{2}$", ev_date):
            continue

        location = ", ".join(
            t for t in (_ebs_en(loc.get("titles")) for loc in (it.get("locations") or []))
            if t
        )
        ref = it.get("reference", "")
        events.append({
            "title":    title or "EC event",
            "date":     ev_date,
            "start_dt": _ebs_dt(start_iso),
            "end_dt":   _ebs_dt(it.get("endDateTime") or ""),
            "location": location,
            "status":   "upcoming" if ev_date > today
                        else "ongoing" if ev_date == today else "past",
            "source":   "ebs",
            "subject":  "",
            "url":      (EBS_DETAIL + ref) if ref else "",
        })

    print(f"[EbS] {len(events)} planned events tagged Séjourné "
          f"(of {len(items)} total upcoming).")
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


# ── Merge all sources ───────────────────────────────────────────────────────────

def collect_all_events() -> list:
    web_events     = scrape_from_website()
    reg_events     = scrape_from_transparency()
    press_events   = scrape_from_presscorner()
    mission_events = scrape_from_missions()
    ebs_events     = scrape_from_ebs()
    ics_events     = scrape_from_ics()

    # Merge, deduplicate by (date, title). Live sources win over the ICS copy
    # so that subject/url/source fields stay populated. EbS comes before the ICS
    # copy so its precise start/end times survive the merge.
    merged = {}
    for ev in (web_events + reg_events + press_events + mission_events
               + ebs_events + ics_events):
        k = f"{ev['date']}|{ev['title']}"
        if k not in merged:
            merged[k] = ev

    out = drop_transparency_duplicates(list(merged.values()))
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
        if ev.get("url"):
            desc_parts.append(ev["url"])
        description = "\\n".join(p.replace(",", "\\,") for p in desc_parts)

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
    line  = f"{badge} {e['date']}  {e['title'][:60]}"
    extra = e.get("location") or e.get("subject")
    if extra:
        line += f"\n     {extra[:60]}"
    return line


def push_notification(new_events: list):
    if not NTFY_TOPIC or not new_events:
        return

    count    = len(new_events)
    n_mtg    = sum(1 for e in new_events if e.get("source") == "transparency")
    n_press  = sum(1 for e in new_events if e.get("source") == "press")
    n_trip   = sum(1 for e in new_events if e.get("source") == "mission")
    n_ebs    = sum(1 for e in new_events if e.get("source") == "ebs")
    n_cal    = count - n_mtg - n_press - n_trip - n_ebs
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
    summary = f"{count} new ({' + '.join(bits)})" if bits else f"{count} new"

    details = "\n".join(_fmt_event_line(e) for e in new_events[:6])
    if count > 6:
        details += f"\n…and {count - 6} more"

    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=(summary + "\n\n" + details).encode("utf-8"),
            headers={
                "Title": "Sejourn - EU Commission",
                "Priority": "high" if upcoming else "default",
                "Tags": "calendar,eu",
            },
            timeout=10,
        )
        print(f"Push notification sent → ntfy:{NTFY_TOPIC}")
    except requests.RequestException as e:
        print(f"Push notification failed: {e}")


def notify_news(state: dict) -> int:
    """Google News 'sightings' channel — notify on new articles mentioning him.

    Deliberately NOT written to the ICS: these are press mentions, not events.
    State keys are namespaced ('news|…') so they never collide with calendar
    events. Returns the number of newly-seen articles (mutates `state`).
    """
    if not NTFY_TOPIC:
        return 0
    try:
        resp = requests.get(GOOGLE_NEWS_RSS, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[News] fetch failed: {e}")
        return 0

    new = []
    for block in re.findall(r"<item>(.*?)</item>", resp.text, re.S)[:40]:
        tm = re.search(r"<title>(.*?)</title>", block, re.S)
        lm = re.search(r"<link>(.*?)</link>", block, re.S)
        title = html.unescape(re.sub(r"<[^>]+>", "", tm.group(1)).strip()) if tm else ""
        link  = re.sub(r"<[^>]+>", "", lm.group(1)).strip() if lm else ""
        if not mentions_sejourne(title):
            continue
        key = "news|" + hashlib.md5(title.encode("utf-8")).hexdigest()[:12]
        if key in state:
            continue
        state[key] = datetime.now().isoformat()
        new.append((title, link))

    if not new:
        print("[News] no new articles.")
        return 0

    body = "\n\n".join(f"📰 {t[:90]}\n{l}" for t, l in new[:6])
    if len(new) > 6:
        body += f"\n…and {len(new) - 6} more"
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=(f"{len(new)} news mention{'s' if len(new) != 1 else ''}\n\n"
                  + body).encode("utf-8"),
            headers={
                "Title": "Séjourné - in the news",
                "Priority": "low",
                "Tags": "newspaper",
            },
            timeout=10,
        )
        print(f"[News] notified {len(new)} new article(s).")
    except requests.RequestException as e:
        print(f"[News] notify failed: {e}")
    return len(new)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=== Séjourné EU Commission Cloud Sync ===\n")

    events = collect_all_events()

    state         = load_state()
    state_changed = False

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
        notify_events = [e for e in new_events if _is_notify(e)]
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
