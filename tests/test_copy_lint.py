import json

from thrift_agent.brain.copy import changed_fields, clamp_title, clean, facts_view, strip_tag_lines
from thrift_agent.brain.verify import lint, verify
from thrift_agent.schema import CopyOut, Ev, Flaw, VerifyOut


def co(**kw):
    base = dict(poshmark_title="Tory Burch Red Suede Bow Ballet Flats size 7.5",
                poshmark_description="Classic flats.\n\nCondition: excellent, light wear on soles.",
                poshmark_style_tags=["classic"], depop_description="cute red suede flats, light wear on the soles",
                depop_hashtags=["torybur ch", "flats"])
    base.update(kw)
    return CopyOut(**base)


def ev(value, **kw):
    return Ev(value=value, photos=[3], source="photo", confidence=0.95, **kw)


SCUFF = [Flaw(description="scuff on left toe", photos=[4])]


def test_title_cleanup():
    assert clamp_title("COPY - Nike Cortez size 7,5") == "Nike Cortez size 7.5"
    long = "Tory Burch " + "very " * 30 + "flats"
    assert len(clamp_title(long)) <= 80 and not clamp_title(long).endswith(" ")


def test_depop_hashtags_and_limit():
    out = clean(co(depop_description="x " * 800 + "#old #tags", depop_hashtags=["Tory Burch", "flats", "red", "y2k", "shoes"]))
    assert len(out.depop_description) <= 1000
    assert out.depop_description.endswith("#toryburch #flats #red #y2k #shoes")
    assert "#old" not in out.depop_description


def test_lint(facts):
    assert lint(facts(), co()) == []
    probs = lint(facts(flaws=[Flaw(description="scuff on left toe")]),
                 co(poshmark_title="Red Flats", poshmark_description="Smoke-free home. NWT."))
    joined = " ".join(probs)
    assert "brand missing" in joined and "US size missing" in joined and "NWT" in joined
    assert "smoke" in joined and "flaws" in joined


def test_lint_catches_past_mistakes(facts):
    # Typical AI-copy errors: women's sandals described as kids' sandals,
    # white sneakers described as light gray.
    kids = lint(facts(), co(poshmark_description="Stylish purple kids sandals. Condition: excellent, light wear."))
    assert any("kids" in p for p in kids)
    color = lint(facts(), co(poshmark_description="Light gray flats. Condition: excellent, light wear."))
    assert any("color" in p for p in color)


def test_title_hard_limit_without_spaces():
    assert len(clamp_title("x" * 120)) == 80
    assert len(clamp_title("a" * 79 + " " + "b" * 10)) <= 80


def test_style_tags_are_truncated_not_rejected():
    out = clean(co(poshmark_style_tags=["a", "b", "c", "d"]))       # 4 tags must validate, then be cut to 3
    assert out.poshmark_style_tags == ["a", "b", "c"]


def test_depop_limit_without_spaces():
    out = clean(co(depop_description="y" * 1500, depop_hashtags=["a", "b"]))
    assert len(out.depop_description) <= 1000 and out.depop_description.endswith("#a #b")
    assert clean(co(depop_description="plain", depop_hashtags=[])).depop_description == "plain"


def test_tag_lines_stripped_anywhere_in_body():
    # The verifier appended a sentence below the model's tag line: those tags must not survive into the body.
    body = "cute flats\n\n#a #b #c #d #e\n\nsmall scuff on the left toe"
    assert strip_tag_lines(body) == "cute flats\n\nsmall scuff on the left toe"
    out = clean(co(depop_description=body, depop_hashtags=["a", "b"]))
    assert out.depop_description.endswith("#a #b") and out.depop_description.count("#") == 2
    assert strip_tag_lines("cute flats #old #tags") == "cute flats"          # tacked onto the last sentence
    assert strip_tag_lines("#1 pick of the season") == "#1 pick of the season"   # not a tag line


