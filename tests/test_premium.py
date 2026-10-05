"""WO26: premium details from the labels and photos, stated exactly in the title and the description — only what a
label or a photo shows. The model is stubbed; no network."""
import pytest
from PIL import Image

from thrift_agent import approve, pipeline
from thrift_agent.brain import labels, premium
from thrift_agent.brain.price import price
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB, loads
from thrift_agent.schema import CopyOut, Ev, Feature, Fiber, Premium, VerifyOut

CFG = premium.config()
LINE = "Gently pre-loved, please see photos for condition."


def pants(facts, **kw):
    """The live white silk pants' shape: no brand, label photo 1."""
    base = dict(item_type="sheer wide-leg pants", category="Pants & Jumpsuits", subcategory="Wide Leg", brand=Ev(),
                size_printed=Ev(value="M", photos=[1], source="photo", confidence=0.9),
                size_us=Ev(value="M", photos=[1], source="photo", confidence=0.9), colors=["White"])
    base.update(kw)
    return facts(**base)


def read(**kw):
    return Premium(**kw)


def silk(pct=100, photos=(1,), part="main"):
    return Fiber(fiber="silk", pct=pct, part=part, photos=list(photos))


# ---------- each feature: found -> title / description ----------

def test_the_live_silk_pants_say_100_percent_silk(facts):
    f = premium.merge(pants(facts), read(composition=[silk()]), 6)
    title = premium.title_with_feature("White Silk Sheer Wide-Leg Pants with Elastic Waist size M", f, CFG)
    assert title == "100% Silk White Sheer Wide-Leg Pants with Elastic Waist size M"
    assert premium.title_with_feature(title, f, CFG) == title                         # the same, run twice
    assert premium.feature_lines(f, CFG) == ["Material: 100% silk."]
    assert f.material.value == "100% silk" and f.material.photos == [1]               # the materials rule's evidence


@pytest.mark.parametrize("composition,phrase,line", [
    ([Fiber(fiber="cashmere", pct=95, photos=[2]), Fiber(fiber="nylon", pct=5, photos=[2])], "Cashmere",
     "Material: 95% cashmere, 5% nylon."),
    ([Fiber(fiber="merino wool", pct=100, photos=[2])], "100% Merino Wool", "Material: 100% merino wool."),
    ([Fiber(fiber="silk", pct=60, photos=[2]), Fiber(fiber="cotton", pct=40, photos=[2])], "Silk Blend",
     "Material: 60% silk, 40% cotton."),
    ([Fiber(fiber="silk", pct=30, photos=[2]), Fiber(fiber="cotton", pct=70, photos=[2])], None,
     "Material: 30% silk, 70% cotton."),                                              # under 50%: the description only
    ([Fiber(fiber="polyester", pct=100, photos=[2])], None, "Material: 100% polyester."),
])
def test_a_premium_fiber_by_its_share(facts, composition, phrase, line):
    f = premium.merge(pants(facts), read(composition=composition), 6)
    best = premium.strongest(f, CFG)
    assert (best.phrase if best else None) == phrase and premium.feature_lines(f, CFG)[0] == line


def test_made_in_a_listed_country_and_never_china(facts):
    italy = premium.merge(pants(facts), read(made_in=Ev(value="MADE IN ITALY", photos=[1])), 6)
    assert premium.title_with_feature("White Wide-Leg Pants size M", italy, CFG) == \
        "Made in Italy White Wide-Leg Pants size M"
    assert premium.feature_lines(italy, CFG) == ["Made in Italy."]
    scot = premium.merge(pants(facts), read(made_in=Ev(value="Made in Scotland", photos=[1])), 6)
    assert premium.feature_lines(scot, CFG) == ["Made in Scotland."]
    for country in ("China", "Made in Bangladesh", "VIETNAM", "India"):
        f = premium.merge(pants(facts), read(made_in=Ev(value=country, photos=[1])), 6)
        title = premium.title_with_feature("White Wide-Leg Pants size M", f, CFG)
        text = premium.ensure_feature_lines(f"Wide-leg pants.\n{LINE}", f, CFG)
        assert premium.strongest(f, CFG) is None and title == "White Wide-Leg Pants size M"
        assert "made in" not in text.lower() and country.split()[-1].lower() not in (title + text).lower()


