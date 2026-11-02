"""WO52 follow-up (the owner, 2026-11-01): a Vinted listing with no brand deleted and posted again with it — only with
no likes, offers or conversations; the listing keeps its price-drop clock; the card follows."""
from __future__ import annotations

import asyncio
import json

import pytest

from thrift_agent import crosslist, notify, vinted_repost
from thrift_agent.config import Settings
from thrift_agent.db import DB

OLD = "https://www.vinted.com/items/10562417161"
NEW = "https://www.vinted.com/items/10700000001"


@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "state.db")
    iid = d.add_item("b1", 1, str(tmp_path))
    d.set_item(iid, status="posted", renders={"poshmark": {"title": "Madewell Perfect Vintage Jean", "brand": "Madewell"}})
    d.upsert_listing(iid, "vinted", status="posted", url=OLD, listing_id="10562417161",
                     posted_at="2026-10-27T20:03:06+00:00", price=40, fields_json={"brand": "Madewell"})
    d.conn.commit()
    d.iid = iid
    return d


@pytest.fixture
def said(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "say", out.append)
    return out


class Fake:
    def __init__(self, info):
        self.info, self.deleted = info, []

    async def vinted_interest(self, url):
        return self.info

    async def vinted_delete(self, url, text):
        self.deleted.append((url, text))
        return True

    async def vinted_practice_delete(self, url):
        return {"window": {"buttons": ["Cancel", "Delete"]}, "closed": True}


FREE = {"gone": False, "likes": 0, "views": 12, "conversations": 0, "reserved": False}


@pytest.mark.parametrize("info,why", [
    (FREE, None),
    ({**FREE, "likes": 2}, "2 likes"),
    ({**FREE, "conversations": 1}, "1 conversation"),
    ({**FREE, "conversations": None, "inbox_error": "HTTP 403"}, "its conversations couldn't be read (HTTP 403)"),
    ({**FREE, "reserved": True}, "it is reserved"),
])
def test_only_a_listing_nobody_showed_interest_in_is_free(info, why):
    assert vinted_repost.interested(info) == why


def test_practice_then_go_deletes_once_and_queues_the_row(db, said):
    poster = Fake(FREE)
    vinted_repost.ask(db, db.iid, "practice")
    asyncio.run(vinted_repost.serve(db, {"vinted": poster}))
    assert vinted_repost.confirm_text(db) == "Delete"
    vinted_repost.ask(db, db.iid, "go")
    asyncio.run(vinted_repost.serve(db, {"vinted": poster}))
    assert poster.deleted == [(OLD, "Delete")]
    row = db.listing(db.iid, "vinted")
    assert row["status"] == "queued" and row["url"] is None and row["attempts"] == 0


def test_go_without_a_practice_or_with_a_like_deletes_nothing(db, said):
    poster = Fake({**FREE, "likes": 1})
    vinted_repost.ask(db, db.iid, "go")
    asyncio.run(vinted_repost.serve(db, {"vinted": poster}))
    assert poster.deleted == [] and "run --practice first" in said[-1]
    db.kv_set(vinted_repost.WINDOW, json.dumps({"buttons": ["Cancel", "Delete"]}))
    vinted_repost.ask(db, db.iid, "go")
    asyncio.run(vinted_repost.serve(db, {"vinted": poster}))
    assert poster.deleted == [] and "stays as it is: 1 like" in said[-1]
    assert db.listing(db.iid, "vinted")["status"] == "posted"


def test_posted_again_keeps_the_price_drop_clock_and_edits_the_card(db, said, monkeypatch):
    cards = []
    monkeypatch.setattr(crosslist, "refresh_card", lambda s, d, iid: cards.append(iid))
    db.kv_set(vinted_repost.WINDOW, json.dumps({"buttons": ["Cancel", "Delete"]}))
    vinted_repost.ask(db, db.iid, "go")
    asyncio.run(vinted_repost.serve(db, {"vinted": Fake(FREE)}))
    db.upsert_listing(db.iid, "vinted", status="posted", url=NEW, posted_at="2026-11-02T01:00:00+00:00")
    assert vinted_repost.after_posted(Settings({"machine_role": "dev"}), db, db.iid, "vinted")
    row = db.listing(db.iid, "vinted")
    fields = json.loads(row["fields_json"])
    assert row["posted_at"] == "2026-10-27T20:03:06+00:00"                  # the clock never restarts
    assert fields["reposted_from"] == OLD and fields["reposted_at"] == "2026-11-02T01:00:00+00:00"
    assert cards == [db.iid] and NEW in said[-1] and OLD in said[-1]
    assert not vinted_repost.after_posted(Settings({"machine_role": "dev"}), db, db.iid, "vinted")   # once


def test_a_delete_that_fails_keeps_nothing_and_the_old_listing_is_never_the_new_one(db, said):
    """Live 2026-11-01 (Talbots): the delete job never reached the listing; the 'before' record had been kept already
    and the sweep took the OLD listing for the reposted one — a wrong line in the private chat."""
    class Failing(Fake):
        async def vinted_delete(self, url, text):
            raise RuntimeError("Vinted delete: not a listing page: https://www.vinted.com/items/new")
    db.kv_set(vinted_repost.WINDOW, json.dumps({"buttons": ["cancel", "delete"]}))
    vinted_repost.ask(db, db.iid, "go")
    asyncio.run(vinted_repost.serve(db, {"vinted": Failing(FREE)}))
    assert not db.kv_get(vinted_repost.BEFORE + db.iid)
    assert db.listing(db.iid, "vinted")["status"] == "posted"
    db.kv_set(vinted_repost.BEFORE + db.iid, json.dumps({"url": OLD, "posted_at": "2026-10-27T20:03:06+00:00"}))
    assert not vinted_repost.after_posted(Settings({"machine_role": "dev"}), db, db.iid, "vinted")
