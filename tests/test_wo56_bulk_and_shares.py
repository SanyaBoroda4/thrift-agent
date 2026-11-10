"""WO56 §0.3 and §6 (2026-11-09).

§0.3 — the photo check's bulk read (the architect, read only: the Tory Burch loafers `i_261012_6e29a8` live on Depop yet
flagged "not in bulk" there since Nov 3 — the Depop bulk read was the shop page's first 25 tiles every time; on Vinted
really gone, its item JSON 404): every listing of a shop of 200+ is found; a listing the bulk data lacks is read by its
OWN data — live clears it, sold is the sale net, a 404 (seen twice, 10+ minutes apart) is "gone from <Site>" for that
row only — never a take-down elsewhere, never relisted; the one re-check of the old flags (`thrift photocheck
--recheck-not-in-bulk`); a row gone from one site no longer stops the item's price drops on the others.

§6 — the Shortcut's new order (the upload first; the iCloud copy only without internet): a whole upload is taken at
once (no 📭, no wait); a share that stopped at 7 of 12 with no iCloud copy is held, never posted with 7, and ONE "share
them again" line comes 15 minutes after its last photo; shared again, the new share replaces the held one — one item,
12 photos, nothing skipped as a duplicate.
(The extension's paging and own-data reads: tests/js/content.test.mjs, "WO56".)"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import random
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from thrift_agent import cli, photocheck, photomem, pipeline, pricedrop, sales, uploads
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB, loads


@pytest.fixture
def env(tmp_path, monkeypatch):
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    s = Settings(data)
    s.ensure_dirs()
    db = DB(s.path("db"))
    said, group = [], []
    monkeypatch.setattr(pipeline.notify, "say", said.append)            # one module: uploads', photocheck's too
    monkeypatch.setattr(pipeline.notify, "group", group.append)
    monkeypatch.setattr(cli.uploads, "due", lambda db_: False)          # the tests fetch from their own Azure
    api_calls = []
    monkeypatch.setattr(sales, "call", lambda db_, path, body: api_calls.append((path, body)) or True)
    return s, db, said, group, api_calls


def events(db: DB, kind: str) -> list:
    return [json.loads(r["detail"]) if r["detail"] and r["detail"][:1] in "{[" else r["detail"]
            for r in db.conn.execute("SELECT detail FROM events WHERE kind=? ORDER BY rowid", (kind,))]


# ======================================================================== §0.3 the photo check's bulk read

POSTED = "2026-10-13T01:20:09+00:00"


def _item(db: DB, n: int, sites=("depop", "vinted"), title=None) -> str:
    bid = db.add_batch(f"/shares/2026-10-12_1200{n:02d}_{n:04d}-{n}", 3)
    db.set_batch(bid, status="split")
    iid = db.add_item(bid, 1, f"/work/{bid}/item_01")
    db.set_item(iid, status="posted", renders={"poshmark": {"title": title or f"Brand Item Number {n} size M"}})
    for mp in sites:
        url = {"poshmark": f"https://poshmark.com/listing/Brand-Item-{n}-6acd94d6cbbea59039cf{n:04x}",
               "depop": f"https://www.depop.com/products/tetianasorokina-brand-item-{n}-{n:04x}/",
               "vinted": f"https://www.vinted.com/items/{10356600000 + n}"}[mp]
        db.upsert_listing(iid, mp, status="posted", url=url, price=40, posted_at=POSTED,
                          fields_json={"photos": ["a.jpg", "b.jpg", "c.jpg"]})
    return iid


def _key(db: DB, iid: str, mp: str) -> str:
    return photocheck._key_of(mp, db.listing(iid, mp))


class Shop:
    """A site's shop as the extension reads it: every listing in its bulk data (the paging is the page script's —
    tests/js), and each missing one's own state on request."""

    def __init__(self, mp, records, states=None):
        self.mp, self.records, self.states, self.asked = mp, records, dict(states or {}), []

    def available(self):
        return True

    async def read_shop(self):
        return {"listings": list(self.records), "shape": ["id", "slug", "price", "photos"], "source": "json",
                "pages": max(1, len(self.records) // 96 + 1), "complete": True}

    async def listing_states(self, keys, control=None):
        self.asked.append(([k["key"] for k in keys], control))
        return {"states": {k["key"]: self.states.get(k["key"], {"state": "unknown"}) for k in keys}, "trusted": True,
                "control": control}

    async def read_photos(self, url, shots=None, sku=None):                  # never needed when the bulk has a count
        raise AssertionError(f"an edit page was opened: {url}")


def _record(db, iid, mp):
    key = _key(db, iid, mp)
    return ({"slug": key, "url": db.listing(iid, mp)["url"]} if mp == "depop" else {"id": key}) | {
        "price": "40", "photos": 3, "sold": False}


def _age(db: DB, key: str, minutes: float) -> None:
    """Time passing for one missing listing: its own-data read and its first 'gone' sighting `minutes` older."""
    absent = loads(db.kv_get(photocheck.ABSENT)) or {}
    if key in absent and absent[key].get("checked"):
        t = datetime.fromisoformat(absent[key]["checked"]) - timedelta(minutes=minutes)
        absent[key]["checked"] = t.isoformat(timespec="seconds")
        db.kv_set(photocheck.ABSENT, json.dumps(absent))
    seen = loads(db.kv_get(photocheck.GONE_SEEN)) or {}
    if key in seen:
        seen[key] = float(seen[key]) - minutes * 60
        db.kv_set(photocheck.GONE_SEEN, json.dumps(seen))


def _due_again(db: DB) -> None:
    """Six hours on: every live listing due for a read, the shops for a bulk read."""
    db.conn.execute("UPDATE listings SET photos_read_at=NULL")
    db.kv_set(photocheck.BULK_AT, "{}")


def test_the_bulk_read_of_a_shop_of_200_finds_every_listing_and_flags_nothing_without_its_own_404_or_sold(env):
    """WO56 §5: 210 listings on each shop, every one in the bulk data — read, nothing flagged, no page opened. Then
    three missing from it: each read by its own data — Depop's live one cleared (its count applied), Depop's 404 only
    remembered at first and recorded gone 10+ minutes later on a second 404 (that row only: no take-down elsewhere,
    one private line), Vinted's sold one the sale net."""
    s, db, said, group, api_calls = env
    items = [_item(db, n) for n in range(210)]
    depop = Shop("depop", [_record(db, i, "depop") for i in items])
    vinted = Shop("vinted", [_record(db, i, "vinted") for i in items])
    ps = {"depop": depop, "vinted": vinted}
    asyncio.run(photocheck.run(s, db, ps))
    assert db.conn.execute("SELECT COUNT(*) FROM listings WHERE photos_live=3").fetchone()[0] == 420
    assert events(db, "photocheck_not_in_bulk") == [] and depop.asked == [] and vinted.asked == []
    assert [e["listings"] for e in events(db, "photocheck_bulk")] == [210, 210]
    assert events(db, "listing_gone") == [] and api_calls == [] and said == []

    live, gone, sold = items[3], items[150], items[209]
    depop.records = [r for r in depop.records if r["slug"] not in (_key(db, live, "depop"), _key(db, gone, "depop"))]
    vinted.records = [r for r in vinted.records if r["id"] != _key(db, sold, "vinted")]
    depop.states = {_key(db, live, "depop"): {"state": "live", "http": 200, "how": "its Depop page", "photos": 3,
                                              "price": "40.00"},
                    _key(db, gone, "depop"): {"state": "gone", "http": 404, "how": "its Depop page"}}
    vinted.states = {_key(db, sold, "vinted"): {"state": "sold", "http": 200, "how": "Vinted's item data",
                                                "price": "40.0"}}
    _due_again(db)
    asyncio.run(photocheck.run(s, db, ps))
    assert sorted((e["site"], e["key"]) for e in events(db, "photocheck_not_in_bulk")) == sorted(
        [("depop", _key(db, live, "depop")), ("depop", _key(db, gone, "depop")), ("vinted", _key(db, sold, "vinted"))])
    assert [sorted(k) for k, _ in depop.asked] == [sorted([_key(db, live, "depop"), _key(db, gone, "depop")])]
    assert db.listing(live, "depop")["status"] == "posted" and db.listing(live, "depop")["photos_live"] == 3
    assert (loads(db.kv_get(photocheck.ABSENT)) or {})[f"{live}|depop"]["state"] == "live"   # cleared: read again
    assert events(db, "photocheck_absent_cleared")[0]["key"] == _key(db, live, "depop")      # once a day, not flagged
    assert db.listing(gone, "depop")["status"] == "posted"                             # one 404: only remembered
    assert db.listing(sold, "vinted")["status"] == "sold"                               # the sale net: at once
    assert [b["kind"] for _, b in api_calls] == ["sold_found"] and api_calls[0][1]["marketplace"] == "vinted"

    asyncio.run(photocheck.run(s, db, ps))                                              # minutes later: not due yet
    assert len(depop.asked) == 1
    _age(db, f"{gone}|depop", 11)                                                       # 11 minutes on: a second 404
    asyncio.run(photocheck.run(s, db, ps))
    assert depop.asked[-1][0] == [_key(db, gone, "depop")]
    row = db.listing(gone, "depop")
    assert row["status"] == "delisted"
    assert row["error"] == "gone from Depop (its own data: its Depop page answers 404) — not relisted"
    assert db.listing(gone, "vinted")["status"] == "posted"                            # nothing else taken down
    assert [b["kind"] for _, b in api_calls] == ["sold_found"]                           # no not_for_sale, ever
    assert said == [("Depop no longer has Brand Item Number 150 size M (deleted by you, or removed by Depop — check the "
                     "Depop app's notifications). Still live on Vinted.")]
    from thrift_agent.post import runner
    assert runner.next_job(s, db, ["depop"], False) is None                             # never relisted
    asyncio.run(photocheck.run(s, db, ps))
    assert len(said) == 1                                                               # one line


def test_only_a_sale_takes_the_item_down_on_its_other_sites(env):
    """WO56 §0.3: until WO56 a listing gone from one site (a 404, Poshmark's Not for Sale) told thrift-api
    `not_for_sale` — take-downs on every other site. Now a gone listing is its own row only; a sale still acts."""
    s, db, said, group, api_calls = env
    iid = _item(db, 1, sites=("poshmark", "depop", "vinted"), title="Tory Burch New Croc-Embossed Block Heel Loafers")
    assert sales.listing_gone(db, iid, "vinted", "its own data: Vinted's item data answers 404", False,
                              db.listing(iid, "vinted")["url"]) == "gone"
    assert api_calls == []
    assert db.listing(iid, "vinted")["status"] == "delisted"
    assert db.listing(iid, "vinted")["error"].startswith("gone from Vinted (its own data: Vinted's item data answers")
    assert [db.listing(iid, mp)["status"] for mp in ("poshmark", "depop")] == ["posted", "posted"]
    assert photocheck.gone_line(db, iid, "vinted", "its own data: …") == (
        "Vinted no longer has Tory Burch New Croc-Embossed Block Heel (deleted by you, or removed by Vinted — check the "
        "Vinted app's notifications). Still live on Poshmark and Depop.")
    # Poshmark: its page 404 twice (10+ minutes apart) — gone there only; its Not for Sale the same, said as it is
    other = _item(db, 2, sites=("poshmark", "depop"))
    row = db.listing(other, "poshmark")
    assert photocheck._gone(db, row, "poshmark", "its own data: its listing page answers 404", False) is False
    _age(db, f"{other}|poshmark", 11)
    assert photocheck._gone(db, row, "poshmark", "its own data: its listing page answers 404", False) is True
    assert api_calls == [] and db.listing(other, "depop")["status"] == "posted"
    assert said[-1].startswith("Poshmark no longer has Brand Item Number 2 size M") and said[-1].endswith(
        "Still live on Depop.")
    assert photocheck.gone_line(db, other, "poshmark", "Poshmark shows it not_for_sale").startswith(
        "Poshmark shows Brand Item Number 2 size M as Not for Sale (set on Poshmark) — nothing else was taken down.")
    # a sale: thrift-api told at once — its take-downs everywhere else
    third = _item(db, 3, sites=("poshmark", "depop"))
    assert photocheck._gone(db, db.listing(third, "depop"), "depop", "Depop shows it sold", True, "40") is True
    assert [b["kind"] for _, b in api_calls] == ["sold_found"] and db.listing(third, "depop")["status"] == "sold"


def test_a_vinted_listing_closed_without_a_sale_is_no_sale(env):
    """The wardrobe's item_closing_action: closed by a sale → the sale net; closed otherwise → gone (that row only)."""
    s, db, said, group, api_calls = env
    a, b = _item(db, 1, sites=("vinted",)), _item(db, 2, sites=("vinted",))
    vinted = Shop("vinted", [{"id": _key(db, a, "vinted"), "is_closed": True, "closing": "sold", "price": "40"},
                             {"id": _key(db, b, "vinted"), "is_closed": True, "closing": "deleted", "price": "40"}])
    asyncio.run(photocheck.run(s, db, {"vinted": vinted}))
    assert db.listing(a, "vinted")["status"] == "sold" and [x["kind"] for _, x in api_calls] == ["sold_found"]
    assert db.listing(b, "vinted")["status"] == "posted"                               # gone: seen once, remembered
    assert events(db, "listing_gone_seen")[-1]["why"] == "its own data: closed on Vinted (deleted)"


def test_a_listing_gone_from_one_site_keeps_the_items_price_drops_on_the_others(env):
    s, db, said, group, api_calls = env
    iid = _item(db, 1, sites=("poshmark", "depop", "vinted"))
    it = db.item(iid)
    assert pricedrop.why_not(db, it) is None
    sales.listing_gone(db, iid, "vinted", "its own data: Vinted's item data answers 404", False, "u")
    assert pricedrop.why_not(db, db.item(iid)) is None                                  # still for sale elsewhere
    db.upsert_listing(iid, "depop", status="delisted", error=None)                      # a take-down (hidden, ours)
    assert pricedrop.why_not(db, db.item(iid)) == "sold or taken down on a site"


def test_the_one_recheck_reads_every_old_flag_by_its_own_data(env):
    """`thrift photocheck --recheck-not-in-bulk --since 2026-11-01`: the listings flagged since then and still posted —
    the Tory Burch loafers on Vinted (gone: 404) and on Depop (live: cleared) — read by the running poster's next photo
    check; ONE private line with what it found; the 404 recorded once a second read 10 minutes later agrees, with its
    line ("Still live on Poshmark and Depop"). A dry run reads and records nothing."""
    s, db, said, group, api_calls = env
    tory = _item(db, 7, sites=("poshmark", "depop", "vinted"), title="Tory Burch New Croc-Embossed Block Heel Loafers")
    other = _item(db, 8, sites=("depop",))
    db.conn.execute("UPDATE listings SET photos_read_at=? WHERE marketplace='poshmark'",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"),))            # Poshmark read just now
    for iid, mp, ts in ((tory, "depop", "2026-11-03T19:41:00+00:00"), (tory, "vinted", "2026-11-09T12:05:47+00:00"),
                        (other, "depop", "2026-10-28T14:29:51+00:00")):                  # before --since: left out
        db.conn.execute("INSERT INTO events VALUES (?,?,?,?)", (ts, iid, "photocheck_not_in_bulk",
                                                              json.dumps({"site": mp, "key": _key(db, iid, mp)})))
    depop = Shop("depop", [_record(db, tory, "depop"), _record(db, other, "depop")],
                 {_key(db, tory, "depop"): {"state": "live", "http": 200, "how": "its Depop page", "photos": 3}})
    vinted = Shop("vinted", [], {_key(db, tory, "vinted"): {"state": "gone", "http": 404, "how": "Vinted's item data"}})
    ps = {"depop": depop, "vinted": vinted}
    db.kv_set(photocheck.BULK_AT, json.dumps({mp: datetime.now(timezone.utc).isoformat() for mp in ps}))   # read
    db.conn.execute("UPDATE listings SET photos_read_at=?", (datetime.now(timezone.utc).isoformat(),))   # just now

    assert photocheck.ask_recheck(db, "2026-11-01", dry=True) == {"depop": 1, "vinted": 1}
    asyncio.run(photocheck.run(s, db, ps))
    assert said[-1].startswith("🔎 Re-check of the listings flagged 'not in bulk' since 2026-11-01: 2 read by their own "
                               "data") and said[-1].endswith("(dry run: nothing recorded).")
    assert loads(db.kv_get(photocheck.ABSENT)) in (None, {}) and loads(db.kv_get(photocheck.GONE_SEEN)) in (None, {})

    assert photocheck.ask_recheck(db, "2026-11-01") == {"depop": 1, "vinted": 1}
    asyncio.run(photocheck.run(s, db, ps))
    assert depop.asked[-1][0] == [_key(db, tory, "depop")] and vinted.asked[-1][0] == [_key(db, tory, "vinted")]
    line = said[-1]
    assert "2 read by their own data" in line and "1 live (flag cleared)" in line and "1 gone" in line
    assert "Tory Burch New Croc-Embossed Block Heel (Vinted)" in line
    assert db.listing(tory, "vinted")["status"] == "posted"                            # one 404: not yet
    assert db.kv_get(photocheck.RECHECK) == ""                                          # served once
    _age(db, f"{tory}|vinted", 11)
    asyncio.run(photocheck.run(s, db, ps))
    assert db.listing(tory, "vinted")["status"] == "delisted"
    assert [db.listing(tory, mp)["status"] for mp in ("poshmark", "depop")] == ["posted", "posted"]
    assert said[-1] == ("Vinted no longer has Tory Burch New Croc-Embossed Block Heel (deleted by you, or removed by "
                        "Vinted — check the Vinted app's notifications). Still live on Poshmark and Depop.")
    assert api_calls == []


def test_the_recheck_command_asks_the_running_poster_and_an_unservable_request_ends_in_a_day(env, monkeypatch):
    from typer.testing import CliRunner
    s, db, said, group, api_calls = env
    monkeypatch.setattr(cli, "settings", lambda: s)
    iid = _item(db, 9, sites=("vinted",))
    db.conn.execute("INSERT INTO events VALUES (?,?,?,?)", ("2026-11-04T17:07:35+00:00", iid, "photocheck_not_in_bulk",
                                                          json.dumps({"site": "vinted", "key": _key(db, iid, "vinted")})))
    r = CliRunner().invoke(cli.app, ["photocheck", "--recheck-not-in-bulk", "--since", "2026-11-01"])
    assert r.exit_code == 0, r.output
    assert "asked the running poster to read 1 listing(s) by their own data (vinted 1)" in r.output
    assert CliRunner().invoke(cli.app, ["photocheck", "--recheck-not-in-bulk", "--since", "Nov 1"]).exit_code != 0
    req = loads(db.kv_get(photocheck.RECHECK))
    assert req["rows"] == [[iid, "vinted"]] and req["dry"] is False
    db.kv_set(photocheck.RECHECK, json.dumps({**req, "asked_at": "2026-11-01T00:00:00+00:00"}))   # never served
    lines = asyncio.run(photocheck.serve_recheck(s, db, {}, datetime.now(timezone.utc)))
    assert lines and "couldn't read 1 of them within a day" in lines[0] and db.kv_get(photocheck.RECHECK) == ""


def test_read_shop_asks_the_running_poster_for_one_bulk_read_now_whatever_its_six_hour_clock_says(env, monkeypatch):
    """WO56b (live 2026-11-10: the Depop bulk read had been fixed; the next one by its clock was 6 hours away): `thrift
    photocheck --read-shop depop` — the Depop shop read in bulk at the poster's next photo check although its last bulk
    read was minutes ago and none of its listings is due; once (the run after reads nothing more); Vinted's clock
    untouched; a site the poster can't read now keeps the request for the next run."""
    from typer.testing import CliRunner
    s, db, said, group, api_calls = env
    monkeypatch.setattr(cli, "settings", lambda: s)
    items = [_item(db, n) for n in range(3)]
    depop = Shop("depop", [_record(db, i, "depop") for i in items])
    vinted = Shop("vinted", [_record(db, i, "vinted") for i in items])
    asyncio.run(photocheck.run(s, db, {"depop": depop, "vinted": vinted}))   # the regular reads: both shops
    assert [e["site"] for e in events(db, "photocheck_bulk")] == ["depop", "vinted"]
    asyncio.run(photocheck.run(s, db, {"depop": depop, "vinted": vinted}))   # minutes later: nothing due, no read
    assert len(events(db, "photocheck_bulk")) == 2
    r = CliRunner().invoke(cli.app, ["photocheck", "--read-shop", "Depop"])
    assert r.exit_code == 0, r.output
    assert "asked the running poster to read the Depop shop in bulk" in r.output
    assert CliRunner().invoke(cli.app, ["photocheck", "--read-shop", "poshmark"]).exit_code != 0
    assert CliRunner().invoke(cli.app, ["photocheck", "--read-shop", "depop", "--recheck-not-in-bulk"]).exit_code != 0
    assert CliRunner().invoke(cli.app, ["photocheck"]).exit_code != 0
    away = Shop("depop", [])
    away.available = lambda: False                                              # the Thrift Chrome away: kept
    asyncio.run(photocheck.run(s, db, {"depop": away, "vinted": vinted}))
    assert len(events(db, "photocheck_bulk")) == 2 and "depop" in loads(db.kv_get(photocheck.BULK_NOW))
    asyncio.run(photocheck.run(s, db, {"depop": depop, "vinted": vinted}))
    assert [e["site"] for e in events(db, "photocheck_bulk")] == ["depop", "vinted", "depop"]
    assert events(db, "photocheck_bulk")[-1]["complete"] is True
    assert loads(db.kv_get(photocheck.BULK_NOW)) == {}
    asyncio.run(photocheck.run(s, db, {"depop": depop, "vinted": vinted}))   # once: nothing more
    assert len(events(db, "photocheck_bulk")) == 3
    assert events(db, "photocheck_bulk_asked") == [{"site": "depop"}]
    assert events(db, "photocheck_not_in_bulk") == [] and said == [] and api_calls == []


# ======================================================================== §6 the Shortcut's new order

SID = "2026-11-09_194212_3141"                       # shared at 7:42 PM
AGAIN = "2026-11-09_195003_2718"                     # shared again at 7:50 PM


def jpeg(n: int) -> bytes:
    """A photo as the iPhone sends it: its own capture time (10 s apart) and pixels (no two alike — no burst)."""
    rnd = random.Random(n)
    im = Image.new("RGB", (48, 64))
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(48 * 64)])
    ex = Image.Exif()
    ex[photomem.MODEL] = "iPhone 16 Pro Max"
    ifd = ex.get_ifd(photomem.EXIF_IFD)
    ifd[photomem.DATE_ORIGINAL] = f"2026:11:09 19:{40 + n // 6:02d}:{(n % 6) * 10:02d}"
    ifd[photomem.SUBSEC_ORIGINAL] = str(100 + n)
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=ex.tobytes())
    return buf.getvalue()


