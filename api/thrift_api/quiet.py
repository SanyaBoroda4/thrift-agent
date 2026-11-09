"""WO54 §3: the owner's quiet hours as thrift-api knows them. From 8:30 AM to 2:00 PM every day (America/New_York) the
Mac — her working laptop — runs NOTHING: launchd unloads the worker, the poster and both Chromes, so no heartbeat, no
sync and no take-downs (unless `allow_takedowns`). Azure goes on taking her shares and the sales emails as when the Mac
is off, and its nets must not cry wolf: none of them judges the Mac while it is quiet on purpose or starting back, and
their clocks ("silent 30 minutes", "2 h after its price", "stuck 15 minutes") count only the Mac's working time.

The same model as the Mac's (thrift_agent/quiet.py): one window that starts each local day, made aware by zoneinfo, so
8:30 is 8:30 on both sides of a DST change; an end at or before the start runs into the next day; the owner's "post
now" lifts the quiet hours for one local date (thrift-api lifts that date's quiet time from the clocks too: on such a
day the Mac works through, and its silence is real). The settings ride the Mac's heartbeat — `info["quiet"]`, its
quiet.beat(): {"enabled", "start", "end", "timezone", "days": "all" | ["mon", …], "allow_takedowns", "override": null |
{"date", "mode": "post_now"}}; no heartbeat, no `quiet` in it, or a malformed one → DEFAULTS (the owner's rule stands).

Pure functions over a `Quiet`, and `read(db)`:
- `is_quiet(now, q)`     inside the quiet hours (not lifted for that date): the Mac runs nothing;
- `settling(now, q)`     … or less than GRACE after they ended — the Mac starts back (launchd loads it, the worker's first
                         heartbeat, the take-downs first): no "offline" or "late" decision then;
- `active_seconds(since, now, q)` / `aged(since, now, need, q)`   the time from `since` to `now` outside the quiet hours."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULTS = {"enabled": True, "start": "08:30", "end": "14:00", "timezone": "America/New_York", "days": "all",
            "allow_takedowns": False}
GRACE = timedelta(minutes=30)            # after the end: the Mac needs a few minutes to start back ("offline" decisions)
POST_NOW = "post_now"                    # the owner's override (ops chat "post now"): that local date is not quiet
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ALL_DAYS = frozenset(range(7))
NO = ("false", "no", "off", "0")
UTC = timezone.utc


@dataclass(frozen=True)
class Quiet:
    enabled: bool
    start: time
    end: time
    tz: ZoneInfo
    days: frozenset
    allow_takedowns: bool
    tz_name: str = "America/New_York"
    lifted_on: date | None = None        # the local date the owner's "post now" lifted
    source: str = "defaults"             # "heartbeat": the Mac's own settings; "defaults": none (or unreadable)

    def label(self) -> str:
        """"8:30 AM – 2:00 PM"."""
        return f"{_hm(self.start)} – {_hm(self.end)}"


def _hm(t: time) -> str:
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


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
    if isinstance(v, str):
        if v.strip().lower() in ("", "all", "every", "daily"):
            return ALL_DAYS
        v = [p for p in v.replace(",", " ").split() if p]
    if not isinstance(v, (list, tuple, set, frozenset)):
        return ALL_DAYS                                   # none, or nothing a list of days: every day
    out = {DAY_NAMES.index(n) for n in (str(d).strip().lower()[:3] for d in v) if n in DAY_NAMES}
    return frozenset(out) if out else ALL_DAYS


def _flag(v, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() not in NO


def _lifted_on(o) -> date | None:
    """The date of the owner's "post now" — the heartbeat's `override`, {"date": "2026-11-09", "mode": "post_now"}."""
    if not isinstance(o, dict) or o.get("mode") != POST_NOW:
        return None
    try:
        return date.fromisoformat(str(o.get("date") or "")[:10])
    except ValueError:
        return None


def from_dict(d: dict | None, source: str = "defaults") -> Quiet:
    """The quiet hours from the heartbeat's `quiet` (missing keys → DEFAULTS; a bad value → its default). Never
    raises: a malformed setting never turns the owner's quiet hours into a stream of false alarms."""
    d = {**DEFAULTS, **(d if isinstance(d, dict) else {})}
    name = str(d.get("timezone") or DEFAULTS["timezone"])
    try:
        tz = ZoneInfo(name)
    except Exception:  # noqa: BLE001 — an unknown zone keeps the default one
        name, tz = DEFAULTS["timezone"], ZoneInfo(DEFAULTS["timezone"])
    return Quiet(enabled=_flag(d.get("enabled"), True), start=_time(d.get("start"), DEFAULTS["start"]),
                 end=_time(d.get("end"), DEFAULTS["end"]), tz=tz, days=_days(d.get("days")),
                 allow_takedowns=_flag(d.get("allow_takedowns"), False), tz_name=name,
                 lifted_on=_lifted_on(d.get("override")), source=source)


