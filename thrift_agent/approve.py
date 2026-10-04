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

pipeline.py, cli.py and post/runner.py call pump, ask_owner, announce, resend_pending and the handlers below."""
from __future__ import annotations

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
from thrift_agent.brain.price import note_floor
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads
from thrift_agent.ingest import segment as seg
from thrift_agent.schema import POSH_CONDITION, Render
from thrift_agent.telegram import MAX_CAPTION, Bot

OFFSET_KEY = "telegram_offset"                       # kv: the last getUpdates update_id we handled
LOCK_KEY = "telegram_queue_lock"                     # kv: who is sending the next message ("<token>@<epoch>")
ROUND_KEY = "telegram_round"                         # kv: items queued since the queue was last empty ("of 10")
LOCK_TTL = 120                                       # seconds: a sender's lock older than this was abandoned (a crash)
DEV_CHAT = "dev"                                     # outbox chat of the dev print (no bot)
QUEUE_KINDS = ("batch", "condition", "kids", "item", "owner_q")      # outbox kinds that wait for an answer
SENDERS = {"batch": "send_batch", "condition": "ask_condition", "kids": "ask_kids", "item": "send_item",
           "owner_q": "send_owner_q"}
NEW_BATCH, NEW_ITEM = "new_batch", "new_item"        # queue entries still being processed: the queue holds there
OWNER_WAITING = ("awaiting_condition", "awaiting_price", "needs_info", "needs_owner")   # item waits for an answer
QUEUED_ITEMS = ("new", *OWNER_WAITING)               # item statuses in the queue ('new': still being processed)
WAITING_ITEM = ("awaiting_price", "needs_info")      # item statuses whose price card is the open question
BATCH_HINT = "Reply to this message: ok | 12>2 | split 7 | merge 2 3 | drop 7"
ITEM_HINT = "Tap a price, type a number, or reply with an answer like 'size 8, 45'"
HELD_HINT = " (still held as a possible re-share: reply 'different item' to list it or 'same item' to drop it)"
CONDITION_QUESTION = "Brand new or worn? (couldn't tell from the photos)"
CONDITION_BUTTONS = (("NWT", "nwt"), ("Like New", "like_new"), ("Good", "good"))    # label, callback choice
CONDITION_HINT = "Tap a button, or reply nwt / like new / good"
CONDITION_TAPPED = {"nwt": "New with tags", "like_new": "Like New (brand new, no tags)", "good": "Good (worn)"}
KIDS_QUESTION = "Girls or Boys? (Poshmark files kids sizes under one of them)"
KIDS_BUTTONS = (("Girls", "girls"), ("Boys", "boys"))
KIDS_HINT = "Tap a button, or reply girls / boys"

_NUM = r"\d+(?:\.\d+)?"
# A number that is money, whichever way the owner says it: "$85", "$ 85", "85 usd", "85 dollars", "price 85", "list: 85".
_MONEY = re.compile(rf"(?:\$\s*({_NUM})|\b({_NUM})\s*(?:usd|dollars?|bucks)\b|\b(?:price|list)\b\s*[:=]?\s*\$?\s*({_NUM}))",
                    re.I)
# A standalone number: not glued to letters or digits (7.5M, 5T, 8-9, 8/9) and not a decimal's tail.
_BARE = re.compile(rf"(?<![\w.$/-])({_NUM})(?![\w/-]|\.\d)")
_SIZE_BEFORE = re.compile(r"\b(?:size|sz|eu|us|uk|toddler|kids?|y|c|cm|in|inch)\s*[:=#]?\s*$", re.I)
_UNIT_AFTER = re.compile(r"\s*(?:cm|mm|in|inch|inches|us|uk|eu|y|t|m|w)\b", re.I)
# A message that is nothing but a price: "28", "$28", "28.00", "28 dollars" (typed without a reply, WO20).
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
    """What an item in the queue waits for: being processed, the condition, Girls/Boys, its price card, or the
    poster's question."""
    if status == "new":
        return NEW_ITEM
    if status == "awaiting_condition":
        return "condition"
    if status == "needs_owner":
        return "owner_q"
    return "kids" if gate.get("ask_kids") else "item"


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
    for b in db.conn.execute("SELECT rowid AS n, id, status, created_at FROM batches "
                             "WHERE status IN ('new', 'needs_confirm')"):
        kind = "batch" if b["status"] == "needs_confirm" else NEW_BATCH
        entries.append(((b["created_at"], b["n"], -1, b["id"]), kind, b["id"]))
    marks = ",".join("?" * len(QUEUED_ITEMS))
    rows = db.conn.execute(
        "SELECT i.id, i.status, i.seq, i.gate, i.deferred_at, COALESCE(b.created_at, i.created_at) AS since, "
        f"COALESCE(b.rowid, 0) AS n FROM items i LEFT JOIN batches b ON b.id = i.batch_id WHERE i.status IN ({marks})",
        QUEUED_ITEMS)
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
    if kind == "batch":
        b = db.batch(ref)
        return b is not None and b["status"] == "needs_confirm"
    it = db.item(ref)
    if it is None or it["status"] not in OWNER_WAITING:
        return False
    return _item_kind(it["status"], loads(it["gate"]) or {}) == kind


