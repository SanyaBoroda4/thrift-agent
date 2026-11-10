"""WO56 — human steps in one minute (the owner, 2026-11-09 8:34 PM: "give me a WO so I can pass captcha or whatever is
needed by human"). Since WO54 §4 the agent's Chrome is hidden, quits when idle and tidies every page no job holds, so a
🚨 "Vinted shows a CAPTCHA" pointed to a window nobody could find. Here (§5 rows 1–5): a CAPTCHA met during a Vinted job
is HELD (its page kept, the Thrift Chrome never quit, the other sites posting) and said ONCE in the private chat with its
screenshot; "show" brings that window to the front for 5 minutes; the page clearing says "✅ Vinted back — posting
resumed" once and the item goes on; a "Just a moment" that passes by itself says nothing at all; a step pending at 8:30
waits through the quiet hours and is reminded once at 2:05 PM. The extension's side is in tests/js."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from ext_fake import PNG

from thrift_agent import alarm, approve, crosslist, doctor, health, humansteps, layout, notify
from thrift_agent import bridge as bm
from thrift_agent.config import Settings
from thrift_agent.db import DB

TOKEN = "wo56-test-token-" + "h" * 30
T0 = datetime(2026, 11, 10, 0, 12, tzinfo=timezone.utc)          # 7:12 PM in New York, Nov 9


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "state.db")


@pytest.fixture
def said(monkeypatch):
    out = SimpleNamespace(ops=[], group=[], photos=[])
    monkeypatch.setattr(notify, "say", lambda text: out.ops.append(text))
    monkeypatch.setattr(notify, "ops", lambda text, **k: out.ops.append(text) or True)
    monkeypatch.setattr(notify, "photo", lambda path, caption: out.photos.append((Path(path), caption)))
    monkeypatch.setattr(notify, "ops_photo", lambda path, caption: out.photos.append((Path(path), caption)) or True)
    monkeypatch.setattr(notify, "group", lambda text: out.group.append(text))
    monkeypatch.setattr(notify, "group_photo", lambda path, caption: out.group.append(caption))
    return out


class Sock:
    """A socket the bridge counts as an open extension link, keeping what it was sent."""
    ready = True
    hold_until = 0.0

    def __init__(self):
        self.sent = []

    def send(self, msg) -> bool:
        self.sent.append(msg)
        return True


def _shot(tmp_path) -> Path:
    p = tmp_path / "i_1-vinted-20261109-191200-captcha.png"
    p.write_bytes(PNG)
    return p


def _events(db, kind):
    return [json.loads(d) for (d,) in db.conn.execute("SELECT detail FROM events WHERE kind=? ORDER BY rowid", (kind,))]


def _captcha(db, tmp_path, at=T0):
    crosslist.block(db, "vinted", "vinted: a CAPTCHA is shown in the Thrift Chrome — captcha page", "captcha",
                    _shot(tmp_path))
    rec = humansteps.pending(db, "vinted")
    rec["since"] = at.isoformat(timespec="seconds")              # the clock the tests below count from
    rec["said"] = [rec["since"]]
    data = humansteps.pending(db)
    data["vinted"] = rec
    db.kv_set(humansteps.KEY, json.dumps(data))
    return rec


# ---------------------------------------------------------------- §5 row 1: a CAPTCHA during a Vinted job

def test_a_captcha_during_a_vinted_job_is_held_and_said_once_privately_with_its_screenshot(db, said, tmp_path):
    _captcha(db, tmp_path)
    assert said.group == []                                       # never the group
    assert said.ops == []                                         # no ⛔ line, no 🚨
    (path, caption), = said.photos                                # ONE message, with the page's picture
    assert path.name.endswith("-captcha.png")
    assert caption.startswith("🔐 Vinted wants a person — a CAPTCHA (screenshot), since ")
    assert "connect to the Mac's screen (Screen Sharing, 100.109.116.57), reply show, solve it in the window that " \
           "comes up, and that's it." in caption
    assert caption.endswith("Depop and Poshmark keep posting meanwhile.")
    p = health.state(db, "vinted")
    assert p["kind"] == "account" and p["page"] == "captcha"      # an account pause: Vinted alone
    assert crosslist.blocked(db, "depop") is None and crosslist.blocked(db, "poshmark") is None
    crosslist.block(db, "vinted", "vinted: a CAPTCHA is shown in the Thrift Chrome — captcha page", "captcha")
    assert len(said.photos) == 1 and said.ops == []               # another job met it: still one message
    assert alarm.tick(db, at=T0 + timedelta(minutes=25)) == [] and said.ops == []    # never a 🚨 for it
    assert _events(db, "human_step_pending")[0]["kind"] == "captcha"


def test_a_sign_in_or_a_code_says_type_it_yourself_and_a_block_page_keeps_its_old_path(db, said, tmp_path):
    crosslist.block(db, "depop", "depop: not logged in in the Thrift Chrome — login page", "login")
    text, = said.ops
    assert text.startswith("🔐 Depop wants you to sign in, since ")
    assert "reply show and type the password or code yourself on the Mac's screen — the agent never types it." in text
    assert said.group == []
    crosslist.block(db, "vinted", "vinted: the site asks for a check (verification) — verify page", "verify")
    assert "🔐 Vinted wants a person — a check that it's you (a code or a confirmation)" in said.ops[-1]
    assert "Poshmark keeps posting meanwhile." in said.ops[-1]   # Depop is paused: not named
    said.ops.clear()
    crosslist.block(db, "poshmark", "Poshmark: 403 Forbidden", "block")   # nobody can type a block away: as before
    assert said.group == ["Poshmark turned the Mac away for now — I'll try again by myself later."]
    assert not humansteps.pending(db, "poshmark")


def test_the_held_step_keeps_the_thrift_chrome_open_and_its_extension_told(db, said, tmp_path):
    _captcha(db, tmp_path)
    b = bm.Bridge(TOKEN)
    sock = Sock()
    b.sockets.add(sock)
    b.steps_source = lambda: humansteps.for_bridge(db)
    quits = []
    late = b.last_work + bm.IDLE_QUIT + bm.LEFT_KEEP + 60          # past the idle quit AND the 6 h of a plain page
    assert b.watch_tick(late, True, None, None, quit_chrome=quits.append) is None and quits == []
    steps, = [m for m in sock.sent if m["type"] == "steps"]
    assert steps["steps"] == {"vinted": {"label": "Vinted — human step", "kind": "captcha", "since": T0.isoformat()}}
    b.watch_tick(late + 15, True, None, None, quit_chrome=quits.append)
    assert len([m for m in sock.sent if m["type"] == "steps"]) == 1   # told again only on a change (or 5 min later)
    humansteps.clear(db, "vinted", "test")
    assert b.watch_tick(late + 30, True, None, None, quit_chrome=quits.append) == "chrome quit"
    assert sock.sent[-1] == {"type": "steps", "steps": {}}         # its page goes (the extension closes it)


def test_the_other_sites_keep_posting_and_its_self_test_waits_while_the_page_is_read(db, said, tmp_path):
    _captcha(db, tmp_path)
    assert not humansteps.watching(db, "vinted", T0)
    assert humansteps.on_ext_event(db, {"event": "step_page", "site": "vinted", "page": "captcha", "held": True},
                                   at=T0 + timedelta(seconds=10)) == "held"
    assert humansteps.watching(db, "vinted", T0 + timedelta(seconds=20))          # read every 10 s: no test run
    assert not humansteps.watching(db, "vinted", T0 + timedelta(seconds=90))      # silent: the self-test may look
    import inspect
    from thrift_agent.post import parallel
    assert "humansteps.watching(db, mp)" in inspect.getsource(parallel._cross_turn)
    assert health.state(db, "depop") is None and health.state(db, "poshmark") is None


# (the real driver and bridge with the stand-in extension, its picture riding the AccountBlocked: tests/test_bridge.py
# test_a_stop_page_stops_the_site_with_its_own_line)


# ---------------------------------------------------------------- §5 row 2: "show"

def _bot(monkeypatch):
    sent = []

    class Bot:
        def send_message(self, text, buttons=None, reply_to=None):
            sent.append(text)
    monkeypatch.setattr(approve, "ops_bot_for", lambda s_: Bot())
    return sent


def _reply(db, text):
    return approve._route_ops(Settings({}), db, {"message": {"message_id": 7, "chat": {"id": 1}, "text": text}})


def test_show_brings_that_window_to_the_front_and_only_then(db, said, tmp_path, monkeypatch):
    _captcha(db, tmp_path)
    sent = _bot(monkeypatch)
    b = bm.Bridge(TOKEN)
    sock = Sock()
    b.sockets.add(sock)
    b.steps_source = lambda: humansteps.for_bridge(db)
    b.shows_source = lambda: humansteps.take_show(db, ["depop", "vinted"])
    b.watch_tick(b.last_work + 1, True, None, None)
    assert not [m for m in sock.sent if m["type"] == "show"]     # nothing comes to the front by itself
    assert _reply(db, "show") == "ops: show"
    assert sent[-1].startswith("✓ Vinted: the window comes to the front on the Mac's screen within 15 seconds")
    assert "goes back by itself after 5 minutes" in sent[-1]
    b.watch_tick(b.last_work + 2, True, None, None)
    shows = [m for m in sock.sent if m["type"] == "show"]
    assert shows == [{"type": "show", "site": "vinted", "ms": 300000}]
    b.watch_tick(b.last_work + 3, True, None, None)
    assert len([m for m in sock.sent if m["type"] == "show"]) == 1   # once per "show"
    humansteps.on_ext_event(db, {"event": "step_shown", "site": "vinted"}, at=T0 + timedelta(minutes=1))
    assert humansteps.pending(db, "vinted")["shown"]


def test_show_with_the_thrift_chrome_closed_wakes_it_and_waits_for_its_hello(db, said, tmp_path):
    _captcha(db, tmp_path)
    b = bm.Bridge(TOKEN)
    woke = []
    b.waker = lambda why: woke.append(why) or {"out": "started", "was_running": False}
    b.steps_source = lambda: humansteps.for_bridge(db)
    humansteps.ask(db, "show")
    b.shows_source = lambda: humansteps.take_show(db, ["depop", "vinted"])
    b.sync_steps()
    assert woke == ["the owner's show (vinted)"] and "vinted" in b.pending_shows
    sock = Sock()
    b._hello_steps(sock)                                          # the extension's hello
    assert [m["type"] for m in sock.sent] == ["steps", "show"] and sock.sent[1]["site"] == "vinted"


def test_later_snoozes_the_reminder_12_hours_and_done_is_accepted(db, said, tmp_path, monkeypatch):
    t0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)   # her replies are stamped now
    _captcha(db, tmp_path, at=t0)
    sent = _bot(monkeypatch)
    assert _reply(db, "later") == "ops: later"
    assert sent[-1].startswith("✓ No reminder about Vinted for 12 hours")
    humansteps.tick(db, at=t0 + timedelta(hours=1, minutes=1))
    assert humansteps.tick(db, at=t0 + timedelta(hours=12, minutes=30)) == []        # snoozed past the 12 h mark
    assert humansteps.tick(db, at=t0 + timedelta(hours=13, minutes=2)) == ["vinted"]  # then the rhythm goes on
    assert _reply(db, "done") == "ops: done" and "Looking at Vinted again now" in sent[-1]
    humansteps.tick(db, at=t0 + timedelta(hours=14))
    assert humansteps.pending(db, "vinted")["done"]
    assert health.state(db, "vinted")["due"] <= (t0 + timedelta(hours=14)).isoformat()  # not read: looked at now


def test_show_later_and_done_typed_on_the_russian_layout(db, said, tmp_path, monkeypatch):
    _captcha(db, tmp_path)
    sent = _bot(monkeypatch)
    assert layout.normalize("ырщц") == ("show", True)
    assert layout.normalize("дфеук") == ("later", True)
    assert layout.normalize("вщту") == ("done", True)
    assert _reply(db, "ырщц") == "ops: show" and sent[-1].startswith("✓ Vinted: the window comes to the front")
    assert humansteps.command("show vinted") == ("show", "vinted") and humansteps.command("show me") is None


def test_show_for_poshmark_opens_its_sign_in_page_in_its_own_browser_in_front(db, said, monkeypatch):
    """Poshmark's human step lives in its own browser (Playwright): "show" opens the sign-in page there, in front; once
    it is signed in its self-test runs at once and the page is closed."""
    import asyncio
    from thrift_agent.post import parallel, poshmark

    class Page:
        def __init__(self):
            self.url, self.closed, self.front, self.went = "", False, 0, []

        async def goto(self, url):
            self.went.append(url)
            self.url = url

        async def evaluate(self, js):
            return None

        async def bring_to_front(self):
            self.front += 1

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    page = Page()

    class Ctx:
        async def new_page(self):
            return page
    browser = SimpleNamespace(ctx=Ctx(), step_page=None, step_until=0.0, lost=lambda why: None)
    monkeypatch.setitem(poshmark.SEL, "captcha", lambda p: SimpleNamespace(count=lambda: asyncio.sleep(0, 0)))
    crosslist.block(db, "poshmark", "not logged in to Poshmark in the poster profile")
    assert said.ops[-1].startswith("🔐 Poshmark wants you to sign in")
    ps = {"poshmark": SimpleNamespace(base_url="https://poshmark.com", username="shop")}
    asyncio.run(parallel._poshmark_step(None, db, ps, browser))
    assert page.went == [] and browser.step_page is None           # nothing opens by itself
    humansteps.ask(db, "show", "poshmark")
    asyncio.run(parallel._poshmark_step(None, db, ps, browser))
    assert page.went == ["https://poshmark.com/login"] and page.front == 1 and browser.step_page is page
    page.url = "https://poshmark.com/feed"                         # signed in by the owner
    asyncio.run(parallel._poshmark_step(None, db, ps, browser))
    assert page.closed and browser.step_page is None
    assert health.state(db, "poshmark")["due"] <= datetime.now(timezone.utc).isoformat()   # its self-test now


def test_with_nothing_pending_show_says_so(db, said, monkeypatch):
    sent = _bot(monkeypatch)
    assert _reply(db, "show") == "ops: show"
    assert sent == ["Nothing waits for a person on the Mac now — every site posts by itself."]


# ---------------------------------------------------------------- §5 row 3: the page clears

def test_the_page_clears_and_says_posting_resumed_once_and_the_item_goes_on(db, said, tmp_path):
    iid = db.add_item("b1", 1, ".")
    db.upsert_listing(iid, "vinted", status="queued")
    _captcha(db, tmp_path)
    humansteps.on_ext_event(db, {"event": "step_shown", "site": "vinted"}, at=T0 + timedelta(minutes=2))
    t = T0 + timedelta(minutes=3)
    assert humansteps.on_ext_event(db, {"event": "step_page", "site": "vinted", "page": "captcha"}, at=t) == "held"
    assert humansteps.on_ext_event(db, {"event": "step_page", "site": "vinted", "page": "form",
                                        "url": "/items/new"}, at=t + timedelta(seconds=10)) == "cleared"
    line, = said.ops
    assert line.startswith("✅ Vinted back — posting resumed, 1 item going up (its check cleared at ")
    assert health.state(db, "vinted") is None                      # posting again: the queued item goes on
    assert crosslist.blocked(db, "vinted") is None
    assert humansteps.on_ext_event(db, {"event": "step_page", "site": "vinted", "page": "form"}, at=t) is None
    assert len(said.ops) == 1                                      # once
    row, = _events(db, "human_step")
    assert row["site"] == "vinted" and row["kind"] == "captcha" and row["person_needed"] is True
    assert row["seconds"] == 190 and row["cleared_at"] and row["at"] == T0.isoformat()


def test_a_page_between_two_states_is_no_proof_and_a_lost_page_is_looked_at_again_once(db, said, tmp_path):
    _captcha(db, tmp_path)
    t = T0 + timedelta(minutes=1)
    assert humansteps.seen(db, "vinted", "unknown", at=t) == "clearing"
    assert humansteps.seen(db, "vinted", "captcha", at=t + timedelta(seconds=10)) == "held"
    health.tested(db, "vinted", False, "x", at=t)                  # its self-test due later
    due = health.state(db, "vinted")["due"]
    assert humansteps.seen(db, "vinted", "gone", held=False, at=t + timedelta(seconds=20)) == "gone"
    assert health.state(db, "vinted")["due"] < due                 # its page is gone: the self-test opens it now
    health.tested(db, "vinted", False, "x", at=t)
    later = health.state(db, "vinted")["due"]
    humansteps.seen(db, "vinted", "gone", held=False, at=t + timedelta(seconds=30))
    assert health.state(db, "vinted")["due"] == later              # once — never a test every turn


def test_a_self_test_that_passes_ends_the_step_with_its_own_line_only(db, said, tmp_path):
    _captcha(db, tmp_path)
    health.tested(db, "vinted", True, "the canary filled")
    assert len(said.ops) == 1 and said.ops[0].startswith("✅ Vinted back — posting resumed")
    assert not any("back to normal" in m for m in said.ops)
    assert not humansteps.pending(db)


# ---------------------------------------------------------------- §5 row 4: a "Just a moment" that passes

def test_a_just_a_moment_that_clears_in_20_seconds_says_nothing_and_is_logged(db, said):
    assert humansteps.on_ext_event(db, {"event": "challenge", "site": "vinted", "state": "cleared", "seconds": 20,
                                        "mode": "publish"}, at=T0) == "challenge cleared"
    assert said.ops == [] and said.group == [] and said.photos == []
    row, = _events(db, "human_step")
    assert row == {"site": "vinted", "kind": "challenge", "page": "challenge",
                   "at": (T0 - timedelta(seconds=20)).isoformat(), "cleared_at": T0.isoformat(), "seconds": 20,
                   "person_needed": False, "mode": "publish", "by": "the page itself"}
    assert health.state(db, "vinted") is None


def test_one_that_stays_waits_quietly_15_minutes_then_is_a_human_step(db, said, tmp_path):
    why = "vinted: a security check (\"Just a moment…\") that didn't pass by itself in 2 minutes — challenge page"
    crosslist.block(db, "vinted", why, "challenge")
    assert said.ops == [] and said.group == [] and said.photos == []          # the first: nothing said
    p = health.state(db, "vinted")
    assert p["page"] == "challenge" and p["wait"] == humansteps.CHALLENGE_RETRY   # its retry 15 minutes later
    assert alarm.tick(db) == [] and said.ops == []                             # no 🚨, no adopted step
    crosslist.block(db, "vinted", why, "challenge", _shot(tmp_path))           # the retry met it again
    (_, caption), = said.photos
    assert caption.startswith("🔐 Vinted wants a person — a “verify you are human” check that didn't pass by itself")


def test_one_that_passes_on_its_retry_says_nothing_and_is_logged(db, said):
    crosslist.block(db, "vinted", "vinted: a security check — challenge page", "challenge")
    health.tested(db, "vinted", True, "the canary filled")                    # the retry passed
    assert said.ops == [] and said.photos == [] and health.state(db, "vinted") is None
    row, = _events(db, "human_step")
    assert row["kind"] == "challenge" and row["person_needed"] is False


def test_a_job_waiting_out_a_challenge_gets_two_more_minutes():
    b = bm.Bridge(TOKEN)
    job = b.submit("vinted", "publish", {"fields": {}})
    job.handed = 1.0
    b._event({"job_id": job.id, "event": "progress", "challenge": "waiting", "what": "a security check"})
    assert job.extra == bm.CHALLENGE_EXTRA


def test_a_stop_page_of_a_job_without_its_own_evidence_path_is_pictured(tmp_path):
    import base64
    b = bm.Bridge(TOKEN)
    b.shots_dir = tmp_path / "shots"
    job = b.submit("depop", "check_login", {})
    job.handed = 1.0
    b._event({"job_id": job.id, "event": "screenshot", "label": "captcha", "png_b64": base64.b64encode(PNG).decode()})
    b._event({"job_id": job.id, "event": "screenshot", "label": "photos", "png_b64": base64.b64encode(PNG).decode()})
    kept = [s for s in job.shots if s["png"]]
    assert len(kept) == 1 and Path(kept[0]["png"]).name.startswith("depop-check_login-")   # only the stop page's


# ---------------------------------------------------------------- §5 row 5: the quiet hours

@pytest.mark.quiet_on
def test_a_step_pending_at_8_30_waits_and_is_reminded_once_at_2_05_pm(db, said, tmp_path):
    _captcha(db, tmp_path, at=T0)                                  # 7:12 PM; quiet 8:30 AM – 2:00 PM (New York)
    assert humansteps.tick(db, at=T0 + timedelta(hours=11)) == []   # 6:12 AM: not 12 h yet
    assert humansteps.tick(db, at=T0 + timedelta(hours=12, minutes=1)) == ["vinted"]   # 7:13 AM: 12 h
    from thrift_agent import quiet
    assert quiet.is_quiet(T0 + timedelta(hours=14)) and quiet.closing(T0 + timedelta(hours=13, minutes=11))
    two = datetime(2026, 11, 10, 19, 0, tzinfo=timezone.utc)      # 2:00 PM: the agent starts again
    assert humansteps.tick(db, at=two) == []                       # the 2:05 PM one is the one
    assert humansteps.tick(db, at=two + timedelta(minutes=5)) == ["vinted"]
    assert said.ops[-1].startswith("🔐 Reminder (")
    assert "Vinted still wants a person — a CAPTCHA, since " in said.ops[-1] and "reply show" in said.ops[-1]
    assert humansteps.tick(db, at=two + timedelta(minutes=20)) == []           # never more
    assert humansteps.tick(db, at=two + timedelta(hours=11)) == []
    assert humansteps.tick(db, at=two + timedelta(hours=12, minutes=6)) == ["vinted"]   # the 12-hour rhythm


@pytest.mark.quiet_on
def test_later_also_covers_the_reminder_after_the_quiet_hours(db, said, tmp_path):
    t = datetime(2026, 11, 10, 13, 0, tzinfo=timezone.utc)        # 8:00 AM
    _captcha(db, tmp_path, at=t)
    humansteps.ask(db, "later", at=t + timedelta(minutes=5))
    humansteps.tick(db, at=t + timedelta(minutes=6))
    assert humansteps.tick(db, at=datetime(2026, 11, 10, 19, 5, tzinfo=timezone.utc)) == []    # snoozed till 8:05 PM
    assert said.ops == []


def test_a_pause_from_before_wo56_is_adopted_with_its_one_message(db, said):
    health.pause(db, "vinted", "Vinted: not logged in", "account", at=T0)      # no step record (an older pause)
    assert alarm.tick(db, at=T0 + timedelta(minutes=1)) == ["vinted"]
    assert said.ops[-1].startswith("🔐 Vinted wants you to sign in") and said.group == []
    assert alarm.tick(db, at=T0 + timedelta(minutes=2)) == []                  # once


# ---------------------------------------------------------------- §4: the record, the doctor

def test_the_doctor_shows_a_pending_step_and_the_days_steps(db, said, tmp_path):
    _captcha(db, tmp_path)
    humansteps.seen(db, "vinted", "captcha", at=T0 + timedelta(seconds=30))
    lines = humansteps.doctor_lines(db, T0 + timedelta(minutes=1))
    assert lines[0].startswith("🔐 Human step: Vinted wants a person — a CAPTCHA since ")
    assert "its page held in the Thrift Chrome, read 30 s ago (captcha)" in lines[0]
    humansteps.on_ext_event(db, {"event": "challenge", "site": "depop", "state": "cleared", "seconds": 12}, at=T0)
    lines = humansteps.doctor_lines(db, T0 + timedelta(minutes=1))
    assert lines[-1].startswith("Human steps in the last day: 1 — Depop challenge ") and "by itself" in lines[-1]
    s = Settings({"paths": {"db": str(tmp_path / "state.db"), "inbox": str(tmp_path / "inbox"),
                            "work": str(tmp_path / "work")}})
    text = "\n".join(doctor.report(s, db, now=T0 + timedelta(minutes=1), api=SimpleNamespace(
        get=lambda path: {"shares": []})))
    assert "🔐 Human step: Vinted wants a person" in text


def test_orders_meeting_a_captcha_begin_the_step_instead_of_their_own_line(db, said):
    from thrift_agent import orders
    orders.stop_today(db, "vinted", "captcha")
    assert humansteps.pending(db, "vinted") and said.ops[-1].startswith("🔐 Vinted wants a person — a CAPTCHA")
    assert not any("Can't read" in m for m in said.ops)
    orders.stop_today(db, "depop", "block")                       # a block page: its old line
    assert said.ops[-1].startswith("⏸ Can't read Depop's orders today")
