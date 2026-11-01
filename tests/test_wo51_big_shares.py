"""WO51 §6 (2026-11-01): a big share is never cut short — a roll past `segmentation.part_photos` is grouped in
overlapping parts and stitched (an item that straddles a seam stays one item); a roll up to it is ONE call exactly as
before (the golden replays' recorded answers still apply); every photo is in an item or told in one private line.

Live that day: a 70-photo share went to the model in one call; the upload took 60 at most and a share past 90 photos
failed outright ("share it in smaller sets")."""
import random
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from thrift_agent import approve, photomem, pipeline, shares
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB, loads
from thrift_agent.ingest import segment as seg
from thrift_agent.ingest.segment import Group, SegOut

T0 = datetime(2026, 11, 1, 12, 39, 25)


def runs(sizes: list[int]) -> list[int]:
    """The true item of each photo for items of these sizes, shot one after the other."""
    return [k for k, n in enumerate(sizes) for _ in range(n)]


def truthful(truth: list[int], calls: list | None = None, wrong=None):
    """A stand-in for the model: groups each part's photos by their true item (photo i of the call is the roll's
    photo `first + i`, read from the path); `wrong(first, labels)` may change the labels first (a part's mistake)."""
    def segment(photos, model, thumb_px, kinds=None, breaks=None, fallback_px=None, max_bytes=0, report=None,
                lead=None):
        first = int(Path(photos[0][0]).stem)
        labels = [truth[int(Path(p).stem)] for p, _ in photos]
        if wrong is not None:
            labels = wrong(first, labels)
        if calls is not None:
            calls.append({"first": first, "n": len(photos), "lead": lead, "breaks": dict(breaks or {})})
        order = list(dict.fromkeys(labels))
        groups = [Group(photos=[i for i, x in enumerate(labels) if x == lab], summary=f"item {lab}",
                        full_item_photos=[labels.index(lab)], confidence=0.95) for lab in order]
        if report is not None:
            report["preview_px"] = thumb_px
        return SegOut(groups=groups)
    return segment


def roll(n: int) -> list[tuple[Path, datetime]]:
    return [(Path(f"{i:03d}.jpg"), T0 + timedelta(seconds=20 * i)) for i in range(n)]


def as_items(out: SegOut) -> list[list[int]]:
    return [g.photos for g in out.groups]


def truth_items(truth: list[int]) -> list[list[int]]:
    return [[i for i, x in enumerate(truth) if x == k] for k in dict.fromkeys(truth)]


# ---------------------------------------------------------------- the windows

@pytest.mark.parametrize("n", [1, 2, 29, 40])
def test_a_roll_up_to_forty_photos_is_one_window(n):
    assert seg.plan_parts(n) == [(0, n)]


@pytest.mark.parametrize("n", [41, 70, 90, 150, 200, 333])
def test_a_bigger_roll_is_cut_in_even_overlapping_windows(n):
    w = seg.plan_parts(n)
    assert w[0][0] == 0 and w[-1][1] == n and len(w) >= 2
    assert all(e - s <= seg.PART_PHOTOS for s, e in w)                        # each small enough for one call
    assert all(w[k][1] - w[k + 1][0] == seg.PART_OVERLAP for k in range(len(w) - 1))   # neighbours share 8
    assert max(e - s for s, e in w) - min(e - s for s, e in w) <= seg.PART_OVERLAP + 1  # no short last part
    assert all(w[k + 1][0] > w[k - 1][1] for k in range(1, len(w) - 1))         # each part has photos of its own


def test_the_windows_of_seventy_and_a_hundred_and_fifty():
    assert seg.plan_parts(70) == [(0, 39), (31, 70)]
    assert seg.plan_parts(150) == [(0, 37), (29, 66), (58, 95), (87, 124), (116, 150)]


# ---------------------------------------------------------------- the stitch

def test_up_to_forty_photos_is_the_same_single_call_as_before(monkeypatch):
    calls = []
    monkeypatch.setattr(seg, "segment", truthful(runs([5, 6, 7, 11]), calls))
    breaks = {5: 120.0, 11: 90.0}
    report: dict = {}
    out = seg.segment_roll(roll(29), "m", 768, kinds=["own"] * 29, breaks=breaks, report=report)
    assert calls == [{"first": 0, "n": 29, "lead": None, "breaks": breaks}]   # one call, no part line, same pauses
    assert as_items(out) == truth_items(runs([5, 6, 7, 11]))
    assert "parts" not in report and report["preview_px"] == 768


