"""The Telegram link (Alex, 2026-10-27: "the Mac's Telegram link keeps dropping"): one keep-alive client, a request
that never left the Mac tried again 1, 2, 4 s later, Telegram's known addresses while DNS fails, never a second send
of a request that went out; the lines that couldn't go held and sent later; the poll's back-off and one event per
outage; a ✅ card that never left sent by the next sweep; one "wasn't listed" line per item and site."""
import socket

import httpx
import pytest

from thrift_agent import approve, cli, crosslist, notify, telegram
from thrift_agent.db import DB
from thrift_agent.post import runner

DNS = "[Errno 8] nodename nor servname provided, or not known"
REAL_ENABLED, REAL_OPS_ENABLED = notify._enabled, notify._ops_enabled   # before conftest switches them off


class Resp:
    status_code = 200

    def __init__(self, body=None):
        self.body = body if body is not None else {"ok": True, "result": {"message_id": 5}}

    def json(self):
        return self.body

    def raise_for_status(self):
        return None


class Wire:
    """telegram.http_post as the network answers it: `fail` = the errors to raise first, in order."""

    def __init__(self, *fail):
        self.fail, self.calls = list(fail), []

    def __call__(self, url, data=None, files=None, timeout=None, **extra):
        self.calls.append({"url": url, "data": data, **extra})
        if self.fail:
            raise self.fail.pop(0)
        return Resp()


@pytest.fixture
def slept(monkeypatch):
    pauses = []
    monkeypatch.setattr(telegram, "_sleep", pauses.append)
    return pauses


def test_a_request_that_never_left_the_mac_is_tried_again_after_1_2_4_s(monkeypatch, slept):
    wire = Wire(httpx.ConnectError("[Errno 54] Connection reset by peer"), httpx.ConnectTimeout("timed out"))
    monkeypatch.setattr(telegram, "http_post", wire)
    assert telegram.Bot("T", "1", {7}).send_message("hi") == 5
    assert len(wire.calls) == 3 and slept == [1.0, 2.0]
    wire = Wire(*[httpx.ConnectError("[Errno 51] Network is unreachable")] * 4)
    monkeypatch.setattr(telegram, "http_post", wire)
    with pytest.raises(RuntimeError, match="telegram sendMessage: ConnectError"):
        telegram.Bot("T", "1", {7}).send_message("hi")
    assert len(wire.calls) == 4                                           # three more tries, then it is said


def test_a_request_that_went_out_is_never_sent_twice(monkeypatch, slept):
    wire = Wire(httpx.ReadTimeout("The read operation timed out"))
    monkeypatch.setattr(telegram, "http_post", wire)
    with pytest.raises(RuntimeError) as e:
        telegram.Bot("T", "1", {7}).send_message("hi")
    assert len(wire.calls) == 1 and slept == [] and telegram.maybe_delivered(e.value)


def test_a_dns_failure_goes_to_telegrams_known_address_with_its_own_name(monkeypatch, slept):
    wire = Wire(httpx.ConnectError(DNS))
    monkeypatch.setattr(telegram, "http_post", wire)
    telegram.Bot("T", "1", {7}).send_message("hi")
    first, second = wire.calls
    assert first["url"] == "https://api.telegram.org/botT/sendMessage"
    assert second["url"] == f"https://{telegram.SEED_ADDRESSES[0]}/botT/sendMessage"
    assert second["headers"] == {"Host": "api.telegram.org"}
    assert second["extensions"] == {"sni_hostname": "api.telegram.org"}   # the certificate is checked for this name
    wire = Wire()
    monkeypatch.setattr(telegram, "http_post", wire)
    telegram.Bot("T", "1", {7}).send_message("again")                      # within the minute: no DNS look first
    assert wire.calls[0]["url"].startswith("https://149.154.")


def test_a_dns_failure_is_recognised_however_it_is_worded():
    assert telegram.dns_failed(httpx.ConnectError(DNS))
    assert telegram.dns_failed(httpx.ConnectError("[Errno -3] Temporary failure in name resolution"))
    wrapped = httpx.ConnectError("connect failed")
    wrapped.__cause__ = socket.gaierror(8, "nodename nor servname provided")
    assert telegram.dns_failed(wrapped)
    assert not telegram.dns_failed(httpx.ConnectError("[Errno 54] Connection reset by peer"))


