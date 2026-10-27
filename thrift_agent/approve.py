"""Owner conversation over Telegram: ONE question at a time (WO20).

Everything that waits for the owner — a batch's contact sheet, "Brand new or worn?", "Girls or Boys?", an item's price
card, the poster's question — is one queue, read from the DB (queue()): the oldest batch first, its contact sheet, then
its items in photo order (each item's questions, then its card), then the next batch; [Later] puts an item behind
everything queued so far. At most ONE message is open (sent, not answered yet); the next is sent when it is answered
(pump). Processing runs ahead on the worker's main thread, so the next card is usually ready at once; the queue never
skips an item that is still being processed. The queue is the DB, so it survives restarts, and a restart re-sends
only the open message (resend_pending).

The agent owns the bot (long polling via getUpdates, offset persisted in the kv table). Replies to the bot's messages
and button presses work in a group with BotFather privacy mode ON; a number typed WITHOUT a reply (the price of the
open card) reaches the bot only with privacy mode OFF or the bot an admin of the group. Only TELEGRAM_CHAT_ID and
senders in TELEGRAM_ALLOWED_USER_IDS are accepted; everything else is ignored.

Without a bot (dev) the messages print and are recorded under the chat "dev", so the queue works the same way there:
the CLI twins (thrift confirm / condition / kids / price / answer) answer them.

pipeline.py, cli.py and post/runner.py call pump, announce, resend_pending and the handlers below. The poster never
asks anything (WO27): it guesses and reports its guesses in its "Posted ✓" message."""
from __future__ import annotations

import contextvars
import json
import math
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from thrift_agent import notify, pipeline, sizebuttons
from thrift_agent.brain import taxonomy
from thrift_agent.brain.price import note_floor
from thrift_agent.brain.gate import SIZE_NOTE
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads
from thrift_agent.ingest import segment as seg
from thrift_agent.schema import POSH_CONDITION, Facts, Render
from thrift_agent.telegram import maybe_delivered, MAX_CAPTION, Bot

OFFSET_KEY = "telegram_offset"                       # kv: the last getUpdates update_id we handled
LOCK_KEY = "telegram_queue_lock"                     # kv: who is sending the next message ("<token>@<epoch>")
ROUND_KEY = "telegram_round"                         # kv: items queued since the queue was last empty ("of 10")
LOCK_TTL = 120                                       # seconds: a sender's lock older than this was abandoned (a crash)
DEV_CHAT = "dev"                                     # outbox chat of the dev print (no bot)
QUEUE_KINDS = ("batch", "regroup", "condition", "category", "kids", "item")   # outbox kinds that wait for an answer
SENDERS = {"batch": "send_batch", "regroup": "send_regroup", "condition": "ask_condition", "category": "ask_category",
           "kids": "ask_kids", "item": "send_item"}
NEW_BATCH, NEW_ITEM = "new_batch", "new_item"        # queue entries still being processed: the queue holds there
OWNER_WAITING = ("awaiting_condition", "awaiting_price", "needs_info")   # item waits for an answer
QUEUED_ITEMS = ("new", *OWNER_WAITING)               # item statuses in the queue ('new': still being processed)
WAITING_ITEM = ("awaiting_price", "needs_info")      # item statuses whose price card is the open question
BATCH_HINT = "Reply to this message: ok | 12>2 | split 7 | merge 2 3 | drop 7"
REGROUP_HINT = "Reply with the fix: 12>2 | split 7 | merge 2 3 | drop 7 (or ok: nothing changes)"
ITEM_HINT = "Tap a price, type a number, or reply with an answer like 'size 8, 45'"
CORRECT_HINT = "To change: reply size … · brand … · condition … · category … · men / women · cover 2"   # WO37/43
DEPARTMENT_WORDS = {"men": "Men", "mens": "Men", "men's": "Men", "man": "Men", "women": "Women", "womens": "Women",
                    "women's": "Women", "woman": "Women", "kids": "Kids", "kid": "Kids", "unisex": "Unisex"}
HELD_HINT = " (still held as a possible re-share: reply 'different item' to list it or 'same item' to drop it)"
CONDITION_QUESTION = "Brand new or worn? (couldn't tell from the photos)"
CONDITION_BUTTONS = (("NWT", "nwt"), ("Like New", "like_new"), ("Good", "good"))    # label, callback choice
CONDITION_HINT = "Tap a button, or reply nwt / like new / good"
CONDITION_TAPPED = {"nwt": "New with tags", "like_new": "Like New (brand new, no tags)", "good": "Good (worn)"}
KIDS_QUESTION = "Girls or Boys? (Poshmark files kids sizes under one of them)"
KIDS_BUTTONS = (("Girls", "girls"), ("Boys", "boys"))
KIDS_HINT = "Tap a button, or reply girls / boys"
CATEGORY_QUESTION = "Which category? (not sure from the photos)"
CATEGORY_HINT = "Tap a category, or reply with one, e.g. 'Shorts' or 'Skirts › Skirt Sets'"
NO_BRAND_BUTTON = "No brand"

_NUM = r"\d+(?:\.\d+)?"
# A number that is money, whichever way the owner says it: "$85", "$ 85", "85 usd", "85 dollars", "price 85", "list: 85".
_MONEY = re.compile(rf"(?:\$\s*({_NUM})|\b({_NUM})\s*(?:usd|dollars?|bucks)\b|\b(?:price|list)\b\s*[:=]?\s*\$?\s*({_NUM}))",
                    re.I)
# A standalone number: not glued to letters or digits (7.5M, 5T, 8-9, 8/9) and not a decimal's tail.
_BARE = re.compile(rf"(?<![\w.$/-])({_NUM})(?![\w/-]|\.\d)")
_SIZE_BEFORE = re.compile(r"\b(?:size|sz|eu|us|uk|toddler|kids?|y|c|cm|in|inch)\s*[:=#]?\s*$", re.I)
_UNIT_AFTER = re.compile(r"\s*(?:cm|mm|in|inch|inches|us|uk|eu|y|t|m|w)\b", re.I)
# A message that is nothing but a price: "28", "$28", "28.00", "28 dollars" (typed without a reply, WO20).
# "cover 2": photo 2 of the item becomes its cover (WO23; a reply to the card, or typed while it is open).
COVER_CMD = re.compile(r"^\s*cover\s*#?\s*(\d{1,2})\s*$", re.I)


def cover_index(number: str) -> int:
    """WO37: "cover 3" is her 3rd shared photo (counted from 1, as she sees them); "cover 0" is taken as the first."""
    return max(int(number) - 1, 0)


SHIPPED_CMD =re.compile(r"^\s*shipped\s+(.+?)\s*$", re.I | re.S)       # WO33: "shipped <title words>"
# Replies to "⚠️ … I can't see it in the closet" (outbox kind "unconfirmed", WO28 §3): "posted <url>" / "retry".
POSTED_CMD = re.compile(r"^\s*(?:it'?s\s+)?posted\s*[:\-]?\s*(\S+)\s*$", re.I)
RETRY_CMD = re.compile(r"^\s*(?:retry|try again|not there|isn'?t there)\s*[.!]*\s*$", re.I)
UNCONFIRMED_HINT = "Reply 'posted <url>' if the listing is on Poshmark (its address), or 'retry' if it is not"
_PLAIN_PRICE = re.compile(r"^\s*\$?\s*(\d{1,5}(?:\.\d{1,2})?)\s*(?:\$|usd|dollars?|bucks)?\s*$", re.I)

_warned_no_users = False


def _now() -> datetime:
    """Clock seam for resend_pending (tests monkeypatch it)."""
    return datetime.now(timezone.utc)


def parse_user_ids(raw: str) -> set[int]:
    out = set()
    for tok in (raw or "").replace(";", ",").split(","):
        tok = tok.strip()
        if tok.lstrip("-").isdigit():
            out.add(int(tok))
    return out


def bot_for(s: Settings) -> Bot | None:
    """The configured Bot, or None when telegram.enabled is false or the env vars are missing (dev: messages print)."""
    global _warned_no_users
    if not s.get("telegram.enabled"):
        return None
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if not tok or not chat:
        return None
    users = parse_user_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS", ""))
    if not users and not _warned_no_users:
        _warned_no_users = True
        print("[approve] TELEGRAM_ALLOWED_USER_IDS is empty: the bot sends but accepts nobody's replies", file=sys.stderr)
    return Bot(tok, chat, users)


def ops_bot_for(s: Settings) -> Bot | None:
    """The bot, speaking to the ops chat (WO29: the owner's private chat; the status message lives there); None
    without one — never the group."""
    group = bot_for(s)
    chat = notify.ops_chat(s)
    if group is None or chat is None:
        return None
    return Bot(group.token, chat, group.allowed_users)


NOOP = "noop"          # the callback of an answered card's "✓ …" button: it shows the answer and does nothing
_IDS = re.compile(r"\b([ib])_\d{6}_[0-9a-f]{6}\b")


def _ack(db: DB, bot: Bot, kind: str, ref: str, label: str) -> None:
    """An answered card (WO29: no message in the group): its buttons become one "✓ …" button that does nothing, on
    every copy of it sent in the last two days."""
    since = (_now() - timedelta(days=2)).isoformat(timespec="seconds")
    rows = db.conn.execute("SELECT message_id FROM outbox WHERE kind=? AND ref=? AND chat_id=? AND sent_at>=? "
                           "ORDER BY rowid DESC LIMIT 5", (kind, ref, str(bot.chat_id), since)).fetchall()
    for r in rows:
        try:
            bot.set_buttons(int(r["message_id"]), [[{"text": label[:60], "callback_data": NOOP}]])
        except Exception as e:  # noqa: BLE001 - "not modified", or gone: the answer is taken either way
            db.log(ref, "card_mark_failed", f"{type(e).__name__}: {e}")


def when_posting(s: Settings, now: datetime | None = None) -> str:
    """When an approved listing goes up, in plain words (WO33): "in the next few minutes" inside the schedule's hours,
    else "at 8:00" (the hours' start)."""
    hours = s.get("schedule.hours") or ["08:00", "23:00"]
    start, end = str(hours[0]), str(hours[1])
    try:
        from zoneinfo import ZoneInfo
        local = (now or _now()).astimezone(ZoneInfo(s.get("schedule.timezone") or "America/New_York"))
    except Exception:  # noqa: BLE001
        local = now or _now()
    if start <= local.strftime("%H:%M") < end:
        return "in the next few minutes"
    return f"at {start[1:] if start.startswith('0') else start}"


def _sites(s: Settings) -> str:
    from thrift_agent import crosslist
    names = ["Poshmark", *(crosslist.LABEL.get(mp, mp) for mp in crosslist.enabled(s))]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def next_step(s: Settings, db: DB, iid: str) -> str:
    """What happens next for the item, in the owner's words (WO33): when it goes up and where, or what it still waits
    for. Never a command: a reply in Telegram is always enough."""
    from thrift_agent import crosslist
    it = db.item(iid)
    if it is None:
        return "nothing else to do"
    live = [crosslist.LABEL.get(r["marketplace"], r["marketplace"]) for r in db.listings_for(iid)
            if r["status"] == "posted"]
    if live:
        return f"it's already live on {' and '.join(live)} — a change there is made in the app"
    status = it["status"]
    if status == "ready" and it["owner_price"] and needs_size(it):     # WO39: nothing goes up without a size
        return "it goes up once a size is tapped on its card"
    if status == "ready" and it["owner_price"] and needs_brand(it):    # 2026-10-27: nor without a brand
        return "it goes up once you reply with its brand (or 'no brand')"
    if status == "ready" and it["owner_price"]:
        return f"it goes up {when_posting(s)} on {_sites(s)}"
    if status == "new":
        return "updating the listing now — its card comes back in about a minute"
    if status == "awaiting_condition":
        return "next: brand new or worn? (the buttons above)"
    if status in WAITING_ITEM:
        gate = loads(it["gate"]) or {}
        if gate.get("hold"):
            return "it waits for 'different item' or 'same item'"
        if gate.get("ask_kids"):
            return "next: Girls or Boys?"
        return "its card stays open for the price" if not it["owner_price"] else "its card stays open for the answer"
    if status == "dropped":
        return "it's dropped — it won't be listed"
    return f"it's {status}"


_REPLYING: contextvars.ContextVar[bool] = contextvars.ContextVar("replying", default=False)


def _confirm(s: Settings, db: DB, bot: Bot, reply_to: int | None, iid: str, understood: str) -> None:
    """The answer every reply gets (WO33, the owner: replies never go nowhere): what was understood and what happens
    next, as a reply to her message, at once. Nothing for a button tap (its "✓ …" label is the answer)."""
    if reply_to is not None and _REPLYING.get():
        bot.send_message(f"✓ {understood} — {next_step(s, db, iid)}", reply_to=reply_to)


def _tell(bot: Bot, mid: int | None, text: str) -> None:
    """A reply in the group to an answer that wasn't taken (WO29): one plain line, no ids."""
    bot.send_message(_IDS.sub(lambda m: "this item" if m[1] == "i" else "this batch", text), reply_to=mid)


# ---------- reply parsing ----------

def parse_reply(text: str) -> tuple[int | None, str | None]:
    """(price, note) from an owner reply: "85" / "$85" / "85.00" / "85 dollars" -> (85, None);
    "size 8, 45" -> (45, "size 8"); "size 8" -> (None, "size 8").

    The price is the number marked as money ($, usd, dollars, "price"/"list"), else the LAST standalone number
    that is not a size ("size 8", "8 us", "7.5M" are never the price). The note is the rest, separators tidied."""
    text = (text or "").strip()
    if not text:
        return None, None
    span, price = None, None
    if m := _MONEY.search(text):
        span, price = m.span(), next(g for g in m.groups() if g)
    else:
        for m in _BARE.finditer(text):
            if _SIZE_BEFORE.search(text[:m.start()]) or _UNIT_AFTER.match(text[m.end():]):
                continue
            span, price = m.span(), m.group(1)
    if span is None:
        return None, text
    amount = int(round(float(price)))
    if amount <= 0:
        return None, text
    note = _tidy(text[:span[0]] + " " + text[span[1]:])
    return amount, note or None


