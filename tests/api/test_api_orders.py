"""WO38 — the sites' own order statuses (POST /orders, thrift_api.orders): shipped / delivered / completed recorded
quietly by the site's order id, a cancellation said as an email's, an order our records lack recorded once (its
take-downs, one 💰 line), finished history left alone; the reminders follow what the site shows (none once it shows
the order on its way; overdue only when the site, read after the ship-by day, still shows it unshipped — one group line,
once); the dashboard shows each sale's shipping state and each site's last sweep."""
import json

import pytest
from apitools import ITEM, NOW, URLS, call, posh_sale, seed, utc, vinted_sale

from thrift_api import core, orders, timers

POSH_ORDER = "6702bb11cc22dd33ee44ff55"          # the order id in apitools.posh_sale


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-10-01T00:00:00+00:00")


def report(db, mp: str, *entries: dict, now=NOW, swept_at=None) -> dict:
    status, out, _ = call(db, "POST", "/orders", {"marketplace": mp, "swept_at": (swept_at or now).isoformat(),
                                                  "orders": list(entries)}, now=now)
    assert status == 200, out
    return out


def posh(status: str, **kw) -> dict:
    return {"order_id": POSH_ORDER, "status": status, "status_text": kw.pop("text", status.title()),
            "title": "Lacoste Tee White size M", "price": 35, "sold_at": "2026-10-08T14:05:00+00:00", **kw}


def a_posh_sale(db, ship_by: str = "2026-10-09") -> str:
    seed(db)
    sale_id = core.process_email(db, posh_sale(), NOW)["sale_id"]
    db.execute("UPDATE sales SET ship_by = ? WHERE id = ?", (ship_by, sale_id))
    return sale_id


def sale_row(db, sale_id: str) -> dict:
    return db.one("SELECT * FROM sales WHERE id = ?", (sale_id,))


def test_shipped_delivered_completed_are_recorded_quietly_by_the_order_id(db, live, sent):
    sale_id = a_posh_sale(db)
    sent.clear()
    out = report(db, "poshmark", posh("shipped", text="In Transit", shipped_at="2026-10-08T21:15:01+00:00"))
    assert out["results"] == [{"order_id": POSH_ORDER, "status": "shipped", "sale_id": sale_id,
                               "action": "linked, shipped"}]
    assert sale_row(db, sale_id)["shipped_at"] == "2026-10-08T21:15:01+00:00"
    report(db, "poshmark", posh("delivered", text="Delivered"), now=utc(2026, 10, 13, 18, 0))
    row = sale_row(db, sale_id)
    assert (row["status"], row["delivered_at"] is not None) == ("done", True)
    report(db, "poshmark", posh("completed", text="Order Complete"), now=utc(2026, 10, 14, 18, 0))
    assert sent == []                                                  # never a word for these three
    kept = db.one("SELECT * FROM orders WHERE marketplace = 'poshmark' AND order_id = ?", (POSH_ORDER,))
    assert (kept["sale_id"], kept["item_id"], kept["status"], kept["status_text"]) == (
        sale_id, ITEM, "completed", "Order Complete")
    assert set(json.loads(kept["seen_json"])) == {"shipped", "delivered", "completed"}   # each status's first sighting
    events = [r["message_id"] for r in db.query("SELECT message_id FROM email_events WHERE message_id LIKE 'mac:order:%' "
                                                "ORDER BY message_id")]
    assert events == [f"mac:order:poshmark:{POSH_ORDER}:{k}" for k in ("COMPLETED", "DELIVERED", "SHIPPED")]


def test_the_same_sweep_twice_changes_nothing(db, live, sent):
    sale_id = a_posh_sale(db)
    report(db, "poshmark", posh("delivered"))
    before = (sale_row(db, sale_id), db.one("SELECT COUNT(*) AS n FROM email_events")["n"])
    sent.clear()
    out = report(db, "poshmark", posh("delivered"))
    assert out["results"][0]["action"] == "linked"
    assert (sale_row(db, sale_id), db.one("SELECT COUNT(*) AS n FROM email_events")["n"]) == before
    assert sent == []


