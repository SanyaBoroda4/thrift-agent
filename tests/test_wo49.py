"""WO49 on the Mac (2026-10-24): one iCloud folder stuck in "Resource deadlock avoided" never holds the others or an
upload (uploads land on the Mac's own disk), the take-downs' "gone already" keeps the listing rows true, Depop's
delete judged by the address, a listing a site shows sold tells thrift-api (the sale net that needs no email), the
Poshmark sweep reads every sales answer, the heartbeat carries progress, the cleanup line waits for 9 PM, and
`thrift doctor` / "doctor"."""
from __future__ import annotations

import asyncio
import errno
import json

import pytest
from PIL import Image

from thrift_agent import alerts, cleanup, cli, doctor, orders, photocheck, pipeline, sales
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB
from thrift_agent.ingest import prep
from thrift_agent.post.ext_driver import ExtensionPoster

T0 = 1_800_000_000.0
MIN = 60


@pytest.fixture
def env(tmp_path, monkeypatch):
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    s = Settings(data)
    s.ensure_dirs()
    db = DB(s.path("db"))
    clock = {"now": T0, "born": {}}
    monkeypatch.setattr(pipeline.time, "time", lambda: clock["now"])
    monkeypatch.setattr(pipeline, "appeared", lambda p: clock["born"].get(str(p), T0))
    monkeypatch.setattr(pipeline, "arrived", lambda p: clock["born"].get(str(p), T0))
    said, group = [], []
    monkeypatch.setattr(pipeline.notify, "say", said.append)
    monkeypatch.setattr(pipeline.notify, "group", group.append)
    monkeypatch.setattr(cli.uploads, "due", lambda db_: False)            # no API in the tests
    return s, db, clock, said, group


def share(s, clock, name, photos=2, marker="done 2", at=T0, base=None, source=None):
    d = (base or s.path("inbox")) / name
    d.mkdir(parents=True)
    for i in range(photos):
        Image.new("RGB", (30, 40), "red").save(d / f"IMG_{i + 1}.jpeg")
    if marker is not None:
        (d / "_done.txt").write_text(marker, encoding="utf-8")
    if source:
        (d / "_source.txt").write_text(source, encoding="utf-8")
    for f in [d, *d.iterdir()]:
        clock["born"][str(f)] = at
    return d


def test_one_stuck_icloud_folder_never_holds_the_others_or_an_upload(env, monkeypatch):
    """The live miss: the 9:39 share's iCloud folder in EDEADLK made every look fail for 71 minutes — the other
    share, whole on the Mac, and the 20-photo upload waited behind it."""
    s, db, clock, said, group = env
    ready = share(s, clock, "2026-10-24_093754_9993")
    stuck = share(s, clock, "2026-10-24_093927_7777")
    upload = share(s, clock, "2026-10-24_094500_1234", base=pipeline.uploads_dir(s), source="azure", at=T0 + 9 * MIN)
    real = prep.icloud_placeholders

    def placeholders(d):
        if d.name == stuck.name:
            raise OSError(errno.EDEADLK, "Resource deadlock avoided", str(d))
        return real(d)
    monkeypatch.setattr(prep, "icloud_placeholders", placeholders)
    clock["now"] = T0 + 10 * MIN
    assert cli._scan_inbox(s, db) == 2
    srcs = sorted(b["src_dir"] for b in db.batches("new"))
    assert srcs == sorted([str(ready), str(upload)])                    # both taken, the upload at once (no iCloud wait)
    assert db.kv_get(alerts.SCAN_DONE)                                   # the look FINISHED: the watcher's progress
    busy = json.loads(db.kv_get(alerts.BUSY_SINCE))
    assert f"share {stuck.name}" in busy                                 # the stuck one: quiet, retried next look
    monkeypatch.setattr(prep, "icloud_placeholders", real)               # iCloud lets go
    assert cli._scan_inbox(s, db) == 1
    assert len(db.batches("new")) == 3


