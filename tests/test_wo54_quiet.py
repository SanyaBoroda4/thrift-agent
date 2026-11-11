"""WO54 §3 — the owner's quiet hours on the Mac (8:30 AM – 2:00 PM America/New_York, every day; the owner, 2026-11-09:
"the Mac is my working laptop"). The calendar job decides by the current time (8:19 / 8:20 / 8:30 / 13:59 / 2:00 PM,
DST days included); from 8:20 the worker and the poster start nothing new, inside the window nothing at all and they
ask for their own stop; the verifying publishes, the 20-minute alarm and job health count only active time; the Mac
sends nothing in the quiet hours and nothing makes a sound at night; "post now" / "quiet now" in the private chat (and
the peek that finds a "post now" while the worker is stopped, never moving the bot's offset, never sending); one
heartbeat after each; no posting hours anywhere; the scripts. Fakes only: no network, no launchd, no browser."""
from __future__ import annotations

import asyncio
import plistlib
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import yaml

from thrift_agent import alarm, approve, cli, config, daily, health, jobs, notify, postcheck, quiet, quietctl, telegram
from thrift_agent.db import DB
from thrift_agent.post import parallel, runner
from thrift_agent.post.base import Outcome
from thrift_agent.scheduler import can_post

import test_parallel as tp
from test_crosslist_flow import _item, _settings
from test_parallel import quick  # noqa: F401 — the fixture
from test_wo53 import _strings

ROOT = Path(__file__).resolve().parents[1]
NY = ZoneInfo("America/New_York")


def at(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=NY).astimezone(timezone.utc)


class Clock:
    """quiet.py's own clock (its datetime.now) fixed — what the quiet hours read; nothing else is touched."""

    def __init__(self, monkeypatch):
        self.t = at(2026, 11, 10, 10, 0)
        me = self

        class Fixed(datetime):
            @classmethod
            def now(cls, tz=None):
                return me.t.astimezone(tz) if tz is not None else me.t.astimezone(NY).replace(tzinfo=None)
        monkeypatch.setattr(quiet, "datetime", Fixed)

    def set(self, *args) -> datetime:
        self.t = at(*args)
        return self.t


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "state.db")


def response(url="https://api.telegram.org/x", body=None):
    return httpx.Response(200, json=body or {"ok": True, "result": {"message_id": 1}}, request=httpx.Request("POST", url))


# ---------------------------------------------------------------- 1. the calendar job's decision, by the current time

@pytest.mark.quiet_on
@pytest.mark.parametrize("hm,stopped,want", [
    ((8, 19), False, "none"), ((8, 20), False, "none"),          # from 8:20 the processes wind down by themselves
    ((8, 29, 59), False, "none"), ((8, 30), False, "stop"), ((13, 59), False, "stop"),
    ((13, 59, 59), True, "stop"), ((14, 0), False, "none"), ((14, 0), True, "start"),
    ((8, 20), True, "none"), ((3, 0), True, "start"), ((23, 50), False, "none"),
])
def test_the_calendar_jobs_decision_by_the_current_time(hm, stopped, want):
    assert quietctl.decide(at(2026, 11, 10, *hm), None, config.settings(), stopped=stopped) == want


@pytest.mark.quiet_on
@pytest.mark.parametrize("day", [(2026, 3, 8), (2026, 3, 9), (2026, 11, 1), (2026, 11, 2)])
def test_the_decision_keeps_8_30_and_2_pm_across_dst(day):
    s = config.settings()
    assert quietctl.decide(at(*day, 8, 29), None, s) == "none"
    assert quietctl.decide(at(*day, 8, 30), None, s) == "stop"
    assert quietctl.decide(at(*day, 13, 59), None, s, stopped=True) == "stop"
    assert quietctl.decide(at(*day, 14, 0), None, s, stopped=True) == "start"


@pytest.mark.quiet_on
def test_takedowns_allowed_keep_the_poster_and_post_now_or_off_start_it(db, settings_override):
    s = config.settings()
    ten = at(2026, 11, 10, 10, 0)
    quiet.set_post_now(db, ten)
    assert quietctl.decide(ten, db, s, stopped=True) == "start"            # her "post now": everything back today
    assert quietctl.decide(ten, db, s) == "none"
    assert quietctl.decide(at(2026, 11, 11, 10, 0), db, s) == "stop"         # tomorrow: quiet again
    settings_override(quiet={"allow_takedowns": True})
    assert quietctl.decide(at(2026, 11, 11, 10, 0), db, config.settings()) == "stop-takedowns"
    settings_override(quiet={"enabled": False})
    assert quietctl.decide(at(2026, 11, 11, 10, 0), db, config.settings(), stopped=True) == "start"


