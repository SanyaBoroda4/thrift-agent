"""WO45 (2026-10-18): the whole path from share to listing — the share ledger, no card from part of a share in its
first 30 minutes, late photos that join their item (its record, its card, its live listings) or become items, a
one-photo group checked against the others, the second copy of a share, test shares, every listing read back after
posting, the API's safety net asking for one retry, a second look for a printed size, the model's guess as the ⭐.

Live: a 20-photo share taken with ONE photo at minute 17 (the Ralph Lauren shirt went up on Vinted and Depop with one
photo of four and never on Poshmark); the other 19 told in a private line and never processed."""
import asyncio
import json
from pathlib import Path

import pytest
from PIL import Image

from thrift_agent import approve, cli, edits, photocheck, photomem, pipeline, sales, shares, sizebuttons
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB, loads
from thrift_agent.ingest import segment as seg
from thrift_agent.schema import Ev, Facts, PhotoRole
from thrift_agent.telegram import Bot

T0 = 1_800_000_000.0
MIN = 60
CHAT, OPS, OWNER = 100, 200, 7


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
    said, group = [], []
    for mod in (pipeline, photomem, approve, photocheck, edits):
        monkeypatch.setattr(mod.notify, "say", said.append)
    monkeypatch.setattr(pipeline.notify, "group", group.append)
    bot, ops = FakeBot(), FakeBot(OPS)
    monkeypatch.setattr(approve, "bot_for", lambda _s: bot)
    monkeypatch.setattr(approve, "ops_bot_for", lambda _s: ops)
    clock = {"now": T0, "born": {}}
    monkeypatch.setattr(pipeline.time, "time", lambda: clock["now"])
    monkeypatch.setattr(pipeline, "appeared", lambda p: clock["born"].get(str(p), T0))
    monkeypatch.setattr(pipeline, "arrived", lambda p: clock["born"].get(str(p), T0))
    return s, db, bot, ops, said, group, clock


def _folder(s, clock, name, marker=None, photos=0, at=T0, colors=None, test=False):
    d = s.path("inbox") / name
    d.mkdir(parents=True)
    clock["born"][str(d)] = at
    for i in range(photos):
        Image.new("RGB", (60, 80), (colors or ["red"])[i % len(colors or ["red"])]).save(d / f"IMG_{i + 1}.jpeg")
    if marker:
        (d / "_done.txt").write_text(marker, encoding="utf-8")
    if test:
        (d / pipeline.TEST_FILE).write_text("1", encoding="utf-8")
    for f in d.iterdir():
        clock["born"][str(f)] = at
    return d


def facts_for(n_photos=1, **kw) -> Facts:
    base = dict(item_type="plaid button-down shirt", department="Men", category="Shirts",
                subcategory="Casual Button Down Shirts",
                brand=Ev(value="Ralph Lauren", photos=[0], source="photo", confidence=0.95),
                size_printed=Ev(), size_us=Ev(value="L", source="owner", confidence=1.0), colors=["Red"],
                condition="like_new", condition_evidence=Ev(value="clean", photos=[0], source="photo", confidence=0.9),
                cover_photo=0, photo_order=list(range(n_photos)),
                photo_roles=[PhotoRole(photo=i, role="front" if i == 0 else "detail") for i in range(n_photos)])
    base.update(kw)
    return Facts(**base)


