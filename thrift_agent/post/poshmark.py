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

After Next (the Mac's review stage, 2026-10-02) the URL stays /create-listing and a "Share Listing" panel slides up
over the form: "‹ Back", the cover and title, a "Promote My Closet" toggle (Off; never touched), Pinterest and Facebook
"Connect Now" (never clicked) and button[data-et-name=list] "List This Item".

After List This Item (the first live listing, 2026-10-03) Poshmark goes to /closet/<user>?created_listing_id=<24 hex>,
then drops the query. The closet did not show the new listing yet 5 s later, so it is reloaded until it does
(_poll_closet). The listing's page is /listing/<slug>-<24 hex>: the slug is the title with everything but letters,
digits and spaces dropped and the words joined by "-" (48 of 48 closet listings: "Toddler size 7.5" -> "Toddler-size-75",
"One-Shoulder" -> "OneShoulder"); the id is a MongoDB ObjectId whose first 8 hex digits are its creation time (the
moment the create form opened, not the click).

UNVERIFIED (record on the Mac from the evidence in failed/shots):
  promote_toggle                the markup of the Promote My Closet toggle (asserted off before List; an unreadable
                                one stops the publish before the click)
  draft_saved                   where Save Draft lands
  captcha                       the wording of Poshmark's bot check
  size_choice(): the Baby tab's labels, kids clothing tabs, Plus sizes.
submit() refuses to save a draft while draft_saved is UNVERIFIED. Publishing is decided by the caller: the poster loop
publishes only with poster.dry_run off and poster.autopublish_confirmed on (runner.run); the supervised publish
(`thrift poster --publish-first <item>`) sets `confirm`, so the owner types LIST at the Share Listing panel. Either way
List This Item is pressed exactly once, and everything after the click is recorded.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import ElementHandle, Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from thrift_agent.post.base import (AccountBlocked, Cancelled, Contains, Mode, NeedsOwner, Poster, PosterError,
                                    human_type, keep_evidence, settle)
from thrift_agent.schema import Render

# The condition menu's labels (verified). Poshmark has no "new without tags": unworn goes up as Like New. It has no
# "very good" either: the owner lists excellent as Like New (WO17: torn between Like New and Good, Like New). The
# shop never lists Fair: a fair reading goes up as Good (the pipeline already made it good and told the owner).
CONDITION_TO_POSH = {
    "NWT": "New With Tags (NWT)", "NWOT": "Like New", "like_new": "Like New",
    "excellent": "Like New", "good": "Good", "fair": "Good",
}
# Each condition item carries Poshmark's code (data-et-prop-content, the codes sold listings store) and shows its
# label over a one-line description, so it is clicked by the code and checked by the label.
CONDITION_CODES = {
    "NWT": "nwt", "NWOT": "uln", "like_new": "uln", "excellent": "uln", "good": "ug", "fair": "ug",
}
POSH_FAIR = ("Fair", "uf")       # recorded on the live form (2026-09-29); never selected: the shop doesn't list Fair
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
_BABY = re.compile(r"\bMonths?\b|^(?:Newborn|Preemie)$", re.I)    # "6 Months", "0-3 Months": the Baby tab (WO23)


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
        if _BABY.search(size):                       # baby clothing sizes sit on the Baby tab (Poshmark's catalog)
            return SizeChoice("Baby", size, verified=False)
        return SizeChoice(f"{g}s", size, verified=False)
    if _PLUS.match(size):
        return SizeChoice("Plus", size.upper(), verified=False)
    return SizeChoice("Standard", size, verified=r.department == "Women" and r.category in ("Shoes", "Tops"))


_DONE = re.compile(r"^\s*done\s*$", re.I)
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
    "drafts_count": lambda p: p.locator('[data-et-name="draftsSection"] .listing-editor__promotion__count'),
    "dropdown_root": lambda anchor: anchor.locator(_DROPDOWN),
    # The cover dialog Poshmark opens after the upload (the Mac snapshot, 2026-09-30).
    "cover_dialog": lambda p: p.locator(_COVER),
    "cover_title": lambda d: d.get_by_text(re.compile(r"^\s*Select a Covershot\.?\s*$", re.I)),
    "cover_thumbs": lambda d: d.locator(".image-edit-modal__thumb"),
    "cover_selected": lambda thumb: thumb.locator("svg.icon-green-checkmark"),
    "cover_crop": lambda d: d.locator(".croppie-container"),
    "cover_apply": lambda d: d.locator('[data-test="modal-footer"] button[data-et-name="apply"]'),
    "any_dialog": lambda p: p.locator('[data-test="modal-container"]:visible, [role="dialog"]:visible'),
    # The form's photo tiles: 6 for 6 photos in the Mac's form stage (2026-10-02).
    "photo_thumbs": lambda p: p.locator('#imagePlaceholder img:visible, img[src^="blob:"]:visible'),
    "leave": lambda p: p.locator('a[data-et-name="discard"]:visible'),     # Cancel: opens "Save Draft" (Mac, 10-02)
    # The "Share Listing" panel after Next (the Mac's review stage, 2026-10-02).
    "share_panel": lambda p: p.locator('[data-test="modal-container"]:visible, [role="dialog"]:visible, '
                                       '.modal:visible').filter(has_text=re.compile("Share Listing")),
    "review_back": lambda p: SEL["share_panel"](p).get_by_text(re.compile(r"^\s*\u2039?\s*Back\s*$")),
    "list_item": lambda p: SEL["share_panel"](p).locator('button[data-et-name="list"]'),
    "share_connect": lambda p: p.locator('a[data-et-name="pn_v2_connect"], a[data-et-name="fb_connect"]'),  # never
    # The closet's listing tiles (the first live listing, 2026-10-03): a cover link a.tile__covershot
    # [data-et-name=listing][data-et-prop-listing_id=<id>] whose image alt is the title, and a title link whose first
    # line is the title; both href="/listing/<slug>-<id>".
    "closet_links": lambda p: p.locator('a[href*="/listing/"]'),
    # Values, not locators. The listing's path (fullmatch on the path): /listing/<slug>-<24 hex id>. The redirect after
    # List This Item: /closet/<user>?created_listing_id=<24 hex id>.
    "listing_url": re.compile(r"/listing/(?P<slug>[^/?#]+)-(?P<id>[0-9a-f]{24})"),
    "created_id": re.compile(r"[?&]created_listing_id=(?P<id>[0-9a-f]{24})(?![0-9a-f])"),
    # ---- UNVERIFIED: see the module docstring ----
    "promote_toggle": lambda panel: panel.locator('input[type="checkbox"]'),
    "draft_saved": re.compile(r"/closet/|/listing/"),
    "captcha": lambda p: p.get_by_text(re.compile("captcha|verify you are human", re.I)),
}
UNVERIFIED = frozenset({"promote_toggle", "draft_saved", "captcha"})
PUBLISH_NEEDS = frozenset({"list_item", "listing_url"})      # submit() publishes only once these are recorded
DRAFT_NEEDS = frozenset({"draft_saved"})

