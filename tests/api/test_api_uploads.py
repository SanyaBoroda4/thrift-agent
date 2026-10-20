"""WO45 §2 (api/thrift_api/uploads.py): the iPhone's shares straight to the API — each photo POSTed as a raw image or
as the one file of a form, the share's end, the Mac's listing, download and fetched call; the share id, the image and
size rules; the same bytes once, a reused name suffixed; the test flag; the daily cleanup; a test message with its own
text."""
import hashlib
import json
from datetime import timedelta

import pytest
from apitools import NOW, call, utc

from thrift_api import http, timers, uploads

SID = "2026-10-18_153045_4821"
PHOTO = f"/share/{SID}/photo"


def jpeg(seed: bytes = b"a") -> bytes:
    """A JPEG by its magic bytes, with line breaks and a "--" inside (a form's delimiter must not be fooled)."""
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + seed * 40 + b"\r\n--\r\n\n" + bytes(range(256))


def png() -> bytes:
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(20)


def heic() -> bytes:
    return b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + bytes(64)


def webp() -> bytes:
    return b"RIFF\x24\x00\x00\x00WEBPVP8 " + bytes(24)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(autouse=True)
def blobs(monkeypatch):
    """The store is memory in every test (never Blob Storage, whatever the environment says)."""
    store = uploads.MemoryStore()
    monkeypatch.setattr(uploads, "_STORE", store)
    return store


def upload(db, data: bytes, sid: str = SID, ctype: str = "image/jpeg", now=NOW, **query):
    status, _, body = http.handle("POST", f"/share/{sid}/photo", {k: str(v) for k, v in query.items()}, data, now=now,
                                  db=db, headers={"Content-Type": ctype})
    return status, json.loads(body)


def end(db, sid: str = SID, now=NOW, body: bytes = b"", **query):
    status, _, out = http.handle("POST", f"/share/{sid}/end", {k: str(v) for k, v in query.items()}, body, now=now,
                                 db=db, headers={"content-type": "application/json"})
    return status, json.loads(out)


def pending(db) -> list:
    return call(db, "GET", "/shares/pending")[1]["shares"]


def form(data: bytes, filename: str | None = "IMG_0001.jpeg", field: str = "photo",
         boundary: str = "----ThriftBoundaryX7") -> tuple[bytes, str]:
    """Shortcuts' "Form" body: a text field first, then the file under any field name."""
    disposition = f'form-data; name="{field}"' + (f'; filename="{filename}"' if filename else "")
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="note"\r\n\r\nhello\r\n'
            f"--{boundary}\r\nContent-Disposition: {disposition}\r\nContent-Type: application/octet-stream\r\n\r\n")
    return head.encode() + data + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def test_a_raw_photo_round_trip(db, blobs):
    assert upload(db, heic(), ctype="image/heic", n=1, of=2, name="IMG_1293.HEIC")[1] == {
        "ok": True, "received": 1, "name": "IMG_1293.HEIC", "stored": True}
    assert upload(db, jpeg(), n=2, of=2, name="IMG_1294.jpeg")[1]["received"] == 2
    [share] = pending(db)
    assert {k: share[k] for k in ("share_id", "count", "received", "ended", "test", "started_at")} == {
        "share_id": SID, "count": 2, "received": 2, "ended": False, "test": False, "started_at": "2026-10-08T15:00:00+00:00"}
    assert share["photos"] == [
        {"name": "IMG_1293.HEIC", "n": 1, "sha256": sha(heic()), "size": len(heic()), "content_type": "image/heic",
         "received_at": "2026-10-08T15:00:00+00:00", "path": f"/shares/{SID}/photo/IMG_1293.HEIC"},
        {"name": "IMG_1294.jpeg", "n": 2, "sha256": sha(jpeg()), "size": len(jpeg()), "content_type": "image/jpeg",
         "received_at": "2026-10-08T15:00:00+00:00", "path": f"/shares/{SID}/photo/IMG_1294.jpeg"}]
    status, headers, body = http.handle("GET", share["photos"][0]["path"], {}, b"", now=NOW, db=db)
    assert (status, headers["Content-Type"], body) == (200, "image/heic", heic())         # byte for byte, HEIC stays
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert blobs.blobs[f"{SID}/IMG_1293.HEIC"] == (heic(), "image/heic")
    assert end(db, count=2)[1] == {"ok": True, "received": 2, "count": 2}
    assert pending(db)[0]["ended"] is True
    status, answer, _ = call(db, "POST", f"/shares/{SID}/fetched", {"names": ["IMG_1293.HEIC", "IMG_1294.jpeg"]})
    assert (status, answer["ok"], answer["received"], answer["blobs_deleted"]) == (200, True, 2, 2)
    assert call(db, "GET", "/shares/pending")[1] == {"shares": []}
    assert blobs.blobs == {}                                                            # WO47: no copy in Azure
    assert http.handle("GET", share["photos"][0]["path"], {}, b"", now=NOW, db=db)[0] == 404
    assert db.one("SELECT COUNT(*) AS n FROM share_uploads WHERE share_id = ?", (SID,))["n"] == 2   # the ledger stays
    status, answer, _ = call(db, "POST", f"/shares/{SID}/fetched", {"names": []})          # again: fine, nothing left
    assert (status, answer["blobs_deleted"]) == (200, 0)