def test_a_cancellation_on_the_site_is_said_as_the_email_says_it(db, live, sent):
    sale_id = a_posh_sale(db)
    sent.clear()
    report(db, "poshmark", posh("cancelled", text="Order Cancelled"))
    assert sale_row(db, sale_id)["status"] == "cancelled"
    assert ("group", "↩️ Lacoste Tee White size M's sale on Poshmark was cancelled.", False) in sent


def test_an_order_is_matched_by_its_listing_or_sku_then_by_title(db, live):
    seed(db)
    first = core.process_email(db, vinted_sale(), NOW)["sale_id"]                # Vinted's email: transaction 9876543210
    db.execute("UPDATE sales SET order_id = NULL WHERE id = ?", (first,))
    by_listing = report(db, "vinted", {"order_id": "111", "status": "shipped", "listing_url": URLS["vinted"],
                                       "sold_at": "2026-10-08T14:00:00+00:00"})
    assert by_listing["results"][0]["sale_id"] == first
    assert sale_row(db, first)["order_id"] == "111"                         # the order id fills the sale's
    db.execute("UPDATE sales SET order_id = NULL WHERE id = ?", (first,))
    db.execute("DELETE FROM orders")
    by_title = report(db, "vinted", {"order_id": "222", "status": "shipped", "title": "Lacoste tee white size m",
                                     "sold_at": "2026-10-08T14:00:00+00:00"})
    assert by_title["results"][0]["sale_id"] == first
    other = report(db, "vinted", {"order_id": "333", "status": "shipped", "title": "Lacoste tee white size m",
                                  "sold_at": "2026-09-01T14:00:00+00:00"})       # the same title a month before: not it
    assert other["results"][0]["sale_id"] != first and other["results"][0]["action"].startswith("recorded")


def test_depops_order_is_the_one_open_depop_sale_sold_near_it(db, live):
    seed(db)
    sale_id = core.process_email(db, {**__import__("apitools").depop_sale(), "text": "Woohoo! You sold “Lacoste Tee "
                                      "White size M” for $35.00.\nView it: " + URLS["depop"]}, NOW)["sale_id"]
    db.execute("UPDATE sales SET order_id = NULL WHERE id = ?", (sale_id,))
    out = report(db, "depop", {"order_id": "5550001", "status": "shipped", "sold_at": "2026-10-08T15:00:00+00:00"})
    assert out["results"][0]["sale_id"] == sale_id


def test_an_order_our_records_lack_is_recorded_once_with_its_take_downs_and_one_line(db, live, sent):
    seed(db)                                                              # live on all three sites; no email came
    sent.clear()
    entry = {"order_id": "22942401831", "status": "to_ship", "title": "Lacoste Tee White size M", "price": 35,
             "sold_at": "2026-10-08T12:00:00+00:00", "listing_url": URLS["vinted"]}
    out = report(db, "vinted", entry)
    assert out["results"][0]["action"] == "recorded"
    sale = db.one("SELECT * FROM sales WHERE order_id = '22942401831'")
    assert (sale["item_id"], sale["status"]) == (ITEM, "delisting")
    tasks = {r["marketplace"] for r in db.query("SELECT marketplace FROM delist_tasks WHERE sale_id = ?", (sale["id"],))}
    assert tasks == {"poshmark", "depop"}
    assert [t for chat, t, _ in sent if chat == "group"] == [
        "💰 Sold on Vinted: Lacoste Tee White size M — $35. Ship by Fri Oct 16."]      # 5 business days, Columbus Day
    sent.clear()
    report(db, "vinted", {**entry, "status": "shipped"}, now=utc(2026, 10, 9, 12, 0))
    assert db.one("SELECT COUNT(*) AS n FROM sales WHERE order_id = '22942401831'")["n"] == 1
    assert sent == []                                                    # recorded once; shipped quietly