def _item(s, db, facts: Facts, status="awaiting_price", title="Ralph Lauren Plaid Button-Down Shirt size L", price=40,
          n_photos=1, share="2026-10-18_121154", posted=()):
    """An item as the worker leaves it after processing, with `n_photos` prepared photos and a manifest."""
    bid = db.add_batch(str(s.path("inbox") / share), n_photos)
    db.set_batch(bid, status="split")
    d = s.path("work") / bid / "item_01"
    (d / "photos").mkdir(parents=True)
    for i in range(n_photos):
        Image.new("RGB", (60, 80), "red").save(d / "photos" / f"{i:02d}.jpg")
    Image.new("RGB", (30, 40), "red").save(d / "cover.jpg")
    (d / pipeline.MANIFEST).write_text(json.dumps([{"file": f"{i:02d}.jpg", "kind": "own", "src": i}
                                                   for i in range(n_photos)]), encoding="utf-8")
    iid = db.add_item(bid, 1, str(d))
    photos = [str(d / "cover.jpg")] + [str(d / "photos" / f"{i:02d}.jpg") for i in range(1, n_photos)]
    render = {"marketplace": "poshmark", "title": title, "description": "A shirt.", "brand": "Ralph Lauren",
              "department": facts.department, "category": facts.category, "subcategory": facts.subcategory,
              "size": "L", "colors": ["Red"], "condition": facts.condition, "price": price, "photos": photos,
              "sku": iid, "size_tab": "Standard", "size_value": "L"}
    db.set_item(iid, status=status, facts=facts.model_dump(), owner_price=price if status in ("ready", "posted") else None,
                price={"target": 40, "list_price": price, "source": "brand", "by_marketplace": {"poshmark": price},
                       "basis": "brand"},
                renders={"poshmark": render},
                gate={"decision": "publish", "reasons": [], "questions": [], "notes": [], "info": []})
    for mp in posted:
        db.upsert_listing(iid, mp, status="posted", url={"poshmark": "https://poshmark.com/listing/x-6ad4f5d7cb00f3049eba1b5b",
                                                         "depop": "https://www.depop.com/products/tetiana-shirt-24cb/",
                                                         "vinted": "https://www.vinted.com/items/10432047307"}[mp],
                          fields_json={"photos": photos[:1] if mp != "poshmark" else []}, price=price,
                          posted_at="2026-10-18T16:30:00+00:00")
    db.log(bid, "archived", {"dest": str(s.path("archive") / f"20261018_{share}")})
    (s.path("archive") / f"20261018_{share}").mkdir(parents=True, exist_ok=True)
    return bid, iid


# ---------------------------------------------------------------- §3 the ledger

def test_share_ids_and_the_ledger_steps(env):
    s, db, bot, ops, said, group, clock = env
    assert shares.share_id_of("2026-10-18_121154") == "2026-10-18_121154"
    assert shares.share_id_of(Path("/x/archive/20261018_2026-10-18_121154")) == "2026-10-18_121154"
    assert shares.share_id_of("20261018_2026-10-18_121154_2") == "2026-10-18_121154"
    assert shares.share_id_of("20261017_2026-10-17_151834-late") == "2026-10-17_151834"
    assert shares.share_id_of("2026-10-18_121154_4821") == "2026-10-18_121154_4821"       # §2's random suffix stays
    shares.seen(db, "2026-10-18_121154", "icloud", 20, 1)
    shares.seen(db, "2026-10-18_121154", "azure", 20, 20)
    row = db.conn.execute("SELECT * FROM shares WHERE id='2026-10-18_121154'").fetchone()
    assert row["source"] == "both" and row["received"] == 20 and row["expected"] == 20 and row["received_at"]
    shares.step(db, "2026-10-18_121154", "complete", batch_id="b_1")
    first = db.conn.execute("SELECT complete_at FROM shares WHERE id='2026-10-18_121154'").fetchone()[0]
    shares.step(db, "2026-10-18_121154", "complete")
    again = db.conn.execute("SELECT complete_at, batch_id FROM shares WHERE id='2026-10-18_121154'").fetchone()
    assert (again["complete_at"], again["batch_id"]) == (first, "b_1")                     # stamped once
    shares.outcome(db, "2026-10-18_121154", "carded")
    shares.outcome(db, "2026-10-18_121154", "failed", "later trouble")
    assert db.conn.execute("SELECT outcome FROM shares WHERE id='2026-10-18_121154'").fetchone()[0] == "carded"
    assert db.conn.execute("SELECT COUNT(*) FROM api_dirty WHERE kind='share'").fetchone()[0] == 1   # synced
    assert shares.lines(db, shares.today(db, "2026-10-18"))[0].startswith("2026-10-18_121154 (both; 20 of 20 photos)")


