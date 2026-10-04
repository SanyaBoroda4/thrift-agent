"""One worker per machine (WO21): two `thrift run` would both long-poll Telegram and take each other's updates (and
both process the same 'new' rows). The worker holds an OS file lock next to the DB; the OS drops it when the process
ends, however it ends, so a crash never leaves a stale lock behind. Who holds it is written beside it (<lock>.pid)."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import IO


class AlreadyRunning(RuntimeError):
    """Another process holds the lock."""


def hold(lock: Path, what: str = "worker") -> IO[bytes]:
    """Take `lock` for the life of this process, or raise AlreadyRunning naming the holder. Keep the returned file
    open (and referenced) for as long as the lock must be held."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    info = lock.with_name(lock.name + ".pid")
    f = open(lock, "a+b")                              # never truncated: the losing side must not touch it
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        holder = info.read_text(encoding="utf-8").strip() if info.exists() else "pid unknown"
        raise AlreadyRunning(f"another {what} is already running ({holder}). Two would take each other's Telegram "
                             "updates. Stop it first: bash deploy/services.sh stop worker (the launchd service), or "
                             "Control+C in the Terminal window that runs thrift run.") from None
    info.write_text(f"pid {os.getpid()}, since {datetime.now():%Y-%m-%d %H:%M}", encoding="utf-8")
    return f
