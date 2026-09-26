"""Owner conversation over Telegram: batch confirmation, ONE price-approval message per item, the poster's
needs_owner questions, and the long-poll loop that turns replies and button presses into pipeline calls.

The agent owns the bot (long polling via getUpdates, offset persisted in the kv table). Works in a group with
BotFather privacy mode ON: everything the owner does is a reply to one of the bot's messages or a button press.
Only TELEGRAM_CHAT_ID and senders in TELEGRAM_ALLOWED_USER_IDS are accepted; everything else is ignored.

The signatures below are the contract used by pipeline.py, cli.py and post/runner.py — keep them."""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thrift_agent import notify, pipeline
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads
from thrift_agent.telegram import MAX_CAPTION, Bot

OFFSET_KEY = "telegram_offset"                       # kv: the last getUpdates update_id we handled
WAITING_ITEM = ("awaiting_price", "needs_info")      # item statuses whose price message is still worth re-sending
BATCH_HINT = "Reply to this message: ok | 12>2 | split 7 | merge 2 3 | drop 7"
ITEM_HINT = "Reply with a number (the price) or an answer like 'size 8, 45'"

_NUM = r"\d+(?:\.\d+)?"
# A number that is money, whichever way the owner says it: "$85", "$ 85", "85 usd", "85 dollars", "price 85", "list: 85".
_MONEY = re.compile(rf"(?:\$\s*({_NUM})|\b({_NUM})\s*(?:usd|dollars?|bucks)\b|\b(?:price|list)\b\s*[:=]?\s*\$?\s*({_NUM}))",
                    re.I)
# A standalone number: not glued to letters or digits (7.5M, 5T, 8-9, 8/9) and not a decimal's tail.
_BARE = re.compile(rf"(?<![\w.$/-])({_NUM})(?![\w/-]|\.\d)")
_SIZE_BEFORE = re.compile(r"\b(?:size|sz|eu|us|uk|toddler|kids?|y|c|cm|in|inch)\s*[:=#]?\s*$", re.I)
_UNIT_AFTER = re.compile(r"\s*(?:cm|mm|in|inch|inches|us|uk|eu|y|t|m|w)\b", re.I)

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


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*([,;])\s*(?:[,;]\s*)+", r"\1 ", text)          # ", ," -> ","
    text = re.sub(r"\s+([,;:.])", r"\1", text)
    return text.strip(" ,;:-–—\t\n")


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
        notify.photo(sheet, caption)
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


def item_caption(iid: str, it) -> tuple[str, int | None]:
    """(caption, suggested price) for the item's approval message. Pure: reads the row's JSON columns only."""
    facts = loads(it["facts"]) or {}
    pr = loads(it["price"]) or {}
    renders = loads(it["renders"]) or {}
    gate = loads(it["gate"]) or {}
    posh = renders.get("poshmark") or next(iter(renders.values()), None) or {}
    brand = _ev(facts, "brand")
    title = posh.get("title") or " ".join(x for x in (brand, facts.get("item_type")) if x) or iid
    lines = [title, f"Item {iid}"]

    size = posh.get("size") or _ev(facts, "size_us")
    if size:
        printed = _ev(facts, "size_printed")
        lines.append(f"Size: {size}" + (f" (printed {printed})" if printed and printed != size else ""))
    cond = facts.get("condition") or "condition unknown"
    n_flaws = len(facts.get("flaws") or [])
    lines.append(f"Condition: {cond}, " + ("no flaws" if n_flaws == 0 else f"{n_flaws} flaw{'s' if n_flaws != 1 else ''}"))

    price = pr.get("list_price")
    price = int(price) if isinstance(price, (int, float)) and price > 0 else None
    if price:
        basis = str(pr.get("basis") or "").strip()
        lines.append(f"Suggested: ${price}" + (f" ({basis[:80]})" if basis else ""))
    else:
        lines.append("Suggested: no price")
    if retail := _ev(facts, "retail_price"):
        lines.append(f"Retail ${retail}")
    if pr.get("source") in ("category_default", "none"):
        lines.append(f"no price history for {brand or 'this brand'}")

    if gate.get("decision") == "needs_info":
        lines.append("Open questions:")
        lines += [f"- {r}" for r in gate.get("reasons") or []]
        lines.append("Reply with the answers and the price, e.g. 'size 8, 45'")
    else:
        lines.append("Reply with a number to change the price.")
    return "\n".join(lines), price


def item_buttons(iid: str, price: int | None) -> list[list[dict]]:
    row = []
    if price:
        row.append({"text": f"Approve ${price}", "callback_data": f"approve:{iid}:{price}"})
    row.append({"text": "Change", "callback_data": f"change:{iid}"})
    return [row]


