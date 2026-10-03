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
    assert labels[:3] == ["Photo 0", "Photo 1", "Photo 2 · retail screenshot"]     # WO17: no clock, pauses only
    assert "unassigned" in captured["system"] and "ONE size" in captured["system"]
    segment.segment(photos, "m", 64)                                             # no kinds = all own
    assert [c["text"] for c in captured["content"] if c.get("type") == "text"][2] == "Photo 2"


def test_contact_sheet_with_kinds(tmp_path):
    photos = []
    for i, c in enumerate(["red", "blue", "green", "gray"]):
        p = tmp_path / f"{i}.jpg"
        Image.new("RGB", (120, 160), c).save(p)
        photos.append(p)
    sheet = contact_sheet(photos, [[0, 1, 2]], tmp_path / "sheet.png", tile=200, cols=4,
                          kinds=["own", "own", "retail", "retail"])                # 2 in item 1, 3 in no item
    assert sheet.exists() and Image.open(sheet).size == (800, 240)



# ---------------------------------------------------------------- WO17: better item splitting

T0 = datetime(2026, 10, 3, 14, 0, 0)


def at(*seconds):
    return [T0 + timedelta(seconds=s) for s in seconds]


def test_a_pause_is_relative_to_the_rolls_own_rhythm():
    burst = at(0, 10, 20, 90, 100, 110)                    # 10 s apart, then a 70 s break: a pause
    assert segment.pauses(burst) == {3: 70.0}
    slow = at(0, 20, 40, 100, 120, 140)                    # 20 s apart: 60 s is not 4 x the usual gap
    assert segment.pauses(slow) == {}
    quick = at(0, 2, 4, 40, 42, 44)                        # 2 s apart: the 30 s floor still applies (36 s > 30 s)
    assert segment.pauses(quick) == {3: 36.0}
    assert segment.pauses(at(0, 500)) == {}                # one gap: nothing to compare it with


def test_retail_screenshots_take_no_part_in_the_timing():
    times = at(0, 10, 9999, 20, 90, 100)                   # a screenshot's file time sits in the middle
    kinds = ["own", "own", "retail", "own", "own", "own"]
    assert segment.pauses(times, kinds) == {4: 70.0}       # 20 -> 90 between own photos 3 and 4
    assert segment.own_pairs(kinds) == [(0, 1), (1, 3), (3, 4), (4, 5)]


@pytest.mark.parametrize("seconds,text", [(45, "45 s"), (61, "1 min"), (89, "1 min"), (150, "3 min"),
                                          (3900, "1 h 5 min")])
def test_pause_wording(seconds, text):
    assert segment.fmt_pause(seconds) == text


def _shot(path, color, backdrop=(128, 128, 128), wobble=0):
    """An item photo: a coloured garment in the middle of a grey backdrop, shifted a little per shot."""
    im = Image.new("RGB", (300, 400), backdrop)
    im.paste(color, (70 + wobble, 90 + wobble, 230 + wobble, 310 + wobble))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)
    return path


def _roll(tmp_path, colors):
    return [_shot(tmp_path / f"{i:02d}.jpg", c, wobble=(i % 3) * 7) for i, c in enumerate(colors)]


RED, BLUE = (200, 30, 40), (30, 60, 200)


def test_the_colour_signature_tells_items_apart_not_angles(tmp_path):
    red1, red2, blue = (_shot(tmp_path / f"{n}.jpg", c, wobble=w) for n, c, w in (("a", RED, 0), ("b", RED, 14),
                                                                                    ("c", BLUE, 0)))
    sig = segment.color_signature
    assert segment.color_distance(sig(red1), sig(red2)) < 0.1          # the same item, another angle
    assert segment.color_distance(sig(red1), sig(blue)) > 0.6          # another item, same backdrop
    assert abs(sum(sig(red1)) - 1) < 1e-9


def test_visual_changes_mark_where_the_look_changes(tmp_path):
    photos = _roll(tmp_path, [RED, RED, RED, BLUE, BLUE, BLUE])
    distances, changes = segment.visual_changes(photos)
    assert changes == {3} and set(distances) == {1, 2, 3, 4, 5}
    retail = segment.visual_changes(photos, ["own", "own", "own", "retail", "own", "own"])[1]
    assert retail == {4}                                              # the screenshot is skipped, not compared


