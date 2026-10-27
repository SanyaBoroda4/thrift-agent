"""WO33 Part G and D on the Mac (thrift_agent/sales.py, post/takedown.py, the parallel workers' take-downs): every
change to an item or a listing reaches thrift-api, in order, after an offline spell too; the heartbeat; take-downs
fetched, done before new listings, the sold item's queued rows skipped, an unrecorded control never tried (the owner
asked instead); the group's "shipped …" reply; and nothing at all without the two .env lines. The API is a stand-in
(httpx.MockTransport): no network."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from thrift_agent import approve, crosslist, daily, notify, sales
from thrift_agent.db import DB
from thrift_agent.post import parallel

from test_crosslist_flow import _item, _settings


class FakeApi:
    """thrift-api as a stand-in: records every call; `down` makes it unreachable; `tasks` is what GET /tasks hands."""

    def __init__(self):
        self.calls, self.down, self.tasks, self.mac_events = [], False, [], []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down", request=request)
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body, dict(request.url.params)))
        assert request.headers.get("x-functions-key") == "mac-key"
        if request.url.path == "/tasks":
            return httpx.Response(200, json={"tasks": self.tasks})
        if request.url.path == "/mac-event":
            self.mac_events.append(body)
            return httpx.Response(200, json={"matched": "s_1", "title": "J. Crew Pants"})
        return httpx.Response(200, json={"ok": True})

    def client(self) -> sales.Api:
        return sales.Api("https://thrift-api.example", "mac-key",
                         client=httpx.Client(transport=httpx.MockTransport(self.handler)))

    def paths(self):
        return [p for _, p, _, _ in self.calls]


@pytest.fixture
def api(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(sales, "api", fake.client)
    return fake


@pytest.fixture
def said(monkeypatch):
    out = SimpleNamespace(group=[], ops=[])
    monkeypatch.setattr(notify, "group", out.group.append)
    monkeypatch.setattr(notify, "say", out.ops.append)
    monkeypatch.setattr(notify, "ops_photo", lambda path, caption: out.ops.append(caption))
    return out


def test_every_change_reaches_the_api_in_order_after_an_offline_spell(tmp_path, api):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    db.upsert_listing(iid, "depop", status="queued")
    api.down = True
    assert "error" in sales.tick(db, force_beat=True)                # offline: nothing lost
    assert db.conn.execute("SELECT COUNT(*) FROM api_dirty").fetchone()[0] == 2
    db.upsert_listing(iid, "depop", status="posted", url="https://www.depop.com/products/shop-x/")
    api.down = False
    done = sales.tick(db, force_beat=True)
    assert done["synced"] == 2 and done["beat"]
    sync = next(b for _, p, b, _ in api.calls if p == "/sync")
    assert [i["id"] for i in sync["items"]] == [iid]
    assert sync["listings"] == [{"item_id": iid, "marketplace": "depop", "status": "posted",
                                 "url": "https://www.depop.com/products/shop-x/", "listing_id": None, "sku": iid,
                                 "price": None, "posted_at": None, "updated_at": db.listing(iid, "depop")["updated_at"],
                                 "price_live": None, "price_read_at": None,                           # WO41
                                 "error": None, "photos_sent": None, "photos_live": None,            # WO45
                                 "photos_read_at": None}]
    assert api.paths().index("/sync") < api.paths().index("/heartbeat")
    assert db.conn.execute("SELECT COUNT(*) FROM api_dirty").fetchone()[0] == 0
    assert sales.tick(db) == {}                                        # nothing due a second later


def test_going_live_checks_poshmark_once_and_reports_what_is_gone(tmp_path, api, said):
    """WO33 D4: on the first heartbeat after "go live", every item on Depop/Vinted is checked against its Poshmark page;
    one no longer for sale goes to the API (its take-downs), an unreadable page is only counted; one ops summary,
    and never again for the same go-live."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    sold, fine, unread, only_posh = (_item(db, n) for n in (1, 2, 3, 4))
    for iid in (sold, fine, unread, only_posh):
        db.upsert_listing(iid, "poshmark", status="posted", url=f"https://poshmark.com/listing/x-{iid}")
    for iid in (sold, fine, unread):
        db.upsert_listing(iid, "depop", status="posted", url=f"https://www.depop.com/products/shop-{iid}/")
    states = {sold: False, fine: True, unread: None}
    seen = []

    def live(url):
        seen.append(url)
        return states[url.rsplit("x-", 1)[1]]
    beat = {"mode": "live", "go_live_at": "2026-10-09T13:00:00+00:00"}
    out = sales.golive_check(db, api.client(), beat["go_live_at"], live=live, pause=0)
    assert out == {"checked": 3, "gone": [sold], "unknown": [unread]} and len(seen) == 3
    assert api.mac_events == [{"kind": "not_for_sale", "item_id": sold, "marketplace": "poshmark",
                               "url": f"https://poshmark.com/listing/x-{sold}"}]
    assert len([m for m in said.ops if m.startswith("Go-live check: 3 items")]) == 1
    assert db.kv_get(sales.GOLIVE) == beat["go_live_at"]