def test_a_share_is_recorded_from_its_first_look_to_its_card(env):
    s, db, bot, ops, said, group, clock = env
    d = _folder(s, clock, "2026-10-18_121705", marker="done 1", photos=1)
    clock["now"] = T0 + 2 * MIN
    assert pipeline.ready_folders(s, db) == [d]
    row = db.conn.execute("SELECT * FROM shares WHERE id='2026-10-18_121705'").fetchone()
    assert row["received"] == 1 and row["expected"] == 1 and row["source"] == "icloud" and not row["complete_at"]
    assert pipeline.take_share(s, db, d) == ("ready", T0)
    [b] = db.batches("new")
    row = db.conn.execute("SELECT * FROM shares WHERE id='2026-10-18_121705'").fetchone()
    assert row["complete_at"] and row["batch_id"] == b["id"] and row["outcome"] is None
    pipeline.process_batch(s, db, b["id"])                                     # one photo: no model call
    row = db.conn.execute("SELECT * FROM shares WHERE id='2026-10-18_121705'").fetchone()
    assert row["grouped_at"] and row["outcome"] is None
    [it] = db.conn.execute("SELECT * FROM items WHERE batch_id=?", (b["id"],)).fetchall()
    db.set_item(it["id"], status="awaiting_price", facts=facts_for().model_dump(),
                renders={"poshmark": {"title": "A shirt", "photos": ["x"], "price": 40}},
                price={"list_price": 40}, gate={"decision": "publish", "reasons": [], "questions": [], "notes": []})
    approve.send_item(s, db, it["id"])
    row = db.conn.execute("SELECT * FROM shares WHERE id='2026-10-18_121705'").fetchone()
    assert row["carded_at"] and row["outcome"] == "carded"
    assert shares.doc(db, "2026-10-18_121705")["outcome"] == "carded"


# ---------------------------------------------------------------- §6 no card from part of a share for 30 minutes

def test_a_partial_share_waits_30_minutes_and_its_card_says_how_many_of_how_many(env):
    s, db, bot, ops, said, group, clock = env
    d = _folder(s, clock, "2026-10-18_121154", marker="done 20", photos=1)
    clock["now"] = T0 + 17 * MIN
    assert pipeline.ready_folders(s, db) == []                                 # live: taken at minute 17 before
    clock["now"] = T0 + 31 * MIN
    assert pipeline.take_share(s, db, d) == ("partial", T0)
    assert said == ["📸 1 of 20 photos of your 12:11 PM share arrived — the card is made from those; the rest are told "
                    "if they come."]
    [b] = db.batches("new")
    pipeline.process_batch(s, db, b["id"])
    [it] = db.conn.execute("SELECT * FROM items WHERE batch_id=?", (b["id"],)).fetchall()
    assert it["photos_expected"] == 20
    caption = approve.item_caption(it["id"], {**dict(it), "facts": json.dumps(facts_for().model_dump()),
                                              "renders": json.dumps({"poshmark": {"title": "A shirt", "photos": ["x"]}}),
                                              "price": json.dumps({"list_price": 40}), "gate": json.dumps({})})[0]
    assert "📷 1 of 20 photos — the rest join when they arrive" in caption
    whole = _item(s, db, facts_for(4), n_photos=4, share="2026-10-18_121705")[1]
    assert "📷 4 photos" in approve.item_caption(whole, db.item(whole))[0]


# ---------------------------------------------------------------- §6 late photos join their item or become items

def _attach(assign):
    """A stand-in for the vision call: {new photo index: letter or None}."""
    def attach(new, refs, model, px):
        return seg.AttachOut(photos=[seg.Attachment(photo=i, item=assign.get(i), confidence=0.9 if assign.get(i) else 0.2)
                                     for i in range(len(new))])
    return attach


