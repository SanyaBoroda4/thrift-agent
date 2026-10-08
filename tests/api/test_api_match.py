"""thrift_api.match (WO33): a sale is our item by its SKU, its listing's address or id, else a title at least 0.9 alike
on the same marketplace (posted listings first, then delisted / sold ones) — never a guess between two items."""
from apitools import IDS, ITEM, TITLE, URLS, seed

from thrift_api import core, match


def test_by_sku_our_item_id(db):
    seed(db)
    assert match.match_sale(db, "depop", {"sku": ITEM}) == (ITEM, "sku")
    assert match.match_sale(db, "depop", {"sku": "i_999999_abcdef"}) == (None, None)


def test_a_sku_only_a_listing_knows(db):
    core.sync(db, {"listings": [{"item_id": "i_261005_aaaaaa", "marketplace": "depop", "status": "posted",
                                 "sku": "i_261005_aaaaaa"}]})
    assert match.item_for_sku(db, "i_261005_aaaaaa") == "i_261005_aaaaaa"


def test_by_the_listing_address_or_id(db):
    seed(db)
    for site in ("poshmark", "depop", "vinted"):
        assert match.match_sale(db, site, {"listing_url": URLS[site]}) == (ITEM, "listing"), site
        assert match.match_sale(db, site, {"listing_id": IDS[site]}) == (ITEM, "listing"), site
    assert match.match_sale(db, "poshmark", {"listing_id": IDS["poshmark"].upper()}) == (ITEM, "listing")
    assert match.match_sale(db, "depop", {"listing_id": IDS["vinted"]}) == (None, None)      # another site's id


def test_the_id_inside_a_stored_address_counts_when_the_id_column_is_empty(db):
    seed(db, ids={"poshmark": None, "depop": None, "vinted": None})
    assert match.item_for_listing(db, "vinted", listing_id=IDS["vinted"]) == ITEM
    assert match.item_for_listing(db, "poshmark", listing_url=URLS["poshmark"] + "?ref=email") == ITEM


def test_by_title_on_the_same_marketplace(db):
    seed(db)
    seed(db, "i_261004_000002", "Zara Linen Shirt Blue size S", urls={}, ids={})
    assert match.match_sale(db, "vinted", {"title": "lacoste tee white SIZE m"}) == (ITEM, "title")
    assert match.match_sale(db, "vinted", {"title": "Lacoste Tee — White, size M!"}) == (ITEM, "title")
    assert match.match_sale(db, "vinted", {"title": "Lacoste Tee White si…"}) == (ITEM, "title")    # cut short
    assert match.match_sale(db, "vinted", {"title": "Lacoste Polo Navy size L"}) == (None, None)
    assert match.match_sale(db, "vinted", {"title": None}) == (None, None)


def test_title_matching_stays_on_the_marketplace_and_prefers_posted_listings(db):
    seed(db, sites=("poshmark",))
    assert match.match_sale(db, "depop", {"title": TITLE}) == (None, None)            # not listed on Depop
    seed(db, "i_old", TITLE, sites=("vinted",), status="delisted", urls={}, ids={})
    seed(db, "i_new", TITLE, sites=("vinted",), status="posted", urls={}, ids={})
    assert match.item_for_title(db, "vinted", TITLE) == "i_new"
    core.sync(db, {"listings": [{"item_id": "i_new", "marketplace": "vinted", "status": "delisted",
                                 "updated_at": "2026-10-09T00:00:00+00:00"}]})
    assert match.item_for_title(db, "vinted", TITLE) is None                          # two delisted: no guess
    core.sync(db, {"listings": [{"item_id": "i_old", "marketplace": "vinted", "status": "queued",
                                 "updated_at": "2026-10-09T00:00:00+00:00"}]})
    assert match.item_for_title(db, "vinted", TITLE) == "i_new"                       # the one delisted listing


def test_two_items_with_the_same_title_are_never_a_guess(db):
    seed(db, "i_a", TITLE, sites=("depop",), urls={}, ids={})
    seed(db, "i_b", TITLE, sites=("depop",), urls={}, ids={})
    assert match.match_sale(db, "depop", {"title": TITLE}) == (None, None)


def test_similarity():
    assert match.norm_title("Lacoste Tee — White, size M!") == "lacoste tee white size m"
    assert match.similarity("Lacoste Tee White size M", "lacoste tee white size m") == 1.0
    assert match.similarity("Lacoste Tee White...", "Lacoste Tee White size M") == 1.0
    assert match.similarity("Lacoste Tee White", "Lacoste Tee White size M") < 0.9         # not marked as cut
    assert match.similarity("", "anything") == 0.0
    assert match.best("Lacoste Tee", [("a", "Lacoste Tee"), ("a", "Lacoste Tee!")]) == ("a", False)
    assert match.best("Lacoste Tee", [("a", "Lacoste Tee"), ("b", "Lacoste Tee")]) == (None, True)
    assert match.best("Lacoste Tee", [("a", "Zara Shirt")]) == (None, False)
