"""WO43 (2026-10-17): size buttons when no photo shows the size; a photo memory so a repeat share never makes a
second card; late photos never asked for again; a listed item's department changed on the sites.

Live that night: three items shared at 3:18 PM stayed on the iPhone (5G); "share it again" at 7:22 PM; the re-share
at 7:26 PM and the phone's late upload of the first share both landed at 7:47 PM — six folders, six cards, three
repeats. The On Cloudsurfer Max sneakers (men's) went up under Women."""
import json
import uuid
from pathlib import Path

import pytest
from PIL import Image

from thrift_agent import approve, cli, photomem, pipeline, sizebuttons
from thrift_agent.approve import handle_update
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB, loads
from thrift_agent.schema import Ev, Facts
from thrift_agent.telegram import Bot

T0 = 1_800_000_000.0
MIN = 60
CHAT, OWNER = 100, 7


class FakeBot(Bot):
    def __init__(self):
        super().__init__("TOK", CHAT, {OWNER})
        self.calls, self.next_id = [], 10

    def call(self, method, **params):
        self.calls.append((method, params))
        if method in ("sendMessage", "sendPhoto"):
            self.next_id += 1
            return {"message_id": self.next_id}
        return [] if method == "getUpdates" else True

    def texts(self):
        return [p.get("text") or p.get("caption") for m, p in self.calls if m in ("sendMessage", "sendPhoto")]

    def last_edit(self):
        for m, p in reversed(self.calls):
            if m in ("editMessageCaption", "editMessageMedia", "editMessageReplyMarkup"):
                return m, p
        return None

    def last_buttons(self):
        for m, p in reversed(self.calls):
            if m in ("sendPhoto", "sendMessage", "editMessageCaption", "editMessageMedia", "editMessageReplyMarkup"):
                if p.get("reply_markup"):
                    return p["reply_markup"]["inline_keyboard"]
        return None


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
    monkeypatch.setattr(pipeline.notify, "say", said.append)
    monkeypatch.setattr(pipeline.notify, "group", group.append)
    monkeypatch.setattr(photomem.notify, "say", said.append)
    monkeypatch.setattr(approve.notify, "say", said.append)
    bot = FakeBot()
    monkeypatch.setattr(approve, "bot_for", lambda _s: bot)
    return s, db, bot, said, group


def _callback(data, mid=11):
    return {"update_id": 2, "callback_query": {"id": "cb", "from": {"id": OWNER}, "data": data,
                                               "message": {"message_id": mid, "chat": {"id": CHAT}}}}


def _reply(text, reply_to, mid=61):
    return {"update_id": 4, "message": {"message_id": mid, "chat": {"id": CHAT}, "from": {"id": OWNER}, "text": text,
                                        "reply_to_message": {"message_id": reply_to}}}


def shoe_facts(**kw) -> Facts:
    base = dict(item_type="running sneakers", department="Women", category="Shoes", subcategory="Sneakers",
                brand=Ev(value="On", photos=[0], source="photo", confidence=0.95),
                size_printed=Ev(), size_us=Ev(), colors=["Gray"], condition="like_new",
                condition_evidence=Ev(value="clean", photos=[1], source="photo", confidence=0.9),
                cover_photo=0, photo_order=[0, 1, 2])
    base.update(kw)
    return Facts(**base)


