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


@pytest.fixture(autouse=True)
def owner_messages(monkeypatch):
    """Owner messages (Telegram, or the dev print) are recorded here instead of sent: (kind, ref), kind = batch |
    condition | kids | item | owner_q. The queue still decides what goes out: one open message at a time (WO20)."""
    sent = []
    for kind, name in approve_senders().items():
        monkeypatch.setattr(pipeline.approve, name, lambda s, db, ref, kind=kind: sent.append((kind, ref)))
    return sent


def approve_senders() -> dict[str, str]:
    return dict(pipeline.approve.SENDERS)


def _split(db, src: str, n: int) -> str:
    """A batch the owner already confirmed (its items exist only then)."""
    bid = db.add_batch(src, n)
    db.set_batch(bid, status="split")
    return bid


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
            return CopyOut(poshmark_title="Tory Burch Red Ballet Flats size 7.5",
                           poshmark_description="Red flats.\nCondition: excellent, light sole wear.",
                           poshmark_style_tags=["classic"], depop_description="red tory burch flats",
                           depop_hashtags=["toryburch", "flats", "red", "ballet", "shoes"])
        if out is VerifyOut:
            return audit or VerifyOut(poshmark_title="Tory Burch Red Ballet Flats size 7.5",
                                      poshmark_description="Red flats.\nCondition: excellent, light sole wear.",
                                      depop_description="red tory burch flats")
        if out.__name__ == "FrontOut":                     # the front check (WO23): no opinion, the roles decide
            return out(views=[], front=-1)
        if out.__name__ == "SizeLabel":
            return out(printed=None)
        if out.__name__ == "UprightOut":                   # the cover is upright as shot
            return out(upright="A")
        raise AssertionError(out)
    return ask


def _settle(share):
    """Age every file by an hour so the share counts as quiet. Never rely on a 0 s settle window: on some Windows
    runners a fresh file's mtime is a few ms ahead of time.time() and ready_folders() would skip the share."""
    old = time.time() - 3600
    for f in share.iterdir():
        os.utime(f, (old, old))


def test_end_to_end(tmp_path, monkeypatch, facts, owner_messages):
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
    assert db.batch(bid)["status"] == "split"                 # accepted at once: segmentation.auto_confirm (WO20b)
    assert ("batch", bid) not in owner_messages                # no contact sheet goes out...
    assert (s.path("work") / bid / "contact_sheet.png").exists()   # ...it is still drawn, for the record
    assert loads(db.batch(bid)["segmentation"])["auto_accepted"]
    items = db.items("new")
    assert len(items) == 2 and not share.exists()             # inbox cleared, batch archived
    pipeline.process_item(s, db, items[0]["id"])

    it = db.item(items[0]["id"])
    gate, renders = loads(it["gate"]), loads(it["renders"])
    assert it["status"] == "awaiting_price" and gate["decision"] == "publish", gate   # nothing lists unpriced
    assert ("item", it["id"]) in owner_messages                                      # ONE approval message
    posh = renders["poshmark"]
    assert posh["price"] == 95 and posh["sku"] == it["id"] and posh["photos"][0].endswith("cover.jpg")
    with Image.open(posh["photos"][0]) as cover:
        assert cover.size == (1200, 1600)                       # 3:4 portrait: Poshmark's cover crop leaves it as is
    assert pipeline.set_price(s, db, it["id"], 90) == "ready"                          # the owner's word
    it = db.item(it["id"])
    assert loads(it["renders"])["poshmark"]["price"] == 90 and loads(it["price"])["source"] == "owner"
    owner_messages.clear()
    pipeline.answer(s, db, it["id"], "worn twice")                                    # a note reprocesses it
    pipeline.process_item(s, db, it["id"])
    it = db.item(it["id"])
    assert it["status"] == "ready" and loads(it["price"])["list_price"] == 90         # price kept, no new message
    assert owner_messages == []


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
    bid = _split(db, str(share), 1)
    d = s.path("work") / bid / "item_01"
    for i, c in enumerate(["red", "darkred", "salmon"]):
        _jpg(d / "photos" / f"{i:02d}.jpg", c)
    return db.add_item(bid, 1, str(d))


