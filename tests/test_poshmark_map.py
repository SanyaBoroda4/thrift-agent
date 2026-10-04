"""Poshmark's own labels, pure: condition labels, the kids shoe size buttons, size tabs, what read_back must show, and
the guard that keeps an unrecorded publish or draft step from running."""
import asyncio
import re

import pytest

from thrift_agent.post import poshmark
from thrift_agent.post.base import Contains, PosterError
from thrift_agent.post.poshmark import (CONDITION_TO_POSH, KIDS_SIZE_OPTIONS, PUBLISH_NEEDS, SEL, UNVERIFIED,
                                        PoshmarkPoster, SizeChoice, size_choice)
from thrift_agent.schema import Render


def render(**kw) -> Render:
    base = dict(marketplace="poshmark", title="t", description="d", brand="Tory Burch", department="Women",
                category="Shoes", subcategory="Flats & Loafers", size="7.5", colors=["Red"], condition="excellent",
                price=85, photos=["a.jpg", "b.jpg"], sku="i_1")
    base.update(kw)
    return Render(**base)


def test_condition_labels_are_poshmarks_menu():
    """WO17, the owner's rules: excellent goes up as Like New (no "very good" on Poshmark), and Fair never."""
    from thrift_agent.post.poshmark import POSH_FAIR
    assert CONDITION_TO_POSH == {"NWT": "New With Tags (NWT)", "NWOT": "Like New", "like_new": "Like New",
                                 "excellent": "Like New", "good": "Good", "fair": "Good"}
    assert "Fair" not in CONDITION_TO_POSH.values() and POSH_FAIR == ("Fair", "uf")   # recorded, never used


@pytest.mark.parametrize("ours,poshmark_group", [
    ("US Toddler 4", ("Baby", "4")), ("US Toddler 7", ("Baby", "7")),                 # the Baby tab holds 0-7
    ("US Toddler 7.5", ("Toddler", "7.5")), ("US Toddler 10", ("Toddler", "10")),
    ("US Toddler 10.5", ("Toddler", "10.5")),                          # a C size our EU rule still calls Toddler
    ("US Little Kid 10", ("Toddler", "10")),
    ("US Little Kid 10.5", ("Toddler", "10.5")), ("US Little Kid 12", ("Toddler", "12")),   # Poshmark: Toddler to 12
    ("US Little Kid 12.5", ("Little", "12.5")), ("US Little Kid 13.5", ("Little", "13.5")),
    ("US Little Kid 1", ("Little", "1")), ("US Big Kid 1", ("Little", "1")), ("US Big Kid 3", ("Little", "3")),
    ("US Big Kid 3.5", ("Big", "3.5")), ("US Big Kid 7", ("Big", "7")),
])
def test_kids_shoe_sizes_map_to_poshmarks_groups(ours, poshmark_group):
    assert KIDS_SIZE_OPTIONS[ours] == poshmark_group


@pytest.mark.parametrize("kw,choice", [
    (dict(), SizeChoice("Standard", "7.5", verified=True)),
    (dict(category="Tops", size="XS"), SizeChoice("Standard", "XS", verified=True)),
    (dict(category="Jeans", size="27"), SizeChoice("Standard", "27", verified=False)),       # tabs not recorded
    (dict(category="Tops", size="1x"), SizeChoice("Plus", "1X", verified=False)),
    (dict(department="Men", size="10"), SizeChoice("Standard", "10", verified=False)),
    (dict(department="Kids", size="EU 24 / US Toddler 7.5", kids_gender="unisex"),
     SizeChoice("Girls", "7.5 (Toddler Girl)", verified=True)),
    (dict(department="Kids", size="US Toddler 7.5", kids_gender=None),
     SizeChoice("Girls", "7.5 (Toddler Girl)", verified=True)),
    (dict(department="Kids", size="EU 36 / US Big Kid 4", kids_gender="boys"),
     SizeChoice("Boys", "4 (Big Boy)", verified=True)),
    (dict(department="Kids", size="EU 22 / US Toddler 6", kids_gender="girls"),
     SizeChoice("Baby", "6", verified=False, loose=True)),
    (dict(department="Kids", category="Dresses", size="4T", kids_gender="girls"),
     SizeChoice("Girls", "4T", verified=False)),                                        # kids clothing: unrecorded
    (dict(department="Kids", size="M", kids_gender="boys"), SizeChoice("Boys", "M", verified=False)),
    (dict(department="Kids", category="Shirts & Tops", size="6 Months", kids_gender="boys"),
     SizeChoice("Baby", "6 Months", verified=False)),                                    # baby clothing: the Baby tab
    (dict(department="Kids", category="Shirts & Tops", size="Newborn", kids_gender="girls"),
     SizeChoice("Baby", "Newborn", verified=False)),
])
def test_size_choice(kw, choice):
    assert size_choice(render(**kw)) == choice


