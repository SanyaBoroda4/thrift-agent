from pathlib import Path

from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from thrift_agent.ingest import prep

# Small stand-ins for the real sizes (1170x2532 phone screen, 3024x4032 iPhone photo): only the ratio matters.
SCREEN, SCREEN_LANDSCAPE, CAMERA = (117, 253), (253, 117), (302, 403)


def save(p: Path, size: tuple[int, int], exif: Image.Exif | None = None) -> Path:
    img = Image.new("RGB", size, "white")
    img.save(p, exif=exif) if exif is not None else img.save(p)
    return p


def camera_exif(make: bool = True, model: bool = True, exposure: bool = False) -> Image.Exif:
    exif = Image.Exif()
    if make:
        exif[271] = "Apple"
    if model:
        exif[272] = "iPhone 15"
    if exposure:
        exif.get_ifd(0x8769)[33434] = IFDRational(1, 60)          # ExposureTime 1/60 s, in the EXIF sub-IFD
    return exif


def test_screen_ratio_png_without_exif_is_retail(tmp_path):
    assert prep.photo_kind(save(tmp_path / "a.png", SCREEN)) == "retail"


def test_camera_jpeg_with_make_model_is_own(tmp_path):
    assert prep.photo_kind(save(tmp_path / "b.jpg", CAMERA, camera_exif())) == "own"
    assert prep.photo_kind(save(tmp_path / "b2.jpg", SCREEN, camera_exif(model=False))) == "own"   # Make alone wins
    assert prep.photo_kind(save(tmp_path / "b3.jpg", SCREEN, camera_exif(make=False))) == "own"    # so does Model


def test_exposure_time_marks_own_even_at_screen_ratio(tmp_path):
    exif = camera_exif(make=False, model=False, exposure=True)
    p = save(tmp_path / "c.jpg", SCREEN, exif)
    with Image.open(p) as im:                                       # the fixture really carries the tag
        assert 33434 in im.getexif().get_ifd(0x8769)
    assert prep.photo_kind(p) == "own"


def test_non_screen_ratio_without_exif_is_own(tmp_path):
    assert prep.photo_kind(save(tmp_path / "d.jpg", (400, 600))) == "own"       # 3:2, ratio 1.5
    assert prep.photo_kind(save(tmp_path / "d2.png", (100, 260))) == "own"      # 2.6, taller than any phone


def test_landscape_screenshot_is_retail(tmp_path):
    assert prep.photo_kind(save(tmp_path / "e.png", SCREEN_LANDSCAPE)) == "retail"


def test_unreadable_file_counts_as_own(tmp_path):
    p = tmp_path / "junk.jpg"
    p.write_bytes(b"not an image")
    assert prep.photo_kind(p) == "own"


def test_photo_kinds(tmp_path):
    paths = [save(tmp_path / "0.png", SCREEN), save(tmp_path / "1.jpg", (400, 600)),
             save(tmp_path / "2.jpg", SCREEN, camera_exif())]
    assert prep.photo_kinds(paths) == ["retail", "own", "own"]
    assert prep.photo_kinds([]) == []


# ---------- WO12: the 3:4 cover ----------

def _photo(p: Path, size: tuple[int, int], bg="white", item="red") -> Path:
    """A photo: `bg` background with an `item` block in the middle half."""
    img = Image.new("RGB", size, bg)
    w, h = size
    img.paste(Image.new("RGB", (w // 2, h // 2), item), (w // 4, h // 4))
    img.save(p)
    return p


def test_the_cover_is_3_by_4_portrait_padded_never_cropped(tmp_path):
    phone = prep.portrait_cover(_photo(tmp_path / "a.jpg", (300, 400)), tmp_path / "c1.jpg")    # 3:4 already
    with Image.open(phone) as im:
        assert im.size == (1200, 1600)
        assert im.getpixel((600, 800))[0] > 200 and im.getpixel((5, 5)) == (255, 255, 255)   # just resized
    wide = prep.portrait_cover(_photo(tmp_path / "b.jpg", (400, 300), bg="navy"), tmp_path / "c2.jpg", 300, 400)
    with Image.open(wide) as im:
        assert im.size == (300, 400)
        top, left = im.getpixel((150, 10)), im.getpixel((2, 200))
        assert top[2] > 100 and top[0] < 40                     # padded above and below in the photo's own navy
        assert left[2] > 100 and left[0] < 40                   # ... and nothing of its width was cut
    tall = prep.portrait_cover(_photo(tmp_path / "d.jpg", (200, 400), bg="green"), tmp_path / "c3.jpg", 300, 400)
    with Image.open(tall) as im:
        assert im.size == (300, 400) and im.getpixel((5, 200))[1] > 100          # padded left and right in green


def test_cover_size_setting():
    assert prep.cover_dims([1200, 1600]) == (1200, 1600)
    assert prep.cover_dims(1600) == (1200, 1600)                 # an old single number: the long edge of a 3:4


def test_cover_hash_matches_the_square_covers_of_older_items(tmp_path):
    """Items listed before the 3:4 cover stored phash(square cover.jpg): the new hash is taken the same way."""
    import imagehash

    src = _photo(tmp_path / "a.jpg", (300, 400))
    old = prep.square_cover(src, tmp_path / "old_cover.jpg", 1600)
    with Image.open(old) as im:
        stored = imagehash.phash(im)
    assert stored - imagehash.hex_to_hash(prep.cover_hash(src)) == 0       # bit for bit (8 = the duplicate limit)
    with Image.open(prep.portrait_cover(src, tmp_path / "new_cover.jpg")) as im:
        assert im.size == (1200, 1600)                          # what Poshmark gets is 3:4 all the same
