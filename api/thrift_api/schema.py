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
)

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
)


def migrate(db: Database) -> None:
    """Every table and index, created when missing; the schema's version in settings."""
    with db.tx():
        if db.kind == "postgres":
            db.execute(f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK})")
        for statement in (*TABLES, *INDEXES):
            db.execute(statement)
        db.execute("INSERT INTO settings (key, value) VALUES ('schema_version', ?) "
                   "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (str(VERSION),))
