"""WO59 §5 — the weekly look for a brand Poshmark's list lacked.

The owner (2026-11-10, 11:28 PM): "Post on Poshmark anyway, with no brand. The brand still goes in the title and
description." A listing posted that way (the form's brand left empty, `PoshmarkPoster._brand`'s check line) is looked
at again once a week: Poshmark's create-listing form is opened, the brand searched as a new listing would search it,
and the form left through Discard (`PoshmarkPoster.brand_known` — no listing is touched, so none of the listing's three
brand edits is spent on a look). Found → ONE "brand" edit of the listing (`edits.queue_brand`, Poshmark only) and the
listing is never looked at again: at most ONE of Poshmark's three brand edits is ever used by this. Not found → looked
at again in a week.

Which listings: those the agent itself posted with the brand field empty — the post's own check line says so ("brand
left empty: Poshmark's list has no …", before WO59; "… isn't in Poshmark's brand list …" since) — never one whose brand
she cleared by hand (her edits are never undone). kv `posh_brand_recheck` {item: {"brand", "since", "checked_at",
"edits", "found"}}."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from thrift_agent import notify
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads

KEY = "posh_brand_recheck"
EVERY = timedelta(days=7)
SEEDED = "posh_brand_recheck_seeded"
NOTES = ("isn't in Poshmark's brand list", "Poshmark's list has no")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def entries(db: DB) -> dict:
    return loads(db.kv_get(KEY)) or {}


def _save(db: DB, data: dict) -> None:
    db.kv_set(KEY, json.dumps(data, sort_keys=True))


def add(db: DB, iid: str, brand: str | None, now: datetime | None = None) -> bool:
    """A Poshmark listing of ours posted with the brand field empty: looked at again weekly. False when known already
    or there is no brand to look for."""
    if not brand:
        return False
    data = entries(db)
    if iid in data:
        return False
    data[iid] = {"brand": brand, "since": _iso(now or _now()), "checked_at": None, "edits": 0, "found": None}
    _save(db, data)
    db.log(iid, "brand_recheck_added", {"brand": brand})
    return True


def empty_by_us(guesses) -> bool:
    """Do a post's check lines say the agent left Poshmark's brand field empty for want of the brand on its list?"""
    return any(any(n in str(g) for n in NOTES) for g in (guesses or []))


def seed(db: DB) -> int:
    """Once: every Poshmark listing of ours live with a brand field the agent left empty (its own post events say so).
    Returns the listings added."""
    if db.kv_get(SEEDED):
        return 0
    added = 0
    for row in db.conn.execute("SELECT l.item_id, i.renders FROM listings l JOIN items i ON i.id = l.item_id WHERE "
                               "l.marketplace='poshmark' AND l.status='posted' AND l.url IS NOT NULL").fetchall():
        events = db.conn.execute("SELECT detail FROM events WHERE ref=? AND kind='post_posted'",
                                 (row["item_id"],)).fetchall()
        if not any(any(n in (e["detail"] or "") for n in NOTES) and "poshmark.com" in (e["detail"] or "")
                   for e in events):
            continue
        brand = ((loads(row["renders"]) or {}).get("poshmark") or {}).get("brand")
        added += bool(add(db, row["item_id"], brand))
    db.kv_set(SEEDED, _iso(_now()))
    return added


def due(db: DB, now: datetime | None = None) -> list[str]:
    """The listings to look at now: live on Poshmark, no brand edit made by this yet, not looked at for a week."""
    now = now or _now()
    out = []
    for iid, e in sorted(entries(db).items(), key=lambda kv: kv[1].get("checked_at") or ""):
        if int(e.get("edits") or 0) >= 1:
            continue
        row = db.listing(iid, "poshmark")
        if row is None or row["status"] != "posted" or not row["url"]:
            continue
        last = e.get("checked_at")
        if last:
            try:
                if now - datetime.fromisoformat(last) < EVERY:
                    continue
            except ValueError:
                pass
        out.append(iid)
    return out


async def run_one(s: Settings, db: DB, poster, ctx, dry: bool = False, now: datetime | None = None) -> bool:
    """The next due listing (see the module): the brand looked for on a create-listing form left through Discard;
    found → ONE "brand" edit queued (Poshmark only), never looked at again. True when one was looked at."""
    from thrift_agent import edits
    from thrift_agent.schema import Render
    now = now or _now()
    todo = due(db, now)
    if not todo or dry or not hasattr(poster, "brand_known"):
        return False
    iid = todo[0]
    it = db.item(iid)
    data = entries(db)
    entry = data.get(iid) or {}
    posh = ((loads(it["renders"]) or {}).get("poshmark") or {}) if it is not None else {}
    if not posh.get("brand"):
        entry.update(checked_at=_iso(now), edits=1, found=None)       # no brand any more (hers): never looked again
        data[iid] = entry
        _save(db, data)
        return True
    try:
        found, left = await poster.brand_known(ctx, Render.model_validate(posh))
    except Exception as e:  # noqa: BLE001 — a look that fails is tried again next week, never the worker's end
        db.log(iid, "brand_recheck_failed", f"{type(e).__name__}: {e}"[:200])
        found, left = None, None
    entry["checked_at"] = _iso(now)
    if left:
        notify.say(f"⚠️ Poshmark: looking again for {posh.get('brand')!r} — {left}")
    if found:
        entry.update(edits=1, found=found)
        data[iid] = entry
        _save(db, data)
        rows = edits.queue_brand(s, db, iid, sites=["poshmark"])
        db.log(iid, "brand_recheck_found", {"brand": posh.get("brand"), "found": found, "rows": rows})
        notify.say(f"🔎 Poshmark lists '{found}' now — {posh.get('title') or iid}: its brand field is set on the "
                   "listing (one brand edit).")
        return True
    data[iid] = entry
    _save(db, data)
    db.log(iid, "brand_recheck_none", {"brand": posh.get("brand")})
    return True
