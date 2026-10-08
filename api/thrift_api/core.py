"""The sales tracking's behaviour (WO33): marketplace emails in; sales, ship-by dates and take-down tasks out; the Mac's
answers; the dashboard's snapshot. Every function takes the database and `now` (aware UTC; default the current time);
the ones that act take `mode` too (default SALES_MODE):

- off     emails are stored and classified, nothing more: no sale, no message, no task;
- replay  sales are recorded and every message goes to the ops chat as "[replay] …" (telegram.py); never a task;
- live    from `go_live_at` (a setting, written by the first live call when missing) the group hears of each sale and
          the Mac gets the take-down tasks; an email dated before it is history: recorded, never announced, never
          taken down.

A sale's take-downs: one `delist_tasks` row for every OTHER marketplace where the item's listing is posted. The Mac
leases them (`take_tasks`, 10 minutes), takes the listing down and reports (`task_result`). Nothing here ever relists.

Messages are collected while a transaction runs and sent only after it commits (`Outbox`): a change that rolled back is
never announced, and a message recorded as sent is never sent twice."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta

from . import deadlines, emails, match, telegram, util
from .db import Database
from .util import BadRequest, Conflict, NotFound, iso, parse_time, site_name

log = logging.getLogger(__name__)

LEASE = timedelta(minutes=10)
MAX_ATTEMPTS = 3
MAX_TEXT = 200_000                       # characters of an email's text kept
ACTING = ("live", "replay")              # the handlings that send messages; only "live" makes tasks
OPEN_TASKS = ("pending", "running")
RESULTS = ("done", "not_found", "failed", "sold")
MAC_PREFIX = "mac:"
SALE_COLUMNS = ("id", "marketplace", "item_id", "listing_id", "title_seen", "price", "order_id", "sold_at", "ship_by",
                "ship_by_source", "shipped_at", "delivered_at", "status", "message_id", "created_at")
SALE_LISTS = {"open": "s.shipped_at IS NULL AND s.status NOT IN ('cancelled', 'done')",
              "unmatched": "s.status = 'unmatched'",
              "all": "1 = 1"}
# the sales a follow-up email can be about, when it doesn't name its order
FOLLOWUP_OPEN = {"SHIPPED": "s.shipped_at IS NULL AND s.status NOT IN ('cancelled', 'done')",
                 "DELIVERED": "s.status NOT IN ('cancelled', 'done')",
                 "CANCELLED": "s.status NOT IN ('cancelled', 'done')"}
# tasks made together come out in the sites' order: Poshmark, Depop, Vinted
SITE_SORT = "CASE {0}marketplace WHEN 'poshmark' THEN 0 WHEN 'depop' THEN 1 WHEN 'vinted' THEN 2 ELSE 3 END"
TASK_ORDER = f"created_at, {SITE_SORT.format('')}, id"
ITEM_COLUMNS = ("title", "brand", "size", "price", "created_at")
LISTING_COLUMNS = ("status", "url", "listing_id", "sku", "price", "posted_at")


class Outbox:
    """The messages of one transaction; `flush()` sends them once it has committed."""

    def __init__(self, now: datetime, mode: str):
        self.now, self.mode, self.messages = now, mode, []

    def group(self, text: str) -> None:
        self.messages.append(("group", text))

    def ops(self, text: str) -> None:
        self.messages.append(("ops", text))

    def flush(self) -> int:
        messages, self.messages = self.messages, []
        return sum(telegram.send(text, chat, now=self.now, mode=self.mode) for chat, text in messages)


# --- settings ------------------------------------------------------------------------------------------------------

def setting(db: Database, key: str) -> str | None:
    row = db.one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else None


def put_setting(db: Database, key: str, value: str) -> None:
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
               (key, value))


def drop_setting(db: Database, key: str) -> None:
    db.execute("DELETE FROM settings WHERE key = ?", (key,))


def go_live(db: Database, now: datetime) -> datetime:
    """`go_live_at`, written as `now` by the first live call that finds it missing."""
    db.execute("INSERT INTO settings (key, value) VALUES ('go_live_at', ?) ON CONFLICT (key) DO NOTHING", (iso(now),))
    return parse_time(setting(db, "go_live_at")) or now


# --- emails --------------------------------------------------------------------------------------------------------

def process_email(db: Database, payload: dict, now: datetime | None = None, mode: str | None = None) -> dict:
    """One email from the Gmail reader, {message_id, thread_id, from, subject, date, text}: stored once (a second POST
    of the same message_id is {"status": "duplicate"} and changes nothing), classified, and — unless the mode is off —
    parsed and acted on. OTHER is "ignored" and its text is not kept; an email that can't be parsed is "failed" with
    its text kept, and the 15-minute timer tries it again (3 attempts in all)."""
    now, mode = _clock(now), mode or util.mode()
    if not isinstance(payload, dict):
        raise BadRequest("the body must be a JSON object")
    message_id = _text(payload.get("message_id")).strip()
    if not message_id or len(message_id) > 256:
        raise BadRequest("message_id is required (at most 256 characters)")
    subject = _text(payload.get("subject"))[:1000]
    text = _text(payload.get("text"))[:MAX_TEXT]
    marketplace = emails.marketplace_of(payload.get("from"))
    kind = emails.classify(marketplace, subject, text) if marketplace else "OTHER"
    event = {"message_id": message_id, "thread_id": _text(payload.get("thread_id"))[:256] or None,
             "marketplace": marketplace, "kind": kind, "subject": subject,
             "received_at": iso(parse_time(payload.get("date")) or now), "parsed_json": None,
             "raw_text": None if kind == "OTHER" else text}
    base = {"message_id": message_id, "kind": kind, "marketplace": marketplace}
    if db.one("SELECT message_id FROM email_events WHERE message_id = ?", (message_id,)):
        return {**base, "status": "duplicate"}
    # the reader's last word on an email it couldn't read (`tries`: after its 3rd try) is always told; a headers-only
    # email without it only when it might be a sale
    if payload.get("unreadable") and marketplace and mode != "off" and (payload.get("tries") or _maybe_sale(kind, subject)):
        # WO33: Gmail wouldn't give this email's text (the reader sent its headers): never a silent loss of a sale
        out = Outbox(now, mode)
        out.ops(f"📭 A {site_name(marketplace)} email arrived without its text (Gmail wouldn't give it): "
                f"\"{subject or '(no subject)'}\", {event['received_at'][:16].replace('T', ' ')} UTC — if it's a "
                "sale, check it in Gmail.")
        out.flush()
    if kind == "OTHER" or mode == "off":
        status = "ignored" if kind == "OTHER" else "new"
        if not _insert_event(db, event, status, now):
            return {**base, "status": "duplicate"}
        return {**base, "status": status, **({"handling": "off"} if status == "new" else {})}
    return _process(db, event, now, mode, fresh=True)


SALE_WORDS = re.compile(r"\b(sold|sale|order|purchase|bought|ship|paid|payment)\b", re.I)


def _maybe_sale(kind: str, subject: str) -> bool:
    """An unreadable email worth a word to the owner: any kind but OTHER, or OTHER whose subject sounds like a sale."""
    return kind != "OTHER" or bool(SALE_WORDS.search(subject or ""))


def retry_email(db: Database, message_id: str, now: datetime | None = None, mode: str | None = None) -> dict:
    """A failed email processed again (the timer's retry); any other email is left as it is."""
    now, mode = _clock(now), mode or util.mode()
    event = db.one("SELECT * FROM email_events WHERE message_id = ?", (message_id,))
    if event is None:
        raise NotFound("no such email")
    if event["status"] != "failed" or mode == "off":
        return {"message_id": message_id, "status": event["status"], "retried": False}
    return _process(db, event, now, mode, fresh=False)


def store_samples(db: Database, samples: object) -> dict:
    """The developer's examples ({"samples": [the /email payloads]}): each message once; one not from a marketplace
    address, or without a message_id, is skipped."""
    if not isinstance(samples, list):
        raise BadRequest("samples must be a list")
    counts = {"stored": 0, "duplicates": 0, "skipped": 0}
    with db.tx():
        for sample in samples:
            message_id = _text(sample.get("message_id")).strip() if isinstance(sample, dict) else ""
            marketplace = emails.marketplace_of(sample.get("from")) if message_id else None
            if not marketplace or len(message_id) > 256:
                counts["skipped"] += 1
                continue
            received = parse_time(sample.get("date"))
            text = _text(sample.get("text"))[:MAX_TEXT]
            added = db.execute("INSERT INTO samples (message_id, marketplace, subject, received_at, text) VALUES (?, ?, ?, ?, ?) "
                               "ON CONFLICT (message_id) DO NOTHING",
                               (message_id, marketplace, _text(sample.get("subject"))[:1000], iso(received) if received else None,
                                text))
            if not added and text and db.execute(   # WO33: a sample stored without its text gets it when sent again
                    "UPDATE samples SET text = ? WHERE message_id = ? AND (text IS NULL OR text = '')", (text, message_id)):
                counts["filled"] = counts.get("filled", 0) + 1
                continue
            counts["stored" if added else "duplicates"] += 1
    return counts


def _insert_event(db: Database, event: dict, status: str, now: datetime, attempts: int = 0) -> int:
    """The email_events row (1), or 0 when its message_id is already there."""
    return db.execute(
        "INSERT INTO email_events (message_id, thread_id, marketplace, kind, subject, received_at, parsed_json, raw_text, "
        "status, attempts, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (message_id) DO NOTHING",
        (event["message_id"], event.get("thread_id"), event["marketplace"], event["kind"], event["subject"],
         event["received_at"], event.get("parsed_json"), event.get("raw_text"), status, attempts, iso(now)))


def _process(db: Database, event: dict, now: datetime, mode: str, fresh: bool) -> dict:
    """Parses and acts on one email in ONE transaction with its row (`fresh`: the row is inserted there too), so a crash
    leaves either the whole result or nothing, and the reader's retry starts clean. A failure is stored as `failed`
    with the text kept (the timer retries it); it is never lost."""
    message_id = event["message_id"]
    received = parse_time(event["received_at"]) or now
    base = {"message_id": message_id, "kind": event["kind"], "marketplace": event["marketplace"]}
    out = Outbox(now, mode)
    try:
        parsed = _facts_of(event, received)
        handling = _handling(db, mode, received, now)
        with db.tx():
            if fresh and not _insert_event(db, event, "new", now):
                return {**base, "status": "duplicate"}
            if event["kind"] == "SALE":
                result = _sale(db, event["marketplace"], parsed, message_id, event["subject"], handling, now, out)
            else:
                result = _followup(db, event["kind"], event["marketplace"], parsed, event["subject"], received, handling,
                                   out)
            db.execute("UPDATE email_events SET status = ?, parsed_json = ?, attempts = COALESCE(attempts, 0) + 1 "
                       "WHERE message_id = ?", (result["status"], _dumps(parsed), message_id))
    except Exception as e:  # noqa: BLE001 — stored as failed and retried by the timer, never lost
        reason = str(e) if isinstance(e, emails.ParseError) else f"{type(e).__name__}: {e}"[:300]
        if not isinstance(e, emails.ParseError):
            log.exception("email %s could not be processed", message_id)
        kept = (_loads(event.get("parsed_json")) or {}) if message_id.startswith(MAC_PREFIX) else {}
        failed = _dumps({**kept, "error": reason})
        if fresh:
            if not _insert_event(db, {**event, "parsed_json": failed}, "failed", now, attempts=1):
                return {**base, "status": "duplicate"}
        else:
            db.execute("UPDATE email_events SET status = 'failed', parsed_json = ?, attempts = COALESCE(attempts, 0) + 1 "
                       "WHERE message_id = ?", (failed, message_id))
        return {**base, "status": "failed", "error": reason}
    out.flush()
    return {**base, **result, "handling": handling}


def _facts_of(event: dict, received: datetime) -> dict:
    """The email's parsed facts; for a sale the Mac found (message_id "mac:…") the facts it sent, stored at once."""
    if event["message_id"].startswith(MAC_PREFIX):
        facts = _loads(event["parsed_json"]) or {}
        facts.pop("error", None)
        return {**facts, "sold_at": parse_time(facts.get("sold_at")) or received, "ship_by_stated": None}
    return emails.parse(event["marketplace"], event["kind"], event["subject"] or "", event["raw_text"] or "", received)


def _handling(db: Database, mode: str, received: datetime, now: datetime) -> str:
    """"replay", or in live mode "live" — "history" for an email dated before go_live_at."""
    if mode != "live":
        return "replay"
    return "history" if received < go_live(db, now) else "live"


# --- a sale --------------------------------------------------------------------------------------------------------

def _sale(db: Database, mp: str, parsed: dict, message_id: str, subject: str | None, handling: str, now: datetime,
          out: Outbox) -> dict:
    """A SALE: one sales row (a second email about the same order or the same item's sale on this site only fills in
    what the first lacked), matched to an item; live, the take-down tasks; acting, the group's message."""
    sold_at = util.aware(parsed.get("sold_at") or now)
    item_id, how = match.match_sale(db, mp, parsed)
    same = _same_sale(db, mp, parsed.get("order_id"), item_id)
    if same is not None:
        _fill_sale(db, same, parsed)
        return {"status": "parsed", "sale_id": same["id"], "sale_status": same["status"], "item_id": same["item_id"],
                "duplicate": True}
    ship_day, source = deadlines.ship_by(mp, sold_at, parsed.get("ship_by_stated"))
    sale_id = util.new_id("s_")
    first = _sale_elsewhere(db, item_id, mp) if item_id else None
    status = "unmatched" if item_id is None else "double_sale" if first else "matched"
    db.execute("INSERT INTO sales (id, marketplace, item_id, listing_id, title_seen, price, order_id, sold_at, ship_by, "
               "ship_by_source, shipped_at, delivered_at, status, message_id, created_at) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
               (sale_id, mp, item_id, parsed.get("listing_id"), parsed.get("title"), util.number(parsed.get("price")),
                parsed.get("order_id"), iso(sold_at), ship_day.isoformat(), source, status, message_id, iso(now)))
    tasks = []
    if status == "matched" and handling == "live":
        tasks = _create_tasks(db, sale_id, item_id, mp, now)
        if tasks:
            status = "delisting"
            db.execute("UPDATE sales SET status = 'delisting' WHERE id = ?", (sale_id,))
    if handling in ACTING:
        title, site = _title(db, item_id, parsed.get("title"), parsed.get("listing_id")), site_name(mp)
        if status == "double_sale":
            _sold_twice(db, out, item_id, title, site_name(first["marketplace"]), site, now)
        else:
            out.group(_sold_line(site, title, parsed.get("price"), ship_day))
        if status == "unmatched":
            out.ops(f"Unmatched sale on {site} ({sale_id}): {subject or title}")
    return {"status": "unmatched" if status == "unmatched" else "matched", "sale_id": sale_id, "sale_status": status,
            "item_id": item_id, "matched_by": how, "ship_by": ship_day.isoformat(), "tasks": [t["id"] for t in tasks]}


def _same_sale(db: Database, mp: str, order_id: str | None, item_id: str | None) -> dict | None:
    """The sale a second email is about: the same order on this site, else this item's open sale on this site (one
    listing sells once) unless its order id says it's another order."""
    if order_id:
        row = db.one("SELECT * FROM sales WHERE marketplace = ? AND order_id = ? ORDER BY created_at LIMIT 1",
                     (mp, order_id))
        if row:
            return row
    if item_id:
        row = db.one("SELECT * FROM sales WHERE marketplace = ? AND item_id = ? AND status <> 'cancelled' "
                     "ORDER BY created_at LIMIT 1", (mp, item_id))
        if row and (not order_id or not row["order_id"] or row["order_id"] == order_id):
            return row
    return None


def _fill_sale(db: Database, sale: dict, parsed: dict) -> None:
    """What a second email about the same sale adds: the fields the first lacked, a ship-by date it states."""
    updates = {}
    for column, key in (("order_id", "order_id"), ("listing_id", "listing_id"), ("title_seen", "title"), ("price", "price")):
        if sale[column] in (None, "") and parsed.get(key) not in (None, ""):
            updates[column] = util.number(parsed[key]) if column == "price" else parsed[key]
    stated = parsed.get("ship_by_stated")
    if isinstance(stated, date) and sale["ship_by_source"] != "email":
        updates.update(ship_by=stated.isoformat(), ship_by_source="email")
    if updates:
        sets = ", ".join(f"{column} = ?" for column in updates)          # the columns above, nothing from outside
        db.execute(f"UPDATE sales SET {sets} WHERE id = ?", (*updates.values(), sale["id"]))


def _sale_elsewhere(db: Database, item_id: str, mp: str, exclude: str | None = None) -> dict | None:
    """The item's first sale that stands (not cancelled) on another marketplace."""
    return db.one("SELECT * FROM sales WHERE item_id = ? AND marketplace <> ? AND status <> 'cancelled' AND id <> ? "
                  "ORDER BY sold_at, created_at LIMIT 1", (item_id, mp, exclude or ""))


def _create_tasks(db: Database, sale_id: str | None, item_id: str, mp: str, now: datetime,
                  skip: tuple[str, ...] = OPEN_TASKS) -> list[dict]:
    """A pending take-down for every other marketplace where the item is posted: one per sale and site, and none for a
    site where the item already has a task in a `skip` status (one under way, by default)."""
    rows = db.query("SELECT marketplace, url FROM listings WHERE item_id = ? AND marketplace <> ? AND status = 'posted'",
                    (item_id, mp))
    marks = ", ".join("?" * len(skip))
    tasks = []
    for row in sorted(rows, key=lambda r: util.SITE_ORDER.get(r["marketplace"], 99)):
        if db.one(f"SELECT id FROM delist_tasks WHERE marketplace = ? AND ((sale_id IS NOT NULL AND sale_id = ?) "
                  f"OR (item_id = ? AND status IN ({marks}))) LIMIT 1", (row["marketplace"], sale_id, item_id, *skip)):
            continue
        task_id = util.new_id("t_")
        db.execute("INSERT INTO delist_tasks (id, sale_id, item_id, marketplace, listing_url, status, attempts, created_at) "
                   "VALUES (?, ?, ?, ?, ?, 'pending', 0, ?)", (task_id, sale_id, item_id, row["marketplace"], row["url"],
                                                                iso(now)))
        tasks.append({"id": task_id, "marketplace": row["marketplace"]})
    return tasks


def _sold_line(site: str, title: str, price: object, ship_day: date) -> str:
    amount = f" — {util.money(price)}" if util.number(price) is not None else ""
    return f"💰 Sold on {site}: {title}{amount}. Ship by {deadlines.fmt_day(ship_day)}."


# --- after a sale: shipped, delivered, cancelled --------------------------------------------------------------------

def _followup(db: Database, kind: str, mp: str, parsed: dict, subject: str | None, received: datetime, handling: str,
              out: Outbox) -> dict:
    sale, why = _find_sale(db, kind, mp, parsed)
    site = site_name(mp)
    if sale is None:
        if handling in ACTING:
            out.ops(f"No sale found for a {kind.lower()} email on {site} ({why}): {subject or '(no subject)'}")
        return {"status": "unmatched", "reason": why}
    title = _title(db, sale["item_id"], sale["title_seen"], sale["listing_id"])
    at = iso(received)
    changed = False
    if kind == "SHIPPED":
        changed = bool(db.execute("UPDATE sales SET shipped_at = ? WHERE id = ? AND shipped_at IS NULL", (at, sale["id"])))
        if changed and handling in ACTING:
            out.ops(f"Shipped: {title} ({site}).")
    elif kind == "DELIVERED":
        changed = bool(db.execute("UPDATE sales SET delivered_at = COALESCE(delivered_at, ?), "
                                  "shipped_at = COALESCE(shipped_at, ?), status = 'done' "
                                  "WHERE id = ? AND status NOT IN ('cancelled', 'done')", (at, at, sale["id"])))
    elif sale["status"] != "cancelled":
        _cancel(db, sale, title, handling, out)
        changed = True
    return {"status": "matched", "sale_id": sale["id"], "changed": changed}


def _find_sale(db: Database, kind: str, mp: str, parsed: dict) -> tuple[dict | None, str]:
    """The sale a follow-up email is about: its order id; else the open sale of the item its listing / SKU names; else
    the one open sale on this site whose title is close enough. (sale, how) or (None, why not)."""
    if parsed.get("order_id"):
        row = db.one("SELECT * FROM sales WHERE marketplace = ? AND order_id = ? ORDER BY created_at DESC LIMIT 1",
                     (mp, parsed["order_id"]))
        if row:
            return row, "order"
    where = FOLLOWUP_OPEN[kind]
    item = match.item_for_sku(db, parsed.get("sku")) or \
        match.item_for_listing(db, mp, parsed.get("listing_id"), parsed.get("listing_url"))
    if item:
        rows = db.query(f"SELECT s.* FROM sales s WHERE s.marketplace = ? AND s.item_id = ? AND {where} "
                        "ORDER BY s.sold_at DESC", (mp, item))
        if len(rows) == 1:
            return rows[0], "listing"
    if parsed.get("title"):
        rows = db.query(f"SELECT s.*, i.title AS item_title FROM sales s LEFT JOIN items i ON i.id = s.item_id "
                        f"WHERE s.marketplace = ? AND {where}", (mp,))
        candidates = [(row["id"], text) for row in rows for text in (row["title_seen"], row["item_title"]) if text]
        sale_id, ambiguous = match.best(parsed["title"], candidates)
        if ambiguous:
            return None, "several open sales have that title"
        if sale_id:
            row = next(r for r in rows if r["id"] == sale_id)
            return {k: row[k] for k in SALE_COLUMNS}, "title"
    return None, "no open sale matches"


def _cancel(db: Database, sale: dict, title: str, handling: str, out: Outbox) -> None:
    """A cancelled sale: its open take-downs are called off and nothing is relisted. When the item was sold twice and
    the FIRST sale is the one cancelled, the other sale stands: it waits for shipping again and keeps the take-downs of
    the remaining sites."""
    db.execute("UPDATE sales SET status = 'cancelled' WHERE id = ?", (sale["id"],))
    site = site_name(sale["marketplace"])
    stands = None
    if sale["item_id"] and sale["status"] != "double_sale":
        stands = db.one("SELECT * FROM sales WHERE item_id = ? AND id <> ? AND status = 'double_sale' "
                        "ORDER BY sold_at, created_at LIMIT 1", (sale["item_id"], sale["id"]))
    tasks = db.query(f"SELECT id, marketplace, status FROM delist_tasks WHERE sale_id = ? ORDER BY {TASK_ORDER}",
                     (sale["id"],))
    called_off, kept = [], []
    for task in tasks:
        if task["status"] not in OPEN_TASKS:
            continue
        if stands and task["marketplace"] != stands["marketplace"]:
            db.execute("UPDATE delist_tasks SET sale_id = ? WHERE id = ?", (stands["id"], task["id"]))
            kept.append(site_name(task["marketplace"]))
        else:
            db.execute("UPDATE delist_tasks SET status = 'cancelled', lease_until = NULL WHERE id = ?", (task["id"],))
            called_off.append(site_name(task["marketplace"]))
    if stands:
        db.execute("UPDATE sales SET status = ? WHERE id = ?", ("delisting" if kept else "matched", stands["id"]))
    if handling not in ACTING:
        return
    line = f"↩️ {title}'s sale on {site} was cancelled."
    if stands:
        line += (f" The {site_name(stands['marketplace'])} sale stands — ship it by "
                 f"{deadlines.fmt_day(day_of(stands['ship_by']))}.")
    out.group(line)
    order = f", order {sale['order_id']}" if sale["order_id"] else ""
    details = [f"Cancelled: {title} on {site} ({sale['id']}{order})."]
    if called_off:
        details.append(f"Take-downs called off: {util.and_list(called_off)}.")
    if kept:
        details.append(f"Take-downs kept for the {site_name(stands['marketplace'])} sale: {util.and_list(kept)}.")
    taken = [site_name(t["marketplace"]) for t in tasks if t["status"] == "done"]
    if taken and not stands:
        details.append(f"Already taken down on {util.and_list(taken)} — relist by hand if wanted.")
    details.append("Nothing is relisted automatically.")
    out.ops(" ".join(details))


# --- the Mac --------------------------------------------------------------------------------------------------------

def sync(db: Database, body: dict, now: datetime | None = None) -> dict:
    """The Mac's items and listings, {"items": [{id, title, brand, size, price, created_at, updated_at}], "listings":
    [{item_id, marketplace, status, url, listing_id, sku, price, posted_at, updated_at}]}: a record is inserted, or
    replaces the stored one when its updated_at is newer (the same record twice changes nothing; a field it leaves out
    keeps its value). No updated_at = now."""
    now = _clock(now)
    if not isinstance(body, dict):
        raise BadRequest("the body must be a JSON object")
    items, listings = body.get("items") or [], body.get("listings") or []
    if not isinstance(items, list) or not isinstance(listings, list):
        raise BadRequest("items and listings must be lists")
    counts = {"items": dict.fromkeys(("inserted", "updated", "unchanged"), 0),
              "listings": dict.fromkeys(("inserted", "updated", "unchanged"), 0)}
    with db.tx():
        for raw in items:
            item_id = _text(raw.get("id")).strip() if isinstance(raw, dict) else ""
            if not item_id:
                raise BadRequest("every item needs an id")
            counts["items"][_upsert(db, "items", {"id": item_id}, raw, ITEM_COLUMNS, now)] += 1
        for raw in listings:
            item_id = _text(raw.get("item_id")).strip() if isinstance(raw, dict) else ""
            marketplace = _text(raw.get("marketplace")).strip().lower() if isinstance(raw, dict) else ""
            if not item_id or not marketplace:
                raise BadRequest("every listing needs an item_id and a marketplace")
            counts["listings"][_upsert(db, "listings", {"item_id": item_id, "marketplace": marketplace}, raw,
                                       LISTING_COLUMNS, now)] += 1
    return counts


def _upsert(db: Database, table: str, key: dict, raw: dict, columns: tuple, now: datetime) -> str:
    """"inserted" | "updated" | "unchanged" (stored as new or newer). `table` and `columns` are this module's own."""
    updated = iso(parse_time(raw.get("updated_at")) or now)
    values = {column: _column(column, raw[column]) for column in columns if column in raw}
    names = [*key, *values, "updated_at"]
    added = db.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))}) "
                       f"ON CONFLICT ({', '.join(key)}) DO NOTHING", (*key.values(), *values.values(), updated))
    if added:
        return "inserted"
    sets = ", ".join(f"{column} = ?" for column in [*values, "updated_at"])
    where = " AND ".join(f"{column} = ?" for column in key)
    changed = db.execute(f"UPDATE {table} SET {sets} WHERE {where} AND (updated_at IS NULL OR updated_at < ?)",
                         (*values.values(), updated, *key.values(), updated))
    return "updated" if changed else "unchanged"


