"""Poshmark's own labels, pure: condition labels, the kids shoe size buttons, size tabs, what read_back must show, and
the guard that keeps an unrecorded publish or draft step from running."""
import asyncio
import re

import pytest

from thrift_agent.post import poshmark
from thrift_agent.post.base import Contains, PosterError
from thrift_agent.post.poshmark import (CONDITION_TO_POSH, KIDS_SIZE_OPTIONS, SEL, UNVERIFIED, PoshmarkPoster,
                                        SizeChoice, size_choice)
from thrift_agent.schema import Render


def render(**kw) -> Render:
    base = dict(marketplace="poshmark", title="t", description="d", brand="Tory Burch", department="Women",
                category="Shoes", subcategory="Flats & Loafers", size="7.5", colors=["Red"], condition="excellent",
                price=85, photos=["a.jpg", "b.jpg"], sku="i_1")
    base.update(kw)
    return Render(**base)


def test_condition_labels_are_poshmarks_menu():
    assert CONDITION_TO_POSH == {"NWT": "New With Tags (NWT)", "NWOT": "Like New", "like_new": "Like New",
                                 "excellent": "Good", "good": "Good", "fair": "Fair"}


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
])
def test_size_choice(kw, choice):
    assert size_choice(render(**kw)) == choice


def test_no_size_no_choice():
    assert size_choice(render(size=None)) is None and size_choice(render(size="  ")) is None


def test_expected_reads_the_dropdowns_as_display_text():
    p = PoshmarkPoster("closet")
    exp = p.expected(render(original_price=228, colors=["Red", "Pink", "Blue"], brand="Levi’s"))
    assert exp["category"] == Contains("Women", "Shoes") and exp["subcategory"] == Contains("Flats & Loafers")
    assert exp["condition"] == Contains("Good") and exp["size"] == Contains("7.5")
    assert exp["colors"] == Contains("Red", "Pink")                        # Poshmark takes two colours
    assert exp["brand"] == "Levi's" and exp["original_price"] == 228 and exp["photos"] == 2
    assert exp["smart_sell"] == "off" and exp["sku"] == "i_1"

    exp = p.expected(render(subcategory=None, size=None, colors=[], brand=None, department="Kids",
                            kids_gender="boys"))
    assert "subcategory" not in exp and "size" not in exp and "colors" not in exp and exp["brand"] == ""
    assert p.expected(render(department="Kids", size="US Toddler 8", kids_gender="boys"))["size"] == \
        Contains("8 (Toddler Boy)")


@pytest.mark.parametrize("mode", ["publish", "draft"])
def test_submit_refuses_while_its_steps_are_unrecorded(mode):
    """Stop, don't guess: until the page after Next, the publish button and where Save Draft lands are recorded,
    only dry-runs can run, whatever poster.dry_run says."""
    class NoPage:
        def __getattr__(self, name):
            raise AssertionError(f"submit touched the page ({name}) before the guard")

    with pytest.raises(PosterError, match=rf"the {mode} step is not recorded yet \(.*UNVERIFIED"):
        asyncio.run(PoshmarkPoster("closet").submit(NoPage(), mode))


def test_every_unverified_name_is_a_selector_and_the_verified_ones_are_the_forms_hooks():
    assert UNVERIFIED <= set(SEL)
    assert {"list_item", "listing_url"} <= UNVERIFIED and "draft_saved" in UNVERIFIED
    verified = set(SEL) - UNVERIFIED
    assert {"photo_input", "title", "description", "category_open", "department", "category_items",
            "subcategory_items", "size_open", "size_tabs", "size_buttons", "size_done", "condition_open",
            "condition_items", "brand", "brand_options", "color_open", "color_tiles", "style_tag", "listing_price",
            "original_price", "price_dialog", "dialog_listing_price", "dialog_original_price", "dialog_smart_sell",
            "dialog_done", "sku", "next", "save_draft", "discard"} <= verified


def test_the_module_docstring_lists_every_unverified_selector():
    doc = poshmark.__doc__
    missing = [name for name in UNVERIFIED if not re.search(rf"\b{name}\b", doc)]
    assert missing == []
