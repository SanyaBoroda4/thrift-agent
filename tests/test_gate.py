from thrift_agent.brain.gate import SIZE_NOTE, GateResult, evaluate
from thrift_agent.schema import Ev, PriceResult

OK_PRICE = PriceResult(target=70, list_price=85, source="brand", by_marketplace={"poshmark": 85})


def test_clean_item_publishes(facts, gate_cfg, pricing_cfg):
    assert evaluate(facts(), OK_PRICE, [], 0, gate_cfg, pricing_cfg).decision == "publish"


def test_an_unsure_or_unread_size_is_a_note_never_a_question(facts, gate_cfg, pricing_cfg):
    """WO33, the owner's rule: never ask about size, never hold — the best reading is listed and the card says so."""
    for ev, said in ((Ev(value=None, confidence=0.2), "none read"), (Ev(value="M", confidence=0.4), "M (0.40)")):
        r = evaluate(facts(size_us=ev), OK_PRICE, [], 0, gate_cfg, pricing_cfg)
        assert r.decision == "publish" and r.questions == [] and not any("size" in x for x in r.reasons)
        assert f"{SIZE_NOTE} {said}" in r.notes


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


def test_the_copy_check_never_holds_its_findings_are_kept_for_the_record(facts, gate_cfg, pricing_cfg):
    """WO33, the owner's rule: the copy check fixes what code can before the gate and never holds the item."""
    r = evaluate(facts(), OK_PRICE, ["brand missing from title"], 0, gate_cfg, pricing_cfg)
    assert (r.decision, r.reasons) == ("publish", ["brand missing from title"])
    r = evaluate(facts(), OK_PRICE, [], 2, gate_cfg, pricing_cfg)
    assert r.decision == "publish" and "verifier removed 2" in r.reasons[0]


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


def test_model_questions_about_optional_facts_are_not_asked(facts, gate_cfg, pricing_cfg):
    """Owner rule: the only questions are brand/size < 0.70, NWT without a tag photo, category Other (plus the
    pipeline's re-share and the poster's needs_owner). A missing optional fact is simply left out."""
    f = facts(questions=["Material composition not visible on any label - is there a fabric content tag?",
                         "What are the measurements?"])
    r = evaluate(f, OK_PRICE, [], 0, gate_cfg, pricing_cfg)
    assert r.decision == "publish" and r.reasons == []


def test_unsure_condition_is_a_note_not_a_question(facts, gate_cfg, pricing_cfg):
    f = facts(condition="good", condition_evidence=Ev(value="some wear", photos=[4], source="photo", confidence=0.55))
    r = evaluate(f, OK_PRICE, [], 0, gate_cfg, pricing_cfg)
    assert r.decision == "publish" and r.reasons == []
    assert any("unsure of the condition (0.55)" in n and "listed as good" in n for n in r.notes)


def test_an_unsure_condition_note_names_the_grade_it_was_weighed_against(facts, gate_cfg, pricing_cfg):
    from thrift_agent.brain.gate import evaluate
    from thrift_agent.schema import Ev, PriceResult
    f = facts(condition="like_new", condition_alternative="good",
              condition_evidence=Ev(value="clean soles", photos=[4], source="photo", confidence=0.6))
    pr = PriceResult(target=70, list_price=85, source="brand", by_marketplace={"poshmark": 85})
    notes = evaluate(f, pr, [], 0, gate_cfg, pricing_cfg).notes
    assert notes == ["model unsure of the condition (0.60, weighed against good): listed as like_new; reply with the "
                     "condition if it is wrong"]



def test_the_owners_answer_is_proof_enough_for_nwt(facts, gate_cfg, pricing_cfg):
    from thrift_agent.brain.gate import evaluate
    from thrift_agent.schema import Ev, PriceResult
    pr = PriceResult(target=70, list_price=85, source="brand", by_marketplace={"poshmark": 85})
    owner = facts(condition="NWT", condition_evidence=Ev(value="owner: new with tags", source="owner", confidence=1.0))
    model = facts(condition="NWT", condition_evidence=Ev(value="looks new", photos=[2], source="photo",
                                                         confidence=0.9))
    assert not any("hang-tag" in r for r in evaluate(owner, pr, [], 0, gate_cfg, pricing_cfg).reasons)
    assert any("hang-tag" in r for r in evaluate(model, pr, [], 0, gate_cfg, pricing_cfg).reasons)
