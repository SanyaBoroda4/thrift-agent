"""WO57 — eBay, thrift-api's side, part 2: eBay's orders read every 5 minutes (an order with our SKU is the sale: the
💰 line, the ship-by day, the take-downs elsewhere, shipped / cancelled after; an order for a listing of hers is ONE
quiet private line and nothing in the group; no buyer field is ever kept); a sale on another site ends the eBay
listing from Azure at once — read first, ours by its SKU, never the Mac's task — and is tried again by the timer; the
fourth site in the nets, the 9 PM line and the dashboard while eBay is on, and nowhere while it is off.

apitools.NOW is 11:00 AM in New York, inside the owner's quiet hours: none of Azure's eBay work waits for them."""
import json
import logging
from datetime import date, timedelta

import pytest
from apitools import DEPOP_ID, IDS, ITEM, NOW, TITLE, URLS, call, posh_sale, seed, tasks, texts, utc
from ebayfake import EBAY_URL, LISTING, PRIVATE, REFRESH, beat, connect, everything, order

from thrift_api import core, dashboard, deadlines, ebay, match, timers, watch
from thrift_api.util import iso

MINUTE, HOUR = timedelta(minutes=1), timedelta(hours=1)
SITES4 = ("poshmark", "depop", "vinted", "ebay")
HERS = {"order_id": "99-11111-22222", "sku": "HER-OWN-SKU-7", "listing": "330000000009"}
LEFT_TO_HER = "Couldn't take Lacoste Tee White size M down on eBay — please mark it sold there."


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-10-01T00:00:00+00:00")


@pytest.fixture
def shop(db, live, fake_ebay, sent):
    """Live, eBay connected, the item listed on all four sites (its eBay listing active, carrying our SKU)."""
    assert connect(db)[0] == 200
    seed4(db)
    fake_ebay.listing()
    sent.clear()
    return fake_ebay


def seed4(db, ebay_id: str | None = LISTING, **kw) -> None:
    seed(db, sites=SITES4, urls={**URLS, "ebay": EBAY_URL if ebay_id else None}, ids={**IDS, "ebay": ebay_id}, **kw)


def task(db, site: str = "ebay") -> dict:
    return db.one("SELECT * FROM delist_tasks WHERE marketplace = ? ORDER BY created_at DESC, id DESC LIMIT 1", (site,))


def row(db, site: str = "ebay") -> str:
    return db.one("SELECT status FROM listings WHERE item_id = ? AND marketplace = ?", (ITEM, site))["status"]


def graphql(fake) -> list[str]:
    return [operation for what, operation in fake.calls if what == "graphql"]


# --- the orders ------------------------------------------------------------------------------------------------------

def test_only_the_named_fields_of_an_order_are_read():
    assert ebay.order_entries(order()) == [{
        "order_id": "12-34567-89012", "status": "to_ship", "status_text": "Awaiting shipment", "title": TITLE,
        "price": "35.0", "sold_at": "2026-10-08T14:05:00.000Z", "listing_id": LISTING, "sku": ITEM,
        "ship_by": "2026-10-12"}]                                   # eBay's 06:59:59Z is the end of Oct 12 in the US
    assert [e["status"] for e in map(ebay.order_entries, (order(status="FULFILLED"), order(cancel="CANCELED"),
                                                          order(payment="FULLY_REFUNDED"), order(status="IN_PROGRESS")))
            for e in e] == ["shipped", "cancelled", "returned", "to_ship"]
    assert ebay.order_entries({"orderId": "1", "lineItems": []}) == [] and ebay.order_entries("nonsense") == []


def test_an_order_with_our_sku_is_the_sale(shop, db, sent):
    shop.orders = [order()]
    assert ebay.read_orders(db, NOW) == {"read": True, "orders": 1, "recorded": 1}
    assert sent == [("group", "💰 Sold on eBay: Lacoste Tee White size M — $35. Ship by Mon Oct 12.", False)]
    sale = db.one("SELECT * FROM sales")
    assert {k: sale[k] for k in ("marketplace", "item_id", "order_id", "listing_id", "price", "sold_at", "ship_by",
                                 "ship_by_source", "status")} == {
        "marketplace": "ebay", "item_id": ITEM, "order_id": "12-34567-89012", "listing_id": LISTING, "price": 35.0,
        "sold_at": "2026-10-08T14:05:00+00:00", "ship_by": "2026-10-12", "ship_by_source": "email", "status": "delisting"}
    assert [(t["marketplace"], t["status"]) for t in tasks(db)] == [("poshmark", "pending"), ("depop", "pending"),
                                                                    ("vinted", "pending")]
    answer = call(db, "GET", "/tasks", query={"sites": ",".join(SITES4)})[1]
    assert [t["marketplace"] for t in answer["tasks"]] == ["poshmark", "depop", "vinted"]      # the Mac's, as ever
    assert answer["sold"] == [ITEM] and answer["sold_on"] == [{"item": ITEM, "site": "ebay"}]  # … and its eBay row: sold
    assert answer["ended"] == [] and graphql(shop) == []                                       # nothing to end on eBay
    asked = shop.requests[-1]
    assert asked["url"] == ("https://apiz.ebay.com/sell/fulfillment/v1/order?filter=lastmodifieddate:"
                            "%5B2026-10-05T15:00:00.000Z..%5D&limit=200")                      # the first read: 3 days back
    assert asked["headers"]["Authorization"] == "Bearer user-access-token-1" and asked["method"] == "GET"
    kept = db.one("SELECT * FROM orders WHERE marketplace = 'ebay'")
    assert (kept["order_id"], kept["sale_id"], kept["item_id"], kept["status"]) == ("12-34567-89012", sale["id"], ITEM,
                                                                                   "to_ship")
    assert call(db, "GET", "/ebay/status")[1]["orders_read_at"] == "2026-10-08T15:00:00+00:00"


