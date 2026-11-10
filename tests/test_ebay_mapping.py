"""WO57 §4/§6 — an item as an eBay listing: the category, the condition, eBay's standard sizes and its item specifics,
for twenty items (tests/ebay_items.py) against eBay's answers — the stand-in category table of tests/ebay_fake.py, or
the recorded answers in tests/fixtures/ebay/ once the spike has made them. No network, no model."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import ebay_fake as fake
from ebay_items import IID, ITEMS, view
from thrift_agent.catalogs.common import ItemView, MappingError
from thrift_agent.ebay import fees, mapping, shipping

PRIVATE_ITEMS = Path(__file__).resolve().parents[1] / "private" / "tests" / "ebay" / "items.json"


def _recorded(name: str) -> dict | None:
    """eBay's own answer kept by `thrift ebay record` (tests/fixtures/ebay/), when it is there."""
    try:
        return json.loads((fake.RECORDED / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def category_of(v: ItemView, recorded: bool = False) -> tuple[mapping.Category, str | None]:
    """The item's category from eBay's answers — the stand-in table's; with `recorded`, eBay's real ones where
    `thrift ebay record` kept them (the client's own file names)."""
    def suggest(q: str) -> dict:
        name = "suggest_" + hashlib.md5(q.strip().lower().encode()).hexdigest()[:16] + ".json"
        return (recorded and _recorded(name)) or fake.suggestions_answer(q)
    answers = [mapping.parse_suggestions(suggest(q)) for q in mapping.category_queries(v)]
    s, note = mapping.pick_category(v, answers)
    aspects = (recorded and _recorded(f"aspects_{s.id}.json")) or fake.aspects_answer(s.id)
    conditions = (recorded and _recorded(f"conditions_{s.id}.json")) or fake.conditions_answer(s.id)
    return mapping.Category(id=s.id, name=s.name, path=s.path, ids=s.ids, aspects=mapping.parse_aspects(aspects),
                            conditions=mapping.parse_conditions(conditions)), note


HOME = {"city": "Springfield", "state": "Illinois", "postal_code": "62701"}   # a stand-in: hers is private


def mapped(v: ItemView, recorded: bool = False, **cfg) -> tuple[mapping.EbayFields, mapping.Category]:
    def suggest(q: str) -> dict:
        name = "suggest_" + hashlib.md5(q.strip().lower().encode()).hexdigest()[:16] + ".json"
        return (recorded and _recorded(name)) or fake.suggestions_answer(q)
    answers = [mapping.parse_suggestions(suggest(q)) for q in mapping.category_queries(v)]
    refused = near = None
    for s, _ in mapping.ranked_categories(v, answers):       # as the poster's plan: the item's next own category
        aspects = (recorded and _recorded(f"aspects_{s.id}.json")) or fake.aspects_answer(s.id)
        conditions = (recorded and _recorded(f"conditions_{s.id}.json")) or fake.conditions_answer(s.id)
        cat = mapping.Category(id=s.id, name=s.name, path=s.path, ids=s.ids, aspects=mapping.parse_aspects(aspects),
                               conditions=mapping.parse_conditions(conditions))
        try:
            got = mapping.map_ebay(v, cat, mapping.Config(**{**HOME, **cfg}))
        except MappingError as e:
            if not mapping.size_refused(e):
                raise
            refused = refused or e
            continue
        if mapping.size_as_written(got):
            return got, cat
        near = near or (got, cat)
    if near:
        return near
    raise refused


def check_listing(v: ItemView, f: mapping.EbayFields, cat: mapping.Category) -> None:
    """What every eBay listing of ours must hold, whatever the item."""
    by_name = {a.name: a for a in cat.aspects}
    for a in cat.aspects:
        if a.required:
            assert f.aspects.get(a.name), f"required aspect {a.name} is missing"
    for name, values in f.aspects.items():
        a = by_name[name]
        assert values and all(v2 and len(v2) <= (a.max_length or 65) for v2 in values)
        assert a.multi or len(values) == 1
        if a.mode == "SELECTION_ONLY":
            assert set(values) <= set(a.values), f"{name}: {values} isn't one of eBay's values"
        if name.lower() in mapping.SIZE_ASPECTS and a.values and mapping.FASHION_ROOT in cat.path:
            assert values[0] in a.values, f"{name}: {values[0]} isn't a standard size"     # eBay's list is closed
    assert f.condition_id in {c for c, _ in cat.conditions} and f.condition_id not in mapping.NEVER_CONDITION
    assert f.aspects["Brand"][0]                                         # a brand, or "Unbranded"
    assert 1 <= len(f.title) <= 80 and f.sku == v.iid
    assert f.photos and f.photos[0] == v.render.photos[0] and len(f.photos) <= 24
    assert f.base_price == v.price and isinstance(f.price, int)
    assert f.price == fees.price_for(v.price, cat.ids, f.shipping["buyer_pays"], 0.07)
    assert f.returns == {"accepted": True, "days": 30, "payer": "BUYER"}
    assert f.location["postal_code"] == "62701" and f.location["country"] == "US" and f.best_offer is True
    assert f.shipping["type"] == "calculated" and f.shipping["handling_days"] == 3 and f.shipping["weight_oz"] > 0


@pytest.mark.parametrize("name", list(ITEMS))
def test_each_of_the_twenty_items_becomes_a_valid_listing_in_its_own_category(name):
    spec, want_category, want = ITEMS[name]
    v = view(**spec)
    f, cat = mapped(v)
    assert cat.id == want_category, f"{name}: {cat.text}"
    check_listing(v, f, cat)
    got = {k: (f.aspects.get(k) or [None])[0] for k in want}
    assert got == want
    assert fake.FakeEbay(recorded=None).check(_input(f)) == []           # and eBay (the fake) would take it


def _input(f: mapping.EbayFields) -> dict:
    from thrift_agent.ebay import client
    return client.create_input(f, ["https://i.ebayimg.com/x.jpg"], client.idempotency_key(f.sku))


def test_the_real_items_of_the_closet_pass_the_same_checks_when_they_are_on_this_machine():
    """private/tests/ebay/items.json (the Mac's own items: facts, the Poshmark render, her price) — never in the
    public repo; skipped where it isn't. Against eBay's recorded answers where `thrift ebay record` kept them, else the
    stand-in table (an item whose kind the stand-in doesn't hold is counted, not failed: at least 20 must map)."""
    if not PRIVATE_ITEMS.is_file():
        pytest.skip("no private/tests/ebay/items.json on this machine")
    from thrift_agent.schema import Facts, Render
    rows = json.loads(PRIVATE_ITEMS.read_text(encoding="utf-8"))
    assert len(rows) >= 20
    done, left = 0, []
    for row in rows:
        v = ItemView(iid=row["id"], facts=Facts.model_validate(row["facts"]),
                     render=Render.model_validate(row["render"]), price=int(row["price"]))
        try:
            f, cat = mapped(v, recorded=True)
        except MappingError as e:
            assert "size" in str(e) or "only" in str(e) or "suggested no category" in str(e), f"{row['id']}: {e}"
            left.append(f"{row['id']}: {e}")
            continue
        check_listing(v, f, cat)
        done += 1
    assert done >= 20, left


# ---------------------------------------------------------------- the five the work order names

def test_the_kids_shoe_takes_ebays_own_size_and_says_which():
    f, cat = mapped(view(**ITEMS["kids' shoe"][0]))
    assert cat.path[-3:] == ["Kids", "Girls", "Girls' Shoes"]
    assert f.aspects["US Shoe Size"] == ["8"] and f.aspects["Department"] == ["Girls"]
    assert any("7.5" in g and "'8'" in g and "nearest" in g for g in f.guesses)       # said in its check line
    assert "Label size: 7.5C." in f.description                         # the label's own size, in the description


def test_the_12_month_snowsuit_is_a_baby_size_in_ebays_spelling():
    f, cat = mapped(view(**ITEMS["12M snowsuit"][0]))
    assert f.aspects["Size"] == ["12 Months"] and f.aspects["Department"] == ["Baby"]
    assert f.aspects["Type"] == ["Snowsuit"] and "Size Type" not in f.aspects
    assert "Label size" not in f.description                            # "12M" IS 12 Months: nothing to add
    assert not [g for g in f.guesses if "size" in g.lower()]


def test_the_handbag_pays_the_15_percent_rule_and_has_no_size():
    v = view(**ITEMS["handbag"][0])
    f, cat = mapped(v)
    assert "169291" in cat.ids and f.quote["rule"] == "Women's bags and handbags"
    assert f.price == 98 and f.price > fees.price_for(100, (), f.shipping["buyer_pays"], 0.07)
    assert "Size" not in f.aspects and f.size is None
    assert abs(f.quote["ebay_payout"] - f.quote["poshmark_payout"]) <= 0.5


def test_an_item_with_no_brand_is_unbranded():
    f, _ = mapped(view(**ITEMS["no-brand top"][0]))
    assert f.aspects["Brand"] == ["Unbranded"] and f.brand == "Unbranded"
    assert not [g for g in f.guesses if "brand" in g.lower()]           # nothing to check: it has none


def test_the_dog_coat_goes_to_pet_supplies_not_to_womens_coats():
    v = view(**ITEMS["dog coat"][0])
    f, cat = mapped(v)
    assert cat.path == ["Pet Supplies", "Dog Supplies", "Apparel"]
    assert f.aspects["Type"] == ["Coat/Jacket"] and f.condition == "New other (see details)"
    assert "Department" not in f.aspects and f.shipping["family"] == "pet"


# ---------------------------------------------------------------- the category

def test_the_category_is_the_items_family_and_department_never_the_first_suggestion_blindly():
    women = [mapping.Suggestion(id="57988", name="Coats, Jackets & Vests", path=["Clothing, Shoes & Accessories", "Men",
                                "Men's Clothing", "Coats, Jackets & Vests"], ids=["11450", "260012", "1059", "57988"]),
             mapping.Suggestion(id="63862", name="Coats, Jackets & Vests", path=["Clothing, Shoes & Accessories", "Women",
                                "Women's Clothing", "Coats, Jackets & Vests"], ids=["11450", "260010", "15724", "63862"])]
    v = view(**ITEMS["puffer jacket"][0])
    chosen, note = mapping.choose_category(v, women)
    assert chosen.id == "63862" and note is None                         # hers, though eBay named the men's first
    only_men, note = mapping.choose_category(v, women[:1])
    assert only_men.id == "57988" and "department" in note               # the closest valid one, said — never refused
    dress = mapping.Suggestion(id="63861", name="Dresses", path=["Clothing, Shoes & Accessories", "Women",
                               "Women's Clothing", "Dresses"], ids=["11450", "260010", "15724", "63861"])
    other, note = mapping.choose_category(v, [dress])
    assert other.id == "63861" and "family" in note
    with pytest.raises(MappingError):
        mapping.choose_category(v, [])


def test_the_queries_say_whose_it_is_and_what_it_is():
    assert mapping.category_queries(view())[0] == "women's wrap midi dress"
    assert mapping.category_queries(view(**ITEMS["kids' shoe"][0]))[0] == "girls toddler girl sneakers"
    assert mapping.category_queries(view(**ITEMS["dog coat"][0]))[0] == "quilted dog coat"
    assert mapping.category_queries(view(**ITEMS["men's sneakers"][0]))[1].startswith("men's New Balance 990v5")
    assert "size" not in mapping.category_queries(view())[1]             # the title without its size tail
    assert mapping.category_queries(view(**ITEMS["kids' shoe"][0]))[-1] == "girls Shoes"      # whose + Poshmark's own
    assert len(mapping.category_queries(view(**ITEMS["dog coat"][0]))) == 2                  # a pet's: no "whose"
    flats = view(dept="Kids", kids="girls", cat="Shoes", sub="Dress Shoes", tab="Girls", item_type="velvet Mary Jane "
                 "flats", title="Childrenchic Velvet Mary Jane Flats Navy Toddler size 7", size_value="7 (Toddler Girl)",
                 size_us="7C", printed="7", brand="Childrenchic")
    _, cat = mapped(flats)                     # eBay named only women's flats for its words: the plain query finds hers
    assert cat.path[:3] == ["Clothing, Shoes & Accessories", "Kids", "Girls"], cat.path


# ---------------------------------------------------------------- the condition (never Fair)

APPAREL = fake.APPAREL


@pytest.mark.parametrize("grade, cid, name", [("NWT", "1000", "New with tags"), ("NWOT", "1500", "New without tags"),
                                              ("like_new", "2990", "Pre-owned - Excellent"),
                                              ("excellent", "2990", "Pre-owned - Excellent"),
                                              ("good", "3000", "Pre-owned - Good"), ("fair", "3000", "Pre-owned - Good")])
def test_our_grades_on_ebays_apparel_conditions(grade, cid, name):
    assert mapping.condition_for(grade, APPAREL) == (cid, name, None)


def test_a_category_without_that_condition_takes_the_nearest_never_fair_and_never_the_other_kind():
    plain = fake.PLAIN                                                   # New / New other / Used
    assert mapping.condition_for("NWT", plain)[:2] == ("1000", "New")
    assert mapping.condition_for("NWOT", plain)[:2] == ("1500", "New other (see details)")
    cid, name, note = mapping.condition_for("like_new", plain)
    assert (cid, name) == ("3000", "Used") and "Pre-owned - Excellent" in note
    assert mapping.condition_for("good", [("3010", "Pre-owned - Fair"), ("2990", "Pre-owned - Excellent")])[0] == "2990"
    assert mapping.condition_for("good", [])[:2] == ("3000", "Pre-owned - Good")        # nothing recorded: the standard
    with pytest.raises(MappingError, match="only new"):                 # used underwear: eBay has no place for it
        mapping.condition_for("good", APPAREL[:3])
    with pytest.raises(MappingError, match="only new"):
        mapped(view(cat="Intimates & Sleepwear", sub="Panties", item_type="lace thong panties",
                    title="Hanky Panky Lace Thong Black size S", size_value="S", size_us="S", printed="S"))
    f, _ = mapped(view(cat="Intimates & Sleepwear", sub="Panties", item_type="lace thong panties", cond="NWT",
                       title="Hanky Panky Lace Thong Black size S", size_value="S", size_us="S", printed="S"))
    assert f.condition == "New with tags"


# ---------------------------------------------------------------- sizes: eBay's list is closed

def test_size_spellings():
    c = mapping.size_candidates
    assert c("Waist 32")[:3] == ["Waist 32", "32"][:3] or "32" in c("Waist 32")
    assert "7.5" in c("7.5 (Toddler Girl)") and "12 Months" in c("12M") and "12M" in c("12 Months")
    assert {"0-3 Months", "0-3M"} <= set(c("0-3 months")) and {"Newborn", "NB"} <= set(c("NB"))
    assert "2XL" in c("XXL") and "XXL" in c("2XL") and "One Size" in c("OS") and "OS" in c("One Size")
    assert c("34E (DD)")[:3] == ["34E (DD)", "34E", "34DD"] and "8" in c("8P") and "M" in c("PM")
    assert c("") == [] and c(None) == []


def test_a_size_is_always_one_of_ebays_values_or_the_site_is_left_out():
    offered = ["XS", "S", "M", "L", "XL", "2XL", "0", "2", "4", "6", "8", "10", "One Size"]
    assert mapping.size_value("XXL", offered, "dress") == ("2XL", None)                  # its own spelling
    value, note = mapping.size_value("S/M", offered, "sweater")
    assert value == "M" and "bigger" in note                                             # the bigger of a dual label
    value, note = mapping.size_value("9", offered, "dress")
    assert value == "10" and "nearest" in note                                           # one step, said
    value, note = mapping.size_value("38", offered, "dress")                             # nothing near: the neutral one
    assert value == "One Size" and "no place" in note
    assert mapping.size_value("38", offered[:-1], "dress") == (None, None)               # … and none: nothing made up
    assert mapping.size_value("Whatever", [], "bed") == ("Whatever", None)               # no list: as written
    with pytest.raises(MappingError, match=r"size 'EU 38' isn't on eBay's size list"):
        cat, _ = category_of(view(cat="Shoes", sub="Sneakers", item_type="leather sneakers",
                                  title="Veja Leather Sneakers White", size_value="EU 38", size_us="EU 38"))
        mapping.map_ebay(view(cat="Shoes", sub="Sneakers", item_type="leather sneakers",
                              title="Veja Leather Sneakers White", size_value="EU 38", size_us="EU 38"), cat,
                         mapping.Config(**HOME))


def test_outside_fashion_a_size_ebays_list_doesnt_hold_goes_as_written():
    """The real dog coat is One Size; eBay's list for dog apparel holds letters. The closed list is apparel's and
    footwear's — a pet's coat keeps its size as written, and still goes to eBay."""
    f, cat = mapped(view(**{**ITEMS["dog coat"][0], "size_value": "One Size", "size_us": "OS", "printed": None}))
    assert cat.path[0] == "Pet Supplies" and f.aspects["Size"] == ["One Size"]
    with pytest.raises(MappingError, match="isn't on eBay's size list"):          # a girl's dress: the list is closed
        mapped(view(dept="Kids", kids="girls", item_type="princess costume dress", title="Princess Dress Blue",
                    size_value="One Size", size_us="OS", printed=None, tab="Girls"))


def test_kids_clothing_goes_to_the_kids_own_categories():
    boys, cat = mapped(view(dept="Kids", kids="boys", cat="Shirts & Tops", sub="Button Down Shirts", tab="Boys",
                            item_type="oxford button-down shirt", title="Ralph Lauren Oxford Shirt Blue size 3T",
                            size_value="3T", size_us="3T", printed="3T", brand="Ralph Lauren"))
    assert cat.path[-3:] == ["Baby", "Baby & Toddler Clothing", "Tops & T-Shirts"] or "Boys" in cat.path
    assert boys.aspects["Size"] == ["3T"] and boys.aspects["Department"][0] in ("Boys", "Baby")
    girls, cat = mapped(view(dept="Kids", kids="girls", cat="Jackets & Coats", sub="Puffers", tab="Girls",
                             item_type="hooded puffer jacket", title="Quilted Hooded Puffer Jacket Pink size 10",
                             size_value="10", size_us="10", printed="10", brand=None))
    assert cat.path[-3:] == ["Girls", "Girls' Clothing (Sizes 4 & Up)", "Outerwear"]
    assert girls.aspects["Size"] == ["10"] and girls.aspects["Department"] == ["Girls"]


def test_an_item_with_no_size_at_all_waits_for_her_size_like_on_every_site():
    v = view(size_value=None, size_us=None, printed=None, title="J.Crew Wrap Midi Dress Navy")
    cat, _ = category_of(v)
    with pytest.raises(MappingError) as e:
        mapping.map_ebay(v, cat)
    assert str(e.value) == mapping.NO_SIZE == "no size on the item"


def test_a_size_under_another_size_type_takes_that_pair():
    """"16W" with Poshmark's Standard tab: eBay lists it under Plus — the valid Size Type + Size pair, said."""
    v = view(size_value="16W", size_us="16W", printed="16W", tab="Standard", title="Torrid Wrap Midi Dress size 16W")
    f, _ = mapped(v)
    assert f.aspects["Size Type"] == ["Plus"] and f.aspects["Size"] == ["16W"]
    assert any("Size Type 'Plus'" in g for g in f.guesses)


def test_the_label_size_goes_into_the_description_only_when_it_differs():
    f, _ = mapped(view(**ITEMS["cashmere cardigan"][0]))                 # label S/M, listed M
    assert f.aspects["Size"] == ["M"] and f.description.rstrip().endswith("Label size: S/M.")
    same, _ = mapped(view())
    assert "Label size" not in same.description
    said = view(printed="S/M", size_value="M", size_us="M", desc="Soft knit.\n\nLabel size: S/M.")
    assert mapped(said)[0].description.count("Label size") == 1          # the copy said it already


# ---------------------------------------------------------------- item specifics

def test_a_required_aspect_the_photos_dont_show_is_never_invented():
    f, cat = mapped(view(**ITEMS["puffer jacket"][0]))
    assert f.aspects["Outer Shell Material"] == ["Unknown"]             # no label was read: no material is claimed
    assert any("Outer Shell Material" in g and "Unknown" in g for g in f.guesses)
    bag, _ = mapped(view(**{**ITEMS["handbag"][0], "material": None, "title": "Coach Shoulder Bag Brown"}))
    assert bag.aspects["Exterior Material"] == ["Unknown"]              # eBay's own neutral value, where it has one


def test_materials_come_from_the_label_the_title_or_visible_denim_only():
    labelled, _ = mapped(view(**ITEMS["silk blouse"][0]))
    assert labelled.aspects["Material"] == ["Silk"]
    titled, _ = mapped(view(**ITEMS["ankle boots"][0]))                 # "Suede" stands in the approved title
    assert titled.aspects["Upper Material"] == ["Suede"]
    denim, _ = mapped(view(**ITEMS["jeans"][0]))
    assert denim.aspects["Material"] == ["Denim"]
    plain, _ = mapped(view())                                           # nothing read: an optional aspect is left out
    assert "Material" not in plain.aspects


def test_an_optional_aspect_we_dont_know_is_left_out_and_nothing_is_guessed():
    f, cat = mapped(view())
    assert {"Sleeve Length", "Neckline", "Occasion", "Pattern", "Vintage", "Theme", "MPN", "Model",
            "Country/Region of Manufacture"} & set(f.aspects) == set()
    assert f.guesses == []
    told, _ = mapped(view(features=("long sleeve", "v-neck"), vintage="1990s", made_in="Italy", style_name="Juliette"))
    assert told.aspects["Sleeve Length"] == ["Long Sleeve"] and told.aspects["Neckline"] == ["V-Neck"]
    assert told.aspects["Vintage"] == ["Yes"] and told.aspects["Country/Region of Manufacture"] == ["Italy"]
    assert told.aspects["Model"] == ["Juliette"]


def test_the_brand_is_ebays_spelling_or_its_own_name_as_text():
    assert mapped(view(brand="J. Crew"))[0].aspects["Brand"] == ["J.Crew"]                 # eBay's spelling of it
    assert mapped(view(brand="alo", label="alo"))[0].aspects["Brand"] == ["Alo Yoga"]      # the one longer name
    assert mapped(view(brand="Sezane"))[0].aspects["Brand"] == ["Sezane"]                  # not listed: as text
    assert mapped(view(brand=None))[0].aspects["Brand"] == ["Unbranded"]


def test_colours_are_ebays_names():
    assert mapped(view(colors=("Cream",)))[0].aspects["Color"] == ["Ivory"]
    assert mapped(view(colors=("Tan",)))[0].aspects["Color"] == ["Beige"]
    assert mapped(view(colors=("Gray",)))[0].aspects["Color"] == ["Gray"]
    assert mapped(view(colors=("Red", "Blue", "Green")))[0].aspects["Color"] == ["Multicolor"]
    assert mapped(view(colors=("Blue",), color_name="rainbow stripe"))[0].aspects["Color"] == ["Multicolor"]


def test_values_are_matched_as_whole_phrases():
    words = ["suede", "ankle", "boot", mapping.BREAK, "tan", mapping.BREAK]
    assert mapping.from_words(["Ankle", "Knee High", "Ankle Boot"], words) == ["Ankle Boot"]     # the longest
    assert mapping.from_words(["Boot Cut", "Rain Boot"], words) == []                             # not in a row
    assert mapping.from_words(["Coat/Jacket", "Sweater"], ["dog", "coat", mapping.BREAK]) == ["Coat/Jacket"]
    assert mapping.from_words(["3/4 Sleeve"], ["3", mapping.BREAK, "sleeve"]) == []
    assert mapping.from_words(["Tan Suede"], ["suede", mapping.BREAK, "tan", "suede"]) == ["Tan Suede"]
    assert mapping.from_words(["Suede Tan"], ["suede", mapping.BREAK, "tan"]) == []               # never across a break


# ---------------------------------------------------------------- the listing's terms

def test_the_listing_carries_the_owners_terms():
    f, _ = mapped(view())
    assert f.returns == {"accepted": True, "days": 30, "payer": "BUYER"} and f.best_offer
    assert f.location == {"city": "Springfield", "state": "Illinois", "postal_code": "62701", "country": "US"}
    assert f.shipping["service"] == "USPS Ground Advantage" and f.shipping["type"] == "calculated"
    flat, _ = mapped(view(), shipping_type="flat", shipping_flat=7.5, handling_days=1)
    assert flat.shipping["type"] == "flat" and flat.shipping["buyer_pays"] == 7.5 and flat.shipping["handling_days"] == 1
    assert flat.price == fees.price_for(35, (), 7.5, 0.07)


def test_settings_reach_the_listing():
    class S:
        def get(self, key, default=None):
            return {"marketplaces.ebay": {"tax_rate": 0.06, "best_offer": False, "handling_days": 2, "max_photos": 12,
                                          "shipping": {"type": "flat", "flat": 6}, "returns": {"days": 30, "payer": "buyer"},
                                          "location": {"city": "Springfield", "state": "IL", "postal_code": 62701}}}[key]
    cfg = mapping.Config.from_settings(S())
    assert (cfg.tax_rate, cfg.best_offer, cfg.handling_days, cfg.max_photos) == (0.06, False, 2, 12)
    assert (cfg.shipping_type, cfg.shipping_flat, cfg.returns_payer, cfg.postal_code, cfg.state) == \
        ("flat", 6, "BUYER", "62701", "IL")
    assert mapping.Config.from_settings(None) == mapping.Config() and mapping.Config().postal_code == ""


def test_the_description_is_simple_html_with_nothing_of_ours_unescaped():
    html = mapping.description_html("Wrap dress <3 & more.\nTie waist.\n\nGently pre-loved, please see photos.")
    assert html == "<p>Wrap dress &lt;3 &amp; more.<br>Tie waist.</p><p>Gently pre-loved, please see photos.</p>"
    assert mapping.description_html("") == ""


def test_a_long_title_is_cut_at_a_word_and_many_photos_at_24():
    long = "Anthropologie Maeve Embroidered Floral Tiered Ruffle Sleeve Midi Wrap Dress Navy Blue Multicolor size 8"
    f, _ = mapped(view(title=long, n_photos=30))
    assert len(f.title) <= 80 and long.startswith(f.title) and not f.title.endswith(" ")
    assert len(f.photos) == 24 and f.photos[0] == "/w/cover.jpg"


# ---------------------------------------------------------------- shipping

def test_parcels_by_family_rounded_up():
    assert shipping.round_up(8) == 8 and shipping.round_up(9) == 12 and shipping.round_up(15.1) == 16
    assert shipping.round_up(17) == 32 and shipping.round_up(33) == 48
    boots = shipping.plan("shoes", "heavy", "Women")
    assert (boots.pounds, boots.ounces, boots.weight_oz) == (4, 0, 64) and boots.buyer_pays == 12.50
    tee = shipping.plan("top", "xs", "Women")
    assert (tee.pounds, tee.ounces) == (0, 8) and tee.buyer_pays == 6.00 and (tee.length, tee.width, tee.height) == (10, 8, 2)
    assert shipping.plan("shoes", "kids_shoes", "Kids").weight_oz == 32
    assert shipping.plan(None).why.startswith("no family read")
    with pytest.raises(shipping.ShippingError):
        shipping.plan("top", kind="freight")
    assert shipping.load().problems() == []
    assert IID.startswith("i_")