@pytest.mark.quiet_on
def test_the_status_lines(db):
    s = config.settings()
    assert quietctl.status_line(at(2026, 11, 10, 7, 0), db, s) == \
        "Quiet hours 8:30 AM – 2:00 PM: next stop 8:30 AM, back at 2:00 PM"
    assert quietctl.status_line(at(2026, 11, 10, 8, 25), db, s) == \
        "Quiet hours 8:30 AM – 2:00 PM: nothing new starts now — quiet at 8:30 AM, back at 2:00 PM"
    assert quietctl.status_line(at(2026, 11, 10, 10, 0), db, s) == "Quiet hours 8:30 AM – 2:00 PM: quiet now — back at 2:00 PM"
    assert quietctl.status_line(at(2026, 11, 10, 15, 0), db, s, short=True) == \
        "on, 8:30 AM – 2:00 PM — next stop tomorrow 8:30 AM, start 2:00 PM"
    assert quietctl.status_line(at(2026, 11, 13, 15, 0), db, s) == \
        "Quiet hours 8:30 AM – 2:00 PM: next stop tomorrow 8:30 AM, back at 2:00 PM"
    quiet.set_post_now(db, at(2026, 11, 10, 10, 0))
    assert quietctl.status_line(at(2026, 11, 10, 10, 5), db, s) == \
        "Quiet hours 8:30 AM – 2:00 PM: lifted for today ('post now') — next stop tomorrow 8:30 AM, back at 2:00 PM"


# ---------------------------------------------------------------- 2. the processes' own gates

@pytest.mark.quiet_on
@pytest.mark.parametrize("hm,want", [
    ((8, 19), ["tick", "pump", "sales", "sleep"]),
    ((8, 20), ["sales", "sleep"]),                    # nothing new: no inbox take, no model call, no card from this thread
    ((8, 29), ["sales", "sleep"]),
    ((8, 30), ["stop asked", "sleep"]),               # nothing at all; the calendar job didn't come: its stop is asked
    ((13, 59), ["stop asked", "sleep"]),
    ((14, 0), ["tick", "pump", "sales", "sleep"]),
])
def test_the_worker_starts_nothing_new_from_8_20_and_nothing_at_all_from_8_30(db, monkeypatch, clock, hm, want):
    seen = []
    monkeypatch.setattr(cli, "_safe_tick", lambda s_, db_: seen.append("tick"))
    monkeypatch.setattr(cli, "_safe_pump", lambda s_, db_: seen.append("pump"))
    monkeypatch.setattr(cli, "_safe_sales", lambda db_, force_beat=False: seen.append("sales"))
    monkeypatch.setattr(cli.time, "sleep", lambda n: seen.append("sleep"))
    monkeypatch.setattr(quietctl, "ask_stop", lambda s_, db_, who, **k: seen.append("stop asked") or True)
    clock.set(2026, 11, 10, *hm)
    cli._worker_iteration(config.settings(), db, 15)
    assert seen == want


@pytest.mark.quiet_on
def test_the_telegram_thread_reads_nothing_in_the_quiet_hours(db, monkeypatch, clock):
    """Her answers stay on Telegram (kept 24 h) and are taken — and answered — at 2:00 PM; until 8:30 they are taken."""
    seen = []
    monkeypatch.setattr(cli, "_safe_poll", lambda s_, db_, bot, t: seen.append("poll") or 0)
    monkeypatch.setattr(cli, "_safe_pump", lambda s_, db_: seen.append("pump"))
    monkeypatch.setattr(cli.time, "sleep", lambda n: seen.append(("nap", n)))
    monkeypatch.setattr(cli.alerts, "scan_stuck", lambda db_: None)
    monkeypatch.setattr(cli.alerts, "watcher_stalled", lambda db_: None)
    state = {"last_resend": cli.time.monotonic()}
    clock.set(2026, 11, 10, 10, 0)
    cli._telegram_iteration(config.settings(), db, object(), state)
    assert seen == [("nap", cli.QUIET_NAP)]
    clock.set(2026, 11, 10, 8, 25)
    cli._telegram_iteration(config.settings(), db, object(), state)
    assert seen[1:] == ["poll", "pump"]


