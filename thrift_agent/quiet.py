"""WO54 §3: the owner's quiet hours — 8:30 AM to 2:00 PM every day (America/New_York): the Mac is her working laptop and
the agent does NOTHING on it then (no processing, no model calls, no posting, no checks, no cleanup, no Telegram sends,
no browsers). From 8:20 nothing new starts (`closing`); at 8:30 launchd unloads the worker, the poster and both Chromes
(deploy/quiet.sh, the calendar job com.thriftagent.quiet); at 2:00 PM they come back. Every other hour everything runs.

Pure functions over settings (`quiet.*`, defaults below) and one kv row: the owner's "post now" (ops chat) lifts the
quiet hours for that local day only (kv `quiet_override` {"date", "mode": "post_now", "at"}); "quiet now" clears it.
DST-safe: every window is a local wall-clock window turned into UTC per day by zoneinfo, so 8:30 is 8:30 on both sides
of a DST change. Clocks that measure "how long" (alarms, the verifying publishes, job health, thrift-api's nets) count
only the time outside the quiet windows (`active_seconds`)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULTS = {"enabled": True, "start": "08:30", "end": "14:00", "timezone": "America/New_York", "days": "all",
            "allow_takedowns": False}
CLOSING = timedelta(minutes=10)          # 8:20: nothing new starts; work in hand finishes
OVERRIDE = "quiet_override"              # kv
POST_NOW = "post_now"
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Quiet:
    enabled: bool
    start: time
    end: time
    tz: ZoneInfo
    days: frozenset
    allow_takedowns: bool
    tz_name: str = "America/New_York"

    def label(self) -> str:
        """ "8:30 AM – 2:00 PM" """
        return f"{_hm(self.start)} – {_hm(self.end)}"


def _hm(t: time) -> str:
    return datetime.combine(date(2000, 1, 1), t).strftime("%I:%M %p").lstrip("0")


def _time(v, default: str) -> time:
    if isinstance(v, time):
        return v
    if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 24 * 60:      # YAML 1.1: an unquoted 8:30 is 510
        return time(v // 60, v % 60)
    try:
        return datetime.strptime(str(v if v is not None else default).strip().zfill(5), "%H:%M").time()
    except ValueError:
        return datetime.strptime(default, "%H:%M").time()


def _days(v) -> frozenset:
    if v in (None, "", "all", "every", "daily"):
        return frozenset(range(7))
    if isinstance(v, str):
        v = [p for p in v.replace(",", " ").split() if p]
    out = set()
    for d in v or ():
        name = str(d).strip().lower()[:3]
        if name in DAY_NAMES:
            out.add(DAY_NAMES.index(name))
    return frozenset(out) if out else frozenset(range(7))


def cfg(s=None) -> Quiet:
    """The quiet hours from settings `quiet:` (missing keys → DEFAULTS). Never raises: a typo keeps the defaults."""
    raw = {}
    try:
        if s is None:
            from thrift_agent.config import settings
            s = settings()
        got = s.get("quiet") if hasattr(s, "get") else None
        raw = dict(got) if isinstance(got, dict) else {}
    except Exception:  # noqa: BLE001 - the quiet hours never break a process
        raw = {}
    return from_dict({**DEFAULTS, **raw})


def from_dict(d: dict | None) -> Quiet:
    d = {**DEFAULTS, **(d or {})}
    try:
        tz = ZoneInfo(str(d.get("timezone") or DEFAULTS["timezone"]))
        tz_name = str(d.get("timezone") or DEFAULTS["timezone"])
    except Exception:  # noqa: BLE001
        tz, tz_name = ZoneInfo(DEFAULTS["timezone"]), DEFAULTS["timezone"]
    return Quiet(enabled=bool(d.get("enabled", True)), start=_time(d.get("start"), DEFAULTS["start"]),
                 end=_time(d.get("end"), DEFAULTS["end"]), tz=tz, days=_days(d.get("days")),
                 allow_takedowns=bool(d.get("allow_takedowns", False)), tz_name=tz_name)


def _now(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def window_of(day: date, q: Quiet) -> tuple[datetime, datetime] | None:
    """The quiet window that STARTS on local `day`, in UTC; None when that day has none. An end at or before the start
    runs into the next day."""
    if not q.enabled or day.weekday() not in q.days or q.start == q.end:
        return None
    a = datetime.combine(day, q.start, tzinfo=q.tz)
    b = datetime.combine(day + timedelta(days=1) if q.end <= q.start else day, q.end, tzinfo=q.tz)
    return a.astimezone(timezone.utc), b.astimezone(timezone.utc)


def windows(since: datetime, until: datetime, q: Quiet) -> list[tuple[datetime, datetime]]:
    """Every quiet window touching [since, until], oldest first."""
    since, until = _now(since), _now(until)
    day = since.astimezone(q.tz).date() - timedelta(days=1)
    last = until.astimezone(q.tz).date()
    out = []
    while day <= last:
        w = window_of(day, q)
        if w and w[1] > since and w[0] < until:
            out.append(w)
        day += timedelta(days=1)
    return out


def current(now: datetime | None = None, q: Quiet | None = None) -> tuple[datetime, datetime] | None:
    """The window `now` is in (by the schedule alone), else None."""
    now, q = _now(now), q or cfg()
    for a, b in windows(now - timedelta(days=1), now + timedelta(seconds=1), q):
        if a <= now < b:
            return a, b
    return None


def _override(db, now: datetime, q: Quiet) -> str | None:
    """The owner's override for the local day of `now` ("post_now"), else None."""
    if db is None:
        return None
    try:
        raw = db.kv_get(OVERRIDE) if hasattr(db, "kv_get") else None
        o = json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(o, dict) or o.get("date") != now.astimezone(q.tz).date().isoformat():
        return None
    return o.get("mode") if o.get("mode") == POST_NOW else None