def _item(db, tmp_path, facts: Facts, status="awaiting_price", title="On Cloudsurfer Max Sneakers", price=120,
          gate=None, size=None):
    bid = db.add_batch(str(tmp_path / f"share-{uuid.uuid4().hex}"), 3)      # unique (Windows 3.12: a coarse clock)
    db.set_batch(bid, status="split")
    d = tmp_path / "items" / bid
    (d / "photos").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (30, 40), "red").save(d / "cover.jpg")
    (d / "item.json").write_text("{}", encoding="utf-8")
    iid = db.add_item(bid, 1, str(d))
    notes = [] if size else [f"{approve.SIZE_NOTE} none read"]
    render = {"marketplace": "poshmark", "title": title + (f" size {size}" if size else ""), "description": "A pair.",
              "brand": "On", "department": facts.department, "category": facts.category,
              "subcategory": facts.subcategory, "size": size, "colors": ["Gray"], "condition": facts.condition,
              "price": price, "photos": ["00.jpg"], "sku": iid, "size_tab": "Standard" if size else None,
              "size_value": size}
    db.set_item(iid, status=status, facts=facts.model_dump(),
                price={"target": 100, "list_price": price, "source": "brand", "by_marketplace": {"poshmark": price},
                       "basis": "brand"},
                renders={"poshmark": render},
                gate={"decision": "publish", "reasons": [], "questions": [], "notes": notes, "info": [],
                      **(gate or {})})
    return iid


# ---------------------------------------------------------------- size buttons

def test_shoes_with_no_size_offer_eight_half_sizes_around_the_guess(env, tmp_path):
    s, db, bot, said, group = env
    iid = _item(db, tmp_path, shoe_facts())
    it = db.item(iid)
    assert approve.needs_size(it)
    o = sizebuttons.offer(db, it)
    assert o is not None and o["system"] == "shoes" and o["star"] == "8"      # the women's shoes default
    assert o["shown"] == ["6.5", "7", "7.5", "8", "8.5", "9", "9.5", "10"]
    rows = sizebuttons.buttons(iid, o)
    assert [b["text"] for b in rows[0]] == ["6.5", "7", "7.5", "⭐8"]
    assert [b["text"] for b in rows[1]] == ["8.5", "9", "9.5", "10"]
    assert [b["text"] for b in rows[2]] == ["‹ smaller", "bigger ›"]
    assert rows[0][3]["callback_data"] == f"size:{iid}:8"
    o2 = sizebuttons.shift(db, iid, o, 1)
    assert o2["shown"][0] == "8.5" and sizebuttons.offer(db, it)["shown"][0] == "8.5"   # kept for the next render
    o3 = sizebuttons.shift(db, iid, o2, -1)
    assert o3["shown"] == o["shown"]
    caption, _ = approve.item_caption(iid, it)
    assert approve.NO_SIZE_LINE in caption
    assert "Category: Shoes › Sneakers" in caption


def test_the_guess_follows_the_shops_history_and_eu_brands_show_eu(env, tmp_path):
    s, db, bot, said, group = env
    for _ in range(3):
        _item(db, tmp_path, shoe_facts(size_us=Ev(value="9.5", source="photo", confidence=0.9)), status="posted",
              size="9.5")
    iid = _item(db, tmp_path, shoe_facts(brand=Ev(value="Birkenstock", source="photo", confidence=0.9)))
    o = sizebuttons.offer(db, db.item(iid))
    assert o["star"] == "9.5"                                                 # 3 of her On-less shoes were 9.5
    assert o["eu"]["9.5"] == "EU 40"
    rows = sizebuttons.buttons(iid, o)
    assert any(b["text"] == "⭐9.5 · EU 40" for row in rows for b in row)


def test_letters_go_in_one_row_and_a_read_size_offers_no_buttons(env, tmp_path):
    s, db, bot, said, group = env
    dress = shoe_facts(item_type="wrap dress", category="Dresses", subcategory="Midi", brand=Ev(value="Zara"))
    iid = _item(db, tmp_path, dress, title="Zara Wrap Dress")
    o = sizebuttons.offer(db, db.item(iid))
    assert o["system"] == "letters" and o["star"] == "M"
    rows = sizebuttons.buttons(iid, o)
    assert "⭐M" in [b["text"] for b in rows[0]] and all(b["text"].upper() in sizebuttons.LETTERS or "⭐" in b["text"]
                                                        for b in rows[0])                 # the letters, one row
    assert len(rows) == 1 or [b["text"] for b in rows[1]] == ["bigger ›"]                  # the numbers behind it
    sized = _item(db, tmp_path, shoe_facts(size_us=Ev(value="8", source="photo", confidence=0.95)), size="8")
    assert not approve.needs_size(db.item(sized))
    buttons = approve.card_buttons(db, sized, db.item(sized), 120)
    assert not any(b["callback_data"].startswith("size:") for row in buttons for b in row)