def test_a_form_body_with_one_file_field(db):
    body, ctype = form(jpeg())
    assert upload(db, body, ctype=ctype, n=1, of=1)[1] == {"ok": True, "received": 1, "name": "IMG_0001.jpeg",
                                                           "stored": True}              # the name from the part
    [share] = pending(db)
    assert share["photos"][0]["sha256"] == sha(jpeg()) and share["photos"][0]["size"] == len(jpeg())   # exact bytes
    body, ctype = form(png(), filename=None, field="whatever")                         # no file name: the query's
    assert upload(db, body, ctype=ctype, n=2, of=2, name="shot.png")[1]["name"] == "shot.png"
    body, ctype = form(webp(), filename="a.webp", boundary="xYz-boundary_1")
    assert upload(db, body, ctype=ctype, n=3)[1]["name"] == "a.webp"
    assert [p["content_type"] for p in pending(db)[0]["photos"]] == ["image/jpeg", "image/png", "image/webp"]


def test_the_multipart_parser_keeps_every_byte():
    data = b"\xff\xd8\xff" + b"\r\n\r\n--x\r\n" * 3 + bytes(range(256)) + b"\r\n"      # breaks and "--" inside
    body, ctype = form(data, boundary="zz-not-in-the-data")
    assert uploads.unwrap(body, ctype) == (data, "IMG_0001.jpeg")
    lf_only = (b"--b\nContent-Disposition: form-data; name=\"f\"; filename=\"p.jpg\"\n\n" + data + b"\n--b--\n")
    assert uploads.unwrap(lf_only, "multipart/form-data; boundary=b") == (data, "p.jpg")
    assert uploads.unwrap(b"raw", "image/jpeg") == (b"raw", None)
    with pytest.raises(uploads.BadRequest):
        uploads.unwrap(b"--b--", "multipart/form-data; boundary=b")                   # no file in it
    with pytest.raises(uploads.BadRequest):
        uploads.unwrap(b"", "multipart/form-data")                                      # no boundary


@pytest.mark.parametrize("sid", ["2026-10-18_153045", "2026-10-18_153045_4821", "2026-10-18_153045_a1b2c3d4"])
def test_share_ids_the_shortcut_makes(db, sid):
    assert upload(db, jpeg(), sid=sid)[0] == 200 and end(db, sid=sid)[0] == 200


@pytest.mark.parametrize("sid", ["x", "2026-10-18", "2026-10-18_1530", "2026-10-18_153045_ABCD", "2026-10-18_153045_",
                                 "2026-10-18_153045_123456789", "..", "2026-10-18_153045_x%2F"])
