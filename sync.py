#!/usr/bin/env python3
"""
Cloud version: scrapes Séjourné's EU Commission calendar,
generates sejourn.ics, and sends ntfy push notifications on new events.
Runs on GitHub Actions every hour — no Mac needed.
"""

import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────

# All commissioners calendar — we filter for Séjourné client-side
# (server-side filter is unreliable from cloud IPs)
BASE_URL = (
    "https://commission.europa.eu/about/organisation/college-commissioners"
    "/calendar-items-president-and-commissioners_en"
    "?f%5B0%5D=ewcms_calendar_status%3Apast"
    "&f%5B1%5D=ewcms_calendar_status%3Aupcoming"
)

NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "ss-calendar-update")
STATE_FILE  = Path("sync_state.json")
ICS_FILE    = Path("sejourn.ics")
DAYS_BACK   = 7
ITEMS_PER_PAGE = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

MONTH_MAP = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4,  "May": 5,  "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# ── Scraper ────────────────────────────────────────────────────────────────────

def fetch_page(page: int) -> BeautifulSoup:
    url = f"{BASE_URL}&page={page}"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def parse_events(soup: BeautifulSoup) -> list:
    events = []
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

        classes = time_el.get("class", [])
        if "ecl-date-block--past" in classes:
            status = "past"
        elif "ecl-date-block--ongoing" in classes:
            status = "ongoing"
        else:
            status = "upcoming"

        title_el    = article.select_one(".ecl-content-block__title")
        location_el = article.select_one(".ecl-content-block__secondary-meta-label")
        title = title_el.get_text(strip=True) if title_el else ""

        # Client-side filter: only keep Séjourné's events
        if "journ" not in title.lower():
            continue

        events.append({
            "title":    title,
            "date":     event_date.isoformat(),
            "location": (location_el.get_text(strip=True) if location_el else ""),
            "status":   status,
        })
    return events


def event_key_scrape(ev: dict) -> str:
    return f"{ev['date']}|{ev['title']}"


def parse_all_events(soup: BeautifulSoup) -> list:
    """Parse ALL commissioner events (no name filter) — used for date-based stopping."""
    events = []
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
            events.append(event_date.isoformat())
        except (KeyError, ValueError):
            continue
    return events


def scrape_events() -> list:
    cutoff = (date.today() - timedelta(days=DAYS_BACK)).isoformat()
    print(f"Fetching events from {cutoff} onward…")
    all_events = []
    seen_keys  = set()

    for page in range(20):  # cap at 20 pages
        if page > 0:
            time.sleep(0.6)
        try:
            soup = fetch_page(page)
        except requests.RequestException as e:
            print(f"Warning: page {page + 1} failed – {e}")
            break

        # Get dates of ALL events on this page (to know when to stop)
        all_dates = parse_all_events(soup)
        if not all_dates:
            break

        # Get Séjourné-filtered events
        sejourn_batch = parse_events(soup)
        for ev in sejourn_batch:
            k = event_key_scrape(ev)
            if ev["date"] >= cutoff and k not in seen_keys:
                seen_keys.add(k)
                all_events.append(ev)

        # Stop when the oldest event on this page is before our cutoff
        if min(all_dates) < cutoff:
            break

    print(f"Found {len(all_events)} events.")
    return all_events


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
        dt  = ev["date"].replace("-", "")
        uid = f"{ev['date']}-{abs(hash(ev['title']))%10**10}@sejourn-eu"
        title    = ev["title"].replace(",", "\\,").replace("\n", "\\n")
        location = ev["location"].replace(",", "\\,")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now}",
            f"DTSTART;VALUE=DATE:{dt}",
            f"DTEND;VALUE=DATE:{dt}",
            f"SUMMARY:{title}",
            f"LOCATION:{location}",
            f"STATUS:CONFIRMED",
        ]
        if ev["status"] in ("upcoming", "ongoing"):
            lines += [
                "BEGIN:VALARM",
                "TRIGGER:-P1D",
                "ACTION:DISPLAY",
                "DESCRIPTION:Tomorrow: " + title[:50],
                "END:VALARM",
                "BEGIN:VALARM",
                "TRIGGER:-PT1H",
                "ACTION:DISPLAY",
                "DESCRIPTION:In 1 hour: " + title[:50],
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
    events = scrape_events()
    if not events:
        print("No events found.")
        return

    state      = load_state()
    new_events = [e for e in events if event_key(e) not in state]
    print(f"{len(events)} events  •  {len(new_events)} new  •  {len(events)-len(new_events)} unchanged")

    # Always regenerate .ics with latest data
    ics_content = generate_ics(events)
    with open(ICS_FILE, "w", encoding="utf-8") as f:
        f.write(ics_content)
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
