"""The sites' own order statuses (WO38). None of the three sites emails the seller when an order ships: the Mac reads
each site's sold orders (read only, a few times a day) and sends them here, `POST /orders`
{"marketplace", "swept_at", "orders": [{"order_id", "status", "status_text", "title", "price", "sold_at",
"listing_id", "listing_url", "sku", "shipped_at", "delivered_at", "completed_at"}]} — no buyer data: the Mac never
sends any and nothing here would keep it.

Each order is kept in `orders` (marketplace, order id, its sale and item, the site's status and its words, and the
first time each status was seen) and becomes the same events the emails give, keyed by the site's order id
("mac:order:<site>:<order id>:<KIND>", so twice is once, like an email processed twice): SHIPPED, DELIVERED and
COMPLETED are recorded QUIETLY (no message: the owner shipped it, she knows); CANCELLED is said as a cancellation
email's is. An order our records lack (its sale email was missed) is recorded once as a SALE by its order id
("mac:order:<site>:<order id>") — the usual path: take-downs elsewhere, one 💰 line — when it is recent and not a
finished one from before going live (those are history, left alone); a cancelled one never.

A sale is found by the order id, then the item (our SKU, the listing), then the title (and the sale within 3 days of
the order), then — Depop, whose emails name no order — the one sale on that site without an order id sold within 3
days (whatever its state: the delivered email may have closed it). When more than one could be it, the order is tied
to none and nothing new is recorded: never a guess, never a second row of one sale — and ONE private line names them.

WO65 (live 2026-11-11, the 9 PM list showed shipped and cancelled sales): Depop's receipts carry no title, and three
Depop sales without an order id sold within 3 days of each other, so none of the three orders was tied — a title-less
order now goes to the one candidate sold within CLOSE (30 minutes) of it (the receipt and its sale email were 8–12
seconds apart). A sale recorded twice — Vinted's "You sold" email, and its label email six days later (beyond the
email pair's 3 days), the order tied to the label's row — is folded together once the order is tied: the twin (same
site, same title, no order id, sold within CLOSE of the ORDER) is kept as 'merged' (`fold_twin`)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from . import core, match, util
from .db import Database
from .util import BadRequest, iso, parse_time

STATUSES = ("to_ship", "awaiting_scan", "shipped", "delivered", "completed", "cancelled", "returned")
# WO66: "awaiting_scan" — the seller's label is printed and the parcel handed over, no carrier scan yet (Poshmark's
# unit "seller_shipped" with "Shipping Label Created"): not to ship (off the 9 PM list's to-ship groups, no reminder),
# not on its way either (no SHIPPED, the sale stays open) — the first scan moves it on
WAITING = "awaiting_scan"
ON_ITS_WAY = ("shipped", "delivered", "completed")             # the site shows it left: no shipping reminder
EVENTS = {"shipped": "SHIPPED", "delivered": "DELIVERED", "completed": "COMPLETED", "cancelled": "CANCELLED"}
PREFIX = "mac:order:"
MAX_ORDERS = 200
RECENT = timedelta(days=45)        # an order our records lack is recorded only if sold this recently
NEAR = timedelta(days=3)           # a sale and an order this close in time can be the same one
CLOSE = timedelta(minutes=30)      # WO65: a sale and an order this close are the same one when nothing else is
DOUBT = "order_doubt:"             # settings: an order that could be several sales, said once
FIELDS = ("order_id", "status", "status_text", "title", "price", "sold_at", "listing_id", "listing_url", "sku",
          "shipped_at", "delivered_at", "completed_at",
          "ship_by",                # WO57: the day the site itself says to ship by ("YYYY-MM-DD"; eBay's orders give it)
          "label_at")               # WO66: when the site's label was made (an "awaiting_scan" order)


def report(db: Database, body: dict, now: datetime | None = None, mode: str | None = None) -> dict:
    """One site's sweep: every order recorded and acted on (see the module); the site's last sweep in `heartbeats`
    ("orders:<site>"). {"marketplace", "orders", "results": [{order_id, status, sale_id, action}]}."""
    now, mode = core._clock(now), mode or util.mode()
    if not isinstance(body, dict):
        raise BadRequest("the body must be a JSON object")
    mp = core._text(body.get("marketplace")).strip().lower()
    if mp not in core._sites(db):
        raise BadRequest("marketplace is poshmark, depop or vinted")
    raw = body.get("orders")
    if not isinstance(raw, list) or len(raw) > MAX_ORDERS:
        raise BadRequest(f"orders is a list of at most {MAX_ORDERS}")
    seen_at = min(parse_time(body.get("swept_at")) or now, now)
    results = sweep(db, mp, raw, seen_at, now, mode)
    return {"marketplace": mp, "orders": len(results), "results": results}


def sweep(db: Database, mp: str, raw: list, seen_at: datetime, now: datetime, mode: str) -> list[dict]:
    """The orders of one sweep, each recorded and acted on, and the site's last sweep kept — the Mac's report of a
    site's sold orders, and (WO57) thrift-api's own read of eBay's (ebay.read_orders). [results]"""
    results = []
    for entry in raw:
        order = clean(entry)
        if order is None:
            results.append({"order_id": None, "action": "skipped: no order id or no known status"})
            continue
        results.append(one(db, mp, order, seen_at, now, mode))
    core.heartbeat(db, f"orders:{mp}", {"orders": len(results), "at": iso(seen_at)}, now, mode)
    return results


