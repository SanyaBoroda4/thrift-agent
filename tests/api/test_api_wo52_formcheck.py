"""WO52 §3: the 9 PM summary's sites line from the Mac's twice-daily form checks (its heartbeat's `formcheck`)."""
import json
from datetime import UTC, datetime

import pytest
from thrift_api import core, watch


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-10-01T00:00:00+00:00")


def _beat(db, checks, **more):
    db.execute("INSERT INTO heartbeats (source, last_seen, info) VALUES ('mac', ?, ?)",      # 9:00 PM EST: online
               ("2026-11-03T02:00:00+00:00", json.dumps({"formcheck": checks, **more})))


def test_the_sites_line_says_both_checks_passed(db, live, sent):
    _beat(db, {"poshmark": {"ok": True, "at": "2026-11-02T19:31:00+00:00", "slot": "14:30", "degraded": False},
               "depop": {"ok": True, "at": "2026-11-02T13:31:00+00:00", "slot": "08:30", "degraded": False},
               "vinted": {"ok": True, "at": "2026-11-02T19:32:00+00:00", "slot": "14:30", "degraded": False}})
    line = watch.evening_line(db, datetime(2026, 11, 3, 2, 5, tzinfo=UTC), "live")       # 21:05 EST Nov 2
    assert line.endswith("\nSites: Poshmark ✓ · Depop ✓ · Vinted ✓ — form checks today: Poshmark 2:30 ✓, Depop 8:30 ✓, "
                         "Vinted 2:30 ✓.")


def test_a_failed_check_shows_its_site(db, live, sent):
    _beat(db, {"poshmark": {"ok": True, "at": "2026-11-02T19:31:00+00:00", "slot": "14:30"},
               "vinted": {"ok": False, "at": "2026-11-02T19:32:00+00:00", "slot": "14:30", "degraded": True}})
    line = watch.evening_line(db, datetime(2026, 11, 3, 2, 5, tzinfo=UTC), "live")
    assert "Sites: Poshmark ✓ · Vinted ✗ form check 2:30 failed — form checks today: Poshmark 2:30 ✓, Vinted 2:30 ✗." \
        in line
    assert "🚨" not in line                                               # WO53: no alarm said, none pointed to


def test_every_slot_checked_today_is_named(db, live, sent):
    _beat(db, {"poshmark": {"ok": True, "at": "2026-11-02T19:31:00+00:00", "slot": "14:30",
                            "today": {"day": "2026-11-02", "passed": ["08:30", "14:30"], "failed": []}}})
    line = watch.evening_line(db, datetime(2026, 11, 3, 2, 5, tzinfo=UTC), "live")
    assert line.endswith("Sites: Poshmark ✓ — form checks today: Poshmark 8:30 ✓ 2:30 ✓.")


def test_no_checks_in_the_heartbeat_no_line(db, live, sent):
    _beat(db, {})
    line = watch.evening_line(db, datetime(2026, 11, 3, 2, 5, tzinfo=UTC), "live")
    assert "Sites:" not in line


def test_tonights_line_names_each_site_s_real_state_and_no_alarm_that_wasnt_sent(db, live, sent):
    """WO53 §3 (live 2026-11-08 9 PM: "Poshmark ✓ Depop ✗ Vinted ✗ — form checks 8:30, 2:30: one failed — see the 🚨
    line" — two ✗ from Nov 7's checks, "one failed", and no 🚨 anywhere): each site for what it is."""
    _beat(db, {"poshmark": {"ok": True, "at": "2026-11-02T19:48:41+00:00", "slot": "14:30",
                            "today": {"day": "2026-11-02", "passed": ["08:30", "14:30"], "failed": []}},
               "vinted": {"ok": False, "degraded": True, "at": "2026-11-01T20:27:52+00:00", "slot": "14:30",
                          "today": {"day": "2026-11-01", "passed": [], "failed": ["14:30"]}}},
          sites={"depop": {"kind": "account", "page": "login", "since": "2026-11-01T21:04:56+00:00"},
                 "vinted": {"kind": "account", "page": "login", "since": "2026-11-01T21:05:03+00:00"}})
    line = watch.evening_line(db, datetime(2026, 11, 3, 2, 5, tzinfo=UTC), "live")
    assert ("Sites: Poshmark ✓ · Depop ✗ logged out since Sun Nov 1, 4:04 PM EST · Vinted ✗ logged out since Sun "
            "Nov 1, 4:05 PM EST — form checks today: Poshmark 8:30 ✓ 2:30 ✓.") in line
    assert "🚨" not in line and "one failed" not in line


def test_an_alarm_said_is_pointed_to(db, live, sent):
    _beat(db, {}, sites={"depop": {"kind": "account", "page": "login", "since": "2026-11-02T21:04:56+00:00"}},
          alarms={"depop": ["2026-11-02T21:30:00+00:00"]})
    line = watch.evening_line(db, datetime(2026, 11, 3, 2, 5, tzinfo=UTC), "live")
    assert "Depop ✗ logged out since 4:04 PM (see the 🚨 line)" in line


def test_the_mac_offline_at_9_pm_says_so_and_counts_azures_shares(db, live, sent):
    db.execute("INSERT INTO heartbeats (source, last_seen, info) VALUES ('mac', ?, NULL)",
               ("2026-11-02T22:19:00+00:00",))                                              # 5:19 PM EST, then silent
    db.execute("INSERT INTO shares_in (share_id, count, received, started_at, test) VALUES (?, 3, 3, ?, 0)",
               ("2026-11-02_203227_4613", "2026-11-03T01:32:27+00:00"))                     # 8:32 PM: in Azure only
    line = watch.evening_line(db, datetime(2026, 11, 3, 2, 5, tzinfo=UTC), "live")
    assert line.startswith("📊 Today (the Mac is offline since 5:19 PM — its figures as of then): 1 share → 0 items")
