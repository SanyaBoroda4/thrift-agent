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
    for name in ("SALES_MODE", "DATABASE_URL", *EBAY_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(http, "_DB", None)


EBAY_ENV = ("EBAY_ENV", "EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_RUNAME", "EBAY_SCOPES", "EBAY_VERIFICATION_TOKEN",
            "EBAY_DELETION_ENDPOINT", "EBAY_TOKEN_KEY", "EBAY_START_LINK", "EBAY_API")


@pytest.fixture(autouse=True)
def no_ebay(monkeypatch):
    """WO57: no test ever reaches eBay — the one way out (thrift_api.ebay.SENDER) fails loudly unless a test puts its
    own fake there (ebayfake.FakeEbay); eBay's settings never come from the machine (api_env)."""
    from thrift_api import ebay

    def refuse(method, url, headers, body, timeout):
        raise AssertionError(f"a test called eBay without a fake: {method} {url.split('?')[0]}")
    monkeypatch.setattr(ebay, "SENDER", refuse)


@pytest.fixture
def fake_ebay(monkeypatch, no_ebay):
    """WO57: eBay set up (its settings as provision.py makes them) and tests/api/ebayfake.FakeEbay in place of the one
    way out to it — the token endpoint, the Listing API, the orders."""
    from ebayfake import install
    return install(monkeypatch)


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    """Every Telegram message the API sends in a test, as (chat, text, silent); nothing reaches the network."""
    from thrift_api import telegram
    log = []
    monkeypatch.setattr(telegram, "SENDER",
                        lambda chat, text, silent, html=False: log.append((chat, text, silent)) or True)
    return log


@pytest.fixture
def no_quiet_hours(monkeypatch):
    """WO54 §3: the owner's quiet hours (8:30 AM – 2:00 PM New York, on by default — thrift_api.quiet.DEFAULTS) turned
    off, as a Mac whose heartbeat says {"quiet": {"enabled": false}}. For the tests of the nets themselves, whose clock
    (apitools.NOW: Thu Oct 8, 11:00 AM) sits inside the quiet hours; test_api_wo54_quiet.py tests the quiet hours."""
    from thrift_api import quiet
    monkeypatch.setitem(quiet.DEFAULTS, "enabled", False)


@pytest.fixture
def db():
    """A migrated in-memory SQLite database."""
    from thrift_api.db import Database
    from thrift_api.schema import migrate
    database = Database("sqlite:///:memory:")
    migrate(database)
    yield database
    database.close()
