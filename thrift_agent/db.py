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
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
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
"""

# batch: new → segmented | needs_confirm → split | failed
# item:  new → extracted → ready | needs_info → (posting → posted | failed) → sold
# post:  queued → posting → posted | drafted | failed | dryrun


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
        self.conn.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (iid, batch_id, seq, "new", dir_, note, None, None, None, None, now(), now()))
        return iid

    def set_item(self, iid: str, **fields: Any) -> None:
        self._update("items", "id", iid, fields)

    def item(self, iid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()

    def items(self, status: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM items WHERE status=? ORDER BY created_at, seq", (status,)).fetchall()

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
