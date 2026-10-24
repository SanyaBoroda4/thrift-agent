"""Worker trouble, told once (WO22), and where (WO29: the group stays quiet).

The iCloud inbox that can't be read is one known condition: a read macOS refuses or interrupts (EPERM, EACCES, EINTR
— seen live while python3.14 waited for its iCloud Drive permission), or a scan stuck in that wait. It is retried
quietly; only when it has lasted INBOX_GRACE (10 min) does the GROUP get ONE plain line (the owner must allow it on
the Mac); "✓ inbox readable again" goes to the ops chat. iCloud still downloading or coordinating a share (EDEADLK
"Resource deadlock avoided", seen live on the first automatic share; EAGAIN, ETIMEDOUT) is not trouble at all: retried
quietly, and only if it lasts BUSY_GRACE (10 min) the OPS chat hears once. Any other worker error goes to the ops
chat once, then at most once a day while it keeps happening (`once`). No error text ever reaches the group.

State lives in the kv table, so the worker's two threads and a restart share it."""
from __future__ import annotations

import errno
import json
import os
import re
from datetime import datetime, timedelta, timezone

from thrift_agent import notify
from thrift_agent.db import DB

INBOX_GRACE = 600                         # seconds of quiet retries before the owner hears about the inbox (WO29)
INBOX_MESSAGE = ("⚠️ Can't read the iCloud inbox — on the Mac, open System Settings → Privacy & Security → Files & "
                 "Folders → python3.14 and turn iCloud Drive on; I continue by myself.")
INBOX_BACK = "✓ inbox readable again"
BUSY_GRACE = 600                          # iCloud busy with the inbox this long before the ops chat hears (WO29)
BUSY_SINCE = "icloud_busy_since"          # kv: {what: since when} — the inbox scan, or a batch reading its share
BUSY_TOLD = "icloud_busy_told"            # kv: [what] already told
REPEAT_EVERY = timedelta(days=1)          # an identical error again: at most one message a day

SCAN_STARTED = "worker_inbox_scan_started"   # kv: when the worker started its current look at the inbox
SCAN_DONE = "worker_inbox_scan"              # kv: when it last finished one (thrift status; deploy waits for it)
INBOX_DOWN = "inbox_unreadable_since"        # kv: since when the inbox can't be read
INBOX_TOLD = "inbox_unreadable_told"         # kv: "1" once the owner was told
SENT = "worker_alerts"                       # kv: {error signature: when it was last sent}
WATCH_BEAT = "worker_inbox_beat"             # kv: the watcher's heartbeat — every look at the inbox, whatever it found (WO42)
WATCH_STALL = 15 * 60                        # seconds of AWAKE, lid-open time without progress before the worker restarts
WATCH_RESTART_SAID = "watcher_restart_said"  # kv: when the restart was last told (at most once an hour)
PROGRESS = "worker_progress"                 # kv: WO49 — {"at": wall time, "what": …} of the worker's last progress
# WO49 (live 2026-10-24: the Mac slept 21 hours with the lid closed; the watcher counted it as a stall and restarted a
# healthy worker the moment the lid opened — and while one stuck iCloud folder made every look fail for 71 minutes, it
# saw a fresh beat and said nothing): the watcher's clock is the Mac's monotonic one (mach_absolute_time — it stops
# while the Mac sleeps), and only real progress moves it: a FINISHED look at the inbox, an upload fetched, a batch or an
# item worked on, or a turn with the lid closed (the worker starts nothing then, by design: no progress is expected).
_STATE = {"started": None, "progress": None, "look": None}   # monotonic stamps of this process


def _mono() -> float:
    """Monotonic clock seam (tests move it); on the Mac it stops while the machine sleeps."""
    import time
    return time.monotonic()


def _since(key: str) -> float:
    if _STATE[key] is None:
        _STATE[key] = _mono()
    return _mono() - _STATE[key]


_STATE["started"] = _mono()                  # this process's start: no stall is judged in its first WATCH_STALL
OUTSIDE_STALL = WATCH_STALL + 5 * 60         # WO49: the poster's outside check acts only after the worker's own watcher
WORKER_LABEL = "com.thriftagent.worker"

_IDS = re.compile(r"\b[ib]_\d{6}_[0-9a-f]{6}\b")


def _now() -> datetime:
    """Clock seam (tests move it)."""
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().isoformat(timespec="seconds")


def _age(iso: str | None) -> float:
    if not iso:
        return 0.0
    return (_now() - datetime.fromisoformat(iso)).total_seconds()


def inbox_unreadable(e: BaseException) -> bool:
    """A read of the inbox that macOS refused or interrupted: the permission condition, not a bug."""
    return isinstance(e, OSError) and e.errno in (errno.EINTR, errno.EPERM, errno.EACCES)


