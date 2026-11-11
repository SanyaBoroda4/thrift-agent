"""WO57 §5/§6 — eBay as the fourth site on the Mac, end to end: the real poster (thrift_agent.ebay.poster), the real
queue, card, price and take-down code, against the fake eBay (tests/ebay_fake.py). No browser, no network.

A listing is validated, created once and read back before the card says live; the card gets its fourth link and each
site's price when eBay's differs; one item never stops the site (rule 4) and what eBay refuses for good is never posted
again; an answer that never came is looked for by SKU, never made twice; a missing consent stops eBay quietly; the
owner's hand-made listings are never touched; "price 35" changes all four sites; a drop recomputes eBay's price; a
listing thrift-api ended is recorded; the backfill lets 25 a day in behind the new shares and resets no clock."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import ebay_fake as fake
from ebay_items import ITEMS, view
from thrift_agent import crosslist, health, humansteps, notify, photocheck, postcheck, pricedrop, sales
from thrift_agent.config import Settings, settings
from thrift_agent.db import DB, loads
from thrift_agent.ebay import backfill, duties, fees
from thrift_agent.ebay import client as api
from thrift_agent.ebay.poster import EbayPoster, listing_id_of, plain_text
from thrift_agent.post import parallel, runner
from thrift_agent.post.base import AccountBlocked, Outcome, PosterError

HOME = {"city": "Springfield", "state": "Illinois", "postal_code": "62701", "country": "US"}
OLD = "2026-10-20T15:00:00+00:00"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Prod-like settings with eBay on and publishing, a database, the chats captured, the API calls captured."""
    base = settings().data
    data = {**base, "machine_role": "prod", "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    data["poster"] = {**base["poster"], "dry_run": False, "autopublish_confirmed": True, "parallel": True}
    mk = {k: dict(v) for k, v in base["marketplaces"].items()}
    mk["poshmark"].update(username="closet", autopublish=True)
    mk["depop"].update(enabled=False)
    mk["vinted"].update(enabled=False)
    mk["ebay"].update(enabled=True, autopublish=True, location=HOME)
    data["marketplaces"] = mk
    data["formcheck"] = {"enabled": False}
    s = Settings(data)
    s.ensure_dirs()
    db = DB(s.path("db"))
    said = SimpleNamespace(ops=[], group=[], api=[])
    monkeypatch.setattr(notify, "say", said.ops.append)
    monkeypatch.setattr(notify, "group", said.group.append)
    monkeypatch.setattr(notify, "ops_photo", lambda path, caption: said.ops.append(caption))
    monkeypatch.setattr(notify, "photo", lambda path, caption: said.ops.append(caption))
    monkeypatch.setattr(sales, "call", lambda db_, path, body: said.api.append((path, body)) or True)
    monkeypatch.setattr(runner, "_paused", lambda db_: None)
    return s, db, said


def tokens(fail=None):
    def fetch(kind):
        if fail is not None:
            raise fail
        return {"access_token": f"tok-{kind}", "expires_at": time.time() + 7200}
    return api.TokenSource(fetch)


def ebay(s, shop=None, **kw) -> tuple[EbayPoster, fake.FakeEbay]:
    shop = shop or fake.FakeEbay(recorded=None)
    client = api.EbayClient(kw.pop("tokens", None) or tokens(), http=shop.client(), cache_dir=s.path("db").parent / "ebay")
    return EbayPoster(s, client=client), shop


def item(s, db, n=1, name="wrap dress", price=50, status="posted", sites=("poshmark",), posted=OLD, **over) -> str:
    """An item of ours as the worker left it: its facts, its Poshmark listing, its photos on disk, her price."""
    bid = db.add_batch(f"/shares/2026-10-20_1200{n:02d}_{n:04d}", 4)
    db.set_batch(bid, status="split")
    folder = s.path("work") / bid / "item_01"
    (folder / "photos").mkdir(parents=True)
    files = []
    for k in range(4):
        p = folder / ("cover.jpg" if k == 0 else f"photos/{k:02d}.jpg")
        p.write_bytes(b"\xff\xd8\xff\xe0" + bytes([n, k]) * 600)
        files.append(str(p))
    iid = db.add_item(bid, 1, str(folder))
    v = view(**{**ITEMS[name][0], **over, "price": price, "iid": iid})
    render = v.render.model_copy(update={"photos": files, "sku": iid})
    db.set_item(iid, status=status, owner_price=price, facts=v.facts.model_dump(),
                renders={"poshmark": render.model_dump()}, gate={"decision": "publish", "reasons": []})
    for mp in sites:
        url = {"poshmark": f"https://poshmark.com/listing/Item-{n}-6acd94d6cbbea59039cf{n:04x}",
               "depop": f"https://www.depop.com/products/shop-item-{n}/",
               "vinted": f"https://www.vinted.com/items/{9000 + n}"}[mp]
        db.upsert_listing(iid, mp, status="posted", url=url, price=price, posted_at=posted)
    return iid


def go(s, db, ps, iid, dry=False):
    return asyncio.run(runner.run_cross(s, db, ps, None, iid, "ebay", dry=dry))


def want(db: DB, iid: str, base: int) -> int:
    """The eBay price the formula gives this listing for a base (its own category and parcel)."""
    f = loads(db.listing(iid, "ebay")["fields_json"])
    return fees.price_for(base, f["category_ids"], f["shipping"]["buyer_pays"], 0.07)


def events(db: DB, kind: str) -> list:
    return [json.loads(r["detail"]) if r["detail"] and r["detail"][:1] in "{[" else r["detail"]
            for r in db.conn.execute("SELECT detail FROM events WHERE kind=? ORDER BY rowid", (kind,))]


# ---------------------------------------------------------------- one listing

def test_an_item_is_validated_created_once_read_back_and_the_card_gets_its_fourth_link(env):
    s, db, said = env
    assert crosslist.enabled(s) == ["ebay"] and crosslist.driver(s, "ebay") == "api" and crosslist.live(s, "ebay")
    iid = item(s, db)
    poster, shop = ebay(s)
    assert crosslist.queue(s, db, iid) == ["ebay"]
    out = go(s, db, {"ebay": poster}, iid)
    row = db.listing(iid, "ebay")
    lid = listing_id_of(row["url"])
    assert out.status == "posted" and row["status"] == "posted" and row["url"] == f"https://www.ebay.com/itm/{lid}"
    assert row["listing_id"] == lid and row["attempts"] == 1
    assert (shop.count("validate"), shop.count("create"), shop.count("image")) == (1, 1, 4)     # every photo, once
    live = shop.listings[lid]
    assert live["sku"] == iid and [i["order"] for i in live["sellerProduct"]["images"]] == [0, 1, 2, 3]
    f = loads(row["fields_json"])
    assert row["price"] == f["price"] == 49 and f["base_price"] == 50                # the same profit as $50 on Poshmark
    assert f["price"] == fees.price_for(50, f["category_ids"], 8.25, 0.07) and f["shipping"]["buyer_pays"] == 8.25
    assert abs(f["quote"]["ebay_payout"] - f["quote"]["poshmark_payout"]) <= 0.5
    assert f["category_id"] == "63861" and f["shipping"]["service_code"] == "USPSParcel"
    assert out.note is None and "created" in poster.lines[-1]                        # read back: nothing differs
    text = crosslist.announce(s, db, iid)
    assert text.split("\n\n")[:3] == [f"✅ {pricedrop.short_name(db, iid)} · $50",  # each site's price: eBay's differs
                                      f'🟣 <a href="{db.listing(iid, "poshmark")["url"]}">Poshmark</a> · $50',
                                      f'🔵 <a href="{row["url"]}">eBay</a> · $49']
    evidence = sorted((s.path("failed") / "shots").glob(f"{iid}-ebay-*.json"))
    assert evidence and "tok-" not in evidence[-1].read_text(encoding="utf-8")       # no token in the evidence


def test_the_card_shows_no_prices_when_ebays_is_the_same(env):
    s, db, said = env
    iid = item(s, db, name="silk blouse", price=20)
    poster, _ = ebay(s)
    crosslist.queue(s, db, iid)
    go(s, db, {"ebay": poster}, iid)
    assert db.listing(iid, "ebay")["price"] == 20
    assert " · $20\n\n🟣 <a" in crosslist.card_text(s, db, iid) and "</a> ·" not in crosslist.card_text(s, db, iid)


def test_without_its_own_autopublish_ebay_only_validates(env):
    s, db, said = env
    s.data["marketplaces"]["ebay"]["autopublish"] = False
    iid = item(s, db)
    poster, shop = ebay(s)
    crosslist.queue(s, db, iid)
    out = go(s, db, {"ebay": poster}, iid, dry=not crosslist.live(s, "ebay"))
    assert out.status == "dryrun" and "nothing created" in out.note and shop.listings == {}
    assert (shop.count("validate"), shop.count("create"), shop.count("image")) == (1, 0, 1)   # the cover only
    assert db.listing(iid, "ebay")["status"] == "dryrun" and any("🧪 dry-run eBay" in x for x in said.ops)


def test_ebay_stays_off_without_the_items_location(env):
    s, db, said = env
    s.data["marketplaces"]["ebay"]["location"] = {"country": "US"}
    assert crosslist.enabled(s) == []
    from thrift_agent import ebay as pkg
    assert "postal_code" in pkg.ready(s)


# ---------------------------------------------------------------- rule 4: one item never stops the site

REFUSAL = {"data": {"createListing": {"__typename": "CreateListingValidationError", "errorCode": "LSTNG",
                                      "errorMessage": "The listing was not created.", "warnings": [],
                                      "violations": [{"violationCode": "LSTNG_BAD_ASPECT", "inputPath": ["product.aspects"],
                                                      "violationDescription": "Dress Length is not valid for this item."}]}}}
POLICY = {"data": {"createListing": {"__typename": "CreateListingValidationError", "errorCode": "LSTNG",
                                     "errorMessage": "The listing was not created.", "warnings": [],
                                     "violations": [{"violationCode": "LSTNG_VERO", "inputPath": ["product.title"],
                                                     "violationDescription": "This brand's items are restricted by the "
                                                                             "VeRO policy for your account."}]}}}


def test_a_refused_item_is_set_aside_and_the_next_one_posts(env):
    s, db, said = env
    a, b = item(s, db, 1), item(s, db, 2, name="silk blouse")
    poster, shop = ebay(s)
    ps = {"ebay": poster}
    crosslist.queue(s, db, a)
    crosslist.queue(s, db, b)
    shop.fail["validate"] = [REFUSAL]
    out = go(s, db, ps, a)
    assert out.status == "failed" and not out.clicked and "Dress Length is not valid" in out.error
    assert db.listing(a, "ebay")["status"] == "failed" and shop.listings == {}
    assert health.kind_of(out.error, poster.error_kind) == health.ITEM              # the item's own, not the site's
    assert go(s, db, ps, b).status == "posted" and crosslist.blocked(db, "ebay") is None   # the site goes straight on
    later = datetime.now(timezone.utc) + timedelta(minutes=31)
    assert crosslist.retry_failed(s, db, at=later) == [(a, "ebay")]                 # tried again by itself
    shop.fail["validate"] = [REFUSAL]
    go(s, db, ps, a)
    crosslist.retry_failed(s, db, at=later + timedelta(minutes=31))
    row = db.listing(a, "ebay")
    assert row["status"] == "failed" and row["error"].startswith(health.SET_ASIDE)  # set aside, never skipped for good
    assert crosslist.blocked(db, "ebay") is None and len(shop.listings) == 1


def test_what_ebay_refuses_for_good_is_recorded_and_never_posted_there_again(env):
    s, db, said = env
    iid = item(s, db)
    poster, shop = ebay(s)
    crosslist.queue(s, db, iid)
    shop.fail["validate"] = [POLICY]
    out = go(s, db, {"ebay": poster}, iid)
    row = db.listing(iid, "ebay")
    assert out.status == "skipped" and row["status"] == "skipped" and "refused by eBay" in row["error"]
    assert [x for x in said.ops if "refused it for good" in x and "VeRO" in x and "stays on the other sites" in x]
    assert said.group == [] and not loads(db.kv_get("alarms") or "{}")              # no alarm: no code change helps
    assert db.listing(iid, "poshmark")["status"] == "posted"                         # the other sites aren't touched
    later = datetime.now(timezone.utc) + timedelta(days=2)
    assert crosslist.retry_failed(s, db, at=later) == [] and crosslist.pending(s, db, ["ebay"]) == []
    sales.repost(db, {"id": "t_1", "item_id": iid, "marketplace": "ebay"})          # the API's safety net keeps it too
    assert db.listing(iid, "ebay")["status"] == "skipped"


def test_a_failure_of_ebays_own_is_never_the_items(env):
    s, db, said = env
    iid = item(s, db)
    poster, shop = ebay(s)
    crosslist.queue(s, db, iid)
    shop.fail["validate"] = [500]
    out = go(s, db, {"ebay": poster}, iid)
    row = db.listing(iid, "ebay")
    assert out.status == "failed" and row["status"] == "queued" and row["attempts"] == 0     # back in line, untouched
    assert health.kind_of(out.error, poster.error_kind) != health.ITEM and shop.listings == {}


# ---------------------------------------------------------------- never twice

def test_an_answer_that_never_came_is_looked_for_by_sku_and_never_made_twice(env, monkeypatch):
    s, db, said = env
    iid = item(s, db)
    poster, shop = ebay(s)
    ps = {"ebay": poster}
    crosslist.queue(s, db, iid)
    shop.fail["create"] = ["lost"]                                                   # eBay made it; we never heard
    out = go(s, db, ps, iid)
    row = db.listing(iid, "ebay")
    assert out.status == "failed" and out.clicked and row["status"] == "failed"
    assert row["error"].startswith(crosslist.UNCONFIRMED) and len(shop.listings) == 1
    assert "verifying" in crosslist.wait_line(db, iid, "ebay", row, s) and said.group == []
    assert crosslist.blocked(db, "ebay") is None                                     # verifying is no failure
    soon = postcheck.now() + timedelta(minutes=3)
    monkeypatch.setattr(postcheck, "now", lambda: soon)
    assert asyncio.run(runner.recheck_unconfirmed(s, db, ps, "ebay")) is True
    row = db.listing(iid, "ebay")
    assert row["status"] == "posted" and listing_id_of(row["url"]) in shop.listings
    assert len(shop.listings) == 1 and shop.count("create") == 1                     # never a second listing


def test_a_listing_already_there_is_recorded_not_made_again(env):
    """The poster died after eBay made the listing and before it was recorded: the next try finds it by its SKU."""
    s, db, said = env
    iid = item(s, db)
    poster, shop = ebay(s)
    crosslist.queue(s, db, iid)
    f = asyncio.run(poster.plan(runner.ItemView.from_row(db.item(iid))))
    made = asyncio.run(poster.client.create(f, ["https://i.ebayimg.com/x.jpg"], api.idempotency_key(iid)))
    out = go(s, db, {"ebay": poster}, iid)
    assert out.status == "posted" and listing_id_of(out.url) == made.listing_id and len(shop.listings) == 1


def test_a_listing_ebay_removed_is_never_followed_by_another(env):
    s, db, said = env
    iid = item(s, db)
    poster, shop = ebay(s)
    crosslist.queue(s, db, iid)
    first = listing_id_of(go(s, db, {"ebay": poster}, iid).url)
    shop.remove(first)
    db.upsert_listing(iid, "ebay", status="queued", url=None)                        # someone requeues it
    out = go(s, db, {"ebay": poster}, iid)
    assert out.status == "skipped" and "refused by eBay" in out.error and "removed" in out.error
    assert len(shop.listings) == 1


# ---------------------------------------------------------------- the consent, her own listings

def test_a_missing_consent_stops_ebay_quietly(env):
    s, db, said = env
    iid = item(s, db)
    poster, shop = ebay(s, tokens=tokens(api.NotConnected("eBay isn't connected: thrift-api has no working consent")))
    assert poster.available() is False and "consent" in poster.why_unavailable()
    assert duties.note_link(db, poster) is False and crosslist.reachable(s, db) == []
    good, shop = ebay(s)
    crosslist.queue(s, db, iid)
    shop.fail["validate"] = [401, 401]                                               # the token refused twice
    out = go(s, db, {"ebay": good}, iid)
    assert out is None and db.listing(iid, "ebay")["status"] == "queued"
    assert health.account_paused(db, "ebay") and said.group == []                    # paused; the group hears nothing
    assert not humansteps.pending(db, "ebay") and not [x for x in said.ops if "show" in x or "🔐" in x]
    assert events(db, "crosslist_blocked")[-1]["human"] == "thrift-api"              # its message has the link


def test_the_owners_hand_made_listings_are_never_touched(env):
    s, db, said = env
    shop = fake.FakeEbay(recorded=None, seller_listings=[fake.hand_listing("Her own listing"),
                                                         fake.hand_listing("Another by hand")])
    hers = list(shop.listings)
    poster, _ = ebay(s, shop)
    iid = item(s, db)
    crosslist.queue(s, db, iid)
    go(s, db, {"ebay": poster}, iid)
    bulk = asyncio.run(poster.read_shop())
    assert [x["sku"] for x in bulk["listings"]] == [iid] and bulk["total"] == 3 and "left alone" in bulk["note"]
    url = f"https://www.ebay.com/itm/{hers[0]}"
    for call in (poster.delist(url), poster.set_price(None, url, 25, 20), poster.read_price(None, url),
                 poster.set_text(url, {"title": "x"}), poster.add_photos(url, ["a.jpg"])):
        with pytest.raises(PosterError, match="isn't one of the agent's"):
            asyncio.run(call)
    assert shop.listings[hers[0]]["listingTerms"]["lifecycleTerms"]["listingStatus"] == "ACTIVE"
    assert shop.count("end") == 0 and shop.count("price") == 0 and shop.count("update") == 0
    states = asyncio.run(poster.listing_states([{"key": hers[0]}]))["states"]
    assert states[hers[0]]["state"] == "unknown"                                     # never read as one of ours


# ---------------------------------------------------------------- prices

class Site:
    """A stand-in for another site's poster: its listing's price read and set."""

    def __init__(self, price):
        self.price, self.sets = float(price), []

    async def read_price(self, ctx, url):
        return {"price": self.price}

    async def set_price(self, ctx, url, old, new, dry=False, shots=None, go=None, fields=None):
        if go is not None and not go():
            return {"saved": False}
        self.price = float(new)
        self.sets.append(new)
        return {"saved": True, "live_price": new, "ok": True}


def four_sites(s, db, price=50):
    iid = item(s, db, name="silk blouse", price=price, sites=("poshmark", "depop", "vinted"))
    poster, shop = ebay(s)
    crosslist.queue(s, db, iid)
    go(s, db, {"ebay": poster}, iid)
    ps = {"poshmark": Site(price), "depop": Site(price), "vinted": Site(price), "ebay": poster}
    return iid, ps, shop


def apply_all(s, db, ps):
    for row in db.conn.execute("SELECT * FROM price_changes WHERE status='pending' ORDER BY rowid").fetchall():
        asyncio.run(pricedrop.apply(s, db, row, ps[row["marketplace"]], None, list(ps)))


def test_price_35_changes_all_four_sites(env):
    s, db, said = env
    iid, ps, shop = four_sites(s, db, 50)
    lid = listing_id_of(db.listing(iid, "ebay")["url"])
    assert len(pricedrop.owner_price(db, iid, 35)) == 4
    apply_all(s, db, ps)
    assert [ps[m].price for m in ("poshmark", "depop", "vinted")] == [35.0, 35.0, 35.0]
    row = db.listing(iid, "ebay")
    assert want(db, iid, 35) == 34 and row["price"] == 34 and loads(row["fields_json"])["base_price"] == 35
    assert float(shop.listings[lid]["items"][0]["itemTerms"]["pricingTerms"]["fixedPrice"]["original"]["value"]) == 34.0
    assert db.item(iid)["owner_price"] == 35
    assert said.ops[-1] == f"✓ {pricedrop.short_name(db, iid)}: $35 on Poshmark, Depop, Vinted, eBay $34"
    assert "🔵" in crosslist.card_text(s, db, iid) and "eBay</a> · $34" in crosslist.card_text(s, db, iid)


def test_a_price_drop_recomputes_ebays_price_from_the_new_base(env):
    s, db, said = env
    iid, ps, shop = four_sites(s, db, 50)
    assert pricedrop.plan(db, item=iid, force=True) == [f"{iid}:auto_1"]
    apply_all(s, db, ps)
    assert ps["poshmark"].price == 45.0                                               # 10% off her price
    row = db.listing(iid, "ebay")
    f = loads(row["fields_json"])
    assert f["base_price"] == 45 and f["price"] == want(db, iid, 45) == 44          # the same profit as $45 on Poshmark
    done = {r["marketplace"]: (r["old_price"], r["new_price"]) for r in db.conn.execute(
        "SELECT * FROM price_changes WHERE status='done'")}
    assert done["ebay"] == (48.0, 44.0) and done["poshmark"] == (50.0, 45.0)
    assert pricedrop.announce(s, db) == (f"🔻 Prices down 10% today: {pricedrop.short_name(db, iid)} $50 → $45 "
                                         f"(eBay $48 → $44).")


def test_a_price_she_lowered_on_ebay_by_hand_drops_from_there_and_is_never_raised(env):
    s, db, said = env
    iid, ps, shop = four_sites(s, db, 50)
    lid = listing_id_of(db.listing(iid, "ebay")["url"])
    shop.hand_edit(lid, price="40.00")                                                # her own change, in eBay's app
    pricedrop.plan(db, item=iid, force=True)
    apply_all(s, db, ps)
    assert float(shop.listings[lid]["items"][0]["itemTerms"]["pricingTerms"]["fixedPrice"]["original"]["value"]) == 36.0


# ---------------------------------------------------------------- a sale elsewhere, the sale net

class Api:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def get(self, path, params=None):
        self.asked.append((path, params))
        return self.answer


def test_a_listing_thrift_api_ended_is_recorded_and_ebays_take_downs_are_never_asked_for(env, monkeypatch):
    s, db, said = env
    iid, ps, shop = four_sites(s, db)
    client = Api({"tasks": [], "sold": [iid], "sold_on": [{"item": iid, "site": "depop"}],
                  "ended": [{"item": iid, "site": "ebay", "result": "done", "at": "2026-11-11T15:00:00+00:00"}]})
    monkeypatch.setattr(sales, "api", lambda: client)
    sales.fetch_tasks(db, ["poshmark", "depop", "vinted", "ebay"], force=True)
    assert client.asked == [("/tasks", {"sites": "poshmark,depop,vinted"})]           # never eBay's: they are Azure's
    assert db.listing(iid, "ebay")["status"] == "delisted" and db.listing(iid, "depop")["status"] == "sold"
    assert events(db, "listing_ended_by_api") == [{"site": "ebay", "result": "done", "at": "2026-11-11T15:00:00+00:00",
                                                    "was": "posted"}]
    sales.fetch_tasks(db, ["poshmark"], force=True)                                   # the same answer again: nothing
    assert len(events(db, "listing_ended_by_api")) == 1
    assert pricedrop.why_not(db, db.item(iid)) is not None                            # no drop for a sold item


def test_the_photo_check_reads_ebay_by_api_a_sale_and_a_removal(env):
    s, db, said = env
    a, b, c = item(s, db, 1), item(s, db, 2, name="silk blouse"), item(s, db, 3, name="jeans")
    poster, shop = ebay(s)
    ps = {"ebay": poster}
    for iid in (a, b, c):
        crosslist.queue(s, db, iid)
        go(s, db, ps, iid)
        db.upsert_listing(iid, "ebay", posted_at=OLD)
    db.conn.execute("UPDATE listings SET photos_read_at=? WHERE marketplace='poshmark'",
                    (datetime.now(timezone.utc).isoformat(),))
    lid = {iid: listing_id_of(db.listing(iid, "ebay")["url"]) for iid in (a, b, c)}
    asyncio.run(photocheck.run(s, db, ps))
    assert [db.listing(i, "ebay")["photos_live"] for i in (a, b, c)] == [4, 4, 4]     # every photo is there
    shop.sell(lid[a])
    shop.remove(lid[b])
    shop.hand_edit(lid[c], price="44.00")
    db.conn.execute("UPDATE listings SET photos_read_at=NULL WHERE marketplace='ebay'")
    db.kv_set(photocheck.BULK_AT, "{}")
    asyncio.run(photocheck.run(s, db, ps))
    assert db.listing(a, "ebay")["status"] == "sold"                                  # the sale net: thrift-api is told
    assert [(p, body["kind"], body["marketplace"]) for p, body in said.api] == [("/mac-event", "sold_found", "ebay")]
    assert db.listing(b, "ebay")["status"] == "posted"                                # a removal acts when seen again
    absent = loads(db.kv_get(photocheck.ABSENT))
    absent[f"{b}|ebay"]["checked"] = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    db.kv_set(photocheck.ABSENT, json.dumps(absent))
    seen = loads(db.kv_get(photocheck.GONE_SEEN))                                     # eleven minutes on, gone still
    db.kv_set(photocheck.GONE_SEEN, json.dumps({k: float(v) - 660 for k, v in seen.items()}))
    asyncio.run(photocheck.run(s, db, ps))
    row = db.listing(b, "ebay")
    assert row["status"] == "delisted" and row["error"].startswith("gone from eBay") and "not relisted" in row["error"]
    assert db.listing(b, "poshmark")["status"] == "posted" and len(said.api) == 1     # the other sites aren't touched
    assert [x for x in said.ops if "eBay no longer has" in x]
    assert db.listing(c, "ebay")["price_live"] == 44.0                                # her change: read, never undone
    assert events(db, "price_differs")[-1] == {"site": "ebay", "site_price": 44.0, "ours": 49.0}
    assert shop.count("price") == 0


# ---------------------------------------------------------------- changes to a live listing

def test_late_photos_a_new_title_and_a_brand_reach_the_live_listing_and_her_own_title_is_left(env, tmp_path):
    s, db, said = env
    iid = item(s, db)
    poster, shop = ebay(s)
    crosslist.queue(s, db, iid)
    url = go(s, db, {"ebay": poster}, iid).url
    lid = listing_id_of(url)
    extra = tmp_path / "late.jpg"
    extra.write_bytes(b"\xff\xd8\xff\xe0" + b"9" * 900)
    out = asyncio.run(poster.add_photos(url, [str(extra)], sku=iid, had=4))
    assert out == {"ok": True, "saved": True, "after": 5} and len(shop.listings[lid]["sellerProduct"]["images"]) == 5
    old = shop.listings[lid]["sellerProduct"]["title"]
    out = asyncio.run(poster.set_text(url, {"title": "J.Crew Wrap Midi Dress Navy Blue size 8", "expect_title": old}, sku=iid))
    assert out == {"ok": True, "saved": True}
    assert asyncio.run(poster.read_text(url, None, iid))["title"].endswith("Navy Blue size 8")
    shop.hand_edit(lid, title="Her own wording")                                      # she changed it by hand since
    out = asyncio.run(poster.set_text(url, {"title": "Ours again", "expect_title": "J.Crew Wrap Midi Dress Navy Blue size 8"},
                                      sku=iid))
    assert out["saved"] is False and out["mismatch"] == "Her own wording"             # left as it is
    assert asyncio.run(poster.set_fields(url, {"brand": "Madewell"}, sku=iid)) == {"ok": True, "saved": True}
    assert asyncio.run(poster.read_fields(url, {"brand": "Madewell"}, None, iid)) == {"ok": True}
    assert plain_text("<p>One &amp; two.<br>Three.</p><p>Four.</p>") == "One & two.\nThree.\n\nFour."


def test_a_take_down_by_hand_ends_only_our_listing(env):
    s, db, said = env
    iid = item(s, db)
    poster, shop = ebay(s)
    crosslist.queue(s, db, iid)
    url = go(s, db, {"ebay": poster}, iid).url
    assert asyncio.run(poster.delist(url)) is True and asyncio.run(poster.delist(url)) is None    # gone already
    assert asyncio.run(poster.delist("https://www.ebay.com/itm/999999999999")) is None
    other = item(s, db, 2, name="jeans")
    crosslist.queue(s, db, other)
    sold = go(s, db, {"ebay": poster}, other).url
    shop.sell(listing_id_of(sold))
    assert asyncio.run(poster.delist(sold)) == "sold"                                 # sold there: nothing is ended


# ---------------------------------------------------------------- the backfill

def test_the_backfill_lets_25_a_day_in_oldest_first_behind_the_new_shares(env):
    s, db, said = env
    old = [item(s, db, n, posted=f"2026-10-{13 + n % 15:02d}T12:00:{n:02d}+00:00") for n in range(1, 31)]
    before = item(s, db, 40, posted="2026-10-01T12:00:00+00:00")                      # before the day: not planned
    sold = item(s, db, 41)
    db.upsert_listing(sold, "depop", status="sold", url="https://www.depop.com/products/x/", posted_at=OLD)
    test = item(s, db, 42)
    db.set_item(test, test=1)
    plan = backfill.plan(s, db)
    assert len(plan) == 30 and set(plan) == set(old) and before not in plan and sold not in plan and test not in plan
    first = min(old, key=lambda i: db.listing(i, "poshmark")["posted_at"])
    assert plan[0] == first and backfill.plan(s, db) == plan                          # the same plan again
    day1 = datetime(2026, 11, 11, 20, 0, tzinfo=timezone.utc)
    read = lambda url: (True, 40 if url == db.listing(first, "poshmark")["url"] else None)   # noqa: E731
    queued = [backfill.tick(s, db, day1 + timedelta(seconds=n), read) for n in range(40)]
    assert [q for q in queued if q] == plan[:25] and queued[25:] == [None] * 15       # 25 a day, the oldest first
    assert backfill.base(db, first) == 40 and backfill.base(db, plan[1]) == 50        # its price on Poshmark that day
    more = [backfill.tick(s, db, day1 + timedelta(days=1, seconds=n), read) for n in range(10)]
    assert [q for q in more if q] == plan[25:] and backfill.status(db)["waiting"] == 0
    new = item(s, db, 50, status="ready", sites=())                                   # a new share, priced just now
    crosslist.queue(s, db, new)
    assert crosslist.pending(s, db, ["ebay"])[0] == (new, "ebay")                     # new shares always go first
    poster, shop = ebay(s)
    out = go(s, db, {"ebay": poster}, first)
    assert out.status == "posted" and loads(db.listing(first, "ebay")["fields_json"])["base_price"] == 40
    assert db.listing(first, "ebay")["price"] == want(db, first, 40)


def test_a_backfilled_listing_never_resets_the_price_drop_clock(env):
    s, db, said = env
    iid = item(s, db, posted="2026-10-15T12:00:00+00:00")
    db.set_item(iid, status="posted")
    now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    pricedrop.plan(db, now)
    was = (db.item(iid)["all_live_at"], db.item(iid)["next_drop_at"])
    assert was[0] and was[1] == "2026-11-14T12:00:00+00:00"                           # 30 days after it went live
    backfill.plan(s, db)
    assert backfill.tick(s, db, now, lambda url: (True, None)) == iid
    pricedrop.plan(db, now)
    assert (db.item(iid)["all_live_at"], db.item(iid)["next_drop_at"]) == was         # a queued eBay row: no change
    poster, _ = ebay(s)
    go(s, db, {"ebay": poster}, iid)                                                  # live on eBay today
    pricedrop.plan(db, now + timedelta(hours=1))
    assert (db.item(iid)["all_live_at"], db.item(iid)["next_drop_at"]) == was         # … and still no reset
    fresh = item(s, db, 2, status="posted", posted="2026-11-10T12:00:00+00:00")       # a new item: eBay counts
    db.upsert_listing(fresh, "ebay", status="posted", url="https://www.ebay.com/itm/256000009999",
                      posted_at="2026-11-11T09:00:00+00:00", price=48)
    pricedrop.plan(db, now)
    assert db.item(fresh)["all_live_at"] == "2026-11-11T09:00:00+00:00"


# ---------------------------------------------------------------- the month's free listings, the fee file, doctor

def test_the_first_listing_that_costs_is_said_once_a_month(env):
    s, db, said = env
    a, b = item(s, db, 1), item(s, db, 2, name="jeans")
    poster, shop = ebay(s)
    shop.insertion_fee = "0.35"
    for iid in (a, b):
        crosslist.queue(s, db, iid)
        go(s, db, {"ebay": poster}, iid)
    assert [x for x in said.ops if "costs" in x] == ["From now each new eBay listing costs $0.35 this month."]
    assert duties.note_fee(s, db, 0.35, datetime(2026, 12, 2, tzinfo=timezone.utc)) is True       # a new month
    assert duties.free_used(s, db) == 2 + 10                                          # ours + her ten by hand


def test_the_fee_files_age_and_the_heartbeat_and_doctor(env):
    s, db, said = env
    from thrift_agent import doctor
    table = fees.load()
    assert duties.note_stale(db, table.checked) is False
    late = table.checked + timedelta(days=90)
    assert duties.note_stale(db, late) is True and duties.note_stale(db, late) is False          # once
    assert "last checked against eBay's fee page 90 days ago" in said.ops[-1]
    poster, _ = ebay(s)
    assert duties.tick(s, db, {"ebay": poster}, dry=True)["connected"] is True
    beat = duties.beat(s, db)
    assert beat["on"] and beat["live"] and beat["connected"] and beat["fees_checked"] == table.checked.isoformat()
    assert beat["free_used"] == 10 and beat["free_month"] == duties.month_of(s)
    lines = "\n".join(doctor.report(s, db))
    assert "eBay API: connected" in lines and f"fee table checked {table.checked:%b %d, %Y}" in lines
    assert "of 250 free listings used this month" in lines
    s.data["marketplaces"]["ebay"]["enabled"] = False
    assert duties.beat(s, db) == {"on": False} and "eBay" not in "\n".join(doctor.report(s, db))


# ---------------------------------------------------------------- the fourth site worker

class Posh:
    name, driver = "poshmark", "stub"

    def __init__(self):
        self.calls, self.held = [], set()

    async def post(self, ctx, r, mode, dry_run, shots, stage="form"):
        self.calls.append(r.sku)
        return Outcome("posted", url=f"https://poshmark.com/listing/{r.sku}", clicked=True)

    async def find_live(self, ctx, r, since, created=None):
        return None, {}


def test_the_fourth_site_worker_posts_beside_poshmark_and_the_card_has_both_links(env, monkeypatch):
    s, db, said = env
    monkeypatch.setattr(parallel, "IDLE", 0.05)
    monkeypatch.setattr(parallel, "HOUSEKEEPING", 0.05)
    monkeypatch.setattr(parallel, "next_gap", lambda schedule: 0.0)
    monkeypatch.setattr(parallel.crosslist, "listing_gap", lambda s_, mp: 0.0)
    monkeypatch.setattr(sales, "api", lambda: None)

    class Ctx:
        async def close(self):
            pass

        async def new_page(self):
            raise AssertionError("no page is opened for eBay")

    class PW:
        async def stop(self):
            pass

    async def opener(profile, tz):
        return PW(), Ctx()
    a, b = item(s, db, 1, status="ready", sites=()), item(s, db, 2, name="handbag", price=100, status="ready", sites=())
    poster, shop = ebay(s)
    ps = {"poshmark": Posh(), "ebay": poster}
    asyncio.run(parallel.run_parallel(s, db, ps, dry=False, stage="form", opener=opener, until_idle=True))
    for iid in (a, b):
        assert db.listing(iid, "poshmark")["status"] == "posted" and db.listing(iid, "ebay")["status"] == "posted"
    assert sorted(x["sku"] for x in shop.listings.values()) == sorted([a, b]) and shop.count("create") == 2
    assert db.listing(b, "ebay")["price"] == 98                                       # the handbag: the 15% rule
    cards = [r["text"] for r in db.conn.execute("SELECT text FROM outbox WHERE kind='posted' ORDER BY rowid")]
    assert len(cards) == 2 and all("🟣" in c and "🔵" in c and "eBay</a>" in c for c in cards)
    assert (loads(db.kv_get(crosslist.EBAY_LINK)) or {}).get("connected") is True
    with pytest.raises(AccountBlocked):
        raise AccountBlocked("x", page="login")


# ---------------------------------------------------------------- our records against eBay's own

def test_a_live_listing_of_ours_that_isnt_recorded_is_recorded_and_two_for_one_item_are_reported(env):
    s, db, said = env
    a, b = item(s, db, 1), item(s, db, 2, name="jeans")
    shop = fake.FakeEbay(recorded=None, seller_listings=[fake.hand_listing("Her own listing")])
    poster, _ = ebay(s, shop)
    ps = {"ebay": poster}
    for iid in (a, b):                                   # eBay made both; the poster died before recording `a`
        f = asyncio.run(poster.plan(runner.ItemView.from_row(db.item(iid))))
        asyncio.run(poster.client.create(f, ["https://i.ebayimg.com/x.jpg"], api.idempotency_key(iid)))
    crosslist.queue(s, db, a)
    db.upsert_listing(a, "ebay", status="posting")
    crosslist.queue(s, db, b)
    go(s, db, ps, b)                                     # `b` is found by its SKU and recorded the ordinary way
    assert duties.reconcile_due(db, time.time())
    out = asyncio.run(duties.reconcile(s, db, ps))
    assert out["adopted"] == [a] and not duties.reconcile_due(db, time.time())
    row = db.listing(a, "ebay")
    assert row["status"] == "posted" and listing_id_of(row["url"]) in shop.listings and row["price"] == 49
    assert [x for x in said.ops if x.startswith("🔗 eBay:") and "was live but not recorded" in x]
    f = asyncio.run(poster.plan(runner.ItemView.from_row(db.item(b))))
    asyncio.run(poster.client.create(f, ["https://i.ebayimg.com/y.jpg"], api.idempotency_key(b, 7)))   # a second one
    again = asyncio.run(duties.reconcile(s, db, ps))
    assert again == {"adopted": [], "double": [b]}
    assert [x for x in said.ops if x.startswith("⚠️ Two live eBay listings for") and "nothing ended" in x]
    assert asyncio.run(duties.reconcile(s, db, ps)) == {"adopted": [], "double": []}       # each line once
    assert shop.count("end") == 0 and len(shop.listings) == 4                                # hers untouched, none ended


# ---------------------------------------------------------------- `thrift ebay …`

def test_the_ebay_commands(env, monkeypatch):
    from typer.testing import CliRunner

    from thrift_agent import cli
    from thrift_agent.ebay import cli as ebay_cli
    s, db, said = env
    monkeypatch.setattr(ebay_cli, "_s", lambda: s)
    monkeypatch.setattr(ebay_cli, "_db", lambda: db)
    monkeypatch.setattr(sales, "api", lambda: None)
    run = CliRunner().invoke
    out = run(cli.app, ["ebay", "price", "10", "20", "50", "--shipping", "6"])
    assert out.exit_code == 0, out.output
    assert "$48" in out.output and "$40.00" in out.output and "fee table checked" in out.output
    live = [item(s, db, n, posted=f"2026-10-2{n}T12:00:00+00:00") for n in (1, 2, 3)]
    out = run(cli.app, ["ebay", "price", "--items", "3"])
    assert out.exit_code == 0 and out.output.count("$50") >= 3, out.output
    out = run(cli.app, ["ebay", "backfill", "--dry-run"])
    assert out.exit_code == 0 and "would plan: 3 items" in out.output and backfill.status(db)["planned"] == 0
    out = run(cli.app, ["ebay", "backfill"])
    assert out.exit_code == 0 and "planned: 3 items" in out.output and backfill.status(db)["planned"] == 3
    out = run(cli.app, ["ebay", "status"])
    assert out.exit_code == 0 and "eBay: ON" in out.output and "ready: yes" in out.output and "3 planned" in out.output
    out = run(cli.app, ["ebay", "spike", live[0]])               # production: refused — the sandbox only
    assert out.exit_code == 1 and "sandbox only" in out.output
    s.data["machine_role"] = "dev"
    out = run(cli.app, ["ebay", "first", live[0], "--yes"])      # the dev machine never makes a real listing
    assert out.exit_code == 1 and "on the Mac only" in out.output
    out = run(cli.app, ["poster", "--publish-first", live[0], "--marketplace", "ebay"])
    assert out.exit_code != 0 or "thrift ebay first" in out.output
