from datetime import datetime, timedelta

import pytest
from PIL import Image

from thrift_agent.brain import llm
from thrift_agent.ingest import prep, segment
from thrift_agent.ingest.segment import Group, SegOut, apply_correction, check, contact_sheet, parse_drops


def g(photos, conf=0.95, full=None, sizes=()):
    return Group(photos=photos, summary="x", full_item_photos=full if full is not None else photos[:1],
                 sizes_read=list(sizes), confidence=conf)


def test_check_clean():
    assert check(SegOut(groups=[g([0, 1, 2]), g([3, 4])]), 5, 0.85) == []


def test_check_flags_problems():
    reasons = check(SegOut(groups=[g([0, 1], sizes=["8", "7.5"]), g([3, 2], conf=0.5, full=[])]), 5, 0.85)
    text = " ".join(reasons)
    assert "missing=[4]" in text and "conflicting sizes" in text and "no full-item" in text
    assert "low confidence" in text and "non-contiguous" in text


def test_check_flags_empty_group():
    reasons = check(SegOut(groups=[g([0, 1, 2]), Group(photos=[], summary="?", full_item_photos=[], confidence=0.9)]),
                    3, 0.85)
    assert "item 2: empty group" in reasons


def test_corrections_can_place_a_photo_the_model_left_out():
    groups = [[0, 1], [3, 4]]                                    # photo 2 is in no item (sheet shows "item 0")
    assert apply_correction(groups, "ok", n=5) == groups           # accepted as-is; the caller checks coverage
    assert apply_correction(groups, "2>1", n=5) == [[0, 1, 2], [3, 4]]
    assert apply_correction(groups, "2>3", n=5) == [[0, 1], [3, 4], [2]]
    assert apply_correction(groups, "split 2", n=5) == [[0, 1], [3, 4], [2]]
    for bad in ("5>1", "99>1", "split 5"):                       # n=5: photos are 0..4
        with pytest.raises(ValueError, match="no photo"):
            apply_correction(groups, bad, n=5)


def test_corrections():
    groups = [[0, 1, 2], [3, 4, 5], [6, 7]]
    assert apply_correction(groups, "ok") == groups
    assert apply_correction(groups, "7>1") == [[0, 1, 2, 7], [3, 4, 5], [6]]
    assert apply_correction(groups, "split 4") == [[0, 1, 2], [3], [4, 5], [6, 7]]
    assert apply_correction(groups, "merge 2 3") == [[0, 1, 2], [3, 4, 5, 6, 7]]
    assert apply_correction(groups, "2>2, merge 1 2") == [[0, 1, 2, 3, 4, 5], [6, 7]]
    with pytest.raises(ValueError):
        apply_correction(groups, "banana")


def test_prep_and_sheet(tmp_path):
    t0 = datetime(2026, 9, 21, 14, 0, 0)
    colors = ["red", "red", "blue", "blue", "green"]
    for i, c in enumerate(colors):
        p = tmp_path / "in" / f"IMG_{i}.jpg"
        p.parent.mkdir(exist_ok=True)
        img = Image.new("RGB", (400, 600), c)
        for x in range(0, 400, 40 + i * 7):            # make each photo distinct for phash
            for y in range(0, 600, 3):
                img.putpixel((x, y), (255, 255, 255))
        exif = Image.Exif()
        exif.get_ifd(0x8769)[36867] = (t0 + timedelta(seconds=5 * i)).strftime("%Y:%m:%d %H:%M:%S")
        img.save(p, exif=exif)
    listed = prep.list_photos(tmp_path / "in")
    assert [p.name for p, _ in listed] == [f"IMG_{i}.jpg" for i in range(5)]
    norm = [prep.normalize(p, tmp_path / "n" / f"{i:03d}.jpg", 256) for i, (p, _) in enumerate(listed)]
    times = [t for _, t in listed]
    kept, dropped = prep.drop_near_duplicates(norm, times, 64, max_seconds=4)   # everything "looks" alike
    assert len(kept) == 5                                                  # …but 5s apart → all kept
    kept2, dropped2 = prep.drop_near_duplicates(norm, times, 64, max_seconds=30)
    assert len(kept2) == 1 and len(dropped2) == 4
    cover = prep.square_cover(kept[0], tmp_path / "cover.jpg", 300)
    assert Image.open(cover).size == (300, 300)
    sheet = contact_sheet(kept, [[0, 1], list(range(2, len(kept)))], tmp_path / "sheet.png")
    assert sheet.exists()


def test_sheet_labels_are_phone_readable(tmp_path):
    photos = []
    for i, c in enumerate(["red", "blue", "green"]):
        p = tmp_path / f"{i}.jpg"
        Image.new("RGB", (120, 160), c).save(p)
        photos.append(p)
    with Image.open(contact_sheet(photos, [[0, 1], [2]], tmp_path / "sheet.png", tile=200, cols=3)) as sheet:
        assert sheet.size == (600, 240)                                       # one row: tile + 40 px label strip
        strip = sheet.crop((0, 200, 200, 240)).convert("L")
        dark = sum(1 for v in strip.tobytes() if v < 200)
        assert dark > 150                                                     # 26 px text, not the 10 px bitmap font