@pytest.mark.quiet_on
def test_the_posters_gate(tmp_path, db, monkeypatch, clock, settings_override):
    asked = []
    monkeypatch.setattr(quietctl, "ask_stop", lambda s_, db_, who, **k: asked.append(who) or True)
    s = settings_override(paths={"control": str(tmp_path / "control")})
    kinds = ("new", "takedown", "any")
    clock.set(2026, 11, 10, 8, 19)
    assert [parallel.quiet_hold(s, db, k) for k in kinds] == [False, False, False]
    clock.set(2026, 11, 10, 8, 25)                     # nothing new; the housekeeping and the browser still there
    assert [parallel.quiet_hold(s, db, k) for k in kinds] == [True, True, False] and asked == []
    assert not parallel.may_take_down(s, db, "vinted")
    clock.set(2026, 11, 10, 10, 0)                     # nothing at all — and the stop asked
    assert [parallel.quiet_hold(s, db, k) for k in kinds] == [True, True, True] and asked == ["poster"] * 3
    settings_override(quiet={"allow_takedowns": True})  # the owner's switch: the poster stays, for take-downs only
    asked.clear()
    assert [parallel.quiet_hold(config.settings(), db, k) for k in kinds] == [True, False, False] and asked == []
    quiet.set_post_now(db)
    assert [parallel.quiet_hold(config.settings(), db, k) for k in kinds] == [False, False, False]


@pytest.mark.quiet_on
def test_from_8_20_no_listing_starts_and_from_2_pm_everything_goes_up(tmp_path, quick, clock):  # noqa: F811
    s = _settings(tmp_path, parallel=True)
    db = DB(s.path("db"))
    items = [_item(db, n) for n in (1, 2)]
    ps = {"poshmark": tp.Timed("poshmark", 0.0), "depop": tp.Timed("depop", 0.0), "vinted": tp.Timed("vinted", 0.0)}
    clock.set(2026, 11, 10, 8, 25)
    asyncio.run(asyncio.wait_for(parallel.run_parallel(s, db, ps, dry=False, stage="form", opener=tp.fake_open,
                                                       until_idle=True), 60))
    assert not any(p.calls for p in ps.values())
    assert all(db.listing(i, "poshmark") is None or db.listing(i, "poshmark")["status"] == "queued" for i in items)
    clock.set(2026, 11, 10, 10, 0)                     # inside: still nothing, Poshmark's browser never opened
    opened = []

    async def opener(profile, tz):
        opened.append(profile)
        return await tp.fake_open(profile, tz)
    asyncio.run(asyncio.wait_for(parallel.run_parallel(s, db, ps, dry=False, stage="form", opener=opener,
                                                       until_idle=True), 60))
    assert not any(p.calls for p in ps.values()) and opened == []
    clock.set(2026, 11, 10, 14, 0)                     # 2:00 PM: the item cut off by the quiet hours goes up
    asyncio.run(asyncio.wait_for(parallel.run_parallel(s, db, ps, dry=False, stage="form", opener=tp.fake_open,
                                                       until_idle=True), 60))
    assert all(db.listing(i, mp)["status"] == "posted" for i in items for mp in ps)


@pytest.mark.parametrize("out,want", [
    (Outcome("failed", error="TargetClosedError: Target page, context or browser has been closed"), "queued"),
    (Outcome("failed", clicked=True, error="PosterError: submitted — verifying"), "verifying"),
])
def test_the_8_30_stop_lets_the_listing_in_hand_finish(tmp_path, quick, monkeypatch, out, want):  # noqa: F811
    """`services.sh stop poster` (launchd's SIGTERM): "each worker finishes the listing in hand" — one not past its
    final click goes back in line (the poster's Chrome quit under it: never the item's fault, the attempt given back);
    one past it is "verifying" (the site's own data settles it from 2:00 PM, its clock paused meanwhile)."""
    holder = {}

    def install(ev):
        holder["ev"], holder["loop"] = ev, asyncio.get_running_loop()
    monkeypatch.setattr(runner, "_install_stop", install)

    class StoppedMidListing(tp.Timed):
        async def post(self, ctx, r, mode, dry_run, shots, stage="form"):
            self.calls.append((r.sku, 0))
            holder["loop"].call_soon_threadsafe(holder["ev"].set)     # SIGTERM arrives during the listing …
            await asyncio.sleep(0.8)                                    # … the listing in hand goes on (a real one
            return out                                                  # takes a minute; the halt is set by then)
    s = _settings(tmp_path, parallel=True)
    db = DB(s.path("db"))
    iid = _item(db, 1)
    ps = {"poshmark": StoppedMidListing("poshmark", 0.0), "depop": tp.Timed("depop", 0.0),
          "vinted": tp.Timed("vinted", 0.0)}
    asyncio.run(asyncio.wait_for(parallel.run_parallel(s, db, ps, dry=False, stage="form", opener=tp.fake_open), 60))
    row = db.listing(iid, "poshmark")
    assert len(ps["poshmark"].calls) == 1
    if want == "queued":
        assert row["status"] == "queued" and (row["attempts"] or 0) == 0, dict(row)
    else:
        assert row["status"] == "failed" and (row["error"] or "").startswith(runner.UNCONFIRMED), dict(row)
        from thrift_agent import crosslist
        assert (crosslist.unconf_state(db, iid, "poshmark") or {}).get("stage") == "wait"