PHOTOS = [(f"IMG_{4100 + n}.JPG", jpeg(n)) for n in range(12)]


class Azure:
    """thrift-api's /shares/pending as the Mac sees it (a fetched share is no longer listed)."""
    url, key, client, timeout = "https://api.test", "mac-key", None, 15.0

    def __init__(self):
        self.rows, self.posts = {}, []

    def upload(self, sid, photos, count, ended, started, last):
        entries = [{"name": name, "n": k, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                    "content_type": "image/jpeg", "received_at": last.isoformat(), "path": f"/shares/{sid}/photo/{name}",
                    "_data": data} for k, (name, data) in enumerate(photos, 1)]
        self.rows[sid] = {"share_id": sid, "count": count, "received": len(photos), "ended": ended, "test": False,
                          "started_at": started.isoformat(), "ended_at": last.isoformat() if ended else None,
                          "last_received_at": last.isoformat(), "photos": entries}

    def get(self, path, params=None):
        assert path == "/shares/pending"
        gone = {p.split("/")[2] for p, _ in self.posts if p.endswith("/fetched")}
        return {"shares": [r for sid, r in self.rows.items() if sid not in gone]}

    def post(self, path, body):
        self.posts.append((path, body))
        return {"ok": True}

    def get_bytes(self, path):
        sid, name = path.split("/")[2], path.rsplit("/", 1)[-1]
        return next(p["_data"] for p in self.rows[sid]["photos"] if p["name"] == name), "image/jpeg"


