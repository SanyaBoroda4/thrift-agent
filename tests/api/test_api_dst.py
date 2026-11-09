"""WO51 L: the end of daylight saving time (Sunday 2026-11-01, 2:00 AM EDT → 1:00 AM EST) in thrift-api. Its timers run
on UTC (function_app.py: every 5 / 15 minutes and hourly) and every local-hour decision reads America/New_York through
deadlines.local (zoneinfo): the night hours (silent sends), the 9 PM line, the 3 AM cleanup, the Mac's silence, the
unposted net (WO54 §3: it rests in the owner's quiet hours, quiet.py — zoneinfo too), the ship-by dates and the
reminders. A fixed −4 hours would move each of them an hour from Nov 1 on."""
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


def test_the_night_hours_across_nov_1():
    """WO54 §3: messages between 10 PM and 7 AM go out silently (23:00–08:00 before)."""
    assert deadlines.night(at(2026, 11, 1, 2, 30))                       # 22:30 EDT Oct 31
    assert deadlines.night(at(2026, 11, 1, 11, 30))                      # 06:30 EST: still night (−4 would say 07:30)
    assert not deadlines.night(at(2026, 11, 1, 12, 0))                   # 07:00 EST
    assert not deadlines.night(at(2026, 10, 31, 11, 30))                 # 07:30 EDT
    assert not deadlines.night(at(2026, 11, 2, 2, 59))                   # 21:59 EST Nov 1
    assert deadlines.night(at(2026, 11, 2, 3, 0))                        # 22:00 EST Nov 1


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
    assert len(sent) == 1 and "stopped answering at 4:00 AM" in sent[0][1]


def test_the_unposted_net_rests_in_the_new_york_quiet_hours(db, live, monkeypatch):
    """WO54 §3: the Mac posts at every hour but the quiet hours (8:30 AM – 2:00 PM New York), so the net rests only in
    them and the 30 minutes after (the Mac starting back) — no longer 22:00–08:00."""
    looked = []
    monkeypatch.setattr(watch, "_unposted_rows", lambda db, now: looked.append(now) or [])
    works = [at(2026, 11, 1, 12, 30),                                            # 07:30 EST (−4 would say 08:30)
             at(2026, 11, 1, 13, 29),                                            # 08:29 EST
             at(2026, 11, 1, 19, 30),                                            # 14:30 EST: the grace is over
             at(2026, 11, 2, 3, 0),                                              # 22:00 EST: at night too, now
             at(2026, 10, 31, 18, 30)]                                           # 14:30 EDT Oct 31
    rests = [at(2026, 11, 1, 13, 30),                                            # 08:30 EST
             at(2026, 11, 1, 18, 59),                                            # 13:59 EST
             at(2026, 11, 1, 19, 29),                                            # 14:29 EST: starting back
             at(2026, 10, 31, 12, 30),                                           # 08:30 EDT Oct 31
             at(2026, 10, 31, 18, 29)]                                           # 14:29 EDT Oct 31
    for when in works + rests:
        watch.unposted(db, when, "live")
    assert looked == works


def test_ship_by_and_reminders_count_new_york_days_across_nov_1():
    assert deadlines.ship_by("poshmark", at(2026, 11, 1, 3, 30)) == (date(2026, 11, 7), "rule")  # Oct 31 23:30 EDT
    assert deadlines.ship_by("poshmark", at(2026, 11, 2, 4, 30)) == (date(2026, 11, 8), "rule")  # Nov 1 23:30 EST
    sale = [{"id": "s1", "status": "open", "ship_by": "2026-11-02"}]
    assert deadlines.reminders_due(at(2026, 11, 1, 13, 30), sale, set()) == []                 # 08:30 EST: before 09:00
    assert deadlines.reminders_due(at(2026, 11, 1, 14, 0), sale, set()) == [("s1", "day_before")]   # 09:00 EST