def test_a_process_inside_the_quiet_hours_asks_for_its_own_stop_at_most_every_5_minutes(db):
    s, ran = config.settings(), []
    t = at(2026, 11, 10, 10, 0)
    run = lambda: ran.append(1) or "kickstart"   # noqa: E731
    assert quietctl.ask_stop(s, db, "worker", now=t, run=run)
    assert not quietctl.ask_stop(s, db, "poster", now=t + timedelta(seconds=60), run=run)
    assert quietctl.ask_stop(s, db, "poster", now=t + timedelta(seconds=301), run=run)
    assert ran == [1, 1]
    assert not quietctl.ask_stop(s, db, "worker")                          # not the Mac (here): never


# ---------------------------------------------------------------- 3. the clocks count active time

@pytest.mark.quiet_on
def test_the_verifying_clock_pauses_in_the_quiet_hours():
    clicked = at(2026, 11, 10, 8, 15)
    assert postcheck.next_due(clicked, clicked) == clicked + timedelta(seconds=10)
    assert postcheck.next_due(clicked, at(2026, 11, 10, 8, 25)) == at(2026, 11, 10, 14, 5)   # never inside the window
    assert postcheck.next_due(clicked, at(2026, 11, 10, 14, 5)) == at(2026, 11, 10, 14, 15)
    assert not postcheck.overdue(clicked, at(2026, 11, 10, 15, 44, 59))     # 15 + 104 min of active time
    assert postcheck.overdue(clicked, at(2026, 11, 10, 15, 45))             # the 2 hours, without the quiet ones

    def r(h, m, result="absent"):
        return {"at": at(2026, 11, 10, h, m).isoformat(), "result": result}
    assert not postcheck.absent_proven([r(8, 28), r(14, 5)], clicked)      # 20 active minutes after the click
    assert not postcheck.absent_proven([r(8, 28), r(14, 9)], clicked)      # 11 apart, but the last only at 24
    assert not postcheck.absent_proven([r(14, 5), r(14, 12)], clicked)     # 7 apart
    assert postcheck.absent_proven([r(14, 5), r(14, 16)], clicked)         # 11 apart, the last 31 active minutes on


@pytest.mark.quiet_on
def test_active_time_moves_a_moment_out_of_the_window():
    assert quiet.advance(at(2026, 11, 10, 10, 0), 60) == at(2026, 11, 10, 14, 1)
    assert quiet.advance(at(2026, 11, 10, 8, 29), 60) == at(2026, 11, 10, 14, 0)       # lands on the start: its end
    assert quiet.advance(at(2026, 11, 10, 8, 0), 24 * 3600) == at(2026, 11, 11, 19, 0)   # 0.5 + 18.5 + 5 hours
    for since, secs in ((at(2026, 11, 10, 7, 0), 7200.0), (at(2026, 3, 7, 20, 0), 50000.0)):
        assert quiet.active_seconds(since, quiet.advance(since, secs)) == secs


def test_without_the_quiet_hours_the_clocks_are_wall_clocks():
    clicked = at(2026, 11, 10, 8, 15)
    assert postcheck.next_due(clicked, at(2026, 11, 10, 8, 25)) == at(2026, 11, 10, 8, 35)
    assert postcheck.overdue(clicked, at(2026, 11, 10, 10, 15))


@pytest.mark.quiet_on
def test_job_health_counts_only_active_time(db, monkeypatch):
    said = []
    monkeypatch.setattr(notify, "say", said.append)
    jobs.ok(db, "order_sweep:poshmark", at=at(2026, 11, 10, 8, 0))
    assert jobs.check(db, at=at(2026, 11, 10, 20, 0)) == []                 # 12 h on the wall, 6.5 of them active
    assert jobs.lines(db, at=at(2026, 11, 10, 20, 0))[0].startswith("✓")
    assert jobs.check(db, at=at(2026, 11, 11, 1, 30)) == ["order_sweep:poshmark"]   # 17.5 − 5.5 = 12 active hours
    assert said == ["⚠️ Poshmark's order sweep hasn't worked for 12 h."]


@pytest.mark.quiet_on
def test_the_20_minute_alarm_counts_only_active_time(db, monkeypatch):
    said = []
    monkeypatch.setattr(notify, "say", said.append)
    iid = db.add_item("b1", 1, ".")
    db.upsert_listing(iid, "vinted", status="queued")
    since = at(2026, 11, 10, 8, 25)
    health.pause(db, "vinted", "infra: Vinted brand: Illegal invocation", health.INFRA,
                 error="Vinted brand: Illegal invocation", at=since)
    said.clear()
    assert alarm.tick(db, at=at(2026, 11, 10, 14, 0)) == []                 # 5 active minutes, not 5.5 hours
    assert alarm.tick(db, at=at(2026, 11, 10, 14, 14)) == []
    assert alarm.tick(db, at=at(2026, 11, 10, 14, 16)) == ["vinted"]        # 21 active minutes
    assert said[-1].startswith("🚨 Vinted posting is blocked since ")


