"""Cancelled sales relisted everywhere (the owner, 2026-11-11), thrift-api's side: a sale cancelled before it shipped
asks the Mac to relist its item (GET /tasks "relist") until the Mac says it is done; a return after it shipped is ONE
private line and waits for her "back <words>"; never twice."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from apitools import call

from thrift_api import core, telegram

NOW = datetime(2026, 11, 12, 3, 30, tzinfo=timezone.utc)


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-10-01T00:00:00+00:00")


@pytest.fixture
def out(monkeypatch):
    log = []
    monkeypatch.setattr(telegram, "SENDER", lambda chat, text, silent, html=False: log.append((chat, text)) or True)
    return log


def sale(db, sid, mp, title, *, item_id="i_1", order_id=None, shipped_at=None, status="matched"):
    db.execute("INSERT INTO items (id, title, created_at, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
               (item_id, title, "2026-10-05T00:00:00+00:00", "2026-10-05T00:00:00+00:00")) if item_id else None
    db.execute("INSERT INTO sales (id, marketplace, item_id, title_seen, price, order_id, sold_at, ship_by, shipped_at, "
               "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (sid, mp, item_id, title, 90.0, order_id, "2026-11-06T17:19:53+00:00", "2026-11-11", shipped_at, status,
                "2026-11-06T17:19:59+00:00"))


def tasks(db):
    status, body, _ = call(db, "GET", "/tasks", query={"sites": "poshmark,depop,vinted"}, now=NOW)
    assert status == 200
    return body.get("relist") or []


def orders(db, mp, *entries):
    status, body, _ = call(db, "POST", "/orders", {"marketplace": mp, "swept_at": NOW.isoformat(), "orders": list(entries)},
                           now=NOW)
    assert status == 200, body
    return body


def event(db, body):
    status, out_, _ = call(db, "POST", "/mac-event", body, now=NOW)
    assert status == 200, out_
    return out_


def test_a_sale_cancelled_before_it_shipped_asks_the_mac_to_relist_until_it_is_done(db, live, out):
    sale(db, "s_seven", "depop", "Seven Wonders Fuzzy Knit Top & Maxi…", item_id="i_seven", order_id="479856215684")
    orders(db, "depop", {"order_id": "479856215684", "status": "cancelled", "status_text": "refunded / AWAITING_SHIPPING",
                         "sold_at": "2026-11-06T17:19:45Z"})
    assert tasks(db) == [{"sale_id": "s_seven", "item_id": "i_seven", "marketplace": "depop", "why": "cancelled"}]
    assert tasks(db)                                                       # asked again until the Mac says done
    assert event(db, {"kind": "relisted", "sale_id": "s_seven", "sites": ["poshmark", "vinted", "depop"]})["status"] == "done"
    assert tasks(db) == []                                                 # never twice
    assert event(db, {"kind": "relisted", "sale_id": "s_seven"})["status"] == "not wanted"
    [ops] = [t for c, t in out if c == "ops" and t.startswith("Cancelled:")]
    assert "It goes back on every site it came down from" in ops


@pytest.mark.parametrize("how", ["her words", "an email"])
def test_every_kind_of_cancel_before_shipping_relists(db, live, out, how):
    sale(db, "s_1", "poshmark", "Lacoste Tee White size M", order_id="6702bb11cc22dd33ee44ff55")
    if how == "her words":
        assert event(db, {"kind": "cancelled", "words": "lacoste tee"})["status"] == "cancelled"
    else:
        from apitools import followup
        core.process_email(db, followup("poshmark", "CANCELLED", "posh-cancel", order="6702bb11cc22dd33ee44ff55"), NOW)
    assert [r["sale_id"] for r in tasks(db)] == ["s_1"]


def test_a_cancel_after_it_shipped_or_of_her_own_ebay_listing_is_never_relisted(db, live, out):
    sale(db, "s_shipped", "poshmark", "Lacoste Tee White size M", order_id="6702bb11cc22dd33ee44ff55",
         shipped_at="2026-11-07T15:00:00+00:00")
    core._cancel(db, db.one("SELECT * FROM sales WHERE id = 's_shipped'"), "Lacoste Tee", "live", core.Outbox(NOW, "live"))
    sale(db, "s_hers", "ebay", "Vintage Coach Bag", item_id=None, order_id="22-15011-34002")
    core._cancel(db, db.one("SELECT * FROM sales WHERE id = 's_hers'"), "Vintage Coach Bag", "live", core.Outbox(NOW, "live"))
    assert tasks(db) == []


def test_a_return_after_shipping_is_one_private_line_and_relists_only_on_her_back_words(db, live, out):
    sale(db, "s_ret", "depop", "Frame Le High Straight Jeans", item_id="i_frame", order_id="4800",
         shipped_at="2026-11-08T15:00:00+00:00")
    orders(db, "depop", {"order_id": "4800", "status": "returned", "status_text": "refunded / DELIVERED",
                         "sold_at": "2026-11-06T17:19:45Z"})
    orders(db, "depop", {"order_id": "4800", "status": "returned", "status_text": "refunded / DELIVERED",
                         "sold_at": "2026-11-06T17:19:45Z"})                # the next sweep: said once
    lines = [t for c, t in out if c == "ops" and "is being returned" in t]
    assert lines == ["Frame Le High Straight Jeans is being returned — reply 'back <words>' when it's with you and "
                     "I'll relist it everywhere."]
    assert not [t for c, t in out if c == "group"] and tasks(db) == []      # never relisted by itself
    assert event(db, {"kind": "back", "words": "nothing like it"})["status"] == "no_match"
    got = event(db, {"kind": "back", "words": "frame jeans"})
    assert got["status"] == "relisting" and got["matched"] == "s_ret"
    assert tasks(db) == [{"sale_id": "s_ret", "item_id": "i_frame", "marketplace": "depop", "why": "back"}]
    event(db, {"kind": "relisted", "sale_id": "s_ret"})
    assert tasks(db) == [] and event(db, {"kind": "back", "words": "frame jeans"})["status"] == "no_match"


def test_nothing_is_asked_for_outside_live(db, out, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "replay")
    sale(db, "s_1", "poshmark", "Lacoste Tee White size M")
    db.execute("UPDATE sales SET relist = 'wanted', relist_why = 'cancelled' WHERE id = 's_1'")
    assert tasks(db) == []