def test_a_share_uploaded_whole_with_no_icloud_copy_is_processed_at_once_with_no_wait_and_no_line(env):
    s, db, said, group, api_calls = env
    now = datetime.now(timezone.utc)
    api = Azure()
    api.upload(SID, PHOTOS, 12, ended=True, started=now - timedelta(minutes=2), last=now - timedelta(seconds=20))
    assert uploads.fetch_new(s, db, api, now) == [SID]                     # whole: at once, no quiet wait
    assert cli._scan_inbox(s, db) == 1                                     # the same look takes it
    [b] = db.batches("new")
    assert b["n_photos"] == 12 and b["src_dir"] == str(pipeline.uploads_dir(s) / SID)
    assert uploads.held_dir(s).is_dir() is False or list(uploads.held_dir(s).iterdir()) == []
    assert cli._scan_inbox(s, db) == 0
    assert group == [] and said == [] and db.kv_get(pipeline.WAITING_SAID) in (None, "{}")   # no 📭, nothing said
    assert not any("iCloud" in str(e) for e in events(db, "share_fetched"))


def _stopped(api, now):
    """The Shortcut stopped after 7 of 12 photos (no end call): its first photo 16 minutes ago, its last 6."""
    api.upload(SID, PHOTOS[:7], 12, ended=False, started=now - timedelta(minutes=16), last=now - timedelta(minutes=6))
    return now - timedelta(minutes=6)


