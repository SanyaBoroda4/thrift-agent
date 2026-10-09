"""WO25: sizes from Poshmark's catalog, "Which category?" with real paths as buttons, "no brand", `thrift reprocess`
with the card compared. The model is stubbed; no network."""
import pytest
from PIL import Image

from thrift_agent import approve, brands, pipeline
from thrift_agent.brain import copy as copywriter, sizes, taxonomy
from thrift_agent.brain.gate import SIZE_NOTE, evaluate
from thrift_agent.brain.verify import lint
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB, loads
from thrift_agent.schema import CategoryPath, CopyOut, Ev, PieceSize, PriceResult, VerifyOut
from thrift_agent.telegram import Bot

CHAT, OWNER = 100, 7
LINE = "Gently pre-loved, please see photos for condition."


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

    def sent(self):
        return [p for m, p in self.calls if m in ("sendMessage", "sendPhoto")]

    def marks(self):
        """The answered cards (WO29): (message_id, the "✓ …" label) of each editMessageReplyMarkup."""
        return [(p["message_id"], p["reply_markup"]["inline_keyboard"][0][0]["text"])
                for m, p in self.calls if m == "editMessageReplyMarkup"]

    def buttons(self):
        """The buttons of the last message that had any: [(text, callback_data)]."""
        last = next(p for p in reversed(self.sent()) if p.get("reply_markup"))
        return [(b["text"], b["callback_data"]) for row in last["reply_markup"]["inline_keyboard"] for b in row]


def _settings(tmp_path, telegram=False):
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    if telegram:
        data["telegram"] = {**base.get("telegram", {}), "enabled": True}
    s = Settings(data)
    s.ensure_dirs()
    return s


def _item(tmp_path, db):
    bid = db.add_batch("share", 3)
    db.set_batch(bid, status="split")
    d = tmp_path / "item"
    for i, color in enumerate(["red", "green", "blue"]):
        (d / "photos").mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (60, 80), color).save(d / "photos" / f"{i:02d}.jpg")
    return db.add_item(bid, 1, str(d))


def _model(state: dict, seen: list | None = None):
    """The stubbed model: Facts from state["facts"](), the copy from state["title"]; `seen` gets each seller note."""
    def ask(model, system, content, out, tool, description, **kw):
        if out.__name__ == "Facts":
            if seen is not None:
                seen.append(next(c["text"] for c in content if c.get("type") == "text"
                                 and c["text"].startswith(("Seller note", "No seller note"))))
            return state["facts"]()
        title = state.get("title", "Tory Burch Red Ballet Flats size 7.5")
        if out is CopyOut:
            return CopyOut(poshmark_title=title, poshmark_description=f"Red flats.\n{LINE}", poshmark_style_tags=[],
                           depop_description=f"red flats. {LINE}", depop_hashtags=["a", "b", "c", "d", "e"])
        if out is VerifyOut:
            return VerifyOut(poshmark_title=title, poshmark_description=f"Red flats.\n{LINE}",
                             depop_description=f"red flats. {LINE}")
        if out.__name__ == "FrontOut":
            return out(views=[], front=-1)
        if out.__name__ == "UprightOut":
            return out(upright="A")
        if out.__name__ == "SizeLabel":
            return out(printed=None)
        raise AssertionError(out)
    return ask


@pytest.fixture
def env(tmp_path, monkeypatch):
    s = _settings(tmp_path, telegram=True)
    db = DB(s.path("db"))
    bot = FakeBot()
    monkeypatch.setattr(approve, "bot_for", lambda _s: bot)
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml",
                        lambda name: {"brands": {"tory burch": {"target": 70}}, "aliases": {}, "category_defaults": {}})
    return s, db, bot


def _cb(data, mid):
    return {"update_id": 3, "callback_query": {"id": "cb", "from": {"id": OWNER}, "data": data,
                                               "message": {"message_id": mid, "chat": {"id": CHAT}}}}


def _reply(text, mid):
    return {"update_id": 4, "message": {"message_id": 90, "chat": {"id": CHAT}, "from": {"id": OWNER}, "text": text,
                                        "reply_to_message": {"message_id": mid}}}


# ---------- 1-2. the catalog and its size menus ----------

def test_the_catalog_is_in_the_repo_with_every_size_menu():
    assert list(taxonomy.catalog()["departments"])[:4] == ["Women", "Men", "Kids", "Home"]
    assert list(taxonomy.size_menus("Women", "Tops")) == ["Standard", "Plus", "Petite", "Juniors", "Maternity"]
    assert list(taxonomy.size_menus("Men", "Shirts")) == ["Standard", "Big & Tall"]
    assert taxonomy.size_menus("Kids", "Shoes")["Baby"][:3] == ["0", "0.5", "1"]          # was UNVERIFIED
    assert taxonomy.size_menus("Women", "Global & Traditional Wear", "Kurtas")["Standard"][-1] == "XL"
    assert taxonomy.size_menus("Women", "Gadgets") == {}