def test_the_same_order_read_again_changes_nothing(shop, db, sent):
    shop.orders = [order()]
    ebay.read_orders(db, NOW)
    sent.clear()
    before = (db.query("SELECT * FROM sales"), db.one("SELECT COUNT(*) AS n FROM email_events")["n"], tasks(db))
    assert ebay.read_orders(db, NOW + 5 * MINUTE) == {"read": True, "orders": 1, "recorded": 0}
    assert (db.query("SELECT * FROM sales"), db.one("SELECT COUNT(*) AS n FROM email_events")["n"], tasks(db)) == before
    assert sent == [] and shop.since() == "2026-10-08T14:45:00.000Z"          # 15 minutes before the last read


def test_an_order_for_a_listing_of_hers_is_one_quiet_private_line(shop, db, sent):
    """Her own eBay listing sold — even one titled like ours: never ours by its title; ONE private line, no question,
    nothing in the group (no 💰 line, no reminder, no cancellation line), nothing taken down."""
    shop.orders = [order(**HERS)]
    assert ebay.read_orders(db, NOW)["recorded"] == 1
    assert sent == [("ops", "Sold on eBay: Lacoste Tee White size M — not one of the agent's listings, nothing to take "
                            "down.", False)]
    sale = db.one("SELECT * FROM sales")
    assert (sale["status"], sale["item_id"], sale["marketplace"]) == ("unmatched", None, "ebay") and tasks(db) == []
    ebay.read_orders(db, NOW + 5 * MINUTE)
    for when in (utc(2026, 10, 11, 15, 0), utc(2026, 10, 12, 15, 0), utc(2026, 10, 14, 15, 0)):   # the day before, due, late
        assert timers.reminders(db, when, "live") == []
    shop.orders = [order(**HERS, cancel="CANCELED")]
    ebay.read_orders(db, utc(2026, 10, 14, 16, 0))
    assert db.one("SELECT status FROM sales")["status"] == "cancelled"
    assert len(sent) == 2 and texts(sent, "group") == [] and sent[1][1].startswith("Cancelled: Lacoste Tee White size M")
    assert not any("?" in text for _, text, _ in sent)
    assert match.match_sale(db, "ebay", {"title": TITLE, "price": 35}) == (None, None)       # a title is never enough
    assert match.match_sale(db, "poshmark", {"title": TITLE, "price": 35}) == (ITEM, "title")


def test_an_order_without_a_sku_is_ours_by_the_listing_id_the_mirror_holds(shop, db, sent):
    shop.orders = [order(sku=None)]
    ebay.read_orders(db, NOW)
    assert db.one("SELECT item_id FROM sales")["item_id"] == ITEM and texts(sent, "group")[0].startswith("💰 Sold on eBay")
    assert match.item_for_listing(db, "ebay", None, f"https://www.ebay.com/itm/lacoste-tee/{LISTING}?hash=x") == ITEM


def test_shipped_is_recorded_quietly_and_a_cancellation_is_said(shop, db, sent):
    shop.orders = [order()]
    ebay.read_orders(db, NOW)
    sale_id = db.one("SELECT id FROM sales")["id"]
    assert timers.reminders(db, utc(2026, 10, 11, 15, 0), "live") == [[sale_id, "day_before"]]
    assert sent[-1] == ("group", "📦 Ship tomorrow: Lacoste Tee White size M (eBay) — due Mon Oct 12.", False)
    sent.clear()
    shop.orders = [order(status="FULFILLED")]
    ebay.read_orders(db, utc(2026, 10, 11, 20, 0))
    sale = db.one("SELECT * FROM sales")
    assert sale["shipped_at"] == "2026-10-11T20:00:00+00:00" and sent == []                   # she shipped it: she knows
    assert timers.reminders(db, utc(2026, 10, 12, 15, 0), "live") == []
    shop.orders = [order(status="FULFILLED", cancel="CANCELED")]
    ebay.read_orders(db, utc(2026, 10, 12, 20, 0))
    assert db.one("SELECT status FROM sales")["status"] == "cancelled"
    assert texts(sent, "group") == ["↩️ Lacoste Tee White size M's sale on eBay was cancelled."]


def test_an_unshipped_ebay_order_is_overdue_by_the_last_read(shop, db, sent):
    """eBay's orders come by what changed: an order still to ship that a read doesn't bring is unchanged — looked at
    now — so the day after its ship-by the group hears it, as for a site the Mac sweeps."""
    shop.orders = [order()]
    ebay.read_orders(db, NOW)
    sale_id = db.one("SELECT id FROM sales")["id"]
    shop.orders = []                                                           # nothing changed since
    ebay.read_orders(db, utc(2026, 10, 13, 14, 0))
    sent.clear()
    assert timers.reminders(db, utc(2026, 10, 13, 14, 5), "live") == [[sale_id, "overdue_site"]]
    assert sent == [("group", "📦 Overdue: Lacoste Tee White size M (eBay) — ship today", False)]


