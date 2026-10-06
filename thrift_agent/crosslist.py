"""Cross-listing on Depop and Vinted (WO30): once an item is live on Poshmark, the same item at the same approved price
goes up on Depop, then Vinted — no extra questions, every dropdown value from the saved catalogs (thrift_agent.catalogs).

- A `listings` row per marketplace: queued when Poshmark posts (or by `thrift crosslist`), then the poster takes it,
  right after the item's Poshmark listing (30–90 s apart, `crosslist.gap_seconds`), within the hours and
  `marketplaces.<m>.daily_cap` (25 a day each).
- The same two keys as Poshmark (poster.dry_run off + poster.autopublish_confirmed on) AND
  `marketplaces.<m>.autopublish`; otherwise a dry run: the whole form filled, the screenshot and the mapped fields to the
  ops chat, the tab closed without publishing.
- A logged-out / CAPTCHA / verification wall stops that marketplace for the window (one plain line in the group); a
  failure before the publish button is retried next window (3 attempts, then skipped with an ops note); a publish the
  Mac was interrupted in is looked for in that site's shop (WO28), never published again blind.
- The group hears ONE line per item when its marketplaces are done: "Posted ✓ <title> — $X · Poshmark <url> · Depop
  <url> · Vinted <url>" (+ " — check: …"); a marketplace that failed or was skipped is left out (the details: ops)."""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path

from thrift_agent import catalogs, daily, notify
from thrift_agent.config import Settings
from thrift_agent.db import DB, listing_id_from, loads

CROSS = catalogs.CROSS
ORDER = ("poshmark", *CROSS)
LABEL = {"poshmark": "Poshmark", "depop": "Depop", "vinted": "Vinted"}
BLOCKS = "crosslist_blocked"           # kv: {mp: {"window", "reason", "since"}}: stopped for this window
WINDOW_SEEN = "crosslist_window"       # kv: the daily window the poster last saw
STREAK = "crosslist_failures"          # kv: {mp: failures in a row}: N in a row stop that marketplace for the window
MAX_ATTEMPTS = 3                       # a failure before the publish button: retried next window, up to this many
UNCONFIRMED = "unconfirmed publish: "
SKIPPED = "skipped: "
ALERTS = {"depop": "Depop needs you to log in on the Mac.",
          "vinted": "Vinted asks for a check — open it on the Mac."}
ALL_DONE_LINE = "✓ All done — safe to close the Mac."


def enabled(s: Settings) -> list[str]:
    """The cross-list marketplaces that are on and whose catalog loads (a broken catalog turns only that one off)."""
    ok = catalogs.check()
    return [mp for mp in CROSS if s.get(f"marketplaces.{mp}.enabled", False) and ok.get(mp) is None]


def live(s: Settings, mp: str) -> bool:
    """Publishes for real: on the Mac, both poster keys AND the marketplace's own autopublish (WO30 §5)."""
    return bool(s.is_prod and not s.get("poster.dry_run", True) and s.get("poster.autopublish_confirmed", False)
                and s.get(f"marketplaces.{mp}.autopublish", False))


def daily_cap(s: Settings, mp: str) -> int:
    return int(s.get(f"marketplaces.{mp}.daily_cap") or s["schedule"]["daily_cap"])


def gap(s: Settings) -> float:
    lo, hi = s.get("crosslist.gap_seconds") or [30, 90]
    return random.uniform(float(lo), float(hi))


def unconfirmed_ref(iid: str, mp: str) -> str:
    """The outbox ref of an "unconfirmed publish" message: the item, plus the marketplace when it isn't Poshmark."""
    return iid if mp == "poshmark" else f"{iid}:{mp}"


def split_ref(ref: str) -> tuple[str, str]:
    iid, _, mp = ref.partition(":")
    return iid, (mp or "poshmark")


# ---------------------------------------------------------------- the queue