def _column(column: str, value: object) -> object:
    if column == "price":
        return util.number(value)
    if column in ("created_at", "posted_at"):
        moment = parse_time(value)
        return iso(moment) if moment else None
    if value is None:
        return None
    text = _text(value).strip()[:2000]
    return text.lower() if column == "status" else text


def take_tasks(db: Database, sites: list[str], now: datetime | None = None, mode: str | None = None) -> list[dict]:
    """The take-downs the Mac should do now on `sites`: pending ones, and running ones whose lease ran out; each is
    leased for 10 minutes (status running). Outside live mode none (nothing is taken down in replay or off)."""
    now, mode = _clock(now), mode or util.mode()
    sites = [s for s in sites if s in util.SITES]
    if mode != "live" or not sites:
        return []
    marks = ", ".join("?" * len(sites))
    until = iso(now + LEASE)
    taken = []
    with db.tx():
        rows = db.query(
            "SELECT t.id, t.sale_id, t.item_id, t.marketplace, t.listing_url, t.attempts, l.listing_id, "
            "COALESCE(i.title, s.title_seen) AS title FROM delist_tasks t "
            "LEFT JOIN items i ON i.id = t.item_id LEFT JOIN sales s ON s.id = t.sale_id "
            "LEFT JOIN listings l ON l.item_id = t.item_id AND l.marketplace = t.marketplace "
            f"WHERE t.marketplace IN ({marks}) AND (t.status = 'pending' OR (t.status = 'running' "
            f"AND (t.lease_until IS NULL OR t.lease_until < ?))) ORDER BY t.created_at, {SITE_SORT.format('t.')}, t.id",
            (*sites, iso(now)))
        for row in rows:
            leased = db.execute("UPDATE delist_tasks SET status = 'running', lease_until = ? WHERE id = ? AND (status = "
                                "'pending' OR (status = 'running' AND (lease_until IS NULL OR lease_until < ?)))",
                                (until, row["id"], iso(now)))
            if leased:
                taken.append({**row, "attempts": int(row["attempts"] or 0), "title": row["title"] or "an item",
                              "lease_until": until})
    return taken


