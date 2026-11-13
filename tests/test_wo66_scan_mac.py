"""WO66 on the Mac (2026-11-13: she shipped 10 parcels at 4–5 PM; the sites flip a sale to "Shipped" only at the first
USPS scan): Poshmark's own order page tells her label from its own — "seller_shipped" with "Shipping Label Created"
is a parcel waiting for its scan (`awaiting_scan`), the label Poshmark makes at every sale is not; the 8:50 PM look
for a site with a parcel still open; her "shipped 1 3" as a reply to the list thrift-api sent."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from thrift_agent import approve, orders
from thrift_agent.db import DB

UTC = timezone.utc


def est(y, m, d, hh, mm=0) -> datetime:
    """A New York wall-clock time in November (EST, UTC−5) as UTC."""
    return datetime(y, m, d, hh, mm, tzinfo=UTC) + timedelta(hours=5)


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "state.db")


@pytest.fixture
def quiet(monkeypatch):
    said, posted = [], []
    from thrift_agent import notify, sales
    monkeypatch.setattr(notify, "say", lambda text: said.append(text))
    monkeypatch.setattr(sales, "call", lambda db, path, body: posted.append((path, body)) or True)
    monkeypatch.setattr(orders.power, "lid_closed", lambda: False)
    return SimpleNamespace(said=said, posted=posted)


# Today's own order pages (scrubbed), as Poshmark drew them at 5:39 PM
LOFT = {"id": "6aee36bb1ae90084d3cffaa3", "display_status": "Shipped",              # her QR label, made 4:39 PM
        "inventory_units": [{"status": "seller_shipped", "shipment_status": "Shipping Label Created"}],
        "shipping_label_info": {"shipping_label_type": "qc", "qr_code_label": {"id": "6af783aefba3bf5b626c2db8"}},
        "line_items": [{"sku": "i_261101_620b94", "product_id": "p1"}]}
ZARA = {"id": "6af6290ec840d3fe3b61eccb", "display_status": "Sold",                 # the label Poshmark made at the sale
        "inventory_units": [{"status": "sold", "shipment_status": "Shipping Label Created"}],
        "shipping_label_info": {"shipping_label_type": "qc", "qr_code_label": {"id": "6af629191af6c3a336f7717e"}},
        "line_items": [{"sku": "i_261018_023b7e", "product_id": "p2"}]}
PLATES = {"id": "6af08c55d05747be5dc84560", "display_status": "In Transit",          # scanned at 5:00 PM
          "inventory_units": [{"status": "shipped", "shipment_status": "USPS in possession of item",
                               "shipped_on": "2026-11-13T14:00:12-08:00"}],
          "shipping_label_info": {"shipping_label_type": "qc"}, "line_items": [{"product_id": "p3"}]}


def test_her_label_with_no_scan_is_awaiting_scan_and_poshmarks_own_label_is_not():
    loft = orders.poshmark_detail(LOFT)
    assert loft["unit_status"] == "awaiting_scan"
    assert loft["label_at"] == "2026-11-13T21:39:58+00:00"                          # 4:39 PM New York, from the QR id
    zara = orders.poshmark_detail(ZARA)
    assert zara["unit_status"] is None and zara["label_at"] is None                  # "sold": still hers to ship
    plates = orders.poshmark_detail(PLATES)
    assert plates["unit_status"] == "shipped" and plates["label_at"] is None
    assert orders.label_time({"shipping_label_info": {"qr_code_label": {"id": "not-an-id"}}}) is None
    assert orders.furthest("to_ship", "awaiting_scan") == "awaiting_scan"
    assert orders.furthest("awaiting_scan", "shipped") == "shipped"


class Resp:
    def __init__(self, url, data):
        self.url, self._data, self.headers = url, data, {"content-type": "application/json"}

    async def json(self):
        return self._data


class Page:
    def __init__(self, rows, details):
        self.rows, self.details, self.handlers, self.fetched, self.url = rows, details, [], [], ""

    def on(self, event, fn):
        self.handlers.append(fn)

    async def goto(self, url, wait_until=None):
        self.url = "https://poshmark.com/seller-hub/sales"
        for fn in self.handlers:
            fn(Resp("https://poshmark.com/vm-rest/users/u1/orders/sales?request=x", {"data": {"sales_summary": self.rows}}))

    async def content(self):
        return "<script>window.__INITIAL_STATE__ = {}</script>"

    async def evaluate(self, js, url):
        self.fetched.append(url)
        order = self.details.get(url.rsplit("/", 1)[-1], {})
        return f"<script>__INITIAL_STATE__ = {json.dumps({'$_order_details': {'order': order}})}</script>"

    async def close(self):
        pass


class Ctx:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page


def row(order: dict, display: str, n: int) -> dict:
    return {"id": order["id"], "title": f"item {n}", "display_status": display,
            "inventory_booked_at": "2026-11-12T13:01:09-08:00", "total_price_amount": {"val": "35.0"}}


def test_the_sweep_reads_the_shipped_ones_pages_too_and_sends_her_label_as_awaiting_scan(monkeypatch):
    monkeypatch.setattr(orders, "_recent", lambda o, now=None: True)
    monkeypatch.setattr(orders, "MORE_ANSWERS", 0)
    many = [dict(ZARA, id=f"6af6290ec840d3fe3b61ec{n:02x}") for n in range(5)]       # five sold, not yet sent
    rows = [row(o, "Sold", n) for n, o in enumerate(many)] + [row(PLATES, "In Transit", 8), row(LOFT, "Shipped", 9)]
    page = Page(rows, {o["id"]: o for o in [*many, PLATES, LOFT]})
    got = {o["order_id"]: o for o in asyncio.run(orders.read_poshmark(Ctx(page)))}
    assert f"/order/sales/{LOFT['id']}" in page.fetched                              # past the five to ship (was: 4)
    assert got[LOFT["id"]]["status"] == "awaiting_scan"
    assert got[LOFT["id"]]["label_at"] == "2026-11-13T21:39:58+00:00"
    assert got[PLATES["id"]]["status"] == "shipped"
    assert {got[o["id"]]["status"] for o in many} == {"to_ship"}                     # Poshmark's own label: still to ship


def test_the_sweep_sends_label_at_and_remembers_a_site_with_parcels_open(db, quiet):
    found = orders.Found([
        {"order_id": "a", "status": "awaiting_scan", "status_text": "Shipped", "label_at": "2026-11-13T21:39:58+00:00",
         "sold_at": "2026-11-12T13:01:09Z"},
        {"order_id": "b", "status": "to_ship", "status_text": "Sold", "sold_at": "2026-11-12T13:01:09Z"},
        {"order_id": "c", "status": "shipped", "status_text": "In Transit", "sold_at": "2026-11-12T13:01:09Z"}])

    async def read():
        return found
    r = asyncio.run(orders.sweep(None, db, "poshmark", read, now=est(2026, 11, 13, 20, 30)))
    assert r["statuses"] == {"awaiting_scan": 1, "to_ship": 1, "shipped": 1}
    sent = quiet.posted[0][1]["orders"]
    assert sent[0]["label_at"] == "2026-11-13T21:39:58+00:00" and sent[1]["label_at"] is None
    assert orders._kv(db, orders.KV_OPEN) == {"poshmark": "2"}

    async def none_open():
        return orders.Found([{"order_id": "c", "status": "shipped", "status_text": "In Transit"}])
    asyncio.run(orders.sweep(None, db, "poshmark", none_open, now=est(2026, 11, 13, 20, 51)))
    assert orders._kv(db, orders.KV_OPEN) == {}                                       # all on their way: no 8:50 look


def test_one_more_look_at_8_50_pm_only_for_a_site_with_a_parcel_open(db, quiet, monkeypatch):
    orders._put(db, orders.KV_SWEPT, "poshmark", est(2026, 11, 13, 20, 31).isoformat())   # the 8:30 PM sweep
    orders._put(db, orders.KV_SWEPT, "vinted", est(2026, 11, 13, 20, 32).isoformat())
    orders._put(db, orders.KV_OPEN, "poshmark", "4")                                       # Vinted had none open
    assert not orders.due(db, "poshmark", est(2026, 11, 13, 20, 49))
    assert orders.due(db, "poshmark", est(2026, 11, 13, 20, 50))
    assert not orders.due(db, "vinted", est(2026, 11, 13, 20, 50))
    orders._put(db, orders.KV_SWEPT, "poshmark", est(2026, 11, 13, 20, 51).isoformat())
    assert not orders.due(db, "poshmark", est(2026, 11, 13, 20, 58))                       # once
    orders._put(db, orders.KV_SWEPT, "poshmark", est(2026, 11, 13, 20, 31).isoformat())
    monkeypatch.setattr(orders.power, "lid_closed", lambda: True)
    assert not orders.due(db, "poshmark", est(2026, 11, 13, 20, 55))                       # never with the lid closed


class Bot:
    def __init__(self):
        self.sent = []

    def send_message(self, text, buttons=None, reply_to=None, **kw):
        self.sent.append(text)
        return 1


def reply_to_the_list(text: str) -> dict:
    """Her reply to the 9 PM list — a message thrift-api sent, so not in the Mac's outbox."""
    return {"message_id": 77, "chat": {"id": -100}, "text": text, "reply_to_message": {"message_id": 999}}