def test_a_heartbeat_in_live_mode_runs_the_check_once(tmp_path, api, said, monkeypatch):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    runs = []
    monkeypatch.setattr(sales, "golive_check", lambda db, client, when: runs.append(when) or {"checked": 0})
    orig = api.handler

    def handler(request):
        if request.url.path == "/heartbeat":
            api.calls.append(("POST", "/heartbeat", None, {}))
            return httpx.Response(200, json={"ok": True, "mode": "live", "go_live_at": "2026-10-09T13:00:00+00:00"})
        return orig(request)
    api.handler = handler
    sales.tick(db, force_beat=True)
    db.kv_set(sales.GOLIVE, runs[0])
    sales.tick(db, force_beat=True)
    assert runs == ["2026-10-09T13:00:00+00:00"]


def test_kept_calls_replay_in_order(tmp_path, api):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    api.down = True
    for n in (1, 2, 3):
        assert not sales.call(db, f"/tasks/t_{n}", {"result": "done"})
    api.down = False
    assert sales.flush(db, api.client()) == 3
    assert api.paths() == ["/tasks/t_1", "/tasks/t_2", "/tasks/t_3"]


def test_without_the_env_lines_it_is_off_and_says_so_once_a_day(tmp_path, said, monkeypatch):
    monkeypatch.setattr(sales, "api", lambda: None)
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    assert sales.tick(db) == {"off": True} and sales.tick(db) == {"off": True}
    assert len([m for m in said.ops if m.startswith("Sales tracking is off")]) == 1


def _task(db, iid, mp, n=1):
    return {"id": f"t_{mp}_{n}", "item_id": iid, "marketplace": mp, "listing_url": f"https://{mp}.example/{iid}",
            "title": "J. Crew Pants", "attempts": 0}


class Taker:
    """A site's poster for take-downs: Depop/Vinted delist(url), Poshmark set_availability(ctx, url, False)."""

    def __init__(self, result=True, raises=None):
        self.result, self.raises, self.urls, self.shot = result, raises, [], "shots/x.png"

    async def delist(self, url):
        self.urls.append(url)
        if self.raises:
            raise self.raises
        return self.result

    async def set_availability(self, ctx, url, available, probe=False, shots=None):
        return await self.delist(url)


