import io
import sys

import httpx
import pytest

from thrift_agent import notify
from thrift_agent.config import Settings


def test_notify_never_raises(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(notify, "_enabled", lambda: (True, "tok", "chat"))

    def boom(*a, **k):
        raise httpx.ConnectError("no network")
    monkeypatch.setattr(notify.httpx, "post", boom)
    notify.say("hello")                                  # Telegram down: logged, not raised
    notify.photo(tmp_path / "missing.png", "caption")    # screenshot never written: text fallback, not raised
    img = tmp_path / "shot.png"
    img.write_bytes(b"png")
    notify.photo(img, "caption")
    assert capsys.readouterr().err.count("failed") == 3


def test_notify_prints_when_disabled(monkeypatch, capsys):
    monkeypatch.setattr(notify, "_enabled", lambda: (False, "", ""))
    notify.say("hi")
    assert "[notify] hi" in capsys.readouterr().out


def test_conftest_blocks_telegram_by_default(monkeypatch, capsys):
    # No patching here: the autouse fixture alone must keep a "live" machine from posting.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    notify.say("would be a real ping")
    assert "[notify] would be a real ping" in capsys.readouterr().out
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
    monkeypatch.setattr(notify, "_enabled", lambda: (False, "", ""))
    notify.say("❌ ⚠️ →")
    notify.photo(tmp_path / "sheet.jpg", "Batch → 3 items")
    out.flush()
    written = buf.getvalue().decode("cp1252")
    assert written.count("[notify]") == 2
    assert "?" in written and "3 items" in written       # unencodable glyphs replaced, the rest intact


def test_failure_log_survives_cp1252_stderr(monkeypatch):
    buf, err = _cp1252_stream()
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(notify, "_enabled", lambda: (True, "tok", "chat"))

    def boom(*a, **k):
        raise httpx.ConnectError("no network → ⚠️")
    monkeypatch.setattr(notify.httpx, "post", boom)
    notify.say("hello")
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
