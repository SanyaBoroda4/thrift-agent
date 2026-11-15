"""WO68 — unpaid eBay orders (the owner, 2026-11-14): seen through the Trading API's GetOrders (the Fulfillment API
shows only paid ones); a sale "not paid yet, don't ship" with the take-downs elsewhere; the agent's two notes at 24 h and
48 h (SendInvoice, else a message to the buyer), never in the quiet hours or at night, never a third; paid at any point
→ everything stops and "✅ Paid on eBay"; the day-4 cancel — eBay's own (its preferences: 4 days, no relist), else ours
an hour later with the ONE reason "buyer hasn't paid", never earlier; cancelled → relisted everywhere; the 9 PM list
says "💵 Paid" on every package and keeps the unpaid ones in their own ⏳ block; a missing scope is ONE private message.
No buyer data is ever kept."""
import html
import inspect
import logging
import re
from datetime import timedelta

import pytest
from apitools import IDS, ITEM, TITLE, URLS, call, seed, texts, utc
from ebayfake import BUYER_ID, LISTING, PRIVATE, START_LINK, connect, everything, order, unpaid_order
from thrift_api import core, ebay, shiplist, timers, unpaid
from thrift_api.util import iso

MINUTE, HOUR, DAY = timedelta(minutes=1), timedelta(hours=1), timedelta(days=1)
COMMIT = utc(2026, 10, 8, 22, 37, 18)            # Thu Oct 8, 6:37 PM in New York (EDT)
REF = f"{LISTING}-10077777777771"
SITES4 = ("poshmark", "depop", "vinted", "ebay")
SOLD = "🛒 Sold on eBay: Lacoste Tee White size M — $44 · not paid yet, don't ship."
NOTE_1 = ("Hi! Just a friendly reminder that your order for Lacoste Tee White size M is waiting for payment. We'll "
          "ship it within 2 business days of payment. Thank you!")
NOTE_2 = ("Hi! Your order for Lacoste Tee White size M is still unpaid. If payment isn't received by Tuesday, October "
          "13 at 6:37 PM ET, the order will be cancelled. Thanks!")


@pytest.fixture
def shop(db, monkeypatch, fake_ebay, sent):
    """Live, eBay connected, the item listed on all four sites; eBay's GetOrders shows its unpaid order."""
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-10-01T00:00:00+00:00")
    assert connect(db)[0] == 200
    seed(db, sites=SITES4, urls={**URLS, "ebay": f"https://www.ebay.com/itm/{LISTING}"}, ids={**IDS, "ebay": LISTING})
    fake_ebay.trading = [unpaid_order()]
    sent.clear()
    return fake_ebay


def tick(db, at):
    return ebay.tick(db, at)


def sale(db) -> dict:
    [row] = db.query("SELECT * FROM sales")
    return row


def plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# --- seen -------------------------------------------------------------------------------------------------------------

def test_an_unpaid_order_is_a_sale_not_paid_yet_with_its_takedowns_and_never_to_ship(shop, db, sent):
    out = tick(db, COMMIT + 5 * MINUTE)
    assert out["unpaid"]["changed"] == [f"{REF}: recorded unpaid"]
    s = sale(db)
    assert (s["payment"], s["pay_ref"], s["order_id"], s["committed_at"], s["cancel_from"]) == (
        "unpaid", REF, None, iso(COMMIT), iso(COMMIT + 5 * DAY))
    assert texts(sent, "group") == [SOLD]                                          # not the 💰 line
    tasks = db.query("SELECT marketplace FROM delist_tasks ORDER BY marketplace")
    assert [t["marketplace"] for t in tasks] == ["depop", "poshmark", "vinted"]   # a second buyer can't buy it
    tick(db, COMMIT + 10 * MINUTE)
    assert len(db.query("SELECT id FROM sales")) == 1 and texts(sent, "group") == [SOLD]      # never twice
    text = everything(db)
    assert BUYER_ID not in text and not any(p in text for p in PRIVATE)            # no buyer data, ever
    assert timers.reminders(db, utc(2026, 10, 12, 14, 0), "live") == []            # never "ship tomorrow"