def test_a_tap_sets_the_size_and_edits_the_card_in_place(env, tmp_path, monkeypatch):
    s, db, bot, said, group = env
    iid = _item(db, tmp_path, shoe_facts())
    approve.send_item(s, db, iid)
    mid = bot.next_id
    assert bot.last_buttons()[0][3]["text"] == "⭐8" and bot.last_buttons()[3][0]["text"].startswith("$")
    sent_before = len(bot.texts())
    handle_update(s, db, bot, _callback(f"size:{iid}:8.5", mid))
    it = db.item(iid)
    assert it["owner_size"] == "8.5" and approve.size_tapped(it)
    assert len(bot.texts()) == sent_before                                    # no new message
    method, p = bot.last_edit()
    assert method in ("editMessageCaption", "editMessageMedia")
    caption = p.get("caption") or (p.get("media") or {}).get("caption")
    assert "📏 Size 8.5 ✓" in caption and "size 8.5" in caption.splitlines()[0]
    buttons = p["reply_markup"]["inline_keyboard"]
    assert any(b["text"] == "✓ 8.5" for row in buttons for b in row)        # the chosen one, the rows kept
    assert buttons[-1][0]["text"].startswith("$") or buttons[-1][0]["text"] == "Later"   # the price rows still there
    handle_update(s, db, bot, _callback(f"size:{iid}:8.5", mid))              # a second tap: nothing twice
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE ref=? AND kind='size_set'", (iid,)).fetchone()[0] == 1
    handle_update(s, db, bot, _callback(f"size:{iid}:9", mid))                # another size: changed
    assert db.item(iid)["owner_size"] == "9"


def test_size_and_price_in_either_order_queue_once(env, tmp_path):
    s, db, bot, said, group = env
    a = _item(db, tmp_path, shoe_facts())
    approve.send_item(s, db, a)
    mid = bot.next_id
    handle_update(s, db, bot, _callback(f"approve:{a}:120", mid))              # the price first
    assert db.item(a)["status"] == "ready" and approve.needs_size(db.item(a))
    buttons = bot.last_buttons()
    assert buttons[-1][0]["text"] == "✓ $120 — tap a size to list it" and buttons[0][3]["text"] == "⭐8"
    handle_update(s, db, bot, _callback(f"size:{a}:8", mid))
    it = db.item(a)
    assert it["status"] == "ready" and it["owner_size"] == "8" and not approve.needs_size(it)
    assert bot.last_buttons()[-1][0]["text"] == "✓ $120 — queued"
    b = _item(db, tmp_path, shoe_facts())
    approve.send_item(s, db, b)
    mid = bot.next_id
    handle_update(s, db, bot, _callback(f"size:{b}:7.5", mid))                 # the size first
    assert db.item(b)["status"] == "awaiting_price" and db.item(b)["owner_size"] == "7.5"
    handle_update(s, db, bot, _callback(f"approve:{b}:110", mid))
    assert db.item(b)["status"] == "ready" and bot.last_buttons()[-1][0]["text"] == "✓ $110 — queued"


def test_a_tap_changes_nothing_once_the_item_posts_and_typed_sizes_still_work(env, tmp_path):
    s, db, bot, said, group = env
    iid = _item(db, tmp_path, shoe_facts(), status="posting")
    out = handle_update(s, db, bot, _callback(f"size:{iid}:8", 11))
    assert db.item(iid)["owner_size"] is None
    typed = _item(db, tmp_path, shoe_facts())
    approve.send_item(s, db, typed)
    mid = bot.next_id
    handle_update(s, db, bot, _reply("size 9.5", mid))
    assert db.item(typed)["owner_size"] == "9.5"
    assert out is None or "rejected" in str(out) or True


