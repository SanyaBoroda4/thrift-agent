"""posts table: upsert, the atomic claim that keeps two posters off one item, and pacing counts."""
from datetime import datetime, timedelta, timezone

import pytest

from thrift_agent.db import DB


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "state.db")


def test_upsert_post_with_no_fields_is_a_touch(db):
    db.upsert_post("i_1", "poshmark")
    row = db.post("i_1", "poshmark")
    assert row["status"] == "queued" and row["attempts"] == 0 and row["updated_at"]
    db.upsert_post("i_1", "poshmark")                       # second touch: no SQL error, still one row
    assert db.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1


def test_upsert_post_insert_then_update_keeps_attempts(db):
    db.upsert_post("i_1", "poshmark", status="posting", mode="draft", attempts=2)
    db.upsert_post("i_1", "poshmark", status="drafted", url="https://x/listing/1")
    row = db.post("i_1", "poshmark")
    assert (row["status"], row["mode"], row["attempts"], row["url"]) == ("drafted", "draft", 2, "https://x/listing/1")


def test_claim_post_succeeds_exactly_once(db):
    assert db.claim_post("i_1", "poshmark", "draft") is True
    row = db.post("i_1", "poshmark")
    assert (row["status"], row["mode"], row["attempts"], row["last_error"]) == ("posting", "draft", 1, None)
    assert db.claim_post("i_1", "poshmark", "draft") is False   # a second poster process loses the race
    assert db.post("i_1", "poshmark")["attempts"] == 1


def test_claim_post_reclaims_queued_and_dryrun_rows(db):
    db.upsert_post("i_1", "poshmark", status="queued", last_error="earlier: not logged in")
    assert db.claim_post("i_1", "poshmark", "publish") is True
    row = db.post("i_1", "poshmark")
    assert (row["status"], row["mode"], row["attempts"], row["last_error"]) == ("posting", "publish", 1, None)

    db.upsert_post("i_2", "poshmark", status="dryrun", attempts=3)
    assert db.claim_post("i_2", "poshmark", "draft") is True
    assert db.post("i_2", "poshmark")["attempts"] == 4


@pytest.mark.parametrize("status", ["posting", "posted", "drafted", "failed"])
def test_claim_post_refuses_rows_that_need_a_human(db, status):
    """'posting' after a crash means: reconcile against the closet, never re-post blindly (invariant 4)."""
    db.upsert_post("i_1", "poshmark", status=status, attempts=1, url="https://x/listing/1")
    assert db.claim_post("i_1", "poshmark", "draft") is False
    row = db.post("i_1", "poshmark")
    assert (row["status"], row["attempts"], row["url"]) == (status, 1, "https://x/listing/1")


def test_claim_post_is_per_marketplace(db):
    assert db.claim_post("i_1", "poshmark", "draft") is True
    assert db.claim_post("i_1", "depop", "draft") is True
    assert db.claim_post("i_1", "depop", "draft") is False


def test_posted_since_counts_dry_runs(db):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(minutes=10)).isoformat(timespec="seconds")
    old = (now - timedelta(hours=3)).isoformat(timespec="seconds")
    since = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    db.upsert_post("i_1", "poshmark", status="posted", posted_at=recent)
    db.upsert_post("i_2", "poshmark", status="drafted", posted_at=recent)
    db.upsert_post("i_3", "poshmark", status="dryrun", posted_at=recent)    # filled the real form: counts
    db.upsert_post("i_4", "poshmark", status="dryrun", posted_at=old)
    db.upsert_post("i_5", "poshmark", status="failed", posted_at=None)
    db.upsert_post("i_6", "poshmark", status="queued")
    db.upsert_post("i_7", "poshmark", status="posting", posted_at=None)
    assert db.posted_since(since) == 3
    assert db.posted_since((now - timedelta(hours=4)).isoformat(timespec="seconds")) == 4
