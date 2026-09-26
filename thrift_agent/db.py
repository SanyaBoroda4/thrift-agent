"""SQLite state. The DB is the source of truth; folders are just inboxes and archives."""
from __future__ import annotations

import json
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
CREATE TABLE IF NOT EXISTS posts (
  item_id TEXT NOT NULL REFERENCES items(id), marketplace TEXT NOT NULL,
  status TEXT NOT NULL, mode TEXT, url TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT, posted_at TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY (item_id, marketplace)
);
CREATE TABLE IF NOT EXISTS events (
  ts TEXT NOT NULL, ref TEXT, kind TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
  chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL,
  sent_at TEXT NOT NULL, resolved_at TEXT,
  PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY, value TEXT
);
"""

# batch: new → needs_confirm → split | failed
# item:  new → awaiting_price | needs_info → ready → (posting → posted | drafted | failed) → sold
#        needs_owner: the poster asked the owner a question; the reply reprocesses the item
# post:  queued → posting → posted | drafted | failed | dryrun   (failed/dryrun with no URL → queued via `thrift requeue`)
# outbox: every Telegram message the agent sent that expects a reply (kind batch | item | owner_q), so a reply or a
#        button press can be mapped back to its batch/item; kv holds the getUpdates offset.

# Columns added after the first release; applied with ALTER TABLE when an older DB is opened.
MIGRATIONS = {"items": {"cover_hash": "TEXT", "owner_price": "INTEGER"}}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now().strftime('%y%m%d')}_{uuid.uuid4().hex[:6]}"


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

    def posts_for(self, iid: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM posts WHERE item_id=? ORDER BY marketplace", (iid,)).fetchall()

    # key/value (the Telegram getUpdates offset)
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, value))

    # outbox: Telegram messages that expect a reply
    def add_outbox(self, chat_id: str, message_id: int, kind: str, ref: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO outbox (chat_id, message_id, kind, ref, sent_at, resolved_at) "
                          "VALUES (?,?,?,?,?,NULL)", (str(chat_id), int(message_id), kind, ref, now()))

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

    # posts
    def post(self, iid: str, mp: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM posts WHERE item_id=? AND marketplace=?", (iid, mp)).fetchone()

    def upsert_post(self, iid: str, mp: str, **fields: Any) -> None:
        """Create the row if needed, then set `fields`. With no fields it is just a touch of updated_at."""
        if self.post(iid, mp) is None:
            self.conn.execute("INSERT INTO posts (item_id, marketplace, status, updated_at) VALUES (?,?,?,?)",
                              (iid, mp, fields.get("status", "queued"), now()))
        sets = ", ".join(f"{k}=?" for k in [*fields, "updated_at"])
        self.conn.execute(f"UPDATE posts SET {sets} WHERE item_id=? AND marketplace=?",
                          (*fields.values(), now(), iid, mp))

    def claim_post(self, iid: str, mp: str, mode: str) -> bool:
        """Atomically take (item, marketplace) for posting: True for exactly one caller.

        The single conditional UPDATE is the lock — two poster processes can never both open the form for one
        item (invariant 4). Only 'queued' and 'dryrun' rows are claimable; 'posting' (crashed mid-form),
        'posted', 'drafted' and 'failed' rows are refused and need a human to reconcile against the closet.
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO posts (item_id, marketplace, status, updated_at) VALUES (?,?,'queued',?)",
            (iid, mp, now()))
        cur = self.conn.execute(
            "UPDATE posts SET status='posting', mode=?, attempts=attempts+1, last_error=NULL, updated_at=? "
            "WHERE item_id=? AND marketplace=? AND status IN ('queued','dryrun')",
            (mode, now(), iid, mp))
        return cur.rowcount == 1

    def posted_since(self, since_iso: str) -> int:
        """Rows that hit the site since `since_iso` — dry-runs included. A dry-run fills the real create form,
        photo uploads and all, so it must count against per_hour_max / daily_cap like a publish (invariant 6)."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE status IN ('posted','drafted','dryrun') AND posted_at >= ?",
            (since_iso,)).fetchone()[0]

    def _update(self, table: str, key: str, value: str, fields: dict[str, Any]) -> None:
        if not fields:
            return
        vals = [json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for v in fields.values()]
        sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
        self.conn.execute(f"UPDATE {table} SET {sets} WHERE {key}=?", (*vals, now(), value))


def loads(v: str | None) -> Any:
    return json.loads(v) if v else None
