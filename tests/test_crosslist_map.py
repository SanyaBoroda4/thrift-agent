"""WO30 mapping: the item's facts → exact values of Depop's and Vinted's catalogs (data/depop_catalog.json,
data/vinted_catalog.json). No network, no model: the model fallback is a stub."""
import json

import pytest
import yaml

from thrift_agent import catalogs
from thrift_agent.catalogs import CatalogError, categories
from thrift_agent.catalogs.common import ItemView, colors_for, condition_key, materials_for, package_class
from thrift_agent.catalogs.depop import description, map_depop
from thrift_agent.catalogs.sizes import MappingError, SizeIn, depop_size, vinted_size
from thrift_agent.catalogs.vinted import map_vinted
from thrift_agent.schema import Ev, Facts, Fiber, Flaw, Premium, Render


def view(dept="Women", cat="Skirts", sub="Mini", item_type="pleated mini skirt", title="J. Crew Pleated Mini Skirt size 4",
         size_value="4", size_us="4", printed="4", colors=("Blue",), cond="good", kids=None, tab="Standard",
         color_name=None, comp=(), material=None, vintage=None, desc="Pleated mini skirt.\nGently pre-loved, please see "
         "photos for condition.", n_photos=12, flaws=(), brand="J. Crew", tags=("Preppy",), set_pieces=None):
    premium = Premium(composition=[Fiber(fiber=f, pct=p) for f, p in comp], vintage=Ev(value=vintage)) \
        if comp or vintage else None
    f = Facts(item_type=item_type, department=dept, kids_gender=kids, category=cat, subcategory=sub,
              brand=Ev(value=brand), size_printed=Ev(value=printed, source="photo"),
              size_us=Ev(value=size_us, source="photo"), colors=list(colors), color_name=color_name, condition=cond,
              condition_evidence=Ev(), cover_photo=0, photo_order=list(range(n_photos)), premium=premium,
              material=Ev(value=material, source="photo") if material else Ev(),
              flaws=[Flaw(description="spot", photos=list(flaws))] if flaws else [], set_pieces=set_pieces)
    photos = ["/w/cover.jpg"] + [f"/w/photos/{i:02d}.jpg" for i in range(1, n_photos)]
    r = Render(marketplace="poshmark", title=title, description=desc, tags=list(tags), brand=brand, department=dept,
               category=cat, subcategory=sub, size=size_us, kids_gender=kids, size_tab=tab, size_value=size_value,
               colors=list(colors), condition=cond, price=35, photos=photos, sku="i_261006_aaaaaa")
    return ItemView(iid="i_261006_aaaaaa", facts=f, render=r, price=35,
                    flaw_photos={f"/w/photos/{i:02d}.jpg" for i in flaws})


def no_model(*a, **k):
    raise AssertionError("the model was asked")


# ---------------------------------------------------------------- the catalogs

def test_both_catalogs_load_and_validate():
    assert catalogs.check() == {"depop": None, "vinted": None}
    assert len(catalogs.depop_catalog().categories) == 322 and len(catalogs.vinted_catalog().categories) == 573


def test_a_broken_catalog_is_refused_with_the_reason(tmp_path):
    data = json.loads(catalogs.path("vinted").read_text(encoding="utf-8"))
    data["categories"]["5523"]["fields"]["size"]["lib"] = "size_99"
    bad = tmp_path / "vinted_catalog.json"
    bad.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CatalogError, match="size_99"):
        catalogs.load("vinted", bad)
    with pytest.raises(CatalogError, match="missing"):
        catalogs.load("depop", tmp_path / "nothing.json")
    data = json.loads(catalogs.path("depop").read_text(encoding="utf-8"))
    del data["categories"]
    bad = tmp_path / "depop_catalog.json"
    bad.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CatalogError, match="not usable"):
        catalogs.load("depop", bad)


def test_every_depop_clothing_category_has_its_product_type():
    dp = catalogs.depop_catalog()
    paths = [p for p in dp.categories if p.split(" > ")[0] in ("Women", "Men", "Kids")]
    assert paths and all(dp.product_type(p) for p in paths)
    assert dp.size_set_id("Women > Bottoms > Pants") == "22" and dp.size_set_id("Women > Accessories > Bags") is None
    assert dp.product_type("Men > Footwear > Sneakers") == "footwear/trainers"


