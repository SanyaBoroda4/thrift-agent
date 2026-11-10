"""WO45: the safety nets that don't depend on the Mac — a complete share with no outcome (§3), an item priced but not
live on a site (§7), and the day's one line at 21:00 (§9). All from the Mac's own records mirrored by /sync (the
share ledger, the items, the listings with their reasons), judged here on the API's clock.

Live (Oct 12–18): 4 of 23 shares never became a card and nothing said so; the Ralph Lauren shirt never reached
Poshmark (one failed attempt, the next window never came that day) and nobody heard. The Mac tells what it knows; the
API tells what the Mac didn't.

WO54 §3: the owner's quiet hours (8:30 AM – 2:00 PM; quiet.py, from the Mac's heartbeat) — the Mac runs nothing then,
on purpose. No net here judges it while it is quiet or starting back (quiet.settling), and every "this long" is the
Mac's working time (quiet.aged: the quiet hours don't count): an item priced at 8:00 AM is not 2 h old until 3:30 PM."""
from __future__ import annotations

from datetime import datetime, timedelta

from . import core, deadlines, ebay, quiet, util
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
# local hours in which her own wait is told (she is asleep at night). WO54 §3: the Mac posts at every hour outside the
# quiet hours now, so the unposted net chases at every hour outside them too (it rested 22:00–08:00 before)
HER_HOURS = (8, 22)
EVENING_HOUR = 21
MAC_OFFLINE = timedelta(minutes=30)       # WO53 §4: the 9 PM line says the Mac is offline (its figures as of then)
EVENING_DONE = "evening_line_day"         # settings: the local day the 📊 line went
# a listing row left as it is — its reason is the owner's to settle, or a state of its own (the Mac says those itself)
KEEP_REASONS = ("no size on the item", "shared again", "already live", "sold", "not published automatically",
                "unconfirmed publish", "mismatch", "retried by the safety net", "skipped: already",
                "refused the form",
                "waiting for the brand",
                "isn't the item's family")   # the Mac's own re-check settles a refused form (WO39; live 2026-10-20 the net
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


WAIT_ON_HER = timedelta(hours=2)          # WO51 §6: a share waiting this long on her answer → ONE "⏳ Waiting for …" line
WAITING_SAID = "share_waiting:"           # settings: + share id — that line said (once)


def shares_watch(db: Database, now: datetime | None = None, mode: str | None = None,
                 q: quiet.Quiet | None = None) -> list[str]:
    """§3, every 5 minutes, while the Mac is online (offline: WO42's line), for each share complete with no outcome —
    WO51 §6 (live 2026-11-01, 1:15 PM: "⏳ Share 12:39 PM (70 photos) stuck at carded since 12:44" while its cards were
    simply going out one at a time behind her open one): the Mac says what the share waits for (`state`, `waiting`,
    `moved_at`, from its card queue), and the watchdog fires only when nothing moves —
    - "work" (its photos grouped or its items processed), "send" (a card to send, none open for her) or "orphan" (an
      item with neither a card nor a place in the queue): no movement for STUCK_AFTER (STUCK_AFTER_BIG past BIG_SHARE
      photos) → ONE line, "⏳ Share 12:39 PM (70 photos) stuck since 12:48 PM: waiting for the next card to go out (8
      queued, none open for her).";
    - "her" (the queue waits for her answer): never "stuck"; after WAIT_ON_HER with no answer, in the daytime, ONE line
      saying so — "⏳ Waiting for her price on "Edikted Crochet Mini Dress" — 8 more cards behind it.";
    - "done": nothing (the outcome comes with the next sync).
    A share from a Mac that doesn't say (no `state`): the rule before WO51 ("stuck at grouped since 12:33" after
    STUCK_AFTER). Each line once per share. Returns the share ids said now.
    WO54 §3: nothing in the quiet hours or the 30 minutes after (the Mac runs nothing, then starts back), and every
    "this long" is the Mac's working time — her 2-hour wait too: the card she would answer isn't even sent then."""
    now, mode = (util.aware(now) if now else util.utcnow()), mode or util.mode()
    if mode == "off" or not _mac_online(db, now):
        return []
    q = q or quiet.read(db)
    if quiet.settling(now, q):
        return []
    out = core.Outbox(now, mode)
    said = []
    with db.tx():
        for row in db.query("SELECT * FROM shares WHERE complete_at IS NOT NULL AND outcome IS NULL "
                            "AND COALESCE(test, 0) = 0 ORDER BY complete_at"):
            done = parse_time(row["complete_at"])
            if done is None:
                continue
            photos = int(row["received"] or 0)
            limit = STUCK_AFTER_BIG if photos > BIG_SHARE else STUCK_AFTER
            state = (row.get("state") or "").strip().lower()
            if state:
                moved = max(parse_time(row.get("moved_at")) or done, done)
                if line := _share_line(db, row, state, moved, limit, now, q):
                    out.ops(line)
                    said.append(row["id"])
                continue
            if not quiet.aged(done, now, limit, q) or core.setting(db, STUCK_SAID + row["id"]):
                continue
            step, at = _step_of(row)
            since = parse_time(at)
            when = deadlines.local(since).strftime("%H:%M") if since else "?"
            out.ops(f"⏳ Share {_share_when(row['id'])} ({photos} photo{'s' if photos != 1 else ''}) stuck at {step} "
                    f"since {when}")
            core.put_setting(db, STUCK_SAID + row["id"], iso(now))
            said.append(row["id"])
    out.flush()
    return said