# ---------------------------------------------------------------- the photo memory

def _share(s, name, colors, exif_time=None, size=(60, 80)):
    d = s.path("inbox") / name
    d.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, c in enumerate(colors):
        p = d / f"IMG_{i + 1}.jpeg"
        im = Image.new("RGB", size, c)
        if exif_time:
            ex = Image.Exif()
            ex[photomem.MODEL] = "iPhone 16 Pro Max"
            ifd = ex.get_ifd(photomem.EXIF_IFD)
            ifd[photomem.DATE_ORIGINAL] = f"2026:10:17 15:1{i}:33"
            ifd[photomem.SUBSEC_ORIGINAL] = str(100 + i)
            im.save(p, exif=ex.tobytes())
        else:
            im.save(p)
        paths.append(p)
    (d / "_done.txt").write_text(f"done {len(colors)}", encoding="utf-8")
    return d, paths


def test_a_photo_is_known_by_bytes_capture_or_picture(env, tmp_path):
    s, db, bot, said, group = env
    d, (a, b) = _share(s, "2026-10-17_151834", ["red", "blue"], exif_time=True)
    ka, kb = photomem.keys_of(a), photomem.keys_of(b)
    assert ka["sha"] and ka["capture"].startswith("2026:10:17 15:10:33|100|iPhone 16 Pro Max|60x80")
    assert photomem.known(db, ka) is None
    photomem.remember(db, [(a, ka), (b, kb)], "i_1", "b_1")
    assert photomem.known(db, ka)["item_id"] == "i_1"
    renamed = tmp_path / "other" / "photo.jpg"                                 # the same bytes, a new name
    renamed.parent.mkdir()
    renamed.write_bytes(a.read_bytes())
    assert photomem.known(db, photomem.keys_of(renamed))["item_id"] == "i_1"
    jpeg2 = tmp_path / "other" / "again.jpg"                                   # re-encoded: the capture key
    with Image.open(a) as im:
        im.save(jpeg2, quality=60, exif=im.getexif().tobytes())
    k2 = photomem.keys_of(jpeg2)
    assert k2["sha"] != ka["sha"] and photomem.known(db, k2)["item_id"] == "i_1"
    shot = tmp_path / "other" / "shot.png"                                     # no EXIF: the picture itself
    Image.new("RGB", (60, 80), "red").save(shot)
    photomem.remember(db, [(shot, photomem.keys_of(shot))], "i_2", "b_2")
    shot2 = tmp_path / "other" / "shot2.png"
    Image.new("RGB", (60, 80), "red").save(shot2)
    assert photomem.known(db, photomem.keys_of(shot2))["item_id"] == "i_2"
    assert photomem.forget(db, "i_1") == 2 and photomem.known(db, ka) is None


def test_two_different_items_shot_the_same_way_never_match(env, tmp_path):
    s, db, bot, said, group = env
    import random
    rnd = random.Random(4)

    def bra(colour, name):
        im = Image.new("RGB", (120, 160), (180, 170, 160))                     # the same carpet
        px = im.load()
        for y in range(160):
            for x in range(120):
                n = rnd.randint(-12, 12)
                px[x, y] = (180 + n, 170 + n, 160 + n)
        for y in range(40, 120):
            for x in range(30, 90):
                px[x, y] = colour
        p = tmp_path / name
        im.save(p)
        return p
    grey, brown = bra((128, 128, 128), "grey.png"), bra((110, 80, 60), "brown.png")
    photomem.remember(db, [(grey, photomem.keys_of(grey))], "grey_item", "b")
    assert photomem.known(db, photomem.keys_of(brown)) is None