@pytest.mark.parametrize("kw,want", [
    (dict(category="Tops", size_us=Ev(value="m", confidence=0.9)), ("Standard", "M")),
    (dict(category="Tops", size_us=Ev(value="14", confidence=0.9)), ("Plus", "14")),
    (dict(category="Tops", size_us=Ev(value="3XL", confidence=0.9)), ("Plus", "XXXL")),
    (dict(category="Tops", size_us=Ev(value="M", confidence=0.9), size_printed=Ev(value="M Petite")), ("Petite", "MP")),
    (dict(category="Tops", size_us=Ev(value="5", confidence=0.9), size_printed=Ev(value="5 Jrs")), ("Juniors", "5")),
    (dict(category="Dresses", item_type="maternity wrap dress", size_us=Ev(value="M", confidence=0.9)),
     ("Maternity", "M")),
    (dict(category="Shoes", size_us=Ev(value="8 1/2", confidence=0.9)), ("Standard", "8.5")),
    (dict(category="Shoes", size_us=Ev(value="38", confidence=0.9)), None),             # EU: not on the menu
    (dict(category="Bags", subcategory=None, size_us=Ev()), ("Standard", "One Size")),
    (dict(category="Intimates & Sleepwear", subcategory=None, size_us=Ev(value="34DD", confidence=0.9)),
     ("Standard", "34E (DD)")),
    (dict(department="Men", category="Jeans", subcategory=None, size_us=Ev(value="32x30", confidence=0.9)),
     ("Standard", "Waist 32")),
    (dict(department="Men", category="Shirts", subcategory=None, size_us=Ev(value="XXL", confidence=0.9)),
     ("Big & Tall", "XXL")),
    (dict(department="Kids", category="Shoes", subcategory=None, kids_gender="girls",
          size_us=Ev(value="7.5", confidence=0.9), size_printed=Ev(value="EU 24")), ("Girls", "7.5 (Toddler Girl)")),
    (dict(department="Kids", category="Shoes", subcategory=None, kids_gender="boys",
          size_us=Ev(value="5", confidence=0.9), size_printed=Ev(value="EU 21")), ("Baby", "5")),
    (dict(department="Kids", category="Shoes", subcategory=None, kids_gender="girls",
          size_us=Ev(value="US Big Kid 4", confidence=0.9)), ("Girls", "4 (Big Girl)")),
    (dict(department="Kids", category="Shirts & Tops", subcategory=None, kids_gender="boys",
          size_us=Ev(value="4t", confidence=0.9)), ("Boys", "4T")),
    (dict(department="Kids", category="Shirts & Tops", subcategory=None, kids_gender="girls",
          size_us=Ev(value="3-6 months", confidence=0.9)), ("Baby", "3-6 Months")),
    (dict(department="Kids", category="Shirts & Tops", subcategory=None, kids_gender="girls",
          size_us=Ev(value="18", confidence=0.9)), None),                                  # a Boys-only size
])
def test_the_size_is_exactly_a_value_of_its_tabs_menu(facts, kw, want):
    got = sizes.poshmark_size(facts(**kw))
    assert got == want
    if got:
        assert got[1] in taxonomy.size_menus(facts(**kw).department, facts(**kw).category,
                                             facts(**kw).subcategory)[got[0]]


def test_every_kids_label_of_the_size_table_is_on_poshmarks_kids_menus():
    menus = taxonomy.size_menus("Kids", "Shirts & Tops")
    offered = {size for tab in menus.values() for size in tab}
    labels = {s for _, s in sizes.KIDS_BY_CM} | set(sizes.KIDS_BY_YEARS.values()) | {s for _, s in sizes.KIDS_BY_MONTHS}
    assert labels <= offered


def test_the_listing_carries_the_menus_value_and_a_size_it_lacks_is_asked(env, facts, monkeypatch):
    s, db, bot = env
    state = {"facts": lambda: facts(department="Men", category="Jeans", subcategory=None,
                                    size_us=Ev(value="32", photos=[1], source="photo", confidence=0.9))}
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model(state))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    posh = loads(db.item(iid)["renders"])["poshmark"]
    assert (posh["size_tab"], posh["size_value"]) == ("Standard", "Waist 32")
    assert "Size Waist 32" in bot.sent()[-1]["caption"]                            # the card shows the menu's words
    state["facts"] = lambda: facts(size_us=Ev(value="38", photos=[1], source="photo", confidence=0.9))
    db.set_item(iid, status="new")
    pipeline.process_item(s, db, iid)
    gate = loads(db.item(iid)["gate"])
    assert gate["questions"] == []                                   # WO33: shown, never asked
    assert f"{SIZE_NOTE} 38 — not on Poshmark's Women Shoes size list (Standard): the nearest is used" in gate["info"]
    assert "Size 38 — my best reading; reply 'size …' to change it" in bot.sent()[-1]["caption"]
    state["facts"] = lambda: facts(category="Bags", subcategory=None, size_us=Ev())   # a bag: no size to ask
    db.set_item(iid, status="new")
    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    assert loads(it["gate"])["questions"] == [] and loads(it["renders"])["poshmark"]["size_value"] == "One Size"


# ---------- 3. "Which category?" ----------

def _unsure(facts):
    """A skirt that might be shorts (a set is never asked since WO27: its bottom decides, see test_never_ask)."""
    return lambda: facts(item_type="bubble skirt", category="Skirts", subcategory="Circle & Skater",
                         size_us=Ev(value="M", photos=[1], source="photo", confidence=0.9),
                         category_confidence=0.55, category_alternatives=[CategoryPath(category="Shorts")])


