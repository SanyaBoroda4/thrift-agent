"""WO42: the Mac's heartbeat missing for 2 hours between 08:00 and 23:00 local → ONE private line per outage, nothing
at night (said in the morning if it is still silent), nothing when it is back."""
from datetime import timedelta

import pytest
from apitools import NOW, texts

from thrift_api import core, timers

OFFLINE = "The Mac has been offline since Thu Oct 8, 11:00 AM EDT — shares and posting wait until it's open."


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-10-01T00:00:00+00:00")


def test_the_mac_silent_two_daytime_hours_is_told_once_per_outage(db, live, sent):
    assert timers.every_15_min(db, NOW)["mac"] == "never"                       # never beat: nothing missed
    core.heartbeat(db, "mac", {}, NOW)                                          # Thu Oct 8, 11:00 AM New York
    assert timers.mac_watch(db, NOW + timedelta(minutes=119), "live") == "ok"
    assert timers.every_15_min(db, NOW + timedelta(minutes=121))["mac"] == "offline"
    for minutes in (136, 180, 300):
        assert timers.mac_watch(db, NOW + timedelta(minutes=minutes), "live") == "offline"
    assert sent == [("ops", OFFLINE, False)]                                    # once per outage, the private chat
    core.heartbeat(db, "mac", {}, NOW + timedelta(hours=6))                     # 5:00 PM: back
    assert timers.mac_watch(db, NOW + timedelta(hours=6, minutes=1), "live") == "back"
    assert timers.mac_watch(db, NOW + timedelta(hours=6, minutes=2), "live") == "ok"
    assert texts(sent) == [OFFLINE]                                             # nothing for the return
    assert timers.mac_watch(db, NOW + timedelta(hours=8, minutes=1), "live") == "offline"   # a new outage, 7:01 PM
    assert texts(sent) == [OFFLINE, "The Mac has been offline since Thu Oct 8, 5:00 PM EDT — shares and posting wait "
                                    "until it's open."]


def test_a_night_outage_is_told_in_the_morning(db, live, sent):
    core.heartbeat(db, "mac", {}, NOW + timedelta(hours=10))                    # 9:00 PM
    assert timers.mac_watch(db, NOW + timedelta(hours=12, minutes=30), "live") == "night"   # 11:30 PM: nothing
    assert timers.mac_watch(db, NOW + timedelta(hours=19), "live") == "night"               # 6:00 AM
    assert sent == []
    assert timers.mac_watch(db, NOW + timedelta(hours=21, minutes=15), "live") == "offline"  # 8:15 AM
    assert texts(sent) == ["The Mac has been offline since Thu Oct 8, 9:00 PM EDT — shares and posting wait until "
                           "it's open."]
    assert timers.mac_watch(db, NOW + timedelta(hours=22), "live") == "offline" and len(sent) == 1
