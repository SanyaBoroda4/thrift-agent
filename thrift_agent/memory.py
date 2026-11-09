"""WO52 §7 (live 2026-11-01: 37% free, pressure "warning", swap 8.6 of 9.0 GB on the 8 GB Mac). What uses the memory,
read from `top` (each process's footprint, compressed included) and `ps`, and grouped: the AGENT — its worker
(`thrift run`), its poster (`thrift poster`, with Poshmark's Playwright Chrome under it: user-data-dir
~/thrift/chrome-profile) and the Thrift Chrome (~/thrift/chrome-cross, its own launchd job), each a process tree told
by its --user-data-dir or its `bin/thrift` command — and the Mac's own apps (Safari's WebKit pages, the owner's
Chrome, the rest).

WO54 §4 (live 2026-11-09: the Thrift Chrome alone held 9.6 GB in 51 pages on her 8 GB laptop, two of them burning a
core each since 1 AM; she couldn't work): her laptop comes first. The agent has a BUDGET — worker, poster and both
Chromes together under BUDGET_MB (2 GB) while posting, near zero when idle (both Chromes quit after 10 idle minutes:
bridge.IDLE_QUIT, parallel.IDLE_CLOSE) — and the kernel's memory pressure decides what starts:
- "warning" → the listing in hand finishes and no new one starts (`may_start`, asked by the site workers before a new
  listing); the Thrift Chrome over RESTART_MB with its sites idle is quit (it opens again for the next job), at most
  once per RESTART_EVERY, one private line;
- "critical" → also both browsers quit as soon as nothing is in hand (`squeeze`: the bridge's watch quits the Thrift
  Chrome, Poshmark's browser closes itself), and come back for the next job that isn't a new listing (a take-down);
  critical for 30 minutes → ONE private line naming the top users (Safari's tabs named when they are the most).
`thrift doctor` shows the top users (`top_lines`), the agent against its budget (`agent_line`) and the laptop as a
measurement (`laptop_lines`: CPU, the pages open, who is in front). Only on the Mac (darwin); every reading is a
subprocess with a timeout, never on a test machine's own system."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable

from thrift_agent import notify
from thrift_agent.db import DB, loads

BUDGET_MB = 2048               # WO54 §4: the whole agent while posting (worker + poster + both Chromes)
RESTART_MB = 3000              # the Thrift Chrome over this at "warning", its sites idle → quit (WO52 §7's restart)
RESTART_EVERY = 12 * 3600
CRITICAL_FOR = 30 * 60
READ_EVERY = 15.0              # the pressure is read at most this often (every site worker asks before a listing)
SQUEEZE_FOR = 120.0            # a "quit the Thrift Chrome" from the tick holds this long (the watch acts within 15 s)
KEY = "memory_state"           # kv: {"restarted": epoch, "critical_since": epoch, "critical_said": epoch}
WAIT_KEY = "memory_wait"       # kv: {site: why} — the sites waiting for memory now (one event per wait)
OURS = {"chrome-cross": "Thrift Chrome (ours)", "chrome-profile": "Poshmark's Chrome (ours)"}
WORKER, POSTER = "Worker (ours)", "Poster (ours)"
AGENT = (OURS["chrome-cross"], OURS["chrome-profile"], POSTER, WORKER)
SHORT = {OURS["chrome-cross"]: "Thrift Chrome", OURS["chrome-profile"]: "Poshmark's Chrome", POSTER: "poster",
         WORKER: "worker"}
LEVELS = {1: "normal", 2: "warning", 4: "critical"}

_LOCK = threading.Lock()
_READ = {"at": None, "level": None}        # the last pressure reading, shared by the poster's threads
_SQUEEZE = {"at": None, "why": None}       # the tick's word: quit the Thrift Chrome now (it held RESTART_MB)


def _run(args: list[str], timeout: float = 15) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:  # noqa: BLE001 — a reading that fails is no reading (never the doctor's or the poster's end)
        return ""


def _mb(v: str) -> float:
    m = re.match(r"([\d.]+)([KMGB]?)", v.strip().upper())
    if not m:
        return 0.0
    n = float(m.group(1))
    return {"K": n / 1024, "M": n, "G": n * 1024, "B": n / 1048576}.get(m.group(2), n / 1048576)


def processes(top_out: str | None = None, ps_out: str | None = None) -> list[dict]:
    """[{pid, ppid, name, mb, group}] — `top`'s footprint per process, grouped by the browser it belongs to."""
    if top_out is None:
        top_out = _run(["top", "-l", "1", "-o", "mem", "-n", "60", "-stats", "pid,mem"])
    if ps_out is None:
        ps_out = _run(["ps", "-axo", "pid=,ppid=,args="])
    procs: dict[int, dict] = {}
    for line in ps_out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            procs[int(parts[0])] = {"pid": int(parts[0]), "ppid": int(parts[1]), "args": parts[2]}
    out = []
    for line in top_out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            pid = int(parts[0])
            p = procs.get(pid) or {"pid": pid, "ppid": 0, "args": "?"}
            out.append({**p, "mb": _mb(parts[1]), "group": _group(pid, procs)})
    return out


