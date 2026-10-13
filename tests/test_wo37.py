"""WO37 — tonight's listings (2026-10-12): covers that show the whole item, no flat bars, a message once per item and
kind (a timeout counts as sent), shares whose photos come late, and a label's waist size. Pure functions and the
approval queue with a fake bot; no network, no browser."""
import pytest
from PIL import Image, ImageStat

from thrift_agent import approve, pipeline
from thrift_agent.brain import premium, sizes
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB
from thrift_agent.ingest import prep
from thrift_agent.schema import Ev, FrontOut, PhotoRole, View
from thrift_agent.telegram import Bot

CHAT, OWNER = 100, 7


def labels(front: int, *photos: tuple) -> FrontOut:
    """A cover check's labels: (photo, view, whole, cut_off, fill[, held])."""
    views = [View(photo=p[0], view=p[1], whole=p[2], cut_off=p[3], fill=p[4], held=p[5] if len(p) > 5 else False)
             for p in photos]
    return FrontOut(views=views, front=front)


def roles(*names: str) -> list[PhotoRole]:
    return [PhotoRole(photo=i, role=r) for i, r in enumerate(names)]


# ---------------------------------------------------------------- the cover: the whole item, from the front

# Tonight's five kinds (the photos and their expected covers are in the private repo's fixtures: private/tests/
# fixtures/wo37_covers): the labels a check gives, and the cover the owner's rule picks.
TONIGHT = {
    # UGG chestnut slippers: held up from behind (the cover live), a side view of the whole pair, sole, label, top
    "ugg": (dict(category="Shoes", item_type="platform slippers"),
            labels(0, (0, "back", True, False, 0.6, True), (1, "back", True, False, 0.5, True),
                   (2, "side", True, False, 0.55, True), (3, "sole", True, False, 0.8),
                   (4, "label", False, True, 0.7), (5, "detail", False, True, 0.9)), 2, None),
    # Schutz booties: toes close up (the cover live), heel, toes, a look into the shafts, soles, labels, the zip —
    # nothing whole from the side: the one showing the most of the pair, and the line to the private chat
    "schutz": (dict(category="Shoes", item_type="pointed-toe ankle booties"),
               labels(1, (0, "back", True, False, 0.55, True), (1, "detail", False, True, 0.9),
                      (2, "detail", False, True, 0.9), (3, "detail", False, True, 0.85), (4, "top", True, False, 0.5),
                      (5, "sole", False, True, 0.6), (6, "label", False, True, 0.7), (7, "inside", False, True, 0.8),
                      (8, "detail", False, True, 0.8)), 0, pipeline.NO_FULL_COVER),
    # Zadig & Voltaire boots: held from the side (whole) — fine as it was
    "zadig": (dict(category="Shoes", item_type="ankle boots"),
              labels(0, (0, "side", True, False, 0.55, True), (1, "three_quarter", True, False, 0.5, True),
                     (2, "back", True, False, 0.5, True), (3, "label", False, True, 0.8),
                     (4, "top", False, True, 0.7), (5, "worn", True, False, 0.4)), 0, None),
    # Tory Burch loafers: looked down into (the cover live), the pair 3/4 from the front (held), one shoe from the side,
    # the sole, from above again, held from behind twice, one shoe from the side
    "tory": (dict(category="Shoes", item_type="block heel loafers"),
             labels(0, (0, "top", True, False, 0.6), (1, "three_quarter", True, False, 0.6, True),
                    (2, "side", False, False, 0.5, True), (3, "sole", False, False, 0.5, True),
                    (4, "top", True, False, 0.6), (5, "back", True, False, 0.55, True),
                    (6, "back", True, False, 0.5, True), (7, "side", False, False, 0.5, True)), 1, None),
    # the Zara vest (photos in the fixture's order): a close-up of the front (the cover live, sideways), the inside
    # label, then — late — the whole front flat lay, a pocket close-up, the back
    "vest": (dict(category="Shirts & Tops", department="Kids", item_type="textured vest"),
             labels(0, (0, "detail", False, True, 0.95), (1, "inside", False, True, 0.8), (2, "front", True, False, 0.6),
                    (3, "detail", False, True, 0.9), (4, "back", True, False, 0.6)), 2, None),
    # the lululemon leggings: the care label held in a hand twice (the cover live), then — late — the whole flat lay and
    # the back close up
    "leggings": (dict(category="Pants & Jumpsuits", item_type="leggings"),
                 labels(0, (0, "label", False, True, 0.7, True), (1, "label", False, True, 0.7, True),
                        (2, "front", True, False, 0.55), (3, "back", False, True, 0.9)), 2, None),
}