# ---------------------------------------------------------------- the category table

def test_every_row_of_the_table_exists_in_both_catalogs():
    """WO30 §9: for every Poshmark subcategory in the table, the Depop path and the Vinted leaf exist (Kids: both the
    Girls and the Boys leaf)."""
    dp, vt = catalogs.depop_catalog(), catalogs.vinted_catalog()
    for (dept, cat, sub), (depop_path, spec) in categories.TABLE.items():
        if depop_path:
            assert f"{dept} > {depop_path}" in dp.categories, (dept, cat, sub, depop_path)
        for g in (("girls", "boys") if dept == "Kids" else (None,)):
            p = categories._vinted_path(dept, spec, g)
            if p is not None:
                assert vt.leaf(p) is not None, (dept, cat, sub, g, p)
    for dept, cat, sub, _rx, depop_path, spec in categories.REFINE:
        if depop_path:
            assert f"{dept} > {depop_path}" in dp.categories
        for g in (("girls", "boys") if dept == "Kids" else (None,)):
            if (p := categories._vinted_path(dept, spec, g)) is not None:
                assert vt.leaf(p) is not None, p


def test_every_poshmark_clothing_subcategory_has_a_row():
    """Every subcategory of Poshmark's clothing, shoe, bag and accessory categories reaches a row (its own or its
    category's); only beauty, toys, costumes and "Other" go to the model."""
    tx = yaml.safe_load((catalogs.DATA_DIR / "poshmark_taxonomy.yaml").read_text(encoding="utf-8"))
    skip = {"Makeup", "Skincare", "Hair", "Bath & Body", "Global & Traditional Wear", "Other", "Grooming", "Toys",
            "Costumes", "Bath, Skin & Hair"}
    for dept in ("Women", "Men", "Kids"):
        for cat, v in tx["departments"][dept]["categories"].items():
            if cat in skip:
                continue
            subs = (v or {}).get("subcategories") if isinstance(v, dict) else v
            for sub in [None, *(subs or [])]:
                assert categories._row(dept, cat, sub) is not None, (dept, cat, sub)


@pytest.mark.parametrize("dept,cat,sub,item_type,kids,depop,vinted", [
    # what the shop listed so far (the Mac, 2026-10-05)
    ("Kids", "Shoes", "Sneakers", "glitter star sneakers", "girls", "Kids > Footwear > Sneakers",
     "Kids > Girls clothing > Shoes > Sneakers > Lace-up sneakers"),
    ("Kids", "Shirts & Tops", None, "graphic tee", "boys", "Kids > Tops > T-shirts",
     "Kids > Boys clothing > Tops & T-shirts > T-shirts"),
    ("Women", "Skirts", "Skirt Sets", "corset top & bubble skirt set", None, "Women > Bottoms > Skirts",
     "Women > Clothing > Skirts"),
    ("Women", "Skirts", "Mini", "denim mini skirt", None, "Women > Bottoms > Skirts", "Women > Clothing > Skirts"),
    ("Women", "Shorts", "Jean Shorts", "cutoff denim shorts", None, "Women > Bottoms > Shorts",
     "Women > Clothing > Shorts & cropped pants > Jean shorts"),
    ("Women", "Pants & Jumpsuits", "Wide Leg", "lambswool wide leg sweater pants", None, "Women > Bottoms > Pants",
     "Women > Clothing > Pants & leggings > Wide-leg pants"),
    ("Women", "Pants & Jumpsuits", "Jumpsuits & Rompers", "leopard print stirrup catsuit", None,
     "Women > Jumpsuits and rompers > Jumpsuits", "Women > Clothing > Jumpsuits & rompers > Jumpsuits"),
    ("Women", "Pants & Jumpsuits", "Jumpsuits & Rompers", "floral romper", None, "Women > Jumpsuits and rompers > Rompers",
     "Women > Clothing > Jumpsuits & rompers > Rompers"),
    ("Men", "Shirts", "Casual Button Down Shirts", "plaid flannel shirt", None, "Men > Tops > Shirts",
     "Men > Clothing > Tops & T-shirts > Shirts > Checked shirts"),
    ("Kids", "Shoes", "Sneakers", "light-up velcro sneakers", "boys", "Kids > Footwear > Sneakers",
     "Kids > Boys clothing > Shoes > Sneakers > Hook-and-loop sneakers"),
])
def test_the_table_places_the_items_listed_so_far(dept, cat, sub, item_type, kids, depop, vinted):
    d = categories.resolve("depop", dept, cat, sub, item_type, kids_gender=kids, ask=no_model)
    v = categories.resolve("vinted", dept, cat, sub, item_type, kids_gender=kids, ask=no_model)
    assert (d.path, v.path) == (depop, vinted) and d.source in ("table", "refined")
    assert v.value == str(catalogs.vinted_catalog().leaf(vinted))


