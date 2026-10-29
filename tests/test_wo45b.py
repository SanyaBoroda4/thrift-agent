"""WO45 follow-up (2026-10-18, the owner): "price 80" on a live item's card changes the price on every site through
the price-edit path; a share folder that vanishes while being read ends `failed` with one private line; the iCloud copy
of a test share is marked a test by the API's word; the photo check reads Depop's and Vinted's shops in bulk and opens
a listing's own page only when something looks wrong."""
import asyncio
import json

import pytest
from PIL import Image

from thrift_agent import approve, photocheck, pipeline, pricedrop, uploads
from thrift_agent.approve import handle_update
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB, loads
from thrift_agent.schema import Ev, Facts, PhotoRole
from thrift_agent.telegram import Bot

T0 = 1_800_000_000.0
CHAT, OWNER = 100, 7


class FakeBot(Bot):
    def __init__(self, chat=CHAT):
        super().__init__("TOK", chat, {OWNER})
        self.calls, self.next_id = [], 10

    def call(self, method, **params):
        self.calls.append((method, params))
        if method in ("sendMessage", "sendPhoto"):
            self.next_id += 1
            return {"message_id": self.next_id}
        return [] if method == "getUpdates" else True

    def texts(self):
        return [p.get("text") or p.get("caption") for m, p in self.calls if m in ("sendMessage", "sendPhoto")]


@pytest.fixture
def env(tmp_path, monkeypatch):
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    data["telegram"] = {**base.get("telegram", {}), "enabled": True}
    s = Settings(data)
    s.ensure_dirs()
    db = DB(s.path("db"))
    said = []
    for mod in (pipeline, approve, photocheck, pricedrop, uploads):
        monkeypatch.setattr(mod.notify, "say", said.append)
    bot = FakeBot()
    monkeypatch.setattr(approve, "bot_for", lambda _s: bot)
    monkeypatch.setattr(approve, "ops_bot_for", lambda _s: bot)
    clock = {"now": T0, "born": {}}
    monkeypatch.setattr(pipeline.time, "time", lambda: clock["now"])
    monkeypatch.setattr(pipeline, "appeared", lambda p: clock["born"].get(str(p), T0))
    monkeypatch.setattr(pipeline, "arrived", lambda p: clock["born"].get(str(p), T0))
    return s, db, bot, said, clock


def facts_for(n=1) -> Facts:
    return Facts(item_type="plaid shirt", department="Men", category="Shirts", subcategory="Casual Button Down Shirts",
                 brand=Ev(value="Ralph Lauren", photos=[0], source="photo", confidence=0.95), size_printed=Ev(),
                 size_us=Ev(value="L", source="owner", confidence=1.0), colors=["Red"], condition="like_new",
                 condition_evidence=Ev(value="clean", photos=[0], source="photo", confidence=0.9), cover_photo=0,
                 photo_order=list(range(n)), photo_roles=[PhotoRole(photo=i, role="front") for i in range(n)])


URLS = {"poshmark": "https://poshmark.com/listing/x-6ad4f5d7cb00f3049eba1b5b",
        "depop": "https://www.depop.com/products/example-shirt-24cb/", "vinted": "https://www.vinted.com/items/10432047307"}


def _posted(s, db, price=40, n_photos=3, sites=("poshmark", "depop", "vinted")):
    import uuid
    bid = db.add_batch(str(s.path("inbox") / f"2026-10-18_121154_{uuid.uuid4().hex[:6]}"), n_photos)
    db.set_batch(bid, status="split")
    d = s.path("work") / bid / "item_01"
    (d / "photos").mkdir(parents=True)
    for i in range(n_photos):
        Image.new("RGB", (60, 80), "red").save(d / "photos" / f"{i:02d}.jpg")
    Image.new("RGB", (30, 40), "red").save(d / "cover.jpg")
    iid = db.add_item(bid, 1, str(d))
    photos = [str(d / "cover.jpg")] + [str(d / "photos" / f"{i:02d}.jpg") for i in range(1, n_photos)]
    db.set_item(iid, status="posted", owner_price=price, facts=facts_for(n_photos).model_dump(),
                price={"list_price": price},
                renders={"poshmark": {"marketplace": "poshmark", "title": "Ralph Lauren Plaid Shirt size L",
                                      "description": "A shirt.", "brand": "Ralph Lauren", "department": "Men",
                                      "category": "Shirts", "size": "L", "price": price, "photos": photos, "sku": iid}},
                gate={"decision": "publish", "reasons": [], "questions": [], "notes": []})
    for mp in sites:
        db.upsert_listing(iid, mp, status="posted", url=URLS[mp], price=price, posted_at="2026-10-18T16:30:00+00:00",
                          fields_json={"photos": photos if mp != "poshmark" else []})
    return iid


