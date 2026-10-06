"""Depop's create-listing form (WO30): photos, the description (Depop has no title: its first line is the title), the
category and every other dropdown as Depop's own Downshift comboboxes — the ids from data/depop_catalog.json:
group-input (category), brand-input, condition-input, variants-input (size), colour-input, source-input, age-input,
style-input, attributes.<name>-input, shippingMethods-input; each one's menu is <id>-menu — then the price and Depop
Shipping with the package size. Boost is never turned on.

Every combobox is cleared before typing (its menu filters by the typed text) and the option whose text IS the catalog
value is picked — never a free-text guess. After the category the size menu lags: it is read until it shows the
expected size before one is picked.

UNVERIFIED (record them on the Mac from the dry run's evidence in failed/shots/: the screenshot, the page's HTML, what
was read back): every SEL entry but the catalog's combobox ids. `submit()` publishes only once the Post button is
recorded (PUBLISH_NEEDS: the supervised `--publish-first`); the unattended loop also needs the listing's address after
Post (AUTOPUBLISH_NEEDS)."""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from thrift_agent import brands
from thrift_agent.post.base import AccountBlocked, PosterError, _norm, settle
from thrift_agent.post.cross import Chosen, CrossPoster, Startswith, same_host
from thrift_agent.schema import Render

BASE = "https://www.depop.com"
_PRODUCT = re.compile(r"^/products/(?!create/?$|edit/?$)([a-z0-9]+(?:-[a-z0-9]+)+)/?$")   # <shop>-<words>: never create

SEL = {
    "photo_input": lambda p: p.locator('input[type="file"]'),
    "photo_thumbs": lambda p: p.locator('[data-testid*="photo" i] img, [class*="PhotoUpload" i] img, '
                                        '[class*="imageContainer" i] img'),
    "description": lambda p: p.locator('textarea#description, textarea[name="description"], '
                                       '[data-testid="description__input"]'),
    "combo": lambda p, cid: p.locator(f'[id="{cid}"]'),
    "menu": lambda p, cid: p.locator(f'[id="{re.sub(r"-input$", "", cid)}-menu"]'),
    "options": lambda menu: menu.locator('[role="option"]'),
    "price": lambda p: p.locator('input#price, input[name="price"], [data-testid="price__input"]'),
    "package": lambda p: p.locator('input[type="radio"][name*="parcel" i], input[type="radio"][name*="package" i], '
                                   'input[type="radio"][name*="size" i]'),
    "boost": lambda p: p.locator('input[type="checkbox"][name*="boost" i], [data-testid*="boost" i] input'),
    "post": lambda p: p.locator('button[type="submit"]').filter(has_text=re.compile(r"^\s*post\s*$", re.I)),
    "login_wall": lambda p: p.locator('form[action*="login" i], [data-testid="login__form"], '
                                      'input[autocomplete="username"]'),
    "captcha": lambda p: p.locator('iframe[src*="captcha" i], iframe[title*="captcha" i], #px-captcha'),
    "shop_links": lambda p: p.locator('a[href^="/products/"]'),
    "listing_url": _PRODUCT,
    "live_photos": lambda p: p.locator('[data-testid*="product" i] img, main img'),
}
UNVERIFIED = frozenset(k for k in SEL if k != "combo") | {"after_post"}
PUBLISH_NEEDS = frozenset({"post"})
AUTOPUBLISH_NEEDS = frozenset({"post", "listing_url", "after_post"})

MENU_MS = 8_000
SIZE_SET_MS = 15_000
THUMB_MS = 90_000


def listing_address(url: str) -> str | None:
    """https://www.depop.com/products/<slug>/ for a Depop product page, else None."""
    if not same_host(url, ("www.depop.com", "depop.com")):
        return None
    m = _PRODUCT.match(urlparse(url.strip()).path)
    return f"{BASE}/products/{m.group(1)}/" if m else None


def match_option(texts: list[str], value: str, category: bool = False) -> int | None:
    """The option whose text IS the value (case and spacing aside). A category is a path ("Women > Bottoms >
    Skirts"): the option shows it whole, or its parts in that order — never just the last word of another
    department's path."""
    want = _norm(value)
    for i, t in enumerate(texts):
        if _norm(t) == want:
            return i
    if category:
        parts = [_norm(p) for p in value.split(" > ")]
        for i, t in enumerate(texts):
            n, pos, ok = _norm(t), 0, True
            for p in parts:
                j = n.find(p, pos)
                if j < 0:
                    ok = False
                    break
                pos = j + len(p)
            if ok:
                return i
    return None