# ---------------------------------------------------------------- 4. Telegram: nothing in the quiet hours, silent nights

@pytest.fixture
def wire(monkeypatch):
    calls = []

    def http_post(url, data=None, files=None, timeout=20, **extra):
        calls.append((url.rsplit("/", 1)[1], dict(data or {})))
        return response(url, {"ok": True, "result": [] if url.endswith("getUpdates") else {"message_id": 1}})
    monkeypatch.setattr(telegram, "http_post", http_post)
    return calls


@pytest.mark.quiet_on
def test_inside_the_quiet_hours_nothing_but_a_read_reaches_telegram(wire, clock):
    clock.set(2026, 11, 10, 10, 0)
    with pytest.raises(telegram.QuietHours):
        telegram.post("t", "sendMessage", data={"chat_id": "1", "text": "x"})
    telegram.post("t", "getUpdates", data={"timeout": 0})
    bot = telegram.Bot("t", "1", {7})
    with pytest.raises(RuntimeError, match="QuietHours") as e:
        bot.send_message("a card")
    assert not telegram.maybe_delivered(e.value)                           # never left the Mac: sent again later
    bot.answer_callback("cb-1")                                            # a tap's spinner: no error, no "Sorry"
    assert [m for m, _ in wire] == ["getUpdates"]
    clock.set(2026, 11, 10, 14, 0)
    bot.send_message("a card")
    assert [m for m, _ in wire] == ["getUpdates", "sendMessage"]


def test_a_query_too_old_is_no_error(monkeypatch):
    def http_post(url, data=None, files=None, timeout=20, **extra):
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: query is too old and response "
                                                                       "timeout expired or query ID is invalid"},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(telegram, "http_post", http_post)
    telegram.Bot("t", "1", {7}).answer_callback("cb-1")                    # the tap made at 10 AM, read at 2 PM


@pytest.mark.quiet_on
@pytest.mark.parametrize("hm,silent", [((21, 59), False), ((22, 0), True), ((3, 0), True), ((6, 59), True),
                                       ((7, 0), False), ((16, 0), False)])
def test_at_night_a_message_makes_no_sound(wire, clock, hm, silent):
    clock.set(2026, 11, 10, *hm)
    telegram.post("t", "sendMessage", data={"chat_id": "1", "text": "✅ card"})
    telegram.post("t", "editMessageText", data={"chat_id": "1", "message_id": 3, "text": "✅ card, edited"})
    assert (wire[0][1].get("disable_notification") == "true") is silent
    assert "disable_notification" not in wire[1][1]                       # an edit makes no sound anyway


@pytest.mark.quiet_on
def test_a_line_said_in_the_quiet_hours_waits_and_says_why(db, wire, monkeypatch, clock):
    monkeypatch.setattr(notify, "_db", lambda: db)
    monkeypatch.setattr(notify, "_ops_enabled", lambda: (True, "tok", "42"))
    clock.set(2026, 11, 10, 10, 0)
    assert notify.ops("✓ Vinted: taken down") is False
    assert notify.held(db) == 1 and wire == []
    assert notify.flush_held(db) == 0 and notify.held(db) == 1             # nothing re-sent in the quiet hours either
    clock.set(2026, 11, 10, 14, 1)
    assert notify.flush_held(db) == 1 and notify.held(db) == 0
    text = wire[0][1]["text"]
    assert text.startswith("✓ Vinted: taken down\n\n(held back since ") and text.endswith(": the quiet hours)")


@pytest.mark.quiet_on
def test_the_queue_sends_and_resends_nothing_in_the_quiet_hours(db, monkeypatch, clock):
    s, sent = config.settings(), []
    monkeypatch.setattr(approve, "bot_for", lambda s_: object())
    monkeypatch.setattr(approve, "open_message", lambda db_, settle=True, lane="real": None)
    monkeypatch.setattr(approve, "next_up", lambda db_, lane="real": ("item", "i_261110_aaaaaa") if lane == "real" else None)
    monkeypatch.setattr(approve, "_send", lambda s_, db_, kind, ref: sent.append(ref))
    clock.set(2026, 11, 10, 10, 0)
    assert approve.pump(s, db) is None and approve.resend_pending(s, db) == [] and sent == []
    clock.set(2026, 11, 10, 14, 0)
    assert approve.pump(s, db) == "item i_261110_aaaaaa" and sent == ["i_261110_aaaaaa"]