def task_result(db: Database, task_id: str, result: str, error: str | None = None, evidence: str | None = None,
                now: datetime | None = None, mode: str | None = None, manual: bool = False) -> dict:
    """The Mac's answer for one take-down: done (the listing is delisted here), not_found, or failed — tried again up
    to 3 attempts, then the owner is asked to mark it sold by hand; `manual` (the Mac doesn't automate that site yet)
    asks at once. A repeated answer changes nothing ("changed": false). When every take-down of a sale is done or
    not found, the group hears "✓ <title> taken down on A and B."."""
    now, mode = _clock(now), mode or util.mode()
    if result not in RESULTS:
        raise BadRequest("result is done, not_found, failed or sold")
    error = _text(error).strip()[:1000] or None
    evidence = _text(evidence).strip()[:2000] or None
    out = Outbox(now, mode)
    with db.tx():
        task = db.one("SELECT * FROM delist_tasks WHERE id = ?", (task_id,))
        if task is None:
            raise NotFound("no such task")
        accepts = {"done": ("pending", "running", "failed"), "not_found": ("pending", "running", "failed"),
                   "sold": ("pending", "running", "failed"),
                   "failed": ("pending", "running") if manual else ("running",)}[result]
        attempts = int(task["attempts"] or 0)
        if task["status"] not in accepts:
            return {"id": task_id, "status": task["status"], "attempts": attempts, "changed": False}
        sale = db.one("SELECT title_seen, listing_id FROM sales WHERE id = ?", (task["sale_id"],)) or {}
        title = _title(db, task["item_id"], sale.get("title_seen"), sale.get("listing_id"))
        site = site_name(task["marketplace"])
        if result == "done":
            status = "done"
            db.execute("UPDATE delist_tasks SET status = 'done', done_at = ?, lease_until = NULL, last_error = NULL, "
                       "evidence = COALESCE(?, evidence) WHERE id = ?", (iso(now), evidence, task_id))
            db.execute("UPDATE listings SET status = 'delisted', updated_at = ? WHERE item_id = ? AND marketplace = ?",
                       (iso(now), task["item_id"], task["marketplace"]))
        elif result == "sold":                # WO33: Depop shows it sold already — nothing deleted: sold twice
            status = "sold"
            db.execute("UPDATE delist_tasks SET status = 'sold', done_at = ?, lease_until = NULL, "
                       "evidence = COALESCE(?, evidence) WHERE id = ?", (iso(now), evidence, task_id))
            db.execute("UPDATE listings SET status = 'sold', updated_at = ? WHERE item_id = ? AND marketplace = ?",
                       (iso(now), task["item_id"], task["marketplace"]))
            first = db.one("SELECT marketplace FROM sales WHERE id = ?", (task["sale_id"],)) or {}
            _sold_twice(db, out, task["item_id"], title, site_name(first.get("marketplace") or ""), site, now)
        elif result == "not_found":
            status = "not_found"
            db.execute("UPDATE delist_tasks SET status = 'not_found', done_at = ?, lease_until = NULL, last_error = ?, "
                       "evidence = COALESCE(?, evidence) WHERE id = ?", (iso(now), error, evidence, task_id))
            out.ops(f"Take-down: {title} wasn't found on {site} ({task_id})" + (f": {error}" if error else "") + ".")
        else:
            attempts += 0 if manual else 1        # a manual take-down was never tried
            final = manual or attempts >= MAX_ATTEMPTS
            status = "failed" if final else "pending"
            db.execute("UPDATE delist_tasks SET status = ?, attempts = ?, last_error = ?, evidence = COALESCE(?, evidence), "
                       "lease_until = NULL, done_at = ? WHERE id = ?",
                       (status, attempts, error, evidence, iso(now) if final else None, task_id))
            if final:
                out.group(f"Couldn't take {title} down on {site} — please mark it sold there.")
                why = "not automated on this site yet" if manual else f"failed {attempts} times"
                out.ops(f"Take-down of {title} on {site} left to the owner ({task_id}, {why})"
                        + (f": {error}" if error else "") + ".")
        _after_task(db, task["sale_id"], out)
    out.flush()
    return {"id": task_id, "status": status, "attempts": attempts, "changed": True}


