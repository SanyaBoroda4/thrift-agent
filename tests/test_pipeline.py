"""End-to-end with the model stubbed out: inbox folder → batch → confirm → items → gate → renders."""
import re
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from thrift_agent import pipeline
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB, loads
from thrift_agent.ingest.segment import Group, SegOut
from thrift_agent.schema import CopyOut, PriceResult, VerifyOut

AUDITED = ("poshmark_title", "poshmark_description", "depop_description")


@pytest.fixture(autouse=True)
def _changed_fields_stub(monkeypatch):
    """copy.changed_fields() lands on another branch. Until it does, compare the audited fields here the same
    way it will: ignoring whitespace and the Depop hashtag line."""
    def changed(draft, audit):
        def norm(text):
            return re.sub(r"(\s*#\w+)+\s*$", "", text).split()
        return [f for f in AUDITED if norm(getattr(draft, f)) != norm(getattr(audit, f))]
    if not hasattr(pipeline.copywriter, "changed_fields"):
        monkeypatch.setattr(pipeline.copywriter, "changed_fields", changed, raising=False)


def fake_ask(facts_factory, audit: VerifyOut | None = None):
    def ask(model, system, content, out, tool, description, **kw):
        if out is SegOut:
            n = sum(1 for c in content if c["type"] == "image")
            return SegOut(groups=[Group(photos=list(range(0, 3)), summary="red flats", full_item_photos=[0],
                                        confidence=0.95),
                                  Group(photos=list(range(3, n)), summary="blue dress", full_item_photos=[3],
                                        confidence=0.95)])
        if out.__name__ == "Facts":
            return facts_factory(photo_order=[0, 1, 2])
        if out is CopyOut:
            return CopyOut(poshmark_title="Tory Burch Red Suede Ballet Flats size 7.5",
                           poshmark_description="Red suede flats.\nCondition: excellent, light sole wear.",
                           poshmark_style_tags=["classic"], depop_description="red tory burch flats",
                           depop_hashtags=["toryburch", "flats", "red", "ballet", "shoes"])
        if out is VerifyOut:
            return audit or VerifyOut(poshmark_title="Tory Burch Red Suede Ballet Flats size 7.5",
                                      poshmark_description="Red suede flats.\nCondition: excellent, light sole wear.",
                                      depop_description="red tory burch flats")
        raise AssertionError(out)
    return ask


def _settle(share):
    """Age every file by an hour so the share counts as quiet. Never rely on a 0 s settle window: on some Windows
    runners a fresh file's mtime is a few ms ahead of time.time() and ready_folders() would skip the share."""
    old = time.time() - 3600
    for f in share.iterdir():
        os.utime(f, (old, old))


def test_end_to_end(tmp_path, monkeypatch, facts):
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    s = Settings(data)
    s.ensure_dirs()
    db = DB(s.path("db"))
    monkeypatch.setattr("thrift_agent.brain.llm.ask", fake_ask(facts))
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml",
                        lambda name: {"brands": {"tory burch": {"target": 70}}, "aliases": {}, "category_defaults": {}})

    share = s.path("inbox") / "2026-09-21_1432"
    share.mkdir()
    t0 = datetime(2026, 9, 21, 14, 32)
    for i, color in enumerate(["red", "darkred", "salmon", "navy", "blue", "skyblue"]):
        img = Image.new("RGB", (300, 400), color)
        for x in range(0, 300, 11 + 5 * i):
            img.paste((255, 255, 255), (x, 0, x + 2, 400))
        exif = Image.Exif()
        exif.get_ifd(0x8769)[36867] = (t0 + timedelta(seconds=4 * i)).strftime("%Y:%m:%d %H:%M:%S")
        img.save(share / f"IMG_{i:04d}.jpg", exif=exif)
    (share / "_done").touch()
    _settle(share)

    [folder] = pipeline.ready_folders(s)
    bid = pipeline.register(s, db, folder)
    pipeline.process_batch(s, db, bid)
    assert db.batch(bid)["status"] == "needs_confirm"        # always_confirm is on by default

    pipeline.confirm(s, db, bid, "ok")
    items = db.items("new")
    assert len(items) == 2 and not share.exists()             # inbox cleared, batch archived
    pipeline.process_item(s, db, items[0]["id"])

    it = db.item(items[0]["id"])
    gate, renders = loads(it["gate"]), loads(it["renders"])
    assert it["status"] == "ready" and gate["decision"] == "publish", gate
    posh = renders["poshmark"]
    assert posh["price"] == 95 and posh["sku"] == it["id"] and posh["photos"][0].endswith("cover.jpg")