def plain_price(text: str) -> int | None:
    """The price in a message that is nothing but one ("28", "$28", "28.00", "28 dollars"), else None."""
    m = _PLAIN_PRICE.match(text or "")
    if not m:
        return None
    amount = math.floor(float(m[1]) + 0.5)
    return amount if amount > 0 else None


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*([,;])\s*(?:[,;]\s*)+", r"\1 ", text)          # ", ," -> ","
    text = re.sub(r"\s+([,;:.])", r"\1", text)
    return text.strip(" ,;:-–—\t\n")


def parse_condition(text: str) -> str | None:
    """A typed answer to the condition question: "nwt" / "with tags" -> nwt; "like new" / "no tags" / "brand new" /
    "nwot" -> like_new; "good" / "worn" / "used" -> good; anything else None."""
    t = text.strip().lower()
    if re.search(r"\bnwt\b|with tags", t):
        return "nwt"
    if re.search(r"like[\s-]*new|\bnwot\b|no tags|without tags|brand new|\bnew\b", t):
        return "like_new"
    if re.search(r"\b(good|worn|used)\b", t):
        return "good"
    return None


def parse_kids(text: str) -> str | None:
    """A typed answer to "Girls or Boys?": girls | boys, else None."""
    t = text.strip().lower()
    if re.search(r"\bgirls?\b", t) and not re.search(r"\bboys?\b", t):
        return "girls"
    if re.search(r"\bboys?\b", t) and not re.search(r"\bgirls?\b", t):
        return "boys"
    return None


# ---------- the queue (WO20) ----------

def _item_kind(status: str, gate: dict) -> str:
    """What an item in the queue waits for: being processed, the condition, its category (WO25), Girls/Boys, its price
    card, or the poster's question."""
    if status == "new":
        return NEW_ITEM
    if status == "awaiting_condition":
        return "condition"
    return "category" if gate.get("ask_category") else "kids" if gate.get("ask_kids") else "item"


LANES = ("real", "test")      # the group's queue, and the test shares' (WO45 §2: their cards go to the private chat)


def lane_of(db: DB, kind: str, ref: str) -> str:
    """Which queue a message belongs to: "test" for a test share's batch or item (WO45 §2: `?test=1`, the cards in the
    private chat), else "real" (the group's). Each lane holds one open message at a time and never the other's — live
    2026-10-20: a 🧪 test card left open in the private chat held nine real cards for an hour and a quarter."""
    row = db.batch(ref) if kind in ("batch", "regroup", NEW_BATCH) else db.item(ref)
    return "test" if row is not None and "test" in row.keys() and row["test"] else "real"


def queue(db: DB, lane: str | None = "real") -> list[tuple[str, str]]:
    """Everything that waits for the owner, in the order they are asked: [(kind, ref)]. The oldest batch first — its
    contact sheet, then its items in photo order, each item's questions before its price card (one entry per item:
    what it waits for now) — then the next batch. An item put off with [Later] goes behind everything that was queued
    when the owner tapped it. NEW_BATCH / NEW_ITEM entries are still being processed: the queue holds there, so nothing
    behind them jumps ahead. `lane`: the group's queue ("real", the default), the test shares' ("test"), or both
    (None: the worker's processing order). A pure read."""
    # Sort key: (when it joined the queue, the batch's insertion order, -1 for the contact sheet / the photo order,
    # id). The insertion order keeps each batch's messages together when two batches were registered in the same
    # second (created_at has whole seconds).
    entries = []
    kinds = {"needs_confirm": "batch", "regroup": "regroup", "new": NEW_BATCH}
    for b in db.conn.execute("SELECT rowid AS n, id, status, created_at, COALESCE(test, 0) AS test FROM batches "
                             "WHERE status IN ('new', 'needs_confirm', 'regroup')"):
        if lane is not None and ("test" if b["test"] else "real") != lane:
            continue
        entries.append(((b["created_at"], b["n"], -1, b["id"]), kinds[b["status"]], b["id"]))
    marks = ",".join("?" * len(QUEUED_ITEMS))
    # The items of a batch whose photos are being fixed ([Wrong photos]) wait, unasked and unprocessed, until the fix.
    rows = db.conn.execute(
        "SELECT i.id, i.status, i.seq, i.gate, i.deferred_at, COALESCE(i.test, 0) AS test, "
        "COALESCE(b.created_at, i.created_at) AS since, "
        f"COALESCE(b.rowid, 0) AS n FROM items i LEFT JOIN batches b ON b.id = i.batch_id WHERE i.status IN ({marks}) "
        "AND COALESCE(b.status, '') != 'regroup'", QUEUED_ITEMS)
    for r in rows:
        if lane is not None and ("test" if r["test"] else "real") != lane:
            continue
        entries.append(((r["deferred_at"] or r["since"], r["n"], r["seq"], r["id"]),
                        _item_kind(r["status"], loads(r["gate"]) or {}), r["id"]))
    return [(kind, ref) for _, kind, ref in sorted(entries)]


def processing_order(db: DB) -> list[tuple[str, str]]:
    """The batches and items still to be processed, in the queue's order (both lanes): the worker takes them in this
    order, so the card the owner needs next is the one ready first (an item reprocessed after an answer jumps ahead)."""
    return [(kind, ref) for kind, ref in queue(db, lane=None) if kind in (NEW_BATCH, NEW_ITEM)]


def next_up(db: DB, lane: str = "real") -> tuple[str, str] | None:
    """The message to send next on `lane`: the head of its queue, or None (nothing waits, or the head is still being
    processed)."""
    q = queue(db, lane)
    if not q or q[0][0] in (NEW_BATCH, NEW_ITEM):
        return None
    return q[0]


def _waits(db: DB, kind: str, ref: str) -> bool:
    """Does the batch/item still wait for this kind of answer?"""
    if kind in ("batch", "regroup"):
        b = db.batch(ref)
        return b is not None and b["status"] == ("needs_confirm" if kind == "batch" else "regroup")
    it = db.item(ref)
    if it is None or it["status"] not in OWNER_WAITING:
        return False
    b = db.batch(it["batch_id"])
    if b is not None and b["status"] == "regroup":
        return False                                  # its batch's photos are being fixed: asked again after the fix
    return _item_kind(it["status"], loads(it["gate"]) or {}) == kind


def open_message(db: DB, settle: bool = True, lane: str = "real"):
    """The one message of `lane` that waits for the owner's answer (an outbox row), or None: the newest unresolved
    queue message whose batch/item still waits for that answer. Every other unresolved queue message of the lane is
    resolved on the way — answered elsewhere (a CLI command, a button on an older copy), moved on (reprocessing,
    dropped), or superseded (a re-send, or the flood of messages from before the queue). Their buttons keep working.
    The other lane's messages are never looked at (a test share's card never counts as the group's open message)."""
    marks = ",".join("?" * len(QUEUE_KINDS))
    rows = [r for r in db.conn.execute(f"SELECT rowid AS n, * FROM outbox WHERE resolved_at IS NULL AND kind IN "
                                       f"({marks}) ORDER BY rowid DESC", QUEUE_KINDS).fetchall()
            if lane_of(db, r["kind"], r["ref"]) == lane]
    found = None
    for r in rows:
        if found is not None and (r["kind"], r["ref"]) == (found["kind"], found["ref"]):
            continue                                  # the same question: a re-send, or the "Change" prompt
        if found is None and _waits(db, r["kind"], r["ref"]):
            found = r
            if not settle:
                break                                 # a look only (thrift status): nothing is closed
            continue
        if settle:
            db.outbox_resolve(r["kind"], r["ref"])
    return found


def _queued_items(db: DB) -> list[str]:
    marks = ",".join("?" * len(QUEUED_ITEMS))
    return [r[0] for r in db.conn.execute(f"SELECT id FROM items WHERE status IN ({marks})", QUEUED_ITEMS)]


def _pending_batches(db: DB) -> int:
    return db.conn.execute("SELECT COUNT(*) FROM batches WHERE status IN ('new', 'needs_confirm')").fetchone()[0]


def _note_round(db: DB) -> None:
    """Remember every item queued since the queue was last empty: the "of 10" in "3 of 10 left"."""
    queued = _queued_items(db)
    if not queued and not _pending_batches(db):
        db.kv_set(ROUND_KEY, "[]")
        return
    seen = set(json.loads(db.kv_get(ROUND_KEY) or "[]")) | set(queued)
    db.kv_set(ROUND_KEY, json.dumps(sorted(seen)))


def progress(db: DB) -> str:
    """ "3 of 10 left": the items still queued (being processed or waiting for an answer) of those queued since the
    queue was last empty; "all done" when none is left."""
    queued = _queued_items(db)
    seen = set(json.loads(db.kv_get(ROUND_KEY) or "[]")) | set(queued)
    if queued:
        return f"{len(queued)} of {len(seen)} left"
    return "next: a new batch" if _pending_batches(db) else "all done"


def _lock(db: DB, wait: float = 10.0) -> str | None:
    """One sender at a time — the worker's two threads, the poster, a CLI command — so two of them never both find
    nothing open and both send. A kv row taken inside an IMMEDIATE transaction; one older than LOCK_TTL was left by a
    crash and is taken over. Returns the token, or None after `wait` seconds."""
    token, deadline = uuid.uuid4().hex, time.monotonic() + wait
    while True:
        with db.tx():
            held = db.kv_get(LOCK_KEY) or ""
            try:
                since = float(held.partition("@")[2] or 0)
            except ValueError:
                since = 0.0
            if not held or time.time() - since > LOCK_TTL:
                db.kv_set(LOCK_KEY, f"{token}@{time.time():.3f}")
                return token
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def _unlock(db: DB, token: str) -> None:
    with db.tx():
        if (db.kv_get(LOCK_KEY) or "").startswith(token + "@"):
            db.conn.execute("DELETE FROM kv WHERE key=?", (LOCK_KEY,))


def _send(s: Settings, db: DB, kind: str, ref: str) -> None:
    globals()[SENDERS[kind]](s, db, ref)              # looked up at call time: tests replace a sender
    if bot_for(s) is None:                            # dev: the print is the open message, answered from the CLI
        mid = db.conn.execute("SELECT COALESCE(MAX(message_id), 0) + 1 FROM outbox WHERE chat_id=?",
                              (DEV_CHAT,)).fetchone()[0]
        db.add_outbox(DEV_CHAT, mid, kind, ref)


def pump(s: Settings, db: DB) -> str | None:
    """Send the next message of each lane if none of that lane is open (one question at a time, the group's queue and
    the test shares' each on its own). Called after every processing step and every answer, and by the worker's loops.
    Returns "kind ref" for what was sent (the group's first); None when nothing was: one is open, nothing waits, the
    head of the queue is still being processed, or another sender holds the lock (it sends)."""
    if db.conn.in_transaction:
        return None                                   # never inside a caller's transaction; the worker pumps soon
    token = _lock(db)
    if token is None:
        return None
    try:
        sent = None
        for lane in LANES:
            if open_message(db, lane=lane) is not None:
                continue
            if lane == "real":
                _note_round(db)
            if (head := next_up(db, lane)) is None:
                continue
            _send(s, db, *head)
            note_send_ok(db)
            sent = sent or " ".join(head)
        return sent
    finally:
        _unlock(db, token)


SEND_FAILED_KEY = "queue_send_failed"   # kv: {"since", "said", "error"} — the next card couldn't be sent since then
SEND_FAILED_SAY_AFTER = 600             # seconds of failed sends before the one private line (the owner, 2026-10-20)


def note_send_failure(db: DB, error: str, now: datetime | None = None) -> str | None:
    """The next message couldn't be sent (Telegram unreachable, a DNS failure — live 2026-10-20, "nodename nor servname
    provided"): remembered; past SEND_FAILED_SAY_AFTER of it, ONE private line says so (the owner never has to notice a
    silent queue by himself). Returns the line said, else None. note_send_ok() clears it."""
    now = now or datetime.now(timezone.utc)
    state = loads(db.kv_get(SEND_FAILED_KEY)) or {}
    since = state.get("since") or now.isoformat(timespec="seconds")
    state = {"since": since, "said": bool(state.get("said")), "error": str(error)[:200]}
    db.kv_set(SEND_FAILED_KEY, json.dumps(state))
    try:
        started = datetime.fromisoformat(since)
    except ValueError:
        started = now
    if state["said"] or (now - started).total_seconds() < SEND_FAILED_SAY_AFTER:
        return None
    waiting = len([1 for kind, _ in queue(db) if kind not in (NEW_BATCH, NEW_ITEM)])
    line = (f"⚠️ The next card couldn't be sent for {int((now - started).total_seconds() // 60)} min "
            f"({state['error'][:120]}) — {waiting} waiting; I keep trying every turn.")
    state["said"] = True
    db.kv_set(SEND_FAILED_KEY, json.dumps(state))
    db.log(None, "queue_send_failed", state)
    notify.say(line)
    return line


def note_send_ok(db: DB) -> None:
    """A message went out: the failure note is cleared (a new episode says once again)."""
    if db.kv_get(SEND_FAILED_KEY):
        db.conn.execute("DELETE FROM kv WHERE key=?", (SEND_FAILED_KEY,))
        db.conn.commit()


