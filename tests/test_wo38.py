"""WO38 — the sites' sold orders, read only (2026-10-14). §1's diagnostic: what leaves an order page is scrubbed of buyer
data (names, addresses, tracking numbers, e-mails, messages) and keeps what ties an order to our sale (ids, status
words, titles, prices, dates)."""
from __future__ import annotations

import json

from thrift_agent import orders

BUYER = ("Jane Doe", "janedoe", "12 Main St", "9400111899223344556677", "jane@example.com", "hi, Jane here")


def poshmark_order() -> dict:
    """The shape of Poshmark's $_order_details.order (harvest.py's fields), with a buyer in it."""
    return {"id": "6ac431effa146f6457260492", "title": "New Naked Wardrobe Leopard Print Stirrup Catsuit",
            "display_status": "Delivered", "inventory_booked_at": "2026-10-05T23:25:41Z",
            "total_price_amount": {"val": "50.00", "currency_code": "USD"},
            "buyer": {"username": "janedoe", "display_handle": "janedoe"}, "buyer_display_handle": "janedoe",
            "shipping_address": {"name": "Jane Doe", "street": "12 Main St"},
            "tracking_number": "9400111899223344556677", "buyerEmail": "jane@example.com",
            "line_items": [{"product_url": "/listing/New-Naked-Wardrobe-6ac0", "title": "Catsuit", "size": "M",
                            "picture_url": "https://cdn/x.jpg"}],
            "order_rating": {"rating": 5, "comment": "hi, Jane here"}, "is_buyer_rated": True,
            "status_changed_at": "2026-10-10T12:00:00Z", "shipping_label_url": "https://x/label.pdf",
            "label_status": "printed", "notification_type": "order_shipped"}


def test_scrub_keeps_what_ties_an_order_and_nothing_that_names_a_buyer():
    out = orders.scrub(poshmark_order())
    assert out["id"] == "6ac431effa146f6457260492"
    assert out["display_status"] == "Delivered"
    assert out["title"].startswith("New Naked Wardrobe")
    assert out["total_price_amount"]["val"] == "50.00"
    assert out["inventory_booked_at"] == "2026-10-05T23:25:41Z"
    assert out["line_items"][0]["product_url"] == "/listing/New-Naked-Wardrobe-6ac0"
    assert out["status_changed_at"] == "2026-10-10T12:00:00Z"
    assert out["label_status"] == "printed"                 # a label's state is a status; its file is not kept
    assert out["notification_type"] == "order_shipped"
    assert out["is_buyer_rated"] is True                     # a flag names no one
    for key in ("buyer", "buyer_display_handle", "shipping_address", "tracking_number", "buyerEmail",
                "shipping_label_url"):
        assert out[key] == orders.SCRUBBED, key
    assert out["order_rating"]["comment"] == orders.SCRUBBED
    assert out["line_items"][0]["picture_url"] == orders.SCRUBBED
    text = json.dumps(out)
    for secret in BUYER:
        assert secret not in text, secret


def test_scrub_masks_a_handle_or_an_email_in_a_kept_string_and_cuts_long_lists():
    out = orders.scrub({"title": "Sold to @janedoe (jane@example.com)", "items": list(range(20)), "code": "1" * 22})
    assert out["title"] == "Sold to @<user> (<email>)"
    assert out["items"][-1] == "<12 more>" and len(out["items"]) == 9
    assert out["code"] == "<long number>"


def test_a_string_under_an_unknown_key_is_never_kept():
    assert orders.scrub({"headline": "Jane Doe bought your item", "subtitle": "12 Main St"}) == \
        {"headline": "<str>", "subtitle": "<str>"}


def test_status_texts_keep_order_words_and_never_a_name_or_a_tracking_number():
    texts = ["Awaiting shipment", "Jane Doe", "Order complete", "Ship by Oct 14", "$45.00", "Mark as shipped",
             "Sold to Jane", "Tracking 9400111899223344556677", "Delivered", "Delivered", "In transit",
             "A sentence much longer than forty characters about the order"]
    assert orders.status_texts(texts) == ["Awaiting shipment", "Order complete", "Ship by Oct 14", "Mark as shipped",
                                          "Delivered", "In transit"]


def test_probe_line_says_what_was_read_or_why_not():
    assert orders.probe_line({"site": "depop", "event": "error", "page": "login", "message": "login page"}) == \
        "✗ login login page"
    line = orders.probe_line({"site": "vinted", "resources": [{"url": "a", "json": {}}, {"url": "b", "read": False}],
                              "status_texts": ["Completed"]})
    assert line == "2 requests (1 JSON read); statuses: Completed"
    line = orders.probe_line({"site": "poshmark", "pages": {"sales": {"rows": [1, 2], "status_texts": ["Sold"]}},
                              "orders": [{}]})
    assert line == "2 order rows, 1 order pages read; statuses: Sold"


def test_the_extension_reads_orders_in_their_own_window_and_never_pictures_them():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "ext"
    bg = (root / "background.js").read_text(encoding="utf-8")
    assert 'siteWindow(job.mode === "orders" ? "orders" : job.site)' in bg
    assert 'if (!j || j.mode === "orders") return;' in bg            # no capture of an order page, ever
    common = (root / "content" / "common.js").read_text(encoding="utf-8")
    assert "if (T.noShots) return;" in common
    assert 'if (job.mode === "orders") return T.orders(job, st);' in common