def test_the_inbox_itself_busy_still_takes_the_uploads(env, monkeypatch):
    s, db, clock, said, group = env
    upload = share(s, clock, "2026-10-24_094500_1234", base=pipeline.uploads_dir(s), source="azure")

    def busy(*a, **k):
        raise OSError(errno.EDEADLK, "Resource deadlock avoided")
    monkeypatch.setattr(pipeline, "ready_folders", busy)
    assert cli._scan_inbox(s, db) == 1
    assert [b["src_dir"] for b in db.batches("new")] == [str(upload)]
    assert db.kv_get(alerts.SCAN_DONE) is None and db.kv_get(alerts.WATCH_BEAT)    # alive, not "finished"


def test_an_uploads_folder_is_archived_on_the_macs_own_disk(env):
    s, db, clock, said, group = env
    upload = share(s, clock, "2026-10-24_094500_1234", base=pipeline.uploads_dir(s), source="azure")
    bid = pipeline.register(s, db, upload)
    dest = pipeline.archive_share(s, db, bid, upload)
    assert dest is not None and dest.parent == pipeline.uploads_dir(s) / pipeline.UPLOADS_ARCHIVE
    assert not upload.exists() and pipeline.upload_folders(s) == []
    assert not list(s.path("archive").iterdir())                         # never into iCloud's archive


def test_the_worker_reports_progress_and_the_lid_closed_is_no_stall(env, monkeypatch):
    s, db, clock, said, group = env
    monkeypatch.setattr(cli, "_safe_tick", lambda s_, db_: 0)
    monkeypatch.setattr(cli, "_safe_pump", lambda s_, db_: None)
    monkeypatch.setattr(cli, "_safe_sales", lambda db_, force_beat=False: None)
    monkeypatch.setattr(cli.time, "sleep", lambda x: None)

    class Window:
        lid_closed = True

        def step(self):
            return None

        def update(self):
            return None
    cli._worker_iteration(s, db, 15, Window())
    assert json.loads(db.kv_get(alerts.PROGRESS))["what"] == "lid closed"


def test_the_heartbeat_carries_progress_and_the_cleanup(env):
    s, db, clock, said, group = env
    alerts.scan_done(db)
    db.kv_set(cleanup.LAST, json.dumps({"at": "2026-10-24T12:57:41+00:00", "freed": 1, "dry": False}))
    info = cli.beat_info(db)
    assert info["inbox_look"] and info["progress"]["what"] == "inbox look" and info["cleanup"]["freed"] == 1


def test_the_daily_cleanup_sends_no_line_of_its_own(env, monkeypatch):
    s, db, clock, said, group = env
    seen = []
    monkeypatch.setattr(cleanup, "ENABLED", True)
    monkeypatch.setattr(Settings, "is_prod", property(lambda self: True))
    monkeypatch.setattr(cleanup, "disk_watch", lambda s_, db_: None)
    monkeypatch.setattr(cleanup, "due", lambda db_: True)
    monkeypatch.setattr(cleanup, "run", lambda s_, db_, say=True: seen.append(say))
    cleanup.tick(s, db)
    assert seen == [False]                                               # the 9 PM summary says it (thrift-api)


# ---------------------------------------------------------------- take-downs and the sale net

def _listing(db, iid, site, status="posted", url="https://example.test/x"):
    db.conn.execute("INSERT OR IGNORE INTO items (id, batch_id, seq, status, dir, created_at, updated_at) "
                    "VALUES (?, 'b_1', 0, 'posted', '/tmp', '2026-10-21', '2026-10-21')", (iid,))
    db.upsert_listing(iid, site, status=status, url=url)


def _takedown(db, tid, iid, site, status, attempts, url="u"):
    db.conn.execute("INSERT INTO takedowns (id, item_id, marketplace, listing_url, title, status, attempts, fetched_at) "
                    "VALUES (?, ?, ?, ?, 'x', ?, ?, '2026-10-24T14:50:00+00:00')", (tid, iid, site, url, status, attempts))


