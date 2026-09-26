"""Owner conversation over Telegram: batch confirmation, ONE price-approval message per item, the poster's
needs_owner questions, and the long-poll loop that turns replies and button presses into pipeline calls.

The agent owns the bot (long polling via getUpdates, offset persisted in the kv table). Works in a group with
BotFather privacy mode ON: everything the owner does is a reply to one of the bot's messages or a button press.
Only TELEGRAM_CHAT_ID and senders in TELEGRAM_ALLOWED_USER_IDS are accepted; everything else is ignored.

This file is a skeleton: another agent fills in the bodies. The signatures below are the contract used by
pipeline.py, cli.py and post/runner.py — keep them."""
from __future__ import annotations

from thrift_agent.config import Settings
from thrift_agent.db import DB
from thrift_agent.telegram import Bot


def bot_for(s: Settings) -> Bot | None:
    """The configured Bot, or None when telegram.enabled is false or the env vars are missing (dev: messages print)."""
    raise NotImplementedError


def parse_reply(text: str) -> tuple[int | None, str | None]:
    """(price, note) from an owner reply: "85" / "$85" / "85.00" / "85 dollars" -> (85, None);
    "size 8, 45" -> (45, "size 8"); "size 8" -> (None, "size 8")."""
    raise NotImplementedError


def send_batch(s: Settings, db: DB, bid: str) -> None:
    """Contact sheet + summary; the owner replies ok / 12>2 / split 7 / merge 2 3 / drop 7. Records the outbox row.
    Without a bot: print the caption (dev)."""
    raise NotImplementedError


def send_item(s: Settings, db: DB, iid: str) -> None:
    """ONE message per item: cover photo, title, size (with system), condition + flaw count, suggested price + basis,
    "Retail $X" if known, "no price history for <brand>" for a category default, the open questions (if any), and
    inline buttons [Approve $P] [Change]. Records the outbox row. Without a bot: print (dev)."""
    raise NotImplementedError


def ask_owner(s: Settings, db: DB, iid: str, question: str) -> None:
    """The poster's separate question when stuck on a field only the owner can answer (kind owner_q)."""
    raise NotImplementedError


def handle_update(s: Settings, db: DB, bot: Bot, update: dict) -> str:
    """Route one Telegram update (message reply or callback_query) to pipeline.confirm / set_price / answer.
    Returns a short description of what happened (for the log); unauthorised or unrelated updates are ignored."""
    raise NotImplementedError


def poll_once(s: Settings, db: DB, bot: Bot, timeout: int) -> int:
    """One getUpdates long poll from the persisted offset; handles each update and persists the offset after it.
    Returns the number of updates handled."""
    raise NotImplementedError


def resend_pending(s: Settings, db: DB, force: bool = False) -> list[str]:
    """Send again every batch/item/question still waiting longer than telegram.resend_after_hours (all of them when
    force=True, e.g. on worker start). Returns the refs re-sent."""
    raise NotImplementedError