def test_no_size_no_choice():
    assert size_choice(render(size=None)) is None and size_choice(render(size="  ")) is None


def test_expected_reads_the_dropdowns_as_display_text():
    p = PoshmarkPoster("closet")
    exp = p.expected(render(original_price=228, colors=["Red", "Pink", "Blue"], brand="Levi’s"))
    assert exp["category"] == Contains("Women", "Shoes") and exp["subcategory"] == Contains("Flats & Loafers")
    assert exp["condition"] == Contains("Like New") and exp["size"] == Contains("7.5")   # excellent: Like New
    assert exp["colors"] == Contains("Red", "Pink")                        # Poshmark takes two colours
    assert exp["brand"] == "Levi's" and exp["original_price"] == 228 and exp["photos"] == 2
    assert exp["smart_sell"] == "off" and exp["sku"] == "i_1"

    exp = p.expected(render(subcategory=None, size=None, colors=[], brand=None, department="Kids",
                            kids_gender="boys"))
    assert "subcategory" not in exp and "size" not in exp and "colors" not in exp and exp["brand"] == ""
    assert p.expected(render(department="Kids", size="US Toddler 8", kids_gender="boys"))["size"] == \
        Contains("8 (Toddler Boy)")


def test_submit_refuses_a_draft_while_where_save_draft_lands_is_unrecorded():
    """Stop, don't guess: until where Save Draft lands is recorded, no draft is saved, whatever poster.dry_run says.
    (Publishing is no longer refused here: List This Item and the listing's address are recorded, and whether the
    poster publishes at all is the runner's poster.dry_run + poster.autopublish_confirmed.)"""
    class NoPage:
        def __getattr__(self, name):
            raise AssertionError(f"submit touched the page ({name}) before the guard")

    with pytest.raises(PosterError, match=r"the draft step is not recorded yet \(draft_saved UNVERIFIED"):
        asyncio.run(PoshmarkPoster("closet").submit(NoPage(), "draft"))
    assert not PUBLISH_NEEDS & UNVERIFIED


def test_every_unverified_name_is_a_selector_and_the_verified_ones_are_the_forms_hooks():
    assert UNVERIFIED <= set(SEL)
    assert {"draft_saved", "promote_toggle"} <= UNVERIFIED
    verified = set(SEL) - UNVERIFIED
    assert {"photo_input", "title", "description", "category_open", "department", "category_items",
            "subcategory_items", "size_open", "size_tabs", "size_buttons", "size_done", "condition_open",
            "condition_option", "brand", "brand_options", "color_open", "color_tiles", "style_tag", "tag_options",
            "listing_price", "original_price", "price_dialog", "dialog_listing_price", "dialog_original_price",
            "dialog_smart_sell", "dialog_done", "sku", "details_toggle", "next", "save_draft", "discard",
            "dropdown_root", "drafts_count"} <= verified
    # WO11, from the Mac snapshot: the cover dialog after the upload and Poshmark's modal hook.
    assert {"cover_dialog", "cover_title", "cover_thumbs", "cover_selected", "cover_crop", "cover_apply",
            "any_dialog"} <= verified
    assert "crop_dialog" not in SEL
    # WO15, from the Mac's form and review stages (2026-10-02): the photo tiles, Cancel's dialog, the Share Listing
    # panel, its ‹ Back and List This Item.
    assert {"photo_thumbs", "leave", "share_panel", "review_back", "list_item", "share_connect",
            "closet_links"} <= verified
    # WO16, from the first live listing (2026-10-03): the listing's address and the redirect after List This Item.
    assert {"listing_url", "created_id"} <= verified


