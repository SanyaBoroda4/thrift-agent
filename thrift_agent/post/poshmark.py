"""Poshmark create-listing adapter.

SELECTORS ARE UNVERIFIED. Record them on the Mac against the live form:
    playwright codegen --channel chrome --user-data-dir ~/thrift/chrome-profile https://poshmark.com/create-listing
Prefer role/label/placeholder locators over CSS classes. Keep every selector in SEL — including the CAPTCHA
text, the thumbnail locator and the published-listing URL pattern — so a form change is a one-file fix.
Run `thrift poster --once --dry-run` after any change.

TODO(M2), while recording on the Mac:
  - SEL["photo_thumbs"] must match uploaded thumbnails ONLY (no placeholders): fill() waits for
    baseline + len(photos) of them and read_back() reports the count as "photos", which is diffed against
    len(r.photos), so any extra match would fail every item.
  - SEL["captcha"]: confirm the wording Poshmark shows for its bot check.
  - SEL["listing_url"]: confirm what the address bar shows right after "List This Item".
  - Condition options (see CONDITION_TO_POSH) and the crop dialog for the cover photo.
  - read_back(): category breadcrumb, size, brand, colors from the form's chips.
"""
from __future__ import annotations

import re
from pathlib import Path

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from thrift_agent.post.base import AccountBlocked, Mode, NeedsOwner, Poster, PosterError, human_type, settle
from thrift_agent.schema import Render

# Poshmark's stored condition codes (verified on sold listings): nwt, uln (like new), ug (good),
# not_nwt (legacy). The "fair" code and the on-screen labels still need confirming on the form.
CONDITION_TO_POSH = {
    "NWT": "nwt", "NWOT": "uln", "like_new": "uln",
    "excellent": "ug", "good": "ug", "fair": "uf?",
}

# UNVERIFIED (M2): the agent's kids size labels -> the text of Poshmark's size option. Poshmark's kids shoe
# picker is believed to show "7.5C"-style toddler/little-kid sizes and "4Y"-style big-kid sizes, but the exact
# option strings MUST be recorded from the live create-listing form on the Mac before fill() uses this table.
# Not used by fill() yet.
KIDS_SIZE_OPTIONS = {
    "US Toddler 4": "4C", "US Toddler 4.5": "4.5C", "US Toddler 5": "5C", "US Toddler 5.5": "5.5C",
    "US Toddler 6": "6C", "US Toddler 6.5": "6.5C", "US Toddler 7": "7C", "US Toddler 7.5": "7.5C",
    "US Toddler 8": "8C", "US Toddler 8.5": "8.5C", "US Toddler 9": "9C", "US Toddler 9.5": "9.5C",
    "US Toddler 10": "10C",
    "US Little Kid 10.5": "10.5C", "US Little Kid 11": "11C", "US Little Kid 11.5": "11.5C",
    "US Little Kid 12": "12C", "US Little Kid 12.5": "12.5C", "US Little Kid 13": "13C",
    "US Little Kid 13.5": "13.5C", "US Little Kid 1": "1Y", "US Little Kid 1.5": "1.5Y", "US Little Kid 2": "2Y",
    "US Little Kid 2.5": "2.5Y", "US Little Kid 3": "3Y",
    "US Big Kid 3.5": "3.5Y", "US Big Kid 4": "4Y", "US Big Kid 4.5": "4.5Y", "US Big Kid 5": "5Y",
    "US Big Kid 5.5": "5.5Y", "US Big Kid 6": "6Y", "US Big Kid 6.5": "6.5Y", "US Big Kid 7": "7Y",
}

SEL = {  # UNVERIFIED — replace with recorded locators
    "photo_input": lambda p: p.locator("input[type=file]").first,
    "photo_thumbs": lambda p: p.locator("[data-test*=image], .img-item"),
    "title": lambda p: p.get_by_placeholder(re.compile("what are you selling", re.I)),
    "description": lambda p: p.get_by_placeholder(re.compile("describe it", re.I)),
    "category_open": lambda p: p.get_by_text(re.compile("^Select Category$", re.I)),
    "category_option": lambda p, name: p.get_by_role("menuitem", name=name).or_(p.get_by_text(name, exact=True)),
    "size_open": lambda p: p.get_by_text(re.compile("^Select Size$", re.I)),
    "size_option": lambda p, name: p.get_by_role("button", name=name, exact=True),
    "size_done": lambda p: p.get_by_role("button", name=re.compile("^Done$", re.I)),
    "brand": lambda p: p.get_by_placeholder(re.compile("enter the brand", re.I)),
    "brand_option": lambda p, name: p.get_by_role("option", name=re.compile(re.escape(name), re.I)).first,
    "nwt_yes": lambda p: p.get_by_role("button", name=re.compile("^Yes$", re.I)),
    "color_open": lambda p: p.get_by_text(re.compile("^Color$", re.I)),
    "color_option": lambda p, name: p.get_by_role("button", name=name, exact=True),
    "style_tag": lambda p: p.get_by_placeholder(re.compile("style tag", re.I)),
    "original_price": lambda p: p.get_by_label(re.compile("original price", re.I)),
    "listing_price": lambda p: p.get_by_label(re.compile("listing price", re.I)),
    "sku": lambda p: p.get_by_placeholder(re.compile("sku", re.I)),
    "next": lambda p: p.get_by_role("button", name=re.compile("^Next$", re.I)),
    "list_item": lambda p: p.get_by_role("button", name=re.compile("^List This Item$", re.I)),
    "save_draft": lambda p: p.get_by_role("button", name=re.compile("save draft", re.I)),
    "restricted_banner": lambda p: p.get_by_text(re.compile("account is restricted", re.I)),
    "captcha": lambda p: p.get_by_text(re.compile("captcha|verify you are human", re.I)),
    "listing_url": re.compile(r"/listing/"),   # a value, not a locator: the address bar once the item is live
}

