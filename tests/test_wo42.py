"""WO42 (2026-10-17): every share ends in a card or a clear message. Live: a share at 3:18 PM whose 11 photos never
reached iCloud — the folder came with its `_done.txt` and nothing else, two more runs made two empty folders, the
worker waited 30 minutes, said one technical line in the private chat and the group heard nothing for four hours.
- cloud-only files are pulled (brctl download, a read through the File Provider), with back-off;
- the Shortcut's count is matched against the photos AND their companions (a Live Photo's video);
- 15 minutes: a share short of its count (or without its end mark) is taken with the camera photos that came, one
  private line; 30 minutes with no camera photo: ONE group line "share it again", the folder archived with a note;
- empty and leftover folders are archived, never deleted, and never block the next share;
- the inbox watcher has a heartbeat; stalled 15 minutes with the lid open → one private line and a restart."""
import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from thrift_agent import alerts, cli, pipeline
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB
from thrift_agent.ingest import prep

T0 = 1_800_000_000.0
MIN = 60


@pytest.fixture
def env(tmp_path, monkeypatch):
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    s = Settings(data)
    s.ensure_dirs()
    db = DB(s.path("db"))
    clock = {"now": T0, "born": {}}
    monkeypatch.setattr(pipeline.time, "time", lambda: clock["now"])
    monkeypatch.setattr(pipeline, "appeared", lambda p: clock["born"].get(str(p), T0))
    monkeypatch.setattr(pipeline, "arrived", lambda p: clock["born"].get(str(p), T0))
    said, group = [], []
    monkeypatch.setattr(pipeline.notify, "say", said.append)
    monkeypatch.setattr(pipeline.notify, "group", group.append)
    return s, db, clock, said, group


def share(s, clock, name, photos=0, screenshots=0, media=(), marker=None, at=T0):
    """A share folder as iCloud delivers it: camera photos (IMG_n.jpeg), screenshots (a phone-screen ratio), other
    media (a Live Photo's .mov), the Shortcut's end mark — every file 'arrived' at `at`."""
    d = s.path("inbox") / name
    d.mkdir(parents=True)
    clock["born"][str(d)] = at
    for i in range(photos):
        Image.new("RGB", (30, 40), "red").save(d / f"IMG_{i + 1}.jpeg")
    for i in range(screenshots):
        Image.new("RGB", (90, 195), "white").save(d / f"IMG_{photos + i + 1}.PNG")
    for m in media:
        (d / m).write_bytes(b"\0" * 10)
    if marker is not None:
        (d / "_done.txt").write_text(marker, encoding="utf-8")
    for f in d.iterdir():
        clock["born"][str(f)] = at
    return d


def test_the_shortcuts_count_is_matched_against_photos_and_their_companions(env):
    s, db, clock, said, group = env
    d = share(s, clock, "2026-10-17_151834", photos=2, media=("IMG_1.mov", "IMG_2.mov"), marker="done 4")
    marker = d / "_done.txt"
    clock["now"] = T0 + 3 * MIN
    assert pipeline.share_state(s, d, marker, 60) == "ready"           # 2 Live Photos: 4 files for "done 4"
    e = share(s, clock, "2026-10-17_151900", photos=2, marker="done 4")
    assert pipeline.share_state(s, e, e / "_done.txt", 60) == "wait"   # 2 of 4 photos: not yet
    clock["now"] = T0 + 16 * MIN
    assert pipeline.share_state(s, e, e / "_done.txt", 60) == "wait"      # WO45: not at 15 min any more
    clock["now"] = T0 + 31 * MIN
    assert pipeline.share_state(s, e, e / "_done.txt", 60) == "partial"   # 30 min: taken with the two


def test_three_of_four_photos_make_a_card_and_one_private_line(env):
    s, db, clock, said, group = env
    d = share(s, clock, "2026-10-17_151834", photos=3, marker="done 4")
    clock["now"] = T0 + 5 * MIN
    assert pipeline.ready_folders(s, db) == []
    clock["now"] = T0 + 16 * MIN
    assert pipeline.ready_folders(s, db) == []                              # WO45 §6: no partial card in 30 min
    clock["now"] = T0 + 31 * MIN
    assert pipeline.ready_folders(s, db) == [d]
    assert pipeline.take_share(s, db, d) == ("partial", T0)
    [b] = db.batches("new")
    assert b["n_photos"] == 3 and json.loads(db.kv_get("late_photos_seen") or "[]") == []
    assert said == ["📸 3 of 4 photos of your 3:18 PM share arrived — the card is made from those; the rest are told "
                    "if they come."]
    assert group == []
    assert pipeline.take_share(s, db, d) is None and len(said) == 1       # once: the batch is known


