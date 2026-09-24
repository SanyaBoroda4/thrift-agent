"""Telegram pings. No-op (prints) when disabled, so dev on Windows needs no bot.

Never raises: by the time we ping, the DB row is already written, so a Telegram outage must not fail a batch
(via _guard) or crash the poster loop. The worst case is a missed message, which goes to stderr instead."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TextIO

import httpx

from thrift_agent.config import Settings, settings


def _print(text: str, file: TextIO | None = None) -> None:
    """print() that survives a cp1252 console (Git Bash, `thrift run > log.txt`, PyCharm without PYTHONIOENCODING).

    Every ping carries an emoji or an arrow. A UnicodeEncodeError here would surface *after* the DB row was written,
    so _guard would flip a healthy batch to 'failed' and then crash again on its own notify.say."""
    out = file or sys.stdout                      # resolved at call time: pytest's capsys swaps sys.stdout
    try:
        print(text, file=out)
    except UnicodeEncodeError:
        enc = getattr(out, "encoding", None) or "ascii"
        try:
            safe = text.encode(enc, "replace").decode(enc)
        except (LookupError, UnicodeError):
            safe = text.encode("ascii", "replace").decode("ascii")
        print(safe, file=out)


def _enabled() -> tuple[bool, str, str]:
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    return bool(settings().get("telegram.enabled") and tok and chat), tok, chat


def check(s: Settings) -> None:
    """Fail fast at prod startup. With telegram.enabled and a blank .env every ping silently degrades to a print
    in the launchd log, and the seller never hears that a batch needs an answer."""
    if s.get("telegram.enabled") and (not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID")):
        raise RuntimeError("telegram.enabled is true but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set in .env "
                           "— the seller would never get a ping")


def _send(tok: str, method: str, **kw) -> None:
    try:
        httpx.post(f"https://api.telegram.org/bot{tok}/{method}", **kw).raise_for_status()
    except Exception as e:  # noqa: BLE001 - notifications are best-effort
        _print(f"[notify] {method} failed: {type(e).__name__}: {e}", file=sys.stderr)


def say(text: str) -> None:
    ok, tok, chat = _enabled()
    if not ok:
        _print(f"[notify] {text}")
        return
    _send(tok, "sendMessage", timeout=20,
          data={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True})


def photo(path: Path, caption: str) -> None:
    ok, tok, chat = _enabled()
    if not ok:
        _print(f"[notify] {caption}  ({path})")
        return
    if not Path(path).is_file():                       # e.g. the screenshot itself failed
        say(f"{caption}\n(no image: {path})")
        return
    try:
        with open(path, "rb") as f:
            _send(tok, "sendPhoto", timeout=60, data={"chat_id": chat, "caption": caption[:1000]}, files={"photo": f})
    except OSError as e:
        say(f"{caption}\n(could not read image {path}: {e})")