def test_the_owners_take_down_of_one_poshmark_listing(tmp_path, monkeypatch):
    """`thrift delist --item <item>` (WO33: the Lacoste tee sold on the owner's own Vinted before going live — no sale
    email could make the task): Not For Sale on Poshmark, the row delisted; an item not live there is left alone."""
    from thrift_agent.post import takedown
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid, other = _item(db), _item(db, 2)
    db.upsert_listing(iid, "poshmark", status="posted", url="https://poshmark.com/listing/Lacoste-Tee-6ac2f8dc64865578bc9aed94")
    taker = Taker()

    async def fake_open(s, sites):
        assert sites == ["poshmark"]
        return {"poshmark": taker}, None, None, None

    async def fake_close(bridge, pw, ctx):
        return None
    monkeypatch.setattr(takedown, "_open", fake_open)
    monkeypatch.setattr(takedown, "_close", fake_close)
    monkeypatch.setattr(takedown.parallel, "delist_ready", lambda mp, poster: None)
    monkeypatch.setattr(takedown.crosslist, "poshmark_live", lambda url: True)        # for sale: the edit page
    lines = asyncio.run(takedown.take_down(s, db, iid))
    assert taker.urls == ["https://poshmark.com/listing/Lacoste-Tee-6ac2f8dc64865578bc9aed94"]
    assert lines == ["Poshmark: Not For Sale — https://poshmark.com/listing/Lacoste-Tee-6ac2f8dc64865578bc9aed94"]
    assert db.listing(iid, "poshmark")["status"] == "delisted"
    assert asyncio.run(takedown.take_down(s, db, iid))[0].endswith("nothing to take down")     # once only
    assert "nothing to take down" in asyncio.run(takedown.take_down(s, db, other))[0]
    assert taker.urls == ["https://poshmark.com/listing/Lacoste-Tee-6ac2f8dc64865578bc9aed94"]
    assert "by hand" in asyncio.run(takedown.take_down(s, db, iid, "vinted"))[0]


def test_the_owners_take_down_of_a_listing_already_gone_opens_no_browser(tmp_path, monkeypatch):
    """WO33, live: the Lacoste tee's Poshmark page answered 404 (deleted by hand) — the row is recorded delisted, the
    poster's Chrome never opened."""
    from thrift_agent.post import takedown
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    db.upsert_listing(iid, "poshmark", status="posted", url="https://poshmark.com/listing/Lacoste-Tee-6ac2f8dc64865578bc9aed94")

    async def no_browser(s, sites):
        raise AssertionError("the browser must not open for a listing already gone")
    monkeypatch.setattr(takedown, "_open", no_browser)
    lines = asyncio.run(takedown.take_down(s, db, iid, live=lambda url: False))
    assert lines == ["Poshmark: already not for sale there (its public page) — "
                     "https://poshmark.com/listing/Lacoste-Tee-6ac2f8dc64865578bc9aed94"]
    assert db.listing(iid, "poshmark")["status"] == "delisted"


def test_a_take_down_skips_the_sold_items_queued_rows_and_reports_done(tmp_path, api, said):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    db.upsert_listing(iid, "depop", status="posted", url="https://depop.example/x")
    db.upsert_listing(iid, "vinted", status="queued")
    api.tasks = [_task(db, iid, "depop")]
    assert sales.fetch_tasks(db, ["depop"], force=True) == 1
    taker = Taker()
    assert asyncio.run(sales.run_takedown("depop", taker, db, None)) is True
    assert taker.urls == [f"https://depop.example/{iid}"]
    assert db.listing(iid, "depop")["status"] == "delisted"
    assert db.listing(iid, "vinted")["status"] == "skipped" and db.listing(iid, "vinted")["error"] == sales.SOLD_SKIP
    result = next(b for _, p, b, _ in api.calls if p == "/tasks/t_depop_1")
    assert result["result"] == "done" and not result["manual"]
    assert asyncio.run(sales.run_takedown("depop", taker, db, None)) is False      # nothing more


