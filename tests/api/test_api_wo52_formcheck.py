"""WO52 §3: the 9 PM summary's sites line from the Mac's twice-daily form checks (its heartbeat's `formcheck`)."""
import json
from datetime import UTC, datetime

import pytest
from thrift_api import core, watch


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-10-01T00:00:00+00:00")


def _beat(db, checks):
    db.execute("INSERT INTO heartbeats (source, last_seen, info) VALUES ('mac', ?, ?)",
               ("2026-11-02T20:00:00+00:00", json.dumps({"formcheck": checks})))


def test_the_sites_line_says_both_checks_passed(db, live, sent):
    _beat(db, {"poshmark": {"ok": True, "at": "2026-11-02T19:31:00+00:00", "slot": "14:30", "degraded": False},
               "depop": {"ok": True, "at": "2026-11-02T13:31:00+00:00", "slot": "08:30", "degraded": False},
               "vinted": {"ok": True, "at": "2026-11-02T19:32:00+00:00", "slot": "14:30", "degraded": False}})
    line = watch.evening_line(db, datetime(2026, 11, 3, 2, 5, tzinfo=UTC), "live")       # 21:05 EST Nov 2
    assert line.endswith("\nSites: Poshmark ✓ Depop ✓ Vinted ✓ — form checks 8:30, 2:30 passed.")


def test_a_failed_check_shows_its_site(db, live, sent):
    _beat(db, {"poshmark": {"ok": True, "at": "2026-11-02T19:31:00+00:00", "slot": "14:30"},
               "vinted": {"ok": False, "at": "2026-11-02T19:32:00+00:00", "slot": "14:30", "degraded": True}})
    line = watch.evening_line(db, datetime(2026, 11, 3, 2, 5, tzinfo=UTC), "live")
    assert "Sites: Poshmark ✓ Vinted ✗ — form checks 2:30: one failed" in line


def test_every_slot_checked_today_is_named(db, live, sent):
    _beat(db, {"poshmark": {"ok": True, "at": "2026-11-02T19:31:00+00:00", "slot": "14:30",
                            "today": {"day": "2026-11-02", "passed": ["08:30", "14:30"], "failed": []}}})
    line = watch.evening_line(db, datetime(2026, 11, 3, 2, 5, tzinfo=UTC), "live")
    assert line.endswith("Sites: Poshmark ✓ — form checks 8:30, 2:30 passed.")


def test_no_checks_in_the_heartbeat_no_line(db, live, sent):
    _beat(db, {})
    line = watch.evening_line(db, datetime(2026, 11, 3, 2, 5, tzinfo=UTC), "live")
    assert "Sites:" not in line