LIVE = "https://poshmark.com/listing/Naturino-Glitter-Star-Sneakers-Toddler-size-75-6ac111490000000000000a01"


def test_the_first_live_listings_address_is_a_listing_address():
    p = PoshmarkPoster("closet")
    assert p.listing_address(LIVE) == LIVE
    assert p.listing_address(LIVE + "?utm=x#top") == LIVE and p.listing_address(LIVE + "/") == LIVE
    for wrong in ("https://poshmark.com/closet/someone", "https://poshmark.com/listing/no-id-here",
                  "http://poshmark.com/listing/x-6ac111490000000000000a01",            # not https
                  "https://evil.example/listing/x-6ac111490000000000000a01",           # not Poshmark
                  "https://poshmark.com/listing/x-6ac111490000000000000a0",            # 23 hex digits
                  "https://poshmark.com/listing/x-6AC11149C24EA63CD4E55880", ""):      # Poshmark's ids are lowercase
        assert p.listing_address(wrong) is None, wrong


def test_poshmarks_slug_is_built_from_the_title():
    from thrift_agent.post.poshmark import _letters, posh_slug
    title = "Naturino Glitter Star Sneakers Toddler size 7.5"
    assert posh_slug(title) == "Naturino-Glitter-Star-Sneakers-Toddler-size-75"   # the live listing's pattern
    # The patterns of the recorded closet titles (2026-10-03), on invented titles: punctuation, even an inner
    # hyphen, is dropped; " - " is just a space.
    for t, slug in (("Classic Straw Sun Hat - Ivory", "Classic-Straw-Sun-Hat-Ivory"),
                    ("Olive One-Shoulder linen Midi Dress size Small", "Olive-OneShoulder-linen-Midi-Dress-size-Small"),
                    ("Navy Women's Wrap Dress size M-L", "Navy-Womens-Wrap-Dress-size-ML"),
                    ("Plaid size 4 Button-Up Shirt Jacket - Red/Black",
                     "Plaid-size-4-ButtonUp-Shirt-Jacket-RedBlack")):
        assert posh_slug(t) == slug and _letters(t) == _letters(slug)
    assert _letters("Levi’s Café 7.5") == _letters("Levis-Cafe-75")


def test_the_created_listing_id_comes_from_poshmarks_redirect():
    from thrift_agent.post.poshmark import _created_id, _id_time
    urls = ["https://poshmark.com/create-listing",
            "https://poshmark.com/closet/someone?created_listing_id=6ac111490000000000000a01",
            "https://poshmark.com/closet/someone"]
    assert _created_id(urls) == "6ac111490000000000000a01" and _created_id(urls[::2]) is None
    assert _id_time("6ac111490000000000000a01") == 1791037769      # 2026-10-03 10:29:29 New York: the form opened


def test_condition_codes_are_poshmarks():
    from thrift_agent.post.poshmark import CONDITION_CODES

    assert CONDITION_CODES == {"NWT": "nwt", "NWOT": "uln", "like_new": "uln", "excellent": "uln", "good": "ug",
                               "fair": "ug"}
    assert set(CONDITION_CODES) == set(CONDITION_TO_POSH) and "uf" not in CONDITION_CODES.values()


def test_the_module_docstring_lists_every_unverified_selector():
    doc = poshmark.__doc__
    missing = [name for name in UNVERIFIED if not re.search(rf"\b{name}\b", doc)]
    assert missing == []


