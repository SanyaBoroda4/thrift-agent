"""WO51 §5: item families — the item's own type words and each site's category read as one family (dress, top,
sweater…), so a listing whose category is in another family than the item can be held back (live 2026-11-01: the
Edikted crochet mini dress under Poshmark's Sweaters › V-Necks and Vinted's "V-neck sweaters")."""
import json
import re
from pathlib import Path

import pytest
import yaml

from thrift_agent import families as F
from thrift_agent.families import family_of_category, family_of_type, same_family

DATA = Path(__file__).resolve().parents[1] / "data"


def test_the_live_items_of_2026_11_01():
    assert family_of_type("crochet-knit mini dress") == "dress"
    assert family_of_category("poshmark", ("Women", "Sweaters", "V-Necks")) == "sweater"
    assert family_of_category("vinted", "Women > Clothing > Jumpers & sweaters > Sweaters > V-neck sweaters") == "sweater"
    assert family_of_category("vinted", "Women > Clothing > Dresses > Mini dresses") == "dress"
    assert family_of_category("vinted", "Women > Clothing > Dresses > Mini-dresses") == "dress"
    assert family_of_category("depop", "Women > Tops > Sweaters") == "sweater"
    assert not same_family(family_of_type("crochet-knit mini dress"),
                           family_of_category("vinted", "Women > Clothing > Jumpers & sweaters > Sweaters > V-neck sweaters"))
    # the pajama set and the costume, and where they went: the right families on all three
    assert family_of_type("gingham pajama set, cami top and shorts") == "sleepwear"
    assert family_of_category("poshmark", ("Women", "Intimates & Sleepwear", "Pajamas")) == "sleepwear"
    assert family_of_category("vinted", "Women > Clothing > Lingerie & nightwear > Sleepwear") == "sleepwear"
    assert family_of_category("depop", "Women > Sleepwear > Pajamas") == "sleepwear"
    assert family_of_type("Belle princess costume dress with tiara and gloves") == "costume"
    assert family_of_category("poshmark", ("Kids", "Costumes", "Theater")) == "costume"
    assert family_of_category("vinted", "Kids > Girls clothing > Costumes") == "costume"
    assert family_of_category("depop", "Kids > Costume > Costume") == "costume"


@pytest.mark.parametrize("phrase, family", [
    ("dress shirt", "top"), ("shirt dress", "dress"), ("plaid shirt dress", "dress"), ("sweater dress", "dress"),
    ("t-shirt dress", "dress"), ("graphic tee", "top"), ("ribbed tank top", "top"), ("bodysuit", "top"),
    ("cowl neck satin slip dress", "dress"), ("lace strapless mini dress with train", "dress"),
    ("sweater vest", "sweater"), ("puffer vest", "outerwear"), ("snap-front vest", "outerwear"),
    ("crew neck sweater", "sweater"), ("cardigan", "sweater"), ("hoodie", "sweater"),
    ("velvet puffer jacket", "outerwear"), ("leather moto jacket", "outerwear"),
    ("boot cut jeans", "bottoms"), ("wide leg sweater pants", "bottoms"), ("leggings", "bottoms"),
    ("biker shorts", "shorts"), ("high-waisted biker shorts", "shorts"), ("textured woven mini skirt", "skirt"),
    ("crop top & skirt 2-piece set", "set"), ("ribbed crop top & biker shorts 2-piece set", "set"), ("co-ord set", "set"),
    ("romper", "jumpsuit"), ("overalls", "jumpsuit"), ("robe", "sleepwear"), ("nightgown", "sleepwear"),
    ("bikini top", "swim"), ("one-piece swimsuit", "swim"), ("swim cover-up dress", "swim"),
    ("sports bra", "intimates"), ("bralette", "intimates"),
    ("ankle boots", "shoes"), ("slip-on sneakers", "shoes"), ("ballet flats", "shoes"), ("tote bag", "bag"),
    ("crossbody bag", "bag"), ("belt", "accessory"), ("jewelry set", "accessory"), ("costume jewelry necklace", "accessory"),
    ("hair clip set", "accessory"),
    # titles too: the head noun, never a detail word
    ("Edikted Crochet Knit Mini Dress V-Neck Flower Tie, Off-White size M", "dress"),
    ("Parke x Target Gingham Cami & Shorts Pajama 2-Piece Set Red size S", "sleepwear"),
    ("Relibeauty Belle Princess Costume Dress with Tiara, Yellow Gold size 11", "costume"),
])
def test_type_phrases(phrase, family):
    assert family_of_type(phrase) == family


