import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import watch  # noqa: E402

RESY_FIND = {
    "results": {
        "venues": [
            {
                "slots": [
                    {"date": {"start": "2026-10-29 17:15:00"}, "config": {"type": "Bar"}},
                    {"date": {"start": "2026-10-29 19:30:00"}, "config": {"type": "Dining Room"}},
                ]
            }
        ]
    }
}

OPENTABLE = {
    "data": {
        "availability": [
            {
                "availabilityDays": [
                    {
                        "dayOffset": 0,
                        "slots": [
                            {"isAvailable": True, "timeOffsetMinutes": -30},
                            {"isAvailable": False, "timeOffsetMinutes": 0},
                            {"isAvailable": True, "timeOffsetMinutes": 45},
                        ],
                    }
                ]
            }
        ]
    }
}


def test_parse_resy():
    slots = watch.parse_resy_find(RESY_FIND)
    assert slots == [
        (dt.datetime(2026, 10, 29, 17, 15), "Bar"),
        (dt.datetime(2026, 10, 29, 19, 30), "Dining Room"),
    ]


def test_parse_opentable_offsets():
    req = dt.datetime(2026, 11, 7, 20, 0)
    assert watch.parse_opentable_availability(OPENTABLE, req) == [
        dt.datetime(2026, 11, 7, 19, 30),
        dt.datetime(2026, 11, 7, 20, 45),
    ]


def test_window_and_dates():
    w = watch.parse_window("18:00-21:30")
    assert watch.in_window(dt.datetime(2026, 10, 29, 19, 30), w)
    assert not watch.in_window(dt.datetime(2026, 10, 29, 17, 15), w)
    today = dt.date(2026, 10, 19)  # Monday
    days = watch.dates_for({"next_days": 7, "weekdays": ["fri", "sat"]}, today)
    assert days == [dt.date(2026, 10, 23), dt.date(2026, 10, 24)]
    assert watch.dates_for({"dates": ["2026-10-01", "2026-11-07"]}, today) == [dt.date(2026, 11, 7)]


def test_seen_realerts_after_slot_disappears():
    s = watch.Slot("Lilia", "Resy", dt.datetime(2026, 10, 29, 19, 30), 2, "u")
    seen = watch.next_seen({}, [s], set(), "t1")
    assert s.key in seen
    seen = watch.next_seen(seen, [], set(), "t2")  # slot got booked
    assert s.key not in seen  # so it alerts again if it reopens
    seen = watch.next_seen({s.key: "t1"}, [], {"Lilia"}, "t2")  # check failed: keep memory
    assert s.key in seen
