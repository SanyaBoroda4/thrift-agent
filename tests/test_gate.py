from thrift_agent.brain.gate import evaluate
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


def test_category_default_price_blocked(facts, gate_cfg, pricing_cfg):
    p = PriceResult(target=45, list_price=55, source="category_default")
    assert evaluate(facts(), p, [], 0, gate_cfg, pricing_cfg).decision == "needs_info"


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
