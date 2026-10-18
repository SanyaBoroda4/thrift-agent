"""Shares straight from the iPhone (WO45 §2): the Shortcut POSTs each photo of a share to the API, then the share's end;
the Mac polls `/shares/pending` every 30 s while it is open, downloads the photos into its inbox as a folder named by
the share id, and marks the share fetched. iCloud keeps saving the same folder as the safety copy (live 2026-10-17:
three shares' photos sat on the iPhone for 4½ hours and iCloud Drive never pushed them — the Mac saw an empty folder).

  POST /share/{share_id}/photo?n=<k>&of=<count>&name=<file name>&test=1    the body is the photo: a raw image
       (image/jpeg, image/heic, image/heif, image/png, image/webp — Shortcuts' "File" body) or multipart/form-data
       with one file field of any name (Shortcuts' "Form" body)      → {"ok": true, "received": <distinct photos>}
  POST /share/{share_id}/end?count=N&test=1                            → {"ok": true, "received": k, "count": N}
  GET  /shares/pending                                                 the shares the Mac hasn't fetched (one query)
  GET  /shares/{share_id}/photo/{name}                                 the bytes, with the stored Content-Type
  POST /shares/{share_id}/fetched    {"names": [...]}                  → {"ok": true, …}

A refusal is {"ok": false, "error": "…"}: 400 for a bad share id, a body that isn't an image (the magic bytes decide,
never the header), a 61st photo; 413 past 30 MB. The same bytes twice (one SHA-256) are stored once, still "ok"; a
different photo under a name already used gets "-2", "-3" … in its name. Stored byte for byte — a HEIC stays a HEIC,
the EXIF (the capture time the photo memory keys on, WO43) untouched.

The bytes go to Blob Storage, container `shares`, blob `<share id>/<name>`, in the function app's own account
(`AzureWebJobsStorage`); only the metadata is in Postgres (`shares_in`, `share_uploads`). Without the connection
string (a local run, the tests) the store is memory, said once in the log. Rows live 30 days (timers.daily), the blobs
too (the account's lifecycle rule, `provision.py uploads`).

Never logged: a photo, its name, a key; a failure as its type and place only (util.where), as the rest of the API."""
from __future__ import annotations

import email.message
import hashlib
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from .db import Database
from .util import ApiError, BadRequest, NotFound, iso

log = logging.getLogger(__name__)

SHARE_ID = re.compile(r"\d{4}-\d{2}-\d{2}_\d{6}(_[0-9a-z]{1,8})?")   # the Shortcut's date_time_random
MAX_BYTES = 30 * 1024 * 1024
MAX_PHOTOS = 60
MAX_COUNT = 500
KEEP_DAYS = 30
CONTAINER = "shares"
CONNECTION_ENV = "AzureWebJobsStorage"
# what the magic bytes say → the stored Content-Type and the extensions the Mac's intake opens under it
KINDS = {"image/jpeg": (".jpg", ".jpeg"), "image/png": (".png",), "image/webp": (".webp",),
         "image/heic": (".heic",), "image/heif": (".heif", ".heic")}
HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx"}
HEIF_BRANDS = {b"mif1", b"msf1", b"heif", b"heim", b"heis", b"hevm", b"hevs"}


class TooLarge(ApiError):
    status = 413


# --- the store -------------------------------------------------------------------------------------------------------

class MemoryStore:
    """The tests' store, and the fallback without a connection string: {path: (bytes, content type)}."""

    def __init__(self):
        self.blobs: dict[str, tuple[bytes, str]] = {}

    def put(self, path: str, data: bytes, content_type: str) -> None:
        self.blobs[path] = (data, content_type)

    def get(self, path: str) -> tuple[bytes, str] | None:
        return self.blobs.get(path)

    def delete(self, path: str) -> bool:
        return self.blobs.pop(path, None) is not None


