"""WO28, the daily window: the Mac is opened ~30 min a day, often on battery, then closed. The power readings, the wake
detection, the idle-sleep assertion, the battery rule, "Back online", the ONE status message (edited, never re-sent)
and what it says, the real timings, iCloud's cloud-only files. No network, no macOS: every seam is injected."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from thrift_agent import approve, daily, pipeline, power
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB
from thrift_agent.ingest import prep
from thrift_agent.schema import Render

NOW = datetime(2026, 10, 5, 18, 0, tzinfo=timezone.utc)              # 14:00 in New York: inside 08:00-23:00
RENDER = Render(marketplace="poshmark", title="Zara Blue Floral Mini Skirt size M", description="Floral mini skirt.",
                brand="Zara", department="Women", category="Skirts", subcategory="Mini", size="M", colors=["Blue"],
                condition="good", price=35, photos=[], sku="i_1")


def _settings(tmp_path, live=True, hours=("08:00", "23:00")) -> Settings:
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}, "machine_role": "prod"}
    data["paths"]["db"] = str(tmp_path / "state.db")
    data["poster"] = {**base["poster"], "dry_run": not live, "autopublish_confirmed": live}
    data["marketplaces"] = {"poshmark": {"enabled": True, "username": "closet", "autopublish": True},
                            "depop": {"enabled": False}}
    data["schedule"] = {**base["schedule"], "hours": list(hours), "timezone": "America/New_York"}
    s = Settings(data)
    s.ensure_dirs()
    return s


def _ready(db: DB, seq: int, decision="publish", price=35) -> str:
    bid = db.add_batch(f"share_{seq}", 3)
    db.set_batch(bid, status="split")
    iid = db.add_item(bid, seq, f"work/{seq}")
    db.set_item(iid, status="ready", owner_price=price, gate={"decision": decision, "reasons": []},
                renders={"poshmark": RENDER.model_dump()})
    return iid


def _waiting(db: DB, seq: int) -> str:
    bid = db.add_batch(f"wait_{seq}", 3)
    db.set_batch(bid, status="split")
    iid = db.add_item(bid, seq, f"work/w{seq}")
    db.set_item(iid, status="awaiting_price", gate={"decision": "publish", "reasons": []},
                renders={"poshmark": RENDER.model_dump()})
    return iid


def _processing(db: DB, seq: int) -> str:
    bid = db.add_batch(f"proc_{seq}", 3)
    db.set_batch(bid, status="split")
    return db.add_item(bid, seq, f"work/p{seq}")                    # status 'new': being processed


class FakeBot:
    chat_id = "100"

    def __init__(self, broken_edit=False):
        self.calls, self.next_id, self.broken_edit = [], 40, broken_edit

    def send_message(self, text, buttons=None, reply_to=None):
        self.next_id += 1
        self.calls.append(("send", self.next_id, text))
        return self.next_id

    def delete_message(self, message_id):
        self.calls.append(("delete", message_id, None))

    def edit_message(self, message_id, text):
        if self.broken_edit:
            raise RuntimeError("telegram editMessageText failed: Bad Request: message to edit not found")
        self.calls.append(("edit", message_id, text))


class Watch:
    """A WakeWatch whose answers the test scripts."""

    def __init__(self, *answers):
        self.answers = list(answers)

    def check(self):
        return self.answers.pop(0) if self.answers else False


class Awake:
    def __init__(self):
        self.calls = []

    def hold(self, on):
        self.calls.append(on)


def _window(s, db, bot=None, said=None, lid=lambda: False, watch=None, battery=lambda: None, scan=None):
    said = [] if said is None else said
    return daily.Window(s, db, bot, say=said.append, scan=scan, lid=lid, watch=watch or Watch(),
                        awake=Awake(), battery=battery, now=lambda: NOW)


# ---------- power: readings ----------

def test_battery_lid_and_wake_readings():
    on_batt = ("Now drawing from 'Battery Power'\n -InternalBattery-0 (id=4653155)\t12%; discharging; 0:41 remaining "
               "present: true")
    on_ac = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=4653155)\t100%; charged; 0:00 remaining present: true"
    assert power.parse_batt(on_batt) == power.Battery(12, False)
    assert power.parse_batt(on_ac) == power.Battery(100, True)
    assert power.parse_batt("Now drawing from 'AC Power'") is None and power.parse_batt(None) is None
    assert power.parse_clamshell('  | |   "AppleClamshellState" = Yes\n') is True
    assert power.parse_clamshell('"AppleClamshellState" = No') is False and power.parse_clamshell("") is None
    assert power.parse_waketime("{ sec = 1728140000, usec = 123456 } Sat Oct  5 10:00:00 2026") == 1728140000.0
    assert power.parse_waketime("{ sec = 0, usec = 0 }") is None and power.parse_waketime(None) is None


def test_off_the_mac_every_reading_is_unknown(monkeypatch):
    monkeypatch.setattr(power, "DARWIN", False)
    assert power.battery() is None and power.lid_closed() is None and power.last_wake() is None


def test_a_wake_is_noticed_by_kern_waketime_or_by_a_gap_in_the_clock():
    clock = {"wall": 1000.0, "mono": 50.0, "wake": 900.0}
    watch = power.WakeWatch(wall=lambda: clock["wall"], mono=lambda: clock["mono"], waketime=lambda: clock["wake"])
    clock["wall"] += 15
    clock["mono"] += 15
    assert watch.check() is False                                  # a normal tick
    clock["wall"] += 3600
    clock["mono"] += 15
    assert watch.check() is True                                   # the wall clock ran an hour, the process 15 s
    clock["wall"] += 15
    clock["mono"] += 15
    clock["wake"] = clock["wall"] - 5
    assert watch.check() is True                                   # kern.waketime moved
    clock["wall"] += 15
    clock["mono"] += 15
    assert watch.check() is False
    unknown = power.WakeWatch(wall=lambda: clock["wall"], mono=lambda: clock["mono"], waketime=lambda: None)
    clock["wall"] += 15
    clock["mono"] += 15
    assert unknown.check() is False                                # no macOS: the clock gap alone


def test_slept_since_a_start():
    assert power.slept_since(1000.0, 900.0, 50.0, wall=lambda: 1200.0, mono=lambda: 250.0,
                             waketime=lambda: 1100.0) is True      # woke after the listing started
    assert power.slept_since(1000.0, 900.0, 50.0, wall=lambda: 1200.0, mono=lambda: 250.0,
                             waketime=lambda: 900.0) is False
    assert power.slept_since(1000.0, None, 50.0, wall=lambda: 1400.0, mono=lambda: 60.0,
                             waketime=lambda: None) is True         # 400 s of wall clock, 10 s of process


def test_the_idle_sleep_assertion_is_caffeinate_tied_to_our_pid_and_only_on_the_mac():
    started = []

    class Proc:
        def __init__(self, args, **kw):
            started.append(args)
            self.done = False

        def poll(self):
            return 0 if self.done else None

        def terminate(self):
            self.done = True

        def wait(self, timeout=None):
            return 0

    awake = power.Awake(popen=Proc, darwin=True, pid=4242)
    awake.hold(True)
    awake.hold(True)                                               # held already: not a second caffeinate
    assert started == [["caffeinate", "-i", "-w", "4242"]] and awake.held
    awake.hold(False)
    assert not awake.held
    off_mac = power.Awake(popen=Proc, darwin=False)
    off_mac.hold(True)
    assert not off_mac.held and len(started) == 1


# ---------- the battery rule ----------

def test_battery_low_pauses_publishing_with_one_line_until_charging_or_20(tmp_path):
    db, said = DB(tmp_path / "state.db"), []
    assert not power.publish_paused(db, power.Battery(12, False), work=False, say=said.append)   # no work: nothing
    assert power.publish_paused(db, power.Battery(14, False), work=True, say=said.append)
    assert power.publish_paused(db, power.Battery(13, False), work=True, say=said.append)
    assert power.publish_paused(db, power.Battery(18, False), work=True, say=said.append)      # 18%: still waits
    assert said == [power.BATTERY_LOW]                                                         # ONE line
    assert not power.publish_paused(db, power.Battery(20, False), work=True, say=said.append)  # back at 20%
    assert power.publish_paused(db, power.Battery(9, False), work=True, say=said.append)       # a new episode
    assert not power.publish_paused(db, power.Battery(9, True), work=True, say=said.append)    # on the charger
    assert said == [power.BATTERY_LOW, power.BATTERY_LOW]
    assert not power.publish_paused(db, None, work=True, say=said.append)                      # no battery


# ---------- "Back online" ----------

def test_start_with_no_work_says_nothing_and_with_work_one_line(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    db, said, resent = DB(s.path("db")), [], []
    monkeypatch.setattr(daily.approve, "resend_pending", lambda s_, db_: resent.append(1) or [])
    w = _window(s, db, said=said)
    assert w.step() == "start" and said == [] and resent == [1]      # the catch-up runs; nothing to say
    db.add_batch("share_new", 4)                                      # a new share, not split yet
    _waiting(db, 1)
    _ready(db, 2)
    w2 = _window(s, db, said=said)
    assert w2.step() == "start"
    assert said == ["Back online — 1 new share, 2 items waiting"]
    assert w2.step() is None                                          # the same window: nothing more


def test_a_wake_runs_the_catch_up_and_a_closed_lid_says_nothing(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    db, said, scans, resent = DB(s.path("db")), [], [], []
    monkeypatch.setattr(daily.approve, "resend_pending", lambda s_, db_: resent.append(1) or [])
    _waiting(db, 1)
    lid = {"closed": False}
    w = _window(s, db, said=said, lid=lambda: lid["closed"], watch=Watch(False, False, True, False, False),
                scan=lambda: scans.append(1))
    assert w.step() == "start" and len(said) == 1                     # start
    assert w.step() is None                                           # a tick
    lid["closed"] = True
    assert w.step() is None and len(said) == 1                        # a maintenance wake, lid closed: quiet
    lid["closed"] = False
    assert w.step() == "wake"                                         # the lid opens: the window begins
    assert said[-1] == "Back online — 1 item waiting" and scans == [1, 1] and resent == [1, 1]
    assert w.update() is not None
    lid["closed"] = True
    assert w.update() is None                                         # lid closed: no status message


# ---------- the status message ----------

def test_the_status_message_is_sent_once_then_edited_and_a_new_window_gets_its_own(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    db, bot = DB(s.path("db")), FakeBot()
    monkeypatch.setattr(daily.approve, "resend_pending", lambda s_, db_: [])
    w = _window(s, db, bot=bot)
    w.step()
    assert w.update() is None or bot.calls == []                      # nothing at all: no message
    item = _processing(db, 1)
    text = w.update()
    assert text.startswith("⏳ Working — 1 item left, about 2 min.") and bot.calls == [("send", 41, text)]
    w.update()
    assert len(bot.calls) == 1                                        # the same text: not touched
    db.set_item(item, status="awaiting_price", gate={"decision": "publish"},
                renders={"poshmark": RENDER.model_dump()})
    text = w.update()
    assert text == ("✓ Safe to close — 1 card is waiting for your answer in Telegram (answers within 24 h are kept)")
    assert bot.calls[-1] == ("edit", 41, text) and [c[0] for c in bot.calls] == ["send", "edit"]
    db.set_item(item, status="posted")
    assert w.update() == daily.ALL_DONE and bot.calls[-1] == ("edit", 41, daily.ALL_DONE)
    daily.Window.begin(w, "wake")                                     # the next window...
    assert bot.calls[-1] == ("delete", 41, None)                      # (the last one's message goes)
    _processing(db, 2)
    w.update()
    assert bot.calls[-1][0] == "send" and bot.calls[-1][1] == 42      # ... gets a message of its own


def test_a_status_message_that_cant_be_edited_is_sent_anew(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    db, bot = DB(s.path("db")), FakeBot(broken_edit=True)
    monkeypatch.setattr(daily.approve, "resend_pending", lambda s_, db_: [])
    w = _window(s, db, bot=bot)
    w.step()
    item = _processing(db, 1)
    w.update()
    db.set_item(item, status="posted")
    w.update()
    assert [c[0] for c in bot.calls] == ["send", "send"]


TIMING = daily.Timing(batch=60, item=150, post=120, gap=240)


@pytest.mark.parametrize("work,poster,hours_open,block,want", [
    (daily.Work(0, 4, 0, 0, 0), daily.PosterNow(True, True), True, None,
     "⏳ Working — 4 items left, about 10 min. Please don't close the Mac yet."),
    (daily.Work(1, 1, 2, 0, 0), daily.PosterNow(True, True), True, None,
     "⏳ Working — 1 new share and 1 item left, about 4 min. Please don't close the Mac yet.\n"
     "2 cards are also waiting for your answer."),
    (daily.Work(0, 0, 0, 3, 0), daily.PosterNow(True, True, next_at=NOW + timedelta(minutes=4)), True, None,
     "⏳ 3 listings still to publish, next in ~4 min. Please don't close the Mac yet."),
    (daily.Work(0, 0, 0, 2, 0), daily.PosterNow(True, True, busy="i_1"), True, None,
     "⏳ 2 listings still to publish, publishing now. Please don't close the Mac yet."),
    (daily.Work(0, 2, 0, 3, 0), daily.PosterNow(True, True), True, None,
     "⏳ Working — 5 items left, about 19 min. Please don't close the Mac yet."),   # 2x150 + 3x120 + 2x240 s
    (daily.Work(0, 0, 0, 0, 0), daily.PosterNow(True, True), True, None, "✓ All done — safe to close the Mac."),
    (daily.Work(0, 0, 2, 0, 0), daily.PosterNow(True, True), True, None,
     "✓ Safe to close — 2 cards are waiting for your answer in Telegram (answers within 24 h are kept)"),
    (daily.Work(0, 0, 0, 3, 0), daily.PosterNow(True, True), False, None,
     "✓ Safe to close — 3 listings will go up after 08:00 next time the Mac is open"),
    (daily.Work(0, 0, 1, 3, 0), daily.PosterNow(True, True), False, None,
     "✓ Safe to close — 3 listings will go up after 08:00 next time the Mac is open; 1 card is waiting for your "
     "answer in Telegram (answers within 24 h are kept)"),
    (daily.Work(0, 0, 0, 1, 0), daily.PosterNow(True, True), True, "the battery is low — they go on once the Mac is "
     "charging", "✓ Safe to close — 1 listing wait: the battery is low — they go on once the Mac is charging"),
    (daily.Work(0, 0, 0, 2, 0), daily.PosterNow(False), True, None,
     "✓ Safe to close — 2 listings wait for the poster (it isn't running)"),
    (daily.Work(0, 0, 0, 0, 1), daily.PosterNow(True, True), True, None,
     "✓ Safe to close — 1 listing needs your look before publishing (see “Not published automatically”)"),
])
def test_what_the_status_message_says(work, poster, hours_open, block, want):
    assert daily.status_text(work, poster, TIMING, hours_open, block, "08:00", NOW) == want


def test_the_window_counts_what_waits_and_what_the_poster_will_publish(tmp_path):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    _ready(db, 1)
    _ready(db, 2, decision="draft")                                     # WO33: never held — it publishes too
    _ready(db, 3, price=None)                                          # not approved: nothing
    _waiting(db, 4)
    _processing(db, 5)
    db.add_batch("share_new", 2)
    assert daily.work(s, db) == daily.Work(new_shares=1, processing=1, cards=1, to_publish=2, held=0)
    dry = _settings(tmp_path / "dry", live=False)
    assert daily.work(dry, db).to_publish == 0 and daily.work(dry, db).held == 2   # dry-run: both wait for you


def test_the_estimate_uses_the_last_runs(tmp_path):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    default = daily.timings(s, db)
    assert (default.item, default.post) == (daily.DEFAULT_S["item"], daily.DEFAULT_S["post"])
    for secs in (100, 140, 120):
        db.log("i_x", "worked", {"kind": "item", "seconds": secs})
    db.log("b_x", "worked", {"kind": "batch", "seconds": 45})
    t0 = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
    for i, (end, secs) in enumerate(((0, 100), (400, 110), (800, 90))):        # gaps: 400-110-0 = 290, 400-90 = 310
        db.conn.execute("INSERT INTO events VALUES (?,?,?,?)", ((t0 + timedelta(seconds=end)).isoformat(),
                                                                 f"i_{i}", "post_posted", json.dumps({"seconds": secs})))
    t = daily.timings(s, db)
    assert (t.item, t.batch, t.post, t.gap) == (120.0, 45.0, 100.0, 300.0)


def test_the_poster_says_what_it_does(tmp_path):
    db = DB(tmp_path / "state.db")
    assert daily.poster_now(db) == daily.PosterNow(running=False)
    daily.poster_beat(db, live=True, busy="i_1")
    assert daily.poster_now(db).running and daily.poster_now(db).busy == "i_1"
    later = datetime.now(timezone.utc) + timedelta(minutes=20)
    assert not daily.poster_now(db, later).running                        # an old heartbeat, even mid-listing
    daily.poster_beat(db, stopped=True)
    assert not daily.poster_now(db).running


def test_outside_the_hours_the_listings_wait_for_next_time(tmp_path):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    night = datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc)              # 00:00 in New York
    assert daily.blocked(s, db, night) == (False, None, "08:00")
    assert daily.blocked(s, db, NOW) == (True, None, "08:00")
    s.flag("PAUSE").write_text("x", encoding="utf-8")
    assert daily.blocked(s, db, NOW)[1].startswith("publishing is paused")


# ---------- iCloud ----------

def _share(s, name, files=("IMG_1.jpg",)):
    d = s.path("inbox") / name
    d.mkdir(parents=True)
    for f in files:
        Image.new("RGB", (60, 80), "red").save(d / f)
    (d / "_done").touch()
    old = time.time() - 3600
    for f in d.iterdir():
        os.utime(f, (old, old))
    return d


def test_cloud_only_files_are_downloaded_first_and_told_once_after_5_minutes(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    db, said, runs = DB(s.path("db")), [], []
    d = _share(s, "2026-10-05_1400", ("IMG_1.jpg", "IMG_2.jpg"))
    in_cloud = {"IMG_2.jpg"}
    monkeypatch.setattr(prep, "dataless", lambda p: p.name in in_cloud)
    monkeypatch.setattr(pipeline.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(pipeline.subprocess, "run", lambda args, **k: runs.append(args))
    monkeypatch.setattr(pipeline.notify, "say", said.append)
    assert pipeline.ready_folders(s, db) == []                           # a file still in iCloud: not yet
    assert runs == [["brctl", "download", str(d)], ["brctl", "download", str(d / "IMG_2.jpg")]]
    assert said == []                                                    # under 5 minutes: quiet
    waiting = json.loads(db.kv_get(pipeline.ICLOUD_WAIT))
    since = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat(timespec="seconds")
    db.kv_set(pipeline.ICLOUD_WAIT, json.dumps({k: {**v, "since": since} for k, v in waiting.items()}))
    pipeline.ready_folders(s, db)
    pipeline.ready_folders(s, db)
    assert said == [pipeline.ICLOUD_MESSAGE]                             # ONE line, after 5 minutes
    in_cloud.clear()                                                     # downloaded
    assert pipeline.ready_folders(s, db) == [d] and json.loads(db.kv_get(pipeline.ICLOUD_WAIT)) == {}


def test_a_dataless_file_is_seen_by_its_flag(tmp_path, monkeypatch):
    f = tmp_path / "IMG_1.jpg"
    f.write_bytes(b"x")

    class St:
        st_flags = prep.SF_DATALESS

    monkeypatch.setattr(type(f), "stat", lambda self, **kw: St())
    assert prep.dataless(f) is True
    monkeypatch.undo()
    assert prep.dataless(f) is False and prep.icloud_placeholders(tmp_path) == []


def test_answers_reach_the_queue_in_order_after_a_sleep(tmp_path, monkeypatch):
    """Telegram keeps the owner's answers 24 h: polled after a wake, they are applied in the order given."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    a, b = _waiting(db, 1), _waiting(db, 2)
    applied = []
    monkeypatch.setattr(approve, "handle_update", lambda s_, db_, bot, u: applied.append(u["update_id"]) or "ok")

    class Bot:
        def get_updates(self, offset, timeout):
            return [{"update_id": 7, "message": {}}, {"update_id": 8, "message": {}}, {"update_id": 9, "message": {}}]

    assert approve.poll_once(s, db, Bot(), 0) == 3
    assert applied == [7, 8, 9] and db.kv_get(approve.OFFSET_KEY) == "9"
    assert a != b