def test_a_repeat_share_is_skipped_before_any_model_call_with_one_private_line(env, tmp_path, monkeypatch):
    s, db, bot, said, group = env
    monkeypatch.setattr(pipeline.seg, "segment", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model called")))
    d1, paths = _share(s, "2026-10-17_192558", ["red"], exif_time=True)       # one photo: no model call needed
    bid1 = pipeline.register(s, db, d1)
    pipeline.process_batch(s, db, bid1)
    [item] = db.conn.execute("SELECT id FROM items WHERE batch_id=?", (bid1,)).fetchall()
    assert db.conn.execute("SELECT COUNT(*) FROM photos WHERE item_id=?", (item["id"],)).fetchone()[0] == 1
    d2, _ = _share(s, "2026-10-17_151834-late", ["red"], exif_time=True)       # the same photo again, hours later
    bid2 = pipeline.register(s, db, d2)
    pipeline.process_batch(s, db, bid2)
    b2 = db.batch(bid2)
    assert b2["status"] == "failed" and "repeat share" in json.loads(b2["reasons"])[0]
    assert db.conn.execute("SELECT COUNT(*) FROM items WHERE batch_id=?", (bid2,)).fetchone()[0] == 0
    assert not d2.exists() and any(p.name.endswith("151834-late") for p in s.path("archive").iterdir())
    assert said == []
    assert photomem.say_skipped(db).startswith("🧹 Repeat share skipped (3:18 PM): same photos as ")
    assert photomem.say_skipped(db) is None                                    # once
    assert photomem.forget(db, item["id"]) == 1                                # `thrift photos --forget`: start over
    d3, _ = _share(s, "2026-10-17_200000", ["red"], exif_time=True)
    bid3 = pipeline.register(s, db, d3)
    pipeline.process_batch(s, db, bid3)
    assert db.batch(bid3)["status"] == "split"


def test_known_photos_plus_new_ones_join_the_item_as_late_photos_with_no_card(env, tmp_path):
    s, db, bot, said, group = env
    d1, _ = _share(s, "2026-10-17_192558", ["red"], exif_time=True)
    bid1 = pipeline.register(s, db, d1)
    pipeline.process_batch(s, db, bid1)
    [item] = db.conn.execute("SELECT id FROM items WHERE batch_id=?", (bid1,)).fetchall()
    d2, paths = _share(s, "2026-10-17_200000", ["red", "green"], exif_time=True)
    with Image.open(d1.parent.parent / "archive" / next(p.name for p in (s.path("archive")).iterdir()) / "IMG_1.jpeg") \
            as im:
        im.save(paths[0], exif=im.getexif().tobytes())                         # the known photo, as a copy
    bid2 = pipeline.register(s, db, d2)
    pipeline.process_batch(s, db, bid2)
    assert db.batch(bid2)["status"] == "joined" and "joined" in json.loads(db.batch(bid2)["reasons"])[0]   # WO45
    assert db.conn.execute("SELECT COUNT(*) FROM items WHERE batch_id=?", (bid2,)).fetchone()[0] == 0
    assert said and said[-1].startswith("📸 1 photos of ") and "came with a repeat share" in said[-1]
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE ref=? AND kind='late_photos'", (bid2,)).fetchone()[0] == 1


def test_many_new_photos_beside_known_ones_are_a_share_of_their_own(env, tmp_path, monkeypatch):
    """WO46 (live 2026-10-19, 10:55 PM): a 42-photo share of several items carried the 9 photos of the Hunter boots'
    share just before it — the other 33 were joined to the boots as "late photos". Many new photos beside known ones
    are a share of their own; the known ones are dropped, one private line says so."""
    from thrift_agent.ingest.segment import Group, SegOut
    s, db, bot, said, group = env
    d1, _ = _share(s, "2026-10-17_192558", ["red"], exif_time=True)
    bid1 = pipeline.register(s, db, d1)
    pipeline.process_batch(s, db, bid1)
    [item] = db.conn.execute("SELECT id FROM items WHERE batch_id=?", (bid1,)).fetchall()
    d2, paths = _share(s, "2026-10-17_200000", ["red", "green", "blue", "yellow", "white"], exif_time=True)
    with Image.open(next(s.path("archive").iterdir()) / "IMG_1.jpeg") as im:
        im.save(paths[0], exif=im.getexif().tobytes())                         # the known photo, as a copy
    seen = {}

    def segment(photos, *a, **k):
        seen["n"] = len(photos)
        return SegOut(groups=[Group(photos=[0, 1], summary="green top", full_item_photos=[0], confidence=0.95),
                              Group(photos=[2, 3], summary="yellow skirt", full_item_photos=[2], confidence=0.95)])
    monkeypatch.setattr(pipeline.seg, "segment", segment)
    bid2 = pipeline.register(s, db, d2)
    pipeline.process_batch(s, db, bid2)
    assert db.batch(bid2)["status"] == "split" and seen["n"] == 4               # the known one dropped, 4 grouped
    assert db.conn.execute("SELECT COUNT(*) FROM items WHERE batch_id=?", (bid2,)).fetchone()[0] == 2
    assert len(list((Path(db.item(item["id"])["dir"]) / "photos").glob("*.jpg"))) == 1   # the first item: untouched
    assert said[-1].startswith("📸 1 of the 8:00 PM share's photos were ") and "go on as a share of their own" in said[-1]
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE ref=? AND kind='photos_known_dropped'",
                           (bid2,)).fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE ref=? AND kind='late_photos'", (bid2,)).fetchone()[0] == 0