def lifted(now: datetime | None = None, db=None, s=None) -> bool:
    """The owner's "post now" holds for today."""
    q = cfg(s)
    return _override(db, _now(now), q) == POST_NOW


def is_quiet(now: datetime | None = None, db=None, s=None) -> bool:
    """Inside the quiet hours (and not lifted for today by "post now"): the agent does nothing."""
    now, q = _now(now), cfg(s)
    return current(now, q) is not None and _override(db, now, q) != POST_NOW


def closing(now: datetime | None = None, db=None, s=None) -> bool:
    """From 10 minutes before the quiet hours until their end (8:20 AM → 2:00 PM): nothing NEW starts (a listing, a
    model batch, a check, the reconciler, the cleanup, a price drop); work in hand finishes."""
    now, q = _now(now), cfg(s)
    if _override(db, now, q) == POST_NOW:
        return False
    for a, b in windows(now - timedelta(days=1), now + CLOSING + timedelta(seconds=1), q):
        if a - CLOSING <= now < b:
            return True
    return False


def active_seconds(since: datetime, now: datetime | None = None, s=None, q: Quiet | None = None) -> float:
    """Seconds from `since` to `now` outside the quiet windows: the clocks of the alarms (20 min, 2 h), the verifying
    publishes and job health stand still while the agent is quiet on purpose."""
    since, now, q = _now(since), _now(now), q or cfg(s)
    if now <= since:
        return 0.0
    total = (now - since).total_seconds()
    for a, b in windows(since, now, q):
        total -= max(0.0, (min(b, now) - max(a, since)).total_seconds())
    return max(0.0, total)


def next_start(now: datetime | None = None, s=None, q: Quiet | None = None) -> datetime | None:
    now, q = _now(now), q or cfg(s)
    for a, _b in windows(now, now + timedelta(days=8), q):
        if a > now:
            return a
    return None


def resume_at(now: datetime | None = None, s=None, q: Quiet | None = None) -> datetime | None:
    """When the agent works again: the end of the window `now` is in (or about to be in, past 8:20), else None."""
    now, q = _now(now), q or cfg(s)
    for a, b in windows(now - timedelta(days=1), now + CLOSING + timedelta(seconds=1), q):
        if a - CLOSING <= now < b:
            return b
    return None


def set_post_now(db, now: datetime | None = None, s=None) -> str:
    """The owner's "post now": the quiet hours lifted for today only. Returns the local date."""
    now, q = _now(now), cfg(s)
    day = now.astimezone(q.tz).date().isoformat()
    db.kv_set(OVERRIDE, json.dumps({"date": day, "mode": POST_NOW, "at": now.isoformat(timespec="seconds")}))
    db.log(None, "quiet_override", {"mode": POST_NOW, "date": day})
    return day


