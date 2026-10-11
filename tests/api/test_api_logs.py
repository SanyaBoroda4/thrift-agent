"""WO35 §6: no log line of thrift-api carries an email's text, a buyer's details, a request body, a key or a token —
a failure is logged as its type and where it happened (util.where), never its message or its traceback text (a
parser's or a database's message can hold any of them). A whole live sale, logged at DEBUG, says none of them either."""
import json
import logging
import re

import pytest
from apitools import BUYER, NOW, call, posh_sale, seed

from thrift_api import core, telegram, util

GO_LIVE = "2026-10-01T00:00:00+00:00"
KEY = "fake-function-key-0123456789abcdef=="
TOKEN = "123456:not-a-real-token"


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", GO_LIVE)


def secrets_in(text: str, *more: str) -> list[str]:
    """The private words found in `text`: the buyer's name, handle and address, the key, the token, and `more`."""
    return [w for w in (*BUYER, KEY, TOKEN, "not-a-real-token", *more) if w in text]


def test_a_failing_email_is_logged_without_its_text(db, live, caplog, monkeypatch):
    """An email whose processing fails: the log says which email, the error's type and where — never the text the
    error carried (here the email's own lines, the buyer and a key)."""
    seed(db)
    email = posh_sale()

    def boom(*args, **kwargs):
        raise RuntimeError(f"Failing row contains ({email['text']} {KEY})")
    monkeypatch.setattr(core, "_sale", boom)
    with caplog.at_level(logging.DEBUG):
        status, out, _ = call(db, "POST", "/email", email)
    assert status == 200 and out["status"] == "failed"
    assert "posh-sale-1 could not be processed: RuntimeError at core.py:" in caplog.text
    assert secrets_in(caplog.text, "Congrats, you made a sale!", "Ship to") == []
    assert all(r.exc_info is None for r in caplog.records)                      # no traceback text either


def test_an_unexpected_error_is_logged_without_the_request_body(db, caplog, monkeypatch):
    body = {"items": [{"id": "i_1", "title": f"for {BUYER[0]}", "note": KEY}], "listings": []}

    def boom(*args, **kwargs):
        raise ValueError(f"could not store {json.dumps(body)}")
    monkeypatch.setattr(core, "sync", boom)
    with caplog.at_level(logging.DEBUG):
        assert call(db, "POST", "/sync", body)[:2] == (500, {"error": "internal error"})
    assert "POST /sync failed: ValueError at " in caplog.text
    assert secrets_in(caplog.text, "could not store") == []
    assert all(r.exc_info is None for r in caplog.records)


def test_a_failing_telegram_sender_is_logged_without_the_token(monkeypatch, caplog):
    def broken(chat, text, silent):
        raise RuntimeError(f"POST https://api.telegram.org/bot{TOKEN}/sendMessage: {text}")
    monkeypatch.setattr(telegram, "SENDER", broken)
    with caplog.at_level(logging.DEBUG):
        assert telegram.send(f"💰 Sold to {BUYER[0]}", "group", now=NOW, mode="live") is False
    assert "telegram: the sender failed: RuntimeError at " in caplog.text
    assert secrets_in(caplog.text, "api.telegram.org") == []


def test_a_whole_live_sale_logs_nothing_private(db, live, sent, caplog):
    seed(db)
    with caplog.at_level(logging.DEBUG):
        assert call(db, "POST", "/email", posh_sale())[1]["status"] == "matched"
        assert call(db, "GET", "/tasks", query={"sites": "depop,vinted"})[0] == 200
    assert secrets_in(caplog.text, "Congrats, you made a sale!") == []


def test_where_names_the_type_and_the_place_only():
    def inner():
        raise KeyError(f"{BUYER[2]} {KEY}")
    try:
        inner()
    except KeyError as e:
        said = util.where(e)
    assert said.startswith("KeyError at test_api_logs.py:") and secrets_in(said) == []


def test_a_timer_fails_without_its_message(monkeypatch):
    """The timers' wrapper (function_app._timer): the host logs a failed run as its type and place only — the error's
    own message (a database row's values) and its chain stay out."""
    pytest.importorskip("azure.functions")
    import function_app

    def boom():
        raise RuntimeError(f"Failing row contains ({BUYER[0]}, {BUYER[2]}, {KEY})")
    with pytest.raises(RuntimeError) as raised:
        function_app._timer(boom)
    assert str(raised.value).startswith("RuntimeError at function_app.py:") and secrets_in(str(raised.value)) == []
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_the_host_logs_warnings_and_our_own_lines():
    """host.json (WO35): the host's own lines from Warning (its per-call "Executing / Executed" lines too: category
    Function.<name>), one request row per call (Host.Results), our code's lines from Information — named per function
    (Function.<name>.User): the Functions host's Application Insights filter has no "*" in a category (its own copy of
    .NET's rule selector, prefixes only); no per-function aggregates, performance counters, live metrics, dependency
    tracking or extended HTTP request details (the query strings); sampling on with every exception kept."""
    from thrift_agent.config import ROOT
    host = json.loads((ROOT / "api" / "host.json").read_text(encoding="utf-8"))
    names = re.findall(r"^@app\.[^\n]+\ndef (\w+)\(", (ROOT / "api" / "function_app.py").read_text(encoding="utf-8"),
                       flags=re.M)
    assert len(names) == 15
    level = host["logging"]["logLevel"]
    assert level == {"default": "Warning", "Host.Results": "Information", "Host.Aggregator": "None",
                     "Function": "Warning", **{f"Function.{n}.User": "Information" for n in names},
                     "Microsoft.Azure.WebJobs.Script.Diagnostics.HealthChecks": "Error"}
    ai = host["logging"]["applicationInsights"]
    assert ai["samplingSettings"] == {"isEnabled": True, "maxTelemetryItemsPerSecond": 1, "evaluationInterval": "00:05:00",
                                      "excludedTypes": "Exception"}
    assert not (ai["enableLiveMetrics"] or ai["enableDependencyTracking"] or ai["enablePerformanceCountersCollection"])
    assert not any(ai["httpAutoCollectionOptions"].values())