def test_no_row_asks_the_model_from_an_enum_and_remembers(learned_categories):
    calls = []

    def model(mp, dept, cat, sub, item_type, title, kids):
        calls.append((mp, dept, cat))
        options = categories.candidates(mp, dept, kids)
        value = next(v for v, p in options.items() if p.endswith("Outerwear > Jackets > Quilted jackets"))
        return categories.Pick(value=value, path=options[value], source="model")

    first = categories.resolve("vinted", "Women", "Jackets & Coats", None, "quilted barn jacket", ask=model)
    again = categories.resolve("vinted", "Women", "Jackets & Coats", None, "quilted barn jacket", ask=no_model)
    assert first.path == again.path == "Women > Clothing > Outerwear > Jackets > Quilted jackets"
    assert (first.source, again.source, len(calls)) == ("model", "learned", 1)
    assert json.loads(learned_categories.read_text(encoding="utf-8"))           # cached on disk


def test_the_model_cannot_answer_outside_the_catalog():
    """The tool input is an enum of the department's leaves; an answer outside it is refused, never used."""
    with pytest.raises(CatalogError):
        categories.resolve("vinted", "Women", "Jackets & Coats", None, "jacket",
                           ask=lambda *a: categories.Pick(value="999999", path="Nowhere", source="model"))
    from pydantic import create_model  # noqa: F401 — the real fallback builds an enum the API enforces
    options = categories.candidates("vinted", "Kids", "boys")
    assert options and all(p.startswith("Kids > Boys clothing > ") for p in options.values())


# ---------------------------------------------------------------- sizes

def _vs(lib, value, **kw):
    s = SizeIn(department=kw.get("dept", "Women"), category=kw.get("cat", "Dresses"), subcategory=kw.get("sub"),
               item_type=kw.get("item_type", "dress"), value=value, tab=kw.get("tab", "Standard"),
               printed=kw.get("printed"), kids_c_or_y=kw.get("cy"))
    return vinted_size(s, lib, catalogs.vinted_catalog().library(lib))


def _ds(set_id, value, **kw):
    dp = catalogs.depop_catalog()
    s = SizeIn(department=kw.get("dept", "Women"), category=kw.get("cat", "Dresses"), subcategory=kw.get("sub"),
               item_type=kw.get("item_type", "dress"), value=value, tab=kw.get("tab", "Standard"),
               printed=kw.get("printed"), kids_c_or_y=kw.get("cy"))
    return depop_size(s, dp.size_set(set_id), dp.size_sets_US[set_id].path)


@pytest.mark.parametrize("value,vid", [("00", 1226), ("0", 102), ("2", 2), ("4", 3), ("6", 3), ("8", 4), ("10", 4),
                                       ("12", 5), ("14", 5), ("16", 6), ("18", 6), ("20", 7), ("22", 7)])
def test_womens_numeric_sizes_on_vinted(value, vid):
    assert _vs("size_0", value)[0].id == vid


def test_womens_letters_and_numbers_on_both_sites():
    for ltr, vid in {"XS": 2, "S": 3, "M": 4, "L": 5, "XL": 6, "XXL": 7, "3XL": 310}.items():
        assert _vs("size_0", ltr)[0].id == vid and _ds("84", ltr)[0] == ltr
    for n in ["00", "0", "2", "4", "6", "8", "10", "12", "14", "16", "18", "20", "22"]:
        assert _ds("4", n)[0] == n


