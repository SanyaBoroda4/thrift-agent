"""The tables (WO33) and `migrate(db)`: CREATE … IF NOT EXISTS only, so it runs at every start and twice changes
nothing. The same DDL for SQLite and Postgres: TEXT, INTEGER and DOUBLE PRECISION (SQLite reads it as REAL; Postgres's
own REAL is single precision, which would turn $35.99 into 35.9900016784668).

What is never stored: a buyer's name, username or address in any parsed field. The email's text (`raw_text`) is kept
for 30 days at most (timers.daily) and never for an email of kind OTHER; `samples` are the developer's examples."""
from __future__ import annotations

from .db import Database

VERSION = 1
MIGRATION_LOCK = 7_227_103_301          # pg_advisory_xact_lock: two instances starting at once migrate one at a time

TABLES = (
    """CREATE TABLE IF NOT EXISTS items (
        id TEXT PRIMARY KEY, title TEXT, brand TEXT, size TEXT, price DOUBLE PRECISION,
        created_at TEXT, updated_at TEXT)""",
    # status as the Mac's: queued, posting, posted, failed, skipped, delisted, sold, dryrun, drafted
    """CREATE TABLE IF NOT EXISTS listings (
        item_id TEXT NOT NULL, marketplace TEXT NOT NULL, status TEXT, url TEXT, listing_id TEXT, sku TEXT,
        price DOUBLE PRECISION, posted_at TEXT, updated_at TEXT,
        PRIMARY KEY (item_id, marketplace))""",
    # kind SALE | SHIPPED | DELIVERED | CANCELLED | OTHER; status new | parsed | matched | unmatched | ignored | failed
    """CREATE TABLE IF NOT EXISTS email_events (
        message_id TEXT PRIMARY KEY, thread_id TEXT, marketplace TEXT, kind TEXT, subject TEXT, received_at TEXT,
        parsed_json TEXT, raw_text TEXT, status TEXT NOT NULL, attempts INTEGER DEFAULT 0, created_at TEXT NOT NULL)""",
    # status new | matched | unmatched | delisting | done | double_sale | cancelled; ship_by a "YYYY-MM-DD" day,
    # ship_by_source email | rule
    """CREATE TABLE IF NOT EXISTS sales (
        id TEXT PRIMARY KEY, marketplace TEXT NOT NULL, item_id TEXT, listing_id TEXT, title_seen TEXT,
        price DOUBLE PRECISION, order_id TEXT, sold_at TEXT, ship_by TEXT, ship_by_source TEXT, shipped_at TEXT,
        delivered_at TEXT, status TEXT NOT NULL, message_id TEXT, created_at TEXT NOT NULL)""",
    # sales.status: unmatched | matched | delisting | double_sale | done | cancelled | merged (WO33: a second row of
    # the same sale, folded into the first and kept — never deleted; every list and lookup leaves it out)
    # status pending | running | done | failed | not_found | cancelled; sale_id NULL for a take-down the Mac asked for
    # (mac-event not_for_sale: the item is no longer for sale on one site, no sale of ours behind it)
    """CREATE TABLE IF NOT EXISTS delist_tasks (
        id TEXT PRIMARY KEY, sale_id TEXT, item_id TEXT NOT NULL, marketplace TEXT NOT NULL, listing_url TEXT,
        status TEXT NOT NULL, attempts INTEGER DEFAULT 0, last_error TEXT, evidence TEXT, lease_until TEXT,
        created_at TEXT NOT NULL, done_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS reminders (
        sale_id TEXT NOT NULL, kind TEXT NOT NULL, sent_at TEXT NOT NULL, PRIMARY KEY (sale_id, kind))""",
    """CREATE TABLE IF NOT EXISTS heartbeats (source TEXT PRIMARY KEY, last_seen TEXT NOT NULL, info TEXT)""",
    """CREATE TABLE IF NOT EXISTS samples (
        message_id TEXT PRIMARY KEY, marketplace TEXT, subject TEXT, received_at TEXT, text TEXT)""",
    """CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)""",
    # WO38: each site's sold orders as the Mac's sweep read them — never a buyer's name, address or tracking number;
    # status to_ship | shipped | delivered | completed | cancelled | returned, seen_json {status: first seen at}
    """CREATE TABLE IF NOT EXISTS orders (
        marketplace TEXT NOT NULL, order_id TEXT NOT NULL, sale_id TEXT, item_id TEXT, title TEXT,
        price DOUBLE PRECISION, sold_at TEXT, status TEXT NOT NULL, status_text TEXT, seen_json TEXT,
        last_seen TEXT NOT NULL, first_seen TEXT NOT NULL,
        PRIMARY KEY (marketplace, order_id))""",
    # WO41: the Mac's price drops (one row per item, site and stage auto_1 | auto_2), mirrored by /sync
    """CREATE TABLE IF NOT EXISTS price_changes (
        id TEXT PRIMARY KEY, item_id TEXT NOT NULL, marketplace TEXT NOT NULL, stage TEXT,
        old_price DOUBLE PRECISION, new_price DOUBLE PRECISION, pct DOUBLE PRECISION, status TEXT, attempts INTEGER,
        error TEXT, due_at TEXT, done_at TEXT, created_at TEXT, updated_at TEXT)""",
    # WO45 §2: a share the iPhone uploads straight to the API (uploads.py) — its metadata only, the bytes are blobs
    # (`shares` container, `<share_id>/<name>`); fetched_at once the Mac has it; 30 days (timers.daily)
    """CREATE TABLE IF NOT EXISTS shares_in (
        share_id TEXT PRIMARY KEY, count INTEGER, received INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL,
        ended_at TEXT, fetched_at TEXT, last_received_at TEXT, test INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS share_uploads (
        share_id TEXT NOT NULL, name TEXT NOT NULL, n INTEGER, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
        content_type TEXT, received_at TEXT NOT NULL, test INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (share_id, name))""",
    # WO45 §3: the Mac's share ledger, mirrored by /sync — one row per share id with the time of each step and its
    # outcome (carded | repeat | empty | same_share | failed | joined); the watchdog (watch.shares_watch) and the
    # dashboard's Shares panel read it
    """CREATE TABLE IF NOT EXISTS shares (
        id TEXT PRIMARY KEY, source TEXT, expected INTEGER, received INTEGER, received_at TEXT, complete_at TEXT,
        grouped_at TEXT, carded_at TEXT, outcome TEXT, outcome_at TEXT, reason TEXT, batch_id TEXT, test INTEGER,
        updated_at TEXT)""",
)

