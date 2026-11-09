"""The brand rule (Alex, 2026-10-27; WO54 §2, the owner 2026-11-09: decide, never wait):
1. no brand to be seen (no brand label, tag or logo; nothing suggesting a known brand) → listed with no brand, never
   asked — each site's no-brand option, "listed with no brand" in the "Check —" line;
2. a brand there but not read surely (a blurry, cut-off or ambiguous label, tag or logo; a known brand's logo or design
   without a readable label) → WO54: decided, never asked — the reading when it is a big brand, else no brand; the
   card's ⚠️ line says which and her reply corrects it (before: the one question, the item waited for her reply);
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


# ---------------------------------------------------------------- 2. a brand there but not read surely: decided

@pytest.mark.parametrize("brand,seen,listed,line", [
    (Ev(value=None, photos=[4], source="photo", confidence=0.0), "unreadable", None,
     "Brand: none — a brand label is in the photos but couldn't be read"),                          # blurry tag
    (Ev(value=None, photos=[4], source="photo", confidence=0.0), None, None,
     "Brand: none — a brand label is in the photos but couldn't be read"),                          # old facts
    (Ev(value="Lululemon", photos=[1], source="photo", confidence=0.5), "logo", "Lululemon",
     "Brand: Lululemon — read with doubt (0.50); a big brand, so it's listed as Lululemon"),       # a big brand: kept
    (Ev(value="Zell", photos=[2], source="photo", confidence=0.4), "unreadable", None,
     "Brand: none — the label reads “Zell” only with doubt (0.40)"),                                # cut off: no brand
    (Ev(value="Nike", photos=[], source="photo", confidence=0.5), "none", "Nike",
     "Brand: Nike — read with doubt (0.50)"),                                                       # a hint of a big one
])
def test_a_brand_there_but_not_read_surely_is_decided_never_asked(facts, gate_cfg, pricing_cfg, brand, seen, listed,
                                                                 line):
    """WO54 §2: settle_brand decides (a big brand never ends as "no brand", WO52) and the gate asks nothing."""
    f, note = pipeline.settle_brand(facts(brand=brand, brand_seen=seen), gate_cfg["min_confidence"]["brand"])
    assert f.brand.value == listed and note.startswith(line)
    assert "reply" not in note.lower() and "?" not in note                       # a statement, never a question
    r = _gate(f, gate_cfg, pricing_cfg)
    assert r.decision == "publish" and r.questions == [] and NO_BRAND_NOTE not in r.notes
    assert not any("brand" in x for x in r.reasons)


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


def test_an_unreadable_brand_never_waits_and_nothing_asks(env, tmp_path, facts, monkeypatch):  # noqa: F811
    """WO54 §2: even an item processed before it (its card still holds "Brand? Couldn't read it") goes up — no 🏷️."""
    s, db, _ = env
    bot = FakeBot()
    monkeypatch.setattr(approve, "bot_for", lambda *_: bot)
    iid = _no_brand_item(db, tmp_path, facts, gate_notes=(), questions=("Brand? Couldn't read it — reply 'brand …'",))
    assert not approve.needs_brand(db.item(iid)) and runner.approved(db.item(iid), {"price": 25})
    assert approve.ask_brands(s, db) == 0 and bot.texts() == []


def test_the_items_waiting_now_get_the_rule_once(env, tmp_path, facts, monkeypatch):  # noqa: F811
    """Applied once at the worker's start (WO54 §2): every waiting item asked the brand is decided — with nothing to be
    seen it lists with no brand; one whose brand is there but unreadable lists with no brand too, its card saying so;
    an unsure reading that isn't a big brand is processed again (the copy must lose it)."""
    s, db, _ = env
    monkeypatch.setattr(approve, "bot_for", lambda *_: FakeBot())
    asked = ("Brand? Couldn't read it — reply 'brand …'",)
    none = _no_brand_item(db, tmp_path, facts, gate_notes=(), questions=asked, name="none")
    blurry = _no_brand_item(db, tmp_path, facts, gate_notes=(), questions=asked, name="blurry")
    unsure = _no_brand_item(db, tmp_path, facts, gate_notes=(), questions=("Brand: read as “Zell”, not sure",),
                            name="unsure")
    db.set_item(blurry, facts=facts(brand=Ev(value=None, photos=[2], source="photo", confidence=0.0)).model_dump())
    db.set_item(unsure, facts=facts(brand=Ev(value="Zell", photos=[2], source="photo", confidence=0.4)).model_dump())
    seen = {}

    def regate(s_, it, f, r, p, k, n):
        seen[it["id"]] = list(n)
        return {"decision": "publish", "reasons": [], "questions": [], "notes": list(n), "info": []}, "ready"
    monkeypatch.setattr(pipeline, "_regate", regate)
    monkeypatch.setattr(pipeline, "_photos_of", lambda it: ([], []))
    assert sorted(pipeline.apply_brand_rule(s, db)) == sorted([none, blurry, unsure])
    assert not any(approve.needs_brand(db.item(i)) for i in (none, blurry, unsure))
    assert seen[none] == [] and seen[blurry][0].startswith(pipeline.BRAND_NONE)         # her card says it, no question
    assert db.item(unsure)["status"] == "new"                                            # processed again: decided then
    assert pipeline.apply_brand_rule(s, db) == []                                       # once