def _settings(tmp_path):
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    s = Settings(data)
    s.ensure_dirs()
    return s


def _jpg(path, color="red"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 80), color).save(path)
    return path


def test_archive_only_touches_inbox_folders(tmp_path):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    outside = _jpg(tmp_path / "somewhere" / "fixture" / "a.jpg").parent      # `thrift process <dir>` on a dev folder
    bid = db.add_batch(str(outside), 1)
    assert pipeline.archive_share(s, db, bid, outside) is None and outside.exists()

    inside = _jpg(s.path("inbox") / "2026-09-21_1432" / "a.jpg").parent
    dest = pipeline.archive_share(s, db, bid, inside)
    assert dest and (dest / "a.jpg").exists() and not inside.exists()

    _jpg(inside / "b.jpg")                                                    # same name again, same day
    dest2 = pipeline.archive_share(s, db, bid, inside)
    assert dest2 != dest and (dest2 / "b.jpg").exists() and not (dest / "b.jpg").exists()


def test_archive_failure_is_not_fatal(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    inside = _jpg(s.path("inbox") / "x" / "a.jpg").parent
    bid = db.add_batch(str(inside), 1)

    def boom(*a, **k):
        raise OSError("cross-volume copy failed")
    monkeypatch.setattr(pipeline.shutil, "move", boom)
    assert pipeline.archive_share(s, db, bid, inside) is None and inside.exists()
    kinds = [r["kind"] for r in db.conn.execute("SELECT kind FROM events WHERE ref=?", (bid,))]
    assert "archive_failed" in kinds


def test_process_batch_with_no_photos_fails_clearly(tmp_path):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    share = _jpg(s.path("inbox") / "share" / "a.jpg").parent
    bid = pipeline.register(s, db, share)
    (share / "a.jpg").unlink()                                                # evicted / moved before processing
    with pytest.raises(ValueError, match="no photos"):
        pipeline.process_batch(s, db, bid)


def test_renders_never_repeat_a_photo(tmp_path, facts):
    s = _settings(tmp_path)
    d = tmp_path / "item"
    photos = [_jpg(d / "photos" / f"{i:02d}.jpg", c) for i, c in enumerate(["red", "green", "blue"])]
    f = facts(cover_photo=1, photo_order=[1, 0, 0, 2, 2, 7])                  # repeats and an out-of-range index
    c = CopyOut(poshmark_title="t", poshmark_description="d", poshmark_style_tags=[], depop_description="d",
                depop_hashtags=[])
    pr = PriceResult(target=70, list_price=85, source="brand", by_marketplace={"poshmark": 85})
    r = pipeline.build_renders(s, "i_1", d, photos, f, c, pr)["poshmark"]
    assert r.photos[0].endswith("cover.jpg") and [Path(p).name for p in r.photos[1:]] == ["00.jpg", "02.jpg"]


def _waiting_batch(s, db, groups, n=3, note=None, name="share"):
    """A batch parked at needs_confirm, as process_batch leaves it, with `n` real photos in the inbox."""
    share = s.path("inbox") / name
    photos = [_jpg(share / f"IMG_{i}.jpg", c) for i, c in zip(range(n), ["red", "green", "blue", "gold", "pink"])]
    bid = db.add_batch(str(share), n)
    db.set_batch(bid, status="needs_confirm", segmentation={
        "groups": groups, "summaries": ["x"] * len(groups), "photos": [str(p) for p in photos],
        "dropped": [], "note": note})
    return bid, share


def _item_photos(db, bid):
    return {row["seq"]: sorted(p.name for p in (Path(row["dir"]) / "photos").glob("*.jpg"))
            for row in db.conn.execute("SELECT seq, dir FROM items WHERE batch_id=?", (bid,))}


def test_confirm_rejects_a_grouping_that_drops_or_doubles_a_photo(tmp_path):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    bid, share = _waiting_batch(s, db, [[0, 1]])                              # the model forgot photo 2
    with pytest.raises(ValueError, match=r"photos \[2\] are in no item"):
        pipeline.confirm(s, db, bid, "ok")
    assert db.items("new") == [] and db.batch(bid)["status"] == "needs_confirm" and share.exists()

    for bad in ([[0, 1, 2], [1]], [[0, 1, 2], []]):                            # doubled / empty item
        with pytest.raises(ValueError, match="appear twice|is empty"):
            pipeline.split(s, db, bid, bad)
    assert db.items("new") == [] and db.batch(bid)["status"] == "needs_confirm"

    pipeline.confirm(s, db, bid, "split 2")                                   # the suggested fix works
    assert db.batch(bid)["status"] == "split" and _item_photos(db, bid) == {1: ["00.jpg", "01.jpg"], 2: ["00.jpg"]}
    assert not share.exists()


def test_split_is_all_or_nothing(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    bid, share = _waiting_batch(s, db, [[0, 1], [2]])
    real_add, calls = db.add_item, []

    def flaky_add(*a, **k):
        calls.append(a)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return real_add(*a, **k)
    monkeypatch.setattr(db, "add_item", flaky_add)

    with pytest.raises(RuntimeError, match="disk full"):
        pipeline.confirm(s, db, bid, "ok")
    assert db.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0    # no orphan for the worker
    assert db.batch(bid)["status"] == "needs_confirm" and share.exists()       # still confirmable, not archived
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE kind='item_created'").fetchone()[0] == 0

    pipeline.confirm(s, db, bid, "ok")                                        # re-confirm: exactly one of each
    assert len(db.items("new")) == 2 and db.batch(bid)["status"] == "split" and not share.exists()
    with pytest.raises(ValueError, match="already has items"):
        pipeline.split(s, db, bid, [[0, 1], [2]])


def test_split_reports_a_batch_note_it_could_not_apply(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    said = []
    monkeypatch.setattr(pipeline.notify, "say", said.append)
    bid, _ = _waiting_batch(s, db, [[0, 1], [2]], note="size 8, NWT")
    pipeline.confirm(s, db, bid, "ok")
    assert [it["note"] for it in db.items("new")] == [None, None]
    assert len(said) == 1 and "size 8, NWT" in said[0] and "thrift answer" in said[0]

    said.clear()
    bid2, _ = _waiting_batch(s, db, [[0, 1, 2]], note="size 8, NWT", name="share2")   # one item: unambiguous
    pipeline.confirm(s, db, bid2, "ok")
    assert [it["note"] for it in db.items("new") if it["batch_id"] == bid2] == ["size 8, NWT"] and said == []


def test_process_batch_refuses_more_photos_than_one_model_call_takes(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    share = _jpg(s.path("inbox") / "big" / "IMG_0.jpg").parent
    bid = pipeline.register(s, db, share)
    t0 = datetime(2026, 9, 21, 14, 0)
    fake = [(share / f"IMG_{i}.jpg", t0 + timedelta(seconds=10 * i)) for i in range(91)]
    monkeypatch.setattr(pipeline.prep, "list_photos", lambda src: fake)
    monkeypatch.setattr(pipeline.prep, "normalize", lambda src, dst, edge: dst)
    monkeypatch.setattr(pipeline.prep, "drop_near_duplicates", lambda paths, times, d: (paths, []))

    def no_call(*a, **k):
        raise AssertionError("seg.segment must not be called for an oversized batch")
    monkeypatch.setattr(pipeline.seg, "segment", no_call)
    with pytest.raises(ValueError, match="91 photos after dedupe; the model takes at most 90"):
        pipeline.process_batch(s, db, bid)


def _new_item(s, db):
    share = _jpg(s.path("inbox") / "share" / "a.jpg").parent
    bid = db.add_batch(str(share), 1)
    d = s.path("work") / bid / "item_01"
    for i, c in enumerate(["red", "darkred", "salmon"]):
        _jpg(d / "photos" / f"{i:02d}.jpg", c)
    return db.add_item(bid, 1, str(d))


def test_unreported_verifier_rewrite_goes_to_draft(tmp_path, monkeypatch, facts):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    rewritten = VerifyOut(poshmark_title="Tory Burch Red Suede Ballet Flats size 7.5",
                          poshmark_description="Gorgeous buttery-soft red suede flats, run true to size.",
                          depop_description="red tory burch flats")                   # changed, but unsupported=[]
    monkeypatch.setattr("thrift_agent.brain.llm.ask", fake_ask(facts, audit=rewritten))
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml",
                        lambda name: {"brands": {"tory burch": {"target": 70}}, "aliases": {}, "category_defaults": {}})
    iid = _new_item(s, db)
    pipeline.process_item(s, db, iid)
    gate = loads(db.item(iid)["gate"])
    assert gate["decision"] == "draft"
    assert any("verifier rewrote poshmark_description" in r for r in gate["reasons"]), gate


def test_answer_rejects_unknown_and_listed_items(tmp_path):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    with pytest.raises(ValueError, match="unknown item i_nope"):
        pipeline.answer(s, db, "i_nope", "size 8")
    iid = _new_item(s, db)
    for status in ("posted", "posting"):
        db.set_item(iid, status=status)
        with pytest.raises(ValueError, match="already listed/posting"):
            pipeline.answer(s, db, iid, "size 8")
    assert db.item(iid)["status"] == "posting" and db.item(iid)["note"] is None


def test_answer_forgets_dry_runs_so_the_corrected_listing_is_tried_again(tmp_path):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _new_item(s, db)
    db.set_item(iid, status="ready", note="size 7")
    db.upsert_post(iid, "poshmark", status="dryrun", mode="draft")
    db.upsert_post(iid, "depop", status="drafted", url="https://depop.com/x")
    pipeline.answer(s, db, iid, "actually size 8")
    it = db.item(iid)
    assert it["status"] == "new" and it["note"] == "size 7; actually size 8"
    assert db.post(iid, "poshmark") is None                                    # next_job will dry-run it again
    assert db.post(iid, "depop")["status"] == "drafted"                        # real history is never erased


def test_register_share_without_photos_fails_and_archives(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    said = []
    monkeypatch.setattr(pipeline.notify, "say", said.append)
    share = s.path("inbox") / "2026-09-22_0900"
    share.mkdir()
    (share / "readme.txt").write_text("just text", encoding="utf-8")
    (share / "_done").touch()
    assert pipeline.register(s, db, share) is None
    [b] = db.batches("failed")
    assert b["n_photos"] == 0 and loads(b["reasons"]) == ["no usable photos"] and db.batches("new") == []
    assert not share.exists() and any(p.name.endswith("2026-09-22_0900") for p in s.path("archive").iterdir())
    assert len(said) == 1 and "no usable photos" in said[0] and "2026-09-22_0900" in said[0]
    assert pipeline.ready_folders(s) == []                                     # nothing left to rescan


# ---------- WO2: retail screenshots, drops, re-share check, requeue ----------

from thrift_agent.schema import Ev  # noqa: E402


def _own_photo(path, color, when, seed):
    """A phone photo: camera EXIF + capture time, 3:4."""
    img = Image.new("RGB", (300, 400), color)
    for x in range(0, 300, 11 + 5 * seed):
        img.paste((255, 255, 255), (x, 0, x + 2, 400))
    exif = Image.Exif()
    exif[271], exif[272] = "Apple", "iPhone"
    exif.get_ifd(0x8769)[36867] = when.strftime("%Y:%m:%d %H:%M:%S")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, exif=exif)
    return path


def _screenshot(path, color, seed):
    """A retailer screenshot: no EXIF, phone-screen aspect; its capture time falls back to mtime (now = last)."""
    img = Image.new("RGB", (117, 253), color)
    for y in range(0, 253, 9 + 3 * seed):
        img.paste((0, 0, 0), (0, y, 117, y + 1))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def _mixed_share(s):
    share = s.path("inbox") / "2026-09-22_1000"
    t0 = datetime(2026, 9, 22, 10, 0)
    for i, c in enumerate(["red", "darkred", "navy", "blue"]):
        _own_photo(share / f"IMG_{i:04d}.jpg", c, t0 + timedelta(seconds=4 * i), i)
    _screenshot(share / "IMG_0010.PNG", "white", 1)          # sorts after the EXIF-dated photos -> index 4
    _screenshot(share / "IMG_0011.PNG", "lightblue", 2)      # index 5
    (share / "_done").touch()
    _settle(share)                                             # mtimes stay later than the 2026-09-22 EXIF times
    return share


def _wo2_ask(facts, seen, seg_out, facts_kw):
    def ask(model, system, content, out, tool, description, **kw):
        labels = [c["text"] for c in content if c["type"] == "text"]
        if out is SegOut:
            seen["segment"] = labels
            return seg_out
        if out.__name__ == "Facts":
            seen["extract"] = labels
            return facts(**facts_kw)
        if out is CopyOut:
            return CopyOut(poshmark_title="Tory Burch Minnie Red Suede Ballet Flats size 7.5",
                           poshmark_description="Red suede flats with a bow.\nCondition: excellent, light sole wear.",
                           poshmark_style_tags=[], depop_description="red tory burch minnie flats, light wear",
                           depop_hashtags=["toryburch", "flats", "red", "ballet", "shoes"])
        if out is VerifyOut:
            return VerifyOut(poshmark_title="Tory Burch Minnie Red Suede Ballet Flats size 7.5",
                             poshmark_description="Red suede flats with a bow.\nCondition: excellent, light sole wear.",
                             depop_description="red tory burch minnie flats, light wear")
        raise AssertionError(out)
    return ask


def test_retail_screenshots_are_detected_assigned_by_content_and_rendered_last(tmp_path, monkeypatch, facts):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    _mixed_share(s)
    seen = {}
    seg_out = SegOut(groups=[Group(photos=[0, 1, 4], summary="red flats", full_item_photos=[0], confidence=0.95),
                             Group(photos=[2, 3, 5], summary="blue dress", full_item_photos=[2], confidence=0.95)])
    facts_kw = dict(photo_order=[0, 1, 2], cover_photo=2,                   # the model picks the screenshot as cover
                    retail_price=Ev(value="$128.00", photos=[2], source="photo", confidence=0.9),
                    style_name=Ev(value="Minnie", photos=[2], source="photo", confidence=0.9),
                    size_us=Ev(value="7.5", photos=[2], source="photo", confidence=0.95))   # screenshot-only evidence
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _wo2_ask(facts, seen, seg_out, facts_kw))
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml",
                        lambda name: {"brands": {"tory burch": {"target": 70}}, "aliases": {}, "category_defaults": {}})

    [folder] = pipeline.ready_folders(s)
    bid = pipeline.register(s, db, folder)
    pipeline.process_batch(s, db, bid)
    segd = loads(db.batch(bid)["segmentation"])
    assert segd["kinds"] == ["own", "own", "own", "own", "retail", "retail"]
    assert any("retail screenshot" in t for t in seen["segment"])
    assert not any("non-contiguous" in r for r in loads(db.batch(bid)["reasons"]) or [])

    pipeline.confirm(s, db, bid, "ok")
    items = db.items("new")
    assert len(items) == 2
    d1 = Path(items[0]["dir"])
    manifest = json.loads((d1 / "photos.json").read_text(encoding="utf-8"))
    assert [m["kind"] for m in manifest] == ["own", "own", "retail"] and manifest[2]["src"] == 4   # own first, retail last

    pipeline.process_item(s, db, items[0]["id"])
    it = db.item(items[0]["id"])
    r = loads(it["renders"])["poshmark"]
    assert any("(retail screenshot)" in t for t in seen["extract"])
    assert r["photos"][0].endswith("cover.jpg") and Path(r["photos"][-1]).name == "02.jpg"   # never the cover, always last
    assert r["original_price"] == 128
    assert r["description"].rstrip().endswith("Retail $128.")
    assert loads(it["facts"])["size_us"]["value"] is None            # a screenshot is not size evidence
    assert it["status"] == "needs_info" and any("size unclear" in x for x in loads(it["gate"])["reasons"])


def test_unassigned_screenshot_can_be_dropped_or_placed(tmp_path, monkeypatch, facts):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    _mixed_share(s)
    seg_out = SegOut(groups=[Group(photos=[0, 1], summary="red flats", full_item_photos=[0], confidence=0.95),
                             Group(photos=[2, 3], summary="blue dress", full_item_photos=[2], confidence=0.95)],
                     unassigned=[4, 5])
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _wo2_ask(facts, {}, seg_out, {}))
    [folder] = pipeline.ready_folders(s)
    bid = pipeline.register(s, db, folder)
    pipeline.process_batch(s, db, bid)
    b = db.batch(bid)
    assert b["status"] == "needs_confirm" and any("screenshot 4 matches no item" in r for r in loads(b["reasons"]))
    with pytest.raises(ValueError, match="in no item"):
        pipeline.confirm(s, db, bid, "ok")                               # screenshots still unplaced: stop, don't guess
    with pytest.raises(ValueError, match="can't drop"):
        pipeline.confirm(s, db, bid, "drop 9")
    pipeline.confirm(s, db, bid, "drop 4, 5>2")
    items = db.items("new")
    kinds = [[m["kind"] for m in json.loads((Path(it["dir"]) / "photos.json").read_text(encoding="utf-8"))]
             for it in items]
    assert kinds == [["own", "own"], ["own", "own", "retail"]]
    assert any(r["kind"] == "photos_dropped" for r in db.conn.execute("SELECT kind FROM events WHERE ref=?", (bid,)))