@pytest.mark.parametrize("phrase", [None, "", "kimono", "vintage", "size M"])
def test_unknown_types(phrase):
    assert family_of_type(phrase) is None


@pytest.mark.parametrize("site, category, family", [
    ("poshmark", ("Men", "Pants", "Dress"), "bottoms"),                 # dress pants, never a dress
    ("poshmark", ("Women", "Jeans", "Boot Cut"), "bottoms"),            # boot cut: no boot
    ("poshmark", ("Men", "Shoes", "Loafers & Slip-Ons"), "shoes"),       # slip-on: no slip
    ("poshmark", ("Men", "Shorts", "Flat Front"), "shorts"),            # flat front: no flats
    ("poshmark", ("Men", "Swim", "Board Shorts"), "swim"),
    ("poshmark", ("Kids", "Swim", "One Piece"), "swim"),
    ("poshmark", ("Kids", "Pajamas", "Pajama Bottoms"), "sleepwear"),
    ("poshmark", ("Women", "Intimates & Sleepwear", "Sports Bras"), "intimates"),
    ("poshmark", ("Women", "Pants & Jumpsuits", "Jumpsuits & Rompers"), "jumpsuit"),
    ("poshmark", ("Women", "Pants & Jumpsuits", "Wide Leg"), "bottoms"),
    ("poshmark", ("Women", "Skirts", "Skirt Sets"), "set"),
    ("poshmark", ("Kids", "Matching Sets", None), "set"),
    ("poshmark", ("Women", "Tops", "Sweatshirts & Hoodies"), "sweater"),
    ("poshmark", ("Women", "Jackets & Coats", "Blazers & Suit Jackets"), "outerwear"),
    ("poshmark", ("Men", "Suits & Blazers", "Suits"), "set"),
    ("poshmark", "Women > Dresses > Mini", "dress"),
    ("poshmark", "Women › Bags › Totes", "bag"),
    ("vinted", "Women > Clothing > Lingerie & nightwear > Dressing gowns", "sleepwear"),
    ("vinted", "Women > Accessories > Jewelry > Jewelry sets", "accessory"),
    ("vinted", "Women > Clothing > Shorts & cropped pants > Jean shorts", "shorts"),
    ("vinted", "Kids > Girls clothing > Sweaters & hoodies > Sweater vests", "sweater"),
    ("vinted", "Women > Clothing > Activewear > Sports bras", "intimates"),
    ("depop", "Women > Dresses > Shirt dresses", "dress"),
    ("depop", "Women > Bottoms > Skirts", "skirt"),
    ("depop", "Women > Tops > Cardigans", "sweater"),
    ("depop", "Women > Swimwear > Swim briefs and shorts", "swim"),
    ("depop", "Women > Swimwear > Bikini and tankini bottoms", "swim"),
    ("depop", "Kids > Onesies and sleepers > Onesies and sleepers", "jumpsuit"),
    ("depop", "Women > Accessories > Bags", "bag"),
])
def test_category_readings(site, category, family):
    assert family_of_category(site, category) == family


@pytest.mark.parametrize("site, category", [
    ("poshmark", ("Women", "Makeup", "Blush")), ("poshmark", ("Kids", "Toys", "Building Sets & Blocks")),
    ("poshmark", ("Home", "Bedding", "Sheets")), ("vinted", "Women > Clothing > Other clothing"),
    ("depop", "Kids > Clothing bundles > Clothing bundles"), ("vinted", None), ("depop", ""), ("poshmark", ()),
])
def test_unknown_categories(site, category):
    assert family_of_category(site, category) is None