def test_changed_fields_ignores_whitespace_case_and_tag_line():
    draft = clean(co(depop_description="cute red suede flats, light wear", depop_hashtags=["a", "b"]))
    assert draft.depop_description.endswith("\n\n#a #b")
    same = VerifyOut(poshmark_title=draft.poshmark_title.upper(),
                     poshmark_description=" ".join(draft.poshmark_description.split()),
                     depop_description="  cute red suede flats,\nlight wear ")
    assert changed_fields(draft, same) == []
    edited = VerifyOut(poshmark_title="Tory Burch Red Suede Flats size 7.5", poshmark_description=draft.poshmark_description,
                       depop_description="cute red suede flats, light wear, small scuff")
    assert changed_fields(draft, edited) == ["poshmark_title", "depop_description"]


def test_facts_view_omits_null_facts(facts):
    view = facts_view(facts(material=Ev(), features=[], color_name=None))
    for absent in ("material", "features", "color_name", "style_name", "size_eu", "cover_photo", "photo_order",
                   "questions"):
        assert absent not in view
    assert "material" not in json.dumps(view)
    assert view["flaws"] == [] and view["condition_label"] == "Excellent used condition"
    assert view["brand"]["value"] == "Tory Burch" and view["colors"] == ["Red"]
    assert facts_view(facts(material=ev("Suede")))["material"]["value"] == "Suede"


def test_verify_sends_depop_body_without_tags_and_includes_style_tags(facts, monkeypatch):
    seen = {}

    def fake_ask(model, system, content, out, tool, description, **kw):
        seen["text"] = content[0]["text"]
        return VerifyOut(poshmark_title="t", poshmark_description="d", depop_description="d")
    monkeypatch.setattr("thrift_agent.brain.llm.ask", fake_ask)
    draft = clean(co(poshmark_style_tags=["classic", "preppy"], depop_hashtags=["a", "b", "c"]))
    verify(facts(), draft, "m")
    sent = json.loads(seen["text"].split("\n\nCOPY\n", 1)[1])
    assert "#" not in sent["depop_description"] and sent["depop_description"].startswith("cute red suede flats")
    assert sent["poshmark_style_tags"] == ["classic", "preppy"]


def test_lint_brand_blank_and_normalised(facts):
    blank = Ev.model_construct(value="   ", photos=[], source="photo", confidence=0.9)   # bypasses the Ev validator
    assert "brand missing from title" not in lint(facts(brand=blank), co())                 # no IndexError either
    assert "brand missing from title" not in lint(facts(brand=ev("Levi's")), co(poshmark_title="Levi’s 501 Jeans size 7.5"))
    assert "brand missing from title" not in lint(facts(brand=ev("J. Crew")), co(poshmark_title="J.Crew Sweater size 7.5"))
    assert "brand missing from title" in lint(facts(brand=ev("J. Crew")), co(poshmark_title="Jacket size 7.5"))
    assert "brand missing from title" in lint(facts(), co(poshmark_title="Red Flats size 7.5"))


def test_lint_size_check_is_not_vacuous(facts):
    eight, m = facts(size_us=ev("8")), facts(size_us=ev("M"))
    msg = "US size missing from title"
    assert msg in lint(eight, co(poshmark_title="Tory Burch Flats size 98"))
    assert msg in lint(eight, co(poshmark_title="Tory Burch Flats size 8.5"))
    assert msg in lint(m, co(poshmark_title="Madewell Tory Burch Top"))
    assert msg not in lint(eight, co(poshmark_title="Tory Burch Flats Size 8"))
    assert msg not in lint(m, co(poshmark_title="Tory Burch Top size M"))
    assert msg not in lint(facts(), co())                                     # "size 7.5"


def test_lint_kids_words(facts):
    assert not any("kids" in p for p in lint(facts(), co(poshmark_description="Soft kid leather flats, light wear.")))
    assert any("kids" in p for p in lint(facts(), co(poshmark_description="Cute kid's flats, light wear on soles.")))
    assert any("kids" in p for p in lint(facts(), co(poshmark_description="Cute girls' flats, light wear on soles.")))


