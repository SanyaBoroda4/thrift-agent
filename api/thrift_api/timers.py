"""The two timers (WO33).

`every_15_min`: the shipping reminders (deadlines.reminders_due, from 09:00 local — "📦 Ship tomorrow" and "📦 Due
today" to the group, an overdue sale to the ops chat — each recorded in `reminders` before it is sent, so never twice,
and none once a sale is shipped); the Gmail reader's heartbeat (silent 30 minutes → ONE "Gmail reader stopped" to the
ops chat per episode, "✓ Gmail reader back" when it beats again); the failed emails tried again (3 attempts in all).
In off mode it does nothing.

`daily` (the timer runs hourly; the work is done once a local day, at 3 AM New York): the emails' text cleared after
30 days, OTHER emails deleted after 90 days."""
from __future__ import annotations

from datetime import datetime, timedelta

from . import core, deadlines, util
from .db import Database
from .util import iso, parse_time, site_name

GMAIL_SILENCE = timedelta(minutes=30)
GMAIL_DOWN = "gmail_down_since"          # settings: the episode, while it lasts
CLEANUP_HOUR = 3
CLEANUP_DONE = "daily_cleanup_day"       # settings: the local day the cleanup last ran
TEXT_DAYS = 30
OTHER_DAYS = 90


def every_15_min(db: Database, now: datetime | None = None, mode: str | None = None) -> dict:
    now, mode = (util.aware(now) if now else util.utcnow()), mode or util.mode()
    if mode == "off":
        return {"mode": mode}
    if mode == "live":
        core.go_live(db, now)                         # a live call like any request: starts the live clock if needed
    return {"mode": mode, "reminders": reminders(db, now, mode), "gmail": gmail_watch(db, now, mode),
            "retried": retry_failed(db, now, mode)}


def reminders(db: Database, now: datetime, mode: str) -> list[list[str]]:
    """The reminders due now, [sale id, kind] each, recorded and then sent."""
    rows = db.query("SELECT s.id, s.marketplace, s.ship_by, s.shipped_at, s.status, s.title_seen, i.title AS item_title "
                    "FROM sales s LEFT JOIN items i ON i.id = s.item_id WHERE s.shipped_at IS NULL "
                    "AND s.ship_by IS NOT NULL AND s.status NOT IN ('cancelled', 'double_sale', 'done', 'merged') "
                    "ORDER BY s.ship_by, s.sold_at, s.id")
    sent = {(row["sale_id"], row["kind"]) for row in db.query("SELECT sale_id, kind FROM reminders")}
    due = deadlines.reminders_due(now, rows, sent)
    by_id = {row["id"]: row for row in rows}
    out = core.Outbox(now, mode)
    done = []
    with db.tx():
        for sale_id, kind in due:
            if not db.execute("INSERT INTO reminders (sale_id, kind, sent_at) VALUES (?, ?, ?) "
                              "ON CONFLICT (sale_id, kind) DO NOTHING", (sale_id, kind, iso(now))):
                continue                              # another run sent it
            row = by_id[sale_id]
            title, site = row["item_title"] or row["title_seen"] or "an item", site_name(row["marketplace"])
            day = deadlines.fmt_day(core.day_of(row["ship_by"]))
            if kind == "day_before":
                out.group(f"📦 Ship tomorrow: {title} ({site}) — due {day}.")
            elif kind == "due_today":
                out.group(f"📦 Due today: {title} ({site}).")
            else:
                out.ops(f"⏰ Overdue: {title} ({site}) — ship-by was {day} and no shipment is recorded ({sale_id}).")
            done.append([sale_id, kind])
    out.flush()
    return done


def gmail_watch(db: Database, now: datetime, mode: str) -> str:
    """"ok" | "stopped" (silent past 30 minutes: said once) | "back" (said once) | "never" (no beat yet: nothing to
    miss)."""
    beat = db.one("SELECT last_seen FROM heartbeats WHERE source = 'gmail'")
    if beat is None:
        return "never"
    seen = parse_time(beat["last_seen"])
    silent = seen is None or now - seen > GMAIL_SILENCE
    down = core.setting(db, GMAIL_DOWN)
    if silent == bool(down):
        return "stopped" if silent else "ok"
    out = core.Outbox(now, mode)
    with db.tx():
        if silent:
            core.put_setting(db, GMAIL_DOWN, beat["last_seen"])
            last = core.local_text(seen) if seen else "never"
            out.ops(f"⚠️ Gmail reader stopped — nothing from it since {last}. Check its Executions in Apps Script.")
        else:
            core.drop_setting(db, GMAIL_DOWN)
            out.ops("✓ Gmail reader back")
    out.flush()
    return "stopped" if silent else "back"


def retry_failed(db: Database, now: datetime, mode: str) -> list[dict]:
    """Every failed email with attempts left, processed again."""
    rows = db.query("SELECT message_id FROM email_events WHERE status = 'failed' AND COALESCE(attempts, 0) < ? "
                    "ORDER BY created_at, message_id", (core.MAX_ATTEMPTS,))
    return [{"message_id": row["message_id"], "status": core.retry_email(db, row["message_id"], now, mode)["status"]}
            for row in rows]


def daily(db: Database, now: datetime | None = None) -> dict:
    """The cleanup, once a local day in the 3 AM hour (whatever the mode)."""
    now = util.aware(now) if now else util.utcnow()
    local = deadlines.local(now)
    if local.hour != CLEANUP_HOUR:
        return {"ran": False, "reason": f"runs at {CLEANUP_HOUR} AM New York time"}
    today = local.date().isoformat()
    if core.setting(db, CLEANUP_DONE) == today:
        return {"ran": False, "reason": "already ran today"}
    with db.tx():
        cleared = db.execute("UPDATE email_events SET raw_text = NULL WHERE raw_text IS NOT NULL AND created_at < ?",
                             (iso(now - timedelta(days=TEXT_DAYS)),))
        deleted = db.execute("DELETE FROM email_events WHERE kind = 'OTHER' AND created_at < ?",
                             (iso(now - timedelta(days=OTHER_DAYS)),))
        core.put_setting(db, CLEANUP_DONE, today)
    return {"ran": True, "raw_text_cleared": cleared, "other_deleted": deleted}
