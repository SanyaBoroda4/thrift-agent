"""WO66 — she shipped 10 parcels at 5 PM and the 9 PM list must not show them as still to ship (the owner, 2026-11-13:
"my wife just shipped 10 items … double check so it won't be on the list at 9pm").

A parcel whose label is printed and handed over (the Mac's sweep: Poshmark's unit "seller_shipped" with "Shipping
Label Created", status "awaiting_scan") leaves the to-ship groups for ONE 📮 block until its first carrier scan; two
days without one, or past its ship-by, it is to ship again with "⏰ label printed Thu, no USPS scan yet — check it went
out". Her "shipped 1 3" / "shipped all" closes packages by the LAST list's numbers ("all": never the 📮 block's).
eBay FULFILLED → shipped at once is covered by test_api_ebay_sales.py (test_only_the_named_fields_of_an_order_are_read,
test_shipped_is_recorded_quietly_and_a_cancellation_is_said)."""
from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from thrift_api import core, orders, shiplist, telegram, timers

NINE_PM = datetime(2026, 11, 12, 2, 0, tzinfo=UTC)         # Wed Nov 11, 9:00 PM New York
SWEPT = NINE_PM - timedelta(minutes=30)                               # the 8:30 PM sweep
CREATED = "2026-11-01T12:00:00+00:00"
LOFT, ZARA, HUNTER = "6aee36bb1ae90084d3cffaa3", "6af1eec48e9e5e7662bfd94d", "6af34fe7dc48740118e49f7d"


@pytest.fixture
def out(monkeypatch):
    log = []
    monkeypatch.setattr(telegram, "SENDER",
                        lambda chat, text, silent, html=False: log.append((chat, text, silent, html)) or True)
    return log


def sale(db, sid, mp, title, *, ship_by, order_id=None, sold_at="2026-11-09T18:00:00+00:00", price=40.0):
    db.execute("INSERT INTO items (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
               (f"i_{sid}", title, CREATED, CREATED))
    db.execute("INSERT INTO sales (id, marketplace, item_id, title_seen, price, order_id, sold_at, ship_by, "
               "ship_by_source, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'rule', 'matched', ?)",
               (sid, mp, f"i_{sid}", title, price, order_id, sold_at, ship_by, CREATED))


def sweep(db, mp, *entries, at=SWEPT):
    """The Mac's POST /orders: (order id, status, label_at or None)."""
    body = {"marketplace": mp, "swept_at": at.isoformat(),
            "orders": [{"order_id": oid, "status": status, "status_text": "Shipping Label Created",
                        **({"label_at": label} if label else {})} for oid, status, label in entries]}
    return orders.report(db, body, now=at, mode="live")


def three(db):
    """Three Poshmark sales to ship, oldest ship-by first: Loft (Fri), Zara (Mon), Hunter (Tue)."""
    sale(db, "s_loft", "poshmark", "Loft Cropped Utility Jacket", ship_by="2026-11-13", order_id=LOFT)
    sale(db, "s_zara", "poshmark", "Zara Kelly Green Satin Blouse", ship_by="2026-11-16", order_id=ZARA)
    sale(db, "s_hunter", "poshmark", "Hunter Chelsea Rain Boots", ship_by="2026-11-17", order_id=HUNTER)


