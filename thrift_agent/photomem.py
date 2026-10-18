"""WO43: the photo memory — every photo that reaches the inbox is remembered for 30 days with the item it went to, so a
repeat share never makes a second card (live 2026-10-17: the iPhone uploaded the 3:18 PM share's photos 4½ hours
later, right after the same three items had been shared again — six folders, six cards, three repeats).

A photo is known when any of these match (checked on the archived shares, 2026-10-17 — all of it survives the
Shortcut, a HEIC turned JPEG, a new file name or folder):
- the same bytes (SHA-256);
- the same capture: the EXIF original time with its sub-seconds, the camera model and the pixel size;
- a near-identical picture, for a photo without EXIF (a screenshot): a perceptual hash within PHASH_MAX plus the
  same average colours. Tight on purpose — two different items shot the same way must never match (the grey and the
  brown Alo bras of Oct 16, same carpet: `tests/test_wo43.py`).
The check runs as each share is taken, before grouping and before any model call (`check_share`); the photos are
recorded with their item in the same step (`remember`, from `pipeline.split`). A second guard sits in the Telegram
outbox (`card_blocked`): no price card for photos that already had one in the last 30 days, or for a retired item.
`thrift photos --forget <item>` clears an item's photos (she really wants to start it over)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import imagehash
from PIL import Image

from thrift_agent import notify
from thrift_agent.db import DB, loads

KEEP_DAYS = 30
PHASH_MAX = 2                     # Hamming distance of the perceptual hash: near-identical only
COLOR_MAX = 10                    # each of the average R, G, B within this (0..255)
SKIPPED = "photomem_skipped"      # kv: the repeat shares skipped in this look, said as one private line
EXIF_IFD, DATE_ORIGINAL, SUBSEC_ORIGINAL, MODEL = 0x8769, 0x9003, 0x9291, 0x0110


def sha256(p: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def capture_key(p: Path) -> str | None:
    """"2026:10:17 15:16:33|103|iPhone 16 Pro Max|5712x4284" — None without an original time (a screenshot has one
    too, with no sub-seconds and no model: still one second and one pixel size)."""
    try:
        with Image.open(p) as im:
            ex = im.getexif()
            ifd = ex.get_ifd(EXIF_IFD)
            when = ifd.get(DATE_ORIGINAL)
            if not when:
                return None
            w, h = im.size
            return f"{str(when).strip()}|{str(ifd.get(SUBSEC_ORIGINAL) or '').strip()}|" \
                   f"{str(ex.get(MODEL) or '').strip()}|{w}x{h}"
    except Exception:  # noqa: BLE001 — not an image, or unreadable: no key
        return None


def picture_keys(p: Path) -> tuple[str | None, list[int] | None]:
    """(perceptual hash, average colours) of the picture, orientation applied."""
    try:
        from PIL import ImageOps, ImageStat
        with Image.open(p) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            ph = str(imagehash.phash(im))
            colors = [round(v) for v in ImageStat.Stat(im.resize((32, 32))).mean[:3]]
        return ph, colors
    except Exception:  # noqa: BLE001
        return None, None


def keys_of(p: Path) -> dict:
    ph, colors = picture_keys(p)
    return {"sha": sha256(p), "capture": capture_key(p), "phash": ph, "colors": colors, "name": p.name}


def _since() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")


def known(db: DB, keys: dict):
    """The remembered row this photo matches (its item in `item_id`), or None."""
    since = _since()
    if keys.get("sha"):
        row = db.conn.execute("SELECT * FROM photos WHERE sha=? AND seen_at>=? ORDER BY rowid DESC LIMIT 1",
                              (keys["sha"], since)).fetchone()
        if row is not None:
            return row
    if keys.get("capture"):
        row = db.conn.execute("SELECT * FROM photos WHERE capture=? AND seen_at>=? ORDER BY rowid DESC LIMIT 1",
                              (keys["capture"], since)).fetchone()
        if row is not None:
            return row
    if keys.get("phash") and keys.get("colors") and not keys.get("capture"):   # no EXIF: the picture itself
        try:
            mine = imagehash.hex_to_hash(keys["phash"])
        except ValueError:
            return None
        for row in db.conn.execute("SELECT * FROM photos WHERE phash IS NOT NULL AND seen_at>=? ORDER BY rowid DESC",
                                   (since,)):
            try:
                if mine - imagehash.hex_to_hash(row["phash"]) > PHASH_MAX:
                    continue
            except ValueError:
                continue
            theirs = loads(row["colors"]) or []
            if len(theirs) == 3 and all(abs(a - b) <= COLOR_MAX for a, b in zip(keys["colors"], theirs)):
                return row
    return None


def remember(db: DB, photos: list[tuple[Path, dict]], item_id: str | None, batch_id: str | None) -> int:
    """Record these photos (with their keys) as the item's; rows older than KEEP_DAYS go. Returns the rows added."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    for p, keys in photos:
        db.conn.execute("INSERT INTO photos (sha, capture, phash, colors, name, item_id, batch_id, seen_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (keys.get("sha"), keys.get("capture"), keys.get("phash"),
                         json.dumps(keys["colors"]) if keys.get("colors") else None, p.name, item_id, batch_id, now))
        n += 1
    db.conn.execute("DELETE FROM photos WHERE seen_at < ?", (_since(),))
    return n