class PricePoster:
    """A stand-in site: the price read, then set once."""

    def __init__(self, live):
        self.live, self.set_calls = live, []

    async def read_price(self, ctx, url):
        return {"price": self.live}

    async def set_price(self, ctx, url, old, new, dry=False, shots=None, go=None, fields=None):
        assert go()
        self.set_calls.append((old, new))
        self.live = new
        return {"saved": True, "ok": True, "live_price": new, "shot": ""}


# ---------------------------------------------------------------- "price 80" on a live item

def test_price_80_on_a_live_items_card_changes_every_site_and_records_it(env, monkeypatch):
    s, db, bot, said, clock = env
    iid = _posted(s, db)
    db.add_outbox(str(CHAT), 50, "item", iid, "the price card")
    db.outbox_resolve("item", iid)
    db.add_outbox(str(CHAT), 60, "posted", iid, "✅ card")
    reply = {"update_id": 9, "message": {"message_id": 70, "chat": {"id": CHAT}, "from": {"id": OWNER}, "text": "price 80",
                                         "reply_to_message": {"message_id": 60}}}
    out = handle_update(s, db, bot, reply)
    assert out.startswith("owner price") and "$80" in out
    assert bot.texts()[-1] == "✓ $80 — the price changes on Poshmark, Depop, Vinted in a minute"
    rows = db.conn.execute("SELECT * FROM price_changes WHERE item_id=? ORDER BY marketplace", (iid,)).fetchall()
    assert [(r["marketplace"], r["stage"], r["status"], r["new_price"]) for r in rows] == \
        [("depop", "owner", "pending", 80.0), ("poshmark", "owner", "pending", 80.0), ("vinted", "owner", "pending", 80.0)]
    assert db.item(iid)["owner_price"] == 80
    monkeypatch.setattr(pricedrop, "may_drop", lambda *a, **k: True)
    posters = {mp: PricePoster(40 if mp != "vinted" else 80) for mp in URLS}          # Vinted shows 80 already
    for mp in ("poshmark", "depop", "vinted"):
        assert asyncio.run(pricedrop.run_one(s, db, mp, posters[mp], None)) is True
    assert posters["poshmark"].set_calls == [(40, 80.0)] and posters["vinted"].set_calls == []
    rows = db.conn.execute("SELECT marketplace, status, old_price, new_price FROM price_changes WHERE item_id=?",
                           (iid,)).fetchall()
    assert all(r["status"] == "done" for r in rows)
    assert db.listing(iid, "poshmark")["price"] == 80 and db.listing(iid, "poshmark")["price_live"] == 80
    assert said[-1] == "✓ Ralph Lauren Plaid Shirt: $80 on Poshmark, Depop, Vinted"
    assert pricedrop.announce(s, db) is None                                   # her own change: never the 🔻 line
    # the same reply on the item's own (old) price card works too; a word reply gets the hint
    reply["message"]["reply_to_message"]["message_id"] = 50
    reply["message"]["text"] = "85"
    assert handle_update(s, db, bot, reply).startswith("owner price")
    reply["message"]["reply_to_message"]["message_id"] = 60
    reply["message"]["text"] = "lovely"
    assert handle_update(s, db, bot, reply).startswith("posted") and bot.texts()[-1] == approve.POSTED_HINT


def test_an_owner_price_keeps_no_drop_and_the_site_minimum(env, monkeypatch):
    s, db, bot, said, clock = env
    iid = _posted(s, db, sites=("poshmark",))
    db.set_item(iid, no_drop="2026-10-18T00:00:00+00:00")
    pricedrop.owner_price(db, iid, 2)                                           # under Poshmark's $3
    monkeypatch.setattr(pricedrop, "may_drop", lambda *a, **k: True)
    assert asyncio.run(pricedrop.run_one(s, db, "poshmark", PricePoster(40), None)) is True
    row = db.conn.execute("SELECT status, error FROM price_changes WHERE item_id=?", (iid,)).fetchone()
    assert row["status"] == "skipped" and "minimum" in row["error"]
    assert said[-1].startswith("⚠️ Ralph Lauren Plaid Shirt: $2 not changed on Poshmark")
    with pytest.raises(ValueError, match="before it is listed"):
        pricedrop.owner_price(db, _item_awaiting(s, db), 30)


def _item_awaiting(s, db):
    iid = _posted(s, db, sites=())
    db.set_item(iid, status="awaiting_price", owner_price=None)
    return iid


# ---------------------------------------------------------------- a share folder that vanishes