def test_photos_without_the_end_mark_are_taken_after_30_minutes(env):
    s, db, clock, said, group = env
    d = share(s, clock, "2026-10-17_151932", photos=4)                 # the Shortcut never wrote _done
    clock["now"] = T0 + 29 * MIN
    assert pipeline.ready_folders(s, db) == []
    clock["now"] = T0 + 30 * MIN + 1
    assert pipeline.take_share(s, db, d) == ("partial", T0)
    assert said == ["📸 Only 4 photos of your 3:19 PM share arrived and the Shortcut's end mark never came — the card "
                    "is made from those; the rest are told if they come."]


def test_a_share_with_no_photos_after_30_minutes_is_told_once_in_the_group_and_waits(env):
    s, db, clock, said, group = env
    d = share(s, clock, "2026-10-17_151834", marker="done 11")
    clock["now"] = T0 + 29 * MIN
    assert pipeline.ready_folders(s, db) == []
    clock["now"] = T0 + 31 * MIN
    assert pipeline.ready_folders(s, db) == [d]
    assert cli._scan_inbox(s, db) == 0
    assert group == ["📭 Photos from your 3:18 PM share are still on the iPhone — unlock it on Wi-Fi and they'll come "
                     "through. No need to share again."]                 # WO43: never "share it again"
    assert said == []
    assert d.exists() and list(s.path("archive").iterdir()) == []      # left where it is: its photos may still come
    assert db.batches("failed") == []
    assert cli._scan_inbox(s, db) == 0 and len(group) == 1             # never repeated
    assert db.kv_get(alerts.WATCH_BEAT)                                # the watcher's heartbeat
    clock["now"] = T0 + 25 * 3600                                      # a day: archived with the note, no line
    cli._scan_inbox(s, db)
    assert not d.exists()
    [arch] = list(s.path("archive").iterdir())
    assert arch.name.endswith("2026-10-17_151834") and (arch / "_done.txt").is_file()
    assert "stayed on the iPhone" in (arch / pipeline.LOST_NOTE).read_text(encoding="utf-8")
    [b] = db.batches("failed")
    assert json.loads(b["reasons"]) == ["no photos arrived"] and b["n_photos"] == 0
    assert len(group) == 1 and len(said) == 1                          # the 3-hour private line came in between


def test_empty_and_leftover_folders_are_one_line_and_never_block_the_next_share(env):
    s, db, clock, said, group = env
    share(s, clock, "2026-10-17_151932")                               # two runs that wrote nothing at all
    share(s, clock, "2026-10-17_151954", at=T0 + 22)
    share(s, clock, "2026-10-17_152010", screenshots=1, marker="done 3", at=T0 + 40)   # only its screenshot came: no card
    clock["now"] = T0 + 31 * MIN
    fresh = share(s, clock, "2026-10-17_154900", photos=3, marker="done 3", at=T0 + 28 * MIN)
    assert cli._scan_inbox(s, db) == 1                                 # the real share is taken …
    assert [b["n_photos"] for b in db.batches("new")] == [3] and fresh.exists()
    assert group == ["📭 Photos from your 3:19 PM and 3:20 PM shares are still on the iPhone — unlock it on Wi-Fi and "
                     "they'll come through. No need to share again."]
    assert list(s.path("archive").iterdir()) == []                     # … the rest wait where they are (WO43)
    assert cli._scan_inbox(s, db) == 0 and len(group) == 1


def test_photos_landing_in_an_archived_folder_are_taken_where_they_are(env):
    s, db, clock, said, group = env
    d = share(s, clock, "2026-10-17_151834", marker="done 3", at=T0 - 25 * 3600)   # a day old: archived
    clock["now"] = T0
    cli._scan_inbox(s, db)
    [arch] = list(s.path("archive").iterdir())
    for i in (1, 2, 3):
        Image.new("RGB", (30, 40)).save(arch / f"IMG_{i}.jpeg")         # the phone caught up, a day later
    [done] = pipeline.late_photos(s, db)
    assert done.startswith(f"{db.batches('failed')[0]['id']}: retaken as b_")
    assert arch.exists() and not d.exists() and not (s.path("inbox") / "2026-10-17_151834-late").exists()
    assert said[-1].startswith("📸 3 photos of the 3:18 PM share came late — taken now")


def test_a_share_empty_for_hours_is_old_news_for_the_group(env):
    s, db, clock, said, group = env
    share(s, clock, "2026-10-17_151834", marker="done 11", at=T0 - 3 * 3600 - 1)   # the Mac was closed meanwhile
    clock["now"] = T0
    cli._scan_inbox(s, db)
    assert group == [] and said == ["📭 Still no photos from the 3:18 PM share after 3 hours — the iPhone hasn't "
                                    "uploaded them (Wi-Fi, Low Power Mode, iCloud storage?). The folder waits; "
                                    "nothing to share again."]


