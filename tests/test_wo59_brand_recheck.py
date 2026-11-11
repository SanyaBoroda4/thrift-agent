"""WO59 §5 (the owner, 2026-11-10: "Post on Poshmark anyway, with no brand. The brand still goes in the title and
description"): a Poshmark listing the agent posted with its brand field empty is looked for again once a week — on a
create-listing form left through Discard, so no brand edit is spent on a look — and the field is set through ONE
brand edit once Poshmark lists the brand; never twice, never a listing whose brand she cleared."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from thrift_agent import brandcheck, notify
from thrift_agent.config import Settings
from thrift_agent.db import DB
from thrift_agent.post import runner

NOW = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)
URL = "https://poshmark.com/listing/Mango-Linen-Wrap-Dress-Beige-size-M-6ae7a55198bbf3ba57153844"


LIVE = {"brand": None}                       # the listing's public page, as the look reads it first


def _db(tmp_path, said, monkeypatch):
    monkeypatch.setattr(notify, "say", said.append)
    from thrift_agent import crosslist
    LIVE["brand"] = None
    monkeypatch.setattr(crosslist, "poshmark_listing", lambda url: dict(LIVE))
    db = DB(tmp_path / "state.db")
    iid = db.add_item("b1", 1, str(tmp_path))
    db.set_item(iid, status="posted", facts={"brand": {"value": "Mango"}},
                renders={"poshmark": {"marketplace": "poshmark", "title": "Mango Linen Wrap Dress Beige size M",
                                      "description": "Linen wrap dress.\n\nBrand: Mango", "brand": "Mango",
                                      "department": "Women", "category": "Dresses", "subcategory": "Midi",
                                      "size": "M", "colors": ["Beige"], "condition": "good", "price": 30,
                                      "photos": ["00.jpg"], "sku": iid}})
    for mp in ("poshmark", "depop", "vinted"):
        db.upsert_listing(iid, mp, status="posted", url=URL if mp == "poshmark" else f"https://{mp}.example/l/1")
    db.conn.commit()
    return db, iid


class Probe:
    """The poster's brand_known: what Poshmark's create form shows for the brand, each look counted."""

    def __init__(self, found=None):
        self.found, self.looks = found, 0

    async def brand_known(self, ctx, r):
        self.looks += 1
        return self.found, None


def _run(s, db, poster, now):
    return asyncio.run(brandcheck.run_one(s, db, poster, None, now=now))


def test_a_post_with_the_brand_field_left_empty_is_looked_at_weekly_and_set_once_found(tmp_path, monkeypatch):
    said = []
    db, iid = _db(tmp_path, said, monkeypatch)
    s = Settings({"machine_role": "dev"})
    out = SimpleNamespace(status="posted", url=URL, error=None, screenshot=None, note=None, draft_left=None,
                          clicked=True, guesses=["Poshmark: 'Mango' isn't in Poshmark's brand list — posted with the "
                                                 "brand in the title and description"], landing=None)
    if brandcheck.empty_by_us(out.guesses):                 # what runner.record does after a Poshmark post
        brandcheck.add(db, iid, "Mango", now=NOW)
    assert brandcheck.due(db, NOW) == [iid]
    probe = Probe(found=None)
    assert _run(s, db, probe, NOW) and probe.looks == 1            # not on Poshmark's list yet
    assert brandcheck.due(db, NOW + timedelta(days=6)) == []         # a week before the next look
    assert db.conn.execute("SELECT COUNT(*) FROM listing_edits").fetchone()[0] == 0
    probe.found = "Mango"                                             # Poshmark added it
    assert brandcheck.due(db, NOW + timedelta(days=7)) == [iid]
    assert _run(s, db, probe, NOW + timedelta(days=7))
    rows = db.conn.execute("SELECT marketplace, kind, payload FROM listing_edits").fetchall()
    assert [(r["marketplace"], r["kind"]) for r in rows] == [("poshmark", "brand")]   # Poshmark only, ONE edit
    assert "Mango" in json.loads(rows[0]["payload"])["brand_names"]
    assert any("Poshmark lists 'Mango' now" in m for m in said)
    assert brandcheck.due(db, NOW + timedelta(days=30)) == []        # at most one of its three brand edits, ever
    assert not _run(s, db, probe, NOW + timedelta(days=30)) and probe.looks == 2


def test_only_the_agents_own_empty_posts_are_seeded_never_one_she_cleared(tmp_path, monkeypatch):
    said = []
    db, iid = _db(tmp_path, said, monkeypatch)
    other = db.add_item("b1", 2, str(tmp_path / "x"))
    db.set_item(other, status="posted", renders={"poshmark": {"brand": "Zara", "title": "Zara Blazer"}})
    db.upsert_listing(other, "poshmark", status="posted", url="https://poshmark.com/listing/Zara-Blazer-6aaa")
    db.log(iid, "post_posted", {"url": URL, "guesses": ["brand left empty: Poshmark's list has no 'Mango'"]})
    db.log(other, "post_posted", {"url": "https://poshmark.com/listing/Zara-Blazer-6aaa", "guesses": []})
    assert brandcheck.seed(db) == 1 and set(brandcheck.entries(db)) == {iid}
    assert brandcheck.seed(db) == 0                                  # once


def test_a_listing_whose_brand_she_removed_is_never_looked_at_again(tmp_path, monkeypatch):
    said = []
    db, iid = _db(tmp_path, said, monkeypatch)
    s = Settings({"machine_role": "dev"})
    brandcheck.add(db, iid, "Mango", now=NOW)
    it = db.item(iid)
    renders = json.loads(it["renders"])
    renders["poshmark"]["brand"] = None                              # her 'no brand'
    db.set_item(iid, renders=renders)
    probe = Probe(found="Mango")
    assert _run(s, db, probe, NOW) and probe.looks == 0
    assert brandcheck.due(db, NOW + timedelta(days=60)) == []
    assert db.conn.execute("SELECT COUNT(*) FROM listing_edits").fetchone()[0] == 0


def test_the_poster_records_a_post_with_an_empty_brand_field(tmp_path, monkeypatch):
    """runner.record_outcome: a Poshmark post whose check line says the brand isn't on Poshmark's list is entered for the weekly
    look (and one that found its brand isn't)."""
    import inspect
    src = inspect.getsource(runner.record_outcome)
    assert "brandcheck.empty_by_us(out.guesses)" in src and "brandcheck.add(db, iid, render.brand)" in src
    assert brandcheck.empty_by_us(["Poshmark: 'Mango' isn't in Poshmark's brand list — posted with the brand in the "
                                   "title and description"])
    assert not brandcheck.empty_by_us(["brand set to 'J. Crew' (from 'J.Crew')"])


def test_a_listing_that_shows_a_brand_now_is_retired_without_a_look(tmp_path, monkeypatch):
    """Live 2026-11-11 12:19 AM: three listings seeded from their empty posts had their brand set since (WO52's
    corrections: Astr, Zara) — a look there would spend a brand edit re-setting it. The listing is read first; a
    brand showing → never looked at, never edited. One look an hour at most."""
    said = []
    db, iid = _db(tmp_path, said, monkeypatch)
    s = Settings({"machine_role": "dev"})
    brandcheck.add(db, iid, "Mango", now=NOW)
    LIVE["brand"] = "Mango"
    probe = Probe(found="Mango")
    assert _run(s, db, probe, NOW) and probe.looks == 0
    assert brandcheck.due(db, NOW + timedelta(days=60)) == []
    assert db.conn.execute("SELECT COUNT(*) FROM listing_edits").fetchone()[0] == 0
    other = db.add_item("b1", 3, str(tmp_path / "o"))
    full = {**json.loads(db.item(iid)["renders"])["poshmark"], "brand": "TOPTEN", "title": "Topten Vest", "sku": other}
    db.set_item(other, status="posted", renders={"poshmark": full})
    db.upsert_listing(other, "poshmark", status="posted", url=URL + "x")
    brandcheck.add(db, other, "TOPTEN", now=NOW)
    LIVE["brand"] = None
    assert not _run(s, db, probe, NOW + timedelta(minutes=30))         # within the hour: no second look
    assert _run(s, db, probe, NOW + timedelta(minutes=61)) and probe.looks == 1