def test_unreported_verifier_rewrite_goes_to_draft(tmp_path, monkeypatch, facts):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    rewritten = VerifyOut(poshmark_title="Tory Burch Red Ballet Flats size 7.5",
                          poshmark_description="Gorgeous buttery-soft red flats, run true to size.",
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
            return CopyOut(poshmark_title="Tory Burch Minnie Red Ballet Flats size 7.5",
                           poshmark_description="Red flats with a bow.\nCondition: excellent, light sole wear.",
                           poshmark_style_tags=[], depop_description="red tory burch minnie flats, light wear",
                           depop_hashtags=["toryburch", "flats", "red", "ballet", "shoes"])
        if out is VerifyOut:
            return VerifyOut(poshmark_title="Tory Burch Minnie Red Ballet Flats size 7.5",
                             poshmark_description="Red flats with a bow.\nCondition: excellent, light sole wear.",
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
    assert db.batch(bid)["status"] == "split"                          # accepted at once (WO20b)
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
    assert r["description"].rstrip().endswith("\nOriginal retail $128.")              # WO26 wording
    assert loads(it["facts"])["size_us"]["value"] is None            # a screenshot is not size evidence
    assert it["status"] == "awaiting_price" and any("size unclear" in x for x in loads(it["gate"])["reasons"])


def _old_flow(s):
    """segmentation.auto_confirm off: the contact sheet and the owner's ok, as before WO20b (a copy: settings are
    shared)."""
    s.data["segmentation"] = {**s.data["segmentation"], "auto_confirm": False}
    return s


def test_unassigned_screenshot_can_be_dropped_or_placed(tmp_path, monkeypatch, facts):
    s = _old_flow(_settings(tmp_path))
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


def test_auto_confirm_leaves_out_a_screenshot_that_matches_no_item(tmp_path, monkeypatch, facts, owner_messages):
    """WO20b: a retail screenshot the model can't place isn't a photo of any item: left out, recorded, never asked."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    _mixed_share(s)
    seg_out = SegOut(groups=[Group(photos=[0, 1], summary="red flats", full_item_photos=[0], confidence=0.95),
                             Group(photos=[2, 3], summary="blue dress", full_item_photos=[2], confidence=0.95),
                             Group(photos=[5], summary="a screenshot", full_item_photos=[5], confidence=0.5)],
                     unassigned=[4])
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _wo2_ask(facts, {}, seg_out, {}))
    [folder] = pipeline.ready_folders(s)
    bid = pipeline.register(s, db, folder)
    pipeline.process_batch(s, db, bid)
    b = db.batch(bid)
    assert b["status"] == "split" and owner_messages == []
    reasons = loads(b["reasons"])
    assert "screenshot 4 matches no item: left out" in reasons and "screenshot 5 matches no item: left out" in reasons
    assert [len(json.loads((Path(i["dir"]) / "photos.json").read_text(encoding="utf-8"))) for i in db.items("new")] == [2, 2]


@pytest.mark.parametrize("groups,unassigned,result", [
    ([[0, 1, 2], [3, 4]], [], ([[0, 1, 2], [3, 4]], [], [])),
    ([[0, 1], [2, 3, 4], []], [], ([[0, 1], [2, 3, 4]], [], [])),                  # an empty group: nothing lost
    ([[0, 1, 2], [3]], [4], ([[0, 1, 2], [3]], [4], ["screenshot 4 matches no item: left out"])),
    ([[0, 1, 2], [3], [4]], [], ([[0, 1, 2], [3]], [4], ["screenshot 4 matches no item: left out"])),
    ([[0, 1], [3, 4]], [], None),                                                  # photo 2 in no item: asked
    ([[0, 1, 2], [2, 3, 4]], [], None),                                            # photo 2 twice: asked
])
def test_accept_grouping(groups, unassigned, result):
    kinds = ["own", "own", "own", "own", "retail"]
    assert pipeline.accept_grouping(groups, 5, kinds, unassigned) == result


def test_a_grouping_that_loses_a_photo_still_asks_with_the_contact_sheet(tmp_path, monkeypatch, facts, owner_messages):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    _mixed_share(s)
    seg_out = SegOut(groups=[Group(photos=[0, 1], summary="red flats", full_item_photos=[0], confidence=0.95),
                             Group(photos=[3, 4, 5], summary="blue dress", full_item_photos=[3], confidence=0.95)])
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _wo2_ask(facts, {}, seg_out, {}))
    [folder] = pipeline.ready_folders(s)
    bid = pipeline.register(s, db, folder)
    pipeline.process_batch(s, db, bid)                                     # photo 2 is in no item: never guessed
    assert db.batch(bid)["status"] == "needs_confirm" and owner_messages == [("batch", bid)]


def test_auto_confirm_off_is_the_old_flow(tmp_path, monkeypatch, facts, owner_messages):
    s = _old_flow(_settings(tmp_path))
    db = DB(s.path("db"))
    _mixed_share(s)
    seg_out = SegOut(groups=[Group(photos=[0, 1, 4], summary="red flats", full_item_photos=[0], confidence=0.95),
                             Group(photos=[2, 3, 5], summary="blue dress", full_item_photos=[2], confidence=0.95)])
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _wo2_ask(facts, {}, seg_out, {}))
    [folder] = pipeline.ready_folders(s)
    bid = pipeline.register(s, db, folder)
    pipeline.process_batch(s, db, bid)
    assert db.batch(bid)["status"] == "needs_confirm" and owner_messages == [("batch", bid)]   # always_confirm
    pipeline.confirm(s, db, bid, "ok")
    assert db.batch(bid)["status"] == "split" and len(db.items("new")) == 2


def test_reshared_item_is_held_as_a_possible_duplicate(tmp_path, monkeypatch, facts):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    monkeypatch.setattr("thrift_agent.brain.llm.ask", fake_ask(facts))
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml",
                        lambda name: {"brands": {"tory burch": {"target": 70}}, "aliases": {}, "category_defaults": {}})
    bid = _split(db, "share", 3)
    iids = []
    for k in (1, 2):                                                       # the same three photos shared twice
        d = tmp_path / f"item_{k}"
        for i, c in enumerate(["red", "green", "blue"]):
            _jpg(d / "photos" / f"{i:02d}.jpg", c)
        iids.append(db.add_item(bid, k, str(d)))
    pipeline.process_item(s, db, iids[0])
    first = db.item(iids[0])
    assert first["status"] == "awaiting_price" and first["cover_hash"]
    pipeline.process_item(s, db, iids[1])
    second = db.item(iids[1])
    assert second["status"] == "awaiting_price" and loads(second["gate"])["decision"] == "needs_info"
    assert any(f"looks like item {iids[0]}" in r for r in loads(second["gate"])["reasons"])
    pipeline.set_price(s, db, iids[1], 60)                               # price first, then the answer
    pipeline.answer(s, db, iids[1], "different item")                    # the seller's word clears the hold
    pipeline.process_item(s, db, iids[1])
    assert db.item(iids[1])["status"] == "ready"                         # priced, nothing unresolved: no new message


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
    with pytest.raises(ValueError, match="only failed/dryrun rows"):
        pipeline.requeue(s, db, iid)
    db.upsert_post(iid, "poshmark", status="failed")
    db.set_item(iid, status="needs_info")
    with pytest.raises(ValueError, match="not ready"):
        pipeline.requeue(s, db, iid)
    with pytest.raises(ValueError, match="unknown item"):
        pipeline.requeue(s, db, "i_nope")


def test_requeue_takes_back_an_item_the_poster_parked_with_a_question(tmp_path):
    """WO11: the first Mac dry-run parked its item in needs_owner on the (then unrecorded) cover dialog. Once the
    poster handles it, `thrift requeue` retries the item as it is — no reprocessing — and closes the question."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = db.add_item(_split(db, "share", 1), 1, str(tmp_path / "item"))
    db.set_item(iid, status="needs_owner")
    db.upsert_post(iid, "poshmark", status="queued", last_error="needs owner: Poshmark opened a dialog ...")
    db.add_outbox("-100", 7, "owner_q", iid, text="Poshmark opened a dialog ...")
    assert pipeline.requeue(s, db, iid) == ["poshmark"]
    row, it = db.post(iid, "poshmark"), db.item(iid)
    assert (row["status"], row["last_error"], it["status"]) == ("queued", None, "ready")
    assert db.outbox_pending() == []                                       # the question is never re-sent
    assert loads(db.conn.execute("SELECT detail FROM events WHERE kind='requeued'").fetchone()[0])["from"] == \
        "needs_owner"

    db.set_item(iid, status="needs_owner")                               # queued but not by the poster's question
    with pytest.raises(ValueError, match="only failed/dryrun rows"):
        pipeline.requeue(s, db, iid)
    db.set_item(iid, status="ready")                                     # a plain queued row: nothing to requeue
    db.upsert_post(iid, "poshmark", status="queued", last_error="needs owner: which brand?")
    with pytest.raises(ValueError, match="only failed/dryrun rows"):
        pipeline.requeue(s, db, iid)


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


# ---------- WO4: the owner's price, open questions, kids sizes ----------

def test_open_questions_ride_with_the_price_and_come_back_until_resolved(tmp_path, monkeypatch, facts, owner_messages):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    state = {"brand": Ev(value=None, confidence=0.2)}                   # the model can't read the brand
    monkeypatch.setattr("thrift_agent.brain.llm.ask", fake_ask(lambda **kw: facts(brand=state["brand"], **kw)))
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml", lambda name: {"brands": {}, "aliases": {}, "category_defaults": {}})
    d = tmp_path / "item"
    for i, c in enumerate(["red", "green", "blue"]):
        _jpg(d / "photos" / f"{i:02d}.jpg", c)
    iid = db.add_item(_split(db, "share", 3), 1, str(d))

    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    assert it["status"] == "awaiting_price" and any("brand unclear" in r for r in loads(it["gate"])["reasons"])
    pr = loads(it["price"])                                              # no brand, no table: still a price (WO20)
    assert pr["source"] == "default" and pr["list_price"] == 20
    assert owner_messages.count(("item", iid)) == 1
    assert any(q.startswith("Brand? Couldn't read it") for q in loads(it["gate"])["questions"])

    assert pipeline.set_price(s, db, iid, 45) == "ready"                  # the owner replied "brand Vince, 45":
    pipeline.answer(s, db, iid, "brand Vince")                            # price first, then the note
    pipeline.process_item(s, db, iid)                                     # still unreadable -> asks again, price kept
    it = db.item(iid)
    assert it["status"] == "awaiting_price" and loads(it["price"])["list_price"] == 45
    assert owner_messages.count(("item", iid)) == 2

    state["brand"] = Ev(value="Vince", photos=[], source="note", confidence=1.0)
    pipeline.answer(s, db, iid, "yes, Vince")
    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    assert it["status"] == "ready" and loads(it["price"])["source"] == "owner"
    assert owner_messages.count(("item", iid)) == 2                       # resolved: no third message


def test_set_price_rules(tmp_path):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = db.add_item(_split(db, "share", 1), 1, str(tmp_path / "item"))
    db.set_item(iid, status="awaiting_price", price={"list_price": 30, "source": "brand", "by_marketplace": {"poshmark": 30}},
                renders={"poshmark": {"price": 30}})
    with pytest.raises(ValueError, match="unknown item"):
        pipeline.set_price(s, db, "i_nope", 10)
    with pytest.raises(ValueError, match="positive"):
        pipeline.set_price(s, db, iid, 0)
    assert pipeline.set_price(s, db, iid, 44) == "ready"
    it = db.item(iid)
    assert it["owner_price"] == 44 and loads(it["renders"])["poshmark"]["price"] == 44
    assert loads(it["price"]) == {"list_price": 44, "source": "owner", "by_marketplace": {"poshmark": 44},
                                  "basis": "owner price $44"}
    db.set_item(iid, status="posted")
    with pytest.raises(ValueError, match="can't be changed"):
        pipeline.set_price(s, db, iid, 50)


def test_kids_size_carries_its_system_in_the_render(tmp_path, facts):
    s = _settings(tmp_path)
    d = tmp_path / "item"
    photos = [_jpg(d / "photos" / f"{i:02d}.jpg", c) for i, c in enumerate(["red", "green"])]
    f = facts(department="Kids", category="Shoes", photo_order=[0, 1],
              size_us=Ev(value="7.5", photos=[1], source="derived", confidence=0.8),
              size_eu=Ev(value="24", photos=[1], source="photo", confidence=0.9))
    c = CopyOut(poshmark_title="t", poshmark_description="d", poshmark_style_tags=[], depop_description="d",
                depop_hashtags=[])
    pr = PriceResult(target=22, list_price=30, source="category_default", by_marketplace={"poshmark": 30})
    assert pipeline.build_renders(s, "i_1", d, photos, f, c, pr)["poshmark"].size == "EU 24 / US Toddler 7.5"


# ---------- WO5: a price alone settles neither NWT nor a re-share ----------

def test_nwt_without_tag_lists_like_new_unless_the_owner_says_nwt(tmp_path, monkeypatch, facts, owner_messages):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    nwt = dict(condition="NWT", condition_evidence=Ev(value="tag visible", photos=[2], source="photo", confidence=0.9))
    monkeypatch.setattr("thrift_agent.brain.llm.ask", fake_ask(lambda **kw: facts(**nwt, **kw)))   # model: NWT, no hang_tag_photo
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml",
                        lambda name: {"brands": {"tory burch": {"target": 70}}, "aliases": {}, "category_defaults": {}})
    d = tmp_path / "item"
    for i, c in enumerate(["red", "green", "blue"]):
        _jpg(d / "photos" / f"{i:02d}.jpg", c)
    iid = db.add_item(_split(db, "share", 3), 1, str(d))

    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    gate = loads(it["gate"])
    assert loads(it["facts"])["condition"] == "like_new" and loads(it["renders"])["poshmark"]["condition"] == "like_new"
    assert gate["decision"] == "publish" and gate["questions"] == [pipeline.NWT_QUESTION] and gate["notes"] == []
    assert "Listed as Like New: no photo of an attached hang tag" in pipeline.NWT_QUESTION
    assert pipeline.set_price(s, db, iid, 90) == "ready"                  # a price alone: like new, ready

    pipeline.answer(s, db, iid, "NWT")                                    # the owner's word is the other proof
    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    assert it["status"] == "ready" and loads(it["facts"])["condition"] == "NWT"
    assert loads(it["facts"])["condition_evidence"]["source"] == "note"
    assert loads(it["gate"])["notes"] == [] and loads(it["gate"])["questions"] == []
    assert loads(it["renders"])["poshmark"]["condition"] == "NWT" and loads(it["price"])["list_price"] == 90


def test_reshare_hold_needs_an_explicit_answer(tmp_path, monkeypatch, facts, owner_messages):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    monkeypatch.setattr("thrift_agent.brain.llm.ask", fake_ask(facts))
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml",
                        lambda name: {"brands": {"tory burch": {"target": 70}}, "aliases": {}, "category_defaults": {}})
    bid = _split(db, "share", 3)
    iids = []
    for k in (1, 2, 3):                                                    # the same photos shared three times
        d = tmp_path / f"item_{k}"
        for i, c in enumerate(["red", "green", "blue"]):
            _jpg(d / "photos" / f"{i:02d}.jpg", c)
        iids.append(db.add_item(bid, k, str(d)))
    pipeline.process_item(s, db, iids[0])
    pipeline.set_price(s, db, iids[0], 70)

    pipeline.process_item(s, db, iids[1])                                  # held as a possible re-share
    assert loads(db.item(iids[1])["gate"])["hold"] == "reshare"
    assert pipeline.set_price(s, db, iids[1], 60) == "awaiting_price"      # the price alone does not release it
    it = db.item(iids[1])
    assert it["status"] == "awaiting_price" and it["owner_price"] == 60
    sent_before = len(owner_messages)
    assert pipeline.answer(s, db, iids[1], "same item") == "dropped"       # confirmed duplicate: dropped, not reprocessed
    assert db.item(iids[1])["status"] == "dropped" and len(owner_messages) == sent_before
    with pytest.raises(ValueError, match="can't reopen"):
        pipeline.answer(s, db, iids[1], "actually list it")

    pipeline.process_item(s, db, iids[2])                                  # held again (twin of item 1)
    assert pipeline.set_price(s, db, iids[2], 65) == "awaiting_price"
    assert pipeline.answer(s, db, iids[2], "different item") == "new"
    pipeline.process_item(s, db, iids[2])
    it = db.item(iids[2])
    assert it["status"] == "ready" and loads(it["gate"])["hold"] is None and loads(it["price"])["list_price"] == 65