def test_cloud_only_files_are_pulled_with_backoff_and_the_card_comes(env, monkeypatch):
    s, db, clock, said, group = env
    d = share(s, clock, "2026-10-17_151834", photos=1, marker="done 2")
    (d / ".IMG_2.jpeg.icloud").touch()                                 # one photo still in the cloud
    runs = []
    monkeypatch.setattr(pipeline.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(pipeline.subprocess, "run", lambda cmd, **k: runs.append(cmd))
    clock["now"] = T0 + 3 * MIN
    assert pipeline.ready_folders(s, db) == [] and runs == [["brctl", "download", str(d)]]
    assert pipeline.ready_folders(s, db) == [] and len(runs) == 1          # asked 15 s ago: back-off
    clock["now"] += 16
    pipeline.ready_folders(s, db)
    assert len(runs) == 2
    clock["now"] += 31                                                     # the third ask also reads the file
    (d / ".IMG_2.jpeg.icloud").unlink()
    Image.new("RGB", (30, 40)).save(d / "IMG_2.jpeg")
    monkeypatch.setattr(prep, "dataless", lambda p: p.name == "IMG_2.jpeg")
    pipeline.ready_folders(s, db)
    assert runs[2:] == [["brctl", "download", str(d)], ["brctl", "download", str(d / "IMG_2.jpeg")],
                        ["head", "-c", "1", str(d / "IMG_2.jpeg")]]
    monkeypatch.setattr(prep, "dataless", lambda p: False)                 # it came
    clock["born"][str(d / "IMG_2.jpeg")] = clock["now"]
    clock["now"] += 2 * MIN
    assert pipeline.ready_folders(s, db) == [d]
    assert pipeline.take_share(s, db, d) == ("ready", T0) and said == [] and group == []


def test_a_slow_read_never_hangs_the_worker(env, monkeypatch):
    s, db, clock, said, group = env
    d = share(s, clock, "2026-10-17_151834", photos=1)
    monkeypatch.setattr(pipeline.platform, "system", lambda: "Darwin")

    def run(cmd, **k):
        if cmd[0] == "head":
            raise subprocess.TimeoutExpired(cmd, k.get("timeout"))
    monkeypatch.setattr(pipeline.subprocess, "run", run)
    db.kv_set(pipeline.PULL, json.dumps({str(d): {"n": 2, "next": 0}}))
    assert pipeline.pull_from_icloud(db, d, [d / "IMG_1.jpeg"]) is True
    assert json.loads(db.kv_get(pipeline.PULL))[str(d)]["n"] == 3


# ---------------------------------------------------------------- the watcher's heartbeat

def _stamp(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def test_the_watcher_restarts_when_it_stalls_with_one_private_line(env, monkeypatch):
    """WO42, as WO49 made it: the stall is AWAKE time on the monotonic clock (it stops while the Mac sleeps) without
    progress — a finished look, a job worked, an upload fetched, a lid-closed turn — never wall time since a beat."""
    s, db, clock, said, group = env
    mono = {"t": 10_000.0}
    monkeypatch.setattr(alerts, "_mono", lambda: mono["t"])
    monkeypatch.setattr(alerts, "_STATE", {"started": mono["t"] - 3600, "progress": None, "look": None})
    lines, left = [], []
    monkeypatch.setattr(alerts.notify, "say", lines.append)
    alerts.scan_started(db)
    alerts.scan_done(db)                                                   # a finished look: progress
    mono["t"] += 14 * 60
    assert alerts.watcher_stalled(db) is None
    alerts.progress(db, "item i_261017_abc123")                           # busy with an item: progress
    mono["t"] += 14 * 60
    assert alerts.watcher_stalled(db) is None
    mono["t"] += 2 * 60
    why = alerts.watcher_stalled(db)
    assert why and why.startswith("no progress since")
    alerts.restart_watcher(db, why, leave=left.append)
    alerts.restart_watcher(db, why, leave=left.append)
    assert left == [3, 3] and len(lines) == 1 and lines[0].startswith("⚠️ The inbox watcher stalled")
    restarts = db.conn.execute("SELECT count(*) FROM events WHERE kind='watcher_restarted'").fetchone()[0]
    assert restarts == 2
    alerts.progress(db, "x")
    alerts.scan_started(db)                                                # a look that never came back
    mono["t"] += 16 * 60
    assert alerts.watcher_stalled(db).startswith("a look at the inbox started")
    monkeypatch.setattr(alerts, "_STATE", {"started": mono["t"] - 5 * 60, "progress": None, "look": None})
    assert alerts.watcher_stalled(db) is None                              # just restarted: nothing judged yet


def test_a_night_with_the_lid_closed_is_no_stall(env, monkeypatch):
    """WO49 (live 2026-10-24: the Mac slept 21 hours; the watcher restarted a healthy worker the moment the lid opened,
    "no look at the inbox since 2026-10-23T16:02"): the lid-closed turns are progress (nothing is owed), and the sleep
    itself never moves the monotonic clock."""
    s, db, clock, said, group = env
    mono = {"t": 50_000.0}
    monkeypatch.setattr(alerts, "_mono", lambda: mono["t"])
    monkeypatch.setattr(alerts, "_STATE", {"started": mono["t"] - 3600, "progress": mono["t"], "look": None})
    for _ in range(40):                                                    # dark wakes, lid closed: 15 s turns
        mono["t"] += 15
        alerts.idle(db)
    mono["t"] += 60                                                        # the lid opens: the first turn comes
    assert alerts.watcher_stalled(db) is None
    assert json.loads(db.kv_get(alerts.PROGRESS))["what"] == "lid closed"


def test_a_look_that_fails_on_icloud_keeps_the_loop_alive(env, monkeypatch):
    """WO49: a look iCloud cut short for the whole inbox (busy) still ends — the loop is alive, no restart; but only
    a FINISHED look moves `thrift status`'s last scan."""
    s, db, clock, said, group = env
    mono = {"t": 90_000.0}
    monkeypatch.setattr(alerts, "_mono", lambda: mono["t"])
    monkeypatch.setattr(alerts, "_STATE", {"started": mono["t"] - 3600, "progress": mono["t"], "look": None})
    for _ in range(80):                                                    # 20 minutes of busy looks
        alerts.scan_started(db)
        mono["t"] += 15
        alerts.look_ended(db)
    assert alerts.watcher_stalled(db) is None
    assert db.kv_get(alerts.SCAN_DONE) is None


def test_the_poster_restarts_a_worker_that_makes_no_progress(env, monkeypatch):
    """WO49: the worker watched from OUTSIDE its process — kv worker_progress unchanged for OUTSIDE_STALL of the
    poster's awake lid-open time → launchd restarts it, one private line an hour; the lid closed resets the clock."""
    s, db, clock, said, group = env
    mono = {"t": 1_000.0}
    monkeypatch.setattr(alerts, "_mono", lambda: mono["t"])
    lines, restarts = [], []
    monkeypatch.setattr(alerts.notify, "say", lines.append)
    check = alerts.WorkerCheck(restart=lambda: restarts.append(1))
    alerts.progress(db, "inbox look")
    assert check.tick(db, False) is None
    mono["t"] += alerts.OUTSIDE_STALL - 60
    assert check.tick(db, False) is None
    mono["t"] += 30
    assert check.tick(db, True) is None                                    # the lid closed: the clock starts again
    mono["t"] += alerts.OUTSIDE_STALL - 60
    assert check.tick(db, False) is None and restarts == []
    mono["t"] += 120
    why = check.tick(db, False)
    assert why and why.startswith("no progress since") and restarts == [1] and len(lines) == 1
    assert "the poster restarted it" in lines[0]
    alerts.progress(db, "item i_261024_aaaaaa")                            # it moves again: nothing
    mono["t"] += 60
    assert check.tick(db, False) is None and restarts == [1]


def test_the_telegram_thread_restarts_the_watcher_only_with_the_lid_open(env, monkeypatch):
    s, db, clock, said, group = env
    monkeypatch.setattr(cli.approve, "poll_once", lambda *a: 0)
    monkeypatch.setattr(cli.approve, "pump", lambda *a: None)
    monkeypatch.setattr(cli.alerts, "watcher_stalled", lambda db_, now=None: "no look at the inbox since …")
    restarts = []
    monkeypatch.setattr(cli.alerts, "restart_watcher", lambda db_, why: restarts.append(why))
    state = {"last_resend": cli.time.monotonic()}
    monkeypatch.setattr(cli.power, "lid_closed", lambda: True)
    cli._telegram_iteration(s, db, object(), state)
    assert restarts == []
    monkeypatch.setattr(cli.power, "lid_closed", lambda: False)
    cli._telegram_iteration(s, db, object(), state)
    assert restarts == ["no look at the inbox since …"]


def test_share_time_is_the_shortcuts_own(env):
    s, db, clock, said, group = env
    assert pipeline.share_time(Path("2026-10-17_151834")) == "3:18 PM"
    assert pipeline.share_time(Path("2026-10-17_0905")) == "9:05 AM"
    assert pipeline.share_time(Path("2026-10-17_0005")) == "12:05 AM"