def queue(s: Settings, db: DB, iid: str, mps: list[str] | None = None, why: str = "poshmark posted") -> list[str]:
    """One 'queued' row per cross-list marketplace the item has none on, at the owner's approved price. Returns them."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    out = []
    for mp in mps if mps is not None else enabled(s):
        if db.listing(iid, mp) is None:
            db.upsert_listing(iid, mp, status="queued", price=it["owner_price"])
            out.append(mp)
    if out:
        db.log(iid, "crosslist_queued", {"mps": out, "why": why})
    return out


def window(db: DB) -> str:
    """The current daily window (WO28: the worker begins one at a start or a wake with the lid open)."""
    return db.kv_get(daily.SESSION_KEY) or datetime.now().strftime("%Y-%m-%d")


def new_window(s: Settings, db: DB) -> bool:
    """Between listings: a window the poster hasn't seen lifts the blocks and puts the failures that never reached the
    publish button back in line (MAX_ATTEMPTS each; the last one becomes 'skipped', with an ops note)."""
    w = window(db)
    if db.kv_get(WINDOW_SEEN) == w:
        return False
    db.kv_set(WINDOW_SEEN, w)
    db.kv_set(BLOCKS, "{}")
    db.kv_set(STREAK, "{}")
    for row in db.conn.execute(f"SELECT * FROM listings WHERE status='failed' AND url IS NULL AND marketplace IN "
                               f"({','.join('?' * len(CROSS))})", CROSS).fetchall():
        if (row["error"] or "").startswith(UNCONFIRMED):
            continue                                    # it may be live: the owner settles it (invariant 4)
        iid, mp = row["item_id"], row["marketplace"]
        if row["attempts"] < MAX_ATTEMPTS:
            db.upsert_listing(iid, mp, status="queued")
            db.log(iid, "crosslist_retry", {"mp": mp, "attempts": row["attempts"]})
        else:
            db.upsert_listing(iid, mp, status="skipped", error=SKIPPED + f"{MAX_ATTEMPTS} attempts: {row['error']}")
            notify.say(f"⏭ {LABEL[mp]} skipped for {iid} after {MAX_ATTEMPTS} attempts: {row['error']}\n"
                       f"Fix it, then: thrift requeue {iid} --marketplace {mp}")
    return True


def blocked(db: DB, mp: str) -> str | None:
    b = (loads(db.kv_get(BLOCKS)) or {}).get(mp)
    return b["reason"] if b and b.get("window") == window(db) else None


def block(db: DB, mp: str, reason: str) -> None:
    """Stop `mp` for this window (logged out, CAPTCHA, a verification wall, failures in a row): ONE plain line in the
    group for an account problem, the reason in the ops chat; Poshmark and the other marketplace go on."""
    blocks = loads(db.kv_get(BLOCKS)) or {}
    if (blocks.get(mp) or {}).get("window") == window(db):
        return
    blocks[mp] = {"window": window(db), "reason": reason, "since": datetime.now(timezone.utc).isoformat()}
    db.kv_set(BLOCKS, json.dumps(blocks))
    db.log(None, "crosslist_blocked", {"mp": mp, "reason": reason})
    if not reason.startswith("failures:"):
        notify.group(ALERTS[mp])
    notify.say(f"⛔ {LABEL[mp]} stopped for this window: {reason}")


def failure(db: DB, mp: str, failed: bool, limit: int = 3) -> None:
    """The per-marketplace circuit breaker: `limit` failures in a row stop that marketplace for the window."""
    streak = loads(db.kv_get(STREAK)) or {}
    streak[mp] = streak.get(mp, 0) + 1 if failed else 0
    db.kv_set(STREAK, json.dumps(streak))
    if streak[mp] >= limit:
        block(db, mp, f"failures: {streak[mp]} in a row")


def capped(s: Settings, db: DB, mp: str) -> bool:
    from thrift_agent.scheduler import windows
    _, midnight = windows(tz=s["schedule"]["timezone"])
    return db.listed_since(midnight, mp) >= daily_cap(s, mp)


def pending(s: Settings, db: DB, mps: list[str] | None = None) -> list[tuple[str, str]]:
    """(item, marketplace) the poster takes now, oldest Poshmark listing first: a 'queued' row — and a 'dryrun' one
    once the marketplace publishes for real — on an enabled marketplace that isn't stopped for the window or at its
    daily cap."""
    out = []
    for mp in mps if mps is not None else enabled(s):
        if blocked(db, mp) or capped(s, db, mp):
            continue
        states = ("queued", "dryrun") if live(s, mp) else ("queued",)
        rows = db.conn.execute(
            f"SELECT l.item_id FROM listings l LEFT JOIN listings p ON p.item_id=l.item_id AND p.marketplace='poshmark' "
            f"WHERE l.marketplace=? AND l.status IN ({','.join('?' * len(states))}) "
            f"ORDER BY COALESCE(p.posted_at, l.updated_at), l.item_id", (mp, *states)).fetchall()
        out += [(r["item_id"], mp) for r in rows]
    return out


def next_job(s: Settings, db: DB, prefer: str | None = None) -> tuple[str, str] | None:
    """The next cross-list job: the item just listed first (Poshmark → Depop → Vinted, then the next item), else the
    oldest waiting one."""
    jobs = pending(s, db)
    if not jobs:
        return None
    if prefer is not None:
        mine = [j for j in jobs if j[0] == prefer]
        if mine:
            return min(mine, key=lambda j: ORDER.index(j[1]))
    first = jobs[0][0]
    return min((j for j in jobs if j[0] == first), key=lambda j: ORDER.index(j[1]))


# ---------------------------------------------------------------- recording an outcome

def fields_summary(fields: dict) -> str:
    keys = ("category", "category_path", "size", "condition", "colors", "materials", "skirt_length", "attributes",
            "source", "age", "package_size", "package_sizes", "brand", "price")
    lines = [f"{k}: {fields[k]}" for k in keys if fields.get(k) not in (None, [], {}, "")]
    lines.append(f"photos: {len(fields.get('photos') or [])}")
    return "\n".join(lines)


def record(s: Settings, db: DB, iid: str, mp: str, title: str, out, fields: dict, seconds: float | None = None,
           slept: bool = False) -> None:
    """The listing row, the event, and the messages for one Depop/Vinted Outcome. Nothing here goes to the group but an
    unconfirmed publish's question (the "Posted ✓" line comes from announce())."""
    from thrift_agent.post import runner           # the poster's own wording for an unconfirmed publish
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    error = out.error
    if out.status == "failed" and out.clicked and not out.url:
        error = UNCONFIRMED + (error or "")
    if out.status == "skipped":
        error = SKIPPED + (error or "")
    status = {"cancelled": "queued"}.get(out.status, out.status)
    guesses = [*fields.get("guesses", []), *out.guesses]
    db.upsert_listing(iid, mp, status=status, url=out.url, listing_id=listing_id_from(mp, out.url),
                      error=error if out.status != "cancelled" else out.note,
                      posted_at=stamp if out.status in ("posted", "dryrun") else None,
                      fields_json={**fields, "guesses": guesses, "note": out.note})
    db.log(iid, f"post_{out.status}", {"mp": mp, "url": out.url, "error": error, "shot": out.screenshot,
                                       "note": out.note, "clicked": out.clicked, "guesses": guesses,
                                       "seconds": round(seconds, 1) if seconds else None, "slept": slept or None})
    shot = Path(out.screenshot or "")
    note = f"\n{out.note}" if out.note else ""
    if out.status == "failed" and out.clicked and not out.url:
        runner.ask_unconfirmed(db, iid, mp, runner.unconfirmed_text(title, slept, mp))
    elif out.status == "failed":
        row = db.listing(iid, mp)
        again = "retried next window" if row and row["attempts"] < MAX_ATTEMPTS else "no more attempts: skipped next window"
        notify.ops_photo(shot, f"❌ {LABEL[mp]} failed ({iid}): {title}\n{error}{note}\n({again})")
    elif out.status == "skipped":
        notify.ops_photo(shot, f"⏭ {LABEL[mp]} skipped ({iid}): {title}\n{out.error}\nFix it, then: thrift requeue "
                               f"{iid} --marketplace {mp}{note}")
    elif out.status == "dryrun":
        notify.ops_photo(shot, f"🧪 dry-run {LABEL[mp]} ({iid}): {title} — ${fields.get('price')}\n"
                               f"{fields_summary(fields)}{note}")
    elif out.status == "posted" and out.note:
        notify.ops_photo(shot, f"{LABEL[mp]} posted ({iid}) {out.url}: {out.note}")


