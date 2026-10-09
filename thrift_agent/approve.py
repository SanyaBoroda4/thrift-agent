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
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from thrift_agent import notify, pipeline
from thrift_agent.brain import taxonomy
from thrift_agent.brain.price import note_floor
from thrift_agent.brain.gate import SIZE_NOTE
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads
from thrift_agent.ingest import segment as seg
from thrift_agent.schema import POSH_CONDITION, Facts, Render
from thrift_agent.telegram import MAX_CAPTION, Bot

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
SHIPPED_CMD = re.compile(r"^\s*shipped\s+(.+?)\s*$", re.I | re.S)       # WO33: "shipped <title words>"
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


def queue(db: DB) -> list[tuple[str, str]]:
    """Everything that waits for the owner, in the order they are asked: [(kind, ref)]. The oldest batch first — its
    contact sheet, then its items in photo order, each item's questions before its price card (one entry per item:
    what it waits for now) — then the next batch. An item put off with [Later] goes behind everything that was queued
    when the owner tapped it. NEW_BATCH / NEW_ITEM entries are still being processed: the queue holds there, so nothing
    behind them jumps ahead. A pure read."""
    # Sort key: (when it joined the queue, the batch's insertion order, -1 for the contact sheet / the photo order,
    # id). The insertion order keeps each batch's messages together when two batches were registered in the same
    # second (created_at has whole seconds).
    entries = []
    kinds = {"needs_confirm": "batch", "regroup": "regroup", "new": NEW_BATCH}
    for b in db.conn.execute("SELECT rowid AS n, id, status, created_at FROM batches "
                             "WHERE status IN ('new', 'needs_confirm', 'regroup')"):
        entries.append(((b["created_at"], b["n"], -1, b["id"]), kinds[b["status"]], b["id"]))
    marks = ",".join("?" * len(QUEUED_ITEMS))
    # The items of a batch whose photos are being fixed ([Wrong photos]) wait, unasked and unprocessed, until the fix.
    rows = db.conn.execute(
        "SELECT i.id, i.status, i.seq, i.gate, i.deferred_at, COALESCE(b.created_at, i.created_at) AS since, "
        f"COALESCE(b.rowid, 0) AS n FROM items i LEFT JOIN batches b ON b.id = i.batch_id WHERE i.status IN ({marks}) "
        "AND COALESCE(b.status, '') != 'regroup'", QUEUED_ITEMS)
    for r in rows:
        entries.append(((r["deferred_at"] or r["since"], r["n"], r["seq"], r["id"]),
                        _item_kind(r["status"], loads(r["gate"]) or {}), r["id"]))
    return [(kind, ref) for _, kind, ref in sorted(entries)]


def processing_order(db: DB) -> list[tuple[str, str]]:
    """The batches and items still to be processed, in the queue's order: the worker takes them in this order, so the
    card the owner needs next is the one ready first (an item reprocessed after an answer jumps ahead)."""
    return [(kind, ref) for kind, ref in queue(db) if kind in (NEW_BATCH, NEW_ITEM)]


def next_up(db: DB) -> tuple[str, str] | None:
    """The message to send next: the head of the queue, or None (nothing waits, or the head is still processed)."""
    q = queue(db)
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