def test_a_share_folder_that_vanishes_while_read_ends_failed_with_one_private_line(env):
    s, db, bot, said, clock = env
    d = s.path("inbox") / "2026-10-12_180232"
    d.mkdir(parents=True)
    Image.new("RGB", (60, 80), "red").save(d / "IMG_1.jpeg")
    (d / "_done.txt").write_text("done 1", encoding="utf-8")
    bid = pipeline.register(s, db, d)
    import shutil
    shutil.rmtree(d)                                                             # gone before the worker read it
    pipeline.process_batch(s, db, bid)                                           # no traceback
    b = db.batch(bid)
    assert b["status"] == "failed" and "vanished" in json.loads(b["reasons"])[0]
    assert said == [pipeline.VANISHED_LINE.format(when="6:02 PM")]
    assert db.conn.execute("SELECT outcome FROM shares WHERE id='2026-10-12_180232'").fetchone()[0] == "failed"


# ---------------------------------------------------------------- the iCloud copy of a test share

def test_the_icloud_copy_of_a_test_share_is_marked_by_the_apis_word(env):
    s, db, bot, said, clock = env
    d = s.path("inbox") / "2026-10-18_170000_9001"
    d.mkdir(parents=True)
    Image.new("RGB", (60, 80), "red").save(d / "IMG_1.jpeg")
    calls = []

    class Client:
        def post(self, path, body):
            calls.append(path)
            return {"ok": True}
    uploads._settle_known(db, Client(), "2026-10-18_170000_9001", d, ["IMG_1.jpeg"], test=True)
    assert (d / pipeline.TEST_FILE).read_text() == "1" and pipeline.is_test_share(d)
    assert calls == ["/shares/2026-10-18_170000_9001/fetched"]
    arch = s.path("archive") / "20261018_2026-10-18_170000_9002"
    arch.mkdir(parents=True)
    uploads._settle_known(db, Client(), "2026-10-18_170000_9002", arch, [], test=True)
    assert said and said[-1].startswith("🧪 The iCloud copy of test share 2026-10-18_170000_9002 was taken before")


# ---------------------------------------------------------------- the photo check in bulk

class BulkPoster:
    def __init__(self, listings, shape=("id", "title", "price", "photos")):
        self.listings, self.shape, self.single = listings, list(shape), []

    def available(self):
        return True

    async def read_shop(self):
        return {"listings": self.listings, "shape": self.shape, "source": "json"}

    async def read_photos(self, url, shots=None, sku=None):
        self.single.append(url)
        return {"count": 3}


def test_the_photo_check_reads_the_shops_in_bulk_and_opens_a_page_only_when_something_looks_wrong(env, monkeypatch):
    s, db, bot, said, clock = env
    a = _posted(s, db, n_photos=3, sites=("vinted", "depop"))
    b = _posted(s, db, n_photos=3, sites=("vinted",))
    db.upsert_listing(b, "vinted", url="https://www.vinted.com/items/999")
    vinted = BulkPoster([{"id": "10432047307", "price": "40.0", "photos": 3},      # a: complete
                         {"id": "999", "price": "35.0", "photos": None}])         # b: no count in the JSON
    depop = BulkPoster([{"slug": "example-shirt-24cb", "price": "40", "photos": 1, "sold": False}])   # a: short
    ps = {"vinted": vinted, "depop": depop}
    done = asyncio.run(photocheck.run(s, db, ps))
    assert db.listing(a, "vinted")["photos_live"] == 3 and db.listing(a, "vinted")["price_live"] == 40
    assert vinted.single == ["https://www.vinted.com/items/999"]                   # b's page: the JSON had no count
    assert db.listing(b, "vinted")["photos_live"] == 3
    assert depop.single == [URLS["depop"]]                                         # a short count: confirmed first
    assert db.listing(a, "depop")["photos_live"] == 3                              # the edit page showed them all
    assert db.conn.execute("SELECT COUNT(*) FROM listing_edits").fetchone()[0] == 0
    assert any("vinted: 3 photos on the site, 3 sent" in x for x in done)
    kinds = [r[0] for r in db.conn.execute("SELECT kind FROM events WHERE kind LIKE 'photocheck%' ORDER BY ts")]
    assert "photocheck_shape" in kinds and "photocheck_bulk" in kinds
    # a second run within 6 hours reads nothing again (the bulk stamp, the per-listing stamps)
    vinted.single.clear()
    depop.single.clear()
    asyncio.run(photocheck.run(s, db, ps))
    assert vinted.single == [] and depop.single == []
    assert len(loads(db.kv_get(photocheck.BULK_AT))) == 2