THUMB_TIMEOUT_MS = 90_000        # 16 photos over home Wi-Fi can take a while
THUMB_POLL_MS = 500
MENU_TIMEOUT_MS = 8_000          # a menu or dialog to open; a list to show the wanted option
SUGGEST_TIMEOUT_MS = 8_000       # brand suggestions after typing
TAG_TIMEOUT_MS = 3_000           # a curated style tag to be offered
LEAVE_TIMEOUT_MS = 5_000          # the link that leaves the form; then the leave dialog to show
AFTER_LIST_MS = 45_000           # after List This Item: the page to leave the form
LEFT_FORM_MS = 3_000             # ... and, once it has, this long to settle before it is recorded
CLOSET_POLL_MS = 90_000          # then the closet is reloaded for this long until the new listing shows
CLOSET_EVERY_MS = 10_000         # ... once every this often
ID_SKEW_S = 600                  # a listing id created this long before the run started is still "new" (clock skew)
POLL_MS = 250
ROOT_TEXT_MAX = 300              # a dropdown "root" showing more text than this holds more than one field

# [id, visible text, "title|aria-label|data-et-name"] of each element in a list.
_ITEM_JS = """e => [e.id || '', (e.innerText || e.textContent || '').trim(),
    ['title', 'aria-label', 'data-et-name'].map(a => e.getAttribute(a) || '').join('|')]"""
_ITEMS_JS = f"els => els.map({_ITEM_JS})"
# The closet's listing links: [href, data-et-prop-listing_id, [its images' alt], the first line of its text] — a tile's
# title is the cover image's alt and the title link's first line ("<title>\n$90\nOS").
_LINKS_JS = """els => els.map(e => [e.getAttribute('href') || '', e.getAttribute('data-et-prop-listing_id') || '',
    [...e.querySelectorAll('img')].map(i => i.alt || '').filter(Boolean),
    ((e.innerText || '').trim().split('\\n')[0] || '').trim()])"""
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


