"""WO54 §3 on thrift-api: the owner's quiet hours, 8:30 AM – 2:00 PM every day (America/New_York). The Mac — her working
laptop — runs NOTHING then (launchd unloads the worker, the poster and both Chromes): no heartbeat, no sync, no
take-downs unless `allow_takedowns`. Azure still takes her shares and the sales emails, and must not cry wolf: no net
that judges the Mac speaks while it is quiet on purpose or starting back (the 30 minutes after), and their clocks count
only its working time. The settings ride the Mac's heartbeat (`info["quiet"]`, thrift_agent.quiet.beat()); none →
the defaults. Messages between 10 PM and 7 AM go out silently; a sale's 💰 line still goes out in the quiet hours.

Today's dates: Mon 2026-11-09 (EST, UTC−5); the DST days Sun 2026-03-08 (EDT from 2 AM) and Sun 2026-11-01 (EST from
2 AM) — a fixed offset would move the window an hour on one side of each."""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from apitools import ITEM, call, depop_sale, seed, texts

from thrift_api import core, http, quiet, timers, uploads, watch
from thrift_api.util import iso

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
MON = date(2026, 11, 9)                     # Monday, EST
TUE = date(2026, 11, 10)
MAC_QUIET = {"enabled": True, "start": "08:30", "end": "14:00", "timezone": "America/New_York", "days": "all",
             "allow_takedowns": False, "override": None}        # thrift_agent.quiet.beat(), as the Mac sends it
LATE = "⚠️ Lacoste Tee White size M sold on Depop but is still listed on Poshmark and Vinted — please mark it sold there."
OFFLINE_TAIL = " Is it plugged in, with the lid open? Her photos are safe in the cloud and will go up when it's back."


def ny(h: int, m: int = 0, day: date = MON) -> datetime:
    """A New York wall-clock time as the aware UTC instant the timers get."""
    return datetime.combine(day, time(h, m), tzinfo=NY).astimezone(UTC)


def beat(db, at: datetime, **cfg) -> None:
    """The Mac's heartbeat at `at`, carrying its quiet hours as thrift_agent.quiet.beat() says them."""
    core.heartbeat(db, "mac", {"quiet": {**MAC_QUIET, **cfg}}, at)


def q_of(**cfg) -> quiet.Quiet:
    return quiet.from_dict({**MAC_QUIET, **cfg}, "heartbeat")


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-10-01T00:00:00+00:00")


# --- the model (quiet.py): the window, DST, the days, "post now", the defaults --------------------------------------

@pytest.mark.parametrize("moment, expected", [
    (ny(8, 29), False), (ny(8, 30), True), (ny(13, 59), True), (ny(14, 0), False),        # Mon Nov 9, EST
    (ny(8, 29, date(2026, 3, 7)), False), (ny(8, 30, date(2026, 3, 7)), True),          # Sat Mar 7, EST
    (ny(13, 59, date(2026, 3, 7)), True), (ny(14, 0, date(2026, 3, 7)), False),
    (ny(8, 29, date(2026, 3, 8)), False), (ny(8, 30, date(2026, 3, 8)), True),          # Sun Mar 8, EDT from 2 AM
    (ny(13, 59, date(2026, 3, 8)), True), (ny(14, 0, date(2026, 3, 8)), False),
    (ny(8, 29, date(2026, 10, 31)), False), (ny(8, 30, date(2026, 10, 31)), True),      # Sat Oct 31, EDT
    (ny(14, 0, date(2026, 10, 31)), False),
    (ny(8, 29, date(2026, 11, 1)), False), (ny(8, 30, date(2026, 11, 1)), True),        # Sun Nov 1, EST from 2 AM
    (ny(13, 59, date(2026, 11, 1)), True), (ny(14, 0, date(2026, 11, 1)), False),
])
def test_the_window_is_new_york_wall_clock_on_both_sides_of_dst(moment, expected):
    assert quiet.is_quiet(moment, quiet.from_dict(None)) is expected


