"""WO52 §4 (the owner, 2026-11-01): every periodic job proves it is alive. Live: Poshmark's order sweep failed every 30
minutes for four days on a closed browser and nobody heard. Each job records its last success and its last error here
(kv `job_health`); a job with no success for its limit — 12 hours, 24 for the daily ones — says ONE private line naming
the job and its last error, once per episode ("✅ … works again" when it recovers); `thrift doctor` shows them all.

    jobs.ok(db, "order_sweep:poshmark")            # after a run that worked
    jobs.failed(db, "order_sweep:poshmark", err)   # after one that didn't
    jobs.check(db)                                 # from the poster's housekeeping and the worker's tick

A job never seen is not judged (a site that is off, a feature not set up)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from thrift_agent import notify
from thrift_agent.db import DB, loads

KEY = "job_health"
LIMITS = {                     # hours without a success before the one line
    "order_sweep": 12, "photo_check": 12, "takedowns": 12, "price_drops": 24, "form_check": 24, "sync": 12,
    "cleanup": 24, "inbox": 12, "heartbeat": 12,
}
LABEL = {"order_sweep": "The order sweep", "photo_check": "The listing photo check", "takedowns": "The take-downs",
         "price_drops": "The price drops", "form_check": "The form check", "sync": "The sync to thrift-api",
         "cleanup": "The daily cleanup", "inbox": "The inbox look", "heartbeat": "The heartbeat to thrift-api"}
SITE = {"poshmark": "Poshmark", "depop": "Depop", "vinted": "Vinted"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def _when(v) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _all(db: DB) -> dict:
    return loads(db.kv_get(KEY)) or {}


def _save(db: DB, data: dict) -> None:
    db.kv_set(KEY, json.dumps(data))


def name_of(job: str) -> str:
    """'order_sweep:poshmark' → "Poshmark's order sweep"."""
    base, _, site = job.partition(":")
    label = LABEL.get(base, base.replace("_", " ").capitalize())
    if site:
        return f"{SITE.get(site, site.capitalize())}'s {label[4:] if label.startswith('The ') else label}"
    return label


def ok(db: DB, job: str, at: datetime | None = None) -> None:
    """A run of `job` worked. A job that was said to be stuck gets ONE "works again" line."""
    t = at or _now()
    data = _all(db)
    j = data.get(job) or {}
    was_said = j.get("said")
    data[job] = {**j, "ok": _iso(t), "said": None, "seen": _iso(t)}
    _save(db, data)
    if was_said:
        notify.say(f"✅ {name_of(job)} works again.")


def failed(db: DB, job: str, error: str | None, at: datetime | None = None) -> None:
    """A run of `job` failed — kept with its words (the line names the last one)."""
    t = at or _now()
    data = _all(db)
    j = data.get(job) or {}
    data[job] = {**j, "error": str(error or "")[:300], "error_at": _iso(t), "seen": _iso(t),
                 "first": j.get("first") or _iso(t)}
    _save(db, data)


def limit_h(job: str) -> int:
    return LIMITS.get(job.partition(":")[0], 12)


def stale(db: DB, at: datetime | None = None) -> list[tuple[str, dict]]:
    """The jobs with no success for their limit — judged from their last success, else their first record."""
    t = at or _now()
    out = []
    for job, j in _all(db).items():
        since = _when(j.get("ok")) or _when(j.get("first"))
        if since is not None and t - since >= timedelta(hours=limit_h(job)):
            out.append((job, j))
    return out


def check(db: DB, at: datetime | None = None) -> list[str]:
    """ONE private line per stuck job per episode: "⚠️ Poshmark's order sweep hasn't worked for 14 h — last error:
    TargetClosedError … (since Oct 28, 10:30 AM)". Returns the jobs said now."""
    t = at or _now()
    data = _all(db)
    said = []
    for job, j in stale(db, t):
        if j.get("said"):
            continue
        since = _when(j.get("ok")) or _when(j.get("first"))
        hours = int((t - since).total_seconds() // 3600) if since else limit_h(job)
        last = f" — last error: {j['error'][:160]}" if j.get("error") else ""
        never = " (no success recorded yet)" if not j.get("ok") else ""
        notify.say(f"⚠️ {name_of(job)} hasn't worked for {hours} h{last}{never}.")
        data[job] = {**j, "said": _iso(t)}
        said.append(job)
    if said:
        _save(db, data)
    return said


def lines(db: DB, at: datetime | None = None) -> list[str]:
    """For `thrift doctor`: each job, its last success and its last error."""
    t = at or _now()
    out = []
    for job, j in sorted(_all(db).items()):
        okt = _when(j.get("ok"))
        age = f"{int((t - okt).total_seconds() // 3600)} h ago" if okt and t - okt >= timedelta(hours=1) else \
            (f"{int((t - okt).total_seconds() // 60)} min ago" if okt else "never")
        bad = okt is None or t - okt >= timedelta(hours=limit_h(job))
        newer = (_when(j.get("error_at")) or t) > (okt or t - timedelta(days=999))
        err = f" · last error: {j['error'][:80]}" if j.get("error") and newer else ""
        out.append(f"{'⚠️' if bad else '✓'} {name_of(job)}: last success {age}{err}")
    return out