def clear_override(db, now: datetime | None = None) -> bool:
    """The owner's "quiet now": the quiet hours back. True when a "post now" was lifted."""
    had = bool(db.kv_get(OVERRIDE))
    db.conn.execute("DELETE FROM kv WHERE key=?", (OVERRIDE,))
    db.log(None, "quiet_override", {"mode": "cleared", "had": had})
    return had


def beat(db=None, s=None, now: datetime | None = None) -> dict:
    """The quiet hours as the heartbeat carries them to thrift-api (mirrored there: no offline line, no 2-hour net and
    no 🚨 while the Mac is quiet on purpose)."""
    q, now = cfg(s), _now(now)
    o = _override(db, now, q)
    return {"enabled": q.enabled, "start": q.start.strftime("%H:%M"), "end": q.end.strftime("%H:%M"),
            "timezone": q.tz_name, "days": "all" if q.days == frozenset(range(7)) else
            [DAY_NAMES[d] for d in sorted(q.days)], "allow_takedowns": q.allow_takedowns,
            "override": {"date": now.astimezone(q.tz).date().isoformat(), "mode": o} if o else None}


def clock(t: datetime, q: Quiet | None = None) -> str:
    """ "2:00 PM" in the quiet hours' zone."""
    q = q or cfg()
    return t.astimezone(q.tz).strftime("%I:%M %p").lstrip("0")


# ---------------------------------------------------------------- WO54 §3: additions for the Mac's side

def advance(since: datetime, seconds: float, s=None, q: Quiet | None = None) -> datetime:
    """The wall time at which `seconds` of ACTIVE time (outside the quiet windows) have passed since `since` — the
    inverse of active_seconds: a verifying publish's next look, counted in active time, lands after the quiet hours,
    never inside them. A moment that falls exactly on a window's start is moved to its end."""
    t, q = _now(since), q or cfg(s)
    left = max(0.0, float(seconds))
    for _ in range(4000):                                   # windows are daily: this is years of them
        ahead = [w for w in windows(t, t + timedelta(days=2), q) if w[1] > t]
        if not ahead:
            if left <= 2 * 86400:
                return t + timedelta(seconds=left)
            t, left = t + timedelta(days=2), left - 2 * 86400
            continue
        a, b = ahead[0]
        if a <= t:                                          # inside a window: no time passes until its end
            t = b
            continue
        gap = (a - t).total_seconds()
        if left < gap:
            return t + timedelta(seconds=left)
        left -= gap
        t = b
    return t + timedelta(seconds=left)


SILENT = ("22:00", "07:00")      # WO54 §3: Telegram messages between these local times go out without a sound


def silent(now: datetime | None = None, s=None) -> bool:
    """10 PM – 7 AM in the quiet hours' zone (settings `telegram.silent_hours`, else SILENT): a message then is sent
    with disable_notification — her phone stays quiet at night; the message is still there in the morning."""
    q = cfg(s)
    try:
        if s is None:
            from thrift_agent.config import settings
            s = settings()
        hours = s.get("telegram.silent_hours") if hasattr(s, "get") else None
    except Exception:  # noqa: BLE001 — never the reason a message isn't sent
        hours = None
    hours = hours if isinstance(hours, (list, tuple)) and len(hours) == 2 else SILENT
    a, b = _time(hours[0], SILENT[0]), _time(hours[1], SILENT[1])
    t = _now(now).astimezone(q.tz).time()
    if a == b:
        return False
    return a <= t < b if a < b else (t >= a or t < b)


def state(now: datetime | None = None, db=None, s=None) -> str:
    """One word for scripts and status lines: "off" (no quiet hours), "lifted" (the owner's "post now" holds for
    today), "quiet" (8:30 AM – 2:00 PM: nothing at all), "closing" (from 8:20: nothing new starts), else "open"."""
    now, q = _now(now), cfg(s)
    if not q.enabled or not q.days:
        return "off"
    if _override(db, now, q) == POST_NOW and resume_at(now, q=q) is not None:
        return "lifted"
    if current(now, q) is not None:
        return "quiet"
    if resume_at(now, q=q) is not None:
        return "closing"
    return "open"
