"""When is the poster allowed to act? Pure functions over settings + DB counts."""
from __future__ import annotations

import random
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from thrift_agent.config import Settings


def in_hours(now_local: datetime, hours: list[str]) -> bool:
    start, end = (time.fromisoformat(h) for h in hours)
    return start <= now_local.time() <= end


def can_post(s: Settings, posted_last_hour: int, posted_today: int, now: datetime | None = None) -> tuple[bool, str]:
    sch = s["schedule"]
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(sch["timezone"]))
    if s.flag("PAUSE").exists():
        return False, "PAUSE flag set"
    if s.flag("HOLD_UNSHIPPED").exists():
        return False, "unshipped orders — ship first"
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
