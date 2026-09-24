import pytest

from thrift_agent.schema import Ev, Facts


@pytest.fixture(autouse=True)
def no_telegram(monkeypatch):
    """Nothing in the suite may reach Telegram.

    test_pipeline calls the real notify with the machine's merged settings; on the Mac settings.local.yaml enables
    Telegram and .env holds the token, so a bare `pytest` (deploy.ps1, mac_setup.sh) would ping the seller a photo
    of a synthetic batch on every deploy. Force the disabled (print) path and make any HTTP call fail loudly.
    test_notify's own tests re-patch _enabled / httpx.post inside the test body, which overrides this."""
    def no_network(*a, **k):
        raise AssertionError("network call in tests")
    monkeypatch.setattr("thrift_agent.notify._enabled", lambda: (False, "", ""))
    monkeypatch.setattr("thrift_agent.notify.httpx.post", no_network)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)


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
