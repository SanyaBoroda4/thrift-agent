import re
from datetime import datetime, time, timezone

import pytest

from thrift_agent import db as dbmod
from thrift_agent.config import Settings
from thrift_agent.scheduler import _parse_hour, can_post, in_hours, windows


def _settings(tmp_path, **sched):
    return Settings({"schedule": {"timezone": "America/New_York", "hours": ["09:00", "21:00"], "per_hour_max": 10,
                                  "daily_cap": 25, "gap_seconds": [150, 420], **sched},
                     "paths": {"control": str(tmp_path)}})


def test_in_hours():
    assert in_hours(datetime(2026, 9, 24, 9, 0), ["09:00", "21:00"])
    assert in_hours(datetime(2026, 9, 24, 21, 0), ["09:00", "21:00"])
    assert not in_hours(datetime(2026, 9, 24, 21, 1), ["09:00", "21:00"])
    assert not in_hours(datetime(2026, 9, 24, 3, 0), ["09:00", "21:00"])


def test_parse_hour_accepts_yaml_ints_and_unpadded_strings():
    assert _parse_hour(540) == time(9, 0)               # YAML 1.1: an unquoted 9:00 is the sexagesimal int 540
    assert _parse_hour("9:00") == time(9, 0)
    assert _parse_hour(" 21:00 ") == time(21, 0)
    assert _parse_hour(time(7, 30)) == time(7, 30)
    for bad in (None, 1.5, "9", "25:00", "9pm", 1440, -1, True):
        with pytest.raises(ValueError, match="schedule.hours"):
            _parse_hour(bad)


def test_in_hours_accepts_yaml_ints_and_unpadded_strings():
    assert in_hours(datetime(2026, 9, 24, 12, 0), [540, 1260])
    assert in_hours(datetime(2026, 9, 24, 12, 0), ["9:00", "21:00"])
    assert not in_hours(datetime(2026, 9, 24, 8, 59), [540, 1260])
    with pytest.raises(ValueError, match="start, end"):
        in_hours(datetime(2026, 9, 24, 12, 0), ["09:00"])


def test_in_hours_window_crossing_midnight():
    night = ["21:00", "02:00"]
    assert in_hours(datetime(2026, 9, 24, 23, 0), night)
    assert in_hours(datetime(2026, 9, 24, 1, 0), night)
    assert not in_hours(datetime(2026, 9, 24, 12, 0), night)


def test_can_post_windows_and_caps(tmp_path):
    s = _settings(tmp_path)
    noon_ny = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)      # 12:00 EDT; zoneinfo needs tzdata on Windows
    assert can_post(s, 0, 0, noon_ny) == (True, "ok")
    assert can_post(s, 0, 0, datetime(2026, 9, 24, 3, 0, tzinfo=timezone.utc))[0] is False   # 23:00 EDT
    assert can_post(s, 10, 0, noon_ny)[1] == "hourly cap reached"
    assert can_post(s, 0, 25, noon_ny)[1] == "daily cap reached"


def test_pause_stops_everything_hold_unshipped_does_not(tmp_path):
    """HOLD_UNSHIPPED holds publishing only (runner.next_job); drafts and dry-runs may still touch the site."""
    s = _settings(tmp_path)
    noon_ny = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)
    (tmp_path / "HOLD_UNSHIPPED").touch()
    assert s.flag_set("HOLD_UNSHIPPED")
    assert can_post(s, 0, 0, noon_ny) == (True, "ok")
    (tmp_path / "PAUSE").touch()
    assert can_post(s, 0, 0, noon_ny) == (False, "PAUSE flag set")


def test_flags_from_the_iphone_are_seen(tmp_path):
    s = _settings(tmp_path)
    noon_ny = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)
    (tmp_path / "PAUSE.txt").touch()                     # iOS Files / Shortcuts add the extension
    assert can_post(s, 0, 0, noon_ny) == (False, "PAUSE flag set")
    (tmp_path / "PAUSE.txt").unlink()
    assert can_post(s, 0, 0, noon_ny) == (True, "ok")
    (tmp_path / ".HOLD_UNSHIPPED.txt.icloud").touch()    # placeholder until the Mac downloads it
    assert s.flag_set("HOLD_UNSHIPPED")                  # the runner reads it; can_post stays open for drafts
    assert can_post(s, 0, 0, noon_ny) == (True, "ok")


def test_windows_match_the_db_timestamp_format():
    now = datetime(2026, 9, 24, 14, 30, tzinfo=timezone.utc)
    hour_ago, midnight = windows(now, "America/New_York")
    assert hour_ago == "2026-09-24T13:30:00+00:00"
    assert midnight == "2026-09-24T04:00:00+00:00"                  # local midnight EDT (UTC-4)
    iso = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
    assert iso.match(dbmod.now()) and iso.match(hour_ago)         # posted_at >= since is a string compare