class AzureStore:
    """Blob Storage, container `shares` of the function app's own account; the SDK imported only here (the tests and
    a local run never need it). A missing container is made on the first put (provision.py makes it too)."""

    def __init__(self, connection: str, container: str = CONTAINER):
        from azure.storage.blob import BlobServiceClient
        self._container = BlobServiceClient.from_connection_string(connection).get_container_client(container)

    def put(self, path: str, data: bytes, content_type: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError
        from azure.storage.blob import ContentSettings
        settings = ContentSettings(content_type=content_type)
        try:
            self._container.upload_blob(path, data, overwrite=True, content_settings=settings)
        except ResourceNotFoundError:
            self._container.create_container()
            self._container.upload_blob(path, data, overwrite=True, content_settings=settings)

    def get(self, path: str) -> tuple[bytes, str] | None:
        from azure.core.exceptions import ResourceNotFoundError
        try:
            stream = self._container.get_blob_client(path).download_blob()
        except ResourceNotFoundError:
            return None
        content_type = (stream.properties.content_settings.content_type if stream.properties.content_settings else None)
        return stream.readall(), content_type or "application/octet-stream"

    def delete(self, path: str) -> bool:
        from azure.core.exceptions import ResourceNotFoundError
        try:
            self._container.get_blob_client(path).delete_blob()
        except ResourceNotFoundError:
            return False
        return True


_STORE = None


def store():
    """The process's store: Blob Storage when AzureWebJobsStorage is set, else memory (said once)."""
    global _STORE
    if _STORE is None:
        connection = os.environ.get(CONNECTION_ENV, "").strip()
        if connection:
            _STORE = AzureStore(connection)
        else:
            log.warning("uploads: %s is not set — photos are kept in memory, lost when the process ends", CONNECTION_ENV)
            _STORE = MemoryStore()
    return _STORE


# --- the body: a raw image, or the one file of a form -----------------------------------------------------------------

def unwrap(data: bytes, content_type: str | None) -> tuple[bytes, str | None]:
    """The photo's bytes and the file name the request carried (None for a raw body): a raw image body, or the first
    file of a multipart/form-data body, whatever its field is called."""
    kind, params = _content_type(content_type)
    if kind != "multipart/form-data":
        return data, None
    boundary = params.get("boundary")
    if not boundary:
        raise BadRequest("multipart/form-data without a boundary")
    part = multipart_file(data, boundary.encode("latin-1"))
    if part is None:
        raise BadRequest("no file in the form body")
    return part


def _content_type(header: str | None) -> tuple[str, dict]:
    message = email.message.Message()
    message["Content-Type"] = (header or "").strip() or "application/octet-stream"
    params = {k.lower(): v for k, v in (message.get_params() or [])[1:] if isinstance(v, str)}
    return message.get_content_type(), params


def multipart_file(body: bytes, boundary: bytes) -> tuple[bytes, str | None] | None:
    """The first part that carries a file name or whose content is an image: its bytes exactly (the line break before
    the next delimiter is the form's, not the file's) and the name. The stdlib's email parser is line-based: a 30 MB
    photo through it would be slow; the boundary is all that needs finding."""
    for chunk in body.split(b"--" + boundary)[1:]:
        if chunk[:2] == b"--":
            break                                                   # the closing delimiter
        chunk = chunk[2:] if chunk[:2] == b"\r\n" else chunk[1:] if chunk[:1] == b"\n" else chunk
        crlf, lf = chunk.find(b"\r\n\r\n"), chunk.find(b"\n\n")       # the headers' end: whichever comes first
        if crlf < 0 and lf < 0:                                         # (the other may sit inside the photo)
            continue
        if crlf >= 0 and (lf < 0 or crlf <= lf):
            head, content = chunk[:crlf], chunk[crlf + 4:]
        else:
            head, content = chunk[:lf], chunk[lf + 2:]
        if content.endswith(b"\r\n"):
            content = content[:-2]
        elif content.endswith(b"\n"):
            content = content[:-1]
        found = re.search(r'filename=(?:"([^"]*)"|([^;\r\n]*))', head.decode("latin-1"), re.I)
        name = (found.group(1) if found.group(1) is not None else found.group(2).strip()) if found else None
        if name is not None or sniff(content):
            return content, name or None
    return None


def sniff(data: bytes) -> tuple[str, str] | None:
    """(content type, extension) from the magic bytes — JPEG, PNG, WebP, HEIC / HEIF (the ftyp box's brands) — else
    None: the Content-Type header is never trusted."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in HEIC_BRANDS:
            return "image/heic", ".heic"
        compatible = {data[i:i + 4] for i in range(16, min(len(data), 64), 4)}
        if brand in HEIF_BRANDS and compatible & HEIC_BRANDS:
            return "image/heic", ".heic"
        if brand in HEIF_BRANDS:
            return "image/heif", ".heif"
    return None


def safe_name(raw: str | None, content_type: str, ext: str, fallback: str) -> str:
    """The stored name: the file's own, without any path, odd characters replaced, never a dot-file (the Mac's intake
    ignores those), its extension the one the bytes say (a HEIC called .jpeg is stored as .heic: the intake opens it by
    its name)."""
    base = (raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._ ")
    path = PurePosixPath(base)
    stem = path.stem.strip("._") if base else ""
    suffix = path.suffix if path.suffix.lower() in KINDS[content_type] else ext
    return f"{(stem or fallback)[:72]}{suffix}"


def share_id_of(value: object) -> str:
    text = str(value or "").strip()
    if not SHARE_ID.fullmatch(text):
        raise BadRequest("the share id is yyyy-mm-dd_hhmmss, with an optional _<random> suffix")
    return text


def _int(query: dict, name: str, most: int) -> int | None:
    value = query.get(name)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    if not text.isdigit() or not 1 <= int(text) <= most:
        raise BadRequest(f"{name} is a whole number from 1 to {most}")
    return int(text)


def _flag(query: dict, name: str) -> bool:
    value = query.get(name)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return str(value if value is not None else "").strip().lower() in ("1", "true", "yes")


# --- the routes' work --------------------------------------------------------------------------------------------------

def receive_photo(db: Database, blobs, share_id: str, data: bytes, content_type: str | None, query: dict,
                  now: datetime) -> dict:
    """One photo of a share, stored and recorded (see the module); {"ok": true, "received": k, "name": <stored>}."""
    share_id = share_id_of(share_id)
    n, of, test = _int(query, "n", MAX_COUNT), _int(query, "of", MAX_COUNT), _flag(query, "test")
    photo, filename = unwrap(data, content_type)
    if len(photo) > MAX_BYTES:
        raise TooLarge(f"a photo is at most {MAX_BYTES // (1024 * 1024)} MB")
    kind = sniff(photo)
    if kind is None:
        raise BadRequest("the body is not a JPEG, PNG, WebP, HEIC or HEIF image")
    kind_type, ext = kind
    digest = hashlib.sha256(photo).hexdigest()
    with db.tx():
        share = _share(db, share_id, now, count=of, test=test)
        rows = db.query("SELECT name, sha256 FROM share_uploads WHERE share_id = ?", (share_id,))
        same = next((row for row in rows if row["sha256"] == digest), None)
        if same is not None:
            return {"ok": True, "received": len(rows), "name": same["name"], "stored": False}
        if len(rows) >= MAX_PHOTOS:
            raise BadRequest(f"a share holds at most {MAX_PHOTOS} photos")
        given = _param(query, "name") or filename
        name = safe_name(given, kind_type, ext, fallback=f"IMG_{n or len(rows) + 1:04d}")
        taken = {row["name"] for row in rows}
        stem, suffix = PurePosixPath(name).stem, PurePosixPath(name).suffix
        k = 1
        while name in taken:
            k += 1
            name = f"{stem}-{k}{suffix}"
        blobs.put(f"{share_id}/{name}", photo, kind_type)          # inside the transaction: a failed put records nothing
        db.execute("INSERT INTO share_uploads (share_id, name, n, sha256, size, content_type, received_at, test) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (share_id, name, n if n is not None else len(rows) + 1, digest, len(photo), kind_type, iso(now),
                    1 if (test or share["test"]) else 0))
        received = len(rows) + 1
        db.execute("UPDATE shares_in SET received = ?, last_received_at = ? WHERE share_id = ?",
                   (received, iso(now), share_id))
    return {"ok": True, "received": received, "name": name, "stored": True}


def end_share(db: Database, share_id: str, query: dict, now: datetime) -> dict:
    """The Shortcut's end of a share, with its count; fine before some photos (the Mac waits for them)."""
    share_id = share_id_of(share_id)
    count, test = _int(query, "count", MAX_COUNT), _flag(query, "test")
    with db.tx():
        share = _share(db, share_id, now, count=count, test=test)
        db.execute("UPDATE shares_in SET ended_at = COALESCE(ended_at, ?) WHERE share_id = ?", (iso(now), share_id))
    return {"ok": True, "received": int(share["received"] or 0), "count": count if count is not None else share["count"]}


