"""WO54 §3 — the owner's quiet hours on the Mac (8:30 AM – 2:00 PM America/New_York, thrift_agent/quiet.py): the
calendar job's decision, its Telegram peek, a process's own stop request, the overrides and the one status line.

deploy/quiet.sh (the launchd calendar job com.thriftagent.quiet: every 10 minutes and at load; a run missed while the
Mac slept comes once at wake, coalesced) asks `.venv/bin/python -I -m thrift_agent.quietctl decide [--stopped]` and
acts on the one word it prints — decided by the CURRENT time, so a late run does the right thing:
- "stop": inside the quiet hours (not lifted by the owner's "post now"): the poster, the worker and the Thrift Chrome
  stopped (launchd bootout: KeepAlive can't bring them back), then nothing of ours left;
- "stop-takedowns": the same but the poster stays, for take-downs only (`quiet.allow_takedowns`, the owner's switch);
- "start": outside them (or lifted), after the quiet hours stopped the agent (`--stopped`: quiet.sh's marker): the
  worker and the poster started (the owner's design, WO54 — a deploy still never starts the poster);
- "none": nothing to do (8:20 – 8:30: the processes themselves start nothing new).
Inside the quiet hours, with the worker stopped, `peek` reads the bot's updates WITHOUT moving its offset: a "post now"
from the owner in the private chat lifts them for today (the worker, started, reads the same update and answers it).
Nothing here ever sends a Telegram message.

Light on purpose (it runs every 10 minutes on her laptop): the standard library, PyYAML / python-dotenv (config) and
thrift_agent.quiet — no model, no browser, no httpx; `thrift_agent.db` only when it writes the override."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thrift_agent import quiet

ROOT = Path(__file__).resolve().parent.parent
QUIET_SH = ROOT / "deploy" / "quiet.sh"
LABEL = "com.thriftagent.quiet"         # the calendar job (deploy/com.thriftagent.quiet.plist)
ASKED = "quiet_stop_asked"              # kv: when a process last asked for its own stop
ASK_EVERY = 300                         # ... at most this often (seconds)
OFFSET_KEY = "telegram_offset"          # = approve.OFFSET_KEY: the worker's getUpdates offset — read here, never written
API = "https://api.telegram.org"
CROSS_PROFILE = Path.home() / "thrift" / "chrome-cross"    # the Thrift Chrome's --user-data-dir (its LaunchAgent)
LIFTED_TEXT = "✓ Quiet hours lifted for today — everything runs now."

_POST_NOW = re.compile(r"^\s*/?\s*post\s*now\s*[.!]*\s*$", re.I)
_QUIET_NOW = re.compile(r"^\s*/?\s*quiet\s*now\s*[.!]*\s*$", re.I)
# the same words typed with the keyboard left on Russian (ЙЦУКЕН where QWERTY was meant): "зщые тщц", "йгшуе тщц"
_RU = str.maketrans("йцукенгшщзхъфывапролджэячсмитьбю", "qwertyuiop[]asdfghjkl;'zxcvbnm,.")


def command(text: str | None) -> str | None:
    """ "post_now" / "quiet_now" for the owner's two words in the private chat (also on the Russian layout)."""
    t = str(text or "").strip().lower()
    for cand in (t, t.translate(_RU)):
        if _POST_NOW.match(cand):
            return "post_now"
        if _QUIET_NOW.match(cand):
            return "quiet_now"
    return None


def _settings():
    from thrift_agent.config import settings
    return settings()


def _utc(v) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


class ReadKV:
    """The kv table, read only (the every-10-minutes decision writes nothing and runs no migration)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        if not self.path.is_file():
            return default
        try:
            con = sqlite3.connect(str(self.path), timeout=10)
        except sqlite3.Error:
            return default
        try:
            row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        except sqlite3.Error:
            return default
        finally:
            con.close()
        return row[0] if row else default


# ---------------------------------------------------------------- the calendar job's decision

def decide(now: datetime | None = None, db=None, s=None, stopped: bool = False) -> str:
    """stop | stop-takedowns | start | none — see the module."""
    s = s if s is not None else _settings()
    st = quiet.state(now, db, s)
    if st == "quiet":
        return "stop-takedowns" if quiet.cfg(s).allow_takedowns else "stop"
    if stopped and st in ("open", "lifted", "off"):
        return "start"
    return "none"


def _when(t: datetime, now: datetime, q: quiet.Quiet) -> str:
    """ "8:30 AM" today, "tomorrow 8:30 AM", else "Wed 8:30 AM" — in the quiet hours' zone."""
    day, today = t.astimezone(q.tz).date(), now.astimezone(q.tz).date()
    hm = quiet.clock(t, q)
    if day == today:
        return hm
    return f"tomorrow {hm}" if day == today + timedelta(days=1) else f"{t.astimezone(q.tz):%a} {hm}"