def _sold_twice(db: Database, out: Outbox, item_id: str | None, title: str, first: str, second: str,
                now: datetime) -> None:
    """ONE "⚠️ Sold twice" line per item, whoever noticed first — the sale emails, or the Mac finding Depop's listing
    sold already (WO33)."""
    key = f"sold_twice:{item_id}"
    if item_id and setting(db, key):
        return
    if item_id:
        put_setting(db, key, iso(now))
    out.group(f"⚠️ Sold twice: {title} sold on {first} and {second}. Cancel the {second} order in its app.")


def _after_task(db: Database, sale_id: str, out: Outbox) -> None:
    """Once none of the sale's take-downs is open: the sale is no longer delisting, and when every one was done or not
    found (none left to the owner), the group hears that the item is down everywhere."""
    tasks = db.query("SELECT marketplace, status FROM delist_tasks WHERE sale_id = ?", (sale_id,))
    if not tasks or any(task["status"] in OPEN_TASKS for task in tasks):
        return
    sale = db.one("SELECT * FROM sales WHERE id = ?", (sale_id,))
    if sale is None:
        return
    if sale["status"] == "delisting":
        db.execute("UPDATE sales SET status = 'matched' WHERE id = ?", (sale_id,))
    if sale["status"] != "cancelled" and all(task["status"] in ("done", "not_found") for task in tasks):
        sites = [site_name(t["marketplace"]) for t in sorted(tasks, key=lambda t: util.SITE_ORDER.get(t["marketplace"], 99))]
        title = _title(db, sale["item_id"], sale["title_seen"], sale["listing_id"])
        out.group(f"✓ {title} taken down on {util.and_list(sites)}.")


