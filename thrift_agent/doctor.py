"""`thrift doctor` (WO49): the whole agent's health on one screen — over SSH, or a reply "doctor" in the private chat.

Live 2026-10-24: a sale missed for a day, a share stuck for 71 minutes and a watcher that restarted a healthy worker —
each visible only by reading the database by hand. This says, in plain lines: is the worker making progress, what
waits (the iCloud inbox, the local upload intake, Azure), is the extension there, each site's state, the take-downs
still open and how old, and today's shares with their outcomes. Read only; never raises (a part it can't read says so).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from thrift_agent import alerts, crosslist, daily, pipeline, shares, uploads
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads

SITES = ("poshmark", "depop", "vinted")


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


def report(s: Settings, db: DB, now: datetime | None = None, api=None) -> list[str]:
    """The lines (see the module). `api`: thrift-api's client (tests pass a stand-in; None: sales.api())."""
    now = now or datetime.now(timezone.utc)
    out = ["🩺 Thrift doctor — " + now.astimezone().strftime("%a %b %d, %H:%M")]
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
    out.append(f"Extension: {'connected' if link.get('connected') else 'NOT connected'}"
               f" (seen {_ago(link.get('at'), now)})" if link else "Extension: never seen")
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
                   + (f" · STOPPED for this window: {why}" if why else ""))
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