def test_tcp_keepalive_is_on_and_set_where_the_os_takes_it():
    opts = telegram.keepalive_options()
    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in opts
    if getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None):
        assert any(level == socket.IPPROTO_TCP and value == 10 for level, _, value in opts)


@pytest.mark.parametrize("text,kind", [
    ("telegram getUpdates: ConnectError: " + DNS, "dns"),
    ("telegram getUpdates: ConnectError: [Errno 51] Network is unreachable", "offline"),
    ("telegram getUpdates: ConnectError: [Errno 54] Connection reset by peer", "reset"),
    ("telegram getUpdates: ReadTimeout: The read operation timed out", "timeout"),
    ("telegram getUpdates: ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", "tls"),
    ("telegram getUpdates failed: Bad Gateway", "http"),
])
def test_each_failure_has_its_kind(text, kind):
    assert telegram.failure_kind(RuntimeError(text)) == kind


# ---------------------------------------------------------------- held lines

@pytest.fixture
def chat(monkeypatch, tmp_path, settings_override):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100group")
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "5550001")
    settings_override(telegram={"enabled": True})
    monkeypatch.setattr(notify, "_enabled", REAL_ENABLED)
    monkeypatch.setattr(notify, "_ops_enabled", REAL_OPS_ENABLED)
    db = DB(tmp_path / "state.db")
    monkeypatch.setattr(notify, "_db", lambda: db)
    return db


def test_a_line_that_never_left_is_held_and_sent_once_telegram_answers(chat, monkeypatch, slept):
    db = chat
    monkeypatch.setattr(telegram, "http_post", Wire(*[httpx.ConnectError(DNS)] * 4))
    assert notify.ops("Check — FRAME Le High Straight Jeans: brand set to 'Frame Denim'") is False
    monkeypatch.setattr(telegram, "http_post", Wire(*[httpx.ConnectError("[Errno 54] Connection reset by peer")] * 4))
    notify.group("🔋 Mac battery low — plug in or I'll pause; nothing will be lost")
    assert notify.held(db) == 2
    wire = Wire()
    monkeypatch.setattr(telegram, "http_post", wire)
    assert notify.flush_held(db) == 2 and notify.held(db) == 0
    texts = [(c["data"]["chat_id"], c["data"]["text"]) for c in wire.calls]
    assert texts[0][0] == "5550001" and texts[0][1].startswith("Check — FRAME")
    assert texts[1][0] == "-100group" and texts[1][1].startswith("🔋 Mac battery low")
    assert all("(held back since " in t and "Telegram couldn't be reached)" in t for _, t in texts)
    assert notify.flush_held(db) == 0                                      # each went once


def test_a_line_that_may_have_arrived_is_never_held(chat, monkeypatch, slept):
    monkeypatch.setattr(telegram, "http_post", Wire(httpx.ReadTimeout("The read operation timed out")))
    assert notify.ops("Back online — 2 new shares") is True                # it went out: counted as said
    assert notify.held(chat) == 0


def test_held_lines_wait_while_telegram_is_still_away_and_old_ones_go(chat, monkeypatch, slept):
    db = chat
    monkeypatch.setattr(telegram, "http_post", Wire(*[httpx.ConnectError(DNS)] * 8))
    notify.ops("one")
    notify.ops("two")
    monkeypatch.setattr(telegram, "http_post", Wire(*[httpx.ConnectError(DNS)] * 4))
    assert notify.flush_held(db) == 0 and notify.held(db) == 2            # still away: both wait, in order
    key = db.conn.execute("SELECT key FROM kv WHERE key LIKE 'telegram_held:%' ORDER BY key").fetchone()[0]
    db.kv_set(key, '{"chat": "ops", "text": "one", "at": "2026-01-01T00:00:00+00:00"}')
    wire = Wire()
    monkeypatch.setattr(telegram, "http_post", wire)
    assert notify.flush_held(db) == 1 and [c["data"]["text"].split("\n")[0] for c in wire.calls] == ["two"]
    assert db.conn.execute("SELECT count(*) FROM events WHERE kind='telegram_held_dropped'").fetchone()[0] == 1


def test_a_refused_line_is_not_held(chat, monkeypatch, slept):
    def refused(url, **kw):
        raise httpx.HTTPStatusError("403 Forbidden: bot can't initiate conversation", request=None, response=None)
    monkeypatch.setattr(telegram, "http_post", refused)
    assert notify.ops("hello") is False and notify.held(chat) == 0         # not a network matter: as before


# ---------------------------------------------------------------- the poll

