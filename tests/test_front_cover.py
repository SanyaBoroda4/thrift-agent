"""WO23: the cover is the FRONT of the item (a comparison, checked in code, turned upright); a department is never the
category; kids sizes from the label's cm or age; "cover N"; thrift recover. The model is stubbed: no network."""
from pathlib import Path

import pytest
from PIL import Image

from thrift_agent import approve, pipeline
from thrift_agent.brain.sizes import KIDS_BY_CM, KIDS_BY_YEARS, kids_clothing_size
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB, loads
from thrift_agent.ingest import prep
from thrift_agent.schema import CopyOut, Ev, Flaw, FrontOut, PhotoRole, VerifyOut, View
from thrift_agent.telegram import Bot

RED, WHITE = (220, 30, 30), (245, 245, 245)


def roles(*names):
    return [PhotoRole(photo=i, role=r) for i, r in enumerate(names)]


def check(front, *views):
    """views: (photo, view, design, upright)."""
    return FrontOut(views=[View(photo=p, view=v, design=d, upright=u) for p, v, d, u in views], front=front)


def tee(facts, **kw):
    """The live Lacoste kids tee (b_261003_8dea57): #0 the printed front lying sideways, #1 the front close-up with the
    logo, #2 the care/size label, #3 the plain back; a small stain cited on the front photo."""
    base = dict(item_type="graphic tee", department="Kids", category="Kids", subcategory="Shirts & Tops",
                kids_gender="boys", kids_gender_confidence=0.9,
                size_printed=Ev(value="4 ans / 104 cm", photos=[1, 2], source="photo", confidence=0.9),
                size_us=Ev(value="4T", photos=[1], source="derived", confidence=0.6),
                photo_roles=roles("front", "label", "label", "back"), cover_photo=0, photo_order=[0, 1, 2, 3],
                flaws=[Flaw(description="small stain near the hem", photos=[0])])
    base.update(kw)
    return facts(**base)


LACOSTE_CHECK = check(0, (0, "front", "strong", 90), (3, "back", "none", 90))


# ---------- the cover ----------

def test_the_lacoste_case_the_sideways_printed_front_not_the_plain_back(facts):
    """Live: #3 (the back) was the cover and the card said "no front flat-lay photo": the stain cited on #0 had taken
    the only front out of the running. A front with a small flaw stays the cover; the turn makes it upright."""
    assert pipeline.choose_cover(tee(facts), 4, None, LACOSTE_CHECK) == (0, 90, None)
    assert pipeline.choose_cover(tee(facts), 4) == (0, 0, None)                       # no check: the roles agree
    wrong = check(3, (0, "front", "strong", 90), (3, "back", "none", 0))              # a check that picks the back...
    assert pipeline.choose_cover(tee(facts), 4, None, wrong) == (0, 90, None)          # ...is overruled in code


@pytest.mark.parametrize("names,front_check,cover,note", [
    (("front", "back"), check(0, (0, "front", "some", 0), (1, "back", "none", 0)), 0, None),          # a shirt
    (("back", "front"), check(1, (0, "back", "some", 0), (1, "front", "some", 0)), 1, None),          # pants: back
    (("front", "back"), check(1, (0, "front", "some", 0), (1, "back", "some", 0)), 0, None),          # pockets = back
    (("front", "back"), check(1, (0, "back", "none", 0), (1, "front", "none", 0)), 1, None),          # a plain dress
    (("side", "back", "detail"), check(0, (0, "side", "some", 0), (1, "back", "none", 0)), 0, None),  # shoes: profile
    (("worn", "front", "back"), check(0, (1, "front", "some", 0), (2, "back", "none", 0)), 1, None),  # a try-on never
    (("front", "back"), check(0, (0, "front", "none", 0), (1, "back", "strong", 0)), 1, None),        # printed side
    (("back", "worn", "label"), check(0, (0, "back", "none", 0)), 0, "nf"),                           # only a back
    (("worn", "label"), None, None, "nf"),                                                              # nothing alone
])
def test_the_cover_is_a_comparison_checked_in_code(facts, names, front_check, cover, note):
    f = facts(photo_roles=roles(*names), cover_photo=0, photo_order=list(range(len(names))))
    pick, _, said = pipeline.choose_cover(f, len(names), None, front_check)
    assert pick == cover and said == (pipeline.NO_FRONT_COVER if note else None)