# ---------------------------------------------------------------- 5. the owner's overrides, the peek, the heartbeat

@pytest.mark.quiet_on
def test_post_now_and_quiet_now_in_the_private_chat(db, monkeypatch, clock):
    s, sent, ran, beats = config.settings(), [], [], []

    class Bot:
        def send_message(self, text, reply_to=None):
            sent.append((text, quiet.lifted(None, db)))                  # was it lifted when the answer went?
    monkeypatch.setattr(approve, "ops_bot_for", lambda s_: Bot())
    monkeypatch.setattr(quietctl, "on_mac", lambda s_: True)
    monkeypatch.setattr(quietctl, "run_tick", lambda: ran.append(1) or "kickstart")
    monkeypatch.setattr(quietctl, "beat_now", lambda db_, **k: beats.append(quiet.beat(db_)["override"]) or True)
    clock.set(2026, 11, 10, 10, 0)

    def say(text, mid):
        return approve._route_ops(s, db, {"message": {"message_id": mid, "chat": {"id": 1}, "text": text}})
    assert say("Post now", 5) == "ops: post now (kickstart)"
    assert quiet.lifted(None, db) and not quiet.is_quiet(None, db) and ran == [1]
    assert sent == [("✓ Quiet hours lifted for today — everything runs now.", True)]
    assert beats == [{"date": "2026-11-10", "mode": "post_now"}]           # thrift-api mirrors it at once
    assert say("йгшуе тщц", 6) == "ops: quiet now (kickstart)"             # "quiet now" typed on the Russian layout
    assert sent[-1] == ("✓ Quiet hours back — the agent stops now and starts again at 2:00 PM.", True)   # said first
    assert quiet.is_quiet(None, db) and beats[-1] is None and ran == [1, 1]
    clock.set(2026, 11, 10, 16, 0)                                         # outside the window: nothing to stop now
    assert say("quiet now", 7) == "ops: quiet now (nothing to stop now)"
    assert sent[-1][0] == "✓ Quiet hours back — from 8:30 AM to 2:00 PM the agent does nothing on the Mac."
    assert say("зщые тщц", 8) == "ops: post now (nothing to start)" and ran == [1, 1]


def test_the_owners_two_words():
    assert [quietctl.command(t) for t in ("post now", "/post now", "Post Now!", "зщые тщц", "quiet now", "QUIET NOW.",
                                          "йгшуе тщц", "post", "now", "post now please", "doctor", None)] == \
        ["post_now"] * 4 + ["quiet_now"] * 3 + [None] * 5


@pytest.fixture
def peek_env(tmp_path, monkeypatch, settings_override):
    s = settings_override(telegram={"enabled": True, "ops_chat_id": "555"}, paths={"db": str(tmp_path / "state.db")})
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "77")
    db = DB(s.path("db"))
    db.kv_set(approve.OFFSET_KEY, "41")
    return s, db


def _update(uid, chat=555, sender=77, text="post now", when=None):
    when = when or at(2026, 11, 10, 9, 58)
    return {"update_id": uid, "message": {"message_id": uid, "chat": {"id": chat}, "from": {"id": sender},
                                          "date": int(when.timestamp()), "text": text}}


@pytest.mark.quiet_on
def test_the_peek_finds_her_post_now_never_moves_the_offset_never_sends(peek_env, clock):
    s, db = peek_env
    clock.set(2026, 11, 10, 10, 0)
    urls = []

    def fetch(url):
        urls.append(url)
        return {"ok": True, "result": [_update(42)]}
    assert quietctl.peek(None, s, db, fetch=fetch, open_db=lambda: db) == "post-now"
    assert urls == ["https://api.telegram.org/bot123:secret/getUpdates?timeout=0&limit=100&offset=42"]
    assert db.kv_get(approve.OFFSET_KEY) == "41"                           # the worker reads it again and answers it
    assert quiet.lifted(None, db) and quietctl.decide(None, db, s, stopped=True) == "start"


@pytest.mark.quiet_on
@pytest.mark.parametrize("updates", [
    [_update(42, sender=99)],                                              # someone else
    [_update(42, chat=-100)],                                              # in the group, not her private chat
    [_update(42, when=at(2026, 11, 9, 21, 0))],                            # yesterday's
    [_update(42), _update(43, text="quiet now")],                          # her last word wins
    [_update(42, text="post")],
])
def test_the_peek_takes_only_her_post_now_of_today(peek_env, clock, updates):
    s, db = peek_env
    clock.set(2026, 11, 10, 10, 0)
    assert quietctl.peek(None, s, db, fetch=lambda url: {"ok": True, "result": updates}, open_db=lambda: db) == "none"
    assert not quiet.lifted(None, db)