def test_an_item_sold_while_queued_is_never_listed(tmp_path, api):
    """Sold on Depop before Vinted got its turn: GET /tasks names the sold item, and its queued rows become skipped
    (sold) — a posted row is left for its take-down."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    db.upsert_listing(iid, "poshmark", status="posted", url="https://poshmark.com/listing/x-" + "b" * 24)
    db.upsert_listing(iid, "vinted", status="queued")
    orig = api.handler

    def handler(request):
        if request.url.path == "/tasks":
            return httpx.Response(200, json={"tasks": [], "sold": [iid]})
        return orig(request)
    api.handler = handler
    sales.fetch_tasks(db, ["vinted"], force=True)
    assert db.listing(iid, "vinted")["status"] == "skipped" and db.listing(iid, "vinted")["error"] == sales.SOLD_SKIP
    assert db.listing(iid, "poshmark")["status"] == "posted"


def test_the_site_an_item_sold_on_holds_it_as_sold(tmp_path, api):
    """2026-10-27: a sold Depop listing leaves Depop's shop data, so nothing marked the UGG Chestnut's or the Nike
    tee's Depop row sold — GET /tasks names the site each item sold ON; only that row becomes sold (the others are
    the take-downs')."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    db.upsert_listing(iid, "depop", status="posted", url="https://www.depop.com/products/shop-nike-celtics-dab5/")
    db.upsert_listing(iid, "poshmark", status="posted", url="https://poshmark.com/listing/x-" + "c" * 24)
    orig = api.handler

    def handler(request):
        if request.url.path == "/tasks":
            return httpx.Response(200, json={"tasks": [], "sold": [iid], "sold_on": [{"item": iid, "site": "depop"},
                                                                                     {"item": "i_gone", "site": "x"}]})
        return orig(request)
    api.handler = handler
    sales.fetch_tasks(db, ["depop"], force=True)
    assert db.listing(iid, "depop")["status"] == "sold"
    assert db.listing(iid, "poshmark")["status"] == "posted"                  # left for its take-down
    assert db.conn.execute("SELECT count(*) FROM events WHERE kind='listing_sold_here'").fetchone()[0] == 1
    sales.fetch_tasks(db, ["depop"], force=True)                              # told again: nothing new
    assert db.conn.execute("SELECT count(*) FROM events WHERE kind='listing_sold_here'").fetchone()[0] == 1


def test_an_unrecorded_control_is_never_tried_and_the_owner_is_asked(tmp_path, api):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    api.tasks = [_task(db, iid, "vinted")]
    sales.fetch_tasks(db, ["vinted"], force=True)
    taker = Taker()
    assert asyncio.run(sales.run_takedown("vinted", taker, db, None,
                                          ready=lambda site, poster: "Vinted's Hide isn't recorded yet")) is True
    assert taker.urls == []
    result = next(b for _, p, b, _ in api.calls if p == "/tasks/t_vinted_1")
    assert result["result"] == "failed" and result["manual"] and "isn't recorded yet" in result["error"]