THUMB_TIMEOUT_MS = 90_000                      # 16 photos over home Wi-Fi can take a while
THUMB_POLL_MS = 500


class PoshmarkPoster(Poster):
    name = "poshmark"
    create_url = "https://poshmark.com/create-listing"

    def __init__(self, username: str):
        self.username = username

    async def check_account(self, page: Page) -> None:
        await page.goto(f"https://poshmark.com/closet/{self.username}")
        await page.wait_for_load_state("domcontentloaded")
        if "/login" in page.url:
            raise AccountBlocked("not logged in to Poshmark in the poster profile")
        if await SEL["captcha"](page).count():
            raise AccountBlocked("CAPTCHA shown — solve it by hand in the poster window")
        if await SEL["restricted_banner"](page).count():
            raise AccountBlocked("Poshmark account is restricted (unshipped/cancelled orders)")

    async def _wait_for_thumbs(self, page: Page, want: int) -> None:
        """Poll SEL["photo_thumbs"] until every upload shows, so read_back() sees the finished form.
        A shortfall after the timeout fails the item here; a shortfall at read_back time is a Mismatch."""
        thumbs = SEL["photo_thumbs"](page)
        waited = 0
        while (have := await thumbs.count()) < want:
            if waited >= THUMB_TIMEOUT_MS:
                raise PosterError(f"only {have}/{want} photo thumbnails after {THUMB_TIMEOUT_MS // 1000}s")
            await page.wait_for_timeout(THUMB_POLL_MS)
            waited += THUMB_POLL_MS

    async def fill(self, page: Page, r: Render) -> None:
        baseline = await SEL["photo_thumbs"](page).count()
        await SEL["photo_input"](page).set_input_files(r.photos)
        await self._wait_for_thumbs(page, baseline + len(r.photos))
        await settle(page, 1, 2)
        # TODO(M2): confirm the crop dialog if it appears for the cover.

        await human_type(SEL["title"](page), r.title)
        await settle(page)
        await SEL["description"](page).fill(r.description)
        await settle(page)

        await self._pick_category(page, r)
        if r.size:
            await SEL["size_open"](page).click()
            await SEL["size_option"](page, r.size).click()
            if await SEL["size_done"](page).count():
                await SEL["size_done"](page).click()
            await settle(page)

        if r.condition == "NWT":
            await SEL["nwt_yes"](page).click()

        if r.brand:
            await human_type(SEL["brand"](page), r.brand)
            await settle(page, 0.8, 1.6)
            opt = SEL["brand_option"](page, r.brand)
            if not await opt.count():
                # Only the owner knows whether Poshmark spells it differently or the item goes under "Other".
                raise NeedsOwner(f"Poshmark's brand list has no match for '{r.brand}'. Which brand should I pick? "
                                 "(reply e.g. 'brand Vince')")
            await opt.click()
            await settle(page)

        if r.colors:
            await SEL["color_open"](page).click()
            for c in r.colors[:2]:
                await SEL["color_option"](page, c).click()
            await settle(page)

        for tag in r.tags[:3]:
            await human_type(SEL["style_tag"](page), tag)
            await page.keyboard.press("Enter")

        if r.original_price:
            await SEL["original_price"](page).fill(str(r.original_price))
        await SEL["listing_price"](page).fill(str(r.price))
        await SEL["sku"](page).fill(r.sku)
        await settle(page)

    async def _pick_category(self, page: Page, r: Render) -> None:
        await SEL["category_open"](page).click()
        for name in [n for n in (r.department, r.category, r.subcategory) if n]:
            try:
                await SEL["category_option"](page, name).click()
            except PlaywrightTimeout:
                # The option never appeared: Poshmark's tree doesn't have this name here. Ask, don't guess.
                raise NeedsOwner(f"Poshmark has no category option '{name}' under {r.department}/{r.category}. "
                                 "Which category/subcategory should I pick?") from None
            await settle(page, 0.3, 0.8)

    async def read_back(self, page: Page) -> dict:
        async def val(key):
            loc = SEL[key](page)
            return await loc.input_value() if await loc.count() else None
        return {
            "title": await val("title"),
            "description": await val("description"),
            "price": await val("listing_price"),
            "sku": await val("sku"),
            "photos": await SEL["photo_thumbs"](page).count(),   # fewer than planned = Mismatch, never publish
            # TODO(M2): read category breadcrumb, size, brand, colors from the form's chips.
        }

    def expected(self, r: Render) -> dict:
        return {"title": r.title, "description": r.description, "price": r.price, "sku": r.sku,
                "photos": len(r.photos)}

    async def submit(self, page: Page, mode: Mode) -> str | None:
        if mode == "draft":
            btn = SEL["save_draft"](page)
            if not await btn.count():
                raise PosterError("no 'save draft' button on the web form")
            await btn.click()
            await page.wait_for_load_state("networkidle")
            return None
        if await SEL["next"](page).count():
            await SEL["next"](page).click()
            await settle(page, 1, 2)
        await SEL["list_item"](page).click()
        await page.wait_for_url(SEL["listing_url"], timeout=60_000)
        return page.url.split("?")[0]


def photos_for(r: Render) -> list[Path]:
    return [Path(p) for p in r.photos]
