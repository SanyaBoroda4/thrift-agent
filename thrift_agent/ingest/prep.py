"""Photo normalization: HEIC→JPEG, EXIF rotation, resize, near-duplicate drop, square cover."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import imagehash
from PIL import Image, ImageOps, ImageStat
from pillow_heif import register_heif_opener

register_heif_opener()

IMG_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
EXIF_IFD, DATETIME_ORIGINAL, DATETIME = 0x8769, 36867, 306


def is_photo(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in IMG_EXT and not p.name.startswith(".")


def icloud_placeholders(folder: Path) -> list[Path]:
    """Files iCloud hasn't downloaded yet show up as .Name.jpg.icloud."""
    return [p for p in folder.iterdir() if p.name.endswith(".icloud")]


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


def square_cover(src: Path, dst: Path, size: int) -> Path:
    """Pad (never crop) to 1:1 using the photo's own border color, so shoes and hems aren't cut."""
    with Image.open(src) as im:
        im = im.convert("RGB")
        w, h = im.size
        border = Image.new("RGB", (w, 4))
        border.paste(im.crop((0, 0, w, 2)), (0, 0))
        border.paste(im.crop((0, h - 2, w, h)), (0, 2))
        bg = tuple(int(c) for c in ImageStat.Stat(border).median)
        side = max(w, h)
        canvas = Image.new("RGB", (side, side), bg)
        canvas.paste(im, ((side - w) // 2, (side - h) // 2))
        canvas = canvas.resize((size, size), Image.Resampling.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(dst, "JPEG", quality=92, optimize=True)
    return dst
