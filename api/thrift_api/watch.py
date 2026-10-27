"""WO45: the safety nets that don't depend on the Mac — a complete share with no outcome (§3), an item priced but not
live on a site (§7), and the day's one line at 21:00 (§9). All from the Mac's own records mirrored by /sync (the
share ledger, the items, the listings with their reasons), judged here on the API's clock.

Live (Oct 12–18): 4 of 23 shares never became a card and nothing said so; the Ralph Lauren shirt never reached
Poshmark (one failed attempt, the next window never came that day) and nobody heard. The Mac tells what it knows; the
API tells what the Mac didn't."""
from __future__ import annotations

from datetime import datetime, timedelta

from . import core, deadlines, util
from .db import Database
from .util import iso, parse_time, site_name

STUCK_AFTER = timedelta(minutes=15)       # a complete share with no outcome this long → ⏳
STUCK_AFTER_BIG = timedelta(minutes=30)   # ... a share of more than BIG_SHARE photos
BIG_SHARE = 20
MAC_ONLINE = timedelta(minutes=20)        # the Mac's heartbeat this recent = it is up (else WO42's offline line)
STUCK_SAID = "share_stuck:"               # settings: + share id — the ⏳ line said (once)
UNPOSTED_AFTER = timedelta(hours=1)       # an item priced this long ago and not live on a site
REPOST = "repost:"                        # settings: + item|site — the retry asked, when
UNPOSTED_SAID = "unposted_said:"          # settings: + item|site — the ⚠️ line said (once)
POSTING_HOURS = (8, 22)                   # local hours in which a missing listing is chased
EVENING_HOUR = 21
EVENING_DONE = "evening_line_day"         # settings: the local day the 📊 line went
# a listing row left as it is — its reason is the owner's to settle, or a state of its own (the Mac says those itself)
KEEP_REASONS = ("no size on the item", "shared again", "already live", "sold", "not published automatically",
                "unconfirmed publish", "mismatch", "retried by the safety net", "skipped: already",
                "refused the form",
                "waiting for the brand")   # the Mac's own re-check settles a refused form (WO39; live 2026-10-20 the net
#                                       re-queued the Tory Burch before that re-check could run)


def _mac_online(db: Database, now: datetime) -> bool:
    beat = db.one("SELECT last_seen FROM heartbeats WHERE source = 'mac'")
    seen = parse_time(beat["last_seen"]) if beat else None
    return seen is not None and now - seen <= MAC_ONLINE


def _share_when(share_id: str) -> str:
    """"12:31" from a share id (2026-10-18_123105…), else the id itself."""
    import re
    m = re.match(r"\d{4}-\d{2}-\d{2}_(\d{2})(\d{2})\d{2}", share_id)
    return f"{int(m[1]) % 12 or 12}:{m[2]} {'AM' if int(m[1]) < 12 else 'PM'}" if m else share_id


def _step_of(row: dict) -> tuple[str, str | None]:
    """The last step the share reached and when: carded > grouped > complete > received."""
    for name in ("carded", "grouped", "complete", "received"):
        if row.get(f"{name}_at"):
            return name, row[f"{name}_at"]
    return "received", None


def shares_watch(db: Database, now: datetime | None = None, mode: str | None = None) -> list[str]:
    """§3, every 5 minutes: a share complete (taken by the Mac) with no outcome after STUCK_AFTER (STUCK_AFTER_BIG past
    BIG_SHARE photos) while the Mac is online → ONE private line per share, "⏳ Share 12:31 (4 photos) stuck at
    grouped since 12:33". Nothing while the Mac is offline (WO42's line covers that), nothing once an outcome came.
    Returns the share ids said now."""
    now, mode = (util.aware(now) if now else util.utcnow()), mode or util.mode()
    if mode == "off" or not _mac_online(db, now):
        return []
    out = core.Outbox(now, mode)
    said = []
    with db.tx():
        for row in db.query("SELECT * FROM shares WHERE complete_at IS NOT NULL AND outcome IS NULL "
                            "AND COALESCE(test, 0) = 0 ORDER BY complete_at"):
            done = parse_time(row["complete_at"])
            if done is None:
                continue
            limit = STUCK_AFTER_BIG if int(row["received"] or 0) > BIG_SHARE else STUCK_AFTER
            if now - done < limit or core.setting(db, STUCK_SAID + row["id"]):
                continue
            step, at = _step_of(row)
            since = parse_time(at)
            when = deadlines.local(since).strftime("%H:%M") if since else "?"
            photos = int(row["received"] or 0)
            out.ops(f"⏳ Share {_share_when(row['id'])} ({photos} photo{'s' if photos != 1 else ''}) stuck at {step} "
                    f"since {when}")
            core.put_setting(db, STUCK_SAID + row["id"], iso(now))
            said.append(row["id"])
    out.flush()
    return said