def open_message(db: DB, settle: bool = True):
    """The one message that waits for the owner's answer (an outbox row), or None: the newest unresolved queue message
    whose batch/item still waits for that answer. Every other unresolved queue message is resolved on the way —
    answered elsewhere (a CLI command, a button on an older copy), moved on (reprocessing, dropped), or superseded (a
    re-send, or the flood of messages from before the queue). Their buttons keep working."""
    marks = ",".join("?" * len(QUEUE_KINDS))
    rows = db.conn.execute(f"SELECT rowid AS n, * FROM outbox WHERE resolved_at IS NULL AND kind IN ({marks}) "
                           "ORDER BY rowid DESC", QUEUE_KINDS).fetchall()
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
    """Send the next message if none is open (one question at a time). Called after every processing step and every
    answer, and by the worker's loops. Returns "kind ref" for what was sent; None when nothing was: one is open,
    nothing waits, the head of the queue is still being processed, or another sender holds the lock (it sends)."""
    if db.conn.in_transaction:
        return None                                   # never inside a caller's transaction; the worker pumps soon
    token = _lock(db)
    if token is None:
        return None
    try:
        if open_message(db) is not None:
            return None
        _note_round(db)
        if (head := next_up(db)) is None:
            return None
        _send(s, db, *head)
        return " ".join(head)
    finally:
        _unlock(db, token)


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
    if sheet.is_file() and len(caption) <= MAX_CAPTION:
        mid = bot.send_photo(sheet, caption)
    elif sheet.is_file():                                   # too many items for one caption: sheet, then the text
        db.add_outbox(bot.chat_id, bot.send_photo(sheet, caption.splitlines()[0]), "batch", bid)
        mid = bot.send_message(caption)
    else:
        mid = bot.send_message(caption)
    db.add_outbox(bot.chat_id, mid, "batch", bid)


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
    if sheet.is_file() and len(caption) <= MAX_CAPTION:
        mid = bot.send_photo(sheet, caption)
    elif sheet.is_file():
        db.add_outbox(bot.chat_id, bot.send_photo(sheet, lines[0]), "regroup", bid)
        mid = bot.send_message(caption)
    else:
        mid = bot.send_message(caption)
    db.add_outbox(bot.chat_id, mid, "regroup", bid)


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
        lines.append(f"Size {size}" + (" — my best reading; reply 'size …' to change it" if guessed else ""))
    elif guessed:
        lines.append("Size: not read from the photos — reply 'size …'")
    cond = facts.get("condition")
    label = POSH_CONDITION.get(cond, cond or "unknown")
    if (facts.get("condition_evidence") or {}).get("source") == "owner":
        label += " (your answer)"                                   # "Brand new or worn?" was answered
    lines.append(f"Condition: {label}")
    if "questions" in gate:                                         # processed since WO20
        lines += [f"⚠️ {n}" for n in notes if not n.startswith(SIZE_NOTE)]
        if questions := gate.get("questions") or []:
            lines += [f"❓ {q}" for q in questions]
            lines.append("Reply to this card with the answer (a price too if you like), e.g. 'size 8, 45'")
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


def price_options(price: int, step: int = PRICE_STEP) -> list[int]:
    """The card's eight one-tap prices (WO32b, the owner's request — mostly up): $10 below the suggestion, the
    suggestion, then six $10 steps above it; the steps start from the suggestion ($35: 25 · 35 · 45 … 95). No low
    button when $10 below would be under $5: then the suggestion and seven steps up. Any typed number still works."""
    low = price - step
    out = [low] if low >= LOWEST_BUTTON else []
    out += [price + k * step for k in range(0, 8 - len(out))]
    return out


def asks_brand(gate: dict) -> bool:
    """Is the brand one of the card's questions (unreadable, or read but unsure)?"""
    return any(q.startswith(("Brand?", "Brand:")) for q in gate.get("questions") or [])


def item_buttons(iid: str, price: int | None, no_brand: bool = False) -> list[list[dict]]:
    """Eight prices in two rows of four — $10 below, ⭐ the suggestion, $10 steps up (WO32b) — / [No brand] (when the
    brand is asked, WO25) / [Later] [Change] [Wrong photos]. Every price button sets that price
    (approve:<item>:<amount>); [No brand] leaves Poshmark's brand empty (nobrand:<item>); [Wrong photos] reopens the
    batch's grouping (regroup:<item>, WO20b)."""
    rows = []
    if price:
        buttons = [{"text": f"⭐${p}" if p == price else f"${p}", "callback_data": f"approve:{iid}:{p}"}
                   for p in price_options(price)]
        rows += [buttons[:4], buttons[4:]]
    if no_brand:
        rows.append([{"text": NO_BRAND_BUTTON, "callback_data": f"nobrand:{iid}"}])
    rows.append([{"text": "Later", "callback_data": f"later:{iid}"}, {"text": "Change", "callback_data": f"change:{iid}"},
                 {"text": "Wrong photos", "callback_data": f"regroup:{iid}"}])
    return rows