def _share_line(db: Database, row: dict, state: str, moved: datetime, limit: timedelta, now: datetime,
                q: quiet.Quiet) -> str | None:
    """WO51 §6: the one line for a share whose Mac says what it waits for, or None (moving, said already, done)."""
    waiting = (row.get("waiting") or "").strip().rstrip(".") or "its next step"
    if state == "done":
        return None
    if state == "her":
        if (not quiet.aged(moved, now, WAIT_ON_HER, q) or core.setting(db, WAITING_SAID + row["id"])
                or not HER_HOURS[0] <= deadlines.local(now).hour < HER_HOURS[1]):
            return None
        core.put_setting(db, WAITING_SAID + row["id"], iso(now))
        return f"⏳ Waiting for {waiting}."
    if not quiet.aged(moved, now, limit, q) or core.setting(db, STUCK_SAID + row["id"]):
        return None
    core.put_setting(db, STUCK_SAID + row["id"], iso(now))
    photos = int(row["received"] or 0)
    return (f"⏳ Share {_share_when(row['id'])} ({photos} photo{'s' if photos != 1 else ''}) stuck since "
            f"{_clock(deadlines.local(moved))}: waiting for {waiting}.")


UNFETCHED_AFTER = timedelta(minutes=10)    # WO49: an upload this long after its last photo, not fetched by the Mac
UNFETCHED_SAID = "upload_unfetched:"       # settings: + share id — the ⏳ line said (once)