@pytest.mark.parametrize("sizes", [
    [5, 5, 6, 3, 3, 7, 6, 8, 10, 6, 5, 6],          # the 70-photo share of 2026-11-01, item by item
    [70],                                            # one item over the whole roll
    [30, 10, 30],                                    # an item across the seam (31–38)
    [35, 4, 31],                                     # a short item inside the overlap
    [31, 39],                                        # an item boundary right at the overlap's start
    [38, 1, 31],                                     # a one-photo item in the overlap
])
def test_seventy_photos_in_two_parts_keep_every_item_whole(monkeypatch, sizes):
    truth = runs(sizes)
    assert len(truth) == 70
    calls = []
    monkeypatch.setattr(seg, "segment", truthful(truth, calls))
    report: dict = {}
    out = seg.segment_roll(roll(70), "m", 768, report=report)
    assert [(c["first"], c["n"]) for c in calls] == [(0, 39), (31, 39)]
    assert all(c["lead"].startswith("This is part ") for c in calls)
    assert as_items(out) == truth_items(truth)
    assert sorted(p for g in out.groups for p in g.photos) == list(range(70))  # every photo, once
    assert report["parts"] == [[0, 39], [31, 70]] and report["doubts"] == []


def test_a_hundred_and_fifty_photos_place_every_photo_in_its_item(monkeypatch):
    rng = random.Random(51)
    for _ in range(40):
        sizes, left = [], 150
        while left:
            k = min(left, rng.choice([1, 2, 3, 4, 5, 6, 6, 7, 8, 10, 12, 20]))
            sizes.append(k)
            left -= k
        truth = runs(sizes)
        monkeypatch.setattr(seg, "segment", truthful(truth))
        out = seg.segment_roll(roll(150), "m", 768)
        assert as_items(out) == truth_items(truth), sizes
        assert sorted(p for g in out.groups for p in g.photos) == list(range(150))


def test_pauses_go_to_the_part_they_fall_in_and_never_before_its_first_photo(monkeypatch):
    calls = []
    monkeypatch.setattr(seg, "segment", truthful(runs([31, 39]), calls))
    seg.segment_roll(roll(70), "m", 768, breaks={10: 60.0, 31: 300.0, 35: 90.0, 60: 45.0})
    assert calls[0]["breaks"] == {10: 60.0, 31: 300.0, 35: 90.0}             # part 1 sees photos 0–38
    assert calls[1]["breaks"] == {4: 90.0, 29: 45.0}                          # part 2 starts at 31: its own pause gone


def test_a_parts_mistake_near_its_own_edge_neither_merges_nor_splits(monkeypatch):
    """The overlap of 70 photos is 31–38, cut at 35. Item A = 0–36, item B = 37–69 (the boundary in part 2's half).
    Part 1, near its own end, wrongly calls 37–38 the same item as A: part 2 — trusted there — decides; A and B stay
    apart. Then the other way: part 2, near its own start, wrongly splits A's tail 31–32 off: part 1 decides."""
    truth = runs([37, 33])

    def part1_merges(first, labels):
        return [0 if first == 0 and first + i in (37, 38) else x for i, x in enumerate(labels)]
    monkeypatch.setattr(seg, "segment", truthful(truth, wrong=part1_merges))
    report: dict = {}
    out = seg.segment_roll(roll(70), "m", 768, report=report)
    assert as_items(out) == truth_items(truth)
    assert report["doubts"] and "photos 31–38" in report["doubts"][0]          # the disagreement is a doubt

    def part2_splits(first, labels):
        return [9 if first == 31 and first + i in (31, 32) else x for i, x in enumerate(labels)]
    monkeypatch.setattr(seg, "segment", truthful(truth, wrong=part2_splits))
    out = seg.segment_roll(roll(70), "m", 768)
    assert as_items(out) == truth_items(truth)


