"""WO51 §6 on the API: the share watchdog fires only when nothing moves. The Mac says what each open share waits for
(`state`, `waiting`, `moved_at` — thrift_agent.shares.refresh_open); a share whose cards wait behind her unanswered
one is never "stuck" (live 2026-11-01, 1:15 PM: "⏳ Share 12:39 PM (70 photos) stuck at carded since 12:44" while its
cards were going out one at a time); a frozen queue, or an item with neither a card nor a place in the queue, is."""
from datetime import timedelta

import pytest
from apitools import NOW, texts

from thrift_api import core, dashboard, watch

SID = "2026-10-08_103925_5986"
HER = 'her price on "Edikted Crochet Mini Dress" — 8 more cards behind it'


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-10-01T00:00:00+00:00")


def _share(db, now, **fields):
    core.heartbeat(db, "mac", {}, now)
    core.sync(db, {"items": [], "listings": [], "shares": [{
        "id": SID, "source": "azure", "expected": 70, "received": 70,
        "received_at": (NOW - timedelta(minutes=10)).isoformat(), "complete_at": (NOW - timedelta(minutes=9)).isoformat(),
        "grouped_at": (NOW - timedelta(minutes=7)).isoformat(), "carded_at": (NOW - timedelta(minutes=5)).isoformat(),
        "updated_at": now.isoformat(), **fields}]}, now)


def test_a_share_waiting_on_her_answer_is_never_stuck_and_said_once_after_two_hours(db, live, sent):
    _share(db, NOW, items=12, cards=4, state="her", waiting=HER, moved_at=(NOW - timedelta(minutes=5)).isoformat())
    row = db.one(f"SELECT * FROM shares WHERE id = '{SID}'")
    assert row["state"] == "her" and row["items"] == 12 and row["waiting"] == HER
    for minutes in (40, 90, 110):                                       # the old rule fired at 30 minutes
        later = NOW + timedelta(minutes=minutes)
        core.heartbeat(db, "mac", {}, later)
        assert watch.shares_watch(db, later, "live") == []
    later = NOW + timedelta(hours=2)
    core.heartbeat(db, "mac", {}, later)
    assert watch.shares_watch(db, later, "live") == [SID]
    assert texts(sent) == ['⏳ Waiting for her price on "Edikted Crochet Mini Dress" — 8 more cards behind it.']
    assert "stuck" not in texts(sent)[0]
    core.heartbeat(db, "mac", {}, later + timedelta(minutes=5))
    assert watch.shares_watch(db, later + timedelta(minutes=5), "live") == []          # once


def test_her_answer_moving_the_queue_keeps_it_quiet(db, live, sent):
    _share(db, NOW, items=12, cards=4, state="her", waiting=HER, moved_at=(NOW - timedelta(minutes=5)).isoformat())
    later = NOW + timedelta(hours=3)
    _share(db, later, cards=9, state="her", waiting=HER.replace("8 more", "3 more"),
           moved_at=(later - timedelta(minutes=20)).isoformat())           # she answered 20 minutes ago
    assert watch.shares_watch(db, later, "live") == [] and sent == []


def test_a_frozen_queue_is_stuck_and_says_what_it_waits_for(db, live, sent):
    waiting = "the next card to go out (8 queued, none open for her)"
    _share(db, NOW, items=12, cards=4, state="send", waiting=waiting, moved_at=(NOW - timedelta(minutes=5)).isoformat())
    later = NOW + timedelta(minutes=20)                                   # 25 minutes still: 70 photos get 30
    core.heartbeat(db, "mac", {}, later)
    assert watch.shares_watch(db, later, "live") == []
    later = NOW + timedelta(minutes=26)
    core.heartbeat(db, "mac", {}, later)
    assert watch.shares_watch(db, later, "live") == [SID]
    assert texts(sent) == ["⏳ Share 10:39 AM (70 photos) stuck since 10:55 AM: waiting for the next card to go out "
                           "(8 queued, none open for her)."]


def test_an_item_with_no_card_and_no_place_in_the_queue_is_stuck(db, live, sent):
    waiting = 'a card for "Alo Yoga Bra" — it has none and no place in the card queue (failed)'
    _share(db, NOW, items=12, cards=11, state="orphan", waiting=waiting,
           moved_at=(NOW - timedelta(minutes=40)).isoformat())
    later = NOW + timedelta(minutes=22)                                   # 31 minutes since the share was complete
    core.heartbeat(db, "mac", {}, later)
    assert watch.shares_watch(db, later, "live") == [SID]
    assert texts(sent)[-1].endswith(f"waiting for {waiting}.")


def test_items_still_being_processed_while_the_worker_moves_are_not_stuck(db, live, sent):
    _share(db, NOW, items=12, cards=2, state="work", waiting="its items to be processed (2 of 12 done)",
           moved_at=(NOW - timedelta(minutes=1)).isoformat())
    for minutes in (20, 45, 70):                                          # an item done every 25 minutes or so
        later = NOW + timedelta(minutes=minutes)
        _share(db, later, cards=minutes // 10, state="work", waiting="its items to be processed",
               moved_at=(later - timedelta(minutes=2)).isoformat())
        assert watch.shares_watch(db, later, "live") == []
    later = NOW + timedelta(minutes=99)                                   # then nothing for 31 minutes
    core.heartbeat(db, "mac", {}, later)
    assert watch.shares_watch(db, later, "live") == [SID]


def test_done_says_nothing_and_a_mac_without_state_keeps_the_old_rule(db, live, sent):
    _share(db, NOW, items=12, cards=12, state="done", moved_at=(NOW - timedelta(hours=3)).isoformat())
    assert watch.shares_watch(db, NOW + timedelta(hours=1), "live") == []
    core.sync(db, {"items": [], "listings": [], "shares": [{
        "id": "2026-10-08_090000", "source": "icloud", "expected": 4, "received": 4,
        "received_at": (NOW - timedelta(minutes=30)).isoformat(), "complete_at": (NOW - timedelta(minutes=20)).isoformat(),
        "updated_at": NOW.isoformat()}]}, NOW)
    core.heartbeat(db, "mac", {}, NOW)
    assert watch.shares_watch(db, NOW, "live") == ["2026-10-08_090000"]
    assert texts(sent)[-1].startswith("⏳ Share 9:00 AM (4 photos) stuck at complete since")


def test_the_dashboard_says_what_an_open_share_waits_for(db, live):
    _share(db, NOW, items=12, cards=4, state="her", waiting=HER, moved_at=NOW.isoformat())
    page = dashboard.render(core.dashboard_data(db, NOW), NOW)
    assert "waiting for her price on" in page