def test_a_failing_take_down_waits_for_its_next_lease_and_the_api_counts(tmp_path, api):
    """WO40 (the UGG, 2026-10-16): three quick tries on ONE lease — the API counted one, the Mac marked its row failed
    for good, and the API re-leased it silently for a day. Now a failure waits for the API to hand the task out again,
    and a task the API hands out is pending here whatever this Mac thought of it."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    api.tasks = [_task(db, iid, "depop")]
    sales.fetch_tasks(db, ["depop"], force=True)
    taker = Taker(raises=RuntimeError("menu didn't open"))
    assert asyncio.run(sales.run_takedown("depop", taker, db, None)) is True
    assert db.conn.execute("SELECT status FROM takedowns").fetchone()[0] == sales.WAITING
    assert asyncio.run(sales.run_takedown("depop", taker, db, None)) is False          # never again on the same lease
    for n in (1, 2):                                            # the API leases it again (its attempts so far)
        api.tasks = [{**api.tasks[0], "attempts": n}]
        sales.fetch_tasks(db, ["depop"], force=True)
        assert db.conn.execute("SELECT status, attempts FROM takedowns").fetchone()[:] == ("pending", n)
        assert asyncio.run(sales.run_takedown("depop", taker, db, None)) is True
    assert [b["result"] for _, p, b, _ in api.calls if p == "/tasks/t_depop_1"] == ["failed"] * 3
    db.conn.execute("UPDATE takedowns SET status='failed'")     # a row an older version left 'failed' here
    sales.fetch_tasks(db, ["depop"], force=True)                # ... the API still hands it out: tried again
    assert db.conn.execute("SELECT status FROM takedowns").fetchone()[0] == "pending"


def test_a_login_page_during_a_take_down_stops_the_site_and_is_no_attempt(tmp_path, api, said):
    from thrift_agent.post.base import AccountBlocked
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    api.tasks = [_task(db, iid, "vinted")]
    sales.fetch_tasks(db, ["vinted"], force=True)
    assert asyncio.run(sales.run_takedown("vinted", Taker(raises=AccountBlocked("Vinted: logged out", page="login")),
                                          db, None)) is True
    assert crosslist.account_blocked(db, "vinted")
    assert not [p for _, p, _, _ in api.calls if p == "/tasks/t_vinted_1"]               # not counted as an attempt


def test_a_listing_already_gone_is_not_found(tmp_path, api):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    api.tasks = [_task(db, iid, "poshmark")]
    sales.fetch_tasks(db, ["poshmark"], force=True)
    assert asyncio.run(sales.run_takedown("poshmark", Taker(result=None), db, None))
    assert next(b for _, p, b, _ in api.calls if p == "/tasks/t_poshmark_1")["result"] == "not_found"


def test_all_done_waits_for_the_take_downs(tmp_path, api):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    db.set_item(iid, status="posted")
    api.tasks = [_task(db, iid, "depop")]
    before = daily.all_done(s, db)
    sales.fetch_tasks(db, ["depop"], force=True)
    assert not daily.all_done(s, db)
    asyncio.run(sales.run_takedown("depop", Taker(), db, None))
    assert daily.all_done(s, db) == before


def test_the_worker_does_its_take_downs_before_new_listings(tmp_path, api, said, monkeypatch):
    """A Vinted take-down (an item sold on Poshmark) comes first; then Vinted's next listing."""
    order = []

    async def takedowns(site, poster, db, ctx):
        if site == "vinted" and sales.pending(db, site):
            order.append(("takedown", site))
            return await sales.run_takedown(site, Taker(), db, ctx)
        return False
    from test_parallel import Timed, fake_open
    s = _settings(tmp_path, parallel=True)
    db = DB(s.path("db"))
    sold, fresh = _item(db, 1), _item(db, 2)
    db.upsert_listing(sold, "vinted", status="posted", url="https://vinted.example/1")
    db.upsert_listing(sold, "poshmark", status="posted", url="https://poshmark.com/listing/x-" + "a" * 24)
    api.tasks = [_task(db, sold, "vinted")]
    sales.fetch_tasks(db, ["vinted"], force=True)
    monkeypatch.setattr(parallel, "IDLE", 0.05)
    monkeypatch.setattr(parallel, "HOUSEKEEPING", 0.05)
    monkeypatch.setattr(parallel, "next_gap", lambda schedule: 0.0)
    monkeypatch.setattr(parallel.crosslist, "listing_gap", lambda s, mp: 0.0)
    monkeypatch.setattr(parallel.runner, "_paused", lambda db: None)
    monkeypatch.setattr(parallel.runner, "start_thrift_chrome", lambda s: "not in a test")

    class Recorder(Timed):
        async def post(self, ctx, r, mode, dry_run, shots, stage="form"):
            order.append(("list", self.name))
            return await super().post(ctx, r, mode, dry_run, shots, stage)
    from test_crosslist_flow import fields_for
    monkeypatch.setattr(parallel.runner, "map_fields", lambda mp, view: fields_for(mp))
    monkeypatch.setattr(parallel.runner.ItemView, "from_row", classmethod(lambda cls, it: SimpleNamespace(
        render=SimpleNamespace(title="J. Crew Pants"))))
    ps = {"poshmark": Recorder("poshmark", 0.05), "depop": Recorder("depop", 0.05), "vinted": Recorder("vinted", 0.05)}
    asyncio.run(parallel.run_parallel(s, db, ps, dry=False, stage="form", opener=fake_open, takedowns=takedowns,
                                      until_idle=True))
    vinted = [x for x in order if x[1] == "vinted"]
    assert vinted[0] == ("takedown", "vinted") and ("list", "vinted") in vinted
    assert db.listing(sold, "vinted")["status"] == "delisted" and db.listing(fresh, "vinted")["status"] == "posted"