# ---------- outgoing ----------

def batch_caption(bid: str, b) -> str:
    segd = loads(b["segmentation"]) or {}
    groups = segd.get("groups") or []
    summaries = segd.get("summaries") or []
    n = len(segd.get("photos") or []) or int(b["n_photos"] or 0)
    lines = [f"Batch {bid}: {n} photos -> {len(groups)} items"]
    for k, g in enumerate(groups, 1):
        summary = summaries[k - 1] if k - 1 < len(summaries) and summaries[k - 1] else "item"
        lines.append(f"item {k}: {summary} - photos {list(g)}")
    if unassigned := segd.get("unassigned"):
        lines.append(f"unassigned screenshots: {list(unassigned)}")
    if pauses := segd.get("pauses"):                    # [[photo, seconds], ...]: breaks in shooting, as on the sheet
        lines.append("pauses before: " + ", ".join(f"#{i} ({seg.fmt_pause(sec)})" for i, sec in pauses))
    if reasons := loads(b["reasons"]) or []:
        lines.append("Check: " + "; ".join(str(r) for r in reasons))
    lines.append(BATCH_HINT)
    return "\n".join(lines)


def send_batch(s: Settings, db: DB, bid: str) -> None:
    """Contact sheet + summary; the owner replies ok / 12>2 / split 7 / merge 2 3 / drop 7. Records the outbox row.
    Without a bot: print the caption (dev)."""
    b = db.batch(bid)
    if b is None:
        raise ValueError(f"unknown batch {bid}")
    caption = batch_caption(bid, b)
    sheet = s.path("work") / bid / "contact_sheet.png"
    bot = bot_for(s)
    if bot is None:
        notify.group_photo(sheet, f"{caption}\n(thrift confirm {bid} ok)")
        return
    if (row := sent_before(db, bot.chat_id, "batch", bid)) is not None:   # WO37: once per batch
        db.conn.execute("UPDATE outbox SET resolved_at=NULL WHERE chat_id=? AND message_id=?",
                        (row["chat_id"], row["message_id"]))
        return

    def send() -> int:
        if sheet.is_file() and len(caption) <= MAX_CAPTION:
            return bot.send_photo(sheet, caption)
        if sheet.is_file():                                 # too many items for one caption: sheet, then the text
            bot.send_photo(sheet, caption.splitlines()[0])
        return bot.send_message(caption)
    once(db, bot, "batch", bid, caption, send)


def send_regroup(s: Settings, db: DB, bid: str) -> None:
    """[Wrong photos] (WO20b): the batch's contact sheet as its items are now, the items listed, and the usual fixes.
    Records the outbox row (kind regroup). Without a bot: print (dev)."""
    b = db.batch(bid)
    if b is None:
        raise ValueError(f"unknown batch {bid}")
    items = db.conn.execute("SELECT * FROM items WHERE batch_id=? ORDER BY seq", (bid,)).fetchall()
    groups = [pipeline.item_group(it) for it in items]
    n = len((loads(b["segmentation"]) or {}).get("photos") or [])
    lines = [f"Wrong photos? Batch {bid}: {n} photos -> {len(items)} items, as they are now"]
    lines += [f"item {it['seq']}: {_title(it, it['id'])[:60]} - photos {g}" for it, g in zip(items, groups)]
    if left_out := sorted(set(range(n)) - {i for g in groups for i in g}):
        lines.append(f"left out: {left_out}")
    lines.append(REGROUP_HINT)
    caption = "\n".join(lines)
    sheet = s.path("work") / bid / pipeline.REGROUP_SHEET
    bot = bot_for(s)
    if bot is None:
        notify.group_photo(sheet, f"{caption}\n(thrift confirm {bid} <fix>)")
        return
    if (row := sent_before(db, bot.chat_id, "regroup", bid)) is not None:   # WO37: once per batch
        db.conn.execute("UPDATE outbox SET resolved_at=NULL WHERE chat_id=? AND message_id=?",
                        (row["chat_id"], row["message_id"]))
        return

    def send() -> int:
        if sheet.is_file() and len(caption) <= MAX_CAPTION:
            return bot.send_photo(sheet, caption)
        if sheet.is_file():
            bot.send_photo(sheet, lines[0])
        return bot.send_message(caption)
    once(db, bot, "regroup", bid, caption, send)


def _ev(facts: dict, name: str) -> str | None:
    ev = facts.get(name)
    return (ev or {}).get("value") if isinstance(ev, dict) else None


def _title(it, iid: str) -> str:
    renders = loads(it["renders"]) or {}
    return (renders.get("poshmark") or next(iter(renders.values()), None) or {}).get("title") or iid


def size_words(render: dict, facts: dict) -> str | None:
    """The size as Poshmark's size menu shows it: "7.5 (Toddler Girl)" for a kids shoe, "6 (Boys)" for kids clothing,
    "7.5" or "M" for an adult; the render's own label when the menu's can't be worked out."""
    from thrift_agent.post.poshmark import size_choice      # the form's size maps (lazy: it imports Playwright)
    try:
        choice = size_choice(Render.model_validate(render)) if render else None
    except ValidationError:
        choice = None
    if choice is None:
        return render.get("size") or _ev(facts, "size_us")
    if choice.tab and choice.tab != "Standard" and "(" not in choice.button:   # "6 (Boys)", "14 (Plus)", "5 (Baby)"
        return f"{choice.button} ({choice.tab})"
    return choice.button


def needs_size(it) -> bool:
    """WO37/WO39: the item has no size at all (none read, none answered) — no site lists it until her reply 'size …'
    to its card. The card's ⚠️ line, the price's answer and the 💤 line all say so."""
    if it is None:
        return False
    facts, renders, gate = loads(it["facts"]) or {}, loads(it["renders"]) or {}, loads(it["gate"]) or {}
    posh = renders.get("poshmark") or next(iter(renders.values()), None) or {}
    guessed = [n for n in [*(gate.get("info") or []), *(gate.get("notes") or [])] if n.startswith(SIZE_NOTE)]
    return not size_words(posh, facts) and bool(guessed)


NEEDS_SIZE = "tap a size to list it"
NEEDS_BRAND = "needs the brand: reply with it (or 'no brand')"
BRAND_ASK = ("🏷️ What brand is {title}? I couldn't read it, and Depop and Vinted need one — reply to this message "
             "with the brand, or 'no brand'.")
BRAND_WAITING = "waiting for the brand"              # a listing row's reason while she is asked (both safety nets keep it)


def needs_brand(it) -> bool:
    """Alex, 2026-10-27: the gate asked the brand — it couldn't be read, or the reading is under 0.70 ("Brand? Couldn't
    read it", "Brand: … — my best reading") — and she hasn't answered (a brand, or 'no brand': items.owner_brand):
    Depop and Vinted require one, so the item goes up nowhere until her reply (live: the pink biker shorts went up as
    Depop's "Other" and Vinted failed). A brand read well, or no label the model is sure of, never asks."""
    if it is None or it["owner_brand"]:
        return False
    if ((loads(it["facts"]) or {}).get("brand") or {}).get("source") == "owner":
        return False
    return asks_brand(loads(it["gate"]) or {})


def ask_brands(s: Settings, db: DB) -> int:
    """ONE message per item priced and waiting for its brand — "🏷️ What brand is <title>? … reply to this message
    with the brand, or 'no brand'." — at the bottom of the group, so she never scrolls back to its card (as the size's,
    WO39). A row of it already failed or skipped (a site that refused it before the rule) is marked waiting, so neither
    safety net re-posts it meanwhile. Once per item (outbox kind `brand`). Returns the messages sent."""
    bot = bot_for(s)
    if bot is None:
        return 0
    sent = 0
    for it in db.items("ready"):
        if not it["owner_price"] or not needs_brand(it) or ("test" in it.keys() and it["test"]):
            continue
        iid = it["id"]
        for row in db.listings_for(iid):
            if row["status"] in ("failed", "skipped", "queued") and not str(row["error"] or "").startswith(BRAND_WAITING):
                db.upsert_listing(iid, row["marketplace"], status="skipped",
                                  error=f"{BRAND_WAITING} (was: {row['error'] or row['status']})"[:500])
        if sent_before(db, bot.chat_id, "brand", iid) is not None:
            continue
        title = (((loads(it["renders"]) or {}).get("poshmark") or {}).get("title")) or iid
        text = BRAND_ASK.format(title=title)
        if once(db, bot, "brand", iid, text, lambda t=text: bot.send_message(t)) is not None:
            sent += 1
    return sent


def _reply_brand(s: Settings, db: DB, bot: Bot, iid: str, text: str, mid: int | None) -> str:
    """A reply to "🏷️ What brand is …?": the brand of THAT item, or 'no brand'. An item not on any site yet gets it
    the usual way (pipeline.set_brand / set_no_brand: the copy and the title follow); one already live on a site
    (it went up before this rule) gets it for the sites still waiting only (pipeline.brand_for_waiting)."""
    from thrift_agent import pipeline
    words = (text or "").strip().strip("'\"").strip()
    if not words or len(words.split()) > 6:
        bot.send_message("Reply with the brand, e.g. 'Zella', or 'no brand'", reply_to=mid)
        return f"brand {iid}: unreadable reply {text!r}"
    m = _BRAND_CMD.match(words)
    brand = m.group(1).strip() if m else words
    none = bool(pipeline.NO_BRAND_WORDS.fullmatch(brand.strip(" .!")))
    it = db.item(iid)
    if it is not None and pipeline.reached_site(db, it):
        pipeline.brand_for_waiting(s, db, iid, None if none else brand)
        db.outbox_resolve("brand", iid)
        _confirm(s, db, bot, mid, iid, "No brand" if none else f"Brand: {brand}")
        return f"brand {iid}: {'none' if none else brand!r} (for the sites still waiting)"
    out = _set_no_brand(s, db, bot, iid, mid) if none else _set_brand(s, db, bot, iid, brand, mid)
    pipeline.release_brand_wait(db, iid)
    db.outbox_resolve("brand", iid)
    return out
SIZE_ASK = "Waiting for a size: {title} — tap one on its card (or reply 'size …' to this message)"


def ask_sizes(s: Settings, db: DB) -> int:
    """WO39 follow-up (the owner): an item priced but waiting for a size gets ONE short message at the bottom of the
    group — "Waiting for a size: <title> — reply 'size …' to this message" — so she never scrolls back to its card; a
    size replied to it is that item's size. Once per item, never repeated (outbox kind `size`, `once`). Returns the
    messages sent."""
    from thrift_agent import daily
    bot = bot_for(s)
    if bot is None:
        return 0
    sent = 0
    for iid in daily.size_waiting(db):
        if sent_before(db, bot.chat_id, "size", iid) is not None:
            continue
        it = db.item(iid)
        title = (((loads(it["renders"]) or {}).get("poshmark") or {}).get("title") if it else None) or iid
        text = SIZE_ASK.format(title=title)
        if once(db, bot, "size", iid, text, lambda t=text: bot.send_message(t)) is not None:
            sent += 1
    return sent


def _reply_size(s: Settings, db: DB, bot: Bot, iid: str, text: str, mid: int | None) -> str:
    """A reply to "Waiting for a size: …" (WO39 follow-up): the size of THAT item ("size S", "S", "size 8.5", with a
    price or not); anything else gets the hint."""
    price, note = parse_reply(text)
    size = size_reply(note if note is not None else text)
    if size is None:
        bot.send_message("Reply with the size, e.g. 'size S' or 'size 8.5'", reply_to=mid)
        return f"size {iid}: unreadable reply {text!r}"
    out = _set_size(s, db, bot, iid, size, price, mid)
    it = db.item(iid)
    if it is not None and it["owner_size"]:
        db.outbox_resolve("size", iid)
    return out


def item_caption(iid: str, it) -> tuple[str, int | None]:
    """(caption, suggested price) for the item's price card, in Poshmark's words (WO20): the title, the size as
    Poshmark's size menu shows it, the condition as Poshmark's label (NWT / Like New / Good), a warning that needs a
    look (the cover, a well-worn pair, a flaw no photo shows) and only the allowed questions. The price is on the
    buttons. Pure: reads the row's JSON columns only."""
    facts = loads(it["facts"]) or {}
    pr = loads(it["price"]) or {}
    renders = loads(it["renders"]) or {}
    gate = loads(it["gate"]) or {}
    posh = renders.get("poshmark") or next(iter(renders.values()), None) or {}
    brand = _ev(facts, "brand")
    lines = [posh.get("title") or " ".join(x for x in (brand, facts.get("item_type")) if x) or iid]
    notes = list(gate.get("notes") or [])
    guessed = [n for n in [*(gate.get("info") or []), *notes] if n.startswith(SIZE_NOTE)]   # WO33: shown, never asked
    if size := size_words(posh, facts):
        from thrift_agent.post.fallback import dual_parts
        printed = str((facts.get("size_printed") or {}).get("value") or "").strip()
        label = f" (label {printed})" if dual_parts(printed) else ""       # WO50: "Size M (label S/M)"
        if it["owner_size"] and (guessed or size_tapped(it)):      # WO43: her tap (or reply) on a card without a size
            lines.append(f"📏 Size {size} ✓{label}")
        else:
            lines.append(f"Size {size}{label}" + (" — my best reading; reply 'size …' to change it" if guessed else ""))
    elif guessed:                                                   # WO37: no site lists it without one; WO43: tap it
        lines.append(NO_SIZE_LINE)
    cond = facts.get("condition")
    label = POSH_CONDITION.get(cond, cond or "unknown")
    if (facts.get("condition_evidence") or {}).get("source") == "owner":
        label += " (your answer)"                                   # "Brand new or worn?" was answered
    lines.append(f"Condition: {label}")
    if line := photo_line(it, posh, facts):                         # WO45 §6: "📷 4 photos"
        lines.append(line)
    if posh.get("category"):                                        # WO37: what the rules decided, shown
        dept = posh.get("department")
        unsure = (facts.get("department_confidence") is not None and float(facts["department_confidence"]) < 0.7
                  and dept in ("Women", "Men"))
        path = {"department": dept, "category": posh.get("category"), "subcategory": posh.get("subcategory")}
        named = (dept in ("Men", "Unisex") or unsure) and dept     # WO43: the department named unless plainly Women
        lines.append("Category: " + (f"{dept} › {taxonomy.path_label(path, dept)}" if named
                                     else taxonomy.path_label(path, dept))
                     + (" — my best reading; reply 'men' or 'women' to change it" if unsure else ""))
    if "questions" in gate:                                         # processed since WO20
        lines += [f"⚠️ {n}" for n in notes if not n.startswith(SIZE_NOTE)]
        if questions := gate.get("questions") or []:
            lines += [f"❓ {q}" for q in questions]
        lines.append(CORRECT_HINT)
    else:                                                           # processed before WO20: as it was recorded
        lines += [f"Note: {n}" for n in gate.get("notes") or []]
        if gate.get("decision") == "needs_info":
            lines.append("Open questions:")
            lines += [f"- {r}" for r in gate.get("reasons") or []]
    price = pr.get("list_price")
    price = int(price) if isinstance(price, (int, float)) and price > 0 else None
    if price is None:
        lines.append("No price yet: type one")
    return "\n".join(lines), price


