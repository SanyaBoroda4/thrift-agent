import json

from thrift_agent.brain.copy import changed_fields, clamp_title, clean, ensure_retail_line, facts_view, strip_tag_lines
from thrift_agent.brain.verify import lint, verify
from thrift_agent.schema import CopyOut, Ev, Flaw, VerifyOut


def co(**kw):
    # No material word by default: the facts fixture has no material, and a material word without one is a lint problem.
    base = dict(poshmark_title="Tory Burch Red Bow Ballet Flats size 7.5",
                poshmark_description="Classic flats.\n\nGently pre-loved, please see photos for condition.",
                poshmark_style_tags=["classic"],
                depop_description="cute red flats. Gently pre-loved, please see photos for condition.",
                depop_hashtags=["torybur ch", "flats"])
    base.update(kw)
    return CopyOut(**base)


def ev(value, **kw):
    return Ev(value=value, photos=[3], source="photo", confidence=0.95, **kw)


SCUFF = [Flaw(description="scuff on left toe", photos=[4])]
LINE = "Gently pre-loved, please see photos for condition."


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


def test_a_line_break_written_as_backslash_n_is_a_line_break():
    """WO24, live: the verifier's rewrite left "…a bubble hem.\\nGently pre-loved…" — the two characters — in two
    listings, and counted as a rewrite that reported no claim."""
    written = "A white skirt.\\nGently pre-loved, please see photos for condition."
    draft = clean(co(poshmark_description="A white skirt.\nGently pre-loved, please see photos for condition."))
    audit = VerifyOut(poshmark_title=draft.poshmark_title, poshmark_description=written,
                      depop_description=draft.depop_description)
    assert changed_fields(draft, audit) == []                         # no rewrite
    out = clean(co(poshmark_title="Zara Skirt\\nsize M", poshmark_description=written,
                   depop_description="a white skirt\\r\\nsize m"))
    assert out.poshmark_title == "Zara Skirt size M" and "\\" not in out.poshmark_description + out.depop_description
    assert out.poshmark_description == "A white skirt.\nGently pre-loved, please see photos for condition."
    assert out.depop_description.startswith("a white skirt\nsize m\n\n#")


def test_facts_view_omits_null_facts(facts):
    view = facts_view(facts(material=Ev(), features=[], color_name=None))
    for absent in ("material", "features", "color_name", "style_name", "size_eu", "cover_photo", "photo_order",
                   "questions"):
        assert absent not in view
    assert "material" not in json.dumps(view)
    assert "flaws" not in view and "condition_evidence" not in view          # the photos show them, not the copy
    assert view["condition_line"] == "Gently pre-loved, please see photos for condition."
    assert facts_view(facts(condition="NWOT"))["condition_line"] == "New without tags."
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
    assert "#" not in sent["depop_description"] and sent["depop_description"].startswith("cute red flats")
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


def test_lint_material_words_are_claims(facts):
    def material_probs(text, **kw):
        c = co(poshmark_title="Nike Air Max Sneakers size 7.5", poshmark_description=text,
               depop_description="cute sneakers, light wear on the soles")
        return [p for p in lint(facts(**kw), c) if "material" in p]
    # The first real run: item_type said "leather sneakers", material was null, the copy said leather twice.
    d = "Leather sneakers with a leather heel tab. Condition: excellent, light wear."
    assert material_probs(d, item_type="leather sneakers") == ["material not in facts: leather"]     # once per word
    assert material_probs(d, material=ev("leather upper")) == []
    assert material_probs(d, material=ev("Leather / rubber")) == []
    faux = "Faux leather sneakers. Condition: excellent, light wear."
    assert material_probs(faux, material=ev("faux leather")) == []
    assert material_probs(faux, material=ev("faux-leather")) == []
    assert material_probs(faux, material=ev("leather")) == ["material not in facts: faux leather"]
    canvas = "Canvas sneakers with a rubber sole. Condition: excellent, light wear."
    assert material_probs(canvas, style_name=ev("Canvas")) == ["material not in facts: rubber"]     # a style name
    assert material_probs(canvas, material=ev("canvas upper, rubber sole")) == []
    assert material_probs("Cotton On tee. Condition: excellent, light wear.", brand=ev("Cotton On")) == []
    assert material_probs("Silky soft, furry lining, woolly look. Condition: excellent, light wear.") == []  # not words
    assert material_probs("Wool blend, a silk bow. Condition: excellent, light wear.", material=ev("wool")) == \
        ["material not in facts: silk"]
    # The title, the Depop body and the style tags are checked too.
    assert "material not in facts: suede" in lint(facts(), co(poshmark_title="Tory Burch Red Suede Flats size 7.5"))
    assert "material not in facts: suede" in lint(facts(), co(depop_description="cute red suede flats, light wear"))
    assert "material not in facts: linen" in lint(facts(), co(poshmark_style_tags=["linen"]))
    assert not any("material" in p for p in lint(facts(), co()))