def test_a_screenshot_or_a_flaw_close_up_is_never_the_cover(facts):
    f = facts(photo_roles=roles("front", "flaw", "front"), cover_photo=1, photo_order=[1, 0, 2])
    assert pipeline.choose_cover(f, 3, ["retail", "own", "own"], check(1, (2, "front", "some", 0))) == (2, 0, None)


def test_the_owners_cover_n_wins(facts):
    assert pipeline.choose_cover(tee(facts), 4, None, LACOSTE_CHECK, owner=3) == (3, 90, None)
    assert pipeline.choose_cover(tee(facts), 4, None, LACOSTE_CHECK, owner=2) == (2, 0, None)


def _sideways_front(path: Path) -> Path:
    """A landscape photo of a tee laid sideways: the collar (red) at the left."""
    im = Image.new("RGB", (400, 300), WHITE)
    im.paste(RED, (0, 0, 60, 300))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)
    return path


def test_a_sideways_cover_is_turned_upright_never_cropped(tmp_path):
    src = _sideways_front(tmp_path / "00.jpg")
    with Image.open(prep.portrait_cover(src, tmp_path / "cover.jpg", 1200, 1600, rotate=90)) as cover:
        assert cover.size == (1200, 1600)
        assert cover.getpixel((600, 100))[1] < 100 and cover.getpixel((600, 1200))[1] > 200   # the collar on top
    with Image.open(prep.portrait_cover(src, tmp_path / "flat.jpg", 1200, 1600)) as flat:
        assert flat.getpixel((40, 800))[1] < 100                      # unturned: the collar stays at the left


def test_exif_orientation_is_applied_before_any_model_sees_a_photo(tmp_path):
    im = Image.new("RGB", (400, 300), WHITE)
    im.paste(RED, (0, 0, 400, 40))                                    # stored with the red band on top...
    exif = Image.Exif()
    exif[0x0112] = 6                                                  # ...and "turn 90° clockwise to show"
    im.save(tmp_path / "raw.jpg", exif=exif)
    out = prep.normalize(tmp_path / "raw.jpg", tmp_path / "work.jpg", 2048)
    with Image.open(out) as upright:
        assert upright.size == (300, 400) and upright.getexif().get(0x0112) is None
        assert upright.getpixel((290, 200))[1] < 100                  # the band is now on the right, as displayed


# ---------- kids sizes ----------

@pytest.mark.parametrize("printed,size", [
    ("4 ans / 104 cm", "4T"), ("104 cm", "4T"), ("Gr. 104", "4T"), ("104", "4T"), ("4A", "4T"), ("4 years", "4T"),
    ("98 cm", "3T"), ("110 cm / 5 ans", "5T"), ("116", "6"), ("122 cm", "7"), ("128 cm 8 ans", "8"), ("5-6 Y", "6"),
    ("134 cm", "10"), ("9 years", "10"), ("152 cm / 12 ans", "12"), ("18 mois", "18 Months"), ("62 cm", "3 Months"),
    ("4", None), ("M", None), ("3T", None), ("EU 38", None), ("", None), (None, None),
])
def test_the_kids_size_table(printed, size):
    assert kids_clothing_size(printed) == size


def test_the_table_only_uses_poshmarks_own_kids_labels():
    """Poshmark's Kids size filter (public, read 2026-10-04): Baby months, 2T-5T, 6, 7, 8, 10, 12, 14, 16."""
    allowed = {"Newborn", "0-3 Months", "3 Months", "6 Months", "9 Months", "12 Months", "18 Months", "24 Months",
               "2T", "3T", "4T", "5T", "6", "7", "8", "10", "12", "14", "16"}
    assert {s for _, s in KIDS_BY_CM} <= allowed and set(KIDS_BY_YEARS.values()) <= allowed


