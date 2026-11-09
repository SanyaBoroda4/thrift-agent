"""WO51 L: the end of daylight saving time (Sunday 2026-11-01, 2:00 AM EDT → 1:00 AM EST). Every schedule on the Mac
reads the clock in America/New_York (zoneinfo), never a fixed −4 hours: the same UTC moment is 08:30 on Oct 31 (EDT,
UTC−4) and 07:30 on Nov 1 (EST, UTC−5). A fixed offset would open the posting hours, the order sweep and the
approval card's "goes up at 8:00" an hour early from Nov 1 on."""
from datetime import UTC, datetime, timedelta

import pytest

from thrift_agent import approve, orders
from thrift_agent.config import Settings
from thrift_agent.db import DB
from thrift_agent.scheduler import can_post, windows


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def _mac(tmp_path):
    """The Mac's own schedule (config/settings.local.yaml: hours 08:00–23:00, America/New_York)."""
    return Settings({"schedule": {"timezone": "America/New_York", "hours": ["08:00", "23:00"], "per_hour_max": 60,
                                  "daily_cap": 25, "gap_seconds": [0, 0]},
                     "paths": {"control": str(tmp_path)}})


def test_posting_runs_at_every_hour_across_nov_1(tmp_path):
    """WO54 §3: no posting hours any more (the Mac's 08:00–23:00 is ignored) — the owner's quiet hours, DST-safe in
    thrift_agent/quiet.py (tests/test_quiet.py, tests/test_wo54_quiet.py), are the only hours the agent keeps off."""
    s = _mac(tmp_path)
    for t in (at(2026, 10, 31, 12, 30), at(2026, 11, 1, 12, 30), at(2026, 11, 1, 13, 0), at(2026, 11, 2, 3, 30),
              at(2026, 11, 2, 4, 1), at(2026, 10, 31, 3, 1), at(2026, 11, 1, 5, 30), at(2026, 11, 1, 6, 30)):
        assert can_post(s, 0, 0, t) == (True, "ok"), t                        # 01:30 EDT and 01:30 EST too


def test_the_day_counts_from_new_york_midnight_on_the_day_of_the_change():
    hour_ago, midnight = windows(at(2026, 11, 1, 19, 0), "America/New_York")    # 14:00 EST
    assert midnight == "2026-11-01T04:00:00+00:00"                              # Nov 1 began at 00:00 EDT
    assert hour_ago == "2026-11-01T18:00:00+00:00"
    assert windows(at(2026, 11, 2, 17, 0), "America/New_York")[1] == "2026-11-02T05:00:00+00:00"   # 00:00 EST
    assert windows(at(2026, 10, 31, 17, 0), "America/New_York")[1] == "2026-10-31T04:00:00+00:00"


@pytest.mark.quiet_on
def test_the_approval_says_when_it_goes_up_in_new_york_time(tmp_path):
    """WO54 §3: at once at every hour, except from 8:20 to the end of the quiet hours — 8:20 New York time on both
    sides of the change (EDT on Oct 31, EST on Nov 2)."""
    s = _mac(tmp_path)
    assert approve.when_posting(s, at(2026, 11, 1, 12, 30)) == "in the next few minutes"   # 07:30 EST
    assert approve.when_posting(s, at(2026, 10, 31, 12, 19)) == "in the next few minutes"  # 08:19 EDT
    assert approve.when_posting(s, at(2026, 10, 31, 12, 20)) == "at 2:00 PM"               # 08:20 EDT (−5 would be 07:20)
    assert approve.when_posting(s, at(2026, 11, 2, 13, 19)) == "in the next few minutes"   # 08:19 EST (−4 would be 09:19)
    assert approve.when_posting(s, at(2026, 11, 2, 13, 20)) == "at 2:00 PM"                # 08:20 EST
    assert approve.when_posting(s, at(2026, 11, 2, 19, 0)) == "in the next few minutes"    # 14:00 EST


def test_the_days_first_order_sweep_waits_for_8_am_new_york(tmp_path, monkeypatch):
    monkeypatch.setattr(orders.power, "lid_closed", lambda: False)
    db = DB(tmp_path / "s.db")
    orders._put(db, orders.KV_SWEPT, "vinted", at(2026, 11, 1, 8, 0).isoformat())          # 03:00 EST
    assert orders.due(db, "vinted", at(2026, 11, 1, 12, 5)) is False                       # 07:05 EST (−4: 08:05)
    assert orders.due(db, "vinted", at(2026, 11, 1, 13, 5)) is True                        # 08:05 EST: the day's first
    orders._put(db, orders.KV_SWEPT, "vinted", at(2026, 10, 31, 7, 0).isoformat())         # 03:00 EDT Oct 31
    assert orders.due(db, "vinted", at(2026, 10, 31, 11, 55)) is False                     # 07:55 EDT
    assert orders.due(db, "vinted", at(2026, 10, 31, 12, 5)) is True                       # 08:05 EDT
    orders._put(db, orders.KV_SWEPT, "vinted", at(2026, 11, 1, 13, 5).isoformat())
    assert orders.due(db, "vinted", at(2026, 11, 1, 13, 5) + timedelta(hours=5, minutes=59)) is False
    assert orders.due(db, "vinted", at(2026, 11, 1, 13, 5) + timedelta(hours=6)) is True   # every 6 hours, in UTC