def everything(top_out: str | None = None, ps_out: str | None = None) -> list[dict]:
    """WO54 §4: EVERY process (not only the top 60 by memory) — [{pid, ppid, args, mb, cpu, group}]: `ps` for the tree,
    the CPU (its decaying average over the last minute) and the resident size; `top`'s footprint where it has one."""
    if ps_out is None:
        ps_out = _run(["ps", "-axo", "pid=,ppid=,rss=,%cpu=,args="])
    if top_out is None:
        top_out = _run(["top", "-l", "1", "-o", "mem", "-n", "400", "-stats", "pid,mem"])
    foot = {}
    for line in (top_out or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            foot[int(parts[0])] = _mb(parts[1])
    procs: dict[int, dict] = {}
    for line in (ps_out or "").splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) < 5 or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        try:
            rss, cpu = float(parts[2]) / 1024, float(parts[3].replace(",", "."))
        except ValueError:
            continue
        pid = int(parts[0])
        procs[pid] = {"pid": pid, "ppid": int(parts[1]), "args": parts[4], "mb": foot.get(pid, rss), "cpu": cpu}
    return [{**p, "group": _group(p["pid"], procs)} for p in procs.values()]


def _group(pid: int, procs: dict[int, dict]) -> str:
    own, seen = pid, set()
    while pid in procs and pid not in seen:
        seen.add(pid)
        args = procs[pid]["args"]
        for mark, name in OURS.items():
            if "user-data-dir=" in args and mark in args:
                return name
        if re.search(r"bin/thrift run(\s|$)", args):          # WO54 §4: the agent's own processes, and their children
            return WORKER
        if re.search(r"bin/thrift poster(\s|$)", args):
            return POSTER
        if "WebKit" in args or "Safari" in args:
            return "Safari (the Mac's own)"
        if "Google Chrome" in args and "--type=" not in args:
            return "Google Chrome (the owner's own)"
        pid = procs[pid]["ppid"]
    args = procs.get(own, {}).get("args", "?")             # not a browser's: the process's own name
    return re.sub(r".*/", "", args.split(" -")[0]).strip()[:40] or "?"


def by_group(procs: list[dict]) -> list[tuple[str, float]]:
    totals: dict[str, float] = {}
    for p in procs:
        totals[p["group"]] = totals.get(p["group"], 0) + p["mb"]
    return sorted(totals.items(), key=lambda x: -x[1])


def _size(mb: float) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def top_lines(procs: list[dict] | None = None, n: int = 6) -> list[str]:
    if procs is None:
        if sys.platform != "darwin":
            return []
        procs = processes()
    return [f"{name}: {_size(mb)}" for name, mb in by_group(procs)[:n]]


def agent(procs: list[dict]) -> dict[str, dict]:
    """WO54 §4: the agent's own processes by part — {group: {"mb", "cpu", "n"}} for its worker, its poster (and what
    the poster runs: Playwright's driver), the Thrift Chrome and Poshmark's Chrome."""
    out = {g: {"mb": 0.0, "cpu": 0.0, "n": 0} for g in AGENT}
    for p in procs:
        if p["group"] in out:
            part = out[p["group"]]
            part["mb"] += float(p.get("mb") or 0)
            part["cpu"] += float(p.get("cpu") or 0)
            part["n"] += 1
    return out


def agent_line(procs: list[dict] | None = None) -> str | None:
    """"Agent: 1.4 GB of its 2 GB budget — Thrift Chrome 820 MB (14) · Poshmark's Chrome 410 MB (9) · poster 120 MB ·
    worker 95 MB" (⚠️ over it); a Chrome that isn't running says "closed". None off the Mac."""
    if procs is None:
        if sys.platform != "darwin":
            return None
        procs = everything()
    parts = agent(procs)
    total = sum(p["mb"] for p in parts.values())
    shown = []
    for g in AGENT:
        p = parts[g]
        if not p["n"]:
            shown.append(f"{SHORT[g]} closed" if g in OURS.values() else f"{SHORT[g]} not running")
        else:
            shown.append(f"{SHORT[g]} {_size(p['mb'])}" + (f" ({p['n']})" if p["n"] > 1 else ""))
    flag = " ⚠️ over it" if total > BUDGET_MB else ""
    return f"Agent: {_size(total)} of its {_size(BUDGET_MB)} budget{flag} — " + " · ".join(shown)