def test_other_share_ids_are_refused(db, sid):
    status, answer = upload(db, jpeg(), sid=sid)
    assert (status, answer["ok"]) == (400, False) and "share id" in answer["error"]
    assert end(db, sid=sid)[0] == 400
    assert pending(db) == []


def test_only_images_by_their_bytes(db):
    for data in (b"hello", b"GIF89a" + bytes(20), b"", b'{"ok": true}', b"\x00\x00\x00\x18ftypavif" + bytes(40)):
        status, answer = upload(db, data, ctype="image/jpeg")                          # the header is not trusted
        assert (status, answer["ok"]) == (400, False) and "not a JPEG" in answer["error"]
    assert upload(db, jpeg(), ctype="text/plain")[0] == 200                             # the bytes are
    assert pending(db)[0]["received"] == 1


def test_the_size_limit_is_413(db):
    status, answer = upload(db, b"\xff\xd8\xff" + bytes(uploads.MAX_BYTES))
    assert (status, answer) == (413, {"ok": False, "error": "a photo is at most 30 MB"})
    assert upload(db, b"\xff\xd8\xff" + bytes(uploads.MAX_BYTES - 3))[0] == 200


def test_sixty_photos_per_share(db):
    for i in range(uploads.MAX_PHOTOS):
        assert upload(db, jpeg(i.to_bytes(2, "big")), name=f"IMG_{i}.jpg")[1]["received"] == i + 1
    status, answer = upload(db, jpeg(b"zz"), name="IMG_61.jpg")
    assert (status, answer) == (400, {"ok": False, "error": "a share holds at most 60 photos"})
    assert pending(db)[0]["received"] == 60
    assert upload(db, jpeg((3).to_bytes(2, "big")), name="again.jpg")[1]["received"] == 60   # the same bytes: fine


def test_the_same_bytes_once_and_a_reused_name_suffixed(db, blobs):
    assert upload(db, jpeg(), name="IMG_1.jpeg")[1] == {"ok": True, "received": 1, "name": "IMG_1.jpeg", "stored": True}
    assert upload(db, jpeg(), name="IMG_2.jpeg")[1] == {"ok": True, "received": 1, "name": "IMG_1.jpeg", "stored": False}
    assert upload(db, jpeg(b"b"), name="IMG_1.jpeg")[1]["name"] == "IMG_1-2.jpeg"
    assert upload(db, jpeg(b"c"), name="IMG_1.jpeg")[1]["name"] == "IMG_1-3.jpeg"
    assert sorted(blobs.blobs) == [f"{SID}/IMG_1-2.jpeg", f"{SID}/IMG_1-3.jpeg", f"{SID}/IMG_1.jpeg"]
    assert [p["name"] for p in pending(db)[0]["photos"]] == ["IMG_1.jpeg", "IMG_1-2.jpeg", "IMG_1-3.jpeg"]


def test_the_end_before_some_photos(db):
    assert end(db, count=2, body=b"{}")[1] == {"ok": True, "received": 0, "count": 2}
    [share] = pending(db)
    assert (share["ended"], share["count"], share["received"], share["photos"]) == (True, 2, 0, [])
    assert upload(db, jpeg(), n=1, of=2, now=NOW + timedelta(seconds=5))[1]["received"] == 1
    [share] = pending(db)
    assert (share["ended"], share["count"], share["received"], share["last_received_at"]) == (
        True, 2, 1, "2026-10-08T15:00:05+00:00")
    assert end(db, body=b"not json at all")[1] == {"ok": True, "received": 1, "count": 2}   # the body is ignored


def test_the_test_flag(db):
    upload(db, jpeg(), test=1)
    end(db, sid="2026-10-18_160000", count=1, test="true")
    upload(db, jpeg(), sid="2026-10-18_170000")
    assert [(s["share_id"], s["test"]) for s in pending(db)] == [(SID, True), ("2026-10-18_160000", True),
                                                                  ("2026-10-18_170000", False)]