def test_lint_size_accepts_every_form_the_copy_rules_produce(facts):
    msg = "US size missing from title"
    kids = dict(department="Kids", size_eu=ev("24"), size_printed=ev("EU 24"))
    title = "Nike Air Max Kids Sneakers size 24 (US 7.5)"                          # the first real run's title
    assert msg not in lint(facts(**kids), co(poshmark_title=title))
    assert msg not in lint(facts(), co(poshmark_title="Nike Air Max Sneakers size 24 (US 7.5)"))
    assert msg in lint(facts(**kids), co(poshmark_title="Nike Kids Sneakers 7.5"))          # no keyword
    for t in ("Nike Sneakers US 7.5", "Nike Sneakers (US 7.5)", "Nike Sneakers EU 24 / US Toddler 7.5",
              "Nike Sneakers, size 7.5", "Nike Sneakers Size 7.5"):
        assert msg not in lint(facts(**kids), co(poshmark_title=t)), t
    assert msg not in lint(facts(**kids, size_us=ev("13C")), co(poshmark_title="Nike Kids Sneakers US Little Kid 13"))
    assert msg not in lint(facts(**kids, size_us=ev("4Y")), co(poshmark_title="Nike Kids Sneakers US Big Kid 4"))
    assert msg not in lint(facts(**kids, size_us=ev("4Y")), co(poshmark_title="Nike Kids Sneakers size 4Y"))
    assert msg in lint(facts(**kids, size_us=ev("13C")), co(poshmark_title="Nike Kids Sneakers US Little Kid 1"))
    assert msg in lint(facts(**kids), co(poshmark_title="Nike Kids Sneakers US 7.55"))
    assert msg in lint(facts(**kids), co(poshmark_title="Nike Kids Sneakers US 17.5"))


def test_lint_kids_size_needs_its_system(facts):
    msg = "kids size without its system (Toddler / Little Kid / Big Kid)"
    kid = facts(department="Kids", size_eu=ev("24"))
    assert msg in lint(kid, co(poshmark_title="Nike Kids Sneakers size 7.5"))
    assert msg in lint(kid, co(poshmark_title="Nike Kids Sneakers US 7.5"))
    assert msg in lint(kid, co(poshmark_title="Nike Air Max Kids Sneakers size 24 (US 7.5)"))    # "24" is not "EU 24"
    assert msg not in lint(kid, co(poshmark_title="Nike Kids Sneakers EU 24 / US Toddler 7.5"))
    assert msg not in lint(kid, co(poshmark_title="Nike Kids Sneakers US Toddler 7.5"))
    assert msg not in lint(kid, co(poshmark_title="Nike Kids Sneakers Toddler size 7.5"))
    assert msg in lint(kid, co(poshmark_title="Nike Kids Sneakers EU 24 US 7.5"))                # EU is not the system
    assert msg not in lint(facts(), co(poshmark_title="Tory Burch Flats size 7.5"))               # not Kids
    tee = facts(department="Kids", category="Tops", size_us=ev("5"))
    assert msg not in lint(tee, co(poshmark_title="Nike Kids Tee size 5"))                      # clothing has no groups
    assert not any("size" in p for p in lint(kid, co(poshmark_title="Nike Kids Sneakers 7.5")) if "system" in p)


