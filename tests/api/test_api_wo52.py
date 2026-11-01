"""WO52 §5: the net that needs no reason — a priced item not live on all three sites 2 hours after its price gets ONE
private 🚨 line naming each missing site's reason (live 2026-11-01: the Zara jacket, Vinted's reasons were "hers")."""
from __future__ import annotations

from datetime import timedelta

import pytest
from apitools import NOW, texts

from thrift_api import core, watch


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-10-01T00:00:00+00:00")


def _item(db, priced_at, iid="i_1"):
    core.sync(db, {"items": [{"id": iid, "title": "Zara Moto Jacket", "price": 170, "status": "posted", "priced": True,
                              "test": 0, "priced_at": priced_at.isoformat(), "created_at": priced_at.isoformat(),
                              "updated_at": priced_at.isoformat()}],
                   "listings": [
                       {"item_id": iid, "marketplace": "poshmark", "status": "posted", "url": "https://poshmark.com/x"},
                       {"item_id": iid, "marketplace": "depop", "status": "skipped",
                        "error": "skipped: no size on the item"},
                       {"item_id": iid, "marketplace": "vinted", "status": "queued"}]}, NOW)


def test_two_hours_after_its_price_one_line_whatever_the_reason(db, live, sent):
    _item(db, NOW - timedelta(hours=1))
    assert watch.not_live(db, NOW, "live") == []                        # not 2 hours yet
    assert watch.not_live(db, NOW + timedelta(hours=1, minutes=5), "live") == ["i_1"]
    line = texts(sent)[-1]
    assert line.startswith("🚨 Zara Moto Jacket isn't live everywhere 2 h after its price")
    assert "Depop: skipped: no size on the item" in line and "Vinted: waiting in line" in line
    assert watch.not_live(db, NOW + timedelta(hours=5), "live") == []    # once


def test_history_is_never_replayed_and_a_live_item_is_quiet(db, live, sent):
    _item(db, NOW - timedelta(days=5))
    assert watch.not_live(db, NOW, "live") == []
    _item(db, NOW - timedelta(hours=3), iid="i_2")
    core.sync(db, {"items": [], "listings": [
        {"item_id": "i_2", "marketplace": mp, "status": "posted", "url": f"https://{mp}/x",
         "updated_at": (NOW + timedelta(minutes=1)).isoformat()} for mp in
        ("depop", "vinted")]}, NOW)
    assert watch.not_live(db, NOW, "live") == []
