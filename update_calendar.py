#!/usr/bin/env python3
"""Scrape a Kids Inc team schedule page and write a subscribable .ics file.

Usage:
    python update_calendar.py                 # fetch live page
    python update_calendar.py page.html       # parse a saved HTML file (for testing)

Output is deterministic: if the schedule hasn't changed, the file is
byte-for-byte identical, so the GitHub Action only commits real changes.
"""
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ---- Configuration ---------------------------------------------------------
URL = "https://amarillo.kidsinc.org/schedule/?team_id=39271"
OUTPUT = "heritage-hills-basketball.ics"
TZ_NAME = "America/Chicago"
GAME_LENGTH = timedelta(hours=1)  # the site doesn't list durations
# ----------------------------------------------------------------------------

TZ = ZoneInfo(TZ_NAME)
TEAM_ID = re.search(r"team_id=(\d+)", URL).group(1)

DATE_RE = re.compile(r"[A-Z]{3},\s+([A-Z]{3})\s+(\d{1,2})\s+(\d{4})", re.I)
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([AP]M)", re.I)
RECORD_RE = re.compile(r"^\d+-\d+-\d+$")


def fetch(path=None):
    if path:
        with open(path, encoding="utf-8") as f:
            return f.read()
    r = requests.get(
        URL,
        headers={"User-Agent": "Mozilla/5.0 (schedule-to-ics; personal use)"},
        timeout=30,
    )
    r.raise_for_status()
    return r.text


def parse(html):
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.get_text(strip=True) if soup.title else ""
    team = title.split(" - ", 1)[-1].strip() or "Team"

    games = {}  # keyed by UID; the page repeats each game in a mobile layout
    for row in soup.find_all("tr"):
        text = row.get_text("\n", strip=True)
        d, t = DATE_RE.search(text), TIME_RE.search(text)
        if not (d and t):
            continue

        teams = [
            a.get_text(strip=True)
            for a in row.find_all("a", href=re.compile(r"team_id="))
        ]
        if len(teams) < 2:
            continue

        month, day, year = d.group(1), int(d.group(2)), int(d.group(3))
        hour, minute, ampm = int(t.group(1)), int(t.group(2)), t.group(3).upper()
        hour = hour % 12 + (12 if ampm == "PM" else 0)
        start = datetime.strptime(f"{month.title()} {day} {year}", "%b %d %Y").replace(
            hour=hour, minute=minute, tzinfo=TZ
        )

        # Venue = leftover lines that aren't the date/time, a team, or a record
        venue_parts = []
        for line in text.split("\n"):
            line = line.strip()
            if (
                not line
                or line in teams
                or RECORD_RE.match(line)
                or DATE_RE.search(line)
                or TIME_RE.fullmatch(line)
            ):
                continue
            venue_parts.append(line.replace("·", "-").strip(" -"))
        venue = " - ".join(p for p in venue_parts if p)

        uid = f"team{TEAM_ID}-{start:%Y%m%dT%H%M}@kidsinc-calendar"
        games[uid] = {
            "start": start,
            "summary": f"{teams[0]} vs {teams[1]}",
            "venue": venue,
        }

    modified = None
    m = re.search(r"Last Modified:\**\s*([A-Za-z]{3}, [A-Za-z]{3} \d{1,2} \d{4} \d{1,2}:\d{2} [AP]M)", soup.get_text(" "))
    if m:
        try:
            modified = datetime.strptime(m.group(1), "%a, %b %d %Y %I:%M %p").replace(tzinfo=TZ)
        except ValueError:
            pass
    return team, games, modified


def esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    """RFC 5545 line folding at 75 octets."""
    raw, out = line.encode("utf-8"), []
    while len(raw) > 75:
        cut = 75
        while (raw[cut] & 0xC0) == 0x80:  # don't split a multibyte char
            cut -= 1
        out.append(raw[:cut].decode("utf-8"))
        raw = b" " + raw[cut:]
    out.append(raw.decode("utf-8"))
    return "\r\n".join(out)


def build_ics(team, games, modified):
    stamp = (modified or datetime(2026, 1, 1, tzinfo=TZ)).astimezone(timezone.utc)
    stamp = stamp.strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//kidsinc schedule scraper//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{esc(team)}",
        f"X-WR-TIMEZONE:{TZ_NAME}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
        "X-PUBLISHED-TTL:PT12H",
        "BEGIN:VTIMEZONE",
        f"TZID:{TZ_NAME}",
        "BEGIN:STANDARD",
        "DTSTART:19701101T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
        "TZOFFSETFROM:-0500",
        "TZOFFSETTO:-0600",
        "TZNAME:CST",
        "END:STANDARD",
        "BEGIN:DAYLIGHT",
        "DTSTART:19700308T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
        "TZOFFSETFROM:-0600",
        "TZOFFSETTO:-0500",
        "TZNAME:CDT",
        "END:DAYLIGHT",
        "END:VTIMEZONE",
    ]
    for uid, g in sorted(games.items(), key=lambda kv: kv[1]["start"]):
        end = g["start"] + GAME_LENGTH
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;TZID={TZ_NAME}:{g['start']:%Y%m%dT%H%M%S}",
            f"DTEND;TZID={TZ_NAME}:{end:%Y%m%dT%H%M%S}",
            f"SUMMARY:{esc(g['summary'])}",
        ]
        if g["venue"]:
            lines.append(f"LOCATION:{esc(g['venue'])}")
        lines += [f"DESCRIPTION:{esc('Source: ' + URL)}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def main():
    html = fetch(sys.argv[1] if len(sys.argv) > 1 else None)
    team, games, modified = parse(html)
    if not games:
        # Don't overwrite a good calendar with an empty one if the page changes.
        sys.exit("No games found - page layout may have changed. Leaving file untouched.")
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        f.write(build_ics(team, games, modified))
    print(f"Wrote {len(games)} games to {OUTPUT}")


if __name__ == "__main__":
    main()