def test_a_share_that_stopped_at_7_of_12_with_no_icloud_copy_is_held_and_one_line_asks_to_share_again(env):
    s, db, said, group, api_calls = env
    now = datetime.now(timezone.utc)
    api = Azure()
    last = _stopped(api, now)
    assert uploads.fetch_new(s, db, api, now) == [SID]                     # its moment (10 min old, 5 quiet): held
    held = uploads.held_copy(s, SID)
    assert held is not None and len([p for p in held.iterdir() if p.suffix == ".JPG"]) == 7
    assert api.posts == [(f"/shares/{SID}/fetched", {"names": [n for n, _ in PHOTOS[:7]]})]   # Azure's copy goes
    assert cli._scan_inbox(s, db) == 0 and db.conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
    assert uploads.held_tick(s, db, now=last + timedelta(minutes=14, seconds=59), force=True) == []
    lines = uploads.held_tick(s, db, now=last + timedelta(minutes=15), force=True)
    assert lines == group == ["📸 The photos shared at 7:42 PM didn't finish uploading — please share them again."]
    for minutes in (16, 60, 600):
        assert uploads.held_tick(s, db, now=last + timedelta(minutes=minutes), force=True) == []
    assert len(group) == 1 and said == []
    row = db.conn.execute("SELECT * FROM shares WHERE id=?", (SID,)).fetchone()
    assert row["outcome"] == "held" and row["reason"].startswith("7 of 12 photos came") and row["complete_at"] is None
    assert cli._scan_inbox(s, db) == 0                                     # never posted with 7: no batch, no item
    assert db.conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0     # never in the photo memory
    assert uploads.held_tick(s, db, now=last + timedelta(days=8), force=True) == [] and uploads.held_copy(s, SID) is None
    assert db.conn.execute("SELECT outcome FROM shares WHERE id=?", (SID,)).fetchone()["outcome"] == "held"   # a week
    #                                                                        on: the folder goes, the record stays


