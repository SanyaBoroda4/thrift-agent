"""WO27: the poster never stops to ask (brands spelled Poshmark's way, the nearest size, the closest subcategory), and the
content rules (brand-first titles, a set's bottom decides its category, denim and a cutoff's raw hem, the verifier's
false alarm, the sample line). Pure functions; no network, no browser (the form itself: test_poshmark_form)."""
import pytest

from thrift_agent import brands, pipeline
from thrift_agent.brain import copy as copywriter, premium
from thrift_agent.brain.verify import lint, unsupported_materials
from thrift_agent.post.fallback import closest, nearest_size
from thrift_agent.post.runner import condition_rule_breaks
from thrift_agent.schema import CopyOut, Ev, Fiber, Premium, Render, VerifyOut

LINE = "Gently pre-loved, please see photos for condition."


# ---------- 1a/1d. brands ----------

@pytest.mark.parametrize("ours,options,choice,guess", [
    ("J.Crew", ["J. Crew", "J. Crew Factory"], "J. Crew", "brand set to 'J. Crew' (from 'J.Crew')"),
    ("J.Crew", ["J. Crew Factory", "J. Crew"], "J. Crew", "brand set to 'J. Crew' (from 'J.Crew')"),   # never Factory
    ("J. Crew", ["J. Crew", "J. Crew Factory"], "J. Crew", None),                    # the same spelling: nothing to say
    ("J.Crew Factory", ["J. Crew", "J. Crew Factory"], "J. Crew Factory",
     "brand set to 'J. Crew Factory' (from 'J.Crew Factory')"),
    ("levi's", ["Levi's", "Levi's Kids"], "Levi's", "brand set to 'Levi's' (from 'levi's')"),
    ("Levis", ["Levi's"], "Levi's", "brand set to 'Levi's' (from 'Levis')"),
    ("Solid & Striped", ["Solid and Striped"], "Solid and Striped",
     "brand set to 'Solid and Striped' (from 'Solid & Striped')"),
    ("Zara Basic", ["Zara", "Zara Kids"], "Zara", "brand set to 'Zara' (from 'Zara Basic')"),
    ("Gap Kids", ["Gap", "Gap Kids"], "Gap Kids", None),                              # ours has the qualifier
    ("Mistguided", ["Missguided", "Misguided Angels"], "Missguided", "brand set to 'Missguided' (from 'Mistguided')"),
])
def test_a_brand_is_picked_by_its_normalised_name_never_another_line(ours, options, choice, guess):
    assert brands.pick(ours, options) == (choice, guess)


@pytest.mark.parametrize("ours,options,why", [
    ("Gap", ["Gap Kids", "GAP Factory"], "brand left empty: Poshmark's list has no 'Gap' (it offers: Gap Kids, "
                                         "GAP Factory)"),
    ("Zzyzx", [], "brand left empty: Poshmark's list has no 'Zzyzx'"),
    ("Tory", ["Tory Burch", "Tory Sport"], "brand left empty: Poshmark's list has no 'Tory' (it offers: Tory Burch, "
                                           "Tory Sport)"),
])
def test_nothing_close_leaves_the_brand_empty(ours, options, why):
    assert brands.pick(ours, options) == (None, why)


def test_brand_keys_ignore_case_spaces_dots_hyphens_apostrophes_and_and():
    assert len({brands.key(n) for n in ("J.Crew", "J. Crew", "j crew", "J-CREW", "J’Crew")}) == 1
    assert brands.key("Solid & Striped") == brands.key("Solid and Striped")
    assert brands.key("Levi's") == brands.key("Levis") != brands.key("Levi")


def test_resolved_spellings_are_learned_and_used(tmp_path, monkeypatch):
    monkeypatch.setattr(brands, "SEED", {"J.Crew": "J. Crew"})
    aliases = brands.Aliases(tmp_path / "data" / "brand_aliases.yaml")
    assert aliases.spell("J.Crew") == "J. Crew" and aliases.spell("j crew") == "J. Crew"   # the seed: live, 2026-10-04
    assert aliases.spell("Vince") == "Vince" and aliases.spell(None) is None
    assert aliases.learn("Mistguided", "Missguided") and not aliases.learn("Mistguided", "Missguided")
    assert not aliases.learn("J.Crew", "J. Crew")                      # already known: the file isn't touched for it
    text = (tmp_path / "data" / "brand_aliases.yaml").read_text(encoding="utf-8")
    assert text.startswith("# Brand names as Poshmark's brand list spells them") and "Mistguided: Missguided" in text
    assert brands.Aliases(aliases.path).spell("mistguided") == "Missguided"   # a fresh reader sees it
    assert brands.Aliases(None).spell("J.Crew") == "J. Crew"               # no file: the seed alone