def _clock(moment: datetime) -> str:
    """"9:40 AM" (portable: no %-I)."""
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def uploads_watch(db: Database, now: datetime | None = None, mode: str | None = None,
                  q: quiet.Quiet | None = None) -> list[str]:
    """WO49, every 5 minutes: a share the iPhone uploaded (ended, or quiet) that the Mac hasn't fetched
    UNFETCHED_AFTER after its last photo, while the Mac's heartbeat says it is online → ONE private line per share,
    "⏳ Share 9:39 AM (20 photos) is in Azure but the Mac hasn't taken it (since 9:40)". The Mac offline: WO42's line
    covers it. Test shares never. Returns the share ids said now.
    WO54 §3: her shares are still taken in the quiet hours (they wait in Azure, as when the Mac is off); the Mac takes
    them when it starts back — nothing is said in the quiet hours or the 30 minutes after, and the 10 minutes are the
    Mac's working time."""
    now, mode = (util.aware(now) if now else util.utcnow()), mode or util.mode()
    if mode == "off" or not _mac_online(db, now):
        return []
    q = q or quiet.read(db)
    if quiet.settling(now, q):
        return []
    out = core.Outbox(now, mode)
    said = []
    with db.tx():
        for row in db.query("SELECT share_id, received, started_at, ended_at, last_received_at FROM shares_in "
                            "WHERE fetched_at IS NULL AND COALESCE(test, 0) = 0 AND received > 0 ORDER BY started_at"):
            last = parse_time(row["last_received_at"] or row["ended_at"] or row["started_at"])
            if (last is None or not quiet.aged(last, now, UNFETCHED_AFTER, q)
                    or core.setting(db, UNFETCHED_SAID + row["share_id"])):
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
    return [{k: row.get(k) for k in ("id", "source", "expected", "received", "received_at", "complete_at", "grouped_at",
                                     "carded_at", "outcome", "reason", "test",
                                     "state", "waiting")} for row in rows]     # WO51 §6: what an open one waits for


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


NOT_LIVE_AFTER = timedelta(hours=2)       # WO52 §5: priced this long ago and not live on all three sites
NOT_LIVE_WINDOW = timedelta(days=3)       # … priced within this (history is never replayed)
NOT_LIVE_SAID = "not_live_said:"          # settings: + item — the 🚨 line said (once)
SITES = util.CORE_SITES                   # the three; eBay with them while it is on (WO57, ebay.on)


def not_live(db: Database, now: datetime | None = None, mode: str | None = None,
             q: quiet.Quiet | None = None) -> list[str]:
    """WO52 §5 (the owner, 2026-11-01): the net that needs no reason — any priced item (never a test item) priced 2
    hours ago and not live on all three sites gets ONE private 🚨 line, WHATEVER the recorded reason (the 1-hour net
    above skips the reasons the Mac settles itself; this one names them): "🚨 Zara Moto Jacket isn't live everywhere 2 h
    after its price — Depop: refused the form …; Vinted: not tried yet". Returns the items said.
    WO54 §3: never in the quiet hours or the 30 minutes after them, and the 2 hours are the Mac's working time: an
    item priced at 8:00 AM is 2 h old at 3:30 PM (8:00–8:30, then 2:00–3:30).
    WO57: while eBay is on (the Mac's heartbeat) "everywhere" is all four — eBay named like the others; an item priced
    before eBay came on owes it nothing unless it has an eBay row (the day eBay starts is not a day of alarms)."""
    now, mode = (util.aware(now) if now else util.utcnow()), mode or util.mode()
    if mode != "live":
        return []
    q = q or quiet.read(db)
    if quiet.settling(now, q):
        return []
    out = core.Outbox(now, mode)
    said = []
    paused = _paused_sites(db)
    ebay_since = ebay.on_since(db, now)
    with db.tx():
        rows = db.query("SELECT id, title, priced_at FROM items WHERE COALESCE(priced, 0) = 1 AND COALESCE(test, 0) = 0 "
                        "AND COALESCE(status, '') NOT IN ('dropped', 'sold') AND priced_at IS NOT NULL AND priced_at <= ? "
                        "AND priced_at >= ?", (iso(now - NOT_LIVE_AFTER), iso(now - NOT_LIVE_WINDOW)))
        for it in rows:
            priced = parse_time(it["priced_at"])
            if priced is None or not quiet.aged(priced, now, NOT_LIVE_AFTER, q):
                continue                                      # less than 2 h of the Mac's working time (WO54 §3)
            if core.setting(db, NOT_LIVE_SAID + it["id"]):
                continue
            listings = {r["marketplace"]: r for r in db.query(
                "SELECT marketplace, status, error FROM listings WHERE item_id = ?", (it["id"],))}
            sites = SITES
            if ebay_since is not None and (util.EBAY in listings or priced >= ebay_since):
                sites = util.SITES                            # WO57: eBay is on, and this item is its to list
            if any((listings.get(mp) or {}).get("status") in ("sold", "delisted") for mp in sites):
                continue                                      # sold or taken down somewhere: not this net's
            missing = [mp for mp in sites if (listings.get(mp) or {}).get("status") != "posted"]
            if not missing:
                continue
            parts = []
            for mp in missing:
                r = listings.get(mp) or {}
                why = (r.get("error") or "").strip()
                p = paused.get(mp) if isinstance(paused.get(mp), dict) else None
                if not why and p and (r.get("status") or "queued") == "queued":
                    # WO53 §7 (live 2026-11-09 01:15: "Depop: waiting in line" while Depop was paused): the site's
                    # own state, from the Mac's heartbeat
                    from .timers import clock
                    why = f"waiting — {site_name(mp)} {pause_words(p)} since {clock(parse_time(p.get('since')), now)}"
                elif not why:
                    why = {"queued": "waiting in line", "posting": "being posted now"}.get(
                        r.get("status") or "", r.get("status") or "not tried yet")
                parts.append(f"{site_name(mp)}: {why[:160]}")
            out.ops(f"🚨 {it['title'] or 'An item'} isn't live everywhere 2 h after its price — " + "; ".join(parts))
            core.put_setting(db, NOT_LIVE_SAID + it["id"], iso(now))
            said.append(it["id"])
    out.flush()
    return said