def test_late_photos_of_a_posted_item_join_it_and_its_live_listings_get_them(env, monkeypatch):
    s, db, bot, ops, said, group, clock = env
    bid, iid = _item(s, db, facts_for(), status="posted", posted=("depop", "vinted"))
    arch = s.path("archive") / "20261018_2026-10-18_121154"
    for name in ("IMG_2088.jpeg", "IMG_2089.jpeg", "IMG_2090.jpeg", "IMG_2096.jpeg"):
        Image.new("RGB", (60, 80), "red" if name < "IMG_2096" else "blue").save(arch / name)
    db.log(bid, "batch_registered", {"folder": "2026-10-18_121154", "photos": 1, "names": ["IMG_2088.jpeg"]})
    [done] = pipeline.late_photos(s, db)
    assert done.startswith(f"{bid}: 3 late, taken as ")
    new = done.split(" as ")[1]
    assert pipeline.late_of(db, new) == bid and pipeline.registered_names(db, new) == {"IMG_2089.jpeg", "IMG_2090.jpeg",
                                                                                         "IMG_2096.jpeg"}
    assert pipeline.batch_dir(db.batch(new)) == arch
    monkeypatch.setattr(pipeline.seg, "attach", _attach({0: "A", 1: "A", 2: None}))   # two are the shirt's, one isn't
    monkeypatch.setattr(pipeline.seg, "segment", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no grouping")))
    pipeline.process_batch(s, db, new)
    it = db.item(iid)
    d = Path(it["dir"])
    assert sorted(p.name for p in (d / "photos").glob("*.jpg")) == ["00.jpg", "01.jpg", "02.jpg"]
    manifest = json.loads((d / pipeline.MANIFEST).read_text())
    assert [e.get("late") for e in manifest] == [None, "IMG_2089.jpeg", "IMG_2090.jpeg"]
    assert loads(it["facts"])["photo_order"] == [0, 1, 2]
    assert len(loads(it["renders"])["poshmark"]["photos"]) == 3
    rows = db.conn.execute("SELECT marketplace, payload, status FROM listing_edits WHERE item_id=? ORDER BY marketplace",
                           (iid,)).fetchall()
    assert [(r["marketplace"], r["status"]) for r in rows] == [("depop", "pending"), ("vinted", "pending")]
    payload = loads(rows[0]["payload"])
    assert payload["had"] == 1 and payload["add"] == 2 and [Path(f).name for f in payload["files"]] == ["01.jpg", "02.jpg"]
    assert db.conn.execute("SELECT COUNT(*) FROM photos WHERE item_id=?", (iid,)).fetchone()[0] == 2   # remembered
    assert any("2 late photos joined" in t and "its listings get them" in t for t in said)
    # the third photo isn't the shirt's: it went on as a share of its own — one new item of the in-place batch
    others = db.conn.execute("SELECT * FROM items WHERE batch_id=?", (new,)).fetchall()
    assert len(others) == 1 and json.loads(others[0]["facts"] or "{}") == {} and db.batch(new)["status"] == "split"
    assert pipeline.late_photos(s, db) == []                                   # nothing late twice


def test_late_photos_that_all_join_end_the_batch_as_joined(env, monkeypatch):
    s, db, bot, ops, said, group, clock = env
    bid, iid = _item(s, db, facts_for(), status="new")                         # not processed yet: it takes them later
    db.set_item(iid, facts=None, renders=None, status="new")
    arch = s.path("archive") / "20261018_2026-10-18_121154"
    for name in ("IMG_2088.jpeg", "IMG_2089.jpeg"):
        Image.new("RGB", (60, 80), "red").save(arch / name)
    db.log(bid, "batch_registered", {"folder": "2026-10-18_121154", "photos": 1, "names": ["IMG_2088.jpeg"]})
    [done] = pipeline.late_photos(s, db)
    new = done.split(" as ")[1]
    monkeypatch.setattr(pipeline.seg, "attach", _attach({0: "A"}))
    pipeline.process_batch(s, db, new)
    assert db.batch(new)["status"] == "joined"
    assert db.conn.execute("SELECT COUNT(*) FROM items WHERE batch_id=?", (new,)).fetchone()[0] == 0
    assert sorted(p.name for p in (Path(db.item(iid)["dir"]) / "photos").glob("*.jpg")) == ["00.jpg", "01.jpg"]
    assert db.conn.execute("SELECT outcome FROM shares WHERE id='2026-10-18_121154'").fetchone()[0] == "joined"


def test_a_small_follow_up_share_is_compared_with_the_previous_shares_items(env, monkeypatch):
    s, db, bot, ops, said, group, clock = env
    bid, iid = _item(s, db, facts_for(), status="posted", posted=("depop",), share="2026-10-18_121154")
    d = _folder(s, clock, "2026-10-18_122000", marker="done 2", photos=2)      # 9 minutes later, two photos
    monkeypatch.setattr(pipeline.seg, "attach", _attach({0: "A", 1: "A"}))
    b2 = pipeline.register(s, db, d)
    refs = pipeline.reference_items(db, b2, d, 2)
    assert [r[3] for r in refs] == [iid]
    pipeline.process_batch(s, db, b2)
    assert db.batch(b2)["status"] == "joined"
    assert len(list((Path(db.item(iid)["dir"]) / "photos").glob("*.jpg"))) == 3
    far = _folder(s, clock, "2026-10-18_124000", marker="done 2", photos=2)   # 29 minutes later: a share of its own
    assert pipeline.reference_items(db, pipeline.register(s, db, far), far, 2) == []
    big = _folder(s, clock, "2026-10-18_121300", marker="done 4", photos=4)   # four photos: a share of its own
    assert pipeline.reference_items(db, pipeline.register(s, db, big), big, 4) == []


def test_a_one_photo_group_is_checked_against_the_other_items(env, monkeypatch):
    s, db, bot, ops, said, group, clock = env
    photos = [s.path("work") / f"p{i}.jpg" for i in range(4)]
    for p in photos:
        Image.new("RGB", (60, 80), "red").save(p)
    monkeypatch.setattr(pipeline.seg, "attach", _attach({0: "A"}))
    groups, summaries = pipeline.merge_singletons(s, db, "b_x", photos, ["own"] * 4, [[0, 1, 2], [3]],
                                                  ["a shirt", "a shirt on a hanger"])
    assert groups == [[0, 1, 2, 3]] and summaries == ["a shirt"]
    monkeypatch.setattr(pipeline.seg, "attach", _attach({0: None}))
    groups, _ = pipeline.merge_singletons(s, db, "b_x", photos, ["own"] * 4, [[0, 1, 2], [3]], ["a", "b"])
    assert groups == [[0, 1, 2], [3]]                                          # a different thing stays its own


# ---------------------------------------------------------------- §4 the second copy of a share

def test_the_second_copy_of_a_share_is_the_same_share_not_a_repeat(env):
    s, db, bot, ops, said, group, clock = env
    d = _folder(s, clock, "2026-10-18_121154", marker="done 1", photos=1)
    (d / pipeline.SOURCE_FILE).write_text("azure", encoding="utf-8")
    clock["now"] = T0 + 2 * MIN
    assert pipeline.take_share(s, db, d) == ("ready", T0)
    [b] = db.batches("new")
    pipeline.process_batch(s, db, b["id"])
    assert not d.exists()
    again = _folder(s, clock, "2026-10-18_121154", marker="done 1", photos=1, at=T0 + 10 * MIN)   # iCloud's copy
    clock["now"] = T0 + 12 * MIN
    assert pipeline.take_share(s, db, again) is None
    assert not again.exists() and len(list(s.path("archive").iterdir())) == 2
    row = db.conn.execute("SELECT * FROM shares WHERE id='2026-10-18_121154'").fetchone()
    assert row["outcome"] == "same_share" and row["source"] == "both"
    assert photomem.say_skipped(db) is None and said == [] and group == []


# ---------------------------------------------------------------- §2 test shares

def test_a_test_shares_card_goes_to_the_private_chat_and_never_posts(env):
    s, db, bot, ops, said, group, clock = env
    d = _folder(s, clock, "2026-10-18_130000", marker="done 1", photos=1, test=True)
    clock["now"] = T0 + 2 * MIN
    pipeline.take_share(s, db, d)
    [b] = db.batches("new")
    assert b["test"] == 1
    pipeline.process_batch(s, db, b["id"])
    [it] = db.conn.execute("SELECT * FROM items WHERE batch_id=?", (b["id"],)).fetchall()
    assert it["test"] == 1
    db.set_item(it["id"], status="ready", owner_price=40, facts=facts_for().model_dump(),
                renders={"poshmark": {"title": "A shirt", "photos": ["x"], "price": 40, "department": "Men",
                                      "category": "Shirts", "size": "L"}},
                price={"list_price": 40}, gate={"decision": "publish", "reasons": [], "questions": [], "notes": []})
    approve.send_item(s, db, it["id"])
    assert bot.texts() == [] and ops.texts() and ops.texts()[0].startswith("🧪 TEST — nothing will be listed")
    from thrift_agent.post import parallel, runner
    assert runner.next_job(s, db, ["poshmark"], False) is None
    assert parallel.feed(s, db) == []
    db.upsert_listing(it["id"], "depop", status="queued")
    from thrift_agent import crosslist
    assert crosslist.pending(s, db, ["depop"]) == []
    assert db.conn.execute("SELECT test FROM shares WHERE id='2026-10-18_130000'").fetchone()[0] == 1


# ---------------------------------------------------------------- §6/§7 every listing read back

def test_the_photo_check_queues_one_photos_edit_for_a_short_listing(env):
    s, db, bot, ops, said, group, clock = env
    bid, iid = _item(s, db, facts_for(4), n_photos=4, status="posted", posted=("depop", "poshmark"))
    photos = loads(db.item(iid)["renders"])["poshmark"]["photos"]
    db.upsert_listing(iid, "depop", fields_json={"photos": photos})
    row = db.listing(iid, "depop")
    note = photocheck.apply(s, db, row, {"photos": 1, "price": 40})
    assert note.endswith("→ 3 added through the edit page")
    lst = db.listing(iid, "depop")
    assert lst["photos_live"] == 1 and lst["photos_read_at"] and lst["price_live"] == 40
    [edit] = db.conn.execute("SELECT * FROM listing_edits WHERE item_id=?", (iid,)).fetchall()
    payload = loads(edit["payload"])
    assert edit["kind"] == "photos" and payload["had"] == 1 and [Path(f).name for f in payload["files"]] \
        == [Path(p).name for p in photos[1:]]
    assert photocheck.apply(s, db, db.listing(iid, "depop"), {"photos": 1, "price": 40}).endswith("(edit asked already)")
    assert db.conn.execute("SELECT COUNT(*) FROM listing_edits WHERE item_id=?", (iid,)).fetchone()[0] == 1
    note = photocheck.apply(s, db, db.listing(iid, "poshmark"), {"photos": 4, "price": 35})   # her hand change
    assert "price 35 on the site vs 40 ours" in note
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE ref=? AND kind='price_differs'", (iid,)).fetchone()[0] == 1
    assert photocheck.candidates(db) == []                                     # read: not due again today


def test_a_photos_edit_runs_on_the_site_and_is_mirrored(env, monkeypatch):
    s, db, bot, ops, said, group, clock = env
    bid, iid = _item(s, db, facts_for(3), n_photos=3, status="posted", posted=("depop", "poshmark"))
    photos = loads(db.item(iid)["renders"])["poshmark"]["photos"]
    assert edits.queue_photos(s, db, iid, "depop", photos[1:], had=1) and edits.queue_photos(s, db, iid, "poshmark",
                                                                                               photos[1:], had=1)
    assert edits.queue_photos(s, db, iid, "depop", photos[1:], had=1) == []    # never two in flight

    class Ext:
        async def add_photos(self, url, files, dry=False, shots=None, go=None, sku=None, had=0):
            assert len(files) == 2 and had == 1 and go()
            return {"ok": True, "saved": True, "note": "the edit page shows 3 photos", "shot": "", "after": 3}

    class Posh:
        async def set_photos(self, ctx, url, payload, dry=False, shots=None, go=None):
            assert payload["add"] == 2 and go()
            return {"ok": True, "saved": True, "note": "the public page shows 3 photos", "shot": ""}
    monkeypatch.setattr(edits, "may_edit", lambda *a, **k: True)
    assert asyncio.run(edits.run_one(s, db, "depop", Ext(), None, ["depop"])) is True
    assert asyncio.run(edits.run_one(s, db, "poshmark", Posh(), None, ["poshmark"])) is True
    for mp in ("depop", "poshmark"):
        row = db.conn.execute("SELECT * FROM listing_edits WHERE item_id=? AND marketplace=?", (iid, mp)).fetchone()
        assert row["status"] == "done", mp
        assert db.listing(iid, mp)["photos_live"] == 3
    assert len(loads(db.listing(iid, "depop")["fields_json"])["photos"]) == 3
    assert said[-1] == "✓ Ralph Lauren Plaid Button-Down Shirt size L is now 2 more photos on Poshmark"


def test_the_api_safety_net_asks_for_one_retry_and_reasons_of_the_owners_stay(env, monkeypatch):
    s, db, bot, ops, said, group, clock = env
    calls = []
    monkeypatch.setattr(sales, "call", lambda db_, path, body: calls.append((path, body)) or True)
    bid, iid = _item(s, db, facts_for(), status="posted", posted=("depop",))
    db.upsert_listing(iid, "poshmark", status="failed", error="PosterError: Poshmark's cover dialog differs")
    sales.repost(db, {"id": "t1", "item_id": iid, "marketplace": "poshmark", "kind": "repost"})
    assert db.listing(iid, "poshmark")["status"] == "queued" and calls[-1] == ("/tasks/t1", {"result": "done"})
    db.upsert_listing(iid, "vinted", status="skipped", error="skipped: no size on the item")
    sales.repost(db, {"id": "t2", "item_id": iid, "marketplace": "vinted", "kind": "repost"})
    assert db.listing(iid, "vinted")["status"] == "skipped" and calls[-1][0] == "/tasks/t2"
    assert "left as it is" in calls[-1][1]["error"]
    sales.repost(db, {"id": "t3", "item_id": "i_nope", "marketplace": "vinted", "kind": "repost"})
    assert calls[-1][1]["result"] == "not_found"


# ---------------------------------------------------------------- §8 the size

def test_a_second_look_finds_a_printed_size_before_any_buttons(env, monkeypatch):
    s, db, bot, ops, said, group, clock = env
    photos = [s.path("work") / f"s{i}.jpg" for i in range(3)]
    for p in photos:
        Image.new("RGB", (60, 80), "red").save(p)
    facts = facts_for(3, size_us=Ev(), department="Women", category="Shoes", subcategory="Sneakers",
                      photo_roles=[PhotoRole(photo=0, role="front"), PhotoRole(photo=1, role="label"),
                                   PhotoRole(photo=2, role="detail")])
    seen = []
    monkeypatch.setattr(pipeline.cover_brain, "read_size_label",
                        lambda ps, model, edge: seen.append([p.name for p in ps]) or "US 8.5 / EU 39")
    out = pipeline.second_look_size(s, db, "i_x", facts, photos, ["own"] * 3)
    assert seen == [["s1.jpg", "s2.jpg"]]                                      # the label and the detail, not the front
    assert out.size_us.value == "8.5" and out.size_printed.value == "US 8.5 / EU 39"
    monkeypatch.setattr(pipeline.cover_brain, "read_size_label", lambda *a: "EU 38")
    out = pipeline.second_look_size(s, db, "i_x", facts, photos, ["own"] * 3)
    assert out.size_us.value is None and out.size_eu.value == "38"
    monkeypatch.setattr(pipeline.cover_brain, "read_size_label", lambda *a: None)
    assert pipeline.second_look_size(s, db, "i_x", facts, photos, ["own"] * 3).size_us.value is None
    sized = facts_for(3, size_us=Ev(value="8", source="photo", confidence=0.9))
    monkeypatch.setattr(pipeline.cover_brain, "read_size_label", lambda *a: (_ for _ in ()).throw(AssertionError()))
    assert pipeline.second_look_size(s, db, "i_x", sized, photos, ["own"] * 3) is sized   # a read size: no call


def test_the_models_guess_marks_the_star_and_mens_sizes_are_on_the_menu(env):
    s, db, bot, ops, said, group, clock = env
    shoe = facts_for(1, size_us=Ev(), department="Men", category="Shoes", subcategory="Sneakers", size_guess="10.5")
    values = ["8", "8.5", "9", "9.5", "10", "10.5", "11", "11.5", "12"]
    assert sizebuttons.model_guess(shoe, values) == "10.5" and sizebuttons.guess(db, shoe, values) == "10.5"
    assert sizebuttons.guess(db, facts_for(1, size_us=Ev(), department="Men", category="Shoes"), values) == "10"
    shirt = facts_for(1, size_us=Ev(), department="Men", category="Shirts", size_guess=None)
    assert sizebuttons.guess(db, shirt, ["S", "M", "L", "XL"]) == "L"
    assert sizebuttons.model_guess(facts_for(1, size_us=Ev(), size_guess="US M"), ["S", "M", "L"]) == "M"


# ---------------------------------------------------------------- the CLI

def test_the_late_and_shares_commands(env, monkeypatch, capsys):
    s, db, bot, ops, said, group, clock = env
    monkeypatch.setattr(cli, "settings", lambda: s)
    monkeypatch.setattr(cli, "_db", lambda: db)
    bid, iid = _item(s, db, facts_for(), status="posted", posted=("vinted",))
    extra = s.path("work") / "extra.jpg"
    Image.new("RGB", (60, 80), "red").save(extra)
    cli.late_cmd(take=None, item=iid, file=[str(extra)], dry_run=False, queue_edits=None)
    assert "1 photos added; edits queued on vinted" in capsys.readouterr().out
    db.conn.execute("DELETE FROM listing_edits")                               # --queue-edits: the rows the sites lack
    cli.late_cmd(take=None, item=None, file=None, dry_run=True, queue_edits=iid)
    out = capsys.readouterr().out
    assert "vinted (1 photos after 1)" in out and "(dry runs)" in out
    assert db.conn.execute("SELECT dry FROM listing_edits WHERE item_id=?", (iid,)).fetchone()[0] == 1
    shares.seen(db, "2026-10-18_121154", "icloud", 20, 1)
    cli.shares_cmd(day="2026-10-18", all_=False)
    assert "2026-10-18_121154 (icloud; 1 of 20 photos)" in capsys.readouterr().out
    with pytest.raises(Exception):
        cli.late_cmd(take=["b_nope"], item=None, file=None, dry_run=False, queue_edits=None)


# ---------------------------------------------------------------- §10 the replay harness

def test_recorded_answers_replay_without_the_model(tmp_path, monkeypatch):
    from pydantic import BaseModel
    from thrift_agent.brain import llm

    class Out(BaseModel):
        n: int

    calls = []
    monkeypatch.setattr(llm, "_ask", lambda *a, **k: calls.append(a[4]) or Out(n=7))
    content = [llm.text("hello"), {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                                "data": "abc"}}]
    rec = llm.Answers(tmp_path / "answers.json", record=True)
    llm.RECORDER = rec
    try:
        assert llm.ask("m", "sys", content, Out, "tool", "d").n == 7 and calls == ["tool"]
        assert llm.ask("m", "sys", content, Out, "tool", "d").n == 7 and calls == ["tool"]     # replayed
        assert rec.hits == 1 and rec.misses == 1 and (tmp_path / "answers.json").exists()
        llm.RECORDER = llm.Answers(tmp_path / "answers.json", record=False)
        assert llm.ask("m", "sys", content, Out, "tool", "d").n == 7 and calls == ["tool"]
        with pytest.raises(llm.MissingAnswer):
            llm.ask("m", "sys (changed)", content, Out, "tool", "d")                             # a new prompt: a miss
    finally:
        llm.RECORDER = None


