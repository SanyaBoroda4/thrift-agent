import json

import pytest

import thrift_agent.harvest as hv
from thrift_agent.config import Settings
from thrift_agent.harvest import _merge_hrefs, _num, build_style, parse_state, slim_order


def test_parse_state_tolerates_spacing():
    html = '<script>window.__INITIAL_STATE__ = {"a": {"b": [1, 2]}};window.x=1</script>'
    assert parse_state(html) == {"a": {"b": [1, 2]}}
    assert parse_state('__INITIAL_STATE__={"k":"v"}') == {"k": "v"}
    with pytest.raises(ValueError):
        parse_state("<html>nothing here</html>")


def test_slim_order_handles_missing_fields():
    o = slim_order({"title": "t", "line_items": [], "total_price_amount": None})
    assert o["title"] == "t" and o["brand"] is None and o["price"] is None and o["via_offer"] is False


def test_num_parses_poshmark_amounts():
    assert _num("45.00") == 45.0
    assert _num("$1,200.50") == 1200.5
    assert _num(7) == 7.0
    assert _num("") == 0.0 and _num(None) == 0.0 and _num("n/a") == 0.0


def test_merge_hrefs_dedupes_within_a_page():
    # The same order can be linked twice on one page; query-string variants are not order pages.
    assert _merge_hrefs([], ["/order/sales/1", "/order/sales/1", "/order/sales/2?tab=x"]) == ["/order/sales/1"]
    assert _merge_hrefs(["/order/sales/1"], ["/order/sales/3", "/order/sales/1", "/order/sales/3"]) == [
        "/order/sales/1", "/order/sales/3"]


def _order(lid, status="Order Complete", price=None, rating=None):
    return {"title": lid, "brand": "B", "category": "Shoes", "size": "7", "product_url": f"/listing/Thing-{lid}",
            "price": price, "earnings": None, "status": status, "cancel_reason": None, "via_offer": False,
            "booked_at": None, "rating": rating, "rating_comment": None, "picture_url": None}


def test_build_style_sorts_numerically_and_skips_errors(tmp_path, monkeypatch):
    hd = tmp_path / "harvest"
    (hd / "listings").mkdir(parents=True)
    orders = [
        _order("aaa", price="9.00", rating=5),           # ties with bbb on rating; '9.00' > '45.00' as strings
        _order("bbb", price="45.00", rating=5),
        _order("ccc", price="$1,200.00", rating=None),   # None rating used to TypeError against an int
        _order("ddd", status="Cancelled", price="80.00", rating=5),
        _order("eee", price="30.00", rating=5),          # listing page unreadable (account restricted)
        {"href": "/order/sales/zzz", "error": "ValueError: no __INITIAL_STATE__ on page"},
    ]
    (hd / "orders.json").write_text(json.dumps(orders), encoding="utf-8")
    for lid in ("aaa", "bbb", "ccc", "ddd"):
        (hd / "listings" / f"{lid}.json").write_text(
            json.dumps({"title": f"T-{lid}", "description": "d", "price_amount": {"val": "50.00"}}), encoding="utf-8")
    (hd / "listings" / "eee.json").write_text(json.dumps({"error": "PostRemovedError"}), encoding="utf-8")
    monkeypatch.setattr(hv, "PRIVATE_DIR", tmp_path / "private")
    s = Settings({"paths": {"harvest": str(hd)}})

    dst = build_style(s, keep=30)

    assert dst == tmp_path / "private" / "style_examples" / "poshmark_listings.json"
    got = json.loads(dst.read_text(encoding="utf-8"))
    assert [e["title"] for e in got] == ["T-bbb", "T-aaa", "T-ccc"]
    assert got[2]["sold_price"] == "$1,200.00"           # values are kept verbatim; only the sort is numeric
