from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from html import unescape
from zoneinfo import ZoneInfo

from .net import fetch_bytes
from .store import digest_text

BLS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
SOURCES = {"BLS": BLS_URL, "Federal Reserve": FED_URL}
EASTERN = ZoneInfo("America/New_York")
FRESH_FOR = timedelta(hours=24)
REFRESH_AFTER = timedelta(hours=6)
MONTHS = {name: index for index, name in enumerate(
    ("January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"), 1)}


def _iso(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).isoformat(timespec="seconds")


def _in_horizon(start: datetime, end: datetime, now: datetime) -> bool:
    return end >= now - timedelta(days=7) and start <= now + timedelta(days=365)


def parse_bls_calendar(raw: bytes, now: datetime | None = None) -> list[dict]:
    instant = now or datetime.now(timezone.utc)
    lines: list[str] = []
    for line in raw.decode("utf-8-sig").splitlines():
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line.strip())
    events: list[dict] = []
    item: dict[str, str] | None = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            item = {}
        elif line == "END:VEVENT":
            if item is None:
                continue
            summary = item.get("SUMMARY", "").replace("\\,", ",")
            if summary not in ("Consumer Price Index", "Employment Situation"):
                item = None
                continue
            uid, time_text = item.get("UID"), item.get("DTSTART")
            if not uid or not time_text:
                raise ValueError("BLS calendar event lacks UID or start time")
            if time_text.endswith("Z"):
                event_time = datetime.strptime(time_text, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc)
            elif item.get("DTSTART_TZID") in ("US-Eastern", "America/New_York"):
                event_time = datetime.strptime(time_text, "%Y%m%dT%H%M%S").replace(
                    tzinfo=EASTERN).astimezone(timezone.utc)
            else:
                raise ValueError("BLS calendar uses an unrecognized time zone")
            start = event_time - timedelta(minutes=30)
            end = event_time + timedelta(minutes=60)
            if _in_horizon(start, end, instant):
                events.append({"source": "BLS", "event_uid": uid, "name": summary,
                               "event_at": _iso(event_time), "starts_at": _iso(start),
                               "ends_at": _iso(end),
                               "risk_groups": ["fed-policy", "us-macro-data"],
                               "source_url": BLS_URL})
            item = None
        elif item is not None and ":" in line:
            key, value = line.split(":", 1)
            parts = key.split(";")
            item[parts[0]] = value
            if parts[0] == "DTSTART":
                for part in parts[1:]:
                    if part.startswith("TZID="):
                        item["DTSTART_TZID"] = part[5:]
    if {event["name"] for event in events} != {
            "Consumer Price Index", "Employment Situation"}:
        raise ValueError("BLS calendar lacks a CPI or Employment release in the horizon")
    return events


def parse_fed_calendar(raw: bytes, now: datetime | None = None) -> list[dict]:
    instant = now or datetime.now(timezone.utc)
    html = raw.decode("utf-8-sig", "replace")
    events: list[dict] = []
    for year in (instant.year, instant.year + 1):
        heading = re.search(rf"\b{year} FOMC Meetings</a>", html)
        if not heading:
            continue
        tail = html[heading.end():]
        next_heading = re.search(r"\b20\d{2} FOMC Meetings</a>", tail)
        section = tail[:next_heading.start()] if next_heading else tail
        matches = re.findall(
            r'fomc-meeting__month[^>]*>\s*<strong>([A-Za-z]+)</strong>.*?'
            r'fomc-meeting__date[^>]*>([^<]+)</div>', section, flags=re.S)
        for month_name, date_text in matches:
            month = MONTHS.get(unescape(month_name))
            days = [int(day) for day in re.findall(r"\d{1,2}", date_text)]
            if month is None or not days or len(days) > 2:
                raise ValueError("Unrecognized Fed meeting date")
            first = datetime(year, month, days[0], tzinfo=EASTERN)
            last = datetime(year, month, days[-1], tzinfo=EASTERN)
            end = last + timedelta(days=1)
            if _in_horizon(first, end, instant):
                events.append({
                    "source": "Federal Reserve",
                    "event_uid": f"fomc-{year}-{month:02d}-{days[-1]:02d}",
                    "name": f"FOMC meeting {year}-{month:02d}-{days[0]:02d}"
                            f" to {year}-{month:02d}-{days[-1]:02d}",
                    "event_at": _iso(first), "starts_at": _iso(first),
                    "ends_at": _iso(end), "risk_groups": ["fed-policy"],
                    "source_url": FED_URL,
                })
    if not events:
        raise ValueError("Fed calendar has no future meeting in the horizon")
    return events