@pytest.mark.parametrize("w,us", [(24, "00"), (25, "0"), (26, "2"), (27, "4"), (28, "6"), (29, "8"), (30, "10"),
                                  (31, "12"), (32, "14"), (33, "16"), (34, "18")])
def test_womens_denim_waist_converts_to_us_first(w, us):
    v, _ = _vs("size_0", str(w), cat="Jeans", sub="Skinny", item_type="skinny jeans")
    assert v.id == _vs("size_0", us)[0].id
    assert _ds("22", str(w), cat="Jeans", sub="Skinny", item_type="skinny jeans")[0] == f'{w}"'
    assert _vs("size_0", "4", cat="Shorts", sub="Jean Shorts", printed=f"W{w}")[0].id == v.id    # the label's W27


@pytest.mark.parametrize("ltr", ["S", "M", "L", "XL", "XXL", "3XL"])
def test_mens_letter_sizes(ltr):
    assert _vs("size_10", ltr, dept="Men", cat="Shirts")[0].title in (ltr, {"3XL": "XXXL"}.get(ltr))
    assert _vs("size_9", ltr, dept="Men", cat="Pants")[0].title in (ltr, {"3XL": "XXXL"}.get(ltr))
    assert _ds("54", ltr, dept="Men", cat="Shirts")[0] == ltr and _ds("60", ltr, dept="Men", cat="Pants")[0] == ltr


@pytest.mark.parametrize("w", list(range(28, 41)))
def test_mens_waist_sizes(w):
    option, guess = _vs("size_9", f"Waist {w}", dept="Men", cat="Pants")
    assert option.title == (f"W{w}" if w not in (37, 39) else f"W{w + 1}")
    assert (guess is None) == (w not in (37, 39))
    assert _ds("60", f"Waist {w}", dept="Men", cat="Pants")[0] == f'{w}"'


def test_mens_shirts_by_neck_size():
    assert _vs("size_11", "Neck 15.5", dept="Men", cat="Shirts")[0].title == "15.5 in"
    value, guess = _ds("54", "Neck 15.5", dept="Men", cat="Shirts")
    assert value == "M" and "neck 15.5" in guess


@pytest.mark.parametrize("value,vinted,depop", [
    ("0-3 Months", "1-3M", "0-3 months"), ("3-6 Months", "3-6M", "3-6 months"), ("6 Months", "3-6M", "3-6 months"),
    ("12 Months", "9-12M", "9-12 months"), ("18 Months", "12-18M", "12-18 months"), ("24 Months", "24M | 2T/2",
                                                                                     "18-24 months"),
    ("2T", "24M | 2T/2", "2 years"), ("3T", "3T/3", "3 years"), ("4T", "4T/4", "4 years"), ("5", "5T", "5 years"),
    ("6", "6", "6 years"), ("6X", "6X/7", "6 years"), ("7", "6X/7", "7 years"), ("8", "8", "8 years"),
    ("10", "10", "10 years"), ("12", "12", "12 years"), ("14", "14", "14 years"), ("16", "16", "16 years")])
def test_kids_clothing_sizes(value, vinted, depop):
    assert _vs("size_16", value, dept="Kids", cat="Shirts & Tops")[0].title == vinted
    assert _ds("100", value, dept="Kids", cat="Shirts & Tops")[0] == depop


@pytest.mark.parametrize("n", ["5", "5.5", "6", "6.5", "7", "7.5", "8", "8.5", "9", "9.5", "10", "10.5", "11"])
def test_womens_shoes_take_the_us_number(n):
    assert _vs("size_5", n, cat="Shoes")[0].title == n and _ds("46", n, cat="Shoes")[0] == f"US {n}"


@pytest.mark.parametrize("n", ["7", "7.5", "8", "8.5", "9", "9.5", "10", "10.5", "11", "11.5", "12", "12.5", "13"])
def test_mens_shoes_take_the_us_number(n):
    assert _vs("size_14", n, dept="Men", cat="Shoes")[0].title == n and _ds("77", n, dept="Men", cat="Shoes")[0] == f"US {n}"


