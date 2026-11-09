"""WO54 §3: the owner's quiet hours (8:30 AM - 2:00 PM America/New_York, every day), DST-safe."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from thrift_agent import quiet
from thrift_agent.db import DB

NY = ZoneInfo("America/New_York")
pytestmark = pytest.mark.quiet_on


def at(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=NY).astimezone(timezone.utc)


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "state.db")


def test_the_window_by_the_minute():
    assert not quiet.is_quiet(at(2026, 11, 9, 8, 29, 59))
    assert quiet.is_quiet(at(2026, 11, 9, 8, 30))
    assert quiet.is_quiet(at(2026, 11, 9, 13, 59, 59))
    assert not quiet.is_quiet(at(2026, 11, 9, 14, 0))
    assert not quiet.is_quiet(at(2026, 11, 9, 3, 0))                 # posting at 3 AM runs (rule 2)
    assert not quiet.is_quiet(at(2026, 11, 9, 23, 0))


def test_nothing_new_starts_from_8_20():
    assert not quiet.closing(at(2026, 11, 10, 8, 19, 59))
    assert quiet.closing(at(2026, 11, 10, 8, 20))
    assert quiet.closing(at(2026, 11, 10, 11, 0))
    assert not quiet.closing(at(2026, 11, 10, 14, 0))
    assert quiet.resume_at(at(2026, 11, 10, 8, 25)) == at(2026, 11, 10, 14, 0)
    assert quiet.resume_at(at(2026, 11, 10, 15, 0)) is None


@pytest.mark.parametrize("day,utc_hour", [
    ((2026, 3, 7), 13),      # EST (UTC-5): 8:30 local = 13:30 UTC
    ((2026, 3, 8), 12),      # spring forward that night: EDT (UTC-4), 8:30 local = 12:30 UTC
    ((2026, 10, 31), 12),    # EDT
    ((2026, 11, 1), 13),     # fall back that night: EST again
])
def test_dst_keeps_8_30_at_8_30(day, utc_hour):
    a, b = quiet.window_of(datetime(*day).date(), quiet.cfg())
    assert (a.hour, a.minute) == (utc_hour, 30)
    assert a.astimezone(NY).strftime("%H:%M") == "08:30" and b.astimezone(NY).strftime("%H:%M") == "14:00"
    assert (b - a) == timedelta(hours=5, minutes=30)


def test_active_time_leaves_out_the_quiet_hours():
    # 8:00 → 15:00 is 7 h, 5.5 of them quiet
    assert quiet.active_seconds(at(2026, 11, 9, 8, 0), at(2026, 11, 9, 15, 0)) == 1.5 * 3600
    # inside the window: no time passes
    assert quiet.active_seconds(at(2026, 11, 9, 9, 0), at(2026, 11, 9, 13, 0)) == 0
    # across two days: two windows out
    assert quiet.active_seconds(at(2026, 11, 9, 7, 0), at(2026, 11, 10, 15, 0)) == (32 - 11) * 3600
    # across the spring-forward night (one hour shorter) — still exactly the window out
    assert quiet.active_seconds(at(2026, 3, 7, 20, 0), at(2026, 3, 8, 15, 0)) == (18 - 5.5) * 3600


def test_post_now_lifts_today_only_and_quiet_now_brings_them_back(db):
    now = at(2026, 11, 9, 10, 0)
    assert quiet.is_quiet(now, db)
    assert quiet.set_post_now(db, now) == "2026-11-09"
    assert not quiet.is_quiet(now, db) and not quiet.closing(now, db) and quiet.lifted(now, db)
    assert quiet.is_quiet(at(2026, 11, 10, 10, 0), db)               # tomorrow: quiet again
    assert quiet.beat(db, now=now)["override"] == {"date": "2026-11-09", "mode": "post_now"}
    assert quiet.clear_override(db) is True
    assert quiet.is_quiet(now, db) and quiet.beat(db, now=now)["override"] is None


def test_settings_typos_never_break_and_days_can_be_named(settings_override):
    settings_override(quiet={"start": 510, "end": "14:00", "days": ["mon", "Tue"], "timezone": "Nowhere/Else"})
    q = quiet.cfg()
    assert q.start.strftime("%H:%M") == "08:30" and q.tz_name == "America/New_York"
    assert quiet.is_quiet(at(2026, 11, 9, 9, 0))                     # a Monday
    assert not quiet.is_quiet(at(2026, 11, 11, 9, 0))                # a Wednesday
    assert q.label() == "8:30 AM – 2:00 PM"


def test_off_means_off(settings_override):
    settings_override(quiet={"enabled": False})
    assert not quiet.is_quiet(at(2026, 11, 9, 9, 0)) and not quiet.closing(at(2026, 11, 9, 9, 0))
    assert quiet.active_seconds(at(2026, 11, 9, 8, 0), at(2026, 11, 9, 15, 0)) == 7 * 3600


def test_the_heartbeat_carries_them():
    b = quiet.beat(now=at(2026, 11, 9, 9, 0))
    assert b == {"enabled": True, "start": "08:30", "end": "14:00", "timezone": "America/New_York", "days": "all",
                 "allow_takedowns": False, "override": None}


@pytest.mark.quiet_on
def test_every_other_test_has_them_off_unless_asked():
    assert quiet.cfg().enabled is True
