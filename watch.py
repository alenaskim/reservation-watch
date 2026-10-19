"""Check Resy and OpenTable for open tables at flagged restaurants and push alerts via ntfy.

Usage:
    python watch.py                 # check everything, alert on new slots
    python watch.py --dry-run       # print what would be alerted, send nothing
    python watch.py --config other.yaml --state seen.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
import yaml

# Public key the resy.com web app sends with every request (not a personal credential).
RESY_API_KEY = "VbWk7s3L4KiK5fzlO7JD3Q2EeBwV4yZt"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass(frozen=True)
class Slot:
    restaurant: str
    platform: str
    when: dt.datetime
    party_size: int
    url: str
    label: str = ""

    @property
    def key(self) -> str:
        return f"{self.platform}|{self.restaurant}|{self.when.isoformat()}|{self.party_size}"


# ---------------------------------------------------------------- config


def parse_window(spec: str | None) -> tuple[dt.time, dt.time]:
    if not spec:
        return dt.time(0, 0), dt.time(23, 59)
    start, end = (s.strip() for s in spec.split("-"))
    return dt.time.fromisoformat(start), dt.time.fromisoformat(end)


def dates_for(entry: dict, today: dt.date) -> list[dt.date]:
    if entry.get("dates"):
        dates = [d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d)) for d in entry["dates"]]
        return sorted(d for d in dates if d >= today)
    days = int(entry.get("next_days", 14))
    allowed = {w.lower()[:3] for w in entry.get("weekdays", WEEKDAYS)}
    candidates = (today + dt.timedelta(days=i) for i in range(days))
    return [d for d in candidates if WEEKDAYS[d.weekday()] in allowed]


def in_window(when: dt.datetime, window: tuple[dt.time, dt.time]) -> bool:
    return window[0] <= when.time() <= window[1]


# ---------------------------------------------------------------- resy


def resy_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f'ResyAPI api_key="{RESY_API_KEY}"',
            "Origin": "https://resy.com",
            "Referer": "https://resy.com/",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
    )
    return s


def resy_venue_id(session: requests.Session, entry: dict) -> int:
    if entry.get("resy_venue_id"):
        return int(entry["resy_venue_id"])
    # https://resy.com/cities/new-york-ny/venues/lilia  or  https://resy.com/cities/ny/lilia
    parts = [p for p in urlparse(entry["resy"]).path.split("/") if p and p != "venues"]
    city, slug = parts[1], parts[-1]
    r = session.get("https://api.resy.com/3/venue", params={"url_slug": slug, "location": city}, timeout=20)
    r.raise_for_status()
    return int(r.json()["id"]["resy"])


def parse_resy_find(payload: dict) -> list[tuple[dt.datetime, str]]:
    out = []
    for venue in payload.get("results", {}).get("venues", []):
        for slot in venue.get("slots", []):
            start = dt.datetime.strptime(slot["date"]["start"], "%Y-%m-%d %H:%M:%S")
            out.append((start, slot.get("config", {}).get("type", "")))
    return out


def check_resy(session: requests.Session, entry: dict, party: int, dates: list[dt.date]) -> list[Slot]:
    venue_id = resy_venue_id(session, entry)
    base_url = entry["resy"].split("?")[0]
    slots = []
    for day in dates:
        r = session.get(
            "https://api.resy.com/4/find",
            params={"lat": 0, "long": 0, "day": day.isoformat(), "party_size": party, "venue_id": venue_id},
            timeout=20,
        )
        r.raise_for_status()
        for when, label in parse_resy_find(r.json()):
            slots.append(
                Slot(entry["name"], "Resy", when, party, f"{base_url}?date={day.isoformat()}&seats={party}", label)
            )
        time.sleep(random.uniform(1.0, 2.5))  # stay polite
    return slots


# ---------------------------------------------------------------- opentable


def parse_opentable_availability(payload, requested: dt.datetime) -> list[dt.datetime]:
    """Find every available slot in an OpenTable availability response.

    The GraphQL shape shifts over time, so walk the JSON for slot-like objects
    ({isAvailable, timeOffsetMinutes}) rather than relying on exact paths.
    Offsets are relative to the requested time; dayOffset on the parent day shifts the date.
    """
    found: list[dt.datetime] = []

    def walk(node, day_offset=0):
        if isinstance(node, dict):
            day_offset = node.get("dayOffset", day_offset)
            if "timeOffsetMinutes" in node and node.get("isAvailable"):
                found.append(requested + dt.timedelta(days=day_offset, minutes=node["timeOffsetMinutes"]))
            for v in node.values():
                walk(v, day_offset)
        elif isinstance(node, list):
            for v in node:
                walk(v, day_offset)

    walk(payload)
    return sorted(set(found))


def check_opentable(browser, entry: dict, party: int, dates: list[dt.date], window) -> list[Slot]:
    base_url = entry["opentable"].split("?")[0]
    # Ask for the middle of the window (rounded to :00/:30); OpenTable returns slots around it.
    mid_minutes = (window[0].hour * 60 + window[0].minute + window[1].hour * 60 + window[1].minute) // 2
    mid = dt.time(mid_minutes // 60, 30 if mid_minutes % 60 >= 30 else 0)
    slots = []
    for day in dates:
        requested = dt.datetime.combine(day, mid)
        url = f"{base_url}?dateTime={quote(requested.strftime('%Y-%m-%dT%H:%M'))}&covers={party}"
        responses = []
        page = browser.new_page(user_agent=USER_AGENT)
        page.on(
            "response",
            lambda resp: responses.append(resp) if "RestaurantsAvailability" in resp.url else None,
        )
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(6000)
            for resp in responses:
                try:
                    for when in parse_opentable_availability(resp.json(), requested):
                        booking = f"{base_url}?dateTime={quote(when.strftime('%Y-%m-%dT%H:%M'))}&covers={party}"
                        slots.append(Slot(entry["name"], "OpenTable", when, party, booking))
                except Exception:
                    continue
            if not responses:
                print(f"  ! OpenTable returned no availability data for {entry['name']} {day} "
                      "(possibly bot-blocked)", file=sys.stderr)
        finally:
            page.close()
        time.sleep(random.uniform(2.0, 4.0))
    return slots


# ---------------------------------------------------------------- alerts + state


def notify(slots: list[Slot], topic: str, server: str) -> None:
    by_restaurant: dict[str, list[Slot]] = {}
    for s in slots:
        by_restaurant.setdefault(s.restaurant, []).append(s)
    for name, group in by_restaurant.items():
        group.sort(key=lambda s: s.when)
        lines = [
            f"{s.when:%a %b %-d, %-I:%M %p} · {s.party_size}p" + (f" · {s.label}" if s.label else "")
            for s in group[:8]
        ]
        if len(group) > 8:
            lines.append(f"…and {len(group) - 8} more")
        requests.post(
            f"{server.rstrip('/')}/{topic}",
            data="\n".join(lines).encode(),
            headers={
                "Title": f"{name}: table open on {group[0].platform}",
                "Click": group[0].url,
                "Tags": "fork_and_knife",
                "Priority": "high",
            },
            timeout=20,
        ).raise_for_status()


def load_seen(path: Path) -> dict[str, str]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def next_seen(seen: dict[str, str], found: list[Slot], failed: set[str], now: str) -> dict[str, str]:
    """Remember only slots that are open right now, so a slot that gets taken and
    reopens later alerts again. Restaurants that errored this run keep their old keys."""
    current = {s.key: seen.get(s.key, now) for s in found}
    kept = {k: v for k, v in seen.items() if k.split("|")[1] in failed}
    return {**kept, **current}


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="restaurants.yaml")
    ap.add_argument("--state", default="state/seen.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    config = yaml.safe_load(Path(args.config).read_text())
    default_party = int(config.get("party_size", 2))
    today = dt.date.today()
    state_path = Path(args.state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    seen = load_seen(state_path)

    topic = os.environ.get("NTFY_TOPIC")
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
    if not topic and not args.dry_run:
        print("NTFY_TOPIC is not set; use --dry-run to test without alerts.", file=sys.stderr)
        return 2

    resy = resy_session()
    browser = playwright = None
    found: list[Slot] = []
    failed: set[str] = set()

    for entry in config.get("watch", []):
        party = int(entry.get("party_size", default_party))
        window = parse_window(entry.get("times"))
        dates = dates_for(entry, today)
        if not dates:
            continue
        try:
            if entry.get("resy"):
                slots = check_resy(resy, entry, party, dates)
            elif entry.get("opentable"):
                if browser is None:
                    from playwright.sync_api import sync_playwright

                    playwright = sync_playwright().start()
                    browser = playwright.chromium.launch()
                slots = check_opentable(browser, entry, party, dates, window)
            else:
                print(f"  ! {entry.get('name')}: needs a resy or opentable URL", file=sys.stderr)
                continue
        except Exception as e:  # one broken restaurant shouldn't stop the rest
            failed.add(entry.get("name"))
            print(f"  ! {entry.get('name')}: {e}", file=sys.stderr)
            continue
        matching = [s for s in slots if in_window(s.when, window)]
        print(f"{entry['name']}: {len(matching)} slot(s) in window across {len(dates)} date(s)")
        found.extend(matching)

    if browser:
        browser.close()
        playwright.stop()

    new = [s for s in found if s.key not in seen]
    for s in new:
        print(f"  NEW  {s.restaurant} {s.platform} {s.when:%Y-%m-%d %H:%M} ({s.party_size}p) {s.url}")
    if new and not args.dry_run:
        notify(new, topic, server)
    if not args.dry_run:
        now = dt.datetime.now().isoformat(timespec="seconds")
        state_path.write_text(json.dumps(next_seen(seen, found, failed, now), indent=1, sort_keys=True))
    return 1 if failed and not found else 0


if __name__ == "__main__":
    sys.exit(main())