def test_a_take_down_that_finds_the_listing_gone_delists_the_row(env, monkeypatch):
    s, db, clock, said, group = env
    _listing(db, "i_261021_382586", "depop")
    _takedown(db, "t_1", "i_261021_382586", "depop", "pending", 0, "https://example.test/x")
    reported = []
    monkeypatch.setattr(sales, "report", lambda db_, tid, result, **k: reported.append((tid, result)))
    monkeypatch.setattr(sales, "skip_sold", lambda db_, iid: None)

    class Gone:
        shot = None

        async def delist(self, url):
            return None
    assert asyncio.run(sales.run_takedown("depop", Gone(), db, None)) is True
    assert reported == [("t_1", "not_found")]
    assert db.listings_for("i_261021_382586")[0]["status"] == "delisted"


def test_rows_a_gone_take_down_left_posted_are_repaired_once(env):
    s, db, clock, said, group = env
    _listing(db, "i_261012_c49967", "depop")
    _takedown(db, "t_2", "i_261012_c49967", "depop", "not_found", 1)
    assert sales.repair_gone(db) == [("i_261012_c49967", "depop")]
    assert sales.repair_gone(db) == []


def test_a_depop_delete_whose_page_reloads_is_judged_by_the_address(monkeypatch):
    p = ExtensionPoster("depop")
    states = iter(["live", "gone"])

    async def state(address):
        return next(states)

    async def job(mode, payload, timeout, shot=None):
        return {"event": "error", "message": "the page reloaded during the take-down", "page": "unknown"}
    monkeypatch.setattr(p, "_depop_state", state)
    monkeypatch.setattr(p, "_job", job)
    monkeypatch.setattr("thrift_agent.post.ext_driver.unverified", lambda *a, **k: set())
    assert asyncio.run(p.delist("https://www.depop.com/products/tktianasorokina-farm-rio-zodiac-print-maxi-5281/")) \
        is True
    assert any("is gone" in n for n in p.notes)


def test_a_listing_a_site_shows_sold_tells_the_api_unless_the_take_down_is_ours(env, monkeypatch):
    s, db, clock, said, group = env
    _listing(db, "i_261021_382586", "poshmark", url="https://poshmark.com/listing/FARM-Rio-6ad952fa64399b4c7bed7e55")
    calls = []
    monkeypatch.setattr(sales, "call", lambda db_, path, body: calls.append((path, body)) or True)
    assert sales.listing_gone(db, "i_261021_382586", "poshmark", "Poshmark shows it sold_out", True,
                              "https://poshmark.com/listing/FARM-Rio-6ad952fa64399b4c7bed7e55", "FARM Rio", 145) == "sold"
    assert calls[0][0] == "/mac-event" and calls[0][1]["kind"] == "sold_found" and calls[0][1]["marketplace"] == "poshmark"
    assert db.listings_for("i_261021_382586")[0]["status"] == "sold"
    _listing(db, "i_261020_3a8891", "vinted")
    _takedown(db, "t_3", "i_261020_3a8891", "vinted", "done", 0)
    assert sales.listing_gone(db, "i_261020_3a8891", "vinted", "hidden", False, "u") == "ours"
    assert len(calls) == 1


def test_the_photo_check_reads_poshmark_every_two_hours_and_reports_a_sold_one(env, monkeypatch):
    s, db, clock, said, group = env
    assert photocheck.POSHMARK_EVERY == 2 * 3600
    _listing(db, "i_261021_382586", "poshmark", url="https://poshmark.com/listing/FARM-Rio-6ad952fa64399b4c7bed7e55")
    db.conn.execute("UPDATE listings SET posted_at='2026-10-22T00:04:57+00:00'")
    monkeypatch.setattr(photocheck.crosslist, "poshmark_listing",
                        lambda url: {"for_sale": False, "status": "sold_out", "sold": True, "photos": 9, "price": 145})
    gone = []
    monkeypatch.setattr(sales, "listing_gone", lambda db_, iid, mp, why, sold, url, title=None, price=None:
                        gone.append((iid, mp, sold)) or "sold")
    asyncio.run(photocheck.run(s, db, {}))
    assert gone == [("i_261021_382586", "poshmark", True)]


# ---------------------------------------------------------------- the Poshmark sweep