def mac_event(db: Database, body: dict, now: datetime | None = None, mode: str | None = None) -> dict:
    """{"kind": "shipped", "words": "lacoste tee"}: the one open sale (not shipped, not cancelled / double / done) whose
    title has all the words is shipped now — "matched" is its id, else null and "reason" says why.
    {"kind": "sold_found", "marketplace", "listing_id" (or "listing_url"), "item_id", "title", "price", "sold_at"}: a
    sale the Mac saw on a marketplace, handled like a SALE email whose message_id is "mac:<marketplace>:<listing_id>".
    {"kind": "not_for_sale", "item_id", "marketplace", "url"}: take-downs elsewhere, no sale (_mac_not_for_sale)."""
    now, mode = _clock(now), mode or util.mode()
    if not isinstance(body, dict):
        raise BadRequest("the body must be a JSON object")
    kind = _text(body.get("kind")).strip().lower()
    if kind == "shipped":
        return _mac_shipped(db, body.get("words"), now, mode)
    if kind == "sold_found":
        return _mac_sold_found(db, body, now, mode)
    if kind == "not_for_sale":
        return _mac_not_for_sale(db, body, now, mode)
    raise BadRequest("kind is shipped, sold_found or not_for_sale")


def _mac_not_for_sale(db: Database, body: dict, now: datetime, mode: str) -> dict:
    """{"kind": "not_for_sale", "item_id", "marketplace", "url"}: the item's page on `marketplace` says it is no longer
    for sale (the Mac's go-live check), so it comes down everywhere else — a take-down without a sale (sale_id NULL)
    for every other site where its listing is posted and which has no take-down for it yet (a cancelled one aside). No
    sale is recorded and nothing is said: the Mac sends its own summary. Live only."""
    item_id = _text(body.get("item_id")).strip()
    marketplace = _text(body.get("marketplace")).strip().lower()
    if not item_id:
        raise BadRequest("item_id is required")
    if marketplace not in util.SITES:
        raise BadRequest("marketplace is poshmark, depop or vinted")
    if mode != "live":
        return {"tasks": 0, "task_ids": [], "mode": mode}
    with db.tx():
        created = _create_tasks(db, None, item_id, marketplace, now,
                                skip=("pending", "running", "done", "failed", "not_found"))
    return {"tasks": len(created), "task_ids": [t["id"] for t in created], "mode": mode}