def test_take_downs_wait_for_the_brake_the_lid_and_a_stopped_site(tmp_path, monkeypatch):
    """Take-downs run outside listing hours (a sold item must not sell twice), but never with the owner's PAUSE, the
    lid closed, or the site stopped for the window."""
    from thrift_agent import crosslist
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    monkeypatch.setattr(parallel.power, "lid_closed", lambda: False)
    assert parallel.may_take_down(s, db, "depop")
    crosslist.block(db, "depop", "Depop: not logged in")
    assert not parallel.may_take_down(s, db, "depop") and parallel.may_take_down(s, db, "vinted")
    # WO40: a site stopped for posting failures (Vinted refused a form twice that day) still takes down what sold
    crosslist.block(db, "vinted", "failures: 3 in a row: Vinted refused the form")
    assert crosslist.blocked(db, "vinted") and parallel.may_take_down(s, db, "vinted")
    monkeypatch.setattr(parallel.power, "lid_closed", lambda: True)
    assert not parallel.may_take_down(s, db, "vinted")
    monkeypatch.setattr(parallel.power, "lid_closed", lambda: False)
    s.flag("PAUSE").write_text("the owner's brake", encoding="utf-8")
    assert not parallel.may_take_down(s, db, "vinted")


def test_the_owners_shipped_reply_goes_to_the_api(tmp_path, api, monkeypatch):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    sent = []
    bot = SimpleNamespace(send_message=lambda text, **k: sent.append(text))
    assert approve._typed(s, db, bot, "shipped jcrew pants", 5) == "shipped: s_1"
    assert api.mac_events == [{"kind": "shipped", "words": "jcrew pants"}] and sent == []   # the API says ✓ itself
    api.down = True
    assert approve._typed(s, db, bot, "Shipped the red flats", 6) == "shipped: kept for later"
    assert db.conn.execute("SELECT path FROM api_outbox").fetchall()[0][0] == "/mac-event"


def test_the_first_sync_sends_every_item_and_listing_made_before_the_triggers(tmp_path, api):
    """The triggers mark only what changes after they exist: once, at the first start, every item and listing is
    marked, so thrift-api holds the listings made before (a sale of one must match); never again on its own;
    `thrift sync --push --all` marks them all again."""
    from thrift_agent import db as db_mod
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    old = _item(db, 1)
    db.upsert_listing(old, "poshmark", status="posted", url="https://poshmark.com/listing/x-" + "c" * 24)
    db.conn.execute("DELETE FROM api_dirty")                       # as on the Mac: made before the triggers were
    db.conn.execute("DELETE FROM kv WHERE key=?", (db_mod.API_SEEDED,))
    db.conn.commit()
    again = DB(s.path("db"))                                       # the worker's next start
    assert {r[0] for r in again.conn.execute("SELECT key FROM api_dirty")} == {old, f"{old}|poshmark"}
    assert sales.push(again, api.client()) == 2
    assert DB(s.path("db")).conn.execute("SELECT COUNT(*) FROM api_dirty").fetchone()[0] == 0   # never twice
    assert again.seed_api_dirty(again=True) == 2


