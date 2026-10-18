"""Shares from the API (WO45 §2), the Mac's side: the iPhone's Shortcut POSTs each photo of a share to thrift-api and
then the share's end (api/thrift_api/uploads.py); while the Mac is open it asks the API for new shares every 30 s,
downloads them and drops each into the inbox as a folder named by the share id — the same name the Shortcut gives the
iCloud copy — so the intake takes it like an iCloud folder (`_done.txt` with "done <count>"; `_source.txt` "azure";
`_test` "1" for a test share, the Shortcut's test copy). Live 2026-10-17: three shares' photos sat on the iPhone for
4½ hours and iCloud Drive never pushed them; the Mac saw an empty folder. iCloud stays the safety copy.

A share is taken when it is ended with every photo in (received >= count), or ended and short but quiet for two
minutes (a photo that never went: the iCloud copy completes the folder later, WO42's partial rule tells the owner), or
never ended but started ten minutes ago with photos (the Shortcut died: what came). The photos are downloaded into
work/uploads/<share id> first (each written as a dot-file and renamed; a file already there with the right size is
kept) and verified by SHA-256 — a mismatch drops the file (event `upload_bad_hash`) and the share is tried again next
time — then the folder is moved whole into the inbox, so the intake never sees it half-written. A folder of that share
already in the inbox or the archive (the iCloud copy came first: `YYYYMMDD_<share id>` there) is never doubled: the
share is only marked fetched (event `share_same_as_icloud`).

How it is called (the worker's inbox scan — not wired here):

    if uploads.due(db):               # every POLL_EVERY seconds at most (a kv stamp, one for every caller)
        uploads.fetch_new(s, db)      # the new shares into the inbox; never raises (the API down: one `api_down`)
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import httpx

from thrift_agent import notify, sales
from thrift_agent.config import Settings
from thrift_agent.db import DB

POLL_EVERY = 30                          # seconds between looks at GET /shares/pending
LAST_POLL = "uploads_last_poll"          # kv: the last look (sales._claim's stamp)
QUIET_AFTER = 120                        # an ended share short of its count: taken once its last photo is this old
STALE_AFTER = 600                        # a share never ended: taken this long after it started, with what came
DOWNLOAD_TIMEOUT = 60.0                  # a 30 MB photo over the owner's Wi-Fi
STAGING = "uploads"                      # work/uploads/<share id>: the download, moved whole into the inbox
SOURCE_FILE, TEST_FILE, DONE_FILE = "_source.txt", "_test", "_done.txt"
SHARE_ID = re.compile(r"\d{4}-\d{2}-\d{2}_\d{6}(_[0-9a-z]{1,8})?")   # the Shortcut's date_time_random
PHOTO_PATH = "/shares/"                  # a photo's path from the listing must be the API's own


def due(db: DB) -> bool:
    """True once every POLL_EVERY seconds (the stamp taken in one transaction, as sales._claim)."""
    return sales._claim(db, LAST_POLL, POLL_EVERY)


def fetch_new(s: Settings, db: DB, client=None, now: datetime | None = None) -> list[str]:
    """The pending shares the rules above allow, into the inbox; the share ids taken (or settled as the iCloud copy).
    Never raises: the API out of reach is one `api_down` event and the next call tries again."""
    client = client if client is not None else sales.api()
    if client is None:
        return []
    now = now or datetime.now(timezone.utc)
    try:
        answer = client.get("/shares/pending")
    except sales.ApiError as e:
        db.log(None, "api_down", {"path": "/shares/pending", "error": str(e)})
        return []
    done = []
    for share in (answer.get("shares") or []) if isinstance(answer, dict) else []:
        sid = str(share.get("share_id") or "") if isinstance(share, dict) else ""
        if not SHARE_ID.fullmatch(sid):
            db.log(None, "upload_bad_id", {"share_id": sid[:80]})          # never a path the API made up
            continue
        if not takeable(share, now):
            continue
        try:
            if take(s, db, client, share, sid):
                done.append(sid)
        except sales.ApiError as e:
            db.log(sid, "api_down", {"path": "/shares", "error": str(e)})
            break
        except OSError as e:
            db.log(sid, "upload_error", {"share_id": sid, "error": f"{type(e).__name__}: {e}"[:300]})
    return done


def takeable(share: dict, now: datetime) -> bool:
    """Whole (ended, every photo in); ended and short but quiet QUIET_AFTER; never ended but STALE_AFTER old — with at
    least one photo in every case."""
    photos = share.get("photos") or []
    received = int(share.get("received") or 0)
    if not photos or received <= 0:
        return False
    count = share.get("count")
    if share.get("ended"):
        if not isinstance(count, int) or received >= count:
            return True
        last = _time(share.get("last_received_at"))
        return last is not None and (now - last).total_seconds() >= QUIET_AFTER
    started = _time(share.get("started_at"))
    return started is not None and (now - started).total_seconds() >= STALE_AFTER


def take(s: Settings, db: DB, client, share: dict, sid: str) -> bool:
    """One share: the folder in the inbox (or the iCloud copy found), the API told. False when it must wait."""
    names = [n for n in (safe_name(p.get("name")) for p in share.get("photos") or [] if isinstance(p, dict)) if n]
    test = bool(share.get("test"))
    found = known_folder(s, sid)
    if found is not None:
        _settle_known(db, client, sid, found, names, test)
        return True
    staging = s.path("work") / STAGING / sid
    if not download(db, client, sid, share, staging):
        return False
    write_marks(staging, share)
    found = known_folder(s, sid)                      # the iCloud copy may have landed during the download
    if found is not None:
        shutil.rmtree(staging, ignore_errors=True)
        _settle_known(db, client, sid, found, names, test)
        return True
    target = s.path("inbox") / sid
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging), str(target))
    db.log(sid, "share_fetched", {"share_id": sid, "photos": names, "test": test, "folder": target.name})
    client.post(f"/shares/{sid}/fetched", {"names": names})
    return True


def _settle_known(db: DB, client, sid: str, found: Path, names: list[str], test: bool) -> None:
    """The share's folder is here already: the iCloud copy (said once), or ours whose fetched call didn't go.
    WO45 follow-up: the iCloud copy of a TEST share carries no mark of its own — the API's word puts `_test` into the
    folder while it still waits in the inbox (the intake then makes a 🧪 card, never a real one); a copy taken
    before the API was asked is told to the owner (its card is a real one: 'same item' retires it)."""
    if source_of(found) != "azure":
        db.log(sid, "share_same_as_icloud", {"share_id": sid, "folder": found.name, "photos": names, "test": test})
        if test:
            if found.parent.name != "archive" and not (found / "_test").exists():
                try:
                    (found / "_test").write_text("1", encoding="utf-8")
                    db.log(sid, "share_marked_test", {"share_id": sid, "folder": found.name})
                except OSError as e:
                    db.log(sid, "share_mark_failed", f"{type(e).__name__}: {e}")
            elif found.parent.name == "archive":
                notify.say(f"🧪 The iCloud copy of test share {sid} was taken before the API said it was a test — its "
                           "card is a real one: reply 'same item' to it, or price it if it is real.")
    client.post(f"/shares/{sid}/fetched", {"names": names})


def known_folder(s: Settings, sid: str) -> Path | None:
    """A folder of this share in the inbox (`<share id>`, `<share id>-late` …) or the archive (`YYYYMMDD_<share id>`,
    `…_2`), else None."""
    for base in (s.path("inbox"), s.path("archive")):
        try:
            folders = sorted(p for p in base.iterdir() if p.is_dir())
        except OSError:
            continue
        for folder in folders:
            if folder.name.startswith(sid) or re.match(r"\d{8}_" + re.escape(sid), folder.name):
                return folder
    return None


def source_of(folder: Path) -> str:
    """"azure" for a folder this module made, else "icloud"."""
    try:
        return "azure" if (folder / SOURCE_FILE).is_file() and "azure" in (folder / SOURCE_FILE).read_text()[:20] \
            else "icloud"
    except OSError:
        return "icloud"


def download(db: DB, client, sid: str, share: dict, staging: Path) -> bool:
    """Every photo of the share into `staging`, byte for byte and checked; False when one must be fetched again."""
    staging.mkdir(parents=True, exist_ok=True)
    for photo in share.get("photos") or []:
        if not isinstance(photo, dict):
            continue
        name = safe_name(photo.get("name"))
        path = str(photo.get("path") or "")
        if not name or not path.startswith(PHOTO_PATH):
            db.log(sid, "upload_bad_name", {"share_id": sid, "name": str(photo.get("name"))[:80], "path": path[:120]})
            continue
        target = staging / name
        size = photo.get("size")
        if target.is_file() and (not isinstance(size, int) or target.stat().st_size == size):
            continue                                                    # already here, whole
        data, _ = get_bytes(client, path)
        want = str(photo.get("sha256") or "")
        if want and hashlib.sha256(data).hexdigest() != want:
            db.log(sid, "upload_bad_hash", {"share_id": sid, "name": name})
            target.unlink(missing_ok=True)
            return False
        part = staging / f".{name}.part"                                # a dot-file: never a photo to the intake
        part.write_bytes(data)
        os.replace(part, target)
    return True


def write_marks(folder: Path, share: dict) -> None:
    """The Shortcut's marks as the intake reads them: `_done.txt` "done <count>" (the share's count, else what came),
    `_source.txt` "azure", `_test` "1" for a test share."""
    photos = len(share.get("photos") or [])
    count = share.get("count") if isinstance(share.get("count"), int) else None
    (folder / DONE_FILE).write_text(f"done {count or int(share.get('received') or 0) or photos}", encoding="utf-8")
    (folder / SOURCE_FILE).write_text("azure", encoding="utf-8")
    if share.get("test"):
        (folder / TEST_FILE).write_text("1", encoding="utf-8")


def get_bytes(client, path: str) -> tuple[bytes, str]:
    """A photo's bytes and Content-Type from the API: the client's own `get_bytes` when it has one, else the request
    made here with its URL and key (sales.Api answers JSON only), a longer timeout for a 30 MB photo."""
    fetch = getattr(client, "get_bytes", None)
    if fetch is not None:
        return fetch(path)
    headers = {"x-functions-key": client.key}
    try:
        if getattr(client, "client", None) is not None:
            r = client.client.get(client.url + path, headers=headers)
        else:
            r = httpx.get(client.url + path, headers=headers, timeout=DOWNLOAD_TIMEOUT)
    except httpx.HTTPError as e:
        raise sales.ApiError(f"GET {path}: {type(e).__name__}") from None
    if r.status_code >= 300:
        raise sales.ApiError(f"GET {path}: HTTP {r.status_code}", r.status_code)
    return r.content, r.headers.get("content-type", "")


def safe_name(raw: object) -> str:
    """A photo's file name as the API listed it, with no path and never a dot-file (the inbox ignores those)."""
    name = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._ ")
    return name[:80]


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