def test_dst_moves_the_window_in_utc_and_never_by_a_fixed_offset():
    q = quiet.from_dict(None)
    assert quiet.window_of(date(2026, 3, 7), q) == (datetime(2026, 3, 7, 13, 30, tzinfo=UTC),
                                                    datetime(2026, 3, 7, 19, 0, tzinfo=UTC))      # EST
    assert quiet.window_of(date(2026, 3, 8), q) == (datetime(2026, 3, 8, 12, 30, tzinfo=UTC),
                                                    datetime(2026, 3, 8, 18, 0, tzinfo=UTC))      # EDT
    assert quiet.window_of(date(2026, 11, 1), q) == (datetime(2026, 11, 1, 13, 30, tzinfo=UTC),
                                                     datetime(2026, 11, 1, 19, 0, tzinfo=UTC))    # EST
    assert quiet.is_quiet(datetime(2026, 3, 8, 13, 29, tzinfo=UTC), q)            # 9:29 EDT (−5 would say 8:29)
    assert not quiet.is_quiet(datetime(2026, 3, 8, 18, 30, tzinfo=UTC), q)        # 14:30 EDT (−5 would say 13:30)
    assert not quiet.is_quiet(datetime(2026, 11, 1, 13, 29, tzinfo=UTC), q)       # 8:29 EST (−4 would say 9:29)
    assert quiet.is_quiet(datetime(2026, 11, 1, 18, 30, tzinfo=UTC), q)           # 13:30 EST (−4 would say 14:30)
    # the working time across each change: 30 h and 32 h of clock, less the two 5½-hour windows
    assert quiet.active_seconds(ny(8, 0, date(2026, 3, 7)), ny(15, 0, date(2026, 3, 8)), q) == 19 * 3600
    assert quiet.active_seconds(ny(8, 0, date(2026, 10, 31)), ny(15, 0, date(2026, 11, 1)), q) == 21 * 3600


def test_active_time_and_the_grace_after_the_end():
    q = quiet.from_dict(None)
    assert quiet.active_seconds(ny(8, 0), ny(15, 30), q) == 2 * 3600            # 8:00–8:30, then 2:00–3:30
    assert quiet.active_seconds(ny(9, 0), ny(13, 0), q) == 0                   # all of it quiet
    assert quiet.active_seconds(ny(15, 0), ny(14, 0), q) == 0                  # backwards: nothing
    assert quiet.aged(ny(8, 0), ny(15, 30), timedelta(hours=2), q)
    assert not quiet.aged(ny(8, 0), ny(15, 29), timedelta(hours=2), q)
    assert quiet.aged(ny(8, 0) - timedelta(days=40), ny(10, 0), timedelta(hours=2), q)      # an old start: cheap, true
    nearly_all = q_of(start="00:01", end="00:00")                              # quiet 23 h 59 min a day
    assert not quiet.aged(ny(0) - timedelta(days=30), ny(0), timedelta(hours=2), nearly_all)   # 30 working minutes
    assert quiet.aged(ny(0) - timedelta(days=150), ny(0), timedelta(hours=2), nearly_all)      # the whole span counted
    assert [quiet.settling(ny(h, m), q) for h, m in ((8, 29), (8, 30), (14, 0), (14, 29), (14, 30))] == [
        False, True, True, True, False]
    assert quiet.ended_between(ny(8, 20), ny(14, 30), q) == ny(14, 0)
    assert quiet.ended_between(ny(14, 5), ny(20, 0), q) is None


def test_post_now_lifts_the_quiet_hours_for_that_date_only():
    q = q_of(override={"date": "2026-11-09", "mode": "post_now"})
    assert q.lifted_on == MON
    assert not quiet.is_quiet(ny(10), q) and not quiet.settling(ny(14, 10), q)
    assert quiet.is_quiet(ny(10, 0, TUE), q)                                   # the next day: quiet again
    assert quiet.active_seconds(ny(8), ny(16), q) == 8 * 3600                  # the Mac worked through it
    assert quiet.active_seconds(ny(8), ny(16, 0, TUE), q) == (32 - 5.5) * 3600
    assert quiet.is_quiet(ny(10), q_of(override={"date": "2026-11-09", "mode": "quiet_now"}))     # not an override
    assert quiet.is_quiet(ny(10), q_of(override={"date": "someday", "mode": "post_now"}))