def clean(entry: object) -> dict | None:
    """The fields of one order the Mac sent, as text / number / time; None without an order id or a known status."""
    if not isinstance(entry, dict):
        return None
    order = {k: entry.get(k) for k in FIELDS}
    order["order_id"] = core._text(order["order_id"]).strip()[:64]
    order["status"] = core._text(order["status"]).strip().lower()
    if not order["order_id"] or order["status"] not in STATUSES:
        return None
    for key in ("status_text", "title", "listing_id", "listing_url", "sku"):
        order[key] = core._text(order[key]).strip()[:300] or None
    order["price"] = util.number(order["price"])
    for key in ("sold_at", "shipped_at", "delivered_at", "completed_at", "label_at"):
        order[key] = parse_time(order[key])
    try:
        order["ship_by"] = date.fromisoformat(str(order["ship_by"])[:10]) if order["ship_by"] else None
    except ValueError:
        order["ship_by"] = None
    return order


def one(db: Database, mp: str, order: dict, seen_at: datetime, now: datetime, mode: str) -> dict:
    """One order recorded and acted on; its messages go once the transaction has committed."""
    out = core.Outbox(now, mode)
    try:
        return _one(db, mp, order, seen_at, now, mode, out)
    finally:
        out.flush()


def _one(db: Database, mp: str, order: dict, seen_at: datetime, now: datetime, mode: str, out: core.Outbox) -> dict:
    with db.tx():
        sale, doubt = find_sale(db, mp, order)
        action = "linked"
        if sale is None and doubt:
            keep(db, mp, order, None, seen_at)
            say_doubt(db, mp, order, doubt if isinstance(doubt, list) else [], out)
            return {"order_id": order["order_id"], "status": order["status"], "sale_id": None,
                    "action": "left alone (it could be one of several sales)"}
        if sale is None:
            if not qualifies(db, order, now):
                keep(db, mp, order, None, seen_at)
                return {"order_id": order["order_id"], "status": order["status"], "sale_id": None,
                        "action": "left alone (finished before going live, or cancelled)"}
            sale = record_sale(db, mp, order, now, mode, out)
            action = "recorded"
        elif sale["order_id"] in (None, "") and not db.one(
                "SELECT id FROM sales WHERE marketplace = ? AND order_id = ? AND id <> ?",
                (mp, order["order_id"], sale["id"])):
            db.execute("UPDATE sales SET order_id = ? WHERE id = ?", (order["order_id"], sale["id"]))
        if action == "linked" and order["ship_by"] is not None:       # WO57: the site's own ship-by day wins
            core._fill_sale(db, sale, {"ship_by_stated": order["ship_by"]})
        keep(db, mp, order, sale, seen_at)
        if twin := fold_twin(db, mp, order, sale):
            action += f", its twin {twin} folded in"
        changed = transition(db, mp, order, sale, seen_at, now, mode, out)
        if sale.get("item_id") and order["status"] != "cancelled":
            # WO40: a sold order the site shows — the item must be down everywhere else: the take-downs it lacks made,
            # one left to the owner handed to the Mac once more
            if made := core.ensure_takedowns(db, sale["item_id"], now, mode, reopen=True):
                changed = ", ".join(filter(None, [changed, "take-downs: " + ", ".join(t["marketplace"] for t in made)]))
    out.flush()
    core._ebay_kick(db, now, mode)            # WO57: an eBay take-down made here is done by thrift-api itself, at once
    return {"order_id": order["order_id"], "status": order["status"], "sale_id": sale["id"],
            "action": f"{action}{', ' + changed if changed else ''}"}


