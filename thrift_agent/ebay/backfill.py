"""WO57 §5 — the eBay backfill: every item the agent posted since a day (the owner: the last four weeks, from
2026-10-13) that is still live on at least one site and not sold also goes to eBay — 25 a day, oldest first, so a
6-feedback account doesn't list a hundred items in one burst. New shares always go at once (the queue takes them
before a backfilled item). The price-drop clocks aren't reset (`items`: pricedrop leaves a backfilled eBay listing out
of the clock).

`thrift ebay backfill` makes the plan (kv `ebay_backfill`); the running poster's housekeeping lets the day's items into
the queue one at a time (`tick`), each at its price on Poshmark that day — the base its eBay price is worked out from
(a price she changed there by hand, or one after a drop, counts); an item not on Poshmark any more takes the lowest
price its live sites show."""
from __future__ import annotations

import json
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