@pytest.mark.quiet_on
def test_the_peek_outside_the_window_or_offline_does_nothing_and_never_shows_the_token(peek_env, clock, capsys):
    s, db = peek_env
    clock.set(2026, 11, 10, 15, 0)
    assert quietctl.peek(None, s, db, fetch=lambda url: pytest.fail("looked outside the window")) == "none"
    clock.set(2026, 11, 10, 10, 0)

    def offline(url):
        raise OSError(f"cannot reach {url}")
    assert quietctl.peek(None, s, db, fetch=offline) == "none"
    assert "123:secret" not in capsys.readouterr().err


def test_one_heartbeat_carries_the_quiet_hours(db):
    seen = []

    def tick(db_, force_beat=False, info=None):
        seen.append((force_beat, info))
        return {"beat": True}
    assert quietctl.beat_now(db, tick=tick, info=lambda db_: {"quiet": quiet.beat(db_)})
    assert seen[0][0] is True and set(seen[0][1]["quiet"]) == {"enabled", "start", "end", "timezone", "days",
                                                                 "allow_takedowns", "override"}
    assert not quietctl.beat_now(db, tick=lambda *a, **k: 1 / 0, info=lambda db_: {})   # never raises
    assert "quiet" in cli.beat_info(db)


# ---------------------------------------------------------------- 6. posting at every other hour

def test_posting_runs_at_every_hour_outside_the_quiet_hours(tmp_path):
    s = _settings(tmp_path)
    for h in (0, 3, 7, 8, 21, 23):
        assert can_post(s, 0, 0, at(2026, 11, 10, h, 30)) == (True, "ok")
    assert can_post(s, 0, 25, at(2026, 11, 10, 3, 0)) == (False, "daily cap reached")    # the cap stays
    assert approve.when_posting(s, at(2026, 11, 10, 3, 0)) == "in the next few minutes"


@pytest.mark.quiet_on
def test_from_8_20_an_answer_says_it_goes_up_at_2_pm(tmp_path, db, clock):
    s = config.settings()
    assert approve.when_posting(s, at(2026, 11, 10, 8, 19)) == "in the next few minutes"
    assert approve.when_posting(s, at(2026, 11, 10, 8, 20)) == "at 2:00 PM"
    assert approve.when_posting(s, at(2026, 11, 10, 13, 0)) == "at 2:00 PM"
    quiet.set_post_now(db, at(2026, 11, 10, 9, 0))
    assert approve.when_posting(s, at(2026, 11, 10, 9, 5), db) == "in the next few minutes"
    clock.set(2026, 11, 10, 8, 25)
    block = daily.blocked(s, db, at(2026, 11, 11, 8, 25))[1]
    assert block == "the quiet hours (8:30 AM – 2:00 PM) — they go up from 2:00 PM"
    assert daily.status_text(daily.Work(0, 0, 0, 3, 0), daily.PosterNow(True, True), daily.Timing(60, 120, 120, 0),
                             True, block, "", at(2026, 11, 11, 8, 25)) == \
        "⏸ Waiting — 3 listings wait: the quiet hours (8:30 AM – 2:00 PM) — they go up from 2:00 PM"


POSTING_HOURS = re.compile(r"listing hours|posting hours|outside the hours|go(?:es)? up at (?:0?8|9)\b|after 0?8:00|"
                           r"at 0?8:00|from 0?8:00|opening hour", re.I)