def test_an_unsure_category_is_asked_with_real_paths_and_the_models_pick_settles_at_once(env, facts, monkeypatch):
    s, db, bot = env
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model({"facts": _unsure(facts)}))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    assert bot.sent()[-1]["caption"].endswith(approve.CATEGORY_QUESTION)            # before the price card
    assert bot.buttons() == [("Skirts › Circle & Skater", f"cat:{iid}:0"), ("Shorts", f"cat:{iid}:1")]
    assert loads(db.item(iid)["gate"])["reasons"][0] == "category unsure (0.55)"
    approve.handle_update(s, db, bot, _cb(f"cat:{iid}:0", bot.next_id))           # the model's own pick
    it = db.item(iid)
    assert it["status"] == "awaiting_price" and loads(it["facts"])["category_confidence"] == 1.0
    assert loads(it["gate"])["ask_category"] == [] and loads(it["owner_category"])["subcategory"] == "Circle & Skater"
    assert bot.sent()[-1]["caption"].startswith("Tory Burch")                     # now the price card
    assert any(text.startswith("⭐$") for text, _ in bot.buttons())


def test_another_category_reprocesses_the_item_with_it(env, facts, monkeypatch):
    s, db, bot = env
    notes = []
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model({"facts": _unsure(facts)}, notes))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    question = bot.next_id
    approve.handle_update(s, db, bot, _cb(f"cat:{iid}:1", question))               # Shorts
    assert db.item(iid)["status"] == "new" and bot.marks() == [(question, "✓ Shorts")]   # WO29: the card, quietly
    pipeline.process_item(s, db, iid)                                               # the model still says Skirts, 0.55
    it = db.item(iid)
    f = loads(it["facts"])
    assert (f["category"], f["subcategory"], f["category_confidence"]) == ("Shorts", None, 1.0)
    assert loads(it["gate"])["ask_category"] == [] and it["status"] == "awaiting_price"
    assert notes[-1] == "Seller note: Category (the owner's answer): Shorts"         # the copy follows the answer


def test_typed_answers_keep_working(env, facts, monkeypatch):
    s, db, bot = env
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model({"facts": _unsure(facts)}))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    question = bot.next_id
    approve.handle_update(s, db, bot, _reply("Pants & Jumpsuits / Wide Leg", question))
    assert loads(db.item(iid)["owner_category"]) == {"department": "Women", "category": "Pants & Jumpsuits",
                                                     "subcategory": "Wide Leg"}
    db.set_item(iid, status="awaiting_price", owner_category=None,
                gate={**loads(db.item(iid)["gate"]), "ask_category": [{"department": "Women", "category": "Shorts",
                                                                       "subcategory": None}]})
    approve.handle_update(s, db, bot, _reply("the bottom has legs, it's shorts really", question))
    assert db.item(iid)["status"] == "new" and "shorts really" in db.item(iid)["note"]   # a note, as before


def test_a_sure_category_is_never_asked(env, facts, monkeypatch):
    s, db, bot = env
    monkeypatch.setattr("thrift_agent.brain.llm.ask",
                        _model({"facts": lambda: facts(category_confidence=0.9,
                                                       category_alternatives=[CategoryPath(category="Shorts")])}))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    assert loads(db.item(iid)["gate"])["ask_category"] == [] and not bot.sent()[-1]["caption"].endswith("photos)")


# ---------- 4. "no brand" ----------

def test_no_brand_is_a_button_on_a_brand_question_and_keeps_the_card(env, facts, monkeypatch):
    s, db, bot = env
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model({"facts": lambda: facts(brand=Ev()),
                                                              "title": "Red Ballet Flats size 7.5"}))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    card = bot.next_id
    assert ("No brand", f"nobrand:{iid}") in bot.buttons()
    sends = len(bot.sent())
    approve.handle_update(s, db, bot, _cb(f"nobrand:{iid}", card))
    it = db.item(iid)
    assert it["owner_brand"] == "none" and it["status"] == "awaiting_price"
    assert loads(it["facts"])["brand"]["source"] == "owner" and loads(it["renders"])["poshmark"]["brand"] is None
    assert not any(q.startswith("Brand") for q in loads(it["gate"])["questions"])
    assert len(bot.sent()) == sends and bot.marks() == []      # WO29: nothing said; the card keeps its buttons
    assert [r["message_id"] for r in db.outbox_pending()] == [card]               # the card stays open
    approve.handle_update(s, db, bot, _cb(f"approve:{iid}:40", card))
    assert db.item(iid)["status"] == "ready"


def test_no_brand_never_shows_without_a_brand_question(env, facts, monkeypatch):
    s, db, bot = env
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model({"facts": facts}))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    assert all(t != "No brand" for t, _ in bot.buttons())


def test_no_brand_on_a_guessed_brand_rewrites_the_listing_without_it(env, facts, monkeypatch):
    s, db, bot = env
    notes = []
    guess = Ev(value="Mistguided", photos=[2], source="photo", confidence=0.6)
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model({"facts": lambda: facts(brand=guess)}, notes))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    approve.handle_update(s, db, bot, _reply("unbranded, 25", bot.next_id))        # the price, then no brand
    it = db.item(iid)
    assert it["owner_price"] == 25 and it["owner_brand"] == "none" and it["status"] == "new"
    pipeline.process_item(s, db, iid)                                               # the model still guesses
    it = db.item(iid)
    assert loads(it["facts"])["brand"] == {"value": None, "photos": [], "source": "owner", "confidence": 1.0}
    assert it["status"] == "ready" and loads(it["renders"])["poshmark"]["brand"] is None
    assert notes[-1] == "Seller note: Brand: none — the owner says the item has no brand"


