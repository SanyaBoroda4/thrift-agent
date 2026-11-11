"""WO57 §5 — the eBay backfill: every item the agent posted since a day (the owner: the last four weeks, from
2026-10-13) that is still live on at least one site and not sold also goes to eBay — 25 a day, oldest first, so a
6-feedback account doesn't list a hundred items in one burst. New shares always go at once (the queue takes them
before a backfilled item). The price-drop clocks aren't reset (`items`: pricedrop leaves a backfilled eBay listing out
of the clock).

`thrift ebay backfill` makes the plan (kv `ebay_backfill`); the running poster's housekeeping lets the day's items into
the queue one at a time (`tick`), each at its price on Poshmark that day — the base its eBay price is worked out from
(a price she changed there by hand, or one after a drop, counts); an item not on Poshmark any more takes the lowest
price its live sites show.

WO61 (the owner, 2026-11-11: "whenever agent will post previous listings i want it to send links for each one"): the
old ✅ card is still edited with its 🔵 eBay line, but that card is days up the chat — so each backfilled item whose
eBay listing is live (posted: read back) gets ONE new group message, silent, "🔵 Now on eBay: <title> — $161" and its
address (`send_links`, the housekeeping's turn; never twice: the event `backfill_link_sent` taken in one statement
before the send; the items already live when this began are recorded without a message, `seed_links`); a failed one
gets none (its private line as before). A new share gets none: its own card is fresh. Once the day's batch is done, or
at 9 PM, ONE line: "🔵 eBay today: 25 more items up · 64 to go · about 3 more days." (`day_line`)."""
from __future__ import annotations

import html
import json
import math
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from thrift_agent.config import Settings
from thrift_agent.db import DB, loads

KEY = "ebay_backfill"          # kv: {"since", "planned_at", "items": {item: {"n", "queued_at", "base", "left"}}}
PER_DAY = 25
SINCE = "2026-10-13"
SITE = "ebay"


def _load(db: DB) -> dict:
    data = loads(db.kv_get(KEY)) or {}
    data.setdefault("items", {})
    return data


def _save(db: DB, data: dict) -> None:
    db.kv_set(KEY, json.dumps(data))


def items(db: DB) -> set[str]:
    """The items the backfill put on eBay (or is about to): their eBay listing never moves the price-drop clock."""
    return set(_load(db)["items"])


def base(db: DB, iid: str) -> int | None:
    """The price a backfilled item's eBay price is worked out from (its price on its live sites the day it was
    queued), or None for an item the backfill didn't queue."""
    entry = _load(db)["items"].get(iid) or {}
    return int(entry["base"]) if entry.get("base") else None


def per_day(s: Settings | None) -> int:
    try:
        return max(0, int((s.get("marketplaces.ebay.backfill.per_day") if s is not None else None) or PER_DAY))
    except (TypeError, ValueError):
        return PER_DAY


def since_of(s: Settings | None) -> str:
    return str((s.get("marketplaces.ebay.backfill.since") if s is not None else None) or SINCE)[:10]


def eligible(db: DB, iid: str) -> str | None:
    """None when the item may go to eBay now, else why not: it must be live on at least one site, not sold anywhere,
    never a test item or the canary, with no eBay row yet."""
    it = db.item(iid)
    if it is None:
        return "no such item"
    if ("test" in it.keys() and it["test"]) or it["status"] in ("canary", "dropped"):
        return f"a {it['status']} item" if it["status"] in ("canary", "dropped") else "a test item"
    rows = db.listings_for(iid)
    if any(r["marketplace"] == SITE for r in rows):
        return "it has an eBay row already"
    if any(r["status"] == "sold" for r in rows) or \
            db.conn.execute("SELECT 1 FROM takedowns WHERE item_id=? LIMIT 1", (iid,)).fetchone():
        return "sold"
    if not any(r["status"] == "posted" and r["url"] for r in rows):
        return "not live on any site"
    return None


def candidates(db: DB, since: str = SINCE) -> list[str]:
    """The items first posted on or after `since` that may go to eBay, the oldest first."""
    rows = db.conn.execute(
        "SELECT item_id, MIN(posted_at) AS first FROM listings WHERE status IN ('posted', 'delisted', 'sold') AND "
        "posted_at IS NOT NULL GROUP BY item_id HAVING first >= ? ORDER BY first, item_id", (since,)).fetchall()
    return [r["item_id"] for r in rows if eligible(db, r["item_id"]) is None]