def test_a_kids_label_with_cm_or_age_settles_the_size(tmp_path, facts):
    s = _settings(tmp_path)
    f = pipeline.settle_kids_size(s, tee(facts, category="Shirts & Tops"))
    assert (f.size_us.value, f.size_us.confidence, f.size_us.source) == ("4T", 0.95, "derived")
    shoe = tee(facts, category="Shoes", size_printed=Ev(value="104 cm"))
    assert pipeline.settle_kids_size(s, shoe).size_us.value == "4T" and pipeline.settle_kids_size(s, shoe) == shoe
    adult = facts(size_printed=Ev(value="104 cm"))
    assert pipeline.settle_kids_size(s, adult) == adult


def test_a_bare_number_is_read_again_once_for_its_units(tmp_path, facts, monkeypatch):
    s = _settings(tmp_path)
    photos = [tmp_path / f"{i:02d}.jpg" for i in range(4)]
    seen = []
    monkeypatch.setattr(pipeline.cover_brain, "read_size_label", lambda p, m, e: seen.append(p) or "4 ans / 104 cm")
    f = pipeline.settle_kids_size(s, tee(facts, category="Shirts & Tops", size_printed=Ev(value="4", photos=[1, 2])),
                                  photos)
    assert f.size_us.value == "4T" and f.size_printed.value == "4 ans / 104 cm" and seen == [photos[1:3]]
    monkeypatch.setattr(pipeline.cover_brain, "read_size_label", lambda p, m, e: "4")     # it really says "4"
    f = pipeline.settle_kids_size(s, tee(facts, category="Shirts & Tops", size_printed=Ev(value="4", photos=[1])),
                                  photos)
    assert f.size_us.confidence == 0.6                                # still unsure: asked, as before


# ---------- end to end: the tee through process_item ----------

def _settings(tmp_path):
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    s = Settings(data)
    s.ensure_dirs()
    return s


def _tee_item(s, db, tmp_path):
    bid = db.add_batch("share", 4)
    db.set_batch(bid, status="split")
    d = tmp_path / "item"
    _sideways_front(d / "photos" / "00.jpg")
    for i, c in ((1, "navy"), (2, "white"), (3, "gray")):
        Image.new("RGB", (300, 400), c).save(d / "photos" / f"{i:02d}.jpg")
    return db.add_item(bid, 1, str(d))


def _ask(facts, front=LACOSTE_CHECK, copy_text="Lacoste kids graphic tee in heather gray."):
    def ask(model, system, content, out, tool, description, **kw):
        if out.__name__ == "Facts":
            return tee(facts)
        if out.__name__ == "FrontOut":
            return front
        if out is CopyOut:
            return CopyOut(poshmark_title="Lacoste Kids Graphic Tee Heather Gray size 4T",
                           poshmark_description=copy_text + "\nGently pre-loved, please see photos for condition.",
                           poshmark_style_tags=[], depop_description=copy_text + " Gently pre-loved, please see "
                                                                                 "photos for condition.",
                           depop_hashtags=["lacoste", "kids", "tee", "graphic", "gray"])
        if out is VerifyOut:
            return VerifyOut(poshmark_title="Lacoste Kids Graphic Tee Heather Gray size 4T",
                             poshmark_description=copy_text + "\nGently pre-loved, please see photos for condition.",
                             depop_description=copy_text + " Gently pre-loved, please see photos for condition.")
        raise AssertionError(out)
    return ask


def test_the_tee_end_to_end_front_upright_shirts_and_tops_4t_no_question(tmp_path, facts, monkeypatch):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _ask(facts))
    monkeypatch.setattr(pipeline.approve, "pump", lambda *a: None)
    iid = _tee_item(s, db, tmp_path)
    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    f, gate, posh = loads(it["facts"]), loads(it["gate"]), loads(it["renders"])["poshmark"]
    assert (f["cover_photo"], f["cover_upright"]) == (0, 90)
    assert [Path(p).name for p in posh["photos"]] == ["cover.jpg", "01.jpg", "02.jpg", "03.jpg"]
    with Image.open(posh["photos"][0]) as cover:
        assert cover.getpixel((600, 100))[1] < 100                    # the collar on top: upright
    assert (posh["category"], posh["subcategory"], posh["size"]) == ("Shirts & Tops", None, "4T")
    assert gate["questions"] == [] and pipeline.NO_FRONT_COVER not in gate["notes"]
    assert posh["description"].rstrip().endswith("Label size: 4 ans / 104 cm.")
    assert loads(it["views"])["front"] == 0


