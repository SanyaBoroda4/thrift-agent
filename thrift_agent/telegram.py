"""Thin Telegram Bot API client (httpx).

`call()` is the single network seam: every other method builds parameters and returns what the API returned.
Parameters are plain Python values — a dict/list (reply_markup, allowed_updates) is JSON-encoded on the wire and a
Path is uploaded as a multipart file — so tests subclass Bot, override call() and see the values, not the encoding."""
from __future__ import annotations

import json
import re
import socket
import threading
import time
from pathlib import Path

import httpx

API = "https://api.telegram.org"
HOST = "api.telegram.org"
TIMEOUT = 20                # seconds; getUpdates adds its own long-poll timeout on top
CONNECT_TIMEOUT = 10        # a connection that can't open in 10 s won't: tried again (below), never waited out for 20

# ---- the link (Alex, 2026-10-27: "the Mac's Telegram link keeps dropping"). Measured on the Mac over 14 days: 4,868
# failed polls — DNS 3,144 (its first resolver is Tailscale's MagicDNS, 100.100.100.100: answers stop around wakes and
# network changes), no network 457 (dark wakes), read timeouts 336 (a poll in flight when the Mac sleeps), connection
# resets 204 (the path to Telegram's Amsterdam servers: in a live probe api.telegram.org was reset 1 time in 60 while
# Apple and Anthropic were never), a captive portal's self-signed certificate 707 (one day of travel, 10-18). So:
# ONE client per process whose connections live 15 s (a poll every ~15 s reuses the open TLS connection — no DNS look
# per call) with TCP keepalive (a dead peer noticed in ~25 s, not at the read timeout); a request that never left the
# Mac (no connection, no DNS, a reset in the handshake) is tried again 1, 2, 4 s later; on a DNS failure the last
# addresses api.telegram.org had are used (the TLS name check stays api.telegram.org); a request that went out and got
# no answer is NEVER sent again (WO37: it may have been delivered).
RETRY_PAUSES = (1.0, 2.0, 4.0)
KEEPALIVE_EXPIRY = 15.0     # an idle connection older than this is not reused (after a sleep it is dead)
LEARN_EVERY = 600           # the addresses refreshed from DNS at most every 10 min, after a call that worked
DNS_DOWN_FOR = 60           # after a DNS failure, the next minute's calls go to the known addresses at once
SEED_ADDRESSES = ("149.154.166.110", "149.154.167.220")   # api.telegram.org (read from the Mac, 2026-10-27) / its peer
NOT_SENT = ("ConnectError", "ConnectTimeout", "PoolTimeout", "WriteError", "WriteTimeout")
_DNS_ERR = re.compile(r"nodename nor servname|Name or service not known|Temporary failure in name resolution|"
                      r"getaddrinfo failed|No address associated|\[Errno (?:8|-2|-3|-5|11001|11002)\]")
_sleep = time.sleep         # the tests' seam
_LOCK = threading.Lock()
_CLIENT: httpx.Client | None = None
_ADDRESSES: list[str] = list(SEED_ADDRESSES)
_STATE = {"learned": 0.0, "dns_down_until": 0.0, "next": 0}


def keepalive_options() -> list[tuple[int, int, int]]:
    """SO_KEEPALIVE, first probe after 10 s idle, then every 5 s, 3 misses = dead — each option only where this OS
    takes it (macOS names the idle time TCP_KEEPALIVE, Linux and Windows TCP_KEEPIDLE)."""
    want = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
    for name, value in ((idle, 10), (getattr(socket, "TCP_KEEPINTVL", None), 5),
                        (getattr(socket, "TCP_KEEPCNT", None), 3)):
        if name is not None:
            want.append((socket.IPPROTO_TCP, name, value))
    ok, probe = [], socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        for level, name, value in want:
            try:
                probe.setsockopt(level, name, value)
                ok.append((level, name, value))
            except OSError:
                pass
    finally:
        probe.close()
    return ok