# ---------- 1b. sizes and subcategories ----------

HALVES = ["5", "5.5", "6", "6.5", "7", "7.5", "8", "8.5", "9", "9.5", "10", "10.5", "11", "12"]


@pytest.mark.parametrize("want,offered,got", [
    ("11.5", HALVES, "12"),                       # a tie: the larger
    ("12.5", HALVES, "12"),
    ("15", HALVES, None),                         # more than one size away: never a guess
    ("XL", ["XS", "S", "M", "L"], "L"),
    ("XXL", ["XS", "S", "M", "L"], None),
    ("2XL", ["L", "XL", "XXL"], "XXL"),           # spelled the menu's way
    ("Waist 33", ["Waist 30", "Waist 32", "Waist 34"], "Waist 34"),
    ("MP", ["XSP", "SP", "LP"], "LP"),            # Petite stays Petite
    ("M", ["XSP", "SP", "LP"], None),             # ...and a plain M is not a Petite size
    ("8", ["7.5 (Toddler Girl)", "8 (Toddler Girl)"], None),   # never another system
    ("One Size", ["One Size"], None),             # nothing to compare: not a guess (an exact pick happens before)
])
def test_the_nearest_size_of_the_same_kind(want, offered, got):
    assert nearest_size(want, offered) == got


@pytest.mark.parametrize("want,offered,cutoff,got", [
    ("Jumpsuits", ["Pants & Jumpsuits", "Shorts"], 0.6, "Pants & Jumpsuits"),
    ("Knee High Boots", ["Ankle Boots & Booties", "Over the Knee Boots", "Sneakers"], 0.6, "Over the Knee Boots"),
    ("Flats and Loafers", ["Flats & Loafers"], 0.6, "Flats & Loafers"),
    ("Zzyzx Shoes", ["Sneakers", "Sandals"], 0.6, None),
    ("Sweatshirts", ["Dresses", "Shorts", "Sweaters", "Tops"], 0.75, None),
])
def test_the_closest_name_on_a_menu(want, offered, cutoff, got):
    assert closest(want, offered, cutoff) == got


# ---------- 2a. the title: the brand first ----------

@pytest.mark.parametrize("title,brand,condition,want", [
    ("100% Merino New J.Crew Wide Leg Sweater Pants Blue size M", "J.Crew", "NWT",
     "J.Crew New 100% Merino Wide Leg Sweater Pants Blue size M"),
    ("100% Merino New J.Crew Wide Leg Sweater Pants Blue size M", "J.Crew", "good",
     "J.Crew 100% Merino Wide Leg Sweater Pants Blue size M"),                     # "New" only for NWT / NWOT
    ("New Leopard Print Stirrup Catsuit Naked Wardrobe size S", "Naked Wardrobe", "NWOT",
     "Naked Wardrobe New Leopard Print Stirrup Catsuit size S"),
    ("Brand New Naturino Sneakers size 7.5", "Naturino", "NWT", "Naturino New Sneakers size 7.5"),
    ("Levi's 501 Cutoff Shorts size 28", "Levi's", "good", "Levi's 501 Cutoff Shorts size 28"),   # already right
    ("New Balance 574 Sneakers size 8", "New Balance", "good", "New Balance 574 Sneakers size 8"),  # the brand's "New"
    ("J. Crew Wide Leg Pants size S", "J.Crew", "good", "J.Crew Wide Leg Pants size S"),   # spelled as the facts say
    ("New Wide Leg Pants size S", None, "NWT", "New Wide Leg Pants size S"),                 # no brand: nothing to move
])
def test_the_brand_comes_first_and_new_only_for_new(facts, title, brand, condition, want):
    f = facts(brand=Ev(value=brand, photos=[1] if brand else [], source="photo", confidence=0.9), condition=condition)
    assert copywriter.title_order(title, f) == want
    assert copywriter.title_order(want, f) == want                                          # run twice, same title


