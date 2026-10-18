"""WO45 §6/§7: every listing is checked after posting — its photo count and its price, from the site's own data.

Live 2026-10-18: the Ralph Lauren shirt went up on Vinted and Depop with one photo of four, and nothing read the
listings back. Now each live listing is read (Poshmark's public JSON; Depop's and Vinted's listing page through the
extension's verify job) — once soon after it posts, then with the order sweep's rhythm (every CHECK_EVERY) — and what
the site shows is kept on its row (`listings.photos_live`, `price_live`). A listing with fewer photos than its item
sent gets ONE "photos" edit (edits.queue_photos: the missing ones through the edit page, nothing else changed); a
listing whose price differs from the one we know is noted (the owner's hand change: recorded, never undone, WO41's
price drops read it the same way). A site that can't be read is left for the next look; an edit that fails three
times is one private line (edits._failed)."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thrift_agent import crosslist, edits, notify
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads

CHECK_EVERY = 6 * 3600            # a live listing read again this often (the order sweep's rhythm)
SOON_AFTER = 10 * 60              # a listing posted this long ago is read for the first time
RUN_EVERY = 5 * 60                # the housekeeping asks this often
PER_RUN = 6                       # listings read per run (a verify job each on Depop / Vinted: never a storm)
LAST_RUN = "photocheck_at"        # kv
ASKED = "photocheck_asked"        # kv: {item|site: the count asked for} — one "photos" edit per shortfall


def due(db: DB, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return now - float(db.kv_get(LAST_RUN) or 0) >= RUN_EVERY


def candidates(db: DB, now: datetime | None = None) -> list:
    """Live listings to read now: never read, or read more than CHECK_EVERY ago; posted at least SOON_AFTER ago."""
    now = now or datetime.now(timezone.utc)
    out = []
    for row in db.conn.execute("SELECT l.* FROM listings l JOIN items i ON i.id=l.item_id WHERE l.status='posted' AND "
                               "l.url IS NOT NULL AND COALESCE(i.test, 0)=0 ORDER BY l.posted_at DESC").fetchall():
        posted = _when(row["posted_at"])
        if posted is not None and now - posted < timedelta(seconds=SOON_AFTER):
            continue
        read = _when(row["photos_read_at"])
        if read is not None and now - read < timedelta(seconds=CHECK_EVERY):
            continue
        out.append(row)
    return out


def sent_photos(db: DB, row) -> list[str]:
    """The photos the listing was posted with, in order: Depop's / Vinted's mapped list, Poshmark's render."""
    fields = loads(row["fields_json"]) or {}
    photos = fields.get("photos")
    if isinstance(photos, list) and photos:
        return [str(p) for p in photos]
    it = db.item(row["item_id"])
    render = ((loads(it["renders"]) or {}).get("poshmark") or {}) if it else {}
    return [str(p) for p in render.get("photos") or []]


def record(db: DB, iid: str, mp: str, photos: int | None, price: float | None) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fields = {"photos_read_at": stamp}
    if photos is not None:
        fields["photos_live"] = int(photos)
    if price is not None:
        fields["price_live"] = float(price)
        fields["price_read_at"] = stamp
    db.upsert_listing(iid, mp, **fields)


def shortfall(s: Settings, db: DB, row, live_photos: int) -> list[str]:
    """What a listing lacks (WO45 §6): the photos sent after the first `live_photos` — Depop's and Vinted's grids keep
    the order they were given, so the first ones are the ones shown — cut to the site's room."""
    sent = sent_photos(db, row)
    have = max(0, int(live_photos))
    missing = [p for p in sent[have:] if Path(p).is_file()]
    if not missing and live_photos < len(sent):              # the files moved on: the item's own photos
        it = db.item(row["item_id"])
        d = Path(it["dir"]) if it else None
        if d is not None and (d / "photos").is_dir():
            own = sorted(str(p) for p in (d / "photos").glob("*.jpg"))
            missing = own[have:]
    return missing


def apply(s: Settings, db: DB, row, live: dict) -> str:
    """One listing's reading applied: the counts recorded; a shortfall → one "photos" edit (once per count); a price
    that differs → noted (never changed). Returns what happened (for the log)."""
    iid, mp = row["item_id"], row["marketplace"]
    photos, price = live.get("photos"), live.get("price")
    record(db, iid, mp, photos, price)
    sent = sent_photos(db, row)
    note = f"{mp}: {photos} photos on the site, {len(sent)} sent"
    if photos is not None and len(sent) and int(photos) < len(sent):
        asked = loads(db.kv_get(ASKED)) or {}
        key = f"{iid}|{mp}"
        if asked.get(key) == len(sent):
            return note + " (edit asked already)"
        missing = shortfall(s, db, row, int(photos))
        rows = edits.queue_photos(s, db, iid, mp, missing, int(photos)) if missing else []
        if rows:
            asked[key] = len(sent)
            db.kv_set(ASKED, json.dumps(asked))
            db.log(iid, "photos_missing", {"site": mp, "live": photos, "sent": len(sent), "adding": len(missing)})
            return note + f" → {len(missing)} added through the edit page"
        if not missing:
            notify.say(f"⚠️ {crosslist.item_name(db, iid)} shows {photos} of {len(sent)} photos on "
                       f"{crosslist.LABEL.get(mp, mp)} and the missing ones aren't on the Mac any more — please add them "
                       "by hand")
            asked[key] = len(sent)
            db.kv_set(ASKED, json.dumps(asked))
    if price is not None and row["price"] is not None and abs(float(price) - float(row["price"])) >= 1:
        db.log(iid, "price_differs", {"site": mp, "site_price": price, "ours": row["price"]})
        note += f"; price {price} on the site vs {row['price']} ours"
    return note


async def run(s: Settings, db: DB, ps: dict, limit: int = PER_RUN) -> list[str]:
    """The housekeeping's run: up to `limit` listings read (Poshmark's JSON in a thread; Depop's / Vinted's verify job
    on their posters when the extension is connected), each reading applied. Never raises."""
    db.kv_set(LAST_RUN, str(time.time()))
    done = []
    for row in candidates(db)[:limit]:
        iid, mp = row["item_id"], row["marketplace"]
        try:
            if mp == "poshmark":
                live = await asyncio.to_thread(crosslist.poshmark_listing, row["url"]) or {}
                if not live:
                    continue
                if live.get("for_sale") is False:
                    record(db, iid, mp, live.get("photos"), live.get("price"))
                    continue
            else:
                poster = ps.get(mp)
                if poster is None or not hasattr(poster, "read_listing") or not getattr(poster, "available",
                                                                                         lambda: True)():
                    continue
                if crosslist.blocked(db, mp):
                    continue
                live = await poster.read_listing(row["url"])
                if live.get("error"):
                    db.log(iid, "photocheck_failed", {"site": mp, "error": str(live["error"])[:200]})
                    continue
            done.append(apply(s, db, row, live))
        except Exception as e:  # noqa: BLE001 — a reading never ends the poster
            db.log(iid, "photocheck_failed", {"site": mp, "error": f"{type(e).__name__}: {e}"[:200]})
    if done:
        db.log(None, "photocheck", {"read": done})
    return done


def _when(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts)
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
