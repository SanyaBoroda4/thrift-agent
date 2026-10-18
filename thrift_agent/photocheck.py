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
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thrift_agent import crosslist, edits, notify
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads

CHECK_EVERY = 6 * 3600            # a live listing read again this often (the order sweep's rhythm)
POSHMARK_EVERY = 24 * 3600        # Poshmark's public JSON (an HTTP GET a listing): once a day is plenty
SOON_AFTER = 10 * 60              # a listing posted this long ago is read for the first time
RUN_EVERY = 5 * 60                # the housekeeping asks this often
PER_RUN = 6                       # single reads per run (an edit-page job each on Depop / Vinted: never a storm)
SINGLE_EVERY = 24 * 3600          # a listing's edit page opened at most this often (the bulk data is the rule)
BULK_EVERY = 6 * 3600             # Depop's / Vinted's shop read in bulk this often (one JSON read, not a page each)
LAST_RUN = "photocheck_at"        # kv
ASKED = "photocheck_asked"        # kv: {item|site: the count asked for} — one "photos" edit per shortfall
BULK_AT = "photocheck_bulk"       # kv: {site: when the shop was last read in bulk}
SINGLE_AT = "photocheck_single"   # kv: {item|site: when its page was last opened}
SHAPE_SAID = "photocheck_shape"   # kv: {site: the JSON's keys, logged once}