def test_no_buyer_field_is_ever_kept_or_logged(shop, db, sent, caplog):
    two = order(order_id="12-00000-00002", line="10022222222222")
    two["lineItems"].append({"lineItemId": "10033333333333", "legacyItemId": HERS["listing"], "sku": HERS["sku"],
                             "title": "Vintage Glass Vase", "lineItemCost": {"value": "12.50", "currency": "USD"},
                             "lineItemFulfillmentStatus": "NOT_STARTED"})
    shop.orders = [order(**HERS), two, order(order_id="12-00000-00003", cancel="CANCELED", sku=None, listing="1")]
    with caplog.at_level(logging.DEBUG):
        assert ebay.read_orders(db, NOW)["orders"] == 4
        shop.orders_status = 500
        ebay.read_orders(db, NOW + 5 * MINUTE)
    said = " ".join(text for _, text, _ in sent)
    assert not any(word in everything(db) + caplog.text + said for word in PRIVATE)
    # an order of two lines is two sales, each by its own line: ours (the 💰 line) and hers (the quiet one)
    sales = {s["order_id"]: s["item_id"] for s in db.query("SELECT order_id, item_id FROM sales")}
    assert sales == {HERS["order_id"]: None, "12-00000-00002:10022222222222": ITEM, "12-00000-00002:10033333333333": None}
    assert len(texts(sent, "group")) == 1 and len([t for t in texts(sent, "ops") if t.startswith("Sold on eBay")]) == 2


def test_ebays_ship_by_day_wins_else_three_business_days(shop, db):
    shop.orders = [order(ship_by=None)]
    ebay.read_orders(db, NOW)
    # sold Thu Oct 8: Fri 9, (the weekend, Columbus Day), Tue 13, Wed 14
    assert tuple(db.one("SELECT ship_by, ship_by_source FROM sales").values()) == ("2026-10-14", "rule")
    assert deadlines.ship_by("ebay", utc(2026, 10, 8, 15, 0)) == (date(2026, 10, 14), "rule")
    assert deadlines.ship_by("ebay", utc(2026, 10, 8, 15, 0), stated=date(2026, 10, 12)) == (date(2026, 10, 12), "email")
    shop.orders = [order()]                                                    # a later read names the day: it wins
    ebay.read_orders(db, NOW + 5 * MINUTE)
    assert tuple(db.one("SELECT ship_by, ship_by_source FROM sales").values()) == ("2026-10-12", "email")


def test_every_page_is_read_and_only_ebays_own_next_is_followed(shop, db, monkeypatch):
    shop.page_size = 1
    shop.orders = [order(**HERS), order(order_id="98-1", sku="X", listing="2"), order()]
    assert ebay.read_orders(db, NOW) == {"read": True, "orders": 3, "recorded": 3} and shop.count("orders") == 3
    real = shop._orders

    def elsewhere(url, headers):
        status, body = real(url, headers)
        return status, json.dumps({**json.loads(body), "next": "https://evil.example/order?offset=1"}).encode()
    monkeypatch.setattr(shop, "_orders", elsewhere)
    assert ebay.read_orders(db, NOW + 5 * MINUTE)["read"] is True
    assert shop.count("orders") == 4 and not any("evil" in r["url"] for r in shop.requests)   # the token went nowhere else


def test_a_read_that_fails_is_tried_again_and_said_after_an_hour(shop, db, sent, caplog):
    shop.orders_status = 500
    out = timers.every_5_min(db, NOW)
    assert out["ebay"] == {"orders": {"read": False, "why": "HTTP 500"}, "takedowns": []} and "shares" in out
    assert call(db, "GET", "/ebay/status")[1]["orders_error"] == "HTTP 500"
    for minutes in range(5, 60, 5):
        timers.every_5_min(db, NOW + minutes * MINUTE)
    assert sent == []
    timers.every_5_min(db, NOW + HOUR)
    assert sent == [("ops", "⚠️ eBay's orders couldn't be read since 11:00 AM (HTTP 500) — a sale on eBay isn't seen "
                            "until it works again; tried every 5 minutes.", False)]
    timers.every_5_min(db, NOW + 65 * MINUTE)
    assert len(sent) == 1 and ebay.site_mark(db, NOW + 65 * MINUTE) == "eBay ✗ its orders aren't read"
    shop.orders_status, shop.orders = 200, [order()]
    timers.every_5_min(db, NOW + 70 * MINUTE)
    assert texts(sent, "ops")[-1] == "✓ eBay's orders are read again." and len(texts(sent, "group")) == 1
    assert call(db, "GET", "/ebay/status")[1]["orders_error"] is None
    assert ebay.site_mark(db, NOW + 70 * MINUTE) == "eBay ✓"


def test_a_token_ebay_no_longer_takes_is_minted_again(shop, db):
    shop.tokens.clear()                                                        # eBay forgot the token it gave
    assert ebay.read_orders(db, NOW) == {"read": False, "why": "HTTP 401"}
    assert ebay.read_orders(db, NOW + 5 * MINUTE)["read"] is True and shop.calls[-2] == ("token", "refresh_token")