def test_lint_kids_size_group_is_poshmarks(facts):
    """Decision (WO10): the title's group is Poshmark's. Nike's chart calls 11C "Little Kid"; Poshmark says Toddler."""
    def kid(size):
        return facts(department="Kids", size_us=ev(size), size_printed=ev(size), size_eu=Ev())

    assert "kids size group in the title is Little Kid, Poshmark's is Toddler" in lint(
        kid("11C"), co(poshmark_title="Nike Kids Sneakers Little Kid size 11"))
    assert "kids size group in the title is Big Kid, Poshmark's is Little Kid" in lint(
        kid("2Y"), co(poshmark_title="Nike Kids Sneakers US Big Kid 2"))
    assert "kids size group in the title is Little Kid / Toddler, Poshmark's is Big Kid" in lint(
        kid("4Y"), co(poshmark_title="Nike Toddler Little Kid Sneakers Big Kid size 4"))
    for size, title in (("11C", "Nike Kids Sneakers Toddler size 11"), ("5C", "Nike Kids Sneakers Toddler size 5"),
                        ("13C", "Nike Kids Sneakers Little Kid size 13"), ("2Y", "Nike Kids Sneakers US Little Kid 2"),
                        ("4Y", "Nike Kids Sneakers big kid size 4")):
        assert not any("group" in p for p in lint(kid(size), co(poshmark_title=title))), title
    tee = facts(department="Kids", category="Tops", size_us=ev("5"))
    assert not any("group" in p for p in lint(tee, co(poshmark_title="Nike Kids Toddler Tee size 5")))   # no groups


def test_facts_view_carries_the_size_label(facts):
    assert facts_view(facts())["size_label"] == "7.5"
    kid = facts(department="Kids", size_eu=ev("24"), size_printed=ev("EU 24"))
    assert facts_view(kid)["size_label"] == "EU 24 / US Toddler 7.5"
    assert "size_label" not in facts_view(facts(size_us=Ev()))


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


def test_a_flaw_is_disclosed_by_the_condition_line_and_its_photo_in_the_listing(facts):
    """The owner's rule: one neutral line, and the flaw's photo in the listing — never words. The cover counts too
    (WO23): the front stays the cover even when a flaw shows on it."""
    assert lint(facts(flaws=SCUFF), co(), photos=[0, 1, 4]) == []
    assert lint(facts(flaws=SCUFF), co(depop_description="cute red flats, super comfy, chic"), photos=[0, 4]) == \
        ["facts list flaws but the depop description lacks the condition line"]
    assert lint(facts(flaws=SCUFF), co(), photos=[4, 0, 1]) == []                    # its photo is the cover: shown
    assert lint(facts(flaws=SCUFF), co(), photos=[0, 1, 2]) == ["flaw photos not in the listing: scuff on left toe"]
    assert lint(facts(flaws=[Flaw(description="small hole (seller note)")]), co(), photos=[0]) == []   # a Note instead
    assert lint(facts(flaws=SCUFF), co()) == []                          # no photo list given: the line alone


def test_wear_words_are_never_in_the_copy(facts):
    probs = lint(facts(flaws=SCUFF), co(poshmark_title="Tory Burch Red Flats Worn Once size 7.5",
                                        poshmark_description="Classic flats, small scuff on the left toe. " + LINE,
                                        depop_description="cute red flats, light wear, a few pills " + LINE,
                                        depop_hashtags=["flats", "worn"]))
    assert probs == ["wear words in the title: worn", "wear words in the poshmark description: scuff",
                     "wear words in the depop description: light wear, pills", "wear words in the tags: worn"]


def test_wear_words_are_whole_words(facts):
    for text in ("Great activewear and footwear for the market.", "Pillow soft, a whole lot of love.",
                 "Stainless steel hardware, tearsheet included.", "Faded wash, stretch denim, mint green.",
                 "Perfect for everyday wear.", "Marks & Spencer wrinkle-free shirt."):
        assert not any("wear words" in p for p in lint(facts(), co(poshmark_description=text + " " + LINE))), text
    for text, word in (("Light wear on soles.", "light wear"), ("A scuffed toe.", "scuffed"),
                       ("Small stain on the lining.", "stain"), ("Some wear and tear.", "some wear and tear"),
                       ("A few pills on the cuffs.", "pills"), ("Smells musty.", "musty, smells"),
                       ("Never worn.", "worn"), ("Fraying straps.", "fraying"), ("A tiny hole.", "hole")):
        assert f"wear words in the poshmark description: {word}" in lint(facts(), co(poshmark_description=text)), text