def find_sale(db: Database, mp: str, order: dict) -> tuple[dict | None, bool | list]:
    """(the sale this order is, False) — see the module; (None, the sales it could be — truthy) when more than one
    could be it; (None, False) when none is."""
    row = db.one("SELECT * FROM sales WHERE marketplace = ? AND order_id = ? AND status <> 'merged' "
                 "ORDER BY created_at LIMIT 1", (mp, order["order_id"]))
    if row:
        return row, False
    item = match.item_for_sku(db, order["sku"]) or match.item_for_listing(db, mp, order["listing_id"],
                                                                           order["listing_url"])
    if item:
        rows = [r for r in db.query("SELECT * FROM sales WHERE marketplace = ? AND item_id = ? AND status <> 'merged' "
                                    "ORDER BY sold_at DESC", (mp, item)) if _near(r, order) and _free(r, order)]
        if len(rows) == 1:
            return rows[0], False
        if len(rows) > 1:
            return None, _doubts(rows)
    if mp == util.EBAY:
        return None, False        # WO57: an eBay order is tied by its id, the SKU or the listing id only — never a title
    rows = [r for r in db.query("SELECT s.*, i.title AS item_title FROM sales s LEFT JOIN items i ON i.id = s.item_id "
                                "WHERE s.marketplace = ? AND s.status <> 'merged'", (mp,))
            if _near(r, order) and _free(r, order)]
    if order["title"]:
        candidates = [(r["id"], text) for r in rows for text in (r["title_seen"], r["item_title"]) if text]
        sale_id, ambiguous = match.best(order["title"], candidates)
        if sale_id and not ambiguous:
            return {k: v for k, v in next(r for r in rows if r["id"] == sale_id).items() if k != "item_title"}, False
        if ambiguous:                    # WO65: two of the same title is never a guess, however close in time
            return None, _doubts([r for r in rows if any(match.best(order["title"], [(r["id"], t)])[0]
                                                         for t in (r["title_seen"], r["item_title"]) if t)] or rows)
    # Depop's emails name no order: the one Depop sale without an order id sold within NEAR of it, whatever its state;
    # WO65: of several, the one sold within CLOSE of the order (a receipt and its email: seconds apart)
    if mp == "depop" and order["sold_at"] is not None:
        loose = [r for r in rows if r["status"] != "cancelled" and r["order_id"] in (None, "")]
        if len(loose) == 1:
            return {k: v for k, v in loose[0].items() if k != "item_title"}, False
        if len(loose) > 1:
            close = [r for r in loose if _close(r, order)]
            if len(close) == 1:
                return {k: v for k, v in close[0].items() if k != "item_title"}, False
            return None, _doubts(close or loose)
    return None, False


def _close(sale: dict, order: dict, within: timedelta = CLOSE) -> bool:
    a, b = parse_time(sale["sold_at"]), order["sold_at"]
    return a is not None and b is not None and abs(a - b) <= within


def _doubts(rows: list[dict]) -> list[dict]:
    """The sales an order could be (truthy: the order is left untied), for the one private line."""
    return [{k: r.get(k) for k in ("id", "title_seen", "item_title", "sold_at")} for r in rows] or [{"id": "?"}]


def say_doubt(db: Database, mp: str, order: dict, rows: list[dict], out: core.Outbox) -> None:
    """WO65: ONE private line per order that could be several sales — naming them; it stays untied (never a guess)."""
    key = f"{DOUBT}{mp}:{order['order_id']}"
    if core.setting(db, key):
        return
    core.put_setting(db, key, iso(order["sold_at"]) if order["sold_at"] else "1")
    names = "; ".join(f"“{r.get('item_title') or r.get('title_seen') or r.get('id')}” ({r.get('id')}, sold "
                      f"{core.local_text(parse_time(r.get('sold_at'))) if parse_time(r.get('sold_at')) else '?'})"
                      for r in rows[:4])
    words = order["title"] or "no title"
    out.ops(f"🔎 {util.site_name(mp)} order {order['order_id']} ({words}, {order['status_text'] or order['status']}) "
            f"could be any of these sales: {names} — left untied, never a guess. Her “shipped <words>” settles one.")