def send_item(s: Settings, db: DB, iid: str) -> None:
    """ONE message per item: cover photo, title, size (with system), condition + flaw count, suggested price + basis,
    "Retail $X" if known, "no price history for <brand>" for a category default, the open questions (if any), and
    inline buttons [Approve $P] [Change]. Records the outbox row. Without a bot: print (dev)."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    caption, price = item_caption(iid, it)
    cover = Path(it["dir"]) / "cover.jpg"
    bot = bot_for(s)
    if bot is None:
        notify.photo(cover, caption) if cover.is_file() else notify.say(caption)
        return
    buttons = item_buttons(iid, price)
    if cover.is_file() and len(caption) <= MAX_CAPTION:
        mid = bot.send_photo(cover, caption, buttons)
    else:
        mid = bot.send_message(caption, buttons)
    db.add_outbox(bot.chat_id, mid, "item", iid, text=caption)


def ask_owner(s: Settings, db: DB, iid: str, question: str) -> None:
    """The poster's separate question when stuck on a field only the owner can answer (kind owner_q)."""
    text = f"Question about {iid}: {question}\nReply to this message."
    bot = bot_for(s)
    if bot is None:
        notify.say(text)
        return
    mid = bot.send_message(text)
    db.add_outbox(bot.chat_id, mid, "owner_q", iid, text=question)


# ---------- incoming ----------

def handle_update(s: Settings, db: DB, bot: Bot, update: dict) -> str:
    """Route one Telegram update (message reply or callback_query) to pipeline.confirm / set_price / answer.
    Returns a short description of what happened (for the log); unauthorised or unrelated updates are ignored."""
    if not bot.authorized(update):
        return "ignored: unauthorized"
    if isinstance(update.get("callback_query"), dict):
        return _handle_callback(s, db, bot, update["callback_query"])
    msg = update.get("message")
    if not isinstance(msg, dict):
        return "ignored: unsupported update"
    reply = msg.get("reply_to_message")
    row = db.outbox_lookup(msg["chat"]["id"], reply["message_id"]) if isinstance(reply, dict) else None
    if row is None:
        return "ignored: not a reply to the bot"
    text = (msg.get("text") or msg.get("caption") or "").strip()
    mid = msg.get("message_id")
    kind, ref = row["kind"], row["ref"]
    if kind == "batch":
        return _reply_batch(s, db, bot, ref, text, mid)
    if kind == "item":
        return _reply_item(s, db, bot, ref, text, mid)
    if kind == "owner_q":
        return _reply_owner_q(s, db, bot, ref, text, mid)
    return f"ignored: unknown outbox kind {kind}"


def _handle_callback(s: Settings, db: DB, bot: Bot, cq: dict) -> str:
    cid = cq.get("id")
    parts = (cq.get("data") or "").split(":")
    src_mid = (cq.get("message") or {}).get("message_id")
    if parts[0] == "approve" and len(parts) == 3:
        iid = parts[1]
        try:
            amount = int(parts[2])
            status = pipeline.set_price(s, db, iid, amount)
        except ValueError as e:
            bot.answer_callback(cid, "Could not set the price")
            bot.send_message(str(e), reply_to=src_mid)
            return f"approve {iid}: rejected: {e}"
        db.outbox_resolve("item", iid)
        bot.answer_callback(cid, f"Approved ${amount}")
        bot.send_message(f"{iid}: approved at ${amount} -> {status}", reply_to=src_mid)
        return f"approved {iid} at ${amount} ({status})"
    if parts[0] == "change" and len(parts) == 2:
        iid = parts[1]
        mid = bot.send_message(f"Reply to this message with the price for {iid}.", reply_to=src_mid)
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
    bot.send_message(f"Batch {bid}: split ok ({text})", reply_to=mid)
    return f"batch {bid}: confirmed {text!r}"


def _reply_item(s: Settings, db: DB, bot: Bot, iid: str, text: str, mid: int | None) -> str:
    price, note = parse_reply(text)
    if price is None and note is None:
        bot.send_message(ITEM_HINT, reply_to=mid)
        return f"item {iid}: empty reply"
    recorded, errors = [], []
    if price is not None:
        try:
            status = pipeline.set_price(s, db, iid, price)
            recorded.append(f"price ${price} -> {status}")
        except ValueError as e:
            errors.append(str(e))
    if note is not None:
        try:
            pipeline.answer(s, db, iid, note)
            recorded.append(f"note {note!r} - reprocessing")
        except ValueError as e:
            errors.append(str(e))
    if recorded:
        db.outbox_resolve("item", iid)
    lines = ([f"{iid}: recorded " + "; ".join(recorded)] if recorded else []) + errors
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
    bot.send_message(f"{iid}: got it - {text!r} (reprocessing)", reply_to=mid)
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
    """Send again every batch/item/question still waiting longer than telegram.resend_after_hours (all of them when
    force=True, e.g. on worker start). Returns the refs re-sent."""
    if bot_for(s) is None:
        return []
    hours = float(s.get("telegram.resend_after_hours", 6) or 0)
    older = None if force else (_now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    sent: list[str] = []
    for row in db.outbox_pending(older):
        kind, ref = row["kind"], row["ref"]
        if kind == "batch":
            b = db.batch(ref)
            if b is not None and b["status"] == "needs_confirm":
                send_batch(s, db, ref)
                sent.append(ref)
            else:
                db.outbox_resolve("batch", ref)
        elif kind == "item":
            it = db.item(ref)
            if it is not None and it["status"] in WAITING_ITEM:
                send_item(s, db, ref)
                sent.append(ref)
            else:
                db.outbox_resolve("item", ref)
        elif kind == "owner_q":
            it = db.item(ref)
            if it is not None and it["status"] == "needs_owner":
                ask_owner(s, db, ref, row["text"] or "the poster needs an answer for this item")
                sent.append(ref)
            else:
                db.outbox_resolve("owner_q", ref)
        else:
            db.outbox_resolve(kind, ref)
    return sent