def plain(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def the_list(db, out, now=NINE_PM):
    got = timers.night_list(db, now, "live")
    return got, plain("\n".join(t for chat, t, _, _ in out if chat == "group"))


def shipped(db, sid):
    return db.one("SELECT shipped_at FROM sales WHERE id = ?", (sid,))["shipped_at"]


# ---------------------------------------------------------------- the 📮 block

def test_a_printed_label_waits_in_the_mailbox_block_not_counted_no_deadline(db, out):
    three(db)
    sweep(db, "poshmark", (LOFT, "awaiting_scan", None), (ZARA, "to_ship", None), (HUNTER, "to_ship", None))
    assert db.one("SELECT label_at FROM orders WHERE order_id = ?", (LOFT,))["label_at"] == SWEPT.isoformat()
    got, text = the_list(db, out)
    assert got["packages"] == 2 and got["waiting"] == 1
    assert "2 packages" in text and "3 packages" not in text
    assert "📮 LABEL PRINTED — WAITING FOR USPS SCAN (1)\nLoft Cropped Utility Jacket · Poshmark" in text
    assert "1. Zara Kelly Green Satin Blouse" in text and "2. Hunter Chelsea Rain Boots" in text
    assert "Loft Cropped Utility Jacket\n" not in text.split("📮")[0]            # not among the to-ship packages
    mailbox = text.split("📮")[1].split("──")[0]
    assert "⏰" not in mailbox and "Ship by" not in mailbox
    assert text.index("📮") > text.index("2. Hunter") and text.index("📮") < text.index("──────")
    assert "Shipped some? Reply “shipped 1 3” or “shipped all”." in text
    assert shipped(db, "s_loft") is None                                         # open: no SHIPPED, no event
    assert not db.one("SELECT 1 AS x FROM email_events WHERE message_id LIKE ?", (f"%{LOFT}:SHIPPED",))


def test_the_first_scan_takes_it_off_the_list_quietly(db, out):
    three(db)
    sweep(db, "poshmark", (LOFT, "awaiting_scan", None))
    sweep(db, "poshmark", (LOFT, "shipped", None), at=SWEPT + timedelta(minutes=20))
    assert shipped(db, "s_loft") is not None and out == []                      # quiet: she shipped it, she knows
    _, text = the_list(db, out)
    assert "Loft" not in text and "📮" not in text


def test_only_waiting_parcels_is_nothing_to_ship_then_the_mailbox_block(db, out):
    sale(db, "s_loft", "poshmark", "Loft Cropped Utility Jacket", ship_by="2026-11-13", order_id=LOFT)
    sweep(db, "poshmark", (LOFT, "awaiting_scan", None))
    got, text = the_list(db, out)
    assert got["packages"] == 0 and got["waiting"] == 1
    assert text.startswith("📦 Nothing to ship tonight") and "📮 LABEL PRINTED — WAITING FOR USPS SCAN (1)" in text
    assert "Shipped some?" not in text


@pytest.mark.parametrize("label_at, ship_by, day", [
    ("2026-11-09T20:00:00+00:00", "2026-11-16", "Mon"),          # printed Mon, no scan by Wed 9 PM: 2+ days
    (None, "2026-11-10", "Wed"),                                  # printed tonight but its ship-by (Tue) has passed
])
def test_no_scan_for_two_days_or_past_ship_by_is_to_ship_again_with_the_check_line(db, out, label_at, ship_by, day):
    sale(db, "s_zara", "poshmark", "Zara Kelly Green Satin Blouse", ship_by=ship_by, order_id=ZARA)
    sweep(db, "poshmark", (ZARA, "awaiting_scan", label_at))
    got, text = the_list(db, out)
    assert got["packages"] == 1 and got["waiting"] == 0 and "📮" not in text
    assert "1. Zara Kelly Green Satin Blouse" in text
    assert f"⏰ label printed {day}, no USPS scan yet — check it went out" in text


def test_the_sites_own_label_time_wins_over_the_sweeps(db):
    sale(db, "s_zara", "poshmark", "Zara Kelly Green Satin Blouse", ship_by="2026-11-16", order_id=ZARA)
    sweep(db, "poshmark", (ZARA, "awaiting_scan", None))
    sweep(db, "poshmark", (ZARA, "awaiting_scan", "2026-11-11T21:39:00+00:00"), at=SWEPT + timedelta(minutes=20))
    assert db.one("SELECT label_at FROM orders")["label_at"] == "2026-11-11T21:39:00+00:00"
    sweep(db, "poshmark", (ZARA, "awaiting_scan", None), at=SWEPT + timedelta(minutes=40))
    assert db.one("SELECT label_at FROM orders")["label_at"] == "2026-11-11T21:39:00+00:00"   # kept


def test_a_waiting_parcel_gets_no_ship_tomorrow_nag(db, out):
    sale(db, "s_loft", "poshmark", "Loft Cropped Utility Jacket", ship_by="2026-11-12", order_id=LOFT)
    sale(db, "s_zara", "poshmark", "Zara Kelly Green Satin Blouse", ship_by="2026-11-12", order_id=ZARA)
    sweep(db, "poshmark", (LOFT, "awaiting_scan", None), (ZARA, "to_ship", None))
    assert timers.reminders(db, datetime(2026, 11, 11, 15, 0, tzinfo=UTC), "live") == [["s_zara", "day_before"]]


def test_health_counts_the_waiting_parcels_apart(db):
    three(db)
    sweep(db, "poshmark", (LOFT, "awaiting_scan", None))
    got = shiplist.status(db, NINE_PM)
    assert got["open_packages"] == 2 and got["waiting_for_scan"] == 1
    assert core.shipping("matched", None, None, "awaiting_scan") == "awaiting_scan"


# ---------------------------------------------------------------- her "shipped 1 3" / "shipped all"

@pytest.mark.parametrize("words, want", [("1 3 5", [1, 3, 5]), ("1, 3", [1, 3]), ("#2", [2]), ("1 and 3", [1, 3]),
                                         ("all", "all"), ("ALL", "all"), ("3 3", [3]), ("levis 501", None),
                                         ("moncler", None), ("all of it", None)])
def test_the_numbered_form_is_read_and_title_words_are_not(words, want):
    assert core.numbered(words) == want


def listed(db, out):
    """Loft waiting; Zara (1), Hunter (2) and a Depop sale (3) on tonight's list."""
    three(db)
    sale(db, "s_dep", "depop", "Premiata Steven Sneakers", ship_by="2026-11-18")
    sweep(db, "poshmark", (LOFT, "awaiting_scan", None), (ZARA, "to_ship", None), (HUNTER, "to_ship", None))
    got, _ = the_list(db, out)
    assert got["packages"] == 3
    numbers = json.loads(core.setting(db, "ship_list_numbers"))
    assert numbers["packages"] == {"1": ["s_zara"], "2": ["s_hunter"], "3": ["s_dep"]}
    assert numbers["waiting"] == ["s_loft"] and numbers["day"] == "2026-11-11"
    out.clear()


def test_shipped_1_3_closes_those_two_with_one_line(db, out):
    listed(db, out)
    got = core.mac_event(db, {"kind": "shipped", "words": "1 3"}, NINE_PM + timedelta(minutes=12), "live")
    assert got["status"] == "shipped" and got["matched"] == ["s_zara", "s_dep"]
    assert got["titles"] == ["Zara Kelly Green Satin Blouse", "Premiata Steven Sneakers"]
    assert out == [("group", "✓ Marked shipped: Zara Kelly Green Satin Blouse · Premiata Steven Sneakers", False, False)]
    assert shipped(db, "s_zara") and shipped(db, "s_dep") and not shipped(db, "s_hunter") and not shipped(db, "s_loft")


def test_shipped_all_closes_every_to_ship_package_never_the_mailbox_block(db, out):
    listed(db, out)
    got = core.mac_event(db, {"kind": "shipped", "words": "all"}, NINE_PM + timedelta(minutes=12), "live")
    assert got["matched"] == ["s_zara", "s_hunter", "s_dep"]
    assert shipped(db, "s_zara") and shipped(db, "s_hunter") and shipped(db, "s_dep")
    assert shipped(db, "s_loft") is None                                       # the 📮 one: its scan closes it
    assert len(out) == 1


def test_one_already_closed_is_named_as_done(db, out):
    listed(db, out)
    core.mac_event(db, {"kind": "shipped", "words": "1"}, NINE_PM + timedelta(minutes=5), "live")
    out.clear()
    got = core.mac_event(db, {"kind": "shipped", "words": "1 2"}, NINE_PM + timedelta(minutes=9), "live")
    assert got["matched"] == ["s_zara", "s_hunter"] and len(out) == 1


def test_a_number_not_on_the_last_list_marks_nothing(db, out):
    listed(db, out)
    got = core.mac_event(db, {"kind": "shipped", "words": "1 7"}, NINE_PM + timedelta(minutes=12), "live")
    assert got["status"] == "no_match" and got["matched"] is None
    assert got["say"] == "No 7 on tonight's list (it had 1–3) — nothing marked."
    assert out == [] and not shipped(db, "s_zara")


def test_before_any_list_the_numbers_say_so(db, out):
    three(db)
    got = core.mac_event(db, {"kind": "shipped", "words": "2"}, NINE_PM, "live")
    assert got["status"] == "no_match"
    assert got["say"] == "No shipping list has gone out yet — reply “shipped <words>” instead."


def test_title_words_still_work_as_before(db, out):
    three(db)
    got = core.mac_event(db, {"kind": "shipped", "words": "hunter boots"}, NINE_PM, "live")
    assert got["status"] == "shipped" and got["matched"] == "s_hunter"
    assert out == [("group", "✓ Marked shipped: Hunter Chelsea Rain Boots", False, False)]


def test_a_preview_never_changes_the_numbers(db, out):
    listed(db, out)
    before = core.setting(db, "ship_list_numbers")
    sale(db, "s_new", "vinted", "Little blue truck", ship_by="2026-11-12")
    shiplist.night_list(db, NINE_PM + timedelta(hours=12), "live", preview=True)
    assert core.setting(db, "ship_list_numbers") == before