def test_the_days_a_window_across_midnight_and_a_window_turned_off():
    weekdays = q_of(days=["mon", "tue", "wed", "thu", "fri"])
    assert quiet.is_quiet(ny(10), weekdays) and not quiet.is_quiet(ny(10, 0, date(2026, 11, 8)), weekdays)   # Sunday
    assert quiet.is_quiet(ny(10, 0, date(2026, 11, 8)), q_of(days="sun mon"))
    night = q_of(start="22:00", end="02:00")
    assert quiet.is_quiet(ny(23), night) and quiet.is_quiet(ny(1, 59, TUE), night)
    assert not quiet.is_quiet(ny(2, 0, TUE), night) and not quiet.is_quiet(ny(10), night)
    assert quiet.active_seconds(ny(21), ny(3, 0, TUE), night) == 2 * 3600
    off = q_of(enabled=False)
    assert not quiet.is_quiet(ny(10), off) and quiet.active_seconds(ny(8), ny(16), off) == 8 * 3600


def test_the_settings_come_from_the_macs_heartbeat_else_the_defaults(db):
    q = quiet.read(db)                                                         # no heartbeat
    assert (q.source, q.enabled, q.label(), q.allow_takedowns) == ("defaults", True, "8:30 AM – 2:00 PM", False)
    core.heartbeat(db, "mac", {"sites": {}}, ny(7))                            # a Mac that doesn't say: the defaults
    assert quiet.read(db).source == "defaults" and quiet.is_quiet(ny(10), quiet.read(db))
    beat(db, ny(7), start="09:00", end="13:00", allow_takedowns=True)
    q = quiet.read(db)
    assert (q.source, q.label(), q.allow_takedowns) == ("heartbeat", "9:00 AM – 1:00 PM", True)
    beat(db, ny(7), enabled="false")                                           # a string still says no
    assert not quiet.read(db).enabled
    # a bad value keeps its default, never an exception
    q = quiet.from_dict({"start": "nonsense", "end": None, "timezone": "Mars/Olympus", "days": 5, "enabled": "yes"})
    assert (q.label(), q.tz_name, q.days, q.enabled) == ("8:30 AM – 2:00 PM", "America/New_York", quiet.ALL_DAYS, True)
    db.execute("UPDATE heartbeats SET info = ? WHERE source = 'mac'", ('{"quiet": {"start": "09:00"',))   # cut JSON
    assert quiet.read(db).source == "defaults"
    db.execute("UPDATE heartbeats SET info = ? WHERE source = 'mac'", (json.dumps(["not", "a", "dict"]),))
    assert quiet.read(db).source == "defaults"


def test_a_big_heartbeat_keeps_its_quiet_hours_readable(db):
    """The beat was cut at 4000 characters: JSON nobody could read, and the quiet hours (with the sites, form checks
    and alarms) fell back to the defaults."""
    core.heartbeat(db, "mac", {"alarms": {"vinted:verify:i_1": ["x" * 6000]}, "quiet": {**MAC_QUIET, "start": "09:00"}},
                   ny(7))
    q = quiet.read(db)
    assert q.source == "heartbeat" and q.start == time(9, 0)


# --- the Mac-offline line (timers.mac_watch) -----------------------------------------------------------------------

def test_no_offline_line_for_the_quiet_hours_one_if_the_mac_doesnt_come_back(db, live, sent):
    beat(db, ny(8, 20))                                                        # its last beat before 8:30
    for h, m in ((8, 51), (10, 0), (13, 59), (14, 15)):                        # 31 min … 6 h of clock: 10–25 of work
        assert timers.mac_watch(db, ny(h, m), "live") == "ok", (h, m)
    assert timers.mac_watch(db, ny(14, 25), "live") == "quiet"                 # 35 working minutes: starting back
    assert sent == []
    assert timers.mac_watch(db, ny(14, 30), "live") == "offline"
    assert sent == [("ops", "The Mac didn't come back after its quiet hours (they ended at 2:00 PM; last heard from at "
                            "8:20 AM)." + OFFLINE_TAIL, False)]
    assert timers.mac_watch(db, ny(14, 45), "live") == "offline" and len(sent) == 1
    beat(db, ny(15, 2))
    assert timers.mac_watch(db, ny(15, 15), "live") == "back"
    assert texts(sent)[-1] == "Mac back at 3:02 PM."


