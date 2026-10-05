"""Photo normalization: HEIC→JPEG, EXIF rotation, resize, near-duplicate drop, the 3:4 cover."""
from __future__ import annotations

import io
import stat
from datetime import datetime
from pathlib import Path

import imagehash
from PIL import Image, ImageOps, ImageStat
from pillow_heif import register_heif_opener

register_heif_opener()

IMG_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
EXIF_IFD, DATETIME_ORIGINAL, DATETIME = 0x8769, 36867, 306
MAKE, MODEL, EXPOSURE_TIME = 271, 272, 33434                   # any of these = a camera took it
SCREEN_RATIO = (1.7, 2.4)                                      # phone screens: 16:9 up to 20:9+


def is_photo(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in IMG_EXT and not p.name.startswith(".")


SF_DATALESS = getattr(stat, "SF_DATALESS", 0x40000000)   # macOS: a file whose data is still in iCloud


def dataless(p: Path) -> bool:
    """A file iCloud Drive keeps in the cloud only (Optimize Mac Storage, or not downloaded yet): it shows its real
    name and size, but has no data until something reads it (SF_DATALESS; macOS only)."""
    try:
        return bool(getattr(p.stat(), "st_flags", 0) & SF_DATALESS)
    except OSError:
        return False


def icloud_placeholders(folder: Path) -> list[Path]:
    """The files of a share iCloud hasn't downloaded yet: the old placeholders (.Name.jpg.icloud) and the dataless
    files of current macOS (WO28 §4)."""
    return [p for p in folder.iterdir() if p.name.endswith(".icloud") or (p.is_file() and dataless(p))]


def capture_time(p: Path) -> datetime:
    try:
        with Image.open(p) as im:
            exif = im.getexif()
            raw = exif.get_ifd(EXIF_IFD).get(DATETIME_ORIGINAL) or exif.get(DATETIME)
        if raw:
            return datetime.strptime(str(raw).strip("\x00"), "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return datetime.fromtimestamp(p.stat().st_mtime)


def photo_kind(p: Path) -> str:
    """"own" (shot by the seller's camera) or "retail" (a phone screenshot of a retailer's product page).
    Read from the ORIGINAL file: normalize() strips EXIF. Camera EXIF (Make/Model/ExposureTime) wins; without it,
    a phone-screen aspect ratio marks a screenshot. Anything unreadable is treated as the seller's own photo."""
    try:
        with Image.open(p) as im:
            exif = im.getexif()
            if MAKE in exif or MODEL in exif or EXPOSURE_TIME in exif.get_ifd(EXIF_IFD):
                return "own"
            w, h = im.size
    except Exception:
        return "own"
    ratio = max(w, h) / max(1, min(w, h))
    return "retail" if SCREEN_RATIO[0] <= ratio <= SCREEN_RATIO[1] else "own"


def photo_kinds(paths: list[Path]) -> list[str]:
    return [photo_kind(p) for p in paths]


def list_photos(folder: Path) -> list[tuple[Path, datetime]]:
    photos = [(p, capture_time(p)) for p in folder.iterdir() if is_photo(p)]
    return sorted(photos, key=lambda t: (t[1], t[0].name))


def normalize(src: Path, dst: Path, long_edge: int) -> Path:
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        im.save(dst, "JPEG", quality=90, optimize=True)
    return dst


def drop_near_duplicates(paths: list[Path], times: list[datetime], max_distance: int,
                         max_seconds: float = 3.0) -> tuple[list[Path], list[Path]]:
    """Drop burst shots: visually near-identical AND taken within a few seconds of the last kept photo.
    Both conditions, so two different items shot on the same backdrop are never merged."""
    kept, dropped = [], []
    last_hash, last_time = None, None
    for p, t in zip(paths, times):
        with Image.open(p) as im:
            h = imagehash.phash(im)
        if (last_hash is not None and h - last_hash <= max_distance
                and abs((t - last_time).total_seconds()) <= max_seconds):
            dropped.append(p)
            continue
        kept.append(p)
        last_hash, last_time = h, t
    return kept, dropped


def _edge_color(im: Image.Image, rows: bool) -> tuple[int, ...]:
    """Median colour of the photo's top and bottom rows (`rows`) or of its left and right columns."""
    w, h = im.size
    if rows:
        strip = Image.new("RGB", (w, 4))
        strip.paste(im.crop((0, 0, w, 2)), (0, 0))
        strip.paste(im.crop((0, h - 2, w, h)), (0, 2))
    else:
        strip = Image.new("RGB", (4, h))
        strip.paste(im.crop((0, 0, 2, h)), (0, 0))
        strip.paste(im.crop((w - 2, 0, w, h)), (2, 0))
    return tuple(int(c) for c in ImageStat.Stat(strip).median)


def _square(im: Image.Image) -> Image.Image:
    """Padded (never cropped) to 1:1 with the colour of the top and bottom rows."""
    im = im.convert("RGB")
    w, h = im.size
    side = max(w, h)
    canvas = Image.new("RGB", (side, side), _edge_color(im, rows=True))
    canvas.paste(im, ((side - w) // 2, (side - h) // 2))
    return canvas


def _save_jpeg(im: Image.Image, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    im.save(dst, "JPEG", quality=92, optimize=True)
    return dst


def square_cover(src: Path, dst: Path, size: int) -> Path:
    """Pad (never crop) to 1:1 using the photo's own border color, so shoes and hems aren't cut."""
    with Image.open(src) as im:
        return _save_jpeg(_square(im).resize((size, size), Image.Resampling.LANCZOS), dst)


_CLOCKWISE = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}


def portrait_cover(src: Path, dst: Path, width: int = 1200, height: int = 1600, rotate: int = 0) -> Path:
    """The listing cover: padded (never cropped) to width:height — 3:4 portrait, the frame of Poshmark's cover dialog,
    so its default crop takes the whole picture — with the colour of the edges that grow (top and bottom for a photo
    wider than 3:4, left and right for a taller one). A 3:4 phone photo is only resized. `rotate`: clockwise degrees
    (90, 180, 270) that put an item lying sideways or upside down upright first (WO23) — an exact quarter turn."""
    with Image.open(src) as im:
        im = im.convert("RGB")
        if rotate % 360:
            im = im.transpose(_CLOCKWISE[rotate % 360])
        w, h = im.size
        if w * height > h * width:                     # wider than width:height: taller canvas
            size, rows = (w, round(w * height / width)), True
        else:                                          # taller (or exact): wider canvas
            size, rows = (round(h * width / height), h), False
        canvas = Image.new("RGB", size, _edge_color(im, rows))
        canvas.paste(im, ((size[0] - w) // 2, (size[1] - h) // 2))
        return _save_jpeg(canvas.resize((width, height), Image.Resampling.LANCZOS), dst)


def cover_dims(value) -> tuple[int, int]:
    """images.cover_size: [width, height] (3:4 portrait); a single number is the long edge of a 3:4 cover."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return int(value[0]), int(value[1])
    return round(int(value) * 3 / 4), int(value)


SQUARE_COVER_SIZE = 1600      # what cover.jpg was before the 3:4 cover: the duplicate check's hashes are of that


def cover_hash(src: Path) -> str:
    """phash of the square cover.jpg this photo would have made before the 3:4 cover (padded to 1:1, 1600 px, JPEG
    q92, all in memory): items listed since compare with the hashes stored for older items bit for bit."""
    with Image.open(src) as im:
        square = _square(im).resize((SQUARE_COVER_SIZE, SQUARE_COVER_SIZE), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    square.save(buf, "JPEG", quality=92, optimize=True)
    buf.seek(0)
    with Image.open(buf) as im:
        return str(imagehash.phash(im))