def _next_stop(now: datetime, db, s, q: quiet.Quiet) -> datetime | None:
    nxt = quiet.next_start(now, q=q)
    if nxt is not None and quiet.lifted(now, db, s) and nxt.astimezone(q.tz).date() == now.astimezone(q.tz).date():
        nxt = quiet.next_start(nxt + timedelta(minutes=1), q=q)      # today's are lifted ("post now")
    return nxt


def status_line(now: datetime | None = None, db=None, s=None, short: bool = False) -> str:
    """`thrift status` / `thrift doctor`: "Quiet hours 8:30 AM – 2:00 PM: next stop tomorrow 8:30 AM, back at 2:00 PM"
    (`short`, services.sh status: "on, 8:30 AM – 2:00 PM — next stop tomorrow 8:30 AM, start 2:00 PM")."""
    s = s if s is not None else _settings()
    now = quiet._now(now)                          # the quiet hours' own clock
    q = quiet.cfg(s)
    st = quiet.state(now, db, s)
    if st == "off":
        return "off" if short else "Quiet hours: off (settings quiet.enabled)"
    back = quiet.resume_at(now, q=q)
    if st == "quiet":
        rest = f"quiet now — {'start' if short else 'back at'} {quiet.clock(back, q)}"
    elif st == "closing":
        rest = (f"nothing new starts now — quiet at {quiet.clock(quiet.next_start(now, q=q) or back, q)}, "
                f"{'start' if short else 'back at'} {quiet.clock(back, q)}")
    else:
        nxt = _next_stop(now, db, s, q)
        end = quiet.window_of(nxt.astimezone(q.tz).date(), q)[1] if nxt is not None else None
        lifted = "lifted for today ('post now') — " if st == "lifted" or quiet.lifted(now, db, s) else ""
        rest = (lifted + (f"next stop {_when(nxt, now, q)}, {'start' if short else 'back at'} {quiet.clock(end, q)}"
                          if nxt is not None and end is not None else "no window ahead"))
    return f"on, {q.label()} — {rest}" if short else f"Quiet hours {q.label()}: {rest}"


# ---------------------------------------------------------------- the peek ("post now" while the worker is stopped)

def _fetch(url: str, timeout: float = 15.0) -> dict:
    """One GET to Telegram (urllib with certifi's roots when there are: python.org's Python on macOS has none)."""
    import ssl
    import urllib.request
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:      # noqa: S310 — a fixed https host
        return json.loads(r.read().decode("utf-8"))


def _user_ids(raw: str | None) -> set[int]:
    return {int(t.strip()) for t in str(raw or "").replace(";", ",").split(",") if t.strip().lstrip("-").isdigit()}


def _ops_chat(s) -> str | None:
    chat = str(os.getenv("TELEGRAM_OPS_CHAT_ID") or s.get("telegram.ops_chat_id") or "").strip()
    return None if not chat or chat == os.getenv("TELEGRAM_CHAT_ID", "").strip() else chat