PHOTO_LINE = "📷 {n} photos"
PHOTO_LINE_PART = "📷 {n} of {want} photos — the rest join when they arrive"


def photo_line(it, posh: dict, facts: dict) -> str | None:
    """WO45 §6: the card says how many photos the item has; a card made from part of a share (`photos_expected` on
    the item, from the share's `done N`) says "3 of 4 — the rest join when they arrive"."""
    n = len(facts.get("photo_roles") or []) or len(posh.get("photos") or [])
    if not n:
        return None
    want = it["photos_expected"] if "photos_expected" in it.keys() else None
    if want and int(want) > n:
        return PHOTO_LINE_PART.format(n=n, want=int(want))
    return PHOTO_LINE.format(n=n)


def card(iid: str, it) -> tuple[str, str] | None:
    """(kind, text) of the question the item puts to the owner now — its price card (caption and price), Girls/Boys or
    "Brand new or worn?" — or None when it waits for none of them. `thrift recover` compares it before and after (WO24):
    a card is sent again only when what it shows changed. Pure, like item_caption."""
    if it["status"] not in ("awaiting_condition", *WAITING_ITEM):
        return None
    gate = loads(it["gate"]) or {}
    kind = _item_kind(it["status"], gate)
    if kind == "item":
        caption, price = item_caption(iid, it)
        return kind, f"{caption}\n${price}" + (f"\n[{NO_BRAND_BUTTON}]" if asks_brand(gate) else "")
    if kind == "category":
        dept = (loads(it["facts"]) or {}).get("department")
        return kind, "\n".join([_title(it, iid), CATEGORY_QUESTION,
                                *(taxonomy.path_label(p, dept) for p in gate["ask_category"])])
    return kind, f"{_title(it, iid)}\n{CONDITION_QUESTION if kind == 'condition' else KIDS_QUESTION}"


PRICE_STEP = 10          # WO32b: the card's price buttons go in $10 steps from the suggestion
LOWEST_BUTTON = 5        # ... and none under $5


PRICE_SHIFT = 80         # WO48: "‹ lower · higher ›" moves the eight buttons by this much (two rows of $10 steps)
PRICE_PAGE = "price_page"   # kv: {item: the shift in dollars} — the card's window, kept for its next render


def price_options(price: int, step: int = PRICE_STEP, shift: int = 0) -> list[int]:
    """The card's eight one-tap prices (WO32b, the owner's request — mostly up): $10 below the suggestion, the
    suggestion, then six $10 steps above it; the steps start from the suggestion ($35: 25 · 35 · 45 … 95). No low
    button when $10 below would be under $5: then the suggestion and seven steps up. Any typed number still works.
    WO48: `shift` moves the whole window (a "higher ›" tap: +$80 — $20–$90 becomes $100–$170)."""
    low = price - step
    out = [low] if low >= LOWEST_BUTTON else []
    out += [price + k * step for k in range(0, 8 - len(out))]
    return [p + shift for p in out]


def price_shift(db: DB, iid: str) -> int:
    """The card's price window as the owner left it (WO48): 0, or +$80 a "higher ›" tap."""
    pages = loads(db.kv_get(PRICE_PAGE)) or {}
    try:
        return max(0, int(pages.get(iid) or 0))
    except (TypeError, ValueError):
        return 0


def can_lower(price: int, shift: int) -> bool:
    """Is there a window below this one with no button under $5 (WO48: "‹ lower" is hidden otherwise)?"""
    return shift > 0 and min(price_options(price, shift=shift - PRICE_SHIFT)) >= LOWEST_BUTTON


def shift_prices(db: DB, iid: str, price: int, direction: int) -> int | None:
    """"‹ lower" / "higher ›" (WO48): the window moved by PRICE_SHIFT and kept in kv for the card's next render; None
    when it can't move that way (nothing under $5, never below the first window)."""
    cur = price_shift(db, iid)
    if direction < 0 and not can_lower(price, cur):
        return None
    new = max(0, cur + direction * PRICE_SHIFT)
    pages = loads(db.kv_get(PRICE_PAGE)) or {}
    pages[iid] = new
    db.kv_set(PRICE_PAGE, json.dumps(pages))
    return new


def forget_price_page(db: DB, iid: str) -> None:
    pages = loads(db.kv_get(PRICE_PAGE)) or {}
    if iid in pages:
        del pages[iid]
        db.kv_set(PRICE_PAGE, json.dumps(pages))


def asks_brand(gate: dict) -> bool:
    """Is the brand one of the card's questions (unreadable, or read but unsure)?"""
    return any(q.startswith(("Brand?", "Brand:")) for q in gate.get("questions") or [])


def item_buttons(iid: str, price: int | None, no_brand: bool = False, sizes: list[list[dict]] | None = None,
                 priced: int | None = None, needs_size_tap: bool = False, shift: int = 0) -> list[list[dict]]:
    """WO43: the size rows first (when the card offers sizes) — right above the prices. Then eight prices in two rows
    of four — $10 below, ⭐ the suggestion, $10 steps up (WO32b) — then WO48's "‹ lower · higher ›" (the window moved
    by $80 in place; "‹ lower" only when no button would go under $5) / [No brand] (when the brand is asked, WO25) /
    [Later] [Change] [Wrong photos]; or, once the price is in (`priced`), one inert "✓ $25 — queued" / "✓ $25 — tap a
    size to list it". Every price button sets that price (approve:<item>:<amount>); the shift taps never price
    (prices:<item>:-1|1); [No brand] leaves Poshmark's brand empty (nobrand:<item>); [Wrong photos] reopens the
    batch's grouping (regroup:<item>, WO20b)."""
    rows = list(sizes or [])
    if priced:
        rows.append([{"text": f"✓ ${priced} — " + ("tap a size to list it" if needs_size_tap else "queued"),
                      "callback_data": NOOP}])
        return rows
    if price:
        buttons = [{"text": f"⭐${p}" if p == price else f"${p}", "callback_data": f"approve:{iid}:{p}"}
                   for p in price_options(price, shift=shift)]
        rows += [buttons[:4], buttons[4:]]
        nav = [{"text": "‹ lower", "callback_data": f"prices:{iid}:-1"}] if can_lower(price, shift) else []
        nav.append({"text": "higher ›", "callback_data": f"prices:{iid}:1"})
        rows.append(nav)
    if no_brand:
        rows.append([{"text": NO_BRAND_BUTTON, "callback_data": f"nobrand:{iid}"}])
    rows.append([{"text": "Later", "callback_data": f"later:{iid}"}, {"text": "Change", "callback_data": f"change:{iid}"},
                 {"text": "Wrong photos", "callback_data": f"regroup:{iid}"}])
    return rows


NO_SIZE_LINE = "📏 No size on the photos — tap it below (⭐ = my guess)"
SIZE_OFFERED = ("awaiting_price", "needs_info", "ready")   # the size rows show while the item hasn't started posting


def size_tapped(it) -> bool:
    """The item's size came from the card's size buttons (WO43: the rows stay, with ✓, until it starts posting)."""
    return bool((loads(it["gate"]) or {}).get("size_tapped"))


def mark_size_tapped(db: DB, iid: str) -> None:
    """Noted in the item's gate doc before `pipeline.set_size` (which keeps the doc's other keys)."""
    it = db.item(iid)
    if it is None:
        return
    gate = loads(it["gate"]) or {}
    if not gate.get("size_tapped"):
        db.set_item(iid, gate={**gate, "size_tapped": True})


def card_buttons(db: DB, iid: str, it, price: int | None) -> list[list[dict]]:
    """The card's buttons as the item stands (WO43): the size rows while it has no size (or its size was tapped and it
    hasn't started posting), ✓ on the size tapped; the price rows, or the inert "✓ $X — …" once the price is in."""
    gate = loads(it["gate"]) or {}
    rows = None
    wants = needs_size(it)
    if it["status"] in SIZE_OFFERED and (wants or size_tapped(it)):
        if (o := sizebuttons.offer(db, it)) is not None:
            rows = sizebuttons.buttons(iid, o, it["owner_size"] if it["owner_size"] else None)
    return item_buttons(iid, price, asks_brand(gate), sizes=rows, priced=it["owner_price"] or None,
                        needs_size_tap=wants, shift=price_shift(db, iid))


def sent_before(db: DB, chat_id: str, kind: str, ref: str):
    """The message of this kind about `ref` already sent to this chat (the newest), or None — WO37: every message to
    the group or the private chat goes out ONCE per item and kind, whatever restarts or reprocessing come after."""
    return db.conn.execute("SELECT * FROM outbox WHERE chat_id=? AND kind=? AND ref=? ORDER BY sent_at DESC, "
                           "rowid DESC LIMIT 1", (str(chat_id), kind, ref)).fetchone()


def once(db: DB, bot: Bot, kind: str, ref: str, text: str | None, send) -> int | None:
    """Send `send()` (returns the message id) as THE message of `kind` about `ref` (WO37): recorded in the outbox
    BEFORE it goes, under a placeholder id; the id replaces it once Telegram answers. A send that went out but got no
    answer (a read timeout — live 2026-10-12, three copies of one question) keeps the placeholder: it counts as sent
    and is never sent again (its buttons work by their data; a reply to it reaches the open card). A send that never
    left (no connection) is taken back, to be tried again. None when it was sent before, or may have been."""
    if sent_before(db, bot.chat_id, kind, ref) is not None:
        return None
    placeholder = -int(time.time() * 1000) - random.randint(0, 999)
    db.add_outbox(bot.chat_id, placeholder, kind, ref, text=text)
    try:
        mid = send()
    except RuntimeError as e:
        if maybe_delivered(e):
            db.log(ref, "telegram_maybe_sent", {"kind": kind, "error": str(e)[:200]})
            return None
        db.conn.execute("DELETE FROM outbox WHERE chat_id=? AND message_id=?", (str(bot.chat_id), placeholder))
        raise
    db.conn.execute("UPDATE outbox SET message_id=? WHERE chat_id=? AND message_id=?",
                    (int(mid), str(bot.chat_id), placeholder))
    return int(mid)


def refresh_card(s: Settings, db: DB, iid: str, reopen: bool = False) -> bool:
    """The item's price card as it is now — caption, buttons, cover photo — EDITED in place (WO37: never a second card;
    "the price card always shows the chosen cover"). `reopen`: it waits for the owner again (the queue's open message).
    An answered card keeps one inert "✓ $X" button. False when there is no card to edit (none sent, or sent without an
    answer from Telegram)."""
    it = db.item(iid)
    bot = card_bot(s, it)
    if bot is None or it is None:
        return False
    row = sent_before(db, bot.chat_id, "item", iid)
    if row is None:
        return False
    if reopen:
        db.conn.execute("UPDATE outbox SET resolved_at=NULL WHERE chat_id=? AND message_id=?",
                        (row["chat_id"], row["message_id"]))
    if int(row["message_id"]) < 0:
        return False
    caption, price = item_caption(iid, it)
    waiting = reopen or row["resolved_at"] is None
    sizing = it["status"] in SIZE_OFFERED and (size_tapped(it) or needs_size(it))   # WO43: the size rows stay
    buttons = (card_buttons(db, iid, it, price) if waiting or sizing else
               [[{"text": f"\u2713 ${it['owner_price']}" if it["owner_price"] else "\u2713", "callback_data": NOOP}]])
    cover = Path(it["dir"]) / "cover.jpg"
    try:
        bot.edit_card(int(row["message_id"]), caption, buttons, photo=cover if cover.is_file() else None)
    except RuntimeError as e:
        db.log(iid, "card_edit_failed", str(e)[:200])
        return False
    db.conn.execute("UPDATE outbox SET text=? WHERE chat_id=? AND message_id=?",
                    (caption, row["chat_id"], row["message_id"]))
    return True


