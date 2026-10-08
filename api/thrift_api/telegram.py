"""Telegram, send-only (WO33). The API only ever calls sendMessage: it never reads the bot's updates and never sets a
webhook, because the Mac's worker is the bot's one reader (long polling) and a second reader would take its updates
(tests/api/test_no_polling.py keeps it that way).

`send(text, chat)` — chat "group" (TELEGRAM_GROUP_CHAT_ID: the owner's group) or "ops" (TELEGRAM_OPS_CHAT_ID: the
developer's private chat), with TELEGRAM_BOT_TOKEN. In the quiet hours (23:00-08:00 America/New_York,
deadlines.quiet) a message goes out silently (`disable_notification`), never later. SALES_MODE replay: every message
goes to the ops chat as "[replay] …"; off: nothing is sent. Plain text, no parse mode, so a title needs no escaping.

`SENDER(chat, text, silent) -> bool` does the HTTP call (urllib, 10 s); the tests replace it. A failed send is logged
(without the token) and dropped: a message is never retried or sent twice."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from urllib import error as urlerror
from urllib import request as urlrequest

from . import deadlines, util

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"
CHAT_ENV = {"group": "TELEGRAM_GROUP_CHAT_ID", "ops": "TELEGRAM_OPS_CHAT_ID"}
TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
REPLAY_PREFIX = "[replay] "
LIMIT = 4096                     # Telegram's longest message
TIMEOUT = 10


def http_send(chat: str, text: str, silent: bool) -> bool:
    """POSTs one message to Telegram's sendMessage; True when Telegram answered ok."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    chat_id = os.environ.get(CHAT_ENV[chat], "").strip()
    if not token or not chat_id:
        log.warning("telegram: %s or %s is not set; message to the %s chat dropped", TOKEN_ENV, CHAT_ENV[chat], chat)
        return False
    payload = {"chat_id": chat_id, "text": text, "disable_notification": silent,
               "link_preview_options": {"is_disabled": True}}
    request = urlrequest.Request(API.format(token=token), data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urlrequest.urlopen(request, timeout=TIMEOUT) as response:
            answer = json.loads(response.read().decode("utf-8") or "{}")
    except urlerror.HTTPError as e:
        log.warning("telegram: the %s chat refused the message: HTTP %s %s", chat, e.code, _description(e, token))
        return False
    except (urlerror.URLError, OSError, ValueError) as e:
        log.warning("telegram: sending to the %s chat failed: %s", chat, str(e).replace(token, "<token>"))
        return False
    if not answer.get("ok"):
        log.warning("telegram: the %s chat refused the message: %s", chat, str(answer.get("description", ""))[:200])
        return False
    return True


SENDER = http_send


def send(text: str, chat: str = "group", now: datetime | None = None, mode: str | None = None) -> bool:
    """Sends `text` to the group or the ops chat as the mode says (see the module); True when it went out."""
    if chat not in CHAT_ENV:
        raise ValueError(f"chat is group or ops, not {chat!r}")
    mode = mode or util.mode()
    if mode == "off":
        return False
    if mode == "replay":
        chat, text = "ops", REPLAY_PREFIX + text
    if len(text) > LIMIT:
        text = text[:LIMIT - 1] + "…"
    silent = deadlines.quiet(now or util.utcnow())
    try:
        return bool(SENDER(chat, text, silent))
    except Exception:  # noqa: BLE001 — a message never takes a request or a timer down with it
        log.exception("telegram: the sender failed")
        return False


def _description(error: urlerror.HTTPError, token: str) -> str:
    try:
        body = json.loads(error.read().decode("utf-8") or "{}")
        return str(body.get("description", ""))[:200].replace(token, "<token>")
    except (OSError, ValueError, AttributeError):
        return ""
