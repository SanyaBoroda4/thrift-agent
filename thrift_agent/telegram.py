"""Thin Telegram Bot API client (httpx).

`call()` is the single network seam: every other method builds parameters and returns what the API returned.
Parameters are plain Python values — a dict/list (reply_markup, allowed_updates) is JSON-encoded on the wire and a
Path is uploaded as a multipart file — so tests subclass Bot, override call() and see the values, not the encoding."""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

API = "https://api.telegram.org"
TIMEOUT = 20                # seconds; getUpdates adds its own long-poll timeout on top
UPLOAD_TIMEOUT = 90         # a photo goes up on a slow network (WO37: 20 s timed out three times on the hotel Wi-Fi)
UPLOADS = ("sendPhoto", "editMessageMedia")
# WO37: a request that went out but whose answer never came — Telegram may well have delivered it (live 2026-10-12:
# three timed-out sendPhoto calls, three copies of one question). Never sent again; a connection that never opened is.
# The request went out whole and the answer never came: Telegram may have acted on it (live 2026-10-12: a sendPhoto that
# timed out three times was delivered three times). A write that failed or timed out never reached it whole: not sent.
MAYBE_SENT = ("ReadTimeout", "RemoteProtocolError", "ReadError")


def maybe_delivered(error: Exception) -> bool:
    """Did this failed call perhaps reach Telegram (sent, no answer)? Then it must never be sent again."""
    m = re.match(r"telegram \w+: (\w+):", str(error))
    return bool(m) and m.group(1) in MAYBE_SENT

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
        timeout = (int(params.get("timeout") or 0) + 10 if method == "getUpdates"
                   else UPLOAD_TIMEOUT if method in UPLOADS else TIMEOUT)
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

    def send_message(self, text: str, buttons: list[list[dict]] | None = None, reply_to: int | None = None,
                     parse_mode: str | None = None) -> int:
        """Returns the sent message_id. `buttons` = rows of {"text": ..., "callback_data": ...}. `parse_mode` "HTML":
        the group's "✅ …" card with its site links (WO34). Link previews are always off."""
        res = self.call("sendMessage", chat_id=self.chat_id, text=text[:MAX_TEXT],
                        reply_markup={"inline_keyboard": buttons} if buttons else None,
                        reply_to_message_id=reply_to, allow_sending_without_reply=True if reply_to else None,
                        disable_web_page_preview=True, parse_mode=parse_mode)
        return int(res["message_id"])

    def edit_message(self, message_id: int, text: str, parse_mode: str | None = None) -> None:
        """Change the text of a message the bot sent (the daily window's status message, WO28; the group's card, a
        site's link put in once it posts, WO34)."""
        self.call("editMessageText", chat_id=self.chat_id, message_id=int(message_id), text=text[:MAX_TEXT],
                  disable_web_page_preview=True, parse_mode=parse_mode)

    def set_buttons(self, message_id: int, buttons: list[list[dict]] | None) -> None:
        """Replace a message's buttons — an answered card gets one "✓ …" button (WO29); None removes them."""
        self.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=int(message_id),
                  reply_markup={"inline_keyboard": buttons or []})

    def delete_message(self, message_id: int) -> None:
        """Remove a message the bot sent (Telegram allows it for 48 h in a group)."""
        self.call("deleteMessage", chat_id=self.chat_id, message_id=int(message_id))

    def send_photo(self, path: Path, caption: str, buttons: list[list[dict]] | None = None) -> int:
        res = self.call("sendPhoto", chat_id=self.chat_id, photo=Path(path), caption=caption[:MAX_CAPTION],
                        reply_markup={"inline_keyboard": buttons} if buttons else None)
        return int(res["message_id"])

    def edit_card(self, message_id: int, caption: str, buttons: list[list[dict]] | None = None,
                  photo: Path | None = None) -> None:
        """WO37: a card changed (a correction, a new cover) is EDITED in place — never sent twice: its photo (with
        `photo`), its caption and its buttons; a text card's text. "message is not modified" is no error."""
        markup = {"inline_keyboard": buttons or []}
        try:
            if photo is not None:
                self.call("editMessageMedia", chat_id=self.chat_id, message_id=int(message_id),
                          media={"type": "photo", "media": "attach://photo", "caption": caption[:MAX_CAPTION]},
                          photo=Path(photo), reply_markup=markup)
                return
            try:
                self.call("editMessageCaption", chat_id=self.chat_id, message_id=int(message_id),
                          caption=caption[:MAX_CAPTION], reply_markup=markup)
            except RuntimeError as e:
                if "no caption" not in str(e).lower() and "there is no" not in str(e).lower():
                    raise
                self.call("editMessageText", chat_id=self.chat_id, message_id=int(message_id), text=caption[:MAX_TEXT],
                          reply_markup=markup, disable_web_page_preview=True)
        except RuntimeError as e:
            if "not modified" not in str(e):
                raise

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