def unposted(db: Database, now: datetime | None = None, mode: str | None = None,
             q: quiet.Quiet | None = None) -> dict:
    """§7, every 15 minutes while the Mac posts: an item priced more than an hour ago that isn't live on a site, with
    no reason the Mac settles itself (a missing size, a twin, sold, autopublish off, an unconfirmed publish, a
    mismatch) → first ONE "repost" task the Mac takes with its take-downs (the row goes back in line once); still not
    live an hour after the Mac answered → ONE private line per item and site: "⚠️ <title> isn't on Poshmark: <reason>".
    {"asked": [...], "said": [...]}.
    WO54 §3: the Mac posts at every hour but the quiet hours, so the net rests only in them (and the 30 minutes after:
    the Mac starting back), no longer 22:00–08:00; its hours are the Mac's working time."""
    now, mode = (util.aware(now) if now else util.utcnow()), mode or util.mode()
    result: dict = {"asked": [], "said": []}
    if mode != "live":
        return result
    q = q or quiet.read(db)
    if quiet.settling(now, q):
        return result
    out = core.Outbox(now, mode)
    with db.tx():
        for row in _unposted_rows(db, now):
            key = f"{row['item_id']}|{row['marketplace']}"
            failed = parse_time(row["updated_at"])
            if failed is None or not quiet.aged(failed, now, UNPOSTED_AFTER, q):
                continue                                      # less than an hour of the Mac's working time (WO54 §3)
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
            if answered is None or not quiet.aged(answered, now, UNPOSTED_AFTER, q):
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


SITE_LABEL = {"poshmark": "Poshmark", "depop": "Depop", "vinted": "Vinted", "ebay": "eBay"}


ACCOUNT_WORDS = {"login": "logged out", "captcha": "shows a CAPTCHA", "verify": "asks for a check",
                 "block": "turned the Mac away"}


def _beat_info(db: Database) -> dict:
    import json
    beat = db.one("SELECT info FROM heartbeats WHERE source = 'mac'")
    try:
        info = json.loads(beat["info"]) if beat and beat["info"] else {}
    except (TypeError, ValueError):
        info = {}
    return info if isinstance(info, dict) else {}


def _paused_sites(db: Database) -> dict:
    """The Mac's paused sites as its last heartbeat named them (health.beat): {site: {kind, page, owner, since,
    reason}}."""
    p = _beat_info(db).get("sites")
    return p if isinstance(p, dict) else {}


