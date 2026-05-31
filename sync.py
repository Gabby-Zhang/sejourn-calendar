#!/usr/bin/env python3
"""
Cloud version: detects new Séjourné EU Commission events and sends ntfy notifications.
Runs on GitHub Actions every hour — no Mac needed.

Two detection strategies (combined):
  1. Scrape the EU Commission website (unfiltered, client-side name filter)
  2. Read the ICS file committed by the Mac script — catches events Mac found when it last ran
"""

import json
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────

# Unfiltered URL — server-side filter doesn't work from cloud IPs,
# so we grab all commissioners and filter for Séjourné client-side.
BASE_URL = (
    "https://commission.europa.eu/about/organisation/college-commissioners"
    "/calendar-items-president-and-commissioners_en"
    "?f%5B0%5D=ewcms_calendar_status%3Apast"
    "&f%5B1%5D=ewcms_calendar_status%3Aupcoming"
)

NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "ss-calendar-update")
STATE_FILE  = Path("sync_state.json")
ICS_FILE    = Path("sejourn.ics")
DAYS_BACK   = 14   # wider window to catch more events

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

# ── Strategy 1: scrape website ─────────────────────────────────────────────────

def fetch_page(page: int) -> BeautifulSoup:
    url = f"{BASE_URL}&page={page}"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def parse_page(soup: BeautifulSoup) -> tuple[list, list]:
    """Returns (sejourn_events, all_dates) from a page."""
    sejourn_events = []
    all_dates = []

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
            event_date = date(
                int(year.get_text(strip=True)),
                MONTH_MAP[month.get_text(strip=True)],
                int(day.get_text(strip=True)),
            )
        except (KeyError, ValueError):
            continue

        all_dates.append(event_date.isoformat())

        title_el    = article.select_one(".ecl-content-block__title")
        location_el = article.select_one(".ecl-content-block__secondary-meta-label")
        title = title_el.get_text(strip=True) if title_el else ""

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
        })

    return sejourn_events, all_dates


def scrape_from_website() -> list:
    cutoff = (date.today() - timedelta(days=DAYS_BACK)).isoformat()
    print(f"[Website] Scraping events from {cutoff} onward…")
    found   = []
    seen    = set()

    for page in range(10):
        if page > 0:
            time.sleep(0.8)
        try:
            soup = fetch_page(page)
        except requests.RequestException as e:
            print(f"[Website] Page {page+1} failed: {e}")
            break

        sejourn_evs, all_dates = parse_page(soup)

        if not all_dates:
            break

        for ev in sejourn_evs:
            k = f"{ev['date']}|{ev['title']}"
            if ev["date"] >= cutoff and k not in seen:
                seen.add(k)
                found.append(ev)

        # Stop when the oldest event on this page is before our cutoff
        if min(all_dates) < cutoff:
            break

        # CDN cache detected: all dates identical means same page served repeatedly
        if len(set(all_dates)) <= 2 and page > 0:
            print(f"[Website] CDN cache detected at page {page+1}, stopping.")
            break

    print(f"[Website] Found {len(found)} Séjourné events.")
    return found


# ── Strategy 2: read ICS file committed by Mac ────────────────────────────────

def scrape_from_ics() -> list:
    """Read the ICS file that the Mac script pushes to this repo."""
    if not ICS_FILE.exists():
        print("[ICS] sejourn.ics not found, skipping.")
        return []

    cutoff = (date.today() - timedelta(days=DAYS_BACK)).isoformat()
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

        event_date = f"{dtstart[:4]}-{dtstart[4:6]}-{dtstart[6:8]}"
        if event_date < cutoff:
            continue

        status = ("upcoming" if event_date > today
                  else "ongoing" if event_date == today
                  else "past")

        events.append({
            "title":    summary,
            "date":     event_date,
            "location": location,
            "status":   status,
        })

    print(f"[ICS] Found {len(events)} Séjourné events in sejourn.ics.")
    return events


# ── Merge both sources ────────────────────────────────────────────────────────

def collect_all_events() -> list:
    web_events = scrape_from_website()
    ics_events = scrape_from_ics()

    # Merge, deduplicate by (date, title)
    seen   = set()
    merged = []
    for ev in web_events + ics_events:
        k = f"{ev['date']}|{ev['title']}"
        if k not in seen:
            seen.add(k)
            merged.append(ev)

    merged.sort(key=lambda e: e["date"], reverse=True)
    print(f"[Total] {len(merged)} unique Séjourné events after merging.")
    return merged


# ── ICS generator ──────────────────────────────────────────────────────────────

def generate_ics(events: list) -> str:
    now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Sejourn EU Commission Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Sejourn - EU Commission",
        "X-WR-TIMEZONE:Europe/Brussels",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]
    for ev in events:
        dt    = ev["date"].replace("-", "")
        uid   = f"{ev['date']}-{abs(hash(ev['title']))%10**10}@sejourn-eu"
        title = ev["title"].replace(",", "\\,").replace("\n", "\\n")
        loc   = ev["location"].replace(",", "\\,")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now}",
            f"DTSTART;VALUE=DATE:{dt}",
            f"DTEND;VALUE=DATE:{dt}",
            f"SUMMARY:{title}",
            f"LOCATION:{loc}",
            "STATUS:CONFIRMED",
        ]
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


def push_notification(new_events: list):
    if not NTFY_TOPIC or not new_events:
        return
    count   = len(new_events)
    summary = f"{count} new activit{'y' if count == 1 else 'ies'} added"
    details = "\n".join(
        f"- {e['date']}  {e['title'][:60]}" for e in new_events[:5]
    )
    if count > 5:
        details += f"\n...and {count - 5} more"
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=(summary + "\n\n" + details).encode("utf-8"),
            headers={
                "Title": "Sejourn - EU Commission",
                "Priority": "high",
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
        print("No events found from either source.")
        return

    state      = load_state()
    new_events = [e for e in events if event_key(e) not in state]
    print(f"\n{len(events)} total  •  {len(new_events)} new  •  {len(events)-len(new_events)} unchanged")

    # Always write ICS with latest merged data
    ics_content = generate_ics(events)
    ICS_FILE.write_text(ics_content, encoding="utf-8")
    print(f"Written {ICS_FILE} ({len(events)} events)")

    if new_events:
        for e in new_events:
            state[event_key(e)] = datetime.now().isoformat()
        save_state(state)
        push_notification(new_events)
        print("Done ✓")
    else:
        print("No new events — nothing to notify.")


if __name__ == "__main__":
    main()
