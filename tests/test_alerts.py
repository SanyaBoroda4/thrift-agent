"""WO22: the iCloud inbox that can't be read is told once (and its recovery); any repeated identical worker error at
most once a day. No network: notify.say is replaced, the clock is moved."""
import errno
from datetime import datetime, timedelta, timezone

import pytest

from thrift_agent import alerts, cli
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB

T0 = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path, monkeypatch):
    base = settings().data
    data = {**base, "machine_role": "dev", "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    s = Settings(data)
    s.ensure_dirs()
    said, clock = [], {"now": T0}
    monkeypatch.setattr(alerts.notify, "say", said.append)
    monkeypatch.setattr(alerts, "_now", lambda: clock["now"])
    return s, DB(s.path("db")), said, clock


def _inbox_fails(monkeypatch, error):
    def ready_folders(s):
        raise error
    monkeypatch.setattr(cli.pipeline, "ready_folders", ready_folders)


@pytest.mark.parametrize("error", [InterruptedError(errno.EINTR, "Interrupted system call", "/Posh/inbox"),
                                   PermissionError(errno.EPERM, "Operation not permitted", "/Posh/inbox"),
                                   PermissionError(errno.EACCES, "Permission denied", "/Posh/inbox")])
def test_an_unreadable_inbox_is_retried_quietly_then_told_once_and_its_recovery(env, monkeypatch, error):
    s, db, said, clock = env
    _inbox_fails(monkeypatch, error)
    cli._safe_tick(s, db)                                         # the first failure: quiet
    clock["now"] = T0 + timedelta(seconds=90)
    cli._safe_tick(s, db)                                         # 90 s: still quiet
    assert said == []
    clock["now"] = T0 + timedelta(seconds=125)
    cli._safe_tick(s, db)                                         # past 2 min: ONE plain message
    assert said == [alerts.INBOX_MESSAGE]
    for minutes in (5, 40, 80, 600):                              # never repeated while it lasts
        clock["now"] = T0 + timedelta(minutes=minutes)
        cli._safe_tick(s, db)
    assert said == [alerts.INBOX_MESSAGE]
    assert "System Settings → Privacy & Security → Files & Folders → python3.14 → iCloud Drive ON" in said[0]

    monkeypatch.setattr(cli.pipeline, "ready_folders", lambda s: [])
    cli._safe_tick(s, db)                                         # readable again: said once
    cli._safe_tick(s, db)
    assert said == [alerts.INBOX_MESSAGE, alerts.INBOX_BACK] and db.kv_get(alerts.SCAN_DONE)

    _inbox_fails(monkeypatch, error)                              # broken again: a new message once it lasts
    t1 = clock["now"]
    cli._safe_tick(s, db)
    clock["now"] = t1 + timedelta(minutes=3)
    cli._safe_tick(s, db)
    assert said == [alerts.INBOX_MESSAGE, alerts.INBOX_BACK, alerts.INBOX_MESSAGE]


def test_a_short_hiccup_says_nothing_at_all(env, monkeypatch):
    s, db, said, clock = env
    _inbox_fails(monkeypatch, InterruptedError(errno.EINTR, "Interrupted system call"))
    cli._safe_tick(s, db)
    monkeypatch.setattr(cli.pipeline, "ready_folders", lambda s: [])
    clock["now"] = T0 + timedelta(seconds=15)
    cli._safe_tick(s, db)
    assert said == [] and db.kv_get(alerts.INBOX_DOWN) is None


def test_items_are_still_processed_while_the_inbox_cannot_be_read(env, monkeypatch):
    s, db, said, clock = env
    _inbox_fails(monkeypatch, PermissionError(errno.EPERM, "Operation not permitted"))
    bid = db.add_batch("share", 1)
    db.set_batch(bid, status="split")
    iid = db.add_item(bid, 1, "item")
    done = []
    monkeypatch.setattr(cli.pipeline, "process_item", lambda s_, db_, ref: done.append(ref) or db_.set_item(
        ref, status="awaiting_price"))
    cli._safe_tick(s, db)
    assert done == [iid]


def test_a_scan_stuck_on_the_permission_prompt_is_told_by_the_telegram_thread(env, monkeypatch):
    """Live (WO21): the read waits on macOS's prompt and neither returns nor raises; the other thread notices."""
    s, db, said, clock = env
    alerts.scan_started(db)                                       # the main thread went into the inbox...
    monkeypatch.setattr(cli.approve, "poll_once", lambda *a: 0)
    monkeypatch.setattr(cli.approve, "pump", lambda *a: None)
    state = {"last_resend": cli.time.monotonic()}
    clock["now"] = T0 + timedelta(seconds=60)
    cli._telegram_iteration(s, db, object(), state)
    assert said == []
    clock["now"] = T0 + timedelta(seconds=130)                    # ...and hasn't come back for over 2 min
    cli._telegram_iteration(s, db, object(), state)
    cli._telegram_iteration(s, db, object(), state)
    assert said == [alerts.INBOX_MESSAGE]
    alerts.scan_done(db)                                          # Allow was clicked: the scan ends
    alerts.inbox_ok(db)
    assert said == [alerts.INBOX_MESSAGE, alerts.INBOX_BACK]


def test_another_error_on_the_inbox_is_a_normal_error_told_once(env, monkeypatch):
    s, db, said, clock = env
    _inbox_fails(monkeypatch, FileNotFoundError(errno.ENOENT, "No such file or directory", "/Posh/inbox"))
    cli._safe_tick(s, db)
    cli._safe_tick(s, db)
    assert said == ["❌ worker tick: FileNotFoundError: [Errno 2] No such file or directory: '/Posh/inbox'"]
    assert db.kv_get(alerts.INBOX_DOWN) is None


def test_a_repeated_identical_error_goes_out_once_then_once_a_day(env):
    s, db, said, clock = env
    for minutes in (0, 15, 40, 300):
        clock["now"] = T0 + timedelta(minutes=minutes)
        alerts.once(db, "❌ worker tick: RuntimeError: boom")
    assert said == ["❌ worker tick: RuntimeError: boom"]
    clock["now"] = T0 + timedelta(hours=24, minutes=1)            # still happening a day later: once more
    alerts.once(db, "❌ worker tick: RuntimeError: boom")
    alerts.once(db, "❌ worker tick: RuntimeError: other")        # a different error is its own message
    assert said == ["❌ worker tick: RuntimeError: boom"] * 2 + ["❌ worker tick: RuntimeError: other"]


def test_many_items_failing_on_the_same_error_are_one_message(env):
    s, db, said, clock = env
    for iid in ("i_261004_aaaaaa", "i_261004_bbbbbb", "i_261004_cccccc"):
        cli._guard(db, iid, lambda: (_ for _ in ()).throw(RuntimeError("invalid x-api-key")), lambda: None)
    assert said == ["❌ i_261004_aaaaaa: RuntimeError: invalid x-api-key"]
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE kind='error'").fetchone()[0] == 3   # all logged