def test_a_premium_line_goes_right_after_the_brand(facts):
    f = premium.merge(facts(brand=Ev(value="J.Crew", photos=[2], source="photo", confidence=0.9), item_type="blazer",
                            category="Jackets & Coats", subcategory=None),
                      read(line=Ev(value="COLLECTION", photos=[2])), 6)
    assert premium.title_with_feature("J.Crew Wool Blazer Navy size 8", f, CFG) == "J.Crew Collection Wool Blazer Navy size 8"
    assert premium.feature_lines(f, CFG) == ["J.Crew Collection."]
    assert premium.price_factor(f, CFG) == (1.2, "J.Crew Collection")
    other = premium.merge(facts(), read(line=Ev(value="Basics", photos=[2])), 6)     # not a premium line
    assert premium.strongest(other, CFG) is None and premium.feature_lines(other, CFG) == []


def test_vintage_only_with_a_concrete_cue(facts):
    cue = [Feature(text="union label", photos=[3])]
    f = premium.merge(pants(facts), read(vintage=Ev(value="1990s", photos=[3]), vintage_cues=cue), 6)
    assert premium.strongest(f, CFG).phrase == "Vintage 90s" and premium.feature_lines(f, CFG) == ["Vintage 90s."]
    assert premium.title_with_feature("Vintage White Wide-Leg Pants size M", f, CFG) == "Vintage 90s White Wide-Leg Pants size M"
    guessed = premium.merge(pants(facts), read(vintage=Ev(value="vintage", photos=[])), 6)   # no photo, no cue
    assert premium.strongest(guessed, CFG) is None and premium.price_factor(guessed, CFG) == (1.0, None)


def test_collab_technical_construction_and_the_hang_tag(facts):
    f = premium.merge(pants(facts), read(collab=Ev(value="H&M x Erdem", photos=[1]),
                                         technical=[Feature(text="UPF 50+", photos=[1])],
                                         construction=[Feature(text="fully lined", photos=[4]),
                                                       Feature(text="unlined pockets", photos=[4]),   # never negative
                                                       Feature(text="hand-beaded", photos=[])],       # never unseen
                                         retail_price=Ev(value="128", photos=[5])), 6)
    assert premium.strongest(f, CFG).phrase == "H&M x Erdem"
    assert premium.feature_lines(f, CFG) == ["H&M x Erdem.", "UPF 50+.", "Fully lined."]
    assert f.retail_price.value == "128"                                              # the hang tag's price
    screenshot = premium.merge(pants(facts, retail_price=Ev(value="150", photos=[6], source="photo", confidence=0.9)),
                               read(retail_price=Ev(value="128", photos=[5])), 7)
    assert screenshot.retail_price.value == "150"                                     # a retailer screenshot wins


def test_the_strongest_feature_wins_and_one_only(facts):
    everything = read(composition=[silk(95)], line=Ev(value="Purple Label", photos=[1]),
                      vintage=Ev(value="1980s", photos=[1]), made_in=Ev(value="Italy", photos=[1]),
                      collab=Ev(value="Limited Edition", photos=[1]))
    f = premium.merge(pants(facts, brand=Ev(value="Ralph Lauren", photos=[1], confidence=0.9)), everything, 6)
    assert premium.strongest(f, CFG).phrase == "Silk"
    f = premium.merge(pants(facts, brand=Ev(value="Ralph Lauren", photos=[1], confidence=0.9)),
                      everything.model_copy(update={"composition": [silk(60)]}), 6)
    assert premium.strongest(f, CFG).phrase == "Purple Label"                         # a blend ranks last
    title = premium.title_with_feature("Ralph Lauren Silk Blend Wide-Leg Pants White size M", f, CFG)
    assert title.startswith("Ralph Lauren Purple Label ") and title.count("Purple Label") == 1


