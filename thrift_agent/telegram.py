"""Thin Telegram Bot API client (httpx).

`call()` is the single network seam: every other method builds parameters and returns what the API returned.
Parameters are plain Python values — a dict/list (reply_markup, allowed_updates) is JSON-encoded on the wire and a
Path is uploaded as a multipart file — so tests subclass Bot, override call() and see the values, not the encoding."""
from __future__ import annotations

import json
from pathlib import Path

import httpx

API = "https://api.telegram.org"
TIMEOUT = 20                # seconds; getUpdates adds its own long-poll timeout on top
MAX_TEXT = 4096             # Telegram limits: message text / photo caption
MAX_CAPTION = 1024
ALLOWED_UPDATES = ["message", "callback_query"]


class Bot:
    def __init__(self, token: str, chat_id: str, allowed_users: set[int]):
        self.token, self.chat_id, self.allowed_users = token, str(chat_id), set(allowed_users)

    def call(self, method: str, **params) -> dict:
        """POST https://api.telegram.org/bot<token>/<method>; returns the 'result' object (a list for getUpdates);
        raises RuntimeError with the API's description on ok=false or a non-2xx status. None params are dropped."""
        data, files = {}, {}
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, Path):
                files[k] = (v.name, v.read_bytes())
            elif isinstance(v, (dict, list)):
                data[k] = json.dumps(v)
            else:
                data[k] = v
        timeout = int(params.get("timeout") or 0) + 10 if method == "getUpdates" else TIMEOUT
        try:
            r = httpx.post(f"{API}/bot{self.token}/{method}", data=data, files=files or None, timeout=timeout)
        except httpx.HTTPError as e:
            raise RuntimeError(f"telegram {method}: {type(e).__name__}: {e}") from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        if not (200 <= r.status_code < 300) or not isinstance(body, dict) or not body.get("ok"):
            desc = body.get("description") if isinstance(body, dict) else None
            raise RuntimeError(f"telegram {method} failed: {desc or f'HTTP {r.status_code}'}")
        return body.get("result")

    def send_message(self, text: str, buttons: list[list[dict]] | None = None, reply_to: int | None = None) -> int:
        """Returns the sent message_id. `buttons` = rows of {"text": ..., "callback_data": ...}."""
        res = self.call("sendMessage", chat_id=self.chat_id, text=text[:MAX_TEXT],
                        reply_markup={"inline_keyboard": buttons} if buttons else None,
                        reply_to_message_id=reply_to, allow_sending_without_reply=True if reply_to else None,
                        disable_web_page_preview=True)
        return int(res["message_id"])

    def send_photo(self, path: Path, caption: str, buttons: list[list[dict]] | None = None) -> int:
        res = self.call("sendPhoto", chat_id=self.chat_id, photo=Path(path), caption=caption[:MAX_CAPTION],
                        reply_markup={"inline_keyboard": buttons} if buttons else None)
        return int(res["message_id"])

    def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        res = self.call("getUpdates", offset=offset, timeout=int(timeout), allowed_updates=ALLOWED_UPDATES)
        return list(res or [])

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_id, text=text or None)

    def authorized(self, update: dict) -> bool:
        """True only for updates from self.chat_id sent by a user in self.allowed_users."""
        if not isinstance(update, dict):
            return False
        if isinstance(update.get("callback_query"), dict):
            cq = update["callback_query"]
            msg, sender = cq.get("message") or {}, cq.get("from") or {}
        elif isinstance(update.get("message"), dict):
            msg = update["message"]
            sender = msg.get("from") or {}
        else:
            return False
        chat = (msg.get("chat") or {}).get("id")
        uid = sender.get("id")
        return (chat is not None and str(chat) == self.chat_id
                and isinstance(uid, int) and not isinstance(uid, bool) and uid in self.allowed_users)
