"""WO57 §1/§6 — eBay's API calls against the fake eBay (tests/ebay_fake.py): tokens from thrift-api, the category data,
photos, and the Listing API end to end — validate, create, read back, change the price, change the listing, end —
with every kind of failure told apart. No network."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

import ebay_fake as fake
from ebay_items import IID, ITEMS, view
from thrift_agent.ebay import client as ebay
from thrift_agent.ebay import mapping


def tokens(log: list | None = None, life: float = 7200.0) -> ebay.TokenSource:
    def fetch(kind: str) -> dict:
        if log is not None:
            log.append(kind)
        return {"access_token": f"tok-{kind}-{len(log) if log is not None else 0}", "expires_at": time.time() + life}
    return ebay.TokenSource(fetch)


def make(shop: fake.FakeEbay | None = None, **kw) -> tuple[ebay.EbayClient, fake.FakeEbay]:
    shop = shop or fake.FakeEbay(recorded=None)
    return ebay.EbayClient(kw.pop("tokens", None) or tokens(), http=shop.client(), **kw), shop


async def fields_for(c: ebay.EbayClient, name: str = "wrap dress", **over) -> mapping.EbayFields:
    v = view(**{**ITEMS[name][0], **over})
    category, _ = await c.category_for(v)
    f = mapping.map_ebay(v, category, mapping.Config(city="Springfield", state="Illinois", postal_code="62701"))
    f.shipping["service_code"] = await c.service_code(f.shipping["service"], f.shipping["service_code"])
    return f


URLS = ["https://i.ebayimg.com/a.jpg", "https://i.ebayimg.com/b.jpg"]
KEY = ebay.idempotency_key(IID)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- tokens

def test_a_token_is_kept_until_two_minutes_before_its_end_and_never_asked_twice_meanwhile():
    log: list = []
    t = tokens(log)
    assert t.token("user") == t.token("user") == "tok-user-1" and log == ["user"]
    assert t.token("app") == "tok-app-2" and log == ["user", "app"]                  # each kind its own
    assert t.token("user", fresh=True) == "tok-user-3"                                # asked for anew after a 401
    short = tokens(log2 := [], life=100)                                              # ends inside the two minutes
    short.token("user")
    short.token("user")
    assert log2 == ["user", "user"]
    assert t.connected is True


def test_no_consent_is_not_connected_and_says_so():
    def none(kind):
        raise ebay.NotConnected("eBay isn't connected: thrift-api has no working consent")
    t = ebay.TokenSource(none)
    with pytest.raises(ebay.NotConnected):
        t.token("user")
    assert t.connected is False and "consent" in t.why


def test_the_idempotency_key_is_32_hex_and_the_same_for_the_same_listing():
    assert KEY == ebay.idempotency_key(IID) and len(KEY) == 32 and int(KEY, 16) >= 0 and KEY == KEY.upper()
    assert ebay.idempotency_key(IID, 1) != KEY and ebay.idempotency_key("i_261110_ffffff") != KEY


# ---------------------------------------------------------------- the category data, photos

def test_category_data_is_asked_once_and_kept_beside_the_database(tmp_path):
    async def go():
        c, shop = make(cache_dir=tmp_path / "ebay")
        first = await fields_for(c)
        again = await fields_for(c)
        assert first == again and first.category_id == "63861"
        assert shop.count("aspects") == 1 and shop.count("conditions") == 1 and shop.count("services") == 1
        assert shop.count("suggest") == 1                                 # the first query named the category
        assert (tmp_path / "ebay" / "aspects_63861.json").is_file()
        assert {t for t in shop.tokens_seen} == {"tok-app-0"}             # the application token, never hers
        other, shop2 = make(cache_dir=tmp_path / "ebay")                  # another process: read from the files
        await fields_for(other)
        assert shop2.calls == []
    run(go())


def test_the_shipping_service_code_is_ebays_own_never_typed_from_memory():
    async def go():
        c, shop = make()
        shop.ground_code = "USPSGroundAdvantageX"
        assert await c.service_code("USPS Ground Advantage", "Guess") == "USPSGroundAdvantageX"
        assert await c.service_code("Pony Express", "Guess") == "Guess"   # not on its list: the file's, eBay decides
        shop.fail["services"] = [500]
        fresh, _ = make(shop)
        assert await fresh.service_code("USPS Ground Advantage", "Guess") == "Guess"
    run(go())


def test_a_photo_goes_up_as_bytes_under_the_image_key(tmp_path):
    async def go():
        photo = tmp_path / "cover.jpg"
        photo.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 2048)
        c, shop = make()
        url = await c.upload_image(photo)
        assert url.startswith("https://i.ebayimg.com/") and shop.images == [url]
        assert shop.calls[-1] == ("image", {"bytes": shop.calls[-1][1]["bytes"]}) and shop.calls[-1][1]["bytes"] > 2048
        assert "tok-user-0" in shop.tokens_seen                           # her token (scope sell.inventory)
        with pytest.raises(ebay.EbayError) as e:
            await c.upload_image(tmp_path / "missing.jpg")
        assert e.value.kind == ebay.ITEM
        shop.fail["image"] = [500]
        with pytest.raises(ebay.EbayError) as e:
            await c.upload_image(photo)
        assert e.value.kind == ebay.SITE and e.value.status == 500
    run(go())


# ---------------------------------------------------------------- the listing: sandbox-shaped, end to end

def test_validate_create_read_back_change_price_and_end():
    async def go():
        c, shop = make()
        f = await fields_for(c)
        checked = await c.create(f, URLS, KEY, validate=True)             # eBay's VALIDATE: nothing is created
        assert checked.validated and checked.listing_id is None and shop.listings == {}
        made = await c.create(f, URLS, KEY)
        lid = made.listing_id
        assert lid and lid.isdigit() and made.live.url == f"https://www.ebay.com/itm/{lid}"
        live = await c.read_one(lid)
        assert live.for_sale and live.status == "ACTIVE" and live.sku == IID
        assert (live.title, live.price, live.category_id, live.condition_id) == (f.title, float(f.price), "63861", "3000")
        assert live.photos == URLS and live.aspects["Brand"] == ["J.Crew"] and live.aspects["Size"] == ["8"]
        assert (live.format, live.duration, live.available, live.best_offer) == ("FIXED_PRICE", "GTC", 1, True)
        assert (live.postal_code, live.country) == ("62701", "US")
        assert (live.returns_accepted, live.returns_days, live.returns_payer) == (True, "DAYS_30", "Buyer")  # eBay's case
        from thrift_agent.ebay.poster import differences
        assert differences(f, live, len(URLS)) == []          # live 2026-11-11: "Buyer" read for BUYER is no difference
        assert (live.shipping_type, live.shipping_service, live.handling) == ("CALCULATED", "USPSParcel", "DAYS_3")
        assert await c.set_price(lid, 31) == []
        assert (await c.read_one(lid)).price == 31.0
        ended = await c.end(lid)
        assert ended.status == "ENDED" and ended.end_reason == "NOT_AVAILABLE" and not ended.for_sale
        with pytest.raises(ebay.EbayError, match="already ended"):
            await c.end(lid)
        with pytest.raises(ebay.EbayError, match="not active"):
            await c.set_price(lid, 20)
        assert await c.read_one("999") is None
    run(go())


def test_what_createlisting_carries():
    async def go():
        c, _ = make()
        f = await fields_for(c)
        inp = ebay.create_input(f, URLS, KEY)
        assert inp["idempotencyKey"] == KEY and inp["marketplace"] == "EBAY_US" and inp["sku"] == IID
        item = inp["listingItems"]["items"][0]
        assert item["sku"] == IID and item["itemTerms"]["availability"] == {"quantityAvailable": 1}
        assert item["itemTerms"]["pricingTerms"] == {"fixedPrice": f"{f.price:.2f}"}
        terms = inp["listingTerms"]
        assert terms["listingFormat"] == "FIXED_PRICE" and terms["lifecycleTerms"] == {"listingDurationInDays": "GTC"}
        assert terms["itemLocation"] == {"country": "US", "postalCode": "62701", "location": "Springfield, Illinois"}
        assert terms["bestOfferTerms"] == {"bestOfferEnabled": True}
        assert terms["returnTerms"] == {"domesticReturnTerms": {"returnAccepted": True, "returnDuration": "DAYS_30",
                                                                "refundMethod": "MONEY_BACK",
                                                                "returnShipmentInvoicee": "BUYER"}}
        ship = terms["fulfillmentTerms"]
        assert ship["shippingTerms"]["marketplaceShippingOptions"] == {"costType": "CALCULATED"}
        assert ship["shippingTerms"]["shippingServices"] == [{"shippingServiceCode": "USPSParcel", "rank": 1}]
        assert ship["shippingTerms"]["handlingTimeDuration"] == "DAYS_3"
        assert ship["packageSpecifications"]["packageWeight"] == {"majorUnit": 1, "minorUnit": 0}
        assert "businessPolicyId" not in json.dumps(inp)                  # terms inline: no business policies
        assert inp["product"]["description"].startswith("<p>") and inp["product"]["imageUrls"] == URLS
        assert "options" not in inp and ebay.create_input(f, URLS, KEY, True)["options"] == {"operationMode": "VALIDATE"}
        f.shipping.update(type="flat", buyer_pays=7.5)
        flat = ebay.create_input(f, URLS, KEY)["listingTerms"]["fulfillmentTerms"]["shippingTerms"]
        assert flat["marketplaceShippingOptions"] == {"costType": "FLAT"}
        assert flat["shippingServices"][0]["shippingCost"] == "7.50"
    run(go())


def test_the_same_call_again_never_makes_a_second_listing():
    """A createListing whose answer was lost: eBay did make it. The same idempotency key gives the same listing, and
    the SKU finds it — it is never made again blind."""
    async def go():
        c, shop = make()
        f = await fields_for(c)
        shop.fail["create"] = ["lost"]
        with pytest.raises(ebay.EbayError) as e:
            await c.create(f, URLS, KEY)
        assert e.value.kind == ebay.INFRA and e.value.unknown             # it may be live
        found = await c.by_sku(IID)
        assert len(found) == 1 and found[0].for_sale
        again = await c.create(f, URLS, KEY)
        assert again.listing_id == found[0].listing_id and len(shop.listings) == 1
    run(go())


def test_ebays_refusals_are_told_apart():
    async def go():
        c, shop = make()
        f = await fields_for(c)
        bad = f.model_copy(update={"aspects": {k: v for k, v in f.aspects.items() if k != "Dress Length"}})
        with pytest.raises(ebay.EbayError) as e:
            await c.create(bad, URLS, KEY, validate=True)
        assert e.value.kind == ebay.ITEM and "Dress Length is missing" in str(e.value)
        assert e.value.violations[0]["code"] == "LSTNG_MISSING_REQUIRED_ASPECT" and shop.listings == {}
        with pytest.raises(ebay.EbayError) as e:                          # a policy: no other try changes it
            await c.create(f.model_copy(update={"title": "Replica Designer Dress"}), URLS, KEY)
        assert e.value.kind == ebay.POLICY and "counterfeit" in str(e.value)
        with pytest.raises(ebay.EbayError) as e:                          # eBay's list of sizes is closed
            await c.create(f.model_copy(update={"aspects": {**f.aspects, "Size": ["Smallish"]}}), URLS, KEY)
        assert e.value.kind == ebay.ITEM and "standard size" in str(e.value)
        with pytest.raises(ebay.EbayError) as e:
            await c.create(f, [], KEY)
        assert "image" in str(e.value)
        limit = {"data": {"createListing": {"__typename": "CreateListingValidationError", "errorCode": "X",
                                            "errorMessage": "You have reached your selling limit.", "violations": [],
                                            "warnings": []}}}
        shop.fail["create"] = [limit]
        with pytest.raises(ebay.EbayError) as e:
            await c.create(f, URLS, KEY)
        assert e.value.kind == ebay.LIMIT
    run(go())


@pytest.mark.parametrize("planned, kind, unknown", [
    ({"errors": [{"message": "Access denied: insufficient scope", "extensions": {"code": 1100, "category": "REQUEST"}}]},
     ebay.AUTH, False),
    ({"errors": [{"message": "Too many requests", "extensions": {"errorCode": "RATE_LIMITED", "category": "REQUEST"}}]},
     ebay.LIMIT, False),
    ({"errors": [{"message": "Internal error", "extensions": {"code": 2000, "category": "APPLICATION"}}]},
     ebay.SITE, True),
    ({"errors": [{"message": "Unknown field createListingX", "extensions": {"code": 3, "category": "REQUEST"}}]},
     ebay.SITE, False),
    (500, ebay.SITE, True), (429, ebay.LIMIT, False), ("timeout", ebay.INFRA, True)])
def test_a_failure_that_isnt_about_the_item_never_looks_like_one(planned, kind, unknown):
    async def go():
        c, shop = make()
        f = await fields_for(c)
        shop.fail["create"] = [planned]
        with pytest.raises(ebay.EbayError) as e:
            await c.create(f, URLS, KEY)
        assert e.value.kind == kind and e.value.unknown is unknown and e.value.kind != ebay.ITEM
    run(go())


def test_a_401_gets_one_more_try_with_a_fresh_token():
    async def go():
        log: list = []
        c, shop = make(tokens=tokens(log))
        f = await fields_for(c)
        shop.fail["create"] = [401]
        made = await c.create(f, URLS, KEY)
        assert made.listing_id and log.count("user") == 2                 # asked again once, then it went through
        shop.fail["end"] = [401, 401]
        with pytest.raises(ebay.EbayError) as e:
            await c.end(made.listing_id)
        assert e.value.kind == ebay.AUTH and e.value.status == 401
    run(go())


def test_changes_touch_only_what_is_given():
    async def go():
        c, shop = make()
        f = await fields_for(c)
        lid = (await c.create(f, URLS, KEY)).listing_id
        live = await c.update(lid, title="J.Crew Wrap Midi Dress Navy Blue size 8")
        assert live.title.endswith("Navy Blue size 8") and live.photos == URLS and live.price == float(f.price)
        live = await c.update(lid, image_urls=[*URLS, "https://i.ebayimg.com/c.jpg"], description="New words.")
        assert len(live.photos) == 3 and live.description == "<p>New words.</p>" and live.title.endswith("size 8")
        assert await c.update(lid, title="Shorter", validate=True) is None                # checked only
        assert (await c.read_one(lid)).title.endswith("size 8")
        with pytest.raises(ebay.EbayError, match="80 characters"):
            await c.update(lid, title="x" * 81)
        assert (await c.update(lid)).listing_id == lid                                   # nothing given: just read
    run(go())


def test_the_sellers_listings_are_read_to_the_last_page_and_hers_by_hand_are_told_apart(monkeypatch):
    async def go():
        monkeypatch.setattr(ebay, "PAGE", 3)
        shop = fake.FakeEbay(recorded=None, seller_listings=[fake.hand_listing(f"By hand {n}") for n in range(4)])
        c, _ = make(shop)
        f = await fields_for(c)
        ours = []
        for n in range(5):
            sku = f"i_261110_00000{n}"
            g = f.model_copy(update={"sku": sku})
            ours.append((await c.create(g, URLS, ebay.idempotency_key(sku))).listing_id)
        await c.end(ours[0])
        listings, complete = await c.active()
        assert complete and len(listings) == 8                            # 4 by hand + 4 of ours still active
        assert sorted(x.listing_id for x in listings if (x.sku or "").startswith("i_")) == sorted(ours[1:])
        assert [x.sku for x in listings if not (x.sku or "").startswith("i_")] == [None] * 4
        mine = await c.by_sku("i_261110_000000")                          # an ended one is found too
        assert [x.status for x in mine] == ["ENDED"]
        many = await c.read([*ours, "1"])
        assert set(many) == {*ours, "1"} and many["1"] is None
    run(go())


def test_what_ebay_says_about_a_listing_that_sold_or_was_removed():
    async def go():
        c, shop = make()
        f = await fields_for(c)
        lid = (await c.create(f, URLS, KEY)).listing_id
        shop.sell(lid)
        sold = await c.read_one(lid)
        assert sold.was_sold and not sold.for_sale and sold.sold == 1
        other = (await c.create(f.model_copy(update={"sku": "i_261110_bbbbbb"}), URLS,
                                ebay.idempotency_key("i_261110_bbbbbb"))).listing_id
        shop.remove(other)
        gone = await c.read_one(other)
        assert gone.on_hold and not gone.for_sale and "removed" in gone.hold and not gone.was_sold
        third = (await c.create(f.model_copy(update={"sku": "i_261110_cccccc"}), URLS,
                                ebay.idempotency_key("i_261110_cccccc"))).listing_id
        shop.hand_edit(third, price="29.00", title="Her own title")
        hers = await c.read_one(third)
        assert hers.price == 29.0 and hers.title == "Her own title"       # her hand change is there to be read
    run(go())


def test_inputs_go_through_variables_only():
    for document in (ebay.CREATE, ebay.UPDATE, ebay.END, ebay.PRICE, ebay.BY_ID, ebay.ACTIVE, ebay.ENDED):
        assert "$input" in document or "$search" in document
        assert "input: {" not in document and document.count("{") == document.count("}")


def test_the_sandbox_has_its_own_addresses():
    c = ebay.EbayClient(tokens(), environment="sandbox")
    assert "sandbox" in c.hosts["graphql"] and "sandbox" in c.hosts["api"] and "sandbox" in c.hosts["media"]
    assert c.item_url("123") == "https://sandbox.ebay.com/itm/123"
    with pytest.raises(ValueError):
        ebay.EbayClient(tokens(), environment="staging")
