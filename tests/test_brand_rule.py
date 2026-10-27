"""The brand rule (Alex, 2026-10-27):
1. no brand to be seen (no brand label, tag or logo; nothing suggesting a known brand) → listed with no brand, never
   asked — each site's no-brand option, "listed with no brand" in the "Check —" line;
2. a brand there but unreadable (a blurry, cut-off or ambiguous label, tag or logo; a known brand's logo or design
   without a readable label) → the one question, the item waits for her reply;
3. a brand read confidently → used, never asked."""
import pytest
from test_wo39 import FakeBot, _item, env  # noqa: F401 (env: fixture)

from thrift_agent import approve, crosslist, pipeline
from thrift_agent.brain import extract
from thrift_agent.brain.gate import NO_BRAND_NOTE, brand_seen, evaluate
from thrift_agent.post import runner
from thrift_agent.schema import Ev, Facts, PriceResult

OK_PRICE = PriceResult(target=70, list_price=85, source="brand", by_marketplace={"poshmark": 85})
NO_BRAND = Ev(value=None, photos=[], source="none", confidence=0.0)


def _gate(facts, gate_cfg, pricing_cfg):
    return evaluate(facts, OK_PRICE, [], 0, gate_cfg, pricing_cfg)


# ---------------------------------------------------------------- 1. no brand to be seen

@pytest.mark.parametrize("seen", [None, "none"])      # None: facts read before the rule (no brand_seen) — the same
def test_no_brand_to_be_seen_is_listed_with_no_brand_never_asked(facts, gate_cfg, pricing_cfg, seen):
    r = _gate(facts(brand=NO_BRAND, brand_seen=seen), gate_cfg, pricing_cfg)
    assert r.questions == [] and r.decision == "publish" and NO_BRAND_NOTE in r.notes
    assert not any("brand" in x for x in r.reasons)


# ---------------------------------------------------------------- 2. a brand there but unreadable

@pytest.mark.parametrize("brand,seen,question", [
    (Ev(value=None, photos=[4], source="photo", confidence=0.0), "unreadable", "Brand? Couldn't read it"),   # blurry tag
    (Ev(value=None, photos=[4], source="photo", confidence=0.0), None, "Brand? Couldn't read it"),          # old facts
    (Ev(value="Lululemon", photos=[1], source="photo", confidence=0.5), "logo", "Brand: read as “Lululemon”"),
    (Ev(value="Zell", photos=[2], source="photo", confidence=0.4), "unreadable", "Brand: read as “Zell”"),   # cut off
    (Ev(value="Nike", photos=[], source="photo", confidence=0.5), "none", "Brand: read as “Nike”"),          # a hint
])
def test_a_brand_there_but_unreadable_is_the_one_question(facts, gate_cfg, pricing_cfg, brand, seen, question):
    r = _gate(facts(brand=brand, brand_seen=seen), gate_cfg, pricing_cfg)
    assert r.decision == "needs_info" and len(r.questions) == 1 and r.questions[0].startswith(question)
    assert NO_BRAND_NOTE not in r.notes


# ---------------------------------------------------------------- 3. a brand read confidently

def test_a_brand_read_confidently_is_used_never_asked(facts, gate_cfg, pricing_cfg):
    r = _gate(facts(brand=Ev(value="Zara", photos=[3], source="photo", confidence=0.95), brand_seen="read"),
              gate_cfg, pricing_cfg)
    assert r.questions == [] and r.decision == "publish" and NO_BRAND_NOTE not in r.notes
    owner = facts(brand=Ev(value=None, photos=[], source="owner", confidence=1.0))      # her 'no brand'
    assert _gate(owner, gate_cfg, pricing_cfg).questions == []


def test_what_the_photos_show_of_the_brand(facts):
    assert brand_seen(facts(brand=NO_BRAND)) == "none"
    assert brand_seen(facts(brand=Ev(value=None, photos=[2], confidence=0.0))) == "unreadable"
    assert brand_seen(facts()) == "read"
    assert brand_seen(facts(brand=NO_BRAND, brand_seen="logo")) == "logo"