def test_kids_shoes_go_up_a_half_size_only_where_the_menu_has_none():
    option, guess = _vs("size_17", "7.5 (Toddler Girl)", dept="Kids", cat="Shoes", cy="C")
    assert option.title == "8 child" and guess == "size set to '8 child' (from 7.5C)"
    assert _vs("size_17", "8", dept="Kids", cat="Shoes", cy="C") == (_vs("size_17", "8", dept="Kids", cat="Shoes",
                                                                         cy="C")[0], None)
    assert _vs("size_17", "4 (Big Girl)", dept="Kids", cat="Shoes", cy="Y")[0].title == "4 junior"
    assert _ds("103", "7.5 (Toddler Girl)", dept="Kids", cat="Shoes", cy="C") == ("7-7.5", None)
    assert _ds("103", "4.5", dept="Kids", cat="Shoes", cy="Y")[0] == "5 (adult)"


def test_a_size_is_never_invented():
    with pytest.raises(MappingError):
        _vs("size_0", None)
    with pytest.raises(MappingError):
        _vs("size_0", "Q")
    with pytest.raises(MappingError):
        _ds("100", "XL", dept="Kids", cat="Shirts & Tops")         # kids letters: not on Depop's kids sizes
    assert _vs("size_4", "One Size", cat="Accessories")[0].title == "One size"   # only when the item is one size


# ---------------------------------------------------------------- condition, colour, material, package

@pytest.mark.parametrize("grade", ["NWT", "NWOT", "like_new", "excellent", "good", "fair"])
def test_condition_is_never_fair(grade):
    dp = catalogs.depop_catalog()
    d = map_depop(view(cond=grade), ask=no_model)
    v = map_vinted(view(cond=grade), ask=no_model, ask_length=no_model)
    assert d.condition != "Used - Fair" and d.condition in dp.condition
    assert v.condition_id != 4 and v.condition != "Satisfactory"
    assert (d.condition, v.condition) == {"NWT": ("Brand new", "New with tags"),
                                         "NWOT": ("Like new", "New without tags"),
                                         "like_new": ("Like new", "Very good"), "excellent": ("Like new", "Very good"),
                                         "good": ("Used - Good", "Good"), "fair": ("Used - Good", "Good")}[grade]
    assert condition_key("fair") == "Good"


def test_colours_use_each_sites_names():
    assert colors_for(view(colors=("Gray",)), "depop") == ["Grey"] and colors_for(view(colors=("Gray",)), "vinted") == ["Gray"]
    assert colors_for(view(colors=("Tan",)), "depop") == ["Tan"] and colors_for(view(colors=("Tan",)), "vinted") == ["Beige"]
    olive = view(colors=("Green",), color_name="olive green")
    assert colors_for(olive, "depop") == colors_for(olive, "vinted") == ["Khaki"]
    teal = view(colors=("Green",), color_name="teal")
    assert (colors_for(teal, "depop"), colors_for(teal, "vinted")) == (["Green"], ["Turquoise"])
    assert colors_for(view(colors=("Blue", "White"), color_name="navy and white"), "vinted") == ["Navy", "White"]
    assert colors_for(view(colors=("Cream",)), "depop") == ["Cream"]


def test_material_only_from_a_label_and_within_the_limits():
    vt, dp = catalogs.vinted_catalog(), catalogs.depop_catalog()
    allowed_v = [o.title for o in vt.library("material_0")]
    blend = view(comp=[("cotton", 60), ("polyester", 30), ("elastane", 6), ("nylon", 4)])
    assert materials_for(blend, "vinted", allowed_v, 3) == ["Cotton", "Polyester", "Elastane"]
    assert materials_for(blend, "depop", dp.attribute_values["material"], 4) == \
        ["Cotton", "Polyester", "Elastane / Lycra / Spandex", "Nylon"]
    assert materials_for(view(), "vinted", allowed_v, 3) == []                       # no label read: nothing
    assert materials_for(view(material="100% lambswool"), "vinted", allowed_v, 3) == ["Wool"]