def test_lint_condition_ladder(facts):
    d = "Classic flats, like new."
    assert "a used item claims like new" in lint(facts(), co(poshmark_description=d))
    assert "a used item claims like new" in lint(facts(condition="like_new"), co(poshmark_description=d))
    assert "a used item claims excellent, no flaws" in lint(facts(condition="good"), co(
        poshmark_description="Excellent shape, no flaws. " + LINE))
    assert not any("claims" in p for p in lint(facts(condition="NWOT"), co(poshmark_description="Like new.")))
    assert "copy claims NWOT but facts are excellent" in lint(facts(), co(poshmark_description="Never worn. Red flats."))
    assert "says NWT but facts aren't NWT" in lint(facts(condition="NWOT"), co(poshmark_description="New with tags flats."))
    assert not any("claims" in p or "NWT" in p for p in lint(facts(condition="NWT"),
                                                              co(poshmark_description="Brand new, NWT red flats.")))
    assert any("title starts with New" in p for p in lint(facts(), co(poshmark_title="New Tory Burch Flats size 7.5")))
    assert not any("title starts with New" in p
                   for p in lint(facts(condition="NWOT"), co(poshmark_title="New Tory Burch Flats size 7.5")))
    assert not any("New" in p for p in lint(facts(), co(poshmark_title="Newport Tory Burch Flats size 7.5")))


def test_lint_length_floor(facts):
    assert "poshmark description too short" in lint(facts(), co(poshmark_description="Red flats."))
    assert "depop description too short" in lint(facts(), co(depop_description="red flats\n\n#a #b #c #d #e"))
    assert not any("too short" in p for p in lint(facts(), co()))


def test_lint_hardware_colors_and_brand_color_words(facts):
    bag = facts(item_type="leather shoulder bag", colors=["Black"], material=ev("leather"))
    hardware = co(poshmark_title="Tory Burch Black Leather Shoulder Bag size 7.5",
                  poshmark_description="Black leather bag with gold-tone hardware. Condition: excellent, light wear.",
                  depop_description="black leather bag, gold hardware, silver zip, light wear on the corners")
    assert not any("color" in p for p in lint(bag, hardware))
    clutch = co(poshmark_title="Tory Burch Black Leather Bag size 7.5",
                poshmark_description="Gold clutch with a black strap. Condition: excellent, light wear.",
                depop_description="black leather bag, light wear on the corners")
    assert "color words not in facts: ['gold']" in lint(bag, clutch)
    jeans = facts(brand=ev("Silver Jeans Co"), item_type="bootcut jeans", colors=["Blue"])
    silver = co(poshmark_title="Silver Jeans Co Bootcut Jeans size 7.5",
                poshmark_description="Blue bootcut jeans. Condition: excellent, light wear.",
                depop_description="blue bootcut jeans, light wear at the hems")
    assert not any("color" in p for p in lint(jeans, silver))


def test_lint_shades_normalise_to_the_palette(facts):
    def color_probs(colors, text):
        c = co(poshmark_title="Tory Burch Blazer size 7.5", poshmark_description=text,
               depop_description="cute blazer, light wear on the cuffs")
        return [p for p in lint(facts(colors=colors), c) if "color" in p]
    assert color_probs(["Blue"], "Navy blazer, one button. Condition: excellent, light wear.") == []
    assert color_probs(["Green"], "Olive blazer. Condition: excellent, light wear.") == []
    assert color_probs(["Red"], "Olive blazer. Condition: excellent, light wear.") == ["color words not in facts: ['olive']"]
    assert color_probs(["Cream"], "Ivory blazer. Condition: excellent, light wear.") == []
    assert color_probs(["Gray"], "Charcoal blazer. Condition: excellent, light wear.") == []
    assert color_probs(["Tan"], "Off-white blazer. Condition: excellent, light wear.") == []      # tan/cream leniency
    rose_gold = color_probs(["Black"], "Rose gold blazer. Condition: excellent, light wear.")
    assert rose_gold == ["color words not in facts: ['rose gold']"]                  # matched as a unit, not "gold"


def test_lint_authentic_allowed_when_it_is_the_style_name(facts):
    vans = dict(brand=ev("Vans"), colors=["Black"], size_us=ev("8"), material=ev("canvas"))
    sneakers = co(poshmark_title="Vans Authentic Black Canvas Sneakers size 8",
                  poshmark_description="Black canvas sneakers. Condition: excellent, light wear.",
                  depop_description="black canvas sneakers, light wear on the soles")
    assert not any("authentic" in p for p in lint(facts(style_name=ev("Authentic"), **vans), sneakers))
    assert "unsupported phrase: authentic" in lint(facts(**vans), sneakers)


def test_lint_style_name_must_be_in_title(facts):
    arizona = facts(brand=ev("Birkenstock"), style_name=ev("Arizona"), item_type="sandals")
    without = co(poshmark_title="Birkenstock Red Suede Sandals size 7.5")
    assert "style name missing from title" not in lint(arizona, co(poshmark_title="Birkenstock Arizona Red Sandals size 7.5"))
    assert "style name missing from title" in lint(arizona, without)
    assert "style name missing from title" not in lint(facts(), without)            # no style name known


