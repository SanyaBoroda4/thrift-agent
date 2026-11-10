"""WO54 §4 — her laptop comes first, outside the quiet hours too. Live 2026-11-09: the Thrift Chrome, started at 1 AM with
--restore-last-session and a Vinted start page, held 9.6 GB in 51 pages on her 8 GB MacBook; two of them burned a core
each until 9:35 AM; she couldn't work. Here: the Thrift Chrome opens only for work and quits after 10 idle minutes
(never under her hands, never while a login page waits for her), a Chrome quit on purpose is no "missing extension",
Poshmark's browser closes when idle and reopens with the next page (no loss, no line), the agent's priorities and its
memory budget, the measurement lines, one core per HEIC, and the archive on the Mac's own disk (iCloud keeps only a
share taken short) with its one-time migration."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import plistlib
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from ext_fake import ws_open, ws_recv, ws_send

from thrift_agent import bridge as bm
from thrift_agent import cleanup, crosslist, doctor, memory, notify, pipeline, uploads
from thrift_agent.config import ROOT, Settings
from thrift_agent.db import DB
from thrift_agent.ingest import prep
from thrift_agent.post import parallel, runner
from thrift_agent.post.ext_driver import ExtensionPoster

TOKEN = "wo54-test-token-" + "k" * 30


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "state.db")


@pytest.fixture
def said(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "say", out.append)
    monkeypatch.setattr(notify, "group", lambda text: out.append("GROUP " + text))
    return out


class Ready:
    """A socket the bridge counts as an open extension link."""
    ready = True
    hold_until = 0.0

    def send(self, msg) -> bool:
        return True


def _woken(b):
    calls = []
    b.waker = lambda why: calls.append(why) or {"out": "chrome: started", "was_running": False}
    return calls


# ---------------------------------------------------------------- the Thrift Chrome on demand

def test_a_job_with_no_extension_asks_for_the_thrift_chrome_once_and_waits_for_it():
    b = bm.Bridge(TOKEN)
    calls = _woken(b)
    assert b.available() and b.link() == "asleep"              # not started yet: a job wakes it
    b.submit("vinted", "publish", {"fields": {}})
    assert calls == ["a vinted publish job"] and b.link() == "waking" and b.available()
    b.submit("depop", "publish", {"fields": {}})
    assert len(calls) == 1                                     # asked once (the watch asks again after 2 min)
    b.wanted_at -= bm.WAKE_GIVE_UP + 1                         # three minutes and it never connected
    assert not b.available() and b.link() == "missing"         # the rows wait; the watch restarts it, then says so


def test_without_a_waker_only_a_connected_extension_counts():
    """The dev machine and the CLI's dry runs elsewhere (and every other test): the old rule."""
    b = bm.Bridge(TOKEN)
    assert not b.available() and b.link() == "missing"
    b.submit("vinted", "publish", {"fields": {}})
    assert not b.available()


def test_a_refused_token_is_never_woken_into_available():
    b = bm.Bridge(TOKEN)
    calls = _woken(b)
    b.refused = 1
    assert not b.available() and b.link() == "refused"
    assert calls == []


def test_it_quits_after_ten_idle_minutes_not_before_and_says_so():
    b = bm.Bridge(TOKEN)
    b.sockets.add(Ready())
    quits, notes = [], []
    b.on_change = lambda kind, detail: notes.append((kind, detail))
    t0 = b.last_work
    tick = lambda dt: b.watch_tick(t0 + dt, True, None, None, quit_chrome=quits.append)  # noqa: E731
    assert tick(bm.IDLE_QUIT - 1) is None and quits == []
    assert tick(bm.IDLE_QUIT) == "chrome quit" and quits == ["nothing to do for 10 minutes"]
    assert not b.quitting and b.quit_at is not None
    assert notes == [("thrift_chrome_quit", {"why": "nothing to do for 10 minutes", "out": None})]


@pytest.mark.parametrize("keep", ["queued", "in hand", "owner", "login page"])
def test_it_is_never_quit_with_work_waiting_under_her_hands_or_with_a_page_waiting_for_her(keep):
    b = bm.Bridge(TOKEN)
    b.sockets.add(Ready())
    quits = []
    now = b.last_work + bm.IDLE_QUIT + 60
    if keep == "queued":
        b.quitting = True                                      # (nothing handed while it is set)
        b.submit("vinted", "publish", {"fields": {}})
        b.quitting = False
    elif keep == "in hand":
        job = b.submit("vinted", "publish", {"fields": {}})
        b.queue.clear()
        job.handed = time.monotonic()
    elif keep == "owner":
        b._pinged({"type": "ping", "using": True, "tabs": 3, "windows": 2})
        assert b.ext_tabs == 3 and b.ext_windows == 2
        now = b.owner_at + bm.OWNER_KEEP - 1
        b.last_work = now - bm.IDLE_QUIT - 1
    else:
        job = b.submit("depop", "check_login", {})
        b._event({"job_id": job.id, "event": "error", "page": "login", "message": "log in"})
        now = b.last_work + bm.IDLE_QUIT + 1
    assert b.watch_tick(now, True, None, None, quit_chrome=quits.append) is None and quits == []


