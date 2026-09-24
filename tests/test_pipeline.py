"""End-to-end with the model stubbed out: inbox folder → batch → confirm → items → gate → renders."""
from datetime import datetime, timedelta

from PIL import Image

from thrift_agent import pipeline
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB, loads
from thrift_agent.ingest.segment import Group, SegOut
from thrift_agent.schema import CopyOut, VerifyOut


def fake_ask(facts_factory):
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
            return VerifyOut(poshmark_title="Tory Burch Red Suede Ballet Flats size 7.5",
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