def test_a_photo_no_part_placed_is_in_no_item_and_the_check_says_so(monkeypatch):
    truth = runs([20, 50])

    def drop_33(first, labels):
        return labels
    real = truthful(truth, wrong=drop_33)

    def segment(photos, *a, **k):
        out = real(photos, *a, **k)
        first = int(Path(photos[0][0]).stem)
        out.groups = [g.model_copy(update={"photos": [i for i in g.photos if first + i != 33]}) for g in out.groups]
        return out
    monkeypatch.setattr(seg, "segment", segment)
    out = seg.segment_roll(roll(70), "m", 768)
    assert 33 not in [p for g in out.groups for p in g.photos]
    assert any("not partitioned" in r and "33" in r for r in seg.check(out, 70, 0.85))


def test_a_screenshot_no_part_matches_is_unassigned(monkeypatch):
    truth = runs([40, 30])
    real = truthful(truth)

    def segment(photos, *a, **k):
        out = real(photos, *a, **k)
        first = int(Path(photos[0][0]).stem)
        if first <= 65 < first + len(photos):
            local = 65 - first
            out.groups = [g.model_copy(update={"photos": [i for i in g.photos if i != local]}) for g in out.groups]
            out.screenshots, out.unassigned = [local], [local]
        return out
    monkeypatch.setattr(seg, "segment", segment)
    out = seg.segment_roll(roll(70), "m", 768)
    assert out.unassigned == [65] and out.screenshots == [65]
    assert sorted(p for g in out.groups for p in g.photos) == [p for p in range(70) if p != 65]


def test_the_stitched_items_keep_their_full_shots_labels_sizes_and_lowest_confidence(monkeypatch):
    truth = runs([30, 10, 30])

    def segment(photos, model, thumb_px, **k):
        first = int(Path(photos[0][0]).stem)
        labels = [truth[first + i] for i in range(len(photos))]
        groups = []
        for lab in dict.fromkeys(labels):
            idx = [i for i, x in enumerate(labels) if x == lab]
            label = [i for i in idx if first + i == 33]                         # item 1's size label: photo 33
            groups.append(Group(photos=idx, summary=f"item {lab} seen from {first}", full_item_photos=idx[:1],
                                label_photos=label, sizes_read=["M"] if label else [],
                                confidence=0.9 if first else 0.8))
        return SegOut(groups=groups)
    monkeypatch.setattr(seg, "segment", segment)
    out = seg.segment_roll(roll(70), "m", 768)
    mid = out.groups[1]
    assert mid.photos == list(range(30, 40)) and mid.label_photos == [33] and mid.sizes_read == ["M"]
    assert mid.confidence == 0.8 and 30 in mid.full_item_photos


# ---------------------------------------------------------------- the pipeline, end to end

@pytest.fixture
def env(tmp_path, monkeypatch):
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    data["telegram"] = {**base.get("telegram", {}), "enabled": False}
    s = Settings(data)
    s.ensure_dirs()
    db = DB(s.path("db"))
    said = []
    monkeypatch.setattr(pipeline.notify, "say", said.append)
    monkeypatch.setattr(pipeline.notify, "group", lambda *a, **k: None)
    monkeypatch.setattr(photomem.notify, "say", said.append)
    monkeypatch.setattr(approve, "bot_for", lambda _s: None)
    monkeypatch.setattr(pipeline, "process_item", lambda *a, **k: None)
    return s, db, said


def _share(s, name: str, n: int) -> Path:
    """n distinct photos, 20 s apart by their EXIF (never a burst), each with its own pattern."""
    d = s.path("inbox") / name
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        im = Image.new("RGB", (60, 80), (37 * i % 256, 91 * i % 256, 53 * i % 256))
        draw = ImageDraw.Draw(im)
        draw.rectangle([i % 50, (3 * i) % 70, i % 50 + 9, (3 * i) % 70 + 9], fill=(255 - 37 * i % 256, 0, 255))
        ex = Image.Exif()
        ex[photomem.MODEL] = "iPhone 16 Pro Max"
        ifd = ex.get_ifd(photomem.EXIF_IFD)
        t = T0 + timedelta(seconds=20 * i)
        ifd[photomem.DATE_ORIGINAL] = t.strftime("%Y:%m:%d %H:%M:%S")
        ifd[photomem.SUBSEC_ORIGINAL] = str(100 + i)
        im.save(d / f"IMG_{785 + i:04d}.jpeg", exif=ex.tobytes())
    (d / "_done.txt").write_text(f"done {n}", encoding="utf-8")
    return d