# ---------- WO9: Poshmark's category names, kids size tab ----------

def _one_item(tmp_path, db, n=3):
    d = tmp_path / "item"
    for i, c in enumerate(["red", "green", "blue"][:n]):
        _jpg(d / "photos" / f"{i:02d}.jpg", c)
    return db.add_item(_split(db, "share", n), 1, str(d))


def test_kids_items_get_poshmarks_category_and_unisex_is_a_girls_or_boys_question(tmp_path, monkeypatch, facts,
                                                                                    owner_messages):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    kid = dict(department="Kids", category="Tops", subcategory=None, kids_gender="unisex", kids_gender_confidence=0.8,
               size_us=Ev(value="4T", photos=[1], source="photo", confidence=0.9))
    monkeypatch.setattr("thrift_agent.brain.llm.ask", fake_ask(lambda **kw: facts(**kid, **kw)))
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml", lambda name: {"brands": {}, "aliases": {}, "category_defaults": {}})
    iid = _one_item(tmp_path, db)
    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    posh = loads(it["renders"])["poshmark"]
    assert loads(it["facts"])["category"] == "Shirts & Tops" and posh["category"] == "Shirts & Tops"
    assert posh["kids_gender"] == "unisex" and posh["size"] == "4T"
    gate = loads(it["gate"])
    assert gate["ask_kids"] and gate["notes"] == [] and owner_messages == [("kids", iid)]   # before the price card
    assert pipeline.set_kids_gender(s, db, iid, "Boys") == "awaiting_price"                 # not priced yet: the card
    it = db.item(iid)
    assert loads(it["renders"])["poshmark"]["kids_gender"] == "boys" and it["owner_kids_gender"] == "boys"
    assert not loads(it["gate"])["ask_kids"]
    pipeline.approve.pump(s, db)
    assert owner_messages == [("kids", iid), ("item", iid)]
    pipeline.process_item(s, db, iid)                                    # a reprocessing never asks again
    assert not loads(db.item(iid)["gate"])["ask_kids"]


