"""WO51 L: the end of daylight saving time (Sunday 2026-11-01, 2:00 AM EDT → 1:00 AM EST) in thrift-api. Its timers run
on UTC (function_app.py: every 5 / 15 minutes and hourly) and every local-hour decision reads America/New_York through
deadlines.local (zoneinfo): the quiet hours, the 9 PM line, the 3 AM cleanup, the Mac's silence, the unposted net, the
ship-by dates and the reminders. A fixed −4 hours would move each of them an hour from Nov 1 on."""
from datetime import UTC, date, datetime

import pytest
from thrift_api import core, deadlines, timers, watch


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-10-01T00:00:00+00:00")


def test_local_time_is_new_york_on_both_sides_of_the_change():
    assert deadlines.local(at(2026, 10, 31, 12, 30)).strftime("%H:%M %Z") == "08:30 EDT"
    assert deadlines.local(at(2026, 11, 1, 12, 30)).strftime("%H:%M %Z") == "07:30 EST"
    assert deadlines.local(at(2026, 11, 1, 5, 30)).strftime("%H:%M %Z") == "01:30 EDT"   # the 1 AM hour, twice
    assert deadlines.local(at(2026, 11, 1, 6, 30)).strftime("%H:%M %Z") == "01:30 EST"


def test_the_quiet_hours_across_nov_1():
    assert deadlines.quiet(at(2026, 11, 1, 3, 30))                       # 23:30 EDT Oct 31
    assert deadlines.quiet(at(2026, 11, 1, 12, 30))                      # 07:30 EST: still quiet (−4 would say 08:30)
    assert not deadlines.quiet(at(2026, 11, 1, 13, 0))                   # 08:00 EST
    assert not deadlines.quiet(at(2026, 10, 31, 12, 30))                 # 08:30 EDT
    assert not deadlines.quiet(at(2026, 11, 2, 3, 59))                   # 22:59 EST Nov 1
    assert deadlines.quiet(at(2026, 11, 2, 4, 0))                        # 23:00 EST Nov 1


def test_the_9_pm_line_goes_once_each_evening_across_nov_1(db, live, sent):
    assert timers.evening(db, at(2026, 11, 1, 0, 59))["line"] is None             # 20:59 EDT Oct 31
    line = timers.evening(db, at(2026, 11, 1, 1, 0))["line"]                      # 21:00 EDT Oct 31
    assert line and line.startswith("📊 Today: 0 shares")
    assert core.setting(db, watch.EVENING_DONE) == "2026-10-31"
    assert timers.evening(db, at(2026, 11, 1, 1, 30))["line"] is None             # once that day
    assert timers.evening(db, at(2026, 11, 2, 1, 0))["line"] is None              # 20:00 EST Nov 1 (−4 would be 21:00)
    line = timers.evening(db, at(2026, 11, 2, 2, 0))["line"]                      # 21:00 EST Nov 1
    assert line and core.setting(db, watch.EVENING_DONE) == "2026-11-01"
    assert timers.evening(db, at(2026, 11, 2, 2, 45))["line"] is None
    assert [m[2] for m in sent] == [False, False]                                  # 9 PM: never the quiet hours


def test_the_cleanup_runs_once_in_the_3_am_hour_new_york(db):
    assert timers.daily(db, at(2026, 10, 31, 7, 10))["ran"] is True              # 03:10 EDT Oct 31
    assert timers.daily(db, at(2026, 11, 1, 7, 10))["ran"] is False              # 02:10 EST Nov 1 (−4 would be 03:10)
    assert timers.daily(db, at(2026, 11, 1, 8, 10))["ran"] is True               # 03:10 EST Nov 1
    assert timers.daily(db, at(2026, 11, 1, 8, 50))["ran"] is False              # once a day


def test_the_macs_silence_is_told_in_new_york_daytime_only(db, live, sent):
    db.execute("INSERT INTO heartbeats (source, last_seen, info) VALUES ('mac', ?, NULL)",
               (at(2026, 11, 1, 9, 0).isoformat(),))                              # 04:00 EST, then silent
    assert timers.mac_watch(db, at(2026, 11, 1, 12, 30), "live") == "night"     # 07:30 EST (−4 would tell at 08:30)
    assert sent == []
    assert timers.mac_watch(db, at(2026, 11, 1, 13, 5), "live") == "offline"    # 08:05 EST: told once
    assert len(sent) == 1 and "offline since" in sent[0][1]


def test_the_unposted_net_works_in_new_york_posting_hours(db, live, monkeypatch):
    looked = []
    monkeypatch.setattr(watch, "_unposted_rows", lambda db, now: looked.append(now) or [])
    watch.unposted(db, at(2026, 11, 1, 12, 30), "live")                          # 07:30 EST: before 08:00
    assert looked == []
    watch.unposted(db, at(2026, 11, 1, 13, 0), "live")                           # 08:00 EST
    watch.unposted(db, at(2026, 11, 2, 2, 59), "live")                           # 21:59 EST
    watch.unposted(db, at(2026, 11, 2, 3, 0), "live")                            # 22:00 EST: the net rests
    assert looked == [at(2026, 11, 1, 13, 0), at(2026, 11, 2, 2, 59)]


def test_ship_by_and_reminders_count_new_york_days_across_nov_1():
    assert deadlines.ship_by("poshmark", at(2026, 11, 1, 3, 30)) == (date(2026, 11, 7), "rule")  # Oct 31 23:30 EDT
    assert deadlines.ship_by("poshmark", at(2026, 11, 2, 4, 30)) == (date(2026, 11, 8), "rule")  # Nov 1 23:30 EST
    sale = [{"id": "s1", "status": "open", "ship_by": "2026-11-02"}]
    assert deadlines.reminders_due(at(2026, 11, 1, 13, 30), sale, set()) == []                 # 08:30 EST: before 09:00
    assert deadlines.reminders_due(at(2026, 11, 1, 14, 0), sale, set()) == [("s1", "day_before")]   # 09:00 EST