def _share(db: Database, share_id: str, now: datetime, count: int | None, test: bool) -> dict:
    """The share's row, made at its first photo or end; `count` fills a missing one, `test` sticks once set."""
    db.execute("INSERT INTO shares_in (share_id, count, received, started_at, test) VALUES (?, ?, 0, ?, ?) "
               "ON CONFLICT (share_id) DO NOTHING", (share_id, count, iso(now), 1 if test else 0))
    if count is not None:
        db.execute("UPDATE shares_in SET count = ? WHERE share_id = ? AND count IS NULL", (count, share_id))
    if test:
        db.execute("UPDATE shares_in SET test = 1 WHERE share_id = ?", (share_id,))
    return db.one("SELECT * FROM shares_in WHERE share_id = ?", (share_id,))


def pending(db: Database) -> dict:
    """The shares not yet fetched by the Mac, oldest first, each with its photos — one query."""
    rows = db.query("SELECT s.share_id, s.count, s.received, s.started_at, s.ended_at, s.last_received_at, s.test, "
                    "u.name, u.n, u.sha256, u.size, u.content_type, u.received_at "
                    "FROM shares_in s LEFT JOIN share_uploads u ON u.share_id = s.share_id "
                    "WHERE s.fetched_at IS NULL ORDER BY s.started_at, s.share_id, u.n, u.name")
    shares: dict[str, dict] = {}
    for row in rows:
        share = shares.get(row["share_id"])
        if share is None:
            share = shares[row["share_id"]] = {
                "share_id": row["share_id"], "count": row["count"], "received": int(row["received"] or 0),
                "ended": row["ended_at"] is not None, "test": bool(row["test"]), "started_at": row["started_at"],
                "ended_at": row["ended_at"], "last_received_at": row["last_received_at"], "photos": []}
        if row["name"] is not None:
            share["photos"].append({"name": row["name"], "n": row["n"], "sha256": row["sha256"], "size": row["size"],
                                    "content_type": row["content_type"], "received_at": row["received_at"],
                                    "path": f"/shares/{row['share_id']}/photo/{row['name']}"})
    return {"shares": list(shares.values())}


