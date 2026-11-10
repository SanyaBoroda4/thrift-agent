"""WO57 §3/§6 — the eBay price: the same profit as the same sale on Poshmark. Every fee branch, the WO's three examples,
the rounding, the fee file's date."""
from __future__ import annotations

import json
from datetime import date

import pytest

from thrift_agent.ebay import fees

SHIP, TAX = 6.0, 0.07
BAGS, SNEAKERS, CLOTHING = ["169291"], ["15709"], ["11450", "15724", "63861"]


def q(base, categories=(), shipping=SHIP, tax=TAX):
    return fees.quote(base, categories, shipping, tax)


def test_poshmark_keeps_2_95_under_15_dollars_and_a_fifth_from_15():
    assert fees.poshmark_payout(10) == pytest.approx(7.05)
    assert fees.poshmark_payout(14) == pytest.approx(11.05)
    assert fees.poshmark_payout(15) == pytest.approx(12.00)          # from $15: 20%
    assert fees.poshmark_payout(20) == pytest.approx(16.00)
    assert fees.poshmark_payout(50) == pytest.approx(40.00)


@pytest.mark.parametrize("base, about", [(20, 20), (50, 48), (10, 10)])
def test_the_work_orders_three_examples_with_6_dollars_shipping(base, about):
    """WO57 §3: "with $6 shipping, B = $20 → about $20; B = $50 → about $48; B = $10 → about $10"."""
    quote = q(base)
    assert quote.price == about
    assert quote.rule == "Most categories"
    # the same profit: the eBay payout at that price is the Poshmark payout, the rounding to a dollar aside
    assert abs(quote.difference) <= 0.5
    assert quote.poshmark_payout == pytest.approx(fees.poshmark_payout(base))


def test_the_fee_is_a_share_of_the_total_item_plus_shipping_plus_tax_and_an_order_fee():
    s = fees.sale(48, (), SHIP, TAX)
    assert s.tax == pytest.approx(0.07 * 54)
    assert s.total == pytest.approx(54 * 1.07)
    assert s.final_value_fee == pytest.approx(0.136 * 54 * 1.07)
    assert s.order_fee == 0.40                                        # the order is over $10
    assert s.payout == pytest.approx(48 - 0.136 * 57.78 - 0.40)
    small = fees.sale(3, (), 0, 0)                                    # a $3 order: the smaller order fee
    assert small.order_fee == 0.30 and small.payout == pytest.approx(3 - 0.136 * 3 - 0.30)
    assert fees.sale(10, (), 0, 0).order_fee == 0.30                  # "up to $10.00": $10 itself
    assert fees.sale(10.01, (), 0, 0).order_fee == 0.40


def test_womens_bags_and_handbags_pay_15_percent():
    bag = q(100, BAGS)
    plain = q(100)
    assert bag.rule == "Women's bags and handbags"
    assert bag.price == 97 and plain.price == 95                      # the higher fee asks a higher price
    s = fees.sale(97, BAGS, SHIP, TAX)
    assert s.final_value_fee == pytest.approx(0.15 * 103 * 1.07)
    assert abs(bag.difference) <= 0.5


def test_a_bag_in_a_subcategory_is_found_by_its_ancestors():
    assert q(100, ["11450", "260010", "169291", "45258"]).rule == "Women's bags and handbags"   # the path holds its id
    assert q(100, ["11450", "260010", "15724", "63861"]).rule == "Most categories"                # a dress: no rule
    assert q(100, ["99999"]).rule == "Most categories"


def test_athletic_shoes_from_150_dollars_pay_8_percent_and_no_order_fee():
    quote = q(250, SNEAKERS)                                          # Poshmark leaves $200
    assert quote.rule == "Athletic shoes, $150 or more"
    assert quote.price == 219
    s = fees.sale(219, SNEAKERS, SHIP, TAX)
    assert s.order_fee == 0 and s.final_value_fee == pytest.approx(0.08 * 225 * 1.07)
    assert abs(quote.difference) <= 0.5
    cheap = q(60, SNEAKERS)                                           # under $150: the ordinary rate and order fee
    assert cheap.rule == "Most categories" and cheap.price == q(60).price


def test_a_price_just_under_the_150_dollar_rule_takes_the_dollar_above_it():
    """B = $150 on Poshmark leaves $120. On eBay the 13.6% rate would need $142 — a $158 total, where the 8% rule
    applies and pays more than asked; the 8% rate would need $132 — a $147 total, where it doesn't apply. The lowest
    price that reaches her profit is the one whose total is $150: never the dollar under it (13.6%, $7 short)."""
    quote = q(150, SNEAKERS)
    assert quote.price == 135 and quote.rule == "Athletic shoes, $150 or more"
    assert fees.sale(134, SNEAKERS, SHIP, TAX).rule == "Most categories"          # $149.80 in all
    assert quote.ebay_payout >= quote.poshmark_payout
    assert fees.ebay_payout(134, SNEAKERS, SHIP, TAX) < quote.poshmark_payout - 5


