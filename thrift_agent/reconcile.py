"""WO53 §7: our records against Poshmark's own data, every 30 minutes (and so before the 9 PM line) — the kind of change
the form checks can't see (they never press Publish).

Live 2026-11-08: three listings went live on Poshmark while our records said "unconfirmed"; had they sold, the sales
were "unmatched" and nothing came down elsewhere. Now the closet, read newest first from its public page state
(postcheck.posh_closet — plain HTTP, nothing clicked), is compared with our records:

- a live listing that is one of our items but isn't recorded — created after our click on Poshmark, our title (as
  Poshmark's slug sees it), our price, and our cover photo — is recorded (runner.confirm_found: posted, the ✅ card, its
  other sites queued) and ONE private line says so;
- two live listings for one item → ONE private line with both links; nothing is ever deleted.

A recorded listing that is gone is the photo check's (its sale net, every 2 hours on Poshmark); Depop's and Vinted's
unrecorded publishes are the verifying re-check's (every 10 minutes, their own data)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from thrift_agent import notify, postcheck
from thrift_agent.db import DB, loads

EVERY_S = 1800
AT = "reconcile_at"               # kv: the last run (epoch seconds)
SAID = "reconcile_said"           # kv: [keys] — each line said once
RECENT = timedelta(days=7)        # only listings created this recently are looked at (the closet's first page)


def due(db: DB, now: float) -> bool:
    return now - float(db.kv_get(AT) or 0) >= EVERY_S


def _said(db: DB, key: str) -> bool:
    said = loads(db.kv_get(SAID)) or []
    if key in said:
        return True
    db.kv_set(SAID, json.dumps([*said, key][-300:]))
    return False


def _recorded_ids(db: DB) -> dict[str, str]:
    """{Poshmark listing id: item} for every listing of ours that carries an address or id."""
    out = {}
    for r in db.conn.execute("SELECT item_id, listing_id, url FROM listings WHERE marketplace='poshmark'"):
        lid = r["listing_id"] or (str(r["url"] or "").rstrip("/").rsplit("-", 1)[-1] if r["url"] else None)
        if lid and postcheck.HEX24.fullmatch(str(lid)):
            out[str(lid)] = r["item_id"]
    return out


def _ours(db: DB) -> list[dict]:
    """Our items with a Poshmark render: id, title, price, cover, the Poshmark row (status, url, when the click was)."""
    out = []
    for it in db.conn.execute("SELECT id, renders, owner_price FROM items WHERE renders IS NOT NULL AND "
                              "COALESCE(test, 0) = 0 AND status NOT IN ('dropped', 'canary')"):
        posh = (loads(it["renders"]) or {}).get("poshmark") or {}
        if not posh.get("title"):
            continue
        row = db.listing(it["id"], "poshmark")
        from thrift_agent import crosslist
        state = crosslist.unconf_state(db, it["id"], "poshmark")
        clicked = None
        if row is not None and (row["status"] == "posting" or state is not None or (
                row["status"] == "failed" and str(row["error"] or "").startswith(crosslist.UNCONFIRMED))):
            clicked = postcheck.clicked_of(state, row)               # a click happened: only then "after our click"
        out.append({"id": it["id"], "title": posh["title"], "price": it["owner_price"] or posh.get("price"),
                    "cover": (posh.get("photos") or [None])[0], "row": row, "clicked": clicked})
    return out


def compare(db: DB, rows: list[dict], now: datetime, base: str = postcheck.POSH) -> dict:
    """The closet's `rows` (posh_closet) against our records: {"record": [(item, url, our cover, their cover)],
    "twins": [(item, url, url)]} — the covers still to be compared (`run`, off the loop's thread)."""
    recorded = _recorded_ids(db)
    ours = _ours(db)
    record, twins = [], []
    for row in rows:
        lid = row.get("id")
        made = postcheck.id_time(lid)
        if not lid or lid in recorded or made is None or now.timestamp() - made > RECENT.total_seconds():
            continue
        url = postcheck.address(lid, row.get("title"), base=base)
        key = postcheck.title_key(row.get("title"))
        for it in ours:
            if postcheck.title_key(it["title"]) != key or not postcheck.same_price(it["price"], row.get("price")):
                continue
            r = it["row"]
            if r is not None and r["status"] == "posted" and r["url"]:
                if r["url"].rstrip("/") != url:
                    twins.append((it["id"], r["url"], url))          # ours twice: said, nothing deleted
                continue
            if it["clicked"] is None or made < it["clicked"].timestamp() - postcheck.ID_SKEW_S:
                continue                                              # made before our click: not ours
            record.append((it["id"], url, it["cover"], row.get("cover")))
    return {"record": record, "twins": twins}


def _cover_check(ours, theirs) -> bool | None:
    check = postcheck.cover_matcher(ours)
    return None if check is None or not theirs else check(theirs)


async def run(s, db: DB, ps: dict, now: datetime | None = None, get=None, cover_check=_cover_check) -> dict:
    """One reconciliation of Poshmark (from the poster's housekeeping). Returns what it did."""
    import asyncio

    from thrift_agent.post import runner
    now = now or datetime.now(timezone.utc)
    db.kv_set(AT, str(now.timestamp()))
    poster = ps.get("poshmark")
    user = getattr(poster, "username", None) or s.get("marketplaces.poshmark.username")
    if not user:
        return {"read": False, "why": "no closet name"}
    closet = await asyncio.to_thread(postcheck.posh_closet, user, get)
    if not closet.get("read"):
        db.log(None, "reconcile", {"read": False, "why": closet.get("why")})
        return {"read": False, "why": closet.get("why")}
    found = compare(db, closet["rows"], now)
    passed: dict[str, list[str]] = {}
    for iid, url, ours, theirs in found["record"]:
        if await asyncio.to_thread(cover_check, ours, theirs) is True:      # the cover must be ours
            passed.setdefault(iid, []).append(url)
    recorded = []
    for iid, urls in passed.items():
        if len(urls) > 1:                                             # two new ones for one item: never a guess
            found["twins"].append((iid, urls[0], urls[1]))
            continue
        runner.confirm_found(s, db, ps, iid, "poshmark", urls[0], "the reconciler: Poshmark's closet shows it")
        recorded.append((iid, urls[0]))
        if not _said(db, f"record:{iid}"):
            notify.say(f"🔗 Poshmark: {_title(db, iid)} was live but not recorded — recorded now: {urls[0]}")
    found["record"] = recorded
    for iid, ours, other in found["twins"]:
        if not _said(db, f"twin:{iid}:{other}"):
            notify.say(f"⚠️ Two live Poshmark listings for {_title(db, iid)}: {ours} and {other} — nothing deleted.")
    db.log(None, "reconcile", {"read": True, "rows": len(closet["rows"]), "recorded": [i for i, _ in found["record"]],
                               "twins": [(i, o) for i, _, o in found["twins"]]})
    return {"read": True, **found}


def _title(db: DB, iid: str) -> str:
    it = db.item(iid)
    return ((loads(it["renders"]) or {}).get("poshmark") or {}).get("title") or iid if it is not None else iid
