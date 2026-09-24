import re
from datetime import datetime, timezone

from thrift_agent import db as dbmod
from thrift_agent.config import Settings
from thrift_agent.scheduler import can_post, in_hours, windows


def _settings(tmp_path, **sched):
    return Settings({"schedule": {"timezone": "America/New_York", "hours": ["09:00", "21:00"], "per_hour_max": 10,
                                  "daily_cap": 25, "gap_seconds": [150, 420], **sched},
                     "paths": {"control": str(tmp_path)}})


def test_in_hours():
    assert in_hours(datetime(2026, 9, 24, 9, 0), ["09:00", "21:00"])
    assert in_hours(datetime(2026, 9, 24, 21, 0), ["09:00", "21:00"])
    assert not in_hours(datetime(2026, 9, 24, 21, 1), ["09:00", "21:00"])
    assert not in_hours(datetime(2026, 9, 24, 3, 0), ["09:00", "21:00"])


def test_can_post_windows_and_caps(tmp_path):
    s = _settings(tmp_path)
    noon_ny = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)      # 12:00 EDT; zoneinfo needs tzdata on Windows
    assert can_post(s, 0, 0, noon_ny) == (True, "ok")
    assert can_post(s, 0, 0, datetime(2026, 9, 24, 3, 0, tzinfo=timezone.utc))[0] is False   # 23:00 EDT
    assert can_post(s, 10, 0, noon_ny)[1] == "hourly cap reached"
    assert can_post(s, 0, 25, noon_ny)[1] == "daily cap reached"


def test_flags_stop_everything(tmp_path):
    s = _settings(tmp_path)
    noon_ny = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)
    (tmp_path / "HOLD_UNSHIPPED").touch()
    assert "unshipped" in can_post(s, 0, 0, noon_ny)[1]
    (tmp_path / "PAUSE").touch()
    assert can_post(s, 0, 0, noon_ny) == (False, "PAUSE flag set")


def test_windows_match_the_db_timestamp_format():
    now = datetime(2026, 9, 24, 14, 30, tzinfo=timezone.utc)
    hour_ago, midnight = windows(now, "America/New_York")
    assert hour_ago == "2026-09-24T13:30:00+00:00"
    assert midnight == "2026-09-24T04:00:00+00:00"                  # local midnight EDT (UTC-4)
    iso = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
    assert iso.match(dbmod.now()) and iso.match(hour_ago)         # posted_at >= since is a string compare