def send_item(s: Settings, db: DB, iid: str) -> None:
    """The item's price card: cover photo, item_caption, item_buttons. Records the outbox row. Without a bot: print
    (dev)."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    from thrift_agent import photomem                   # WO43's second guard: never a card for photos that had one
    if why := photomem.card_blocked(db, iid):
        db.log(iid, "card_blocked", why)
        if it["status"] != "dropped":
            db.set_item(iid, status="dropped")
            db.log(iid, "dropped", {"why": f"card blocked: {why}"})
        notify.say(f"🧹 No card for {_title(it, iid)}: {why} — the item is retired")
        return
    caption, price = item_caption(iid, it)
    cover = Path(it["dir"]) / "cover.jpg"
    test = bool(it["test"]) if "test" in it.keys() else False
    bot = (ops_bot_for(s) or bot_for(s)) if test else bot_for(s)   # WO45 §2: a test share's card to the private chat
    if test:
        caption = f"🧪 TEST — nothing will be listed\n{caption}"
    if bot is None:
        text = f"{caption}\n(thrift price {iid} {price or '<amount>'})"
        notify.group_photo(cover, text) if cover.is_file() else notify.group(text)
        return
    buttons = card_buttons(db, iid, it, price)
    from thrift_agent import shares                     # WO45 §3: the share's ledger sees the card
    if sent_before(db, bot.chat_id, "item", iid) is not None:   # WO37: the card that went out is edited, never resent
        refresh_card(s, db, iid, reopen=True)
        shares.batch_carded(db, it["batch_id"])
        return
    once(db, bot, "item", iid, caption,
         lambda: bot.send_photo(cover, caption, buttons) if cover.is_file() and len(caption) <= MAX_CAPTION
         else bot.send_message(caption, buttons))
    shares.batch_carded(db, it["batch_id"])


def _ask(s: Settings, db: DB, iid: str, kind: str, question: str, buttons: list[list[dict]], cli: str) -> None:
    """A question about one item before its card: cover photo, title, the question, its buttons. Records the outbox
    row. Without a bot: print with the CLI twin (dev)."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    caption = f"{_title(it, iid)}\n{question}"
    cover = Path(it["dir"]) / "cover.jpg"
    bot = bot_for(s)
    if bot is None:
        text = f"{caption}\n({cli})"
        notify.group_photo(cover, text) if cover.is_file() else notify.group(text)
        return
    if (row := sent_before(db, bot.chat_id, kind, iid)) is not None:     # WO37: asked once, ever
        db.conn.execute("UPDATE outbox SET resolved_at=NULL WHERE chat_id=? AND message_id=?",
                        (row["chat_id"], row["message_id"]))
        return
    once(db, bot, kind, iid, caption,
         lambda: bot.send_photo(cover, caption, buttons) if cover.is_file() else bot.send_message(caption, buttons))


def condition_buttons(iid: str) -> list[list[dict]]:
    return [[{"text": label, "callback_data": f"cond:{iid}:{choice}"} for label, choice in CONDITION_BUTTONS]]


def ask_condition(s: Settings, db: DB, iid: str) -> None:
    """Shoes in doubt between brand new and worn (pipeline.shoe_condition_doubt), before the price card:
    "Brand new or worn? (couldn't tell from the photos)" [NWT] [Like New] [Good]."""
    _ask(s, db, iid, "condition", CONDITION_QUESTION, condition_buttons(iid),
         f"thrift condition {iid} nwt|like_new|good")


def category_buttons(iid: str, options: list[dict], department: str | None) -> list[list[dict]]:
    """One button per real Poshmark path, e.g. [Skirts › Skirt Sets] [Shorts] (cat:<item>:<n>)."""
    return [[{"text": taxonomy.path_label(p, department), "callback_data": f"cat:{iid}:{n}"}]
            for n, p in enumerate(options)]


def ask_category(s: Settings, db: DB, iid: str) -> None:
    """The model wasn't 0.70 sure of the category, or gave one Poshmark doesn't have (WO25): "Which category?" with
    1-3 real paths as buttons, before Girls/Boys and the price card (the price and the size menu follow it)."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    options = (loads(it["gate"]) or {}).get("ask_category") or []
    dept = (loads(it["facts"]) or {}).get("department")
    _ask(s, db, iid, "category", CATEGORY_QUESTION, category_buttons(iid, options, dept),
         f'thrift category {iid} "<category › subcategory>"')


def kids_buttons(iid: str) -> list[list[dict]]:
    return [[{"text": label, "callback_data": f"kids:{iid}:{choice}"} for label, choice in KIDS_BUTTONS]]


def ask_kids(s: Settings, db: DB, iid: str) -> None:
    """A kids item the model wasn't 0.70 sure was for girls or boys (pipeline.kids_question), before the price card:
    "Girls or Boys?" [Girls] [Boys]."""
    _ask(s, db, iid, "kids", KIDS_QUESTION, kids_buttons(iid), f"thrift kids {iid} girls|boys")


def announce(s: Settings, text: str) -> bool:
    """A line to the OPS chat when the owner acts from the CLI (thrift price / answer / confirm …): WO29, the group
    hears none of it. Best effort: True when sent."""
    return notify.ops(text, once=False)


# ---------- incoming ----------

def handle_update(s: Settings, db: DB, bot: Bot, update: dict) -> str:
    """Route one Telegram update (a reply, a button press, a number typed on its own) to the pipeline, then send the
    next question (pump). Returns a short description of what happened (for the log); unauthorised or unrelated
    updates are ignored."""
    if not bot.authorized(update):
        if _from_ops_chat(s, bot, update):
            if "callback_query" in update:                 # WO45 §2: a tap on a 🧪 test card in the private chat
                ops = ops_bot_for(s)
                return _handle_callback(s, db, ops, update["callback_query"]) if ops else "ignored: no ops chat"
            return _route_ops(s, db, update)               # WO33: a reply there is enough too
        return "ignored: unauthorized"
    result = _route(s, db, bot, update)
    if not result.startswith("ignored"):
        pump(s, db)
    return result


def card_bot(s: Settings, it) -> Bot | None:
    """The bot that holds the item's card: the group's, or the private chat's for a test share's item (WO45 §2)."""
    if it is not None and "test" in it.keys() and it["test"]:
        return ops_bot_for(s) or bot_for(s)
    return bot_for(s)


UNMATCHED_LINE = re.compile(r"Unmatched sale on (\w+) \((s_[0-9a-f]+)\)")


def _from_ops_chat(s: Settings, bot: Bot, update: dict) -> bool:
    """A message — or a button tap (WO45 §2: a 🧪 test card) — in the ops chat (the owner's private chat with the
    bot) from an allowed user."""
    msg = update.get("message") if isinstance(update, dict) else None
    cq = update.get("callback_query") if isinstance(update, dict) else None
    chat = notify.ops_chat(s)
    if chat is None:
        return False
    if isinstance(cq, dict):
        uid = (cq.get("from") or {}).get("id")
        return (str(((cq.get("message") or {}).get("chat") or {}).get("id")) == str(chat) and isinstance(uid, int)
                and uid in bot.allowed_users)
    if not isinstance(msg, dict):
        return False
    uid = (msg.get("from") or {}).get("id")
    return str((msg.get("chat") or {}).get("id")) == str(chat) and isinstance(uid, int) and uid in bot.allowed_users


def _route_ops(s: Settings, db: DB, update: dict) -> str:
    """The owner's reply in the ops chat (WO33: no message may need a terminal command). A reply to "Unmatched sale on
    <Site> (s_…)" with words from an item's title matches the sale to that item (thrift-api: its take-downs, as for a
    matched sale email); anything else gets a short hint. Always an answer."""
    from thrift_agent import sales
    msg = update["message"]
    bot, mid = ops_bot_for(s), msg.get("message_id")
    text = (msg.get("text") or "").strip()
    quoted = ((msg.get("reply_to_message") or {}).get("text") or "")
    if bot is None:
        return "ignored: no ops chat"
    reply = msg.get("reply_to_message")
    if isinstance(reply, dict) and reply.get("message_id") is not None:   # WO35: "⚠️ … can't confirm" lives here now
        row = db.outbox_lookup(msg["chat"]["id"], reply["message_id"])
        if row is not None and row["kind"] == "unconfirmed":
            return _reply_unconfirmed(s, db, bot, row["ref"], text, mid)
        if row is not None and row["kind"] in ("refused", "skipped"):     # WO39: a refused form — 'retry'
            return _reply_skipped(s, db, bot, row["ref"], text, mid, kind=row["kind"])
    if text.lower().strip(" /!.?") == "doctor":       # WO49: the health check, on the phone
        from thrift_agent import doctor
        bot.send_message(doctor.text(s, db), reply_to=mid)
        return "ops: doctor"
    if re.fullmatch(r"go\s+titles?", text.lower().strip(" /!.?")):     # WO50: the live titles' review, applied
        from thrift_agent import titles
        rows = titles.apply_review(s, db)
        bot.send_message(f"✓ {len(rows)} title change{'s' if len(rows) != 1 else ''} queued — each site's edit page, "
                         "nothing else on the listings; a line here as each one is live" if rows else
                         "Nothing to change: no reviewed title is waiting (send `thrift titles --review` again)",
                         reply_to=mid)
        return f"ops: go titles ({len(rows)})"
    m = UNMATCHED_LINE.search(quoted)
    if m is None or not text:
        bot.send_message("Replies here act on an 'Unmatched sale' message (reply with words from the item's title) or "
                         "a '⚠️ … can't confirm' one ('posted <link>' or 'retry'); 'doctor' shows the agent's health",
                         reply_to=mid)
        return "ops: hint"
    sale_id, words = m[2], [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1]
    hits = []
    for it in db.conn.execute("SELECT id, renders FROM items WHERE renders IS NOT NULL"):
        posh = (loads(it["renders"]) or {}).get("poshmark") or {}
        title = posh.get("title") or ""
        known = f"{title} {posh.get('brand_label') or ''}".lower()    # WO36: "mng" finds the Mango cardigan too
        if words and all(w in known for w in words):
            hits.append((it["id"], title))
    if len(hits) != 1:
        bot.send_message("No item's title has all of those words — try others" if not hits else
                         f"{len(hits)} items match: " + "; ".join(t for _, t in hits[:5]) + " — add a word",
                         reply_to=mid)
        return f"ops: {len(hits)} items for {words}"
    iid, title = hits[0]
    client = sales.api()
    if client is None:
        bot.send_message("Sales tracking isn't set up on the Mac", reply_to=mid)
        return "ops: no API"
    try:
        out = client.post(f"/sales/{sale_id}/match", {"item_id": iid})
    except sales.ApiError as e:
        bot.send_message("The sales tracker didn't answer — reply again in a minute", reply_to=mid)
        return f"ops: match {sale_id} failed: {e}"
    downs = len(out.get("tasks") or [])
    bot.send_message(f"✓ Matched to {title}" + (f" — {downs} take-down{'s' if downs != 1 else ''} queued; they run "
                                                f"before the next listing on each site" if downs else
                                                " — nothing to take down (history, or not listed elsewhere)"),
                     reply_to=mid)
    return f"ops: matched {sale_id} to {iid}"


def _route(s: Settings, db: DB, bot: Bot, update: dict) -> str:
    if isinstance(update.get("callback_query"), dict):
        return _handle_callback(s, db, bot, update["callback_query"])
    msg = update.get("message")
    if not isinstance(msg, dict):
        return "ignored: unsupported update"
    token = _REPLYING.set(True)                        # a message from the owner: it gets its answer (WO33)
    watch = _Answered(bot, msg.get("message_id"))
    try:
        result = _route_message(s, db, watch, msg)
    finally:
        _REPLYING.reset(token)
    if not result.startswith("ignored") and not watch.answered and msg.get("message_id") is not None:
        _fallback_answer(s, db, bot, msg)                # a handler said nothing: never silence (WO33)
    return result


class _Answered:
    """The bot, noting whether the owner's message got a reply — an answer, a hint or an error (WO33)."""

    def __init__(self, bot: Bot, mid: int | None):
        self._bot, self._mid, self.answered = bot, mid, False

    def __getattr__(self, name):
        return getattr(self._bot, name)

    def send_message(self, text: str, buttons: list[list[dict]] | None = None, reply_to: int | None = None) -> int:
        if reply_to is not None and reply_to == self._mid:
            self.answered = True
        return self._bot.send_message(text, buttons, reply_to=reply_to)


def _fallback_answer(s: Settings, db: DB, bot: Bot, msg: dict) -> None:
    """"✓ Got it — <what happens next>" for a reply no handler answered: the item's next step, a batch's cards."""
    from thrift_agent import crosslist
    reply = msg.get("reply_to_message")
    row = db.outbox_lookup(msg["chat"]["id"], reply["message_id"]) if isinstance(reply, dict) else open_message(db)
    if row is None:
        return
    if row["kind"] in ("batch", "regroup"):
        nxt = "its cards follow"
    else:
        nxt = next_step(s, db, crosslist.split_ref(row["ref"])[0])
    bot.send_message(f"✓ Got it — {nxt}", reply_to=msg["message_id"])