def test_a_sale_the_mac_saw_sold_is_marked_unpaid_with_the_same_line(shop, db, sent):
    call(db, "POST", "/mac-event", {"kind": "sold_found", "marketplace": "ebay", "listing_id": LISTING,
                                    "item_id": ITEM, "title": TITLE, "price": 44, "sold_at": iso(COMMIT + MINUTE)},
         now=COMMIT + 2 * MINUTE)
    assert texts(sent, "group")[0].startswith("💰 Sold on eBay")
    tick(db, COMMIT + 5 * MINUTE)
    s = sale(db)
    assert (s["payment"], s["pay_ref"]) == ("unpaid", REF) and texts(sent, "group")[-1] == SOLD
    assert len(db.query("SELECT id FROM sales")) == 1


def test_her_own_listing_is_never_acted_on(shop, db, sent):
    shop.trading = [unpaid_order(ref="330000000009-10066666666661", sku="HER-OWN-SKU-7")]
    for hours in (1, 25, 49, 121):
        tick(db, COMMIT + hours * HOUR)
    assert db.query("SELECT id FROM sales") == [] and shop.invoices == [] and shop.cancels == [] and sent == []


# --- the two notes ------------------------------------------------------------------------------------------------

def test_the_first_note_goes_24_hours_after_the_purchase_once(shop, db, sent):
    tick(db, COMMIT + 5 * MINUTE)
    tick(db, COMMIT + 23 * HOUR)
    assert shop.invoices == []
    tick(db, COMMIT + 24 * HOUR + 2 * MINUTE)                                     # Fri 6:39 PM: may write
    assert shop.invoices == [{"ref": REF, "text": NOTE_1, "copy": "false"}]
    assert texts(sent, "ops")[-1] == "💳 Reminder sent to the eBay buyer of Lacoste Tee White size M."
    for minutes in (5, 10, 60):
        tick(db, COMMIT + 24 * HOUR + minutes * MINUTE)
    assert len(shop.invoices) == 1                                                 # never twice
    assert unpaid.reminders_count(db, sale(db), COMMIT + 25 * HOUR) == 2           # eBay's own 24 h one, and ours


def test_a_note_never_goes_at_night_or_in_her_quiet_hours(shop, db, sent):
    late = utc(2026, 10, 9, 3, 0)                                                  # Thu Oct 8, 11:00 PM
    shop.trading = [unpaid_order(created=iso(late).replace("+00:00", ".000Z"))]
    tick(db, late + 5 * MINUTE)
    tick(db, late + 24 * HOUR + 5 * MINUTE)                                        # Fri 11:05 PM: night
    tick(db, utc(2026, 10, 10, 11, 55))                                            # Sat 7:55 AM: night
    assert shop.invoices == []
    tick(db, utc(2026, 10, 10, 12, 0))                                             # Sat 8:00 AM: the next allowed minute
    assert len(shop.invoices) == 1 and shop.invoices[0]["text"] == NOTE_1
    early = utc(2026, 10, 9, 13, 0)                                                # Fri 9:00 AM purchase
    assert not unpaid.may_write(early + DAY, unpaid.quiet.read(db))               # Sat 9:00 AM: her quiet hours
    assert not unpaid.may_write(utc(2026, 10, 10, 17, 59), unpaid.quiet.read(db))   # 1:59 PM
    assert unpaid.may_write(utc(2026, 10, 10, 18, 0), unpaid.quiet.read(db))        # 2:00 PM