def _mac_shipped(db: Database, words: object, now: datetime, mode: str) -> dict:
    text = " ".join(words) if isinstance(words, list) else _text(words)
    wanted = re.findall(r"[a-z0-9]+", text.lower())
    if not wanted:
        raise BadRequest("words is required")
    rows = db.query("SELECT s.id, s.marketplace, s.title_seen, i.title AS item_title FROM sales s "
                    "LEFT JOIN items i ON i.id = s.item_id WHERE s.shipped_at IS NULL "
                    "AND s.status NOT IN ('cancelled', 'double_sale', 'done') ORDER BY s.sold_at")
    hits = [row for row in rows if any(all(w in title.lower() for w in wanted)
                                       for title in (row["item_title"], row["title_seen"]) if title)]
    if len(hits) != 1:
        reason = (f"no open sale's title has all of: {' '.join(wanted)}" if not hits
                  else f"{len(hits)} open sales match: add a word")
        return {"status": "no_match" if not hits else "ambiguous", "matched": None, "title": None, "reason": reason,
                "candidates": [{"sale_id": r["id"], "title": r["item_title"] or r["title_seen"],
                                "marketplace": r["marketplace"]} for r in hits]}
    sale = hits[0]
    title = sale["item_title"] or sale["title_seen"]
    out = Outbox(now, mode)
    with db.tx():
        db.execute("UPDATE sales SET shipped_at = ? WHERE id = ? AND shipped_at IS NULL", (iso(now), sale["id"]))
        out.group(f"✓ Marked shipped: {title}")
    out.flush()
    return {"status": "shipped", "matched": sale["id"], "title": title, "marketplace": sale["marketplace"]}


