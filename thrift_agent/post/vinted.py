"""Vinted's upload form (WO30), in its order: photos, title, description, category (the leaf by its id, else the tree
walked by its path), brand (looked up in Vinted's brands — exact or normalised only), size, condition, colour (≤ 2),
material (≤ 3), the skirt length Skirts require, price, the package size radio. Every value is the catalog's
(data/vinted_catalog.json): an option is picked when its text IS the value. Promotion / bump offers are closed, never
accepted; a "first listing" or verification page stops Vinted for the window ("Vinted asks for a check — open it on
the Mac.").

UNVERIFIED (record them on the Mac from the dry run's evidence in failed/shots/): every SEL entry. `submit()` publishes
only once the Upload button is recorded (PUBLISH_NEEDS: the supervised `--publish-first`); the unattended loop also
needs the item page Vinted lands on (AUTOPUBLISH_NEEDS)."""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from playwright.async_api import BrowserContext, Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from thrift_agent import brands, catalogs
from thrift_agent.post.base import AccountBlocked, PosterError, _norm, human_type, settle
from thrift_agent.post.cross import Chosen, CrossPoster, same_host
from thrift_agent.schema import Render

BASE = "https://www.vinted.com"
_ITEM = re.compile(r"^/items/(\d+)(?:-[\w-]*)?/?$")
PACKAGE_CACHE = catalogs.DATA_DIR / "vinted_package_sizes.json"      # {leaf id: [codes]}: git-ignored, written here

SEL = {
    "photo_input": lambda p: p.locator('input[type="file"]'),
    "photo_thumbs": lambda p: p.locator('[data-testid*="photo-uploader" i] img, [data-testid*="image" i] img'),
    "title": lambda p: p.locator('[data-testid="title--input"], input#title, input[name="title"]'),
    "description": lambda p: p.locator('[data-testid="description--input"], textarea#description, '
                                       'textarea[name="description"]'),
    "dropdown": lambda p, name: p.locator(f'[data-testid="{name}-select-dropdown-input"], input#{name}, '
                                          f'[data-testid="{name}-select-dropdown"] input'),
    "catalog_leaf": lambda p, cid: p.locator(f'[id="catalog-{cid}"], [data-testid="catalog-{cid}"], '
                                             f'[data-testid="catalog-select-dropdown-row-{cid}"]'),
    "rows": lambda p: p.locator('[role="option"], [data-testid$="dropdown-row"], [data-testid*="dropdown-row-"]'),
    "search": lambda p: p.locator('[data-testid$="dropdown-search-input"], input[type="search"]'),
    "price": lambda p: p.locator('[data-testid="price-input--input"], input#price, input[name="price"]'),
    "package": lambda p: p.locator('input[type="radio"][name*="package" i]'),
    "upload": lambda p: p.locator('[data-testid="upload-form-save-button"]'),
    "promo_close": lambda p: p.locator('[data-testid*="bump" i] button[aria-label*="close" i], '
                                       '[role="dialog"] button[aria-label*="close" i]'),
    "verify_wall": lambda p: p.get_by_text(re.compile(r"verify (your|this) (account|phone|email)|confirm your "
                                                      r"(phone|email)|before you (list|upload)", re.I)),
    "login_wall": lambda p: p.locator('[data-testid="header--login-button"], a[href^="/member/signup"], '
                                      '[data-testid*="auth" i] form'),
    "captcha": lambda p: p.locator('iframe[src*="captcha" i], iframe[title*="captcha" i], #px-captcha, '
                                   'iframe[src*="datadome" i]'),
    "shop_links": lambda p: p.locator('a[href*="/items/"]'),
    "listing_url": _ITEM,
    "live_photos": lambda p: p.locator('[data-testid*="item-photo" i] img'),
}
UNVERIFIED = frozenset(SEL) | {"after_upload", "brands_api", "package_api"}
PUBLISH_NEEDS = frozenset({"upload"})
AUTOPUBLISH_NEEDS = frozenset({"upload", "listing_url", "after_upload"})

MENU_MS = 8_000
THUMB_MS = 90_000
_FETCH_JS = """async (u) => {
  try {
    const r = await fetch(u, {credentials: 'include', headers: {'Accept': 'application/json'}});
    if (!r.ok) return {error: r.status};
    return await r.json();
  } catch (e) { return {error: String(e)}; }
}"""


def listing_address(url: str) -> str | None:
    """https://www.vinted.com/items/<id> for a Vinted item page, else None."""
    if not same_host(url, ("www.vinted.com", "vinted.com")):
        return None
    m = _ITEM.match(urlparse(url.strip()).path)
    return f"{BASE}/items/{m.group(1)}" if m else None