def calendar_due(conn: sqlite3.Connection, now: datetime | None = None) -> bool:
    instant = now or datetime.now(timezone.utc)
    for source in SOURCES:
        row = conn.execute(
            "SELECT fetched_at FROM calendar_fetches WHERE source=?", (source,)
        ).fetchone()
        age = instant - datetime.fromisoformat(row["fetched_at"]) if row else None
        if age is None or age < timedelta(0) or age >= REFRESH_AFTER:
            return True
    return False


def refresh_calendars(conn: sqlite3.Connection, *, force: bool = False,
                      now: datetime | None = None) -> dict:
    instant = now or datetime.now(timezone.utc)
    results: dict[str, dict] = {}
    for source, url in SOURCES.items():
        prior = conn.execute(
            "SELECT fetched_at FROM calendar_fetches WHERE source=?", (source,)
        ).fetchone()
        age = instant - datetime.fromisoformat(prior["fetched_at"]) if prior else None
        if (not force and age is not None and
                timedelta(0) <= age < REFRESH_AFTER):
            results[source] = {"status": "fresh", "fetched_at": prior["fetched_at"]}
            continue
        try:
            raw = fetch_bytes(url, accept="text/calendar" if source == "BLS" else "text/html")
            windows = (parse_bls_calendar(raw, instant) if source == "BLS"
                       else parse_fed_calendar(raw, instant))
        except Exception as exc:
            results[source] = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
            continue
        fetched_at = _iso(instant)
        content_hash = hashlib.sha256(raw).hexdigest()
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for window in windows:
                revision_hash = digest_text({key: window[key] for key in (
                    "name", "event_at", "starts_at", "ends_at", "risk_groups")})
                conn.execute("""INSERT OR IGNORE INTO calendar_revisions
                    (source,event_uid,content_hash,observed_at,name,event_at,starts_at,ends_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (source, window["event_uid"], revision_hash, fetched_at,
                     window["name"], window["event_at"], window["starts_at"], window["ends_at"]))
            conn.execute("DELETE FROM calendar_windows WHERE source=?", (source,))
            conn.executemany("""INSERT INTO calendar_windows
                (source,event_uid,name,event_at,starts_at,ends_at,risk_groups_json,
                 source_url,fetched_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                [(source, window["event_uid"], window["name"], window["event_at"],
                  window["starts_at"], window["ends_at"],
                  json.dumps(window["risk_groups"]), url, fetched_at) for window in windows])
            conn.execute("""INSERT INTO calendar_fetches(source,source_url,fetched_at,content_hash)
                VALUES(?,?,?,?) ON CONFLICT(source) DO UPDATE SET
                source_url=excluded.source_url,fetched_at=excluded.fetched_at,
                content_hash=excluded.content_hash""",
                (source, url, fetched_at, content_hash))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        results[source] = {"status": "updated", "windows": len(windows),
                           "fetched_at": fetched_at, "content_hash": content_hash}
    return results


def active_official_window(conn: sqlite3.Connection, risk_group: str,
                           now: datetime | None = None) -> str | None:
    instant = now or datetime.now(timezone.utc)
    if risk_group == "fed-policy":
        required = ("Federal Reserve", "BLS")
    elif risk_group == "us-macro-data":
        required = ("BLS",)
    else:
        return None
    for source in required:
        row = conn.execute(
            "SELECT fetched_at FROM calendar_fetches WHERE source=?", (source,)
        ).fetchone()
        age = instant - datetime.fromisoformat(row["fetched_at"]) if row else None
        if age is None or not timedelta(0) <= age <= FRESH_FOR:
            raise ValueError(f"Official {source} release calendar is missing or stale")
    moment = _iso(instant)
    for row in conn.execute("""SELECT source,name,risk_groups_json FROM calendar_windows
        WHERE starts_at<=? AND ends_at>? ORDER BY starts_at""", (moment, moment)):
        if risk_group in json.loads(row["risk_groups_json"]):
            return f'{row["source"]}: {row["name"]}'
    return None