# ---------- the worker and the lid ----------

def test_an_item_cut_off_by_the_mac_sleeping_is_taken_again_not_failed(tmp_path, monkeypatch):
    from thrift_agent import cli
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _processing(db, 1)
    calls, said = [], []
    monkeypatch.setattr(cli.pipeline, "ready_folders", lambda s_, db_=None: [])
    monkeypatch.setattr(cli.alerts.notify, "say", said.append)

    def process(s_, db_, ref):
        calls.append(ref)
        if len(calls) == 1:
            raise ConnectionError("the model call was cut off")
        db_.set_item(ref, status="awaiting_price")

    monkeypatch.setattr(cli.pipeline, "process_item", process)
    monkeypatch.setattr(cli.power, "slept_since", lambda *a: True)          # the lid closed during the call
    cli._tick(s, db)
    assert db.item(iid)["status"] == "new" and said == []                  # not failed, nothing told
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE kind='interrupted_by_sleep'").fetchone()[0] == 1
    monkeypatch.setattr(cli.power, "slept_since", lambda *a: False)
    cli._tick(s, db)
    assert calls == [iid, iid] and db.item(iid)["status"] == "awaiting_price"
    db.set_item(iid, status="new")
    monkeypatch.setattr(cli.pipeline, "process_item", lambda *a: (_ for _ in ()).throw(ValueError("a real bug")))
    cli._tick(s, db)
    assert db.item(iid)["status"] == "failed" and said and said[0].startswith(f"❌ {iid}: ValueError")


