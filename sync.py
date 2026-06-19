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

Design note: strategies 1 and 2 fail in *different* ways — (1) survives the
facet dropping him (name filter), (2) survives the aggregate page going stale.
Together they self-heal: when the EU fixes pagination, (1) auto-resumes full
coverage with zero code changes.
"""

import hashlib
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

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
]

NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "ss-calendar-update")
STATE_FILE  = Path("sync_state.json")
ICS_FILE    = Path("sejourn.ics")
DAYS_BACK   = 30            # rolling window for fresh events worth notifying on
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
}


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
        if "journ" not in title.lower():
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
        dtstart  = ""
        location = ""
        for line in re.split(r"\r\n|\n", block):
            if line.startswith("SUMMARY:"):
                summary = line[8:].replace("\\,", ",").replace("\\n", "\n").strip()
            elif line.startswith("DTSTART;VALUE=DATE:"):
                dtstart = line[19:].strip()
            elif line.startswith("LOCATION:"):
                location = line[9:].replace("\\,", ",").strip()

        if not summary or not dtstart or len(dtstart) != 8:
            continue
        if "journ" not in summary.lower():
            continue

        event_date = f"{dtstart[:4]}-{dtstart[4:6]}-{dtstart[6:8]}"
        status = ("upcoming" if event_date > today
                  else "ongoing" if event_date == today
                  else "past")

        events.append({
            "title":    summary,
            "date":     event_date,
            "location": location,
            "status":   status,
            "source":   "ics",
            "subject":  "",
            "url":      "",
        })

    print(f"[ICS] Found {len(events)} Séjourné events in sejourn.ics.")
    return events


# ── Merge all sources ───────────────────────────────────────────────────────────

def collect_all_events() -> list:
    web_events = scrape_from_website()
    reg_events = scrape_from_transparency()
    ics_events = scrape_from_ics()

    # Merge, deduplicate by (date, title). Live sources win over the ICS copy
    # so that subject/url/source fields stay populated.
    merged = {}
    for ev in web_events + reg_events + ics_events:
        k = f"{ev['date']}|{ev['title']}"
        if k not in merged:
            merged[k] = ev

    out = list(merged.values())
    out.sort(key=lambda e: e["date"], reverse=True)
    print(f"[Total] {len(out)} unique Séjourné events after merging.")
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
        if ev.get("url"):
            desc_parts.append(ev["url"])
        description = "\\n".join(p.replace(",", "\\,") for p in desc_parts)

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now}",
            f"DTSTART;VALUE=DATE:{dt}",
            f"DTEND;VALUE=DATE:{dt}",
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
    n_cal    = count - n_mtg
    upcoming = any(e["status"] in ("upcoming", "ongoing") for e in new_events)

    # Headline reflects the mix of sources.
    bits = []
    if n_cal:
        bits.append(f"{n_cal} agenda")
    if n_mtg:
        bits.append(f"{n_mtg} meeting{'s' if n_mtg != 1 else ''}")
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


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=== Séjourné EU Commission Cloud Sync ===\n")

    events = collect_all_events()
    if not events:
        print("No events found from any source.")
        return

    state      = load_state()
    new_events = [e for e in events if event_key(e) not in state]

    # Notify only on recent events; older backlog is recorded silently so the
    # ICS/history stays complete without blasting the phone with stale items.
    cutoff       = (date.today() - timedelta(days=DAYS_BACK)).isoformat()
    notify_events = [e for e in new_events if e["date"] >= cutoff]
    print(f"\n{len(events)} total  •  {len(new_events)} new "
          f"({len(notify_events)} recent, {len(new_events)-len(notify_events)} backlog) "
          f"•  {len(events)-len(new_events)} unchanged")

    # Always write ICS with the latest merged data.
    ICS_FILE.write_text(generate_ics(events), encoding="utf-8")
    print(f"Written {ICS_FILE} ({len(events)} events)")

    if new_events:
        for e in new_events:
            state[event_key(e)] = datetime.now().isoformat()
        save_state(state)
        if notify_events:
            push_notification(notify_events)
        else:
            print("New events are all backlog — recorded without notifying.")
        print("Done ✓")
    else:
        print("No new events — nothing to notify.")


if __name__ == "__main__":
    main()
