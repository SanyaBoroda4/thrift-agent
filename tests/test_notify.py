import httpx

from thrift_agent import notify


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