def fold_twin(db: Database, mp: str, order: dict, sale: dict) -> str | None:
    """WO65: the same sale recorded twice — its twin (same site, same title, no order id, not merged or cancelled, sold
    within CLOSE of the order) folded into `sale`, the order's: kept as 'merged' (never deleted), its price and earlier
    sale time carried over. Returns the twin's id, or None."""
    if order["sold_at"] is None:
        return None
    title = sale.get("title_seen") or order["title"]
    rows = db.query("SELECT * FROM sales WHERE marketplace = ? AND id <> ? AND status NOT IN ('merged', 'cancelled') "
                    "AND (order_id IS NULL OR order_id = '')", (mp, sale["id"]))
    twins = [r for r in rows if _close(r, order) and title and r["title_seen"]
             and match.best(title, [(r["id"], r["title_seen"])])[0]
             and (r["item_id"] in (None, "") or r["item_id"] == sale.get("item_id"))]
    if len(twins) != 1:
        return None
    twin = twins[0]
    updates = {}
    if sale.get("price") in (None, "") and twin["price"] not in (None, ""):
        updates["price"] = twin["price"]
    if (t1 := parse_time(twin["sold_at"])) and (t0 := parse_time(sale.get("sold_at"))) and t1 < t0:
        updates["sold_at"] = twin["sold_at"]
    if updates:
        sets = ", ".join(f"{column} = ?" for column in updates)          # the columns above, nothing from outside
        db.execute(f"UPDATE sales SET {sets} WHERE id = ?", (*updates.values(), sale["id"]))
    db.execute("UPDATE sales SET status = 'merged' WHERE id = ?", (twin["id"],))
    return twin["id"]


def _near(sale: dict, order: dict) -> bool:
    """Sold within NEAR of each other (either date unknown: can't tell, so yes)."""
    a, b = parse_time(sale["sold_at"]), order["sold_at"]
    return a is None or b is None or abs(a - b) <= NEAR


def _free(sale: dict, order: dict) -> bool:
    """The sale holds no other order id."""
    return sale["order_id"] in (None, "") or sale["order_id"] == order["order_id"]


def qualifies(db: Database, order: dict, now: datetime) -> bool:
    """An order our records lack is recorded when it is recent and not cancelled, and — if it was sold before going
    live — still on its way to the buyer (finished history is left alone)."""
    sold = order["sold_at"]
    if order["status"] == "cancelled" or sold is None or now - sold > RECENT:
        return False
    live_since = parse_time(core.setting(db, "go_live_at"))
    return (live_since is not None and sold >= live_since) or order["status"] in ("to_ship", WAITING, "shipped")


def record_sale(db: Database, mp: str, order: dict, now: datetime, mode: str, out: core.Outbox) -> dict:
    """The missed SALE, once by its order id, through the same path as a sale email (take-downs, the 💰 line)."""
    message_id = f"{PREFIX}{mp}:{order['order_id']}"
    facts = {"title": order["title"], "listing_url": order["listing_url"], "listing_id": order["listing_id"],
             "sku": order["sku"], "price": order["price"], "order_id": order["order_id"],
             "sold_at": iso(order["sold_at"])}
    event = {"message_id": message_id, "thread_id": None, "marketplace": mp, "kind": "SALE",
             "subject": "found on the site by the order sweep", "received_at": iso(order["sold_at"]),
             "parsed_json": json.dumps(facts), "raw_text": None}
    if core._insert_event(db, event, "new", now):
        received = order["sold_at"]
        parsed = {**facts, "sold_at": received, "ship_by_stated": order.get("ship_by")}
        result = core._sale(db, mp, parsed, message_id, event["subject"], core._handling(db, mode, received, now), now,
                            out)
        db.execute("UPDATE email_events SET status = ?, attempts = 1 WHERE message_id = ?", (result["status"], message_id))
    return db.one("SELECT * FROM sales WHERE marketplace = ? AND order_id = ? AND status <> 'merged' "
                  "ORDER BY created_at LIMIT 1", (mp, order["order_id"]))