def test_lint_100_percent_needs_the_fiber_on_the_label(facts):
    d = "100% Cotton tee, red. Condition: excellent."
    assert "unsupported phrase: 100% cotton" in lint(facts(), co(poshmark_description=d))
    assert "unsupported phrase: 100% cotton" not in lint(facts(material=ev("100% Cotton")), co(poshmark_description=d))
    assert "unsupported phrase: 100% cotton" not in lint(facts(material=ev("cotton")), co(poshmark_description=d))
    assert "unsupported phrase: 100% silk" in lint(facts(material=ev("cotton")),
                                                    co(poshmark_description="100% silk blouse. Condition: excellent."))


def test_lint_banned_phrases_in_hashtags_and_style_tags(facts):
    smoke = lint(facts(), co(depop_description="cute red flats, light wear on the soles\n\n#smokefree #flats"))
    assert "unsupported phrase: #smokefree" not in smoke and any("smokefree" in p for p in smoke)
    assert any("pet free" in p for p in lint(facts(), co(poshmark_description="Pet free home. Light wear on soles.")))
    assert "unsupported phrase: authentic" in lint(facts(), co(poshmark_style_tags=["authentic"]))
    assert any("color" in p for p in lint(facts(), co(poshmark_style_tags=["navy"])))


def test_lint_flaws_checked_on_both_descriptions(facts):
    probs = lint(facts(flaws=SCUFF), co(poshmark_description="Classic flats, small scuff on the left toe.",
                                        depop_description="cute red suede flats, super comfy"))
    assert probs == ["facts list flaws but the depop description doesn't mention any"]
    probs = lint(facts(flaws=SCUFF), co(poshmark_description="Classic red suede flats, super comfy.",
                                        depop_description="cute red suede flats, scuffed left toe"))
    assert probs == ["facts list flaws but the poshmark description doesn't mention any"]


def test_lint_flaw_words_are_whole_words(facts):
    for text in ("Great activewear and footwear for the market.", "Pillow soft, a whole lot of love.",
                 "Stainless steel hardware, tearsheet included."):
        assert any("flaws" in p for p in lint(facts(flaws=SCUFF), co(poshmark_description=text)))
    for text in ("Light wear on soles and a scuffed toe.", "Small stain on the lining.", "One faded spot, see photo.",
                 "A few pills on the cuffs, a mark on the heel."):
        assert not any("poshmark description doesn't mention" in p for p in lint(facts(flaws=SCUFF),
                                                                                  co(poshmark_description=text)))


def test_lint_condition_ladder(facts):
    d = "Classic flats, like new. Light wear on soles."
    assert "copy claims like_new but facts are excellent" in lint(facts(), co(poshmark_description=d))
    assert "copy claims NWOT but facts are excellent" in lint(facts(), co(poshmark_description="Never worn. Red flats."))
    assert "says NWT but facts aren't NWT" in lint(facts(condition="NWOT"), co(poshmark_description="New with tags flats."))
    assert not any("claims" in p or "NWT" in p for p in lint(facts(condition="NWT"),
                                                              co(poshmark_description="Brand new, NWT red flats.")))
    assert not any("claims" in p for p in lint(facts(condition="like_new"), co(poshmark_description=d)))
    assert any("title starts with New" in p for p in lint(facts(), co(poshmark_title="New Tory Burch Flats size 7.5")))
    assert not any("title starts with New" in p
                   for p in lint(facts(condition="NWOT"), co(poshmark_title="New Tory Burch Flats size 7.5")))
    assert not any("New" in p for p in lint(facts(), co(poshmark_title="Newport Tory Burch Flats size 7.5")))


def test_lint_length_floor(facts):
    assert "poshmark description too short" in lint(facts(), co(poshmark_description="Red flats."))
    assert "depop description too short" in lint(facts(), co(depop_description="red flats\n\n#a #b #c #d #e"))
    assert not any("too short" in p for p in lint(facts(), co()))