def test_finished_history_and_a_cancelled_order_are_left_alone(db, live, sent):
    old = {"order_id": "1", "status": "completed", "title": "Vans size 13", "price": 20,
           "sold_at": "2026-09-22T02:55:27+00:00"}                         # before going live, finished
    gone = {"order_id": "2", "status": "cancelled", "title": "Zara backpack", "price": 12,
            "sold_at": "2026-10-07T02:55:27+00:00"}
    out = report(db, "vinted", old, gone)
    assert [r["action"] for r in out["results"]] == ["left alone (finished before going live, or cancelled)"] * 2
    assert db.one("SELECT COUNT(*) AS n FROM sales")["n"] == 0 and sent == []
    assert db.one("SELECT COUNT(*) AS n FROM orders")["n"] == 2          # still kept as orders seen


def test_an_older_order_still_to_ship_is_recorded_without_a_word(db, live, sent):
    out = report(db, "vinted", {"order_id": "3", "status": "to_ship", "title": "Sparkling boots size 6", "price": 3,
                                "sold_at": "2026-09-28T23:39:53+00:00"})   # sold before going live, not shipped yet
    assert out["results"][0]["action"] == "recorded"
    sale = db.one("SELECT * FROM sales WHERE order_id = '3'")
    assert sale["status"] == "unmatched" and sale["ship_by"]             # its reminders follow
    assert sent == []                                                    # history: no 💰 line


def test_a_shipped_order_gets_no_reminder(db, live, sent):
    a_posh_sale(db)
    report(db, "poshmark", posh("shipped"), now=utc(2026, 10, 8, 12, 0))
    sent.clear()
    for when in (utc(2026, 10, 8, 13, 0), utc(2026, 10, 9, 13, 0), utc(2026, 10, 10, 13, 0)):
        assert timers.every_15_min(db, when)["reminders"] == []
    assert sent == []


def test_overdue_is_one_group_line_once_only_when_the_site_still_shows_it_unshipped(db, live, sent):
    sale_id = a_posh_sale(db, ship_by="2026-10-09")                      # due Fri Oct 9
    report(db, "poshmark", posh("to_ship", text="Awaiting Shipment"), now=utc(2026, 10, 9, 12, 0))
    sent.clear()
    assert timers.every_15_min(db, utc(2026, 10, 10, 13, 0))["reminders"] == []   # no reading since the day ended
    report(db, "poshmark", posh("to_ship", text="Awaiting Shipment"), now=utc(2026, 10, 10, 12, 30))   # 08:30 Sat
    assert timers.every_15_min(db, utc(2026, 10, 10, 13, 0))["reminders"] == [[sale_id, "overdue_site"]]
    assert sent == [("group", "📦 Overdue: Lacoste Tee White size M (Poshmark) — ship today", False)]
    report(db, "poshmark", posh("to_ship"), now=utc(2026, 10, 11, 12, 30))
    assert timers.every_15_min(db, utc(2026, 10, 11, 13, 0))["reminders"] == []    # once
    assert len(sent) == 1                                                # and never the ops line on top


def test_a_sale_the_sweep_never_tied_to_an_order_keeps_the_ops_line(db, live, sent):
    sale_id = a_posh_sale(db, ship_by="2026-10-09")
    report(db, "poshmark", {"order_id": "elsewhere", "status": "completed", "title": "Something else",
                            "sold_at": "2026-09-01T00:00:00+00:00"}, now=utc(2026, 10, 10, 12, 0))
    sent.clear()
    timers.every_15_min(db, utc(2026, 10, 10, 13, 0))
    assert [chat for chat, _, _ in sent] == ["ops"] and sale_id in sent[0][1]


def test_the_dashboard_shows_each_sales_shipping_state_and_each_sites_last_sweep(db, live):
    a_posh_sale(db)
    report(db, "poshmark", posh("delivered", text="Delivered"), now=utc(2026, 10, 13, 18, 0))
    data = core.dashboard_data(db, utc(2026, 10, 13, 19, 0))
    assert data["items"][0]["sale"]["shipping"] == "delivered"
    assert data["swept"] == {"poshmark": "2026-10-13T18:00:00+00:00"}
    status, page, _ = call(db, "GET", "/dashboard", now=utc(2026, 10, 13, 19, 0))
    assert status == 200 and "Delivered" in page
    assert "Orders read on the sites: Poshmark Tue Oct 13, 2:00 PM · Depop — · Vinted —" in page


