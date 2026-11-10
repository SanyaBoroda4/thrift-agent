"""The two timers (WO33).

`every_15_min`: the shipping reminders (deadlines.reminders_due, from 09:00 local — "📦 Ship tomorrow" and "📦 Due
today" to the group; overdue, WO38: "📦 Overdue: … — ship today" in the group when the site itself, read after the
ship-by day, still shows it unshipped, else the ops line — each recorded in `reminders` before it is sent, so never
twice, and none once a sale is shipped or its site shows it on its way); the Gmail reader's heartbeat (silent 30
minutes → ONE "Gmail reader stopped" to the ops chat per episode, "✓ Gmail reader back" when it beats again); the
failed emails tried again (3 attempts in all).
In off mode it does nothing.

`daily` (the timer runs hourly; the work is done once a local day, at 3 AM New York): the emails' text cleared after
30 days, OTHER emails deleted after 90 days, the uploaded shares' rows after 30 days (WO45, uploads.cleanup).

WO54 §3: every net here that judges the Mac (its silence, the late take-downs, the unposted and 2-hour nets, the share
and upload watchdogs) knows the owner's quiet hours (quiet.py, from the Mac's heartbeat): it says nothing while the Mac
is quiet on purpose or starting back, and its clock counts only the Mac's working time. The reminders, the Gmail
reader's watch and the sales lines are not the Mac's: they go on as before."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from . import core, deadlines, ebay, orders, quiet, uploads, util, watch
from .db import Database
from .util import iso, parse_time, site_name

log = logging.getLogger(__name__)

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
    q = quiet.read(db)                                # WO54 §3: the quiet hours, as the Mac's last heartbeat said them
    return {"mode": mode,
            "reclassified": len(core.reclassify(db, now, mode)),   # WO49: an email a fixed rule now reads (before the
            "reminders": reminders(db, now, mode), "gmail": gmail_watch(db, now, mode),   # take-downs' safety net)
            "mac": mac_watch(db, now, mode, q),
            "retried": retry_failed(db, now, mode), "late_takedowns": core.say_late_takedowns(db, now, mode, q),
            "unposted": watch.unposted(db, now, mode, q),       # WO45 §7: an item priced but not live on a site
            "not_live": watch.not_live(db, now, mode, q),       # WO52 §5: … 2 h after its price, whatever the reason
            "ebay": _ebay(ebay.consent_watch, db, now, mode),   # WO57: eBay's permission — refused, or near its end
            "quiet": quiet.describe(now, q)["now"]}


def _ebay(job, db: Database, now: datetime, mode: str):
    """WO57: one piece of eBay's work in a timer. A failure there never stops the timer's other work: it is logged as
    its type and place (never its message: a token or an order could be in it) and the next run tries again. Azure's
    eBay work is not the Mac's: it goes on in the owner's quiet hours."""
    try:
        return job(db, now, mode)
    except Exception as e:  # noqa: BLE001
        log.error("the timer's eBay work failed: %s", util.where(e))
        return "failed"


def every_5_min(db: Database, now: datetime | None = None, mode: str | None = None) -> dict:
    """WO45 §3: the share watchdog — a complete share with no outcome after 15 minutes (30 past 20 photos) while the
    Mac is online → one private line per share. WO49: and an upload the Mac hasn't fetched 10 minutes after it ended
    (live 2026-10-24: a 20-photo share sat in Azure 71 minutes, never watched — the watchdog saw only shares the Mac
    had taken). WO54 §3: both rest in the quiet hours and count the Mac's working time only.
    WO57: eBay's orders read (a sale there → the 💰 line, the take-downs elsewhere) and every open eBay take-down done
    (ebay.tick) — whatever the Mac does, in the quiet hours too."""
    now, mode = (util.aware(now) if now else util.utcnow()), mode or util.mode()
    if mode == "off":
        return {"mode": mode}
    q = quiet.read(db)
    return {"mode": mode, "shares": watch.shares_watch(db, now, mode, q), "uploads": watch.uploads_watch(db, now, mode, q),
            "ebay": _ebay(ebay.tick, db, now, mode)}


def evening(db: Database, now: datetime | None = None, mode: str | None = None) -> dict:
    """WO45 §9: the day's one line in the 21:00 hour (the hourly timer)."""
    return {"line": watch.evening_line(db, now, mode)}