def test_the_final_note_names_the_cancel_and_there_is_never_a_third(shop, db, sent):
    tick(db, COMMIT + 5 * MINUTE)
    tick(db, COMMIT + 24 * HOUR + 5 * MINUTE)
    tick(db, COMMIT + 48 * HOUR + 5 * MINUTE)                                      # Sat 6:42 PM
    assert [i["text"] for i in shop.invoices] == [NOTE_1, NOTE_2]
    assert texts(sent, "ops")[-1] == ("💳 Final reminder sent to the eBay buyer of Lacoste Tee White size M — it "
                                      "cancels Tue Oct 13 6:37 PM if still unpaid.")
    for hours in (60, 72, 96, 110):
        tick(db, COMMIT + hours * HOUR)
    assert len(shop.invoices) == 2                                                 # never a third


def test_a_note_due_when_the_next_is_due_already_gives_way_to_it(shop, db, sent):
    tick(db, COMMIT + 49 * HOUR)                                                   # first seen at 49 h (Sat 7:37 PM)
    assert [i["text"] for i in shop.invoices] == [NOTE_2]
    tick(db, COMMIT + 50 * HOUR)
    assert len(shop.invoices) == 1


def test_sendinvoice_refused_the_note_goes_as_a_message_and_the_buyer_is_never_kept(shop, db, sent, caplog):
    shop.invoice_errors = ["21917091"]
    tick(db, COMMIT + 5 * MINUTE)
    with caplog.at_level(logging.DEBUG):
        tick(db, COMMIT + 24 * HOUR + 5 * MINUTE)
    assert shop.invoices == [] and shop.messages == [{"item": LISTING, "subject": "Your order is waiting for payment",
                                                      "body": NOTE_1, "to": BUYER_ID, "type": "General"}]
    assert texts(sent, "ops")[-1] == "💳 Reminder sent to the eBay buyer of Lacoste Tee White size M."
    assert BUYER_ID not in everything(db) and BUYER_ID not in caplog.text and BUYER_ID not in " ".join(texts(sent))


def test_a_note_whose_answer_never_came_is_never_sent_again(shop, db, sent):
    shop.invoice_down = True
    tick(db, COMMIT + 5 * MINUTE)
    tick(db, COMMIT + 24 * HOUR + 5 * MINUTE)
    tick(db, COMMIT + 24 * HOUR + 10 * MINUTE)
    assert [n for n, what in shop.calls if what == "SendInvoice"] == ["trading"] and shop.messages == []
    assert "may not have reached eBay" in texts(sent, "ops")[-1]


def test_a_note_ebay_refuses_three_times_is_said_once(shop, db, sent):
    shop.invoice_errors, shop.aaq_errors = ["21917091"], ["518"]
    tick(db, COMMIT + 5 * MINUTE)
    for minutes in (5, 10, 15, 20, 25):
        tick(db, COMMIT + 24 * HOUR + minutes * MINUTE)
    assert [what for n, what in shop.calls if n == "trading"].count("AddMemberMessageAAQToPartner") == 3
    assert len([t for t in texts(sent, "ops") if t.startswith("⚠️ Couldn't send the eBay buyer")]) == 1
    assert shop.invoices == [] and shop.messages == []


# --- paid ---------------------------------------------------------------------------------------------------------

def test_paid_at_30_hours_everything_stops_and_it_joins_the_packages(shop, db, sent):
    tick(db, COMMIT + 5 * MINUTE)
    tick(db, COMMIT + 24 * HOUR + 5 * MINUTE)
    paid = COMMIT + 30 * HOUR
    shop.trading[0]["paid"] = iso(paid).replace("+00:00", ".000Z")
    shop.orders = [order(order_id="12-34567-89012", line="10077777777771", price="44.0",
                         created=iso(COMMIT).replace("+00:00", ".000Z"), ship_by="2026-10-14T06:59:59.000Z")]
    tick(db, paid + 5 * MINUTE)
    s = sale(db)
    assert (s["payment"], s["order_id"], s["ship_by"]) == ("paid", "12-34567-89012", "2026-10-13")
    assert texts(sent, "group")[-1] == "✅ Paid on eBay: Lacoste Tee White size M — ship by Tue Oct 13."
    assert len(db.query("SELECT id FROM sales")) == 1                              # the paid order is the same sale
    for hours in (48, 96, 121, 125):
        tick(db, COMMIT + hours * HOUR)
    assert len(shop.invoices) == 1 and shop.cancels == []                          # no final note, no cancel
    sent.clear()
    shiplist.night_list(db, utc(2026, 10, 11, 1, 0))                               # Sat Oct 10, 9 PM
    [page] = [m for c, m, _ in sent if c == "group"]
    text = plain(page)
    assert "1. Lacoste Tee White" in text and "eBay · $44 · 💵 Paid" in text and "NOT PAID" not in text