def read(db) -> Quiet:
    """The quiet hours the Mac's last heartbeat carried (`info["quiet"]`), else DEFAULTS."""
    beat = db.one("SELECT info FROM heartbeats WHERE source = 'mac'")
    try:
        info = json.loads(beat["info"]) if beat and beat["info"] else {}
    except (TypeError, ValueError):
        info = {}                                         # unreadable: the owner's rule stands (DEFAULTS)
    got = info.get("quiet") if isinstance(info, dict) else None
    return from_dict(got, "heartbeat") if isinstance(got, dict) else from_dict(None)


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def window_of(day: date, q: Quiet) -> tuple[datetime, datetime] | None:
    """The quiet window that STARTS on local `day`, in UTC, by the schedule alone; None when that day has none."""
    if not q.enabled or day.weekday() not in q.days or q.start == q.end:
        return None
    a = datetime.combine(day, q.start, tzinfo=q.tz)
    b = datetime.combine(day + timedelta(days=1) if q.end <= q.start else day, q.end, tzinfo=q.tz)
    return a.astimezone(UTC), b.astimezone(UTC)


def windows(since: datetime, until: datetime, q: Quiet) -> list[tuple[datetime, datetime]]:
    """Every scheduled window touching [since, until], oldest first."""
    since, until = _aware(since), _aware(until)
    day, last = since.astimezone(q.tz).date() - timedelta(days=1), until.astimezone(q.tz).date()
    out = []
    while day <= last:
        w = window_of(day, q)
        if w and w[1] > since and w[0] < until:
            out.append(w)
        day += timedelta(days=1)
    return out


def spans(since: datetime, until: datetime, q: Quiet) -> list[tuple[datetime, datetime]]:
    """The quiet time touching [since, until]: the windows, less the local day the owner's "post now" lifted (the same
    test as the Mac's is_quiet: a moment is lifted when its local date is the override's)."""
    if q.lifted_on is None:
        return windows(since, until, q)
    lo = datetime.combine(q.lifted_on, time(0), tzinfo=q.tz).astimezone(UTC)
    hi = datetime.combine(q.lifted_on + timedelta(days=1), time(0), tzinfo=q.tz).astimezone(UTC)
    out = []
    for a, b in windows(since, until, q):
        if b <= lo or a >= hi:
            out.append((a, b))
            continue
        if a < lo:
            out.append((a, lo))
        if b > hi:
            out.append((hi, b))
    return out


def is_quiet(now: datetime, q: Quiet) -> bool:
    """Inside the quiet hours, not lifted for that date: the Mac runs nothing."""
    now = _aware(now)
    return any(a <= now < b for a, b in spans(now - timedelta(days=1), now + timedelta(seconds=1), q))


def settling(now: datetime, q: Quiet, grace: timedelta = GRACE) -> bool:
    """Inside the quiet hours, or less than `grace` after they ended — the Mac starting back (launchd loads it, the
    worker's first heartbeat, the take-downs first): no net judges the Mac then."""
    now = _aware(now)
    return any(a <= now < b + grace
               for a, b in spans(now - timedelta(days=1) - grace, now + timedelta(seconds=1), q))


def active_seconds(since: datetime, now: datetime, q: Quiet) -> float:
    """The seconds from `since` to `now` outside the quiet hours: elapsed minus the overlap with the quiet time (0
    when `now` is not after `since`)."""
    since, now = _aware(since), _aware(now)
    if now <= since:
        return 0.0
    total = (now - since).total_seconds()
    for a, b in spans(since, now, q):
        total -= max(0.0, (min(b, now) - max(a, since)).total_seconds())
    return max(0.0, total)


def aged(since: datetime, now: datetime, need: timedelta, q: Quiet) -> bool:
    """At least `need` of the Mac's working time from `since` to `now`. Cheap for an old `since` (a sale a month
    back): the last days are counted first, the whole span only when they don't hold enough."""
    since, now = _aware(since), _aware(now)
    if now - since < need:
        return False                                      # not even that much time: never that much working time
    want = need.total_seconds()
    tail = max(since, now - need - timedelta(days=2))
    return active_seconds(tail, now, q) >= want or (tail > since and active_seconds(since, now, q) >= want)


def ended_between(since: datetime, now: datetime, q: Quiet) -> datetime | None:
    """The end of the last quiet time that ended after `since` and by `now` (a silence that ran through it), else
    None."""
    since, now = _aware(since), _aware(now)
    ends = [b for a, b in spans(since, now, q) if since < b <= now]
    return ends[-1] if ends else None


def describe(now: datetime, q: Quiet) -> dict:
    """The quiet hours as GET /health shows them: thrift-api's own view, to check that it knows them."""
    state = "quiet" if is_quiet(now, q) else "starting back" if settling(now, q) else "working"
    return {"enabled": q.enabled, "hours": q.label(), "timezone": q.tz_name,
            "days": "all" if q.days == ALL_DAYS else [DAY_NAMES[d] for d in sorted(q.days)],
            "allow_takedowns": q.allow_takedowns, "lifted_on": q.lifted_on.isoformat() if q.lifted_on else None,
            "source": q.source, "now": state}
