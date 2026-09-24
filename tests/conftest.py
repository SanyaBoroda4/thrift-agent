import pytest

from thrift_agent.schema import Ev, Facts


@pytest.fixture
def facts():
    def make(**kw):
        base = dict(
            item_type="suede ballet flats", department="Women", category="Shoes", subcategory="Flats & Loafers",
            brand=Ev(value="Tory Burch", photos=[3], source="photo", confidence=0.97),
            size_printed=Ev(value="7.5M", photos=[3], source="photo", confidence=0.95),
            size_us=Ev(value="7.5", photos=[3], source="photo", confidence=0.95),
            colors=["Red"], condition="excellent",
            condition_evidence=Ev(value="light sole wear", photos=[4], source="photo", confidence=0.9),
            cover_photo=0, photo_order=[0, 1, 2, 3, 4],
        )
        base.update(kw)
        return Facts(**base)
    return make


@pytest.fixture
def pricing_cfg():
    return {"floor": 20, "list_markup": 1.2, "round_to": 5,
            "condition_multiplier": {"NWT": 1.25, "NWOT": 1.15, "like_new": 1.05, "excellent": 1.0,
                                     "good": 0.85, "fair": 0.65},
            "marketplace_multiplier": {"poshmark": 1.0, "depop": 0.9}}


@pytest.fixture
def gate_cfg():
    return {"min_confidence": {"brand": 0.85, "size": 0.85, "condition": 0.8},
            "allow_category_default_price": False}