def _route_message(s: Settings, db: DB, bot: Bot, msg: dict) -> str:
    text = (msg.get("text") or msg.get("caption") or "").strip()
    mid = msg.get("message_id")
    reply = msg.get("reply_to_message")
    if not isinstance(reply, dict):
        return _typed(s, db, bot, text, mid)
    row = db.outbox_lookup(msg["chat"]["id"], reply["message_id"])
    if row is None:
        return "ignored: not a reply to the bot"
    kind, ref = row["kind"], row["ref"]
    if kind not in ("batch", "regroup") and _no_drop_reply(text):
        return _reply_no_drop(db, bot, ref, mid)
    if kind == "batch":
        return _reply_batch(s, db, bot, ref, text, mid)
    if kind == "regroup":
        return _reply_regroup(s, db, bot, ref, text, mid)
    if kind == "item":
        return _reply_item(s, db, bot, ref, text, mid)
    if kind == "owner_q":
        return _reply_owner_q(s, db, bot, ref, text, mid)
    if kind == "condition":
        if (choice := parse_condition(text)) is None:
            bot.send_message(CONDITION_HINT, reply_to=mid)
            return f"condition {ref}: unreadable reply {text!r}"
        return _set_condition(s, db, bot, ref, choice, mid)
    if kind == "kids":
        if (choice := parse_kids(text)) is None:
            bot.send_message(KIDS_HINT, reply_to=mid)
            return f"kids {ref}: unreadable reply {text!r}"
        return _set_kids(s, db, bot, ref, choice, mid)
    if kind == "category":
        return _reply_category(s, db, bot, ref, text, mid)
    if kind == "size":
        return _reply_size(s, db, bot, ref, text, mid)
    if kind == "brand":
        return _reply_brand(s, db, bot, ref, text, mid)
    if kind == "unconfirmed":
        return _reply_unconfirmed(s, db, bot, ref, text, mid)
    if kind == "skipped":
        return _reply_skipped(s, db, bot, ref, text, mid)
    if kind == "posted":                                  # WO45 follow-up: "price 80" on the ✅ card
        return _reply_posted(s, db, bot, ref, text, mid)
    return f"ignored: unknown outbox kind {kind}"


PRICE_ONLY = re.compile(r"^\s*(?:price|list)?\s*\$?\s*(\d{1,5}(?:\.\d{1,2})?)\s*(?:dollars|usd)?\s*[.!]?\s*$", re.I)
POSTED_HINT = ("Reply 'price 80' here to change the price on every site, or 'no drop' to keep the automatic drops "
               "off this item")


def _reply_posted(s: Settings, db: DB, bot: Bot, iid: str, text: str, mid: int | None) -> str:
    """A reply to an item's ✅ card: "price 80" (or a bare number) changes the price on every site it is on."""
    if m := PRICE_ONLY.match(text or ""):
        return _owner_price(s, db, bot, iid, int(round(float(m[1]))), mid)
    bot.send_message(POSTED_HINT, reply_to=mid)
    return f"posted {iid}: unreadable reply {text!r}"


def _owner_price(s: Settings, db: DB, bot: Bot, iid: str, amount: int, reply_to: int | None) -> str:
    """WO45 follow-up (the owner): her price for a live item, changed on every site through the price-edit path
    (pricedrop.owner_price) and recorded; one answer at once, one private line when the sites show it."""
    from thrift_agent import crosslist, pricedrop
    try:
        rows = pricedrop.owner_price(db, iid, amount)
    except ValueError as e:
        _tell(bot, reply_to, str(e))
        return f"owner price {iid}: rejected ${amount}: {e}"
    sites = ", ".join(crosslist.LABEL.get(r.split("_")[-3], r.split("_")[-3]) for r in rows)
    _tell(bot, reply_to, f"✓ ${amount} — the price changes on {sites} in a minute")
    return f"owner price {iid}: ${amount} on {sites}"


def _no_drop_reply(text: str) -> bool:
    from thrift_agent import pricedrop
    return bool(pricedrop.NO_DROP.match(text or ""))


def _reply_no_drop(db: DB, bot: Bot, ref: str, mid: int | None) -> str:
    """WO41: 'no drop' replied to an item's card (or any message of the item): its price never drops automatically."""
    from thrift_agent import crosslist, pricedrop
    iid = crosslist.split_ref(ref)[0]
    if db.item(iid) is None:
        bot.send_message("I can't tell which item that is — reply 'no drop' to the item's own card", reply_to=mid)
        return f"no drop: unknown item {ref}"
    called_off = pricedrop.exclude(db, iid)
    bot.send_message(f"✓ No price drops for {crosslist.item_name(db, iid)} — its price stays as it is"
                     + (" (the drop under way is called off)" if called_off else ""), reply_to=mid)
    db.log(iid, "price_drop_excluded", {"by": "reply"})
    return f"no drop {iid}"


def _reply_skipped(s: Settings, db: DB, bot: Bot, ref: str, text: str, mid: int | None, kind: str = "skipped") -> str:
    """A listing the poster skipped (WO33: a reply settles it, never a command): 'retry' puts it back in line."""
    from thrift_agent import crosslist
    iid, mp = crosslist.split_ref(ref)
    site = crosslist.LABEL.get(mp, mp)
    if not RETRY_CMD.match(text or ""):
        bot.send_message(f"Reply 'retry' to try it on {site} again", reply_to=mid)
        return f"skipped {iid}: unreadable reply {text!r}"
    try:
        pipeline.requeue(s, db, iid, mp)
    except ValueError as e:
        _tell(bot, mid, str(e))
        return f"skipped {iid}: retry rejected: {e}"
    _ack(db, bot, kind, ref, "✓ retry — back in line")
    db.outbox_resolve(kind, ref)
    bot.send_message(f"✓ Retry — it goes back in line on {site}, {when_posting(s)}", reply_to=mid)
    return f"skipped {iid}: retry on {mp}"


def _reply_unconfirmed(s: Settings, db: DB, bot: Bot, ref: str, text: str, mid: int | None) -> str:
    """A listing that may be live (the Mac slept while publishing, or its address wasn't found; WO28 §3). "posted
    <url>": queued for the poster, which opens the page between listings and records it (✅ confirmed live). "retry":
    the owner looked and it isn't there — it goes back in line. The CLI twins: `thrift mark-posted`, `thrift retry`.
    WO30: the message's ref names the marketplace when it isn't Poshmark ("<item>:depop")."""
    from thrift_agent import crosslist, daily
    iid, mp = crosslist.split_ref(ref)
    site = crosslist.LABEL.get(mp, mp)
    row = db.listing(iid, mp)
    if row is not None and row["status"] == "posted":   # settled already (WO35: a late 'retry' does nothing)
        db.outbox_resolve("unconfirmed", ref)
        bot.send_message(f"✓ Already settled — it's live on {site}" + (f": {row['url']}" if row["url"] else ""),
                         reply_to=mid)
        return f"unconfirmed {iid}: already posted"
    if m := POSTED_CMD.match(text or ""):
        try:
            address = pipeline.request_posted(s, db, iid, m[1], mp)
        except ValueError as e:
            _tell(bot, mid, f"That isn't a {site} listing link for this item — open the listing, copy its link and "
                            "reply 'posted <link>' again" if "listing address" in str(e) else str(e))
            return f"unconfirmed {iid}: rejected {m[1]!r}: {e}"
        _ack(db, bot, "unconfirmed", ref, "✓ link received — checking it")
        if not daily.poster_now(db).running:
            notify.say(f"{iid}: 'posted {address}' queued — the poster isn't running, it checks when it starts")
        bot.send_message(f"✓ Link received — I check it on {site} between listings and say when it's confirmed",
                         reply_to=mid)
        return f"unconfirmed {iid}: posted {address} (queued for the poster)"
    if RETRY_CMD.match(text or ""):
        try:
            pipeline.retry_unconfirmed(s, db, iid, mp)
        except ValueError as e:
            _tell(bot, mid, str(e))
            return f"unconfirmed {iid}: retry rejected: {e}"
        _ack(db, bot, "unconfirmed", ref, "✓ retry — it goes back in line")
        bot.send_message(f"✓ Retry — it goes back in line on {site}, {when_posting(s)}", reply_to=mid)
        return f"unconfirmed {iid}: retry"
    bot.send_message(UNCONFIRMED_HINT, reply_to=mid)
    return f"unconfirmed {iid}: unreadable reply {text!r}"


def _shipped(db: DB, bot: Bot, words: str, mid: int | None) -> str:
    """The owner's "shipped <title words>" (WO33 E2): to thrift-api, which marks the open sale whose title has those
    words shipped (no more reminders) and says "✓ Marked shipped: <title>" in the group. The API away: kept and sent
    when it answers again."""
    from thrift_agent import sales
    client = sales.api()
    body = {"kind": "shipped", "words": words}
    if client is None:
        bot.send_message("Sales tracking isn't set up on the Mac yet — mark it shipped in the site's app", reply_to=mid)
        return "shipped: no API"
    try:
        out = client.post("/mac-event", body)
    except sales.ApiError:
        sales.queue(db, "/mac-event", body)
        bot.send_message("Got it — it's marked shipped as soon as the Mac reaches the sales tracker again", reply_to=mid)
        return "shipped: kept for later"
    if not (out.get("matched") or out.get("sale_id")):
        bot.send_message(f"No open sale matches \"{words}\" — try words from its title", reply_to=mid)
        return f"shipped: no match for {words!r}"
    return f"shipped: {out.get('matched') or out.get('sale_id')}"


def _typed(s: Settings, db: DB, bot: Bot, text: str, mid: int | None) -> str:
    """A message that replies to nothing. A plain number is the price of the open card (WO20), "cover 2" its cover
    (WO23); anything else is the owners' own chat and is ignored. A typed number under the floor is not taken (a stray
    "2" in the chat must not price an item at $2): a reply to the card, or `thrift price`, sets it."""
    if m := SHIPPED_CMD.match(text or ""):
        return _shipped(db, bot, m[1], mid)
    if m := COVER_CMD.match(text or ""):
        row = open_message(db)
        if row is None or row["kind"] != "item":
            bot.send_message("No price card is open", reply_to=mid)
            return f"typed {text!r}: no price card open"
        return _set_cover(s, db, bot, row["ref"], cover_index(m[1]), mid)
    if pipeline.NO_BRAND_WORDS.fullmatch((text or "").strip(" .!")):
        row = open_message(db)
        if row is None or row["kind"] != "item":
            return "ignored: not a reply to the bot"
        return _set_no_brand(s, db, bot, row["ref"], mid)
    if CONDITION_CMD.match(text or "") or CATEGORY_CMD.match(text or ""):   # WO37: typed while a card is open
        row = open_message(db)
        if row is None or row["kind"] != "item":
            bot.send_message("No price card is open", reply_to=mid)
            return f"typed {text!r}: no price card open"
        return _correction(s, db, bot, row["ref"], text, None, mid) or "ignored"
    amount = plain_price(text)
    if amount is None and size_reply(text) is not None and SIZE_SAID.match(text or ""):
        # WO39: "size S" typed on its own — which card? never guessed (two bras in a row): she replies to the card
        bot.send_message("Which item? Reply 'size …' to its card (swipe the card to reply)", reply_to=mid)
        return f"typed {text!r}: a size needs a reply to its card"
    if amount is None:
        return "ignored: not a reply to the bot"
    row = open_message(db)
    if row is None or row["kind"] != "item":
        bot.send_message("No price card is open" + (" — answer the question above first" if row is not None else ""),
                         reply_to=mid)
        return f"typed ${amount}: no price card open"
    it = db.item(row["ref"])
    floor = max(int(s["pricing"]["floor"]), note_floor(it["note"] if it else None) or 0)
    if amount < floor:
        bot.send_message(f"${amount} is under the ${floor} floor — reply to the card with {amount} if you mean it",
                         reply_to=mid)
        return f"typed ${amount}: under the floor, not taken"
    return _set_price(s, db, bot, row["ref"], amount, mid)


def _set_price(s: Settings, db: DB, bot: Bot, iid: str, amount: int, reply_to: int | None) -> str:
    """The owner's price (a price button, a reply, a typed number), then the one-line confirmation."""
    try:
        status = pipeline.set_price(s, db, iid, amount)
    except ValueError as e:
        _tell(bot, reply_to, str(e))
        return f"price {iid}: rejected ${amount}: {e}"
    forget_price_page(db, iid)                         # WO48: the window starts afresh for any later card
    if status in OWNER_WAITING:                        # still open: a re-share hold, or Girls/Boys not answered yet
        gate = loads(db.item(iid)["gate"]) or {}
        _ack(db, bot, "item", iid, f"✓ ${amount} recorded")
        if gate.get("hold"):                           # a question only the owner can settle: said plainly
            _tell(bot, reply_to, f"${amount} recorded{HELD_HINT}")
    elif needs_size(db.item(iid)):
        # WO39: an item with no size isn't queued anywhere (the queue moves on); WO43: the card keeps its size rows
        # and one inert "✓ $25 — tap a size to list it" — said once, on the card itself
        db.outbox_resolve("item", iid)
        if not refresh_card(s, db, iid):
            _ack(db, bot, "item", iid, f"✓ ${amount} — {NEEDS_SIZE}")
    elif needs_brand(db.item(iid)):
        # Alex, 2026-10-27: the brand couldn't be read — nothing goes up until her reply (Depop and Vinted need one);
        # the card says so and ONE message asks (ask_brands, right away)
        db.outbox_resolve("item", iid)
        _ack(db, bot, "item", iid, f"✓ ${amount} — {NEEDS_BRAND}")
        try:
            ask_brands(s, db)
        except Exception as e:  # noqa: BLE001 — the window's tick asks again
            db.log(iid, "error", f"brand message: {type(e).__name__}: {e}")
    else:
        db.outbox_resolve("item", iid)
        # WO29: no "✓ $X — N left" message: said on the button (the size rows stay when the size was tapped, WO43)
        if not (size_tapped(db.item(iid)) and refresh_card(s, db, iid)):
            _ack(db, bot, "item", iid, f"✓ ${amount} — queued")
    if not (status in OWNER_WAITING and (loads(db.item(iid)["gate"]) or {}).get("hold")):
        _confirm(s, db, bot, reply_to, iid, f"${amount}")          # WO33: a reply always gets its answer
    return f"price {iid}: ${amount} ({status})"