def test_with_the_lid_closed_the_worker_starts_nothing(tmp_path, monkeypatch):
    from thrift_agent import cli
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    ticks = []
    monkeypatch.setattr(cli, "_safe_tick", lambda s_, db_: ticks.append(1))
    monkeypatch.setattr(cli, "_safe_pump", lambda s_, db_: ticks.append(2))
    monkeypatch.setattr(cli.time, "sleep", lambda secs: None)
    monkeypatch.setattr(daily.approve, "resend_pending", lambda s_, db_: [])
    lid = {"closed": True}
    w = _window(s, db, lid=lambda: lid["closed"])
    cli._worker_iteration(s, db, 15, w)
    assert ticks == []                                                     # a maintenance wake: nothing started
    lid["closed"] = False
    cli._worker_iteration(s, db, 15, w)
    assert ticks == [1, 2]                                                 # the lid is open: the window works


def test_a_quiet_poster_in_its_pause_or_in_a_long_listing_is_still_running(tmp_path):
    db = DB(tmp_path / "state.db")
    beat = datetime.now(timezone.utc)
    daily.poster_beat(db, live=True, busy=None, next_at=(beat + timedelta(minutes=20)).isoformat(timespec="seconds"))
    assert daily.poster_now(db, beat + timedelta(minutes=15)).running        # a long human pause: quiet, not gone
    assert not daily.poster_now(db, beat + timedelta(minutes=30)).running    # well past it: gone
    daily.poster_beat(db, busy="i_1", next_at=None)
    assert daily.poster_now(db, beat + timedelta(minutes=10)).running        # a long listing
    assert not daily.poster_now(db, beat + timedelta(minutes=20)).running


def test_the_listing_being_published_keeps_the_window_working(tmp_path):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _ready(db, 1)
    db.claim_listing(iid, "poshmark")                                # the last one, being published now
    w = daily.work(s, db)
    assert w.to_publish == 1
    daily.poster_beat(db, live=True, busy=iid)
    text = daily.status_text(w, daily.poster_now(db), TIMING, True, None, "08:00", NOW)
    assert text == "⏳ 1 listing still to publish, publishing now. Please don't close the Mac yet."