def test_a_seventy_photo_share_is_grouped_in_two_parts_into_its_twelve_items(env, monkeypatch):
    s, db, said = env
    truth = runs([5, 5, 6, 3, 3, 7, 6, 8, 10, 6, 5, 6])
    calls = []
    monkeypatch.setattr(pipeline.seg, "segment", truthful(truth, calls))
    bid = pipeline.register(s, db, _share(s, "2026-11-01_123925_5986", 70))
    pipeline.process_batch(s, db, bid)
    assert db.batch(bid)["status"] == "split"
    assert [(c["first"], c["n"]) for c in calls] == [(0, 39), (31, 39)]
    segd = loads(db.batch(bid)["segmentation"])
    assert segd["parts"] == [[0, 39], [31, 70]] and segd["groups"] == truth_items(truth)
    items = db.conn.execute("SELECT id, dir FROM items WHERE batch_id=? ORDER BY seq", (bid,)).fetchall()
    assert len(items) == 12
    counts = [len(list((Path(r["dir"]) / "photos").glob("*.jpg"))) for r in items]
    assert counts == [5, 5, 6, 3, 3, 7, 6, 8, 10, 6, 5, 6] and sum(counts) == 70     # every photo in its item
    assert not [line for line in said if "in no item" in line]


def test_a_hundred_and_fifty_photo_share_is_no_longer_refused(env, monkeypatch):
    s, db, said = env
    rng = random.Random(7)
    sizes, left = [], 150
    while left:
        k = min(left, rng.randint(3, 9))
        sizes.append(k)
        left -= k
    truth = runs(sizes)
    calls = []
    monkeypatch.setattr(pipeline.seg, "segment", truthful(truth, calls))
    bid = pipeline.register(s, db, _share(s, "2026-11-01_150000_ab12", 150))
    pipeline.process_batch(s, db, bid)
    assert db.batch(bid)["status"] == "split" and len(calls) == 5 and max(c["n"] for c in calls) <= 40
    assert loads(db.batch(bid)["segmentation"])["groups"] == truth_items(truth)
    assert db.conn.execute("SELECT COUNT(*) FROM items WHERE batch_id=?", (bid,)).fetchone()[0] == len(sizes)


def test_a_screenshot_in_no_item_is_one_private_line_naming_it(env, monkeypatch):
    s, db, said = env

    def segment(photos, *a, **k):
        return SegOut(groups=[Group(photos=[0, 1], summary="green top", full_item_photos=[0], confidence=0.95)],
                      screenshots=[2], unassigned=[2])
    monkeypatch.setattr(pipeline.seg, "segment", segment)
    bid = pipeline.register(s, db, _share(s, "2026-11-01_123925_5986", 3))
    pipeline.process_batch(s, db, bid)
    assert db.batch(bid)["status"] == "split"
    [line] = [x for x in said if "in no item" in x]
    assert line == ("📸 1 of the 12:39 PM share's photos is in no item: IMG_0787.jpeg — a screenshot that matches none "
                    "of its items.")


def test_a_lone_photo_in_a_long_roll_is_compared_with_its_nearest_twelve_groups(env, monkeypatch, tmp_path):
    s, db, said = env
    photos = []
    for i in range(31):
        p = tmp_path / f"{i:03d}.jpg"
        Image.new("RGB", (8, 8), (i, i, i)).save(p)
        photos.append(p)
    groups = [[0, 1, 2]] + [[i] for i in range(3, 31)]                         # 29 groups, 28 of one photo
    seen = []

    def attach(new, refs, model, px):
        seen.append([r[1] for r in refs])
        return seg.AttachOut(photos=[])
    monkeypatch.setattr(pipeline.seg, "attach", attach)
    pipeline.merge_singletons(s, db, "b_1", photos, ["own"] * 31, groups, [f"g{k}" for k in range(len(groups))])
    assert seen and all(len(r) <= pipeline.MAX_ATTACH_GROUPS for r in seen)
    first = seen[0]                                                            # group 1 (photo 3): its neighbours
    assert first == [f"g{k}" for k in [0, *range(2, 13)]]


# ---------------------------------------------------------------- what an open share waits for (the watchdog's input)

