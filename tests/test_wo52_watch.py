"""WO52 §4/§5/§7: every job proves it's alive; one alarm that can't be missed; the Mac's memory."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from thrift_agent import alarm, health, jobs, memory, notify
from thrift_agent.config import Settings
from thrift_agent.db import DB

T0 = datetime(2026, 10, 28, 14, 30, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "state.db")


@pytest.fixture
def said(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "say", out.append)
    monkeypatch.setattr(notify, "ops_photo", lambda shot, text: out.append(text))
    monkeypatch.setattr(notify, "group", lambda text: out.append("GROUP " + text))
    return out


# ---------------------------------------------------------------- §4: jobs

def test_the_order_sweep_failing_for_12_hours_says_one_line(db, said):
    """Live: Poshmark's sweep failed every 30 min from Oct 28 to Nov 1 and nobody heard."""
    jobs.ok(db, "order_sweep:poshmark", at=T0)
    for h in range(1, 30):
        jobs.failed(db, "order_sweep:poshmark", "TargetClosedError: browser has been closed", at=T0 + timedelta(hours=h))
        jobs.check(db, at=T0 + timedelta(hours=h))
    assert len(said) == 1
    assert said[0].startswith("⚠️ Poshmark's order sweep hasn't worked for 12 h — last error: TargetClosedError")
    jobs.ok(db, "order_sweep:poshmark", at=T0 + timedelta(hours=31))
    assert said[-1] == "✅ Poshmark's order sweep works again."


def test_a_daily_job_has_24_hours(db, said):
    jobs.ok(db, "cleanup", at=T0)
    assert jobs.check(db, at=T0 + timedelta(hours=23)) == []
    assert jobs.check(db, at=T0 + timedelta(hours=25)) == ["cleanup"]


def test_a_job_never_seen_is_not_judged_and_doctor_lists_them(db, said):
    assert jobs.check(db, at=T0) == []
    jobs.ok(db, "takedowns", at=T0)
    assert jobs.lines(db, at=T0 + timedelta(minutes=5)) == ["✓ The take-downs: last success 5 min ago"]


# ---------------------------------------------------------------- §5: the alarm

def _paused(db, at, error="Vinted brand: Illegal invocation (at vinted.js:812)"):
    iid = db.add_item("b1", 1, ".")
    db.upsert_listing(iid, "vinted", status="queued")
    health.pause(db, "vinted", f"infra: {error}", health.INFRA, error=error, at=at)


def test_blocked_20_minutes_with_items_waiting_is_one_alarm_with_the_request(db, said):
    _paused(db, T0)
    said.clear()
    assert alarm.tick(db, at=T0 + timedelta(minutes=19)) == []
    assert alarm.tick(db, at=T0 + timedelta(minutes=21)) == ["vinted"]
    msg = said[-1]
    assert msg.startswith("🚨 Vinted posting is blocked since ") and "1 item waiting" in msg
    assert 'Step "brand": Illegal invocation' in msg
    assert "Paste this to Claude Code:\n```\n" in msg and "then post the waiting items" in msg
    assert alarm.tick(db, at=T0 + timedelta(hours=2)) == []                 # not again within 4 h
    assert alarm.tick(db, at=T0 + timedelta(hours=4, minutes=30)) == ["vinted"]   # still blocked: once more
    assert alarm.tick(db, at=T0 + timedelta(hours=9)) == []                 # at most twice a day


def test_the_owners_pause_and_a_login_page_never_alarm(db, said):
    iid = db.add_item("b1", 1, ".")
    db.upsert_listing(iid, "depop", status="queued")
    health.owner_pause(db, "depop")
    health.pause(db, "vinted", "Vinted: not logged in", "account", at=T0)
    db.upsert_listing(iid, "vinted", status="queued")
    assert alarm.tick(db, at=T0 + timedelta(hours=1)) == []


def test_the_alarm_clears_with_the_back_to_normal_line(db, said):
    _paused(db, T0)
    alarm.tick(db, at=T0 + timedelta(minutes=25))
    health.tested(db, "vinted", True)
    assert said[-1] == "✅ Vinted back to normal — posting resumed, 1 item going up."
    alarm.tick(db, at=T0 + timedelta(minutes=30))
    assert db.kv_get(alarm.KEY) == "{}"