@pytest.mark.parametrize("ours, site, want", [
    ((None, None, None), None, "to_ship"), (("matched", "2026-10-08", None), None, "shipped"),
    (("matched", None, None), "delivered", "delivered"), (("done", "x", "y"), "completed", "completed"),
    (("matched", "2026-10-08", None), "to_ship", "shipped"),           # her own "shipped" reply wins over a stale read
    (("cancelled", None, None), "shipped", "cancelled"), (("matched", None, None), "returned", "returned")])
def test_shipping_state_is_the_furthest_of_ours_and_the_sites(ours, site, want):
    assert core.shipping(*ours, site) == want


def test_a_report_says_what_it_skipped_and_refuses_another_site(db, live):
    out = report(db, "poshmark", {"order_id": "", "status": "shipped"}, {"order_id": "9", "status": "lost"})
    assert [r["action"] for r in out["results"]] == ["skipped: no order id or no known status"] * 2
    status, body, _ = call(db, "POST", "/orders", {"marketplace": "ebay", "orders": []})
    assert status == 400


def test_no_buyer_field_is_ever_kept(db, live):
    report(db, "vinted", {"order_id": "7", "status": "to_ship", "title": "Vans size 12", "sold_at": "2026-10-08T00:00:00Z",
                          "buyer": "janeq_closet", "address": "123 Secret Lane", "tracking_number": "9400111"})
    dump = json.dumps([dict(r) for r in db.query("SELECT * FROM orders")] +
                      [dict(r) for r in db.query("SELECT * FROM email_events")] +
                      [dict(r) for r in db.query("SELECT * FROM sales")])
    for secret in ("janeq_closet", "123 Secret Lane", "9400111"):
        assert secret not in dump
    assert set(orders.FIELDS) >= {"order_id", "status"} and not {"buyer", "address"} & set(orders.FIELDS)


def test_a_depop_order_ties_to_its_closed_sale_and_never_records_it_twice(db, live, sent):
    """The live Madewell case: Depop's delivered email had closed the sale (done, no order id); the sweep's receipt —
    "Shipped", sold "20 days ago" — is that sale, not a new one."""
    core.process_email(db, {**__import__("apitools").depop_sale(sku=None, date="2026-09-23T21:33:00.000Z"),
                            "text": "Woohoo! You sold “Madewell striped "
                            "sweater” for $30.00.\nView it: https://www.depop.com/products/shop-madewell/"},
                       utc(2026, 9, 23, 21, 33))
    sale = db.one("SELECT * FROM sales WHERE marketplace = 'depop'")
    db.execute("UPDATE sales SET status = 'done', order_id = NULL, delivered_at = '2026-10-03T23:41:34+00:00', "
               "shipped_at = '2026-10-03T23:41:34+00:00' WHERE id = ?", (sale["id"],))
    sent.clear()
    out = report(db, "depop", {"order_id": "915093750", "status": "shipped", "sold_at": "2026-09-24T12:00:00+00:00"},
                 now=utc(2026, 10, 14, 9, 0))
    assert out["results"][0]["sale_id"] == sale["id"] and out["results"][0]["action"].startswith("linked")
    assert db.one("SELECT COUNT(*) AS n FROM sales WHERE marketplace = 'depop'")["n"] == 1
    assert db.one("SELECT order_id FROM sales WHERE id = ?", (sale["id"],))["order_id"] == "915093750"
    assert sent == []


def test_an_order_that_could_be_one_of_two_sales_is_tied_to_none_and_records_nothing(db, live, sent):
    for n, when in ((1, utc(2026, 10, 5, 12)), (2, utc(2026, 10, 6, 12))):
        core.process_email(db, {**__import__("apitools").depop_sale(message_id=f"d{n}", sku=None),
                                "text": f"Woohoo! You sold “Thing {n}” for $30.00."}, when)
    db.execute("UPDATE sales SET order_id = NULL")
    out = report(db, "depop", {"order_id": "77", "status": "shipped", "sold_at": "2026-10-05T18:00:00+00:00"})
    assert out["results"][0]["action"] == "left alone (it could be one of several sales)"
    assert db.one("SELECT COUNT(*) AS n FROM sales")["n"] == 2
