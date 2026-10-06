"""notify: the group gets only what is sent to it on purpose (group()); everything else goes to the ops chat (WO29),
never falls back to the group, is dropped when the ops chat can't be reached, and goes at most once a day."""
import io
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from thrift_agent import notify
from thrift_agent.config import Settings
from thrift_agent.db import DB

REAL_ENABLED, REAL_OPS_ENABLED = notify._enabled, notify._ops_enabled   # before conftest switches them off


@pytest.fixture
def wire(monkeypatch, tmp_path, settings_override):
    """Telegram on, with a group and an ops chat; every POST recorded as (method, chat_id, text) instead of sent."""
    sent = []

    class Resp:
        def raise_for_status(self):
            return None

    def post(url, data=None, files=None, timeout=None):
        sent.append((url.rsplit("/", 1)[-1], (data or {}).get("chat_id"), (data or {}).get("text")
                     or (data or {}).get("caption")))
        return Resp()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100group")
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "5550001")
    settings_override(telegram={"enabled": True})
    monkeypatch.setattr(notify, "_enabled", REAL_ENABLED)
    monkeypatch.setattr(notify, "_ops_enabled", REAL_OPS_ENABLED)
    db = DB(tmp_path / "state.db")
    monkeypatch.setattr(notify, "_db", lambda: db)
    monkeypatch.setattr(notify.httpx, "post", post)
    return sent, db


def test_say_goes_to_the_ops_chat_and_only_group_reaches_the_group(wire):
    sent, _ = wire
    notify.say("❌ worker tick: OSError: [Errno 11] Resource deadlock avoided")
    notify.group("Posted ✓ Zara Floral Mini Skirt size M — $35 · https://poshmark.com/listing/x")
    assert sent == [("sendMessage", "5550001", "❌ worker tick: OSError: [Errno 11] Resource deadlock avoided"),
                    ("sendMessage", "-100group", "Posted ✓ Zara Floral Mini Skirt size M — $35 · "
                                                 "https://poshmark.com/listing/x")]


def test_without_an_ops_chat_ops_messages_are_dropped_never_sent_to_the_group(wire, monkeypatch, capsys):
    sent, _ = wire
    monkeypatch.delenv("TELEGRAM_OPS_CHAT_ID")
    notify.say("Back online — 1 item waiting")
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "-100group")             # the group's own id is no ops chat
    notify.say("Poster started (LIVE) — poshmark · 18:43")
    assert sent == [] and capsys.readouterr().out.count("[ops]") == 2


def test_the_ops_chat_id_may_come_from_the_private_settings(wire, monkeypatch, settings_override):
    sent, _ = wire
    monkeypatch.delenv("TELEGRAM_OPS_CHAT_ID")
    settings_override(telegram={"enabled": True, "ops_chat_id": 5550002})
    notify.say("hello")
    assert sent == [("sendMessage", "5550002", "hello")]


def test_an_ops_chat_that_fails_is_left_alone_for_a_while_and_never_falls_back(wire, monkeypatch, capsys):
    sent, db = wire

    def refused(url, data=None, files=None, timeout=None):
        sent.append(("refused", data.get("chat_id"), data.get("text")))
        raise httpx.HTTPStatusError("403 Forbidden: bot can't initiate conversation with a user", request=None,
                                    response=None)

    monkeypatch.setattr(notify.httpx, "post", refused)
    assert notify.ops("first") is False                                  # not /started yet: tried, refused
    assert notify.ops("second") is False                                 # ... then left alone for DOWN_FOR
    assert sent == [("refused", "5550001", "first")]                     # never the group
    assert "unreachable, dropped" in capsys.readouterr().err
    db.kv_set(notify.OPS_DOWN, (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    notify.ops("third")
    assert sent[-1] == ("refused", "5550001", "third")                   # tried again after the pause


def test_identical_ops_messages_go_once_a_day(wire):
    sent, db = wire
    notify.say("❌ i_261005_aaaaaa: RuntimeError: invalid x-api-key")
    notify.say("❌ i_261005_bbbbbb: RuntimeError: invalid x-api-key")      # the same error on another item
    notify.say("Back online — 1 item waiting")
    assert [t for _, _, t in sent] == ["❌ i_261005_aaaaaa: RuntimeError: invalid x-api-key",
                                       "Back online — 1 item waiting"]
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1, minutes=1)).isoformat(timespec="seconds")
    db.kv_set(notify.OPS_SENT, '{"' + notify.signature("❌ i_x: RuntimeError: invalid x-api-key").replace(
        "i_x", "<id>") + f'": "{yesterday}"}}')
    notify.say("❌ i_261005_cccccc: RuntimeError: invalid x-api-key")
    assert sent[-1][2] == "❌ i_261005_cccccc: RuntimeError: invalid x-api-key"   # a day later: once more