SID = "2026-11-01_123925_5986"
NAMES = ["Edikted Crochet Mini Dress", "Alo Yoga Bra", "Zara Mini Skirt", "Levi's 501 Shorts"]


def _ledger_share(db, n_items=4, status="awaiting_price"):
    """A share grouped into `n_items` items, each processed and waiting for its price card (none sent yet)."""
    bid = db.add_batch(f"/inbox/{SID}", 20)
    db.set_batch(bid, status="split")
    shares.seen(db, SID, "azure", expected=20, received=20, batch_id=bid)
    shares.step(db, SID, "complete", batch_id=bid)
    shares.step(db, SID, "grouped", batch_id=bid)
    ids = []
    for k in range(n_items):
        iid = db.add_item(bid, k + 1, f"/work/{bid}/item_{k + 1:02d}")
        db.set_item(iid, status=status, renders={"poshmark": {"title": NAMES[k % len(NAMES)] + " size M",
                                                               "short_name": NAMES[k % len(NAMES)]}})
        ids.append(iid)
    return bid, ids


def _card(db, iid, mid, resolved=False):
    db.conn.execute("INSERT INTO outbox (chat_id, message_id, kind, ref, text, sent_at, resolved_at) "
                    "VALUES ('c', ?, 'item', ?, 'card', '2026-11-01T17:44:00+00:00', ?)",
                    (mid, iid, "2026-11-01T17:50:00+00:00" if resolved else None))


def _row(db):
    return db.conn.execute("SELECT * FROM shares WHERE id=?", (SID,)).fetchone()


def test_a_share_whose_cards_wait_behind_her_open_one_waits_for_her(env):
    s, db, said = env
    bid, ids = _ledger_share(db)
    _card(db, ids[0], 11)                                                      # the first card is open, unanswered
    st = shares.queue_state(db, _row(db))
    assert st["state"] == "her" and st["items"] == 4 and st["cards"] == 1
    assert st["waiting"] == 'her price on "Edikted Crochet Mini Dress" — 3 more cards behind it'
    assert shares.refresh_open(db, force=True) == [SID]
    row = _row(db)
    assert row["state"] == "her" and row["waiting"] == st["waiting"] and row["outcome"] is None
    assert shares.refresh_open(db, force=True) == []                           # nothing changed: nothing written
    assert all(k in shares.doc(db, SID) for k in shares.QUEUE_FIELDS)          # synced to the API's watchdog


def test_a_frozen_queue_or_an_item_with_no_place_in_it_is_not_waiting_for_her(env):
    s, db, said = env
    bid, ids = _ledger_share(db)
    _card(db, ids[0], 11, resolved=True)                                       # she answered the first ...
    db.set_item(ids[0], status="ready")
    st = shares.queue_state(db, _row(db))                                      # ... and no next card went out
    assert st["state"] == "send" and st["waiting"] == "the next card to go out (3 queued, none open for her)"
    db.set_item(ids[2], status="failed")                                       # an item with no card, out of the queue
    st = shares.queue_state(db, _row(db))
    assert st["state"] == "orphan"
    assert st["waiting"] == 'a card for "Zara Mini Skirt" — it has none and no place in the card queue (failed)'


def test_items_still_being_processed_are_work_and_every_card_sent_is_the_outcome(env):
    s, db, said = env
    bid, ids = _ledger_share(db, status="new")
    st = shares.queue_state(db, _row(db))
    assert st["state"] == "work" and st["waiting"] == "its items to be processed (0 of 4 done)"
    for k, iid in enumerate(ids):
        db.set_item(iid, status="awaiting_price")
        _card(db, iid, 20 + k, resolved=k < 3)
        if k < 3:
            db.set_item(iid, status="ready")
    st = shares.queue_state(db, _row(db))
    assert st["state"] == "done" and st["cards"] == 4
    shares.refresh_open(db, force=True)
    assert _row(db)["outcome"] == "carded"


def test_the_ledger_is_looked_at_once_a_minute(env):
    s, db, said = env
    _ledger_share(db)
    assert shares.refresh_open(db, at=1000.0) == [SID]
    db.conn.execute("UPDATE shares SET state=NULL WHERE id=?", (SID,))
    assert shares.refresh_open(db, at=1030.0) == []                            # 30 s later: not yet
    assert shares.refresh_open(db, at=1061.0) == [SID]