def test_ensure_retail_line(facts):
    priced = facts(retail_price=ev("128"))
    assert ensure_retail_line("Classic flats.\n\nWorn once.  \n", priced) == "Classic flats.\n\nWorn once.\nOriginal retail $128."
    assert ensure_retail_line("Classic flats. Retail $128.", priced) == "Classic flats. Retail $128."
    assert ensure_retail_line("Classic flats. retails for $128", priced) == "Classic flats. retails for $128"
    assert ensure_retail_line("Classic flats.", facts()) == "Classic flats."
    assert ensure_retail_line("Classic flats.", facts(retail_price=ev("n/a"))) == "Classic flats."
    assert ensure_retail_line("Classic flats.", facts(retail_price=ev("$1,299.00"))) == "Classic flats.\nOriginal retail $1299."


def test_title_shows_the_us_size_only(facts):
    """Owner rule: never an EU size in the title. Kids: 'Toddler size 7.5'; adults: 'size 7.5'. The EU size goes in
    the description."""
    kid = facts(department="Kids", category="Shoes", colors=["Pink"],
                brand=ev("Naturino"), style_name=ev("Glitter Star"),
                size_us=Ev(value="7.5", photos=[1], source="derived", confidence=0.8),
                size_eu=Ev(value="24", photos=[1], source="photo", confidence=0.9))
    desc = "Pink glitter star sneakers. EU 24 / US Toddler 7.5.\nCondition: excellent, light wear."
    ok = lint(kid, co(poshmark_title="Naturino Glitter Star Pink Sneakers Toddler size 7.5", poshmark_description=desc,
                      depop_description="pink naturino glitter star sneakers, toddler 7.5, light wear"))
    assert not [p for p in ok if "size" in p], ok
    bad = lint(kid, co(poshmark_title="Naturino Glitter Star Pink Sneakers EU 24 / US Toddler 7.5",
                       poshmark_description=desc, depop_description="pink naturino glitter star sneakers, light wear"))
    assert any("US size only" in p and "EU 24" in p for p in bad)
    bad = lint(kid, co(poshmark_title="Naturino Glitter Star Kids Pink Sneakers size 24 (US 7.5)",
                       poshmark_description=desc, depop_description="pink naturino glitter star sneakers, light wear"))
    assert any("US size only" in p and "size 24" in p for p in bad)

    adult = facts(size_eu=Ev(value="38", photos=[3], source="photo", confidence=0.9))
    desc = "Red flats.\nSize 38 EU, fits US 7.5.\nCondition: excellent, light sole wear."
    ok = lint(adult, co(poshmark_title="Tory Burch Red Ballet Flats size 7.5", poshmark_description=desc,
                        depop_description="red tory burch flats, eu 38 / us 7.5, light wear"))
    assert not [p for p in ok if "size" in p], ok
    bad = lint(adult, co(poshmark_title="Tory Burch Red Ballet Flats EU 38 size 7.5", poshmark_description=desc,
                         depop_description="red tory burch flats, light wear"))
    assert any("US size only" in p and "EU 38" in p for p in bad)
    bad = lint(adult, co(poshmark_title="Tory Burch Red Ballet Flats size 38", poshmark_description=desc,
                         depop_description="red tory burch flats, light wear"))
    assert "US size missing from title" in bad and any("found size 38" in p for p in bad)


def test_style_tags_are_poshmarks_curated_ones_and_materials_need_evidence(facts):
    """WO12: only the 130 tags Poshmark offers, spelled its way, at most 3; a material tag only with evidence."""
    from thrift_agent.brain.verify import fit_style_tags

    tags = ["casual", "Leather", "boho", "Y2K", "Casual", "vintage", "Floral"]
    assert fit_style_tags(tags, facts()) == ["Casual", "Y2K", "Vintage"]           # Leather: no label says so
    on_label = facts(material=ev("leather upper, rubber sole"))
    assert fit_style_tags(tags, on_label) == ["Casual", "Leather", "Y2K"]
    assert fit_style_tags(["Faux Fur", "Sherpa"], facts(material=ev("100% polyester fur"))) == ["Sherpa"]
    assert fit_style_tags(["Faux Fur"], facts(material=ev("faux fur"))) == ["Faux Fur"]
    assert fit_style_tags(["Denim"], facts(brand=ev("Silver Jeans Co"))) == []            # "denim" is a material
    assert fit_style_tags([], facts()) == []