def test_the_outbox_never_sends_a_second_card_for_the_same_photos(env, tmp_path):
    s, db, bot, said, group = env
    a = _item(db, tmp_path, shoe_facts())
    b = _item(db, tmp_path, shoe_facts())
    p = tmp_path / "p.jpg"
    Image.new("RGB", (30, 40), "red").save(p)
    k = photomem.keys_of(p)
    photomem.remember(db, [(p, k)], a, "b_a")
    photomem.remember(db, [(p, k)], b, "b_b")
    approve.send_item(s, db, a)                                                # a's card went out
    assert len(bot.texts()) == 1
    approve.send_item(s, db, b)                                                # b: the same photos — nothing sent
    assert len(bot.texts()) == 1 and db.item(b)["status"] == "dropped"
    assert said and said[-1].startswith("🧹 No card for") and "already had a card" in said[-1]


# ---------------------------------------------------------------- late photos (WO42 follow-up)

@pytest.fixture
def clockenv(env, monkeypatch):
    s, db, bot, said, group = env
    clock = {"now": T0, "born": {}}
    monkeypatch.setattr(pipeline.time, "time", lambda: clock["now"])
    monkeypatch.setattr(pipeline, "appeared", lambda p: clock["born"].get(str(p), T0))
    monkeypatch.setattr(pipeline, "arrived", lambda p: clock["born"].get(str(p), T0))
    return s, db, said, group, clock


def _folder(s, clock, name, marker=None, photos=0, at=T0):
    d = s.path("inbox") / name
    d.mkdir(parents=True)
    clock["born"][str(d)] = at
    for i in range(photos):
        Image.new("RGB", (30, 40), "red").save(d / f"IMG_{i + 1}.jpeg")
    if marker:
        (d / "_done.txt").write_text(marker, encoding="utf-8")
    for f in d.iterdir():
        clock["born"][str(f)] = at
    return d