def test_shared_again_the_new_share_replaces_the_held_one_one_item_twelve_photos_no_duplicate(env, monkeypatch):
    from thrift_agent.ingest.segment import Group, SegOut
    s, db, said, group, api_calls = env
    now = datetime.now(timezone.utc)
    api = Azure()
    last = _stopped(api, now)
    uploads.fetch_new(s, db, api, now)
    uploads.held_tick(s, db, now=last + timedelta(minutes=15), force=True)        # the line went out
    again = last + timedelta(minutes=17)
    api.upload(AGAIN, PHOTOS, 12, ended=True, started=again - timedelta(minutes=1), last=again)
    assert uploads.fetch_new(s, db, api, again) == [AGAIN]                  # whole: taken at once
    assert uploads.held_tick(s, db, now=again, force=True) == []
    assert uploads.held_copy(s, SID) is None                               # dropped: the new share replaces it
    old = db.conn.execute("SELECT * FROM shares WHERE id=?", (SID,)).fetchone()
    assert old["outcome"] == "replaced" and "shared again at 7:50 PM" in old["reason"]
    assert events(db, "share_replaced") == [{"share_id": SID, "outcome": "replaced", "why": "shared again at 7:50 PM"}]
    assert cli._scan_inbox(s, db) == 1
    [b] = db.batches("new")
    assert b["n_photos"] == 12

    def segment(photos, *a, **k):
        return SegOut(groups=[Group(photos=list(range(len(photos))), summary="loafers", full_item_photos=[0],
                                    confidence=0.95)])
    monkeypatch.setattr(pipeline.seg, "segment", segment)
    pipeline.process_batch(s, db, b["id"])
    [it] = db.conn.execute("SELECT * FROM items WHERE batch_id=?", (b["id"],)).fetchall()
    assert db.batch(b["id"])["status"] == "split" and db.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert len([p for p in (pipeline.Path(it["dir"]) / "photos").iterdir() if p.suffix == ".jpg"]) == 12
    assert db.conn.execute("SELECT COUNT(*) FROM photos WHERE item_id=?", (it["id"],)).fetchone()[0] == 12
    assert events(db, "batch_repeat") == [] and events(db, "photos_known") == []         # nothing skipped as a repeat
    assert group == ["📸 The photos shared at 7:42 PM didn't finish uploading — please share them again."]   # once