def open_message(db: DB):
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
            continue
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
        notify.photo(sheet, f"{caption}\n(thrift confirm {bid} ok)")
        return
    if sheet.is_file() and len(caption) <= MAX_CAPTION:
        mid = bot.send_photo(sheet, caption)
    elif sheet.is_file():                                   # too many items for one caption: sheet, then the text
        db.add_outbox(bot.chat_id, bot.send_photo(sheet, caption.splitlines()[0]), "batch", bid)
        mid = bot.send_message(caption)
    else:
        mid = bot.send_message(caption)
    db.add_outbox(bot.chat_id, mid, "batch", bid)


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
    if choice.tab in ("Girls", "Boys", "Baby") and "(" not in choice.button:
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
    if size := size_words(posh, facts):
        lines.append(f"Size {size}")
    cond = facts.get("condition")
    label = POSH_CONDITION.get(cond, cond or "unknown")
    if (facts.get("condition_evidence") or {}).get("source") == "owner":
        label += " (your answer)"                                   # "Brand new or worn?" was answered
    lines.append(f"Condition: {label}")
    if "questions" in gate:                                         # processed since WO20
        lines += [f"⚠️ {n}" for n in gate.get("notes") or []]
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


def price_options(price: int, floor: int = 20, step: int = 5) -> list[int]:
    """Four round prices near the suggestion, for one-tap buttons (WO20): two below and two above it — $5 apart under
    $100, $10 up to $250, $25 above — each rounded to `step`, never under the floor and never the suggestion itself;
    a slot that would fall under the floor moves above instead."""
    gap = 5 if price < 100 else 10 if price < 250 else 25
    out: list[int] = []
    for k in (-2, -1, 1, 2, 3, 4, 5, 6):
        p = max(step, step * math.floor((price + k * gap) / step + 0.5))
        if p >= floor and p != price and p not in out:
            out.append(p)
        if len(out) == 4:
            break
    return sorted(out)


def item_buttons(iid: str, price: int | None, floor: int = 20, step: int = 5) -> list[list[dict]]:
    """[✅ $X] / four nearby prices / [Later] [Change]. Every price button sets that price (approve:<item>:<amount>)."""
    rows = []
    if price:
        rows.append([{"text": f"✅ ${price}", "callback_data": f"approve:{iid}:{price}"}])
        if options := price_options(price, floor, step):
            rows.append([{"text": f"${p}", "callback_data": f"approve:{iid}:{p}"} for p in options])
    rows.append([{"text": "Later", "callback_data": f"later:{iid}"}, {"text": "Change", "callback_data": f"change:{iid}"}])
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
        notify.photo(cover, text) if cover.is_file() else notify.say(text)
        return
    floor = max(int(s["pricing"]["floor"]), note_floor(it["note"]) or 0)
    buttons = item_buttons(iid, price, floor, int(s["pricing"]["round_to"]))
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
        notify.photo(cover, text) if cover.is_file() else notify.say(text)
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


def kids_buttons(iid: str) -> list[list[dict]]:
    return [[{"text": label, "callback_data": f"kids:{iid}:{choice}"} for label, choice in KIDS_BUTTONS]]


def ask_kids(s: Settings, db: DB, iid: str) -> None:
    """A kids item the model wasn't 0.70 sure was for girls or boys (pipeline.kids_question), before the price card:
    "Girls or Boys?" [Girls] [Boys]."""
    _ask(s, db, iid, "kids", KIDS_QUESTION, kids_buttons(iid), f"thrift kids {iid} girls|boys")