def test_the_feature_follows_the_brand_and_the_title_keeps_its_order(facts):
    f = facts(item_type="wide leg sweater pants", category="Pants & Jumpsuits", subcategory="Wide Leg",
              brand=Ev(value="J. Crew", photos=[1], source="photo", confidence=0.95), condition="good",
              colors=["Blue"], size_us=Ev(value="M", photos=[2], source="photo", confidence=0.95))
    f = premium.merge(f, Premium(composition=[Fiber(fiber="merino", pct=100, part="main", photos=[3])]), 6)
    title = premium.title_with_feature("100% Merino New J. Crew Wide Leg Sweater Pants Blue size M", f,
                                       premium.config())
    assert title == "J. Crew 100% Merino Wool Wide Leg Sweater Pants Blue size M"   # the copy's "100% Merino" not doubled


# ---------- 2b/2c. a cutoff's raw hem, denim on denim ----------

def _denim(facts, **kw):
    base = dict(item_type="cutoff jean shorts", department="Women", category="Shorts", subcategory="Jean Shorts",
                brand=Ev(value="Levi's", photos=[1], source="photo", confidence=0.95), condition="good",
                colors=["Blue"], size_us=Ev(value="28", photos=[2], source="photo", confidence=0.95))
    return facts(**{**base, **kw})


def _copy(title, description):
    return CopyOut(poshmark_title=title, poshmark_description=description, poshmark_style_tags=[],
                   depop_description=description, depop_hashtags=["a", "b", "c", "d", "e"])


def test_denim_needs_no_label_on_a_visibly_denim_item(facts):
    shorts = _denim(facts)
    assert unsupported_materials("Levi's 501 Cutoff Denim Shorts Light Wash Raw Hem size 28", shorts) == []
    jacket = facts(item_type="denim trucker jacket", category="Jackets & Coats", subcategory="Jean Jackets")
    assert unsupported_materials("Denim Trucker Jacket", jacket) == []
    jeans = facts(item_type="straight leg jeans", category="Jeans", subcategory="Straight Leg")
    assert unsupported_materials("Denim straight jeans", jeans) == []
    dress = facts(item_type="midi dress", category="Dresses", subcategory="Midi")
    assert unsupported_materials("Denim midi dress", dress) == ["denim"]          # not visibly denim: a claim
    assert unsupported_materials("Leather cutoff shorts", shorts) == ["leather"]   # other materials still need a label


def test_a_raw_or_frayed_hem_is_a_cutoffs_style_and_wear_anywhere_else(facts):
    shorts = _denim(facts)
    title = "Levi's 501 Cutoff Denim Shorts Light Wash Raw Hem size 28"
    text = f"Classic 501 cutoffs with a frayed hem.\n{LINE}"
    assert not [p for p in lint(shorts, _copy(title, text)) if "wear words" in p]
    assert copywriter.condition_wording(text, "good", style_ok=copywriter.cutoff(shorts)) == text
    sweater = facts(item_type="crew neck sweater", category="Sweaters", subcategory="Crewneck", condition="good")
    problems = lint(sweater, _copy("Vince Crew Neck Sweater size M", f"Soft crew neck with a frayed hem.\n{LINE}"))
    assert any("wear words in the poshmark description: frayed" in p for p in problems)
    assert "frayed hem" not in copywriter.condition_wording(f"Soft crew neck with a frayed hem.\n{LINE}", "good")
    stain = f"Classic 501 cutoffs. Small stain on the back pocket.\n{LINE}"     # the hem is style; a stain never is
    assert "stain" not in copywriter.condition_wording(stain, "good", style_ok=True)


