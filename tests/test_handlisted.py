"""WO33: our items the owner already listed by hand on Vinted / Depop — scored against her shop's listings (made-up
ones shaped like the live Vinted tiles: "<title>, brand: …, condition: …, size: …, $35.00"), listed for her by
number, sure and unsure alike, and recorded only for the numbers she confirms."""
from thrift_agent import handlisted as hl
from thrift_agent.db import DB

from test_crosslist_flow import _item, _settings

OURS = [
    {"id": "i_1", "title": "Naturino Pastel Rainbow Glitter Star Sneakers Toddler size 7.5", "brand": "Naturino",
     "size": "EU 24 / US Toddler 7.5", "price": 50},
    {"id": "i_2", "title": "Missguided White Corset Top & Bubble Skirt 2-Piece Set size M", "brand": "Missguided",
     "size": "M", "price": 30},
    {"id": "i_3", "title": "Zara Blue Floral Print Mini Skirt Side Zip Lined size M", "brand": "Zara", "size": "M",
     "price": 35},
    {"id": "i_4", "title": "Levi's 501 Cutoff Denim Shorts Washed Black size 26", "brand": "Levi's", "size": "26",
     "price": 30},
]
SHOP = [
    {"url": "https://www.vinted.com/items/1", "text": "Naturino size 7.5, brand: Naturino, condition: New without tags, "
                                                       "size: 7 toddler, $40.00"},
    {"url": "https://www.vinted.com/items/2", "text": "Misguided set skirt size M, brand: Missguided, condition: Very "
                                                       "good, size: M / US 8-10, $25.00"},
    {"url": "https://www.vinted.com/items/3", "text": "Zara dress size M, brand: Zara, condition: New with tags, size: "
                                                       "M / US 8-10, $100.00"},
    {"url": "https://www.vinted.com/items/4", "text": "Minnetonka boots size 2, brand: minnetonka, condition: Good, "
                                                       "size: 2 baby, $5.00"},
    {"url": "https://www.vinted.com/items/9", "text": "J. Crew Wide Leg Sweater Pants, brand: J.Crew, $35.00"},
]


def test_her_listings_of_our_items_are_found_sure_ones_first_and_a_lookalike_is_only_a_maybe():
    found = hl.candidates(OURS, {"vinted": SHOP}, ours_urls={"https://www.vinted.com/items/9"})
    got = {(c.item, c.url): c.sure for c in found}
    assert got[("i_1", "https://www.vinted.com/items/1")] is True              # Naturino 7.5
    assert got[("i_2", "https://www.vinted.com/items/2")] is True              # her spelling "Misguided"
    assert got[("i_3", "https://www.vinted.com/items/3")] is False             # Zara, M — but a dress, not a skirt
    assert not any(c.item == "i_4" for c in found)                             # nothing like the Levi's
    assert [c.n for c in found] == [1, 2, 3] and found[-1].item == "i_3"       # the sure ones first


def test_the_list_says_nothing_is_marked_until_confirmed():
    text = hl.message(hl.candidates(OURS, {"vinted": SHOP}, set()), {"vinted": len(SHOP)})
    assert "Nothing is marked until you confirm" in text and "1. SAME?" in text and "not sure" in text


def test_only_the_confirmed_numbers_are_recorded(tmp_path):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    a, b = _item(db, 1), _item(db, 2)
    hl.save(db, [hl.Candidate(1, a, "A", "vinted", "https://www.vinted.com/items/1", "x", 0.7, True),
                 hl.Candidate(2, b, "B", "vinted", "https://www.vinted.com/items/2", "y", 0.5, False)])
    lines = hl.apply(db, [1, 7])
    assert db.listing(a, "vinted")["status"] == "posted" and db.listing(a, "vinted")["url"].endswith("/items/1")
    assert db.listing(b, "vinted") is None                                      # not confirmed: untouched
    assert lines[1].startswith("7: no such number")
    assert hl.apply(db, [1])[0].endswith("— left as it is")                    # never twice


def test_depop_addresses_give_their_words_and_a_short_word_is_never_a_near_brand():
    """Live (WO33): Depop's tiles read "Sold" and its addresses end with "/" — the address's words were lost, and
    "Solid & Striped" matched the badge "Sold". Now the words count, and a brand needs every word of it."""
    vans = {"url": "https://www.depop.com/products/someone-vans-size-13-whitegreen-checkerboard-2dae/", "text": "Sold"}
    ours = {"id": "i_5", "title": "Solid & Striped White Knit Crop Top & Flare Pants 2-Piece Set size M",
            "brand": "Solid & Striped", "size": "M", "price": 45}
    assert hl.score(ours, vans["text"], vans["url"]) == 0
    lacoste = {"url": "https://www.depop.com/products/someone-lacoste-graphic-tee-kids-4t-1a2b/", "text": ""}
    tee = {"id": "i_6", "title": "Lacoste Graphic Tee Heather Gray Cotton Crewneck Kids size 4T", "brand": "Lacoste",
           "size": "4T", "price": 20}
    assert hl.score(tee, lacoste["text"], lacoste["url"]) >= hl.SURE