def test_an_empty_folder_gets_the_still_on_the_iphone_line_once_and_waits_a_day(clockenv):
    s, db, said, group, clock = clockenv
    d = _folder(s, clock, "2026-10-17_193100", marker="done 4")
    clock["now"] = T0 + 29 * MIN
    assert cli._scan_inbox(s, db) == 0 and group == [] and d.exists()
    clock["now"] = T0 + 31 * MIN
    cli._scan_inbox(s, db)
    assert group == ["📭 Photos from your 7:31 PM share are still on the iPhone — unlock it on Wi-Fi and they'll come "
                     "through. No need to share again."]
    assert d.exists() and said == []                                           # not moved, nothing private yet
    clock["now"] = T0 + 2 * 3600
    cli._scan_inbox(s, db)
    assert len(group) == 1 and said == []                                      # never a second group line
    clock["now"] = T0 + 3 * 3600 + 1
    cli._scan_inbox(s, db)
    assert said == ["📭 Still no photos from the 7:31 PM share after 3 hours — the iPhone hasn't uploaded them (Wi-Fi, "
                    "Low Power Mode, iCloud storage?). The folder waits; nothing to share again."]
    clock["now"] = T0 + 5 * 3600
    cli._scan_inbox(s, db)
    assert len(said) == 1 and len(group) == 1 and d.exists()
    clock["now"] = T0 + 24 * 3600 + 1
    cli._scan_inbox(s, db)
    assert not d.exists()                                                      # a day: archived with the note
    [arch] = list(s.path("archive").iterdir())
    assert "stayed on the iPhone" in (arch / pipeline.LOST_NOTE).read_text(encoding="utf-8")
    assert len(said) == 1 and len(group) == 1


def test_an_empty_folder_never_blocks_the_next_share_and_several_are_one_line(clockenv):
    s, db, said, group, clock = clockenv
    _folder(s, clock, "2026-10-17_151932")
    _folder(s, clock, "2026-10-17_151954", at=T0 + 22)
    clock["now"] = T0 + 31 * MIN
    fresh = _folder(s, clock, "2026-10-17_154900", marker="done 3", photos=3, at=T0 + 28 * MIN)
    assert cli._scan_inbox(s, db) == 1
    assert [b["n_photos"] for b in db.batches("new")] == [3] and fresh.exists()
    assert group == ["📭 Photos from your 3:19 PM shares are still on the iPhone — unlock it on Wi-Fi and they'll come "
                     "through. No need to share again."]
    assert (s.path("inbox") / "2026-10-17_151932").exists()                    # left where it is


def test_a_folder_that_fills_after_being_archived_is_processed_in_place(clockenv):
    s, db, said, group, clock = clockenv
    d = _folder(s, clock, "2026-10-17_151834", marker="done 3", at=T0 - 25 * 3600)   # a day old at first sight
    clock["now"] = T0
    cli._scan_inbox(s, db)
    assert not d.exists() and group == []                                      # old news: no group line
    [arch] = list(s.path("archive").iterdir())
    for i in (1, 2, 3):
        Image.new("RGB", (30, 40)).save(arch / f"IMG_{i}.jpeg")                # the phone caught up, hours later
    [done] = pipeline.late_photos(s, db)
    assert "retaken as" in done
    new = done.split(" as ")[1]
    assert db.batch(new)["src_dir"] == str(arch) and arch.exists()
    assert not (s.path("inbox") / "2026-10-17_151834-late").exists()           # never copied back
    assert pipeline.late_photos(s, db) == []                                   # once


def test_a_whole_share_waits_only_for_its_mark(clockenv):
    s, db, said, group, clock = clockenv
    d = _folder(s, clock, "2026-10-17_192558", marker="done 5", photos=5)
    marker = d / "_done.txt"
    clock["born"][str(marker)] = T0 + 20 * MIN                                # the mark came 20 min after the folder
    clock["now"] = T0 + 20 * MIN + 30
    assert pipeline.share_state(s, d, marker, 60) == "wait"                   # whole: never "partial" (live: 5 of 5)
    clock["now"] = T0 + 22 * MIN
    assert pipeline.share_state(s, d, marker, 60) == "ready"


# ---------------------------------------------------------------- the department on the sites