def forget(db: DB, item_id: str) -> int:
    cur = db.conn.execute("DELETE FROM photos WHERE item_id=?", (item_id,))
    return cur.rowcount


def check_share(db: DB, paths: list[Path]) -> tuple[dict[str, str], list[Path], dict[str, dict]]:
    """({photo name: the item it is known as}, the new photos, {photo name: keys}) for a share's photos."""
    known_as, new, keys = {}, [], {}
    for p in paths:
        k = keys_of(p)
        keys[p.name] = k
        row = known(db, k)
        if row is not None and row["item_id"]:
            known_as[p.name] = row["item_id"]
        else:
            new.append(p)
    return known_as, new, keys


def title_of(db: DB, iid: str) -> str:
    it = db.item(iid)
    if it is None:
        return iid
    posh = (loads(it["renders"]) or {}).get("poshmark") or {}
    return posh.get("title") or iid


def note_skipped(db: DB, when: str, titles: list[str]) -> None:
    """A repeat share skipped in this look; `say_skipped` says them all in one private line."""
    rows = loads(db.kv_get(SKIPPED)) or []
    rows.append({"when": when, "titles": titles})
    db.kv_set(SKIPPED, json.dumps(rows))


def say_skipped(db: DB) -> str | None:
    """ONE private line for the repeat shares skipped in this look — "🧹 Repeat share skipped (7:42 PM): same photos as
    <title>." — or None when there were none."""
    rows = loads(db.kv_get(SKIPPED)) or []
    if not rows:
        return None
    db.kv_set(SKIPPED, "[]")
    whens = list(dict.fromkeys(r["when"] for r in rows))
    titles = list(dict.fromkeys(t for r in rows for t in r["titles"]))
    text = (f"🧹 Repeat share{'s' if len(rows) > 1 else ''} skipped ({', '.join(whens)}): same photos as "
            f"{', '.join(titles)}.")
    notify.say(text)
    db.log(None, "repeat_share_said", {"shares": rows})
    return text


def card_blocked(db: DB, iid: str, chat_id: str | None = None) -> str | None:
    """WO43's second guard, asked before a price card goes out: why it must not — the item is retired, or its photos
    already had a card (another item's, in the last 30 days). None when the card may go."""
    it = db.item(iid)
    if it is None:
        return "unknown item"
    if it["status"] == "dropped":
        return "the item is retired"
    since = _since()
    for row in db.conn.execute("SELECT sha, capture FROM photos WHERE item_id=?", (iid,)):
        others = db.conn.execute(
            "SELECT DISTINCT item_id FROM photos WHERE item_id IS NOT NULL AND item_id<>? AND seen_at>=? AND "
            "((sha IS NOT NULL AND sha=?) OR (capture IS NOT NULL AND capture=?))",
            (iid, since, row["sha"], row["capture"])).fetchall()
        for o in others:
            carded = db.conn.execute(
                "SELECT 1 FROM outbox WHERE kind='item' AND ref=? AND sent_at>=? " + ("AND chat_id=? " if chat_id else "")
                + "LIMIT 1", (o["item_id"], since, *([str(chat_id)] if chat_id else []))).fetchone()
            if carded:
                return f"its photos already had a card: {title_of(db, o['item_id'])}"
    return None