def test_paid_seen_first_by_getorders_says_it_once_with_ebays_rule(shop, db, sent):
    tick(db, COMMIT + 5 * MINUTE)
    paid = COMMIT + 26 * HOUR
    shop.trading[0]["paid"] = iso(paid).replace("+00:00", ".000Z")
    tick(db, paid + 5 * MINUTE)
    assert sale(db)["payment"] == "paid" and texts(sent, "group")[-1].startswith("✅ Paid on eBay: Lacoste Tee White")
    tick(db, paid + 10 * MINUTE)
    assert len([t for t in texts(sent, "group") if t.startswith("✅ Paid")]) == 1


# --- the day-4 cancel ---------------------------------------------------------------------------------------------

def test_ebay_didnt_cancel_ours_goes_an_hour_after_the_cancel_moment_and_it_is_relisted(shop, db, sent):
    tick(db, COMMIT + 5 * MINUTE)
    tick(db, COMMIT + 5 * DAY + 30 * MINUTE)
    assert shop.cancels == []                                                      # eBay's own cancel goes first
    tick(db, COMMIT + 5 * DAY + HOUR + MINUTE)
    assert shop.cancels == [{"commitment": f"ic-{REF}", "reason": "UNPAID_CONTRACT", "relist": False}]
    tick(db, COMMIT + 5 * DAY + HOUR + 6 * MINUTE)                                 # read back: eBay shows it cancelled
    s = sale(db)
    assert (s["status"], s["relist"], s["relist_why"]) == ("cancelled", "wanted", "unpaid")
    assert texts(sent, "group")[-1] == ("↩️ Lacoste Tee White size M: the eBay buyer didn't pay — cancelled and "
                                        "relisted everywhere.")
    assert call(db, "GET", "/tasks", now=COMMIT + 5 * DAY + 2 * HOUR)[1]["relist"] == [
        {"sale_id": s["id"], "item_id": ITEM, "marketplace": "ebay", "why": "unpaid"}]
    tick(db, COMMIT + 5 * DAY + 3 * HOUR)
    assert len(shop.cancels) == 1


def test_ebays_own_cancel_is_read_and_ours_never_goes(shop, db, sent):
    tick(db, COMMIT + 5 * MINUTE)
    shop.trading[0].update(status="Cancelled", cancel="CancelClosedForCommitment")
    tick(db, COMMIT + 4 * DAY + 2 * HOUR)
    assert sale(db)["status"] == "cancelled" and sale(db)["relist_why"] == "unpaid"
    tick(db, COMMIT + 6 * DAY)
    assert shop.cancels == []


def test_the_guard_no_cancel_before_ebay_allows_it_and_no_other_reason(shop, db):
    tick(db, COMMIT + 5 * MINUTE)
    s = sale(db)
    before = len(shop.calls)
    for at in (COMMIT + 4 * DAY - MINUTE, COMMIT + 5 * DAY - MINUTE):
        with pytest.raises(unpaid.TooEarly):
            unpaid.cancel_unpaid("any-token", s, at)
    early = {**s, "cancel_from": iso(COMMIT + DAY)}                                # a stored moment too early
    with pytest.raises(unpaid.TooEarly):
        unpaid.cancel_unpaid("any-token", early, COMMIT + 3 * DAY)
    assert len(shop.calls) == before                                               # nothing reached eBay
    assert list(inspect.signature(unpaid.cancel_unpaid).parameters) == ["token", "sale", "now"]   # no reason to pass
    assert unpaid.REASON == "UNPAID_CONTRACT" and "OUT_OF_STOCK" not in inspect.getsource(unpaid)
    assert unpaid.allowed_from(early) == COMMIT + 4 * DAY                          # never within 4 days