def transition(db: Database, mp: str, order: dict, sale: dict, seen_at: datetime, now: datetime, mode: str,
               out: core.Outbox) -> str | None:
    """The sale moved to what the site shows, through the emails' events (once each, by the order id); the words of
    what changed, or None."""
    status = order["status"]
    kinds = {"shipped": ["SHIPPED"], "delivered": ["SHIPPED", "DELIVERED"], "completed": ["SHIPPED", "COMPLETED"],
             "cancelled": ["CANCELLED"]}.get(status, [])
    done = []
    if status == "returned":
        # the owner, 2026-11-11: a return after it shipped is on its way back to her — ONE private line, relisted only
        # on her "back <words>"; a refund before it shipped is a cancellation (the Mac reads it so)
        current = db.one("SELECT * FROM sales WHERE id = ?", (sale["id"],))
        if current and core.note_return(db, current, out):
            done.append("return noted")
    for kind in kinds:
        message_id = f"{PREFIX}{mp}:{order['order_id']}:{kind}"
        at = {"SHIPPED": order["shipped_at"], "DELIVERED": order["delivered_at"],
              "COMPLETED": order["completed_at"] or order["delivered_at"]}.get(kind) or seen_at
        event = {"message_id": message_id, "thread_id": None, "marketplace": mp, "kind": kind,
                 "subject": f"seen on the site: {order['status_text'] or status}", "received_at": iso(at),
                 "parsed_json": json.dumps({"order_id": order["order_id"], "quiet": kind != "CANCELLED"}),
                 "raw_text": None}
        if not core._insert_event(db, event, "matched", now):
            continue                                       # this status was recorded before: never twice
        current = db.one("SELECT * FROM sales WHERE id = ?", (sale["id"],))
        if core.apply_followup(db, current, kind, at, core._handling(db, mode, at, now), out,
                               quiet=kind != "CANCELLED"):
            done.append(kind.lower())
    return ", ".join(done) or None


def keep(db: Database, mp: str, order: dict, sale: dict | None, seen_at: datetime) -> None:
    """The order's row: its sale and item, the site's status and words, and the first time each status was seen.
    WO66: an "awaiting_scan" order's label time — the site's own when it gives one, else the first sweep that saw it
    waiting (kept)."""
    row = db.one("SELECT seen_json, first_seen FROM orders WHERE marketplace = ? AND order_id = ?",
                 (mp, order["order_id"]))
    seen = (core._loads(row["seen_json"]) if row else None) or {}
    seen.setdefault(order["status"], iso(seen_at))
    values = (sale["id"] if sale else None, sale["item_id"] if sale else None, order["title"], order["price"],
              iso(order["sold_at"]) if order["sold_at"] else None, order["status"], order["status_text"],
              json.dumps(seen, sort_keys=True), iso(seen_at))
    label = iso(order["label_at"]) if order.get("label_at") else None
    fallback = iso(seen_at) if order["status"] == WAITING else None
    if row:
        db.execute("UPDATE orders SET sale_id = COALESCE(?, sale_id), item_id = COALESCE(?, item_id), "
                   "title = COALESCE(?, title), price = COALESCE(?, price), sold_at = COALESCE(?, sold_at), "
                   "status = ?, status_text = ?, seen_json = ?, last_seen = ?, label_at = COALESCE(?, label_at, ?) "
                   "WHERE marketplace = ? AND order_id = ?",
                   (*values, label, fallback, mp, order["order_id"]))
    else:
        db.execute("INSERT INTO orders (marketplace, order_id, sale_id, item_id, title, price, sold_at, status, "
                   "status_text, seen_json, last_seen, first_seen, label_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (mp, order["order_id"], *values, iso(seen_at), label or fallback))


def swept(db: Database) -> dict[str, str]:
    """Each site's last sweep: {site: its time}."""
    return {row["source"].split(":", 1)[1]: row["last_seen"]
            for row in db.query("SELECT source, last_seen FROM heartbeats WHERE source LIKE 'orders:%'")}