def test_shared_again_before_the_15_minutes_no_line_at_all(env):
    """She saw the phone's own "Only 7 of 12 photos went through" and shared again at once: the held share goes before
    its line is due — nothing is ever said about it."""
    s, db, said, group, api_calls = env
    now = datetime.now(timezone.utc)
    api = Azure()
    last = _stopped(api, now)
    api.upload(AGAIN, PHOTOS[:9], 12, ended=False, started=now - timedelta(minutes=1), last=now)   # still uploading
    assert uploads.fetch_new(s, db, api, now) == [SID]                     # held; the new one not takeable yet
    assert uploads.held_copy(s, SID) is not None
    assert uploads.held_tick(s, db, now=now, force=True) == []
    assert uploads.held_copy(s, SID) is None                               # its photos seen in the newer share
    assert db.conn.execute("SELECT outcome FROM shares WHERE id=?", (SID,)).fetchone()["outcome"] == "replaced"
    assert uploads.held_tick(s, db, now=last + timedelta(minutes=20), force=True) == [] and group == []


def test_a_held_share_known_to_the_photo_memory_is_replaced_or_a_repeat(env):
    """Shared again with no internet (the new share an iCloud copy only): it isn't in Azure's listings — once its item
    is made, the photo memory knows the held photos: `replaced`. Photos that had their item BEFORE the held share
    (a second run of the Shortcut on photos listed already): `repeat`. Either way nothing is said."""
    s, db, said, group, api_calls = env
    now = datetime.now(timezone.utc)
    api = Azure()
    last = _stopped(api, now)
    uploads.fetch_new(s, db, api, now)
    folder = uploads.held_copy(s, SID)
    photo = folder / PHOTOS[3][0]
    photomem.remember(db, [(photo, photomem.keys_of(photo))], "i_261109_aaaaaa", "b_x")   # made into an item now
    assert uploads.held_tick(s, db, now=last + timedelta(minutes=5), force=True) == []
    row = db.conn.execute("SELECT outcome, reason FROM shares WHERE id=?", (SID,)).fetchone()
    assert row["outcome"] == "replaced" and uploads.held_copy(s, SID) is None
    other = "2026-11-09_203000_5555"
    api.upload(other, PHOTOS[:4], 12, ended=True, started=now - timedelta(minutes=3), last=now - timedelta(minutes=2))
    db.conn.execute("UPDATE photos SET seen_at='2026-11-01T00:00:00+00:00'")              # known since Nov 1
    uploads.fetch_new(s, db, api, now)
    assert uploads.held_copy(s, other) is not None
    assert uploads.held_tick(s, db, now=now + timedelta(minutes=20), force=True) == []
    assert db.conn.execute("SELECT outcome FROM shares WHERE id=?", (other,)).fetchone()["outcome"] == "repeat"
    assert group == [] and said == []


