"""data/poshmark_taxonomy.yaml and fit(): the model's category onto Poshmark's own names."""
import pytest

from thrift_agent.brain import taxonomy
from thrift_agent.schema import Facts


def test_the_verified_lists_are_the_ones_read_off_the_form():
    depts = taxonomy.load()["departments"]
    assert depts["Women"]["verified"] and depts["Kids"]["verified"]
    assert not depts["Men"]["verified"] and not depts["Home"]["verified"]
    women, kids = depts["Women"]["categories"], depts["Kids"]["categories"]
    assert list(women) == ["Accessories", "Bags", "Dresses", "Intimates & Sleepwear", "Jackets & Coats", "Jeans",
                           "Jewelry", "Makeup", "Pants & Jumpsuits", "Shoes", "Shorts", "Skirts", "Sweaters", "Swim",
                           "Tops", "Skincare", "Hair", "Bath & Body", "Global & Traditional Wear", "Other"]
    assert len(women["Shoes"]) == 17 and women["Shoes"][0] == "Ankle Boots & Booties"
    assert list(kids) == ["Accessories", "Bottoms", "Dresses", "Jackets & Coats", "Matching Sets", "One Pieces",
                          "Pajamas", "Shirts & Tops", "Shoes", "Swim", "Costumes", "Bath, Skin & Hair", "Toys", "Other"]
    assert kids["Shoes"] == ["Baby & Walker", "Boots", "Dress Shoes", "Moccasins", "Rain & Snow Boots",
                             "Sandals & Flip Flops", "Slippers", "Sneakers", "Water Shoes"]


def test_every_alias_points_at_a_name_poshmark_has():
    tax = taxonomy.load()
    depts = tax["departments"]
    for dept, table in tax["aliases"]["categories"].items():
        assert set(table.values()) <= set(depts[dept]["categories"]), dept
    for scope, table in tax["aliases"]["subcategories"].items():
        dept, cat = scope.split("/")
        assert set(table.values()) <= set(depts[dept]["categories"][cat]), scope


@pytest.mark.parametrize("dept,cat,sub,want_cat,want_sub", [
    ("Kids", "Tops", None, "Shirts & Tops", None),                       # the work order's example
    ("Kids", "tops", None, "Shirts & Tops", None),
    ("Kids", "Jeans", None, "Bottoms", None),
    ("Kids", "shoe", "rain boots", "Shoes", "Rain & Snow Boots"),
    ("Kids", "Shoes", "Sandals", "Shoes", "Sandals & Flip Flops"),
    ("Women", "shoes", "booties", "Shoes", "Ankle Boots & Booties"),
    ("Women", "Shoes", "Flats and Loafers", "Shoes", "Flats & Loafers"),
    ("Women", "Dress", "Midi", "Dresses", "Midi"),                          # Dresses' subcategories aren't recorded
    ("Women", "Jackets and Coats", None, "Jackets & Coats", None),
    ("Women", "Coats", None, "Jackets & Coats", None),
    ("Women", "Sweatshirts & Hoodies", None, "Tops", None),
    ("Kids", "Sweatpants", None, "Bottoms", None),
    ("Women", "Swimwear", "One Pieces", "Swim", "One Pieces"),
    ("Men", "Hoodies", None, "Hoodies", None),                              # unconfirmed list: passed on as is
])
def test_fit_puts_the_models_words_on_poshmarks_names(facts, dept, cat, sub, want_cat, want_sub):
    f, notes, questions = taxonomy.fit(facts(department=dept, category=cat, subcategory=sub))
    assert (f.category, f.subcategory, notes, questions) == (want_cat, want_sub, [], [])


def test_a_subcategory_poshmark_does_not_have_is_dropped_with_a_note(facts):
    f, notes, questions = taxonomy.fit(facts(category="Shoes", subcategory="Knee High Boots"))
    assert f.subcategory is None and questions == []
    assert notes == ["subcategory 'Knee High Boots' is not in Poshmark's Women Shoes list: listed without one; "
                     "reply e.g. 'subcategory Ankle Boots & Booties' to set it"]


def test_a_category_or_department_poshmark_does_not_have_is_a_question(facts):
    f, notes, questions = taxonomy.fit(facts(category="Gadgets"))
    assert questions == ["category 'Gadgets' is not one of Poshmark's Women categories — "
                         "reply e.g. 'category Tops'"] and f.category == "Gadgets"
    _, _, questions = taxonomy.fit(facts(department="Kids", category="Gadgets"))
    assert questions == ["category 'Gadgets' is not one of Poshmark's Kids categories — "
                         "reply e.g. 'category Accessories'"]
    _, _, questions = taxonomy.fit(facts(department="Unisex", category="Shoes"))
    assert questions == ["Poshmark has no Unisex department — reply 'department Women' or 'department Men'"]


def test_the_extraction_prompt_lists_the_names(facts):
    text = taxonomy.prompt_text()
    assert "- Women: Accessories, Bags, Dresses," in text
    assert "  - Women > Shoes: Ankle Boots & Booties, Athletic Shoes," in text
    assert "  - Kids > Shoes: Baby & Walker, Boots," in text
    assert "- Men (unconfirmed list): " in text and "Unisex" not in text


def test_fit_returns_a_copy(facts):
    f = facts(department="Kids", category="Tops")
    out, _, _ = taxonomy.fit(f)
    assert f.category == "Tops" and out.category == "Shirts & Tops" and isinstance(out, Facts)


def test_poshmarks_curated_style_tags():
    tags = taxonomy.style_tags()
    assert len(tags) == len(set(tags)) == 130 and tags[:3] == ["70s", "80s", "90s"] and tags[-1] == "Y2K"
    for said, poshmark in (("leopard print", "Leopard Print"), ("y2k", "Y2K"), ("two tone", "Two-Tone"),
                           ("Stripe", "Stripes"), ("CASUAL", "Casual"), ("cruelty free", "Cruelty-Free")):
        assert taxonomy.style_tag(said) == poshmark, said
    assert taxonomy.style_tag("boho") is None and taxonomy.style_tag("Classic") is None and taxonomy.style_tag(" ") is None