def icloud_busy(e: BaseException) -> bool:
    """iCloud still downloading or coordinating a file (EDEADLK "Resource deadlock avoided" — errno 11 on macOS —,
    EAGAIN, ETIMEDOUT): the share isn't ready yet, nothing is wrong (WO29)."""
    return isinstance(e, OSError) and e.errno in {errno.EDEADLK, errno.EAGAIN, errno.ETIMEDOUT}


def busy(db: DB, what: str, why: str, say=None) -> bool:
    """iCloud is busy with `what` (the inbox scan, or a batch): quiet; once it has lasted BUSY_GRACE, ONE line to the
    ops chat. True when that line went out."""
    since = json.loads(db.kv_get(BUSY_SINCE) or "{}")
    if what not in since:
        since[what] = _stamp()
        db.kv_set(BUSY_SINCE, json.dumps(since))
        db.log(None, "icloud_busy", {"what": what, "why": why})
    told = json.loads(db.kv_get(BUSY_TOLD) or "[]")
    if what in told or _age(since[what]) < BUSY_GRACE:
        return False
    (say or notify.say)(f"⚠️ iCloud has been busy with {what} for {int(_age(since[what]) // 60)} min ({why}) — "
                        "still retrying quietly")
    db.kv_set(BUSY_TOLD, json.dumps([*told, what]))
    return True


def not_busy(db: DB, what: str, say=None) -> None:
    """`what` was read: the busy condition is over (one ops line if the ops chat had heard of it)."""
    since = json.loads(db.kv_get(BUSY_SINCE) or "{}")
    if what not in since:
        return
    told = json.loads(db.kv_get(BUSY_TOLD) or "[]")
    since.pop(what)
    db.kv_set(BUSY_SINCE, json.dumps(since))
    if what in told:
        db.kv_set(BUSY_TOLD, json.dumps([w for w in told if w != what]))
        (say or notify.say)(f"✓ iCloud free again: {what}")


def inbox_trouble(db: DB, why: str, since: str | None = None, say=None) -> bool:
    """The inbox can't be read right now. Quiet until the condition has lasted INBOX_GRACE (counted from `since`, the
    first failure by default), then ONE message; never again until it has recovered. True when the message went out."""
    if not db.kv_get(INBOX_DOWN):
        db.kv_set(INBOX_DOWN, since or _stamp())
        db.log(None, "inbox_unreadable", why)
    if db.kv_get(INBOX_TOLD) == "1" or _age(db.kv_get(INBOX_DOWN)) < INBOX_GRACE:
        return False
    (say or notify.group)(INBOX_MESSAGE)                  # the owner must act on the Mac: a plain line, the group
    db.kv_set(INBOX_TOLD, "1")
    db.log(None, "inbox_alert", why)
    return True


def inbox_ok(db: DB, say=None) -> bool:
    """The inbox was read: clears the condition, with "✓ inbox readable again" if the owner had been told."""
    if not db.kv_get(INBOX_DOWN):
        return False
    told = db.kv_get(INBOX_TOLD) == "1"
    db.conn.execute("DELETE FROM kv WHERE key IN (?, ?)", (INBOX_DOWN, INBOX_TOLD))
    db.log(None, "inbox_readable", {"told": told})
    if told:
        (say or notify.say)(INBOX_BACK)
    return told


def scan_started(db: DB) -> None:
    db.kv_set(SCAN_STARTED, _stamp())
    _STATE["look"] = _mono()


def scan_done(db: DB) -> None:
    """A look at the inbox that finished — the watcher's progress (WO49)."""
    db.kv_set(SCAN_DONE, _stamp())
    beat(db)
    progress(db, "inbox look")


def look_ended(db: DB) -> None:
    """WO49: a look that ended early on iCloud's word for the whole inbox (busy, unreadable — told by their own lines):
    the loop is alive and will look again, so the watcher doesn't restart it; `thrift status`'s last finished scan
    stays as it was."""
    beat(db)
    progress(db, "inbox look (iCloud busy)")


def beat(db: DB) -> None:
    """The inbox watcher's heartbeat (WO42): the worker's look at the inbox ran, whatever it found or iCloud said."""
    db.kv_set(WATCH_BEAT, _stamp())
    _STATE["look"] = None


def progress(db: DB | None, what: str) -> None:
    """WO49: the worker moved on — the watcher's clock starts again, and kv `worker_progress` (the poster's outside
    check, the heartbeat to thrift-api, `thrift doctor`) says what and when."""
    _STATE["progress"] = _mono()
    if db is not None:
        db.kv_set(PROGRESS, json.dumps({"at": _stamp(), "what": what}))


def idle(db: DB | None) -> None:
    """WO49: a turn with the lid closed — nothing is started by design, so no progress is owed (the night is no stall)."""
    progress(db, "lid closed")


