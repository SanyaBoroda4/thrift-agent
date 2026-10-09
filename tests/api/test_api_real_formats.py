"""WO33: the parsers against the shapes of the seller's real marketplace mail (165 samples, scrubbed, in the private
repo — these are made-up copies of each shape: no real title, id, name or address). Each kind × each site, the traps
("a listing that you liked just sold" is someone else's sale), and the sale's life through the API: Vinted's label
states the ship-by date, its "This order is completed" closes the sale, Depop's delivered email (it names only the
buyer) closes the one open Depop sale."""
from datetime import datetime, timezone

import pytest

from thrift_api import core, emails

from apitools import NOW, sale, seed, texts

AT = datetime(2026, 9, 29, 15, 51, tzinfo=timezone.utc)

POSH_SALE = ('"Kids Sneakers Black size 12" just sold to @buyer1 on Poshmark!',
             "Hi Ann! Great news - you just sold \"Kids Sneakers Black size 12\" on Poshmark.\n"
             "Poshmark expects our sellers to ship their sales within 2 days.\nBuyer\n[buyer]\n@[user]\n\n"
             "Order Date\nSeptember 26, 2026\n\nOrder ID 6ab7cb1fff8a471e6bfa36ed\nTracking Number [tracking]\n\n"
             "Kids Sneakers Black size 12\nSize: 12 (Toddler Boy)\nPrice: $22.00\n$22.00\n\n"
             "Your Earnings (minus fee and taxes) $17.60")
POSH_SHIPPED = ("Thank you for shipping Kids Sneakers Black size 12",
                "Order Number\n6ab7cb1fff8a471e6bfa36ed\nHi Ann,\nThanks for shipping your sale Kids Sneakers Black size "
                "12. Track your order's progress below.")
POSH_CANCELLED = ('Please do not ship: "Navy Shirt Dress size M" for @buyer3 was canceled',
                  "Re: Order Id 6a9f55097c2b6abba857e4a6\nHi Ann,\nWe wanted to let you know that order # "
                  "6a9f55097c2b6abba857e4a6 for \"Navy Shirt Dress size M\" was canceled.")
POSH_LIKED_SOLD = ('A listing that you liked just sold: "Linen Maxi Skirt"', "Someone else's listing you liked sold.")
POSH_REMINDER = ('Reminder to ship your Poshmark order: "Kids Sneakers Black size 12"',
                 "Re: Order # 6ab7cb1fff8a471e6bfa36ed\nCongratulations again on your sale ...")
POSH_EARNINGS = ('Congrats! Your earnings from "Kids Sneakers Black size 12" for @buyer1 have been deposited', "...")
VINTED_SALE = ("You sold an item on Vinted",
               "Hello Ann,\nbuyer9 has bought\n\nKai Run Shoes size 8\n$6.40\n\nWe will transfer the buyer's payment to "
               "your Vinted Wallet once the order is completed.\n\nPlease send this order within 5 days.")
VINTED_LABEL = ("Kai Run Shoes size 8 shipping label – use by 10/06/2026 09:51 AM",
                "Hello Ann,\nYour shipping label is attached to this message.\nShipment Information\n"
                "Item name: Kai Run Shoes size 8\nPackage size: Under 500.0 g\nTracking code: [tracking]\n"
                "Shipment deadline: 10/06/2026 09:51 AM\nTransaction ID: 22703814298")
VINTED_COMPLETED = ("This order is completed",
                    "[shop], your sale is complete.\nYour sale of Kai Run Shoes size 8 was completed successfully.\n"
                    "Transaction ID: #22703814298\nItem price: $6.40")
DEPOP_SALE = ("Your USPS shipping label and sale confirmation for @buyer2",
              "You've made a sale!\nMost sellers ship in 3 days.\nOrder details\nimage\n"
              "Madewell Italian yarn striped sweater in blue,...\n Size:\nS\n\n£30.00\n Ship to\n[address]\n")
DEPOP_DELIVERED = ("Your sale to @buyer2 was delivered", "Your sale was delivered\nPlease check tracking for more details.")
DEPOP_OFFER = ("@buyer4 made you an offer", "...")