def peek(now: datetime | None = None, s=None, db=None, fetch=None, open_db=None) -> str:
    """Inside the quiet hours, the worker stopped: the owner's last word of today in the private chat, from the bot's
    updates read from the worker's own offset + 1 (which confirms only what the worker has handled; the offset itself
    is never moved and nothing is ever sent). "post now" → the quiet hours lifted for today (kv `quiet_override`, as the
    worker's own answer would set it) → "post-now"; else "none". Never raises; never prints the token."""
    s = s if s is not None else _settings()
    now = quiet._now(now)                          # the quiet hours' own clock
    q = quiet.cfg(s)
    db = db if db is not None else ReadKV(s.path("db"))
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), _ops_chat(s)
    users = _user_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS"))
    if not (s.get("telegram.enabled") and token and chat and users) or not quiet.is_quiet(now, db, s):
        return "none"
    offset = db.kv_get(OFFSET_KEY)
    url = f"{API}/bot{token}/getUpdates?timeout=0&limit=100" + (f"&offset={int(offset) + 1}"
                                                                 if str(offset or "").isdigit() else "")
    try:
        body = (fetch or _fetch)(url)
    except Exception as e:  # noqa: BLE001 — offline, Telegram away: the next run looks again
        print(f"peek: {type(e).__name__}", file=sys.stderr)          # never the URL: it holds the token
        return "none"
    today, last = now.astimezone(q.tz).date(), None
    for u in (body.get("result") or []) if isinstance(body, dict) and body.get("ok") else []:
        msg = u.get("message") if isinstance(u, dict) else None
        if not isinstance(msg, dict) or str((msg.get("chat") or {}).get("id")) != str(chat):
            continue
        uid = (msg.get("from") or {}).get("id")
        if not isinstance(uid, int) or isinstance(uid, bool) or uid not in users:
            continue
        try:
            sent = datetime.fromtimestamp(int(msg.get("date") or 0), timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        if sent.astimezone(q.tz).date() != today:
            continue                                        # "post now" holds for the day it was said only
        last = command(msg.get("text")) or last
    if last != "post_now":
        return "none"
    real = (open_db or (lambda: _open_db(s)))()
    quiet.set_post_now(real, now)
    real.log(None, "quiet_peek", {"found": "post now"})
    return "post-now"


def _open_db(s):
    from thrift_agent.db import DB
    return DB(s.path("db"))


# ---------------------------------------------------------------- a process's own stop, the owner's overrides

def run_tick() -> str:
    """deploy/quiet.sh tick, now and outside this process: the calendar job kickstarted (launchd runs it in its own
    context), else the script in a session of its own (never killed with the process that asked). The Mac only."""
    try:
        r = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, timeout=20)
        if r.returncode == 0:
            return "kickstart"
    except (OSError, subprocess.SubprocessError, AttributeError):
        pass
    try:
        subprocess.Popen(["/bin/bash", str(QUIET_SH), "tick"], cwd=str(ROOT), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError as e:
        return f"not run: {type(e).__name__}: {e}"
    return "detached"


def on_mac(s) -> bool:
    return bool(getattr(s, "is_prod", False)) and sys.platform == "darwin"


def ask_stop(s, db, who: str, now: datetime | None = None, run=None) -> bool:
    """A process found itself inside the quiet hours (the calendar job missed it, or isn't loaded): it asks for its own
    stop — deploy/quiet.sh tick, detached. The Mac only; at most every ASK_EVERY seconds (kv). True when asked."""
    if run is None and not on_mac(s):
        return False
    t = quiet._now(now)
    last = _utc(db.kv_get(ASKED))
    if last is not None and 0 <= (t - last).total_seconds() < ASK_EVERY:
        return False
    db.kv_set(ASKED, t.isoformat(timespec="seconds"))
    how = (run or run_tick)()
    db.log(None, "quiet_stop_asked", {"who": who, "how": str(how)[:120]})
    return True


def beat_now(db, tick=None, info=None) -> bool:
    """One heartbeat to thrift-api now, its info carrying quiet.beat() (key "quiet", read there as it is): right after
    the owner's "post now" / "quiet now", and by the 8:30 stop before it unloads the worker — thrift-api mirrors the
    override or the stop at once (else a stop with no last beat reads as "Mac stopped answering" ~30 min later). The
    changes not yet synced go with it. Never raises; True when the beat went."""
    try:
        if tick is None:
            from thrift_agent import sales
            tick = sales.tick
        if info is None:
            from thrift_agent.cli import beat_info          # the heartbeat's whole info (a rare, one-off import)
            info = beat_info
        done = tick(db, force_beat=True, info=info(db))
        return bool((done or {}).get("beat"))
    except Exception as e:  # noqa: BLE001 — the API away is a quiet event; its next beat says it all
        try:
            db.log(None, "error", f"quiet beat: {type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            pass
        return False


def after_override(s, db, now: datetime | None = None, run=None) -> str | None:
    """After the owner's "post now" / "quiet now": inside today's window everything is started / stopped at once
    (the tick, detached — it decides by the new state); outside it nothing needs doing now. Returns how, or None."""
    if run is None and not on_mac(s):
        return None
    if quiet.current(now, quiet.cfg()) is None:
        return None
    return str((run or run_tick)())


# ---------------------------------------------------------------- `python -I -m thrift_agent.quietctl …`

USAGE = ("usage: python -I -m thrift_agent.quietctl decide [--stopped] | peek | state | status [--short] | profiles "
         "| beat")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "status"
    s = _settings()
    db = ReadKV(s.path("db"))
    if cmd == "decide":
        print(decide(None, db, s, stopped="--stopped" in args))
    elif cmd == "peek":
        print(peek(None, s, db))
    elif cmd == "state":
        print(quiet.state(None, db, s))
    elif cmd == "status":
        print(status_line(None, db, s, short="--short" in args))
    elif cmd == "profiles":                        # the agent's two Chromes, by their --user-data-dir
        print(s.path("chrome_profile"))
        print(CROSS_PROFILE)
    elif cmd == "beat":                            # the 8:30 stop's last heartbeat (deploy/quiet.sh)
        print("beat sent" if beat_now(_open_db(s)) else "beat: not sent (see the event log)")
    else:
        print(USAGE, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
