"""WO57 §5 — eBay's own housekeeping, from the poster's shared duties (never in the quiet hours — the caller's rule):
the consent's state for the daily window and doctor (kv `ebay_link`), the backfill's next item, the month's free
listings (250 a month without a store, then $0.35 each: ONE private line the first time a month passes them), the fee
file's age (ONE private line at 90 days). Also what the heartbeat carries for thrift-api's 9 PM line, and doctor's
lines. Nothing here calls eBay but the token check the poster makes anyway."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from thrift_agent import notify
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads
from thrift_agent.ebay import LABEL, SITE, backfill, fees

LINK = "ebay_link"                 # kv (crosslist.EBAY_LINK): {"connected", "at", "why"}
SAID = "ebay_said"                 # kv: {"fee_month": "2026-11", "stale": "2026-11-10"} — each line once
HAND_LISTINGS = 10                 # the owner's own hand-made eBay listings: they use free listings too (a setting)


def _tz(s: Settings | None) -> ZoneInfo:
    return ZoneInfo(str((s.get("schedule.timezone") if s is not None else None) or "America/New_York"))


def on(s: Settings | None) -> bool:
    return bool(s is not None and s.get("marketplaces.ebay.enabled", False))


def note_link(db: DB, poster, now: datetime | None = None) -> bool:
    """The poster's view of the eBay consent, kept for the daily window ("still to publish" never waits on a consent
    that isn't there) and doctor. Returns whether eBay can be worked on now."""
    ok = bool(poster.available())
    was = loads(db.kv_get(LINK)) or {}
    if was.get("connected") != ok:
        db.log(None, "ebay_link", {"connected": ok, "why": None if ok else poster.why_unavailable()})
    if was.get("connected") != ok or not was:
        db.kv_set(LINK, json.dumps({"connected": ok, "why": None if ok else poster.why_unavailable(),
                                    "at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")}))
    return ok