def test_the_extraction_asks_the_model_what_it_saw():
    assert "brand_seen, always:" in extract.SYSTEM and "brand_seen" in Facts.model_fields


# ---------------------------------------------------------------- what the owner sees, and the items waiting

def _no_brand_item(db, tmp_path, facts, gate_notes=(NO_BRAND_NOTE,), questions=(), name="nb"):
    iid = _item(db, tmp_path, facts, "Ribbed Biker Shorts Light Pink size S", status="ready", size="S", name=name)
    db.set_item(iid, owner_price=25,
                facts=facts(brand=NO_BRAND, item_type="biker shorts", category="Shorts", subcategory="Athletic Shorts",
                            size_us=Ev(value="S", photos=[1], source="photo", confidence=0.9)).model_dump(),
                gate={"decision": "needs_info" if questions else "publish", "reasons": list(questions),
                      "questions": list(questions), "notes": [], "info": list(gate_notes)})   # as process_item keeps them
    return iid


def test_a_no_brand_item_goes_up_at_once_and_the_check_line_says_it(env, tmp_path, facts, monkeypatch):  # noqa: F811
    s, db, said = env
    iid = _no_brand_item(db, tmp_path, facts)
    it = db.item(iid)
    assert not approve.needs_brand(it) and runner.approved(it, {"price": 25})       # never held, never asked
    db.upsert_listing(iid, "depop", status="posted", url="https://www.depop.com/products/shop-shorts-1/",
                      fields_json={"guesses": ["brand set to 'Other' (no brand)", "size set to 'S' (from S)"]})
    db.upsert_listing(iid, "vinted", status="posted", url="https://www.vinted.com/items/1",
                      fields_json={"guesses": ["listed without a brand"]})
    crosslist._say_checks(db, iid, ["depop", "vinted"])
    check = [m for m in said if m.startswith("Check —")]
    assert len(check) == 1 and f"- {crosslist.LISTED_NO_BRAND}" in check[0]
    assert "(no brand)" not in check[0] and "listed without a brand" not in check[0]   # said once, for the item
    assert "size set to 'S'" in check[0]


def test_an_unreadable_brand_waits_and_asks(env, tmp_path, facts, monkeypatch):  # noqa: F811
    s, db, _ = env
    bot = FakeBot()
    monkeypatch.setattr(approve, "bot_for", lambda *_: bot)
    iid = _no_brand_item(db, tmp_path, facts, gate_notes=(), questions=("Brand? Couldn't read it — reply 'brand …'",))
    assert approve.needs_brand(db.item(iid)) and not runner.approved(db.item(iid), {"price": 25})
    assert approve.ask_brands(s, db) == 1


def test_the_items_waiting_now_get_the_rule_once(env, tmp_path, facts, monkeypatch):  # noqa: F811
    """Applied once at the worker's start: a waiting item asked the brand with nothing to be seen lists with no brand;
    one whose brand is there but unreadable keeps its question."""
    s, db, _ = env
    monkeypatch.setattr(approve, "bot_for", lambda *_: FakeBot())
    asked = ("Brand? Couldn't read it — reply 'brand …'",)
    none = _no_brand_item(db, tmp_path, facts, gate_notes=(), questions=asked, name="none")
    blurry = _no_brand_item(db, tmp_path, facts, gate_notes=(), questions=asked, name="blurry")
    db.set_item(blurry, facts=facts(brand=Ev(value=None, photos=[2], source="photo", confidence=0.0)).model_dump())
    monkeypatch.setattr(pipeline, "_regate", lambda s_, it, f, r, p, k, n: (
        {"decision": "publish", "reasons": [], "questions": [], "notes": [NO_BRAND_NOTE], "info": []}, "ready"))
    monkeypatch.setattr(pipeline, "_photos_of", lambda it: ([], []))
    assert pipeline.apply_brand_rule(s, db) == [none]
    assert not approve.needs_brand(db.item(none)) and approve.needs_brand(db.item(blurry))
    assert pipeline.apply_brand_rule(s, db) == []                                       # once
