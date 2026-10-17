"""WO41 — automatic price drops (the owner, 2026-10-17): 10% off on every site 30 days after the item is live on all
of them, 10% again 30 days later, then never again. The clock, who drops, the price math, exactly two drops, one Save
and never a second without the site's own data, a sale mid-drop, the one group line a day, 'no drop'; the extension
driver's edit-page job (Depop's blank fields filled, a refusal reported, a dry run never saved); the API's mirror and
dashboard. The sites are stand-ins: nothing reaches a real one."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from ext_fake import FakeExtension
from test_bridge import TOKEN, _bridge
from test_crosslist_flow import _item, _settings

from thrift_agent import approve, notify, pricedrop, sales
from thrift_agent.db import DB
from thrift_agent.post.base import Outcome
from thrift_agent.post.ext_driver import ExtensionPoster

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
URLS = {"poshmark": "https://poshmark.com/listing/Levis-501-Cutoff-Shorts-6702aa11bb22cc33dd44ee55",
        "depop": "https://www.depop.com/products/shop-levis-501-cutoff-shorts-1a2b/",
        "vinted": "https://www.vinted.com/items/10355633825"}


def iso(t):
    return t.isoformat(timespec="seconds")


@pytest.fixture
def said(monkeypatch):
    out = SimpleNamespace(group=[], ops=[])
    monkeypatch.setattr(notify, "group", out.group.append)
    monkeypatch.setattr(notify, "say", out.ops.append)
    return out


@pytest.fixture
def s(tmp_path):
    return _settings(tmp_path)


@pytest.fixture
def db(s):
    return DB(s.path("db"))


def live(db, posted: dict, seq=1, price=25) -> str:
    """An item posted on each site at the given time (None: that site's row is `failed`, never live)."""
    iid = _item(db, seq)
    db.set_item(iid, status="posted", owner_price=price)
    for mp, when in posted.items():
        if when is None:
            db.upsert_listing(iid, mp, status="failed", error="refused")
        else:
            db.upsert_listing(iid, mp, status="posted", url=URLS[mp], posted_at=iso(when), price=price)
    return iid


class Site:
    """One site's listing as the site shows it: its price, its state, every Save counted."""

    def __init__(self, price, state=None, stale=False, fail=None):
        self.price, self.state, self.stale, self.fail = price, state, stale, fail
        self.saves, self.reads, self.dry = 0, 0, 0

    async def read_price(self, ctx, url):
        self.reads += 1
        if self.state:
            return {"unavailable": self.state, "price": self.price}
        return {"price": self.price}

    async def set_price(self, ctx, url, old, new, dry=False, shots=None, go=None, fields=None):
        if self.price != old:
            return {"ok": False, "saved": False, "mismatch": self.price}
        if dry:
            self.dry += 1
            return {"ok": True, "saved": False}
        if self.fail:
            return {"ok": False, "saved": False, "error": self.fail}
        if go is not None and not go():
            return {"ok": False, "saved": False, "error": "stopped before Save"}
        self.saves += 1
        if not self.stale:
            self.price = new
        return {"ok": self.price == new, "saved": True, "live_price": self.price}


def drop_all(s, db, sites: dict, now=NOW):
    """Every site's due drops made, as the workers would on their idle turns."""
    async def go():
        for mp, site in sites.items():
            for row in pricedrop.due_rows(db, mp, now):
                await pricedrop.apply(s, db, row, site, None, list(sites))
    asyncio.run(go())


def rows(db, iid):
    return {(r["marketplace"], r["stage"]): r for r in db.conn.execute(
        "SELECT * FROM price_changes WHERE item_id=? AND dry=0", (iid,)).fetchall()}


# ---------------------------------------------------------------- the clock

def test_the_clock_starts_when_the_last_site_went_live(db):
    iid = live(db, {"poshmark": NOW - timedelta(days=40), "depop": NOW - timedelta(days=35),
                    "vinted": NOW - timedelta(days=29)})
    assert pricedrop.plan(db, NOW) == []                                    # Vinted live 29 days: not yet
    it = db.item(iid)
    assert it["all_live_at"] == iso(NOW - timedelta(days=29))
    assert it["next_drop_at"] == iso(NOW + timedelta(days=1))
    assert pricedrop.plan(db, NOW + timedelta(days=1)) == [f"{iid}:auto_1"]


def test_a_site_that_never_goes_live_starts_the_clock_7_days_after_the_first(db):
    iid = live(db, {"poshmark": NOW - timedelta(days=36), "depop": NOW - timedelta(days=36), "vinted": None})
    assert db.item(iid)["all_live_at"] is None
    pricedrop.plan(db, NOW - timedelta(days=31))                            # day 5: no clock yet
    assert db.item(iid)["all_live_at"] is None
    assert pricedrop.plan(db, NOW) == []
    assert db.item(iid)["all_live_at"] == iso(NOW - timedelta(days=29))    # first live + 7 days
    assert [k for k in pricedrop.plan(db, NOW + timedelta(days=1))] == [f"{iid}:auto_1"]
    assert set(rows(db, iid)) == {("poshmark", "auto_1"), ("depop", "auto_1")}   # only the live sites


def test_a_backfilled_item_counts_from_when_it_reached_all_three(db):
    """The Oct 8–9 backfill: Poshmark live since September, Depop and Vinted since the backfill."""
    iid = live(db, {"poshmark": datetime(2026, 9, 2, tzinfo=timezone.utc),
                    "depop": datetime(2026, 10, 8, 18, tzinfo=timezone.utc),
                    "vinted": datetime(2026, 10, 9, 14, tzinfo=timezone.utc)})
    pricedrop.plan(db, datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert db.item(iid)["next_drop_at"] == iso(datetime(2026, 11, 8, 14, tzinfo=timezone.utc))


def test_a_relisted_site_starts_a_new_clock(s, db, said):
    iid = live(db, {"poshmark": NOW - timedelta(days=31), "depop": NOW - timedelta(days=31)})
    pricedrop.plan(db, NOW)
    drop_all(s, db, {"poshmark": Site(25), "depop": Site(25)})
    assert db.item(iid)["drop_stage"] == 1
    db.upsert_listing(iid, "depop", posted_at=iso(NOW + timedelta(days=3)), url=URLS["depop"] + "?new")   # deleted, posted again
    pricedrop.plan(db, NOW + timedelta(days=4))
    it = db.item(iid)
    assert it["drop_stage"] == 0 and it["next_drop_at"] == iso(NOW + timedelta(days=33))


# ---------------------------------------------------------------- who drops

@pytest.mark.parametrize("how", ["sold", "takedown", "delisted", "excluded", "not posted"])
def test_no_drop_for_an_item_sold_taken_down_hidden_or_excluded(db, how):
    iid = live(db, {"poshmark": NOW - timedelta(days=40), "depop": NOW - timedelta(days=40)})
    if how == "sold":
        db.upsert_listing(iid, "depop", status="sold")
    elif how == "takedown":
        db.conn.execute("INSERT INTO takedowns (id, item_id, marketplace, status, fetched_at) VALUES "
                        "('t_1', ?, 'depop', 'pending', ?)", (iid, iso(NOW)))
    elif how == "delisted":
        db.upsert_listing(iid, "poshmark", status="delisted")            # Not For Sale / Hidden
    elif how == "excluded":
        pricedrop.exclude(db, iid)
    else:
        db.set_item(iid, status="ready")
    assert pricedrop.plan(db, NOW) == [] and rows(db, iid) == {}


def test_a_listing_reserved_on_vinted_is_skipped_there_the_others_drop(s, db, said):
    iid = live(db, {"poshmark": NOW - timedelta(days=31), "vinted": NOW - timedelta(days=31)})
    pricedrop.plan(db, NOW)
    posh, vinted = Site(25), Site(25, state="reserved")
    drop_all(s, db, {"poshmark": posh, "vinted": vinted})
    r = rows(db, iid)
    assert r[("poshmark", "auto_1")]["status"] == "done" and posh.price == 22
    assert r[("vinted", "auto_1")]["status"] == "skipped" and "reserved" in r[("vinted", "auto_1")]["error"]
    assert vinted.saves == 0


# ---------------------------------------------------------------- the price

@pytest.mark.parametrize("old,new", [(25, 22), (40, 36), (95, 85), (110, 99), (20, 18), (24.99, 22)])
def test_whole_dollars_rounded_down_from_90_percent(old, new):
    assert pricedrop.dropped(old) == new


def test_each_site_drops_from_its_own_live_price(s, db, said):
    """The J. Crew pants: Poshmark at a price she changed by hand, Depop and Vinted at the approved one."""
    iid = live(db, {"poshmark": NOW - timedelta(days=31), "depop": NOW - timedelta(days=31),
                    "vinted": NOW - timedelta(days=31)}, price=45)
    pricedrop.plan(db, NOW)
    sites = {"poshmark": Site(40), "depop": Site(45), "vinted": Site(45)}
    drop_all(s, db, sites)
    assert [sites[m].price for m in ("poshmark", "depop", "vinted")] == [36, 40, 40]
    r = rows(db, iid)
    assert (r[("poshmark", "auto_1")]["old_price"], r[("poshmark", "auto_1")]["new_price"]) == (40, 36)
    assert db.listing(iid, "depop")["price_live"] == 40


def test_the_floor_and_a_sites_minimum_skip_that_site_never_a_smaller_cut(s, db, said):
    s.data.setdefault("price_drop", {})["floor"] = 15
    iid = live(db, {"poshmark": NOW - timedelta(days=31), "depop": NOW - timedelta(days=31)}, price=16)
    pricedrop.plan(db, NOW)
    posh, depop = Site(16), Site(30)
    drop_all(s, db, {"poshmark": posh, "depop": depop})
    r = rows(db, iid)
    assert r[("poshmark", "auto_1")]["status"] == "skipped" and "floor $15" in r[("poshmark", "auto_1")]["error"]
    assert posh.saves == 0 and posh.price == 16 and depop.price == 27
    assert pricedrop.too_low("poshmark", 2, None) and not pricedrop.too_low("depop", 2, None)


# ---------------------------------------------------------------- exactly two, once each

def test_exactly_two_drops_ever_whatever_the_reruns(s, db, said):
    iid = live(db, {"poshmark": NOW - timedelta(days=31), "depop": NOW - timedelta(days=31)}, price=40)
    sites = {"poshmark": Site(40), "depop": Site(40)}
    for day in range(0, 200, 5):                                          # the poster's turns, restarts and all
        when = NOW + timedelta(days=day)
        pricedrop.plan(db, when)
        pricedrop.plan(db, when)
        drop_all(s, db, sites, when)
    assert [r["stage"] for r in rows(db, iid).values()].count("auto_1") == 2
    assert {k[1] for k in rows(db, iid)} == {"auto_1", "auto_2"}
    assert sites["poshmark"].saves == 2 and sites["poshmark"].price == 32   # 40 → 36 → 32, then nothing
    it = db.item(iid)
    assert it["drop_stage"] == 2 and it["next_drop_at"] is None
    assert pricedrop.plan(db, NOW + timedelta(days=400), item=iid, force=True) == []


def test_a_save_the_site_doesnt_show_is_read_again_never_saved_twice_blind(s, db, said, monkeypatch):
    iid = live(db, {"depop": NOW - timedelta(days=31)})
    pricedrop.plan(db, NOW)
    depop = Site(25, stale=True)
    drop_all(s, db, {"depop": depop})
    row = rows(db, iid)[("depop", "auto_1")]
    assert row["status"] == "unconfirmed" and depop.saves == 1
    depop.price = 22                                                       # the site caught up
    monkeypatch.setattr(pricedrop, "_now", lambda: NOW + timedelta(minutes=10))
    drop_all(s, db, {"depop": depop}, NOW + timedelta(minutes=10))
    assert rows(db, iid)[("depop", "auto_1")]["status"] == "done" and depop.saves == 1   # read, not saved again


def test_a_save_that_didnt_take_counts_and_waits_for_the_next_window(s, db, said, monkeypatch):
    iid = live(db, {"vinted": NOW - timedelta(days=31)})
    pricedrop.plan(db, NOW)
    vinted = Site(25, stale=True)
    drop_all(s, db, {"vinted": vinted})
    monkeypatch.setattr(pricedrop, "_now", lambda: NOW + timedelta(minutes=10))
    drop_all(s, db, {"vinted": vinted}, NOW + timedelta(minutes=10))     # still $25: not taken — no Save now
    row = rows(db, iid)[("vinted", "auto_1")]
    assert row["status"] == "pending" and row["attempts"] == 1 and vinted.saves == 1
    assert pricedrop.due_rows(db, "vinted", NOW + timedelta(hours=1)) == []   # the next window, not before


def test_three_failures_ask_the_owner_once(s, db, said, monkeypatch):
    iid = live(db, {"depop": NOW - timedelta(days=31)})
    pricedrop.plan(db, NOW)
    depop = Site(25, fail="refused: Category: This field is required")
    for n in range(4):
        when = NOW + timedelta(hours=7 * n)
        monkeypatch.setattr(pricedrop, "_now", lambda when=when: when)
        drop_all(s, db, {"depop": depop}, when)
    row = rows(db, iid)[("depop", "auto_1")]
    assert row["status"] == "failed" and row["attempts"] == 3
    assert said.ops == ["Price drop on Depop failed for Tory Burch Red Ballet Flats size 7.5: $25 → $22. "
                        "Please change it by hand."]


def test_a_price_she_changed_by_hand_counts_as_done(s, db, said, monkeypatch):
    iid = live(db, {"depop": NOW - timedelta(days=31)})
    pricedrop.plan(db, NOW)
    depop = Site(25, fail="Save stayed disabled")
    drop_all(s, db, {"depop": depop})
    depop.price = 21                                                       # she lowered it herself
    monkeypatch.setattr(pricedrop, "_now", lambda: NOW + timedelta(hours=7))
    drop_all(s, db, {"depop": depop}, NOW + timedelta(hours=7))
    row = rows(db, iid)[("depop", "auto_1")]
    assert row["status"] == "done" and row["new_price"] == 21 and depop.saves == 0


def test_a_sale_mid_drop_stops_it_before_the_save(s, db, said, monkeypatch):
    iid = live(db, {"poshmark": NOW - timedelta(days=31), "depop": NOW - timedelta(days=31)})
    pricedrop.plan(db, NOW)

    def sold_now(db_, sites, force=False):                                 # GET /tasks right before Save: it sold
        db_.conn.execute("INSERT OR IGNORE INTO takedowns (id, item_id, marketplace, status, fetched_at) VALUES "
                         "('t_9', ?, 'depop', 'pending', ?)", (iid, iso(NOW)))
        return 1
    monkeypatch.setattr(sales, "fetch_tasks", sold_now)
    posh = Site(25)
    drop_all(s, db, {"poshmark": posh, "depop": Site(25)})
    assert posh.saves == 0 and {r["status"] for r in rows(db, iid).values()} == {"skipped"}


def test_a_sold_item_calls_off_its_open_drops(s, db):
    iid = live(db, {"poshmark": NOW - timedelta(days=31)})
    pricedrop.plan(db, NOW)
    sales.skip_sold(db, iid)
    assert rows(db, iid)[("poshmark", "auto_1")]["status"] == "skipped"


# ---------------------------------------------------------------- the group, once a day

def test_one_group_line_a_day_once_the_drops_are_done_none_without(s, db, said, monkeypatch):
    monkeypatch.setattr(pricedrop, "short_name", lambda db_, iid: {1: "Levi's 501 shorts", 2: "Zara skirt"}[
        int(db_.item(iid)["seq"])])
    assert pricedrop.announce(s, db, NOW) is None                         # a day without drops: nothing
    a = live(db, {"poshmark": NOW - timedelta(days=31), "depop": NOW - timedelta(days=31)}, seq=1)
    b = live(db, {"vinted": NOW - timedelta(days=31)}, seq=2)
    pricedrop.plan(db, NOW)
    assert pricedrop.announce(s, db, NOW) is None                         # not done yet
    drop_all(s, db, {"poshmark": Site(25), "depop": Site(25), "vinted": Site(25)})
    db.conn.execute("UPDATE price_changes SET stage='auto_2' WHERE item_id=?", (b,))
    text = pricedrop.announce(s, db, NOW)
    assert text == "🔻 Prices down 10% today: Levi's 501 shorts $25 → $22 · Zara skirt $25 → $22 (last drop)."
    assert said.group == [text]
    assert pricedrop.announce(s, db, NOW) is None and said.group == [text]   # once
    assert a


# ---------------------------------------------------------------- 'no drop'

class Bot:
    def __init__(self):
        self.sent = []

    def send_message(self, text, buttons=None, reply_to=None):
        self.sent.append((text, reply_to))
        return 900 + len(self.sent)


def test_a_no_drop_reply_to_the_items_card_excludes_it_and_is_answered(s, db, said):
    iid = live(db, {"poshmark": NOW - timedelta(days=31)})
    pricedrop.plan(db, NOW)
    db.conn.execute("INSERT INTO outbox (chat_id, message_id, kind, ref, text, sent_at) VALUES "
                    "('-100', 55, 'posted', ?, 'card', ?)", (iid, iso(NOW)))
    bot = Bot()
    msg = {"message_id": 77, "chat": {"id": -100}, "text": "No drop", "reply_to_message": {"message_id": 55}}
    assert approve._route_message(s, db, bot, msg) == f"no drop {iid}"
    assert bot.sent[0][1] == 77 and bot.sent[0][0].startswith("✓ No price drops for ")
    assert db.item(iid)["no_drop"] and rows(db, iid)[("poshmark", "auto_1")]["status"] == "skipped"
    assert pricedrop.plan(db, NOW + timedelta(days=90)) == []


# ---------------------------------------------------------------- the extension driver's edit-page job

async def _price_with_fake(results, mp="depop", dry=False, price=25.0, new=22.0, site_price=None, go=None):
    b = await _bridge()
    fake = FakeExtension(b.port, TOKEN, results)
    if site_price is not None:
        fake.price_on[mp] = site_price
    fake.start()
    await asyncio.wait_for(fake.connected.wait(), 5)
    p = ExtensionPoster(mp, "12345" if mp == "vinted" else "shop", bridge=b)
    try:
        out = await p.set_price(None, URLS[mp], price, new, dry=dry, go=go, fields={"sku": "i_1", "category": "x"})
    finally:
        fake.stop()
        await b.close()
    return out, fake


@pytest.mark.parametrize("mp", ["depop", "vinted"])
def test_the_edit_page_price_saved_once_and_read_on_the_site(mp):
    out, fake = asyncio.run(_price_with_fake([Outcome("posted")], mp=mp))
    assert out["ok"] and out["saved"] and out["live_price"] == 22 and fake.clicks == 1, out
    job = fake.jobs[0]
    assert job["mode"] == "price" and job["price"] == 22 and job["expect"] == 25
    assert job["edit_url"] == {"depop": "https://www.depop.com/products/edit/shop-levis-501-cutoff-shorts-1a2b/",
                               "vinted": "https://www.vinted.com/items/10355633825/edit"}[mp]


def test_a_dry_run_fills_the_price_and_never_saves():
    out, fake = asyncio.run(_price_with_fake([Outcome("posted")], dry=True))
    assert out["ok"] and not out["saved"] and fake.clicks == 0


def test_depops_blank_fields_are_filled_from_the_listing_before_the_save():
    out, fake = asyncio.run(_price_with_fake([Outcome("posted", note="blank")]))
    assert out["ok"] and out["refilled"] == ["category", "brand"] and fake.clicks == 1
    assert fake.jobs[0]["fields"]["category"] == "x"                       # the listing's own fields went with the job


def test_a_refused_save_is_reported_with_its_error_and_nothing_saved():
    out, fake = asyncio.run(_price_with_fake([Outcome("posted", note="refused")]))
    assert fake.clicks == 1 and not out["ok"] and not out["saved"]
    assert out["error"] == "Depop refused the change (Category: This field is required)"


def test_an_edit_page_showing_another_price_is_never_typed_into():
    out, fake = asyncio.run(_price_with_fake([Outcome("posted")], site_price=30.0))
    assert out["mismatch"] == 30 and fake.clicks == 0 and fake.commands[-1]["type"] == "cancel"


def test_a_no_from_the_last_check_is_never_saved():
    out, fake = asyncio.run(_price_with_fake([Outcome("posted")], go=lambda: False))
    assert not out["saved"] and fake.clicks == 0 and out["error"] == "stopped before Save"


def test_vinted_reserved_is_read_from_its_own_data():
    async def go():
        b = await _bridge()
        fake = FakeExtension(b.port, TOKEN, [Outcome("posted")])
        fake.price_on["vinted"], fake.state["vinted"] = 25.0, "reserved"
        fake.start()
        await asyncio.wait_for(fake.connected.wait(), 5)
        try:
            return await ExtensionPoster("vinted", "12345", bridge=b).read_price(None, URLS["vinted"])
        finally:
            fake.stop()
            await b.close()
    assert asyncio.run(go()) == {"unavailable": "reserved", "price": 25.0}


# ---------------------------------------------------------------- the sync and the API's mirror

def test_the_price_changes_reach_the_api_never_a_dry_run(s, db, said, monkeypatch):
    calls = []
    client = SimpleNamespace(post=lambda path, body: calls.append((path, body)) or {"ok": True})
    iid = live(db, {"poshmark": NOW - timedelta(days=31)})
    pricedrop.plan(db, NOW)
    pricedrop.dry_rows(db, iid)
    drop_all(s, db, {"poshmark": Site(25)})
    sales.push(db, client)
    body = {k: v for path, b in calls for k, v in b.items() if path == "/sync"}
    assert [c["stage"] for c in body["price_changes"]] == ["auto_1"]
    assert body["price_changes"][0]["new_price"] == 22
    item = next(i for _, b in calls for i in b["items"] if i["id"] == iid)
    assert item["drop_stage"] == 1 and item["next_drop_at"]