def send_owner_q(s: Settings, db: DB, iid: str) -> None:
    """The poster's question (kind owner_q) when its turn comes; the reply is attached to the item as a note."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    question = it["owner_question"] or "the poster needs an answer for this item"
    text = f"{_title(it, iid)}\nQuestion from the poster: {question}\nReply to this message."
    bot = bot_for(s)
    if bot is None:
        notify.say(f"{text}\n(thrift answer {iid} \"…\")")
        return
    mid = bot.send_message(text)
    db.add_outbox(bot.chat_id, mid, "owner_q", iid, text=question)


def ask_owner(s: Settings, db: DB, iid: str, question: str) -> None:
    """The poster's question when stuck on a field only the owner can answer (the item is already needs_owner): kept
    with the item and asked when its turn in the queue comes."""
    db.set_item(iid, owner_question=question)
    pump(s, db)


def announce(s: Settings, text: str) -> bool:
    """A short one-way line to the group when the owner acts from the CLI (thrift price / answer / confirm), so
    everyone who approves sees what changed. Best effort: True when sent, False without a bot or on an error."""
    bot = bot_for(s)
    if bot is None:
        return False
    try:
        bot.send_message(text)
        return True
    except Exception as e:  # noqa: BLE001 - never let a courtesy message break the command
        print(f"[telegram] announce failed: {type(e).__name__}: {e}", file=sys.stderr)
        return False


# ---------- incoming ----------

def handle_update(s: Settings, db: DB, bot: Bot, update: dict) -> str:
    """Route one Telegram update (a reply, a button press, a number typed on its own) to the pipeline, then send the
    next question (pump). Returns a short description of what happened (for the log); unauthorised or unrelated
    updates are ignored."""
    if not bot.authorized(update):
        return "ignored: unauthorized"
    result = _route(s, db, bot, update)
    if not result.startswith("ignored"):
        pump(s, db)
    return result


def _route(s: Settings, db: DB, bot: Bot, update: dict) -> str:
    if isinstance(update.get("callback_query"), dict):
        return _handle_callback(s, db, bot, update["callback_query"])
    msg = update.get("message")
    if not isinstance(msg, dict):
        return "ignored: unsupported update"
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
    return f"ignored: unknown outbox kind {kind}"


def _typed(s: Settings, db: DB, bot: Bot, text: str, mid: int | None) -> str:
    """A message that replies to nothing. A plain number is the price of the open card (WO20); anything else is the
    owners' own chat and is ignored. A typed number under the floor is not taken (a stray "2" in the chat must not
    price an item at $2): a reply to the card, or `thrift price`, sets it."""
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
        bot.send_message(str(e), reply_to=reply_to)
        return f"price {iid}: rejected ${amount}: {e}"
    if status in OWNER_WAITING:                        # still open: a re-share hold, or Girls/Boys not answered yet
        gate = loads(db.item(iid)["gate"]) or {}
        why = HELD_HINT if gate.get("hold") else " (Girls or Boys? is still open)" if gate.get("ask_kids") else ""
        bot.send_message(f"✓ ${amount} recorded{why}", reply_to=reply_to)
    else:
        db.outbox_resolve("item", iid)
        bot.send_message(f"✓ ${amount} — {progress(db)}", reply_to=reply_to)
    return f"price {iid}: ${amount} ({status})"


def _set_condition(s: Settings, db: DB, bot: Bot, iid: str, choice: str, reply_to: int | None) -> str:
    try:
        pipeline.set_condition(s, db, iid, choice)
    except ValueError as e:
        bot.send_message(str(e), reply_to=reply_to)
        return f"condition {iid}: rejected {choice!r}: {e}"
    db.outbox_resolve("condition", iid)
    bot.send_message(f"✓ {CONDITION_TAPPED[choice]} — repricing, its card comes next", reply_to=reply_to)
    return f"condition {iid}: {choice}"


def _set_kids(s: Settings, db: DB, bot: Bot, iid: str, choice: str, reply_to: int | None) -> str:
    try:
        status = pipeline.set_kids_gender(s, db, iid, choice)
    except ValueError as e:
        bot.send_message(str(e), reply_to=reply_to)
        return f"kids {iid}: rejected {choice!r}: {e}"
    db.outbox_resolve("kids", iid)
    bot.send_message(f"✓ {choice.title()}" + ("" if status in OWNER_WAITING else f" — {progress(db)}"),
                     reply_to=reply_to)
    return f"kids {iid}: {choice} ({status})"


def _handle_callback(s: Settings, db: DB, bot: Bot, cq: dict) -> str:
    cid = cq.get("id")
    parts = (cq.get("data") or "").split(":")
    src_mid = (cq.get("message") or {}).get("message_id")
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
        db.outbox_resolve("item", parts[1])
        bot.answer_callback(cid, "Later: moved to the end")
        return f"later {parts[1]}"
    if parts[0] == "cond" and len(parts) == 3 and parts[2] in CONDITION_TAPPED:
        result = _set_condition(s, db, bot, parts[1], parts[2], src_mid)
        bot.answer_callback(cid, CONDITION_TAPPED[parts[2]] if "rejected" not in result else "Could not set it")
        return result
    if parts[0] == "kids" and len(parts) == 3 and parts[2] in ("girls", "boys"):
        result = _set_kids(s, db, bot, parts[1], parts[2], src_mid)
        bot.answer_callback(cid, parts[2].title() if "rejected" not in result else "Could not set it")
        return result
    if parts[0] == "change" and len(parts) == 2:
        iid = parts[1]
        mid = bot.send_message(f"Reply to this message with the price for {iid} (or just type it).", reply_to=src_mid)
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
        bot.send_message(str(e), reply_to=mid)
        return f"batch {bid}: rejected {text!r}: {e}"
    db.outbox_resolve("batch", bid)
    n = db.conn.execute("SELECT COUNT(*) FROM items WHERE batch_id=?", (bid,)).fetchone()[0]
    bot.send_message(f"✓ {n} item{'s' if n != 1 else ''} — the cards follow one at a time", reply_to=mid)
    return f"batch {bid}: confirmed {text!r}"


def _reply_item(s: Settings, db: DB, bot: Bot, iid: str, text: str, mid: int | None) -> str:
    price, note = parse_reply(text)
    if price is None and note is None:
        bot.send_message(ITEM_HINT, reply_to=mid)
        return f"item {iid}: empty reply"
    if note is None:
        return _set_price(s, db, bot, iid, price, mid)
    recorded, errors = [], []
    if price is not None:                              # kept through the reprocessing: never asked again
        try:
            pipeline.set_price(s, db, iid, price)
            recorded.append(f"${price}")
        except ValueError as e:
            errors.append(str(e))
    try:
        outcome = pipeline.answer(s, db, iid, note)
        recorded.append("same item: dropped" if outcome == "dropped" else f"noted {note!r}, reprocessing")
    except ValueError as e:
        errors.append(str(e))
    if recorded:
        db.outbox_resolve("item", iid)
    lines = (["✓ " + ", ".join(recorded)] if recorded else []) + errors
    bot.send_message("\n".join(lines), reply_to=mid)
    return f"item {iid}: " + "; ".join(recorded + [f"error: {e}" for e in errors])


def _reply_owner_q(s: Settings, db: DB, bot: Bot, iid: str, text: str, mid: int | None) -> str:
    if not text:
        bot.send_message(f"Reply to this message with the answer for {iid}.", reply_to=mid)
        return f"owner_q {iid}: empty reply"
    try:
        pipeline.answer(s, db, iid, text)
    except ValueError as e:
        bot.send_message(str(e), reply_to=mid)
        return f"owner_q {iid}: rejected: {e}"
    db.outbox_resolve("owner_q", iid)
    bot.send_message("✓ got it — reprocessing", reply_to=mid)
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
            try:
                bot.send_message(f"Sorry, that failed: {type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                pass
        if (uid := u.get("update_id")) is not None:
            db.kv_set(OFFSET_KEY, str(uid))
        n += 1
    return n


def resend_pending(s: Settings, db: DB, force: bool = False) -> list[str]:
    """Send the open message again — only that one (WO20) — once it has waited longer than
    telegram.resend_after_hours, or at once with force=True (the worker starts: whatever waited over a sleep). The
    copy above keeps working (its buttons, replies to it). With nothing open, the next message goes out instead, so
    a restart never stalls the queue. Returns the refs re-sent."""
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
        if not force and row["sent_at"] >= (_now() - timedelta(hours=hours)).isoformat(timespec="seconds"):
            return []
        db.outbox_resolve(row["kind"], row["ref"])
        _send(s, db, row["kind"], row["ref"])
        return [row["ref"]]
    finally:
        _unlock(db, token)