def test_a_login_page_keeps_it_six_hours_at_most_and_a_result_clears_it():
    b = bm.Bridge(TOKEN)
    b.sockets.add(Ready())
    job = b.submit("depop", "check_login", {})
    b._event({"job_id": job.id, "event": "error", "page": "captcha", "message": "a CAPTCHA"})
    assert "depop" in b.left_for_owner
    since = b.left_for_owner["depop"][1]
    assert b.quit_reason(since + bm.LEFT_KEEP - 1) is None
    assert b.quit_reason(since + bm.LEFT_KEEP + 1) == "nothing to do for 10 minutes"
    job = b.submit("depop", "check_login", {})
    b._event({"job_id": job.id, "event": "result", "page": "form"})
    assert b.left_for_owner == {}


def test_short_of_memory_an_idle_chrome_quits_at_once_but_never_mid_job():
    b = bm.Bridge(TOKEN)
    b.sockets.add(Ready())
    quits = []
    squeeze = lambda: "memory pressure critical"  # noqa: E731
    job = b.submit("vinted", "publish", {"fields": {}})
    b.queue.clear()
    job.handed = time.monotonic()
    assert b.watch_tick(b.last_work + 5, True, None, None, quit_chrome=quits.append, squeeze=squeeze) is None
    job.final, job.done = {"event": "result"}, True
    assert b.watch_tick(b.last_work + 5, True, None, None, quit_chrome=quits.append, squeeze=squeeze) == "chrome quit"
    assert quits == ["short of memory: memory pressure critical"]


def test_a_chrome_quit_on_purpose_is_no_missing_extension():
    """Nothing waits for it: no start, no restart, no "hasn't checked in" line — for hours."""
    b = bm.Bridge(TOKEN)
    b.waker = lambda why: None
    b.quit_at = b.started
    started, restarted, said = [], [], []
    for minutes in range(0, 600, 5):
        assert b.watch_tick(b.started + minutes * 60, True, lambda: started.append(1), said.append,
                            lambda: restarted.append(1)) is None
    assert started == restarted == said == []
    assert b.link() == "asleep" and b.available()


def test_work_that_comes_while_it_quits_waits_and_wakes_it_once_it_is_gone():
    async def go():
        b = bm.Bridge(TOKEN)
        b.sockets.add(Ready())
        calls = _woken(b)
        let_go = threading.Event()

        def slow_quit(why):
            let_go.wait(5)
            return "quit (pid 1)"
        assert b.watch_tick(b.last_work + bm.IDLE_QUIT + 1, True, None, None, quit_chrome=slow_quit) == "chrome quit"
        assert b.quitting
        b.sockets.clear()                                      # its socket went with it
        b.last_seen = None
        job = b.submit("vinted", "publish", {"fields": {}})
        assert b._next() is None                               # nothing handed to a Chrome that is going away
        assert calls == []                                     # and no start while it still shuts down
        let_go.set()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if calls:
                break
        return b, job, calls
    b, job, calls = run(go())
    assert not b.quitting and calls == ["work came while it quit"] and job in b.queue


def test_its_hello_clears_the_wake_and_its_pings_say_her_focus():
    async def go():
        b = await bm.Bridge(TOKEN, port=0).start()
        try:
            _woken(b)
            b.want("a job")
            assert b.wanted_at is not None and b.woke_at is not None
            r, w, _ = await ws_open(b.port)
            ws_send(w, {"type": "hello", "token": TOKEN, "loaded": bm.files_hash()})
            assert (await ws_recv(r))["type"] == "welcome"
            assert b.wanted_at is None and b.woke_at is None and b.connected_at is not None
            ws_send(w, {"type": "ping", "using": True, "tabs": 4, "windows": 2})
            assert (await ws_recv(r))["type"] == "pong"
            await asyncio.sleep(0.05)
            assert b.owner_using() and b.ext_tabs == 4 and b.ext_windows == 2 and b.link() == "connected"
            w.close()
        finally:
            await b.close()
    run(go())


