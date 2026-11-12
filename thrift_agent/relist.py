"""Cancelled sales relisted everywhere (the owner, 2026-11-11: "from now on, every cancelled sale is relisted
automatically").

A sale cancelled before it shipped — by the site, the buyer or her, or refunded before the parcel left — puts its item
back on every site it came down from, at each site's last price. thrift-api knows the cancellation and hands it over in
`GET /tasks` ("relist": [{sale_id, item_id, marketplace, why}], `sales.fetch_tasks` → `take`); a return after it
shipped is thrift-api's ONE private line, and her "back <words>" makes it a relist like this one.

The plan (`plan`, kv `relists`): each site whose row is `delisted` (our take-down) or `sold` (the site it sold on).
Each site worker does its own on an idle turn (`run_one` — never in the quiet hours, with PAUSE, the lid closed or a
sign-in page: the take-downs' rule), first looking at the site's OWN data, so a listing the site restored by itself
(or still live) is kept, never made twice:
- Poshmark: its public listing — for sale already → kept; else Availability back to For Sale (`set_availability`, the
  edit page's List This Item; the public page decides), the same listing.
- Vinted: our Hide undone — the listing's own page, its "Unhide" pressed once, done when it offers Hide again (an
  already-shown listing is kept).
- Depop: its page — live → kept (Depop may restore a cancelled sale's listing); sold or gone → posted again as a new
  listing through the normal queue (the row back to `queued`), its price-drop clock kept: the row's old `posted_at` is
  put back once the new listing is live.
- eBay: an ended listing → posted again (the poster makes a new listing for an ended one, by its SKU's generation).
Each site back up says ONE group line: "♻️ Back on Depop: <title> — $90" and its address. Never twice (per sale and
site); when every site of the sale is settled the Mac tells thrift-api (`POST /mac-event` {"kind": "relisted"}).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from thrift_agent import crosslist, notify
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads

KV = "relists"                      # {sale_id: {"item", "why", "at", "sites": {site: {state, url, price, posted_at, …}}}}
TRIES = 3                           # a site's relist failed this often: ONE private line, left to her
DUE, WAITING, DONE, KEPT, FAILED = "due", "waiting", "done", "kept", "failed"
SETTLED = (DONE, KEPT, FAILED)
POSTED_AGAIN = ("depop", "ebay")    # a new listing (the old one deleted / sold / ended)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(db: DB) -> dict:
    data = loads(db.kv_get(KV)) or {}
    return data if isinstance(data, dict) else {}


def _save(db: DB, data: dict) -> None:
    db.kv_set(KV, json.dumps(data))


def plan(db: DB, sale_id: str, iid: str, why: str = "cancelled") -> dict | None:
    """The sale's relist: every site whose row came down for it (delisted) or holds it as sold. Once per sale (a
    second call changes nothing). Returns the plan, or None when it was planned before."""
    data = _load(db)
    if sale_id in data:
        return None
    sites = {}
    for r in db.listings_for(iid):
        if r["status"] in ("delisted", "sold") and r["url"]:
            sites[r["marketplace"]] = {"state": DUE, "was": r["status"], "url": r["url"],
                                       "price": r["price_live"] or r["price"], "posted_at": r["posted_at"], "tries": 0}
    entry = {"item": iid, "why": why, "at": _now(), "sites": sites}
    data[sale_id] = entry
    _save(db, data)
    db.log(iid, "relist_planned", {"sale": sale_id, "why": why, "sites": sorted(sites)})
    if not sites:
        _settle(db, sale_id, entry)
    return entry


def take(db: DB, entries: list) -> list[str]:
    """GET /tasks' "relist": each new sale planned. Returns the sales planned now."""
    out = []
    for e in entries or []:
        if isinstance(e, dict) and e.get("sale_id") and e.get("item_id") and plan(db, str(e["sale_id"]),
                                                                                  str(e["item_id"]),
                                                                                  str(e.get("why") or "cancelled")):
            out.append(str(e["sale_id"]))
    return out


def due(db: DB, site: str) -> list[tuple[str, dict]]:
    """(sale, entry) whose `site` still waits — oldest first."""
    return [(sid, e) for sid, e in sorted(_load(db).items(), key=lambda kv: kv[1].get("at") or "")
            if (e.get("sites") or {}).get(site, {}).get("state") in (DUE, WAITING)]


def _title(db: DB, iid: str, site: str) -> str:
    row = db.listing(iid, site)
    fields = (loads(row["fields_json"]) or {}) if row is not None and row["fields_json"] else {}
    if fields.get("title"):
        return str(fields["title"])
    if site == "depop" and fields.get("description"):
        return str(fields["description"]).split("\n", 1)[0]
    it = db.item(iid)
    render = ((loads(it["renders"]) or {}).get("poshmark") or {}) if it is not None else {}
    return str(render.get("title") or crosslist.item_name(db, iid))


