"""End-to-end with the model stubbed out: inbox folder → batch → confirm → items → gate → renders."""
import re
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
    monkeypatch.setitem(s.data["inbox"], "settle_seconds", 0)

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