def skip_unmappable(db: DB, iid: str, mp: str, title: str, why: str) -> None:
    """A required field the catalog can't take from the facts: this marketplace is skipped for this item (the others
    go on), with the reason in the ops chat."""
    db.upsert_listing(iid, mp, status="skipped", error=SKIPPED + why)
    db.log(iid, "post_skipped", {"mp": mp, "error": why, "mapping": True})
    notify.say(f"⏭ {LABEL[mp]} skipped ({iid}): {title}\ncan't map: {why}")


# ---------------------------------------------------------------- the group's one line per item

def announced(db: DB, iid: str) -> set[str]:
    done: set[str] = set()
    for (detail,) in db.conn.execute("SELECT detail FROM events WHERE ref=? AND kind='posted_announced'", (iid,)):
        done |= set((loads(detail) or {}).get("mps") or [])
    return done


def posted_line(s: Settings, db: DB, iid: str, mps: list[str], done: bool = False) -> str:
    """ "Posted ✓ <title> — $X · Poshmark <url> · Depop <url> · Vinted <url>" (+ " — check: …"; + "✓ All done …")."""
    it = db.item(iid)
    render = ((loads(it["renders"]) or {}).get("poshmark") or {}) if it else {}
    rows = {r["marketplace"]: r for r in db.listings_for(iid)}
    price = (rows.get(mps[0]) or {})["price"] if mps and rows.get(mps[0]) and rows[mps[0]]["price"] else \
        (it["owner_price"] if it else None) or render.get("price")
    checks = []
    for mp in mps:
        f = loads(rows[mp]["fields_json"]) or {}
        for c in [*(f.get("guesses") or []), *(f"copy: {x}" for x in (f.get("checks") or []))]:
            checks.append(c if mp == "poshmark" else f"{LABEL[mp]}: {c}")
    parts = " · ".join(f"{LABEL[mp]} {rows[mp]['url'] or ''}".rstrip() for mp in mps)
    text = f"Posted ✓ {render.get('title') or iid} — ${price} · {parts}"
    text += f" — check: {'; '.join(checks)}" if checks else ""
    return text + (f"\n{ALL_DONE_LINE}" if done else "")