def test_open_bridge_wires_the_waker_the_quit_and_the_link(tmp_path, monkeypatch):
    s = Settings({"machine_role": "prod", "paths": {"db": str(tmp_path / "state.db")},
                  "bridge": {"token_file": str(tmp_path / "ext_token"), "port": 0}})
    (tmp_path / "ext_token").write_text(TOKEN, encoding="utf-8")
    db = DB(s.path("db"))
    woken = []
    monkeypatch.setattr(runner, "chrome_waker", lambda s_: lambda why: woken.append(why) or {
        "out": "chrome: started", "was_running": False})
    monkeypatch.setattr(runner.power, "lid_closed", lambda: False)
    monkeypatch.setattr(bm, "WATCH_EVERY", 0.02)

    async def go():
        ps = {"vinted": ExtensionPoster("vinted")}
        b, task = await runner.open_bridge(s, db, ps)
        try:
            assert ps["vinted"].available()                    # asleep: a job wakes it
            b.submit("vinted", "verify", {"listing_url": "https://www.vinted.com/items/1"})
            await asyncio.sleep(0.3)
        finally:
            await runner.close_bridge(b, task)
    run(go())
    assert woken == ["a vinted verify job"]
    ev = db.conn.execute("SELECT detail FROM events WHERE kind='thrift_chrome_started'").fetchone()
    detail = json.loads(ev["detail"])
    assert detail["on_demand"] is True and detail["was_running"] is False and detail["out"] == "chrome: started"
    link = json.loads(db.kv_get(crosslist.EXT_LINK))
    assert link["connected"] is True and link["link"] == "waking"     # the daily window counts it: it is coming


def test_the_closed_line_is_said_only_for_a_chrome_lost_in_the_middle_of_a_job(db, said):
    b = bm.Bridge(TOKEN)
    runner.chrome_changed(db, b, ["vinted"], "thrift_chrome_started", {"why": "a job", "out": {"was_running": False}})
    assert said == []                                          # quit on purpose / never started: no line
    b.lost_mid_job = time.monotonic()
    runner.chrome_changed(db, b, ["vinted"], "thrift_chrome_started", {"why": "a job", "out": {"was_running": False}})
    assert said == ["The Thrift Chrome was closed on the Mac in the middle of a listing — reopened. It opens and closes "
                    "by itself when there is work; no need to quit it."]
    assert json.loads(db.kv_get("formcheck_request"))["vinted"] == "the Thrift Chrome was closed in the middle of a job"


def test_the_extension_driver_wakes_it_before_waiting():
    b = bm.Bridge(TOKEN)
    calls = _woken(b)
    p = ExtensionPoster("depop", bridge=b)
    assert p.available()
    assert run(p.wait_connected(0.3)) is False and calls == ["depop: a job waits for the extension"]


# ---------------------------------------------------------------- the quit: its own pid, by its profile