def _front(read: Callable[[list[str]], str] | None = None) -> tuple[str | None, int | None]:
    """The app in front (its name, its pid) — LaunchServices' own answer (`lsappinfo`), no special permission. (None,
    None) when this session can't see it (an SSH session may not)."""
    read = read or (lambda args: _run(args, 5))
    asn = (read(["lsappinfo", "front"]) or "").strip()
    if not asn or "ASN" not in asn:
        return None, None
    name = re.search(r'"(?:LSDisplayName|name)"\s*=\s*"([^"]+)"', read(["lsappinfo", "info", "-only", "name", asn]) or "")
    pid = re.search(r'"pid"\s*=\s*(\d+)', read(["lsappinfo", "info", "-only", "pid", asn]) or "")
    return (name[1] if name else None), (int(pid[1]) if pid else None)


def laptop_lines(procs: list[dict] | None = None, link: dict | None = None,
                 read: Callable[[list[str]], str] | None = None, load: str | None = None) -> list[str]:
    """WO54 §4's measurement (thrift doctor, at 2–3 PM while posting and again after): the agent's CPU against the
    Mac's, the pages its Chromes hold (renderers; the extension's own count of its tabs and windows), and whether one
    of the agent's windows is in front. Read only. `link`: kv `ext_link` (the poster's view of the extension)."""
    if procs is None:
        if sys.platform != "darwin":
            return []
        procs = everything()
    parts = agent(procs)
    total_cpu = sum(float(p.get("cpu") or 0) for p in procs)
    mine = sum(p["cpu"] for p in parts.values())
    others = {}
    for p in procs:
        if p["group"] not in AGENT:
            others[p["group"]] = others.get(p["group"], 0.0) + float(p.get("cpu") or 0)
    busiest = ", ".join(f"{g} {c:.0f}%" for g, c in sorted(others.items(), key=lambda x: -x[1])[:3] if c >= 1)
    load = load if load is not None else (_run(["sysctl", "-n", "vm.loadavg"], 5) if sys.platform == "darwin" else "")
    avg = " ".join(re.findall(r"\d+[.,]\d+", load or "")[:3])
    lines = [f"CPU now: the agent {mine:.0f}% (" + ", ".join(f"{SHORT[g]} {parts[g]['cpu']:.0f}%" for g in AGENT)
             + f") of the Mac's {total_cpu:.0f}%" + (f" · load {avg}" if avg else "")
             + (f" · busiest of hers: {busiest}" if busiest else "")]
    renderers = {g: sum(1 for p in procs if p["group"] == g and "--type=renderer" in p.get("args", "")
                        and "--extension-process" not in p.get("args", "")) for g in OURS.values()}
    link = link or {}
    tabs = f"{link.get('tabs')} tabs in {link.get('windows')} windows" if link.get("tabs") is not None else "tabs unknown"
    lines.append(f"Pages open: Thrift Chrome {renderers[OURS['chrome-cross']]} page processes ({tabs}, the extension's "
                 f"count; it is {link.get('link') or 'unknown'}) · Poshmark's Chrome "
                 f"{renderers[OURS['chrome-profile']]} page processes")
    name, pid = _front(read)
    group = next((p["group"] for p in procs if p["pid"] == pid), None) if pid else None
    if name is None:
        front = "the app in front can't be read from this session"
    elif group in AGENT:
        front = f"⚠️ {SHORT.get(group, group)} is the app in front"
    else:
        front = f"{name} (no agent window in front)"
    lines.append(f"In front: {front} · a Thrift Chrome window has her focus: "
                 f"{'yes' if link.get('using') else 'no'} (the extension's view)")
    return lines


def pressure(read: Callable[[str], str | None] | None = None) -> int | None:
    """kern.memorystatus_vm_pressure_level: 1 normal, 2 warning, 4 critical (None off the Mac)."""
    if read is None:
        if sys.platform != "darwin":
            return None
        read = lambda k: _run(["sysctl", "-n", k], 5).strip() or None   # noqa: E731
    v = read("kern.memorystatus_vm_pressure_level")
    return int(v) if v and v.isdigit() else None


def level_now(now: float | None = None, read=None) -> int | None:
    """The pressure level, read at most every READ_EVERY seconds (shared by the poster's threads)."""
    t = time.monotonic() if now is None else now
    with _LOCK:
        if _READ["at"] is not None and t - _READ["at"] < READ_EVERY:
            return _READ["level"]
    level = pressure(read)
    with _LOCK:
        _READ.update(at=t, level=level)
    return level


def reset() -> None:
    """Forget the shared readings (the tests: no reading of one test reaches the next)."""
    with _LOCK:
        _READ.update(at=None, level=None)
        _SQUEEZE.update(at=None, why=None)


