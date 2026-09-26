import os

import pytest

from thrift_agent import config
from thrift_agent.schema import Ev, Facts

SECRETS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY")


@pytest.fixture(scope="session", autouse=True)
def _scrub_session_env():
    """The suite never sees the machine's secrets, even when the shell exported them (launchd, a sourced .env)."""
    saved = {k: os.environ.pop(k) for k in SECRETS if k in os.environ}
    yield
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    """Tests read config/settings.yaml and nothing else: never config/settings.local.yaml, private/settings.yaml
    or .env, whatever machine runs them.

    On the Mac the real local config says machine_role: prod + telegram.enabled: true, and a bare `pytest` (run by
    deploy.ps1 and mac_setup.sh) used to fail in notify.check() because the token was not in .env yet. A test that
    wants prod behaviour or private data opts in with `settings_override` or by pointing config.PRIVATE_DIR at a
    fixture of its own. The settings cache is cleared before and after every test so nothing leaks between tests."""
    nowhere = tmp_path / "no-such-file"
    monkeypatch.setattr(config, "ENV_FILE", nowhere)
    monkeypatch.setattr(config, "OVERRIDE_FILES", ())
    monkeypatch.setattr(config, "PRIVATE_DIR", tmp_path / "no-private")   # load_yaml/style_dir fall back to the examples
    for k in SECRETS:
        monkeypatch.delenv(k, raising=False)
    config.settings.cache_clear()
    yield
    config.settings.cache_clear()


@pytest.fixture
def settings_override():
    """Opt in to non-default settings for one test, e.g. settings_override(machine_role="prod", telegram={"enabled": True}).
    Mutates the cached Settings, so every reader of settings() in that test sees it; the cache is reset afterwards."""
    def apply(**over):
        s = config.settings()
        s.data = config._merge(s.data, over)
        return s
    return apply


@pytest.fixture(autouse=True)
def no_telegram(monkeypatch):
    """Nothing in the suite may reach Telegram: force the disabled (print) path and make any HTTP call fail loudly.
    test_notify's own tests re-patch _enabled / httpx.post inside the test body, which overrides this."""
    def no_network(*a, **k):
        raise AssertionError("network call in tests")
    monkeypatch.setattr("thrift_agent.notify._enabled", lambda: (False, "", ""))
    monkeypatch.setattr("thrift_agent.notify.httpx.post", no_network)


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
    return {"min_confidence": {"brand": 0.70, "size": 0.70, "condition": 0.70}}
