"""WO52 §7 (live 2026-11-01: 37% free, pressure "warning", swap 8.6 of 9.0 GB on the 8 GB Mac). What uses the memory,
read from `top` (each process's footprint, compressed included) and grouped: OUR browsers — the Thrift Chrome
(user-data-dir ~/thrift/chrome-cross) and Poshmark's poster Chrome (~/thrift/chrome-profile), each with its helper
processes — and the Mac's own apps (Safari's WebKit pages, the owner's Chrome, the rest).

- `thrift doctor` shows the top users (`top_lines`).
- The poster's housekeeping (`tick`): pressure warning or critical, the Thrift Chrome over RESTART_MB and the extension
  sites idle → the Thrift Chrome restarted through its LaunchAgent (never the Google Chrome app; its windows come back
  with --restore-last-session), at most once per RESTART_EVERY, one private line. Pressure critical for 30 minutes →
  ONE private line naming the top users (Safari's tabs named when they are the most).
Only on the Mac (darwin); every reading is a subprocess with a timeout, never on a test machine's own system."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections.abc import Callable

from thrift_agent import notify
from thrift_agent.db import DB, loads

RESTART_MB = 3000
RESTART_EVERY = 12 * 3600
CRITICAL_FOR = 30 * 60
KEY = "memory_state"           # kv: {"restarted": epoch, "critical_since": epoch, "critical_said": epoch}
OURS = {"chrome-cross": "Thrift Chrome (ours)", "chrome-profile": "Poshmark's Chrome (ours)"}


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


def _group(pid: int, procs: dict[int, dict]) -> str:
    seen = set()
    while pid in procs and pid not in seen:
        seen.add(pid)
        args = procs[pid]["args"]
        for mark, name in OURS.items():
            if "user-data-dir=" in args and mark in args:
                return name
        if "WebKit" in args or "Safari" in args:
            return "Safari (the Mac's own)"
        if "Google Chrome" in args and "--type=" not in args:
            return "Google Chrome (the owner's own)"
        pid = procs[pid]["ppid"]
    args = procs.get(pid, {}).get("args", "?")
    return re.sub(r".*/", "", args.split(" --")[0])[:40] or "?"


def by_group(procs: list[dict]) -> list[tuple[str, float]]:
    totals: dict[str, float] = {}
    for p in procs:
        totals[p["group"]] = totals.get(p["group"], 0) + p["mb"]
    return sorted(totals.items(), key=lambda x: -x[1])


def top_lines(procs: list[dict] | None = None, n: int = 6) -> list[str]:
    if procs is None:
        if sys.platform != "darwin":
            return []
        procs = processes()
    return [f"{name}: {mb / 1024:.1f} GB" if mb >= 1024 else f"{name}: {mb:.0f} MB" for name, mb in by_group(procs)[:n]]


def pressure(read: Callable[[str], str | None] | None = None) -> int | None:
    """kern.memorystatus_vm_pressure_level: 1 normal, 2 warning, 4 critical (None off the Mac)."""
    if read is None:
        if sys.platform != "darwin":
            return None
        read = lambda k: _run(["sysctl", "-n", k], 5).strip() or None   # noqa: E731
    v = read("kern.memorystatus_vm_pressure_level")
    return int(v) if v and v.isdigit() else None


def tick(s, db: DB, idle: bool, now: float | None = None, procs: list[dict] | None = None,
         level: int | None = None, restart: Callable[[], str] | None = None) -> str | None:
    """The housekeeping turn: maybe restart the Thrift Chrome; maybe the 30-minute critical line. Returns what it did."""
    t = now if now is not None else time.time()
    level = pressure() if level is None else level
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
            from thrift_agent.post import runner
            out = (restart or (lambda: runner.restart_thrift_chrome(s)))()
            st["restarted"] = t
            db.log(None, "memory_restart", {"site": "thrift_chrome", "mb": round(ours), "out": str(out)[:200]})
            notify.say(f"🧠 The Mac was short of memory: the Thrift Chrome held {ours / 1024:.1f} GB — restarted it "
                       f"while nothing was being listed (its windows come back by themselves).")
            did = "restarted"
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