def may_start(db: DB | None = None, site: str | None = None, now: float | None = None,
              level: int | None = None) -> tuple[bool, str]:
    """WO54 §4: may a site worker start a NEW listing now? (True, "") unless the Mac is short of memory: "warning" →
    (False, why) — the listing in hand finishes, the next waits until the pressure is normal again; "critical" → the
    same, and the browsers quit meanwhile (`squeeze`). Off the Mac (no reading): always True. Cheap (the reading is
    shared, READ_EVERY) and safe from any thread; each site's wait is one `memory_wait` event, its end
    `memory_go`. `level`: a reading (the tests)."""
    level = level_now(now) if level is None else level
    if level is None or level < 2:
        _waited(db, site, None, level)
        return True, ""
    why = (f"the Mac is short of memory (pressure {LEVELS.get(level, level)}) — the listing in hand finished; new "
           "listings wait until it eases" + (", and the browsers quit meanwhile" if level >= 4 else ""))
    _waited(db, site, why, level)
    return False, why


def _waited(db: DB | None, site: str | None, why: str | None, level: int | None) -> None:
    if db is None or site is None:
        return
    try:
        st = loads(db.kv_get(f"{WAIT_KEY}:{site}")) or {}
        if bool(st.get("why")) == bool(why):
            return
        db.kv_set(f"{WAIT_KEY}:{site}", json.dumps({"why": why, "at": time.time()}))
        db.log(None, "memory_wait" if why else "memory_go", {"site": site, "level": level, "why": why})
    except Exception:  # noqa: BLE001 — the record never stops a listing
        pass


def squeeze(which: str = "thrift", now: float | None = None) -> str | None:
    """WO54 §4: why an IDLE browser of the agent should quit now, else None — the Mac's pressure "critical" (both),
    or (the Thrift Chrome only) the tick's word that it held RESTART_MB at "warning". The browsers act on it only with
    nothing in hand: the bridge's watch (the Thrift Chrome), Browser.ensure (Poshmark's)."""
    t = time.monotonic() if now is None else now
    level = level_now(t)
    with _LOCK:
        said = _SQUEEZE["why"] if _SQUEEZE["at"] is not None and t - _SQUEEZE["at"] < SQUEEZE_FOR else None
    if level is not None and level >= 4:
        return "memory pressure critical"
    if which == "thrift" and said:
        return said
    return None


def tick(s, db: DB, idle: bool, now: float | None = None, procs: list[dict] | None = None,
         level: int | None = None, quit: Callable[[str], object] | None = None) -> str | None:
    """The housekeeping turn: maybe quit the Thrift Chrome; maybe the 30-minute critical line. Returns what it did.
    `quit(why)`: how the Thrift Chrome is told to quit (the tests); by default the bridge's watch does it (`squeeze`)."""
    t = now if now is not None else time.time()
    level = level_now() if level is None else level
    st = loads(db.kv_get(KEY)) or {}
    if level is None:
        return None
    did = None
    if level >= 4:
        st.setdefault("critical_since", t)
    else:
        st.pop("critical_since", None)
        st.pop("critical_said", None)
    if level >= 2 and idle and (st.get("restarted") is None or t - float(st["restarted"]) >= RESTART_EVERY):
        procs = processes() if procs is None else procs
        ours = sum(mb for g, mb in by_group(procs) if g == OURS["chrome-cross"])
        if ours >= RESTART_MB:
            why = f"the Thrift Chrome held {ours / 1024:.1f} GB"
            if quit is not None:
                out = quit(why)
            else:
                with _LOCK:
                    _SQUEEZE.update(at=time.monotonic(), why=why)
                out = "asked the bridge's watch"
            st["restarted"] = t
            db.log(None, "memory_quit", {"site": "thrift_chrome", "mb": round(ours), "out": str(out)[:200]})
            notify.say(f"🧠 The Mac was short of memory: the Thrift Chrome held {ours / 1024:.1f} GB — quit it while "
                       "nothing was being listed (it opens again for the next job).")
            did = "quit"
    if level >= 4 and t - float(st["critical_since"]) >= CRITICAL_FOR and not st.get("critical_said"):
        procs = processes() if procs is None else procs
        groups = by_group(procs)
        top = ", ".join(f"{g} {mb / 1024:.1f} GB" for g, mb in groups[:4])
        safari = next((mb for g, mb in groups if g.startswith("Safari")), 0)
        hint = (" Closing some Safari tabs on the Mac would help most." if groups and groups[0][0].startswith("Safari")
                and safari >= 1024 else "")
        notify.say(f"🧠 The Mac has been short of memory for 30 min. Biggest users: {top}.{hint}")
        st["critical_said"] = t
        did = did or "said"
    db.kv_set(KEY, json.dumps(st))
    return did