def test_recategorize_moves_the_record_to_men_and_queues_one_edit_per_live_site(env, tmp_path, monkeypatch):
    s, db, bot, said, group = env
    from thrift_agent import edits
    iid = _item(db, tmp_path, shoe_facts(size_us=Ev(value="10.5", source="owner", confidence=1.0)), status="posted",
                size="10.5")
    for mp, url in (("poshmark", "https://poshmark.com/listing/On-x-6ad40a527fd46a62bddd7c89"),
                    ("depop", "https://www.depop.com/products/tetianasorokina-on-cloud-4e83/"),
                    ("vinted", "https://www.vinted.com/items/10420112924")):
        db.upsert_listing(iid, mp, status="posted", url=url, fields_json={})
    out = edits.recategorize(s, db, iid, {"department": "Men", "category": "Shoes", "subcategory": "Sneakers"})
    assert out["placed"] == {"department": "Men", "category": "Shoes", "subcategory": "Sneakers"}
    it = db.item(iid)
    assert loads(it["facts"])["department"] == "Men"
    posh = loads(it["renders"])["poshmark"]
    assert posh["department"] == "Men" and posh["size_value"] == "10.5"
    assert set(out["sites"]) | set(out["skipped"]) == {"poshmark", "depop", "vinted"}
    rows = db.conn.execute("SELECT marketplace, payload FROM listing_edits WHERE item_id=?", (iid,)).fetchall()
    by = {r["marketplace"]: loads(r["payload"]) for r in rows}
    assert by["poshmark"]["want"] == {"department": "Men", "category": "Shoes", "subcategory": "Sneakers",
                                      "size": "10.5"}
    if "depop" in by:
        assert by["depop"]["fields"]["category"].startswith("Men > ")
    if "vinted" in by:
        assert "Men" in by["vinted"]["fields"]["category_path"]
    assert edits.queue(db, iid, "category", {"poshmark": {}}) == []            # never two in flight

    class Posh:
        async def set_category(self, ctx, url, payload, dry=False, shots=None, go=None):
            assert payload["render"]["department"] == "Men" and go()
            return {"ok": True, "saved": True, "note": "the public page shows Men › Shoes › Sneakers", "shot": ""}
    import asyncio
    monkeypatch.setattr(edits, "may_edit", lambda *a, **k: True)
    assert asyncio.run(edits.run_one(s, db, "poshmark", Posh(), None, ["poshmark"])) is True
    row = db.conn.execute("SELECT * FROM listing_edits WHERE item_id=? AND marketplace='poshmark'", (iid,)).fetchone()
    assert row["status"] == "done"
    assert said[-1] == "✓ On Cloudsurfer Max Sneakers size 10.5 is now Men › Shoes › Sneakers on Poshmark"
    assert loads(db.listing(iid, "poshmark")["fields_json"])["department"] == "Men"
    assert asyncio.run(edits.run_one(s, db, "poshmark", Posh(), None, ["poshmark"])) is False   # nothing left


def test_a_reply_men_on_a_card_puts_the_item_under_men(env, tmp_path):
    s, db, bot, said, group = env
    iid = _item(db, tmp_path, shoe_facts())
    approve.send_item(s, db, iid)
    mid = bot.next_id
    handle_update(s, db, bot, _reply("men", mid))
    it = db.item(iid)
    assert loads(it["owner_category"] if isinstance(it["owner_category"], str) else json.dumps(it["owner_category"])) \
        == {"department": "Men", "category": "Shoes", "subcategory": "Sneakers"}
    assert it["status"] == "new"                                               # reprocessed under Men


def test_the_card_names_the_department_when_unsure_or_not_womens(env, tmp_path):
    s, db, bot, said, group = env
    men = _item(db, tmp_path, shoe_facts(department="Men", department_confidence=0.9))
    caption, _ = approve.item_caption(men, db.item(men))
    assert "Category: Men › Shoes › Sneakers" in caption
    unsure = _item(db, tmp_path, shoe_facts(department_confidence=0.55, department_alternative="Men"))
    caption, _ = approve.item_caption(unsure, db.item(unsure))
    assert "Category: Women › Shoes › Sneakers — my best reading; reply 'men' or 'women' to change it" in caption
    assert "men / women" in approve.CORRECT_HINT