def test_the_cli_twin_and_the_gate_and_lint(env, facts, monkeypatch):
    s, db, bot = env
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model({"facts": lambda: facts(brand=Ev())}))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    assert pipeline.answer(s, db, iid, "No brand.") == "awaiting_price" and db.item(iid)["owner_brand"] == "none"
    gate_cfg = {"min_confidence": {"brand": 0.70, "size": 0.70, "condition": 0.70}}
    owner = facts(brand=Ev(value=None, source="owner", confidence=1.0))
    assert evaluate(owner, PriceResult(target=30, list_price=40, source="owner"), [], 0, gate_cfg, {}).questions == []
    copy = CopyOut(poshmark_title="Tory Burch Red Flats size 7.5", poshmark_description=f"Red flats.\n{LINE}",
                   poshmark_style_tags=[], depop_description=f"red flats. {LINE}", depop_hashtags=["a"] * 5)
    assert "names brand 'tory burch' the facts don't have" in lint(owner, copy, [0], ["tory burch"])
    assert not any("names brand" in p for p in lint(facts(), copy, [0], ["tory burch"]))


# ---------- 5. a set's title, and `thrift reprocess` with the card compared ----------

@pytest.mark.parametrize("title,pieces,want", [
    ("Solid & Striped Knit Cropped Top & Flare Pants Set size M", 2,
     "Solid & Striped Knit Cropped Top & Flare Pants 2-Piece Set size M"),
    ("Corset Top and Bubble Skirt size M", 2, "Corset Top and Bubble Skirt 2-Piece Set size M"),
    ("Corset Top & Bubble Skirt 2-Piece Set size M", 2, "Corset Top & Bubble Skirt 2-Piece Set size M"),
    ("Lacoste Graphic Tee size 4T", None, "Lacoste Graphic Tee size 4T"),
    ("A" * 70 + " Set size M", 2, "A" * 70 + " Set size M"),                      # no room in 80: left as it is
])
def test_a_set_says_how_many_pieces_in_its_title(facts, title, pieces, want):
    assert copywriter.ensure_set_title(title, facts(set_pieces=pieces)) == want


def test_reprocess_keeps_an_unchanged_card_and_resends_a_changed_one(env, facts, monkeypatch):
    s, db, bot = env
    state = {"facts": lambda: facts(brand=Ev()), "title": "Red Ballet Flats size 7.5"}
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model(state))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    card, sends = bot.next_id, len(bot.sent())
    out = pipeline.reprocess(s, db, iid)                                            # the same reading
    assert (out["status"], out["card"]) == ("awaiting_price", "unchanged") and len(bot.sent()) == sends
    assert [r["message_id"] for r in db.outbox_pending()] == [card]
    state["facts"] = lambda: facts(brand=Ev(), set_pieces=2)                        # the set rule now applies
    state["title"] = "Knit Top & Flare Pants Set size 7.5"
    out = pipeline.reprocess(s, db, iid)
    assert out["card"] == "sent again" and out["title"] == "Knit Top & Flare Pants 2-Piece Set size 7.5"
    assert bot.sent()[-1]["caption"].startswith("Knit Top & Flare Pants 2-Piece Set") and bot.next_id == card + 1


def test_reprocess_loses_to_an_answer_and_never_touches_a_listed_item(env, facts, monkeypatch):
    s, db, bot = env
    calls = {"n": 0}
    base = _model({"facts": facts})

    def ask(model, system, content, out, tool, description, **kw):
        if out is CopyOut and calls["n"] == 1:
            import time
            time.sleep(1.1)                                                         # updated_at has whole seconds
            pipeline.set_price(s, db, iid, 45)                                      # the owner's price, meanwhile
        if out is CopyOut:
            calls["n"] += 1
        return base(model, system, content, out, tool, description, **kw)
    monkeypatch.setattr("thrift_agent.brain.llm.ask", ask)
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    with pytest.raises(ValueError, match="changed while it was being reprocessed"):
        pipeline.reprocess(s, db, iid)
    assert db.item(iid)["status"] == "ready" and db.item(iid)["owner_price"] == 45
    db.set_item(iid, status="posted")
    with pytest.raises(ValueError, match="on the marketplace"):
        pipeline.reprocess(s, db, iid)


def test_the_cli_commands(tmp_path, monkeypatch, facts):
    from typer.testing import CliRunner

    from thrift_agent import cli
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = db.add_item(db.add_batch("share", 1), 1, str(tmp_path / "item"))
    db.set_item(iid, status="awaiting_price", facts=facts().model_dump())
    monkeypatch.setattr(cli, "settings", lambda: s)
    monkeypatch.setattr(cli, "_db", lambda: db)
    monkeypatch.setattr(cli.approve, "pump", lambda *a: None)
    monkeypatch.setattr(cli.approve, "announce", lambda *a: True)
    monkeypatch.setattr(cli, "_tick_unless_worker", lambda *a: None)
    monkeypatch.setattr(cli.pipeline, "reprocess", lambda s_, db_, i: {"status": "awaiting_price", "card": "unchanged",
                                                                      "title": "T", "category": "Shoes"})
    r = CliRunner().invoke(cli.app, ["reprocess", iid], terminal_width=200)
    assert r.exit_code == 0 and "card unchanged (not sent again)" in " ".join(r.output.split()), r.output
    got = []
    monkeypatch.setattr(cli.pipeline, "set_category", lambda s_, db_, i, path: got.append(path) or "new")
    r = CliRunner().invoke(cli.app, ["category", iid, "Skirts > Skirt Sets"], terminal_width=200)
    assert r.exit_code == 0 and got == [{"department": "Women", "category": "Skirts", "subcategory": "Skirt Sets"}]
    r = CliRunner().invoke(cli.app, ["category", iid, "Gadgets"], terminal_width=200)
    assert r.exit_code == 1 and "no such Poshmark category" in " ".join(r.output.split())


