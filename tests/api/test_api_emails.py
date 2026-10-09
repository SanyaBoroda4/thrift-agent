"""thrift_api.emails (WO33): the sender's marketplace, the kind of email by each site's table (offers, likes, payouts,
promotions, reminders… are OTHER), the facts of a sale or a follow-up — and never a buyer's name or address."""
import json
from datetime import date

import pytest
from apitools import BUYER, IDS, ITEM, TITLE, URLS, depop_sale, posh_sale, utc, vinted_sale

from thrift_api import emails
from thrift_api.emails import ParseError, classify, marketplace_of, parse

RECEIVED = utc(2026, 10, 8, 14, 5)                     # Thu Oct 8, 10:05 in New York


@pytest.mark.parametrize("sender, site", [
    ("Poshmark <noreply@poshmark.com>", "poshmark"),
    ("noreply@poshmark.com", "poshmark"),
    ('"Poshmark" <orders@mail.poshmark.com>', "poshmark"),
    ("Depop <hello@depop.com.>", "depop"),
    ("Vinted <no-reply@VINTED.com>", "vinted"),
    ("Team (Vinted) <news@e.vinted.com>", "vinted"),
    ("Poshmark <noreply@poshmark.com.evil.io>", None),       # a look-alike domain
    ("noreply@notposhmark.com", None),
    ("poshmark.com <x@gmail.com>", None),                     # the display name never counts
    ("", None), (None, None), ("no address at all", None),
])
def test_marketplace_of(sender, site):
    assert marketplace_of(sender) == site


@pytest.mark.parametrize("site, subject, text, kind", [
    # sales
    ("poshmark", "Congratulations! Your item has sold", "", "SALE"),
    ("poshmark", "You made a sale!", "", "SALE"),
    ("poshmark", "Congrats — your listing sold", "", "SALE"),
    ("depop", "You sold an item!", "", "SALE"),
    ("depop", "Your item sold", "", "SALE"),
    ("depop", "janeq_closet bought your item", "", "SALE"),
    ("vinted", "Your item has been sold", "", "SALE"),
    ("vinted", "You’ve made a sale", "", "SALE"),                 # a curly apostrophe
    ("vinted", "Offer accepted — you made a sale!", "", "SALE"),  # an offer that became a sale
    ("poshmark", "Order update", "Congrats, you made a sale! Item: Tee", "SALE"),          # the body decides
    # follow-ups
    ("poshmark", "Your shipment has been scanned", "", "SHIPPED"),
    ("poshmark", "Your order is in transit", "", "SHIPPED"),
    ("depop", "Your item was marked as shipped", "", "SHIPPED"),
    ("vinted", "Your parcel is on its way", "", "SHIPPED"),
    ("poshmark", "Order update", "The label was scanned by USPS today.", "SHIPPED"),
    ("poshmark", "Your order was delivered", "", "DELIVERED"),
    ("depop", "Delivered: your sold item", "", "DELIVERED"),
    ("vinted", "Order update", "The parcel has been delivered.", "DELIVERED"),
    ("poshmark", "Your order was cancelled", "", "CANCELLED"),
    ("depop", "Order canceled", "", "CANCELLED"),
    ("vinted", "Transaction cancelled", "", "CANCELLED"),
    ("vinted", "Order update", "Your order has been cancelled and refunded.", "CANCELLED"),
    # everything else
    ("poshmark", "You received an offer on Lacoste Tee", "", "OTHER"),
    ("poshmark", "jane liked your listing", "", "OTHER"),
    ("poshmark", "An item you liked has sold", "", "OTHER"),
    ("poshmark", "jane shared your listing", "", "OTHER"),
    ("depop", "New follower", "", "OTHER"),
    ("depop", "You have a new message", "", "OTHER"),
    ("depop", "You've been paid", "", "OTHER"),
    ("vinted", "Money transferred to your balance", "", "OTHER"),
    ("poshmark", "Your earnings are ready to redeem", "", "OTHER"),
    ("poshmark", "Reminder: ship your order", "You sold an item, please ship it.", "OTHER"),
    ("vinted", "Your item hasn't been shipped yet", "", "OTHER"),
    ("poshmark", "Cancellation request for your order", "", "OTHER"),
    ("depop", "You bought an item!", "", "OTHER"),
    ("vinted", "Order confirmation", "", "OTHER"),
    ("poshmark", "20% off this weekend only", "", "OTHER"),
    ("poshmark", "Join tonight's Posh Party", "", "OTHER"),
    ("depop", "Your weekly digest", "you sold 3 items this week", "OTHER"),
    ("vinted", "Verify your email", "", "OTHER"),
    ("poshmark", "Welcome to Poshmark", "", "OTHER"),
    ("poshmark", "Hello", "Nothing to see here.", "OTHER"),
])
def test_classify(site, subject, text, kind):
    assert classify(site, subject, text) == kind


def test_classify_unknown_marketplace_is_other():
    assert classify(None, "You made a sale!", "") == "OTHER"
    assert classify("ebay", "You made a sale!", "") == "OTHER"


def test_every_site_has_its_own_table_with_its_real_subjects_then_the_guards():
    assert set(emails.RULES) == {"poshmark", "depop", "vinted"}
    for site, table in emails.RULES.items():
        real = emails.REAL[site]                # WO33: the subjects of the seller's own mail come first
        assert table[:len(real)] == real
        assert table[len(real):len(real) + len(emails.GUARDS)] == emails.GUARDS
        assert {rule.kind for rule in table} == set(emails.KINDS)
        wheres = [rule.where for rule in table]
        assert wheres == sorted(wheres, key=lambda w: w != "subject")      # every subject rule before a body one


