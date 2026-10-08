"""The Function's package (api/thrift_api) on the import path for its tests: it is deployed from api/, not installed."""
import sys

import pytest

from thrift_agent.config import ROOT

if str(ROOT / "api") not in sys.path:
    sys.path.insert(0, str(ROOT / "api"))


@pytest.fixture(autouse=True)
def api_env(monkeypatch):
    """SALES_MODE and DATABASE_URL never come from the machine running the tests, and the database the API opens
    lazily is forgotten after each test."""
    from thrift_api import http
    for name in ("SALES_MODE", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(http, "_DB", None)


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    """Every Telegram message the API sends in a test, as (chat, text, silent); nothing reaches the network."""
    from thrift_api import telegram
    log = []
    monkeypatch.setattr(telegram, "SENDER", lambda chat, text, silent: log.append((chat, text, silent)) or True)
    return log


@pytest.fixture
def db():
    """A migrated in-memory SQLite database."""
    from thrift_api.db import Database
    from thrift_api.schema import migrate
    database = Database("sqlite:///:memory:")
    migrate(database)
    yield database
    database.close()
