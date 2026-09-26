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
