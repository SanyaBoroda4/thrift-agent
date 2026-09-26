from pathlib import Path

import pytest
import yaml

from thrift_agent.brain.price import category_default, nice_round, note_original_price, note_price, price
from thrift_agent.schema import Ev

TIERS = {
    "brands": {"tory burch": {"target": 70}, "ugg": {"target": 65}, "h&m": {"target": 22}},
    "aliases": {"ugg australia": "ugg"},
    "category_defaults": {"Shoes": 45},                         # the old flat shape: one table for every department
}
NESTED = {
    "brands": {"tory burch": {"target": 70}},
    "category_defaults": {"Women": {"Shoes": 40, "Dresses": 35},
                          "Kids": {"Shoes": 22, "Jackets & Coats": 25, "Tops": 10, "other": 12}},
}
NOBODY = Ev(value="Nobody Knows", photos=[3], source="photo", confidence=0.9)
EXAMPLE_TIERS = Path(__file__).resolve().parents[1] / "config" / "brand_tiers.example.yaml"


def test_brand_price_uses_markup_and_condition(facts, pricing_cfg):
    r = price(facts(), TIERS, pricing_cfg)
    assert r.source == "brand" and r.target == 70
    assert r.list_price == 85                       # 70 × 1.0 × 1.2 = 84 → 85
    assert r.by_marketplace["depop"] == 75          # 85 × 0.9 = 76.5 → 75
    assert "'tory burch'" in r.basis and "$70" in r.basis


def test_alias_and_nwt(facts, pricing_cfg):
    f = facts(brand=facts().brand.model_copy(update={"value": "UGG Australia"}), condition="NWT")
    r = price(f, TIERS, pricing_cfg)
    assert r.target == 65 and r.list_price == 100   # 65 × 1.25 × 1.2 = 97.5 → 100 (19.5 steps, half-up and half-even agree)


def test_unknown_brand_falls_back_to_category(facts, pricing_cfg):
    f = facts(brand=facts().brand.model_copy(update={"value": "Nobody Knows"}))
    assert price(f, TIERS, pricing_cfg).source == "category_default"


def test_category_defaults_are_per_department(facts, pricing_cfg):
    r = price(facts(brand=NOBODY, department="Kids"), NESTED, pricing_cfg)
    assert r.source == "category_default" and r.target == 22 and "Kids 'Shoes'" in r.basis
    r = price(facts(brand=NOBODY, department="Kids", category="Sweaters"), NESTED, pricing_cfg)
    assert r.source == "category_default" and r.target == 12 and "Kids 'other'" in r.basis
    assert price(facts(brand=NOBODY), NESTED, pricing_cfg).target == 40                        # Women Shoes
    assert price(facts(brand=NOBODY, category="Sweaters"), NESTED, pricing_cfg).source == "none"   # no Women other
    r = price(facts(brand=NOBODY, department="Unisex"), NESTED, pricing_cfg)                  # department not listed
    assert r.source == "none" and r.list_price is None
    assert price(facts(department="Kids"), NESTED, pricing_cfg).source == "brand"             # a brand hit wins


def test_flat_category_defaults_apply_to_every_department(facts, pricing_cfg):
    for dept in ("Women", "Men", "Kids"):
        r = price(facts(brand=NOBODY, department=dept), TIERS, pricing_cfg)
        assert r.source == "category_default" and r.target == 45, dept
    assert category_default({"Shoes": 45, "other": 10}, "Kids", "Tops") == (10, "other")
    assert category_default({"shoes": 45}, "Kids", "Shoes") == (45, "Shoes")                 # keys match loosely
    assert category_default({"Women": {"Shoes": 40}, "Kids": None}, "Kids", "Shoes") is None   # `Kids:` left empty
    assert category_default(None, "Women", "Shoes") is None


def test_example_tier_file_has_kids_defaults(facts, pricing_cfg):
    tiers = yaml.safe_load(EXAMPLE_TIERS.read_text(encoding="utf-8"))
    r = price(facts(brand=NOBODY, department="Kids"), tiers, pricing_cfg)
    assert r.source == "category_default" and r.target == 22
    assert price(facts(brand=NOBODY, department="Kids", category="Sweaters"), tiers, pricing_cfg).target == 12
    assert price(facts(brand=NOBODY, department="Home", category="Decor"), tiers, pricing_cfg).target == 20
    assert price(facts(brand=NOBODY), tiers, pricing_cfg).target == 40