@pytest.mark.parametrize("groups,flag", [
    ([[0, 1, 2], [3, 4, 5]], None),                                                      # as shot: nothing to doubt
    ([[0, 1, 2, 3, 4, 5]], "item 1: a pause (1 min) and a visual change between photos 2 and 3 — two items?"),
    ([[0, 1], [2, 3, 4, 5]], "items 1 and 2: no pause and no visual change between photos 1 and 2 — one item?"),
])
def test_timing_check_doubts_a_grouping_that_both_signals_contradict(tmp_path, groups, flag):
    photos = _roll(tmp_path, [RED, RED, RED, BLUE, BLUE, BLUE])
    breaks = segment.pauses(at(0, 10, 20, 90, 100, 110))
    reasons = segment.timing_check(groups, None, breaks, segment.visual_changes(photos)[1])
    assert (flag in reasons) if flag else reasons == [], reasons


def test_one_signal_alone_never_raises_a_doubt(tmp_path):
    black = _roll(tmp_path, [(20, 20, 20)] * 6)                         # two black dresses: colour can't tell
    pause_only = segment.timing_check([[0, 1, 2], [3, 4, 5]], None, {3: 70.0}, segment.visual_changes(black)[1])
    assert pause_only == []                                            # a pause between them is enough of a reason
    burst = _roll(tmp_path / "b", [RED, RED, RED, BLUE, BLUE, BLUE])   # shot back to back, no pause
    assert segment.timing_check([[0, 1, 2], [3, 4, 5]], None, {}, segment.visual_changes(burst)[1]) == []


def test_the_model_sees_pauses_not_clock_times(tmp_path, monkeypatch):
    photos = [(p, ts) for p, ts in zip(_roll(tmp_path, [RED, RED, BLUE, BLUE]), at(0, 10, 75, 85))]
    seen = {}
    monkeypatch.setattr(llm, "ask", lambda model, system, content, *a, **k: seen.update(content=content) or SegOut(groups=[]))
    segment.segment(photos, "m", 64, breaks={2: 65.0})
    texts = [c["text"] for c in seen["content"] if c["type"] == "text"]
    assert texts[:5] == ["Photo 0", "Photo 1", "— pause 1 min —", "Photo 2", "Photo 3"]
    assert "Time is a tiebreaker only" in segment.SYSTEM and "Never split on a pause alone" in segment.SYSTEM


def _px(content):
    import base64
    import io
    im = next(c for c in content if c["type"] == "image")
    return max(Image.open(io.BytesIO(base64.b64decode(im["source"]["data"]))).size)


def test_previews_fall_back_to_the_smaller_size_when_the_request_is_too_large(tmp_path, monkeypatch):
    photos = [(p, ts) for p, ts in zip(_roll(tmp_path, [RED, BLUE]), at(0, 10))]
    sizes = []
    monkeypatch.setattr(llm, "ask", lambda model, system, content, *a, **k: sizes.append(_px(content)) or SegOut(groups=[]))
    report = {}
    segment.segment(photos, "m", 200, fallback_px=96, report=report)                  # fits: the big previews
    segment.segment(photos, "m", 200, fallback_px=96, max_bytes=10, report=report)    # too large: the small ones
    assert sizes == [200, 96] and report == {"preview_px": 96}


def test_a_413_from_the_api_is_answered_with_the_smaller_previews(tmp_path, monkeypatch):
    class TooLarge(Exception):
        status_code = 413
    photos = [(p, ts) for p, ts in zip(_roll(tmp_path, [RED, BLUE]), at(0, 10))]
    sizes = []

    def ask(model, system, content, *a, **k):
        sizes.append(_px(content))
        if len(sizes) == 1:
            raise TooLarge("request_too_large")
        return SegOut(groups=[])
    monkeypatch.setattr(llm, "ask", ask)
    report = {}
    segment.segment(photos, "m", 200, fallback_px=96, report=report)
    assert sizes == [200, 96] and report == {"preview_px": 96}

    class Overloaded(Exception):
        status_code = 529
    monkeypatch.setattr(llm, "ask", lambda *a, **k: (_ for _ in ()).throw(Overloaded("busy")))
    with pytest.raises(Overloaded):                                                   # anything else: as before
        segment.segment(photos, "m", 200, fallback_px=96)


def test_the_contact_sheet_marks_the_photo_after_a_pause(tmp_path):
    photos = _roll(tmp_path, [RED, RED, BLUE, BLUE])
    plain = Image.open(contact_sheet(photos, [[0, 1], [2, 3]], tmp_path / "a.png", tile=200, cols=4)).convert("RGB")
    marked = Image.open(contact_sheet(photos, [[0, 1], [2, 3]], tmp_path / "b.png", tile=200, cols=4,
                                      breaks={2: 65.0})).convert("RGB")
    banner = (2 * 200 + 10, 12)                                       # top left of photo 2's tile
    assert marked.getpixel(banner) == (34, 34, 34) and plain.getpixel(banner) != (34, 34, 34)
    assert marked.getpixel((10, 12)) == plain.getpixel((10, 12))      # photo 0: no pause, no banner