def send_item(s: Settings, db: DB, iid: str) -> None:
    """The item's price card: cover photo, item_caption, item_buttons. Records the outbox row. Without a bot: print
    (dev)."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    caption, price = item_caption(iid, it)
    cover = Path(it["dir"]) / "cover.jpg"
    bot = bot_for(s)
    if bot is None:
        text = f"{caption}\n(thrift price {iid} {price or '<amount>'})"
        notify.group_photo(cover, text) if cover.is_file() else notify.group(text)
        return
    buttons = item_buttons(iid, price, asks_brand(loads(it["gate"]) or {}))
    if cover.is_file() and len(caption) <= MAX_CAPTION:
        mid = bot.send_photo(cover, caption, buttons)
    else:
        mid = bot.send_message(caption, buttons)
    db.add_outbox(bot.chat_id, mid, "item", iid, text=caption)


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
    mid = bot.send_photo(cover, caption, buttons) if cover.is_file() else bot.send_message(caption, buttons)
    db.add_outbox(bot.chat_id, mid, kind, iid, text=caption)


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
            return _route_ops(s, db, update)               # WO33: a reply there is enough too
        return "ignored: unauthorized"
    result = _route(s, db, bot, update)
    if not result.startswith("ignored"):
        pump(s, db)
    return result


UNMATCHED_LINE = re.compile(r"Unmatched sale on (\w+) \((s_[0-9a-f]+)\)")


def _from_ops_chat(s: Settings, bot: Bot, update: dict) -> bool:
    """A message in the ops chat (the owner's private chat with the bot) from an allowed user."""
    msg = update.get("message") if isinstance(update, dict) else None
    chat = notify.ops_chat(s)
    if not isinstance(msg, dict) or chat is None:
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
    m = UNMATCHED_LINE.search(quoted)
    if m is None or not text:
        bot.send_message("Replies here act on an 'Unmatched sale' message: reply to it with words from the item's "
                         "title", reply_to=mid)
        return "ops: hint"
    sale_id, words = m[2], [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1]
    hits = []
    for it in db.conn.execute("SELECT id, renders FROM items WHERE renders IS NOT NULL"):
        title = ((loads(it["renders"]) or {}).get("poshmark") or {}).get("title") or ""
        if words and all(w in title.lower() for w in words):
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
    if kind == "unconfirmed":
        return _reply_unconfirmed(s, db, bot, ref, text, mid)
    if kind == "skipped":
        return _reply_skipped(s, db, bot, ref, text, mid)
    return f"ignored: unknown outbox kind {kind}"


def _reply_skipped(s: Settings, db: DB, bot: Bot, ref: str, text: str, mid: int | None) -> str:
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
    _ack(db, bot, "skipped", ref, "✓ retry — back in line")
    db.outbox_resolve("skipped", ref)
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
        return _set_cover(s, db, bot, row["ref"], int(m[1]), mid)
    if pipeline.NO_BRAND_WORDS.fullmatch((text or "").strip(" .!")):
        row = open_message(db)
        if row is None or row["kind"] != "item":
            return "ignored: not a reply to the bot"
        return _set_no_brand(s, db, bot, row["ref"], mid)
    amount = plain_price(text)
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
    if status in OWNER_WAITING:                        # still open: a re-share hold, or Girls/Boys not answered yet
        gate = loads(db.item(iid)["gate"]) or {}
        _ack(db, bot, "item", iid, f"✓ ${amount} recorded")
        if gate.get("hold"):                           # a question only the owner can settle: said plainly
            _tell(bot, reply_to, f"${amount} recorded{HELD_HINT}")
    else:
        db.outbox_resolve("item", iid)
        _ack(db, bot, "item", iid, f"✓ ${amount} — queued")     # WO29: no "✓ $X — N left" message
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
    """The owner's "cover N": the item's listing is rebuilt with photo N first; a card still waiting is sent again
    (the queue re-sends it with the new cover), an item already priced shows its new cover in the reply."""
    try:
        status = pipeline.set_cover(s, db, iid, n)
    except ValueError as e:
        _tell(bot, reply_to, str(e))
        return f"cover {iid}: rejected {n}: {e}"
    _ack(db, bot, "item", iid, f"✓ cover: photo {n}")
    _confirm(s, db, bot, reply_to, iid, f"Cover: photo {n}")
    if status in WAITING_ITEM:
        db.outbox_resolve("item", iid)                 # the card comes again with its new cover
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
        return _set_cover(s, db, bot, iid, int(m[1]), mid)
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
    if note is None:
        return _set_price(s, db, bot, iid, price, mid)
    if (size := size_reply(note)) is not None:          # WO33: "Size S", "size s", "S" — with or without a price
        return _set_size(s, db, bot, iid, size, price, mid)
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
    """Send the open message again — only that one (WO20) — once it has waited longer than
    telegram.resend_after_hours (the Mac slept; Telegram keeps the owner's replies 24 h). The worker runs this when it
    starts too, with the same rule: a restart — every deploy — never repeats a card the owner has just been sent
    (WO24; it used to, each time). The copy above keeps working (its buttons, replies to it). With nothing open, the
    next message goes out instead, so a restart never stalls the queue. Returns the refs re-sent."""
    if bot_for(s) is None or db.conn.in_transaction:
        return []
    token = _lock(db)
    if token is None:
        return []
    try:
        row = open_message(db)
        if row is None:
            _note_round(db)
            if head := next_up(db):
                _send(s, db, *head)
            return []
        hours = float(s.get("telegram.resend_after_hours", 6) or 0)
        if row["sent_at"] >= (_now() - timedelta(hours=hours)).isoformat(timespec="seconds"):
            return []
        db.outbox_resolve(row["kind"], row["ref"])
        _send(s, db, row["kind"], row["ref"])
        return [row["ref"]]
    finally:
        _unlock(db, token)