def _mac_sold_found(db: Database, body: dict, now: datetime, mode: str) -> dict:
    marketplace = _text(body.get("marketplace")).strip().lower()
    if marketplace not in util.SITES:
        raise BadRequest("marketplace is poshmark, depop or vinted")
    url = _text(body.get("listing_url")).strip() or None
    listing_id = _text(body.get("listing_id")).strip() or emails.listing_id_from_url(marketplace, url)
    if not listing_id:
        raise BadRequest("listing_id (or a listing_url with one) is required")
    message_id = f"{MAC_PREFIX}{marketplace}:{listing_id}"
    sold_at = parse_time(body.get("sold_at")) or now
    facts = {"title": _text(body.get("title")).strip() or None, "listing_url": url, "listing_id": listing_id,
             "sku": _text(body.get("item_id") or body.get("sku")).strip() or None,
             "price": util.number(body.get("price")), "order_id": _text(body.get("order_id")).strip() or None,
             "sold_at": iso(sold_at)}
    event = {"message_id": message_id, "thread_id": None, "marketplace": marketplace, "kind": "SALE",
             "subject": "found sold by the Mac", "received_at": iso(sold_at), "parsed_json": _dumps(facts),
             "raw_text": None}
    base = {"message_id": message_id, "kind": "SALE", "marketplace": marketplace}
    if db.one("SELECT message_id FROM email_events WHERE message_id = ?", (message_id,)):
        return {**base, "status": "duplicate"}
    if mode == "off":
        if not _insert_event(db, event, "new", now):
            return {**base, "status": "duplicate"}
        return {**base, "status": "new", "handling": "off"}
    return _process(db, event, now, mode, fresh=True)


def heartbeat(db: Database, source: object, info: object = None, now: datetime | None = None,
              mode: str | None = None) -> dict:
    """A reader's or the Mac's sign of life, answered with the mode and go_live_at (the Mac reconciles once per
    go_live_at it sees; in live mode a heartbeat is a live call and sets it when missing)."""
    now, mode = _clock(now), mode or util.mode()
    source = _text(source).strip()[:64]
    if not source:
        raise BadRequest("source is required")
    db.execute("INSERT INTO heartbeats (source, last_seen, info) VALUES (?, ?, ?) "
               "ON CONFLICT (source) DO UPDATE SET last_seen = excluded.last_seen, info = excluded.info",
               (source, iso(now), None if info is None else _dumps(info)[:4000]))
    if mode == "live":
        go_live(db, now)
    return {"ok": True, "status": "ok", "source": source, "last_seen": iso(now), "mode": mode,
            "go_live_at": setting(db, "go_live_at")}


def sold_items(db: Database, now: datetime | None = None, mode: str | None = None) -> list[str]:
    """The items with a sale that stands (not cancelled) whose email came after go_live_at, in the last 30 days — the
    Mac skips their still-queued listings. Live only (else [])."""
    now, mode = _clock(now), mode or util.mode()
    if mode != "live":
        return []
    since = max(go_live(db, now), now - timedelta(days=30))
    rows = db.query("SELECT DISTINCT s.item_id FROM sales s JOIN email_events e ON e.message_id = s.message_id "
                    "WHERE s.item_id IS NOT NULL AND s.status <> 'cancelled' AND e.received_at >= ? ORDER BY s.item_id",
                    (iso(since),))
    return [row["item_id"] for row in rows]


# --- the owner's and the developer's views ---------------------------------------------------------------------------

def sales_list(db: Database, state: str = "open") -> list[dict]:
    """The sales, newest first, each with "title" (the item's, else the email's) and its take-downs: open (not
    shipped, not cancelled or done), unmatched, or all."""
    state = (state or "open").strip().lower()
    if state not in SALE_LISTS:
        raise BadRequest("state is open, unmatched or all")
    rows = db.query(f"SELECT s.*, i.title AS item_title FROM sales s LEFT JOIN items i ON i.id = s.item_id "
                    f"WHERE {SALE_LISTS[state]} ORDER BY s.sold_at DESC, s.created_at DESC")
    tasks: dict[str, list] = {}
    for task in db.query("SELECT id, sale_id, marketplace, status, attempts, last_error FROM delist_tasks "
                         f"ORDER BY {TASK_ORDER}"):
        tasks.setdefault(task["sale_id"], []).append({k: task[k] for k in ("id", "marketplace", "status", "attempts",
                                                                           "last_error")})
    return [{**{k: row[k] for k in SALE_COLUMNS}, "title": row["item_title"] or row["title_seen"],
             "tasks": tasks.get(row["id"], [])} for row in rows]