# ---------------------------------------------------------------- §7: memory

TOP = """PID MEM
77656 2286M
28373 1200M
77645 868M
28187 880M
20075 288M
"""
PS = """77645 1 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=/Users/x/thrift/chrome-cross
77656 77645 /Applications/Google Chrome.app/.../Google Chrome Helper (GPU) --type=gpu-process
28373 1 /System/Library/Frameworks/WebKit.framework/.../com.apple.WebKit.WebContent
28187 1 /System/Library/Frameworks/WebKit.framework/.../com.apple.WebKit.WebContent
20075 1 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
"""


def test_memory_is_grouped_by_whose_browser():
    groups = dict(memory.by_group(memory.processes(TOP, PS)))
    assert round(groups["Thrift Chrome (ours)"]) == 2286 + 868
    assert round(groups["Safari (the Mac's own)"]) == 1200 + 880
    assert "Google Chrome (the owner's own)" in groups


def test_the_thrift_chrome_is_restarted_when_idle_under_pressure(db, said):
    s = Settings({"machine_role": "dev"})
    procs = memory.processes(TOP, PS)
    calls = []
    assert memory.tick(s, db, idle=False, now=1000, procs=procs, level=2, restart=lambda: calls.append(1)) is None
    assert memory.tick(s, db, idle=True, now=1000, procs=procs, level=2, restart=lambda: calls.append(1)) == "restarted"
    assert calls == [1] and said[-1].startswith("🧠 The Mac was short of memory: the Thrift Chrome held 3.1 GB")
    assert memory.tick(s, db, idle=True, now=2000, procs=procs, level=2, restart=lambda: calls.append(1)) is None


def test_critical_for_30_minutes_says_one_line_naming_safari(db, said):
    s = Settings({"machine_role": "dev"})
    top = "PID MEM\n28373 4000M\n77645 900M\n"
    procs = memory.processes(top, PS)
    memory.tick(s, db, idle=False, now=0, procs=procs, level=4)
    assert said == []
    memory.tick(s, db, idle=False, now=1900, procs=procs, level=4)
    assert said[-1].startswith("🧠 The Mac has been short of memory for 30 min. Biggest users: Safari")
    assert "Closing some Safari tabs" in said[-1]
    memory.tick(s, db, idle=False, now=4000, procs=procs, level=4)
    assert len(said) == 1


# ---------------------------------------------------------------- §4: no browser stays dead

def test_poshmarks_chrome_quit_by_hand_is_reopened_with_one_line_and_probed_when_idle(tmp_path, db, said, monkeypatch):
    import asyncio

    from thrift_agent.post import parallel

    class Ctx:
        def __init__(self):
            self.handlers, self.dead = {}, False

        def on(self, ev, fn):
            self.handlers[ev] = fn

        async def cookies(self):
            if self.dead:
                raise RuntimeError("Target page, context or browser has been closed")
            return []

        async def close(self):
            pass

        async def new_page(self):
            class P:
                async def close(self):
                    pass
            return P()

    class Pw:
        async def stop(self):
            pass

    made = []

    async def opener(profile, tz):
        made.append(Ctx())
        return Pw(), made[-1]

    clock = {"t": 0.0}
    monkeypatch.setattr(parallel.time, "monotonic", lambda: clock["t"])
    s = Settings({"machine_role": "dev", "paths": {"db": str(tmp_path / "state.db"), "chrome_profile": str(tmp_path)},
                  "schedule": {"timezone": "America/New_York"}})
    b = parallel.Browser(s, db, object(), opener)

    async def go():
        await b.ensure()
        made[0].handlers["close"]()                 # quit from the Dock
        await b.ensure()
        made[1].dead = True                         # gone silently: found by the 5-minute probe
        clock["t"] += 301
        await b.ensure()

    asyncio.run(go())
    assert len(made) == 3
    assert said == ["Poshmark's Chrome was closed on the Mac — reopened. Please don't quit the Chrome windows on the Mac."]


def test_a_process_of_no_browser_is_named_by_itself():
    ps = PS + "174 1 /System/Library/PrivateFrameworks/SkyLight.framework/Resources/WindowServer -daemon\n"
    groups = dict(memory.by_group(memory.processes("PID MEM\n174 1755M\n", ps)))
    assert groups == {"WindowServer": 1755}
