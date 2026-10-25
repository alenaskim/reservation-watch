"""Check Resy and OpenTable for open tables at flagged restaurants and push alerts via ntfy.

Usage:
    python watch.py                 # check everything once, alert on new slots
    python watch.py --serve         # run forever: background checks + release-time sprints
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
from zoneinfo import ZoneInfo

import requests
import yaml

# Public key the resy.com web app sends with every request (not a personal credential).
RESY_API_KEY = "VbWk7s3L4KiK5fzlO7JD3Q2EeBwV4yZt"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# Restaurants release tables on New York time; servers usually run on UTC.
TZ = ZoneInfo(os.environ.get("WATCH_TZ", "America/New_York"))


def now_local() -> dt.datetime:
    """Current wall-clock time in TZ, without tzinfo (matches Resy/OpenTable slot times)."""
    return dt.datetime.now(TZ).replace(tzinfo=None)


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


def resy_venue(session: requests.Session, entry: dict) -> tuple[int, str]:
    """Return (venue id, booking page URL) for a Resy entry."""
    base_url = entry["resy"].split("?")[0]
    if entry.get("resy_venue_id"):
        return int(entry["resy_venue_id"]), base_url
    # https://resy.com/cities/new-york-ny/venues/lilia  or  https://resy.com/cities/ny/lilia
    parts = [p for p in urlparse(entry["resy"]).path.split("/") if p and p != "venues"]
    city, slug = parts[1], parts[-1]
    r = session.get("https://api.resy.com/3/venue", params={"url_slug": slug, "location": city}, timeout=20)
    if r.ok:
        venue_id = int(r.json()["id"]["resy"])
        print(f"  {entry['name']}: Resy venue {venue_id} (from URL)")
        return venue_id, base_url
    # Slug didn't resolve; fall back to Resy's own name search around NYC.
    r = session.post(
        "https://api.resy.com/3/venuesearch/search",
        json={
            "query": entry.get("search", entry["name"]),
            "geo": {"latitude": 40.7128, "longitude": -74.0060},
            "types": ["venue"],
            "per_page": 5,
        },
        timeout=20,
    )
    r.raise_for_status()
    hits = r.json().get("search", {}).get("hits", [])
    if not hits:
        raise RuntimeError(f"no Resy venue found for {entry['name']!r}; add resy_venue_id to restaurants.yaml")
    hit = hits[0]
    venue_id = int(hit["id"]["resy"])
    print(f"  {entry['name']}: Resy venue {venue_id} = {hit.get('name')!r} (from search; "
          "add resy_venue_id to pin it)")
    if hit.get("url_slug"):
        city = hit.get("location", {}).get("url_slug") or city
        base_url = f"https://resy.com/cities/{city}/venues/{hit['url_slug']}"
    return venue_id, base_url


def resy_open_dates(session: requests.Session, venue_id: int, party: int, dates: list[dt.date]) -> list[dt.date]:
    """One calendar call tells us which days have any bookable inventory, so we only
    fetch exact times for those. If the calendar call fails, check every date."""
    try:
        r = session.get(
            "https://api.resy.com/4/venue/calendar",
            params={
                "venue_id": venue_id,
                "num_seats": party,
                "start_date": dates[0].isoformat(),
                "end_date": dates[-1].isoformat(),
            },
            timeout=20,
        )
        r.raise_for_status()
        available = {
            d["date"] for d in r.json().get("scheduled", []) if d.get("inventory", {}).get("reservation") == "available"
        }
    except Exception as e:
        print(f"  ! calendar lookup failed ({e}); checking every date", file=sys.stderr)
        return dates
    return [d for d in dates if d.isoformat() in available]


def parse_resy_find(payload: dict) -> list[tuple[dt.datetime, str]]:
    out = []
    for venue in payload.get("results", {}).get("venues", []):
        for slot in venue.get("slots", []):
            start = dt.datetime.strptime(slot["date"]["start"], "%Y-%m-%d %H:%M:%S")
            out.append((start, slot.get("config", {}).get("type", "")))
    return out


def check_resy(session: requests.Session, entry: dict, party: int, dates: list[dt.date],
               venue: tuple[int, str] | None = None, polite: bool = True) -> list[Slot]:
    venue_id, base_url = venue or resy_venue(session, entry)
    slots = []
    for day in resy_open_dates(session, venue_id, party, dates):
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
        if polite:
            time.sleep(random.uniform(1.0, 2.5))
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


def notify(slots: list[Slot], topic: str, server: str, urgent: bool = False) -> None:
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
                "Title": f"{name}: table open on {group[0].platform}" + (" - BOOK NOW" if urgent else ""),
                "Click": group[0].url,
                "Tags": "rotating_light" if urgent else "fork_and_knife",
                "Priority": "urgent" if urgent else "high",
            },
            timeout=20,
        ).raise_for_status()


def send_message(topic: str, server: str, title: str, body: str, click: str | None = None,
                 priority: str = "default") -> None:
    headers = {"Title": title, "Priority": priority, "Tags": "alarm_clock"}
    if click:
        headers["Click"] = click
    requests.post(f"{server.rstrip('/')}/{topic}", data=body.encode(), headers=headers, timeout=20).raise_for_status()


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


# ---------------------------------------------------------------- release-time sprints

SPRINT_LEAD = dt.timedelta(seconds=30)  # start polling a little before the drop
SPRINT_LENGTH = dt.timedelta(minutes=10)
HEADS_UP = dt.timedelta(minutes=5)


@dataclass(frozen=True)
class Drop:
    """One scheduled release: at `at`, tables for `released` go live."""

    entry_name: str
    at: dt.datetime
    released: dt.date

    @property
    def key(self) -> str:
        return f"{self.entry_name}|{self.at.isoformat()}"


def drops_for(entry: dict, day: dt.date) -> list[Drop]:
    """Drops happening on `day` for this restaurant, skipping released dates you don't want.

    Config:  drop: {time: "11:00", days_ahead: 20, skip: [tue]}
    `skip` lists weekdays on which the restaurant doesn't release.
    """
    spec = entry.get("drop")
    if not spec or WEEKDAYS[day.weekday()] in {w.lower()[:3] for w in spec.get("skip", [])}:
        return []
    released = day + dt.timedelta(days=int(spec["days_ahead"]))
    wanted = {w.lower()[:3] for w in entry.get("weekdays", WEEKDAYS)}
    if WEEKDAYS[released.weekday()] not in wanted:
        return []
    if entry.get("dates") and released.isoformat() not in {str(d) for d in entry["dates"]}:
        return []
    at = dt.datetime.combine(day, dt.time.fromisoformat(str(spec["time"])))
    return [Drop(entry["name"], at, released)]


# ---------------------------------------------------------------- watcher


class Watcher:
    def __init__(self, config_path: str, state_path: str, dry_run: bool):
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.seen = load_seen(self.state_path)
        self.dry_run = dry_run
        self.topic = os.environ.get("NTFY_TOPIC")
        self.server = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
        self.resy = resy_session()
        self.venues: dict[str, tuple[int, str]] = {}  # resolved once per process
        self.reload()

    def reload(self) -> None:
        config = yaml.safe_load(self.config_path.read_text())
        default_party = int(config.get("party_size", 2))
        defaults = config.get("defaults", {})
        self.entries = []
        for entry in config.get("watch", []):
            entry = {**defaults, **entry}
            entry["party_size"] = int(entry.get("party_size", default_party))
            self.entries.append(entry)

    def save(self) -> None:
        if not self.dry_run:
            self.state_path.write_text(json.dumps(self.seen, indent=1, sort_keys=True))

    def alert(self, slots: list[Slot], urgent: bool = False) -> None:
        for s in slots:
            print(f"  NEW  {s.restaurant} {s.platform} {s.when:%Y-%m-%d %H:%M} ({s.party_size}p) {s.url}", flush=True)
        if slots and not self.dry_run:
            notify(slots, self.topic, self.server, urgent=urgent)

    def message(self, title: str, body: str, click: str | None = None, priority: str = "default") -> None:
        print(f"  MSG  {title}: {body}", flush=True)
        if not self.dry_run:
            send_message(self.topic, self.server, title, body, click, priority)

    def resy_venue(self, entry: dict) -> tuple[int, str]:
        if entry["name"] not in self.venues:
            self.venues[entry["name"]] = resy_venue(self.resy, entry)
        return self.venues[entry["name"]]

    # -- background pass over every restaurant and date

    def check_all(self) -> int:
        today = now_local().date()
        browser = playwright = None
        found: list[Slot] = []
        failed: set[str] = set()
        for entry in self.entries:
            party = entry["party_size"]
            window = parse_window(entry.get("times"))
            dates = dates_for(entry, today)
            if not dates:
                continue
            try:
                if entry.get("resy"):
                    slots = check_resy(self.resy, entry, party, dates, self.resy_venue(entry))
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
            print(f"{entry['name']}: {len(matching)} slot(s) in window across {len(dates)} date(s)", flush=True)
            found.extend(matching)
        if browser:
            browser.close()
            playwright.stop()

        self.alert([s for s in found if s.key not in self.seen])
        self.seen = next_seen(self.seen, found, failed, now_local().isoformat(timespec="seconds"))
        self.save()
        return 1 if failed and not found else 0

    # -- fast polling right around a release

    def sprint_poll(self, entry: dict, drop: Drop) -> None:
        """One quick look at the newly released date (±1 day, in case the restaurant's
        "N days out" is counted differently). Calendar first, then exact times only
        for dates that just opened."""
        party = entry["party_size"]
        window = parse_window(entry.get("times"))
        dates = [drop.released + dt.timedelta(days=i) for i in (-1, 0, 1)]
        venue = self.resy_venue(entry)
        slots = check_resy(self.resy, entry, party, dates, venue, polite=False)
        new = [s for s in slots if in_window(s.when, window) and s.key not in self.seen]
        if new:
            self.alert(new, urgent=True)
            now = now_local().isoformat(timespec="seconds")
            self.seen.update({s.key: now for s in new})
            self.save()

    def serve(self, check_every: dt.timedelta, poll_seconds: float) -> None:
        sprintable = [e for e in self.entries if e.get("drop") and e.get("resy")]
        upcoming = self.upcoming_drops(now_local())
        first = (f"next: {upcoming[0][1].entry_name} at {upcoming[0][1].at:%a %-I:%M %p}" if upcoming
                 else "no release times configured")
        self.message("Reservation watch is running",
                     f"Watching {len(self.entries)} restaurants; release-time sprints for "
                     f"{len(sprintable)}; {first}.")
        next_check = now_local()
        heads_up_sent: set[str] = set()
        last_poll: dict[str, dt.datetime] = {}
        while True:
            now = now_local()
            active = []
            for entry, drop in self.upcoming_drops(now):
                if drop.at - HEADS_UP <= now < drop.at - SPRINT_LEAD and drop.key not in heads_up_sent:
                    heads_up_sent.add(drop.key)
                    base = entry["resy"].split("?")[0]
                    try:
                        self.message(f"{entry['name']} releases tables in 5 min",
                                     f"{drop.at:%-I:%M %p}: tables for {drop.released:%a %b %-d} go live. "
                                     "Open Resy now and stay logged in.",
                                     click=f"{base}?date={drop.released.isoformat()}&seats={entry['party_size']}",
                                     priority="high")
                    except Exception as e:
                        print(f"  ! heads-up failed: {e}", file=sys.stderr)
                if drop.at - SPRINT_LEAD <= now <= drop.at + SPRINT_LENGTH:
                    active.append((entry, drop))

            if active:
                for entry, drop in active:
                    if now - last_poll.get(drop.key, dt.datetime.min) >= dt.timedelta(seconds=poll_seconds):
                        last_poll[drop.key] = now
                        try:
                            self.sprint_poll(entry, drop)
                        except Exception as e:
                            print(f"  ! sprint {entry['name']}: {e}", file=sys.stderr, flush=True)
                time.sleep(random.uniform(0.3, 1.0))
                continue

            # Don't start a slow background pass right before a sprint.
            soon = any(drop.at - HEADS_UP - check_every / 3 <= now < drop.at for _, drop in self.upcoming_drops(now))
            if now >= next_check and not soon:
                print(f"--- background check {now:%Y-%m-%d %H:%M}", flush=True)
                try:
                    self.reload()
                    self.check_all()
                except Exception as e:
                    print(f"  ! background check failed: {e}", file=sys.stderr, flush=True)
                next_check = now_local() + check_every
            time.sleep(1)

    def upcoming_drops(self, now: dt.datetime) -> list[tuple[dict, Drop]]:
        """Today's and tomorrow's drops that haven't finished yet, soonest first."""
        out = []
        for entry in self.entries:
            if not entry.get("resy"):
                continue
            for day in (now.date(), now.date() + dt.timedelta(days=1)):
                for drop in drops_for(entry, day):
                    if drop.at + SPRINT_LENGTH >= now:
                        out.append((entry, drop))
        return sorted(out, key=lambda x: x[1].at)


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="restaurants.yaml")
    ap.add_argument("--state", default="state/seen.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--serve", action="store_true", help="run forever with release-time sprints")
    ap.add_argument("--check-every", type=float, default=15, help="minutes between background checks (--serve)")
    ap.add_argument("--poll-seconds", type=float, default=5, help="seconds between polls during a sprint")
    args = ap.parse_args()

    if not os.environ.get("NTFY_TOPIC") and not args.dry_run:
        print("NTFY_TOPIC is not set; use --dry-run to test without alerts.", file=sys.stderr)
        return 2

    watcher = Watcher(args.config, args.state, args.dry_run)
    if args.serve:
        watcher.serve(dt.timedelta(minutes=args.check_every), args.poll_seconds)
    return watcher.check_all()


if __name__ == "__main__":
    sys.exit(main())