def test_a_mac_back_after_the_quiet_hours_hears_nothing_at_all(db, live, sent):
    beat(db, ny(8, 25))
    for h, m in ((9, 0), (12, 0), (14, 0)):
        assert timers.mac_watch(db, ny(h, m), "live") == "ok"
    beat(db, ny(14, 4))
    assert timers.mac_watch(db, ny(14, 15), "live") == "ok"
    assert sent == []                                                         # no offline line, so no "back" line


def test_an_outage_said_before_the_quiet_hours_gets_its_back_line_after_them(db, live, sent):
    beat(db, ny(7, 40))
    assert timers.mac_watch(db, ny(8, 15), "live") == "offline"                # the hour before: as ever
    assert texts(sent) == ["The Mac stopped answering at 7:40 AM." + OFFLINE_TAIL]
    assert timers.mac_watch(db, ny(10, 0), "live") == "offline" and len(sent) == 1
    beat(db, ny(14, 3))
    assert timers.mac_watch(db, ny(14, 15), "live") == "back"
    assert texts(sent)[-1] == "Mac back at 2:03 PM."


@pytest.mark.parametrize("day", [date(2026, 3, 8), date(2026, 11, 1)])
def test_the_offline_line_keeps_new_york_time_on_the_dst_days(db, live, sent, day):
    beat(db, ny(8, 20, day))
    for h, m in ((9, 50), (13, 45), (14, 15)):                                 # a fixed offset errs here by an hour
        assert timers.mac_watch(db, ny(h, m, day), "live") == "ok", (day, h, m)
    assert timers.mac_watch(db, ny(14, 29, day), "live") == "quiet"
    assert sent == []
    assert timers.mac_watch(db, ny(14, 30, day), "live") == "offline"
    assert texts(sent)[0].startswith("The Mac didn't come back after its quiet hours (they ended at 2:00 PM;")


def test_post_now_makes_the_silence_real_that_day(db, live, sent):
    beat(db, ny(9), override={"date": "2026-11-09", "mode": "post_now"})      # she said "post now": the Mac works
    assert timers.mac_watch(db, ny(9, 29), "live") == "ok"
    assert timers.mac_watch(db, ny(9, 31), "live") == "offline"
    assert texts(sent) == ["The Mac stopped answering at 9:00 AM." + OFFLINE_TAIL]


def test_post_now_of_another_day_lifts_nothing(db, live, sent):
    beat(db, ny(8, 20, TUE), override={"date": "2026-11-09", "mode": "post_now"})   # Monday's, not Tuesday's
    assert timers.mac_watch(db, ny(12, 0, TUE), "live") == "ok"
    assert timers.mac_watch(db, ny(14, 25, TUE), "live") == "quiet"
    assert sent == []


# --- the 2-hour net (watch.not_live) -------------------------------------------------------------------------------

def priced(db, iid: str, at: datetime, title: str = "Moncler Down Snowsuit 12 Months") -> None:
    """An item priced at `at`, live on Depop and Vinted, still in line for Poshmark."""
    core.sync(db, {"items": [{"id": iid, "title": title, "price": 80, "status": "ready", "priced": True, "test": 0,
                              "priced_at": iso(at), "created_at": iso(at), "updated_at": iso(at)}],
                   "listings": [{"item_id": iid, "marketplace": mp, "status": "queued" if mp == "poshmark" else "posted",
                                 "url": None if mp == "poshmark" else f"https://{mp}.example/{iid}",
                                 "updated_at": iso(at)} for mp in ("poshmark", "depop", "vinted")]}, at)