def test_notify_never_raises(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(notify, "_enabled", lambda: (True, "tok", "chat"))
    monkeypatch.setattr(notify, "_ops_enabled", lambda: (True, "tok", "ops"))

    def boom(*a, **k):
        raise httpx.ConnectError("no network")
    monkeypatch.setattr(notify.httpx, "post", boom)
    notify.group("hello")                                # Telegram down: logged, not raised
    notify.say("hello")
    notify.photo(tmp_path / "missing.png", "caption")    # screenshot never written: text fallback, not raised
    img = tmp_path / "shot.png"
    img.write_bytes(b"png")
    notify.photo(img, "caption")
    notify.group_photo(img, "caption")
    assert capsys.readouterr().err.count("failed") == 5


def test_notify_prints_when_disabled(monkeypatch, capsys):
    notify.group("hi")
    notify.say("there")
    out = capsys.readouterr().out
    assert "[notify] hi" in out and "[ops] there" in out


def test_conftest_blocks_telegram_by_default(monkeypatch, capsys):
    # No patching here: the autouse fixture alone must keep a "live" machine from posting.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    notify.group("would be a real ping")
    notify.say("would be a real ops ping")
    out = capsys.readouterr().out
    assert "[notify] would be a real ping" in out and "[ops] would be a real ops ping" in out
    with pytest.raises(AssertionError, match="network call in tests"):
        notify.httpx.post("https://api.telegram.org/botx/sendMessage")


def _cp1252_stream() -> tuple[io.BytesIO, io.TextIOWrapper]:
    buf = io.BytesIO()
    return buf, io.TextIOWrapper(buf, encoding="cp1252", errors="strict", write_through=True)


def test_disabled_path_survives_cp1252_stdout(monkeypatch, tmp_path):
    """Git Bash / `thrift run > log.txt` / PyCharm without PYTHONIOENCODING: stdout is cp1252 and every ping has an
    emoji. The DB row is already written by then, so this must degrade, never raise."""
    buf, out = _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", out)
    notify.group("❌ ⚠️ →")
    notify.photo(tmp_path / "sheet.jpg", "Batch → 3 items")
    out.flush()
    written = buf.getvalue().decode("cp1252")
    assert written.count("[notify]") == 1 and written.count("[ops]") == 1
    assert "?" in written and "3 items" in written       # unencodable glyphs replaced, the rest intact


def test_failure_log_survives_cp1252_stderr(monkeypatch):
    buf, err = _cp1252_stream()
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(notify, "_enabled", lambda: (True, "tok", "chat"))

    def boom(*a, **k):
        raise httpx.ConnectError("no network → ⚠️")
    monkeypatch.setattr(notify.httpx, "post", boom)
    notify.group("hello")
    err.flush()
    assert "sendMessage failed" in buf.getvalue().decode("cp1252")


def test_check_raises_when_enabled_without_env(monkeypatch):
    s = Settings({"telegram": {"enabled": True}})
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / TELEGRAM_ALLOWED_USER_IDS"):
        notify.check(s)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")      # still blind without the chat id
    with pytest.raises(RuntimeError, match="TELEGRAM_CHAT_ID"):
        notify.check(s)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")       # pings would arrive, but nobody could approve
    with pytest.raises(RuntimeError, match="TELEGRAM_ALLOWED_USER_IDS"):
        notify.check(s)


def test_check_passes_when_disabled_or_fully_configured(monkeypatch):
    notify.check(Settings({"telegram": {"enabled": False}}))     # dev: no bot needed
    notify.check(Settings({}))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "555")
    notify.check(Settings({"telegram": {"enabled": True}}))
