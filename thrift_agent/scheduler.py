"""When is the poster allowed to act? Pure functions over settings + DB counts."""
from __future__ import annotations

import random
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from thrift_agent.config import Settings


def _parse_hour(h: object) -> time:
    """Accepts "09:00", "9:00" or 540. YAML 1.1 reads an unquoted `9:00` as the sexagesimal int 540, and a crash here
    would crash-loop the poster under launchd, pinging 'Poster started' every minute."""
    if isinstance(h, time):
        return h
    if isinstance(h, bool):                     # bool is an int; True would silently become 00:01
        raise ValueError(f"schedule.hours: expected 'HH:MM', got {h!r}")
    if isinstance(h, int):
        if not 0 <= h < 24 * 60:
            raise ValueError(f"schedule.hours: {h} is not a minute of the day (0..1439); quote times as 'HH:MM'")
        return time(h // 60, h % 60)
    if isinstance(h, str):
        try:
            return datetime.strptime(h.strip().zfill(5), "%H:%M").time()
        except ValueError:
            raise ValueError(f"schedule.hours: cannot parse {h!r}; expected 'HH:MM' such as '09:00'") from None
    raise ValueError(f"schedule.hours: expected 'HH:MM' strings (quoted in YAML), got {h!r}")


def in_hours(now_local: datetime, hours: list) -> bool:
    if len(hours) != 2:
        raise ValueError(f"schedule.hours must be [start, end], got {hours!r}")
    start, end = (_parse_hour(h) for h in hours)
    t = now_local.time()
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end               # window crosses midnight, e.g. ["21:00", "02:00"]


def can_post(s: Settings, posted_last_hour: int, posted_today: int, now: datetime | None = None) -> tuple[bool, str]:
    """May the poster touch the site at all? PAUSE, listing hours and the pacing caps.

    HOLD_UNSHIPPED is not checked here: it holds *publishing* only (runner.next_job), drafts and dry-runs still run."""
    sch = s["schedule"]
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(sch["timezone"]))
    if s.flag_set("PAUSE"):
        return False, "PAUSE flag set"
    if not in_hours(local, sch["hours"]):
        return False, f"outside listing hours {sch['hours']}"
    if posted_last_hour >= sch["per_hour_max"]:
        return False, "hourly cap reached"
    if posted_today >= sch["daily_cap"]:
        return False, "daily cap reached"
    return True, "ok"


def windows(now: datetime | None = None, tz: str = "America/New_York") -> tuple[str, str]:
    """ISO UTC timestamps for 'one hour ago' and 'local midnight'."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(tz))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return ((now - timedelta(hours=1)).isoformat(timespec="seconds"), midnight.isoformat(timespec="seconds"))


def next_gap(sch: dict) -> float:
    lo, hi = sch["gap_seconds"]
    gap = random.uniform(lo, hi)
    if random.random() < 0.1:          # occasional longer break
        gap *= random.uniform(2, 4)
    return gap