def test_the_2_hour_net_counts_working_time_and_never_speaks_in_the_quiet_hours(db, live, sent):
    beat(db, ny(4, 50))
    priced(db, "i_early", ny(5, 0))
    priced(db, "i_631", ny(6, 31))
    priced(db, "i_800", ny(8, 0))
    assert watch.not_live(db, ny(7, 0), "live") == ["i_early"]                  # the hour before: as ever
    for h, m in ((10, 0), (13, 59), (14, 1), (14, 29)):                        # i_631 is 2 h old at 2:01 PM …
        assert watch.not_live(db, ny(h, m), "live") == [], (h, m)
    assert watch.not_live(db, ny(14, 30), "live") == ["i_631"]                  # … said once the Mac had its 30 min
    assert watch.not_live(db, ny(15, 29), "live") == []
    assert watch.not_live(db, ny(15, 30), "live") == ["i_800"]                  # 8:00 → 3:30 PM (8:00–8:30, 2–3:30)
    assert texts(sent)[-1] == ("🚨 Moncler Down Snowsuit 12 Months isn't live everywhere 2 h after its price — "
                               "Poshmark: waiting in line")
    assert all(silent is False for _, _, silent in sent)


@pytest.mark.parametrize("day", [date(2026, 3, 8), date(2026, 11, 1)])
def test_the_2_hour_net_keeps_new_york_time_on_the_dst_days(db, live, sent, day):
    """A fixed offset puts the window an hour off: 7:30 AM – 1:00 PM on Nov 1 (−4), 9:30 AM – 3:00 PM on Mar 8 (−5)."""
    beat(db, ny(5, 40, day))
    priced(db, "i_545", ny(5, 45, day))
    priced(db, "i_700", ny(7, 0, day))
    assert watch.not_live(db, ny(7, 45, day), "live") == ["i_545"]             # −4 would call 7:45 quiet
    for h, m in ((9, 15), (13, 30), (14, 29)):                                 # −5 would say i_700 at 9:15
        assert watch.not_live(db, ny(h, m, day), "live") == [], (day, h, m)
    assert watch.not_live(db, ny(14, 30, day), "live") == ["i_700"]            # 7:00–8:30, then 2:00–2:30


def test_the_2_hour_net_at_night_is_silent_and_post_now_makes_it_real_time(db, live, sent):
    beat(db, ny(19, 55))
    priced(db, "i_night", ny(20, 0))
    assert watch.not_live(db, ny(22, 0), "live") == ["i_night"]
    assert sent[-1][2] is True                                                # 10 PM – 7 AM: silent (WO54 §3)
    beat(db, ny(8, 0, TUE), override={"date": "2026-11-10", "mode": "post_now"})
    priced(db, "i_tue", ny(8, 0, TUE))
    assert watch.not_live(db, ny(10, 0, TUE), "live") == ["i_tue"]              # lifted: 2 h of clock
    assert sent[-1][2] is False


# --- the late take-downs (core.late_takedowns) and a sale in the quiet hours ----------------------------------------

def test_a_sale_in_the_quiet_hours_is_said_at_once_its_late_line_waits_for_working_time(db, live, sent):
    seed(db)
    beat(db, ny(8, 20))
    out = core.process_email(db, depop_sale(date=iso(ny(10))), ny(10))
    assert out["sale_status"] == "delisting"
    assert sent[0][0] == "group" and sent[0][1].startswith("💰 Sold on Depop: Lacoste Tee White size M")
    assert sent[0][2] is False                                                # the 💰 line rings in the quiet hours
    for h, m in ((12, 5), (14, 30), (15, 59)):                                 # 2 h of clock at noon: no work yet
        assert timers.every_15_min(db, ny(h, m))["late_takedowns"] == [], (h, m)
        assert core.late_takedowns(db, ny(h, m)) == []                         # the dashboard's flag agrees
    assert timers.every_15_min(db, ny(16, 0))["late_takedowns"] == [out["sale_id"]]      # 2:00–4:00 PM
    assert texts(sent, "group")[-1] == LATE
    assert [e["item_id"] for e in core.late_takedowns(db, ny(16, 0))] == [ITEM]