def due(db: DB, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return now - float(db.kv_get(LAST_RUN) or 0) >= RUN_EVERY


def candidates(db: DB, now: datetime | None = None) -> list:
    """Live listings to read now: never read, or read more than CHECK_EVERY ago (Poshmark: POSHMARK_EVERY); posted
    at least SOON_AFTER ago."""
    now = now or datetime.now(timezone.utc)
    out = []
    for row in db.conn.execute("SELECT l.* FROM listings l JOIN items i ON i.id=l.item_id WHERE l.status='posted' AND "
                               "l.url IS NOT NULL AND COALESCE(i.test, 0)=0 ORDER BY l.posted_at DESC").fetchall():
        posted = _when(row["posted_at"])
        if posted is not None and now - posted < timedelta(seconds=SOON_AFTER):
            continue
        read = _when(row["photos_read_at"])
        every = POSHMARK_EVERY if row["marketplace"] == "poshmark" else CHECK_EVERY
        if read is not None and now - read < timedelta(seconds=every):
            continue
        out.append(row)
    return out


def _key_of(mp: str, row) -> str | None:
    """How a listing is found in the site's bulk data: Vinted's id, Depop's slug."""
    from thrift_agent.db import listing_id_from
    return listing_id_from(mp, row["url"]) or row["listing_id"]


def bulk_lookup(mp: str, bulk: dict) -> dict[str, dict]:
    """The bulk read's records by Vinted id / Depop slug (and by id too)."""
    out: dict[str, dict] = {}
    for rec in bulk.get("listings") or []:
        if not isinstance(rec, dict):
            continue
        for key in (rec.get("id"), rec.get("slug")):
            if key:
                out[str(key)] = rec
        if mp == "depop" and rec.get("url"):
            from thrift_agent.db import listing_id_from
            slug = listing_id_from("depop", rec["url"])
            if slug:
                out[slug] = rec
    return out


async def bulk_reads(s: Settings, db: DB, ps: dict, sites: list[str], now: datetime) -> dict[str, dict[str, dict]]:
    """WO45 follow-up (the owner: a listing page per item every 6 hours looks like a bot): Depop's and Vinted's shops
    read in ONE JSON read each, every BULK_EVERY — {site: {key: record}}; a site that can't be read now gives {}. The
    JSON's keys are logged once per site (`photocheck_shape`), so the live names are known."""
    stamps = loads(db.kv_get(BULK_AT)) or {}
    said = loads(db.kv_get(SHAPE_SAID)) or {}
    out: dict[str, dict[str, dict]] = {}
    for mp in sites:
        poster = ps.get(mp)
        if poster is None or not hasattr(poster, "read_shop") or not getattr(poster, "available", lambda: True)():
            continue
        if crosslist.blocked(db, mp):
            continue
        last = _when(stamps.get(mp))
        if last is not None and now - last < timedelta(seconds=BULK_EVERY):
            continue
        try:
            bulk = await poster.read_shop()
        except Exception as e:  # noqa: BLE001
            db.log(None, "photocheck_bulk_failed", {"site": mp, "error": f"{type(e).__name__}: {e}"[:200]})
            continue
        if bulk.get("error"):
            db.log(None, "photocheck_bulk_failed", {"site": mp, "error": str(bulk["error"])[:200]})
            continue
        stamps[mp] = now.isoformat(timespec="seconds")
        db.kv_set(BULK_AT, json.dumps(stamps))
        if bulk.get("shape") and said.get(mp) != bulk["shape"]:
            said[mp] = bulk["shape"]
            db.kv_set(SHAPE_SAID, json.dumps(said))
            db.log(None, "photocheck_shape", {"site": mp, "keys": bulk["shape"], "source": bulk.get("source")})
        out[mp] = bulk_lookup(mp, bulk)
        db.log(None, "photocheck_bulk", {"site": mp, "listings": len(bulk.get("listings") or []),
                                         "source": bulk.get("source"), "note": bulk.get("note")})
    return out


def _single_ok(db: DB, key: str, now: datetime) -> bool:
    """A listing's own page is opened at most every SINGLE_EVERY (the bulk data is the rule)."""
    stamps = loads(db.kv_get(SINGLE_AT)) or {}
    last = _when(stamps.get(key))
    if last is not None and now - last < timedelta(seconds=SINGLE_EVERY):
        return False
    stamps[key] = now.isoformat(timespec="seconds")
    if len(stamps) > 500:
        stamps = dict(sorted(stamps.items(), key=lambda kv: kv[1])[-400:])
    db.kv_set(SINGLE_AT, json.dumps(stamps))
    return True


def sent_photos(db: DB, row) -> list[str]:
    """The photos the listing was posted with, in order: Depop's / Vinted's mapped list, Poshmark's render."""
    fields = loads(row["fields_json"]) or {}
    photos = fields.get("photos")
    if isinstance(photos, list) and photos:
        return [str(p) for p in photos]
    it = db.item(row["item_id"])
    render = ((loads(it["renders"]) or {}).get("poshmark") or {}) if it else {}
    return [str(p) for p in render.get("photos") or []]


def money(value) -> float | None:
    """A price as the site's page gives it — 35, "35.00", "$35.00", "1,250" (live 2026-10-18: Vinted's and Depop's
    pages say "$35.00") — as a number, or None."""
    if value is None:
        return None
    m = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(m.group()) if m else None


def record(db: DB, iid: str, mp: str, photos: int | None, price) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fields = {"photos_read_at": stamp}
    if photos is not None:
        fields["photos_live"] = int(photos)
    if (amount := money(price)) is not None:
        fields["price_live"] = amount
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
    amount = money(price)
    if amount is not None and row["price"] is not None and abs(amount - float(row["price"])) >= 1:
        db.log(iid, "price_differs", {"site": mp, "site_price": amount, "ours": row["price"]})
        note += f"; price {amount:g} on the site vs {row['price']} ours"
    return note


async def run(s: Settings, db: DB, ps: dict, limit: int = PER_RUN) -> list[str]:
    """The housekeeping's run. Poshmark: the public JSON of up to `limit` listings (an HTTP GET each, once a day).
    Depop / Vinted: the shop read in BULK (one JSON read every 6 hours: every listing's price, state and photo count)
    and applied to every due listing at once; a listing's own edit page is opened only when the bulk data lacks its
    photo count or shows fewer than were sent (to confirm before any edit), at most once a day per listing and
    `limit` a run. Never raises."""
    db.kv_set(LAST_RUN, str(time.time()))
    now = datetime.now(timezone.utc)
    due = candidates(db, now)
    bulk = await bulk_reads(s, db, ps, sorted({r["marketplace"] for r in due if r["marketplace"] != "poshmark"}), now)
    done, singles = [], 0
    for row in due:
        iid, mp = row["item_id"], row["marketplace"]
        try:
            if mp == "poshmark":
                if singles >= limit:
                    continue
                singles += 1
                live = await asyncio.to_thread(crosslist.poshmark_listing, row["url"]) or {}
                if not live:
                    continue
                if live.get("for_sale") is False:
                    record(db, iid, mp, live.get("photos"), live.get("price"))
                    continue
                done.append(apply(s, db, row, live))
                continue
            poster = ps.get(mp)
            if poster is None or not getattr(poster, "available", lambda: True)() or crosslist.blocked(db, mp):
                continue
            key = _key_of(mp, row)
            rec = (bulk.get(mp) or {}).get(str(key)) if key else None
            sent = len(sent_photos(db, row))
            live = {}
            if rec is not None:
                if rec.get("sold") or rec.get("is_closed") or rec.get("is_hidden"):
                    record(db, iid, mp, rec.get("photos"), rec.get("price"))
                    continue                                 # not for sale: nothing to add, nothing to say
                live = {"photos": rec.get("photos"), "price": rec.get("price")}
            elif mp in bulk:
                db.log(iid, "photocheck_not_in_bulk", {"site": mp, "key": key})
            # the edit page only when the bulk data gives no photo count, or a short one (confirmed before any edit)
            if (live.get("photos") is None or int(live["photos"]) < sent) and hasattr(poster, "read_photos"):
                if singles >= limit or not _single_ok(db, f"{iid}|{mp}", now):
                    if live.get("photos") is None:
                        continue                             # nothing to apply without a count; next time
                else:
                    singles += 1
                    tiles = await poster.read_photos(row["url"], s.path("failed") / "shots", iid)
                    if tiles.get("count") is not None:
                        live = {**live, "photos": max(int(live.get("photos") or 0), int(tiles["count"]))}
            if live.get("photos") is None and live.get("price") is None:
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
