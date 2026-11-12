"""Cancelled sales relisted everywhere (the owner, 2026-11-11): a sale cancelled before it shipped puts its item back
on every site it came down from, at each site's last price, one "♻️ Back on <Site>" group line each; a listing the
site restored by itself is kept, never made twice; never in the quiet hours; never twice."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from thrift_agent import crosslist, notify, relist, sales
from thrift_agent.db import DB

IID = "i_261005_d99174"
POSH = "https://poshmark.com/listing/Seven-Wonders-Fuzzy-Knit-Top-Maxi-Skirt-2Piece-Set-Brown-size-S-6ac435d77e9749ef615b2df1"
DEPOP = "https://www.depop.com/products/tetianasouokina-seven-wonders-fuzzy-knit-top-523c/"
VINTED = "https://www.vinted.com/items/10330896819"
TITLE = "Seven Wonders Fuzzy Knit Top & Maxi Skirt 2-Piece Set Brown size S"


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = DB(tmp_path / "state.db")
    bid = db.add_batch("/shares/2026-10-05_120000_0001", 4)
    iid = db.add_item(bid, 1, str(tmp_path))
    db.conn.execute("UPDATE items SET id=? WHERE id=?", (IID, iid))
    db.set_item(IID, status="posted", owner_price=90, renders={"poshmark": {"title": TITLE}})
    db.upsert_listing(IID, "poshmark", status="delisted", url=POSH, price=90, price_live=90.0,
                      posted_at="2026-10-05T23:43:01+00:00")
    db.upsert_listing(IID, "vinted", status="delisted", url=VINTED, price=90, price_live=90.0,
                      posted_at="2026-10-11T12:07:18+00:00", fields_json={"title": TITLE})
    db.upsert_listing(IID, "depop", status="sold", url=DEPOP, price=90, posted_at="2026-10-09T12:58:44+00:00",
                      fields_json={"description": TITLE + "\n\nSoft fuzzy knit."})
    said = SimpleNamespace(group=[], ops=[], api=[])
    monkeypatch.setattr(notify, "group", said.group.append)
    monkeypatch.setattr(notify, "say", said.ops.append)
    monkeypatch.setattr(sales, "call", lambda db_, path, body: said.api.append((path, body)) or True)
    monkeypatch.setattr(crosslist, "poshmark_listing", lambda url: {"for_sale": False, "status": "not_for_sale"})
    return db, said


class Posh:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    async def set_availability(self, ctx, url, available, shots=None):
        self.calls.append((url, available))
        return self.ok


class Vinted:
    def __init__(self, answer=True):
        self.answer, self.calls = answer, []

    async def relist(self, url):
        self.calls.append(url)
        return self.answer


class Depop:
    def __init__(self, state="sold"):
        self.state, self.calls = state, []

    async def depop_state(self, url):
        self.calls.append(url)
        return self.state


def run(db, site, poster):
    return asyncio.run(relist.run_one(None, db, site, poster, None, shots=Path(".")))


def test_a_cancel_before_shipping_relists_every_site_it_came_down_from_one_line_each(env):
    db, said = env
    assert relist.take(db, [{"sale_id": "s_a42901dd01a7", "item_id": IID, "marketplace": "depop",
                             "why": "cancelled"}]) == ["s_a42901dd01a7"]
    posh, vinted, depop = Posh(), Vinted(), Depop("sold")
    assert run(db, "poshmark", posh) and posh.calls == [(POSH, True)]
    assert run(db, "vinted", vinted) and vinted.calls == [VINTED]
    assert run(db, "depop", depop) and db.listing(IID, "depop")["status"] == "queued"      # posted again, new listing
    assert said.group == [f"♻️ Back on Poshmark: {TITLE} — $90\n{POSH}", f"♻️ Back on Vinted: {TITLE} — $90\n{VINTED}"]
    assert db.listing(IID, "poshmark")["status"] == "posted" and db.listing(IID, "vinted")["status"] == "posted"
    new = "https://www.depop.com/products/tetianasouokina-seven-wonders-fuzzy-knit-top-9f2e/"
    db.upsert_listing(IID, "depop", status="posted", url=new, price=90, posted_at="2026-11-12T03:30:00+00:00")
    assert run(db, "depop", depop)                                                       # its new listing is live
    assert said.group[-1] == f"♻️ Back on Depop: {TITLE} — $90\n{new}"
    assert db.listing(IID, "depop")["posted_at"] == "2026-10-09T12:58:44+00:00"           # the price-drop clock kept
    assert said.api == [("/mac-event", {"kind": "relisted", "sale_id": "s_a42901dd01a7",
                                        "sites": ["depop", "poshmark", "vinted"]})]
    for site, poster in (("poshmark", posh), ("vinted", vinted), ("depop", depop)):     # never twice
        assert not run(db, site, poster)
    assert len(said.group) == 3 and len(posh.calls) == 1 and len(vinted.calls) == 1


def test_a_listing_the_site_restored_itself_is_kept_never_made_twice(env, monkeypatch):
    db, said = env
    relist.plan(db, "s_1", IID)
    monkeypatch.setattr(crosslist, "poshmark_listing", lambda url: {"for_sale": True, "status": "available"})
    posh, vinted, depop = Posh(), Vinted("shown"), Depop("live")
    assert run(db, "poshmark", posh) and posh.calls == []                 # for sale already: nothing changed
    assert run(db, "vinted", vinted)                                       # not hidden: kept
    assert run(db, "depop", depop) and db.listing(IID, "depop")["url"] == DEPOP     # Depop restored it: kept
    assert db.listing(IID, "depop")["status"] == "posted"
    assert [line.split(":")[0] for line in said.group] == ["♻️ Back on Poshmark", "♻️ Back on Vinted", "♻️ Back on Depop"]
    assert relist.status(db) == {"s_1": {"poshmark": "kept", "vinted": "kept", "depop": "kept"}}


def test_planned_once_per_sale_and_only_the_sites_it_came_down_from(env):
    db, said = env
    db.upsert_listing(IID, "ebay", status="posted", url="https://www.ebay.com/itm/407356800000", price=86)
    entry = relist.plan(db, "s_1", IID)
    assert sorted(entry["sites"]) == ["depop", "poshmark", "vinted"]       # a live site isn't touched
    assert relist.plan(db, "s_1", IID) is None and relist.take(db, [{"sale_id": "s_1", "item_id": IID}]) == []


def test_an_ended_ebay_listing_is_posted_again(env):
    db, said = env
    db.upsert_listing(IID, "ebay", status="delisted", url="https://www.ebay.com/itm/407356800000", price=86,
                      posted_at="2026-11-11T20:00:00+00:00")
    relist.plan(db, "s_1", IID)
    assert run(db, "ebay", None) and db.listing(IID, "ebay")["status"] == "queued"


def test_a_site_that_keeps_failing_says_so_once_and_is_left_to_her(env):
    db, said = env
    relist.plan(db, "s_1", IID)
    posh = Posh(ok=False)
    for _ in range(3):
        run(db, "poshmark", posh)
    assert len(posh.calls) == 3 and said.group == []
    [line] = [m for m in said.ops if m.startswith("⚠️ Couldn't put")]
    assert "Poshmark" in line and POSH in line
    assert not run(db, "poshmark", posh) and len(posh.calls) == 3


def test_never_in_the_quiet_hours_it_waits_until_2_pm(env, monkeypatch):
    db, said = env
    from thrift_agent.post import parallel
    relist.plan(db, "s_1", IID)
    calls = []

    async def run_one(*a, **k):
        calls.append(a)
        return True
    monkeypatch.setattr(relist, "run_one", run_one)
    s = SimpleNamespace(path=lambda name: Path("."), flag_set=lambda name: False)
    monkeypatch.setattr(parallel, "quiet_hold", lambda s_, db_, what="new": True)
    assert asyncio.run(parallel.relist_turn(s, db, "poshmark", Posh(), None)) is False and calls == []
    monkeypatch.setattr(parallel, "quiet_hold", lambda s_, db_, what="new": False)
    monkeypatch.setattr(parallel, "may_take_down", lambda s_, db_, site: True)
    assert asyncio.run(parallel.relist_turn(s, db, "poshmark", Posh(), None)) is True and len(calls) == 1


def test_get_tasks_relist_is_taken_by_fetch_tasks(env, monkeypatch):
    db, said = env

    class Api:
        def get(self, path, params):
            return {"tasks": [], "sold": [], "sold_on": [], "relist": [{"sale_id": "s_9", "item_id": IID,
                                                                      "marketplace": "depop", "why": "back"}]}
    monkeypatch.setattr(sales, "api", lambda: Api())
    sales.fetch_tasks(db, ["poshmark", "depop", "vinted"], force=True)
    assert relist.status(db) == {"s_9": {"poshmark": "due", "vinted": "due", "depop": "due"}}