def test_the_poster_s_condition_check_lets_a_cutoffs_raw_hem_through():
    r = Render(marketplace="poshmark", title="Levi's 501 Cutoff Denim Shorts Light Wash Raw Hem size 28",
               description=f"Classic cutoffs, frayed hem.\n{LINE}", brand="Levi's", department="Women",
               category="Shorts", subcategory="Jean Shorts", size="26", colors=["Black"], condition="good", price=30,
               photos=[], sku="i_1")
    assert condition_rule_breaks(r) == []
    sweater = r.model_copy(update={"title": "Vince Sweater Frayed Hem size M", "category": "Sweaters",
                                   "subcategory": None})
    assert condition_rule_breaks(sweater) != []


# ---------- 2d. the verifier's false alarm ----------

def test_words_the_verifier_only_took_out_are_no_rewrite():
    draft = CopyOut(poshmark_title="Levi's 501 Cutoff Shorts size 28",
                    poshmark_description="Classic cutoffs ✨ with a light fade.\nGently pre-loved, please see photos for "
                                         "condition.",
                    poshmark_style_tags=[], depop_description="classic levi's cutoffs ✨ light fade\n\n#levis #cutoffs",
                    depop_hashtags=["levis", "cutoffs", "denim", "y2k", "summer"])
    removed = VerifyOut(poshmark_title="Levi's 501 Cutoff Shorts size 28",
                        poshmark_description="Classic cutoffs.\\nGently pre-loved, please see photos for condition.",
                        depop_description="Classic Levi's cutoffs.")              # emoji, words and the tag line out
    assert copywriter.changed_fields(draft, removed) == []
    added = removed.model_copy(update={"depop_description": "Classic Levi's cutoffs in 100% cotton."})
    assert copywriter.changed_fields(draft, added) == ["depop_description"]      # a word put in is still a rewrite


# ---------- 2e. a set's bottom decides ----------

@pytest.mark.parametrize("kw,category,subcategory", [
    (dict(item_type="crop top and flared pants set", category="Tops", subcategory=None), "Pants & Jumpsuits",
     "Boot Cut & Flare"),
    (dict(item_type="knit tank and wide leg pants set", category="Sweaters"), "Pants & Jumpsuits", "Wide Leg"),
    (dict(item_type="corset top and bubble skirt set", category="Skirts", subcategory="Mini"), "Skirts", "Skirt Sets"),
    (dict(item_type="linen shirt and shorts set", category="Tops", subcategory=None), "Shorts", None),
    (dict(item_type="ribbed top and leggings", set_pieces=2, category="Tops"), "Pants & Jumpsuits", "Leggings"),
    (dict(item_type="tee and shorts set", department="Kids", category="Shirts & Tops"), "Matching Sets", None),
])
def test_a_set_goes_under_its_bottom(facts, kw, category, subcategory):
    f = pipeline.settle_set(facts(**{"category_confidence": 0.55, **kw}))
    assert (f.category, f.subcategory, f.category_confidence, f.category_alternatives) == (category, subcategory, 1.0,
                                                                                            [])
    assert f.set_pieces == (kw.get("set_pieces") or 2)


@pytest.mark.parametrize("kw", [
    dict(item_type="satin pajama set", category="Intimates & Sleepwear"),       # sleepwear: the model's call
    dict(item_type="bikini set", category="Swim"),
    dict(item_type="wide leg pants", category="Pants & Jumpsuits"),              # not a set
    dict(item_type="top and skirt set", category="Dresses"),                     # the owner's pick wins, below
])
def test_what_the_set_rule_leaves_alone(facts, kw):
    f = facts(**kw)
    owner = '{"department": "Women", "category": "Dresses"}' if kw["category"] == "Dresses" else None
    assert pipeline.settle_set(f, owner) == f


# ---------- 2f. the sample line ----------

@pytest.mark.parametrize("value,brand,line", [
    ("Sample: 1ST PROTO FIT", "J. Crew", "J. Crew Sample (1st Proto Fit)."),
    ("SAMPLE TYPE: 1ST PROTO FIT", "J. Crew", "J. Crew Sample (1st Proto Fit)."),
    ("Sample", "J. Crew", "J. Crew Sample."),
    ("Sample", None, "Sample."),
    ("H&M x Erdem", "H&M", "H&M x Erdem."),
    ("Limited Edition.", None, "Limited Edition."),
])
def test_a_sample_says_whose_and_which(value, brand, line):
    assert premium.collab_line(value, brand) == line