def photo(db: Database, blobs, share_id: str, name: str) -> tuple[bytes, str]:
    """A stored photo's bytes and Content-Type; 404 for one that isn't there (a row without its blob too)."""
    share_id = share_id_of(share_id)
    row = db.one("SELECT name, content_type FROM share_uploads WHERE share_id = ? AND name = ?", (share_id, name))
    found = blobs.get(f"{share_id}/{row['name']}") if row else None
    if found is None:
        raise NotFound("no such photo")
    return found[0], row["content_type"] or found[1]


def mark_fetched(db: Database, share_id: str, body: dict, now: datetime) -> dict:
    """The Mac has the share: fetched_at set (again is fine), the names it took accepted for the record."""
    share_id = share_id_of(share_id)
    names = body.get("names") if isinstance(body, dict) else None
    if names is not None and not isinstance(names, list):
        raise BadRequest("names is a list")
    row = db.one("SELECT received, fetched_at FROM shares_in WHERE share_id = ?", (share_id,))
    if row is None:
        raise NotFound("no such share")
    db.execute("UPDATE shares_in SET fetched_at = COALESCE(fetched_at, ?) WHERE share_id = ?", (iso(now), share_id))
    return {"ok": True, "share_id": share_id, "received": int(row["received"] or 0),
            "fetched_at": row["fetched_at"] or iso(now)}


def cleanup(db: Database, now: datetime) -> int:
    """The rows older than KEEP_DAYS, gone (timers.daily); the blobs go by the account's lifecycle rule."""
    cutoff = iso(now - timedelta(days=KEEP_DAYS))
    gone = db.execute("DELETE FROM share_uploads WHERE received_at < ?", (cutoff,))
    gone += db.execute("DELETE FROM shares_in WHERE started_at < ?", (cutoff,))
    return gone


def _param(query: dict, name: str) -> str:
    value = query.get(name)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip()