class DepopPoster(CrossPoster):
    name = "depop"
    site = "Depop"
    base_url = BASE
    create_url = f"{BASE}/products/create/"
    unverified = UNVERIFIED
    publish_needs = PUBLISH_NEEDS
    autopublish_needs = AUTOPUBLISH_NEEDS

    def __init__(self, shop: str = "", aliases: brands.Aliases | None = None, strict: bool = False):
        super().__init__(shop, strict)
        self.aliases = aliases or brands.Aliases(None)

    async def _account(self, page: Page) -> None:
        if "/login" in page.url or "/signup" in page.url or await SEL["login_wall"](page).count():
            raise AccountBlocked("Depop: not logged in in the poster profile")
        if await SEL["captcha"](page).count():
            raise AccountBlocked("Depop: a CAPTCHA is shown — solve it by hand in the poster window")

    # ---------------------------------------------------------------- fill

    async def fill(self, page: Page, r: Render) -> None:
        f = self.fields
        if f is None:
            raise PosterError("depop: no mapped fields for this item")
        self._seen = {}
        await self._step("photos", self._photos(page, f.photos))
        await self._step("description", SEL["description"](page).first.fill(f.description))
        await self._step("category", self._choose(page, "group-input", f.category, typed=f.category.split(" > ")[-1],
                                                  category=True))
        if f.size:
            await self._step("size", self._size(page, f.size))
        if f.brand:
            await self._step("brand", self._brand(page, f.brand))
        await self._step("condition", self._choose(page, "condition-input", f.condition))
        for cid, values in (("colour-input", f.colors), ("source-input", f.source),
                            ("age-input", [f.age] if f.age else []), ("style-input", f.style)):
            for v in values:
                await self._step(cid, self._choose(page, cid, v, optional=cid != "colour-input"))
        for name, values in f.attributes.items():
            for v in values:
                await self._step(f"attributes.{name}", self._choose(page, f"attributes.{name}-input", v, optional=True))
        await self._step("price", SEL["price"](page).first.fill(str(f.price)))
        await self._step("shipping", self._shipping(page, f.shipping, f.package_size))
        await settle(page)

    async def _photos(self, page: Page, photos: list[str]) -> None:
        files = [p for p in photos if Path(p).is_file()]
        if len(files) != len(photos):
            raise PosterError(f"{len(photos) - len(files)} photo file(s) missing")
        await SEL["photo_input"](page).first.set_input_files(files)
        deadline = time.monotonic() + THUMB_MS / 1000
        while time.monotonic() < deadline and await SEL["photo_thumbs"](page).count() < len(files):
            await asyncio.sleep(0.5)

    async def _open(self, page: Page, cid: str) -> tuple[Locator, Locator]:
        box = SEL["combo"](page, cid).first
        await box.wait_for(state="visible", timeout=MENU_MS)
        await box.scroll_into_view_if_needed()
        await box.click()
        return box, SEL["menu"](page, cid).first

    async def _options(self, menu: Locator) -> list[str]:
        try:
            await SEL["options"](menu).first.wait_for(state="visible", timeout=MENU_MS)
        except PlaywrightTimeout:
            return []
        return [re.sub(r"\s+", " ", t).strip() for t in await SEL["options"](menu).all_inner_texts()]

    async def _choose(self, page: Page, cid: str, value: str, typed: str | None = None, category: bool = False,
                      optional: bool = False) -> None:
        """Clear the combobox, type, pick the option whose text is the value (WO30)."""
        if optional and not await SEL["combo"](page, cid).count():
            self.notes.append(f"{cid}: not on this form, left out")
            return
        box, menu = await self._open(page, cid)
        await box.fill("")                                  # the menu filters by what is typed: clear it first
        await box.press_sequentially(typed if typed is not None else value, delay=40)
        await settle(page, 0.3, 0.7)
        texts = await self._options(menu)
        idx = match_option(texts, value, category)
        if idx is None:
            await box.press("Escape")
            raise PosterError(f"{cid}: no option {value!r} (offered: {texts[:8]})")
        await SEL["options"](menu).nth(idx).click()
        self._picked(cid, value, texts[idx])
        await settle(page, 0.3, 0.8)

    async def _size(self, page: Page, size: str) -> None:
        """The size menu lags after a category change (the catalog's known_gaps): read it until the size shows."""
        cid, deadline, texts = "variants-input", time.monotonic() + SIZE_SET_MS / 1000, []
        while time.monotonic() < deadline:
            box, menu = await self._open(page, cid)
            await box.fill("")
            texts = await self._options(menu)
            await box.press("Escape")
            if match_option(texts, size) is not None:
                break
            await asyncio.sleep(1)
        self._seen["size_menu"] = texts[:60]
        await self._choose(page, cid, size)

    async def _brand(self, page: Page, brand: str) -> None:
        """Depop's own brand option, exact or normalised only (brands.strict_pick); else empty, said in "Posted ✓"."""
        box, menu = await self._open(page, "brand-input")
        await box.fill("")
        await box.press_sequentially(self.aliases.spell(brand) or brand, delay=40)
        await settle(page, 0.3, 0.7)
        texts = await self._options(menu)
        choice, guess = brands.strict_pick(brand, texts)
        if choice is None:
            await box.fill("")
            await box.press("Escape")
            self.guesses.append(f"brand left empty (Depop has no '{brand}')")
            return
        await SEL["options"](menu).nth(texts.index(choice)).click()
        self._picked("brand-input", brand, choice)
        if guess:
            self.guesses.append(guess)
        await settle(page)

    async def _shipping(self, page: Page, method: str, package: str) -> None:
        await self._choose(page, "shippingMethods-input", method)
        radios = SEL["package"](page)
        for i in range(await radios.count()):
            label = await _label_of(page, radios.nth(i))
            if _norm(label).startswith(_norm(package)):
                await radios.nth(i).check()
                self._seen["package"] = label
                return
        raise PosterError(f"package size {package!r} isn't offered")

    # ---------------------------------------------------------------- read back

    async def read_back(self, page: Page) -> dict:
        async def value(loc: Locator) -> str | None:
            try:
                return await loc.first.input_value(timeout=2_000)
            except Exception:  # noqa: BLE001
                return None
        boost = SEL["boost"](page)
        seen = {"description": await value(SEL["description"](page)), "price": await value(SEL["price"](page)),
                "photos": str(await SEL["photo_thumbs"](page).count()), "package": self._seen.get("package"),
                "boost": any([await boost.nth(i).is_checked() for i in range(await boost.count())]),
                **{k: v for k, v in (self._seen.get("picked") or {}).items()},
                "shown": self._seen.get("shown"), "size_menu": self._seen.get("size_menu")}
        return seen

    def expected(self, r: Render) -> dict:
        f = self.fields
        want: dict = {"description": f.description, "price": f.price, "photos": str(len(f.photos)),
                      "group-input": Chosen([f.category]), "condition-input": Chosen([f.condition]), "boost": False,
                      "package": Startswith(f.package_size)}
        if f.size:
            want["variants-input"] = Chosen([f.size])
        if f.colors:
            want["colour-input"] = Chosen(list(f.colors))
        return want

    # ---------------------------------------------------------------- after

    def _publish_button(self, page: Page) -> Locator:
        return SEL["post"](page)

    def listing_address(self, url: str) -> str | None:
        return listing_address(url)

    async def _live_photo_count(self, page: Page) -> int | None:
        return None                                          # UNVERIFIED: the product page's gallery

    async def _shop_listings(self, ctx: BrowserContext) -> dict[str, str]:
        if not self.shop:
            return {}
        page = await ctx.new_page()
        try:
            await page.goto(f"{BASE}/{self.shop.strip('/')}/")
            await page.wait_for_load_state("domcontentloaded")
            hrefs = await SEL["shop_links"](page).evaluate_all("els => els.map(e => e.getAttribute('href'))")
        finally:
            await page.close()
        out = {}
        for h in hrefs:
            if h and (m := _PRODUCT.match(urlparse(h).path)):
                out[f"{BASE}/products/{m.group(1)}/"] = m.group(1).replace("-", " ")
        return out


async def _label_of(page: Page, radio: Locator) -> str:
    rid = await radio.get_attribute("id")
    if rid and await (label := page.locator(f'label[for="{rid}"]')).count():
        return re.sub(r"\s+", " ", await label.first.inner_text()).strip()
    return re.sub(r"\s+", " ", await radio.locator("xpath=ancestor::label[1]").inner_text()).strip()
