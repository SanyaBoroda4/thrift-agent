"""data/poshmark_taxonomy.yaml and fit(): the model's category onto Poshmark's own names."""
import pytest

from thrift_agent.brain import taxonomy
from thrift_agent.schema import Facts


def test_the_verified_lists_are_the_ones_read_off_the_form():
    """WO24: every department is the form's own catalog (read 2026-10-04); the Women Shoes and Kids lists recorded live
    on 2026-09-29 agree with it."""
    depts = taxonomy.load()["departments"]
    assert all(dept["verified"] for dept in depts.values()) and list(depts) == ["Women", "Kids", "Men", "Home"]
    women, kids, men = depts["Women"]["categories"], depts["Kids"]["categories"], depts["Men"]["categories"]
    assert list(women) == ["Accessories", "Bags", "Dresses", "Intimates & Sleepwear", "Jackets & Coats", "Jeans",
                           "Jewelry", "Makeup", "Pants & Jumpsuits", "Shoes", "Shorts", "Skirts", "Sweaters", "Swim",
                           "Tops", "Skincare", "Hair", "Bath & Body", "Global & Traditional Wear", "Other"]
    assert len(women["Shoes"]) == 17 and women["Shoes"][0] == "Ankle Boots & Booties"
    assert women["Skirts"][-1] == "Skirt Sets" and women["Swim"] == ["Bikinis", "Coverups", "One Pieces", "Sarongs"]
    assert {"Jumpsuits & Rompers", "Wide Leg"} <= set(women["Pants & Jumpsuits"])
    assert women["Global & Traditional Wear"][0] == "Ao Dais" and women["Other"] is None
    assert list(kids) == ["Accessories", "Bottoms", "Dresses", "Jackets & Coats", "Matching Sets", "One Pieces",
                          "Pajamas", "Shirts & Tops", "Shoes", "Swim", "Costumes", "Bath, Skin & Hair", "Toys", "Other"]
    assert kids["Shoes"] == ["Baby & Walker", "Boots", "Dress Shoes", "Moccasins", "Rain & Snow Boots",
                             "Sandals & Flip Flops", "Slippers", "Sneakers", "Water Shoes"]
    assert list(men) == ["Accessories", "Bags", "Jackets & Coats", "Jeans", "Pants", "Shirts", "Shoes", "Shorts",
                         "Suits & Blazers", "Sweaters", "Swim", "Underwear & Socks", "Grooming",
                         "Global & Traditional Wear", "Other"]
    assert "Sweatshirts & Hoodies" in men["Shirts"] and list(depts["Home"]["categories"])[-1] == "Other"


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
    ("Kids", "Jeans", None, "Bottoms", "Jeans"),                            # Poshmark's subcategory: lifted (WO24)
    ("Kids", "shoe", "rain boots", "Shoes", "Rain & Snow Boots"),
    ("Kids", "Shoes", "Sandals", "Shoes", "Sandals & Flip Flops"),
    ("Women", "shoes", "booties", "Shoes", "Ankle Boots & Booties"),
    ("Women", "Shoes", "Flats and Loafers", "Shoes", "Flats & Loafers"),
    ("Women", "Dress", "Midi", "Dresses", "Midi"),
    ("Women", "Jackets and Coats", None, "Jackets & Coats", None),
    ("Women", "Coats", None, "Jackets & Coats", None),
    ("Women", "Sweatshirts & Hoodies", None, "Tops", "Sweatshirts & Hoodies"),
    ("Kids", "Sweatpants", None, "Bottoms", "Sweatpants & Joggers"),
    ("Women", "Swimwear", "One Pieces", "Swim", "One Pieces"),
    ("Men", "Hoodies", None, "Shirts", "Sweatshirts & Hoodies"),            # one part of Poshmark's name
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
                         "reply e.g. 'category Shirts & Tops'"]                      # a useful example (WO23)
    _, _, questions = taxonomy.fit(facts(department="Unisex", category="Shoes"))
    assert questions == ["Poshmark has no Unisex department — reply 'department Women' or 'department Men'"]


