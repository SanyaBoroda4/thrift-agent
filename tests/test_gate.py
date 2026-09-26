from thrift_agent.brain.gate import GateResult, evaluate
from thrift_agent.schema import Ev, PriceResult

OK_PRICE = PriceResult(target=70, list_price=85, source="brand", by_marketplace={"poshmark": 85})


def test_clean_item_publishes(facts, gate_cfg, pricing_cfg):
    assert evaluate(facts(), OK_PRICE, [], 0, gate_cfg, pricing_cfg).decision == "publish"


def test_unreadable_size_needs_info(facts, gate_cfg, pricing_cfg):
    f = facts(size_us=Ev(value=None, confidence=0.2))
    r = evaluate(f, OK_PRICE, [], 0, gate_cfg, pricing_cfg)
    assert r.decision == "needs_info" and any("size" in x for x in r.reasons)


def test_nwt_without_tag_photo_blocked(facts, gate_cfg, pricing_cfg):
    f = facts(condition="NWT", condition_evidence=Ev(value="looks new", photos=[], source="photo", confidence=0.9))
    assert evaluate(f, OK_PRICE, [], 0, gate_cfg, pricing_cfg).decision == "needs_info"


def test_nwt_from_note_allowed(facts, gate_cfg, pricing_cfg):
    f = facts(condition="NWT", condition_evidence=Ev(value="NWT", source="note", confidence=1.0))
    assert evaluate(f, OK_PRICE, [], 0, gate_cfg, pricing_cfg).decision == "publish"


def test_thresholds_follow_the_owner_rule(facts, gate_cfg, pricing_cfg):
    # The owner's only routine input is the price: a size at exactly 0.70 (an EU→US conversion) and a used condition
    # at 0.75 (two flaws listed) publish; a brand under 0.70 is a real question.
    size = facts(size_us=Ev(value="7.5", photos=[3], source="derived", confidence=0.70))
    assert evaluate(size, OK_PRICE, [], 0, gate_cfg, pricing_cfg).decision == "publish"
    cond = facts(condition="good", condition_evidence=Ev(value="two scuffs", photos=[4], source="photo", confidence=0.75))
    assert evaluate(cond, OK_PRICE, [], 0, gate_cfg, pricing_cfg).decision == "publish"
    brand = facts(brand=Ev(value="Nike", photos=[3], source="photo", confidence=0.69))
    r = evaluate(brand, OK_PRICE, [], 0, gate_cfg, pricing_cfg)
    assert r.decision == "needs_info" and r.reasons == ["brand unclear (Nike, 0.69)"]


def test_price_never_blocks(facts, gate_cfg, pricing_cfg):
    # The owner approves the price in the Telegram message, so a price-table miss is not a question for the gate:
    # a category default, no price basis at all and a price under the floor all publish when the facts are clean.
    default = PriceResult(target=22, list_price=30, source="category_default")
    assert evaluate(facts(), default, [], 0, gate_cfg, pricing_cfg).decision == "publish"
    none = PriceResult(target=None, list_price=None, source="none", basis="no brand or category price")
    assert evaluate(facts(), none, [], 0, gate_cfg, pricing_cfg).decision == "publish"
    low = PriceResult(target=10, list_price=10, source="brand", by_marketplace={"poshmark": 10})
    assert evaluate(facts(), low, [], 0, gate_cfg, pricing_cfg) == GateResult("publish", [])


def test_lint_or_verifier_edits_make_draft(facts, gate_cfg, pricing_cfg):
    assert evaluate(facts(), OK_PRICE, ["brand missing from title"], 0, gate_cfg, pricing_cfg).decision == "draft"
    assert evaluate(facts(), OK_PRICE, [], 2, gate_cfg, pricing_cfg).decision == "draft"


def test_nwt_with_hang_tag_photo_publishes(facts, gate_cfg, pricing_cfg):
    f = facts(condition="NWT", hang_tag_photo=2,
              condition_evidence=Ev(value="attached hang tag", photos=[2], source="photo", confidence=0.95))
    assert evaluate(f, OK_PRICE, [], 0, gate_cfg, pricing_cfg).decision == "publish"


def test_nwt_needs_hang_tag_photo_not_just_condition_photos(facts, gate_cfg, pricing_cfg):
    # The old rule accepted any condition photo; a box shot or a "looks new" photo is not a hang tag.
    f = facts(condition="NWT", condition_evidence=Ev(value="looks new", photos=[2], source="photo", confidence=0.95))
    r = evaluate(f, OK_PRICE, [], 0, gate_cfg, pricing_cfg)
    assert r.decision == "needs_info" and "NWT claimed without a hang-tag photo" in r.reasons


def test_other_category_needs_info(facts, gate_cfg, pricing_cfg):
    msg = "category/subcategory is 'Other' — pick the real Poshmark category"
    for kw in (dict(category="Other"), dict(category=" other "), dict(category=""), dict(subcategory="Other")):
        r = evaluate(facts(**kw), OK_PRICE, [], 0, gate_cfg, pricing_cfg)
        assert r.decision == "needs_info" and msg in r.reasons, kw
    assert evaluate(facts(subcategory=None), OK_PRICE, [], 0, gate_cfg, pricing_cfg).decision == "publish"
