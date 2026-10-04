"""Worker trouble, told once (WO22).

The iCloud inbox that can't be read is one known condition: a read macOS refuses or interrupts (EPERM, EACCES, EINTR
— seen live while python3.14 waited for its iCloud Drive permission), or a scan stuck in that wait. It is retried
quietly; only when it has lasted INBOX_GRACE does the owner get ONE message, and "✓ inbox readable again" when it
recovers. Any other worker error is sent once, then at most once a day while it keeps happening (`once`).

State lives in the kv table, so the worker's two threads and a restart share it."""
from __future__ import annotations

import errno
import json
import re
from datetime import datetime, timedelta, timezone

from thrift_agent import notify
from thrift_agent.db import DB

INBOX_GRACE = 120                         # seconds of quiet retries before the owner hears about the inbox
INBOX_MESSAGE = ("⚠️ Can't read the iCloud inbox. On the Mac: System Settings → Privacy & Security → Files & Folders "
                 "→ python3.14 → iCloud Drive ON (or Full Disk Access). Then I continue by myself.")
INBOX_BACK = "✓ inbox readable again"
REPEAT_EVERY = timedelta(days=1)          # an identical error again: at most one message a day

SCAN_STARTED = "worker_inbox_scan_started"   # kv: when the worker started its current look at the inbox
SCAN_DONE = "worker_inbox_scan"              # kv: when it last finished one (thrift status; deploy waits for it)
INBOX_DOWN = "inbox_unreadable_since"        # kv: since when the inbox can't be read
INBOX_TOLD = "inbox_unreadable_told"         # kv: "1" once the owner was told
SENT = "worker_alerts"                       # kv: {error signature: when it was last sent}

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


def inbox_trouble(db: DB, why: str, since: str | None = None, say=None) -> bool:
    """The inbox can't be read right now. Quiet until the condition has lasted INBOX_GRACE (counted from `since`, the
    first failure by default), then ONE message; never again until it has recovered. True when the message went out."""
    if not db.kv_get(INBOX_DOWN):
        db.kv_set(INBOX_DOWN, since or _stamp())
        db.log(None, "inbox_unreadable", why)
    if db.kv_get(INBOX_TOLD) == "1" or _age(db.kv_get(INBOX_DOWN)) < INBOX_GRACE:
        return False
    (say or notify.say)(INBOX_MESSAGE)
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


def scan_done(db: DB) -> None:
    db.kv_set(SCAN_DONE, _stamp())


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