def test_the_poll_backs_off_and_logs_one_event_per_outage(tmp_path, monkeypatch):
    db = DB(tmp_path / "state.db")
    answers = [RuntimeError("telegram getUpdates: ConnectError: " + DNS)] * 2 + \
              [RuntimeError("telegram getUpdates: ReadTimeout: The read operation timed out"), 2]
    asked, pauses, flushed = [], [], []

    def poll_once(s, db_, bot, timeout):
        asked.append(timeout)
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(approve, "poll_once", poll_once)
    monkeypatch.setattr(cli.time, "sleep", pauses.append)
    monkeypatch.setattr(notify, "flush_held", lambda db_=None: flushed.append(1) or 0)
    for _ in range(4):
        cli._safe_poll(None, db, None, 50)
    assert asked == [25] * 4                                               # a long poll stays under 30 s
    assert pauses == [5.0, 10.0, 20.0]
    kinds = [r[0] for r in db.conn.execute("SELECT kind FROM events WHERE kind LIKE 'telegram_%' ORDER BY rowid")]
    assert kinds == ["telegram_down", "telegram_up"]
    import json
    up = json.loads(db.conn.execute("SELECT detail FROM events WHERE kind='telegram_up'").fetchone()[0])
    assert up["failed_polls"] == 3 and up["kinds"] == {"dns": 2, "timeout": 1}
    assert flushed == [1]                                                  # the held lines go once it answers
    assert db.conn.execute("SELECT count(*) FROM events WHERE kind='error'").fetchone()[0] == 0


def test_poll_backoff_tops_out_at_a_minute():
    assert [cli.poll_backoff(n) for n in range(1, 8)] == [5, 10, 20, 40, 60, 60, 60]


# ---------------------------------------------------------------- the ✅ card, the ⏭ line

def _posted_item(tmp_path):
    from test_crosslist_flow import _item, _settings
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    db.upsert_listing(iid, "poshmark", status="posted", url="https://poshmark.com/listing/x-" + "a" * 24)
    return s, db, iid


def test_a_card_that_never_left_is_sent_by_the_next_sweep_and_one_that_may_have_arrived_never_twice(tmp_path,
                                                                                                    monkeypatch):
    s, db, iid = _posted_item(tmp_path)
    monkeypatch.setattr(crosslist, "settled", lambda *a: True)
    monkeypatch.setattr(crosslist, "card_text", lambda *a, **k: "✅ Zara Mini Skirt · $20")
    errors = [RuntimeError("telegram sendMessage: ConnectError: " + DNS)]
    sent = []

    class Bot:
        chat_id = "-100group"

        def send_message(self, text, parse_mode=None):
            if errors:
                raise errors.pop(0)
            sent.append(text)
            return 77
    monkeypatch.setattr(approve, "bot_for", lambda s_: Bot())
    assert crosslist.announce(s, db, iid) is None and iid in crosslist.waiting_lines(db)
    assert crosslist.announce(s, db, iid) == "✅ Zara Mini Skirt · $20" and sent == ["✅ Zara Mini Skirt · $20"]
    assert iid not in crosslist.waiting_lines(db)
    (tmp_path / "b").mkdir()
    s2, db2, iid2 = _posted_item(tmp_path / "b")
    errors[:] = [RuntimeError("telegram sendMessage: ReadTimeout: The read operation timed out")]
    assert crosslist.announce(s2, db2, iid2) == "✅ Zara Mini Skirt · $20"          # may have arrived: counted
    assert iid2 not in crosslist.waiting_lines(db2) and sent == ["✅ Zara Mini Skirt · $20"]


def test_the_same_wasnt_listed_line_is_said_once(tmp_path, monkeypatch):
    s, db, iid = _posted_item(tmp_path)
    sent = []

    class Bot:
        chat_id = "-100group"

        def send_message(self, text, *a, **k):
            sent.append(text)
            return 90 + len(sent)
    monkeypatch.setattr(approve, "bot_for", lambda s_: Bot())
    runner.ask_skipped(db, iid, "poshmark", "Bath & Body Works Plush Robe", "no size on Poshmark's menu")
    runner.ask_skipped(db, iid, "poshmark", "Bath & Body Works Plush Robe", "no size on Poshmark's menu")
    assert len(sent) == 1                                                  # live 10-24: the robe's line came twice
    runner.ask_skipped(db, iid, "poshmark", "Bath & Body Works Plush Robe", "another reason")
    assert len(sent) == 2