@pytest.mark.parametrize("dept,cat,sub,want_cat,want_sub", [
    ("Women", "Jumpsuits & Rompers", "Jumpsuits", "Pants & Jumpsuits", "Jumpsuits & Rompers"),   # live: the catsuit
    ("Women", "Wide Leg", None, "Pants & Jumpsuits", "Wide Leg"),
    ("Women", "Skirt Sets", None, "Skirts", "Skirt Sets"),
    ("Women", "Rompers", None, "Pants & Jumpsuits", "Jumpsuits & Rompers"),
    ("Women", "Swim", "Cover-Ups", "Swim", "Coverups"),                                        # Poshmark's spelling
    ("Women", "Tees", "Tees - Short Sleeve", "Tops", "Tees - Short Sleeve"),   # two of Tops' fit: the model's kept
    ("Women", "Pants & Jumpsuits", "Jumpsuits", "Pants & Jumpsuits", "Jumpsuits & Rompers"),    # a part of the name
    ("Women", "Skirts", "A-Line", "Skirts", "A-Line or Full"),
    ("Kids", "Rompers", None, "Bottoms", "Jumpsuits & Rompers"),
])
def test_a_subcategory_given_as_the_category_is_put_under_its_category(facts, dept, cat, sub, want_cat, want_sub):
    """WO24, live: category "Jumpsuits & Rompers" (Poshmark's subcategory of Pants & Jumpsuits) was asked as "not one of
    Poshmark's Women categories". A name that is a subcategory of exactly one category is put under it — no question."""
    f, notes, questions = taxonomy.fit(facts(department=dept, category=cat, subcategory=sub))
    assert (f.category, f.subcategory, notes, questions) == (want_cat, want_sub, [], [])


@pytest.mark.parametrize("cat", ["Maxi", "Skinny", "Mini"])
def test_a_subcategory_of_several_categories_is_still_asked(facts, cat):
    """"Maxi" is a dress and a skirt: never a guess."""
    f, _, questions = taxonomy.fit(facts(category=cat, subcategory=None))
    assert questions == [f"category '{cat}' is not one of Poshmark's Women categories — reply e.g. 'category Tops'"]


def test_the_extraction_prompt_lists_the_names(facts):
    text = taxonomy.prompt_text()
    assert "- Women: Accessories, Bags, Dresses," in text
    assert "  - Women > Shoes: Ankle Boots & Booties, Athletic Shoes," in text
    assert "  - Women > Skirts: A-Line or Full, Asymmetrical, Circle & Skater, High Low, Maxi, Midi, Mini, Pencil, " \
           "Skirt Sets" in text
    assert "  - Kids > Shoes: Baby & Walker, Boots," in text
    assert "- Men: Accessories, Bags, Jackets & Coats," in text and "unconfirmed" not in text and "Unisex" not in text


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


def _departments():
    return list(taxonomy.load()["departments"])


@pytest.mark.parametrize("department", _departments())
def test_a_department_is_never_the_category(facts, department):
    """WO23, live: category "Kids", subcategory "Shirts & Tops" — the model put the department one level down. Whatever
    the department, its name in the category slot is replaced by the real category: the subcategory it gave, else the
    item type's noun; else the question names a real example."""
    categories = list(taxonomy.load()["departments"][department].get("categories") or {})
    real = categories[0]
    f, _, questions = taxonomy.fit(facts(department=department, category=department, subcategory=real))
    assert f.category == real and f.subcategory is None and questions == []
    for slot in (department.lower(), department.upper(), "Kids", "Women"):
        f, _, _ = taxonomy.fit(facts(department=department, category=slot, subcategory=real))
        assert f.category not in taxonomy.DEPARTMENT_WORDS, (department, slot)


@pytest.mark.parametrize("department,item_type,category", [
    ("Kids", "graphic tee", "Shirts & Tops"), ("Kids", "denim jacket", "Jackets & Coats"),
    ("Kids", "light-up sneakers", "Shoes"), ("Women", "wrap midi dress", "Dresses"),
    ("Women", "suede ankle boots", "Shoes"), ("Women", "crossbody bag", "Bags"),
])
def test_the_item_type_names_the_category_when_the_department_took_its_place(facts, department, item_type, category):
    f, _, questions = taxonomy.fit(facts(department=department, category=department, subcategory=None,
                                         item_type=item_type))
    assert f.category == category and questions == []


def test_a_department_with_nothing_to_go_on_is_still_a_question_with_a_real_example(facts):
    f, _, questions = taxonomy.fit(facts(department="Kids", category="Kids", subcategory=None, item_type="thing"))
    assert questions == ["category 'Kids' is not one of Poshmark's Kids categories — reply e.g. 'category Shirts & Tops'"]


def test_the_prompts_say_where_a_set_a_catsuit_and_flowy_pants_go():
    """WO24, live: a corset top + bubble skirt set went up as Dresses, a catsuit as category "Jumpsuits & Rompers",
    sheer wide-leg pants as Swim > Cover-Ups with no swimwear in sight."""
    from thrift_agent.brain import copy as copywriter, extract
    assert "Skirts > Skirt Sets" in extract.SYSTEM and "never a dress" in extract.SYSTEM
    assert "Pants & Jumpsuits > Jumpsuits & Rompers" in extract.SYSTEM
    assert "unless the photos clearly show swimwear" in extract.SYSTEM
    assert '"2-Piece Set" in\n  the title' in copywriter.SYSTEM and "set_pieces" in copywriter.SYSTEM
    assert "set_pieces" in extract.SYSTEM and "category_confidence" in extract.SYSTEM