@pytest.mark.parametrize("typed, words", [("shipped 1 3", "1 3"), ("Shipped all", "all"),
                                          ("shipped hunter boots", "hunter boots")])
def test_her_shipped_reply_to_the_list_reaches_the_api(db, monkeypatch, typed, words):
    from thrift_agent import sales
    posted = []

    class Api:
        def post(self, path, body):
            posted.append((path, body))
            return {"status": "shipped", "matched": ["s_1", "s_3"], "titles": ["A", "C"]}
    monkeypatch.setattr(sales, "api", lambda: Api())
    bot = Bot()
    answer = approve._route_message(None, db, bot, reply_to_the_list(typed))
    assert posted == [("/mac-event", {"kind": "shipped", "words": words})]
    assert answer.startswith("shipped: ") and bot.sent == []                # the API says "✓ Marked shipped: …"


def test_a_number_not_on_the_list_gets_the_apis_one_line(db, monkeypatch):
    from thrift_agent import sales

    class Api:
        def post(self, path, body):
            return {"status": "no_match", "matched": None, "say": "No 7 on tonight's list (it had 1–5) — nothing marked."}
    monkeypatch.setattr(sales, "api", lambda: Api())
    bot = Bot()
    approve._route_message(None, db, bot, reply_to_the_list("shipped 7"))
    assert bot.sent == ["No 7 on tonight's list (it had 1–5) — nothing marked."]


def test_any_other_reply_to_a_message_the_mac_didnt_send_is_still_ignored(db, monkeypatch):
    from thrift_agent import sales
    monkeypatch.setattr(sales, "api", lambda: pytest.fail("nothing goes to the API"))
    assert approve._route_message(None, db, Bot(), reply_to_the_list("28")) == "ignored: not a reply to the bot"
    assert approve._route_message(None, db, Bot(), reply_to_the_list("thanks!")) == "ignored: not a reply to the bot"
