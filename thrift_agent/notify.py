"""Telegram pings, to two places (WO29: a quiet group).

- The GROUP (TELEGRAM_CHAT_ID), where the owner answers: only the cards (sent by approve.py), "Posted ✓ …", and a few
  plain action-needed lines (battery low, the Mac slept while publishing, the iCloud inbox can't be read, ⏸, ⏭) —
  `group()` / `group_photo()`, called on purpose for those and nothing else.
- The OPS chat (TELEGRAM_OPS_CHAT_ID in .env, else telegram.ops_chat_id — the owner's private chat with the bot):
  everything else. `say()` / `photo()` go there, so a message nobody classified can never land in the group. It never
  falls back to the group: without an ops chat, or when sending there fails (the owner hasn't sent the bot /start),
  the message is logged and dropped. An identical message (item and batch ids aside) goes at most once a day.

No-op (prints) when disabled, so dev on Windows needs no bot. Never raises: by the time we ping, the DB row is
already written, so a Telegram outage must not fail a batch (via _guard) or crash the poster loop. The worst case is a
missed message, which goes to stderr instead."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TextIO

import httpx

from thrift_agent.config import Settings, settings

OPS_SENT = "ops_sent"            # kv: {message signature: when it last went to the ops chat}
OPS_DOWN = "ops_down_until"      # kv: after a failed send, the ops chat is left alone until then
REPEAT_EVERY = timedelta(days=1)
DOWN_FOR = timedelta(minutes=10)
_IDS = re.compile(r"\b[ib]_\d{6}_[0-9a-f]{6}\b")


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
    """(on, token, the group's chat id)."""
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    return bool(settings().get("telegram.enabled") and tok and chat), tok, chat


def ops_chat(s: Settings | None = None) -> str | None:
    """The ops chat's id: TELEGRAM_OPS_CHAT_ID, else telegram.ops_chat_id (private settings); None when there is none
    — or when it is the group's own id: the group never gets ops messages."""
    s = s or settings()
    chat = str(os.getenv("TELEGRAM_OPS_CHAT_ID") or s.get("telegram.ops_chat_id") or "").strip()
    if not chat or chat == os.getenv("TELEGRAM_CHAT_ID", "").strip():
        return None
    return chat


def _ops_enabled() -> tuple[bool, str, str | None]:
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), ops_chat()
    return bool(settings().get("telegram.enabled") and tok and chat), tok, chat


def check(s: Settings) -> None:
    """Fail fast at prod startup. With telegram.enabled and a blank .env every ping silently degrades to a print
    in the launchd log, and the seller never hears that a batch needs an answer."""
    if not s.get("telegram.enabled"):
        return
    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALLOWED_USER_IDS") if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"telegram.enabled is true but {' / '.join(missing)} not set in .env — the seller would "
                           "never get a ping, or nobody could approve (run `thrift telegram setup` for the ids)")


def _send(tok: str, method: str, **kw) -> bool:
    try:
        httpx.post(f"https://api.telegram.org/bot{tok}/{method}", **kw).raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001 - notifications are best-effort
        _print(f"[notify] {method} failed: {type(e).__name__}: {e}", file=sys.stderr)
        return False


# ---------- the group: on purpose only ----------

def group(text: str) -> None:
    """A message for the group (WO29): "Posted ✓ …" or a plain action-needed line. Nothing else."""
    ok, tok, chat = _enabled()
    if not ok:
        _print(f"[notify] {text}")
        return
    _send(tok, "sendMessage", timeout=20,
          data={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True})


def group_photo(path: Path, caption: str) -> None:
    ok, tok, chat = _enabled()
    if not ok:
        _print(f"[notify] {caption}  ({path})")
        return
    if not Path(path).is_file():
        group(caption)
        return
    try:
        with open(path, "rb") as f:
            _send(tok, "sendPhoto", timeout=60, data={"chat_id": chat, "caption": caption[:1000]}, files={"photo": f})
    except OSError:
        group(caption)


# ---------- the ops chat: everything else ----------

def _db():
    try:
        from thrift_agent.db import DB
        return DB(settings().path("db"))
    except Exception:  # noqa: BLE001 - no DB here (a bare CLI): no once-a-day, no back-off
        return None


def signature(text: str) -> str:
    """Identical messages are identical apart from the item / batch they name."""
    return _IDS.sub("<id>", text)


def ops_down(db) -> bool:
    """The ops chat failed a little while ago (the owner hasn't sent the bot /start yet): leave it alone."""
    until = db.kv_get(OPS_DOWN) if db is not None else None
    return bool(until) and datetime.now(timezone.utc) < datetime.fromisoformat(until)


def mark_ops_down(db, why: str) -> None:
    if db is None:
        return
    db.kv_set(OPS_DOWN, (datetime.now(timezone.utc) + DOWN_FOR).isoformat(timespec="seconds"))
    db.log(None, "ops_chat_down", why)


def _repeat(db, text: str) -> bool:
    """True when the same message went to the ops chat less than a day ago; else records it now."""
    if db is None:
        return False
    sig, now = signature(text), datetime.now(timezone.utc)
    sent = json.loads(db.kv_get(OPS_SENT) or "{}")
    if (last := sent.get(sig)) and now - datetime.fromisoformat(last) < REPEAT_EVERY:
        return True
    sent = {k: v for k, v in sent.items() if now - datetime.fromisoformat(v) < timedelta(days=7)}
    sent[sig] = now.isoformat(timespec="seconds")
    db.kv_set(OPS_SENT, json.dumps(sent))
    return False


def ops(text: str, *, once: bool = True) -> bool:
    """A message for the ops chat; True when it went out. Logged and dropped when there is no ops chat or it can't be
    reached — never sent to the group."""
    ok, tok, chat = _ops_enabled()
    if not ok:
        _print(f"[ops] {text}")
        return False
    db = _db()
    if once and _repeat(db, text):
        return False
    if ops_down(db):
        _print(f"[ops] (ops chat unreachable, dropped) {text}", file=sys.stderr)
        return False
    sent = _send(tok, "sendMessage", timeout=20,
                 data={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True})
    if not sent:
        mark_ops_down(db, "sendMessage failed")
    return sent


def ops_photo(path: Path, caption: str) -> bool:
    ok, tok, chat = _ops_enabled()
    if not ok:
        _print(f"[ops] {caption}  ({path})")
        return False
    if not Path(path).is_file():                       # e.g. the screenshot itself failed
        return ops(f"{caption}\n(no image: {path})")
    db = _db()
    if _repeat(db, caption) or ops_down(db):
        return False
    try:
        with open(path, "rb") as f:
            sent = _send(tok, "sendPhoto", timeout=60, data={"chat_id": chat, "caption": caption[:1000]},
                         files={"photo": f})
    except OSError as e:
        return ops(f"{caption}\n(could not read image {path}: {e})", once=False)
    if not sent:
        mark_ops_down(db, "sendPhoto failed")
    return sent


def say(text: str) -> None:
    """The default: the ops chat (WO29). For the group, call group() on purpose."""
    ops(text)


def photo(path: Path, caption: str) -> None:
    ops_photo(path, caption)