@pytest.mark.parametrize("case", sorted(TONIGHT))
def test_tonights_covers_by_the_owners_rule(facts, case):
    """Never a label, a close-up, a look into shoes, a cut-off photo or a hand holding the item up from behind; the
    whole item from the front (shoes: the side or 3/4) wins — even over the model's own pick."""
    kw, check, cover, note = TONIGHT[case]
    n = len(check.views)
    f = facts(photo_roles=roles(*["other"] * n), cover_photo=0, photo_order=list(range(n)), **kw)
    pick, _, said = pipeline.choose_cover(f, n, None, check)
    assert (pick, said) == (cover, note)


@pytest.mark.parametrize("views,cover,note", [
    # clothing: a front flat lay or hanger shot, whole; worn only when there is none
    ([(0, "worn", True, False, 0.4), (1, "front", True, False, 0.5)], 1, None),
    ([(0, "worn", True, False, 0.4), (1, "detail", False, True, 0.9)], 0, None),
    # the front cut off at an edge is no cover while a whole one exists, however much more it fills
    ([(0, "front", False, True, 0.95), (1, "front", True, False, 0.4)], 1, None),
    # nothing whole: the one showing the most of the item, never a label — and the note
    ([(0, "label", False, True, 0.9), (1, "detail", False, True, 0.8), (2, "front", False, True, 0.6)], 2,
     pipeline.NO_FULL_COVER),
])
def test_the_rule_for_clothing(facts, views, cover, note):
    n = len(views)
    f = facts(category="Tops", photo_roles=roles(*["other"] * n), cover_photo=0, photo_order=list(range(n)))
    assert pipeline.choose_cover(f, n, None, labels(views[0][0], *views))[::2] == (cover, note)


def test_the_owners_cover_wins_and_the_rest_follow_in_her_order(facts):
    """'cover N' wins over the rule; after the cover: other fronts, back, side, top, worn, details, labels, flaws."""
    check = labels(0, (0, "label", False, True, 0.7), (1, "back", True, False, 0.5), (2, "front", True, False, 0.5),
                   (3, "flaw", False, True, 0.9), (4, "worn", True, False, 0.4), (5, "detail", False, True, 0.8))
    f = facts(photo_roles=roles(*["other"] * 6), cover_photo=0, photo_order=list(range(6)))
    assert pipeline.choose_cover(f, 6, None, check, owner=4)[0] == 4
    assert pipeline.labelled_order(check, 6) == [2, 1, 4, 5, 0, 3]


def test_a_check_from_before_keeps_its_old_reading(facts):
    """A stored WO23 comparison (front / back / side only, no labels of whole) is read as before."""
    old = FrontOut(views=[View(photo=0, view="back", design="none"), View(photo=1, view="front", design="some")],
                   front=1)
    f = facts(photo_roles=roles("back", "front"), cover_photo=1, photo_order=[1, 0])
    assert not pipeline.labelled(old) and pipeline.choose_cover(f, 2, None, old)[0] == 1


# ---------------------------------------------------------------- the cover file: whole, no flat bars

