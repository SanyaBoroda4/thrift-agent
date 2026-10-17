"""WO41 — automatic price drops (the owner, 2026-10-17): an item live on all its sites for 30 days and not sold is 10%
cheaper on every site; 30 days later, if it still hasn't sold, 10% again; then never again.

- The clock (`clock`): the moment the item was live on EVERY site it is meant to be on — the last site's posted_at
  (hand-listed rows count with their recorded date; a backfilled item's clock is when it reached all three, not its old
  Poshmark date). A site that never goes live (failed, skipped, a dry run): the clock starts 7 days after the item's
  first live site. A relisted site (posted again after a drop was made there) starts a new clock (`plan`).
- Who drops (`why_not`): live on its sites, not sold anywhere (no take-down, no listing sold / delisted — Hidden and Not
  For Sale are 'delisted'), not excluded by the owner ('no drop'); on the site itself, for sale (Poshmark), not reserved
  or hidden (Vinted), not sold (Depop) — read right before the change.
- The price (`dropped`): per site, from that site's own live price (read before the change — a price she changed by
  hand counts), whole dollars rounded DOWN from 90% ($25 → $22, $95 → $85, $110 → $99): every cut is 10% or more (a
  Poshmark drop of 10% notifies the likers). A site under `price_drop.floor` (off by default) or its own minimum is
  skipped for that stage — never a smaller cut. Nothing else on the listing changes.
- The work (`run_one`, from each site worker on an idle turn, inside the posting hours — take-downs and new listings
  first): one row per item, site and stage; the price read, the edit page filled, the ONE Save, the site's own data
  read until it shows the new price. Unclear → `unconfirmed`: read again before anything else, never a second Save
  without that read. A failure is tried again the next window (6 hours later); 3 → the owner is asked in the private
  chat. A sale that arrives mid-drop (GET /tasks asked once more right before the Save) stops it.
- The group (`announce`): ONE line a day once the day's drops are done — "🔻 Prices down 10% today: … $25 → $22 · …"
  ("(last drop)" on a second drop); nothing on a day without drops."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from thrift_agent import crosslist, notify
from thrift_agent.config import Settings
from thrift_agent.db import DB

STAGES = ("auto_1", "auto_2")            # never a third
AFTER = timedelta(days=30)                # live on all sites → drop 1; drop 1 → drop 2
NEVER_LIVE = timedelta(days=7)            # a site that never goes live: the clock starts this long after the first
RETRY_AFTER = timedelta(hours=6)          # a failed attempt: tried again the next window
RECHECK_AFTER = timedelta(minutes=5)      # saved, the site's data not showing it yet: read again
MAX_ATTEMPTS = 3
PERCENT = 10
SITE_MIN = {"poshmark": 3, "depop": 1, "vinted": 1}    # each site's own lowest price
OPEN = ("pending", "saving", "unconfirmed")
SAID = "price_drop_said"                  # kv: the local day the group's line went out
NO_DROP = re.compile(r"^\s*no[\s-]*(price[\s-]*)?drops?\s*[.!]?\s*$", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds") if t else None


def _t(value) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def dropped(price: float | int | str) -> int:
    """90% of the price in whole dollars, rounded down: every cut is 10% or more."""
    return int((Decimal(str(price)) * Decimal(100 - PERCENT) / Decimal(100)).to_integral_value(rounding=ROUND_FLOOR))


def floor_of(s: Settings | None) -> float | None:
    value = s.get("price_drop.floor") if s is not None else None
    try:
        return float(value) if value not in (None, "", False) else None
    except (TypeError, ValueError):
        return None


def too_low(site: str, new: float, floor: float | None) -> str | None:
    """Why `new` can't be listed on the site (its own minimum, the owner's floor), else None."""
    if new < SITE_MIN.get(site, 1):
        return f"${new:g} is under {crosslist.LABEL.get(site, site)}'s minimum ${SITE_MIN.get(site, 1)}"
    if floor is not None and new < floor:
        return f"${new:g} is under the floor ${floor:g}"
    return None


# ---------------------------------------------------------------- the clock and the stages

def clock(rows, now: datetime | None = None) -> datetime | None:
    """When the item was live on every site it is meant to be on: the last posted_at; with a site not live (yet, or
    never), 7 days after the first live site — None until then."""
    now = now or _now()
    live = [t for r in rows if r["status"] == "posted" and (t := _t(r["posted_at"]))]
    if not live:
        return None
    if all(r["status"] in ("posted", "delisted", "sold") for r in rows):
        return max(live)
    start = min(live) + NEVER_LIVE
    return start if now >= start else None


def why_not(db: DB, it, rows=None) -> str | None:
    """Why the item gets no drop now (None: it may drop) — from our own records; the site is read before the change."""
    if it is None:
        return "no such item"
    rows = rows if rows is not None else db.listings_for(it["id"])
    if it["status"] != "posted":
        return f"the item is {it['status']}"
    if it["no_drop"]:
        return "the owner said no drop"
    if [r for r in rows if r["status"] in ("sold", "delisted")]:
        return "sold or taken down on a site"
    if db.conn.execute("SELECT 1 FROM takedowns WHERE item_id=?", (it["id"],)).fetchone():
        return "sold (a take-down was made)"
    if not [r for r in rows if r["status"] == "posted" and r["url"]]:
        return "not live on any site"
    return None


def _relisted(db: DB, iid: str, rows) -> bool:
    """A site posted again after a drop was made there (a new listing after a delete): a new clock."""
    for r in rows:
        posted = _t(r["posted_at"])
        if r["status"] != "posted" or posted is None:
            continue
        made = db.conn.execute("SELECT MIN(created_at) FROM price_changes WHERE item_id=? AND marketplace=? AND dry=0 "
                               "AND stage IN (?, ?)", (iid, r["marketplace"], *STAGES)).fetchone()[0]
        if made and posted > _t(made):
            return True
    return False


def plan(db: DB, now: datetime | None = None, item: str | None = None, force: bool = False) -> list[str]:
    """Every live item's clock, stage and next date brought up to date; a stage that is due (or `force`d — `thrift
    price-drop --now`) gets one row per live site. Returns 'item:stage' for each stage started."""
    now = now or _now()
    started = []
    sql = "SELECT * FROM items WHERE status='posted'" + (" AND id=?" if item else "")
    for it in db.conn.execute(sql, (item,) if item else ()).fetchall():
        iid, rows = it["id"], db.listings_for(it["id"])
        stage = int(it["drop_stage"] or 0)
        if stage and _relisted(db, iid, rows):
            stage = 0
            db.set_item(iid, drop_stage=0, last_drop_at=None)
            db.log(iid, "price_drop_clock", {"why": "relisted: a new clock"})
        start = clock(rows, now)
        last = _t(it["last_drop_at"])
        blocked = why_not(db, it, rows)
        if stage >= len(STAGES) or start is None or blocked:
            nxt = None
        else:
            nxt = start + AFTER if stage == 0 else (last or start) + AFTER
        _set(db, iid, all_live_at=_iso(start), next_drop_at=_iso(nxt))
        if nxt is None or (now < nxt and not force):
            continue
        name = STAGES[stage]
        live = [r for r in rows if r["status"] == "posted" and r["url"]]
        with db.tx() as c:
            for r in live:
                c.execute("INSERT OR IGNORE INTO price_changes (id, item_id, marketplace, stage, status, due_at, "
                          "created_at, updated_at) VALUES (?,?,?,?,'pending',?,?,?)",
                          (f"pc_{iid}_{r['marketplace']}_{name}", iid, r["marketplace"], name, _iso(nxt), _iso(now),
                           _iso(now)))
            c.execute("UPDATE items SET drop_stage=?, last_drop_at=?, next_drop_at=?, updated_at=? WHERE id=?",
                      (stage + 1, _iso(now), _iso(now + AFTER) if stage + 1 < len(STAGES) else None, _iso(_now()), iid))
        db.log(iid, "price_drop_due", {"stage": name, "sites": [r["marketplace"] for r in live], "forced": force})
        started.append(f"{iid}:{name}")
    return started


def _set(db: DB, iid: str, **values) -> None:
    it = db.item(iid)
    if it is not None and any(it[k] != v for k, v in values.items()):
        db.set_item(iid, **values)                   # updated_at too: the API takes only a newer record


def dry_rows(db: DB, iid: str) -> list[str]:
    """`thrift price-drop --dry-run <item>`: a dry run on each live site — the edit page filled with the next stage's
    price, never saved, screenshots kept. Done by the running poster."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    stamp = _iso(_now())
    out = []
    for r in db.listings_for(iid):
        if r["status"] == "posted" and r["url"]:
            rid = f"pcdry_{iid}_{r['marketplace']}_{_now():%Y%m%d%H%M%S}"
            db.conn.execute("INSERT INTO price_changes (id, item_id, marketplace, stage, status, dry, created_at, "
                            "updated_at) VALUES (?,?,?,'dry','pending',1,?,?)", (rid, iid, r["marketplace"], stamp, stamp))
            out.append(r["marketplace"])
    return out


def exclude(db: DB, iid: str) -> int:
    """The owner's 'no drop': this item never drops; its open drops are called off. Returns how many."""
    db.set_item(iid, no_drop=_iso(_now()), next_drop_at=None)
    return cancel(db, iid, "the owner said no drop")


def cancel(db: DB, iid: str, why: str) -> int:
    """The item's open drops called off (sold, excluded): never saved after this."""
    cur = db.conn.execute(f"UPDATE price_changes SET status='skipped', error=?, updated_at=? WHERE item_id=? AND "
                          f"status IN ({','.join('?' * len(OPEN))})", (why, _iso(_now()), iid, *OPEN))
    if cur.rowcount:
        db.log(iid, "price_drop_cancelled", {"why": why, "rows": cur.rowcount})
    return cur.rowcount


# ---------------------------------------------------------------- one site's next drop

def due_rows(db: DB, site: str, now: datetime | None = None) -> list:
    now = now or _now()
    rows = db.conn.execute(f"SELECT * FROM price_changes WHERE marketplace=? AND status IN ({','.join('?' * len(OPEN))}) "
                           "ORDER BY dry DESC, created_at", (site, *OPEN)).fetchall()
    return [r for r in rows if (_t(r["retry_at"]) or now) <= now]


def may_drop(s: Settings, db: DB, site: str, dry: bool) -> bool:
    """Inside the posting hours, the poster LIVE, never with PAUSE, the lid closed or the site stopped for the window."""
    from thrift_agent import power
    from thrift_agent.post import runner
    from thrift_agent.scheduler import can_post
    if dry or not s.get("price_drop.enabled", True) or s.flag_set("PAUSE") or runner._paused(db):
        return False
    if power.lid_closed() is True or crosslist.blocked(db, site):
        return False
    return can_post(s, 0, 0)[0]


async def run_one(s: Settings, db: DB, site: str, poster, ctx, sites: list[str] | None = None,
                  dry: bool = False) -> bool:
    """The site's next due drop (or the owner's dry run), if any. True when it worked on one."""
    if not may_drop(s, db, site, dry):
        return False
    rows = due_rows(db, site)
    if not rows:
        return False
    await apply(s, db, rows[0], poster, ctx, sites or [site])
    return True


def _update(db: DB, rid: str, **values) -> None:
    values["updated_at"] = _iso(_now())
    db.conn.execute(f"UPDATE price_changes SET {', '.join(f'{k}=?' for k in values)} WHERE id=?", (*values.values(), rid))


def _done(db: DB, row, new: float, note: str, stage: str | None = None) -> None:
    old = row["old_price"]
    pct = round(100 * (1 - new / old), 1) if old else None
    _update(db, row["id"], status="done", new_price=new, pct=pct, done_at=_iso(_now()), error=None, evidence=note,
            **({"stage": stage} if stage else {}))
    db.upsert_listing(row["item_id"], row["marketplace"], price_live=new, price_read_at=_iso(_now()))
    db.log(row["item_id"], "price_dropped", {"mp": row["marketplace"], "stage": stage or row["stage"], "old": old,
                                             "new": new, "note": note})


def _skip(db: DB, row, why: str) -> None:
    _update(db, row["id"], status="skipped", error=why)
    db.log(row["item_id"], "price_drop_skipped", {"mp": row["marketplace"], "stage": row["stage"], "why": why})


def _failed(db: DB, row, why: str, title: str) -> None:
    attempts = int(row["attempts"] or 0) + 1
    final = attempts >= MAX_ATTEMPTS
    _update(db, row["id"], attempts=attempts, error=why[:500], status="failed" if final else "pending",
            retry_at=None if final else _iso(_now() + RETRY_AFTER))
    db.log(row["item_id"], "price_drop_failed", {"mp": row["marketplace"], "stage": row["stage"], "why": why[:300],
                                                 "attempts": attempts})
    if final:
        old, new = row["old_price"], row["new_price"]
        prices = f": ${old:g} → ${new:g}" if old and new else ""
        notify.say(f"Price drop on {crosslist.LABEL.get(row['marketplace'], row['marketplace'])} failed for {title}"
                   f"{prices}. Please change it by hand.")


def _read_line(db: DB, row, read: dict) -> None:
    if read.get("price") is not None:
        db.upsert_listing(row["item_id"], row["marketplace"], price_live=read["price"], price_read_at=_iso(_now()))


async def apply(s: Settings, db: DB, row, poster, ctx, sites: list[str]) -> str:
    """One row: the site's live price read, the new price worked out, the edit page filled and saved ONCE, the site's
    data read until it shows the new price. Returns the row's new status."""
    from thrift_agent import sales
    iid, site = row["item_id"], row["marketplace"]
    it = db.item(iid)
    title = crosslist.item_name(db, iid) if it is not None else iid
    listing = db.listing(iid, site)
    dry = bool(row["dry"])
    if not dry and (why := why_not(db, it)):
        _skip(db, row, why)
        return "skipped"
    if listing is None or listing["status"] != "posted" or not listing["url"]:
        _skip(db, row, "not live on this site")
        return "skipped"
    url = listing["url"]
    from thrift_agent.post.base import AccountBlocked
    try:
        read = await poster.read_price(ctx, url)
    except AccountBlocked:
        raise                                       # a login / CAPTCHA page: the site stops for the window
    except Exception as e:  # noqa: BLE001 — nothing was changed
        read = {"error": f"{type(e).__name__}: {e}"}
    _read_line(db, row, read)
    live = read.get("price")
    if read.get("error") or live is None:
        if row["status"] in ("saving", "unconfirmed"):     # saved before: never a second Save without a read
            _update(db, row["id"], status="unconfirmed", retry_at=_iso(_now() + RECHECK_AFTER),
                    attempts=int(row["attempts"] or 0) + 1)
            if int(row["attempts"] or 0) + 1 >= MAX_ATTEMPTS:
                _failed(db, db.conn.execute("SELECT * FROM price_changes WHERE id=?", (row["id"],)).fetchone(),
                        "saved, but the site's price couldn't be read", title)
            return "unconfirmed"
        _failed(db, row, f"couldn't read the price: {read.get('error') or 'no price shown'}", title)
        return "pending"
    if read.get("unavailable"):
        _skip(db, row, f"not for sale on the site ({read['unavailable']})")
        return "skipped"
    old = row["old_price"]
    if row["status"] in ("saving", "unconfirmed"):
        if row["new_price"] is not None and live == row["new_price"]:
            _done(db, row, live, "the site's data shows the new price")
            return "done"
        if old is not None and live < old:
            _done(db, row, live, f"changed by hand to ${live:g}", stage=None)
            return "done"
        # the old price still: the Save didn't take — one attempt counted, tried again the next window
        _failed(db, _fresh(db, row["id"]), "saved, but the site still shows the old price", title)
        return _fresh(db, row["id"])["status"]
    if old is not None and live < old and not dry:
        _done(db, row, live, f"changed by hand to ${live:g}")
        return "done"
    old = live
    new = row["new_price"] if row["new_price"] is not None and row["old_price"] == live else dropped(live)
    if (low := too_low(site, new, floor_of(s))) and not dry:
        _update(db, row["id"], old_price=old, new_price=new)
        _skip(db, row, low)
        return "skipped"
    _update(db, row["id"], old_price=old, new_price=new, pct=round(100 * (1 - new / old), 1) if old else None)
    row = _fresh(db, row["id"])

    def go() -> bool:
        """Right before the one Save: a sale that arrived meanwhile stops it; else the row is 'saving'."""
        try:
            sales.fetch_tasks(db, sites, force=True)
        except Exception:  # noqa: BLE001 — the API away: our own records decide
            pass
        if why := why_not(db, db.item(iid)):
            _skip(db, row, f"stopped before Save: {why}")
            return False
        _update(db, row["id"], status="saving")
        return True

    shots = s.path("failed") / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    try:
        out = await poster.set_price(ctx, url, old, new, dry=dry, shots=shots, go=go, fields=_fields(listing))
    except Exception as e:  # noqa: BLE001
        if isinstance(e, AccountBlocked):
            if _fresh(db, row["id"])["status"] == "saving":
                _update(db, row["id"], status="unconfirmed", retry_at=_iso(_now()))
            raise
        out = {"saved": _fresh(db, row["id"])["status"] == "saving", "error": f"{type(e).__name__}: {e}"}
    row = _fresh(db, row["id"])
    if row["status"] == "skipped":
        return "skipped"
    evidence = str(out.get("shot") or "")
    if dry:
        _update(db, row["id"], status="done" if out.get("ok") else "failed", done_at=_iso(_now()),
                error=out.get("error"), evidence=evidence)
        notify.say(f"Price drop dry run on {crosslist.LABEL.get(site, site)} — {title}: ${old:g} → ${new:g} "
                   + ("filled in on the edit page, not saved" if out.get("ok") else f"not filled: {out.get('error')}")
                   + (f" · {Path(evidence).name}" if evidence else ""))
        return "done"
    if out.get("mismatch") is not None:              # the edit page showed another price: read again next turn
        _update(db, row["id"], status="pending", old_price=None, new_price=None, evidence=evidence,
                error=f"the edit page showed ${out['mismatch']} — read again")
        return "pending"
    if not out.get("saved"):
        _failed(db, row, out.get("error") or "not saved", title)
        return "pending"
    after = out.get("live_price")
    if after is not None and float(after) == float(new):
        _done(db, row, new, f"saved; the site shows ${new:g}")
        _update(db, row["id"], evidence=evidence)
        return "done"
    _update(db, row["id"], status="unconfirmed", retry_at=_iso(_now() + RECHECK_AFTER), evidence=evidence,
            error=out.get("error") or f"saved; the site showed {after if after is not None else 'no price'} yet")
    return "unconfirmed"


def _fresh(db: DB, rid: str):
    return db.conn.execute("SELECT * FROM price_changes WHERE id=?", (rid,)).fetchone()


def _fields(listing) -> dict:
    """The fields the listing was posted with (Depop's blank ones are filled from them), its item id as the SKU (the
    evidence is named by it)."""
    from thrift_agent.db import loads
    fields = (loads(listing["fields_json"]) or {}) if listing["fields_json"] else {}
    return {**fields, "sku": fields.get("sku") or listing["item_id"]}


# ---------------------------------------------------------------- the group's line, once a day

def _today(s: Settings) -> str:
    tz = s.get("schedule.timezone") or "America/New_York"
    return datetime.now(ZoneInfo(tz)).date().isoformat()


def short_name(db: DB, iid: str) -> str:
    """The item's name as on its group card ("Levi's 501 Cutoff Shorts"): the copy's short name, else the title cut."""
    from thrift_agent.brain import copy as copywriter
    from thrift_agent.db import loads
    from thrift_agent.schema import Facts
    it = db.item(iid)
    render = ((loads(it["renders"]) or {}).get("poshmark") or {}) if it else {}
    try:
        facts = Facts.model_validate(loads(it["facts"])) if it and it["facts"] else None
    except Exception:  # noqa: BLE001 — the title alone
        facts = None
    return copywriter.short_name(render.get("short_name"), render.get("title") or iid, facts)


def announce(s: Settings, db: DB, now: datetime | None = None) -> str | None:
    """ONE group line a day, once the day's drops are done (none still to try now); nothing on a day without drops."""
    now = now or _now()
    today = _today(s)
    if db.kv_get(SAID) == today:
        return None
    if [r for r in db.conn.execute(f"SELECT * FROM price_changes WHERE dry=0 AND status IN "
                                   f"({','.join('?' * len(OPEN))})", OPEN).fetchall()
            if (_t(r["retry_at"]) or now) <= now]:
        return None
    rows = db.conn.execute("SELECT * FROM price_changes WHERE dry=0 AND status='done' AND announced_at IS NULL "
                           "ORDER BY item_id, marketplace").fetchall()
    if not rows:
        return None
    parts, seen = [], []
    order = {r["item_id"]: tuple(db.conn.execute("SELECT created_at, rowid FROM items WHERE id=?",
                                                 (r["item_id"],)).fetchone() or ("", 0)) for r in rows}
    for iid in sorted(order, key=order.get):                 # the oldest item first
        mine = [r for r in rows if r["item_id"] == iid]
        pairs = {(r["old_price"], r["new_price"]) for r in mine}
        if len(pairs) == 1:
            old, new = next(iter(pairs))
            prices = f"${old:g} → ${new:g}"
        else:
            prices = ", ".join(f"{crosslist.LABEL.get(r['marketplace'], r['marketplace'])} ${r['old_price']:g} → "
                               f"${r['new_price']:g}" for r in mine)
        last = " (last drop)" if any(r["stage"] == STAGES[-1] for r in mine) else ""
        parts.append(f"{short_name(db, iid)} {prices}{last}")
        seen.extend(r["id"] for r in mine)
    text = f"🔻 Prices down {PERCENT}% today: " + " · ".join(parts) + "."
    notify.group(text)
    with db.tx() as c:
        c.executemany("UPDATE price_changes SET announced_at=? WHERE id=?", [(_iso(now), rid) for rid in seen])
    db.kv_set(SAID, today)
    db.log(None, "price_drop_said", {"text": text})
    return text


# ---------------------------------------------------------------- what is due, for the owner

def listing_text(db: DB, now: datetime | None = None) -> list[str]:
    """`thrift price-drop --list`: each live item's clock, stage and next drop, the soonest first."""
    now = now or _now()
    out = []
    for it in db.conn.execute("SELECT * FROM items WHERE status='posted' ORDER BY next_drop_at IS NULL, next_drop_at"):
        stage = int(it["drop_stage"] or 0)
        why = why_not(db, it)
        nxt = _t(it["next_drop_at"])
        when = (f"next drop {nxt:%Y-%m-%d}" + (" (due now)" if nxt <= now else f" (in {(nxt - now).days} days)")
                if nxt else ("no more drops" if stage >= len(STAGES) else f"no drop: {why or 'not live on all sites yet'}"))
        since = _t(it["all_live_at"])
        out.append(f"{it['id']} {crosslist.item_name(db, it['id'])} — drops done {stage}/2"
                   + (f", live on all since {since:%Y-%m-%d}" if since else "") + f" — {when}")
    return out