@pytest.mark.parametrize("size_us,tab,button,title_group", [
    ("5C", "Baby", "5", "Toddler"), ("7C", "Baby", "7", "Toddler"),        # the title still says Toddler (WO10)
    ("7.5C", "Girls", "7.5 (Toddler Girl)", "Toddler"), ("11C", "Girls", "11 (Toddler Girl)", "Toddler"),
    ("12C", "Girls", "12 (Toddler Girl)", "Toddler"),
    ("12.5C", "Girls", "12.5 (Little Girl)", "Little Kid"), ("13.5C", "Girls", "13.5 (Little Girl)", "Little Kid"),
    ("1Y", "Girls", "1 (Little Girl)", "Little Kid"), ("3Y", "Girls", "3 (Little Girl)", "Little Kid"),
    ("3.5Y", "Girls", "3.5 (Big Girl)", "Big Kid"), ("7Y", "Girls", "7 (Big Girl)", "Big Kid"),
])
def test_the_title_and_the_form_name_the_same_group(facts, size_us, tab, button, title_group):
    """sizes.py (title, description, Render.size) and the form mapping agree for every kids shoe size."""
    from thrift_agent.brain import sizes
    from thrift_agent.schema import Ev

    ev = Ev(value=size_us, photos=[1], source="photo", confidence=0.9)
    f = facts(department="Kids", category="Shoes", size_us=ev, size_printed=ev, size_eu=Ev(), kids_gender="unisex")
    choice = size_choice(render(department="Kids", size=sizes.size_label(f), kids_gender=f.kids_gender))
    assert (choice.tab, choice.button) == (tab, button)
    assert sizes.title_size(f) == f"{title_group} size {size_us[:-1]}"


class _Row:
    """An element as _pick sees it: [id, text, attrs]."""
    def __init__(self, row):
        self.row = row

    async def evaluate(self, js):
        return self.row


class _List:
    """A type-ahead list that re-renders after the first read: `after` is what nth(i) finds by then."""
    def __init__(self, first, second, after):
        self.first, self.second, self.after, self.reads = first, second, after, 0

    async def evaluate_all(self, js):
        self.reads += 1
        return self.first if self.reads == 1 else self.second

    def nth(self, i):
        lst = self

        class One:
            async def element_handle(self, timeout):
                from playwright.async_api import TimeoutError as PlaywrightTimeout
                if lst.reads == 1:
                    if lst.after is None:
                        raise PlaywrightTimeout("the list re-rendered: no such element any more")
                    return _Row(lst.after)
                return _Row(lst.second[i])
        return One()


class _Page:
    async def wait_for_timeout(self, ms):
        pass


@pytest.mark.parametrize("after", [None, ["", "Casual", "Casual"]])    # gone, or another option at that index
def test_pick_reads_a_list_again_when_it_changed_under_it(after):
    """Seen on CI: the tag list filtered itself between reading it and taking "Denim" (index 2)."""
    full = [["", "70s", "70s"], ["", "Casual", "Casual"], ["", "Denim", "Denim"]]
    lst = _List(full, [["", "Denim", "Denim"]], after)
    handle, options = asyncio.run(PoshmarkPoster("closet")._pick(_Page(), lst, "Denim"))
    assert handle.row == ["", "Denim", "Denim"] and lst.reads == 2 and options == ["Denim"]


def test_the_read_back_matches_what_the_live_form_showed():
    """The Mac's form stage (2026-10-02): every closed dropdown's text exactly as the live form read back."""
    from thrift_agent.post.base import compare
    r = render(department="Kids", category="Shoes", subcategory="Sneakers", size="EU 24 / US Toddler 7.5",
               kids_gender="unisex", colors=["Pink", "Green"], condition="good", brand="Naturino", price=50,
               photos=[f"{i:02d}.jpg" for i in range(6)])
    seen = {"title": "t", "description": "d", "brand": "Naturino", "price": "50", "original_price": "0", "sku": "i_1",
            "photos": 6, "category": "Kids Shoes", "subcategory": "Sneakers", "size": "7.5 (Toddler Girl)",
            "condition": "Good", "colors": "Pink Green", "smart_sell": "off"}
    assert compare(seen, PoshmarkPoster("closet").expected(r)) == {}
    assert compare({**seen, "category": "Women Shoes"}, PoshmarkPoster("closet").expected(r)) != {}
    assert compare({**seen, "size": "7.5 (Toddler Boy)"}, PoshmarkPoster("closet").expected(r)) != {}
