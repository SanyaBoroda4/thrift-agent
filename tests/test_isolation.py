"""The suite must not depend on the machine it runs on.

CI runs the whole suite a second time with a prod-like config/settings.local.yaml, a private/settings.yaml, a
.env and exported TELEGRAM_* / ANTHROPIC_API_KEY in place (.github/prodlike_config.py). These tests are what prove
that setup is invisible: if isolation ever regresses, they go red there."""
import os

from thrift_agent import config
from thrift_agent.config import settings

SECRETS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY")


def test_settings_come_from_the_shipped_defaults_only():
    s = settings()
    assert s.get("machine_role") == "dev" and s.is_prod is False
    assert s.get("telegram.enabled") is False
    assert s.get("marketplaces.poshmark.username") == ""          # private/settings.yaml is not merged
    assert config.OVERRIDE_FILES == () and not config.ENV_FILE.exists() and not config.PRIVATE_DIR.exists()


def test_secrets_are_not_visible():
    assert not any(k in os.environ for k in SECRETS)


def test_private_data_falls_back_to_the_examples():
    assert config.style_dir() == config.ROOT / "data" / "style_examples"
    assert "birkenstock" in config.load_yaml("brand_tiers.yaml")["brands"]   # config/brand_tiers.example.yaml


def test_prod_behaviour_is_an_explicit_opt_in(settings_override):
    s = settings_override(machine_role="prod", telegram={"enabled": True})
    assert s.is_prod and settings().get("telegram.enabled") is True     # the same cached object


def test_nothing_leaks_between_tests():
    assert settings().get("machine_role") == "dev"                      # the opt-in above did not survive
