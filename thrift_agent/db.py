"""SQLite state. The DB is the source of truth; folders are just inboxes and archives."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY, src_dir TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
  n_photos INTEGER, segmentation TEXT, reasons TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
  seq INTEGER NOT NULL, status TEXT NOT NULL, dir TEXT NOT NULL,
  note TEXT, facts TEXT, price TEXT, renders TEXT, gate TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, cover_hash TEXT
);
CREATE TABLE IF NOT EXISTS listings (
  item_id TEXT NOT NULL REFERENCES items(id), marketplace TEXT NOT NULL,
  status TEXT NOT NULL, url TEXT, listing_id TEXT, price INTEGER, fields_json TEXT,
  posted_at TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
  PRIMARY KEY (item_id, marketplace)
);
CREATE TABLE IF NOT EXISTS events (
  ts TEXT NOT NULL, ref TEXT, kind TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
  chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL,
  text TEXT, sent_at TEXT NOT NULL, resolved_at TEXT,
  PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS api_dirty (
  kind TEXT NOT NULL, key TEXT NOT NULL, changed_at TEXT NOT NULL, PRIMARY KEY (kind, key)
);
CREATE TRIGGER IF NOT EXISTS api_dirty_item_ins AFTER INSERT ON items BEGIN
  INSERT OR REPLACE INTO api_dirty VALUES ('item', NEW.id, strftime('%Y-%m-%dT%H:%M:%f', 'now'));
END;
CREATE TRIGGER IF NOT EXISTS api_dirty_item_upd AFTER UPDATE ON items BEGIN
  INSERT OR REPLACE INTO api_dirty VALUES ('item', NEW.id, strftime('%Y-%m-%dT%H:%M:%f', 'now'));
END;
CREATE TRIGGER IF NOT EXISTS api_dirty_listing_ins AFTER INSERT ON listings BEGIN
  INSERT OR REPLACE INTO api_dirty VALUES ('listing', NEW.item_id || '|' || NEW.marketplace,
                                           strftime('%Y-%m-%dT%H:%M:%f', 'now'));
END;
CREATE TRIGGER IF NOT EXISTS api_dirty_listing_upd AFTER UPDATE ON listings BEGIN
  INSERT OR REPLACE INTO api_dirty VALUES ('listing', NEW.item_id || '|' || NEW.marketplace,
                                           strftime('%Y-%m-%dT%H:%M:%f', 'now'));
END;
CREATE TABLE IF NOT EXISTS api_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS takedowns (
  id TEXT PRIMARY KEY, item_id TEXT NOT NULL, marketplace TEXT NOT NULL, listing_url TEXT, title TEXT,
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT, fetched_at TEXT NOT NULL, done_at TEXT
);
"""

# batch: new → needs_confirm → split | failed
# item:  new → awaiting_price | needs_info → ready → (posting → posted | drafted | failed) → sold
#        needs_owner: the poster asked the owner a question; the reply reprocesses the item
#        dropped: the owner confirmed a held re-share is the same garment as an existing item
# listing (WO30, one row per item and marketplace — poshmark | depop | vinted; it replaced the `posts` table, whose
#        rows were copied in once and which is left as it was):
#        queued → posting → posted | failed | skipped, later delisted | sold; also dryrun (a dry-run filled the form)
#        and drafted. failed / skipped / dryrun with no URL → queued via `thrift requeue`. `fields_json` holds the
#        values mapped for the site's form before it opens; `price` the approved price it listed at.
# api_dirty (WO33): the items and listings changed since the last /sync — filled by the triggers above (every change,
#        whoever made it), emptied by sales.push once the API took them; api_outbox: the other calls to the API
#        (task results, the owner's 'shipped' replies) kept while it is out of reach and replayed in order;
#        takedowns: the take-downs the API handed to this Mac (a sale elsewhere), done by the poster's site workers.
# outbox: every Telegram message the agent sent that expects a reply (kind batch | condition | kids | item | owner_q),
#        so a reply or a button press can be mapped back to its batch/item. At most one is open at a time (WO20,
#        approve.pump); the dev print is recorded under chat 'dev'. kv holds the getUpdates offset, the queue's
#        send lock and the current round's items.