def test_a_bug_in_ebays_work_never_stops_the_timer(shop, db, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError(f"an order of {PRIVATE[0]} with token {REFRESH}")
    monkeypatch.setattr(ebay, "read_orders", boom)
    with caplog.at_level(logging.DEBUG):
        out = timers.every_5_min(db, NOW)
    assert out["ebay"] == {"orders": "failed", "takedowns": []} and out["shares"] == [] and out["uploads"] == []
    assert "ebay: orders failed: RuntimeError at " in caplog.text and REFRESH not in caplog.text
    assert PRIVATE[0] not in caplog.text
    monkeypatch.setattr(ebay, "tick", boom)
    assert timers.every_5_min(db, NOW)["ebay"] == "failed"
    monkeypatch.setattr(ebay, "consent_watch", boom)
    assert timers.every_15_min(db, NOW)["ebay"] == "failed"


def test_the_orders_wait_for_a_consent(db, live, fake_ebay, sent):
    assert ebay.read_orders(db, NOW) == {"read": False}                        # set up, never connected: nobody asked
    connect(db)
    fake_ebay.token_status = 400
    assert ebay.read_orders(db, NOW + 3 * HOUR) == {"read": False, "why": "not connected"}
    assert fake_ebay.count("orders") == 0 and call(db, "GET", "/ebay/status")[1]["orders_error"] is None


# --- a sale elsewhere ends the eBay listing from Azure ------------------------------------------------------------------

def test_a_sale_on_another_site_ends_the_ebay_listing_at_once(shop, db, sent):
    status, out, _ = call(db, "POST", "/email", posh_sale())
    assert (status, out["status"]) == (200, "matched") and shop.ended == [LISTING]
    done = task(db)
    assert (done["status"], done["evidence"], done["done_at"]) == ("done", f"ended on eBay: {LISTING}",
                                                                   "2026-10-08T15:00:00+00:00")
    assert row(db) == "delisted"                                               # the mirror follows; the Mac's row later
    # the listing was READ first, then ended once — by its id, the reason "no longer available", inputs as variables
    assert graphql(shop) == ["sellerListings", "endListing"]
    asked = shop.requests[-1]
    assert asked["url"] == "https://graphqlapi.ebay.com/graphql" and asked["method"] == "POST"
    assert json.loads(asked["body"])["variables"] == {"input": {"listingIdentifier": {"listingId": LISTING},
                                                                "endListingReason": "NOT_AVAILABLE"}}
    assert asked["headers"]["Authorization"] == "Bearer user-access-token-1"
    # the Mac is never asked for it — and learns of it from GET /tasks
    assert [t["marketplace"] for t in core.take_tasks(db, list(SITES4), NOW)] == ["depop", "vinted"]
    answer = call(db, "GET", "/tasks", query={"sites": "poshmark,depop,vinted,ebay"})[1]
    assert answer["tasks"] == [] and answer["ended"] == [{"item": ITEM, "site": "ebay", "at": "2026-10-08T15:00:00+00:00",
                                                          "result": "done"}]
    assert call(db, "GET", "/ebay/status")[1]["ended"] == 1
    for site in ("depop", "vinted"):
        core.task_result(db, task(db, site)["id"], "done", now=NOW + 5 * MINUTE)
    assert texts(sent, "group")[-1] == "✓ Lacoste Tee White size M taken down on Depop, Vinted and eBay."
    assert timers.every_5_min(db, NOW + 5 * MINUTE)["ebay"]["takedowns"] == [] and shop.ended == [LISTING]   # once


@pytest.mark.parametrize("how", ["a listing synced after the sale", "the Mac saw it sold", "a site's orders"])
def test_wherever_the_takedown_is_made_it_is_done_after_that_request(shop, db, how):
    if how == "a listing synced after the sale":
        db.execute("DELETE FROM listings WHERE marketplace = 'ebay'")
        core.process_email(db, posh_sale(), NOW)
        assert shop.ended == []
        call(db, "POST", "/sync", {"listings": [{"item_id": ITEM, "marketplace": "ebay", "status": "posted",
                                                 "listing_id": LISTING, "url": EBAY_URL,
                                                 "updated_at": iso(NOW + MINUTE)}]}, now=NOW + MINUTE)
    elif how == "the Mac saw it sold":
        call(db, "POST", "/mac-event", {"kind": "sold_found", "marketplace": "depop", "listing_id": DEPOP_ID,
                                        "item_id": ITEM, "title": TITLE, "price": 35, "sold_at": iso(NOW)})
    else:
        call(db, "POST", "/orders", {"marketplace": "vinted", "swept_at": iso(NOW), "orders": [
            {"order_id": "777", "status": "to_ship", "sku": ITEM, "title": TITLE, "price": 35,
             "sold_at": "2026-10-08T14:00:00+00:00"}]})
    assert shop.ended == [LISTING] and task(db)["status"] == "done" and row(db) == "delisted"


def test_the_listing_is_found_by_our_sku_when_the_mirror_holds_no_id(live, fake_ebay, db, sent):
    connect(db)
    seed4(db, ebay_id=None)
    fake_ebay.listing("325000000444")
    core.process_email(db, posh_sale(), NOW)
    assert fake_ebay.ended == ["325000000444"] and graphql(fake_ebay) == ["activeListings", "endListing"]
    assert json.loads(fake_ebay.requests[-2]["body"])["variables"]["search"]["filters"]["skus"] == [ITEM]


def test_a_relisting_the_mirror_doesnt_hold_yet_comes_down_too(shop, db):
    shop.listing(state="ENDED")                                                # the id the mirror holds: ended before
    shop.listing("325000000777")                                               # … and listed again, same SKU
    core.process_email(db, posh_sale(), NOW)
    assert shop.ended == ["325000000777"] and task(db)["evidence"] == "ended on eBay: 325000000777"


def test_a_listing_already_ended_is_done_and_one_already_sold_is_sold_twice(shop, db, sent):
    shop.listing(state="ENDED")
    core.process_email(db, posh_sale(), NOW)
    assert (task(db)["status"], task(db)["evidence"]) == ("done", "already ended on eBay")
    assert shop.ended == [] and "endListing" not in graphql(shop) and row(db) == "delisted"


def test_a_listing_that_sold_on_ebay_first_is_sold_twice_once(shop, db, sent):
    shop.listing(state="ENDED", sold=1)
    core.process_email(db, posh_sale(), NOW)
    assert (task(db)["status"], row(db)) == ("sold", "sold") and shop.ended == []
    twice = "⚠️ Sold twice: Lacoste Tee White size M sold on Poshmark and eBay. Cancel the eBay order in its app."
    assert texts(sent, "group").count(twice) == 1
    assert call(db, "GET", "/tasks")[1]["ended"][0]["result"] == "sold"
    shop.orders = [order()]                                                    # then its order is read: the same news
    ebay.read_orders(db, NOW + 5 * MINUTE)
    assert db.one("SELECT status FROM sales WHERE marketplace = 'ebay'")["status"] == "double_sale"
    assert texts(sent, "group").count(twice) == 1 and len([t for t in texts(sent, "group") if "Sold on eBay" in t]) == 0


def test_a_buyer_a_moment_faster_is_sold_twice_not_a_failure(shop, db, sent):
    shop.sells_as_it_ends = True
    core.process_email(db, posh_sale(), NOW)
    assert (task(db)["status"], task(db)["evidence"]) == ("sold", f"eBay listing {LISTING} sold before it could be ended")
    assert any(text.startswith("⚠️ Sold twice") for text in texts(sent, "group"))


@pytest.mark.parametrize("sku", ["HER-OWN-SKU-7", None])
def test_a_listing_that_isnt_ours_is_never_ended(shop, db, sent, sku):
    """The id our mirror holds shows another SKU (or none) on eBay: read, refused, a failed try — never ended."""
    shop.listing(sku=sku)
    core.process_email(db, posh_sale(), NOW)
    failed = task(db)
    assert (failed["status"], failed["attempts"], failed["last_error"]) == (
        "pending", 1, "the eBay listing our records hold carries another SKU — not ours to end")
    assert shop.ended == [] and graphql(shop) == ["sellerListings", "activeListings", "endedListings"]
    assert shop.listings[LISTING]["state"] == "ACTIVE" and row(db) == "posted"
    for minutes in (5, 10):
        timers.every_5_min(db, NOW + minutes * MINUTE)
    assert task(db)["status"] == "failed" and LEFT_TO_HER in texts(sent, "group") and shop.ended == []


def test_no_listing_on_ebay_is_not_found(shop, db, sent):
    shop.listings.clear()
    core.process_email(db, posh_sale(), NOW)
    gone = task(db)
    assert gone["status"] == "not_found" and shop.ended == []
    assert f"Take-down: Lacoste Tee White size M wasn't found on eBay ({gone['id']}): eBay has no listing with this item's " \
           "SKU." in texts(sent, "ops")
    assert call(db, "GET", "/tasks")[1]["ended"][0]["result"] == "not_found"


def test_a_failure_is_tried_again_by_the_timer_and_the_third_is_left_to_her(shop, db, sent):
    shop.graphql_status = 500
    core.process_email(db, posh_sale(), NOW)
    first = task(db)
    assert (first["status"], first["attempts"], first["last_error"], first["lease_until"]) == (
        "pending", 1, "HTTP 500", "2026-10-08T15:04:00+00:00")
    core.sync(db, {"items": [], "listings": []}, NOW + MINUTE)                 # another request: not another try
    call(db, "POST", "/email", posh_sale("posh-sale-again"), now=NOW + 2 * MINUTE)
    assert task(db)["attempts"] == 1 and LEFT_TO_HER not in texts(sent, "group")
    # open as it is, it is never the Mac's: not when it asks for eBay's tasks, not when its lease would have run out
    for when in (NOW + 2 * MINUTE, NOW + 4 * MINUTE + timedelta(seconds=30)):
        assert "ebay" not in [t["marketplace"] for t in core.take_tasks(db, list(SITES4), when)]
    assert [t["marketplace"] for t in call(db, "GET", "/tasks", query={"sites": "ebay"}, now=NOW + 3 * MINUTE)[1]["tasks"]] == []
    assert timers.every_5_min(db, NOW + 5 * MINUTE)["ebay"]["takedowns"] == [
        {"id": first["id"], "item": ITEM, "result": "failed"}]
    assert task(db)["attempts"] == 2 and ebay.site_mark(db, NOW + 5 * MINUTE) == "eBay ✗ a take-down failed"
    timers.every_5_min(db, NOW + 10 * MINUTE)
    last = task(db)
    assert (last["status"], last["attempts"]) == ("failed", 3) and texts(sent, "group").count(LEFT_TO_HER) == 1
    assert f"Take-down of Lacoste Tee White size M on eBay left to the owner ({last['id']}, failed 3 times): HTTP 500." \
        in texts(sent, "ops")
    assert REFRESH not in " ".join(texts(sent))                                # eBay's own words (its body) never
    for minutes in (15, 20):
        timers.every_5_min(db, NOW + minutes * MINUTE)
    assert texts(sent, "group").count(LEFT_TO_HER) == 1 and shop.ended == []
    assert ebay.site_mark(db, NOW + 20 * MINUTE) == "eBay ✗ a take-down failed"   # still listed there: still open
    assert call(db, "GET", "/tasks")[1]["ended"] == []


def test_ebay_back_before_the_third_try_ends_it(shop, db, sent):
    shop.graphql_status = 500
    core.process_email(db, posh_sale(), NOW)
    shop.graphql_status = 200
    timers.every_5_min(db, NOW + 5 * MINUTE)
    assert shop.ended == [LISTING] and task(db)["status"] == "done" and LEFT_TO_HER not in texts(sent, "group")
    assert ebay.site_mark(db, NOW + 5 * MINUTE) == "eBay ✓"


def test_without_a_consent_the_owner_hears_the_listing_is_still_up(db, live, fake_ebay, sent):
    """Set up, never connected: no call to eBay, three failed tries, the group's line — a double sale is the risk."""
    seed4(db)
    core.process_email(db, posh_sale(), NOW)
    assert (task(db)["status"], task(db)["last_error"]) == ("pending", "not connected")
    for minutes in (5, 10):
        timers.every_5_min(db, NOW + minutes * MINUTE)
    assert task(db)["status"] == "failed" and texts(sent, "group").count(LEFT_TO_HER) == 1 and fake_ebay.calls == []


def test_a_refused_consent_found_by_a_takedown_is_the_human_step_too(shop, db, sent):
    shop.token_status = 400
    core.process_email(db, posh_sale(), NOW + 3 * HOUR)                        # the token of the consent has ended
    assert task(db)["last_error"] == "not connected" and shop.ended == []
    private = [text for text in texts(sent, "ops") if text.startswith("🔐 eBay wants a person")]
    assert len(private) == 1 and "(since 2:00 PM)" in private[0] and not any("🔐" in t for t in texts(sent, "group"))
    for minutes in (5, 10):
        timers.every_5_min(db, NOW + 3 * HOUR + minutes * MINUTE)
    assert texts(sent, "group").count(LEFT_TO_HER) == 1
    assert len([text for text in texts(sent) if "🔐" in text]) == 1            # the step is said once


@pytest.mark.parametrize("trouble, error", [
    ("errors", "GraphQL error RATE_LIMITED"), ("refused", "eBay refused to end it (LSTNG_END_VALIDATION_ERROR, "
                                                          "LSTNG_ALREADY_ENDED)"),
    ("away", "eBay couldn't be reached (OSError)"), ("inventory", "ending an eBay listing made with the Inventory API "
                                                                  "isn't built yet (EBAY_API=inventory)")])
def test_every_kind_of_no_is_a_failed_try_with_its_kind(shop, db, monkeypatch, trouble, error):
    if trouble == "errors":                                                    # HTTP 200 carrying `errors`
        shop.graphql_errors = [{"message": "Too many requests for seller thrift_shop",
                                "extensions": {"errorCode": "RATE_LIMITED"}}]
    elif trouble == "refused":
        shop.end_refused = True
    elif trouble == "away":
        shop.down = True
    else:
        monkeypatch.setenv("EBAY_API", "inventory")
    core.process_email(db, posh_sale(), NOW)
    assert (task(db)["status"], task(db)["attempts"], task(db)["last_error"]) == ("pending", 1, error)
    assert shop.ended == [] and row(db) == "posted"
    if trouble == "inventory":
        assert graphql(shop) == []


def test_a_token_ebay_refuses_is_dropped_and_the_next_try_ends_it(shop, db):
    shop.tokens.clear()
    core.process_email(db, posh_sale(), NOW)
    assert task(db)["last_error"] == "HTTP 401"
    timers.every_5_min(db, NOW + 5 * MINUTE)
    assert shop.ended == [LISTING] and task(db)["status"] == "done"


def test_nothing_is_taken_down_outside_live_mode_or_inside_a_transaction(db, fake_ebay, sent, monkeypatch):
    connect(db)                                                                # replay (the default): no task at all
    seed4(db)
    fake_ebay.listing()
    core.process_email(db, posh_sale(), NOW)
    assert tasks(db) == [] and ebay.run_takedowns(db, NOW) == [] and graphql(fake_ebay) == []
    db.execute("INSERT INTO delist_tasks (id, sale_id, item_id, marketplace, status, attempts, created_at) "
               "VALUES ('t_by_hand', NULL, ?, 'ebay', 'pending', 0, ?)", (ITEM, iso(NOW)))
    ebay.kick(db, NOW)
    assert graphql(fake_ebay) == [] and call(db, "GET", "/tasks")[1]["ended"] == []
    monkeypatch.setenv("SALES_MODE", "live")
    with db.tx():                                                              # a call to eBay is never made in here
        ebay.kick(db, NOW)
        assert ebay.run_takedowns(db, NOW) == []
    assert graphql(fake_ebay) == []
    ebay.kick(db, NOW)
    assert fake_ebay.ended == [LISTING] and task(db)["status"] == "done"


def test_the_safety_nets_repost_of_an_ebay_row_is_still_the_macs(shop, db):
    db.execute("INSERT INTO delist_tasks (id, sale_id, item_id, marketplace, status, attempts, created_at, kind) "
               "VALUES ('t_repost', NULL, ?, 'ebay', 'pending', 0, ?, 'repost')", (ITEM, iso(NOW)))
    assert ebay.run_takedowns(db, NOW) == [] and graphql(shop) == []
    assert [(t["id"], t["kind"]) for t in core.take_tasks(db, ["ebay"], NOW)] == [("t_repost", "repost")]
    assert call(db, "GET", "/tasks")[1]["ended"] == []


def test_the_queries_are_whole_and_take_their_inputs_as_variables():
    for query, variable in ((ebay.Q_LISTING, "$input"), (ebay.Q_ACTIVE, "$search"), (ebay.Q_ENDED, "$search"),
                            (ebay.M_END, "$input")):
        assert query.count("{") == query.count("}") and query.count("(") == query.count(")")
        assert variable in query and '"' not in query and not any(ch.isdigit() for ch in query)
    assert "SellerActiveListingSearchInput" in ebay.Q_ACTIVE and "SellerEndedListingSearchInput" in ebay.Q_ENDED
    assert "endedListings(searchInput: $search)" in ebay.Q_ENDED and "quantitySold" in ebay.Q_LISTING


# --- the fourth site, only while it is on ---------------------------------------------------------------------------------

def test_ebay_is_on_by_the_macs_word_else_by_the_mirror(db):
    assert ebay.on(db) is False and ebay.sites(db) == ("poshmark", "depop", "vinted")
    seed4(db)
    assert ebay.on(db) is True                                                 # the Mac says nothing of eBay: the mirror
    core.heartbeat(db, "mac", {"ebay": {"on": False}}, NOW)
    assert ebay.on(db) is False                                                # its word wins
    core.heartbeat(db, "mac", {"ebay": {"on": True, "live": False}}, NOW)
    assert ebay.sites(db) == SITES4
    db.execute("DELETE FROM listings WHERE marketplace = 'ebay'")
    core.heartbeat(db, "mac", {"sites": {}}, NOW)
    assert ebay.on(db) is False


def test_the_mac_may_speak_of_ebay_once_it_is_on(db, sent):
    found = {"kind": "sold_found", "marketplace": "ebay", "listing_id": LISTING, "item_id": ITEM, "title": TITLE,
             "price": 35}
    assert call(db, "POST", "/mac-event", found)[0] == 400
    assert call(db, "POST", "/orders", {"marketplace": "ebay", "orders": []})[0] == 400
    seed4(db)
    beat(db)
    assert call(db, "POST", "/orders", {"marketplace": "ebay", "orders": []})[0] == 200
    status, out, _ = call(db, "POST", "/mac-event", found)
    assert (status, out["status"], out["item_id"]) == (200, "matched", ITEM)


def priced(db, item: str, at, sites=("poshmark", "depop", "vinted"), **rows) -> None:
    """An item priced at `at`, live on `sites`; `rows`: other sites' rows as {site: (status, error)}."""
    listings = [{"item_id": item, "marketplace": site, "status": "posted", "updated_at": iso(at)} for site in sites]
    listings += [{"item_id": item, "marketplace": site, "status": status, "error": error, "updated_at": iso(at)}
                 for site, (status, error) in rows.items()]
    core.sync(db, {"items": [{"id": item, "title": f"Item {item}", "price": 35, "status": "ready", "priced": 1,
                              "priced_at": iso(at), "created_at": iso(at), "updated_at": iso(at)}],
                   "listings": listings}, at)


def test_not_live_means_all_three_until_ebay_is_on_then_all_four(db, live, sent, no_quiet_hours):
    priced(db, "i_before", NOW - 3 * HOUR)
    assert watch.not_live(db, NOW, "live") == []                               # eBay off: the three are everywhere
    beat(db)                                                                   # the Mac: eBay is on
    assert watch.not_live(db, NOW, "live") == []                               # priced before it began: owes it nothing
    priced(db, "i_new", NOW + 10 * MINUTE)
    priced(db, "i_queued", NOW + 10 * MINUTE, ebay=("queued", None))
    priced(db, "i_all", NOW + 10 * MINUTE, sites=SITES4)
    priced(db, "i_before", NOW - 3 * HOUR, ebay=("failed", "eBay refused the listing: a missing aspect"))
    assert watch.not_live(db, NOW + 2 * HOUR, "live") == ["i_before"]          # it has an eBay row now: it counts
    assert watch.not_live(db, NOW + 2 * HOUR + 15 * MINUTE, "live") == ["i_new", "i_queued"]
    assert texts(sent, "ops") == [
        "🚨 Item i_before isn't live everywhere 2 h after its price — eBay: eBay refused the listing: a missing aspect",
        "🚨 Item i_new isn't live everywhere 2 h after its price — eBay: not tried yet",
        "🚨 Item i_queued isn't live everywhere 2 h after its price — eBay: waiting in line"]
    core.heartbeat(db, "mac", {"quiet": {"enabled": False}, "ebay": {"on": False}}, NOW + 3 * HOUR)
    priced(db, "i_later", NOW + 3 * HOUR)
    assert watch.not_live(db, NOW + 6 * HOUR, "live") == []                    # off again: three are everywhere


def evening(db, heartbeat: dict, at=utc(2026, 10, 9, 1, 5)) -> str:
    core.heartbeat(db, "mac", {"quiet": {"enabled": False}, **heartbeat}, at - 5 * MINUTE)
    return watch.evening_line(db, at, "live")                                  # 9:05 PM on Oct 8 in New York


def test_the_9_pm_line_counts_four_sites_and_marks_ebay_only_while_it_is_on(db, live, fake_ebay, sent):
    connect(db)
    made = utc(2026, 10, 8, 16, 0)
    priced(db, "i_four", made, sites=SITES4)
    priced(db, "i_three", made, ebay=("queued", None))
    check = {"poshmark": {"ok": True, "at": "2026-10-08T18:31:00+00:00", "slot": "14:30"}}
    line = evening(db, {"formcheck": check, "ebay": {"on": True, "live": True, "free_used": 12, "free_month": "2026-10",
                                                     "fees_checked": "2026-10-01"}})
    assert line == ("📊 Today: 0 shares → 2 items, 1 live on all 4 sites with all photos.\n"
                    "Sites: Poshmark ✓ · eBay ✓ — form checks today: Poshmark 2:30 ✓. "
                    "eBay: 12 of 250 free listings used this month.")
    day, at = "2026-10-08", utc(2026, 10, 9, 1, 5)
    core.heartbeat(db, "mac", {"ebay": {"on": True, "free_used": 250, "free_month": "2026-09"}}, at)
    assert watch.sites_line(db, day, at) == "\nSites: eBay ✓."                 # last month's count isn't this month's
    core.heartbeat(db, "mac", {"ebay": {"on": True}, "sites": {"ebay": {"reason": "its API refuses listings",
                                                                       "since": "2026-10-08T20:04:00+00:00"}}}, at)
    assert watch.sites_line(db, day, at) == ("\nSites: eBay ✗ paused: its API refuses listings since 4:04 PM — no form "
                                             "check ran today.")
    core.heartbeat(db, "mac", {"ebay": {"on": True}}, at)
    db.execute("INSERT INTO settings (key, value) VALUES ('ebay_needs_consent', ?)", (json.dumps({"since": iso(at)}),))
    assert watch.sites_line(db, day, at) == "\nSites: eBay ✗ its permission ended."
    db.execute("DELETE FROM settings WHERE key IN ('ebay_needs_consent', 'ebay_consent')")
    assert watch.sites_line(db, day, at) == "\nSites: eBay ✗ not connected yet."


def test_the_9_pm_line_is_as_it_was_while_ebay_is_off(db, live, fake_ebay, sent, monkeypatch):
    made = utc(2026, 10, 8, 16, 0)
    priced(db, "i_three", made)
    check = {"poshmark": {"ok": True, "at": "2026-10-08T18:31:00+00:00", "slot": "14:30"}}
    line = evening(db, {"formcheck": check})
    assert line == ("📊 Today: 0 shares → 1 item, 1 live on all 3 sites with all photos.\n"
                    "Sites: Poshmark ✓ — form checks today: Poshmark 2:30 ✓.")
    assert watch.sites_line(db, "2026-10-08", utc(2026, 10, 9, 1, 5)).count("eBay") == 0
    core.heartbeat(db, "mac", {"ebay": {"on": True}}, made)
    monkeypatch.delenv("EBAY_CLIENT_ID")
    assert ebay.site_mark(db, made) == "eBay ✗ not set up" and ebay.free_line(db, made) == ""


def test_the_dashboard_shows_ebays_column_only_while_it_is_on(db, sent):
    seed(db)
    off = core.dashboard_data(db, NOW)
    assert "ebay" not in off and "eBay" not in dashboard.render(off, NOW)
    seed4(db)
    beat(db)
    on = core.dashboard_data(db, NOW)
    page = dashboard.render(on, NOW)
    assert on["ebay"] is True and '<th scope="col" class="s">eBay</th>' in page
    assert '<dt>eBay</dt><dd id="c-ebay">1</dd>' in page and f'href="{EBAY_URL}"' in page and "Open on eBay" in page
    assert "Orders read on the sites: Poshmark — · Depop — · Vinted — · eBay —" in page
    assert page.count("<th ") == 9 and 'colspan="9"' in page
    core.heartbeat(db, "mac", {"ebay": {"on": False}}, NOW)                    # turned off on the Mac: three again
    again = dashboard.render(core.dashboard_data(db, NOW), NOW)
    assert "eBay" not in again and again.count("<th ") == 8


def test_an_ebay_sale_reads_as_ebay_on_the_dashboard(shop, db):
    beat(db)
    shop.orders = [order()]
    ebay.read_orders(db, NOW)
    page = dashboard.render(core.dashboard_data(db, NOW), NOW)
    assert 'eBay<span class="sep"> · </span><span class="day">Thu Oct 8</span>' in page
    assert "Orders read on the sites: Poshmark — · Depop — · Vinted — · eBay Thu Oct 8, 11:00 AM" in page