def month_of(s: Settings | None, now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(_tz(s)).strftime("%Y-%m")


def free_used(s: Settings | None, db: DB, now: datetime | None = None) -> int:
    """About how many of the month's free listings are used: our eBay listings made this month, our older ones still
    live whose monthly renewal day has come (a Good 'Til Cancelled listing renews each month and counts again), and
    the owner's own hand-made listings (`marketplaces.ebay.hand_listings`). eBay's own count is the truth — its
    answer to each new listing names the fee, and the first one that costs says so (`note_fee`)."""
    now = (now or datetime.now(timezone.utc)).astimezone(_tz(s))
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start_utc = start.astimezone(timezone.utc).isoformat(timespec="seconds")
    new = db.conn.execute("SELECT COUNT(*) FROM listings WHERE marketplace=? AND posted_at >= ? AND status IN "
                          "('posted', 'sold', 'delisted')", (SITE, start_utc)).fetchone()[0]
    renewed = 0
    for (posted,) in db.conn.execute("SELECT posted_at FROM listings WHERE marketplace=? AND status='posted' AND "
                                     "posted_at < ?", (SITE, start_utc)):
        try:
            day = datetime.fromisoformat(posted).astimezone(_tz(s)).day
        except (TypeError, ValueError):
            continue
        renewed += 1 if day <= now.day else 0
    try:
        hand = int(s.get("marketplaces.ebay.hand_listings", HAND_LISTINGS)) if s is not None else HAND_LISTINGS
    except (TypeError, ValueError):
        hand = HAND_LISTINGS
    return int(new) + renewed + max(0, hand)


def _said(db: DB) -> dict:
    return loads(db.kv_get(SAID)) or {}


def note_fee(s: Settings | None, db: DB, fee: float | None, now: datetime | None = None) -> bool:
    """eBay named an insertion fee for the listing just made: the month's free listings are used up — ONE private
    line the first time in a month. True when it was said."""
    if not fee or float(fee) <= 0:
        return False
    month = month_of(s, now)
    said = _said(db)
    if said.get("fee_month") == month:
        return False
    said["fee_month"] = month
    db.kv_set(SAID, json.dumps(said))
    try:
        each = fees.load().insertion.fee
    except fees.FeesError:
        each = float(fee)
    db.log(None, "ebay_free_listings_used", {"month": month, "fee": float(fee)})
    notify.say(f"From now each new eBay listing costs ${each:.2f} this month.")
    return True


def note_stale(db: DB, today=None) -> bool:
    """The fee file was last compared with eBay's fee page 90 days ago or more: ONE private line (per check date)."""
    try:
        table = fees.load()
    except fees.FeesError:
        return False
    days = fees.stale(table, today)
    said = _said(db)
    if days is None or said.get("stale") == table.checked.isoformat():
        return False
    said["stale"] = table.checked.isoformat()
    db.kv_set(SAID, json.dumps(said))
    notify.say(f"eBay's fee table (data/ebay_fees.json) was last checked against eBay's fee page {days} days ago "
               f"({table.checked:%b %d, %Y}) — the eBay prices are worked out from it; it needs a look.")
    return True


def tick(s: Settings, db: DB, ps: dict, dry: bool = False, now: datetime | None = None, read_poshmark=None) -> dict:
    """The poster's housekeeping turn for eBay. Returns what it did (the tests read it)."""
    poster = ps.get(SITE)
    if poster is None:
        return {}
    done: dict = {"connected": note_link(db, poster, now)}
    done["stale"] = note_stale(db, (now or datetime.now(timezone.utc)).astimezone(_tz(s)).date())
    if not dry and done["connected"]:
        if read_poshmark is None:
            from thrift_agent import crosslist
            read_poshmark = crosslist.poshmark_page
        done["backfilled"] = backfill.tick(s, db, now, read_poshmark)
    return done


def beat(s: Settings | None, db: DB, now: datetime | None = None) -> dict:
    """What the heartbeat carries for thrift-api: is eBay on, live, connected; the month's free listings; the fee
    file's date."""
    if not on(s):
        return {"on": False}
    from thrift_agent import crosslist
    link = loads(db.kv_get(LINK)) or {}
    try:
        checked = fees.load().checked.isoformat()
    except fees.FeesError:
        checked = None
    return {"on": True, "live": crosslist.live(s, SITE), "connected": link.get("connected"),
            "free_used": free_used(s, db, now), "free_month": month_of(s, now), "fees_checked": checked,
            "backfill": backfill.status(db)}


def doctor_lines(s: Settings | None, db: DB, now: datetime | None = None) -> list[str]:
    """`thrift doctor`: eBay's consent, the fee file's date, the month's free listings, the backfill."""
    if not on(s):
        return []
    link = loads(db.kv_get(LINK)) or {}
    state = "connected" if link.get("connected") else f"NOT connected ({link.get('why') or 'not asked yet'})"
    out = [f"{LABEL} API: {state}"]
    try:
        table = fees.load()
        days = fees.age_days(table, (now or datetime.now(timezone.utc)).astimezone(_tz(s)).date())
        out.append(f"  fee table checked {table.checked:%b %d, %Y} ({days} days ago)" + (" ⚠️" if days >= fees.STALE_DAYS else "")
                   + f" · about {free_used(s, db, now)} of {table.insertion.free_per_month} free listings used this month")
    except fees.FeesError as e:
        out.append(f"  ⚠️ fee table: {e}")
    st = backfill.status(db)
    if st["planned"]:
        out.append(f"  backfill since {st.get('since')}: {st['live']} live of {st['queued']} queued, {st['waiting']} "
                   f"waiting ({backfill.per_day(s)} a day)" + (f", {st['left']} left out" if st["left"] else ""))
    return out


# ---------------------------------------------------------------- our records against eBay's own (WO53 §7's net, for eBay)

RECONCILE_EVERY = 1800             # seconds: every 30 minutes, and so before the 9 PM line
RECONCILE_AT = "ebay_reconcile_at"
RECONCILE_SAID = "ebay_reconcile_said"


def reconcile_due(db: DB, now: float) -> bool:
    return now - float(db.kv_get(RECONCILE_AT) or 0) >= RECONCILE_EVERY


def _once(db: DB, key: str) -> bool:
    said = loads(db.kv_get(RECONCILE_SAID)) or []
    if key in said:
        return False
    db.kv_set(RECONCILE_SAID, json.dumps([*said, key][-300:]))
    return True


async def reconcile(s: Settings, db: DB, ps: dict, now: float | None = None) -> dict:
    """The seller's active eBay listings that carry one of OUR item ids as their SKU, against our records: a live
    listing that isn't recorded (the poster died between eBay's answer and the record) is recorded — posted, the ✅
    card — with ONE private line; two live listings for one item → ONE private line with both links, nothing ended.
    A recorded listing that is gone is the photo check's; the owner's hand-made listings (no SKU of ours) are never
    looked at. Returns what it found."""
    import time

    from thrift_agent import crosslist
    from thrift_agent.post import runner
    poster = ps.get(SITE)
    out: dict = {"adopted": [], "double": []}
    db.kv_set(RECONCILE_AT, str(now if now is not None else time.time()))
    if poster is None or not poster.available():
        return out
    try:
        listings, _ = await poster.client.active()
    except Exception as e:  # noqa: BLE001 — unreadable now: the next half hour
        db.log(None, "ebay_reconcile_failed", f"{type(e).__name__}: {e}"[:200])
        return out
    by_item: dict[str, list] = {}
    for x in listings:
        if poster.ours(x):
            by_item.setdefault(x.sku, []).append(x)
    for iid, mine in sorted(by_item.items()):
        if db.item(iid) is None:
            continue
        title = crosslist.item_name(db, iid)
        if len(mine) > 1 and _once(db, f"double:{iid}:{','.join(sorted(x.listing_id for x in mine))}"):
            out["double"].append(iid)
            notify.say(f"⚠️ Two live eBay listings for {title}: " + " and ".join(x.url for x in mine)
                       + " — nothing ended.")
        row = db.listing(iid, SITE)
        recorded = row is not None and row["status"] == "posted" and row["url"]
        if recorded or (row is not None and row["status"] in ("sold", "delisted", "skipped")):
            continue                                   # recorded, or ours to leave: a take-down, a sale, a refusal
        live = mine[0]
        runner.confirm_found(s, db, ps, iid, SITE, live.url, "the reconciler: eBay's own listings show it")
        if live.price is not None:
            db.upsert_listing(iid, SITE, price=int(round(float(live.price))))
        out["adopted"].append(iid)
        if _once(db, f"adopted:{iid}:{live.listing_id}"):
            notify.say(f"🔗 eBay: {title} was live but not recorded — recorded now: {live.url}")
    if out["adopted"] or out["double"]:
        db.log(None, "ebay_reconcile", out)
    return out