def _packages_cache() -> dict:
    try:
        return json.loads(PACKAGE_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class VintedPoster(CrossPoster):
    name = "vinted"
    site = "Vinted"
    base_url = BASE
    create_url = f"{BASE}/items/new"
    unverified = UNVERIFIED
    publish_needs = PUBLISH_NEEDS
    autopublish_needs = AUTOPUBLISH_NEEDS

    async def _account(self, page: Page, status: int | None = None) -> None:
        """Recorded live (2026-10-06, the poster's profile): logged out, /items/new shows "Join and sell pre-loved
        clothes with no fees" with the header's [data-testid=header--login-button] and /member/signup links."""
        text = await self._page_text(page)
        if why := self._blocked(status, text):
            raise AccountBlocked(why)
        if any(x in page.url for x in ("/signup", "/login", "/member/signup")) or await SEL["login_wall"](page).count() \
                or re.search(r"join and sell|already have an account\??\s*log in", text, re.I):
            raise AccountBlocked("Vinted: not logged in in the poster profile")
        if await SEL["captcha"](page).count():
            raise AccountBlocked("Vinted: a CAPTCHA is shown — solve it by hand in the poster window")
        if await SEL["verify_wall"](page).count():
            raise AccountBlocked("Vinted: a verification (first listing / account check) is asked — open it on the Mac")

    # ---------------------------------------------------------------- fill

    async def fill(self, page: Page, r: Render) -> None:
        f = self.fields
        if f is None:
            raise PosterError("vinted: no mapped fields for this item")
        self._seen = {}
        await self._dismiss(page)
        await self._step("photos", self._photos(page, f.photos))
        await self._step("title", human_type(SEL["title"](page).first, f.title))
        await self._step("description", SEL["description"](page).first.fill(f.description))
        await self._step("category", self._category(page, f.category_id, f.category_path))
        if f.brand:
            await self._step("brand", self._brand(page, f.brand, f.category_id))
        if f.size:
            await self._step("size", self._pick(page, "size", f.size))
        await self._step("condition", self._pick(page, "condition", f.condition))
        for c in f.colors:
            await self._step("color", self._pick(page, "color", c))
        for m in f.materials:
            await self._step("material", self._pick(page, "material", m))
        if f.skirt_length:
            await self._step("skirt_length", self._pick(page, "skirt_length", f.skirt_length))
        await self._step("price", SEL["price"](page).first.fill(str(f.price)))
        await self._step("package", self._package(page, f.category_id, f.package_sizes))
        await self._dismiss(page)
        await settle(page)

    async def _dismiss(self, page: Page) -> None:
        """Close a promotion / bump offer if one is open — never accept it."""
        close = SEL["promo_close"](page)
        for i in range(await close.count()):
            try:
                await close.nth(i).click(timeout=2_000)
                self.notes.append("closed a promotion offer")
            except Exception:  # noqa: BLE001
                pass

    async def _photos(self, page: Page, photos: list[str]) -> None:
        files = [p for p in photos if Path(p).is_file()]
        if len(files) != len(photos):
            raise PosterError(f"{len(photos) - len(files)} photo file(s) missing")
        await SEL["photo_input"](page).first.set_input_files(files)
        deadline = time.monotonic() + THUMB_MS / 1000
        while time.monotonic() < deadline and await SEL["photo_thumbs"](page).count() < len(files):
            await asyncio.sleep(0.5)

    async def _rows(self, page: Page) -> list[str]:
        try:
            await SEL["rows"](page).first.wait_for(state="visible", timeout=MENU_MS)
        except PlaywrightTimeout:
            return []
        return [re.sub(r"\s+", " ", t).strip() for t in await SEL["rows"](page).all_inner_texts()]

    async def _click_row(self, page: Page, text: str) -> str:
        texts = await self._rows(page)
        for i, t in enumerate(texts):
            if _norm(t) == _norm(text) or _norm(t.split(" | ")[0]) == _norm(text):
                await SEL["rows"](page).nth(i).click()
                await settle(page, 0.3, 0.7)
                return t
        raise PosterError(f"no row {text!r} (offered: {texts[:8]})")

    async def _category(self, page: Page, cid: int, path: str) -> None:
        """The leaf by its id when the tree shows it, else the tree walked by the path, part by part."""
        box = SEL["dropdown"](page, "catalog").first
        await box.click()
        await settle(page)
        leaf = SEL["catalog_leaf"](page, cid)
        if await leaf.count():
            await leaf.first.click()
        else:
            for part in path.split(" > "):
                await self._click_row(page, part)
        self._picked("category", path, str(cid))

    async def _brand(self, page: Page, brand: str, cid: int) -> None:
        """Vinted's own brand: looked up in its brands (exact or normalised only, brands.strict_pick); none → the field
        stays empty and "Posted ✓" says so. A brand Vinted flags for authenticity or as luxury is still listed, and the
        ops chat is told."""
        data = await page.evaluate(_FETCH_JS, f"/api/v2/item_upload/brands?category_id={cid}&keyword={quote(brand)}")
        found = (data or {}).get("brands") or []
        choice, guess = brands.strict_pick(brand, [b.get("title") or "" for b in found])
        if choice is None:
            self.guesses.append(f"brand left empty (Vinted has no '{brand}')")
            self._seen["brands_api"] = data if isinstance(data, dict) and data.get("error") else len(found)
            return
        flags = next((b for b in found if b.get("title") == choice), {})
        if flags.get("requires_authenticity_check") or flags.get("is_luxury"):
            self.notes.append(f"brand {choice!r} is flagged by Vinted (authenticity check / luxury): listed anyway")
        box = SEL["dropdown"](page, "brand").first
        await box.click()
        search = SEL["search"](page)
        await human_type(search.first if await search.count() else box, choice)
        shown = await self._click_row(page, choice)
        self._picked("brand", brand, shown)
        if guess:
            self.guesses.append(guess)

    async def _pick(self, page: Page, field: str, value: str) -> None:
        box = SEL["dropdown"](page, field).first
        await box.wait_for(state="visible", timeout=MENU_MS)
        await box.click()
        shown = await self._click_row(page, value)
        self._picked(field, value, shown)
        await page.keyboard.press("Escape")

    async def _package(self, page: Page, cid: int, wanted: list[str]) -> None:
        """The first package size of the ladder the leaf offers (X_SMALL only where Vinted offers it): the radios on the
        form, after the shipping estimate API per leaf when it answers (cached in data/vinted_package_sizes.json)."""
        offered = _packages_cache().get(str(cid))
        if offered is None:
            data = await page.evaluate(_FETCH_JS, f"https://api.www.vinted.com/shipping-estimation/external/catalogs/"
                                                  f"{cid}/package_sizes")
            codes = [p.get("code") for p in (data or {}).get("package_sizes") or [] if isinstance(p, dict)]
            if codes:
                cache = _packages_cache()
                cache[str(cid)] = codes
                PACKAGE_CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
                offered = codes
        radios = SEL["package"](page)
        labels = [await _label_of(page, radios.nth(i)) for i in range(await radios.count())]
        names = {"X_SMALL": "extra small", "SMALL": "small", "MEDIUM": "medium", "LARGE": "large"}
        for code in wanted:
            if offered is not None and code not in offered:
                continue
            for i, label in enumerate(labels):
                if _norm(label).startswith(names[code]) and not (code == "SMALL" and _norm(label).startswith("extra")):
                    await radios.nth(i).check()
                    self._seen["package"] = code
                    return
        raise PosterError(f"package size {wanted} not offered (radios: {labels[:6]}, API: {offered})")

    # ---------------------------------------------------------------- read back

    async def read_back(self, page: Page) -> dict:
        async def value(loc: Locator) -> str | None:
            try:
                return await loc.first.input_value(timeout=2_000)
            except Exception:  # noqa: BLE001
                return None
        return {"title": await value(SEL["title"](page)), "description": await value(SEL["description"](page)),
                "price": await value(SEL["price"](page)), "photos": str(await SEL["photo_thumbs"](page).count()),
                "package": self._seen.get("package"), **(self._seen.get("picked") or {}),
                "shown": self._seen.get("shown")}

    def expected(self, r: Render) -> dict:
        f = self.fields
        want: dict = {"title": f.title, "description": f.description, "price": f.price, "photos": str(len(f.photos)),
                      "category": Chosen([f.category_path]), "condition": Chosen([f.condition])}
        if f.size:
            want["size"] = Chosen([f.size])
        if f.colors:
            want["color"] = Chosen(list(f.colors))
        if f.materials:
            want["material"] = Chosen(list(f.materials))
        if f.skirt_length:
            want["skirt_length"] = Chosen([f.skirt_length])
        return want

    # ---------------------------------------------------------------- after

    def _publish_button(self, page: Page) -> Locator:
        return SEL["upload"](page)

    async def _after_click(self, page: Page) -> None:
        await self._dismiss(page)

    def listing_address(self, url: str) -> str | None:
        return listing_address(url)

    async def _live_photo_count(self, page: Page) -> int | None:
        return None                                          # UNVERIFIED: the item page's gallery

    async def _shop_listings(self, ctx: BrowserContext) -> dict[str, str]:
        if not self.shop:
            return {}
        page = await ctx.new_page()
        try:
            await page.goto(f"{BASE}/member/{self.shop.strip('/')}")
            await page.wait_for_load_state("domcontentloaded")
            rows = await SEL["shop_links"](page).evaluate_all(
                "els => els.map(e => [e.getAttribute('href'), e.getAttribute('title') || e.innerText || ''])")
        finally:
            await page.close()
        out = {}
        for href, text in rows:
            if href and (url := listing_address(href if href.startswith("http") else BASE + href)):
                out[url] = f"{out.get(url, '')} {text}".strip()
        return out


async def _label_of(page: Page, radio: Locator) -> str:
    rid = await radio.get_attribute("id")
    if rid and await (label := page.locator(f'label[for="{rid}"]')).count():
        return re.sub(r"\s+", " ", await label.first.inner_text()).strip()
    return re.sub(r"\s+", " ", await radio.locator("xpath=ancestor::label[1]").inner_text()).strip()
