from datetime import datetime, timedelta

import pytest
from PIL import Image

from thrift_agent.ingest import prep
from thrift_agent.ingest.segment import Group, SegOut, apply_correction, check, contact_sheet


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
