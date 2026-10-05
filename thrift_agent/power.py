"""The Mac's power state for the daily window (WO28): the battery, the lid, whether it slept, and a "don't idle-sleep"
assertion held only while there is work.

The owner opens the Mac about once a day, often on battery, for ~30 minutes, then closes the lid. Every macOS call
here is one small read-only subprocess (pmset, ioreg, sysctl) or `caffeinate`; on any other system they answer
"unknown" (None) and hold nothing, so dev and the tests never depend on them. Closing the lid still sleeps the Mac —
that is macOS; the daily window's messages and the poster's closet check after a wake cover it."""
from __future__ import annotations

import os
import platform
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from thrift_agent.db import DB

DARWIN = platform.system() == "Darwin"
LOW_PCT = 15          # on battery below this, with work left: no new publish (the listing in hand is finished)
RESUME_PCT = 20       # ... until the Mac is charging or back at this level
WAKE_GAP_S = 90       # the wall clock ran this much longer than the process did: it slept (any system)
BATTERY_KEY = "battery_low_since"     # kv: since when publishing waits for the charger (an episode)
BATTERY_LOW = "🔋 Mac battery low — plug in or I'll pause; nothing will be lost"


@dataclass(frozen=True)
class Battery:
    percent: int
    on_ac: bool                       # drawing from AC power (charging, or full on the charger)


def _run(*args: str, timeout: float = 5.0) -> str | None:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def parse_batt(text: str | None) -> Battery | None:
    """`pmset -g batt`: "Now drawing from 'Battery Power'\\n -InternalBattery-0 (id=…)\\t84%; discharging; …"."""
    m = re.search(r"\b(\d{1,3})%", text or "")
    if not m:
        return None                   # a Mac without a battery, or no answer
    return Battery(min(int(m[1]), 100), "'AC Power'" in (text or ""))


def battery() -> Battery | None:
    return parse_batt(_run("pmset", "-g", "batt")) if DARWIN else None


def parse_clamshell(text: str | None) -> bool | None:
    """`ioreg -r -k AppleClamshellState -d 1`: "AppleClamshellState" = Yes when the lid is closed."""
    m = re.search(r'"AppleClamshellState"\s*=\s*(Yes|No)\b', text or "")
    return None if m is None else m[1] == "Yes"


def lid_closed() -> bool | None:
    """True with the lid closed (the Mac asleep, or one of its short maintenance wakes at night), False when open,
    None when unknown (not a laptop, not a Mac)."""
    return parse_clamshell(_run("ioreg", "-r", "-k", "AppleClamshellState", "-d", "1")) if DARWIN else None


def parse_waketime(text: str | None) -> float | None:
    """`sysctl -n kern.waketime`: "{ sec = 1728140000, usec = 123456 } Sat Oct  5 10:00:00 2026"."""
    m = re.search(r"\bsec\s*=\s*(\d+)", text or "")
    return float(m[1]) if m and int(m[1]) > 0 else None


def last_wake() -> float | None:
    """The epoch second the Mac last woke from sleep (kern.waketime), or None."""
    return parse_waketime(_run("sysctl", "-n", "kern.waketime")) if DARWIN else None


class WakeWatch:
    """Notices that the machine slept since the last look (WO28 §1): kern.waketime moved (macOS), or the wall clock
    ran WAKE_GAP_S longer than this process did — a gap in the tick clock. The clocks and the wake reader are
    seams."""

    def __init__(self, gap_s: float = WAKE_GAP_S, wall: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic, waketime: Callable[[], float | None] = last_wake):
        self.gap_s, self.wall, self.mono, self.waketime = gap_s, wall, mono, waketime
        self._wall, self._mono, self._wake = wall(), mono(), waketime()

    def check(self) -> bool:
        """True when a sleep happened since the previous check (or since this watch was made)."""
        w, m, k = self.wall(), self.mono(), self.waketime()
        slept = (w - self._wall) - (m - self._mono) > self.gap_s
        woke = k is not None and self._wake is not None and k > self._wake
        self._wall, self._mono = w, m
        if k is not None:
            self._wake = k
        return slept or woke


def slept_since(started: float, wake_before: float | None, mono_started: float, *,
                wall: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic,
                waketime: Callable[[], float | None] = last_wake) -> bool:
    """Did the Mac sleep during something that started at `started` (wall) / `mono_started` (monotonic)? The poster
    asks it after each listing (WO28 §3)."""
    k = waketime()
    if k is not None and (wake_before is None or k > wake_before) and k >= started:
        return True
    return (wall() - started) - (mono() - mono_started) > WAKE_GAP_S


class Awake:
    """`caffeinate -i -w <our pid>` while there is work (WO28 §5): the Mac doesn't idle-sleep in the middle of it, on
    battery too, and is released the moment the work is done so it sleeps normally. `-w` ties the assertion to this
    process: if we die, it goes too. Display sleep stays allowed."""

    def __init__(self, popen: Callable = subprocess.Popen, darwin: bool = DARWIN, pid: int | None = None):
        self.popen, self.darwin, self.pid = popen, darwin, pid or os.getpid()
        self._proc = None

    @property
    def held(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def hold(self, on: bool) -> None:
        if on and not self.held and self.darwin:
            try:
                self._proc = self.popen(["caffeinate", "-i", "-w", str(self.pid)], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
            except OSError:
                self._proc = None
        elif not on and self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 — it dies with us anyway (-w)
                pass
            self._proc = None


def publish_paused(db: DB, batt: Battery | None, work: bool, say: Callable[[str], object]) -> bool:
    """The battery rule (WO28 §5). On battery below LOW_PCT while work remains: ONE line (BATTERY_LOW) and no new
    publish; the episode lasts until the Mac is on AC or back at RESUME_PCT, then publishing goes on quietly (the
    status message shows it). The state is in kv, so the worker and the poster share it. True while paused."""
    since = db.kv_get(BATTERY_KEY)
    if since:
        if batt is None or batt.on_ac or batt.percent >= RESUME_PCT:
            db.conn.execute("DELETE FROM kv WHERE key=?", (BATTERY_KEY,))
            db.log(None, "battery_ok", {"percent": batt.percent if batt else None, "ac": batt.on_ac if batt else None})
            return False
        return True
    if work and batt is not None and not batt.on_ac and batt.percent < LOW_PCT:
        db.kv_set(BATTERY_KEY, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        db.log(None, "battery_low", {"percent": batt.percent})
        say(BATTERY_LOW)
        return True
    return False