def test_nothing_on_the_labels_changes_nothing(facts):
    f = premium.merge(pants(facts), read(), 6)
    assert premium.strongest(f, CFG) is None and premium.feature_lines(f, CFG) == []
    assert premium.title_with_feature("White Wide-Leg Pants size M", f, CFG) == "White Wide-Leg Pants size M"
    assert premium.ensure_feature_lines(f"Pants.\n{LINE}", f, CFG) == f"Pants.\n{LINE}"
    assert premium.price_factor(f, CFG) == (1.0, None)
    assert premium.merge(pants(facts), None, 6) == pants(facts)                       # a failed read: untouched


def test_80_characters_keep_brand_feature_and_size(facts):
    f = premium.merge(facts(brand=Ev(value="Brunello Cucinelli", photos=[2], confidence=0.9),
                            item_type="crewneck sweater", category="Sweaters", subcategory=None, colors=["Gray"],
                            color_name="heather gray", size_us=Ev(value="M", photos=[2], confidence=0.9)),
                      read(composition=[Fiber(fiber="cashmere", pct=100, photos=[2])]), 6)
    long = "Brunello Cucinelli Heather Gray Ribbed Crewneck Sweater with Monili Beaded Trim Detail Relaxed Fit size M"
    title = premium.title_with_feature(long, f, CFG)
    assert len(title) <= 80 and title.startswith("Brunello Cucinelli 100% Cashmere ") and title.endswith(" size M")
    assert "Sweater" in title and "Gray" in title and " with " not in title and not title.endswith("with size M")
    assert premium.title_with_feature(title, f, CFG) == title


def test_the_price_factor_is_applied_once_the_largest(facts, pricing_cfg):
    f = premium.merge(pants(facts), read(composition=[silk()], vintage=Ev(value="vintage", photos=[1]),
                                         vintage_cues=[Feature(text="single-stitch hem", photos=[1])]), 6)
    assert premium.price_factor(f, CFG) == (1.3, "100% silk")                         # not 1.3 x 1.2
    tiers = {"brands": {}, "aliases": {}, "category_defaults": {"Women": {"other": 40}}}
    plain = price(f, tiers, pricing_cfg)
    rich = price(f, tiers, pricing_cfg, None, premium.price_factor(f, CFG))
    assert rich.list_price > plain.list_price and "100% silk 1.3" in rich.basis
    assert price(f, tiers, pricing_cfg, "price 44", (1.3, "x")).list_price == 44      # the seller's own price stays


# ---------- the label read ----------

def test_the_labels_are_sent_large_and_the_details_at_the_usual_size(tmp_path, monkeypatch):
    for name in ("00.jpg", "01.jpg"):
        Image.new("RGB", (3000, 2000), "white").save(tmp_path / name)
    seen = []
    monkeypatch.setattr(labels.llm, "image", lambda p, edge: seen.append((p.name, edge)) or {"type": "image"})
    monkeypatch.setattr(labels.llm, "ask", lambda *a, **k: Premium(composition=[silk()]))
    out = labels.read_labels([(1, tmp_path / "01.jpg")], [(0, tmp_path / "00.jpg")], "m", 2048, 1568)
    assert seen == [("01.jpg", 2048), ("00.jpg", 1568)] and out.composition[0].pct == 100


# ---------- through the pipeline: a new item, and recover on an item from before WO26 ----------

def _settings(tmp_path):
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    s = Settings(data)
    s.ensure_dirs()
    return s


def _item(tmp_path, db):
    bid = db.add_batch("share", 3)
    db.set_batch(bid, status="split")
    d = tmp_path / "item"
    (d / "photos").mkdir(parents=True)
    for i, color in enumerate(["white", "gray", "white"]):
        Image.new("RGB", (60, 80), color).save(d / "photos" / f"{i:02d}.jpg")
    return db.add_item(bid, 1, str(d))


def _model(facts_factory, label_read, calls):
    def ask(model, system, content, out, tool, description, **kw):
        calls.append(out.__name__)
        title = "White Silk Sheer Wide-Leg Pants size M"
        if out.__name__ == "Facts":
            return facts_factory()
        if out.__name__ == "Premium":
            return label_read
        if out is CopyOut:
            return CopyOut(poshmark_title=title, poshmark_description=f"Wide-leg pants in white silk.\n{LINE}",
                           poshmark_style_tags=[], depop_description=f"white silk pants. {LINE}",
                           depop_hashtags=["a", "b", "c", "d", "e"])
        if out is VerifyOut:
            return VerifyOut(poshmark_title=title, poshmark_description=f"Wide-leg pants in white silk.\n{LINE}",
                             depop_description=f"white silk pants. {LINE}")
        if out.__name__ == "FrontOut":
            return out(views=[], front=-1)
        if out.__name__ == "UprightOut":
            return out(upright="A")
        raise AssertionError(out)
    return ask


