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
the order), then — Depop, whose emails name no order — the one open sale on that site sold within 3 days."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import core, match, util
from .db import Database
from .util import BadRequest, iso, parse_time

STATUSES = ("to_ship", "shipped", "delivered", "completed", "cancelled", "returned")
ON_ITS_WAY = ("shipped", "delivered", "completed")             # the site shows it left: no shipping reminder
EVENTS = {"shipped": "SHIPPED", "delivered": "DELIVERED", "completed": "COMPLETED", "cancelled": "CANCELLED"}
PREFIX = "mac:order:"
MAX_ORDERS = 200
RECENT = timedelta(days=45)        # an order our records lack is recorded only if sold this recently
NEAR = timedelta(days=3)           # a sale and an order this close in time can be the same one
FIELDS = ("order_id", "status", "status_text", "title", "price", "sold_at", "listing_id", "listing_url", "sku",
          "shipped_at", "delivered_at", "completed_at")


def report(db: Database, body: dict, now: datetime | None = None, mode: str | None = None) -> dict:
    """One site's sweep: every order recorded and acted on (see the module); the site's last sweep in `heartbeats`
    ("orders:<site>"). {"marketplace", "orders", "results": [{order_id, status, sale_id, action}]}."""
    now, mode = core._clock(now), mode or util.mode()
    if not isinstance(body, dict):
        raise BadRequest("the body must be a JSON object")
    mp = core._text(body.get("marketplace")).strip().lower()
    if mp not in util.SITES:
        raise BadRequest("marketplace is poshmark, depop or vinted")
    raw = body.get("orders")
    if not isinstance(raw, list) or len(raw) > MAX_ORDERS:
        raise BadRequest(f"orders is a list of at most {MAX_ORDERS}")
    seen_at = min(parse_time(body.get("swept_at")) or now, now)
    results = []
    for entry in raw:
        order = clean(entry)
        if order is None:
            results.append({"order_id": None, "action": "skipped: no order id or no known status"})
            continue
        results.append(one(db, mp, order, seen_at, now, mode))
    core.heartbeat(db, f"orders:{mp}", {"orders": len(results), "at": iso(seen_at)}, now, mode)
    return {"marketplace": mp, "orders": len(results), "results": results}


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
    for key in ("sold_at", "shipped_at", "delivered_at", "completed_at"):
        order[key] = parse_time(order[key])
    return order


def one(db: Database, mp: str, order: dict, seen_at: datetime, now: datetime, mode: str) -> dict:
    out = core.Outbox(now, mode)
    with db.tx():
        sale = find_sale(db, mp, order)
        action = "linked"
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
        keep(db, mp, order, sale, seen_at)
        changed = transition(db, mp, order, sale, seen_at, now, mode, out)
    out.flush()
    return {"order_id": order["order_id"], "status": order["status"], "sale_id": sale["id"],
            "action": f"{action}{', ' + changed if changed else ''}"}


def find_sale(db: Database, mp: str, order: dict) -> dict | None:
    """The sale this order is (see the module), never a guess between two."""
    row = db.one("SELECT * FROM sales WHERE marketplace = ? AND order_id = ? AND status <> 'merged' "
                 "ORDER BY created_at LIMIT 1", (mp, order["order_id"]))
    if row:
        return row
    item = match.item_for_sku(db, order["sku"]) or match.item_for_listing(db, mp, order["listing_id"],
                                                                           order["listing_url"])
    if item:
        rows = [r for r in db.query("SELECT * FROM sales WHERE marketplace = ? AND item_id = ? AND status <> 'merged' "
                                    "ORDER BY sold_at DESC", (mp, item)) if _near(r, order) and _free(r, order)]
        if len(rows) == 1:
            return rows[0]
    rows = [r for r in db.query("SELECT s.*, i.title AS item_title FROM sales s LEFT JOIN items i ON i.id = s.item_id "
                                "WHERE s.marketplace = ? AND s.status <> 'merged'", (mp,))
            if _near(r, order) and _free(r, order)]
    if order["title"]:
        candidates = [(r["id"], text) for r in rows for text in (r["title_seen"], r["item_title"]) if text]
        sale_id, ambiguous = match.best(order["title"], candidates)
        if sale_id and not ambiguous:
            return {k: v for k, v in next(r for r in rows if r["id"] == sale_id).items() if k != "item_title"}
        if ambiguous:
            return None
    # Depop's emails name no order: the one open sale on this site sold within NEAR of it
    open_rows = [r for r in rows if r["status"] not in ("cancelled", "done") and r["order_id"] in (None, "")]
    if mp == "depop" and len(open_rows) == 1 and order["sold_at"] is not None:
        return {k: v for k, v in open_rows[0].items() if k != "item_title"}
    return None


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
    return (live_since is not None and sold >= live_since) or order["status"] in ("to_ship", "shipped")


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
        parsed = {**facts, "sold_at": received, "ship_by_stated": None}
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
    """The order's row: its sale and item, the site's status and words, and the first time each status was seen."""
    row = db.one("SELECT seen_json, first_seen FROM orders WHERE marketplace = ? AND order_id = ?",
                 (mp, order["order_id"]))
    seen = (core._loads(row["seen_json"]) if row else None) or {}
    seen.setdefault(order["status"], iso(seen_at))
    values = (sale["id"] if sale else None, sale["item_id"] if sale else None, order["title"], order["price"],
              iso(order["sold_at"]) if order["sold_at"] else None, order["status"], order["status_text"],
              json.dumps(seen, sort_keys=True), iso(seen_at))
    if row:
        db.execute("UPDATE orders SET sale_id = COALESCE(?, sale_id), item_id = COALESCE(?, item_id), "
                   "title = COALESCE(?, title), price = COALESCE(?, price), sold_at = COALESCE(?, sold_at), "
                   "status = ?, status_text = ?, seen_json = ?, last_seen = ? WHERE marketplace = ? AND order_id = ?",
                   (*values, mp, order["order_id"]))
    else:
        db.execute("INSERT INTO orders (marketplace, order_id, sale_id, item_id, title, price, sold_at, status, "
                   "status_text, seen_json, last_seen, first_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (mp, order["order_id"], *values, iso(seen_at)))


def swept(db: Database) -> dict[str, str]:
    """Each site's last sweep: {site: its time}."""
    return {row["source"].split(":", 1)[1]: row["last_seen"]
            for row in db.query("SELECT source, last_seen FROM heartbeats WHERE source LIKE 'orders:%'")}