MAC_SILENCE = timedelta(minutes=30)      # WO53 §4: 2 h was most of the evening's outage (asleep 5:19 PM, told 7:30)
MAC_DOWN = "mac_down_since"              # settings: the outage, while it lasts
MAC_HOURS = (8, 23)                      # local hours in which the silence is told


def clock(moment: datetime | None, now: datetime | None = None) -> str:
    """"5:19 PM" (the owner's zone) — with the day when it isn't today ("Sun Nov 8, 5:19 PM EST")."""
    if moment is None:
        return "when it last spoke"
    when = deadlines.local(moment)
    if now is not None and deadlines.local(now).date() != when.date():
        return core.local_text(moment)
    return f"{when.hour % 12 or 12}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}"


def mac_silent(seen: datetime | None, now: datetime, q: quiet.Quiet, after: timedelta = MAC_SILENCE) -> bool:
    """WO54 §3: the Mac silent `after` of its working time — the quiet hours (8:30 AM – 2:00 PM) don't count: it runs
    nothing then on purpose, so no heartbeat is missing."""
    return seen is None or quiet.active_seconds(seen, now, q) > after.total_seconds()


def mac_watch(db: Database, now: datetime, mode: str, q: quiet.Quiet | None = None) -> str:
    """WO42 → WO53 §4: the Mac's heartbeat (every 15 minutes while it is open, and on every wake) missing for 30
    minutes between 08:00 and 23:00 local → ONE private line per outage, in plain words that say what to check: "The
    Mac stopped answering at 5:19 PM. Is it plugged in, with the lid open? Her photos are safe in the cloud and will go
    up when it's back."; nothing at night (said in the morning if it is still silent); when it is back, ONE line: "Mac
    back at 9:12 PM — 2 shares waiting, working on them." — only for an outage whose line was said.
    WO54 §3: the 30 minutes are the Mac's working time (quiet.active_seconds: 8:30 AM – 2:00 PM don't count), and
    nothing is said in the quiet hours or the 30 minutes after they end (the Mac starting back); a Mac that doesn't
    come back after them is said as that ("didn't come back after its quiet hours").
    "ok" | "offline" (said, or already said) | "night" (silent, not said) | "quiet" (in or just after the quiet hours:
    not said) | "back" | "never"."""
    beat = db.one("SELECT last_seen FROM heartbeats WHERE source = 'mac'")
    if beat is None:
        return "never"
    q = q or quiet.read(db)
    seen = parse_time(beat["last_seen"])
    silent = mac_silent(seen, now, q)
    down = core.setting(db, MAC_DOWN)
    if not silent:
        if down:
            waiting = [s for s in uploads.pending(db)["shares"] if not s["test"]]
            n = len(waiting)
            out = core.Outbox(now, mode)
            with db.tx():
                core.drop_setting(db, MAC_DOWN)
                out.ops(f"Mac back at {clock(seen, now)}" + (f" — {n} share{'s' if n != 1 else ''} waiting, working on "
                                                              f"{'them' if n != 1 else 'it'}." if n else "."))
            out.flush()
            return "back"
        return "ok"
    if down:
        return "offline"
    if quiet.settling(now, q):
        return "quiet"                                # WO54 §3: quiet on purpose, or starting back: never an outage
    if not (MAC_HOURS[0] <= deadlines.local(now).hour < MAC_HOURS[1]):
        return "night"
    ended = quiet.ended_between(seen, now, q) if seen else None
    if ended is not None:                             # silent through the quiet hours, and not back since they ended
        what = (f"The Mac didn't come back after its quiet hours (they ended at {clock(ended, now)}; last heard from at "
                f"{clock(seen, now)}).")
    else:
        what = f"The Mac stopped answering at {clock(seen, now)}."
    out = core.Outbox(now, mode)
    with db.tx():
        core.put_setting(db, MAC_DOWN, beat["last_seen"])
        out.ops(f"{what} Is it plugged in, with the lid open? Her photos are safe in the cloud and will go up when "
                "it's back.")
    out.flush()
    return "offline"