def test_the_icloud_copy_still_decides_when_there_is_one(env, monkeypatch):
    """The no-internet branch / the old Shortcut: an iCloud copy in the inbox — a short upload beside it is NOT held
    (`choose`'s rules, as before WO56); a held share whose iCloud copy turns up gets no line while that copy waits,
    no 📭 "no need to share again" beside its own line, and once the copy is taken the held copy goes."""
    s, db, said, group, api_calls = env
    now = datetime.now(timezone.utc)
    api = Azure()
    api.upload(SID, PHOTOS[:7], 12, ended=True, started=now - timedelta(minutes=5), last=now - timedelta(minutes=3))
    folder = s.path("inbox") / SID
    folder.mkdir()
    (folder / "IMG_4100.JPG").write_bytes(PHOTOS[0][1])                    # the iCloud copy, still coming in
    assert uploads.fetch_new(s, db, api, now) == [SID]
    assert uploads.held_copy(s, SID) is None                               # the upload had more in hand: taken (WO54)

    s2 = "2026-11-09_201500_1111"
    api.upload(s2, PHOTOS[:5], 12, ended=True, started=now - timedelta(minutes=5), last=now - timedelta(minutes=3))
    assert uploads.fetch_new(s, db, api, now) == [s2] and uploads.held_copy(s, s2) is not None   # no copy: held
    late = s.path("inbox") / s2
    late.mkdir()                                                            # its iCloud copy turns up, empty
    (late / "_done.txt").write_text("done 12")
    assert uploads.held_tick(s, db, now=now + timedelta(minutes=30), force=True) == []   # that copy's rules decide
    assert uploads.fetch_new(s, db, api, now) == [] and not uploads.holds(db, s2)   # Azure has nothing of it now
    monkeypatch.setattr(pipeline, "appeared", lambda p: (now - timedelta(minutes=31)).timestamp())
    monkeypatch.setattr(pipeline, "arrived", lambda p: (now - timedelta(minutes=31)).timestamp())
    assert pipeline.take_share(s, db, late) is None and group == []        # no 📭 beside a held share
    for name, data in PHOTOS:                                              # iCloud brings all 12: the share
        (late / name).write_bytes(data)
    bid = pipeline.register(s, db, late)
    assert bid is not None
    assert uploads.held_tick(s, db, now=now + timedelta(minutes=32), force=True) == []
    assert uploads.held_copy(s, s2) is None and events(db, "share_held_dropped")[-1]["batch"] == bid


def test_poshmarks_own_not_for_sale_still_takes_the_item_down_elsewhere(env):
    """The lead's review of WO56: her Not for Sale on Poshmark is HER signal (sold off the sites, or put away) — WO49's
    rule stays: seen twice, thrift-api is told `not_for_sale` and the other sites come down. Only a 404 / a removal /
    a close without a sale is that row alone (above)."""
    s, db, said, group, api_calls = env
    iid = _item(db, 4, sites=("poshmark", "depop", "vinted"))
    row = db.listing(iid, "poshmark")
    assert photocheck._gone(db, row, "poshmark", "Poshmark shows it not_for_sale", False, not_for_sale=True) is False
    _age(db, f"{iid}|poshmark", 11)
    assert photocheck._gone(db, row, "poshmark", "Poshmark shows it not_for_sale", False, not_for_sale=True) is True
    assert [b["kind"] for _, b in api_calls] == ["not_for_sale"]
    assert db.listing(iid, "poshmark")["status"] == "delisted"
