"""WO52 §1 (live 2026-11-01): a sub-line falls back to its parent ("Zara TRF Collection" → Zara), "the Label" drops
("ASTR the Label" → ASTR), a big brand never ends as "no brand", and a live listing's brand is fixed on its edit page."""
from __future__ import annotations

import json

import pytest

from thrift_agent import brands, edits
from thrift_agent.config import Settings
from thrift_agent.db import DB
from thrift_agent.post.ext_driver import BrandOf


@pytest.mark.parametrize("name,expect", [
    ("Zara TRF Collection", ["Zara TRF Collection", "Zara TRF", "Zara"]),
    ("ASTR the Label", ["ASTR the Label", "ASTR"]),
    ("Free People Movement", ["Free People Movement", "Free People"]),
    ("Lauren Ralph Lauren", ["Lauren Ralph Lauren"]),
    ("J. Crew Factory", ["J. Crew Factory", "J. Crew"]),
    ("Forever 21", ["Forever 21"]),
])
def test_the_names_tried_end_with_the_parent(name, expect):
    assert brands.candidates(name) == expect


@pytest.mark.parametrize("name,is_big", [("Zara TRF Collection", True), ("Forever 21", True), ("Nike", True),
                                         ("Madewell", True), ("Relibeauty", False), ("ASTR the Label", False)])
def test_big_brands(name, is_big):
    assert brands.big(name) is is_big


def test_a_big_brand_never_reads_back_as_no_brand():
    names = brands.candidates("Zara TRF Collection")
    big = BrandOf(names, [])
    assert big.matches("Zara") and not big.matches("List without brand") and not big.matches("Other")
    small = BrandOf(brands.candidates("Relibeauty"), ["Other"])
    assert small.matches("Other")


def _db_with_live(tmp_path):
    db = DB(tmp_path / "state.db")
    iid = db.add_item("b1", 1, str(tmp_path))
    db.set_item(iid, status="posted", renders={"poshmark": {"title": "Zara TRF Collection Moto Jacket",
                                                            "brand": "Zara TRF Collection"}},
                facts={"brand": {"value": "Zara TRF Collection"}})
    for mp in ("poshmark", "depop", "vinted"):
        db.upsert_listing(iid, mp, status="posted", url=f"https://{mp}.example/l/1")
    db.conn.commit()
    return db, iid


def test_queue_brand_makes_one_edit_per_live_site(tmp_path):
    db, iid = _db_with_live(tmp_path)
    s = Settings({"machine_role": "dev"})
    assert len(edits.queue_brand(s, db, iid)) == 3
    rows = {r["marketplace"]: json.loads(r["payload"]) for r in db.conn.execute(
        "SELECT marketplace, payload FROM listing_edits WHERE kind='brand'")}
    assert rows["vinted"]["fields"]["brand_candidates"][-1] == "Zara"
    assert rows["vinted"]["fields"]["brand_big"] is True
    assert "Zara" in rows["poshmark"]["brand_names"]
    assert edits.queue_brand(s, db, iid) == []          # never two in flight


def test_the_edit_read_back_wants_one_of_our_names():
    from thrift_agent.post.ext_driver import ExtensionPoster
    p = ExtensionPoster.__new__(ExtensionPoster)
    f = {"brand": "Zara TRF Collection", "brand_candidates": brands.candidates("Zara TRF Collection")}
    assert p._fields_match(f, {"brand": "Zara"})
    assert not p._fields_match(f, {"brand": "List without brand"})


def test_a_petite_waist_maps_on_depop():
    """Depop 88 against 89 (2026-11-01): the Madewell jeans (size 27P) were skipped on Depop since Oct 27."""
    from thrift_agent import catalogs
    from thrift_agent.catalogs.sizes import SizeIn, depop_size
    cat = catalogs.depop_catalog()
    sid = cat.size_set_id("Women > Bottoms > Jeans")
    v, note = depop_size(SizeIn("Women", "Jeans", "Wide Leg", "wide-leg jeans", "27P", "Petite", "27P"),
                         cat.size_set(sid), cat.size_sets_US[sid].path)
    assert v == '27"' and "27P" in note