def test_without_its_scopes_one_message_asks_for_them_and_the_cancel_says_the_taps(shop, db, sent):
    consent = core._loads(core.setting(db, ebay.CONSENT))
    old = " ".join(s for s in ebay.DEFAULT_SCOPES.split() if not s.endswith(("commitment.read", "cancellation")))
    core.put_setting(db, ebay.CONSENT, core._dumps({**consent, "scopes": old}))
    shop.cancel_scope = False
    tick(db, COMMIT + 5 * MINUTE)
    tick(db, COMMIT + 10 * MINUTE)
    [ask] = [t for t in texts(sent, "ops") if t.startswith("🔐 eBay: one more permission")]
    assert "sell.commitment.read (to find the unpaid order)" in ask and "sell.cancellation" in ask and START_LINK in ask
    tick(db, COMMIT + 5 * DAY + HOUR + MINUTE)
    tick(db, COMMIT + 5 * DAY + 2 * HOUR + 2 * MINUTE)
    [taps] = [t for t in texts(sent, "ops") if t.startswith("⚠️ Lacoste Tee White size M — the eBay buyer hasn't paid")]
    assert "Selling → Orders → Lacoste Tee White size M → Cancel order → Buyer hasn't paid" in taps
    assert len([t for t in texts(sent, "ops") if t.startswith("🔐 eBay: one more permission")]) == 1
    assert call(db, "GET", "/health")[1]["unpaid"]["missing_scopes"] == ["sell.commitment.read", "sell.cancellation"]


# --- the preferences ----------------------------------------------------------------------------------------------

def test_ebays_own_cancel_is_set_to_4_days_without_its_relist_and_read_back(shop, db):
    token = ebay.access_token(db, "user", COMMIT)["access_token"]
    assert unpaid.preferences(token) == {"opt_in": None, "delay_days": None, "auto_relist": None, "excluded": 0}
    got = unpaid.set_preferences(token)
    assert shop.prefs == {"OptInStatus": "true", "DelayBeforeOpeningDispute": "4", "AutoRelist": "false"}
    assert got == {"opt_in": True, "delay_days": 4, "auto_relist": False, "excluded": 0} and unpaid.wanted(got)


# --- the 9 PM list ------------------------------------------------------------------------------------------------

def test_the_list_says_paid_on_every_package_and_keeps_the_unpaid_ones_apart(shop, db, sent):
    core.process_email(db, {"message_id": "posh-1", "thread_id": "t", "from": "Poshmark <noreply@poshmark.com>",
                            "subject": "Congratulations! Your item has sold", "date": "2026-10-08T14:05:00.000Z",
                            "text": "Item: Zara Linen Blazer White size S\nPrice: $40.00\nOrder ID: "
                                    "6702bb11cc22dd33ee44ff66"}, utc(2026, 10, 8, 14, 10))
    tick(db, COMMIT + 5 * MINUTE)
    sent.clear()
    out = shiplist.night_list(db, utc(2026, 10, 9, 1, 0))
    [page] = [m for c, m, _ in sent if c == "group"]
    text = plain(page)
    assert "1 package  ·  🟢 1 later  ·  ⏳ 1 not paid" in text
    assert "Poshmark · $40 · 💵 Paid · sold Thu Oct 8" in text
    assert ("⏳  NOT PAID YET — DON'T SHIP (1)\nLacoste Tee White · eBay · $44\nsold Thu Oct 8 · 0 reminders sent · "
            "cancels Tue Oct 13 6:37 PM if still unpaid") in text
    assert "2. " not in text and text.index("NOT PAID YET") > text.index("1. ") and text.index("NOT PAID") < text.index("──")
    assert out["packages"] == 1 and out["not_paid"] == 1
    numbers = core._loads(core.setting(db, shiplist.NUMBERS))
    assert numbers["unpaid"] == [sale_id(db, "ebay")] and sale_id(db, "ebay") not in numbers["packages"]["1"]
    call(db, "POST", "/mac-event", {"kind": "shipped", "words": "all"}, now=utc(2026, 10, 9, 1, 30))
    assert db.one("SELECT shipped_at FROM sales WHERE marketplace = 'ebay'")["shipped_at"] is None   # never touched
    assert shiplist.status(db, utc(2026, 10, 9, 2, 0))["not_paid"] == 1