def test_take_downs_allowed_in_the_quiet_hours_keep_the_clock(db, live, sent):
    seed(db)
    beat(db, ny(8, 20), allow_takedowns=True)
    sale_id = core.process_email(db, depop_sale(date=iso(ny(10))), ny(10))["sale_id"]
    assert timers.every_15_min(db, ny(11, 55))["late_takedowns"] == []
    assert timers.every_15_min(db, ny(12, 5))["late_takedowns"] == [sale_id]      # the Mac takes them down then
    assert texts(sent, "group")[-1] == LATE


# --- the share and upload watchdogs, the unposted net ---------------------------------------------------------------

def share(db, sid: str, at: datetime, **fields) -> None:
    core.sync(db, {"items": [], "listings": [], "shares": [{"id": sid, "source": "azure", "expected": 4, "received": 4,
                                                            "updated_at": iso(at), **fields}]}, at)


def test_a_share_is_never_stuck_in_the_quiet_hours(db, live, sent):
    sid = "2026-11-09_081500_1111"
    share(db, sid, ny(8, 20), received_at=iso(ny(8, 15)), complete_at=iso(ny(8, 16)), state="work",
          waiting="its items to be processed (1 of 4 done)", moved_at=iso(ny(8, 20)))
    beat(db, ny(8, 25))
    assert watch.shares_watch(db, ny(8, 45), "live") == []                     # unmoved 25 min of clock, 10 of work
    beat(db, ny(14, 2))
    assert watch.shares_watch(db, ny(14, 10), "live") == []                    # starting back
    beat(db, ny(14, 25))
    assert timers.every_5_min(db, ny(14, 30))["shares"] == [sid]
    assert texts(sent) == ["⏳ Share 8:15 AM (4 photos) stuck since 8:20 AM: waiting for its items to be processed "
                           "(1 of 4 done)."]


def test_her_two_hour_wait_stands_still_in_the_quiet_hours(db, live, sent):
    sid = "2026-11-09_065500_2222"
    waiting = 'her price on "Southern Tide Pants" — 3 more cards behind it'
    share(db, sid, ny(7, 0), received_at=iso(ny(6, 55)), complete_at=iso(ny(6, 56)), state="her", waiting=waiting,
          moved_at=iso(ny(7, 0)))
    beat(db, ny(8, 50))
    assert watch.shares_watch(db, ny(9, 0), "live") == []                      # 2 h of clock: the old rule said it
    beat(db, ny(14, 20))
    assert watch.shares_watch(db, ny(14, 25), "live") == []
    beat(db, ny(14, 29))
    assert watch.shares_watch(db, ny(14, 30), "live") == [sid]                 # 7:00–8:30 + 2:00–2:30
    assert texts(sent) == [f"⏳ Waiting for {waiting}."]