def test_names_are_cleaned_and_follow_the_bytes(db):
    assert upload(db, jpeg(b"1"), name="../../etc/passwd.jpg")[1]["name"] == "passwd.jpg"
    assert upload(db, jpeg(b"2"), name="C:\\Users\\x\\IMG 2.JPG")[1]["name"] == "IMG_2.JPG"
    assert upload(db, jpeg(b"3"), n=3)[1]["name"] == "IMG_0003.jpg"                     # no name at all
    assert upload(db, heic(), name="photo.jpeg")[1]["name"] == "photo.heic"              # the bytes say HEIC
    assert upload(db, png(), name=".hidden")[1]["name"] == "hidden.png"                  # never a dot-file
    assert upload(db, webp(), name="x" * 200 + ".webp")[1]["name"] == "x" * 72 + ".webp"
    assert upload(db, jpeg(b"4"), n="0")[0] == 400 and upload(db, jpeg(b"4"), of="abc")[0] == 400


def test_an_unknown_photo_or_share(db):
    assert call(db, "GET", f"/shares/{SID}/photo/IMG_9.jpg")[0] == 404
    assert call(db, "GET", "/shares/bad-id/photo/IMG_9.jpg")[0] == 400
    assert call(db, "POST", f"/shares/{SID}/fetched", {"names": []})[0] == 404
    upload(db, jpeg(), name="IMG_1.jpg")
    assert http.handle("GET", f"/shares/{SID}/photo/IMG_1.jpg", {}, b"", now=NOW, db=db)[0] == 200
    assert call(db, "GET", f"/shares/{SID}/photo/IMG_2.jpg")[0] == 404
    assert call(db, "POST", f"/shares/{SID}/fetched", {"names": "IMG_1.jpg"})[0] == 400
    assert call(db, "GET", f"/share/{SID}/photo")[0] == 405


@pytest.mark.parametrize("three_am", [utc(2026, 10, 8, 7, 30)])
def test_the_daily_cleanup_drops_shares_after_thirty_days(db, three_am):
    upload(db, jpeg(b"old"), sid="2026-09-01_120000", now=three_am - timedelta(days=31))
    end(db, sid="2026-09-01_120000", count=1, now=three_am - timedelta(days=31))
    upload(db, jpeg(b"new"), sid="2026-09-30_120000", now=three_am - timedelta(days=10))
    assert timers.daily(db, three_am)["shares_deleted"] == 2                             # the row and its photo
    assert [s["share_id"] for s in pending(db)] == ["2026-09-30_120000"]


def test_a_test_message_with_its_own_text(db, sent, monkeypatch):
    line = "🔑 The upload key for the Shortcut: abc — paste it into the Shortcut's x-functions-key header"
    assert call(db, "POST", "/test-message", {"chat": "ops", "text": line})[1] == {"sent": True, "chat": "ops",
                                                                                   "mode": "replay"}
    assert sent == [("ops", "[replay] " + line, False)]
    monkeypatch.setenv("SALES_MODE", "live")
    assert call(db, "POST", "/test-message", {"chat": "ops", "text": line})[1]["sent"] is True
    assert sent[-1] == ("ops", line, False)
    assert call(db, "POST", "/test-message", {"chat": "ops", "text": 5})[0] == 400
    assert call(db, "POST", "/test-message", {"chat": "ops", "text": "  "})[0] == 400
    assert call(db, "POST", "/test-message", {"chat": "ops"})[1]["sent"] is True       # the usual line still


def test_the_store_is_memory_without_a_connection_string(monkeypatch, caplog):
    monkeypatch.setattr(uploads, "_STORE", None)
    monkeypatch.delenv(uploads.CONNECTION_ENV, raising=False)
    assert isinstance(uploads.store(), uploads.MemoryStore) and uploads.store() is uploads.store()
    store = uploads.MemoryStore()
    store.put("a/b.jpg", b"x", "image/jpeg")
    assert store.get("a/b.jpg") == (b"x", "image/jpeg") and store.delete("a/b.jpg") and not store.delete("a/b.jpg")