def announce(s: Settings | None, db: DB, iid: str) -> str | None:
    """The group's line for every marketplace of the item posted since its last line. Returns it (None: nothing new)."""
    seen = announced(db, iid)
    mps = [r["marketplace"] for r in db.listings_for(iid) if r["status"] == "posted" and r["marketplace"] not in seen]
    if not mps:
        return None
    text = posted_line(s, db, iid, mps, done=s is not None and daily.all_done(s, db))
    db.log(iid, "posted_announced", {"mps": mps})
    notify.group(text)
    return text


# ---------------------------------------------------------------- backfill: the existing closet

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 "
      "Safari/537.36")


def poshmark_live(url: str) -> bool | None:
    """Is this Poshmark listing still for sale? Its public page: listingDetails.inventory.status "available" → True;
    any other status, or a page that's gone (404 / 410) → False; a page that can't be read → None (unknown)."""
    import httpx

    from thrift_agent.harvest import parse_state
    try:
        r = httpx.get(url, headers={"User-Agent": UA, "Accept": "text/html"}, timeout=20, follow_redirects=True)
    except httpx.HTTPError:
        return None
    if r.status_code in (404, 410):
        return False
    if r.status_code != 200:
        return None
    try:
        details = parse_state(r.text).get("$_listing_details", {}).get("listingDetails", {})
    except ValueError:
        return None
    status = ((details.get("inventory") or {}).get("status") or "").lower()
    return None if not status else status == "available"


def backfill(s: Settings, db: DB, mps: list[str], check: bool = True, dry: bool = False,
             live=poshmark_live) -> list[tuple[str, str]]:
    """`thrift crosslist --backfill`: every item live on Poshmark that isn't on these marketplaces yet, oldest first,
    queued — after its Poshmark page says it is still for sale (sold, removed or unreadable: left out). The poster
    then takes them within each marketplace's daily cap. `dry`: only says what it would queue."""
    out = []
    rows = db.conn.execute("SELECT item_id, url FROM listings WHERE marketplace='poshmark' AND status='posted' "
                           "AND url IS NOT NULL ORDER BY posted_at, item_id").fetchall()
    for row in rows:
        iid = row["item_id"]
        missing = [mp for mp in mps if db.listing(iid, mp) is None]
        if not missing:
            continue
        if check:
            state = live(row["url"])
            if state is None:
                out.append((iid, "couldn't read its Poshmark page: left out"))
                continue
            if not state:
                out.append((iid, "no longer for sale on Poshmark: left out"))
                continue
        if dry:
            out.append((iid, f"would queue {', '.join(missing)}"))
        else:
            queue(s, db, iid, missing, why="backfill")
            out.append((iid, f"queued {', '.join(missing)}"))
    return out