def test_floor_and_note(facts, pricing_cfg):
    f = facts(brand=facts().brand.model_copy(update={"value": "H&M"}), condition="fair")
    assert price(f, TIERS, pricing_cfg).list_price == 20
    assert price(f, TIERS, pricing_cfg, note="price 44").list_price == 44
    assert price(f, TIERS, pricing_cfg, note="floor 30").list_price == 30


def test_nice_round():
    assert nice_round(84, 5) == 85 and nice_round(2, 5) == 5


def test_nice_round_is_half_up_not_bankers():
    # round() is half-to-even: 12.5 → 10 and 22.5 → 20 but 27.5 → 30. Prices round half up consistently.
    assert nice_round(12.5, 5) == 15
    assert nice_round(22.5, 5) == 25
    assert nice_round(27.5, 5) == 30
    assert nice_round(84, 5) == 85
    assert nice_round(2, 5) == 5


# --- note parsing -------------------------------------------------------------------------------------------


def test_note_price_ignores_original_and_retail_price():
    note = "original price $120, worn twice"
    assert note_price(note) is None
    assert note_original_price(note) == 120
    assert note_price("retail price 200") is None
    assert note_price("msrp price $200") is None
    assert note_price("paid price 90") is None


@pytest.mark.parametrize("note, expected", [
    ("price 44", 44),
    ("price: 45", 45),
    ("price=45", 45),
    ("price $45", 45),
    ("list at $50", 50),
    ("list $50", 50),
    ("nothing to see here", None),
    (None, None),
])
def test_note_price_accepts_separators(note, expected):
    assert note_price(note) == expected


def test_note_price_and_original_price_side_by_side():
    note = "retail 200, price 60"
    assert note_price(note) == 60
    assert note_original_price(note) == 200
    assert note_original_price("paid $40 for these") == 40
    assert note_original_price("price 60") is None
    assert note_original_price(None) is None


# --- price() ------------------------------------------------------------------------------------------------


def test_note_price_is_not_rerounded_per_marketplace(facts, pricing_cfg):
    r = price(facts(), TIERS, pricing_cfg, note="price 44")
    assert r.source == "note" and r.list_price == 44
    assert r.by_marketplace["poshmark"] == 44       # 1.0 multiplier: exactly what the seller wrote, not $45
    assert r.by_marketplace["depop"] == 40          # 44 × 0.9 = 39.6 → nearest dollar, no step rounding


def test_note_price_still_respects_floor(facts, pricing_cfg):
    r = price(facts(), TIERS, pricing_cfg, note="price 15")
    assert r.list_price == 20 and r.by_marketplace["poshmark"] == 20 and r.by_marketplace["depop"] == 20


def test_price_carries_original_price_from_note(facts, pricing_cfg):
    r = price(facts(), TIERS, pricing_cfg, note="retail 200, price 60")
    assert r.source == "note" and r.list_price == 60 and r.original_price == 200
    assert price(facts(), TIERS, pricing_cfg).original_price is None
    # A retail mention alone does not set the asking price; it only fills Original Price next to the brand price.
    r = price(facts(), TIERS, pricing_cfg, note="original price $120")
    assert r.source == "brand" and r.list_price == 85 and r.original_price == 120
    # ...and survives the no-price path too, so the copy can still show it.
    assert price(facts(), None, pricing_cfg, note="paid 40").original_price == 40


@pytest.mark.parametrize("tiers", [
    {"brands": None, "aliases": None, "category_defaults": None},   # `aliases:` with nothing under it
    {},
    None,                                                           # entirely empty YAML file
])
def test_empty_tier_sections_do_not_crash(facts, pricing_cfg, tiers):
    r = price(facts(), tiers, pricing_cfg)
    assert r.source == "none" and r.list_price is None
    # A seller note still prices the item even with no tiers at all.
    assert price(facts(), tiers, pricing_cfg, note="price 44").list_price == 44


def test_tier_brand_keys_are_normalised_like_the_model_brand(facts, pricing_cfg):
    tiers = {"brands": {"Tory Burch": {"target": 70}}}
    f = facts(brand=facts().brand.model_copy(update={"value": "tory  burch"}))
    r = price(f, tiers, pricing_cfg)
    assert r.source == "brand" and r.target == 70


def test_tier_alias_keys_and_values_are_normalised(facts, pricing_cfg):
    tiers = {"brands": {"UGG": {"target": 65}}, "aliases": {"Ugg  Australia": " UGG "}}
    f = facts(brand=facts().brand.model_copy(update={"value": "ugg australia"}))
    r = price(f, tiers, pricing_cfg)
    assert r.source == "brand" and r.target == 65
