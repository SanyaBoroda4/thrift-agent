"""`thrift doctor` (WO49): the whole agent's health on one screen — over SSH, or a reply "doctor" in the private chat.

Live 2026-10-24: a sale missed for a day, a share stuck for 71 minutes and a watcher that restarted a healthy worker —
each visible only by reading the database by hand. This says, in plain lines: is the worker making progress, what
waits (the iCloud inbox, the local upload intake, Azure), is the extension there, each site's state, the take-downs
still open and how old, and today's shares with their outcomes. Read only; never raises (a part it can't read says so).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone

from thrift_agent import alerts, crosslist, daily, pipeline, shares, uploads
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads

SITES = ("poshmark", "depop", "vinted")
PRESSURE = {"1": "normal", "2": "warning", "4": "critical"}      # kern.memorystatus_vm_pressure_level


def _sysctl(name: str) -> str | None:
    """`sysctl -n <name>` on the Mac, or None (not there, timed out)."""
    try:
        r = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _megabytes(text: str, key: str) -> float | None:
    m = re.search(rf"{key}\s*=\s*([\d.]+)([KMG])", text or "")
    return float(m[1]) * {"K": 1 / 1024, "M": 1, "G": 1024}[m[2]] if m else None


def memory_line(read: Callable[[str], str | None] | None = None) -> str:
    """WO51: the Mac's memory in one line — "Memory: 35% free · pressure normal · swap 8.1 of 9.0 GB used ⚠️" (the
    8 GB Mac lives near its swap's end: the owner's own Safari tabs, the Thrift Chrome, a big share's images). ⚠️
    when the kernel says warning / critical, under 10% is free, or the swap is 80% full. `read`: sysctl -n (tests)."""
    read = read or _sysctl
    free, level = read("kern.memorystatus_level"), read("kern.memorystatus_vm_pressure_level")
    swap = read("vm.swapusage") or ""
    parts, alarm = [], False
    if free and free.strip().isdigit():
        parts.append(f"{int(free)}% free")
        alarm |= int(free) < 10
    if level is not None and level.strip() in PRESSURE:
        parts.append(f"pressure {PRESSURE[level.strip()]}")
        alarm |= level.strip() != "1"
    total, used = _megabytes(swap, "total"), _megabytes(swap, "used")
    if total:
        parts.append(f"swap {used / 1024:.1f} of {total / 1024:.1f} GB used")
        alarm |= used is not None and used >= 0.8 * total
    if not parts:
        return "Memory: unreadable now"
    return "Memory: " + " · ".join(parts) + (" ⚠️" if alarm else "")


def _ago(ts: str | None, now: datetime) -> str:
    """"4 min ago" / "2 h 10 min ago" / "never" for an ISO time."""
    if not ts:
        return "never"
    try:
        when = datetime.fromisoformat(str(ts))
    except ValueError:
        return str(ts)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    s = max(0, int((now - when).total_seconds()))
    if s < 90:
        return f"{s} s ago"
    if s < 3600:
        return f"{s // 60} min ago"
    h, m = divmod(s // 60, 60)
    return f"{h} h {m} min ago" if h < 48 else f"{h // 24} days ago"


def _seconds(ts: str | None, now: datetime) -> float | None:
    try:
        when = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now - when).total_seconds()


def _count_dirs(path) -> str:
    try:
        return str(sum(1 for p in path.iterdir() if p.is_dir() and not p.name.startswith(".")
                       and p.name != pipeline.UPLOADS_ARCHIVE))
    except FileNotFoundError:
        return "0"
    except OSError as e:
        return f"unreadable now ({type(e).__name__})"


EXT_STATES = {"connected": "connected", "asleep": "asleep (the Thrift Chrome is closed — it opens for the next job)",
              "waking": "waking (the Thrift Chrome was asked to start)", "missing": "NOT connected",
              "refused": "NOT connected (its token was refused)"}


def report(s: Settings, db: DB, now: datetime | None = None, api=None,
           memory: Callable[[], str] | None = None, laptop: Callable[[], list[str]] | None = None) -> list[str]:
    """The lines (see the module). `api`: thrift-api's client (tests pass a stand-in; None: sales.api()). `memory`:
    the memory line's reader — on the Mac `memory_line` (sysctl), elsewhere none (tests never read the machine).
    `laptop`: WO54 §4's lines — the agent against its budget, its CPU, the pages its Chromes hold, who is in front —
    on the Mac read from ps / top / lsappinfo (`laptop_now`), elsewhere none."""
    now = now or datetime.now(timezone.utc)
    out = ["🩺 Thrift doctor — " + now.astimezone().strftime("%a %b %d, %H:%M")]
    if memory is None and s.is_prod and sys.platform == "darwin":
        memory = memory_line
    if memory is not None:
        try:
            out.append(memory())
        except Exception as e:  # noqa: BLE001 — a part it can't read says so
            out.append(f"Memory: unreadable now ({type(e).__name__})")
    try:                                                           # WO54 §3: the owner's quiet hours
        from thrift_agent import quietctl
        out.append(quietctl.status_line(now, db))
    except Exception as e:  # noqa: BLE001 — a part it can't read says so
        out.append(f"Quiet hours: unreadable now ({type(e).__name__})")
    # the worker
    prog = loads(db.kv_get(alerts.PROGRESS)) or {}
    out.append(f"Worker: last progress {_ago(prog.get('at'), now)}" + (f" ({prog.get('what')})" if prog.get("what") else "")
               + f" · last finished inbox look {_ago(db.kv_get(alerts.SCAN_DONE), now)}"
               + f" · Azure polled {_ago(_poll_time(db), now)}")
    started, done = db.kv_get(alerts.SCAN_STARTED), db.kv_get(alerts.SCAN_DONE)
    age = _seconds(started, now)
    if started and (not done or done < started) and age is not None and age >= 300:
        out.append(f"  ⚠️ a look at the inbox started {_ago(started, now)} and hasn't finished")
    busy = loads(db.kv_get(alerts.BUSY_SINCE)) or {}
    if busy:
        out.append("  iCloud busy with: " + ", ".join(f"{k} (since {_ago(v, now)})" for k, v in busy.items()))
    # the poster and the extension
    p = daily.poster_now(db, now)
    st = loads(db.kv_get(daily.POSTER_STATE)) or {}
    out.append(f"Poster: {'running' if p.running else 'NOT running'}" + (" · LIVE" if p.running and p.live else "")
               + (f" · busy with {p.busy}" if p.running and p.busy else "") + (" · PAUSED" if p.running and p.paused else "")
               + f" · its beat {_ago(st.get('beat'), now)}")
    link = loads(db.kv_get(crosslist.EXT_LINK)) or {}
    state = EXT_STATES.get(link.get("link")) or ("connected" if link.get("connected") else "NOT connected")
    out.append(f"Extension: {state} (seen {_ago(link.get('at'), now)})" if link else "Extension: never seen")
    # what waits
    azure = _azure_pending(api)
    out.append(f"Waiting: iCloud inbox {_count_dirs(s.path('inbox'))} folder(s) · local upload intake "
               f"{_count_dirs(pipeline.uploads_dir(s))} · Azure not fetched {azure}")
    # each site
    counts = db.counts_by_marketplace()
    for mp in SITES:
        c = counts.get(mp) or {}
        why = crosslist.blocked(db, mp)
        parts = [f"{st_} {n}" for st_, n in sorted(c.items()) if st_ in ("posted", "queued", "posting", "failed")]
        out.append(f"{crosslist.LABEL.get(mp, mp)}: " + (", ".join(parts) or "nothing")
                   + (f" · PAUSED: {why}" if why else ""))
    from thrift_agent import health, humansteps
    out += [f"  {line}" for line in health.summary(db)]          # WO51: each pause and its next test
    try:                                                           # WO56: a human step pending, the last day's steps
        out += humansteps.doctor_lines(db, now)
    except Exception as e:  # noqa: BLE001 — a part it can't read says so
        out.append(f"Human steps: unreadable now ({type(e).__name__})")
    from thrift_agent import jobs
    from thrift_agent import memory as mem
    if tops := mem.top_lines():                                    # WO52 §7: who holds the memory
        out.append("Memory by app: " + " · ".join(tops))
    if laptop is None and s.is_prod and sys.platform == "darwin":
        laptop = lambda: laptop_now(link)                           # noqa: E731
    if laptop is not None:                                         # WO54 §4: the agent against its budget, the laptop
        try:
            out += laptop()
        except Exception as e:  # noqa: BLE001 — a part it can't read says so
            out.append(f"Agent: unreadable now ({type(e).__name__})")
    job_lines = jobs.lines(db)                                     # WO52 §4: each job's last success
    if job_lines:
        out.append("Jobs:")
        out += [f"  {line}" for line in job_lines]
    from thrift_agent import formcheck
    out += [f"  {line}" for line in formcheck.summary(db)]       # WO52 §3: each site's last form check
    # take-downs not done
    rows = db.conn.execute("SELECT item_id, marketplace, status, attempts, error, fetched_at FROM takedowns "
                           "WHERE status NOT IN ('done', 'not_found', 'sold') ORDER BY fetched_at").fetchall()
    if rows:
        out.append(f"Take-downs open: {len(rows)}")
        for r in rows[:6]:
            out.append(f"  {r['item_id']} on {r['marketplace']}: {r['status']} (attempts {r['attempts'] or 0}, "
                       f"fetched {_ago(r['fetched_at'], now)})" + (f" — {str(r['error'])[:80]}" if r["error"] else ""))
    else:
        out.append("Take-downs open: none")
    # today's shares
    today = shares.lines(db)
    out.append(f"Today's shares: {len(today)}" + ("" if today else " (none yet)"))
    out += [f"  {line}" for line in today[:8]]
    return out


def laptop_now(link: dict | None = None) -> list[str]:
    """WO54 §4's measurement, read once (ps + top): the agent's total against its 2 GB budget, then the CPU, the pages
    open and who is in front (memory.agent_line / laptop_lines)."""
    from thrift_agent import memory as mem
    procs = mem.everything()
    return [line for line in [mem.agent_line(procs), *mem.laptop_lines(procs, link=link)] if line]


def _poll_time(db: DB) -> str | None:
    """The last poll of the uploads (kv `uploads_last_poll` holds a timestamp, as sales._claim writes it)."""
    raw = db.kv_get(uploads.LAST_POLL)
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(float(raw), timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return raw


def _azure_pending(api) -> str:
    """How many shares thrift-api holds that the Mac hasn't fetched ("off": no API here; "?": it didn't answer)."""
    if api is None:
        from thrift_agent import sales
        api = sales.api()
    if api is None:
        return "off"
    try:
        answer = api.get("/shares/pending")
    except Exception:  # noqa: BLE001 — a part it can't read says so
        return "? (the API didn't answer)"
    pending = [x for x in (answer.get("shares") or []) if isinstance(x, dict) and not x.get("test")] \
        if isinstance(answer, dict) else []
    return str(len(pending)) + (f" ({', '.join(str(x.get('share_id')) for x in pending[:3])})" if pending else "")


def text(s: Settings, db: DB, api=None) -> str:
    return "\n".join(report(s, db, api=api))


def as_json(s: Settings, db: DB) -> str:
    return json.dumps(report(s, db), ensure_ascii=False)
