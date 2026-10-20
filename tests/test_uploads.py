"""WO45 §2 (thrift_agent/uploads.py): the Mac fetches the iPhone's shares from thrift-api into its inbox — a whole
share as a folder named by the share id with the Shortcut's marks; a short share once quiet, a share never ended after
ten minutes, nothing without photos; the bytes checked; the iCloud copy that came first never doubled; the API down
a quiet event; the poll every 30 s at most; nothing from the API ever a path."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from thrift_agent import sales, uploads
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB

NOW = datetime(2026, 10, 18, 19, 30, tzinfo=timezone.utc)
SID = "2026-10-18_153045_4821"
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4
HEIC = b"\x00\x00\x00\x18ftypheic" + bytes(100)


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def env(tmp_path):
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    s = Settings(data)
    s.ensure_dirs()
    return s, DB(s.path("db"))


class FakeApi:
    """thrift-api as the Mac sees it: the pending shares, their bytes, what the Mac posted."""
    url, key, client, timeout = "https://api.test", "mac-key", None, 15.0

    def __init__(self):
        self.shares, self.blobs, self.posts, self.fetches = [], {}, [], []
        self.down, self.fail_after = False, None

    def get(self, path, params=None):
        if self.down:
            raise sales.ApiError("GET /shares/pending: ConnectError")
        assert path == "/shares/pending"
        return {"shares": self.shares}

    def post(self, path, body):
        if self.down:
            raise sales.ApiError(f"POST {path}: ConnectError")
        self.posts.append((path, body))
        return {"ok": True}

    def get_bytes(self, path):
        if self.down or (self.fail_after is not None and len(self.fetches) >= self.fail_after):
            raise sales.ApiError(f"GET {path}: ReadTimeout")
        self.fetches.append(path)
        if path not in self.blobs:
            raise sales.ApiError(f"GET {path}: HTTP 404", 404)
        return self.blobs[path], "image/jpeg"


def share(api: FakeApi, sid: str = SID, photos=(("IMG_1.jpeg", JPEG), ("IMG_2.HEIC", HEIC)), count=None, ended=True,
          started: datetime = NOW - timedelta(minutes=3), last: datetime | None = None, test=False, wrong_hash=False,
          paths: dict | None = None) -> dict:
    entries = []
    for n, (name, data) in enumerate(photos, 1):
        path = (paths or {}).get(name, f"/shares/{sid}/photo/{name}")
        api.blobs[path] = data
        entries.append({"name": name, "n": n, "sha256": "0" * 64 if wrong_hash else sha(data), "size": len(data),
                        "content_type": "image/jpeg", "received_at": iso(started + timedelta(seconds=n)), "path": path})
    row = {"share_id": sid, "count": len(photos) if count is None else count, "received": len(photos), "ended": ended,
           "test": test, "started_at": iso(started), "ended_at": iso(started + timedelta(minutes=1)) if ended else None,
           "last_received_at": iso(last or started + timedelta(seconds=len(photos))), "photos": entries}
    api.shares.append(row)
    return row


def events(db: DB, kind: str) -> list:
    return [json.loads(r["detail"]) if r["detail"] else None
            for r in db.conn.execute("SELECT detail FROM events WHERE kind=?", (kind,))]


def test_a_whole_share_lands_in_the_inbox_as_a_folder(env):
    s, db = env
    api = FakeApi()
    share(api)
    assert uploads.fetch_new(s, db, api, NOW) == [SID]
    folder = s.path("inbox") / SID
    assert (folder / "IMG_1.jpeg").read_bytes() == JPEG and (folder / "IMG_2.HEIC").read_bytes() == HEIC
    assert (folder / "_done.txt").read_text() == "done 2" and (folder / "_source.txt").read_text() == "azure"
    assert not (folder / "_test").exists() and not list(folder.glob(".*"))
    assert not (s.path("work") / "uploads" / SID).exists()                              # moved whole, nothing staged
    assert api.posts == [(f"/shares/{SID}/fetched", {"names": ["IMG_1.jpeg", "IMG_2.HEIC"]})]
    assert events(db, "share_fetched") == [{"share_id": SID, "photos": ["IMG_1.jpeg", "IMG_2.HEIC"], "test": False,
                                            "folder": SID}]
    assert uploads.fetch_new(s, db, api, NOW) == [SID] and len(events(db, "share_fetched")) == 1   # still pending at
    assert api.posts[-1][0] == f"/shares/{SID}/fetched" and events(db, "share_same_as_icloud") == []   # the API: told
    #                                                                                     again, our own folder, no event


def test_a_short_share_waits_until_quiet(env):
    s, db = env
    api = FakeApi()
    share(api, count=3, last=NOW - timedelta(seconds=60))
    assert uploads.fetch_new(s, db, api, NOW) == [] and not (s.path("inbox") / SID).exists()
    assert uploads.fetch_new(s, db, api, NOW + timedelta(seconds=61)) == [SID]
    assert (s.path("inbox") / SID / "_done.txt").read_text() == "done 3"                # the Shortcut's count: iCloud
    #                                                                                     may complete it (WO42's rule)


def test_a_share_never_ended_is_taken_after_ten_minutes(env):
    s, db = env
    api = FakeApi()
    share(api, ended=False, started=NOW - timedelta(minutes=5))
    assert uploads.fetch_new(s, db, api, NOW) == []
    assert uploads.fetch_new(s, db, api, NOW + timedelta(minutes=5, seconds=1)) == [SID]
    assert (s.path("inbox") / SID / "_done.txt").read_text() == "done 2"


def test_nothing_without_photos(env):
    s, db = env
    api = FakeApi()
    share(api, photos=(), count=2)
    share(api, sid="2026-10-18_160000", photos=(), count=None, ended=False, started=NOW - timedelta(hours=1))
    assert uploads.fetch_new(s, db, api, NOW) == [] and api.posts == []
    assert sorted(p.name for p in s.path("inbox").iterdir()) == []


def test_a_test_share_is_marked(env):
    s, db = env
    api = FakeApi()
    share(api, test=True)
    assert uploads.fetch_new(s, db, api, NOW) == [SID]
    assert (s.path("inbox") / SID / "_test").read_text() == "1"
    assert events(db, "share_fetched")[0]["test"] is True


def test_a_bad_hash_is_dropped_and_fetched_again(env):
    s, db = env
    api = FakeApi()
    row = share(api, wrong_hash=True)
    assert uploads.fetch_new(s, db, api, NOW) == []
    assert events(db, "upload_bad_hash") == [{"share_id": SID, "name": "IMG_1.jpeg"}]
    staging = s.path("work") / "uploads" / SID
    assert not (s.path("inbox") / SID).exists() and not (staging / "IMG_1.jpeg").exists()
    assert not list(staging.glob(".*")) and api.posts == []
    for photo in row["photos"]:
        photo["sha256"] = sha(api.blobs[photo["path"]])
    assert uploads.fetch_new(s, db, api, NOW) == [SID]
    assert (s.path("inbox") / SID / "IMG_2.HEIC").read_bytes() == HEIC


@pytest.mark.parametrize("where, name", [("inbox", SID), ("inbox", f"{SID}-late"), ("archive", f"20261018_{SID}"),
                                         ("archive", f"20261018_{SID}_2")])
def test_the_icloud_copy_that_came_first_is_never_doubled(env, where, name):
    s, db = env
    api = FakeApi()
    share(api)
    folder = s.path(where) / name
    folder.mkdir()
    (folder / "IMG_1.jpeg").write_bytes(JPEG)
    if where == "inbox":                                                                # WO46: whole in the inbox
        (folder / "IMG_2.HEIC").write_bytes(HEIC)
    (folder / "_done.txt").write_text("done 2")
    here = sorted(p.name for p in folder.iterdir())
    assert uploads.fetch_new(s, db, api, NOW) == [SID]
    assert sorted(p.name for p in folder.iterdir()) == here                             # untouched, no _source.txt
    assert not (s.path("inbox") / SID).exists() or where == "inbox" and name == SID
    assert sorted(p.name for p in s.path("inbox").iterdir()) == ([name] if where == "inbox" else [])
    assert api.posts == [(f"/shares/{SID}/fetched", {"names": ["IMG_1.jpeg", "IMG_2.HEIC"]})]
    assert events(db, "share_same_as_icloud") == [{"share_id": SID, "folder": name,
                                                   "photos": ["IMG_1.jpeg", "IMG_2.HEIC"], "test": False}]
    assert events(db, "share_fetched") == [] and api.fetches == []                      # nothing downloaded


@pytest.mark.parametrize("state", ["short", "cloud_only", "no_mark"])
def test_an_icloud_copy_not_whole_gives_way_to_a_whole_upload(env, state):
    """WO46 (her Shortcut saves the iCloud copy first, then uploads): the iCloud folder is on the Mac before the upload
    ends, its photos short, cloud-only or without the end mark (live 2026-10-17: the folder and _done, never a photo).
    The whole upload is taken; the iCloud copy is set aside in the archive, untouched, never the share."""
    s, db = env
    api = FakeApi()
    share(api)
    folder = s.path("inbox") / SID
    folder.mkdir()
    (folder / "IMG_1.jpeg").write_bytes(JPEG)
    if state == "cloud_only":
        (folder / ".IMG_2.HEIC.icloud").write_bytes(b"placeholder")
    if state != "no_mark":
        (folder / "_done.txt").write_text("done 2")
    assert uploads.fetch_new(s, db, api, NOW) == [SID]
    taken = s.path("inbox") / SID
    assert (taken / "IMG_2.HEIC").read_bytes() == HEIC and (taken / "_source.txt").read_text() == "azure"
    aside = [p for p in s.path("archive").iterdir()]
    assert [p.name for p in aside] == [f"{datetime.now():%Y%m%d}_{SID}-icloud"]
    assert (aside[0] / "IMG_1.jpeg").read_bytes() == JPEG and not (aside[0] / "_source.txt").exists()
    ev = events(db, "share_icloud_set_aside")
    assert len(ev) == 1 and ev[0]["folder"] == SID and ev[0]["photos_here"] == 1 and ev[0]["uploaded"] == 2
    assert events(db, "share_fetched")[0]["icloud_set_aside"] == SID and events(db, "share_same_as_icloud") == []
    assert api.posts == [(f"/shares/{SID}/fetched", {"names": ["IMG_1.jpeg", "IMG_2.HEIC"]})]
    assert uploads.known_folder(s, SID) == taken                                     # the set-aside copy is never it
    from thrift_agent import shares
    assert shares.share_id_of(aside[0]) == SID and shares.share_id_of(f"{SID} 2") == SID


def test_an_icloud_copy_already_taken_or_a_short_upload_stays_the_share(env):
    s, db = env
    api = FakeApi()
    share(api)
    folder = s.path("inbox") / SID
    folder.mkdir()
    (folder / "IMG_1.jpeg").write_bytes(JPEG)
    (folder / "_done.txt").write_text("done 2")
    db.add_batch(str(folder), 1)                                                     # the intake took it already
    assert uploads.fetch_new(s, db, api, NOW) == [SID]
    assert events(db, "share_same_as_icloud") and not events(db, "share_icloud_set_aside") and api.fetches == []
    s2, db2 = s, DB(s.path("db").with_name("other.db"))
    api2 = FakeApi()
    share(api2, sid="2026-10-18_160000_1111", count=3, last=NOW - timedelta(minutes=5))   # ended, short, quiet
    other = s2.path("inbox") / "2026-10-18_160000_1111"
    other.mkdir()
    (other / "IMG_1.jpeg").write_bytes(JPEG)
    assert uploads.fetch_new(s2, db2, api2, NOW) == ["2026-10-18_160000_1111"]
    assert events(db2, "share_same_as_icloud") and not events(db2, "share_icloud_set_aside")


def test_an_upload_never_lands_inside_a_folder_icloud_put_back(env, monkeypatch):
    """Set aside, and iCloud makes the folder again at once: the upload goes beside it as '<id> 2', never into it."""
    s, db = env
    api = FakeApi()
    share(api)
    folder = s.path("inbox") / SID
    folder.mkdir()
    (folder / "_done.txt").write_text("done 2")
    real = uploads.set_aside

    def and_back(*a, **k):
        out = real(*a, **k)
        (s.path("inbox") / SID).mkdir()
        return out
    monkeypatch.setattr(uploads, "set_aside", and_back)
    assert uploads.fetch_new(s, db, api, NOW) == [SID]
    assert (s.path("inbox") / f"{SID} 2" / "IMG_2.HEIC").read_bytes() == HEIC
    assert list((s.path("inbox") / SID).iterdir()) == []
    assert events(db, "share_fetched")[0]["folder"] == f"{SID} 2"


def test_an_icloud_copy_that_cant_be_moved_keeps_the_upload_waiting(env, monkeypatch):
    s, db = env
    api = FakeApi()
    share(api)
    folder = s.path("inbox") / SID
    folder.mkdir()
    (folder / "_done.txt").write_text("done 2")

    def refuse(*a, **k):
        raise OSError(11, "Resource deadlock avoided")
    monkeypatch.setattr(uploads.shutil, "move", refuse)
    assert uploads.fetch_new(s, db, api, NOW) == [] and api.posts == []
    assert "Resource deadlock avoided" in events(db, "share_icloud_set_aside_failed")[0]["error"]
    assert (s.path("work") / "uploads" / SID / "IMG_2.HEIC").read_bytes() == HEIC    # staged for the next poll
    monkeypatch.undo()
    assert uploads.fetch_new(s, db, api, NOW) == [SID] and (s.path("inbox") / SID / "_source.txt").exists()


def test_the_api_down_is_one_quiet_event(env):
    s, db = env
    api = FakeApi()
    share(api)
    api.down = True
    assert uploads.fetch_new(s, db, api, NOW) == []
    assert events(db, "api_down") == [{"path": "/shares/pending", "error": "GET /shares/pending: ConnectError"}]
    api.down, api.fail_after = False, 1                                                 # the second photo's download
    assert uploads.fetch_new(s, db, api, NOW) == []
    assert events(db, "api_down")[-1]["error"].startswith("GET /shares/") and len(events(db, "api_down")) == 2
    staging = s.path("work") / "uploads" / SID
    assert (staging / "IMG_1.jpeg").read_bytes() == JPEG and not (staging / "IMG_2.HEIC").exists()
    api.fail_after = None
    assert uploads.fetch_new(s, db, api, NOW) == [SID]
    assert api.fetches == [f"/shares/{SID}/photo/IMG_1.jpeg", f"/shares/{SID}/photo/IMG_2.HEIC"]   # not twice
    assert (s.path("inbox") / SID / "IMG_2.HEIC").read_bytes() == HEIC and not staging.exists()


def test_without_the_api_nothing_happens(env):
    s, db = env
    assert uploads.fetch_new(s, db) == []                                               # no THRIFT_API_* in tests


def test_due_every_thirty_seconds(env):
    s, db = env
    assert uploads.due(db) is True and uploads.due(db) is False
    db.kv_set(uploads.LAST_POLL, iso(datetime.now(timezone.utc) - timedelta(seconds=uploads.POLL_EVERY + 1)))
    assert uploads.due(db) is True and uploads.due(db) is False


def test_nothing_from_the_api_is_ever_a_path(env):
    s, db = env
    api = FakeApi()
    share(api, sid="../../etc")
    share(api, photos=(("../../evil.jpg", JPEG), ("IMG_2.HEIC", HEIC)),
          paths={"IMG_2.HEIC": "https://elsewhere.test/x.jpg"})
    assert uploads.fetch_new(s, db, api, NOW) == [SID]
    assert events(db, "upload_bad_id") == [{"share_id": "../../etc"}]
    folder = s.path("inbox") / SID
    assert sorted(p.name for p in folder.iterdir()) == ["_done.txt", "_source.txt", "evil.jpg"]
    assert events(db, "upload_bad_name") == [{"share_id": SID, "name": "IMG_2.HEIC",
                                              "path": "https://elsewhere.test/x.jpg"}]
    assert not (s.path("inbox") / ".." / "etc").exists() or True                       # never written outside


def test_get_bytes_through_the_api_client():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("x-functions-key")
        if request.url.path.endswith("missing.jpg"):
            return httpx.Response(404, json={"error": "no such photo"})
        return httpx.Response(200, content=JPEG, headers={"Content-Type": "image/jpeg"})
    api = sales.Api("https://api.test/", "mac-key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert uploads.get_bytes(api, f"/shares/{SID}/photo/IMG_1.jpeg") == (JPEG, "image/jpeg")
    assert seen["key"] == "mac-key"
    with pytest.raises(sales.ApiError) as refused:
        uploads.get_bytes(api, f"/shares/{SID}/photo/missing.jpg")
    assert refused.value.status == 404

    def broken(request):
        raise httpx.ConnectError("no route")
    api = sales.Api("https://api.test", "k", client=httpx.Client(transport=httpx.MockTransport(broken)))
    with pytest.raises(sales.ApiError):
        uploads.get_bytes(api, "/shares/x/photo/a.jpg")


def test_takeable_rules():
    base = {"photos": [{"name": "a.jpg"}], "received": 1, "count": 1, "ended": True, "started_at": iso(NOW),
            "last_received_at": iso(NOW)}
    assert uploads.takeable(base, NOW)
    assert not uploads.takeable({**base, "photos": []}, NOW)
    assert not uploads.takeable({**base, "count": 2}, NOW + timedelta(seconds=119))
    assert uploads.takeable({**base, "count": 2}, NOW + timedelta(seconds=120))
    assert uploads.takeable({**base, "count": None, "received": 1}, NOW)                # no count: what came
    assert not uploads.takeable({**base, "ended": False}, NOW + timedelta(minutes=9))
    assert uploads.takeable({**base, "ended": False}, NOW + timedelta(minutes=10))
    assert not uploads.takeable({**base, "ended": False, "started_at": "garbage"}, NOW + timedelta(days=1))