# ---------- WO27: never asked — a set's category; the owner's brand and title ----------

def test_a_set_is_never_asked_which_category_its_bottom_decides(env, facts, monkeypatch):
    s, db, bot = env
    unsure_set = lambda: facts(item_type="corset top and bubble skirt set", category="Skirts",  # noqa: E731
                               subcategory="Mini", size_us=Ev(value="M", photos=[1], source="photo", confidence=0.9),
                               category_confidence=0.55, category_alternatives=[CategoryPath(category="Shorts")])
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model({"facts": unsure_set}))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    f, gate = loads(it["facts"]), loads(it["gate"])
    assert (f["category"], f["subcategory"], f["category_confidence"], f["set_pieces"]) == ("Skirts", "Skirt Sets",
                                                                                           1.0, 2)
    assert gate["ask_category"] == [] and it["status"] == "awaiting_price"
    assert not any(approve.CATEGORY_QUESTION in (m.get("caption") or m.get("text") or "") for m in bot.sent())
    assert bot.sent()[-1]["caption"].startswith("Tory Burch")                     # straight to the price card


def test_a_plain_reply_to_the_cards_brand_question_is_the_brand(env, facts, monkeypatch):
    """WO27 1e: "Vince" replied to a card that asks the brand sets the brand (live: "J. Crew" was taken as a note)."""
    s, db, bot = env
    state = {"facts": lambda: facts(brand=Ev(value="Vinse", photos=[1], source="photo", confidence=0.5)),
             "title": "Vinse Red Ballet Flats size 7.5"}
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model(state))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    card = bot.next_id
    assert "reply 'brand …'" in bot.sent()[-1]["caption"]
    out = approve.handle_update(s, db, bot, _reply("Vince", card))
    it = db.item(iid)
    assert out == f"brand {iid}: 'Vince' (awaiting_price)" and it["owner_brand"] == "Vince" and it["note"] is None
    f, posh = loads(it["facts"]), loads(it["renders"])["poshmark"]
    assert (f["brand"]["value"], f["brand"]["source"]) == ("Vince", "owner")
    assert (posh["brand"], posh["title"]) == ("Vince", "Vince Red Ballet Flats size 7.5")
    assert loads(it["gate"])["questions"] == []
    assert bot.sent()[-1]["caption"].startswith("Vince Red Ballet Flats")          # the card again, with the brand
    assert bot.next_id == card + 2 and bot.marks() == [(card, "✓ brand: Vince")]   # the old copy marked, and
    assert _answers(bot)[-1].startswith("✓ Brand: Vince — ")                      # WO33: her reply answered


def test_edit_sets_the_owners_title_and_brand_and_both_stay(env, facts, monkeypatch, tmp_path):
    """`thrift edit` (WO27): the owner's exact words, kept through reprocessing; the price kept; another spelling of the
    same name learned for next time."""
    s, db, bot = env
    state = {"facts": lambda: facts(item_type="wide leg sweater pants", category="Pants & Jumpsuits",
                                    subcategory="Wide Leg", condition="NWOT", colors=["Blue"],
                                    brand=Ev(value="J.Crew", photos=[1], source="photo", confidence=0.95),
                                    size_us=Ev(value="M", photos=[1], source="photo", confidence=0.95)),
             "title": "J.Crew New Wide Leg Sweater Pants Blue size M"}
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model(state))
    monkeypatch.setattr("thrift_agent.brands.SEED", {})                           # as live, before the seed
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    pipeline.set_price(s, db, iid, 35)
    title = "J. Crew 100% Merino Wide Leg Sweater Pants Blue size M"
    assert pipeline.edit_listing(s, db, iid, title=title, brand="J. Crew") == "ready"
    it = db.item(iid)
    posh = loads(it["renders"])["poshmark"]
    assert (posh["title"], posh["brand"], posh["price"], it["owner_price"]) == (title, "J. Crew", 35, 35)
    assert (it["owner_title"], it["owner_brand"]) == (title, "J. Crew")
    assert "J.Crew" not in posh["description"]
    assert brands.for_settings(s).spell("J.Crew") == "J. Crew"                    # learned: Poshmark's spelling
    pipeline.reprocess(s, db, iid)                                                  # the model again: words kept
    it = db.item(iid)
    posh = loads(it["renders"])["poshmark"]
    assert (posh["title"], posh["brand"], posh["price"], it["status"]) == (title, "J. Crew", 35, "ready")
    with pytest.raises(ValueError, match="1-80 characters"):
        pipeline.edit_listing(s, db, iid, title="x" * 81)
    db.set_item(iid, status="posted")
    with pytest.raises(ValueError, match="on the marketplace|can't be changed now"):
        pipeline.edit_listing(s, db, iid, title="New title")


def _set_facts(facts, **kw):
    base = dict(item_type="knit cardigan and pants set", category="Pants & Jumpsuits", subcategory="Wide Leg",
                condition="like_new", colors=["Tan"], set_pieces=2,
                brand=Ev(value="MNG", photos=[1], source="photo", confidence=0.95),
                size_printed=Ev(value="S", photos=[2], source="photo", confidence=0.9))
    return facts(**{**base, **kw})


