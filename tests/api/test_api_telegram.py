"""thrift_api.telegram (WO33): send-only — the group or the ops chat, silent in the quiet hours (never later), everything
to the ops chat as "[replay] …" in replay, nothing in off; the HTTP call itself (sendMessage), the token never logged."""
import io
import json
import logging
from urllib import error as urlerror

import pytest
from apitools import NOW, utc

from thrift_api import telegram

TOKEN = "123456:not-a-real-token"


class Response:
    def __init__(self, body: bytes):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def telegram_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_GROUP_CHAT_ID", "-1001")
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "42")


def test_live_sends_where_it_is_asked(monkeypatch, sent):
    monkeypatch.setenv("SALES_MODE", "live")
    assert telegram.send("💰 Sold", "group", now=NOW) is True
    assert telegram.send("details", "ops", now=NOW) is True
    assert sent == [("group", "💰 Sold", False), ("ops", "details", False)]


def test_the_quiet_hours_are_silent_never_late(sent):
    telegram.send("night", "group", now=utc(2026, 10, 9, 3, 30), mode="live")        # 23:30 in New York
    telegram.send("early", "group", now=utc(2026, 10, 9, 11, 59), mode="live")       # 07:59
    telegram.send("morning", "group", now=utc(2026, 10, 9, 12, 0), mode="live")      # 08:00
    assert sent == [("group", "night", True), ("group", "early", True), ("group", "morning", False)]


def test_replay_goes_to_ops_and_off_sends_nothing(sent):
    assert telegram.send("💰 Sold", "group", now=NOW, mode="replay") is True
    assert telegram.send("details", "ops", now=NOW, mode="replay") is True
    assert telegram.send("x", "group", now=NOW, mode="off") is False
    assert sent == [("ops", "[replay] 💰 Sold", False), ("ops", "[replay] details", False)]


def test_the_mode_defaults_to_replay(sent):
    telegram.send("hello", "group", now=NOW)
    assert sent == [("ops", "[replay] hello", False)]


def test_a_bad_chat_and_a_failing_sender(monkeypatch):
    with pytest.raises(ValueError):
        telegram.send("x", "everyone", now=NOW, mode="live")

    def broken(*args):
        raise RuntimeError("down")
    monkeypatch.setattr(telegram, "SENDER", broken)
    assert telegram.send("x", "group", now=NOW, mode="live") is False


def test_a_long_message_is_cut_to_telegrams_limit(sent):
    telegram.send("x" * 5000, "ops", now=NOW, mode="live")
    assert len(sent[0][1]) == 4096 and sent[0][1].endswith("…")


def test_the_http_call_is_one_send_message(monkeypatch, telegram_env):
    seen = []

    def urlopen(request, timeout):
        seen.append((request, timeout))
        return Response(b'{"ok": true, "result": {}}')
    monkeypatch.setattr(telegram.urlrequest, "urlopen", urlopen)
    assert telegram.http_send("group", "💰 Sold", True) is True
    assert telegram.http_send("ops", "details", False) is True
    (group, timeout), (ops, _) = seen
    assert group.full_url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert (group.get_method(), timeout, group.get_header("Content-type")) == ("POST", 10, "application/json")
    assert json.loads(group.data) == {"chat_id": "-1001", "text": "💰 Sold", "disable_notification": True,
                                      "link_preview_options": {"is_disabled": True}}
    assert json.loads(ops.data)["chat_id"] == "42"


def test_no_request_without_the_settings(monkeypatch):
    monkeypatch.setattr(telegram.urlrequest, "urlopen", lambda *args, **kwargs: pytest.fail("a request was made"))
    assert telegram.http_send("group", "x", False) is False


def test_failures_are_logged_without_the_token(monkeypatch, telegram_env, caplog):
    def unreachable(request, timeout):
        raise urlerror.URLError(f"cannot reach {request.full_url}")
    monkeypatch.setattr(telegram.urlrequest, "urlopen", unreachable)
    with caplog.at_level(logging.WARNING):
        assert telegram.http_send("group", "x", False) is False

    def refused(request, timeout):
        raise urlerror.HTTPError(request.full_url, 400, "Bad Request", {},
                                 io.BytesIO(b'{"ok": false, "description": "Bad Request: chat not found"}'))
    monkeypatch.setattr(telegram.urlrequest, "urlopen", refused)
    with caplog.at_level(logging.WARNING):
        assert telegram.http_send("ops", "x", False) is False
    monkeypatch.setattr(telegram.urlrequest, "urlopen", lambda request, timeout: Response(b'{"ok": false}'))
    assert telegram.http_send("ops", "x", False) is False
    assert "chat not found" in caplog.text and "<token>" in caplog.text
    assert TOKEN not in caplog.text and "not-a-real-token" not in caplog.text
