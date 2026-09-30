"""Poshmark create-listing adapter.

Verified against the live form on 2026-09-29 (web, logged in, nothing saved) and against the DOM snapshot of the
first Mac dry-run on 2026-09-30 (the cover dialog, the dropdown component, the condition items, the curated style
tags, the collapsed SKU, the Cancel link and its "Save Draft" dialog): every SEL entry that is not in UNVERIFIED. The
form has few ARIA roles or labels, so the verified locators are its own hooks: data-vv-name (form fields), data-et-name
(tracked links and buttons), data-test, ids, placeholders and visible texts, plus, where nothing else exists, the BEM
classes of its menus (dropdown__link, dropdown__menu__item, ...).

Clicks are real Playwright clicks on the element Poshmark listens on. In the inspection a JS element.click() on a
subcategory <li> did NOT register, while clicking its inner <a> did, so a menu item is clicked through its inner <a>
when it has one (_click). Nothing here calls element.click() or dispatch_event().

Fill order: photos (then Poshmark's "Select a Covershot." dialog: its default crop, Apply), title, description,
category, subcategory, size, condition, brand, colors, style tags, the Listing Price dialog, SKU (behind "show
details"). read_back() reads every field back, including the text each closed dropdown shows, and Poster.post() diffs
it against expected() before anything else happens. Any other dialog after the upload fails the item with the evidence.

UNVERIFIED (record on the Mac from the dry-run evidence in failed/shots, see reports/2026-09-30_11_*.md):
  photo_thumbs                  the form's photo tiles once the cover dialog is applied (not in the 2026-09-30
                                snapshot; the dialog's own tiles are verified: cover_thumbs)
  leave                         that the form's Cancel link opens the "Save Draft" dialog (both are in the snapshot)
  review_back, list_item,       the page after Next and its way back, the final publish button, the address after
  listing_url, draft_saved      publishing, where Save Draft lands
  captcha                       the wording of Poshmark's bot check
  size_choice(): the Baby tab's labels, kids clothing tabs, Plus sizes.
submit() refuses to publish or save a draft while a step it needs is UNVERIFIED: dry-runs only until then.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.async_api import ElementHandle, Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from thrift_agent.post.base import (AccountBlocked, Contains, Mode, NeedsOwner, Poster, PosterError, human_type,
                                    settle)
from thrift_agent.schema import Render

# The condition menu's labels (verified). Poshmark has no "new without tags": unworn goes up as Like New, and
# excellent as Good.
CONDITION_TO_POSH = {
    "NWT": "New With Tags (NWT)", "NWOT": "Like New", "like_new": "Like New",
    "excellent": "Good", "good": "Good", "fair": "Fair",
}
# Each condition item carries Poshmark's code (data-et-prop-content, the codes sold listings store) and shows its
# label over a one-line description, so it is clicked by the code and checked by the label.
CONDITION_CODES = {
    "NWT": "nwt", "NWOT": "uln", "like_new": "uln", "excellent": "ug", "good": "ug", "fair": "uf",
}
DEPARTMENTS = ("women", "men", "kids", "home", "pets", "electronics")    # a.dropdown__link[data-et-name=...]


def _num(n: float) -> str:
    return f"{n:g}"                                     # 7.0 -> "7", 7.5 -> "7.5"


def _kids_shoe_options() -> dict[str, tuple[str, str]]:
    """Our kids shoe label (brain/sizes.py, without its EU part) -> (Poshmark's group, number).

    Poshmark, Kids > Shoes (verified 2026-09-29), on the Girls and Boys tabs: Toddler 7.5-12, Little 12.5-13.5 and 1-3,
    Big 3.5-7, each button reading e.g. "7.5 (Toddler Girl)" / "7.5 (Toddler Boy)"; the Baby tab holds 0-7 (labels
    UNVERIFIED). Our labels (brain/sizes.py) use the same groups since WO10, except that 0-7C stays "Toddler" (what
    buyers search). The table also takes the labels of Renders made before WO10, when "Little Kid" was any C size above
    10 and "Big Kid" any Y size: a "Toddler" label is always a C size, "Little Kid" a C size from 10 up and a Y size
    below it, "Big Kid" a Y size."""
    out: dict[str, tuple[str, str]] = {}
    for half in range(0, 28):                           # C sizes 0 .. 13.5
        n = _num(half / 2)
        group = ("Baby" if half <= 14 else "Toddler" if half <= 24 else "Little", n)
        out[f"US Toddler {n}"] = group
        if half >= 20:
            out[f"US Little Kid {n}"] = group
    for half in range(2, 15):                           # Y sizes 1 .. 7
        n = _num(half / 2)
        out[f"US Big Kid {n}"] = out[f"US Little Kid {n}"] = ("Little" if half <= 6 else "Big", n)
    return out


KIDS_SIZE_OPTIONS = _kids_shoe_options()


@dataclass(frozen=True)
class SizeChoice:
    tab: str | None          # the size menu's tab: Standard, Plus, Girls, Boys, Baby
    button: str              # the size button's text; its id is "size-<text>"
    verified: bool           # tab and button text were seen on the live form: a missing button is the owner's question
    loose: bool = False      # also accept "<button> (...)": the Baby tab's labels are not recorded


_KIDS_LABEL = re.compile(r"\bUS (Toddler|Little Kid|Big Kid) (\d{1,2}(?:\.5)?)$")
_PLUS = re.compile(r"^[0-6]X$", re.I)


def size_choice(r: Render) -> SizeChoice | None:
    """Which tab and which size button the Render's size is on Poshmark's size menu, or None without a size.

    Adults: the Standard tab, button = the US size ("7.5", "XS", "00"); verified for Women's shoes and tops. Plus
    sizes (1X..) go to the Plus tab (labels UNVERIFIED). Kids: the Girls or Boys tab by kids_gender (unisex or unread
    goes under Girls; the approval message said so). Kids shoes map through KIDS_SIZE_OPTIONS, e.g.
    "EU 24 / US Toddler 7.5" -> Girls / "7.5 (Toddler Girl)"; kids clothing tabs are UNVERIFIED."""
    size = (r.size or "").strip()
    if not size:
        return None
    if r.department == "Kids":
        g = "Boy" if r.kids_gender == "boys" else "Girl"
        m = _KIDS_LABEL.search(size) if r.category == "Shoes" else None
        if m and (opt := KIDS_SIZE_OPTIONS.get(f"US {m[1]} {m[2]}")):
            group, n = opt
            if group == "Baby":
                return SizeChoice("Baby", n, verified=False, loose=True)
            return SizeChoice(f"{g}s", f"{n} ({group} {g})", verified=True)
        return SizeChoice(f"{g}s", size, verified=False)
    if _PLUS.match(size):
        return SizeChoice("Plus", size.upper(), verified=False)
    return SizeChoice("Standard", size, verified=r.department == "Women" and r.category in ("Shoes", "Tops"))


_DONE = re.compile(r"^\s*done\s*$", re.I)
_BACK = re.compile(r"^\s*(back|edit|cancel)\s*$", re.I)
_DROPDOWN = 'xpath=ancestor-or-self::*[@data-test="dropdown"][1]'     # Poshmark's dropdown component
_COVER = '.image-edit-modal [data-test="modal-container"]:visible'  # the "Select a Covershot." dialog, when open

SEL = {
    # ---- verified on the live form, 2026-09-29 ----
    "photo_input": lambda p: p.locator("input#img-file-input"),
    "title": lambda p: p.locator('input[data-vv-name="title"]'),
    "description": lambda p: p.locator('textarea[data-vv-name="description"]'),
    "category_open": lambda p: p.get_by_text("Select Category", exact=True).locator("visible=true"),
    "department": lambda p, et: p.locator(f'a.dropdown__link[data-et-name="{et}"]:visible'),
    "department_all": lambda p: p.locator('a[data-et-name="all"]:visible'),
    # The class sits on the <li> (the inspection) or on its <a> (the departments in the snapshot): either is fine.
    "category_items": lambda p: p.locator('.dropdown__menu__item:visible:not([data-et-name="all"])'),
    "subcategory_items": lambda p: p.locator('a.dropdown__link[data-et-name="sub_category"]:visible'),
    "size_open": lambda p: p.locator('[data-test="size"]'),
    "size_tabs": lambda p: p.locator("a.navigation--horizontal__link:visible"),
    "size_buttons": lambda p: p.locator("button.multi-size-selector__button:visible"),
    "size_done": lambda p: p.locator('button[data-et-name="apply"]:visible'),
    "condition_open": lambda p: p.get_by_text("Select Condition", exact=True).locator("visible=true"),
    "condition_option": lambda p, code: p.locator(
        f'[data-et-name="listing_condition"][data-et-prop-content="{code}"]:visible'),
    "brand": lambda p: p.get_by_placeholder("Enter the Brand/Designer"),
    "brand_options": lambda p: p.locator("ul.listing-editor__suggestions-list > li > .dropdown__link:visible"),
    "color_open": lambda p: p.get_by_text("Select up to 2 colors", exact=True).locator("visible=true"),
    "color_tiles": lambda p: p.locator("li.listing-editor__tile--color:visible"),
    "color_done": lambda p: p.locator('button[data-et-name="apply"]:visible').or_(p.get_by_role("button", name=_DONE)),
    "style_tag": lambda p: p.locator('input[data-vv-name="style-tag-input"]'),
    "tag_options": lambda p: p.locator(
        'ul.listing-editor__suggestions-list [data-et-on-name="style_tag"][data-et-element-type="button"]:visible'),
    "listing_price": lambda p: p.locator('input[data-vv-name="listingPrice"]'),
    "original_price": lambda p: p.locator('input[data-vv-name="originalPrice"]'),
    "price_dialog": lambda p: p.locator(".listing-price-suggestion-modal:visible"),
    "dialog_listing_price": lambda p: p.locator("input#listing-price-modal-listing-price-input"),
    "dialog_original_price": lambda p: p.locator("input#listing-price-modal-original-price-input"),
    "dialog_smart_sell": lambda d: d.locator("input[type=checkbox]"),
    "dialog_done": lambda d: d.get_by_role("button", name=_DONE),
    "sku": lambda p: p.locator('input[data-vv-name="sku"]'),
    "details_toggle": lambda p: p.locator("a.listing-editor-toggle-link").filter(
        has_text=re.compile(r"^\s*show details\s*$", re.I)),
    "next": lambda p: p.locator('button[data-et-name="next"]'),
    "save_draft": lambda p: p.locator('button[data-et-name="save_draft"]:visible'),
    "discard": lambda p: p.locator('button[data-et-name="discard"]:visible'),
    "restricted_banner": lambda p: p.get_by_text(re.compile("account is restricted", re.I)),   # verified Sep 2026
    "dropdown_root": lambda anchor: anchor.locator(_DROPDOWN),
    # The cover dialog Poshmark opens after the upload (the Mac snapshot, 2026-09-30).
    "cover_dialog": lambda p: p.locator(_COVER),
    "cover_title": lambda d: d.get_by_text(re.compile(r"^\s*Select a Covershot\.?\s*$", re.I)),
    "cover_thumbs": lambda d: d.locator(".image-edit-modal__thumb"),
    "cover_selected": lambda thumb: thumb.locator("svg.icon-green-checkmark"),
    "cover_crop": lambda d: d.locator(".croppie-container"),
    "cover_apply": lambda d: d.locator('[data-test="modal-footer"] button[data-et-name="apply"]'),
    "any_dialog": lambda p: p.locator('[data-test="modal-container"]:visible, [role="dialog"]:visible'),
    # ---- UNVERIFIED: see the module docstring ----
    "photo_thumbs": lambda p: p.locator('#imagePlaceholder img:visible, img[src^="blob:"]:visible'),
    "leave": lambda p: p.locator('a[data-et-name="discard"]:visible'),          # the form's Cancel link
    "review_back": lambda p: p.get_by_role("button", name=_BACK).or_(p.get_by_role("link", name=_BACK)),
    "list_item": lambda p: p.get_by_role("button", name=re.compile(r"^\s*list( this item)?\s*$", re.I)),
    "listing_url": re.compile(r"/listing/"),       # a value, not a locator: the address bar once the item is live
    "draft_saved": re.compile(r"/closet/|/listing/"),
    "captcha": lambda p: p.get_by_text(re.compile("captcha|verify you are human", re.I)),
}
UNVERIFIED = frozenset({"photo_thumbs", "leave", "review_back", "list_item", "listing_url", "draft_saved", "captcha"})
PUBLISH_NEEDS = frozenset({"list_item", "listing_url"})      # submit() goes live only once these are recorded
DRAFT_NEEDS = frozenset({"draft_saved"})

THUMB_TIMEOUT_MS = 90_000        # 16 photos over home Wi-Fi can take a while
THUMB_POLL_MS = 500
MENU_TIMEOUT_MS = 8_000          # a menu or dialog to open; a list to show the wanted option
SUGGEST_TIMEOUT_MS = 8_000       # brand suggestions after typing
TAG_TIMEOUT_MS = 3_000           # a curated style tag to be offered
LEAVE_TIMEOUT_MS = 5_000          # the link that leaves the form; then the leave dialog to show
POLL_MS = 250
ROOT_TEXT_MAX = 300              # a dropdown "root" showing more text than this holds more than one field

# [id, visible text, "title|aria-label|data-et-name"] of each element in a list.
_ITEMS_JS = """els => els.map(e => [e.id || '', (e.innerText || e.textContent || '').trim(),
    ['title', 'aria-label', 'data-et-name'].map(a => e.getAttribute(a) || '').join('|')])"""
# The page after Next, for the review stage: what is on it, not what to click.
_RECORD_JS = """() => {
  const shown = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const text = e => (e.innerText || e.value || '').trim().replace(/\\s+/g, ' ').slice(0, 160);
  const info = e => ({tag: e.tagName.toLowerCase(), text: text(e), id: e.id || null,
    et: e.getAttribute('data-et-name'), test: e.getAttribute('data-test'), type: e.getAttribute('type'),
    href: e.getAttribute('href'), disabled: !!e.disabled,
    cls: typeof e.className === 'string' ? e.className.slice(0, 160) : null});
  const all = sel => [...document.querySelectorAll(sel)].filter(shown);
  return {url: location.href, title: document.title,
    buttons: all('button, [role=button], input[type=submit], input[type=button]').map(info),
    tracked: all('[data-et-name]').map(info),
    headings: all('h1, h2, h3, h4').map(text).filter(Boolean),
    labels: all('label, legend').map(text).filter(Boolean),
    dialogs: all('[role=dialog], .modal').map(text)};
}"""


def _straight(s: str) -> str:
    """Curly apostrophes as typed on a phone ("Levi’s") -> the straight ones Poshmark's lists use."""
    return str(s or "").replace("’", "'").replace("‘", "'")


def _key(s: str) -> str:
    return re.sub(r"\s+", " ", _straight(s)).strip().casefold()


def _is(want: str, el_id: str, text: str, attrs: str, loose: bool = False) -> bool:
    """Is this list element the option `want`? Its visible text or that text's first line (an item may show a
    description under its label), case, spacing and curly quotes ignored; its id "size-<want>"; or its title /
    aria-label / data-et-name. `loose` also takes "<want> (...)"."""
    w = _key(want)
    first = text.strip().splitlines()[0] if text.strip() else ""
    if w in (_key(text), _key(first)) or el_id == f"size-{want}" or any(_key(a) == w for a in attrs.split("|") if a):
        return True
    return loose and re.fullmatch(rf"{re.escape(w)}(?:\s*\(.*\))?", _key(text)) is not None


def _offer(options: list[str], limit: int = 12) -> str:
    if not options:
        return ""
    more = "..." if len(options) > limit else ""
    return f" (it offers: {', '.join(options[:limit])}{more})"


async def _click(el: ElementHandle) -> None:
    """A real click on the element Poshmark listens on: a menu item's inner <a> when it has one."""
    inner = await el.query_selector("a")
    await (inner or el).click()


async def _type(loc: Locator, text: str) -> None:
    await loc.fill("")
    await human_type(loc, text)


class PoshmarkPoster(Poster):
    name = "poshmark"
    base_url = "https://poshmark.com"

    def __init__(self, username: str):
        self.username = username
        self.notes: list[str] = []
        self._roots: dict[str, ElementHandle | None] = {}   # dropdown -> the element that shows its choice
        self._state: dict = {}                             # what fill() saw for read_back: thumbnails, Smart Sell

    @property
    def create_url(self) -> str:
        return f"{self.base_url}/create-listing"

    async def check_account(self, page: Page) -> None:
        await page.goto(f"{self.base_url}/closet/{self.username}")
        await page.wait_for_load_state("domcontentloaded")
        if "/login" in page.url:
            raise AccountBlocked("not logged in to Poshmark in the poster profile")
        if await SEL["captcha"](page).count():
            raise AccountBlocked("CAPTCHA shown — solve it by hand in the poster window")
        if await SEL["restricted_banner"](page).count():
            raise AccountBlocked("Poshmark account is restricted (unshipped/cancelled orders)")

    # ---------------------------------------------------------------- fill

    async def fill(self, page: Page, r: Render) -> None:
        self._roots, self._state = {}, {}
        await self._photos(page, r)
        await _type(SEL["title"](page), r.title)
        await settle(page)
        await SEL["description"](page).fill(r.description)
        await settle(page)
        await self._category(page, r)
        await self._size(page, r)
        await self._condition(page, r)
        await self._brand(page, r)
        await self._colors(page, r)
        await self._tags(page, r)
        await self._price(page, r)
        await self._sku(page, r)
        await settle(page)

    async def _photos(self, page: Page, r: Render) -> None:
        """Upload, apply Poshmark's cover dialog, then wait until every thumbnail shows. A thumbnail shortfall is
        noted and left to the diff (read_back counts them), so a dry-run still fills and records the rest of the
        form; a dialog that isn't the recorded cover dialog fails the item."""
        thumbs = SEL["photo_thumbs"](page)
        baseline = await thumbs.count()
        self._state["thumb_baseline"] = baseline
        await SEL["photo_input"](page).set_input_files(r.photos)
        want, waited = baseline + len(r.photos), 0
        while True:
            if await self._dialog_after_upload(page, len(r.photos)):
                continue                               # the cover dialog was applied: now the tiles
            if (have := await thumbs.count()) >= want:
                break
            if waited >= THUMB_TIMEOUT_MS:
                self.notes.append(f"only {have - baseline}/{len(r.photos)} photo thumbnails after "
                                  f"{THUMB_TIMEOUT_MS // 1000}s")
                break
            await page.wait_for_timeout(THUMB_POLL_MS)
            waited += THUMB_POLL_MS
        await settle(page, 1, 2)
        await self._dialog_after_upload(page, len(r.photos))    # one that came late

    async def _dialog_after_upload(self, page: Page, n_photos: int) -> bool:
        """True when Poshmark's cover dialog was open and has been applied. Any other dialog fails the item
        (Poster.post keeps the screenshot and the page): stop, don't guess which of its buttons keeps the photos."""
        dialog = SEL["cover_dialog"](page)
        if await dialog.count():
            if "cover_dialog" in self._state:
                raise PosterError("Poshmark's cover dialog opened again after Apply")
            await self._apply_cover(page, dialog, n_photos)
            return True
        other = SEL["any_dialog"](page)
        if await other.count():
            text = re.sub(r"\s+", " ", await other.first.inner_text()).strip()
            raise PosterError(f"a dialog that isn't recorded opened after the photo upload: '{text[:120]}'")
        return False

    async def _apply_cover(self, page: Page, dialog: Locator, n_photos: int) -> None:
        """The recorded "Select a Covershot." dialog: every uploaded photo listed, the first (our cover) preselected,
        a crop frame with a zoom slider and rotate buttons, Cancel / Apply. Poshmark's default crop is kept: the frame,
        the slider and the rotate buttons are never touched; Apply, then the dialog must close. A dialog that differs
        from the recording fails the item."""
        differs = []
        if not await SEL["cover_title"](dialog).count():
            differs.append("no 'Select a Covershot.' title")
        if await SEL["cover_crop"](dialog).count() != 1:
            differs.append("no crop frame")
        apply = SEL["cover_apply"](dialog)
        if await apply.count() != 1:
            differs.append("no Apply button")
        elif _key(await apply.inner_text()) != "apply":
            differs.append(f"the confirm button reads '{(await apply.inner_text()).strip()}'")
        if differs:
            raise PosterError("Poshmark's cover dialog differs from the recorded one: " + "; ".join(differs))
        thumbs, waited = SEL["cover_thumbs"](dialog), 0
        while (have := await thumbs.count()) < n_photos:      # it may list the photos as they are read
            if waited >= THUMB_TIMEOUT_MS:
                raise PosterError(f"Poshmark's cover dialog lists {have} of {n_photos} photos")
            await page.wait_for_timeout(THUMB_POLL_MS)
            waited += THUMB_POLL_MS
        if not await SEL["cover_selected"](thumbs.first).count():
            raise PosterError("Poshmark's cover dialog did not preselect the first photo (the cover)")
        self._state["cover_dialog"] = {"photos": have, "crop": "Poshmark's default"}
        await settle(page, 0.5, 1.2)
        await apply.click()
        try:
            await dialog.wait_for(state="hidden", timeout=MENU_TIMEOUT_MS)
        except PlaywrightTimeout:
            raise PosterError("Poshmark's cover dialog did not close after Apply") from None
        await settle(page, 0.5, 1.2)

    async def _category(self, page: Page, r: Render) -> None:
        et = r.department.strip().lower()
        if et not in DEPARTMENTS:
            raise NeedsOwner(f"Poshmark has no '{r.department}' department. Which one should it go under? "
                             "(reply e.g. 'department Women')")
        trigger = SEL["category_open"](page)
        await self._remember("category", trigger)
        await trigger.click()
        dept, back = SEL["department"](page, et), SEL["department_all"](page)
        await self._wait(dept.or_(back), "the category menu")
        if not await dept.count():                     # it reopened inside a department: back to the list
            await back.first.click()
            await self._wait(dept, "the department list")
        await dept.first.click()
        await settle(page, 0.3, 0.8)
        picked, options = await self._choose(page, SEL["category_items"](page), r.category)
        if not picked:
            if not options:
                raise PosterError(f"the {r.department} category list did not show")
            raise NeedsOwner(f"Poshmark has no category '{r.category}' under {r.department}{_offer(options)}. "
                             "Which category should I pick? (reply e.g. 'category Tops')")
        await settle(page, 0.3, 0.8)
        await self._subcategory(page, r)

    async def _subcategory(self, page: Page, r: Render) -> None:
        """The subcategory menu opens by itself once a category is picked; "None" is one of its options. Its options
        are the <a> elements: a click on their <li> does not register."""
        items = SEL["subcategory_items"](page)
        try:
            await items.first.wait_for(state="visible", timeout=MENU_TIMEOUT_MS)
        except PlaywrightTimeout:
            if r.subcategory:
                raise PosterError(f"no subcategory menu opened after {r.department}/{r.category}") from None
            return
        await self._remember("subcategory", items.first)
        picked, options = await self._choose(page, items, r.subcategory or "None")
        if not picked:
            if not r.subcategory:
                raise PosterError(f"the subcategory menu has no 'None'{_offer(options)}")
            raise NeedsOwner(f"Poshmark has no subcategory '{r.subcategory}' under {r.department}/{r.category}"
                             f"{_offer(options)}. Which one should I pick? (reply e.g. 'subcategory Sneakers')")
        await settle(page, 0.3, 0.8)

    async def _size(self, page: Page, r: Render) -> None:
        choice = size_choice(r)
        if choice is None:
            return
        where = f"{r.department}/{r.category}" + (f" ({choice.tab})" if choice.tab else "")
        await SEL["size_open"](page).click()
        if choice.tab:
            picked, tabs = await self._choose(page, SEL["size_tabs"](page), choice.tab)
            if picked:
                await settle(page, 0.3, 0.8)
            elif choice.verified:
                raise PosterError(f"the size menu for {r.department}/{r.category} has no '{choice.tab}' tab"
                                  f"{_offer(tabs)}")
        picked, sizes = await self._choose(page, SEL["size_buttons"](page), choice.button, loose=choice.loose)
        if not picked:
            if not sizes:
                raise PosterError(f"the size menu for {where} shows no sizes")
            if choice.verified:
                raise NeedsOwner(f"Poshmark's size list for {where} has no '{choice.button}'{_offer(sizes, 30)}. "
                                 "Which size should I pick? (reply e.g. 'size 8')")
            raise PosterError(f"size '{choice.button}' is not in {where}{_offer(sizes, 30)}; this size list is not "
                              "recorded yet")
        done = SEL["size_done"](page)
        await self._wait(done, "the size menu's Done button")
        await done.first.click()
        await settle(page, 0.3, 0.8)

    async def _condition(self, page: Page, r: Render) -> None:
        """Clicked by Poshmark's code, then checked by the label the item shows over its description."""
        label, code = CONDITION_TO_POSH[r.condition], CONDITION_CODES[r.condition]
        trigger = SEL["condition_open"](page)
        await self._remember("condition", trigger)
        await trigger.click()
        option = SEL["condition_option"](page, code).first
        try:
            await option.wait_for(state="visible", timeout=MENU_TIMEOUT_MS)
        except PlaywrightTimeout:                      # the codes are verified: a missing one means the form changed
            raise PosterError(f"the condition menu has no '{label}' ({code})") from None
        shown = (await option.inner_text()).strip().splitlines()
        if not shown or _key(shown[0]) != _key(label):
            raise PosterError(f"condition {code} reads '{shown[0] if shown else ''}', expected '{label}'")
        await option.click()
        await settle(page, 0.3, 0.8)

    async def _brand(self, page: Page, r: Render) -> None:
        if not r.brand:
            return
        await _type(SEL["brand"](page), _straight(r.brand))
        picked, options = await self._choose(page, SEL["brand_options"](page), r.brand, timeout_ms=SUGGEST_TIMEOUT_MS)
        if not picked:
            # Only the owner knows whether Poshmark spells it differently or the item goes under another brand.
            raise NeedsOwner(f"Poshmark's brand list has no match for '{r.brand}'{_offer(options, 8)}. Which brand "
                             "should I pick? (reply e.g. 'brand Vince')")
        await settle(page)

    async def _colors(self, page: Page, r: Render) -> None:
        colors = [c for c in r.colors if c][:2]
        if not colors:
            return
        trigger = SEL["color_open"](page)
        await self._remember("colors", trigger)
        await trigger.click()
        for color in colors:
            picked, options = await self._choose(page, SEL["color_tiles"](page), color)
            if not picked:                             # the tiles are our 15-colour palette: a missing one = a change
                raise PosterError(f"no '{color}' colour tile{_offer(options, 15)}")
            await settle(page, 0.2, 0.6)
        done = SEL["color_done"](page)
        await self._wait(done, "the colour menu's Done button")
        await done.first.click()
        await settle(page, 0.3, 0.8)

    async def _tags(self, page: Page, r: Render) -> None:
        """Curated style tags only: each tag is typed and picked from Poshmark's own suggestions. A tag it doesn't
        offer is cleared and left out (free-typed tags are UNVERIFIED), and the owner is told."""
        box, skipped = SEL["style_tag"](page), []
        for tag in [t for t in r.tags if t][:3]:
            await _type(box, tag)
            picked, _ = await self._choose(page, SEL["tag_options"](page), tag, timeout_ms=TAG_TIMEOUT_MS)
            if not picked:
                await box.fill("")
                skipped.append(tag)
                continue
            await settle(page, 0.2, 0.6)
        if await SEL["tag_options"](page).count():     # a list left open (after a skipped tag) would cover the price
            await box.press("Escape")
        if skipped:
            self.notes.append(f"style tags Poshmark doesn't offer, left out: {', '.join(skipped)}")

    async def _price(self, page: Page, r: Render) -> None:
        """The Listing Price dialog: listing price, original price (the retail price, else empty), Smart Sell must
        be off (asserted, never toggled), Shipping Discount left at No Discount, then Done."""
        await SEL["listing_price"](page).click()
        dialog = SEL["price_dialog"](page)
        await self._wait(dialog, "the Listing Price dialog")
        await _type(SEL["dialog_listing_price"](page), str(r.price))
        original = SEL["dialog_original_price"](page)
        await original.fill("")
        if r.original_price:
            await _type(original, str(r.original_price))
        smart_sell = SEL["dialog_smart_sell"](dialog)
        if (n := await smart_sell.count()) != 1:
            raise PosterError(f"the Listing Price dialog has {n} checkboxes; expected one (Smart Sell)")
        if await smart_sell.is_checked():
            raise PosterError("Smart Sell is on in the Listing Price dialog; it must stay off (it drops the price "
                              "by itself). Switch it off on Poshmark, then requeue the item")
        self._state["smart_sell"] = "off"
        self._state["price_dialog"] = re.sub(r"\s+", " ", await dialog.inner_text())[:400]
        await SEL["dialog_done"](dialog).click()
        await dialog.wait_for(state="hidden", timeout=MENU_TIMEOUT_MS)
        await settle(page)

    async def _sku(self, page: Page, r: Render) -> None:
        """The SKU sits in the collapsed Additional Details: "show details" first. Cost price and other info stay
        empty."""
        box = SEL["sku"](page)
        if not await box.is_visible():
            await SEL["details_toggle"](page).first.click()
            try:
                await box.wait_for(state="visible", timeout=MENU_TIMEOUT_MS)
            except PlaywrightTimeout:
                raise PosterError("the SKU field did not show after 'show details'") from None
        await box.fill(r.sku)

    # ---------------------------------------------------------------- helpers

    async def _wait(self, loc: Locator, what: str) -> None:
        try:
            await loc.first.wait_for(state="visible", timeout=MENU_TIMEOUT_MS)
        except PlaywrightTimeout:
            raise PosterError(f"{what} did not show") from None

    async def _pick(self, page: Page, items: Locator, want: str, *, loose: bool = False,
                    timeout_ms: int | None = None) -> tuple[ElementHandle | None, list[str]]:
        """The visible list element that is `want`, polling while the list renders, plus the texts on offer (for the
        owner's question). (None, texts) when it never shows."""
        timeout_ms = MENU_TIMEOUT_MS if timeout_ms is None else timeout_ms
        waited = 0
        while True:
            rows = await items.evaluate_all(_ITEMS_JS)
            options = list(dict.fromkeys(text.strip().splitlines()[0] for _, text, _ in rows if text.strip()))
            for i, (el_id, text, attrs) in enumerate(rows):
                if _is(want, el_id, text, attrs, loose):
                    handle = await items.nth(i).element_handle(timeout=MENU_TIMEOUT_MS)
                    if handle is not None:
                        return handle, options
            if waited >= timeout_ms:
                return None, options
            await page.wait_for_timeout(POLL_MS)
            waited += POLL_MS

    async def _choose(self, page: Page, items: Locator, want: str, *, loose: bool = False,
                      timeout_ms: int | None = None) -> tuple[bool, list[str]]:
        """Find `want` among the visible `items` (_pick) and click it (_click): (clicked, the texts on offer). A list
        that re-renders between the find and the click detaches the element; that is tried once more."""
        for attempt in (1, 2):
            el, options = await self._pick(page, items, want, loose=loose, timeout_ms=timeout_ms)
            if el is None:
                return False, options
            try:
                await _click(el)
                return True, options
            except Exception as e:  # noqa: BLE001
                if attempt == 2 or "not attached" not in str(e):
                    raise
                await page.wait_for_timeout(POLL_MS)
        return False, []

    async def _remember(self, name: str, anchor: Locator) -> None:
        """Keep the element a dropdown shows its choice in, for read_back: its BEM root (class "dropdown"), else the
        anchor's parent. A handle, because the anchor's own text ("Select Category") is gone once a choice is made."""
        try:
            root = SEL["dropdown_root"](anchor)
            target = root if await root.count() else anchor.locator("xpath=..")
            self._roots[name] = await target.first.element_handle(timeout=MENU_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 — read_back then reports the field unreadable, which fails the diff
            self._roots[name] = None

    async def _root_text(self, name: str) -> str | None:
        el = self._roots.get(name)
        if el is None:
            return None
        try:
            text = re.sub(r"\s+", " ", await el.inner_text()).strip()
        except Exception:  # noqa: BLE001 — detached by a re-render: unreadable
            return None
        return text if len(text) <= ROOT_TEXT_MAX else None

    # ---------------------------------------------------------------- read back, diff

    async def read_back(self, page: Page) -> dict:
        async def value(key: str) -> str | None:
            loc = SEL[key](page)
            return await loc.input_value() if await loc.count() else None

        size = SEL["size_open"](page)
        return {
            "title": await value("title"),
            "description": await value("description"),
            "brand": await value("brand"),
            "price": await value("listing_price"),
            "original_price": await value("original_price"),
            "sku": await value("sku"),
            "photos": await SEL["photo_thumbs"](page).count() - self._state.get("thumb_baseline", 0),
            "category": await self._root_text("category"),
            "subcategory": await self._root_text("subcategory"),
            "size": re.sub(r"\s+", " ", await size.inner_text()).strip() if await size.count() else None,
            "condition": await self._root_text("condition"),
            "colors": await self._root_text("colors"),
            "smart_sell": self._state.get("smart_sell"),         # asserted off while the dialog was open
            "price_dialog": self._state.get("price_dialog"),     # recorded only, not diffed
            "cover_dialog": self._state.get("cover_dialog"),     # recorded only: None when Poshmark showed none
        }

    def expected(self, r: Render) -> dict:
        exp = {
            "title": r.title, "description": r.description, "brand": _straight(r.brand), "price": r.price,
            "original_price": r.original_price, "sku": r.sku, "photos": len(r.photos),
            "category": Contains(r.department, r.category),
            "condition": Contains(CONDITION_TO_POSH[r.condition]),
            "smart_sell": "off",
        }
        if r.subcategory:
            exp["subcategory"] = Contains(r.subcategory)
        if choice := size_choice(r):
            exp["size"] = Contains(choice.button)
        if colors := [c for c in r.colors if c][:2]:
            exp["colors"] = Contains(*colors)
        return exp

    # ---------------------------------------------------------------- leave, review, submit

    async def discard(self, page: Page) -> str | None:
        """Leave through the form's Cancel link and its "Save Draft" dialog ("Do you want to save this listing as a
        draft?"), pressing "Discard Changes", so no draft is left behind. Both are in the 2026-09-30 snapshot; that the
        link opens the dialog is UNVERIFIED (SEL["leave"]): anything short of a Discard click is reported."""
        try:
            button = SEL["discard"](page).first
            if not await button.is_visible():          # the review stage may have opened the dialog already
                await SEL["leave"](page).first.click(timeout=LEAVE_TIMEOUT_MS)
                await button.wait_for(state="visible", timeout=LEAVE_TIMEOUT_MS)
            await button.click()
            await page.wait_for_load_state("domcontentloaded")
        except Exception as e:  # noqa: BLE001
            return f"left the form without Poshmark's Discard dialog ({type(e).__name__}): check the closet's drafts"
        return None

    async def review(self, page: Page, r: Render, shots: Path) -> str | None:
        """Dry-run stage "review": press Next, record the page after it (a screenshot and <item>-review.json with its
        buttons, tracked links, headings and labels), then come back. Only Next and a Back/Edit/Cancel control are
        clicked here: never the final publish button."""
        await SEL["next"](page).click()
        await page.wait_for_load_state("domcontentloaded")
        await settle(page, 1.5, 2.5)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        await page.screenshot(path=str(shots / f"{r.sku}-{self.name}-{stamp}-review.png"), full_page=True)
        out = shots / f"{r.sku}-review.json"
        out.write_text(json.dumps(await page.evaluate(_RECORD_JS), indent=1), encoding="utf-8")
        back = SEL["review_back"](page)
        try:
            if await back.count():
                await back.first.click(timeout=LEAVE_TIMEOUT_MS)
            else:
                await page.go_back(timeout=LEAVE_TIMEOUT_MS * 2)
            await page.wait_for_load_state("domcontentloaded")
        except Exception as e:  # noqa: BLE001 — the record is written; discard() still tries to leave cleanly
            return f"review page recorded in {out.name}; could not go back ({type(e).__name__})"
        await settle(page, 0.5, 1)
        return f"review page recorded in {out.name}"

    async def submit(self, page: Page, mode: Mode) -> str | None:
        needs = PUBLISH_NEEDS if mode == "publish" else DRAFT_NEEDS
        if missing := sorted(needs & UNVERIFIED):
            raise PosterError(f"the {mode} step is not recorded yet ({', '.join(missing)} UNVERIFIED in "
                              "post/poshmark.py); keep poster.dry_run on until it is")
        if mode == "draft":
            await SEL["save_draft"](page).first.click()
            await page.wait_for_url(SEL["draft_saved"], timeout=60_000)
            return None
        await SEL["next"](page).click()
        await settle(page, 1, 2)
        await SEL["list_item"](page).click()
        await page.wait_for_url(SEL["listing_url"], timeout=60_000)
        return page.url.split("?")[0]