def test_reshared_item_is_held_as_a_possible_duplicate(tmp_path, monkeypatch, facts):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    monkeypatch.setattr("thrift_agent.brain.llm.ask", fake_ask(facts))
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml",
                        lambda name: {"brands": {"tory burch": {"target": 70}}, "aliases": {}, "category_defaults": {}})
    bid = db.add_batch("share", 3)
    iids = []
    for k in (1, 2):                                                       # the same three photos shared twice
        d = tmp_path / f"item_{k}"
        for i, c in enumerate(["red", "green", "blue"]):
            _jpg(d / "photos" / f"{i:02d}.jpg", c)
        iids.append(db.add_item(bid, k, str(d)))
    pipeline.process_item(s, db, iids[0])
    first = db.item(iids[0])
    assert first["status"] == "ready" and first["cover_hash"]
    pipeline.process_item(s, db, iids[1])
    second = db.item(iids[1])
    assert second["status"] == "needs_info"
    assert any(f"looks like item {iids[0]}" in r for r in loads(second["gate"])["reasons"])
    pipeline.answer(s, db, iids[1], "different item")                    # the seller's word clears the hold
    pipeline.process_item(s, db, iids[1])
    assert db.item(iids[1])["status"] == "ready"


def test_requeue_only_failed_or_dryrun_rows_without_a_url(tmp_path):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    bid = db.add_batch("share", 1)
    iid = db.add_item(bid, 1, str(tmp_path / "item"))
    db.set_item(iid, status="ready")
    with pytest.raises(ValueError, match="nothing to requeue"):
        pipeline.requeue(s, db, iid)
    db.upsert_post(iid, "poshmark", status="failed", last_error="Mismatch")
    assert pipeline.requeue(s, db, iid) == ["poshmark"]
    assert db.post(iid, "poshmark")["status"] == "queued" and db.post(iid, "poshmark")["last_error"] is None
    db.upsert_post(iid, "poshmark", status="dryrun")
    assert pipeline.requeue(s, db, iid, "poshmark") == ["poshmark"]
    db.upsert_post(iid, "poshmark", status="failed", url="https://example.invalid/listing/1")
    with pytest.raises(ValueError, match="listing URL"):                  # it reached the site: reconcile by hand
        pipeline.requeue(s, db, iid)
    db.upsert_post(iid, "poshmark", status="posted", url=None)
    with pytest.raises(ValueError, match="only failed/dryrun"):
        pipeline.requeue(s, db, iid)
    db.upsert_post(iid, "poshmark", status="failed")
    db.set_item(iid, status="needs_info")
    with pytest.raises(ValueError, match="not ready"):
        pipeline.requeue(s, db, iid)
    with pytest.raises(ValueError, match="unknown item"):
        pipeline.requeue(s, db, "i_nope")


def test_archive_can_live_next_to_the_inbox(tmp_path):
    """A13: on the Mac the archive is Posh/archive, a sibling of Posh/inbox inside the iCloud container."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    s.data["paths"]["archive"] = str(s.path("inbox").parent / "archive")
    s.path("archive").mkdir(exist_ok=True)
    inside = _jpg(s.path("inbox") / "2026-09-21_1432" / "a.jpg").parent
    bid = db.add_batch(str(inside), 1)
    dest = pipeline.archive_share(s, db, bid, inside)
    assert dest and dest.parent == s.path("inbox").parent / "archive" and not inside.exists()