def test_a_golden_case_runs_in_a_scratch_workspace_and_is_compared(env, tmp_path, monkeypatch):
    from thrift_agent import replay
    s, db, bot, ops, said, group, clock = env
    case = tmp_path / "case"
    for name, colors in (("2026-10-18_121154", ["red"]), ("2026-10-18_121500", ["red"])):
        d = case / "shares" / name
        d.mkdir(parents=True)
        for i, c in enumerate(colors):
            Image.new("RGB", (60, 80), c).save(d / f"IMG_{i + 1}.jpeg")
        (d / "_done.txt").write_text(f"done {len(colors)}", encoding="utf-8")
    (case / "expected.yaml").write_text("items: 1\noutcomes: [null, repeat]\neach:\n  - photos: 1\n    department: Men\n"
                                        "said_contains: ['Repeat share skipped']\n", encoding="utf-8")

    def fake_process_item(s_, db_, iid):                                       # the model's part, stood in for
        db_.set_item(iid, status="awaiting_price", facts=facts_for().model_dump(),
                     renders={"poshmark": {"title": "Ralph Lauren Shirt", "photos": ["x"]}},
                     price={"list_price": 40}, gate={"decision": "publish", "reasons": [], "questions": [], "notes": []})
        return {"status": "awaiting_price"}
    monkeypatch.setattr(replay.pipeline, "process_item", fake_process_item)
    cards = []
    out = replay.run(s, case, tmp_path / "work", record=False, card=lambda iid, cap, cover: cards.append(cap))
    assert [x["outcome"] for x in out["shares"]] == [None, "repeat"]           # the same photo again: a repeat
    assert len(out["items"]) == 1 and out["items"][0]["department"] == "Men" and out["items"][0]["photos"] == 1
    assert out["mismatches"] == [] and cards and "Ralph Lauren Shirt" in cards[0]
    assert db.conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0   # the live database untouched
    assert "answers: 0 replayed, 0 asked" in replay.summary(out)
    (case / "expected.yaml").write_text("items: 2\n", encoding="utf-8")
    out = replay.run(s, case, tmp_path / "work", record=False)
    assert out["mismatches"] == ["1 items, expected 2"]
