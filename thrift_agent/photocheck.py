"""WO45 §6/§7: every listing is checked after posting — its photo count and its price, from the site's own data.

Live 2026-10-18: the Ralph Lauren shirt went up on Vinted and Depop with one photo of four, and nothing read the
listings back. Now each live listing is read (Poshmark's public JSON; Depop's and Vinted's listing page through the
extension's verify job) — once soon after it posts, then with the order sweep's rhythm (every CHECK_EVERY) — and what
the site shows is kept on its row (`listings.photos_live`, `price_live`). A listing with fewer photos than its item
sent gets ONE "photos" edit (edits.queue_photos: the missing ones through the edit page, nothing else changed); a
listing whose price differs from the one we last set there (`our_price`: our newest drop, else the posted price) is
noted ONCE per change (the owner's hand change: recorded, never undone, WO41's price drops read it the same way). A
site that can't be read is left for the next look; an edit that fails three times is one private line
(edits._failed).

WO56 §0.3 (the architect, read only, 2026-11-09: the Tory Burch loafers `i_261012_6e29a8` were live on Depop, "listed
28 days ago", yet flagged `photocheck_not_in_bulk` there since Nov 3 — Depop's bulk read was the shop page's first 25
tiles every time, its products JSON never having answered; on Vinted they really were gone, the item JSON 404): the
bulk read pages through ALL of each shop (the extension: Vinted's wardrobe to its last page, Depop's products JSON by
its own cursor, else every tile of the shop page scrolled); a listing the bulk data lacks is never called gone for that
— its OWN data is read (`confirm_absent`: Depop its listing page, Vinted its item JSON; a listing known live read
first, so a wrong address can't make everything "404"): live → nothing more; sold → the sale net; 404 → gone, seen
again 10+ minutes later. And only a SALE takes the item down on its other sites: a listing merely gone from one site
(404, removed, closed without a sale, Poshmark's Not for Sale) is that row only — `delisted`, "gone from <Site> (its
own data: …) — not relisted", ONE private line — and is never posted there again (`sales.listing_gone`). The flags of
before are re-read once by `thrift photocheck --recheck-not-in-bulk` (`ask_recheck` → `serve_recheck`)."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thrift_agent import jobs, crosslist, edits, notify
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads

CHECK_EVERY = 6 * 3600            # a live listing read again this often (the order sweep's rhythm)
POSHMARK_EVERY = 2 * 3600         # Poshmark's public JSON (an HTTP GET a listing). WO49: every 2 hours — it is also the
#                                   sale safety net that needs no email (live: the dress sold on Poshmark, its email
#                                   unrecognised, and stayed on Depop and Vinted for a day)
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
ABSENT = "photocheck_absent"      # kv (WO56): {item|site: {"key", "since", "checked", "state", "gone"}} — listings the
#                                   bulk data lacked: each read by its own data, never called gone for the absence
ABSENT_EVERY = 24 * 3600          # a listing still missing from the bulk data: its own data read again this often …
ABSENT_PER_RUN = 20               # … this many a site in one run (ONE job, one fetch each, a breath between)
CONTROL = "photocheck_control"    # kv (WO56): {site: {"key", "url"}} — a listing the last bulk read showed (known live)
RECHECK = "photocheck_recheck"    # kv (WO56): the one re-check `thrift photocheck --recheck-not-in-bulk` asked for
RECHECK_CAP = 60                  # the re-check's listings a site in one job
GONE_PREFIX = "gone from "        # (WO56) the error of a row its site no longer has: "gone from Vinted (…) — not relisted"


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
            jobs.failed(db, f"photo_check:{mp}", f"{type(e).__name__}: {e}")            # WO52 §4
            continue
        if bulk.get("error"):
            db.log(None, "photocheck_bulk_failed", {"site": mp, "error": str(bulk["error"])[:200]})
            jobs.failed(db, f"photo_check:{mp}", str(bulk["error"]))
            continue
        jobs.ok(db, f"photo_check:{mp}")
        stamps[mp] = now.isoformat(timespec="seconds")
        db.kv_set(BULK_AT, json.dumps(stamps))
        if bulk.get("shape") and said.get(mp) != bulk["shape"]:
            said[mp] = bulk["shape"]
            db.kv_set(SHAPE_SAID, json.dumps(said))
            db.log(None, "photocheck_shape", {"site": mp, "keys": bulk["shape"], "source": bulk.get("source")})
        out[mp] = bulk_lookup(mp, bulk)
        # WO56 §0.3: how far the read went — its pages, whether it reached the shop's end (`complete`), the JSON address
        # that answered (`base`: to be recorded in ext/selectors.json) and the ones that didn't (`tried`)
        db.log(None, "photocheck_bulk", {"site": mp, "listings": len(bulk.get("listings") or []),
                                         "source": bulk.get("source"), "note": bulk.get("note"),
                                         **{k: bulk[k] for k in ("pages", "complete", "total", "base", "tried")
                                            if bulk.get(k) is not None}})
        _keep_control(db, mp, bulk)
    return out


def _keep_control(db: DB, mp: str, bulk: dict) -> None:
    """WO56: a listing the bulk read showed — known live — kept for the own-data reads (`confirm_absent`): a 404 is
    believed only when this one answers."""
    from thrift_agent.db import listing_id_from
    for rec in bulk.get("listings") or []:
        if not isinstance(rec, dict):
            continue
        key = (rec.get("slug") or listing_id_from("depop", rec.get("url"))) if mp == "depop" else rec.get("id")
        if key:
            known = loads(db.kv_get(CONTROL)) or {}
            known[mp] = {"key": str(key), "url": rec.get("url")}
            db.kv_set(CONTROL, json.dumps(known))
            return


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
    before = _price_read(row)                         # WO54: the reading before this one (a change is said once)
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
    ours = our_price(db, row)
    if amount is not None and ours is not None and abs(amount - ours) >= 1:
        note += f"; price {amount:g} on the site vs {ours:g} ours"
        if before is None or abs(before - amount) >= 1:   # a new change — the same reading again is no news
            db.log(iid, "price_differs", {"site": mp, "site_price": amount, "ours": ours})
    return note


def _price_read(row) -> float | None:
    try:
        value = row["price_live"]
    except (IndexError, KeyError):
        return None
    return float(value) if value is not None else None


def our_price(db: DB, row) -> float | None:
    """WO54: the price WE last set on this listing — the newest price change done there (an automatic drop, or her
    own price through us), else the price it was posted at. Live 2026-11-09: 79 `price_differs` in a day for 21
    listings — 26 of them our own drops (30 → 27 …) compared with the posted price, the other 53 fourteen hand changes
    read again every 2 h (Poshmark) or 6 h (Vinted)."""
    try:
        done = db.conn.execute("SELECT new_price FROM price_changes WHERE item_id=? AND marketplace=? AND dry=0 AND "
                               "status='done' AND new_price IS NOT NULL ORDER BY COALESCE(done_at, created_at) DESC "
                               "LIMIT 1", (row["item_id"], row["marketplace"])).fetchone()
    except sqlite3.OperationalError:                  # a database from before the price drops
        done = None
    if done is not None:
        return float(done["new_price"])
    return float(row["price"]) if row["price"] is not None else None


async def run(s: Settings, db: DB, ps: dict, limit: int = PER_RUN) -> list[str]:
    """The housekeeping's run. Poshmark: the public JSON of up to `limit` listings (an HTTP GET each, once a day).
    Depop / Vinted: the shop read in BULK (one JSON read every 6 hours: every listing's price, state and photo count)
    and applied to every due listing at once; a listing's own edit page is opened only when the bulk data lacks its
    photo count or shows fewer than were sent (to confirm before any edit), at most once a day per listing and
    `limit` a run. WO56: a listing the bulk data lacks is read by its own data (`confirm_absent`, at most
    ABSENT_PER_RUN a site in one job), never called gone for the absence. Never raises."""
    db.kv_set(LAST_RUN, str(time.time()))
    now = datetime.now(timezone.utc)
    done: list[str] = []
    try:
        done += await serve_recheck(s, db, ps, now)          # WO56: `thrift photocheck --recheck-not-in-bulk`
    except Exception as e:  # noqa: BLE001 — a reading never ends the poster
        db.log(None, "photocheck_recheck_failed", f"{type(e).__name__}: {e}"[:200])
    due = candidates(db, now)
    bulk = await bulk_reads(s, db, ps, sorted({r["marketplace"] for r in due if r["marketplace"] != "poshmark"}), now)
    absent = loads(db.kv_get(ABSENT)) or {}
    stamp = now.isoformat(timespec="seconds")
    check: dict[str, list] = {}
    singles = 0
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
                    status = live.get("status") or "not for sale"
                    why = ("its own data: its listing page answers 404" if status == "gone"   # WO56: deleted / removed
                           else f"Poshmark shows it {status}")
                    _gone(db, row, mp, why, bool(live.get("sold")), live.get("price"))
                    continue
                _back(db, row, mp)                             # for sale: a first 'gone' reading forgotten
                done.append(apply(s, db, row, live))
                continue
            poster = ps.get(mp)
            if poster is None or not getattr(poster, "available", lambda: True)() or crosslist.blocked(db, mp):
                continue
            key = _key_of(mp, row)
            rec = (bulk.get(mp) or {}).get(str(key)) if key else None
            sent = len(sent_photos(db, row))
            live = {}
            here = f"{iid}|{mp}"
            if rec is not None:
                if absent.pop(here, None) is not None:        # WO56: in the shop's data again — no own read needed
                    db.log(iid, "photocheck_absent_cleared", {"site": mp, "key": key, "why": "in the bulk data again"})
                if rec.get("sold") or rec.get("is_closed") or rec.get("is_hidden"):
                    record(db, iid, mp, rec.get("photos"), rec.get("price"))
                    closing = str(rec.get("closing") or "").lower()
                    if rec.get("sold") or (rec.get("is_closed") and (not closing or "sold" in closing)):
                        _gone(db, row, mp, f"{mp.title()} shows it sold", True, rec.get("price"))   # WO49: its take-
                    elif rec.get("is_closed"):                 # downs elsewhere. WO56: closed without a sale — gone
                        _gone(db, row, mp, f"its own data: closed on {crosslist.LABEL.get(mp, mp)} ({closing})", False)
                    continue                                 # hidden (by hand, or our own take-down): nothing to say
                live = {"photos": rec.get("photos"), "price": rec.get("price")}
            elif key and (mp in bulk or here in absent):
                # WO56 §0.3: missing from the shop's data — never "gone" for that: its own data is read (below)
                if here not in absent:
                    absent[here] = {"key": str(key), "since": stamp}
                    db.log(iid, "photocheck_not_in_bulk", {"site": mp, "key": key})
                if _absent_due(absent[here], now):
                    check.setdefault(mp, []).append(row)
                continue
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
            _back(db, row, mp)                             # for sale: a first 'gone' reading forgotten
            done.append(apply(s, db, row, live))
        except Exception as e:  # noqa: BLE001 — a reading never ends the poster
            db.log(iid, "photocheck_failed", {"site": mp, "error": f"{type(e).__name__}: {e}"[:200]})
    # WO56: the missing listings whose own read is due though their rows weren't (the 10-minute second read of a 404)
    queued = {f"{r['item_id']}|{r['marketplace']}" for rows in check.values() for r in rows}
    for here, entry in list(absent.items()):
        iid, _, mp = here.partition("|")
        row = db.listing(iid, mp)
        if row is None or row["status"] != "posted" or not row["url"]:
            absent.pop(here, None)                         # sold, taken down or gone since: nothing to read
            continue
        if here not in queued and _absent_due(entry, now):
            check.setdefault(mp, []).append(row)
    db.kv_set(ABSENT, json.dumps(absent))
    try:
        done += [x[4] for x in await confirm_absent(s, db, ps, check, now)]
    except Exception as e:  # noqa: BLE001 — a reading never ends the poster
        db.log(None, "photocheck_failed", {"site": "own data", "error": f"{type(e).__name__}: {e}"[:200]})
    if done:
        db.log(None, "photocheck", {"read": done})
    return done


def _absent_due(entry: dict, now: datetime) -> bool:
    """WO56: a missing listing's own data is read when never read yet; GONE_AGAIN after a first 404 (the second
    reading that acts); else once every ABSENT_EVERY."""
    checked = _when(entry.get("checked"))
    if checked is None:
        return True
    wait = GONE_AGAIN if entry.get("gone") else ABSENT_EVERY
    return (now - checked).total_seconds() >= wait


async def confirm_absent(s: Settings, db: DB, ps: dict, check: dict[str, list], now: datetime, dry: bool = False,
                         cap: int = ABSENT_PER_RUN) -> list[tuple[str, str, str, bool, str]]:
    """WO56 §0.3: the listings the bulk data lacked, each read from the site's OWN data — ONE job a site
    (`listing_states`: Depop its listing page, Vinted its item JSON; a listing known live read first) — and what it
    says decides: live → the flag cleared (the reading applied when it carries the photos or the price); sold → the
    sale net (its take-downs elsewhere); 404 → gone, acting when seen again GONE_AGAIN later (`_gone`): that row only,
    never relisted; hidden or a draft → nothing (by hand, or our own take-down); anything else → read again tomorrow.
    `dry`: read and reported, nothing recorded. Returns [(item, site, state, acted, note)]."""
    absent = loads(db.kv_get(ABSENT)) or {}
    stamp = now.isoformat(timespec="seconds")
    known = loads(db.kv_get(CONTROL)) or {}
    out: list[tuple[str, str, str, bool, str]] = []
    for mp, rows in sorted(check.items()):
        poster = ps.get(mp)
        if poster is None or not hasattr(poster, "listing_states") or not getattr(poster, "available", lambda: True)() \
                or crosslist.blocked(db, mp):
            continue
        rows = [r for r in rows if _key_of(mp, r)][:cap]
        if not rows:
            continue
        keys = [{"key": str(_key_of(mp, r)), "url": r["url"]} for r in rows]
        try:
            res = await poster.listing_states(keys, known.get(mp))
        except Exception as e:  # noqa: BLE001 — a login page, the extension away: the next run
            db.log(None, "photocheck_absent_failed", {"site": mp, "error": f"{type(e).__name__}: {e}"[:200]})
            continue
        if res.get("error"):
            db.log(None, "photocheck_absent_failed", {"site": mp, "error": str(res["error"])[:200]})
            continue
        if not res.get("trusted"):
            db.log(None, "photocheck_control_unread", {"site": mp, "control": res.get("control")})
        states = res.get("states") or {}
        label = crosslist.LABEL.get(mp, mp)
        for r in rows:
            iid, key = r["item_id"], str(_key_of(mp, r))
            st = states.get(key) or {}
            state = str(st.get("state") or "unknown")
            here = f"{iid}|{mp}"
            db.log(iid, "photocheck_absent_checked", {"site": mp, "key": key, "state": state, "http": st.get("http"),
                                                      "how": st.get("how"), "detail": st.get("detail"), "dry": dry})
            acted = False
            if dry:
                out.append((iid, mp, state, False, f"{mp} {key}: {state} (dry run)"))
                continue
            entry = absent.setdefault(here, {"key": key, "since": stamp})
            entry.update(checked=stamp, state=state)
            if state == "live":
                # cleared — and kept as read live, so a bulk read that still lacks it (the shop page's tiles, not to
                # its end) doesn't flag it again: its own data is read again once a day (its count, its price)
                entry.pop("gone", None)
                _back(db, r, mp)
                db.log(iid, "photocheck_absent_cleared", {"site": mp, "key": key, "why": f"{st.get('how')} shows it live"})
                if st.get("photos") is not None or st.get("price") is not None:
                    apply(s, db, r, {"photos": st.get("photos"), "price": st.get("price")})
            elif state == "sold":
                absent.pop(here, None)
                acted = _gone(db, r, mp, f"{label} shows it sold ({st.get('how') or 'its own data'})", True,
                              st.get("price"))
            elif state == "gone":
                entry["gone"] = True
                acted = _gone(db, r, mp, f"its own data: {st.get('how') or 'its page'} answers {st.get('http') or 404}",
                              False)
                if acted:
                    absent.pop(here, None)
            elif state in ("hidden", "draft"):
                absent.pop(here, None)                 # hidden by hand, or our own take-down: nothing to say
            out.append((iid, mp, state, acted, f"{mp} {key}: {state}" + (" — recorded" if acted else "")))
    if not dry:
        db.kv_set(ABSENT, json.dumps(absent))
    return out


# ---------------------------------------------------------------- WO56: the one re-check of the old flags

def flagged_since(db: DB, since: str) -> list:
    """The listings `photocheck_not_in_bulk` flagged since `since` (an ISO day or time) that are still posted — the
    rows themselves, Depop's and Vinted's."""
    seen, rows = set(), []
    for r in db.conn.execute("SELECT ref, detail FROM events WHERE kind='photocheck_not_in_bulk' AND ts >= ? "
                             "ORDER BY ts", (since,)).fetchall():
        site = (loads(r["detail"]) or {}).get("site") if r["detail"] else None
        if not r["ref"] or site not in ("depop", "vinted") or (r["ref"], site) in seen:
            continue
        seen.add((r["ref"], site))
        row = db.listing(r["ref"], site)
        if row is not None and row["status"] == "posted" and row["url"]:
            rows.append(row)
    return rows


def ask_recheck(db: DB, since: str, dry: bool = False) -> dict[str, int]:
    """`thrift photocheck --recheck-not-in-bulk [--since …] [--dry-run]`: every listing flagged "not in bulk" since
    `since` and still posted is read again by its own data — by the running poster (the extension is its), at its
    next photo check. Returns {site: listings asked}."""
    rows = flagged_since(db, since)
    by_site: dict[str, int] = {}
    for r in rows:
        by_site[r["marketplace"]] = by_site.get(r["marketplace"], 0) + 1
    db.kv_set(RECHECK, json.dumps({"since": since, "dry": bool(dry), "rows": [[r["item_id"], r["marketplace"]]
                                                                             for r in rows],
                                   "asked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}))
    db.log(None, "photocheck_recheck_asked", {"since": since, "dry": bool(dry), "by_site": by_site})
    return by_site


async def serve_recheck(s: Settings, db: DB, ps: dict, now: datetime) -> list[str]:
    """The re-check `ask_recheck` asked for, by the running poster: the listings' own data read (RECHECK_CAP a site in
    one job; the rest at the next runs) and applied as `confirm_absent` does — a 404 is recorded gone when seen again
    10 minutes later (the regular runs read it then) — and ONE private line with what was found."""
    req = loads(db.kv_get(RECHECK)) or {}
    pairs = [tuple(x) for x in req.get("rows") or [] if isinstance(x, list) and len(x) == 2]
    if not req or not pairs:
        if req:
            db.kv_set(RECHECK, "")
        return []
    asked = _when(req.get("asked_at"))
    if asked is not None and (now - asked).total_seconds() > ABSENT_EVERY:      # a site never readable since: said once
        db.kv_set(RECHECK, "")
        line = (f"🔎 The re-check of the listings flagged 'not in bulk' since {req.get('since')} couldn't read "
                f"{len(pairs)} of them within a day (the site's page or the Thrift Chrome away) — the photo check reads "
                "them by itself when it can.")
        notify.say(line)
        return [line]
    check: dict[str, list] = {}
    for iid, mp in pairs:
        row = db.listing(iid, mp)
        if row is not None and row["status"] == "posted" and row["url"] and ps.get(mp) is not None:
            check.setdefault(mp, []).append(row)
    if not check:
        db.kv_set(RECHECK, "")
        return []
    now_check = {mp: rows[:RECHECK_CAP] for mp, rows in check.items()}
    results = await confirm_absent(s, db, ps, now_check, now, dry=bool(req.get("dry")), cap=RECHECK_CAP)
    if not results:
        return []                                         # a site couldn't be read now: the request stays, next run
    served = {(iid, mp) for iid, mp, *_ in results}       # past the cap, or a site that couldn't be read: next runs
    rest = [[r["item_id"], mp] for mp, rows in check.items() for r in rows if (r["item_id"], mp) not in served]
    db.kv_set(RECHECK, json.dumps({**req, "rows": rest}) if rest else "")
    counts: dict[str, int] = {}
    for _, _, state, _, _ in results:
        counts[state] = counts.get(state, 0) + 1
    dry = bool(req.get("dry"))
    words = ({"live": "live", "sold": "sold", "gone": "gone (404)", "hidden": "hidden", "draft": "a draft",
              "unknown": "unreadable now"} if dry else
             {"live": "live (flag cleared)", "sold": "sold (the sale net)",
              "gone": "gone (recorded once a second read 10 minutes later agrees)", "hidden": "hidden (left as is)",
              "draft": "a draft (left as is)", "unknown": "unreadable now (read again tomorrow)"})
    parts = [f"{n} {words.get(k, k)}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    gone = [f"{crosslist.item_name(db, iid)} ({crosslist.LABEL.get(mp, mp)})" for iid, mp, state, _, _ in results
            if state == "gone"]
    line = (f"🔎 Re-check of the listings flagged 'not in bulk' since {req.get('since')}: {len(results)} read by their "
            f"own data — {', '.join(parts) or 'nothing'}" + (f". Gone: {'; '.join(gone[:6])}" if gone else "")
            + (f". {len(rest)} more at the next runs" if rest else "") + (" (dry run: nothing recorded)."
                                                                        if req.get("dry") else "."))
    notify.say(line)
    db.log(None, "photocheck_rechecked", {"counts": counts, "gone": gone, "rest": len(rest), "dry": bool(req.get("dry"))})
    return [line]


GONE_SEEN = "photocheck_gone_seen"   # kv: {item|site: when a listing was first seen not for sale (not sold)}
GONE_AGAIN = 10 * 60               # WO49: a "not for sale" / 404 reading acts only when seen again this much later


def _back(db: DB, row, mp: str) -> None:
    """WO49: the listing read for sale — a first "not for sale" sighting of it is forgotten."""
    seen = loads(db.kv_get(GONE_SEEN)) or {}
    if seen.pop(f"{row['item_id']}|{mp}", None) is not None:
        db.kv_set(GONE_SEEN, json.dumps(seen))


def _gone(db: DB, row, mp: str, why: str, sold: bool, price=None) -> bool:
    """WO49: a listing we hold 'posted' that its site shows sold or no longer for sale. The site saying SOLD acts at
    once — `sales.listing_gone` tells thrift-api (the sale: take-downs everywhere else) unless the take-down is ours;
    "not for sale" or a page that answers 404 only when seen again GONE_AGAIN later (one odd answer never takes
    anything down). WO56 §0.3: a listing merely gone (404, removed, closed without a sale, Not for Sale) is that row
    only — `delisted`, "gone from <Site> (…) — not relisted", nothing taken down elsewhere — and ONE private line says
    so (`gone_line`). True when it acted. Never raises."""
    from thrift_agent import sales
    key = f"{row['item_id']}|{mp}"
    if not sold:
        seen = loads(db.kv_get(GONE_SEEN)) or {}
        first = seen.get(key)
        now = time.time()
        if first is None or now - float(first) < GONE_AGAIN:
            if first is None:
                seen[key] = now
                db.kv_set(GONE_SEEN, json.dumps(seen))
            db.log(row["item_id"], "listing_gone_seen", {"site": mp, "why": why, "first": first is None})
            return False
        seen.pop(key, None)
        db.kv_set(GONE_SEEN, json.dumps(seen))
    try:
        title = ((loads((db.item(row["item_id"]) or {"renders": None})["renders"]) or {}).get("poshmark") or {}).get("title")
        did = sales.listing_gone(db, row["item_id"], mp, why, sold, row["url"], title, price)
    except Exception as e:  # noqa: BLE001 — the check goes on
        db.log(row["item_id"], "listing_gone_failed", {"site": mp, "error": f"{type(e).__name__}: {e}"[:200]})
        return False
    if did == "gone":
        notify.say(gone_line(db, row["item_id"], mp, why))
    return True


def gone_line(db: DB, iid: str, mp: str, why: str) -> str:
    """WO56 §0.2's line for a listing its site no longer has — nothing else was taken down, nothing asked:
    "Vinted no longer has the Tory Burch … (deleted by you, or removed by Vinted — check the Vinted app's
    notifications). Still live on Poshmark and Depop." Poshmark's Not for Sale is said as what it is."""
    site = crosslist.LABEL.get(mp, mp)
    others = [crosslist.LABEL.get(r["marketplace"], r["marketplace"]) for r in db.listings_for(iid)
              if r["marketplace"] != mp and r["status"] == "posted"]
    tail = f"Still live on {crosslist._joined(others)}." if others else "It isn't live on any other site."
    name = crosslist.item_name(db, iid)
    if "not_for_sale" in why or "not for sale" in why.lower():
        return f"{site} shows {name} as Not for Sale (set on {site}) — nothing else was taken down. {tail}"
    return (f"{site} no longer has {name} (deleted by you, or removed by {site} — check the {site} app's "
            f"notifications). {tail}")


def _when(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts)
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