def test_the_poshmark_sweep_reads_every_sales_answer(monkeypatch):
    """The live miss: the sweep kept the FIRST /orders/sales answer — a list of shipped orders only — so an order
    awaiting shipment (every fresh sale) was never read. Every answer is read now, rows merged by order id."""
    monkeypatch.setattr(orders, "MORE_ANSWERS", 0.01)

    def row(oid, status):
        return {"id": oid, "title": "FARM Rio dress", "display_status": status, "inventory_booked_at": None,
                "total_price_amount": {"val": "145.00"}}

    class Resp:
        def __init__(self, rows):
            self.url = "https://poshmark.com/vm-rest/users/x/orders/sales?request=1"
            self.headers = {"content-type": "application/json"}
            self.rows = rows

        async def json(self):
            return {"data": {"sales_summary": self.rows}}

    class Page:
        url = "https://poshmark.com/seller-hub/sales"

        def on(self, name, fn):
            self.fn = fn

        async def goto(self, url, wait_until=None):
            self.fn(Resp([row("6ad3810105255d89ee529f4b", "In Transit")]))
            self.fn(Resp([row("6ad952fa64399b4c7bed7e55", "Awaiting Shipment"),
                          row("6ad3810105255d89ee529f4b", "In Transit")]))

        async def evaluate(self, *a):
            raise RuntimeError("no detail page in the test")

        async def close(self):
            pass

    class Ctx:
        async def new_page(self):
            return Page()
    got = asyncio.run(orders.read_poshmark(Ctx()))
    assert sorted((o["order_id"], o["status"]) for o in got) == [("6ad3810105255d89ee529f4b", "shipped"),
                                                                ("6ad952fa64399b4c7bed7e55", "to_ship")]


# ---------------------------------------------------------------- thrift doctor

class FakeApi:
    def get(self, path):
        return {"shares": [{"share_id": "2026-10-24_093927_7777", "test": False}]}


def test_the_doctor_says_everything_on_one_screen(env):
    s, db, clock, said, group = env
    alerts.scan_done(db)
    share(s, clock, "2026-10-24_093927_7777")
    lines = doctor.report(s, db, api=FakeApi())
    joined = "\n".join(lines)
    assert lines[0].startswith("🩺 Thrift doctor")
    assert "Worker: last progress" in joined and "Poster: NOT running" in joined
    assert "iCloud inbox 1 folder(s)" in joined and "Azure not fetched 1 (2026-10-24_093927_7777)" in joined
    for site in ("Poshmark", "Depop", "Vinted"):
        assert f"\n{site}:" in joined
    assert "Take-downs open: none" in joined and "Today's shares" in joined
    assert len(lines) < 40


def test_doctor_is_a_reply_in_the_private_chat(env, monkeypatch):
    s, db, clock, said, group = env
    from thrift_agent import approve
    sent = []

    class Bot:
        def send_message(self, text, reply_to=None):
            sent.append(text)
    monkeypatch.setattr(approve, "ops_bot_for", lambda s_: Bot())
    monkeypatch.setattr(doctor, "text", lambda s_, db_, api=None: "🩺 Thrift doctor — test")
    out = approve._route_ops(s, db, {"message": {"message_id": 5, "chat": {"id": 1}, "text": "doctor"}})
    assert out == "ops: doctor" and sent == ["🩺 Thrift doctor — test"]


# ---------------------------------------------------------------- the robe's "S/M", the BCBG title on Vinted

def test_a_labels_two_sizes_list_under_the_bigger_one_and_the_description_keeps_them(facts):
    """Live 2026-10-24: a Bath & Body Works robe labelled S/M, priced, skipped on all three sites (no menu has S/M)."""
    from thrift_agent.brain import copy as copywriter
    from thrift_agent.schema import Ev
    robe = facts(item_type="plush robe", category="Intimates & Sleepwear", subcategory="Robes", size_printed=Ev(),
                 size_us=Ev(value="S/M", photos=[2], source="photo", confidence=0.9))
    out = pipeline.settle_dual_size(robe)
    assert out.size_us.value == "M" and out.size_us.source == "derived" and out.size_printed.value == "S/M"
    assert copywriter.ensure_label_size("Plush robe.", out).endswith("\nLabel size: S/M.")
    assert pipeline.settle_dual_size(facts(size_us=Ev(value="M-L", source="photo", confidence=0.9))).size_us.value == "L"
    owner = facts(size_us=Ev(value="S/M", source="owner", confidence=1.0))
    assert pipeline.settle_dual_size(owner).size_us.value == "S/M"                  # the owner's size stands
    assert pipeline.settle_dual_size(facts()).size_us.value == "7.5"               # one size: nothing


