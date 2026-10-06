"""listings table (WO30; the `posts` table before it): upsert, the atomic claim that keeps two posters off one item,
pacing counts, and the one-time copy of the old `posts` rows."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from thrift_agent.db import DB, listing_id_from


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "state.db")


def test_upsert_post_with_no_fields_is_a_touch(db):
    db.upsert_listing("i_1", "poshmark")
    row = db.listing("i_1", "poshmark")
    assert row["status"] == "queued" and row["attempts"] == 0 and row["updated_at"]
    db.upsert_listing("i_1", "poshmark")                       # second touch: no SQL error, still one row
    assert db.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1


def test_upsert_post_insert_then_update_keeps_attempts(db):
    db.upsert_listing("i_1", "poshmark", status="posting", attempts=2)
    db.upsert_listing("i_1", "poshmark", status="drafted", url="https://x/listing/1")
    row = db.listing("i_1", "poshmark")
    assert (row["status"], row["attempts"], row["url"]) == ("drafted", 2, "https://x/listing/1")


def test_upsert_listing_stores_the_mapped_fields_as_json(db):
    db.upsert_listing("i_1", "vinted", status="queued", price=35, fields_json={"category_id": 5523, "size_id": 3})
    row = db.listing("i_1", "vinted")
    assert row["price"] == 35 and json.loads(row["fields_json"]) == {"category_id": 5523, "size_id": 3}


def test_claim_post_succeeds_exactly_once(db):
    assert db.claim_listing("i_1", "poshmark") is True
    row = db.listing("i_1", "poshmark")
    assert (row["status"], row["attempts"], row["error"]) == ("posting", 1, None)
    assert db.claim_listing("i_1", "poshmark") is False   # a second poster process loses the race
    assert db.listing("i_1", "poshmark")["attempts"] == 1


def test_claim_post_reclaims_queued_and_dryrun_rows(db):
    db.upsert_listing("i_1", "poshmark", status="queued", error="earlier: not logged in")
    assert db.claim_listing("i_1", "poshmark") is True
    row = db.listing("i_1", "poshmark")
    assert (row["status"], row["attempts"], row["error"]) == ("posting", 1, None)

    db.upsert_listing("i_2", "poshmark", status="dryrun", attempts=3)
    assert db.claim_listing("i_2", "poshmark") is True
    assert db.listing("i_2", "poshmark")["attempts"] == 4


@pytest.mark.parametrize("status", ["posting", "posted", "drafted", "failed", "skipped"])
def test_claim_post_refuses_rows_that_need_a_human(db, status):
    """'posting' after a crash means: reconcile against the closet, never re-post blindly (invariant 4)."""
    db.upsert_listing("i_1", "poshmark", status=status, attempts=1, url="https://x/listing/1")
    assert db.claim_listing("i_1", "poshmark") is False
    row = db.listing("i_1", "poshmark")
    assert (row["status"], row["attempts"], row["url"]) == (status, 1, "https://x/listing/1")


def test_claim_post_is_per_marketplace(db):
    assert db.claim_listing("i_1", "poshmark") is True
    assert db.claim_listing("i_1", "depop") is True
    assert db.claim_listing("i_1", "depop") is False


def test_posted_since_counts_dry_runs(db):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(minutes=10)).isoformat(timespec="seconds")
    old = (now - timedelta(hours=3)).isoformat(timespec="seconds")
    since = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    db.upsert_listing("i_1", "poshmark", status="posted", posted_at=recent)
    db.upsert_listing("i_2", "poshmark", status="drafted", posted_at=recent)
    db.upsert_listing("i_3", "poshmark", status="dryrun", posted_at=recent)    # filled the real form: counts
    db.upsert_listing("i_4", "poshmark", status="dryrun", posted_at=old)
    db.upsert_listing("i_5", "poshmark", status="failed", posted_at=None)
    db.upsert_listing("i_6", "poshmark", status="queued")
    db.upsert_listing("i_7", "poshmark", status="posting", posted_at=None)
    assert db.listed_since(since) == 3
    assert db.listed_since((now - timedelta(hours=4)).isoformat(timespec="seconds")) == 4


def test_listed_since_counts_one_marketplace(db):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(minutes=10)).isoformat(timespec="seconds")
    since = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    db.upsert_listing("i_1", "poshmark", status="posted", posted_at=recent)
    db.upsert_listing("i_1", "depop", status="posted", posted_at=recent)
    db.upsert_listing("i_2", "depop", status="dryrun", posted_at=recent)
    assert (db.listed_since(since, "poshmark"), db.listed_since(since, "depop"), db.listed_since(since, "vinted"),
            db.listed_since(since)) == (1, 2, 0, 3)


def test_listings_for_orders_poshmark_depop_vinted(db):
    for mp in ("vinted", "poshmark", "depop"):
        db.upsert_listing("i_1", mp)
    assert [r["marketplace"] for r in db.listings_for("i_1")] == ["poshmark", "depop", "vinted"]
    assert db.counts_by_marketplace() == {"depop": {"queued": 1}, "poshmark": {"queued": 1}, "vinted": {"queued": 1}}


def _old_db(path):
    """A state.db as WO29 left it: the `posts` table, no `listings`."""
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE items (id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, seq INTEGER NOT NULL, status TEXT NOT NULL,
          dir TEXT NOT NULL, note TEXT, facts TEXT, price TEXT, renders TEXT, gate TEXT, created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, cover_hash TEXT, owner_price INTEGER);
        CREATE TABLE posts (item_id TEXT NOT NULL, marketplace TEXT NOT NULL, status TEXT NOT NULL, mode TEXT,
          url TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, posted_at TEXT, updated_at TEXT NOT NULL,
          PRIMARY KEY (item_id, marketplace));
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
    """)
    url = "https://poshmark.com/listing/Levis-501-Cutoff-Shorts-size-26-6ac111490000000000000a01"
    c.execute("INSERT INTO items (id, batch_id, seq, status, dir, renders, created_at, updated_at, owner_price) VALUES "
              "('i_1','b_1',1,'posted','d',?, 't','t',30)", (json.dumps({"poshmark": {"price": 30}}),))
    c.execute("INSERT INTO items (id, batch_id, seq, status, dir, created_at, updated_at) VALUES "
              "('i_2','b_1',2,'ready','d','t','t')")
    c.execute("INSERT INTO posts VALUES ('i_1','poshmark','posted','publish',?,1,NULL,'2026-10-05T19:51:00+00:00',"
              "'2026-10-05T19:51:00+00:00')", (url,))
    c.execute("INSERT INTO posts VALUES ('i_2','poshmark','failed','publish',NULL,1,'skipped: no size',NULL,'t')")
    c.commit()
    c.close()
    return url