def reminders(db: Database, now: datetime, mode: str) -> list[list[str]]:
    """The reminders due now, [sale id, kind] each, recorded and then sent. WO38: a sale its site shows on its way
    (shipped, delivered, completed — the order sweep) gets none; on a site the sweep reads, "overdue" is the site's own
    word: "📦 Overdue: <title> (<Site>) — ship today" in the group, once, only when the site — read after the ship-by
    day ended — still shows it waiting to ship (a sale the sweep never tied to an order keeps the ops line)."""
    rows = db.query("SELECT s.id, s.marketplace, s.ship_by, s.shipped_at, s.status, s.title_seen, i.title AS item_title, "
                    "o.status AS site_status, o.last_seen AS site_seen "
                    "FROM sales s LEFT JOIN items i ON i.id = s.item_id LEFT JOIN orders o ON o.sale_id = s.id "
                    "WHERE s.shipped_at IS NULL "
                    "AND s.ship_by IS NOT NULL AND s.status NOT IN ('cancelled', 'double_sale', 'done', 'merged') "
                    # WO57: an eBay sale of a listing that isn't ours is hers (core.hers): no reminder of it
                    "AND NOT (s.marketplace = 'ebay' AND s.item_id IS NULL) "
                    "ORDER BY s.ship_by, s.sold_at, s.id")
    sent = {(row["sale_id"], row["kind"]) for row in db.query("SELECT sale_id, kind FROM reminders")}
    swept = orders.swept(db)
    due = deadlines.reminders_due(now, rows, sent)
    by_id = {row["id"]: row for row in rows}
    out = core.Outbox(now, mode)
    done = []
    with db.tx():
        for sale_id, kind in due:
            row = by_id[sale_id]
            if row["site_status"] in LEFT_ON_SITE:
                continue                              # the site shows it on its way (or not to be shipped): nothing
            if kind == "overdue" and row["marketplace"] in swept and row["site_status"] is not None:
                continue                              # the site's own reading decides (below)
            if not _record(db, sale_id, kind, now):
                continue                              # another run sent it
            title, site = row["item_title"] or row["title_seen"] or "an item", site_name(row["marketplace"])
            day = deadlines.fmt_day(core.day_of(row["ship_by"]))
            if kind == "day_before":
                out.group(f"📦 Ship tomorrow: {title} ({site}) — due {day}.")
            elif kind == "due_today":
                out.group(f"📦 Due today: {title} ({site}).")
            else:
                out.ops(f"⏰ Overdue: {title} ({site}) — ship-by was {day} and no shipment is recorded ({sale_id}).")
            done.append([sale_id, kind])
        for row in rows:
            if (row["id"], SITE_OVERDUE) in sent or not site_overdue(row, swept, now):
                continue
            if not _record(db, row["id"], SITE_OVERDUE, now):
                continue
            title, site = row["item_title"] or row["title_seen"] or "an item", site_name(row["marketplace"])
            out.group(f"📦 Overdue: {title} ({site}) — ship today")
            done.append([row["id"], SITE_OVERDUE])
    out.flush()
    return done


SITE_OVERDUE = "overdue_site"
LEFT_ON_SITE = ("shipped", "delivered", "completed", "cancelled", "returned")


def _record(db: Database, sale_id: str, kind: str, now: datetime) -> bool:
    return bool(db.execute("INSERT INTO reminders (sale_id, kind, sent_at) VALUES (?, ?, ?) "
                           "ON CONFLICT (sale_id, kind) DO NOTHING", (sale_id, kind, iso(now))))


def site_overdue(row: dict, swept: dict, now: datetime, at: str = "09:00") -> bool:
    """The ship-by day is over and the site, read after it ended, still shows the order waiting to ship (from `at`
    local time, as the other reminders)."""
    if row["marketplace"] not in swept or row["site_status"] != "to_ship" or not row["ship_by"] or row["shipped_at"]:
        return False
    local_now = deadlines.local(now)
    if local_now.time() < deadlines._hm(at):
        return False
    ship_by = core.day_of(row["ship_by"])
    if local_now.date() <= ship_by:
        return False
    seen = parse_time(row["site_seen"])
    day_over = datetime.combine(ship_by + timedelta(days=1), datetime.min.time(), tzinfo=local_now.tzinfo)
    return seen is not None and deadlines.local(seen) >= day_over


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
        shares = uploads.cleanup(db, now)                                       # WO45 §2: the blobs go by Azure's rule
        core.put_setting(db, CLEANUP_DONE, today)
    return {"ran": True, "raw_text_cleared": cleared, "other_deleted": deleted, "shares_deleted": shares}
