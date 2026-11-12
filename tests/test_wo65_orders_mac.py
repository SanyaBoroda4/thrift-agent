"""WO65 on the Mac (live 2026-11-11: the 9 PM list showed shipped and cancelled sales as still to ship): the status
sentences the sweep didn't know (said once, then every sweep dropped those orders silently), Depop's refund before
shipping, the 8:30 PM sweep before the 9 PM list, her "cancelled <words>"."""
from __future__ import annotations

import asyncio
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


@pytest.mark.parametrize("site, words, ours", [
    ("vinted", "Payment successful!", "to_ship"),                       # live 2026-11-11: "Book about basketball"
    ("vinted", "Shipping label sent to seller.", "to_ship"),            # live 2026-11-11: "Little blue truck"
    ("vinted", "Order completed successfully. The buyer accepted the item.", "completed"),   # "Unicorn head"
    ("depop", "refunded / AWAITING_SHIPPING", "cancelled"),             # live: Seven Wonders, the app says "Canceled"
    ("depop", "refunded / DELIVERED", "returned"),                      # a refund after it arrived: a return
    ("depop", "refunded / IN_TRANSIT", "returned"),
    ("depop", "shipped / IN_TRANSIT", "shipped"),                       # live: Jen's maxi
    ("depop", "shipping_pending / AWAITING_SHIPPING", "to_ship"),       # live: Southern Tide
])
def test_tonights_status_sentences(site, words, ours):
    assert orders.status_of(site, words) == ours


def test_the_sweep_sends_tonights_vinted_sales_it_used_to_drop(db, quiet):
    found = orders.Found([
        {"order_id": "24141255168", "status_text": "Payment successful!", "title": "Book about basketball",
         "sold_at": "2026-11-11T13:01:00Z"},
        {"order_id": "24142000000", "status_text": "Shipping label sent to seller.", "title": "Little blue truck",
         "sold_at": "2026-11-11T14:19:47Z"}])
    for o in found:
        o["status"] = orders.status_of("vinted", o["status_text"])

    async def read():
        return found
    r = asyncio.run(orders.sweep(None, db, "vinted", read, now=est(2026, 11, 11, 20, 30)))
    assert r["status"] == "swept" and r["orders"] == 2 and r["statuses"] == {"to_ship": 2}
    assert [o["order_id"] for o in quiet.posted[0][1]["orders"]] == ["24141255168", "24142000000"]
    assert quiet.said == []                                              # nothing unknown any more


def test_an_unknown_status_word_is_said_once_privately_and_that_order_left_as_it_is(db, quiet):
    found = orders.Found([{"order_id": "1", "status": None, "status_text": "Something new from Vinted",
                           "title": "x", "sold_at": "2026-11-11T13:01:00Z"}])

    async def read():
        return found
    r = asyncio.run(orders.sweep(None, db, "vinted", read, now=est(2026, 11, 11, 20, 30)))
    assert r["orders"] == 0 and quiet.posted[0][1]["orders"] == []
    assert len(quiet.said) == 1 and "Something new from Vinted" in quiet.said[0]


def test_one_more_sweep_at_8_30_pm_once_when_the_lid_is_open(db, quiet, monkeypatch):
    orders._put(db, orders.KV_SWEPT, "vinted", est(2026, 11, 11, 19, 1).isoformat())   # the 7:01 PM sweep
    assert not orders.due(db, "vinted", est(2026, 11, 11, 20, 29))
    assert orders.due(db, "vinted", est(2026, 11, 11, 20, 30))                          # 8:30 PM
    orders._put(db, orders.KV_SWEPT, "vinted", est(2026, 11, 11, 20, 31).isoformat())
    assert not orders.due(db, "vinted", est(2026, 11, 11, 20, 45))                      # once
    assert not orders.due(db, "vinted", est(2026, 11, 11, 23, 59))
    orders._put(db, orders.KV_SWEPT, "vinted", est(2026, 11, 11, 19, 1).isoformat())
    monkeypatch.setattr(orders.power, "lid_closed", lambda: True)
    assert not orders.due(db, "vinted", est(2026, 11, 11, 20, 35))                      # never with the lid closed


class Bot:
    def __init__(self):
        self.sent = []

    def send_message(self, text, reply_to=None, **kw):
        self.sent.append(text)
        return 1


@pytest.mark.parametrize("typed, kind, words", [("cancelled seven wonders", "cancelled", "seven wonders"),
                                                ("Canceled Seven Wonders", "cancelled", "Seven Wonders"),
                                                ("shipped unicorn", "shipped", "unicorn")])
def test_her_shipped_and_cancelled_words_go_to_the_api(db, monkeypatch, typed, kind, words):
    from thrift_agent import sales
    posted = []

    class Api:
        def post(self, path, body):
            posted.append((path, body))
            return {"matched": "s_1", "status": body["kind"]}
    monkeypatch.setattr(sales, "api", lambda: Api())
    answer = approve._typed(None, db, Bot(), typed, 5)
    assert posted == [("/mac-event", {"kind": kind, "words": words})] and answer == f"{kind}: s_1"