def test_nothing_to_ship_but_an_unpaid_one_still_shows_it(shop, db, sent):
    tick(db, COMMIT + 5 * MINUTE)
    for hours in (24, 48):
        tick(db, COMMIT + hours * HOUR + 5 * MINUTE)
    sent.clear()
    shiplist.night_list(db, utc(2026, 10, 11, 1, 0))                               # Sat Oct 10, 9 PM
    [page] = [m for c, m, _ in sent if c == "group"]
    text = plain(page)
    assert text.startswith("📦 Nothing to ship tonight") and "NOT PAID YET — DON'T SHIP (1)" in text
    assert "4 reminders sent" in text                                              # eBay's two, and the agent's two


def sale_id(db, site: str) -> str:
    return db.one("SELECT id FROM sales WHERE marketplace = ?", (site,))["id"]


# --- the Edikted dress (WO68 §6) ----------------------------------------------------------------------------------

def test_the_edikted_dress_gets_only_the_final_note_after_2_pm_sunday_and_no_new_line(shop, db, sent):
    ref = "407358644533-10086346752915"
    committed = utc(2026, 11, 12, 23, 37, 18)
    seed(db, item_id="i_261101_46ce94", title="Edikted Crochet Knit Mini Dress", sites=SITES4,
         urls={**URLS, "ebay": "https://www.ebay.com/itm/407358644533"}, ids={**IDS, "ebay": "407358644533"})
    call(db, "POST", "/mac-event", {"kind": "sold_found", "marketplace": "ebay", "listing_id": "407358644533",
                                    "item_id": "i_261101_46ce94", "title": "Edikted Crochet Knit Mini Dress",
                                    "price": 44, "sold_at": "2026-11-13T13:08:22+00:00"},   # as live
         now=utc(2026, 11, 13, 13, 10))
    shop.trading = [unpaid_order(ref=ref, sku="i_261101_46ce94", title="Edikted Crochet Knit Mini Dress",
                                 created="2026-11-12T23:37:18.000Z")]
    sent.clear()
    tick(db, utc(2026, 11, 15, 3, 30))                                             # Sat 10:30 PM: the deploy
    s = db.one("SELECT * FROM sales WHERE pay_ref = ?", (ref,))
    assert (s["payment"], s["cancel_from"]) == ("unpaid", iso(committed + 5 * DAY))   # Tue Nov 17, 6:37 PM
    assert s["sold_at"] == iso(committed)                          # eBay's purchase, not the Mac's look 13 h later
    assert texts(sent, "group") == []                                              # announced on Nov 12: no new line
    for at in (utc(2026, 11, 15, 13, 0), utc(2026, 11, 15, 18, 55)):              # Sun 8:00 AM, 1:55 PM
        tick(db, at)
    assert shop.invoices == []
    tick(db, utc(2026, 11, 15, 19, 0))                                             # Sun 2:00 PM
    assert [i["text"] for i in shop.invoices] == [
        ("Hi! Your order for Edikted Crochet Knit Mini Dress is still unpaid. If payment isn't received by Tuesday, "
         "November 17 at 6:37 PM ET, the order will be cancelled. Thanks!")]
    for at in (utc(2026, 11, 15, 22, 0), utc(2026, 11, 16, 19, 0)):
        tick(db, at)
    assert len(shop.invoices) == 1