UNFETCHED_AFTER = timedelta(minutes=10)    # WO49: an upload this long after its last photo, not fetched by the Mac
UNFETCHED_SAID = "upload_unfetched:"       # settings: + share id — the ⏳ line said (once)


def _clock(moment: datetime) -> str:
    """"9:40 AM" (portable: no %-I)."""
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def uploads_watch(db: Database, now: datetime | None = None, mode: str | None = None) -> list[str]:
    """WO49, every 5 minutes: a share the iPhone uploaded (ended, or quiet) that the Mac hasn't fetched
    UNFETCHED_AFTER after its last photo, while the Mac's heartbeat says it is online → ONE private line per share,
    "⏳ Share 9:39 AM (20 photos) is in Azure but the Mac hasn't taken it (since 9:40)". The Mac offline: WO42's line
    covers it. Test shares never. Returns the share ids said now."""
    now, mode = (util.aware(now) if now else util.utcnow()), mode or util.mode()
    if mode == "off" or not _mac_online(db, now):
        return []
    out = core.Outbox(now, mode)
    said = []
    with db.tx():
        for row in db.query("SELECT share_id, received, started_at, ended_at, last_received_at FROM shares_in "
                            "WHERE fetched_at IS NULL AND COALESCE(test, 0) = 0 AND received > 0 ORDER BY started_at"):
            last = parse_time(row["last_received_at"] or row["ended_at"] or row["started_at"])
            if last is None or now - last < UNFETCHED_AFTER or core.setting(db, UNFETCHED_SAID + row["share_id"]):
                continue
            photos = int(row["received"] or 0)
            out.ops(f"⏳ Share {_share_when(row['share_id'])} ({photos} photo{'s' if photos != 1 else ''}) is in Azure "
                    f"but the Mac hasn't taken it (since {_clock(deadlines.local(last))})")
            core.put_setting(db, UNFETCHED_SAID + row["share_id"], iso(now))
            said.append(row["share_id"])
    out.flush()
    return said


def shares_today(db: Database, now: datetime | None = None) -> list[dict]:
    """§3, the dashboard's Shares panel: today's shares (the local day), newest first — id, source, photos, each
    step's time, the outcome and its reason."""
    now = util.aware(now) if now else util.utcnow()
    day = deadlines.local(now).date().isoformat()
    rows = db.query("SELECT * FROM shares WHERE id LIKE ? OR received_at LIKE ? ORDER BY id DESC",
                    (f"{day}%", f"{day}%"))
    return [{k: row[k] for k in ("id", "source", "expected", "received", "received_at", "complete_at", "grouped_at",
                                 "carded_at", "outcome", "reason", "test")} for row in rows]


def _unposted_rows(db: Database, now: datetime) -> list[dict]:
    """The (item, site) pairs the net chases: the item priced (and ready or posted, never a test item), its row on the
    site failed or skipped an hour ago or more, with no reason of the kinds the Mac settles itself."""
    cutoff = iso(now - UNPOSTED_AFTER)
    rows = db.query("SELECT l.item_id, l.marketplace, l.status, l.error, l.updated_at, i.title FROM listings l "
                    "JOIN items i ON i.id = l.item_id WHERE l.status IN ('failed', 'skipped') AND "
                    "COALESCE(i.priced, 0) = 1 AND COALESCE(i.test, 0) = 0 AND COALESCE(i.status, '') IN "
                    "('ready', 'posted') AND l.updated_at <= ? ORDER BY l.updated_at, l.item_id", (cutoff,))
    out = []
    for row in rows:
        error = (row["error"] or "").lower()
        if any(k.lower() in error for k in KEEP_REASONS):
            continue
        out.append(dict(row))
    return out