@pytest.mark.parametrize("kw,asked", [
    (dict(kids_gender="girls", kids_gender_confidence=0.9), False),        # sure: used silently
    (dict(kids_gender="boys", kids_gender_confidence=0.70), False),
    (dict(kids_gender="boys", kids_gender_confidence=0.69), True),         # below 0.70: asked [Girls] [Boys]
    (dict(kids_gender="unisex", kids_gender_confidence=0.95), True),       # Poshmark has no unisex size list
    (dict(kids_gender=None), True),
    (dict(kids_gender="girls", kids_gender_confidence=0.2, size_us=Ev(value=None)), False),   # no size: no size list
    (dict(department="Women", kids_gender=None), False),
])
def test_kids_department_question_only_below_0_70(facts, kw, asked):
    assert pipeline.kids_question(facts(**{"department": "Kids", **kw})) is asked


def test_a_kids_item_already_priced_is_ready_once_girls_or_boys_is_answered(tmp_path, monkeypatch, facts,
                                                                            owner_messages):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    kid = dict(department="Kids", category="Shoes", subcategory=None, kids_gender="girls", kids_gender_confidence=0.5,
               size_us=Ev(value="US Toddler 7.5", photos=[1], source="photo", confidence=0.9))
    monkeypatch.setattr("thrift_agent.brain.llm.ask", fake_ask(lambda **kw: facts(**kid, **kw)))
    iid = _one_item(tmp_path, db)
    pipeline.process_item(s, db, iid)
    assert pipeline.set_price(s, db, iid, 30) == "awaiting_price"        # Girls/Boys is still open: not ready yet
    assert pipeline.set_kids_gender(s, db, iid, "girls") == "ready"
    with pytest.raises(ValueError, match="girls or boys"):
        pipeline.set_kids_gender(s, db, iid, "unisex")


def test_a_category_poshmark_does_not_have_is_asked_in_the_approval_message(tmp_path, monkeypatch, facts,
                                                                            owner_messages):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    monkeypatch.setattr("thrift_agent.brain.llm.ask", fake_ask(lambda **kw: facts(category="Gadgets", **kw)))
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml",
                        lambda name: {"brands": {"tory burch": {"target": 70}}, "aliases": {}, "category_defaults": {}})
    iid = _one_item(tmp_path, db)
    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    gate = loads(it["gate"])
    # WO25: asked with buttons, real paths only — the model's subcategory names one (Shoes › Flats & Loafers)
    assert it["status"] == "awaiting_price" and gate["decision"] == "needs_info" and owner_messages == [("category", iid)]
    assert gate["reasons"][0] == "category 'Gadgets' is not one of Poshmark's Women categories — reply e.g. " \
                                 "'category Tops'"
    assert gate["ask_category"] == [{"department": "Women", "category": "Shoes", "subcategory": "Flats & Loafers"}]
    assert not any(q.startswith("category") for q in gate["questions"])        # not on the card as well
    assert pipeline.set_price(s, db, iid, 60) == "awaiting_price"              # like Girls/Boys: answered first
    assert pipeline.set_category(s, db, iid, gate["ask_category"][0]) == "new"   # not the model's: reprocessed
    assert loads(db.item(iid)["owner_category"])["category"] == "Shoes" and db.item(iid)["owner_price"] == 60