# ---------- "cover N" ----------

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


def _bot_env(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    s.data["telegram"] = {**s.data.get("telegram", {}), "enabled": True}
    db = DB(s.path("db"))
    bot = FakeBot()
    monkeypatch.setattr(approve, "bot_for", lambda _s: bot)
    return s, db, bot


def _processed_tee(s, db, tmp_path, facts, monkeypatch):
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _ask(facts))
    iid = _tee_item(s, db, tmp_path)
    pipeline.process_item(s, db, iid)
    return iid


def _msg(text, reply_to=None, mid=60):
    m = {"message_id": mid, "chat": {"id": CHAT}, "from": {"id": OWNER}, "text": text}
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": 5, "message": m}


def test_a_reply_cover_n_sets_the_cover_and_sends_the_card_again(tmp_path, facts, monkeypatch):
    s, db, bot = _bot_env(tmp_path, monkeypatch)
    iid = _processed_tee(s, db, tmp_path, facts, monkeypatch)
    card = bot.next_id                                                # the card went out after processing
    assert approve.handle_update(s, db, bot, _msg("cover 2", reply_to=card)) == f"cover {iid}: photo 2 (awaiting_price)"
    it = db.item(iid)
    assert it["owner_cover"] == 2 and loads(it["facts"])["cover_photo"] == 2
    assert [Path(p).name for p in loads(it["renders"])["poshmark"]["photos"]][:2] == ["cover.jpg", "00.jpg"]
    assert bot.calls[-2][1]["text"].startswith("✓ cover: photo 2")
    assert bot.calls[-1][0] == "sendPhoto" and bot.next_id == card + 2          # the card, again
    pipeline.process_item(s, db, iid)                                 # kept through any reprocessing
    assert loads(db.item(iid)["facts"])["cover_photo"] == 2


def test_cover_n_typed_while_the_card_is_open_and_its_limits(tmp_path, facts, monkeypatch):
    s, db, bot = _bot_env(tmp_path, monkeypatch)
    iid = _processed_tee(s, db, tmp_path, facts, monkeypatch)
    assert approve.handle_update(s, db, bot, _msg("Cover 3")).startswith(f"cover {iid}: photo 3")
    assert approve.handle_update(s, db, bot, _msg("cover 9")).startswith(f"cover {iid}: rejected 9")
    assert "has photos 0..3 — no photo 9" in bot.texts()[-1]
    db.set_item(iid, status="posted")
    db.upsert_post(iid, "poshmark", status="posted", url="https://poshmark.com/listing/x-0000000000000000000000a1")
    with pytest.raises(ValueError, match="already on the marketplace"):
        pipeline.set_cover(s, db, iid, 1)


# ---------- thrift recover ----------

def test_recover_fixes_cover_category_and_size_and_keeps_price_and_answers(tmp_path, facts, monkeypatch):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    monkeypatch.setattr(pipeline.approve, "pump", lambda *a: None)
    iid = _processed_tee(s, db, tmp_path, facts, monkeypatch)
    # the item as WO20 left it live: the back as cover, the department as category, 4T unsure, priced by the owner
    old = tee(facts, cover_photo=3)
    renders = loads(db.item(iid)["renders"])
    renders["poshmark"].update(category="Kids", subcategory="Shirts & Tops",
                               description="Lacoste kids graphic tee.\nGently pre-loved, please see photos for condition.")
    gate = {"decision": "needs_info", "reasons": ["category 'Kids' is not one of Poshmark's Kids categories"],
            "questions": ["category 'Kids' is not one of Poshmark's Kids categories — reply e.g. 'category "
                          "Accessories'", "Size: read as “4T”, not sure — reply 'size …' if it's wrong"],
            "notes": [pipeline.NO_FRONT_COVER], "hold": None, "ask_kids": False}
    price = {"target": 12, "list_price": 20, "source": "owner", "by_marketplace": {"poshmark": 20}, "basis": "owner"}
    db.set_item(iid, status="ready", owner_price=20, owner_condition="NWOT", owner_kids_gender="boys", views=None,
                facts=old.model_dump(), renders=renders, gate=gate, price=price)
    asked = []
    monkeypatch.setattr(pipeline.cover_brain, "front_check", lambda photos, m, e: asked.append([i for i, _ in photos])
                        or LACOSTE_CHECK)
    out = pipeline.recover_item(s, db, iid)
    assert (out["cover"], out["role"], out["view"], out["upright"]) == (0, "front", "front", 90)
    assert (out["category"], out["size"], out["questions"], out["status"]) == ("Shirts & Tops", "4T", [], "ready")
    assert asked == [[0, 3]]                                          # only the photos of the item alone compared
    it = db.item(iid)
    assert (it["owner_price"], it["owner_condition"], it["owner_kids_gender"]) == (20, "NWOT", "boys")
    assert loads(it["price"]) == price                                 # the price stays as it was
    gate = loads(it["gate"])
    assert gate["questions"] == [] and pipeline.NO_FRONT_COVER not in gate["notes"]
    posh = loads(it["renders"])["poshmark"]
    assert (posh["category"], posh["size"]) == ("Shirts & Tops", "4T") and "Label size: 4 ans / 104 cm." in posh[
        "description"]
    assert [Path(p).name for p in posh["photos"]] == ["cover.jpg", "01.jpg", "02.jpg", "03.jpg"]