def _with_label(facts):
    from thrift_agent.schema import PhotoRole
    return lambda: pants(facts, material=Ev(value="silk", photos=[1], source="photo", confidence=0.9),
                         photo_roles=[PhotoRole(photo=0, role="front"), PhotoRole(photo=1, role="label"),
                                      PhotoRole(photo=2, role="back")], cover_photo=0, photo_order=[0, 1, 2])


def test_a_new_item_gets_the_label_read_in_its_title_description_and_price(tmp_path, monkeypatch, facts):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    monkeypatch.setattr(pipeline.approve, "pump", lambda *a: None)
    calls = []
    found = read(composition=[silk()], made_in=Ev(value="China", photos=[1]), retail_price=Ev(value="128", photos=[1]))
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model(_with_label(facts), found, calls))
    iid = _item(tmp_path, db)
    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    posh = loads(it["renders"])["poshmark"]
    assert posh["title"] == "100% Silk White Sheer Wide-Leg Pants size M" and calls.count("Premium") == 1
    assert posh["description"] == f"Wide-leg pants in white silk.\nMaterial: 100% silk.\n{LINE}\nOriginal retail $128."
    assert posh["original_price"] == 128 and "china" not in (posh["title"] + posh["description"]).lower()
    assert "100% silk 1.3" in loads(it["price"])["basis"]


def test_recover_reads_the_labels_once_for_an_item_from_before(tmp_path, monkeypatch, facts):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    monkeypatch.setattr(pipeline.approve, "pump", lambda *a: None)
    calls = []
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model(_with_label(facts), read(), calls))
    iid = _item(tmp_path, db)
    pipeline.process_item(s, db, iid)
    assert pipeline.set_price(s, db, iid, 60) == "ready"                             # the owner priced it
    f = loads(db.item(iid)["facts"])
    f["premium"] = None                                                               # processed before WO26
    db.set_item(iid, facts=f)
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model(_with_label(facts), read(composition=[silk()]), calls))
    out = pipeline.recover_item(s, db, iid)
    it = db.item(iid)
    posh = loads(it["renders"])["poshmark"]
    assert out["title"] == posh["title"] == "100% Silk White Sheer Wide-Leg Pants size M"
    assert "Material: 100% silk." in posh["description"] and out["features"].startswith("title: 100% Silk")
    assert posh["price"] == 60 and it["owner_price"] == 60                            # the owner's price untouched
    calls.clear()
    assert pipeline.recover_item(s, db, iid)["title"] == posh["title"] and "Premium" not in calls   # read once
    assert approve.card(iid, db.item(iid)) is None                                     # ready: no card to resend


def test_recover_reprices_a_suggestion_and_sends_its_open_card_again(tmp_path, monkeypatch, facts):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    monkeypatch.setattr(pipeline.approve, "pump", lambda *a: None)
    calls = []
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model(_with_label(facts), read(), calls))
    iid = _item(tmp_path, db)
    pipeline.process_item(s, db, iid)
    before = loads(db.item(iid)["price"])["list_price"]
    f = loads(db.item(iid)["facts"])
    f["premium"] = None
    db.set_item(iid, facts=f)
    db.add_outbox("-100", 5, "item", iid)                                             # its card is the open one
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _model(_with_label(facts), read(composition=[silk()]), calls))
    out = pipeline.recover_item(s, db, iid)
    it = db.item(iid)
    assert loads(it["price"])["list_price"] > before and "100% silk 1.3" in loads(it["price"])["basis"]
    assert loads(it["renders"])["poshmark"]["price"] == loads(it["price"])["list_price"]
    assert out["card"] == "sent again" and db.outbox_pending() == []                 # title and price changed