def test_depop_already_sold_is_reported_sold_and_nothing_is_deleted(tmp_path, api):
    """WO33: Depop's listing shows sold already — the poster returns "sold" (nothing pressed): the API is told so
    (its group line "Sold twice"), the row is sold here."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    db.upsert_listing(iid, "depop", status="posted", url="https://www.depop.com/products/shop-x-1a2b/")
    api.tasks = [_task(db, iid, "depop")]
    sales.fetch_tasks(db, ["depop"], force=True)
    assert asyncio.run(sales.run_takedown("depop", Taker(result="sold"), db, None)) is True
    assert next(b for _, p, b, _ in api.calls if p == "/tasks/t_depop_1")["result"] == "sold"
    assert db.listing(iid, "depop")["status"] == "sold"


class Scripted:
    """ExtensionPoster._job answered from a script: (mode, event) pairs in order."""

    def __init__(self, script):
        self.script, self.asked = list(script), []

    async def __call__(self, mode, payload, timeout, shot=None):
        self.asked.append((mode, payload))
        want, ev = self.script.pop(0)
        assert want == mode, (want, mode)
        return ev


LIVE = {"event": "result", "live": {"product": True, "availability": "https://schema.org/InStock"}}
SOLD = {"event": "result", "live": {"product": True, "availability": "https://schema.org/SoldOut"}}
GONE = {"event": "error", "page": "unknown", "message": "not a listing page"}


def _depop(monkeypatch, script):
    from thrift_agent.post import ext_driver
    p = ext_driver.ExtensionPoster("depop", bridge=None)
    monkeypatch.setattr(ext_driver, "unverified", lambda site, path=None: set())
    job = Scripted(script)
    monkeypatch.setattr(p, "_job", job)
    return p, job


def test_depop_delete_presses_once_and_counts_only_when_the_address_is_gone(monkeypatch):
    url = "https://www.depop.com/products/shop-j-crew-pants-11b1/"
    p, job = _depop(monkeypatch, [("verify", LIVE), ("delist", {"event": "result", "delete_pressed": True}),
                                  ("verify", GONE)])
    assert asyncio.run(p.delist(url)) is True
    assert job.asked[1] == ("delist", {"listing_url": url + "manage/", "check_url": url})


def test_depop_sold_already_is_never_deleted(monkeypatch):
    p, job = _depop(monkeypatch, [("verify", SOLD)])
    assert asyncio.run(p.delist("https://www.depop.com/products/shop-j-crew-pants-11b1/")) == "sold"
    assert [m for m, _ in job.asked] == ["verify"]                     # no delete job at all


def test_depop_still_there_after_delete_is_an_error_and_gone_before_is_not_found(monkeypatch):
    from thrift_agent.post.base import PosterError
    p, _ = _depop(monkeypatch, [("verify", LIVE), ("delist", {"event": "result", "delete_pressed": True}),
                                ("verify", LIVE)])
    with pytest.raises(PosterError, match="still there"):
        asyncio.run(p.delist("https://www.depop.com/products/shop-j-crew-pants-11b1/"))
    p2, _ = _depop(monkeypatch, [("verify", GONE)])
    assert asyncio.run(p2.delist("https://www.depop.com/products/shop-j-crew-pants-11b1/")) is None


def test_the_workers_ask_for_take_downs_once_for_all_sites(tmp_path, api):
    """WO35: the three site workers ask GET /tasks once for all the poster's sites, at most every TASKS_EVERY seconds
    — they had asked on every idle turn (three workers every 20 s: ~5,600 calls on 2026-10-09, and the API's log hit
    its 0.1 GB daily cap)."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    sites = ["poshmark", "depop", "vinted"]
    for _ in range(10):
        for site in sites:
            assert asyncio.run(parallel.takedowns(site, None, db, None, sites=sites)) is False
    assert [q for _, p, _, q in api.calls if p == "/tasks"] == [{"sites": "poshmark,depop,vinted"}]
    stale = datetime.now(timezone.utc) - timedelta(seconds=sales.TASKS_EVERY + 1)
    db.kv_set(sales.LAST_TASKS, stale.isoformat(timespec="seconds"))
    for site in sites:
        asyncio.run(parallel.takedowns(site, None, db, None, sites=sites))
    assert api.paths().count("/tasks") == 2
    sales.fetch_tasks(db, sites, force=True)                         # the CLI's `thrift delist --run` still asks now
    assert api.paths().count("/tasks") == 3