def watcher_stalled(db: DB, now: datetime | None = None) -> str | None:
    """WO42/WO49: why the worker looks stalled, in AWAKE lid-open time (the monotonic clock; the caller checks the
    lid) — a look at the inbox started WATCH_STALL ago that never finished, or no progress for WATCH_STALL — else
    None. Never in the first WATCH_STALL of this process. `now` is unused (kept for the callers' signature)."""
    if _since("started") < WATCH_STALL:
        return None
    if _STATE["look"] is not None and _mono() - _STATE["look"] >= WATCH_STALL:
        return f"a look at the inbox started at {db.kv_get(SCAN_STARTED) or '?'} never finished"
    if _since("progress") >= WATCH_STALL:
        last = loads_dict(db.kv_get(PROGRESS)).get("at") or db.kv_get(SCAN_DONE) or "?"
        return f"no progress since {last}"
    return None


def loads_dict(text: str | None) -> dict:
    try:
        value = json.loads(text) if text else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def restart_watcher(db: DB, why: str, leave=None) -> None:
    """WO42: ONE private line (at most one an hour), the event, and the process ended with status 3 — launchd starts
    the worker again (KeepAlive). `os._exit`, not SystemExit: the main thread may be the one that hangs."""
    db.log(None, "watcher_restarted", why)
    said = db.kv_get(WATCH_RESTART_SAID)
    if not said or _age(said) >= 3600:
        db.kv_set(WATCH_RESTART_SAID, _stamp())
        notify.say(f"⚠️ The inbox watcher stalled ({why}) — restarting the worker. Shares are taken again in a minute.")
    (leave or os._exit)(3)


def _kickstart_worker() -> None:
    """launchd restarts the worker service (kill + start: `launchctl kickstart -k`). The Mac only."""
    import platform
    import subprocess
    if platform.system() != "Darwin":
        return
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{WORKER_LABEL}"], check=False,
                   capture_output=True, timeout=30)


class WorkerCheck:
    """WO49: the worker watched from OUTSIDE its process — the poster's housekeeping (another launchd service). The
    worker's kv `worker_progress` unchanged for OUTSIDE_STALL of the POSTER's monotonic time with the lid open (the
    lid closed resets it; the Mac asleep doesn't count) → launchd restarts the worker (`kickstart -k`: a thread stuck
    in the worker can't stop that), one event, ONE private line an hour at most. The worker's own watcher (its
    Telegram thread, WATCH_STALL) normally acts first; this one covers a worker whose threads are all stuck."""

    def __init__(self, restart=None):
        self.restart = restart or _kickstart_worker
        self.seen: str | None = None
        self.since: float | None = None

    def tick(self, db: DB, lid_closed: bool | None) -> str | None:
        value, now = db.kv_get(PROGRESS), _mono()
        if lid_closed or self.since is None or value != self.seen:
            self.seen, self.since = value, now
            return None
        if now - self.since < OUTSIDE_STALL:
            return None
        self.since = now                                 # one restart per stall window
        why = f"no progress since {loads_dict(value).get('at') or '?'}"
        db.log(None, "worker_restarted_from_poster", why)
        said = db.kv_get(WATCH_RESTART_SAID)
        if not said or _age(said) >= 3600:
            db.kv_set(WATCH_RESTART_SAID, _stamp())
            notify.say(f"⚠️ The worker made no progress for {OUTSIDE_STALL // 60} min ({why}) — the poster restarted it.")
        self.restart()
        return why


def scan_stuck(db: DB) -> str | None:
    """When the current look at the inbox started, if it has been running for INBOX_GRACE or more (a read waiting on
    macOS's permission prompt never returns or raises); else None. Checked from the worker's Telegram thread."""
    started, done = db.kv_get(SCAN_STARTED), db.kv_get(SCAN_DONE)
    if started and (not done or done < started) and _age(started) >= INBOX_GRACE:
        return started
    return None


def signature(text: str) -> str:
    """Identical errors are identical apart from the batch/item they hit."""
    return _IDS.sub("<id>", text)


def once(db: DB, text: str, say=None) -> bool:
    """Send `text` unless the same error (signature) went out less than REPEAT_EVERY ago. True when sent. Every
    occurrence is still in the event log."""
    sig = signature(text)
    sent = json.loads(db.kv_get(SENT) or "{}")
    last = sent.get(sig)
    if last and _now() - datetime.fromisoformat(last) < REPEAT_EVERY:
        return False
    sent = {k: v for k, v in sent.items() if _now() - datetime.fromisoformat(v) < timedelta(days=7)}
    sent[sig] = _stamp()
    db.kv_set(SENT, json.dumps(sent))
    (say or notify.say)(text)
    return True