def client() -> httpx.Client:
    """The process's one connection pool to Telegram (thread-safe; the worker's two threads share it)."""
    global _CLIENT
    with _LOCK:
        if _CLIENT is None:
            _CLIENT = httpx.Client(
                transport=httpx.HTTPTransport(socket_options=keepalive_options()),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4, keepalive_expiry=KEEPALIVE_EXPIRY))
        return _CLIENT


def http_post(url: str, data=None, files=None, timeout: float = TIMEOUT, **extra) -> httpx.Response:
    """The single network seam (the tests replace it): one POST on the shared client."""
    return client().post(url, data=data, files=files, timeout=httpx.Timeout(timeout, connect=min(timeout, CONNECT_TIMEOUT)),
                         **extra)


def dns_failed(error: BaseException) -> bool:
    x, depth = error, 0
    while x is not None and depth < 8:
        if isinstance(x, socket.gaierror):
            return True
        x, depth = x.__cause__ or x.__context__, depth + 1
    return bool(_DNS_ERR.search(str(error)))


def learn_addresses(now: float | None = None) -> None:
    """api.telegram.org's addresses as DNS gives them now (after a call that worked), for the next DNS outage."""
    now = time.monotonic() if now is None else now
    if now - _STATE["learned"] < LEARN_EVERY:
        return
    _STATE["learned"] = now
    try:
        found = [ai[4][0] for ai in socket.getaddrinfo(HOST, 443, socket.AF_INET, socket.SOCK_STREAM)]
    except OSError:
        return
    if found:
        _ADDRESSES[:] = list(dict.fromkeys(found + _ADDRESSES))[:6]


def _pinned() -> str:
    addr = _ADDRESSES[_STATE["next"] % len(_ADDRESSES)]
    _STATE["next"] += 1
    return addr


def post(token: str, method: str, data=None, files=None, timeout: float = TIMEOUT) -> httpx.Response:
    """The one way to Telegram (the Bot and notify): retried 1, 2, 4 s later while the request never left the Mac;
    by a known address while DNS fails. Raises the last httpx error; a request that may have gone out (no answer) is
    raised at once — never sent twice."""
    pinned = _pinned() if time.monotonic() < _STATE["dns_down_until"] else None
    for i, pause in enumerate((0.0, *RETRY_PAUSES)):
        if pause:
            _sleep(pause)
        try:
            if pinned:
                r = http_post(f"https://{pinned}/bot{token}/{method}", data=data, files=files, timeout=timeout,
                              headers={"Host": HOST}, extensions={"sni_hostname": HOST})
            else:
                r = http_post(f"{API}/bot{token}/{method}", data=data, files=files, timeout=timeout)
        except httpx.HTTPError as e:
            if type(e).__name__ not in NOT_SENT or i == len(RETRY_PAUSES):
                raise
            if dns_failed(e):
                _STATE["dns_down_until"] = time.monotonic() + DNS_DOWN_FOR
                pinned = _pinned()
            elif pinned:
                pinned = _pinned()                     # that address didn't answer: the next one
            continue
        if not pinned:
            learn_addresses()
        return r
    raise RuntimeError("unreachable")                  # the loop returns or raises


def failure_kind(error: BaseException | str) -> str:
    """A failed call in one word, for the link's events: dns, offline, timeout, reset, tls, http, other."""
    t = str(error)
    if dns_failed(error) if isinstance(error, BaseException) else _DNS_ERR.search(t):
        return "dns"
    for kind, words in (("offline", ("Network is unreachable", "Errno 51", "No route to host", "Errno 65")),
                        ("tls", ("CERTIFICATE_VERIFY_FAILED", "SSL")),
                        ("reset", ("Errno 54", "Connection reset", "RemoteProtocolError", "Server disconnected")),
                        ("timeout", ("Timeout", "timed out")),
                        ("http", ("failed: ", "Bad Gateway", "HTTP "))):
        if any(w in t for w in words):
            return kind
    return "other"
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
            r = post(self.token, method, data=data, files=files or None, timeout=timeout)
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