@pytest.mark.parametrize("site,mail,kind", [
    ("poshmark", POSH_SALE, "SALE"), ("poshmark", POSH_SHIPPED, "SHIPPED"), ("poshmark", POSH_CANCELLED, "CANCELLED"),
    ("poshmark", POSH_LIKED_SOLD, "OTHER"), ("poshmark", POSH_REMINDER, "OTHER"), ("poshmark", POSH_EARNINGS, "OTHER"),
    ("vinted", VINTED_SALE, "SALE"), ("vinted", VINTED_LABEL, "SALE"), ("vinted", VINTED_COMPLETED, "DELIVERED"),
    ("depop", DEPOP_SALE, "SALE"), ("depop", DEPOP_DELIVERED, "DELIVERED"), ("depop", DEPOP_OFFER, "OTHER"),
])
def test_every_real_shape_is_its_kind(site, mail, kind):
    assert emails.classify(site, *mail) == kind


def test_the_facts_of_each_real_shape():
    p = emails.parse("poshmark", "SALE", *POSH_SALE, AT)
    assert (p["title"], p["price"], p["order_id"]) == ("Kids Sneakers Black size 12", 22.0, "6ab7cb1fff8a471e6bfa36ed")
    assert emails.parse("poshmark", "SHIPPED", *POSH_SHIPPED, AT)["order_id"] == "6ab7cb1fff8a471e6bfa36ed"
    c = emails.parse("poshmark", "CANCELLED", *POSH_CANCELLED, AT)
    assert (c["order_id"], c["title"]) == ("6a9f55097c2b6abba857e4a6", "Navy Shirt Dress size M")
    v = emails.parse("vinted", "SALE", *VINTED_SALE, AT)
    assert (v["title"], v["price"], v["ship_by_stated"]) == ("Kai Run Shoes size 8", 6.4, None)
    label = emails.parse("vinted", "SALE", *VINTED_LABEL, AT)
    assert (label["title"], label["order_id"], label["ship_by_stated"].isoformat()) == (
        "Kai Run Shoes size 8", "22703814298", "2026-10-06")
    done = emails.parse("vinted", "DELIVERED", *VINTED_COMPLETED, AT)
    assert (done["title"], done["order_id"]) == ("Kai Run Shoes size 8", "22703814298")
    d = emails.parse("depop", "SALE", *DEPOP_SALE, AT)
    assert d["title"].startswith("Madewell Italian yarn striped sweater") and d["price"] == 30.0
    assert not any(emails.parse("depop", "DELIVERED", *DEPOP_DELIVERED, AT).values())


def mail(site: str, shape: tuple[str, str], message_id: str, date: str) -> dict:
    sender = {"poshmark": "Poshmark <noreply@poshmark.com>", "depop": "Depop <hello@mail.depop.com>",
              "vinted": "Vinted <no-reply@vinted.com>"}[site]
    return {"message_id": message_id, "thread_id": f"t-{message_id}", "from": sender, "subject": shape[0],
            "date": date, "text": shape[1]}


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-09-01T00:00:00+00:00")


def test_a_vinted_sale_its_label_and_its_completion_are_one_sale(db, live, sent):
    seed(db, item_id="i_260929_aaaaaa", title="Kai Run Shoes size 8", sites=("vinted",), price=6.4,
         urls={"vinted": "https://www.vinted.com/items/7000000001-kai-run"}, ids={"vinted": "7000000001"})
    first = core.process_email(db, mail("vinted", VINTED_SALE, "v1", "2026-09-29T15:51:27Z"), NOW)
    assert first["status"] == "matched" and first["item_id"] == "i_260929_aaaaaa"
    core.process_email(db, mail("vinted", VINTED_LABEL, "v2", "2026-09-29T15:54:27Z"), NOW)
    s = sale(db, first["sale_id"])
    assert (s["ship_by"], s["ship_by_source"], s["order_id"]) == ("2026-10-06", "email", "22703814298")
    assert len([t for t in texts(sent, "group") if t.startswith("💰")]) == 1        # the label is no second sale
    core.process_email(db, mail("vinted", VINTED_COMPLETED, "v3", "2026-09-30T18:37:20Z"), NOW)
    assert sale(db, first["sale_id"])["status"] == "done"