def test_an_upload_in_the_quiet_hours_waits_for_the_mac(db, live, sent):
    for sid, n, start, last, fetched in (("2026-11-09_082500_4444", 5, ny(8, 25), ny(8, 26), None),
                                         ("2026-11-09_100000_3333", 20, ny(10, 0), ny(10, 1), ny(14, 5))):
        db.execute("INSERT INTO shares_in (share_id, count, received, started_at, ended_at, last_received_at, "
                   "fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (sid, n, n, iso(start), iso(last), iso(last), iso(fetched) if fetched else None))
    beat(db, ny(8, 25))
    assert watch.uploads_watch(db, ny(8, 40), "live") == []                    # 14 min of clock, 4 of work
    beat(db, ny(14, 3))
    assert watch.uploads_watch(db, ny(14, 10), "live") == []                   # starting back
    beat(db, ny(14, 25))
    assert timers.every_5_min(db, ny(14, 30))["uploads"] == ["2026-11-09_082500_4444"]   # the other one was taken
    assert texts(sent) == ["⏳ Share 8:25 AM (5 photos) is in Azure but the Mac hasn't taken it (since 8:26 AM)"]


def test_the_unposted_net_rests_in_the_quiet_hours_and_counts_working_time(db, live, sent):
    beat(db, ny(7, 55))
    core.sync(db, {"items": [{"id": "i_1", "title": "Ralph Lauren Shirt", "price": 40, "status": "ready", "priced": True,
                              "test": 0, "created_at": iso(ny(7)), "updated_at": iso(ny(7))}],
                   "listings": [{"item_id": "i_1", "marketplace": "poshmark", "status": "failed",
                                 "error": "PosterError: boom", "updated_at": iso(ny(8, 0))}]}, ny(8, 0))
    for h, m in ((9, 15), (14, 15), (14, 29)):
        assert watch.unposted(db, ny(h, m), "live") == {"asked": [], "said": []}, (h, m)
    assert timers.every_15_min(db, ny(14, 30))["unposted"] == {"asked": ["i_1|poshmark"], "said": []}   # 30 + 30 min


# --- what goes on unchanged in the quiet hours ---------------------------------------------------------------------

def test_her_shares_are_taken_at_10_am_as_ever(db, monkeypatch, live, sent):
    monkeypatch.setattr(uploads, "_STORE", uploads.MemoryStore())
    sid, ten = "2026-11-09_100000_7a7a", ny(10)
    photo = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"q" * 64
    status, _, body = http.handle("POST", f"/share/{sid}/photo", {"n": "1", "of": "1", "name": "IMG_0001.jpeg"}, photo,
                                  now=ten, db=db, headers={"Content-Type": "image/jpeg"})
    assert (status, json.loads(body)) == (200, {"ok": True, "received": 1, "name": "IMG_0001.jpeg", "stored": True})
    status, _, body = http.handle("POST", f"/share/{sid}/end", {"count": "1"}, b"", now=ten, db=db,
                                  headers={"content-type": "application/json"})
    assert (status, json.loads(body)) == (200, {"ok": True, "received": 1, "count": 1})
    status, out, _ = call(db, "GET", "/shares/pending", now=ten)
    assert status == 200 and [(s["share_id"], s["received"], s["ended"]) for s in out["shares"]] == [(sid, 1, True)]
    assert http.handle("GET", out["shares"][0]["photos"][0]["path"], {}, b"", now=ny(14, 1), db=db)[2] == photo
    status, out, _ = call(db, "POST", f"/shares/{sid}/fetched", {"names": ["IMG_0001.jpeg"]}, now=ny(14, 1))
    assert status == 200 and out["ok"] is True
    assert sent == []                                                         # nothing said about it


def test_the_quiet_state_in_health_and_the_15_minute_timer(db, live):
    assert call(db, "GET", "/health", now=ny(10))[1]["quiet"] == {
        "enabled": True, "hours": "8:30 AM – 2:00 PM", "timezone": "America/New_York", "days": "all",
        "allow_takedowns": False, "lifted_on": None, "source": "defaults", "now": "quiet"}
    beat(db, ny(8, 20))
    assert call(db, "GET", "/health", now=ny(14, 10))[1]["quiet"]["now"] == "starting back"
    assert call(db, "GET", "/health", now=ny(15))[1]["quiet"]["source"] == "heartbeat"
    assert [timers.every_15_min(db, ny(h))["quiet"] for h in (10, 15)] == ["quiet", "working"]


def test_the_evening_line_never_calls_the_quiet_hours_offline(db, live, sent):
    beat(db, ny(19, 55), start="20:00", end="23:00")                           # an owner's window over 9 PM
    line = watch.evening_line(db, ny(21, 5), "live")
    assert line.startswith("📊 Today (the Mac is in its quiet hours — its figures as of 7:55 PM): "), line
    core.drop_setting(db, watch.EVENING_DONE)
    beat(db, ny(18, 25), start="18:30", end="20:45")                           # … one that ended at 8:45 PM
    line = watch.evening_line(db, ny(21, 5), "live")
    assert line.startswith("📊 Today (the Mac is starting back after its quiet hours — its figures as of 6:25 PM): ")
    core.drop_setting(db, watch.EVENING_DONE)
    beat(db, ny(8, 20))                                                        # the default window; never back
    line = watch.evening_line(db, ny(21, 5), "live")
    assert line.startswith("📊 Today (the Mac is offline since 8:20 AM — its figures as of then): "), line
    core.drop_setting(db, watch.EVENING_DONE)
    beat(db, ny(20, 55))
    assert watch.evening_line(db, ny(21, 5), "live").startswith("📊 Today: ")