def _all_paths():
    vinted = json.loads((DATA / "vinted_catalog.json").read_text(encoding="utf-8"))["categories"]
    depop = json.loads((DATA / "depop_catalog.json").read_text(encoding="utf-8"))["categories"]
    posh = yaml.safe_load((DATA / "poshmark_taxonomy.yaml").read_text(encoding="utf-8"))["departments"]
    out = [("vinted", x["path"]) for x in vinted.values()] + [("depop", p) for p in depop]
    for dep, body in posh.items():
        for cat, subs in (body.get("categories") or {}).items():
            out += [("poshmark", (dep, cat, None))] + [("poshmark", (dep, cat, sub)) for sub in subs or []]
    return out


CLOTHES = re.compile(r"^(women|men|kids)\b", re.IGNORECASE)
NON_CLOTHING = re.compile(r"makeup|skincare|\bhair\b(?! accessories)|bath|grooming|toys|home|electronics|global",
                          re.IGNORECASE)
GENERIC = re.compile(r"\bother\b|bundles|twins|activewear$|formal wear", re.IGNORECASE)


def test_every_clothing_category_of_the_three_catalogs_has_a_family():
    """Every clothing, shoes, bags and accessories category the three sites offer reads as a family — except the
    generic ones ("Other clothing", "Clothing bundles"): none is unknown by accident."""
    missing = []
    for site, cat in _all_paths():
        path = cat if isinstance(cat, str) else " > ".join(x for x in cat if x)
        if not CLOTHES.match(path) or NON_CLOTHING.search(path) or GENERIC.search(path.split(" > ")[-1]):
            continue
        if family_of_category(site, cat) is None:
            missing.append((site, path))
    assert missing == []


def test_no_category_reads_as_the_family_of_a_modifier():
    """The words that name a thing only as a modifier never decide a category: a "Dress" subcategory of pants, "Dress
    Shoes", "Dress Shirts", "Boot Cut", "Slip-Ons", "Flat Front"."""
    for site, cat in _all_paths():
        path = (cat if isinstance(cat, str) else " > ".join(x for x in cat if x)).lower()
        fam = family_of_category(site, cat)
        leaf = path.split(" > ")[-1]
        if re.search(r"dress (shoes|shirts|socks)|^dress$", leaf):
            assert fam != "dress", path
        if "boot cut" in leaf or "bootcut" in leaf:
            assert fam != "shoes", path
        if "slip-on" in leaf:
            assert fam != "intimates", path


@pytest.mark.parametrize("a, b, same", [
    ("dress", "sweater", False), ("dress", "top", False), ("dress", "set", False), ("dress", "skirt", False),
    ("top", "dress", False), ("shoes", "bag", False), ("sweater", "bottoms", False), ("costume", "top", False),
    ("set", "bottoms", True), ("set", "skirt", True), ("set", "shorts", True), ("sleepwear", "intimates", True),
    ("sleepwear", "set", True), ("intimates", "top", True), ("sweater", "top", True), ("sweater", "outerwear", True),
    ("bottoms", "jumpsuit", True), ("skirt", "shorts", True), ("costume", "dress", True), ("bag", "accessory", True),
    ("dress", "dress", True), (None, "dress", True), ("dress", None, True), (None, None, True), ("", "top", True),
])
def test_same_family(a, b, same):
    assert same_family(a, b) is same
    assert same_family(b, a) is same


def test_the_families_are_the_ones_named():
    assert set(F.FAMILIES) == {"dress", "skirt", "bottoms", "shorts", "top", "sweater", "outerwear", "set", "sleepwear",
                               "swim", "intimates", "jumpsuit", "shoes", "bag", "accessory", "costume"}
    assert set(F.VOCAB.values()) <= set(F.FAMILIES)
    assert {fam for pair in F.COMPATIBLE for fam in pair} <= set(F.FAMILIES)