def _mac(monkeypatch, ps_args: dict[int, str], launchd_pid: int | None, alive_polls: int = 0):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(runner.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(runner, "THRIFT_CHROME_DIR", "/Users/x/thrift/chrome-cross")
    calls, kills = [], []

    def fake_run(args, **kw):
        calls.append(args)
        if args[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(args, 0, f"state = running\n\tpid = {launchd_pid}\n" if launchd_pid else
                                               "", "")
        if args[0] == "ps":
            return subprocess.CompletedProcess(args, 0, ps_args.get(int(args[-1]), "") + "\n", "")
        if args[0] == "pgrep":
            return subprocess.CompletedProcess(args, 0, "\n".join(str(p) for p in ps_args) + "\n", "")
        raise AssertionError(args)
    polls = {"n": alive_polls}

    def fake_kill(pid, sig):
        kills.append((pid, sig))
        if sig == 0:
            if polls["n"] <= 0:
                raise ProcessLookupError
            polls["n"] -= 1
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(runner.os, "kill", fake_kill)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    return calls, kills


THRIFT = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=/Users/x/thrift/chrome-cross " \
         "--no-first-run --no-startup-window"
HERS = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


QUIT = runner.quit_thrift_chrome                 # the real one: tests/conftest.py replaces the module's own per test


def _quit(s, wait=1.0):
    return QUIT(s, wait)


def test_the_quit_is_one_sigterm_to_the_thrift_chromes_own_main_process(monkeypatch):
    calls, kills = _mac(monkeypatch, {4242: THRIFT}, 4242)
    assert _quit(Settings({"machine_role": "prod"})) == "quit (pid 4242)"
    assert kills == [(4242, signal.SIGTERM), (4242, 0)]
    assert ["launchctl", "print", "gui/501/com.thrift.chrome-cross"] in calls


@pytest.mark.parametrize("args", [HERS, HERS + " --type=renderer --user-data-dir=/Users/x/thrift/chrome-cross",
                                  HERS + " --user-data-dir=/Users/x/thrift/chrome-crossover"])
def test_the_quit_never_touches_her_chrome_a_helper_or_another_profile(monkeypatch, args):
    calls, kills = _mac(monkeypatch, {777: args}, 777)
    assert _quit(Settings({"machine_role": "prod"})) == "not running" and kills == []


def test_the_quit_waits_for_chrome_and_never_kills_harder(monkeypatch):
    calls, kills = _mac(monkeypatch, {4242: THRIFT}, None, alive_polls=10_000)    # started by hand: found by pgrep
    clock = {"t": 0.0}
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock.__setitem__("t", clock["t"] + 0.5) or clock["t"])
    out = _quit(Settings({"machine_role": "prod"}), wait=5)
    assert out.startswith("asked to quit (pid 4242); still running after 5 s")
    assert {sig for _pid, sig in kills} == {signal.SIGTERM, 0} and kills[0] == (4242, signal.SIGTERM)
    assert sum(1 for _pid, sig in kills if sig == signal.SIGTERM) == 1


def test_off_the_mac_nothing_is_quit():
    assert QUIT(Settings({"machine_role": "dev"})).startswith("not on the Mac")


# ---------------------------------------------------------------- Poshmark's browser: closed when idle, no loss

class Page:
    def __init__(self):
        self.handlers = {}

    def on(self, ev, fn):
        self.handlers[ev] = fn

    async def close(self):
        if "close" in self.handlers:
            self.handlers["close"]()


class Ctx:
    def __init__(self):
        self.handlers, self.closed = {}, False

    def on(self, ev, fn):
        self.handlers[ev] = fn

    async def cookies(self):
        return []

    async def close(self):
        self.closed = True

    async def new_page(self):
        page = Page()
        if "page" in self.handlers:
            self.handlers["page"](page)
        return page


class Pw:
    async def stop(self):
        pass


def _browser(tmp_path, db, monkeypatch, fail_after=None):
    made = []

    async def opener(profile, tz):
        if fail_after is not None and len(made) >= fail_after:
            raise RuntimeError("Chrome failed to start")
        made.append(Ctx())
        return Pw(), made[-1]
    clock = {"t": 1000.0}
    monkeypatch.setattr(parallel.time, "monotonic", lambda: clock["t"])
    s = Settings({"machine_role": "dev", "paths": {"db": str(tmp_path / "state.db"), "chrome_profile": str(tmp_path)},
                  "schedule": {"timezone": "America/New_York"}})
    return parallel.Browser(s, db, object(), opener), made, clock


def _kinds(db):
    return [r["kind"] for r in db.conn.execute("SELECT kind FROM events ORDER BY rowid")]


def test_poshmarks_browser_closes_after_ten_idle_minutes_and_the_next_page_reopens_it(tmp_path, db, said, monkeypatch):
    b, made, clock = _browser(tmp_path, db, monkeypatch)

    async def go():
        assert await b.ensure() and len(made) == 1
        page = await b.ctx.new_page()                   # a listing
        clock["t"] += 3600
        assert await b.ensure() and not b.asleep        # a page of ours is open: never closed under it
        await page.close()
        clock["t"] += parallel.IDLE_CLOSE - 1
        assert await b.ensure() and not b.asleep
        clock["t"] += 2
        assert await b.ensure() and b.asleep and made[0].closed
        made[0].handlers["close"]()                     # its own close event, after our sleep: no loss
        assert b.alive and await b.ensure() and len(made) == 1     # asleep: nothing opened for nothing
        await b.ctx.new_page()                          # the next job's first page reopens it
        assert not b.asleep and len(made) == 2 and b.ctx is made[1]
    run(go())
    kinds = _kinds(db)
    assert kinds == ["browser_slept", "browser_woke"]   # no browser_lost, no relaunch, no failure
    assert said == [] and db.kv_get("formcheck_request") is None and b.tries == 0


def test_short_of_memory_it_closes_at_once_with_no_page_open(tmp_path, db, monkeypatch):
    b, made, clock = _browser(tmp_path, db, monkeypatch)
    monkeypatch.setattr(memory, "squeeze", lambda which="thrift": "memory pressure critical" if which == "poshmark"
                        else None)

    async def go():
        await b.ensure()
        clock["t"] += 5
        await b.ensure()
    run(go())
    assert b.asleep and json.loads(db.conn.execute("SELECT detail FROM events WHERE kind='browser_slept'")
                                   .fetchone()["detail"])["why"] == "short of memory: memory pressure critical"


def test_a_reopening_that_fails_is_a_lost_browser_reopened_with_the_back_off(tmp_path, db, said, monkeypatch):
    b, made, clock = _browser(tmp_path, db, monkeypatch, fail_after=1)

    async def go():
        await b.ensure()
        clock["t"] += parallel.IDLE_CLOSE + 1
        await b.ensure()
        assert b.asleep
        with pytest.raises(RuntimeError, match="Chrome failed to start"):
            await b.ctx.new_page()                      # the job fails before its form: nothing done
        assert not b.asleep and not b.alive
        assert not await b.ensure()                     # the usual reopening, its back-off and its count
    run(go())
    assert b.tries == 1 and "browser_relaunch_failed" in _kinds(db) and said == []


# ---------------------------------------------------------------- the agent's priority, the plists, the scripts

def _plist(name):
    return plistlib.loads((ROOT / "deploy" / name).read_bytes())


def test_the_agent_runs_below_her_apps():
    """The worker: launchd's Background class (batch work; inherited by what it starts). The poster and the Thrift
    Chrome: Nice 10 and low-priority disk I/O — not Background, which clamps Chrome to background QoS (the throttled
    pages of WO28 / WO32b stalled live listings)."""
    worker, poster, chrome = (_plist(f) for f in ("com.thriftagent.worker.plist", "com.thriftagent.poster.plist",
                                                   "com.thrift.chrome-cross.plist"))
    assert worker["ProcessType"] == "Background"
    for p in (poster, chrome):
        assert p["Nice"] == 10 and p["LowPriorityIO"] is True and p.get("ProcessType") != "Background"
    assert chrome["RunAtLoad"] is False and "KeepAlive" not in chrome and chrome["ExitTimeOut"] == 30


def test_services_start_chrome_loads_the_installed_plist_then_kickstarts_and_restart_applies_new_plists():
    sh = (ROOT / "deploy" / "services.sh").read_text(encoding="utf-8")
    start = sh[sh.index("start_chrome() {"):sh.index("\n}\n", sh.index("start_chrome() {"))]
    assert "load_chrome >/dev/null" in start and 'launchctl kickstart "$DOMAIN/$(label chrome)"' in start
    load = sh[sh.index("load_chrome() {"):sh.index("\n}\n", sh.index("load_chrome() {"))]
    assert "chrome_current" in load and "bootout" in load and "kickstart" not in load     # loaded, never started
    assert 'grep -q "chrome_cross.sh"' in sh                    # an older job (a start page, a restore) is replaced
    restart = sh[sh.index("restart() {"):sh.index("\n}\n", sh.index("restart() {"))]
    assert "launchctl bootout" in restart and "launchctl bootstrap" in restart
    assert "launchctl kickstart -k" not in restart
    assert "load) " in sh or "load)\n" in sh


def test_mac_setup_makes_the_local_archive_and_marks_the_agents_folders_for_spotlight():
    setup = (ROOT / "deploy" / "mac_setup.sh").read_text(encoding="utf-8")
    assert "mkdir -p ~/thrift/var/local.noindex/archive" in setup
    assert ".metadata_never_index" in setup
    for folder in ("~/thrift/var/work", "~/thrift/var/failed", "~/thrift/chrome-cross", "~/thrift/chrome-profile"):
        assert folder in setup
    assert "mv ~/thrift/var" not in setup                       # the database's folder is never renamed


def test_the_extension_and_the_bridge_share_one_hash_salt():
    background = (bm.EXT_DIR / "background.js").read_text(encoding="utf-8")
    salt = re.search(r'const HASH_SALT = "([^"]+)\\n";', background).group(1)
    assert bm.HASH_SALT == f"{salt}\n".encode() == b"wo56-reload-2\n"     # WO56b: bumped with the reload race


def test_a_big_share_is_decoded_one_core_at_a_time():
    from pillow_heif import options
    assert prep.DECODE_THREADS == 1 and options.DECODE_THREADS == 1


# ---------------------------------------------------------------- the memory budget

def test_may_start_waits_at_warning_and_critical_and_never_off_the_mac(db):
    assert memory.may_start(db, "vinted", level=1) == (True, "")
    assert memory.may_start(db, "vinted", level=None) == (True, "")
    ok, why = memory.may_start(db, "vinted", level=2)
    assert not ok and "pressure warning" in why and "listing in hand finished" in why
    ok, why = memory.may_start(db, "vinted", level=4)
    assert not ok and "pressure critical" in why and "browsers quit" in why
    assert memory.may_start(db, "vinted", level=1)[0]
    assert _kinds(db) == ["memory_wait", "memory_go"]           # one event a wait, not one a turn
    assert memory.may_start(level=2)[0] is False                # no db: no record, the same answer


def test_squeeze_at_critical_for_both_browsers_and_never_off_the_mac(monkeypatch):
    assert memory.squeeze("thrift") is None and memory.squeeze("poshmark") is None
    monkeypatch.setattr(memory, "pressure", lambda read=None: 4)
    memory.reset()
    assert memory.squeeze("poshmark") == memory.squeeze("thrift") == "memory pressure critical"


PS = """\
100 1 0 0.0 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
101 100 409600 30.5 /Applications/Google Chrome.app/Contents/Frameworks/Helper --type=renderer
200 1 51200 1.0 /Users/x/thrift-agent/.venv/bin/python3.14 /Users/x/thrift-agent/.venv/bin/thrift run
201 200 1024 0.0 /usr/bin/caffeinate -i -w 200
300 1 102400 2.0 /Users/x/thrift-agent/.venv/bin/python3.14 /Users/x/thrift-agent/.venv/bin/thrift poster
301 300 40960 0.5 /Users/x/thrift-agent/.venv/lib/playwright/driver/node cli.js run-driver
302 301 204800 5.0 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=/Users/x/thrift/chrome-profile
303 302 307200 12.0 /Applications/Google Chrome.app/Contents/Frameworks/Helper --type=renderer --lang=en-US
400 1 81920 1.0 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=/Users/x/thrift/chrome-cross
401 400 512000 25.0 /Applications/Google Chrome.app/Contents/Frameworks/Helper --type=renderer
402 400 30720 0.1 /Applications/Google Chrome.app/Contents/Frameworks/Helper --type=renderer --extension-process
500 1 1048576 40.0 /System/Library/Frameworks/WebKit.framework/XPCServices/com.apple.WebKit.WebContent
"""


def test_the_agent_is_told_from_her_apps_and_measured_against_its_budget():
    procs = memory.everything(top_out="", ps_out=PS)
    parts = memory.agent(procs)
    assert parts["Worker (ours)"]["n"] == 2 and parts["Poster (ours)"]["n"] == 2       # caffeinate, the driver
    assert parts["Poshmark's Chrome (ours)"]["n"] == 2 and parts["Thrift Chrome (ours)"]["n"] == 3
    line = memory.agent_line(procs)
    assert line.startswith("Agent: 1.3 GB of its 2.0 GB budget — Thrift Chrome 610 MB (3) · Poshmark's Chrome 500 MB (2)")
    assert "poster 140 MB (2)" in line and "worker 51 MB (2)" in line and "over it" not in line
    big = memory.everything(top_out="400 2500M\n", ps_out=PS)
    assert "⚠️ over it" in memory.agent_line(big)
    groups = dict(memory.by_group(procs))
    assert "Google Chrome (the owner's own)" in groups and "Safari (the Mac's own)" in groups


def test_the_laptop_lines_cpu_pages_and_who_is_in_front():
    procs = memory.everything(top_out="", ps_out=PS)
    link = {"tabs": 3, "windows": 2, "link": "connected", "using": False}

    def front(pid):
        def read(args):
            if args == ["lsappinfo", "front"]:
                return "ASN:0x0-0x1d01d:\n"
            if "name" in args:
                return '"LSDisplayName"="Safari"\n' if pid == 500 else '"LSDisplayName"="Google Chrome"\n'
            return f'"pid"={pid}\n'
        return read
    lines = memory.laptop_lines(procs, link=link, read=front(500), load="{ 2.10 1.90 1.80 }")
    assert lines[0].startswith("CPU now: the agent 47% (Thrift Chrome 26%, Poshmark's Chrome 17%, poster 2%, worker 1%)"
                               " of the Mac's 117%")
    assert "load 2.10 1.90 1.80" in lines[0] and "busiest of hers: Safari (the Mac's own) 40%" in lines[0]
    assert lines[1] == ("Pages open: Thrift Chrome 1 page processes (3 tabs in 2 windows, the extension's count; it is "
                        "connected) · Poshmark's Chrome 1 page processes")
    assert lines[2] == ("In front: Safari (no agent window in front) · a Thrift Chrome window has her focus: no (the "
                        "extension's view)")
    assert memory.laptop_lines(procs, link=link, read=front(400), load="")[2].startswith(
        "In front: ⚠️ Thrift Chrome is the app in front")
    assert memory.laptop_lines(procs, link=link, read=lambda args: "", load="")[2].startswith(
        "In front: the app in front can't be read from this session")


def test_the_doctor_shows_the_extension_asleep_and_the_agent_against_its_budget(tmp_path):
    s = Settings({"machine_role": "dev", "paths": {"db": str(tmp_path / "s.db"), "inbox": str(tmp_path / "inbox"),
                                                   "work": str(tmp_path / "work")}})
    db = DB(s.path("db"))
    db.kv_set(crosslist.EXT_LINK, json.dumps({"connected": True, "link": "asleep", "at": "2026-11-09T19:00:00+00:00"}))

    class NoApi:
        def get(self, path):
            return {"shares": []}
    lines = doctor.report(s, db, api=NoApi(), laptop=lambda: ["Agent: 0 MB of its 2.0 GB budget — …", "CPU now: …"])
    assert any(x.startswith("Extension: asleep (the Thrift Chrome is closed — it opens for the next job)") for x in lines)
    assert "Agent: 0 MB of its 2.0 GB budget — …" in lines and "CPU now: …" in lines


# ---------------------------------------------------------------- the archive on the Mac's own disk

def _icloud(tmp_path) -> Settings:
    posh = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Posh"
    return Settings({"machine_role": "prod", "paths": {
        "inbox": str(posh / "inbox"), "archive": str(tmp_path / "thrift" / "var" / "local.noindex" / "archive"),
        "work": str(tmp_path / "thrift" / "var" / "work"), "db": str(tmp_path / "thrift" / "var" / "state.db"),
        "failed": str(tmp_path / "thrift" / "var" / "failed")}, "inbox": {"done_marker": "_done"}})


def _share(s, name="2026-11-09_140000_ab12", photos=2, count=2, extra=()):
    d = s.path("inbox") / name
    d.mkdir(parents=True)
    for i in range(photos):
        (d / f"IMG_{i}.jpg").write_bytes(b"\xff\xd8\xff" + bytes([i]) * 100)
    for name_ in extra:
        (d / name_).write_bytes(b"x" * 50)
    (d / "_done.txt").write_text(f"done {count}", encoding="utf-8")
    return d


def test_a_whole_share_leaves_icloud_for_the_macs_own_disk_copied_and_checked_first(tmp_path, db):
    s = _icloud(tmp_path)
    d = _share(s, extra=("IMG_0.mov",))
    dest = pipeline.archive_share(s, db, "b_1", d)
    assert dest.parent == s.path("archive") and not d.exists()
    assert sorted(p.name for p in dest.iterdir()) == ["IMG_0.jpg", "IMG_0.mov", "IMG_1.jpg", "_done.txt"]
    assert (dest / "IMG_1.jpg").read_bytes() == b"\xff\xd8\xff" + bytes([1]) * 100
    assert json.loads(db.conn.execute("SELECT detail FROM events WHERE kind='archived'").fetchone()["detail"])["dest"] \
        == str(dest)


def test_a_share_taken_short_waits_in_icloud_where_its_late_photos_land(tmp_path, db):
    s = _icloud(tmp_path)
    d = _share(s, photos=2, count=5)
    dest = pipeline.archive_share(s, db, "b_1", d)
    assert dest.parent == pipeline.late_archive(s) == s.path("inbox").parent / "archive"


def test_a_share_with_a_file_still_in_the_cloud_is_never_downloaded_to_move_it(tmp_path, db, monkeypatch):
    s = _icloud(tmp_path)
    d = _share(s, photos=2, count=3, extra=("IMG_0.mov",))
    monkeypatch.setattr(prep, "dataless", lambda p: p.suffix == ".mov")
    dest = pipeline.archive_share(s, db, "b_1", d)
    assert dest.parent == pipeline.late_archive(s) and (dest / "IMG_0.mov").exists()       # renamed in iCloud


def test_a_copy_that_does_not_check_out_is_never_followed_by_a_delete(tmp_path, db, monkeypatch):
    s = _icloud(tmp_path)
    d = _share(s)
    real = pipeline.shutil.copy2
    monkeypatch.setattr(pipeline.shutil, "copy2", lambda a, b: real(a, b) if not str(a).endswith("IMG_1.jpg") else
                        Path(b).write_bytes(b"short"))
    dest = pipeline.archive_share(s, db, "b_1", d)
    assert dest.parent == pipeline.late_archive(s)              # not out of iCloud: renamed there, nothing lost
    assert (dest / "IMG_1.jpg").read_bytes() == b"\xff\xd8\xff" + bytes([1]) * 100
    assert not any(s.path("archive").iterdir()) if s.path("archive").exists() else True
    assert "archive_local_failed" in _kinds(db)


def test_the_dev_machine_archive_is_unchanged(tmp_path, db):
    s = Settings({"machine_role": "dev", "paths": {"inbox": str(tmp_path / "inbox"), "archive": str(tmp_path / "arch"),
                                                   "work": str(tmp_path / "work")}})
    d = _share(s)
    dest = pipeline.archive_share(s, db, "b_1", d)
    assert dest.parent == tmp_path / "arch" and pipeline.late_archive(s) == tmp_path / "arch"


def test_the_cleanup_and_the_upload_intake_see_icloud_s_short_shares(tmp_path, db):
    s = _icloud(tmp_path)
    late = pipeline.late_archive(s) / "20261109_2026-11-09_150000_cd34"
    late.mkdir(parents=True)
    (late / "IMG_9.jpg").write_bytes(b"x")
    assert uploads.known_folder(s, "2026-11-09_150000_cd34") == late
    assert cleanup.sizes(s)["archive_icloud"] == 1


# ---------------------------------------------------------------- the one-time migration (deploy/move_archive.py)

def _script():
    spec = importlib.util.spec_from_file_location("move_archive", ROOT / "deploy" / "move_archive.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_migration_moves_every_folder_it_can_checks_each_copy_and_the_records_follow(tmp_path, db):
    mv = _script()
    s = _icloud(tmp_path)
    src_root, dest_root = pipeline.late_archive(s), s.path("archive")
    old = src_root / "20261101_2026-11-01_100000_aa11"           # whole, its batch split long ago
    old.mkdir(parents=True)
    (old / "IMG_1.jpg").write_bytes(b"a" * 300)
    (old / "_done.txt").write_text("done 1", encoding="utf-8")
    b1 = db.add_batch(str(s.path("inbox") / "2026-11-01_100000_aa11"), 1)
    db.set_batch(b1, status="split")
    db.log(b1, "archived", {"dest": str(old)})
    db.conn.execute("UPDATE batches SET created_at='2026-11-01T15:00:00+00:00' WHERE id=?", (b1,))
    late = db.add_batch(f"{old}#101010", 1)                      # an in-place late batch on that folder
    db.set_batch(late, status="split")
    short = src_root / "20261109_2026-11-09_090000_bb22"         # taken short this morning: its late photos may come
    short.mkdir()
    (short / "IMG_2.jpg").write_bytes(b"b" * 10)
    (short / "_done.txt").write_text("done 4", encoding="utf-8")
    b2 = db.add_batch(str(s.path("inbox") / "2026-11-09_090000_bb22"), 1)
    db.set_batch(b2, status="split")
    db.log(b2, "archived", {"dest": str(short)})
    stray = src_root / "20261020_unknown"                        # no share of ours: moved all the same
    stray.mkdir()
    (stray / "x.jpg").write_bytes(b"c")
    rows = mv.plan(s, db, src_root, dest_root)
    by = {Path(r["folder"]).name: r for r in rows}
    assert by[old.name]["move"] and by[stray.name]["move"] and not by[short.name]["move"]
    assert "taken short" in by[short.name]["why"]
    assert all(r["move"] for r in mv.plan(s, db, src_root, dest_root, every=True))           # --all
    out = mv.move(db, by[old.name], wait=0, pull_fn=lambda d, w: [])
    assert out["moved"] and out["files"] == 2 and out["bytes"] == 300 + len("done 1") and not old.exists()
    new = dest_root / old.name
    assert (new / "IMG_1.jpg").read_bytes() == b"a" * 300
    dests = {r["ref"]: json.loads(r["detail"])["dest"] for r in
             db.conn.execute("SELECT ref, detail FROM events WHERE kind='archived' ORDER BY rowid")}
    assert dests[b1] == str(new)                                  # late_photos and the cleanup follow it
    assert db.batch(late)["src_dir"] == f"{new}#101010"


def test_the_migration_never_moves_a_folder_with_files_still_in_the_cloud(tmp_path, db):
    mv = _script()
    s = _icloud(tmp_path)
    d = pipeline.late_archive(s) / "20261101_x"
    d.mkdir(parents=True)
    (d / "IMG_1.HEIC").write_bytes(b"h")
    row = {"folder": str(d), "dest": str(s.path("archive") / d.name), "batches": [], "move": True, "why": ""}
    out = mv.move(db, row, wait=0, pull_fn=lambda folder, w: [folder / "IMG_1.HEIC"])
    assert not out["moved"] and out["cloud_only"] == ["IMG_1.HEIC"] and d.exists()
    assert not (s.path("archive") / d.name).exists()


def test_the_migration_refuses_a_destination_inside_icloud(tmp_path, monkeypatch, capsys):
    mv = _script()
    s = _icloud(tmp_path)
    monkeypatch.setattr(mv, "settings", lambda: s)
    assert mv.main(["--to", str(s.path("inbox").parent / "elsewhere")]) == 2
    assert "inside iCloud Drive" in capsys.readouterr().out