def _set_condition(s: Settings, db: DB, bot: Bot, iid: str, choice: str, reply_to: int | None) -> str:
    try:
        pipeline.set_condition(s, db, iid, choice)
    except ValueError as e:
        _tell(bot, reply_to, str(e))
        return f"condition {iid}: rejected {choice!r}: {e}"
    _ack(db, bot, "condition", iid, f"✓ {CONDITION_TAPPED[choice]}")
    db.outbox_resolve("condition", iid)
    _confirm(s, db, bot, reply_to, iid, CONDITION_TAPPED[choice])
    return f"condition {iid}: {choice}"


def _set_kids(s: Settings, db: DB, bot: Bot, iid: str, choice: str, reply_to: int | None) -> str:
    try:
        status = pipeline.set_kids_gender(s, db, iid, choice)
    except ValueError as e:
        _tell(bot, reply_to, str(e))
        return f"kids {iid}: rejected {choice!r}: {e}"
    _ack(db, bot, "kids", iid, f"✓ {choice.title()}")
    db.outbox_resolve("kids", iid)
    _confirm(s, db, bot, reply_to, iid, choice.title())
    return f"kids {iid}: {choice} ({status})"


def _handle_callback(s: Settings, db: DB, bot: Bot, cq: dict) -> str:
    cid = cq.get("id")
    parts = (cq.get("data") or "").split(":")
    src_mid = (cq.get("message") or {}).get("message_id")
    if parts[0] == NOOP:                               # an answered card's "✓ …" button
        bot.answer_callback(cid)
        return "ignored: answered card"
    if parts[0] == "approve" and len(parts) == 3 and parts[2].isdigit():
        result = _set_price(s, db, bot, parts[1], int(parts[2]), src_mid)
        bot.answer_callback(cid, f"${parts[2]}" if "rejected" not in result else "Could not set the price")
        return result
    if parts[0] == "size" and len(parts) == 3 and parts[2]:         # WO43: a size tapped on the card
        result = _tap_size(s, db, bot, parts[1], parts[2])
        bot.answer_callback(cid, f"Size {parts[2]}" if "rejected" not in result else "Can't change the size now")
        return result
    if parts[0] == "prices" and len(parts) == 3 and parts[2] in ("-1", "1"):  # WO48: ‹ lower · higher ›
        it = db.item(parts[1])
        price = item_caption(parts[1], it)[1] if it is not None else None
        if it is None or it["owner_price"] or it["status"] not in SIZE_OFFERED or not price:
            bot.answer_callback(cid, "Nothing to shift")
            return f"prices {parts[1]}: nothing to shift"
        if shift_prices(db, parts[1], int(price), int(parts[2])) is None:
            bot.answer_callback(cid, "No lower prices")
            return f"prices {parts[1]}: at the bottom"
        refresh_card(s, db, parts[1])
        bot.answer_callback(cid)
        return f"prices {parts[1]}: shifted {parts[2]}"
    if parts[0] == "sizes" and len(parts) == 3 and parts[2] in ("-1", "1"):   # ‹ smaller · bigger ›
        it = db.item(parts[1])
        o = sizebuttons.offer(db, it) if it is not None and it["status"] in SIZE_OFFERED else None
        if o is None:
            bot.answer_callback(cid, "No sizes to show")
            return f"sizes {parts[1]}: nothing to shift"
        sizebuttons.shift(db, parts[1], o, int(parts[2]))
        refresh_card(s, db, parts[1])
        bot.answer_callback(cid)
        return f"sizes {parts[1]}: shifted {parts[2]}"
    if parts[0] == "later" and len(parts) == 2:
        try:
            pipeline.defer_item(s, db, parts[1])
        except ValueError as e:
            bot.answer_callback(cid, "Nothing to put off")
            return f"later {parts[1]}: rejected: {e}"
        _ack(db, bot, "item", parts[1], "✓ later — it comes back at the end")
        db.outbox_resolve("item", parts[1])
        bot.answer_callback(cid, "Later: moved to the end")
        return f"later {parts[1]}"
    if parts[0] == "cond" and len(parts) == 3 and parts[2] in CONDITION_TAPPED:
        result = _set_condition(s, db, bot, parts[1], parts[2], src_mid)
        bot.answer_callback(cid, CONDITION_TAPPED[parts[2]] if "rejected" not in result else "Could not set it")
        return result
    if parts[0] == "cat" and len(parts) == 3 and parts[2].isdigit():
        result = _set_category_option(s, db, bot, parts[1], int(parts[2]), src_mid)
        bot.answer_callback(cid, "Got it" if "rejected" not in result else "Could not set it")
        return result
    if parts[0] == "nobrand" and len(parts) == 2:
        result = _set_no_brand(s, db, bot, parts[1], src_mid)
        bot.answer_callback(cid, NO_BRAND_BUTTON if "rejected" not in result else "Could not set it")
        return result
    if parts[0] == "kids" and len(parts) == 3 and parts[2] in ("girls", "boys"):
        result = _set_kids(s, db, bot, parts[1], parts[2], src_mid)
        bot.answer_callback(cid, parts[2].title() if "rejected" not in result else "Could not set it")
        return result
    if parts[0] == "regroup" and len(parts) == 2:
        try:
            bid = pipeline.start_regroup(s, db, parts[1])
        except ValueError as e:
            bot.answer_callback(cid, "Can't change its photos")
            _tell(bot, src_mid, str(e))
            return f"regroup {parts[1]}: rejected: {e}"
        bot.answer_callback(cid, "The batch's photos follow")
        return f"regroup {parts[1]}: batch {bid} reopened"
    if parts[0] == "change" and len(parts) == 2:
        iid = parts[1]
        mid = bot.send_message("Reply to this message with the price (or just type it).", reply_to=src_mid)
        db.add_outbox(bot.chat_id, mid, "item", iid)
        bot.answer_callback(cid)
        return f"change {iid}: asked for the price"
    bot.answer_callback(cid, "Unknown button")
    return f"ignored: unknown callback {cq.get('data')!r}"


def _reply_batch(s: Settings, db: DB, bot: Bot, bid: str, text: str, mid: int | None) -> str:
    if not text:
        bot.send_message(BATCH_HINT, reply_to=mid)
        return f"batch {bid}: empty reply"
    try:
        pipeline.confirm(s, db, bid, text)
    except ValueError as e:
        _tell(bot, mid, str(e))
        return f"batch {bid}: rejected {text!r}: {e}"
    n = db.conn.execute("SELECT COUNT(*) FROM items WHERE batch_id=?", (bid,)).fetchone()[0]
    _ack(db, bot, "batch", bid, f"✓ {n} item{'s' if n != 1 else ''} — the cards follow")
    db.outbox_resolve("batch", bid)
    return f"batch {bid}: confirmed {text!r}"


def _reply_regroup(s: Settings, db: DB, bot: Bot, bid: str, text: str, mid: int | None) -> str:
    if not text:
        bot.send_message(REGROUP_HINT, reply_to=mid)
        return f"regroup {bid}: empty reply"
    try:
        out = pipeline.regroup(s, db, bid, text)
    except ValueError as e:
        _tell(bot, mid, str(e))
        return f"regroup {bid}: rejected {text!r}: {e}"
    changed = len(out["rebuilt"]) + len(out["created"])
    _ack(db, bot, "regroup", bid, "✓ no change — the cards follow" if not changed and not out["removed"] else
         f"✓ {changed} item{'s' if changed != 1 else ''} rebuilt"
         + (f", {len(out['removed'])} removed" if out["removed"] else "") + " — the cards follow")
    db.outbox_resolve("regroup", bid)
    notify.say(f"batch {bid}: regrouped ({text!r}) — " + ", ".join(f"{k} {len(v)}" for k, v in out.items()))
    return f"regroup {bid}: {text!r} -> " + ", ".join(f"{k} {len(v)}" for k, v in out.items())


def _set_cover(s: Settings, db: DB, bot: Bot, iid: str, n: int, reply_to: int | None) -> str:
    """The owner's "cover N" (n counted from 0 here; she counts from 1, WO37): the item's listing is rebuilt with that
    photo first and its card EDITED to show it — never sent again — and her reply answered."""
    try:
        status = pipeline.set_cover(s, db, iid, n)
    except ValueError as e:
        _tell(bot, reply_to, str(e))
        return f"cover {iid}: rejected {n}: {e}"
    refresh_card(s, db, iid)                           # "the price card always shows the chosen cover" (WO37)
    _confirm(s, db, bot, reply_to, iid, f"Cover: photo {n + 1}")
    return f"cover {iid}: photo {n} ({status})"


def _set_no_brand(s: Settings, db: DB, bot: Bot, iid: str, reply_to: int | None) -> str:
    """The owner's [No brand] (WO25): Poshmark's brand stays empty and the brand question goes. A card still waiting
    for its price stays as it is (its price buttons still work); a listing that named the model's guess is rewritten
    first, and its card follows."""
    try:
        status = pipeline.set_no_brand(s, db, iid)
    except ValueError as e:
        _tell(bot, reply_to, str(e))
        return f"nobrand {iid}: rejected: {e}"
    if status not in OWNER_WAITING:                    # a card still open for its price stays as it is
        _ack(db, bot, "item", iid, "✓ No brand" + (" — its card follows" if status == "new" else ""))
        _confirm(s, db, bot, reply_to, iid, "No brand")
        db.outbox_resolve("item", iid)
    return f"nobrand {iid}: ({status})"


def _set_category_option(s: Settings, db: DB, bot: Bot, iid: str, n: int, reply_to: int | None) -> str:
    """A tap on "Which category?": option n of the item's stored options."""
    it = db.item(iid)
    options = ((loads(it["gate"]) or {}).get("ask_category") or []) if it else []
    if not 0 <= n < len(options):
        _tell(bot, reply_to, "That choice isn't open any more")
        return f"category {iid}: rejected option {n}"
    return _set_category(s, db, bot, iid, options[n], reply_to)


def _set_category(s: Settings, db: DB, bot: Bot, iid: str, path: dict, reply_to: int | None) -> str:
    try:
        status = pipeline.set_category(s, db, iid, path)
    except ValueError as e:
        _tell(bot, reply_to, str(e))
        return f"category {iid}: rejected {path}: {e}"
    label = taxonomy.path_label(path)
    _ack(db, bot, "category", iid, f"✓ {label}")
    _confirm(s, db, bot, reply_to, iid, f"Category: {label}")
    db.outbox_resolve("category", iid)
    return f"category {iid}: {label} ({status})"


def _reply_category(s: Settings, db: DB, bot: Bot, iid: str, text: str, mid: int | None) -> str:
    """A typed answer to "Which category?" (WO25): one of the options by its words or number, another real path
    ("Shorts", "Skirts › Skirt Sets"), or anything else as a note — the item is reprocessed with it, as before."""
    it = db.item(iid)
    if it is None or not (text or "").strip():
        bot.send_message(CATEGORY_HINT, reply_to=mid)
        return f"category {iid}: empty reply"
    options = (loads(it["gate"]) or {}).get("ask_category") or []
    if text.strip().isdigit() and 1 <= int(text.strip()) <= len(options):
        return _set_category(s, db, bot, iid, options[int(text.strip()) - 1], mid)
    facts = Facts.model_validate(loads(it["facts"]))
    if path := taxonomy.parse_path(text, facts):
        return _set_category(s, db, bot, iid, path, mid)
    try:
        pipeline.answer(s, db, iid, text)
    except ValueError as e:
        _tell(bot, mid, str(e))
        return f"category {iid}: rejected {text!r}: {e}"
    _ack(db, bot, "category", iid, "✓ noted — rechecking")
    db.outbox_resolve("category", iid)
    return f"category {iid}: noted {text!r}"


_BRAND_CMD = re.compile(r"^\s*brand\s*[:=]?\s*(.+?)\s*$", re.I)
# Words that make a reply something other than a brand: an answer of another kind, or a size.
_NOT_A_BRAND = re.compile(r"^\s*(?:size|sz|category|subcategory|cover|nwt|nwot|new|same item|different item|condition|"
                          r"retail|recheck|department|price|xs|s|m|l|xl|xxl|\d+(?:\.\d+)?\s*[a-z]{0,2})\b", re.I)


def brand_reply(text: str, gate: dict) -> str | None:
    """The brand a reply to a card gives (WO27): "brand J. Crew", or — when the card asks the brand — the reply as it
    is ("J. Crew"; live: taken as a note, the item was reprocessed and stuck again). None for anything else."""
    if m := _BRAND_CMD.match(text or ""):
        return m.group(1)
    words = (text or "").split()
    if asks_brand(gate) and words and len(words) <= 5 and not _NOT_A_BRAND.match(text):
        return text.strip()
    return None


def _set_brand(s: Settings, db: DB, bot: Bot, iid: str, brand: str, reply_to: int | None, kind: str = "item") -> str:
    try:
        status = pipeline.set_brand(s, db, iid, brand)
    except ValueError as e:
        _tell(bot, reply_to, str(e))
        return f"brand {iid}: rejected {brand!r}: {e}"
    if status not in OWNER_WAITING:
        db.outbox_resolve(kind, iid)
    if not db.conn.execute("SELECT 1 FROM outbox WHERE kind=? AND ref=? AND resolved_at IS NULL", (kind, iid)
                           ).fetchone():                # done, or closed to come again with the brand: the old copy
        _ack(db, bot, kind, iid, f"✓ brand: {brand}")
    _confirm(s, db, bot, reply_to, iid, f"Brand: {brand}")
    return f"brand {iid}: {brand!r} ({status})"