def test_the_old_posts_rows_become_listings_once(tmp_path):
    """WO30 migration: every Poshmark-posted item keeps its poshmark/posted row with the URL (and its id and price); a
    skip becomes 'skipped'; the old table is left as it was; opening the DB again copies nothing twice."""
    path = tmp_path / "state.db"
    url = _old_db(path)
    db = DB(path)
    posted = db.listing("i_1", "poshmark")
    assert (posted["status"], posted["url"], posted["listing_id"], posted["price"], posted["posted_at"]) == (
        "posted", url, "6ac111490000000000000a01", 30, "2026-10-05T19:51:00+00:00")
    skipped = db.listing("i_2", "poshmark")
    assert (skipped["status"], skipped["error"]) == ("skipped", "skipped: no size")
    assert db.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 2           # untouched
    assert db.kv_get("listings_migrated")
    db.upsert_listing("i_1", "depop", status="queued")
    db.conn.execute("UPDATE listings SET status='sold' WHERE item_id='i_1' AND marketplace='poshmark'")
    again = DB(path)                                                                   # a second process, a restart
    assert again.listing("i_1", "poshmark")["status"] == "sold"                       # never copied over again
    assert again.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 3


def test_a_new_db_has_no_posts_table(db):
    assert not db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='posts'").fetchone()


def test_listing_ids_from_addresses():
    assert listing_id_from("poshmark", "https://poshmark.com/listing/Tee-size-M-6ac111490000000000000a01") == \
        "6ac111490000000000000a01"
    assert listing_id_from("vinted", "https://www.vinted.com/items/7012345678-levis-501-shorts?referrer=x") == \
        "7012345678"
    assert listing_id_from("depop", "https://www.depop.com/products/shopname-levis-501-shorts-1a2b/") == \
        "shopname-levis-501-shorts-1a2b"
    assert listing_id_from("poshmark", None) is None and listing_id_from("ebay", "https://x/1") is None