@pytest.mark.parametrize("read", ["pieces", "one value"])
def test_a_set_whose_pieces_have_two_sizes_is_listed_under_the_bigger_one(env, facts, monkeypatch, read):
    """WO33, the owner's rule (live: the MNG cardigan S with pants XS — asked, then held): the bigger size in the size
    field and the title, the description says which piece is which; no size question. The model may name the pieces
    in piece_sizes, or — as live — in one size value: "S (cardigan), XS (pants)"."""
    s, db, bot = env
    if read == "pieces":
        kw = dict(size_us=Ev(value="XS", photos=[2], source="photo", confidence=0.6),
                  piece_sizes=[PieceSize(piece="cardigan", size="S", photos=[2]), PieceSize(piece="pants", size="XS")])
    else:
        kw = dict(size_us=Ev(value="S (cardigan), XS (pants)", photos=[2], source="photo", confidence=0.6))
    state = {"facts": lambda: _set_facts(facts, **kw), "title": "MNG Knit Cardigan & Wide Leg Pants Tan 2-Piece Set size XS"}
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model(state))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    f, posh, gate = loads(it["facts"]), loads(it["renders"])["poshmark"], loads(it["gate"])
    assert (f["size_us"]["value"], f["size_us"]["source"]) == ("S", "derived")
    assert posh["size"] == "S" and posh["title"] == "MNG Knit Cardigan & Wide Leg Pants Tan 2-Piece Set size S"
    assert f"Red flats.\nCardigan: size S. Pants: size XS.\n{LINE}" == posh["description"]
    assert not any("size" in q.lower() for q in gate["questions"]) and "❓" not in bot.sent()[-1]["caption"]


def test_the_owners_sizes_for_a_sets_pieces_stay_through_reprocessing(env, facts, monkeypatch):
    """`thrift edit --sizes 'Cardigan=S,Pants=XS'` (WO33): the bigger size in the size field and the title, the line
    in the description, the price kept — and the owner's sizes stay when the model reads the item again."""
    s, db, bot = env
    state = {"facts": lambda: _set_facts(facts, size_us=Ev(value="XS", photos=[2], source="photo", confidence=0.9)),
             "title": "MNG Knit Cardigan & Wide Leg Pants Tan 2-Piece Set size XS"}
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model(state))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    pipeline.set_price(s, db, iid, 100)
    for run in ("set", "reprocessed"):
        if run == "set":
            assert pipeline.set_piece_sizes(s, db, iid, [("Cardigan", "S"), ("Pants", "XS")]) == "ready"
        else:
            pipeline.reprocess(s, db, iid)
        it = db.item(iid)
        posh = loads(it["renders"])["poshmark"]
        assert (posh["size"], posh["price"], it["owner_price"], it["status"]) == ("S", 100, 100, "ready"), run
        assert posh["title"].endswith("2-Piece Set size S"), run
        assert "Cardigan: size S. Pants: size XS." in posh["description"], run
        assert loads(it["owner_sizes"]) == [{"piece": "Cardigan", "size": "S"}, {"piece": "Pants", "size": "XS"}]
    with pytest.raises(ValueError, match="every piece"):
        pipeline.set_piece_sizes(s, db, iid, [("Cardigan", "S")])


def test_the_bigger_size_and_the_pieces_line():
    assert [sizes.bigger_size(v) for v in (["S", "XS"], ["XS", "S"], ["M", "L"], ["4", "2"], ["XS", "4"], ["S", "38"])] \
        == ["S", "S", "L", "4", "4", "38"]
    assert sizes.bigger_size(["one", "two"]) == "one"                       # not comparable: the first piece's


def _answers(bot, owner_mid=90):
    """The bot's replies to the owner's message (WO33: every reply gets one)."""
    return [p["text"] for m, p in bot.calls if m == "sendMessage" and p.get("reply_to_message_id") == owner_mid]


def _unsure_size(facts):
    return lambda: facts(item_type="knit cardigan", category="Sweaters", subcategory="Cardigans", condition="like_new",
                         colors=["Tan"], brand=Ev(value="MNG", photos=[1], source="photo", confidence=0.95),
                         size_printed=Ev(value="XS", photos=[2], source="photo", confidence=0.5),
                         size_us=Ev(value="XS", photos=[2], source="photo", confidence=0.5))


def test_an_unsure_size_is_shown_never_asked_and_the_item_never_held(env, facts, monkeypatch):
    """WO33, the owner's rule: never a question about size — the best reading is listed and the card says so — and
    a problem the copy check finds never holds the item: priced, it is ready and it publishes."""
    from thrift_agent.post import parallel, runner
    s, db, bot = env
    state = {"facts": _unsure_size(facts), "title": "MNG Knit Cardigan Tan"}          # no size in the title
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model(state))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    gate, posh = loads(it["gate"]), loads(it["renders"])["poshmark"]
    assert not any("size" in q.lower() for q in gate["questions"]) and gate["decision"] == "publish"
    assert posh["title"] == "MNG Knit Cardigan Tan size XS"                         # the copy check's fix, no hold
    caption = bot.sent()[-1]["caption"]
    assert "Size XS — my best reading; reply 'size …' to change it" in caption and "❓" not in caption
    assert pipeline.set_price(s, db, iid, 40) == "ready"
    s.data["marketplaces"]["poshmark"]["autopublish"] = True
    monkeypatch.setattr(type(s), "is_prod", property(lambda self: True))
    assert runner.publishable(s, db, ["poshmark"]) == [(iid, "poshmark")]
    assert runner.held(s, db, ["poshmark"]) == []
    db.set_item(iid, gate={**gate, "decision": "draft", "reasons": ["verifier rewrote x"]})   # an old 'draft' too
    assert runner.publishable(s, db, ["poshmark"]) == [(iid, "poshmark")]
    s.data["marketplaces"]["depop"] = {"enabled": True}
    monkeypatch.setattr("thrift_agent.crosslist.enabled", lambda _s: ["depop"])
    assert parallel.feed(s, db) == [iid] and db.listing(iid, "depop")["status"] == "queued"