def test_recover_sends_a_waiting_card_again_and_leaves_a_listed_item_alone(tmp_path, facts, monkeypatch):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    monkeypatch.setattr(pipeline.approve, "pump", lambda *a: None)
    iid = _processed_tee(s, db, tmp_path, facts, monkeypatch)
    db.add_outbox("-100", 7, "item", iid)
    monkeypatch.setattr(pipeline.cover_brain, "front_check", lambda photos, m, e: LACOSTE_CHECK)
    assert pipeline.recover_item(s, db, iid)["status"] == "awaiting_price"   # no price yet: its card comes again
    assert db.outbox_pending() == []
    db.set_item(iid, status="posted")
    with pytest.raises(ValueError, match="nothing to recover|on the marketplace"):
        pipeline.recover_item(s, db, iid)


def test_recover_command(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from thrift_agent import cli
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    bid = db.add_batch("share", 2)
    ids = [db.add_item(bid, k, f"item_{k}") for k in (1, 2)]
    monkeypatch.setattr(cli, "settings", lambda: s)
    monkeypatch.setattr(cli, "_db", lambda: db)
    monkeypatch.setattr(cli.approve, "pump", lambda *a: None)

    def recover(s_, db_, iid):
        if iid == ids[1]:
            raise ValueError(f"item {iid} is on the marketplace (poshmark posted) — left as it is")
        return {"cover": 0, "role": "front", "view": "front", "upright": 90, "category": "Shirts & Tops", "size": "4T",
                "questions": [], "status": "ready", "before": {"cover": 3, "category": "Kids", "size": "4T"}}
    monkeypatch.setattr(cli.pipeline, "recover_item", recover)
    r = CliRunner().invoke(cli.app, ["recover", bid], terminal_width=200)
    out = " ".join(r.output.split())
    assert r.exit_code == 0, r.output
    assert f"recovered {ids[0]}: cover #0 (front, front check: front, turned 90°) was #3; category Shirts & Tops was Kids" in out
    assert f"left as it is {ids[1]}" in out


def test_recover_never_writes_over_an_answer_that_came_in_meanwhile(tmp_path, facts, monkeypatch):
    """The owner prices cards while recover runs: an answer that lands between its read and its write wins."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    monkeypatch.setattr(pipeline.approve, "pump", lambda *a: None)
    iid = _processed_tee(s, db, tmp_path, facts, monkeypatch)

    def check_while_the_owner_answers(photos, m, e):
        import time
        time.sleep(1.1)                                               # updated_at has whole seconds
        pipeline.set_price(s, db, iid, 25)                            # the owner's price, meanwhile
        return LACOSTE_CHECK
    monkeypatch.setattr(pipeline.cover_brain, "front_check", check_while_the_owner_answers)
    with pytest.raises(ValueError, match="changed while it was being recovered"):
        pipeline.recover_item(s, db, iid)
    it = db.item(iid)
    assert it["status"] == "ready" and it["owner_price"] == 25 and loads(it["renders"])["poshmark"]["price"] == 25
