from thrift_agent.brain.price import nice_round, price

TIERS = {
    "brands": {"tory burch": {"target": 70}, "ugg": {"target": 65}, "h&m": {"target": 22}},
    "aliases": {"ugg australia": "ugg"},
    "category_defaults": {"Shoes": 45},
}


def test_brand_price_uses_markup_and_condition(facts, pricing_cfg):
    r = price(facts(), TIERS, pricing_cfg)
    assert r.source == "brand" and r.target == 70
    assert r.list_price == 85                       # 70 × 1.0 × 1.2 = 84 → 85
    assert r.by_marketplace["depop"] == 75          # 85 × 0.9 = 76.5 → 75


def test_alias_and_nwt(facts, pricing_cfg):
    f = facts(brand=facts().brand.model_copy(update={"value": "UGG Australia"}), condition="NWT")
    r = price(f, TIERS, pricing_cfg)
    assert r.target == 65 and r.list_price == 100   # 65 × 1.25 × 1.2 = 97.5 → 100


def test_unknown_brand_falls_back_to_category(facts, pricing_cfg):
    f = facts(brand=facts().brand.model_copy(update={"value": "Nobody Knows"}))
    assert price(f, TIERS, pricing_cfg).source == "category_default"


def test_floor_and_note(facts, pricing_cfg):
    f = facts(brand=facts().brand.model_copy(update={"value": "H&M"}), condition="fair")
    assert price(f, TIERS, pricing_cfg).list_price == 20
    assert price(f, TIERS, pricing_cfg, note="price 44").list_price == 44
    assert price(f, TIERS, pricing_cfg, note="floor 30").list_price == 30


def test_nice_round():
    assert nice_round(84, 5) == 85 and nice_round(2, 5) == 5