def test_no_posting_hours_anywhere():
    """WO54 §3: the 08:00–23:00 (and 09:00–21:00) posting hours are gone — no message names them, no code reads them,
    the settings have none."""
    hits = []
    for path in sorted((ROOT / "thrift_agent").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        hits += [f"{path.name}: reads the posting hours" for _ in re.finditer(r'get\("schedule\.hours"|\["hours"\]', text)]
        hits += [f"{path.name}: outside listing hours" for _ in re.finditer("outside listing hours", text)]
        hits += [f"{path.name}:{n}: {t[:80]!r}" for n, t in _strings(path) if POSTING_HOURS.search(t)]
    assert not hits, "\n".join(hits)
    data = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    assert "hours" not in data["schedule"] and data["quiet"]["start"] == "08:30" and data["quiet"]["end"] == "14:00"


# ---------------------------------------------------------------- 7. the scripts (read as text: the Mac runs them)

def _read(name: str) -> str:
    return (ROOT / "deploy" / name).read_text(encoding="utf-8")


def _code(name: str) -> str:
    """The script without its comment lines (they tell what it never does)."""
    return "\n".join(line for line in _read(name).splitlines() if not line.lstrip().startswith("#"))


def test_the_calendar_job_runs_the_tick_every_10_minutes_and_at_load():
    job = plistlib.loads(_read("com.thriftagent.quiet.plist").replace("__HOME__", "/Users/x").encode())
    assert job["Label"] == "com.thriftagent.quiet"
    assert job["ProgramArguments"] == ["/bin/bash", "/Users/x/thrift-agent/deploy/quiet.sh", "tick"]
    assert job["RunAtLoad"] is True and "KeepAlive" not in job
    assert job["StartCalendarInterval"] == [{"Minute": m} for m in (0, 10, 20, 30, 40, 50)]
    assert job["ProcessType"] == "Background"


def test_quiet_sh_stops_by_launchd_in_order_and_never_touches_her_browsers():
    script = _code("quiet.sh")
    stop = script[script.index("do_stop() {"):script.index("do_start() {")]
    full = stop[stop.index("stop_one poster 330"):]                        # (the take-downs branch stops the worker only)
    order = [full.index(x) for x in ("stop_one poster 330", "ctl beat", "stop_one worker 150", "stop_one chrome 30",
                                     "sweep")]
    assert order == sorted(order) and stop.index('touch "$MARK"') < stop.index("stop_one poster 330")
    assert 'bash "$SERVICES" stop "$svc"' in script                         # services.sh stop: launchctl bootout
    assert "--user-data-dir=$HOME/thrift/chrome-cross" in script and 'Q_P1="--user-data-dir=$POSH_PROFILE"' in script
    for never in ("pkill", "killall", "Safari", "osascript", "quit app"):
        assert never not in script, never
    start = script[script.index("do_start() {"):script.index("tick() {")]
    assert 'start worker' in start and 'start poster' in start and "chrome" not in start   # Chrome: the poster's to open
    assert start.index('rm -f "$MARK"') > start.index("start poster")       # the marker goes only once both are up
    tick = script[script.index("tick() {"):script.index("check() {")]
    assert 'ctl decide --stopped' in tick and "ctl peek" in tick
    assert tick.index("do_stop") < tick.index("ctl peek") and "! loaded worker" in tick   # the peek: worker stopped
    assert 'mkdir "$LOCK"' in tick                                          # one tick at a time
    for gnu in ("timeout ", "sed -i", "readlink -f", "grep -P", "date -d", "mapfile", "readarray", "declare -A",
                "stat -c", ",,}"):
        assert gnu not in script, gnu                                     # macOS bash 3.2, BSD tools


def test_setup_loads_the_calendar_job_and_deploy_waits_for_the_quiet_hours_to_end():
    setup = _read("mac_setup.sh")
    assert 'launchctl bootstrap "gui/$(id -u)" "$QUIET_PLIST"' in setup
    assert setup.index("com.thriftagent.quiet.plist") < setup.index('[ -n "${THRIFT_DEPLOY:-}" ] && exit 0')
    assert "the quiet hours' calendar job didn't load" in setup
    deploy = _read("mac_deploy.sh")
    assert deploy.index("THRIFT_DEPLOY_ANYWAY") < deploy.index("if [ -d private/.git ]")    # before anything is set up
    assert 'back "the owner\'s quiet hours"' in deploy and "quiet|closing)" in deploy
    forced = deploy[deploy.rindex("quiet|closing)"):]
    assert forced.index("bash deploy/quiet.sh tick") < forced.index("exit 0") < forced.index("restart worker")
    assert "start poster" not in deploy                                     # a deploy never starts the poster
    services = _read("services.sh")
    assert "quietctl status --short" in services and "com.thriftagent.quiet isn't loaded" in services


@pytest.mark.skipif(sys.platform == "win32", reason="bash -n on the Mac / Linux (Windows' bash may be WSL's)")
def test_the_scripts_parse():
    for name in ("quiet.sh", "services.sh", "mac_setup.sh", "mac_deploy.sh"):
        r = subprocess.run(["bash", "-n", str(ROOT / "deploy" / name)], capture_output=True, text=True, check=False)
        assert r.returncode == 0, (name, r.stderr)


def test_the_calendar_jobs_python_stays_light(tmp_path):
    """Every 10 minutes on her laptop: the decision imports no model, no browser, no HTTP library."""
    code = ("import sys, thrift_agent.config, thrift_agent.quietctl as q; q.decide(None, None, {'quiet': {}}); "
            "q.status_line(None, None, {'quiet': {}}); heavy = sorted({m.split('.')[0] for m in sys.modules} "
            "& {'httpx', 'anthropic', 'pydantic', 'PIL', 'typer', 'rich', 'playwright', 'numpy'}); print(heavy)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=False,
                       env={"PATH": "", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
                            "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[]", r.stdout