def test_vinted_titles_lose_a_shouting_brand_only_when_capitals_dominate():
    """Live 2026-10-24: Vinted refused "BCBGMAXAZRIA New Black Slouchy Shoulder Bag Knotted Strap" — "Title contains
    too many capital letters"."""
    from thrift_agent.catalogs.vinted import vinted_title
    assert vinted_title("BCBGMAXAZRIA New Black Slouchy Shoulder Bag Knotted Strap") == \
        "Bcbgmaxazria New Black Slouchy Shoulder Bag Knotted Strap"
    for ok in ("UGG Tasman Slippers Chestnut size 7", "Levi's 501 Cutoff Denim Shorts size 26",
               "J. Crew 100% Merino Wide Leg Sweater Pants Blue size M"):
        assert vinted_title(ok) == ok


def test_one_odd_poshmark_answer_never_takes_anything_down(env, monkeypatch):
    """A 404 or "not for sale" acts only when seen again later (a for-sale reading between forgets it); SOLD at once."""
    s, db, clock, said, group = env
    _listing(db, "i_261021_382586", "poshmark", url="https://poshmark.com/listing/FARM-Rio-6ad952fa64399b4c7bed7e55")
    row = db.listings_for("i_261021_382586")[0]
    gone, now = [], {"t": T0}
    monkeypatch.setattr(photocheck.time, "time", lambda: now["t"])
    monkeypatch.setattr(sales, "listing_gone", lambda db_, iid, mp, why, sold, url, title=None, price=None:
                        gone.append(sold) or "x")
    photocheck._gone(db, row, "poshmark", "Poshmark shows it gone", False)
    assert gone == []                                                     # the first sighting: only remembered
    now["t"] += 5 * MIN
    photocheck._gone(db, row, "poshmark", "Poshmark shows it gone", False)
    assert gone == []                                                     # too soon
    photocheck._back(db, row, "poshmark")                                 # for sale again: forgotten
    now["t"] += 20 * MIN
    photocheck._gone(db, row, "poshmark", "Poshmark shows it gone", False)
    assert gone == []
    now["t"] += 11 * MIN
    photocheck._gone(db, row, "poshmark", "Poshmark shows it gone", False)
    assert gone == [False]                                                # seen again, 10+ minutes later
    photocheck._gone(db, row, "poshmark", "Poshmark shows it sold_out", True)
    assert gone == [False, True]                                          # sold: at once


def test_a_listing_taken_down_or_sold_is_never_posted_again(env, monkeypatch):
    from thrift_agent.post import runner
    s, db, clock, said, group = env
    monkeypatch.setattr(Settings, "is_prod", property(lambda self: True))
    monkeypatch.setattr(runner.Render, "model_validate", classmethod(lambda cls, d: d))
    s.data.setdefault("marketplaces", {}).setdefault("poshmark", {})["autopublish"] = True
    _listing(db, "i_261021_382586", "poshmark", status="queued")
    db.conn.execute("UPDATE items SET status='ready', owner_price=145, renders=? WHERE id='i_261021_382586'",
                    (json.dumps({"poshmark": {"title": "Dress", "price": 145}}),))
    db.conn.execute("DELETE FROM listings WHERE item_id='i_261021_382586'")
    assert runner.next_job(s, db, ["poshmark"], dry=False) is not None          # a real candidate with no row
    for status in ("delisted", "sold"):
        _listing(db, "i_261021_382586", "poshmark", status=status)
        assert runner.next_job(s, db, ["poshmark"], dry=False) is None, status