def unposted(db: Database, now: datetime | None = None, mode: str | None = None) -> dict:
    """§7, every 15 minutes in posting hours: an item priced more than an hour ago that isn't live on a site, with no
    reason the Mac settles itself (a missing size, a twin, sold, autopublish off, an unconfirmed publish, a mismatch)
    → first ONE "repost" task the Mac takes with its take-downs (the row goes back in line once); still not live an
    hour after the Mac answered → ONE private line per item and site: "⚠️ <title> isn't on Poshmark: <reason>".
    {"asked": [...], "said": [...]}."""
    now, mode = (util.aware(now) if now else util.utcnow()), mode or util.mode()
    result: dict = {"asked": [], "said": []}
    if mode != "live":
        return result
    hour = deadlines.local(now).hour
    if not POSTING_HOURS[0] <= hour < POSTING_HOURS[1]:
        return result
    out = core.Outbox(now, mode)
    with db.tx():
        for row in _unposted_rows(db, now):
            key = f"{row['item_id']}|{row['marketplace']}"
            if core.setting(db, UNPOSTED_SAID + key):
                continue
            task = db.one("SELECT id, status, done_at, created_at FROM delist_tasks WHERE item_id = ? AND marketplace = ? "
                          "AND kind = 'repost' ORDER BY created_at DESC LIMIT 1", (row["item_id"], row["marketplace"]))
            if task is None:
                task_id = util.new_id("t_")
                db.execute("INSERT INTO delist_tasks (id, sale_id, item_id, marketplace, listing_url, status, attempts, "
                           "created_at, kind) VALUES (?, NULL, ?, ?, NULL, 'pending', 0, ?, 'repost')",
                           (task_id, row["item_id"], row["marketplace"], iso(now)))
                core.put_setting(db, REPOST + key, iso(now))
                result["asked"].append(key)
                continue
            if task["status"] in ("pending", "running"):
                continue                                      # the Mac hasn't answered yet
            answered = parse_time(task["done_at"]) or parse_time(task["created_at"])
            if answered is None or now - answered < UNPOSTED_AFTER:
                continue                                      # the retry may still be on its way
            fresh = parse_time(row["updated_at"])
            if fresh is not None and fresh < answered and task["status"] == "done":
                pass                                          # the row didn't move since: the retry didn't take
            title = row["title"] or "an item"
            reason = (row["error"] or row["status"] or "no reason recorded").strip()[:160]
            out.ops(f"⚠️ {title} isn't on {site_name(row['marketplace'])}: {reason}")
            core.put_setting(db, UNPOSTED_SAID + key, iso(now))
            result["said"].append(key)
    out.flush()
    return result


def _cleanup_today(db: Database, day: str) -> str:
    """WO49: the Mac's daily cleanup (its heartbeat's `cleanup`, kv cleanup_last there) as the summary's last part —
    " 🧹 Cleanup freed 270 MB." when it ran this local day and freed something; the Mac sends no line of its own."""
    import json
    beat = db.one("SELECT info FROM heartbeats WHERE source = 'mac'")
    try:
        info = json.loads(beat["info"]) if beat and beat["info"] else {}
    except (TypeError, ValueError):
        info = {}
    last = info.get("cleanup") if isinstance(info, dict) else None
    if not isinstance(last, dict) or last.get("dry"):
        return ""
    at = parse_time(last.get("at"))
    freed = util.number(last.get("freed")) or 0
    if at is None or deadlines.local(at).date().isoformat() != day or freed <= 0:
        return ""
    size = f"{freed / 1024 ** 3:.1f} GB" if freed >= 1024 ** 3 else f"{freed / 1024 ** 2:.0f} MB"
    return f" 🧹 Cleanup freed {size}."


def evening_line(db: Database, now: datetime | None = None, mode: str | None = None) -> str | None:
    """§9, once a local day in the 21:00 hour: "📊 Today: 7 shares → 8 items, 7 live on all 3 sites with all photos,
    1 waiting for a size." to the private chat. Returns the line, or None when it isn't the hour or it went already."""
    now, mode = (util.aware(now) if now else util.utcnow()), mode or util.mode()
    local = deadlines.local(now)
    if local.hour != EVENING_HOUR or mode == "off":
        return None
    day = local.date().isoformat()
    if core.setting(db, EVENING_DONE) == day:
        return None
    shares = [r for r in shares_today(db, now) if not r.get("test")]
    items = db.query("SELECT id FROM items WHERE created_at LIKE ? AND COALESCE(test, 0) = 0", (f"{day}%",))
    item_ids = [r["id"] for r in items]
    live_all = 0
    for iid in item_ids:
        rows = db.query("SELECT marketplace, status, photos_sent, photos_live FROM listings WHERE item_id = ?", (iid,))
        posted = {r["marketplace"]: r for r in rows if r["status"] == "posted"}
        if set(posted) >= set(util.SITES) and all(
                r["photos_live"] is None or r["photos_sent"] is None or int(r["photos_live"]) >= int(r["photos_sent"])
                for r in posted.values()):
            live_all += 1
    waiting = db.one("SELECT COUNT(DISTINCT l.item_id) AS n FROM listings l JOIN items i ON i.id = l.item_id WHERE "
                     "l.status = 'skipped' AND LOWER(COALESCE(l.error, '')) LIKE '%no size on the item%' AND "
                     "COALESCE(i.status, '') <> 'posted' AND COALESCE(i.test, 0) = 0")
    n_wait = int(waiting["n"]) if waiting else 0
    text = (f"📊 Today: {len(shares)} share{'s' if len(shares) != 1 else ''} → {len(item_ids)} item"
            f"{'s' if len(item_ids) != 1 else ''}, {live_all} live on all {len(util.SITES)} sites with all photos"
            + (f", {n_wait} waiting for a size" if n_wait else "") + "." + _cleanup_today(db, day))
    out = core.Outbox(now, mode)
    with db.tx():
        core.put_setting(db, EVENING_DONE, day)
        out.ops(text)
    out.flush()
    return text