# Columns added after the first release; applied with ALTER TABLE when an older DB is opened.
MIGRATIONS = {"items": {"cover_hash": "TEXT", "owner_price": "INTEGER", "owner_condition": "TEXT",
                        "owner_kids_gender": "TEXT", "deferred_at": "TEXT", "owner_question": "TEXT",
                        "owner_cover": "INTEGER", "views": "TEXT",
                        "owner_brand": "TEXT",           # WO25: "none" = the owner said the item has no brand
                        "owner_category": "TEXT",        # WO25: {"department", "category", "subcategory"} the owner picked
                        "owner_title": "TEXT"}}          # WO27: the owner's exact title (`thrift edit --title`)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now().strftime('%y%m%d')}_{uuid.uuid4().hex[:6]}"


LISTINGS_MIGRATED = "listings_migrated"      # kv: when the old `posts` rows were copied into `listings` (once)
ANNOUNCED_MIGRATED = "announced_migrated"    # kv: when the items live before WO32b were marked announced (once)
API_SEEDED = "api_dirty_seeded"               # kv: when every item and listing was first marked for /sync (once)
_POSH_ID = re.compile(r"-([0-9a-f]{24})/?$")


def listing_id_from(mp: str, url: str | None) -> str | None:
    """The site's own id of a listing, from its address: Poshmark's 24-hex id, Vinted's number, Depop's slug."""
    if not url:
        return None
    path = url.split("?")[0].rstrip("/")
    if mp == "poshmark":
        m = _POSH_ID.search(path)
        return m.group(1) if m else None
    if mp == "vinted":
        m = re.search(r"/items/(\d+)", path)
        return m.group(1) if m else None
    if mp == "depop":       # <shop>-<words> (never "create"); Post lands on its /manage/ view (WO32b)
        m = re.search(r"/products/([a-z0-9]+(?:-[a-z0-9]+)+)(?:/manage)?$", path)
        return m.group(1) if m else None
    return None


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self._migrate_posts()
        self._migrate_announced()
        self.seed_api_dirty()

    def seed_api_dirty(self, again: bool = False) -> int:
        """WO33: every item and listing marked for /sync once — the triggers mark only what changes after they were
        made, and thrift-api must hold the listings made before, or a sale of one could never be matched. `again`
        (`thrift sync --push --all`): the whole mirror sent again. Returns how many were marked."""
        with self.tx() as c:
            if not again and c.execute("SELECT 1 FROM kv WHERE key=?", (API_SEEDED,)).fetchone():
                return 0
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            c.execute("INSERT OR IGNORE INTO api_dirty SELECT 'item', id, ? FROM items", (stamp,))
            n = c.execute("SELECT changes()").fetchone()[0]
            c.execute("INSERT OR IGNORE INTO api_dirty SELECT 'listing', item_id || '|' || marketplace, ? FROM listings",
                      (stamp,))
            n += c.execute("SELECT changes()").fetchone()[0]
            c.execute("INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (API_SEEDED, now()))
            return n

    def _migrate_announced(self) -> None:
        """WO32b: every item already live on Poshmark counts as announced in the group — the flow before WO30 said its
        "Posted ✓" without the posted_announced events (the live miss: a Poshmark listing from days before announced
        again, with no new link, when Depop was added). Once; from then on the events say it."""
        with self.tx() as c:
            if c.execute("SELECT 1 FROM kv WHERE key=?", (ANNOUNCED_MIGRATED,)).fetchone():
                return
            n = 0
            for (iid,) in c.execute("SELECT item_id FROM listings WHERE marketplace='poshmark' AND status='posted'"
                                    ).fetchall():
                if c.execute("SELECT 1 FROM events WHERE ref=? AND kind='posted_announced'", (iid,)).fetchone():
                    continue
                mps = [r[0] for r in c.execute("SELECT marketplace FROM listings WHERE item_id=? AND status='posted'",
                                               (iid,))]
                c.execute("INSERT INTO events VALUES (?,?,?,?)",
                          (now(), iid, "posted_announced", json.dumps({"mps": mps, "migrated": "WO32b"})))
                n += 1
            c.execute("INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (ANNOUNCED_MIGRATED, now()))
            if n:
                c.execute("INSERT INTO events VALUES (?,?,?,?)", (now(), None, "announced_migrated",
                                                                  json.dumps({"items": n})))

    def _migrate_posts(self) -> None:
        """WO30: the old per-marketplace `posts` rows become `listings` rows, once — every Poshmark-posted item keeps
        its poshmark/posted row and URL. A skip (failed + "skipped: …") becomes the status 'skipped'. The old table
        is left as it was (a code rollback still finds it)."""
        if not self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='posts'").fetchone():
            return
        with self.tx() as c:
            if c.execute("SELECT 1 FROM kv WHERE key=?", (LISTINGS_MIGRATED,)).fetchone():
                return
            n = 0
            for row in c.execute("SELECT * FROM posts").fetchall():
                status, error = row["status"], row["last_error"]
                if status == "failed" and (error or "").startswith("skipped: "):
                    status = "skipped"
                item = c.execute("SELECT owner_price, renders FROM items WHERE id=?", (row["item_id"],)).fetchone()
                render = ((loads(item["renders"]) or {}).get(row["marketplace"]) or {}) if item else {}
                price = (item["owner_price"] if item else None) or render.get("price")
                cur = c.execute(
                    "INSERT OR IGNORE INTO listings (item_id, marketplace, status, url, listing_id, price, posted_at, "
                    "error, attempts, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (row["item_id"], row["marketplace"], status, row["url"],
                     listing_id_from(row["marketplace"], row["url"]), price, row["posted_at"], error,
                     row["attempts"], row["updated_at"]))
                n += cur.rowcount
            c.execute("INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (LISTINGS_MIGRATED, now()))
            c.execute("INSERT INTO events VALUES (?,?,?,?)", (now(), None, "listings_migrated", json.dumps({"rows": n})))

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def log(self, ref: str | None, kind: str, detail: Any = None) -> None:
        self.conn.execute("INSERT INTO events VALUES (?,?,?,?)",
                          (now(), ref, kind, json.dumps(detail, default=str) if detail is not None else None))

    # batches
    def add_batch(self, src_dir: str, n_photos: int) -> str | None:
        bid = new_id("b")
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO batches VALUES (?,?,?,?,?,?,?,?)",
            (bid, src_dir, "new", n_photos, None, None, now(), now()))
        return bid if cur.rowcount else None

    def set_batch(self, bid: str, **fields: Any) -> None:
        self._update("batches", "id", bid, fields)

    def batch(self, bid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()

    def batches(self, status: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM batches WHERE status=? ORDER BY created_at", (status,)).fetchall()

    # items
    def add_item(self, batch_id: str, seq: int, dir_: str, note: str | None = None) -> str:
        iid = new_id("i")
        self.conn.execute("INSERT INTO items (id, batch_id, seq, status, dir, note, created_at, updated_at) "
                          "VALUES (?,?,?,?,?,?,?,?)", (iid, batch_id, seq, "new", dir_, note, now(), now()))
        return iid

    def set_item(self, iid: str, **fields: Any) -> None:
        self._update("items", "id", iid, fields)

    def item(self, iid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()

    def items(self, status: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM items WHERE status=? ORDER BY created_at, seq", (status,)).fetchall()

    def recent_covers(self, since_iso: str, exclude: str | None = None) -> list[sqlite3.Row]:
        """(id, cover_hash, renders) of items created since `since_iso` that have a cover hash: the re-share check."""
        return self.conn.execute(
            "SELECT id, cover_hash, renders FROM items WHERE cover_hash IS NOT NULL AND created_at >= ? AND id != ? "
            "ORDER BY created_at DESC", (since_iso, exclude or "")).fetchall()

    def listings_for(self, iid: str) -> list[sqlite3.Row]:
        """The item's rows, in the order the poster takes the marketplaces: Poshmark, Depop, Vinted."""
        return self.conn.execute(
            "SELECT * FROM listings WHERE item_id=? ORDER BY CASE marketplace WHEN 'poshmark' THEN 0 WHEN 'depop' THEN 1 "
            "WHEN 'vinted' THEN 2 ELSE 3 END", (iid,)).fetchall()

    # key/value (the Telegram getUpdates offset)
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, value))

    # outbox: Telegram messages that expect a reply
    def add_outbox(self, chat_id: str, message_id: int, kind: str, ref: str, text: str | None = None) -> None:
        """`text` keeps what was asked (an owner_q question) so resend_pending can send it again."""
        self.conn.execute("INSERT OR REPLACE INTO outbox (chat_id, message_id, kind, ref, text, sent_at, resolved_at) "
                          "VALUES (?,?,?,?,?,?,NULL)", (str(chat_id), int(message_id), kind, ref, text, now()))

    def outbox_lookup(self, chat_id: str, message_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM outbox WHERE chat_id=? AND message_id=?",
                                 (str(chat_id), int(message_id))).fetchone()

    def outbox_pending(self, older_than_iso: str | None = None) -> list[sqlite3.Row]:
        """The latest unresolved message per (kind, ref), optionally only those sent before `older_than_iso`."""
        rows = self.conn.execute(
            "SELECT o.* FROM outbox o JOIN (SELECT kind, ref, MAX(sent_at) AS last FROM outbox WHERE resolved_at IS NULL "
            "GROUP BY kind, ref) m ON o.kind=m.kind AND o.ref=m.ref AND o.sent_at=m.last WHERE o.resolved_at IS NULL "
            "ORDER BY o.sent_at").fetchall()
        return [r for r in rows if older_than_iso is None or r["sent_at"] < older_than_iso]

    def outbox_resolve(self, kind: str, ref: str) -> None:
        self.conn.execute("UPDATE outbox SET resolved_at=? WHERE kind=? AND ref=? AND resolved_at IS NULL",
                          (now(), kind, ref))

    # listings (WO30): one row per item and marketplace
    def listing(self, iid: str, mp: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM listings WHERE item_id=? AND marketplace=?", (iid, mp)).fetchone()

    def upsert_listing(self, iid: str, mp: str, **fields: Any) -> None:
        """Create the row if needed, then set `fields`. With no fields it is just a touch of updated_at."""
        if self.listing(iid, mp) is None:
            self.conn.execute("INSERT INTO listings (item_id, marketplace, status, updated_at) VALUES (?,?,?,?)",
                              (iid, mp, fields.get("status", "queued"), now()))
        vals = [json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for v in fields.values()]
        sets = ", ".join(f"{k}=?" for k in [*fields, "updated_at"])
        self.conn.execute(f"UPDATE listings SET {sets} WHERE item_id=? AND marketplace=?",
                          (*vals, now(), iid, mp))

    def claim_listing(self, iid: str, mp: str, count: bool = True) -> bool:
        """Atomically take (item, marketplace) for posting: True for exactly one caller.

        The single conditional UPDATE is the lock — two poster processes can never both open the form for one
        item (invariant 4). Only 'queued' and 'dryrun' rows are claimable; 'posting' (crashed mid-form),
        'posted', 'drafted', 'failed' and 'skipped' rows are refused and need a human to reconcile against the closet.
        `count`: the attempt is counted here (the Playwright posters: claimed before the form opens); the extension
        driver counts it when the job starts (begin_attempt) and claims at the go-ahead (WO32b).
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO listings (item_id, marketplace, status, updated_at) VALUES (?,?,'queued',?)",
            (iid, mp, now()))
        cur = self.conn.execute(
            f"UPDATE listings SET status='posting', {'attempts=attempts+1, ' if count else ''}error=NULL, updated_at=? "
            "WHERE item_id=? AND marketplace=? AND status IN ('queued','dryrun')",
            (now(), iid, mp))
        return cur.rowcount == 1

    def begin_attempt(self, iid: str, mp: str) -> bool:
        """An extension job starts on (item, marketplace) (WO32b): the attempt counted, the row left as it is — it
        becomes 'posting' only at the go-ahead (claim_listing). True when the row is one the poster may take."""
        self.conn.execute(
            "INSERT OR IGNORE INTO listings (item_id, marketplace, status, updated_at) VALUES (?,?,'queued',?)",
            (iid, mp, now()))
        cur = self.conn.execute(
            "UPDATE listings SET attempts=attempts+1, error=NULL, updated_at=? "
            "WHERE item_id=? AND marketplace=? AND status IN ('queued','dryrun')", (now(), iid, mp))
        return cur.rowcount == 1

    def listed_since(self, since_iso: str, mp: str | None = None) -> int:
        """Rows that hit the site since `since_iso` — dry-runs included — on `mp`, or on all marketplaces. A dry-run
        fills the real create form, photo uploads and all, so it counts against the caps like a publish (invariant
        6). WO30: the caps are per marketplace."""
        sql = "SELECT COUNT(*) FROM listings WHERE status IN ('posted','drafted','dryrun') AND posted_at >= ?"
        args: tuple = (since_iso,)
        if mp is not None:
            sql, args = sql + " AND marketplace=?", (since_iso, mp)
        return self.conn.execute(sql, args).fetchone()[0]

    def counts_by_marketplace(self) -> dict[str, dict[str, int]]:
        """{marketplace: {status: n}} for `thrift status` (WO30)."""
        out: dict[str, dict[str, int]] = {}
        for mp, st, n in self.conn.execute("SELECT marketplace, status, COUNT(*) FROM listings GROUP BY 1, 2"):
            out.setdefault(mp, {})[st] = n
        return out

    def _update(self, table: str, key: str, value: str, fields: dict[str, Any]) -> None:
        if not fields:
            return
        vals = [json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for v in fields.values()]
        sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
        self.conn.execute(f"UPDATE {table} SET {sets} WHERE {key}=?", (*vals, now(), value))


def loads(v: str | None) -> Any:
    return json.loads(v) if v else None