def test_the_listing_keeps_only_poshmarks_curated_style_tags(tmp_path, monkeypatch, facts, owner_messages):
    """WO12: the copy model's tags are put on Poshmark's list before lint; a material tag needs a label."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    base = fake_ask(facts)

    def ask(model, system, content, out, tool, description, **kw):
        result = base(model, system, content, out, tool, description, **kw)
        if out is CopyOut:
            result.poshmark_style_tags = ["casual", "boho", "Leather"]
        return result
    monkeypatch.setattr("thrift_agent.brain.llm.ask", ask)
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml",
                        lambda name: {"brands": {"tory burch": {"target": 70}}, "aliases": {}, "category_defaults": {}})
    iid = _one_item(tmp_path, db)
    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    assert loads(it["renders"])["poshmark"]["tags"] == ["Casual"]            # boho: not Poshmark's; Leather: no label
    assert not any("material" in r for r in loads(it["gate"])["reasons"])   # dropped, so never a lint problem


# ---------------------------------------------------------------- WO16: the owner's condition rule, photos

def test_facts_from_before_the_photo_roles_keep_the_old_flaw_rule(facts):
    """Facts without photo_roles (before WO20): a photo that shows a flaw is not the cover when a clean one exists.
    With roles the front is the cover even when a flaw shows on it (WO23, test_front_cover.py)."""
    from thrift_agent.schema import Flaw
    f = facts(cover_photo=3, photo_order=[3, 0, 1, 2, 4], flaws=[Flaw(description="scuff", photos=[3, 4])])
    assert pipeline.photo_order(f, 5) == [0, 3, 1, 2, 4]              # the first clean photo goes first
    assert pipeline.photo_order(facts(cover_photo=3, photo_order=[3, 0, 1, 2, 4]), 5)[0] == 3   # no flaw: as chosen
    every = facts(cover_photo=0, photo_order=[0, 1], flaws=[Flaw(description="hole", photos=[0, 1])])
    assert pipeline.photo_order(every, 2)[0] == 0                     # nothing clean to swap in
    assert pipeline.flaw_notes(every, [0, 1]) == []                  # no note: the cover shows it, that is fine


def test_flaw_photos_survive_the_photo_limit_and_a_flaw_without_one_is_a_note(facts):
    from thrift_agent.schema import Flaw
    assert pipeline.fit_photos(list(range(10)), {8, 9}, 4) == [0, 1, 8, 9]
    assert pipeline.fit_photos([5, 1, 2], {5}, 2) == [5, 1]          # the cover is never what makes room
    assert pipeline.fit_photos([0, 1, 2], set(), 5) == [0, 1, 2]
    f = facts(flaws=[Flaw(description="scuff on left toe", photos=[4]), Flaw(description="small hole (seller note)")])
    assert pipeline.flaw_notes(f, [0, 1, 4]) == [
        "flaw without a photo, so the listing doesn't show it: small hole (seller note)"]


def test_every_marketplaces_listing_keeps_the_flaw_photos(tmp_path, facts):
    from thrift_agent.schema import Flaw
    import copy
    s = _settings(tmp_path)
    s.data["marketplaces"] = copy.deepcopy(s.data["marketplaces"])  # _settings shares the loaded dicts: never mutate
    s.data["marketplaces"]["poshmark"]["max_photos"] = 3            # fewer slots than photos
    d = tmp_path / "item"
    colors = ["red", "green", "blue", "gold", "pink", "white"]
    photos = [_jpg(d / "photos" / f"{i:02d}.jpg", c) for i, c in enumerate(colors)]
    f = facts(cover_photo=5, photo_order=[5, 0, 1, 2, 3, 4], flaws=[Flaw(description="scuff", photos=[5])])
    c = CopyOut(poshmark_title="t", poshmark_description="d", poshmark_style_tags=[], depop_description="d",
                depop_hashtags=[])
    pr = PriceResult(target=70, list_price=85, source="brand", by_marketplace={"poshmark": 85})
    r = pipeline.build_renders(s, "i_1", d, photos, f, c, pr)["poshmark"]
    assert [Path(p).name for p in r.photos] == ["cover.jpg", "05.jpg", "01.jpg"]   # cover from 00; 05 kept, not cut
    assert pipeline.listing_photos(s, f, 6) == [0, 5, 1]               # what lint checks: the same photos


def test_the_owner_rule_rewrites_nothing_but_drops_wear_words_and_adds_the_line(tmp_path, monkeypatch, facts,
                                                                              owner_messages):
    """The live listing's own description (2026-10-03) through the whole pipeline: the wear sentence goes, the neutral
    line takes its place, the rest is the model's text, and lint finds nothing."""
    from thrift_agent.schema import Flaw
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    worded = ("Red flats with a bow.\nSize 38 EU, fits US 7.5. Good used condition, worn with dirt and scuffing on "
              "the soles, light staining on the toe.")
    base = fake_ask(lambda **kw: facts(flaws=[Flaw(description="scuffed soles", photos=[2])], **kw))

    def ask(model, system, content, out, tool, description, **kw):
        result = base(model, system, content, out, tool, description, **kw)
        if out in (CopyOut, VerifyOut):
            result.poshmark_description = worded
            result.depop_description = "red tory burch flats, light wear on the soles"
        return result
    monkeypatch.setattr("thrift_agent.brain.llm.ask", ask)
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml",
                        lambda name: {"brands": {"tory burch": {"target": 70}}, "aliases": {}, "category_defaults": {}})
    iid = _one_item(tmp_path, db)
    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    posh = loads(it["renders"])["poshmark"]
    assert posh["description"] == ("Red flats with a bow.\nSize 38 EU, fits US 7.5. Gently pre-loved, please see "
                                   "photos for condition.")
    assert loads(it["gate"])["decision"] == "publish", loads(it["gate"])        # nothing left for lint to find
    assert "02.jpg" in [Path(p).name for p in posh["photos"][1:]]               # the flaw's photo, not the cover


def test_a_flaw_without_a_photo_is_a_note_in_the_approval_message(tmp_path, monkeypatch, facts, owner_messages):
    from thrift_agent.schema import Flaw
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    flawed = [Flaw(description="scuffed soles", photos=[1]), Flaw(description="small hole inside (seller note)")]
    monkeypatch.setattr("thrift_agent.brain.llm.ask", fake_ask(lambda **kw: facts(flaws=flawed, **kw)))
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml",
                        lambda name: {"brands": {"tory burch": {"target": 70}}, "aliases": {}, "category_defaults": {}})
    iid = _one_item(tmp_path, db)
    pipeline.process_item(s, db, iid)
    notes = loads(db.item(iid)["gate"])["notes"]
    assert "flaw without a photo, so the listing doesn't show it: small hole inside (seller note)" in notes
    caption, _ = pipeline.approve.item_caption(iid, db.item(iid))
    assert "⚠️ flaw without a photo, so the listing doesn't show it" in caption


# ---------------------------------------------------------------- WO17: the owner's condition rules

@pytest.mark.parametrize("cond,alt,want,notes", [
    ("good", "like_new", "like_new", []),            # torn between Good and Like New: Like New
    ("like_new", "good", "like_new", []),
    ("excellent", "like_new", "like_new", []),
    ("good", "excellent", "excellent", []),          # excellent goes up as Like New too
    ("good", None, "good", []),
    ("fair", None, "good", [pipeline.WELL_WORN]),    # never Fair
    ("fair", "good", "good", [pipeline.WELL_WORN]),
    ("good", "fair", "good", [pipeline.WELL_WORN]),
    ("NWT", "like_new", "NWT", []),                  # NWT is settle_nwt's: strict, never by doubt
    ("like_new", "NWT", "like_new", []),
])
def test_the_owners_condition_rules(facts, cond, alt, want, notes):
    f, said = pipeline.settle_condition(facts(condition=cond, condition_alternative=alt))
    assert (f.condition, said) == (want, notes)


def test_a_fair_reading_goes_up_as_good_and_the_owner_is_told(tmp_path, monkeypatch, facts, owner_messages):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    monkeypatch.setattr("thrift_agent.brain.llm.ask", fake_ask(lambda **kw: facts(condition="fair", **kw)))
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml",
                        lambda name: {"brands": {"tory burch": {"target": 70}}, "aliases": {}, "category_defaults": {}})
    iid = _one_item(tmp_path, db)
    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    assert loads(it["facts"])["condition"] == "good" and loads(it["renders"])["poshmark"]["condition"] == "good"
    assert "looked well-worn — listed as Good; check before approving" in loads(it["gate"])["notes"]
    caption, _ = pipeline.approve.item_caption(iid, it)
    assert "⚠️ looked well-worn — listed as Good; check before approving" in caption
    assert "Condition: Good" in caption                               # Poshmark's label (WO20)


def test_the_extract_prompt_carries_the_owners_condition_rules():
    from thrift_agent.brain import extract
    assert "condition_alternative" in extract.SYSTEM and "choose like_new" in extract.SYSTEM
    assert "the lower one" not in extract.SYSTEM and "never lists Fair" in extract.SYSTEM


