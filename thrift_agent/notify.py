"""Telegram pings. No-op (prints) when disabled, so dev on Windows needs no bot."""
from __future__ import annotations

import os
from pathlib import Path

import httpx

from thrift_agent.config import settings


def _enabled() -> tuple[bool, str, str]:
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    return bool(settings().get("telegram.enabled") and tok and chat), tok, chat


def say(text: str) -> None:
    ok, tok, chat = _enabled()
    if not ok:
        print(f"[notify] {text}")
        return
    httpx.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=20,
               data={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True})


def photo(path: Path, caption: str) -> None:
    ok, tok, chat = _enabled()
    if not ok:
        print(f"[notify] {caption}  ({path})")
        return
    with open(path, "rb") as f:
        httpx.post(f"https://api.telegram.org/bot{tok}/sendPhoto", timeout=60,
                   data={"chat_id": chat, "caption": caption[:1000]}, files={"photo": f})