def _price(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    return f" — ${v:,.0f}" if abs(v - round(v)) < 0.005 else f" — ${v:,.2f}"


def back_line(db: DB, iid: str, site: str, price, url: str) -> str:
    return f"♻️ Back on {crosslist.LABEL.get(site, site)}: {_title(db, iid, site)}{_price(price)}\n{url}"


def _mark(db: DB, sale_id: str, site: str, **values) -> dict:
    data = _load(db)
    entry = data.get(sale_id) or {}
    entry.setdefault("sites", {}).setdefault(site, {}).update(values)
    data[sale_id] = entry
    _save(db, data)
    return entry


def _back(db: DB, sale_id: str, iid: str, site: str, url: str, price, how: str) -> None:
    """The site is back up: its row posted (its address), ONE group line — never twice."""
    entry = _load(db).get(sale_id) or {}
    st = (entry.get("sites") or {}).get(site) or {}
    if st.get("said"):
        return
    _mark(db, sale_id, site, state=DONE if how != "kept" else KEPT, url=url, said=_now(), how=how)
    notify.group(back_line(db, iid, site, price, url))
    db.log(iid, "relisted", {"sale": sale_id, "site": site, "url": url, "how": how})
    _settle(db, sale_id, _load(db).get(sale_id) or {})


def _settle(db: DB, sale_id: str, entry: dict) -> None:
    """Every site settled: thrift-api told once (never asked for again)."""
    if entry.get("acked") or any(st.get("state") not in SETTLED for st in (entry.get("sites") or {}).values()):
        return
    from thrift_agent import sales
    sent = sales.call(db, "/mac-event", {"kind": "relisted", "sale_id": sale_id,
                                         "sites": sorted((entry.get("sites") or {}))})
    data = _load(db)
    if sale_id in data:
        data[sale_id]["acked"] = _now() if sent else None
        _save(db, data)


def _failed(db: DB, sale_id: str, iid: str, site: str, why: str) -> None:
    entry = _load(db).get(sale_id) or {}
    st = (entry.get("sites") or {}).get(site) or {}
    tries = int(st.get("tries") or 0) + 1
    if tries >= TRIES:
        _mark(db, sale_id, site, state=FAILED, tries=tries, error=why[:300])
        notify.say(f"⚠️ Couldn't put {_title(db, iid, site)} back on {crosslist.LABEL.get(site, site)} after {tries} "
                   f"tries: {why[:200]}. Please relist it by hand there: {st.get('url')}")
        _settle(db, sale_id, _load(db).get(sale_id) or {})
    else:
        _mark(db, sale_id, site, tries=tries, error=why[:300])
    db.log(iid, "relist_failed", {"sale": sale_id, "site": site, "why": why[:300], "tries": tries})


def _requeue(db: DB, sale_id: str, iid: str, site: str) -> None:
    """Posted again as a new listing by the site's normal queue; the old address kept in the plan."""
    db.upsert_listing(iid, site, status="queued", url=None, listing_id=None, error=None, attempts=0)
    _mark(db, sale_id, site, state=WAITING, requeued=_now())
    db.log(iid, "relist_requeued", {"sale": sale_id, "site": site})


def _follow(db: DB, sale_id: str, iid: str, site: str, st: dict) -> bool:
    """A site posted again: once its new listing is live, its old price-drop clock put back and the line said."""
    row = db.listing(iid, site)
    if row is None:
        return False
    if row["status"] == "posted" and row["url"]:
        if st.get("posted_at"):
            db.conn.execute("UPDATE listings SET posted_at=? WHERE item_id=? AND marketplace=?",
                            (st["posted_at"], iid, site))
        _back(db, sale_id, iid, site, row["url"], row["price"] or st.get("price"), "posted again")
        return True
    if row["status"] in ("skipped", "failed") and "set aside" not in str(row["error"] or ""):
        _failed(db, sale_id, iid, site, f"posting it again ended {row['status']}: {row['error'] or ''}")
        return True
    return False


async def run_one(s: Settings, db: DB, site: str, poster, ctx=None, shots: Path | None = None) -> bool:
    """The site's next relist (see the module), by its worker. True when it worked on one."""
    for sale_id, entry in due(db, site):
        iid = entry["item"]
        st = entry["sites"][site]
        if st["state"] == WAITING:
            if _follow(db, sale_id, iid, site, st):
                return True
            continue                               # its new listing is still on its way: the queue posts it
        url, price = st["url"], st.get("price")
        try:
            if site == "poshmark":
                live = await asyncio.to_thread(crosslist.poshmark_listing, url)
                if live and live.get("for_sale") is True:      # for sale on Poshmark already: kept, never twice
                    db.upsert_listing(iid, site, status="posted")
                    _back(db, sale_id, iid, site, url, price, "kept")
                    return True
                ok = await poster.set_availability(ctx, url, True, shots=shots)
                if ok:
                    db.upsert_listing(iid, site, status="posted")
                    _back(db, sale_id, iid, site, url, price, "for sale again")
                else:
                    _failed(db, sale_id, iid, site, "Poshmark's page doesn't show it for sale after the change")
                return True
            if site == "vinted":
                got = await poster.relist(url)
                if got:
                    db.upsert_listing(iid, site, status="posted")
                    _back(db, sale_id, iid, site, url, price, "kept" if got == "shown" else "shown again")
                else:
                    _failed(db, sale_id, iid, site, "Vinted still shows it hidden")
                return True
            if site == "depop":
                state = await poster.depop_state(url)
                if state == "live":                # Depop restored it (or it never came down): kept, never twice
                    db.upsert_listing(iid, site, status="posted")
                    _back(db, sale_id, iid, site, url, price, "kept")
                else:
                    _requeue(db, sale_id, iid, site)
                return True
            if site in POSTED_AGAIN:               # eBay: an ended listing — the poster makes a new one
                _requeue(db, sale_id, iid, site)
                return True
            _failed(db, sale_id, iid, site, f"{site}: no relist known")
            return True
        except Exception as e:  # noqa: BLE001 — a login page and the like: the take-downs' own rules stop the site
            from thrift_agent.post.base import AccountBlocked
            if isinstance(e, AccountBlocked):
                raise
            _failed(db, sale_id, iid, site, f"{type(e).__name__}: {e}")
            return True
    return False


def status(db: DB) -> dict:
    """For doctor and the report: each sale's relist by site."""
    return {sid: {site: st.get("state") for site, st in (e.get("sites") or {}).items()} for sid, e in _load(db).items()}
