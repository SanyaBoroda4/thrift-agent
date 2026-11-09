"""WO42 → WO53 §4: the Mac's heartbeat missing for 30 minutes between 08:00 and 23:00 local → ONE private line per
outage in plain words (what to check), nothing at night (said in the morning if it is still silent), ONE line when it is
back (the shares waiting in Azure)."""
from datetime import timedelta

import pytest
from apitools import NOW, texts

from thrift_api import core, timers

OFFLINE = ("The Mac stopped answering at 11:00 AM. Is it plugged in, with the lid open? Her photos are safe in the "
           "cloud and will go up when it's back.")


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-10-01T00:00:00+00:00")


def test_the_mac_silent_thirty_daytime_minutes_is_told_once_per_outage_and_its_return_once(db, live, sent):
    assert timers.every_15_min(db, NOW)["mac"] == "never"                       # never beat: nothing missed
    core.heartbeat(db, "mac", {}, NOW)                                          # Thu Oct 8, 11:00 AM New York
    assert timers.mac_watch(db, NOW + timedelta(minutes=29), "live") == "ok"
    assert timers.every_15_min(db, NOW + timedelta(minutes=31))["mac"] == "offline"
    for minutes in (46, 120, 300):
        assert timers.mac_watch(db, NOW + timedelta(minutes=minutes), "live") == "offline"
    assert sent == [("ops", OFFLINE, False)]                                    # once per outage, the private chat
    core.heartbeat(db, "mac", {}, NOW + timedelta(hours=6))                     # 5:00 PM: back
    assert timers.mac_watch(db, NOW + timedelta(hours=6, minutes=1), "live") == "back"
    assert timers.mac_watch(db, NOW + timedelta(hours=6, minutes=2), "live") == "ok"
    assert texts(sent) == [OFFLINE, "Mac back at 5:00 PM."]                     # one line for the return
    assert timers.mac_watch(db, NOW + timedelta(hours=6, minutes=31), "live") == "offline"   # a new outage, 5:31 PM
    assert texts(sent)[-1] == ("The Mac stopped answering at 5:00 PM. Is it plugged in, with the lid open? Her photos "
                               "are safe in the cloud and will go up when it's back.")


def test_the_return_names_the_shares_waiting_in_azure(db, live, sent, monkeypatch):
    core.heartbeat(db, "mac", {}, NOW)
    timers.mac_watch(db, NOW + timedelta(minutes=40), "live")                   # said
    monkeypatch.setattr(timers.uploads, "pending", lambda db_: {"shares": [{"test": False}, {"test": False},
                                                                           {"test": True}]})
    core.heartbeat(db, "mac", {}, NOW + timedelta(hours=10, minutes=12))        # 9:12 PM
    assert timers.mac_watch(db, NOW + timedelta(hours=10, minutes=13), "live") == "back"
    assert texts(sent)[-1] == "Mac back at 9:12 PM — 2 shares waiting, working on them."

def test_a_night_outage_is_told_in_the_morning(db, live, sent):
    core.heartbeat(db, "mac", {}, NOW + timedelta(hours=10))                    # 9:00 PM
    assert timers.mac_watch(db, NOW + timedelta(hours=12, minutes=30), "live") == "night"   # 11:30 PM: nothing
    assert timers.mac_watch(db, NOW + timedelta(hours=19), "live") == "night"               # 6:00 AM
    assert sent == []
    assert timers.mac_watch(db, NOW + timedelta(hours=21, minutes=15), "live") == "offline"  # 8:15 AM
    assert texts(sent) == ["The Mac stopped answering at Thu Oct 8, 9:00 PM EDT. Is it plugged in, with the lid "
                           "open? Her photos are safe in the cloud and will go up when it's back."]
    assert timers.mac_watch(db, NOW + timedelta(hours=22), "live") == "offline" and len(sent) == 1