def _photo(path, w, h):
    im = Image.new("RGB", (w, h))
    for x in range(w):                                               # a gradient: no flat colour anywhere
        for y in range(0, h, 7):
            im.putpixel((x, y), (x * 255 // w, y * 255 // h, 120))
    im = im.resize((w, h))
    im.save(path)
    return path


def test_a_wide_photo_is_extended_with_its_own_background_never_flat_bars(tmp_path):
    src = _photo(tmp_path / "wide.jpg", 400, 300)
    out = prep.portrait_cover(src, tmp_path / "cover.jpg", 300, 400)
    with Image.open(out) as im:
        assert im.size == (300, 400)
        band = im.crop((0, 0, 300, 40))
        assert len(band.getcolors(300 * 40) or range(1000)) > 50              # no single flat colour
        middle = im.crop((0, 87, 300, 312)).resize((40, 30))                  # the whole photo, nothing cut
    with Image.open(src) as orig:
        diff = ImageStat.Stat(Image.blend(orig.convert("RGB").resize((40, 30)), middle, 0.5)).mean
    assert all(v > 0 for v in diff)


def test_a_3_4_photo_is_only_resized(tmp_path):
    src = _photo(tmp_path / "tall.jpg", 300, 400)
    out = prep.portrait_cover(src, tmp_path / "cover.jpg", 600, 800)
    with Image.open(out) as im, Image.open(src) as orig:
        assert im.size == (600, 800)
        a, b = im.resize((30, 40)).tobytes(), orig.convert("RGB").resize((30, 40)).tobytes()
        assert max(abs(x - y) for x, y in zip(a, b)) < 40


def test_a_turned_cover_stays_whole(tmp_path):
    src = _photo(tmp_path / "side.jpg", 300, 400)                           # a 3:4 photo turned a quarter: wide
    out = prep.portrait_cover(src, tmp_path / "cover.jpg", 300, 400, rotate=90)
    with Image.open(out) as im:
        assert im.size == (300, 400) and len(im.crop((0, 0, 300, 50)).getcolors(300 * 50) or range(1000)) > 50


# ---------------------------------------------------------------- one message per item and kind

class TimeoutBot(Bot):
    """sendPhoto times out once (the request went out, the answer never came — or not, with `refused`)."""

    def __init__(self, refused=False):
        super().__init__("TOK", CHAT, {OWNER})
        self.calls, self.next_id, self.fail, self.refused = [], 10, True, refused

    def call(self, method, **params):
        self.calls.append((method, params))
        if method in ("sendPhoto", "sendMessage") and self.fail:
            self.fail = False
            kind = "ConnectError: [Errno 54] Connection reset by peer" if self.refused else \
                "ReadTimeout: The read operation timed out"
            raise RuntimeError(f"telegram {method}: {kind}")
        if method in ("sendMessage", "sendPhoto"):
            self.next_id += 1
            return {"message_id": self.next_id}
        return [] if method == "getUpdates" else True


@pytest.fixture
def env(tmp_path, monkeypatch):
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    data["telegram"] = {**base.get("telegram", {}), "enabled": True}
    s = Settings(data)
    s.ensure_dirs()
    return s, DB(s.path("db"))


def _card_item(db, tmp_path, facts):
    bid = db.add_batch("share", 3)
    db.set_batch(bid, status="split")
    d = tmp_path / "item"
    (d / "photos").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (30, 40), "red").save(d / "cover.jpg")
    iid = db.add_item(bid, 1, str(d))
    db.set_item(iid, status="awaiting_price", facts=facts().model_dump(),
                price={"target": 30, "list_price": 40, "source": "brand", "by_marketplace": {"poshmark": 40},
                       "basis": "brand"},
                renders={"poshmark": {"marketplace": "poshmark", "title": "Flats", "size": "7.5", "price": 40}},
                gate={"decision": "publish", "reasons": [], "questions": [], "notes": []})
    return iid


@pytest.mark.parametrize("refused,photos_sent", [(False, 1), (True, 2)])
def test_a_card_whose_send_timed_out_is_never_sent_again(env, tmp_path, facts, monkeypatch, refused, photos_sent):
    """Live 2026-10-12: three sendPhoto calls timed out on the hotel Wi-Fi and the owner got four copies of one
    question. A send that went out without an answer counts as sent (a placeholder in the outbox) — the queue moves on,
    a restart sends nothing; a connection that never opened is taken back and sent on the next pump."""
    s, db = env
    bot = TimeoutBot(refused=refused)
    monkeypatch.setattr(approve, "bot_for", lambda _s: bot)
    iid = _card_item(db, tmp_path, facts)
    try:
        approve.pump(s, db)
    except RuntimeError:
        pass                                                        # the worker logs it and pumps again
    approve.pump(s, db)
    approve.resend_pending(s, db)
    assert [m for m, _ in bot.calls].count("sendPhoto") == photos_sent
    rows = db.conn.execute("SELECT * FROM outbox WHERE ref=?", (iid,)).fetchall()
    assert len(rows) == 1 and (rows[0]["message_id"] < 0) == (not refused)


def test_a_rerun_and_a_restart_send_nothing_twice(env, tmp_path, facts, monkeypatch):
    s, db = env
    bot = TimeoutBot()
    bot.fail = False
    monkeypatch.setattr(approve, "bot_for", lambda _s: bot)
    iid = _card_item(db, tmp_path, facts)
    approve.pump(s, db)
    db.outbox_resolve("item", iid)                                  # the card closed (a correction, a reprocessing)
    approve.pump(s, db)                                             # it waits again: edited and reopened, not resent
    approve.resend_pending(s, db)                                   # a restart
    methods = [m for m, _ in bot.calls]
    assert methods.count("sendPhoto") == 1 and "editMessageMedia" in methods
    assert approve.open_message(db)["ref"] == iid


# ---------------------------------------------------------------- shares whose photos come late

def test_a_share_is_whole_only_once_its_photos_are_here(tmp_path, monkeypatch):
    s = settings()
    d = tmp_path / "share"
    d.mkdir()
    (d / "_done.txt").write_text("done 3", encoding="utf-8")
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(pipeline.time, "time", lambda: clock["now"])
    monkeypatch.setattr(pipeline, "arrived", lambda p: 1_000_000.0 - 300)         # all here 5 minutes ago
    marker = d / "_done.txt"
    assert pipeline.expected_photos(marker) == 3
    assert not pipeline.share_complete(s, d, marker, 60)                         # none of the 3 photos yet
    for i in range(2):
        Image.new("RGB", (30, 40)).save(d / f"IMG_{i}.jpeg")
    assert not pipeline.share_complete(s, d, marker, 60)                         # 2 of 3
    Image.new("RGB", (30, 40)).save(d / "IMG_2.jpeg")
    assert pipeline.share_complete(s, d, marker, 60)                             # all 3: .jpeg too
    marker.write_text("done", encoding="utf-8")                                  # no count: photos, and quiet
    monkeypatch.setattr(pipeline, "arrived", lambda p: clock["now"] - (10 if p.name == "IMG_2.jpeg" else 300))
    assert not pipeline.share_complete(s, d, marker, 60)                         # a photo just came: wait
    for p in d.glob("IMG_*"):
        p.unlink()
    monkeypatch.setattr(pipeline, "arrived", lambda p: clock["now"] - 600)
    assert not pipeline.share_complete(s, d, marker, 60)                         # no photo, 10 min: still waiting
    monkeypatch.setattr(pipeline, "arrived", lambda p: clock["now"] - 31 * 60)
    assert pipeline.share_complete(s, d, marker, 60)                             # 30 min: taken (and said)


def test_photos_that_come_after_an_empty_share_send_it_again(env, monkeypatch):
    s, db = env
    said = []
    monkeypatch.setattr(pipeline.notify, "say", said.append)
    archived = s.path("archive") / "20261012_2026-10-12_205751"
    archived.mkdir(parents=True)
    bid = db.add_batch(str(s.path("inbox") / "2026-10-12_205751"), 0)
    db.set_batch(bid, status="failed")
    db.log(bid, "archived", {"dest": str(archived)})
    assert pipeline.late_photos(s, db) == []                                     # nothing came yet
    for i in (1296, 1297, 1298):
        Image.new("RGB", (30, 40)).save(archived / f"IMG_{i}.jpeg")
    assert pipeline.late_photos(s, db) == [f"{bid}: retaken"]
    assert (s.path("inbox") / "2026-10-12_205751-late").is_dir() and not archived.exists()
    assert said and said[0].startswith("📸 3 photos came late")
    assert pipeline.late_photos(s, db) == []                                     # once


def test_photos_that_come_after_an_item_was_made_are_told_once(env, monkeypatch):
    s, db = env
    said = []
    monkeypatch.setattr(pipeline.notify, "say", said.append)
    archived = s.path("archive") / "20261012_2026-10-12_205814"
    archived.mkdir(parents=True)
    for i in (1300, 1301):
        Image.new("RGB", (30, 40)).save(archived / f"IMG_{i}.jpeg")
    bid = db.add_batch(str(s.path("inbox") / "2026-10-12_205814"), 2)
    db.set_batch(bid, status="split")
    db.log(bid, "archived", {"dest": str(archived)})
    iid = db.add_item(bid, 1, str(s.path("work") / "item"))
    db.set_item(iid, renders={"poshmark": {"title": "lululemon Printed Leggings size 2"}})
    assert pipeline.late_photos(s, db) == []
    Image.new("RGB", (30, 40)).save(archived / "IMG_1299.jpeg")                 # the full flat lay, late
    assert pipeline.late_photos(s, db) == [f"{bid}: 1 late"]
    assert said == ["📸 1 photos of lululemon Printed Leggings size 2 came after it was taken (IMG_1299.jpeg) — the "
                    "listing doesn't have them"]
    assert pipeline.late_photos(s, db) == []


# ---------------------------------------------------------------- titles and sizes

def test_a_label_waist_is_the_size_listed(facts):
    """Live: FRAME jeans labelled 24 went up as 'size 00' (the model's conversion); Poshmark's menu has 24."""
    jeans = facts(category="Jeans", subcategory="Straight Leg",
                  size_printed=Ev(value="24", photos=[2], source="photo", confidence=0.95),
                  size_us=Ev(value="00", photos=[2], source="derived", confidence=0.8))
    f = pipeline.settle_waist(jeans)
    assert sizes.poshmark_size(f) == ("Standard", "24") and sizes.title_size(f) == "size 24"
    tee = facts(category="Tops", size_printed=Ev(value="24", photos=[2], source="photo", confidence=0.9))
    assert pipeline.settle_waist(tee) == tee                                     # not a waist on a top


def test_a_collab_that_is_the_brand_is_never_said_twice(facts):
    """Live: "adidas by Stella McCartney adidas by Stella McCartney Running Sneakers Neon" — the label read named the
    brand as a collab, and the 80-character cut then took the size."""
    from thrift_agent.schema import Premium
    p = Premium.model_validate({"collab": {"value": "adidas by Stella McCartney", "photos": [4], "source": "photo",
                                           "confidence": 0.75}})
    f = facts(brand=Ev(value="adidas by Stella McCartney", photos=[1], source="photo", confidence=0.95),
              item_type="knit running sneakers", colors=["Orange"], premium=p,
              size_us=Ev(value="8", photos=[4], source="photo", confidence=0.95))
    title = premium.title_with_feature("adidas by Stella McCartney Knit Running Sneakers Neon Orange size 8", f,
                                       premium.config())
    assert title == "adidas by Stella McCartney Knit Running Sneakers Neon Orange size 8"
    collab = Premium.model_validate({"collab": {"value": "H&M x Erdem", "photos": [4], "source": "photo",
                                                "confidence": 0.9}})
    assert premium.collab(facts(brand=Ev(value="H&M", photos=[1], source="photo", confidence=0.9),
                                premium=collab)) == "x Erdem"


def test_the_80_character_cut_never_takes_the_brand_or_the_size():
    keep = ["Long Brand Name Here", "", "size 8", "Sneakers", "Orange"]
    title = premium.fit_title("Long Brand Name Here Extremely Long Feature Words Running Sneakers Neon Orange "
                              "Bright size 8", keep)
    assert len(title) <= 80 and title.startswith("Long Brand Name Here") and title.endswith("size 8")


def test_routine_notes_stay_out_of_the_private_chat():
    from thrift_agent.crosslist import noteworthy
    assert noteworthy("category menu: Women > Bottoms › Skirts; removed 'White' from colour-input (Depop's own "
                      "suggestion); after the click: form page: refused: There was a problem listing your product; "
                      "looked up: {\"listings\": 24}") is None
    assert noteworthy("colour-input: no option 'Teal' (it offers: Black) — left empty") is not None


def test_no_pause_between_listings_on_any_site():
    """WO37 §4 (the owner): no gap after a listing on Poshmark, Depop or Vinted, and no occasional longer break; the
    daily cap, the hours and the stops stay (test_parallel runs three items on three sites back to back)."""
    from thrift_agent import crosslist
    from thrift_agent.scheduler import next_gap
    s = settings()
    assert {next_gap(s["schedule"]) for _ in range(200)} == {0.0}
    assert crosslist.listing_gap(s, "depop") == crosslist.listing_gap(s, "vinted") == 0.0
    assert s.get("schedule.daily_cap") == 25


def test_a_title_never_carries_a_size_no_photo_shows(facts):
    """Live: the Clarks' title said "size 8" though no photo shows a size — the card read as if it had one."""
    from thrift_agent.brain import copy
    none = Ev(value=None, photos=[], source="none", confidence=0.0)
    f = facts(size_us=none, size_printed=none)
    assert copy.ensure_title_size("Clarks Taupe Brown Ankle Boots Buckle Strap Block Heel size 8", f) == \
        "Clarks Taupe Brown Ankle Boots Buckle Strap Block Heel"
    assert copy.ensure_title_size("Clarks Ankle Boots size 7", facts()) == "Clarks Ankle Boots size 7.5"


def test_no_size_is_one_line_for_the_item_and_a_size_puts_its_sites_back_in_line(env, tmp_path, facts, monkeypatch):
    from thrift_agent import crosslist
    s, db = env
    said = []
    monkeypatch.setattr(crosslist.notify, "say", said.append)
    iid = _card_item(db, tmp_path, facts)
    db.set_item(iid, status="ready", renders={
        mp: {"marketplace": mp, "title": "Clarks Ankle Boots", "description": "Brown suede ankle boots.", "size": None,
             "price": 25} for mp in ("poshmark", "depop", "vinted")})
    for mp in ("depop", "vinted"):
        crosslist.skip_unmappable(db, iid, mp, "Clarks Ankle Boots", crosslist.NO_SIZE)
    assert set(said) == {"⏭ Clarks Ankle Boots: no size on the item — it goes up once you reply 'size …' to its card"}
    db.upsert_listing(iid, "poshmark", status="failed", error="PosterError: the Share Listing panel did not show")
    pipeline.set_size(s, db, iid, "8")         # (an unconfirmed publish anywhere: refused before — _answerable)
    rows = {r["marketplace"]: (r["status"], r["error"]) for r in
            db.conn.execute("SELECT * FROM listings WHERE item_id=?", (iid,))}
    assert rows == {"depop": ("queued", None), "vinted": ("queued", None), "poshmark": ("queued", None)}


@pytest.mark.parametrize("item_type,sub,department,owner,want", [
    ("croc-embossed block heel loafers", "Heels", "Women", None, "Flats & Loafers"),     # tonight's Tory Burch
    ("penny loafers", "Pumps", "Women", None, "Flats & Loafers"),
    ("suede loafers", "Heels", "Men", None, "Loafers & Slip-Ons"),
    ("block heel pumps", "Heels", "Women", None, "Heels"),                              # pumps stay
    ("block heel loafers", "Heels", "Women", '{"category": "Shoes", "subcategory": "Heels"}', "Heels"),   # her word
])
def test_loafers_are_never_heels(facts, item_type, sub, department, owner, want):
    f = facts(item_type=item_type, subcategory=sub, department=department)
    assert pipeline.settle_shoe_kind(f, owner).subcategory == want


@pytest.mark.parametrize("error,maybe", [
    ("telegram sendPhoto: ReadTimeout: The read operation timed out", True),         # tonight: delivered 3 times
    ("telegram sendPhoto: RemoteProtocolError: Server disconnected without sending a response.", True),
    ("telegram sendPhoto: WriteTimeout: The write operation timed out", False),     # never reached Telegram whole
    ("telegram sendPhoto: ConnectError: [Errno 8] nodename nor servname provided", False),
    ("telegram sendPhoto failed: Bad Request: wrong file identifier", False),
])
def test_only_a_request_that_went_out_whole_counts_as_maybe_sent(error, maybe):
    from thrift_agent.telegram import maybe_delivered
    assert maybe_delivered(RuntimeError(error)) is maybe


def test_a_cover_fix_finds_the_right_photo_in_each_live_listing(env, tmp_path, facts):
    """WO37 §2: each site's photos as posted (Poshmark's render, Depop's and Vinted's form fields): where the right photo
    is among them — a listing that doesn't have it is left for the owner."""
    from pathlib import Path

    from thrift_agent import coverfix
    s, db = env
    iid = _card_item(db, tmp_path, facts)
    d = Path(db.item(iid)["dir"])
    for i in range(4):
        Image.new("RGB", (30, 40), "blue").save(d / "photos" / f"{i:02d}.jpg")
    paths = [str(d / "cover.jpg")] + [str(d / "photos" / f"{i:02d}.jpg") for i in (2, 1, 3)]
    db.set_item(iid, status="posted", renders={"poshmark": {"title": "UGG", "photos": paths}})
    db.upsert_listing(iid, "poshmark", status="posted", url="https://poshmark.com/listing/UGG-6acd8ea6ce5b0ac5ccde36eb")
    db.upsert_listing(iid, "depop", status="posted", url="https://www.depop.com/products/x-ugg-87d9/",
                      fields_json={"photos": paths})
    db.upsert_listing(iid, "vinted", status="posted", url="https://www.vinted.com/items/1",
                      fields_json={"photos": paths[:2]})
    plans = coverfix.plans(db, iid, d / "photos" / "01.jpg")
    assert [(p.mp, p.position, len(p.photos)) for p in plans] == [("poshmark", 2, 4), ("depop", 2, 4),
                                                                   ("vinted", None, 2)]
    assert [p.mp for p in coverfix.plans(db, iid, d / "photos" / "01.jpg", ["depop"])] == ["depop"]
    assert coverfix.right_photo(s, db, iid, 3)[0] == 2                                   # her 3rd photo
    with pytest.raises(ValueError, match="there is no photo 9"):
        coverfix.right_photo(s, db, iid, 9)