def _letters(s: str) -> str:
    """ASCII letters and digits only, lowercased: how a title and its listing slug compare. Poshmark's slug drops
    everything but letters, digits and spaces and joins the words with "-", so "Toddler size 7.5" and
    "Toddler-size-75" are the same here (48 of 48 closet listings, 2026-10-03)."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def posh_slug(title: str) -> str:
    """The slug Poshmark gives a listing with this title: "Top - Black" -> "Top-Black", "7.5" -> "75"."""
    return "-".join(re.sub(r"[^A-Za-z0-9\s]", "", title).split())


def _created_id(urls: list[str]) -> str | None:
    """The listing id Poshmark names in its redirect after List This Item (?created_listing_id=<id>), if any."""
    for url in urls:
        if m := SEL["created_id"].search(url):
            return m["id"]
    return None


def _id_time(listing_id: str) -> int:
    """The creation time (epoch seconds) in a listing id: its first 8 hex digits, as in any MongoDB ObjectId."""
    return int(listing_id[:8], 16)


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
    counts_drafts = True                 # the create page shows "Drafts N": Poster.post checks each dry-run left none

    def __init__(self, username: str):
        self.username = username
        self.notes: list[str] = []
        self._roots: dict[str, ElementHandle | None] = {}   # dropdown -> the element that shows its choice
        self._state: dict = {}                             # what fill() saw for read_back: thumbnails, Smart Sell
        self._render: Render | None = None
        self._closet_before: set[str] = set()              # the closet's listing ids before this post
        self._started = 0.0                                # when this post started (epoch s): new ids are younger
        # The supervised first publish: an async (render, panel text) -> bool, True only when the owner typed LIST.
        self.confirm = None

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
        # What the closet lists now, so a new listing can be told from an older one with the same title.
        self._started = time.time()
        rows = await SEL["closet_links"](page).evaluate_all(_LINKS_JS)
        self._closet_before = set(self._listings(rows))

    async def drafts(self, page: Page) -> int | None:
        """The count in the create page's Drafts panel ("Drafts 0"), or None when it can't be read."""
        count = SEL["drafts_count"](page).first
        try:
            await count.wait_for(state="attached", timeout=MENU_TIMEOUT_MS)
            text = (await count.text_content() or "").strip()
        except PlaywrightTimeout:
            return None
        return int(text) if text.isdigit() else None

    # ---------------------------------------------------------------- fill

    async def fill(self, page: Page, r: Render) -> None:
        self._roots, self._state, self._render = {}, {}, r
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
        await self._confirm_size(page, choice)
        await settle(page, 0.3, 0.8)

    async def _confirm_size(self, page: Page, choice: SizeChoice) -> None:
        """After the pick, Poshmark's size menu either waits for Done (the inspection, 2026-09-29) or closes by itself
        with the size on the form's size field (Kids shoes, Mac dry-run #2, 2026-10-01). Either is fine, for any size;
        anything else fails the item (Poster.post keeps the screenshot and the page)."""
        done, field, sizes = SEL["size_done"](page), SEL["size_open"](page), SEL["size_buttons"](page)
        waited = 0
        while True:
            if await done.count():                     # (b) Done to press
                await done.first.click()
                self._state["size_menu"] = "Done"
                return
            shown = re.sub(r"\s+", " ", await field.inner_text()).strip() if await field.count() else ""
            if not await sizes.count() and Contains(choice.button).matches(shown):
                self._state["size_menu"] = "closed by itself"   # (a) the menu closed with the size on the form
                return
            if waited >= MENU_TIMEOUT_MS:
                raise PosterError(f"after picking '{choice.button}' the size menu neither showed Done nor closed with "
                                  f"that size on the form (the size field shows '{shown}')")
            await page.wait_for_timeout(POLL_MS)
            waited += POLL_MS

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
                    # The list may re-render between reading it and taking the element (a type-ahead filtering as
                    # you type): take it only if it is still there and still the option, else read the list again.
                    try:
                        handle = await items.nth(i).element_handle(timeout=POLL_MS * 2)
                    except PlaywrightTimeout:
                        handle = None
                    if handle is not None and _is(want, *await handle.evaluate(_ITEM_JS), loose):
                        return handle, options
                    break
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
            "size_menu": self._state.get("size_menu"),           # recorded only: "Done" | "closed by itself"
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
        draft?"), pressing "Discard Changes", so no draft is left behind (the Mac's form stage, 2026-10-02). A Share
        Listing panel still open (a review, a cancelled publish) is closed first: its backdrop takes every click meant
        for the form. Anything short of a Discard click is reported."""
        try:
            await self._close_share_panel(page)
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
        """Dry-run stage "review": press Next, record the Share Listing panel that slides up (a screenshot, the page's
        HTML and <item>-review.json with its buttons, tracked links, headings and labels), then ‹ Back. Only Next and
        ‹ Back are clicked here: never List This Item, never Promote My Closet or Connect Now."""
        await SEL["next"](page).click()
        panel = SEL["share_panel"](page)
        try:
            await panel.first.wait_for(state="visible", timeout=MENU_TIMEOUT_MS)
        except PlaywrightTimeout:
            pass                                       # recorded as it is; discard() still tries to leave
        await settle(page, 1.5, 2.5)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        await page.screenshot(path=str(shots / f"{r.sku}-{self.name}-{stamp}-review.png"), full_page=True)
        (shots / f"{r.sku}-review.html").write_text(await page.content(), encoding="utf-8")
        out = shots / f"{r.sku}-review.json"
        out.write_text(json.dumps(await page.evaluate(_RECORD_JS), indent=1), encoding="utf-8")
        try:
            await self._close_share_panel(page)
        except Exception as e:  # noqa: BLE001 — the record is written; discard() still tries to leave cleanly
            return f"review page recorded in {out.name}; could not close the Share Listing panel ({type(e).__name__})"
        await settle(page, 0.5, 1)
        return f"review page recorded in {out.name}"

    async def _close_share_panel(self, page: Page) -> None:
        """‹ Back on the Share Listing panel, then wait until it has slid away. Nothing to do when none is open."""
        panel = SEL["share_panel"](page)
        if not await panel.count():
            return
        await SEL["review_back"](page).first.click(timeout=LEAVE_TIMEOUT_MS)
        await panel.first.wait_for(state="hidden", timeout=LEAVE_TIMEOUT_MS)

    async def _check_share_panel(self, page: Page, r: Render) -> str:
        """Before List This Item: the panel shows this item's title and Promote My Closet is off (never touched). An
        unreadable toggle stops here, before anything is published. Returns the panel's text."""
        panel = SEL["share_panel"](page).first
        text = re.sub(r"\s+", " ", await panel.inner_text()).strip()
        if _key(r.title)[:40] not in _key(text):
            raise PosterError("the Share Listing panel doesn't show this item's title")
        toggle = SEL["promote_toggle"](panel)
        n = await toggle.count()
        if n > 1:
            raise PosterError(f"the Share Listing panel has {n} toggles: can't tell which is Promote My Closet")
        if n == 1 and await toggle.is_checked():
            raise PosterError("Promote My Closet is on in the Share Listing panel; it must stay off")
        if n == 0 and not re.search(r"promote my closet off\b", _key(text)):
            raise PosterError("can't tell whether Promote My Closet is off in the Share Listing panel")
        return text

    async def submit(self, page: Page, mode: Mode) -> str | None:
        self.clicked = False                           # until the click that can't be undone, the form can be left
        needs = PUBLISH_NEEDS if mode == "publish" else DRAFT_NEEDS
        if missing := sorted(needs & UNVERIFIED):
            raise PosterError(f"the {mode} step is not recorded yet ({', '.join(missing)} UNVERIFIED in "
                              "post/poshmark.py); keep poster.dry_run on until it is")
        if mode == "draft":
            self.clicked = True
            await SEL["save_draft"](page).first.click()
            await page.wait_for_url(SEL["draft_saved"], timeout=60_000)
            return None
        return await self._publish(page)

    async def _publish(self, page: Page) -> str:
        """Next, the Share Listing panel checked, the owner's LIST (supervised), List This Item pressed exactly once,
        then everything after the click recorded and the new listing's address found."""
        r = self._render
        await SEL["next"](page).click()
        await self._wait(SEL["share_panel"](page), "the Share Listing panel")
        text = await self._check_share_panel(page, r)
        button = SEL["list_item"](page)
        if (n := await button.count()) != 1 or _key(await button.first.inner_text()) != "list this item":
            raise PosterError(f"the Share Listing panel's List This Item differs from the recording ({n} found)")
        if self.confirm is not None and not await self.confirm(r, text):
            raise Cancelled("not published: LIST wasn't typed")
        before, navigations, native = page.url, [], []

        def on_navigated(frame) -> None:
            if frame == page.main_frame:
                navigations.append(frame.url)

        async def on_dialog(dialog) -> None:           # what Playwright does by default (dismiss), but recorded
            native.append({"type": dialog.type, "message": dialog.message[:300]})
            await dialog.dismiss()
        page.on("framenavigated", on_navigated)
        page.on("dialog", on_dialog)
        self.clicked = True
        click_error = None
        try:
            await button.first.click()                 # exactly once, whatever happens next
        except Exception as e:  # noqa: BLE001 — it may still have gone through: watch, record, never click again
            click_error = f"{type(e).__name__}: {(str(e).strip().splitlines() or [''])[0][:200]}"
        return await self._after_list(page, r, before, navigations, native, click_error)

    def _evidence(self, name: str, sku: str | None = None) -> Path:
        shot = self.shot or Path(f"{sku or (self._render.sku if self._render else 'item')}-{self.name}.png")
        return shot.with_name(f"{shot.stem}-{name}.png")

    def listing_address(self, url: str) -> str | None:
        """The canonical address of a listing on this site — https://poshmark.com/listing/<slug>-<24 hex id>, as the
        first live listing's (2026-10-03) — or None for anything else. A query, a fragment or a final "/" is dropped."""
        u, base = urlparse((url or "").strip()), urlparse(self.base_url)
        path = u.path.rstrip("/")
        if (u.scheme, u.netloc) != (base.scheme, base.netloc) or not SEL["listing_url"].fullmatch(path):
            return None
        return f"{self.base_url}{path}"

    def _listings(self, rows: list) -> dict[str, dict]:
        """The closet's listings by id, from _LINKS_JS rows: {"path", "slug", "titles"} (two links per tile)."""
        out: dict[str, dict] = {}
        for href, lid, alts, first in rows:
            m = SEL["listing_url"].fullmatch(urlparse(href).path.rstrip("/"))
            if not m or (lid and lid != m["id"]):
                continue
            e = out.setdefault(m["id"], {"path": m.group(0), "slug": m["slug"], "titles": set()})
            e["titles"] |= {_key(t) for t in [*alts, first] if t}
        return out

    def _ours(self, rows: list, r: Render, created: str | None) -> tuple[list[str], dict]:
        """This post's listing among the closet's: not there before the post (check_account), the tile shows this
        title AND the address carries a slug of this title, and it is the one Poshmark named (created_listing_id) —
        or, when Poshmark named none, an id created since the post started. Returns (the addresses, what was seen)."""
        listings = self._listings(rows)
        want, letters = _key(r.title), _letters(r.title)
        new = [lid for lid in listings if lid not in self._closet_before]
        titled = [lid for lid in new if want in listings[lid]["titles"] and _letters(listings[lid]["slug"]) == letters]
        if created:
            ours = [lid for lid in titled if lid == created]
        else:
            ours = [lid for lid in titled if _id_time(lid) >= self._started - ID_SKEW_S]
        seen = {"listings": len(listings), "new": new[:10], "new_with_this_title": titled,
                "created_listing_shown": created in listings if created else None}
        return [f"{self.base_url}{listings[lid]['path']}" for lid in ours], seen

    async def _poll_closet(self, page: Page, r: Render, created: str | None) -> tuple[str | None, dict]:
        """Reload the closet until this listing shows (CLOSET_POLL_MS, every CLOSET_EVERY_MS): Poshmark lands on
        /closet/<user> after List This Item, and the new listing was not there yet 5 s later (2026-10-03). The page
        Poshmark landed on is read first and then reloaded; any other landing is left alone and the closet is read in
        a new tab. Read only: nothing is clicked. Returns (the address or None, the record for <shot>-closet.json)."""
        closet = f"{self.base_url}/closet/{self.username}"
        landed = urlparse(page.url).path.rstrip("/") == f"/closet/{self.username}"
        tab = page if landed else await page.context.new_page()
        record: dict = {"created_listing_id": created, "listings_before": len(self._closet_before),
                        "landed_on_closet": landed, "polls": []}
        start = time.monotonic()
        try:
            while True:
                try:
                    if record["polls"] or not landed:
                        await tab.goto(closet)
                        await tab.wait_for_load_state("domcontentloaded")
                    try:
                        await SEL["closet_links"](tab).first.wait_for(state="attached", timeout=MENU_TIMEOUT_MS)
                    except PlaywrightTimeout:
                        pass
                    ours, seen = self._ours(await SEL["closet_links"](tab).evaluate_all(_LINKS_JS), r, created)
                except Exception as e:  # noqa: BLE001 — a reload that failed: try again on the next round
                    ours, seen = [], {"error": f"{type(e).__name__}"}
                record["polls"].append({"t_s": round(time.monotonic() - start, 1), **seen, "ours": ours})
                if len(ours) == 1:
                    others = [lid for lid in seen["new_with_this_title"] if not ours[0].endswith(lid)]
                    record["found"], record["other_new_with_this_title"] = ours[0], others
                    return ours[0], record
                if len(ours) > 1 or time.monotonic() - start >= CLOSET_POLL_MS / 1000:
                    return None, record                # several: never a guess; none: it may still be live
                await asyncio.sleep(CLOSET_EVERY_MS / 1000)
        finally:
            if tab is not page:
                await tab.close()

    async def _after_list(self, page: Page, r: Render, before: str, navigations: list[str], native: list[dict],
                          click_error: str | None) -> str:
        """Watch the page after List This Item until it leaves the form (its address, the dialogs it shows), keep it
        all as evidence (<shot>-after-list.png/.html/.json), then find the new listing's address: the page itself when
        it is a listing, else the closet, reloaded until the listing shows (<shot>-closet.json). Nothing is clicked
        here. No address found fails the item: it may be live."""
        waited, left_at, dialogs = 0, None, []
        while waited < AFTER_LIST_MS and not self.listing_address(page.url) and not page.is_closed():
            try:
                for text in await SEL["any_dialog"](page).all_inner_texts():
                    text = re.sub(r"\s+", " ", text).strip()[:300]
                    if text and text not in dialogs:
                        dialogs.append(text)
            except Exception:  # noqa: BLE001 — the page is navigating: look again next time
                pass
            if "/create-listing" not in page.url:      # it left the form, for something that isn't a listing
                left_at = waited if left_at is None else left_at
                if waited - left_at >= LEFT_FORM_MS:
                    break
            await asyncio.sleep(THUMB_POLL_MS / 1000)
            waited += THUMB_POLL_MS
        created = _created_id([*navigations, page.url])
        record = {"url_before": before, "url_after": page.url, "created_listing_id": created,
                  "click_error": click_error, "navigations": navigations, "dialogs": dialogs, "native_dialogs": native,
                  "waited_ms": waited}
        try:
            record["page"] = await page.evaluate(_RECORD_JS)
        except Exception:  # noqa: BLE001
            pass
        await keep_evidence(page, self._evidence("after-list"), record)
        if url := self.listing_address(page.url):
            return url
        url, closet = await self._poll_closet(page, r, created)
        self._evidence("closet").with_suffix(".json").write_text(json.dumps(closet, indent=1), encoding="utf-8")
        if url:
            if others := closet.get("other_new_with_this_title"):
                self.notes.append(f"another new listing with this title showed up in the closet ({', '.join(others)}):"
                                  " check for a duplicate")
            return url
        last = closet["polls"][-1] if closet["polls"] else {}
        several = len(last.get("ours") or [])
        why = (f"{several} new listings with this title in the closet, none named by Poshmark: never a guess"
               if several > 1 else
               f"the new listing didn't show in the closet within {CLOSET_POLL_MS // 1000} s"
               + (f" (Poshmark named it {created})" if created else ""))
        clicked = f"the click raised {click_error}; " if click_error else ""
        raise PosterError(f"after List This Item no listing address: {clicked}the page went to {page.url}; {why}. "
                          "It may be live: check the closet, then `thrift mark-posted`")

    async def verify_live(self, page: Page, url: str, r: Render) -> None:
        """The base check (title and price), plus whether the page carries the SKU (recorded: owner's view only?)."""
        await super().verify_live(page, url, r)
        record = {"url": url, "title": True, "price": True, "sku_on_page": r.sku in await page.content()}
        self._evidence("live", r.sku).with_suffix(".json").write_text(json.dumps(record, indent=1), encoding="utf-8")