def test_a_tier_in_the_file_puts_its_rate_on_the_part_of_a_sale_over_it(tmp_path):
    """The file can say "13.6% up to $2,000, 9% on the part above" (`up_to`): the fee and the solver follow it."""
    data = json.loads(fees.DATA.read_text(encoding="utf-8"))
    data["default"]["tiers"] = [{"up_to": 7500, "rate": 0.136}, {"rate": 0.0235}]
    data["rules"].append({"name": "Clothing", "categories": ["11450"],
                          "tiers": [{"up_to": 2000, "rate": 0.136}, {"rate": 0.09}]})
    file = tmp_path / "fees.json"
    file.write_text(json.dumps(data), encoding="utf-8")
    table = fees.load(file)
    assert fees.sale(3000, CLOTHING, 0, 0, table).final_value_fee == pytest.approx(0.136 * 2000 + 0.09 * 1000)
    assert fees.sale(10000, (), 0, 0, table).final_value_fee == pytest.approx(0.136 * 7500 + 0.0235 * 2500)
    quote = fees.quote(4000, CLOTHING, 0, 0, table)                   # the solver follows the tiers
    assert abs(quote.difference) <= 0.5 and quote.price < fees.quote(4000, (), 0, 0).price


@pytest.mark.parametrize("base", [3, 5, 9, 10, 12, 14, 15, 16, 20, 25, 35, 48, 50, 75, 99, 120, 180, 250, 400, 900])
@pytest.mark.parametrize("categories", [(), BAGS, SNEAKERS, CLOTHING])
def test_every_price_is_a_whole_dollar_that_leaves_her_poshmarks_profit(base, categories):
    quote = q(base, categories)
    assert isinstance(quote.price, int) and quote.price >= 1
    # never short of Poshmark's profit by more than the rounding to a dollar; over it only by the rounding too, or by
    # a cheaper rule's own jump (athletic shoes at $150)
    assert quote.ebay_payout >= quote.poshmark_payout - 0.5
    assert quote.ebay_payout <= quote.poshmark_payout + 0.5 or quote.rule.startswith("Athletic shoes")


def test_rounding_is_to_the_nearest_whole_dollar():
    for base in range(5, 120):
        quote = q(base)
        assert abs(quote.price - quote.exact) <= 0.5 + 1e-6


def test_a_higher_base_never_gives_a_lower_ebay_price():
    last = 0
    for base in range(3, 400):
        price = fees.price_for(base, (), SHIP, TAX)
        assert price >= last
        last = price


def test_shipping_and_tax_the_buyer_pays_raise_the_price_a_little():
    free, six, twelve = q(50, shipping=0).price, q(50, shipping=6).price, q(50, shipping=12).price
    assert free <= six <= twelve and twelve - free <= 3
    assert q(50, tax=0.0).price <= q(50, tax=0.10).price


def test_a_base_price_must_be_a_positive_number():
    for bad in (0, -5, float("nan")):
        with pytest.raises(ValueError):
            fees.quote(bad)


def test_the_fee_file_carries_the_day_it_was_checked_and_is_stale_after_90_days():
    f = fees.load()
    assert isinstance(f.checked, date) and f.source.startswith("https://www.ebay.com/")
    assert f.insertion.free_per_month == 250 and f.insertion.fee == 0.35
    assert fees.stale(f, f.checked) is None
    assert fees.stale(f, date.fromordinal(f.checked.toordinal() + 89)) is None
    assert fees.stale(f, date.fromordinal(f.checked.toordinal() + 90)) == 90
    assert fees.quote(20, (), SHIP, TAX).checked == f.checked


def test_the_month_s_free_listings_then_35_cents_each():
    f = fees.load()
    assert fees.insertion_fee(f, 0) == 0 and fees.insertion_fee(f, 249) == 0
    assert fees.insertion_fee(f, 250) == 0.35 and fees.insertion_fee(f, 400) == 0.35


def test_a_fee_file_that_is_missing_or_broken_says_so(tmp_path):
    with pytest.raises(fees.FeesError, match="missing"):
        fees.load(tmp_path / "none.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(fees.FeesError, match="not usable"):
        fees.load(bad)
    data = json.loads(fees.DATA.read_text(encoding="utf-8"))
    del data["checked"]
    bad.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(fees.FeesError, match="checked"):
        fees.load(bad)


def test_a_changed_fee_file_changes_the_price(tmp_path):
    """The rates come from the file, by category: a new rate is a new price with no code change."""
    data = json.loads(fees.DATA.read_text(encoding="utf-8"))
    data["default"]["tiers"] = [{"rate": 0.20}]
    data["checked"] = "2027-01-15"
    file = tmp_path / "fees.json"
    file.write_text(json.dumps(data), encoding="utf-8")
    dearer = fees.quote(50, (), SHIP, TAX, fees.load(file))
    assert dearer.price > q(50).price and dearer.checked == date(2027, 1, 15)