def test_corrections_reject_nonsense():
    groups = [[0, 1, 2], [3, 4, 5], [6, 7]]
    for bad in ("12>0", "99>2", "4>5", "merge 2 2", "merge 0 1", "merge 1 9", "split 99"):
        with pytest.raises(ValueError):
            apply_correction(groups, bad)
    assert apply_correction(groups, "7>4") == [[0, 1, 2], [3, 4, 5], [6], [7]]   # item N+1 = a new item
    assert groups == [[0, 1, 2], [3, 4, 5], [6, 7]]                              # input never mutated


# --- retail screenshots ---------------------------------------------------------------------------------

def kinds_for(n, retail=()):
    return ["retail" if i in retail else "own" for i in range(n)]


def test_check_retail_photo_does_not_break_contiguity():
    seg = SegOut(groups=[g([0, 1, 5]), g([2, 3, 4])])            # 5 is a screenshot shared last, of item 1
    assert check(seg, 6, 0.85, kinds=kinds_for(6, retail={5})) == []
    assert "non-contiguous" in " ".join(check(seg, 6, 0.85))     # without kinds it still is
    assert check(SegOut(groups=[g([0, 1, 5]), g([2, 3, 4])], screenshots=[5]), 6, 0.85) == []   # model-spotted


def test_check_own_photos_still_must_be_contiguous_around_retail():
    seg = SegOut(groups=[g([0, 3, 5]), g([1, 2, 4])])
    text = " ".join(check(seg, 6, 0.85, kinds=kinds_for(6, retail={5})))
    assert "item 1: non-contiguous photos [0, 3]" in text and "item 2: non-contiguous photos [1, 2, 4]" in text


def test_check_group_of_only_screenshots():
    seg = SegOut(groups=[g([0, 1, 2, 3, 4]), g([5], full=[5])])
    reasons = check(seg, 6, 0.85, kinds=kinds_for(6, retail={5}))
    assert reasons == ["item 2: only screenshots, no own photo"]
    seg = SegOut(groups=[g([0, 1, 5], full=[5]), g([2, 3, 4])])  # a screenshot is not a full-item photo
    assert check(seg, 6, 0.85, kinds=kinds_for(6, retail={5})) == ["item 1: no full-item photo"]


def test_check_unassigned_screenshot():
    seg = SegOut(groups=[g([0, 1, 2]), g([3, 4, 5, 6])], unassigned=[7])
    reasons = check(seg, 8, 0.85, kinds=kinds_for(8, retail={7}))
    assert reasons == ["screenshot 7 matches no item — reply '7>2' (into item 2) or 'drop 7'"]   # partition passes
    seg = SegOut(groups=[g([0, 1, 2]), g([3, 4, 5, 6, 7])], unassigned=[7])
    assert "duplicated=[7]" in " ".join(check(seg, 8, 0.85, kinds=kinds_for(8, retail={7})))


def test_parse_drops():
    assert parse_drops("drop 7, 3>2, drop 9") == ("3>2", [7, 9])
    assert parse_drops("ok") == ("ok", [])
    assert parse_drops("drop 7") == ("ok", [7])
    assert parse_drops("Drop 7, merge 1 2") == ("merge 1 2", [7])
    assert parse_drops("5>1, split 3") == ("5>1, split 3", [])


def test_segment_labels_retail_screenshots(tmp_path, monkeypatch):
    t0 = datetime(2026, 9, 21, 14, 0, 0)
    photos = []
    for i, c in enumerate(["red", "blue", "green"]):
        p = tmp_path / f"{i}.jpg"
        Image.new("RGB", (60, 80), c).save(p)
        photos.append((p, t0 + timedelta(seconds=10 * i)))
    captured = {}

    def fake_ask(model, system, content, out, tool, description, **kw):
        captured.update(system=system, content=content)
        return SegOut(groups=[])

    monkeypatch.setattr(llm, "ask", fake_ask)
    segment.segment(photos, "m", 64, kinds=["own", "own", "retail"])
    labels = [c["text"] for c in captured["content"] if c.get("type") == "text"]
    assert labels[:3] == ["Photo 0 · t=+0s", "Photo 1 · t=+10s", "Photo 2 · retail screenshot"]
    assert "unassigned" in captured["system"] and "ONE size" in captured["system"]
    segment.segment(photos, "m", 64)                                             # no kinds = all own
    assert [c["text"] for c in captured["content"] if c.get("type") == "text"][2] == "Photo 2 · t=+20s"


def test_contact_sheet_with_kinds(tmp_path):
    photos = []
    for i, c in enumerate(["red", "blue", "green", "gray"]):
        p = tmp_path / f"{i}.jpg"
        Image.new("RGB", (120, 160), c).save(p)
        photos.append(p)
    sheet = contact_sheet(photos, [[0, 1, 2]], tmp_path / "sheet.png", tile=200, cols=4,
                          kinds=["own", "own", "retail", "retail"])                # 2 in item 1, 3 in no item
    assert sheet.exists() and Image.open(sheet).size == (800, 240)