@pytest.mark.parametrize("reply,size,price", [("Size S", "S", None), ("size s", "S", None), ("S", "S", None),
                                              ("Size S, 100", "S", 100), ("S $100", "S", 100),
                                              ("size 8.5", "8.5", None)])
def test_a_size_reply_sets_the_size_and_gets_an_answer(env, facts, monkeypatch, reply, size, price):
    """WO33, live: "Size S" replied to the card at 5:54 went nowhere the owner could see. Now "Size S", "size s", "S",
    with or without a price, sets the size at once (no model call) — the size field and the title — and the bot
    answers her message with what it understood and what happens next."""
    s, db, bot = env
    state = {"facts": _unsure_size(facts), "title": "MNG Knit Cardigan Tan size XS"}
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model(state))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    card = bot.next_id
    out = approve.handle_update(s, db, bot, _reply(reply, card))
    it = db.item(iid)
    posh = loads(it["renders"])["poshmark"]
    assert (it["owner_size"], posh["size"]) == (size, size), out
    assert posh["title"].endswith(f"size {size}")
    answer = _answers(bot)
    assert len(answer) == 1 and answer[0].startswith(f"✓ Size {size}"), answer
    if price is None:
        assert it["status"] == "awaiting_price" and "its card stays open for the price" in answer[0]
    else:
        assert it["status"] == "ready" and f"${price}" in answer[0] and "it goes up" in answer[0]
        assert "Poshmark" in answer[0]


def test_a_reply_after_the_price_is_still_answered(env, facts, monkeypatch):
    """The live case: the card already answered by its price ($100, the card closed), then "Size S" replied to it:
    the size is set, the price kept, and the answer says when it goes up."""
    s, db, bot = env
    state = {"facts": _unsure_size(facts), "title": "MNG Knit Cardigan Tan size XS"}
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model(state))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    card = bot.next_id
    approve.handle_update(s, db, bot, _cb(f"approve:{iid}:100", card))
    approve.handle_update(s, db, bot, _reply("Size S", card))
    it = db.item(iid)
    assert (it["status"], it["owner_price"], loads(it["renders"])["poshmark"]["size"]) == ("ready", 100, "S")
    assert _answers(bot) and _answers(bot)[-1].startswith("✓ Size S — it goes up")


@pytest.mark.parametrize("reply", ["the cardigan is cropped", "45"])
def test_every_reply_to_a_card_gets_an_answer(env, facts, monkeypatch, reply):
    """A note (reprocessed) or a price: either way an answer — never silence."""
    s, db, bot = env
    state = {"facts": _unsure_size(facts), "title": "MNG Knit Cardigan Tan size XS"}
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model(state))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    approve.handle_update(s, db, bot, _reply(reply, bot.next_id))
    answer = _answers(bot)
    assert len(answer) == 1 and answer[0].startswith("✓ "), answer
    assert ("Noted" in answer[0]) if reply[0].isalpha() else ("$45" in answer[0] and "it goes up" in answer[0])
    assert "thrift " not in answer[0]                                          # never a terminal command


def test_a_skipped_listing_is_settled_by_a_reply_never_a_command(env, facts, monkeypatch):
    """WO33, the owner: no message may need a terminal command. A skipped listing's group message takes a reply:
    'retry' puts it back in line, and the bot says so."""
    from thrift_agent.post import runner
    from thrift_agent.post.base import Outcome
    from thrift_agent.schema import Render
    s, db, bot = env
    state = {"facts": _unsure_size(facts), "title": "MNG Knit Cardigan Tan size XS"}
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model(state))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    pipeline.set_price(s, db, iid, 40)
    render = Render.model_validate(loads(db.item(iid)["renders"])["poshmark"])
    said = []
    monkeypatch.setattr("thrift_agent.notify.photo", lambda path, caption: said.append(caption))
    monkeypatch.setattr("thrift_agent.notify.say", lambda text: said.append(text))
    runner.record_outcome(db, iid, "poshmark", render, Outcome("skipped", error="skipped: no size menu"), ["poshmark"])
    message = bot.sent()[-1]["text"]
    assert message.startswith("⏭ MNG Knit Cardigan Tan size XS wasn't listed on Poshmark") and "'retry'" in message
    assert not any("thrift " in t for t in [message, *said])
    approve.handle_update(s, db, bot, _reply("retry", bot.next_id))
    assert db.listing(iid, "poshmark")["status"] == "queued"
    assert _answers(bot)[-1].startswith("✓ Retry — it goes back in line on Poshmark")


