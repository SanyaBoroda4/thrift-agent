"""WO65 — the 9 PM list showed shipped and cancelled sales as still to ship (live 2026-11-11). The orders were read by
the sweep but never tied: Depop's receipts carry no title and three Depop sales sold within 3 days of each other; a
Vinted sale recorded twice (its first email six days before the label's) kept its first row open. Built from that
night's real (scrubbed) orders and sale rows."""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

import pytest
from apitools import call

from thrift_api import core, shiplist, telegram, timers

NOW = datetime(2026, 11, 12, 2, 36, tzinfo=timezone.utc)            # Wed Nov 11, 9:36 PM New York
NINE_PM = datetime(2026, 11, 12, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-10-01T00:00:00+00:00")


@pytest.fixture
def out(monkeypatch):
    log = []
    monkeypatch.setattr(telegram, "SENDER",
                        lambda chat, text, silent, html=False: log.append((chat, text)) or True)
    return log


def sale(db, sid, mp, title, sold_at, *, item_id=None, order_id=None, status="matched", ship_by="2026-11-13",
         price=50.0, shipped_at=None, delivered_at=None):
    db.execute("INSERT INTO sales (id, marketplace, item_id, title_seen, price, order_id, sold_at, ship_by, "
               "ship_by_source, shipped_at, delivered_at, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
               "?, ?)", (sid, mp, item_id, title, price, order_id, sold_at, ship_by, "rule", shipped_at, delivered_at,
                        status, sold_at))


def report(db, mp, *entries, now=NOW):
    status, body, _ = call(db, "POST", "/orders", {"marketplace": mp, "swept_at": now.isoformat(),
                                                   "orders": list(entries)}, now=now)
    assert status == 200, body
    return {r["order_id"]: r for r in body["results"]}


def row(db, sid):
    return db.one("SELECT * FROM sales WHERE id = ?", (sid,))


def depop_three(db):
    """Tonight's three Depop sales: their emails name no order, and they sold within 3 days of each other."""
    sale(db, "s_jen", "depop", "Jen's Pirate Booty Dusty Blue Floral Maxi…", "2026-11-05T21:04:37+00:00",
         item_id="i_jen", price=62, ship_by="2026-11-10")
    sale(db, "s_seven", "depop", "Seven Wonders Fuzzy Knit Top & Maxi…", "2026-11-06T17:19:53+00:00",
         item_id="i_seven", price=90, ship_by="2026-11-11")
    sale(db, "s_tide", "depop", "Southern Tide New Haisley Stripe Crew Neck…", "2026-11-08T12:06:35+00:00",
         item_id="i_tide", price=40, ship_by="2026-11-13")


def receipt(order_id, status, text, sold_at, **kw):
    """A Depop receipt as the sweep sends it: no title (Depop's receipts carry none)."""
    return {"order_id": order_id, "status": status, "status_text": text, "title": None, "sold_at": sold_at, **kw}


def on_list(db, now=NINE_PM):
    return [s.id for s, _ in shiplist.open_sales(db, now)[0]]


def test_a_depop_order_shipped_ties_to_the_sale_sold_seconds_from_it_and_leaves_the_list(db, live, out):
    depop_three(db)
    got = report(db, "depop", receipt("479781545898", "shipped", "shipped / IN_TRANSIT", "2026-11-05T21:04:25.971224Z",
                                      shipped_at="2026-11-09T21:44:34.000Z"))
    assert got["479781545898"]["sale_id"] == "s_jen" and got["479781545898"]["action"].startswith("linked")
    assert row(db, "s_jen")["shipped_at"] == "2026-11-09T21:44:34+00:00" and row(db, "s_jen")["order_id"] == "479781545898"
    assert "s_jen" not in on_list(db) and {"s_seven", "s_tide"} <= set(on_list(db))
    assert not [t for c, t in out if c == "group"]                         # shipped is recorded quietly


def test_a_depop_order_cancelled_cancels_its_sale_and_never_says_a_sale(db, live, out):
    depop_three(db)
    got = report(db, "depop", receipt("479856215684", "cancelled", "refunded / AWAITING_SHIPPING",
                                      "2026-11-06T17:19:45.515561Z"))
    assert got["479856215684"]["sale_id"] == "s_seven" and row(db, "s_seven")["status"] == "cancelled"
    assert "s_seven" not in on_list(db)
    group = [t for c, t in out if c == "group"]
    assert not any(t.startswith("💰") for t in group)                       # no sale line
    assert group == ["↩️ Seven Wonders Fuzzy Knit Top & Maxi…'s sale on Depop was cancelled."]


def test_all_three_of_tonights_depop_receipts_tie_each_to_its_own(db, live):
    depop_three(db)
    got = report(db, "depop",
                 receipt("480013913120", "to_ship", "shipping_pending / AWAITING_SHIPPING", "2026-11-08T12:06:27.646538Z"),
                 receipt("479856215684", "cancelled", "refunded / AWAITING_SHIPPING", "2026-11-06T17:19:45.515561Z"),
                 receipt("479781545898", "shipped", "shipped / IN_TRANSIT", "2026-11-05T21:04:25.971224Z"))
    assert {k: v["sale_id"] for k, v in got.items()} == {"480013913120": "s_tide", "479856215684": "s_seven",
                                                         "479781545898": "s_jen"}
    assert on_list(db) == ["s_tide"]                                         # the one really still to ship


def test_a_depop_order_far_from_every_sale_is_still_never_a_guess(db, live, out):
    depop_three(db)
    got = report(db, "depop", receipt("4799", "shipped", "shipped / IN_TRANSIT", "2026-11-07T03:00:00Z"))
    assert got["4799"]["sale_id"] is None and "several" in got["4799"]["action"]
    [line] = [t for c, t in out if c == "ops"]
    assert line.startswith("🔎 Depop order 4799") and "Jen's Pirate Booty" in line and "never a guess" in line
    report(db, "depop", receipt("4799", "shipped", "shipped / IN_TRANSIT", "2026-11-07T03:00:00Z"))
    assert len([t for c, t in out if c == "ops"]) == 1                       # said once


def test_a_vinted_sale_recorded_twice_is_folded_when_its_order_is_tied_and_leaves_the_list(db, live):
    """Unicorn head: the "You sold" email (Oct 28, no order id) and the label email six days later (its row tied to the
    order, shipped, delivered) — the first row stayed open and showed 7 days late."""
    sale(db, "s_first", "vinted", "Unicorn head", "2026-10-28T14:10:06+00:00", status="unmatched", price=9.6,
         ship_by="2026-11-04")
    sale(db, "s_label", "vinted", "Unicorn head", "2026-11-03T22:06:52+00:00", order_id="23674345744", status="done",
         price=None, ship_by="2026-11-04", shipped_at="2026-11-04T17:07:36+00:00",
         delivered_at="2026-11-07T19:05:45+00:00")
    assert "s_first" in on_list(db)
    got = report(db, "vinted", {"order_id": "23674345744", "status": "completed",
                                "status_text": "Order completed successfully. The buyer accepted the item.",
                                "title": "Unicorn head", "sold_at": "2026-10-28T14:09:49Z", "price": "9.6"})
    assert got["23674345744"]["sale_id"] == "s_label" and "twin s_first folded in" in got["23674345744"]["action"]
    assert row(db, "s_first")["status"] == "merged"                         # kept, never deleted
    kept = row(db, "s_label")
    assert kept["price"] == 9.6 and kept["sold_at"] == "2026-10-28T14:10:06+00:00"
    assert "s_first" not in on_list(db) and on_list(db) == []


def test_a_vinted_order_completed_of_an_item_not_ours_ties_by_title_and_leaves_the_list(db, live):
    sale(db, "s1", "vinted", "Lacoste T-shirt size 4", "2026-10-04T13:08:30+00:00", status="unmatched")
    got = report(db, "vinted", {"order_id": "22859497475", "status": "completed",
                                "status_text": "Order completed successfully. The buyer accepted the item.",
                                "title": "Lacoste T-shirt size 4", "sold_at": "2026-10-04T13:08:20Z"})
    assert got["22859497475"]["sale_id"] == "s1" and row(db, "s1")["status"] == "done"
    assert on_list(db) == []


def test_two_sales_of_the_same_title_in_the_window_stay_untied_with_one_line_naming_both(db, live, out):
    sale(db, "s_a", "vinted", "Book about basketball", "2026-11-11T13:01:00+00:00", status="unmatched", price=3)
    sale(db, "s_b", "vinted", "Book about basketball", "2026-11-11T14:19:47+00:00", status="unmatched", price=3)
    got = report(db, "vinted", {"order_id": "24141255168", "status": "shipped", "status_text": "Shipped",
                                "title": "Book about basketball", "sold_at": "2026-11-11T13:01:05Z"})
    assert got["24141255168"]["sale_id"] is None
    [line] = [t for c, t in out if c == "ops"]
    assert "s_a" in line and "s_b" in line and "never a guess" in line
    assert {"s_a", "s_b"} <= set(on_list(db))


def test_her_shipped_and_cancelled_words_close_a_sale_at_once(db, live, out):
    sale(db, "s_unicorn", "vinted", "Unicorn head", "2026-10-28T14:10:06+00:00", status="unmatched", price=9.6)
    sale(db, "s_seven", "depop", "Seven Wonders Fuzzy Knit Top & Maxi…", "2026-11-06T17:19:53+00:00", item_id="i_seven")
    status, body, _ = call(db, "POST", "/mac-event", {"kind": "shipped", "words": "unicorn"}, now=NOW)
    assert status == 200 and body["matched"] == "s_unicorn" and row(db, "s_unicorn")["shipped_at"]
    status, body, _ = call(db, "POST", "/mac-event", {"kind": "cancelled", "words": "seven wonders"}, now=NOW)
    assert status == 200 and body["status"] == "cancelled" and row(db, "s_seven")["status"] == "cancelled"
    assert on_list(db) == []
    status, body, _ = call(db, "POST", "/mac-event", {"kind": "cancelled", "words": "nothing like it"}, now=NOW)
    assert body["status"] == "no_match"


def test_the_list_says_when_each_site_was_checked_and_names_one_not_checked_for_14_hours(db, live, out):
    sale(db, "s1", "depop", "Frame Jeans", "2026-11-09T18:00:00+00:00", ship_by="2026-11-13")
    sale(db, "s2", "vinted", "Little blue truck", "2026-11-11T14:19:47+00:00", status="unmatched", price=3,
         ship_by="2026-11-18")
    core.heartbeat(db, "orders:depop", {}, datetime(2026, 11, 12, 1, 33, tzinfo=timezone.utc))      # 8:33 PM
    core.heartbeat(db, "orders:vinted", {}, datetime(2026, 11, 11, 12, 10, tzinfo=timezone.utc))    # 7:10 AM: 14 h
    core.heartbeat(db, "orders:poshmark", {}, datetime(2026, 11, 12, 1, 31, tzinfo=timezone.utc))
    timers.night_list(db, NINE_PM, "live")
    [(chat, text)] = [m for m in out if m[0] == "group"]
    plain = html.unescape(re.sub(r"<[^>]+>", "", text))
    assert "🔎 Checked on the sites: Depop 8:33 PM" in plain                 # the sites on the list, fresh
    assert "⚠️ Vinted not checked since 7:10 AM — its sales may be out of date." in plain
    assert "Poshmark" not in plain.split("Checked on the sites:")[1].split("\n")[0]   # not on the list, fresh: left out
    assert "Reply “shipped <words>” or “cancelled <words>”" in plain


def test_all_caught_up_still_names_a_stale_site(db, live, out):
    core.heartbeat(db, "orders:vinted", {}, NINE_PM - timedelta(hours=30))
    core.heartbeat(db, "orders:depop", {}, NINE_PM - timedelta(minutes=27))
    core.heartbeat(db, "orders:poshmark", {}, NINE_PM - timedelta(minutes=29))
    timers.night_list(db, NINE_PM, "live")
    [(chat, text)] = [m for m in out if m[0] == "group"]
    assert text.startswith("📦 <b>Nothing to ship tonight</b>") and "⚠️ Vinted not checked since Tue 3:00 PM" in text
