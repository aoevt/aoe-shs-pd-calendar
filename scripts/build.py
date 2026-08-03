"""Fetch a published Outlook ICS feed and generate sessions.js for the calendar widget.

Reads the feed URL from the ICS_URL environment variable (set as a GitHub Actions
secret). Never commit the feed URL to the repository: the long token in the URL
grants read access to the calendar.

Conventions read from each event:
  - CATEGORIES  -> program area (first category wins; used for tag + filter)
  - DESCRIPTION -> optional structured lines, case-insensitive:
        Audience: <text>
        Register: <url or "No registration needed">
        Details: <url>
  - Location containing "virtual" (or empty) is shown as Virtual

Events with no category are grouped under "General".
Recurring events are expanded into individual dated sessions.
"""

import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta

import icalendar
import recurring_ical_events

# Program areas are assigned colors in first-seen order from this palette.
# To pin a category to a specific color permanently, add it to PINNED below.
# Prefixes stripped from category names before display (Outlook keeps the
# namespaced name, e.g. "SHS - BTA"; the website shows "BTA").
STRIP_PREFIXES = ["SHS - ", "SHS-"]

PALETTE = ["#007834", "#6e2c8e", "#00629b", "#9c3d00", "#4d5d2c", "#8a1e41", "#4a4a4a", "#00575c"]
# Overflow colors for categories not pinned below (all pass 4.5:1 with white text)
OVERFLOW = ["#205493", "#5b3256", "#254441", "#6b3f00", "#5c5c5c"]
PINNED = {
    "BTA": "#007834",
    "HHB": "#6e2c8e",
    "McKinney-Vento": "#00629b",
    "Rule 4500": "#9c3d00",
    "Foster Care": "#4d5d2c",
    "Universal Screening": "#8a1e41",
    "School Safety": "#4a4a4a",
    "Discipline": "#00575c",
}

WINDOW_PAST_DAYS = 60       # keep this many days of past sessions (widget hides them by default)
WINDOW_FUTURE_DAYS = 550    # look ahead this many days


def fetch_ics(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "aoe-calendar-widget/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def text_of(component, key, default=""):
    v = component.get(key)
    if v is None:
        return default
    try:
        return str(v)
    except Exception:
        return default


def parse_description(desc: str):
    """Pull Audience:/Register:/Details: lines out of the event body."""
    out = {"audience": "", "register": "", "details": ""}
    if not desc:
        return out
    # Outlook bodies arrive with literal \n sequences and carriage returns
    desc = desc.replace("\\n", "\n").replace("\r", "")
    for line in desc.split("\n"):
        line = line.strip()
        m = re.match(r"(?i)^(audience|register|details)\s*:\s*(.+)$", line)
        if m:
            out[m.group(1).lower()] = m.group(2).strip()
    return out


def fmt_time(dt):
    if not isinstance(dt, datetime):
        return ""
    h = dt.hour % 12 or 12
    ampm = "a.m." if dt.hour < 12 else "p.m."
    if dt.minute:
        return f"{h}:{dt.minute:02d} {ampm}"
    return f"{h}:00 {ampm}"


def main():
    url = os.environ.get("ICS_URL", "").strip()
    if not url:
        print("ERROR: ICS_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    raw = fetch_ics(url)
    cal = icalendar.Calendar.from_ical(raw)

    start = date.today() - timedelta(days=WINDOW_PAST_DAYS)
    end = date.today() + timedelta(days=WINDOW_FUTURE_DAYS)
    events = recurring_ical_events.of(cal).between(start, end)

    programs = {}
    colors_used = 0
    sessions = []

    def program_key(name: str) -> str:
        nonlocal colors_used
        for pre in STRIP_PREFIXES:
            if name.lower().startswith(pre.lower()):
                name = name[len(pre):].strip()
                break
        key = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "general"
        if key not in programs:
            if name in PINNED:
                color = PINNED[name]
            else:
                available = [c for c in PALETTE + OVERFLOW if c not in PINNED.values()] or OVERFLOW
                color = available[colors_used % len(available)]
                colors_used += 1
            label = name if len(name) <= 16 else name  # full name; widget sizes tags to fit
            programs[key] = {"label": label, "name": name, "color": color}
        return key

    for ev in events:
        summary = text_of(ev, "SUMMARY").strip()
        if not summary:
            continue

        dtstart = ev.get("DTSTART").dt
        dtend = ev.get("DTEND").dt if ev.get("DTEND") else None

        if isinstance(dtstart, datetime):
            d = dtstart.date()
            time_str = fmt_time(dtstart)
            if isinstance(dtend, datetime):
                time_str += " - " + fmt_time(dtend)
        else:
            d = dtstart
            time_str = "All day"

        cats = ev.get("CATEGORIES")
        if cats:
            try:
                cat_list = cats.to_ical().decode("utf-8", "ignore").split(",")
            except AttributeError:
                cat_list = [str(cats)]
            category = cat_list[0].strip() or "General"
        else:
            category = "General"
        pkey = program_key(category)

        body = parse_description(text_of(ev, "DESCRIPTION"))
        location = text_of(ev, "LOCATION").strip()
        loc_str = "Virtual" if (not location or "virtual" in location.lower()
                                or "teams" in location.lower() or "zoom" in location.lower()) else location

        meta_parts = [p for p in [time_str, loc_str, body["audience"]] if p]

        session = {
            "date": d.isoformat(),
            "program": pkey,
            "title": summary,
            "meta": " \u00b7 ".join(meta_parts),
        }

        reg = body["register"]
        if reg and reg.lower().startswith("http"):
            session["action"] = "reg"
            session["url"] = reg
            session["linkText"] = "Register"
        elif body["details"].lower().startswith("http"):
            session["action"] = "details"
            session["url"] = body["details"]
            session["linkText"] = "More information"
        elif reg:
            session["action"] = "none"
            session["noteText"] = reg
        else:
            session["action"] = "none"
            session["noteText"] = ""

        sessions.append(session)

    sessions.sort(key=lambda s: s["date"])

    out = (
        "// Generated by scripts/build.py - do not edit by hand.\n"
        "// Last build: " + datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%d %H:%M UTC") + "\n"
        "const PROGRAMS = " + json.dumps(programs, indent=2) + ";\n"
        "const SESSIONS = " + json.dumps(sessions, indent=2) + ";\n"
    )

    out_path = os.path.join(os.path.dirname(__file__), "..", "sessions.js")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out)

    print(f"Wrote {len(sessions)} sessions across {len(programs)} program areas to sessions.js")


if __name__ == "__main__":
    main()