def test_the_copy_prompt_offers_only_poshmarks_style_tags(facts, monkeypatch):
    from thrift_agent.brain import copy as copywriter
    seen = {}

    def ask(model, system, content, out, tool, description, **kw):
        seen.update(system=system, text=content[0]["text"])
        return CopyOut(poshmark_title="t", poshmark_description="d", depop_description="d", depop_hashtags=[])
    monkeypatch.setattr("thrift_agent.brain.llm.ask", ask)
    copywriter.write(facts(), "model", {"poshmark_footer": "", "depop_footer": ""})
    assert "POSHMARK STYLE TAGS below" in seen["system"]
    assert "POSHMARK STYLE TAGS (the only ones Poshmark offers): 70s, 80s, 90s, Activewear," in seen["text"]


# ---------------------------------------------------------------- WO16: the owner's condition rule

# Shaped like the first live listing's description (the real text stays in private/NOTES.md).
LIVE = ("Glitter star sneakers with silver accents. Fun everyday shoe for a little one.\n"
        "Size 24 EU, fits US Toddler 7.5. Good used condition, worn with dirt and scuffing on the soles, light "
        "staining on the toe, and some fraying at the straps.")


def test_condition_wording_on_a_description_like_the_first_live_listings():
    from thrift_agent.brain.copy import condition_wording
    assert condition_wording(LIVE, "good") == (
        "Glitter star sneakers with silver accents. Fun everyday shoe for a little one.\n"
        "Size 24 EU, fits US Toddler 7.5. Gently pre-loved, please see photos for condition.")


def test_condition_wording_places_the_line_and_never_rewrites():
    from thrift_agent.brain.copy import condition_wording
    assert condition_wording("Classic flats. Perfect for everyday wear.\nRetail $228.", "excellent") == \
        "Classic flats. Perfect for everyday wear.\n" + LINE + "\nRetail $228."            # before Retail, if missing
    assert condition_wording("Classic red flats, like new.\n\nRetail $228.", "like_new") == LINE + "\n\nRetail $228."
    assert condition_wording("Red flats. " + LINE, "good") == "Red flats. " + LINE           # already there: as is
    assert condition_wording("New with tags. Never worn, still boxed.", "NWT") == "New with tags."   # no line for new
    assert condition_wording("cute flats, light wear on the soles", "fair") == LINE
    assert condition_wording("Smells musty. Holes. Stains.", "good") == LINE                 # one line, not three


def test_condition_rule_cleans_both_descriptions_and_the_tags(facts):
    from thrift_agent.brain.copy import condition_rule
    out = condition_rule(clean(co(poshmark_description="Red flats. Light wear on the soles.",
                                  depop_description="red flats, scuffed toe\n\n#flats #scuffed",
                                  poshmark_style_tags=["Classic"], depop_hashtags=["flats", "scuffed", "worn"])),
                         facts(condition="good"))
    assert out.poshmark_description == "Red flats. " + LINE
    assert out.depop_description == LINE + "\n\n#flats" and out.depop_hashtags == ["flats"]
    assert lint(facts(condition="good"), out) == []


def test_both_prompts_carry_the_owners_condition_rule():
    from thrift_agent.brain import copy as copywriter, verify as verifier
    assert "condition_line" in copywriter.SYSTEM and "never put in words" in copywriter.SYSTEM
    assert "never \"like new\", \"excellent\"" in copywriter.SYSTEM.replace("is never", "never")
    assert "Never add or restore a description of wear" in verifier.SYSTEM
    assert "missing flaws added" not in verifier.SYSTEM and "mention every flaw" not in copywriter.SYSTEM



# ---------------------------------------------------------------- WO18: the copy for new shoes

def test_the_condition_line_for_new_items(facts):
    from thrift_agent.brain.copy import condition_line
    assert condition_line(facts(condition="NWT")) == "New with tags."
    assert condition_line(facts(condition="NWT", box_photo=3)) == "New in box."             # the box is in the photos
    assert condition_line(facts(condition="NWOT", box_photo=3)) == "New without tags."
    assert condition_line(facts(condition="good")) == LINE
    view = facts_view(facts(condition="NWT", box_photo=3, unworn=ev("yes"), condition_alternative="NWOT"))
    assert view["condition_line"] == "New in box."
    assert not {"unworn", "box_photo", "condition_alternative"} & set(view)                  # evidence, not copy