def test_depops_delivered_email_closes_the_one_open_depop_sale(db, live, sent):
    seed(db, item_id="i_260923_bbbbbb", title="Madewell Italian yarn striped sweater in blue S", sites=("depop",),
         price=30, urls={"depop": "https://www.depop.com/products/shop-madewell-sweater-1a2b/"},
         ids={"depop": "shop-madewell-sweater-1a2b"})
    first = core.process_email(db, mail("depop", DEPOP_SALE, "d1", "2026-09-23T21:33:00Z"), NOW)
    assert first["item_id"] == "i_260923_bbbbbb"
    core.process_email(db, mail("depop", DEPOP_DELIVERED, "d2", "2026-10-03T23:41:34Z"), NOW)
    assert sale(db, first["sale_id"])["status"] == "done"


def test_the_replay_runs_the_samples_through_in_replay_mode_and_sums_them_up(db, sent, monkeypatch):
    """WO33 Part I.6: the 90 days of samples in REPLAY mode — every message to the ops chat as "[replay]", none to the
    group, no take-downs — oldest first, then one summary; a second run changes nothing."""
    monkeypatch.setenv("SALES_MODE", "replay")
    seed(db, item_id="i_260929_aaaaaa", title="Kai Run Shoes size 8", sites=("vinted", "poshmark"), price=6.4,
         urls={"vinted": "https://www.vinted.com/items/7000000001-kai-run",
               "poshmark": "https://poshmark.com/listing/Kai-Run-Shoes-size-8-6ab7cb1fff8a471e6bfa36aa"},
         ids={"vinted": "7000000001", "poshmark": "6ab7cb1fff8a471e6bfa36aa"})
    for mid, site, shape, date in (("s1", "vinted", VINTED_SALE, "2026-09-29T15:51:27+00:00"),
                                   ("s2", "vinted", VINTED_LABEL, "2026-09-29T15:54:27+00:00"),
                                   ("s3", "vinted", VINTED_COMPLETED, "2026-09-30T18:37:20+00:00"),
                                   ("s4", "poshmark", POSH_SALE, "2026-09-26T13:39:48+00:00"),
                                   ("s5", "poshmark", POSH_LIKED_SOLD, "2026-09-27T10:00:00+00:00")):
        db.execute("INSERT INTO samples (message_id, marketplace, subject, received_at, text) VALUES (?, ?, ?, ?, ?)",
                   (mid, site, shape[0], date, shape[1]))
    counts = core.replay(db, NOW)
    assert (counts["sales"], counts["matched"], counts["unmatched"], counts["delivered"], counts["other"]) == (2, 1, 1, 1, 1)
    assert not texts(sent, "group")                                       # replay: nothing to the group
    ops = texts(sent, "ops")
    assert all(t.startswith("[replay] ") for t in ops)
    assert any("💰 Sold on Vinted: Kai Run Shoes size 8" in t for t in ops)
    assert ops[-1].startswith("[replay] Replay of 5 emails (90 days): 2 sales — 1 matched")
    assert not db.query("SELECT * FROM delist_tasks")                      # never a take-down in replay
    before = len(sent)
    again = core.replay(db, NOW)
    assert again["duplicates"] == 5 and len(sent) == before + 1           # only the summary again


def test_the_replay_refills_an_email_the_live_poll_stored_without_its_text(db, sent, monkeypatch):
    """The reader's bytes bug: the live poll stored a Vinted sale email with empty text (failed); its sample has the
    text — the replay puts it in and processes it once."""
    monkeypatch.setenv("SALES_MODE", "replay")
    seed(db, item_id="i_260929_aaaaaa", title="Kai Run Shoes size 8", sites=("vinted",), price=6.4,
         urls={"vinted": "https://www.vinted.com/items/7000000001-kai-run"}, ids={"vinted": "7000000001"})
    stored = core.process_email(db, mail("vinted", (VINTED_SALE[0], ""), "s1", "2026-09-29T15:51:27Z"), NOW)
    assert stored["status"] == "failed"                                    # a sale by its subject, no text to parse
    db.execute("INSERT INTO samples (message_id, marketplace, subject, received_at, text) VALUES (?, ?, ?, ?, ?)",
               ("s1", "vinted", VINTED_SALE[0], "2026-09-29T15:51:27+00:00", VINTED_SALE[1]))
    counts = core.replay(db, NOW)
    assert (counts["refilled"], counts["sales"], counts["matched"]) == (1, 1, 1)
    assert db.one("SELECT status FROM email_events WHERE message_id = 's1'")["status"] != "failed"