def test_a_poshmark_sale():
    e = posh_sale()
    assert parse("poshmark", "SALE", e["subject"], e["text"], RECEIVED) == {
        "title": TITLE, "listing_url": URLS["poshmark"], "listing_id": IDS["poshmark"], "sku": None, "price": 35.0,
        "order_id": "6702bb11cc22dd33ee44ff55", "sold_at": RECEIVED, "ship_by_stated": None}


def test_a_depop_sale_with_our_sku():
    e = depop_sale()
    facts = parse("depop", "SALE", e["subject"], e["text"], RECEIVED)
    assert (facts["title"], facts["sku"], facts["listing_id"], facts["price"], facts["order_id"]) == (
        TITLE, ITEM, IDS["depop"], 35.0, "88771234")


def test_a_vinted_sale_with_a_stated_ship_by_date():
    e = vinted_sale()
    facts = parse("vinted", "SALE", e["subject"], e["text"], RECEIVED)
    assert (facts["title"], facts["listing_id"], facts["order_id"], facts["ship_by_stated"]) == (
        TITLE, IDS["vinted"], "9876543210", date(2026, 10, 13))


def test_no_buyer_data_in_the_facts():
    for site, e in (("poshmark", posh_sale()), ("depop", depop_sale()), ("vinted", vinted_sale())):
        facts = json.dumps(parse(site, "SALE", e["subject"], e["text"], RECEIVED), default=str)
        for private in (*BUYER, "Jane", "Secret", "Springfield"):
            assert private not in facts, (site, private)


@pytest.mark.parametrize("sentence, expected", [
    ("You sold Vans Slip On Sneakers for $40.00 to Jane Doe", "Vans Slip On Sneakers"),
    ("You sold Vans Slip On Sneakers to @jane_doe!", "Vans Slip On Sneakers"),
    ("You've just sold your Levi's 501 Jeans 32x30 — nice work", "Levi's 501 Jeans 32x30"),
    ("You sold “Free People Maxi Dress”", "Free People Maxi Dress"),
    ("Your item Zara Linen Shirt Blue has been sold.", "Zara Linen Shirt Blue"),
    ("Item: Lacoste Tee White si...", "Lacoste Tee White si…"),             # an ellipsis is kept
    ("You sold an item!", None),                                                # no title in it
])
def test_titles_from_sentences_stop_before_the_buyer(sentence, expected):
    assert emails.title("depop", "", sentence) == expected


def test_a_poshmark_title_from_the_listing_address():
    assert emails.title("poshmark", "You made a sale!", "See it: " + URLS["poshmark"], URLS["poshmark"]) == TITLE


def test_the_price_is_the_sale_price_not_a_fee_or_the_earnings():
    assert emails.price("Shipping: $7.97\nYou earn $28.00\nSold for $35.00") == 35.0
    assert emails.price("Shipping price: $7.97\nPrice: $1,250.00") == 1250.0
    assert emails.price("You sold it for $40 to @jane") == 40.0
    assert emails.price("Fees: $5.00\nOrder total: $42.50") == 42.5
    assert emails.price("Your payout of $28.00 is on its way") is None
    assert emails.price("No amount here") is None


def test_order_ids():
    assert emails.order_id("poshmark", "Open https://poshmark.com/order/sales/6702BB11cc22dd33ee44ff55 now") == \
        "6702bb11cc22dd33ee44ff55"
    assert emails.order_id("depop", "Order number: 88771234") == "88771234"
    assert emails.order_id("vinted", "Order #: AB-12345") == "AB-12345"
    assert emails.order_id("vinted", "Transaction ID 9876543210") == "9876543210"
    assert emails.order_id("vinted", "Your order is confirmed") is None


def test_a_listing_address_inside_a_redirect_link():
    text = "Tap https://click.poshmark.com/ls?u=https%3A%2F%2Fposhmark.com%2Flisting%2FLacoste-Tee-" + IDS["poshmark"]
    assert emails.listing_ref("poshmark", text)[1] == IDS["poshmark"]
    assert emails.listing_id_from_url("vinted", URLS["vinted"]) == IDS["vinted"]
    assert emails.listing_id_from_url("depop", URLS["depop"]) == IDS["depop"]
    assert emails.listing_id_from_url("depop", None) is None


@pytest.mark.parametrize("text, expected", [
    ("Please ship by Fri, Oct 9.", date(2026, 10, 9)),
    ("Ship by: October 13", date(2026, 10, 13)),
    ("It must be shipped before 10/12/2026", date(2026, 10, 12)),
    ("Please ship within 5 days (by Oct 14th)", date(2026, 10, 14)),
    ("Ship within 7 days with the prepaid label.", None),                     # the rule decides, not the email
    ("Please ship by Feb 30", None),                                           # no such day
    ("Please ship by Dec 1", None),                                            # too far off to be this sale's
])
def test_a_stated_ship_by_date(text, expected):
    assert emails.ship_by_stated(text, RECEIVED) == expected


def test_a_stated_date_in_january_is_next_years():
    assert emails.ship_by_stated("Please ship by Jan 4", utc(2026, 12, 30, 15, 0)) == date(2027, 1, 4)


def test_parse_errors_and_other():
    with pytest.raises(ParseError):
        parse("poshmark", "SALE", "You made a sale!", "Congrats! Ship within 7 days.", RECEIVED)
    # a follow-up with nothing to go on is no error (WO33): the API finds no sale and says so, once
    assert not any(parse("vinted", "SHIPPED", "Your parcel is on its way", "Track it in the app.", RECEIVED).values())
    assert parse("poshmark", "OTHER", "Hello", "anything", RECEIVED) == {}
    assert parse("depop", "DELIVERED", "Delivered", "Item: Lacoste Tee", RECEIVED)["title"] == "Lacoste Tee"