def plan(s: Settings | None, db: DB, since: str | None = None, dry: bool = False, only: list[str] | None = None,
         not_before: datetime | None = None) -> list[str]:
    """`thrift ebay backfill`: the plan — every candidate not yet in it, appended in order (an item already planned
    keeps its place and its state). Returns the whole list still to queue, in order. `dry`: nothing is recorded.
    `only`: just these items, in this order (the owner's first five, 2026-11-11) — one that may not go to eBay raises
    ValueError naming why, and nothing is planned. `not_before`: the poster lets nothing in before it (a start
    tomorrow after the quiet hours); kept until a later plan names another."""
    since = (since or since_of(s))[:10]
    data = _load(db)
    planned = data["items"]
    n = max((e.get("n", 0) for e in planned.values()), default=0)
    if only is not None:
        refused = {iid: why for iid in only if (why := eligible(db, iid)) is not None and iid not in planned}
        if refused:
            raise ValueError("; ".join(f"{iid}: {why}" for iid, why in refused.items()))
        fresh = [iid for iid in dict.fromkeys(only) if iid not in planned]
    else:
        fresh = [iid for iid in candidates(db, since) if iid not in planned]
    if not_before is not None and not dry:
        data["not_before"] = not_before.astimezone(timezone.utc).isoformat(timespec="seconds")
    waiting = [iid for iid, e in sorted(planned.items(), key=lambda kv: kv[1].get("n", 0))
               if not e.get("queued_at") and not e.get("left")]
    if dry:
        return [*waiting, *fresh]
    for iid in fresh:
        n += 1
        planned[iid] = {"n": n}
    data.update(since=since, planned_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    _save(db, data)
    if fresh:
        db.log(None, "ebay_backfill_planned", {"since": since, "added": len(fresh), "waiting": len(waiting) + len(fresh)})
    return [*waiting, *fresh]


def _today(s: Settings | None, now: datetime) -> date:
    tz = (s.get("schedule.timezone") if s is not None else None) or "America/New_York"
    return now.astimezone(ZoneInfo(str(tz))).date()


def current_price(db: DB, iid: str, read_poshmark=None) -> int | None:
    """The item's price today — the base of its eBay price: Poshmark's own (its public page, else the price last read
    there), else the lowest its other live sites show, else her approved price."""
    rows = {r["marketplace"]: r for r in db.listings_for(iid) if r["status"] == "posted" and r["url"]}
    posh = rows.get("poshmark")
    if posh is not None:
        if read_poshmark is not None:
            try:
                _, price = read_poshmark(posh["url"])
            except Exception:  # noqa: BLE001 — unreadable now: the price last read
                price = None
            if price:
                return int(price)
        if posh["price_live"] or posh["price"]:
            return int(round(float(posh["price_live"] or posh["price"])))
    seen = [float(r["price_live"] or r["price"]) for r in rows.values() if r["price_live"] or r["price"]]
    if seen:
        return int(round(min(seen)))
    it = db.item(iid)
    return int(it["owner_price"]) if it is not None and it["owner_price"] else None


def tick(s: Settings, db: DB, now: datetime | None = None, read_poshmark=None) -> str | None:
    """The poster's housekeeping turn: ONE more planned item into eBay's queue while today's `per_day` aren't reached
    — the oldest first, still eligible (sold or gone meanwhile: left out, noted), at its price today. Returns the item
    queued, else None."""
    from thrift_agent import crosslist
    data = _load(db)
    planned = data["items"]
    if not planned:
        return None
    now = now or datetime.now(timezone.utc)
    start = data.get("not_before")
    if start:
        try:
            if now < datetime.fromisoformat(str(start)):
                return None                              # not yet: the plan starts later (`--not-before`)
        except ValueError:
            pass
    today = _today(s, now).isoformat()
    done_today = sum(1 for e in planned.values() if str(e.get("queued_day") or "") == today)
    if done_today >= per_day(s):
        return None
    for iid, entry in sorted(planned.items(), key=lambda kv: kv[1].get("n", 0)):
        if entry.get("queued_at") or entry.get("left"):
            continue
        why = eligible(db, iid)
        if why is not None:
            entry["left"] = why
            _save(db, data)
            db.log(iid, "ebay_backfill_left", {"why": why})
            continue
        price = current_price(db, iid, read_poshmark)
        if not price:
            entry["left"] = "no price"
            _save(db, data)
            continue
        entry.update(queued_at=now.isoformat(timespec="seconds"), queued_day=today, base=int(price))
        _save(db, data)
        crosslist.queue(s, db, iid, [SITE], why="ebay backfill")
        db.log(iid, "ebay_backfill_queued", {"base": int(price), "n": entry.get("n"), "today": done_today + 1})
        return iid
    return None


def status(db: DB) -> dict:
    """For `thrift ebay backfill --status` and doctor: planned / queued / live / left / waiting."""
    planned = _load(db)["items"]
    live = 0
    for iid, e in planned.items():
        row = db.listing(iid, SITE) if e.get("queued_at") else None
        live += 1 if row is not None and row["status"] == "posted" else 0
    return {"planned": len(planned), "queued": sum(1 for e in planned.values() if e.get("queued_at")), "live": live,
            "left": sum(1 for e in planned.values() if e.get("left")),
            "waiting": sum(1 for e in planned.values() if not e.get("queued_at") and not e.get("left")),
            "since": _load(db).get("since"), "not_before": _load(db).get("not_before")}


# --- WO61: a link for every backfilled item, and the day's line -----------------------------------------------------

LINK_EVENT = "backfill_link_sent"          # events: the item's link message, taken before it goes (never twice)
LINK_FAILED = "backfill_link_failed"
LINKS_SEEDED = "ebay_backfill_links_seeded"   # kv: the items live before WO61 recorded without a message, once
DAY_SAID = "ebay_backfill_day_said"        # kv: the local day the day's line went
DAY_LINE_HOUR = 21                         # 9 PM: the line goes then if the day's batch isn't done by itself
LINKS_PER_TURN = 5
LINK_TRIES = 3                             # a message Telegram refused this often is left (one private line)
SETTLED = ("posted", "failed", "skipped", "delisted", "sold")


def link_text(title: str, price: float | None, url: str) -> str:
    """"🔵 Now on eBay: Zara TRF Collection 100% Genuine Leather Moto Jacket Black size S — $161" and the address on
    its own line (HTML: the title escaped)."""
    money = f" — ${float(price):,.0f}" if price else ""
    return f"🔵 Now on eBay: {html.escape(' '.join(str(title or 'an item').split()), quote=False)}{money}\n{url}"


def _linked(db: DB) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT DISTINCT ref FROM events WHERE kind=?", (LINK_EVENT,))}


def links_due(db: DB) -> list[tuple[str, object]]:
    """(item, its eBay row) for each backfilled item live on eBay whose link message hasn't gone — in plan order."""
    done = _linked(db)
    out = []
    for iid, entry in sorted(_load(db)["items"].items(), key=lambda kv: kv[1].get("n", 0)):
        if not entry.get("queued_at") or iid in done:
            continue
        row = db.listing(iid, SITE)
        if row is not None and row["status"] == "posted" and row["url"]:
            out.append((iid, row))
    return out


def seed_links(db: DB) -> int:
    """Once: the backfilled items already live on eBay when the links began (the owner's first five, 2026-11-11 — she
    has their links) are recorded as linked, without a message."""
    if db.kv_get(LINKS_SEEDED):
        return 0
    due = links_due(db)
    for iid, row in due:
        db.log(iid, LINK_EVENT, {"url": row["url"], "seeded": True})
    db.kv_set(LINKS_SEEDED, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return len(due)


def _title(db: DB, iid: str, row) -> str:
    fields = loads(row["fields_json"]) if row["fields_json"] else {}
    if fields.get("title"):
        return str(fields["title"])
    it = db.item(iid)
    renders = (loads(it["renders"]) or {}) if it is not None else {}
    return str((renders.get("poshmark") or {}).get("title") or iid)


def _to_group(s: Settings, text: str, silent: bool) -> None:
    """The group, HTML; printed on the dev machine (no bot). Raises when it never left (the caller takes it back)."""
    from thrift_agent import approve, notify
    bot = approve.bot_for(s) if s is not None else None
    if bot is None:
        notify.group(text)
        return
    bot.send_message(text, parse_mode="HTML", silent=silent)


def send_links(s: Settings, db: DB, limit: int = LINKS_PER_TURN) -> list[str]:
    """WO61: the link messages due (at most `limit` a turn), each taken in ONE statement before it goes; a send that
    never left is taken back for the next turn; one that may have arrived counts as sent. Returns the items sent."""
    from thrift_agent import notify, telegram
    seed_links(db)
    sent = []
    for iid, row in links_due(db)[:limit]:
        tries = db.conn.execute("SELECT COUNT(*) FROM events WHERE ref=? AND kind=?", (iid, LINK_FAILED)).fetchone()[0]
        if tries >= LINK_TRIES:
            continue
        text = link_text(_title(db, iid, row), row["price"], row["url"])
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = db.conn.execute(
            "INSERT INTO events (ts, ref, kind, detail) SELECT ?, ?, ?, ? WHERE NOT EXISTS "
            "(SELECT 1 FROM events WHERE ref=? AND kind=?)",
            (stamp, iid, LINK_EVENT, json.dumps({"url": row["url"], "price": row["price"]}), iid, LINK_EVENT))
        if cur.rowcount != 1:
            continue                                   # another process sent it a moment ago
        claim = cur.lastrowid
        try:
            _to_group(s, text, silent=True)
        except Exception as e:  # noqa: BLE001 — Telegram away or refusing: never sent twice, never a crash
            if telegram.maybe_delivered(e):
                db.log(iid, "telegram_maybe_sent", {"kind": LINK_EVENT, "error": str(e)[:200]})
                sent.append(iid)
                continue
            db.conn.execute("DELETE FROM events WHERE rowid=?", (claim,))
            db.log(iid, LINK_FAILED, {"error": str(e)[:200]})
            if tries + 1 >= LINK_TRIES:
                notify.ops(f"🔵 The eBay link message for {iid} was refused {LINK_TRIES} times — left: {row['url']}")
            break                                      # Telegram away: the rest wait for the next turn
        sent.append(iid)
    return sent


def day_line(s: Settings, db: DB, now: datetime | None = None) -> str | None:
    """WO61: ONE group line a day once the day's batch is done — the day's `per_day` queued (or nothing left to queue)
    and each of them settled on eBay — or at 9 PM, whichever is first: "🔵 eBay today: 25 more items up · 64 to go ·
    about 3 more days." Only on a day something went up. Returns the line sent."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(str((s.get("schedule.timezone") if s is not None else None)
                                        or "America/New_York")))
    today = local.date().isoformat()
    if db.kv_get(DAY_SAID) == today:
        return None
    planned = _load(db)["items"]
    todays = [iid for iid, e in planned.items() if str(e.get("queued_day") or "") == today]
    rows = {iid: db.listing(iid, SITE) for iid in todays}
    up = [iid for iid, r in rows.items() if r is not None and r["status"] == "posted"]
    if not up:
        return None
    waiting = sum(1 for e in planned.values() if not e.get("queued_at") and not e.get("left"))
    done = (len(todays) >= per_day(s) or waiting == 0) and all(r is not None and r["status"] in SETTLED
                                                               for r in rows.values())
    if not done and local.hour < DAY_LINE_HOUR:
        return None
    n = len(up)
    if waiting:
        days = math.ceil(waiting / max(1, per_day(s)))
        text = (f"🔵 eBay today: {n} more item{'s' if n != 1 else ''} up · {waiting} to go · about {days} more "
                f"day{'s' if days != 1 else ''}.")
    else:
        text = f"🔵 eBay today: {n} more item{'s' if n != 1 else ''} up — every planned item is on eBay now ✓"
    db.kv_set(DAY_SAID, today)                         # taken before it goes: once a day
    try:
        _to_group(s, text, silent=False)
    except Exception as e:  # noqa: BLE001
        from thrift_agent import telegram
        if not telegram.maybe_delivered(e):
            db.kv_set(DAY_SAID, "")                    # never left: the next turn tries again
            return None
    db.log(None, "ebay_backfill_day_line", {"day": today, "up": n, "waiting": waiting})
    return text