def match_sale(db: Database, sale_id: str, item_id: object, now: datetime | None = None,
               mode: str | None = None) -> dict:
    """The developer's match of a sale to an item (an unmatched one, or a wrong match without take-downs): matched,
    or double_sale when the item already sold elsewhere; live and not history, the take-downs as for a SALE email."""
    now, mode = _clock(now), mode or util.mode()
    item_id = _text(item_id).strip()
    if not item_id:
        raise BadRequest("item_id is required")
    out = Outbox(now, mode)
    with db.tx():
        sale = db.one("SELECT * FROM sales WHERE id = ?", (sale_id,))
        if sale is None:
            raise NotFound("no such sale")
        item = db.one("SELECT id, title FROM items WHERE id = ?", (item_id,))
        if item is None:
            raise NotFound("no such item")
        task_ids = [t["id"] for t in db.query(f"SELECT id FROM delist_tasks WHERE sale_id = ? ORDER BY {TASK_ORDER}",
                                              (sale_id,))]
        if sale["item_id"] == item_id:
            return {"sale_id": sale_id, "item_id": item_id, "status": sale["status"], "tasks": task_ids, "changed": False}
        if task_ids:
            raise Conflict("this sale already has take-down tasks")
        status = sale["status"]
        first = _sale_elsewhere(db, item_id, sale["marketplace"], exclude=sale_id)
        if status in ("new", "unmatched", "matched", "double_sale"):
            status = "double_sale" if first else "matched"
        db.execute("UPDATE sales SET item_id = ?, status = ? WHERE id = ?", (item_id, status, sale_id))
        if mode == "off":
            handling = "off"
        elif mode == "replay":
            handling = "replay"
        else:
            handling = "history" if (parse_time(sale["sold_at"]) or now) < go_live(db, now) else "live"
        tasks = []
        if status == "matched" and handling == "live":
            tasks = _create_tasks(db, sale_id, item_id, sale["marketplace"], now)
            if tasks:
                status = "delisting"
                db.execute("UPDATE sales SET status = 'delisting' WHERE id = ?", (sale_id,))
        site, title = site_name(sale["marketplace"]), item["title"] or item_id
        if status == "double_sale" and handling in ACTING:
            _sold_twice(db, out, item_id, title, site_name(first["marketplace"]), site, now)
        downs = f"; take-downs: {util.and_list([site_name(t['marketplace']) for t in tasks])}" if tasks else ""
        out.ops(f"Matched the {site} sale {sale_id} to {title} ({status}){downs}.")
    out.flush()
    return {"sale_id": sale_id, "item_id": item_id, "status": status, "tasks": [t["id"] for t in tasks], "changed": True}


def health(db: Database, now: datetime | None = None, mode: str | None = None) -> dict:
    """The database answers; the mode; each heartbeat's last time; the counts worth a look."""
    now, mode = _clock(now), mode or util.mode()
    db.one("SELECT 1 AS ok")

    def count(sql: str) -> int:
        return int(db.one(sql)["n"])
    return {"ok": True, "db": "ok", "mode": mode,
            "heartbeats": {row["source"]: row["last_seen"]
                           for row in db.query("SELECT source, last_seen FROM heartbeats ORDER BY source")},
            "open_sales": count("SELECT COUNT(*) AS n FROM sales WHERE shipped_at IS NULL AND status <> 'cancelled'"),
            "unmatched_sales": count("SELECT COUNT(*) AS n FROM sales WHERE status = 'unmatched'"),
            "open_tasks": count("SELECT COUNT(*) AS n FROM delist_tasks WHERE status IN ('pending', 'running')"),
            "failed_emails": count("SELECT COUNT(*) AS n FROM email_events WHERE status = 'failed'"),
            "go_live_at": setting(db, "go_live_at"), "now": iso(now)}


def dashboard_data(db: Database, now: datetime | None = None) -> dict:
    """The snapshot dashboard.render reads: every item with its listings and its sale (the one that stands — a cancelled
    or a double sale only when there is no other), the number of unmatched sales, the time."""
    now = _clock(now)
    items = {row["id"]: {**row, "listings": {}, "sale": None}
             for row in db.query("SELECT id, title, price, created_at FROM items ORDER BY created_at, id")}
    for row in db.query("SELECT item_id, marketplace, status, url FROM listings"):
        if row["item_id"] in items:
            items[row["item_id"]]["listings"][row["marketplace"]] = {"status": row["status"], "url": row["url"]}
    rank = {"cancelled": 0, "double_sale": 1}
    chosen: dict[str, tuple] = {}
    for row in db.query("SELECT item_id, marketplace, sold_at, price, ship_by, shipped_at, status FROM sales "
                        "WHERE item_id IS NOT NULL"):
        key = (rank.get(row["status"], 2), row["sold_at"] or "")
        if row["item_id"] in items and (row["item_id"] not in chosen or key > chosen[row["item_id"]][0]):
            chosen[row["item_id"]] = (key, row)
    for item_id, (_, row) in chosen.items():
        items[item_id]["sale"] = {k: row[k] for k in ("marketplace", "sold_at", "price", "ship_by", "shipped_at", "status")}
    unmatched = int(db.one("SELECT COUNT(*) AS n FROM sales WHERE status = 'unmatched'")["n"])
    return {"items": list(items.values()), "unmatched": unmatched, "generated_at": iso(now)}


def test_message(chat: str = "ops", now: datetime | None = None, mode: str | None = None) -> dict:
    """"✓ thrift-api is up — <local time>" to the ops chat (or the group), as the mode routes it; off sends nothing."""
    now, mode = _clock(now), mode or util.mode()
    if chat not in telegram.CHAT_ENV:
        raise BadRequest("chat is ops or group")
    if mode == "off":
        return {"sent": False, "chat": chat, "mode": mode}
    sent = telegram.send(f"✓ thrift-api is up — {local_text(now)}", chat, now=now, mode=mode)
    return {"sent": sent, "chat": "ops" if mode == "replay" else chat, "mode": mode}


# --- small helpers -------------------------------------------------------------------------------------------------

def local_text(moment: datetime) -> str:
    """"Thu Oct 8, 11:05 AM EDT" in the owner's zone."""
    when = deadlines.local(moment)
    return (f"{deadlines.fmt_day(when.date())}, {when.hour % 12 or 12}:{when.minute:02d} "
            f"{'AM' if when.hour < 12 else 'PM'} {when.tzname()}")


def _title(db: Database, item_id: str | None, seen: str | None, listing_id: str | None = None) -> str:
    """The item's title, else the one the email gave, else the listing's id."""
    if item_id:
        row = db.one("SELECT title FROM items WHERE id = ?", (item_id,))
        if row and row["title"]:
            return row["title"]
    if seen:
        return seen
    return f"listing {listing_id}" if listing_id else "an item"


def day_of(value: object) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _clock(now: datetime | None) -> datetime:
    return util.aware(now) if now is not None else util.utcnow()


def _text(value: object) -> str:
    return "" if value is None else value if isinstance(value, str) else str(value)


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_plain)


def _plain(value: object) -> str:
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"not JSON: {type(value).__name__}")


def _loads(text: str | None) -> dict | None:
    try:
        value = json.loads(text) if text else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