def _reply_item(s: Settings, db: DB, bot: Bot, iid: str, text: str, mid: int | None) -> str:
    if m := COVER_CMD.match(text or ""):
        return _set_cover(s, db, bot, iid, cover_index(m[1]), mid)
    if pipeline.NO_BRAND_WORDS.search(text or ""):     # "no brand", "unbranded, 25": the [No brand] button (WO25)
        price, note = parse_reply(pipeline.NO_BRAND_WORDS.sub("", text).strip(" ,.;:-!"))
        if price is not None:
            _set_price(s, db, bot, iid, price, mid)      # first: an approved price stays through any reprocessing
        result = _set_no_brand(s, db, bot, iid, mid)
        if note and "rejected" not in result:
            try:
                pipeline.answer(s, db, iid, note)
                _ack(db, bot, "item", iid, "✓ No brand · noted — rechecking")
                db.outbox_resolve("item", iid)
            except ValueError as e:
                _tell(bot, mid, str(e))
        return result
    price, note = parse_reply(text)
    if price is None and note is None:
        bot.send_message(ITEM_HINT, reply_to=mid)
        return f"item {iid}: empty reply"
    posted = db.item(iid)
    if note is None and posted is not None and posted["status"] == "posted" and PRICE_ONLY.match(text or ""):
        return _owner_price(s, db, bot, iid, price, mid)   # WO45 follow-up: a live item's price, on every site
    if note is None:
        return _set_price(s, db, bot, iid, price, mid)
    if (size := size_reply(note)) is not None:          # WO33: "Size S", "size s", "S" — with or without a price
        return _set_size(s, db, bot, iid, size, price, mid)
    if (fixed := _correction(s, db, bot, iid, note, price, mid)) is not None:   # WO37: condition / category / kids
        return fixed
    it = db.item(iid)
    if (brand := brand_reply(note, (loads(it["gate"]) or {}) if it else {})) is not None:
        if price is not None:
            _set_price(s, db, bot, iid, price, mid)      # first: an approved price stays through it all
        return _set_brand(s, db, bot, iid, brand, mid)
    recorded, errors, labels = [], [], []
    if price is not None:                              # kept through the reprocessing: never asked again
        try:
            pipeline.set_price(s, db, iid, price)
            recorded.append(f"${price}")
            labels.append(f"${price}")
        except ValueError as e:
            errors.append(str(e))
    try:
        outcome = pipeline.answer(s, db, iid, note)
        recorded.append("same item: dropped" if outcome == "dropped" else f"noted {note!r}, reprocessing")
        labels.append("same item — dropped" if outcome == "dropped" else "noted — rechecking")
    except ValueError as e:
        errors.append(str(e))
    if recorded:
        _ack(db, bot, "item", iid, "✓ " + " · ".join(labels))
        db.outbox_resolve("item", iid)
        said = [f"${price}"] if price is not None and f"${price}" in labels else []
        said += ["same item: dropped"] if "same item — dropped" in labels else \
            [f"Noted: \u201c{note}\u201d"] if any(lb.startswith("noted") for lb in labels) else []
        _confirm(s, db, bot, mid, iid, " · ".join(said) or "Got it")
    if errors:
        _tell(bot, mid, "\n".join(errors))
    return f"item {iid}: " + "; ".join(recorded + [f"error: {e}" for e in errors])


CONDITION_CMD = re.compile(r"^\s*condition\s*[:=]?\s*(.+?)\s*$", re.I)           # WO37: "condition nwt"
CATEGORY_CMD = re.compile(r"^\s*category\s*[:=]?\s*(.+?)\s*$", re.I)             # "category Shoes / Flats & Loafers"


def _correction(s: Settings, db: DB, bot: Bot, iid: str, note: str, price: int | None, mid: int | None) -> str | None:
    """WO37: what the rules decided, corrected by a reply to the card — `condition nwt|like new|good`, `category …`,
    `girls` / `boys`. Answered at once; None when the reply is none of these."""
    if m := CONDITION_CMD.match(note or ""):
        choice = parse_condition(m[1])
        if choice is None:
            _tell(bot, mid, "Condition: reply 'condition nwt', 'condition like new' or 'condition good'")
            return f"item {iid}: condition {m[1]!r} not understood"
        if price is not None:
            _set_price(s, db, bot, iid, price, mid)
        try:
            pipeline.correct_condition(s, db, iid, choice)
        except ValueError as e:
            _tell(bot, mid, str(e))
            return f"condition {iid}: rejected {choice!r}: {e}"
        refresh_card(s, db, iid)
        _confirm(s, db, bot, mid, iid, f"Condition: {CONDITION_TAPPED.get(choice, choice)}")
        return f"condition {iid}: {choice}"
    if m := CATEGORY_CMD.match(note or ""):
        if price is not None:
            _set_price(s, db, bot, iid, price, mid)
        return _reply_category(s, db, bot, iid, m[1], mid)
    if (kids := parse_kids(note)) is not None and price is None:
        return _set_kids(s, db, bot, iid, kids, mid)
    if (dept := DEPARTMENT_WORDS.get((note or "").strip().lower().rstrip(".!"))) and dept != "Kids":   # WO43
        if price is not None:
            _set_price(s, db, bot, iid, price, mid)
        return _set_department(s, db, bot, iid, dept, mid)
    return None


def _set_department(s: Settings, db: DB, bot: Bot, iid: str, dept: str, mid: int | None) -> str:
    """WO43 (live: the On Cloudsurfer Max sneakers listed under Women — men's): a reply 'men' / 'women' puts the item's
    category under that department (the same category and subcategory where that department has them; the item is
    reprocessed with it, so the size menu and the copy follow). An item already on the marketplace: `thrift
    recategorize` changes it on the sites."""
    it = db.item(iid)
    if it is None:
        _tell(bot, mid, "Unknown item")
        return f"department {iid}: unknown item"
    facts = loads(it["facts"]) or {}
    path = {"department": dept, "category": facts.get("category"), "subcategory": facts.get("subcategory")}
    if path["department"] == facts.get("department"):
        _confirm(s, db, bot, mid, iid, f"{dept} already")
        return f"department {iid}: {dept} already"
    placed = taxonomy.place(Facts.model_validate(facts), dept, path["category"] or "", path["subcategory"])
    if placed is None:
        placed = taxonomy.place(Facts.model_validate(facts), dept, path["category"] or "", None)
    if placed is None:
        _tell(bot, mid, f"Poshmark has no {path['category']} under {dept} — reply 'category {dept} › …' with its name")
        return f"department {iid}: no {path['category']} under {dept}"
    return _set_category(s, db, bot, iid, placed, mid)


SIZE_SAID = re.compile(r"^\s*(?:size|sz)\s*[:=#]?\s*([A-Za-z0-9][A-Za-z0-9./ -]{0,11}?)\s*[.!]?\s*$", re.I)
LETTER_SIZE = re.compile(r"^\s*(XXXS|XXS|XS|S|M|L|XL|XXL|XXXL|[2-5]XL|[23]XS)\s*[.!]?\s*$", re.I)


def size_reply(note: str | None) -> str | None:
    """The size a reply gives (WO33): "Size S", "size s", "size 8.5", or a bare letter size "S" / "XL" — letters in
    capitals. None for anything else (a number alone is a price)."""
    m = SIZE_SAID.match(note or "") or LETTER_SIZE.match(note or "")
    if m is None:
        return None
    size = m[1].strip()
    return size.upper() if re.fullmatch(r"[A-Za-z0-9]{1,4}", size) and not size.isdigit() else size


def _set_size(s: Settings, db: DB, bot: Bot, iid: str, size: str, price: int | None, mid: int | None) -> str:
    """A size the owner replied (and a price with it): set at once, no model call; one answer for both."""
    said = []
    if price is not None:
        try:
            pipeline.set_price(s, db, iid, price)
            said.append(f"${price}")
        except ValueError as e:
            _tell(bot, mid, str(e))
            return f"item {iid}: rejected ${price}: {e}"
    try:
        status = pipeline.set_size(s, db, iid, size)
    except ValueError as e:
        _tell(bot, mid, str(e))
        return f"item {iid}: size {size!r} rejected: {e}"
    if status not in OWNER_WAITING:
        db.outbox_resolve("item", iid)
    _ack(db, bot, "item", iid, "✓ " + " · ".join([f"size {size}", *said]))
    _confirm(s, db, bot, mid, iid, " · ".join([f"Size {size}", *said]))
    return f"item {iid}: size {size!r}" + (f", ${price}" if price is not None else "") + f" ({status})"


def _tap_size(s: Settings, db: DB, bot: Bot, iid: str, size: str) -> str:
    """WO43: a size button — the size of THIS card's item only, set at once (no model call), the card EDITED in place
    (the line "📏 Size 8 ✓", the button ✓, the title's size, the rows kept so another tap changes it) — no new message.
    A second tap of the same size does nothing twice; an item that started posting, or is retired, changes nothing."""
    it = db.item(iid)
    if it is None or it["status"] not in SIZE_OFFERED:
        return f"size {iid}: rejected: " + ("unknown item" if it is None else f"it is {it['status']}")
    if it["owner_size"] == size:
        return f"size {iid}: {size!r} already"
    mark_size_tapped(db, iid)
    try:
        status = pipeline.set_size(s, db, iid, size)
    except ValueError as e:
        return f"size {iid}: rejected {size!r}: {e}"
    sizebuttons.forget_page(db, iid)
    if status not in OWNER_WAITING:
        db.outbox_resolve("item", iid)
    refresh_card(s, db, iid, reopen=status in OWNER_WAITING)
    return f"size {iid}: {size!r} tapped ({status})"


def _reply_owner_q(s: Settings, db: DB, bot: Bot, iid: str, text: str, mid: int | None) -> str:
    """A reply to a question the poster asked before WO27 (it asks none now): to its brand question, the reply is the
    brand; anything else a note, as before."""
    if not text:
        bot.send_message("Reply to this message with the answer.", reply_to=mid)
        return f"owner_q {iid}: empty reply"
    it = db.item(iid)
    if it is not None and "brand" in (it["owner_question"] or "").lower():
        brand = (_BRAND_CMD.match(text) or re.match(r"(.*)", text)).group(1).strip()
        result = _set_brand(s, db, bot, iid, brand, mid, kind="owner_q")
        if "rejected" not in result:
            db.outbox_resolve("owner_q", iid)
            if db.item(iid)["status"] == "needs_owner":
                try:
                    pipeline.requeue(s, db, iid)         # back to 'ready' as it is, its old question closed
                except ValueError as e:
                    _tell(bot, mid, str(e))
        return result
    try:
        pipeline.answer(s, db, iid, text)
    except ValueError as e:
        _tell(bot, mid, str(e))
        return f"owner_q {iid}: rejected: {e}"
    _ack(db, bot, "owner_q", iid, "✓ got it — rechecking")
    db.outbox_resolve("owner_q", iid)
    return f"owner_q {iid}: answered {text!r}"


def _ref_of(db: DB, update: dict) -> str | None:
    """The batch/item an update is about, for the event log; None when it is not ours."""
    cq = update.get("callback_query")
    if isinstance(cq, dict):
        parts = (cq.get("data") or "").split(":")
        return parts[1] if len(parts) >= 2 and parts[1] else None
    msg = update.get("message") or {}
    reply = msg.get("reply_to_message") if isinstance(msg, dict) else None
    chat = (msg.get("chat") or {}).get("id") if isinstance(msg, dict) else None
    if isinstance(reply, dict) and chat is not None and reply.get("message_id") is not None:
        row = db.outbox_lookup(chat, reply["message_id"])
        return row["ref"] if row else None
    return None


def poll_once(s: Settings, db: DB, bot: Bot, timeout: int) -> int:
    """One getUpdates long poll from the persisted offset; handles each update and persists the offset after it.
    Returns the number of updates handled."""
    offset = db.kv_get(OFFSET_KEY)
    updates = bot.get_updates(offset=int(offset) + 1 if offset else None, timeout=timeout)
    n = 0
    for u in updates:
        ref = _ref_of(db, u)
        try:
            result = handle_update(s, db, bot, u)
            db.log(ref, "telegram", result)
        except Exception as e:  # noqa: BLE001 - one bad update must not stop the loop or be re-read forever
            db.log(ref, "telegram_error", f"{type(e).__name__}: {e}")
            notify.say(f"❌ telegram update ({ref or 'no item'}): {type(e).__name__}: {e}")   # the detail: ops
            try:
                bot.send_message("Sorry, that didn't go through — please try again.")  # WO29: plain in the group
            except Exception:  # noqa: BLE001
                pass
        if (uid := u.get("update_id")) is not None:
            db.kv_set(OFFSET_KEY, str(uid))
        n += 1
    return n


def resend_pending(s: Settings, db: DB) -> list[str]:
    """WO37 (the owner: never the same message twice): nothing is sent again, however long the open message has
    waited — the worker runs this on start and on a wake only to close older copies and, with nothing open, to send
    the next message (a restart never stalls the queue). The open copy keeps working (its buttons, replies to it).
    Returns the refs re-sent: always none since WO37 (was: the open one past telegram.resend_after_hours)."""
    if bot_for(s) is None or db.conn.in_transaction:
        return []
    token = _lock(db)
    if token is None:
        return []
    try:
        for lane in LANES:            # each lane on its own: the group's queue, the test shares' (2026-10-20)
            if open_message(db, lane=lane) is None:
                if lane == "real":
                    _note_round(db)
                if head := next_up(db, lane):
                    _send(s, db, *head)
                    note_send_ok(db)
        return []                     # WO37: a card is sent once — never again after hours (it stays where it is)
    finally:
        _unlock(db, token)