@pytest.mark.parametrize("kw,cls", [
    (dict(cat="Tops", sub="Tees - Short Sleeve", item_type="graphic tee"), "xs"),
    (dict(cat="Jeans", sub="Skinny", item_type="skinny jeans"), "medium"),
    (dict(cat="Jackets & Coats", sub="Trench Coats", item_type="trench coat"), "heavy"),
    (dict(cat="Shoes", sub="Ankle Boots & Booties", item_type="suede ankle boots"), "heavy"),
    (dict(cat="Shoes", sub="Sneakers", item_type="sneakers"), "medium"),
    (dict(cat="Dresses", sub="Midi", item_type="midi dress"), "light"),
    (dict(dept="Kids", cat="Shirts & Tops", sub=None, item_type="graphic tee", kids="boys"), "xs"),
])
def test_package_size_class(kw, cls):
    assert package_class(view(**kw)) == cls


# ---------------------------------------------------------------- the whole item, both sites

def test_a_skirt_on_vinted_needs_its_length():
    v = map_vinted(view(), ask=no_model, ask_length=no_model)                       # "Mini" from the subcategory
    assert (v.category_path, v.skirt_length, v.size) == ("Women > Clothing > Skirts", "Mini", "S / US 4-6")
    asked = []
    v = map_vinted(view(sub="A-Line or Full", item_type="a-line skirt", title="J. Crew A-Line Skirt size 4"),
                   ask=no_model, ask_length=lambda vw, titles: asked.append(titles) or "Knee-length")
    assert v.skirt_length == "Knee-length" and asked == [("Mini", "Knee-length", "Midi", "Maxi", "Asymmetrical")]


def test_depop_fields_and_description():
    d = map_depop(view(sub="Midi", item_type="silk midi skirt", title="J. Crew 100% Silk Midi Skirt Blue size 4",
                       comp=[("silk", 100)], vintage="1990s"), ask=no_model)
    assert d.category == "Women > Bottoms > Skirts" and d.size == "4" and d.size_set == "22"
    assert d.attributes == {"material": ["Silk"], "dress-length": ["Midi"]}
    assert (d.source, d.age, d.package_size, d.shipping) == (["Vintage"], "90s", "Small", "Depop Shipping")
    assert d.hashtags == ["jcrew", "midi", "preppy", "blue", "90s"] or len(d.hashtags) <= 5
    assert d.description.splitlines()[0] == "J. Crew 100% Silk Midi Skirt Blue size 4"


def test_the_depop_description_keeps_the_title_and_hashtags_within_1000():
    tags = ["levis", "jeanshorts", "y2k", "black", "90s"]
    long_body = ("Washed black cutoffs. " * 80).strip()
    text = description("Levi's 501 Cutoff Denim Shorts Washed Black size 26", long_body, tags)
    assert len(text) <= 1000 and text.splitlines()[0] == "Levi's 501 Cutoff Denim Shorts Washed Black size 26"
    assert text.endswith("#levis #jeanshorts #y2k #black #90s") and text.count("#") == 5
    assert description("T", "Short body.", tags) == "T\n\nShort body.\n\n#levis #jeanshorts #y2k #black #90s"


def test_depop_keeps_8_photos_with_every_flaw():
    d = map_depop(view(n_photos=14, flaws=(11, 12, 13)), ask=no_model)
    assert len(d.photos) == 8 and d.photos[0] == "/w/cover.jpg"
    assert {"/w/photos/11.jpg", "/w/photos/12.jpg", "/w/photos/13.jpg"} <= set(d.photos)
    v = map_vinted(view(n_photos=14), ask=no_model, ask_length=no_model)
    assert len(v.photos) == 14                                                       # up to 20 on Vinted


def test_a_required_size_that_doesnt_fit_skips_only_that_site():
    item = view(dept="Kids", cat="Shirts & Tops", sub="Tees - Short Sleeve", item_type="tee", size_value="XL",
                size_us="XL", kids="girls", tab="Girls")
    with pytest.raises(MappingError):
        map_depop(item, ask=no_model)                  # Depop's kids sizes are ages only
    assert map_vinted(item, ask=no_model, ask_length=no_model).size == "XL"