def pause_words(p: dict) -> str:
    """A paused site's state in the owner's words: "logged out", "paused by you", "paused: its sell form changed …"."""
    if p.get("kind") == "account":
        return ACCOUNT_WORDS.get(p.get("page") or "", "stopped by a page it showed")
    if p.get("owner"):
        return "paused by you"
    return f"paused: {p.get('reason') or 'a problem'}"


def _slot(slot) -> str:
    try:
        hh, mm = str(slot).split(":")
        return f"{int(hh) % 12 or 12}:{mm}"
    except ValueError:
        return str(slot)


def sites_line(db: Database, day: str, now: datetime | None = None) -> str:
    """WO52 §3 → WO53 §3: each site's state, true and whole, from the Mac's heartbeat — "Sites: Poshmark ✓ · Depop ✗
    logged out since Sun Nov 8, 4:04 PM EST · Vinted ✓ — form checks today: Poshmark 8:30 ✓ 2:30 ✓, Depop 2:30 ✓."
    A site is ✗ for what it is (its pause: a login page, failures, the owner's pause; today's failed check, its step and
    time) — never for a stale check of another day (live 2026-11-08: Nov 7's checks shown as "one failed"); a 🚨 is
    named only when one was said in the last day ("see the 🚨 line" with none sent). Nothing for a Mac that sends
    neither checks nor pauses.
    WO57, while eBay is on: "· eBay ✓" after the three (ebay.site_mark: ✓ when connected with no take-down of its own
    left open, else what is wrong — "eBay ✗ its permission ended") and, when the Mac counts them, " eBay: 12 of 250
    free listings used this month."."""
    import json
    beat = db.one("SELECT info FROM heartbeats WHERE source = 'mac'")
    try:
        info = json.loads(beat["info"]) if beat and beat["info"] else {}
    except (TypeError, ValueError):
        info = {}
    info = info if isinstance(info, dict) else {}
    checks = info.get("formcheck") if isinstance(info.get("formcheck"), dict) else {}
    paused = info.get("sites") if isinstance(info.get("sites"), dict) else {}
    alarms = info.get("alarms") if isinstance(info.get("alarms"), dict) else {}
    with_ebay = ebay.on(db)
    if not checks and not paused and not with_ebay:
        return ""
    from .timers import clock
    marks, done = [], []
    for site in util.CORE_SITES:
        label = SITE_LABEL.get(site, site)
        c = checks.get(site) if isinstance(checks.get(site), dict) else None
        p = paused.get(site) if isinstance(paused.get(site), dict) else None
        if c is None and p is None:
            continue                                  # nothing known about it: never a ✓ it didn't earn
        c = c or {}
        today = c.get("today") if isinstance(c.get("today"), dict) and c["today"].get("day") == day else {}
        passed, failed = list(today.get("passed") or []), list(today.get("failed") or [])
        at = parse_time(c.get("at"))
        if not today and at is not None and deadlines.local(at).date().isoformat() == day and c.get("slot"):
            (passed if c.get("ok") and not c.get("degraded") else failed).append(c["slot"])   # only its last check
        alarmed = any(str(k).split(":", 1)[0] == site for k in alarms)
        if p:
            since = clock(parse_time(p.get("since")), now)
            marks.append(f"{label} ✗ {pause_words(p)} since {since}" + (" (see the 🚨 line)" if alarmed else ""))
        elif failed:
            err = str(c.get("error") or "").split("\n", 1)[0][:90]
            marks.append(f"{label} ✗ form check {', '.join(_slot(x) for x in failed)} failed"
                         + (f" ({err})" if err else "") + (" (see the 🚨 line)" if alarmed else ""))
        elif (not c.get("ok") or c.get("degraded")) and not passed and at is not None:
            marks.append(f"{label} ✗ its last form check failed ({clock(at, now)})"
                         + (" (see the 🚨 line)" if alarmed else ""))
        else:
            marks.append(f"{label} ✓")
        if passed or failed:
            done.append(f"{label} " + " ".join([*(f"{_slot(x)} ✓" for x in passed), *(f"{_slot(x)} ✗" for x in failed)]))
    when = (" — form checks today: " + ", ".join(done)) if done else " — no form check ran today"
    if with_ebay:
        # WO57: eBay has no form to check — its mark is thrift-api's own knowledge (the permission, its take-downs, its
        # orders), or the Mac's pause of it; then the month's free listings as the Mac counts them
        now = util.aware(now) if now else util.utcnow()
        p = paused.get(util.EBAY) if isinstance(paused.get(util.EBAY), dict) else None
        if p:
            marks.append(f"eBay ✗ {pause_words(p)} since {clock(parse_time(p.get('since')), now)}"
                         + (" (see the 🚨 line)" if any(str(k).split(":", 1)[0] == util.EBAY for k in alarms) else ""))
        else:
            marks.append(ebay.site_mark(db, now))
        if not checks and not paused:
            when = ""                                 # a Mac that says nothing of the three: eBay's mark alone
        return "\nSites: " + " · ".join(marks) + when + "." + ebay.free_line(db, now)
    return "\nSites: " + " · ".join(marks) + when + "."


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
    shares = {r["id"] for r in shares_today(db, now) if not r.get("test")}
    # WO53 §4: Azure's own ledger too — her photos reach it while the Mac is off
    shares |= {r["share_id"] for r in db.query("SELECT share_id FROM shares_in WHERE share_id LIKE ? AND "
                                               "COALESCE(test, 0) = 0", (f"{day}%",))}
    items = db.query("SELECT id FROM items WHERE created_at LIKE ? AND COALESCE(test, 0) = 0", (f"{day}%",))
    item_ids = [r["id"] for r in items]
    live_all = 0
    sites = ebay.sites(db)                            # WO57: the three, and eBay while it is on
    for iid in item_ids:
        rows = db.query("SELECT marketplace, status, photos_sent, photos_live FROM listings WHERE item_id = ?", (iid,))
        posted = {r["marketplace"]: r for r in rows if r["status"] == "posted"}
        if set(posted) >= set(sites) and all(
                r["photos_live"] is None or r["photos_sent"] is None or int(r["photos_live"]) >= int(r["photos_sent"])
                for r in posted.values()):
            live_all += 1
    waiting = db.one("SELECT COUNT(DISTINCT l.item_id) AS n FROM listings l JOIN items i ON i.id = l.item_id WHERE "
                     "l.status = 'skipped' AND LOWER(COALESCE(l.error, '')) LIKE '%no size on the item%' AND "
                     "COALESCE(i.status, '') <> 'posted' AND COALESCE(i.test, 0) = 0")
    n_wait = int(waiting["n"]) if waiting else 0
    beat = db.one("SELECT last_seen FROM heartbeats WHERE source = 'mac'")
    seen = parse_time(beat["last_seen"]) if beat else None
    from .timers import clock, mac_silent
    q = quiet.read(db)
    head = "📊 Today"
    if seen is not None and quiet.settling(now, q) and now - seen > MAC_OFFLINE:
        # WO54 §3: quiet on purpose (an owner's window over 9 PM), or starting back after it — never "offline"
        state = "in its quiet hours" if quiet.is_quiet(now, q) else "starting back after its quiet hours"
        head = f"📊 Today (the Mac is {state} — its figures as of {clock(seen, now)})"
    elif seen is not None and mac_silent(seen, now, q, MAC_OFFLINE):
        head = f"📊 Today (the Mac is offline since {clock(seen, now)} — its figures as of then)"
    text = (f"{head}: {len(shares)} share{'s' if len(shares) != 1 else ''} → {len(item_ids)} item"
            f"{'s' if len(item_ids) != 1 else ''}, {live_all} live on all {len(sites)} sites with all photos"
            + (f", {n_wait} waiting for a size" if n_wait else "") + "." + _cleanup_today(db, day)
            + sites_line(db, day, now))
    out = core.Outbox(now, mode)
    with db.tx():
        core.put_setting(db, EVENING_DONE, day)
        out.ops(text)
    out.flush()
    return text