def test_a_reply_no_handler_answered_still_gets_an_answer(env, facts, monkeypatch):
    """The safety net (WO33): whatever a reply's handler did, a reply never goes without an answer."""
    s, db, bot = env
    state = {"facts": _unsure_size(facts), "title": "MNG Knit Cardigan Tan size XS"}
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model(state))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    monkeypatch.setattr(approve, "_reply_item", lambda *a: f"item {iid}: handled quietly")
    approve.handle_update(s, db, bot, _reply("anything at all", bot.next_id))
    assert _answers(bot) == ["✓ Got it — its card stays open for the price"]


def test_the_copy_check_fixes_a_titles_new_and_eu_size(facts):
    used = facts(brand=Ev(value="New Balance", photos=[1], source="photo", confidence=0.9), condition="good",
                 size_us=Ev(value="8", photos=[1], source="photo", confidence=0.9))
    assert copywriter.fix_title("New Balance New 574 Sneakers Red EU 38 size 8", used) == \
        "New Balance 574 Sneakers Red size 8"
    assert copywriter.fix_title("New Balance 574 Sneakers (EU 38) Red size 8", used) == "New Balance 574 Sneakers Red size 8"
    nwt = facts(condition="NWT", brand=Ev(value="Zara", photos=[1], source="photo", confidence=0.9))
    assert copywriter.fix_title("Zara New Floral Skirt size 8", nwt) == "Zara New Floral Skirt size 8"


def test_an_unmatched_sale_is_matched_by_a_reply_in_the_ops_chat(env, facts, monkeypatch):
    """WO33, the owner: no message may need a terminal command — the API's "Unmatched sale" line in the ops chat is
    answered with words from the item's title: the Mac matches it (thrift-api makes the take-downs) and says so."""
    s, db, bot = env
    state = {"facts": _unsure_size(facts), "title": "MNG Knit Cardigan Tan size XS"}
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model(state))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)
    ops = FakeBot()
    ops.chat_id = "555"
    monkeypatch.setattr(approve, "ops_bot_for", lambda _s: ops)
    monkeypatch.setattr("thrift_agent.notify.ops_chat", lambda _s=None: "555")
    calls = []

    class Api:
        def post(self, path, body):
            calls.append((path, body))
            return {"sale_id": "s_abc123", "status": "delisting", "tasks": ["t_1"]}
    monkeypatch.setattr("thrift_agent.sales.api", lambda: Api())

    def ops_reply(text):
        return {"update_id": 9, "message": {"message_id": 91, "chat": {"id": 555}, "from": {"id": OWNER},
                                            "text": text, "reply_to_message": {"message_id": 7, "text":
                                            "Unmatched sale on Vinted (s_abc123): MNG Cardigan\nIf it's one of ours, "
                                            "reply to this message with words from its title; if not, nothing to do."}}}
    assert approve.handle_update(s, db, bot, ops_reply("mng cardigan")) == f"ops: matched s_abc123 to {iid}"
    assert calls == [("/sales/s_abc123/match", {"item_id": iid})]
    assert ops.sent()[-1]["text"].startswith("✓ Matched to MNG Knit Cardigan Tan size XS — 1 take-down queued")
    assert approve.handle_update(s, db, bot, ops_reply("velvet boots")) == "ops: 0 items for ['velvet', 'boots']"
    assert ops.sent()[-1]["text"] == "No item's title has all of those words — try others"


def test_the_learned_spelling_is_used_from_the_start(env, facts, monkeypatch):
    s, db, bot = env
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model({"facts": lambda: facts(
        brand=Ev(value="J.Crew", photos=[1], source="photo", confidence=0.95))}))
    iid = _item(s.path("db").parent, db)
    pipeline.process_item(s, db, iid)                                               # the seed: J.Crew -> J. Crew
    it = db.item(iid)
    assert loads(it["facts"])["brand"]["value"] == "J. Crew" and loads(it["renders"])["poshmark"]["brand"] == "J. Crew"



def test_the_edit_command(tmp_path, monkeypatch, facts):
    from typer.testing import CliRunner

    from thrift_agent import cli
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = db.add_item(db.add_batch("share", 1), 1, str(tmp_path / "item"))
    db.set_item(iid, status="ready", facts=facts().model_dump(),
                renders={"poshmark": {"title": "J. Crew Pants size S", "brand": "J. Crew", "price": 35}})
    monkeypatch.setattr(cli, "settings", lambda: s)
    monkeypatch.setattr(cli, "_db", lambda: db)
    monkeypatch.setattr(cli.approve, "pump", lambda *a: None)
    said = []
    monkeypatch.setattr(cli.approve, "announce", lambda s_, text: said.append(text) or True)
    got = []
    monkeypatch.setattr(cli.pipeline, "edit_listing", lambda s_, db_, i, title=None, brand=None:
                        got.append((i, title, brand)) or "ready")
    r = CliRunner().invoke(cli.app, ["edit", iid, "--brand", "J. Crew", "--title", "J. Crew Pants size S"],
                           terminal_width=200)
    assert r.exit_code == 0 and got == [(iid, "J. Crew Pants size S", "J. Crew")], r.output
    assert "J. Crew Pants size S · brand J. Crew · $35 · ready" in " ".join(r.output.split())
    assert said == [f"{iid}: title 'J. Crew Pants size S', brand 'J. Crew' set from the CLI"]
    r = CliRunner().invoke(cli.app, ["edit", iid], terminal_width=200)
    assert r.exit_code == 1 and "give --title, --brand and/or --sizes" in " ".join(r.output.split())
