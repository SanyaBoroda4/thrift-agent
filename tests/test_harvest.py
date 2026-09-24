import pytest

from thrift_agent.harvest import parse_state, slim_order


def test_parse_state_tolerates_spacing():
    html = '<script>window.__INITIAL_STATE__ = {"a": {"b": [1, 2]}};window.x=1</script>'
    assert parse_state(html) == {"a": {"b": [1, 2]}}
    assert parse_state('__INITIAL_STATE__={"k":"v"}') == {"k": "v"}
    with pytest.raises(ValueError):
        parse_state("<html>nothing here</html>")


def test_slim_order_handles_missing_fields():
    o = slim_order({"title": "t", "line_items": [], "total_price_amount": None})
    assert o["title"] == "t" and o["brand"] is None and o["price"] is None and o["via_offer"] is False