# Columns added after a table was first made (WO41), added where missing: Postgres by ADD COLUMN IF NOT EXISTS,
# SQLite by its table_info (it has no IF NOT EXISTS there).
COLUMNS = {
    "items": {"all_live_at": "TEXT", "drop_stage": "INTEGER", "next_drop_at": "TEXT", "no_drop": "TEXT",
              # WO45 §7: the safety net — the Mac's status, whether the owner priced it, a test item, its photos
              "status": "TEXT", "priced": "INTEGER", "test": "INTEGER", "photos": "INTEGER"},
    "listings": {"price_live": "DOUBLE PRECISION", "price_read_at": "TEXT",
                 # WO45 §6/§7: the row's reason, the photos sent and the photos the site shows
                 "error": "TEXT", "photos_sent": "INTEGER", "photos_live": "INTEGER", "photos_read_at": "TEXT"},
    "sales": {"drops": "INTEGER"},              # WO41: the item's price drops made before it sold
    "delist_tasks": {"kind": "TEXT"},           # WO45 §7: NULL / delist = a take-down; repost = the safety net's one retry
}

INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_listings_marketplace ON listings (marketplace, status)",
    "CREATE INDEX IF NOT EXISTS ix_email_events_status ON email_events (status, attempts)",
    "CREATE INDEX IF NOT EXISTS ix_email_events_created ON email_events (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_sales_item ON sales (item_id)",
    "CREATE INDEX IF NOT EXISTS ix_sales_order ON sales (marketplace, order_id)",
    "CREATE INDEX IF NOT EXISTS ix_sales_status ON sales (status)",
    "CREATE INDEX IF NOT EXISTS ix_delist_tasks_status ON delist_tasks (status, marketplace)",
    "CREATE INDEX IF NOT EXISTS ix_delist_tasks_item ON delist_tasks (item_id, marketplace)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_delist_tasks_sale_site ON delist_tasks (sale_id, marketplace)",
    "CREATE INDEX IF NOT EXISTS ix_orders_sale ON orders (sale_id)",
    "CREATE INDEX IF NOT EXISTS ix_price_changes_item ON price_changes (item_id, marketplace)",
    "CREATE INDEX IF NOT EXISTS ix_shares_in_pending ON shares_in (fetched_at, started_at)",         # WO45 §2
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_share_uploads_sha ON share_uploads (share_id, sha256)",  # the same bytes once
)


def migrate(db: Database) -> None:
    """Every table and index, created when missing; the schema's version in settings."""
    with db.tx():
        if db.kind == "postgres":
            db.execute(f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK})")
        for statement in TABLES:
            db.execute(statement)
        for table, columns in COLUMNS.items():
            if db.kind == "postgres":
                for column, kind in columns.items():
                    db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}")
                continue
            have = {row["name"] for row in db.query(f"PRAGMA table_info({table})")}
            for column, kind in columns.items():
                if column not in have:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        for statement in INDEXES:
            db.execute(statement)
        db.execute("INSERT INTO settings (key, value) VALUES ('schema_version', ?) "
                   "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (str(VERSION),))