# ---------------------------------------------------------------- WO17: better item splitting

@pytest.mark.parametrize("confidence", [0.8, 0.9])
def test_a_roll_with_a_break_shows_the_pause_and_doubts_a_grouping_across_it(tmp_path, monkeypatch, owner_messages,
                                                                             confidence):
    """The pause is always shown; the doubt only where the model itself was unsure (under segmentation.min_confidence
    0.85, WO20): a confident grouping is not second-guessed by a pause and a colour change."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    share = s.path("inbox") / "2026-10-03_1400"
    t0 = datetime(2026, 10, 3, 14, 0)
    for i, (c, sec) in enumerate([("red", 0), ("red", 8), ("red", 16), ("blue", 136), ("blue", 144), ("blue", 152)]):
        img = Image.new("RGB", (300, 400), (128, 128, 128))
        img.paste(c, (70 + 5 * i, 90, 230 + 5 * i, 310))           # a garment on the same grey backdrop
        exif = Image.Exif()
        exif[271], exif[272] = "Apple", "iPhone"
        exif.get_ifd(0x8769)[36867] = (t0 + timedelta(seconds=sec)).strftime("%Y:%m:%d %H:%M:%S")
        share.mkdir(parents=True, exist_ok=True)
        img.save(share / f"IMG_{i:04d}.jpg", exif=exif)
    (share / "_done").touch()
    _settle(share)
    seen = {}

    def ask(model, system, content, out, tool, description, **kw):        # the model lumps it all into one item
        seen["model"], seen["texts"] = model, [c["text"] for c in content if c["type"] == "text"]
        return SegOut(groups=[Group(photos=list(range(6)), summary="red and blue", full_item_photos=[0],
                                    confidence=confidence)])
    monkeypatch.setattr("thrift_agent.brain.llm.ask", ask)
    [folder] = pipeline.ready_folders(s)
    bid = pipeline.register(s, db, folder)
    pipeline.process_batch(s, db, bid)
    assert seen["model"] == "claude-opus-5-5" and "— pause 2 min —" in seen["texts"]
    assert seen["texts"].index("— pause 2 min —") == seen["texts"].index("Photo 3") - 1
    b = db.batch(bid)
    segd = loads(b["segmentation"])
    assert segd["pauses"] == [[3, 120]] and segd["changes"] == [3] and segd["preview_px"] == 768
    doubt = "item 1: a pause (2 min) and a visual change between photos 2 and 3 — two items?"
    caption = pipeline.approve.batch_caption(bid, b)
    assert "pauses before: #3 (2 min)" in caption
    if confidence < 0.85:
        assert doubt in loads(b["reasons"]) and "two items?" in caption
    else:
        assert loads(b["reasons"]) == [] and "Check:" not in caption



# ---------------------------------------------------------------- WO18: shoes, brand new or worn?

def _unworn(value, confidence):
    return Ev(value=value, photos=[2], source="photo", confidence=confidence)


@pytest.mark.parametrize("value,confidence,ask", [
    ("yes", 0.29, False), ("yes", 0.30, True), ("yes", 0.55, True), ("yes", 0.80, True), ("yes", 0.81, False),
    ("yes", 0.97, False),                                   # box, tags, sole stickers: a clear yes, never asked
    ("no", 0.70, True), ("no", 0.71, False),                # "no" at 0.70 = 0.30 sure it is unworn
    ("no", 0.95, False),                                    # sole wear, footbed imprints: a clear no, never asked
    (None, 0.0, False),                                     # no reading at all: no question either
])
def test_the_shoe_doubt_rule_boundaries(facts, value, confidence, ask):
    assert pipeline.shoe_condition_doubt(facts(unworn=_unworn(value, confidence))) is ask


@pytest.mark.parametrize("cond,alt,ask", [
    ("good", "like_new", True), ("like_new", "excellent", True), ("NWOT", "good", True), ("NWT", "fair", True),
    ("NWT", "NWOT", False), ("good", "excellent", False), ("like_new", "NWOT", False), ("good", None, False),
])
def test_two_grades_across_new_and_used_are_a_doubt(facts, cond, alt, ask):
    assert pipeline.shoe_condition_doubt(facts(condition=cond, condition_alternative=alt,
                                               unworn=_unworn("yes", 0.95))) is ask


def test_only_shoes_are_ever_asked(facts):
    for category in ("Dresses", "Bags", "Accessories", "Jackets & Coats"):
        f = facts(category=category, unworn=_unworn("yes", 0.5), condition="good", condition_alternative="like_new")
        assert not pipeline.shoe_condition_doubt(f), category


def _shoe_ask(facts, **kw):
    base = dict(category="Shoes", condition="good", condition_alternative="like_new", unworn=_unworn("yes", 0.6))
    return fake_ask(lambda **k: facts(**{**base, **kw, **k}))


def test_a_shoe_in_doubt_is_asked_before_the_price_and_the_answer_reprices_it(tmp_path, monkeypatch, facts,
                                                                              owner_messages):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _shoe_ask(facts))
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml",
                        lambda name: {"brands": {"tory burch": {"target": 70}}, "aliases": {}, "category_defaults": {}})
    iid = _one_item(tmp_path, db)
    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    assert it["status"] == "awaiting_condition" and owner_messages == [("condition", iid)]     # no price card yet
    before = loads(it["price"])["list_price"]

    assert pipeline.set_condition(s, db, iid, "NWT") == "new"
    assert db.item(iid)["owner_condition"] == "NWT"
    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    facts_after = loads(it["facts"])
    assert facts_after["condition"] == "NWT" and facts_after["condition_evidence"]["source"] == "owner"
    assert it["status"] == "awaiting_price" and owner_messages[-1] == ("item", iid)    # now the price card
    assert loads(it["price"])["list_price"] > before                                     # repriced as new with tags
    assert not any("hang-tag" in r for r in loads(it["gate"])["reasons"])                # the owner's word is the proof
    pipeline.process_item(s, db, iid)                                                    # a later reprocessing
    assert owner_messages.count(("condition", iid)) == 1                                 # never asks again


@pytest.mark.parametrize("choice,condition,line", [("like_new", "NWOT", "New without tags."),
                                                   ("good", "good", "Gently pre-loved")])
def test_the_owners_answer_sets_the_listed_condition(tmp_path, monkeypatch, facts, owner_messages, choice, condition,
                                                     line):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _shoe_ask(facts))
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml",
                        lambda name: {"brands": {"tory burch": {"target": 70}}, "aliases": {}, "category_defaults": {}})
    iid = _one_item(tmp_path, db)
    pipeline.process_item(s, db, iid)
    pipeline.set_condition(s, db, iid, choice)
    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    assert loads(it["facts"])["condition"] == condition and loads(it["renders"])["poshmark"]["condition"] == condition
    from thrift_agent.brain.copy import condition_line
    from thrift_agent.schema import Facts
    assert condition_line(Facts.model_validate(loads(it["facts"]))).startswith(line)


@pytest.mark.parametrize("kw", [
    dict(category="Dresses"),                                               # not shoes: never asked
    dict(unworn=Ev(value="yes", photos=[2], source="photo", confidence=0.97), condition_alternative=None),  # clear new
    dict(unworn=Ev(value="no", photos=[2], source="photo", confidence=0.95), condition_alternative=None),   # clear worn
])
def test_no_question_when_not_shoes_or_when_the_photos_are_clear(tmp_path, monkeypatch, facts, owner_messages, kw):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    monkeypatch.setattr("thrift_agent.brain.llm.ask", _shoe_ask(facts, **kw))
    monkeypatch.setattr("thrift_agent.pipeline.load_yaml",
                        lambda name: {"brands": {"tory burch": {"target": 70}}, "aliases": {}, "category_defaults": {}})
    iid = _one_item(tmp_path, db)
    pipeline.process_item(s, db, iid)
    assert db.item(iid)["status"] == "awaiting_price" and owner_messages == [("item", iid)]


def test_set_condition_refuses_nonsense_and_a_listed_item(tmp_path):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _new_item(s, db)
    db.set_item(iid, status="awaiting_condition")
    with pytest.raises(ValueError, match="must be one of nwt, like_new, good"):
        pipeline.set_condition(s, db, iid, "fair")                       # never Fair (WO17)
    with pytest.raises(ValueError, match="first needs its condition"):
        pipeline.set_price(s, db, iid, 50)                               # the price comes after the answer
    db.set_item(iid, status="posted")
    with pytest.raises(ValueError, match="can't be changed now"):
        pipeline.set_condition(s, db, iid, "good")
    assert pipeline.owner_choice("Like New") == pipeline.owner_choice("like-new") == "like_new"


def test_a_screenshot_is_no_proof_of_an_unworn_pair_or_its_box(facts):
    from thrift_agent.brain.extract import SYSTEM, strip_screenshot_evidence
    f = strip_screenshot_evidence(facts(unworn=Ev(value="yes", photos=[4], source="photo", confidence=0.9),
                                        box_photo=4), retail={4})
    assert f.unworn.value is None and f.box_photo is None
    assert "unworn" in SYSTEM and "box_photo" in SYSTEM



# ---------------------------------------------------------------- WO19: a failed batch, back to the worker

FORCED_400 = 'tool_choice: type "tool" and "any" are not supported for this model.'


def _api_like(groups):
    """The Messages API as claude-opus-5-5 answers it: a forced tool_choice is a 400 (the Mac's failure, WO19);
    tool_choice auto gets the report_groups call. (A copy of test_llm's fake: test modules don't import each other.)"""
    import httpx
    from types import SimpleNamespace
    from anthropic import BadRequestError
    calls = []

    def create(**kw):
        calls.append(kw)
        if kw["model"].startswith("claude-opus-5-5") and kw["tool_choice"]["type"] in ("tool", "any"):
            req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            body = {"type": "error", "error": {"type": "invalid_request_error", "message": FORCED_400}}
            raise BadRequestError(FORCED_400, response=httpx.Response(400, request=req, json=body), body=body)
        block = SimpleNamespace(type="tool_use", name="report_groups", id="toolu_1", input={"groups": groups})
        return SimpleNamespace(content=[block], stop_reason="tool_use")
    return SimpleNamespace(messages=SimpleNamespace(create=create)), calls


def test_a_batch_that_failed_on_the_tool_choice_400_is_requeued_and_split(tmp_path, monkeypatch, owner_messages):
    from thrift_agent.brain import llm
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    share = s.path("inbox") / "2026-10-03_1500"
    t0 = datetime(2026, 10, 3, 15, 0)
    for i, colour in enumerate(["red", "blue"]):
        _own_photo(share / f"IMG_{i:04d}.jpg", colour, t0 + timedelta(seconds=10 * i), i)
    (share / "_done").touch()
    _settle(share)
    [folder] = pipeline.ready_folders(s)
    bid = pipeline.register(s, db, folder)
    db.set_batch(bid, status="failed")                    # what the worker did with the 400 before the fix
    db.log(bid, "error", "Traceback (most recent call last):\n  ...\nanthropic.BadRequestError: Error code: 400 - "
                         + FORCED_400)
    assert pipeline.last_error(db, bid) == "anthropic.BadRequestError: Error code: 400 - " + FORCED_400

    assert pipeline.requeue_batch(s, db, bid) == "new" and [b["id"] for b in db.batches("new")] == [bid]
    groups = [{"photos": [0], "summary": "red top", "full_item_photos": [0], "confidence": 0.95},
              {"photos": [1], "summary": "blue top", "full_item_photos": [1], "confidence": 0.95}]
    c, calls = _api_like(groups)
    monkeypatch.setattr(llm, "client", lambda: c)
    pipeline.process_batch(s, db, bid)                     # what the worker does with a 'new' batch
    assert db.batch(bid)["status"] == "split" and ("batch", bid) not in owner_messages     # accepted at once (WO20b)
    assert loads(db.batch(bid)["segmentation"])["groups"] == [[0], [1]]
    assert [k["model"] for k in calls] == ["claude-opus-5-5"] and calls[0]["tool_choice"]["type"] == "auto"


def test_requeue_batch_takes_only_a_failed_batch_whose_photos_are_still_there(tmp_path):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    with pytest.raises(ValueError, match="unknown batch b_nope"):
        pipeline.requeue_batch(s, db, "b_nope")
    share = _jpg(s.path("inbox") / "share" / "a.jpg").parent
    bid = db.add_batch(str(share), 1)
    with pytest.raises(ValueError, match="is new — only a failed batch can be requeued"):
        pipeline.requeue_batch(s, db, bid)
    db.set_batch(bid, status="failed")
    gone = db.add_batch(str(tmp_path / "moved-away"), 1)
    db.set_batch(gone, status="failed")
    with pytest.raises(ValueError, match="its share folder is gone"):
        pipeline.requeue_batch(s, db, gone)
    assert pipeline.last_error(db, bid) is None
    assert pipeline.requeue_batch(s, db, bid) == "new"
    assert "requeued" in [e["kind"] for e in db.conn.execute("SELECT kind FROM events WHERE ref=?", (bid,))]


# ---------------------------------------------------------------- WO20: the cover is the front of the item; redo

def _roles(*roles):
    from thrift_agent.schema import PhotoRole
    return [PhotoRole(photo=i, role=r) for i, r in enumerate(roles)]


@pytest.mark.parametrize("roles,model_cover,kinds,flawed,cover,note", [
    (("front", "detail", "label", "back"), 3, None, [], 0, None),     # live: the plain back picked over the print
    (("worn", "front", "back"), 0, None, [], 1, None),                # a try-on / mirror photo is never the cover
    (("label", "side", "front"), 0, None, [], 2, None),
    (("tag", "box", "front"), 0, None, [], 2, None),
    (("front", "front"), 1, None, [], 1, None),                       # among fronts, the model's own pick
    (("front", "front"), 0, ["retail", "own"], [], 1, None),          # a screenshot never
    (("front", "front"), 0, None, [0], 0, None),                      # a front that shows a flaw stays (WO23)
    (("worn", "back", "label", "side"), 0, None, [], 3, None),        # a shoe's side profile: a fine cover (WO23)
    (("worn", "back", "label"), 0, None, [], 1, "nf"),
    (("worn", "label"), 0, None, [], 0, "nf"),                        # nothing of the item alone: the model's pick, told
    ((), 2, None, [], 2, None),                                       # facts from before WO20: the model's pick
])
def test_the_cover_is_the_front_of_the_item_alone(facts, roles, model_cover, kinds, flawed, cover, note):
    n = max(len(roles), 3)
    kinds = kinds + ["own"] * (n - len(kinds)) if kinds else None
    f = facts(photo_roles=_roles(*roles), cover_photo=model_cover, photo_order=list(range(n)),
              flaws=[{"description": "small hole", "photos": flawed}] if flawed else [])
    pick, upright, said = pipeline.choose_cover(f, n, kinds)
    assert said == (pipeline.NO_FRONT_COVER if note else None) and upright == 0
    if pick is not None:                                              # what process_item records
        f = f.model_copy(update={"cover_photo": pick})
    order = pipeline.photo_order(f, n, kinds)
    assert order[0] == cover and sorted(order) == list(range(n))


def test_the_rest_of_the_photo_order_stays_and_screenshots_stay_last(facts):
    f = facts(photo_roles=_roles("back", "front", "worn", "front", "label"), cover_photo=0, photo_order=[0, 2, 4, 1, 3])
    kinds = ["own", "own", "own", "retail", "own"]
    pick, _, _ = pipeline.choose_cover(f, 5, kinds)
    assert pick == 1                                                  # the model's back pick is not the cover
    assert pipeline.photo_order(f.model_copy(update={"cover_photo": pick}), 5, kinds) == [1, 0, 2, 4, 3]


def test_no_front_photo_is_said_on_the_card(tmp_path, monkeypatch, facts, owner_messages):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    roles = dict(photo_roles=_roles("worn", "back", "label"), cover_photo=0)
    monkeypatch.setattr("thrift_agent.brain.llm.ask", fake_ask(lambda **kw: facts(**roles, **kw)))
    iid = _one_item(tmp_path, db)
    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    assert loads(it["gate"])["notes"] == [pipeline.NO_FRONT_COVER]
    from thrift_agent.schema import Facts
    assert pipeline.photo_order(Facts(**loads(it["facts"])), 3)[0] == 1          # the back: the item alone
    caption, _ = pipeline.approve.item_caption(iid, it)
    assert "⚠️ cover: no front flat-lay photo" in caption


def _redo_item(tmp_path, db, bid, seq, status, **fields):
    d = tmp_path / f"redo_{seq}"
    for i, c in enumerate(["red", "green", "blue"]):
        _jpg(d / "photos" / f"{i:02d}.jpg", c)
    iid = db.add_item(bid, seq, str(d))
    db.set_item(iid, status=status, **fields)
    return iid


def test_redo_rebuilds_what_never_reached_the_site_and_leaves_the_rest(tmp_path, monkeypatch, facts, owner_messages):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    monkeypatch.setattr("thrift_agent.brain.llm.ask", fake_ask(facts))
    bid = _split(db, "share", 18)
    priced = {"list_price": 40, "source": "owner"}
    ready = _redo_item(tmp_path, db, bid, 1, "ready", owner_price=40, owner_condition="NWOT", price=priced)
    db.upsert_post(ready, "poshmark", status="dryrun")
    waiting = _redo_item(tmp_path, db, bid, 2, "awaiting_price")
    db.add_outbox("-100", 7, "item", waiting)
    posted = _redo_item(tmp_path, db, bid, 3, "posted")
    db.upsert_post(posted, "poshmark", status="posted", url="https://example.invalid/listing/x")
    unsure = _redo_item(tmp_path, db, bid, 4, "failed")
    db.upsert_post(unsure, "poshmark", status="failed", last_error="unconfirmed publish: no URL after Next")
    dropped = _redo_item(tmp_path, db, bid, 5, "dropped")
    broken = _redo_item(tmp_path, db, bid, 6, "failed")                    # processing failed: rebuilt too

    rebuilt, kept = pipeline.redo_batch(s, db, bid)
    assert rebuilt == [ready, waiting, broken]
    assert kept == [f"{posted} (poshmark posted)", f"{unsure} (poshmark: unconfirmed publish)", f"{dropped} (dropped)"]
    for iid in rebuilt:
        it = db.item(iid)
        assert (it["status"], it["owner_price"], it["price"], it["renders"]) == ("new", None, None, None)
    assert db.item(ready)["owner_condition"] == "NWOT"                   # an answer about the item itself stays
    assert db.post(ready, "poshmark") is None and db.outbox_pending() == []
    assert db.post(posted, "poshmark")["status"] == "posted" and db.item(posted)["status"] == "posted"
    assert db.post(unsure, "poshmark")["last_error"].startswith("unconfirmed publish")

    for iid in rebuilt:                                                   # the worker, from the confirmed grouping:
        pipeline.process_item(s, db, iid)
    assert db.batch(bid)["status"] == "split" and owner_messages == [("item", ready)]   # no sheet; one card at a time
    assert {db.item(i)["status"] for i in rebuilt} == {"awaiting_price"}


def test_redo_refuses_a_batch_with_nothing_to_rebuild(tmp_path):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    bid = _split(db, "share", 3)
    posted = _redo_item(tmp_path, db, bid, 1, "posted")
    with pytest.raises(ValueError, match=re.escape(f"every item reached the site ({posted} (posted))")):
        pipeline.redo_batch(s, db, bid)
    waiting = db.add_batch("share-2", 3)
    db.set_batch(waiting, status="needs_confirm")
    with pytest.raises(ValueError, match="no items"):
        pipeline.redo_batch(s, db, waiting)
    with pytest.raises(ValueError, match="unknown batch"):
        pipeline.redo_batch(s, db, "b_000000_nope")


def test_an_answer_that_arrives_during_processing_is_not_overwritten(tmp_path, monkeypatch, facts, owner_messages):
    """The worker answers Telegram on its own thread (WO20): a note that lands while the item is being processed makes
    that result out of date. The item stays 'new' (no card goes out) and the next turn processes it with the note."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _one_item(tmp_path, db)
    calls = []
    base = fake_ask(facts)

    def ask(model, system, content, out, *a, **kw):
        if out.__name__ == "Facts" and not calls:
            calls.append(1)
            time.sleep(1.1)                                              # updated_at has whole seconds
            pipeline.answer(s, db, iid, "brand Vince")                   # the owner's reply, handled meanwhile
        return base(model, system, content, out, *a, **kw)
    monkeypatch.setattr("thrift_agent.brain.llm.ask", ask)
    pipeline.process_item(s, db, iid)
    it = db.item(iid)
    assert (it["status"], it["note"], it["facts"]) == ("new", "brand Vince", None) and owner_messages == []
    pipeline.process_item(s, db, iid)                                    # the worker's next turn
    assert db.item(iid)["status"] == "awaiting_price" and owner_messages == [("item", iid)]