def test_the_running_poster_asks_for_take_downs_for_its_sites_at_once(tmp_path, api, said, monkeypatch):
    """The poster's own take-downs (the API set up, none passed in): its idle workers turn many times, the API is asked
    once, for all three sites."""
    from test_parallel import Timed, fake_open
    s = _settings(tmp_path, parallel=True)
    db = DB(s.path("db"))
    monkeypatch.setattr(parallel, "IDLE", 0.02)
    monkeypatch.setattr(parallel, "HOUSEKEEPING", 0.02)
    monkeypatch.setattr(parallel.runner, "_paused", lambda db: None)
    monkeypatch.setattr(parallel.runner, "start_thrift_chrome", lambda s: "not in a test")
    turns = []
    real = sales.run_takedown

    async def counted(site, poster, db, ctx, ready=None, shots=None):
        turns.append(site)
        return await real(site, poster, db, ctx, ready=ready, shots=shots)
    monkeypatch.setattr(sales, "run_takedown", counted)
    ps = {"poshmark": Timed("poshmark", 0.0), "depop": Timed("depop", 0.0), "vinted": Timed("vinted", 0.0)}
    asyncio.run(parallel.run_parallel(s, db, ps, dry=False, stage="form", opener=fake_open, until_idle=True))
    assert len(turns) >= 6 and set(turns) == {"poshmark", "depop", "vinted"}
    assert [q for _, p, _, q in api.calls if p == "/tasks"] == [{"sites": "poshmark,depop,vinted"}]


def test_a_call_the_api_refuses_for_good_never_holds_the_outbox(tmp_path, api, monkeypatch):
    """WO38: the outbox keeps its order while the API is away — but a call it ANSWERS with a refusal (a route it doesn't
    have yet: 404) is dropped with an event, so the calls after it still go; a key refused (401) holds it as before."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    refuse = {"/orders": 404}
    inner = api.handler

    def handler(request):
        if request.url.path in refuse:
            api.calls.append((request.method, request.url.path, None, {}))
            return httpx.Response(refuse[request.url.path], json={"error": "no such path"})
        return inner(request)
    monkeypatch.setattr(sales, "api", lambda: sales.Api("https://thrift-api.example", "mac-key",
                                                         client=httpx.Client(transport=httpx.MockTransport(handler))))
    sales.queue(db, "/orders", {"marketplace": "vinted", "orders": []})
    sales.queue(db, "/mac-event", {"kind": "shipped", "words": "jcrew"})
    assert sales.flush(db, sales.api()) == 2
    assert api.paths() == ["/orders", "/mac-event"] and not db.conn.execute("SELECT * FROM api_outbox").fetchall()
    assert db.conn.execute("SELECT detail FROM events WHERE kind = 'api_refused'").fetchone()[0] == \
        '{"path": "/orders", "status": 404}'
    refuse["/orders"] = 401
    sales.queue(db, "/orders", {"marketplace": "vinted", "orders": []})
    sales.queue(db, "/mac-event", {"kind": "shipped", "words": "jcrew"})
    with pytest.raises(sales.ApiError):
        sales.flush(db, sales.api())
    assert [r[0] for r in db.conn.execute("SELECT path FROM api_outbox ORDER BY id")] == ["/orders", "/mac-event"]
