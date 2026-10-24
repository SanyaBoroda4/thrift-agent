"""PoshmarkPoster against a static copy of the create-listing form (tests/fixtures/poshmark_create_listing.html).

The fixture reproduces the form as recorded: the inspection of 2026-09-29 and the DOM snapshot of the first Mac
dry-run, 2026-09-30 (the "Select a Covershot." dialog, Poshmark's dropdown component, department links, category
<li>, subcategory <a> inside an <li> that ignores a click of its own, size tabs and buttons with the kids ids,
condition items with their codes and descriptions, colour tiles, the curated style tags, the Listing Price dialog with
Smart Sell, the SKU behind "show details", Cancel and its "Save Draft" dialog), plus the current guesses for what is
still UNVERIFIED (the photo tiles after Apply, closed-dropdown text, that Cancel opens the dialog, the page after
Next).

Real headless Chrome and real Playwright clicks. No network: every request of the test browser is answered from the
fixture or aborted, and the site lives at https://fixture.invalid, a name that can never resolve.
"""
import asyncio
import json
import time
from pathlib import Path

import pytest
from PIL import Image

pw_api = pytest.importorskip("playwright.async_api")

from thrift_agent import brands  # noqa: E402
from thrift_agent.post import poshmark  # noqa: E402
from thrift_agent.post.base import PosterError, Skipped, compare  # noqa: E402
from thrift_agent.schema import Render  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "poshmark_create_listing.html"
EDIT_FIXTURE = Path(__file__).parent / "fixtures" / "poshmark_edit_listing.html"
BASE = "https://fixture.invalid"
SKU = "i_260929_abc123"


@pytest.fixture(scope="module")
def chrome():
    """(event loop, browser): one headless Chrome for the module — the installed Chrome the poster uses, else
    Playwright's own Chromium. Skipped on a machine that has neither."""
    loop = asyncio.new_event_loop()

    async def start():
        pw = await pw_api.async_playwright().start()
        errors = []
        for kw in ({"channel": "chrome"}, {}):
            try:
                return pw, await pw.chromium.launch(headless=True, **kw)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{kw.get('channel', 'bundled chromium')}: {str(e).splitlines()[0]}")
        await pw.stop()
        raise RuntimeError("; ".join(errors))

    try:
        pw, browser = loop.run_until_complete(start())
    except Exception as e:  # noqa: BLE001
        loop.close()
        pytest.skip(f"no headless Chrome for Playwright here ({e})")
    yield loop, browser
    loop.run_until_complete(browser.close())
    loop.run_until_complete(pw.stop())
    loop.close()


def listing(title: str, price="85", made: float | None = None, n: int = 0, sku: str = "") -> dict:
    """A closet listing as Poshmark makes one: slug from the title, a 24-hex id whose first 8 digits are its creation
    time (default: an hour ago, i.e. older than any post a test starts)."""
    lid = f"{int(made if made is not None else time.time() - 3600):08x}{n:016x}"
    return {"slug": f"{poshmark.posh_slug(title)}-{lid}", "id": lid, "title": title, "price": str(price), "sku": sku,
            "shows_at": 0}


class Site:
    """Answers every request of one test context: the fixture for /create-listing; the closet (its listings, newest
    first, in Poshmark's tile markup) and each listing's page (title and price); the fixture's beacons /api/list-click
    and /api/created; a plain page for any other path of BASE; an abort for anything else. `urls` is every address the
    browser asked for. A listing made by the fixture shows in the closet only after `lag` more closet loads, as on the
    Mac, where the closet Poshmark landed on 5 s after List This Item did not show it yet."""

    def __init__(self, closet=(), appear=(), lag=1):
        self.urls: list[str] = []
        self.listings: list[dict] = [dict(x) for x in closet]     # listing() dicts, oldest first
        self.appear = [dict(x) for x in appear]                    # listed elsewhere while List This Item is pressed
        self.lag, self.closet_loads, self.list_clicks = lag, 0, 0
        self.updates = 0                                           # WO37: Update pressed on the edit page

    async def _html(self, route, body: str):
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

    def _tile(self, x: dict) -> str:
        from html import escape
        title, href = escape(x["title"]), f'/listing/{x["slug"]}'
        return (f'<div class="card"><a href="{href}" class="tile__covershot" data-et-name="listing" '
                f'data-et-prop-listing_id="{x["id"]}"><div class="img__container"><img alt="{title}"></div>'
                f'<div class="views">73</div></a><a href="{href}" class="tile__title" data-et-name="listing" '
                f'data-et-prop-listing_id="{x["id"]}"><div>{title}</div>'
                f'<div>${x["price"]}</div><div>OS</div></a></div>')

    async def handle(self, route):
        from html import escape
        from urllib.parse import parse_qs, urlparse
        url = route.request.url
        self.urls.append(url)
        path = urlparse(url).path
        if not url.startswith(BASE + "/"):
            await route.abort()
        elif path == "/create-listing":
            await self._html(route, FIXTURE.read_text(encoding="utf-8"))
        elif path.startswith("/edit-listing/"):                    # WO37: the edit page's photos
            await self._html(route, EDIT_FIXTURE.read_text(encoding="utf-8"))
        elif path == "/api/update-pressed":
            await route.fulfill(status=204, body="")
        elif path == "/api/update":
            self.updates += 1
            await route.fulfill(status=204, body="")
        elif path == "/api/list-click":
            self.list_clicks += 1
            self.listings += [{**x, "shows_at": self.closet_loads + 1 + self.lag} for x in self.appear]
            await route.fulfill(status=204, body="")
        elif path == "/api/created":
            q = parse_qs(urlparse(url).query)
            x = listing(q["title"][0], q.get("price", [""])[0], made=time.time(), n=len(self.listings),
                        sku=q.get("sku", [""])[0])
            self.listings.append({**x, "shows_at": self.closet_loads + 1 + self.lag})
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps({"slug": x["slug"], "id": x["id"]}))
        elif path.startswith("/closet/"):
            self.closet_loads += 1
            shown = [x for x in self.listings if x["shows_at"] <= self.closet_loads]
            tiles = "".join(self._tile(x) for x in reversed(shown))
            drop = "<script>history.replaceState(null, '', location.pathname)</script>"   # as Poshmark: no query
            await self._html(route, f"<html><body><h1>closet</h1>{tiles}{drop}</body></html>")
        elif path.startswith("/listing/"):
            x = next((x for x in self.listings if x["slug"] == path.split("/")[-1]), None)
            sku = f'<p class="sku">SKU {escape(x["sku"])}</p>' if x and x.get("sku") else ""
            await self._html(route, f'<html><body><h1>{escape(x["title"])}</h1><p class="price">${x["price"]}</p>'
                                    f"{sku}</body></html>" if x else "<html><body>Not Found</body></html>")
        else:
            await self._html(route, "<html><body><p>elsewhere</p></body></html>")

    def visited(self, part: str) -> list[str]:
        return [u for u in self.urls if part in u]


def drive(chrome, scenario, closet=(), appear=(), lag=1, **variant):
    """Run `scenario(ctx, site)` in a fresh context; `variant` becomes window.__FIXTURE (see the fixture's header);
    `closet` is the listings the closet holds before the run."""
    loop, browser = chrome

    async def go():
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        site = Site(closet, appear, lag)
        await ctx.route("**/*", site.handle)
        await ctx.add_init_script(f"window.__FIXTURE = {json.dumps(variant)};")
        try:
            return await scenario(ctx, site)
        finally:
            await ctx.close()

    return loop.run_until_complete(go())


@pytest.fixture
def posh(monkeypatch):
    """A PoshmarkPoster on the fixture site, without the human pauses and with short timeouts."""
    async def instant(*a, **k):
        pass

    async def quick_type(loc, value):
        await loc.click()
        await loc.fill(value)                    # one input event instead of a keystroke each: same suggestions

    monkeypatch.setattr(poshmark, "settle", instant)
    monkeypatch.setattr(poshmark, "human_type", quick_type)
    # 10-20x what a step takes on a laptop, so a slow CI runner still passes; the "not offered" tests wait them out.
    for name, ms in (("MENU_TIMEOUT_MS", 2000), ("SUGGEST_TIMEOUT_MS", 1500), ("TAG_TIMEOUT_MS", 800),
                     ("LEAVE_TIMEOUT_MS", 1500), ("THUMB_TIMEOUT_MS", 5000), ("POLL_MS", 25),
                     ("AFTER_LIST_MS", 2000), ("LEFT_FORM_MS", 300), ("CLOSET_POLL_MS", 6000),
                     ("CLOSET_EVERY_MS", 150)):
        monkeypatch.setattr(poshmark, name, ms)
    p = poshmark.PoshmarkPoster("closet")
    p.base_url = BASE
    return p


@pytest.fixture
def photos(tmp_path):
    out = []
    for i, color in enumerate(["red", "green", "blue"]):
        path = tmp_path / "photos" / f"{i:02d}.jpg"
        path.parent.mkdir(exist_ok=True)
        Image.new("RGB", (64, 64), color).save(path)
        out.append(str(path))
    return out


def render(photos, **kw) -> Render:
    base = dict(marketplace="poshmark", title="Tory Burch Minnie Red Ballet Flats size 7.5",
                description="Red ballet flats with the gold logo.\nWorn twice, light wear on the soles.\nRetail $228.",
                tags=["Casual"], brand="Tory Burch", department="Women", category="Shoes",
                subcategory="Flats & Loafers", size="7.5", colors=["Red"], condition="excellent", price=85,
                original_price=228, photos=photos, sku=SKU)
    base.update(kw)
    return Render(**base)


async def open_form(ctx, p):
    page = await ctx.new_page()
    await page.goto(p.create_url)
    return page


async def run_steps(p, page, r, steps):
    """fill(), or only the named fill steps in order ("_category", "_size", ...): a test about one step skips the
    rest of the form (each real click costs Playwright's actionability wait)."""
    if steps is None:
        await p.fill(page, r)
    for step in steps or ():
        await getattr(p, step)(page, r)


def fill_and_read(chrome, p, r, steps=None, **variant):
    """(read_back, the fixture's event log) after fill() or the given steps."""
    async def scenario(ctx, site):
        page = await open_form(ctx, p)
        await run_steps(p, page, r, steps)
        return await p.read_back(page), await page.evaluate("window.__events")
    return drive(chrome, scenario, **variant)


def diff_on(seen, r, p, *keys):
    """compare() limited to the fields a step-level test filled."""
    return {k: v for k, v in compare(seen, p.expected(r)).items() if k in keys}


def impatient(monkeypatch, ms=400):
    """Short waits from here on, for a test that waits out an option the form does not offer."""
    for name in ("MENU_TIMEOUT_MS", "SUGGEST_TIMEOUT_MS", "LEAVE_TIMEOUT_MS"):
        monkeypatch.setattr(poshmark, name, ms)


def fill_expecting(chrome, p, r, error, steps=None, monkeypatch=None, **variant):
    """The exception fill() raised (or, with `steps`, those fill steps in order: "_category", "_size", ...), plus the
    listing price on the form at that moment (empty = it stopped before the price). With `monkeypatch`, the last step
    runs impatient: the steps before it keep the generous waits."""
    async def scenario(ctx, site):
        page = await open_form(ctx, p)
        with pytest.raises(error) as info:
            await run_steps(p, page, r, steps[:-1] if steps and monkeypatch else steps)
            if steps and monkeypatch:
                impatient(monkeypatch)
                await run_steps(p, page, r, steps[-1:])
        return info.value, await page.locator('input[data-vv-name="listingPrice"]').input_value()
    return drive(chrome, scenario, **variant)


# ---------------------------------------------------------------- the whole form

def test_women_shoes_fill_reads_back_exactly_what_was_planned(chrome, posh, photos):
    r = render(photos)
    seen, events = fill_and_read(chrome, posh, r)
    assert compare(seen, posh.expected(r)) == {}, seen
    assert events == ["cover-apply", "category:Women/Shoes", "subcategory:Flats & Loafers", "size-tab:Standard",
                      "size:7.5", "condition:Like New", "brand:Tory Burch", "color:Red", "price-done",
                      "show-details"]          # the SKU sits behind "show details"; no style tag (WO36), "Casual" planned
    assert seen["cover_dialog"] == {"photos": 3, "crop": "Poshmark's default"}
    assert seen["photos"] == 3 and seen["price"] == "$85" and seen["original_price"] == "$228"
    assert seen["category"] == "Women Shoes" and seen["subcategory"] == "Flats & Loafers" and seen["size"] == "7.5"
    assert seen["smart_sell"] == "off" and seen["sku"] == SKU
    assert "Shipping Discount Optional" in seen["price_dialog"]           # the recorded default: nothing chosen
    assert posh.notes == []


def test_the_form_as_filled_matches_every_condition_label(chrome, posh, photos):
    """NWOT has no label of its own on Poshmark: it goes up as Like New; excellent as Good."""
    r = render(photos, condition="NWOT", colors=["Black", "White"], original_price=None, tags=[])
    seen, events = fill_and_read(chrome, posh, r, steps=["_condition", "_colors", "_price"])
    assert diff_on(seen, r, posh, "condition", "colors", "price", "original_price", "smart_sell") == {}, seen
    assert "condition:Like New" in events and ["color:Black", "color:White"] == [e for e in events if "color" in e]
    assert seen["original_price"] == "0" and seen["colors"] == "Black White"       # Poshmark's "0" for empty


@pytest.mark.parametrize("gender,size,tab,button", [
    ("unisex", "EU 24 / US Toddler 7.5", "Girls", "7.5 (Toddler Girl)"),
    (None, "EU 31 / US Little Kid 13", "Girls", "13 (Little Girl)"),
    ("boys", "US Big Kid 4", "Boys", "4 (Big Boy)"),
    ("girls", "US Big Kid 2", "Girls", "2 (Little Girl)"),         # labels a Render got before WO10: Poshmark
    ("boys", "EU 29 / US Little Kid 11.5", "Boys", "11.5 (Toddler Boy)"),   # calls 1-3Y Little, C up to 12 Toddler
])
def test_kids_shoes_go_on_the_gender_tab_with_poshmarks_own_label(chrome, posh, photos, gender, size, tab, button):
    r = render(photos, department="Kids", category="Shoes", subcategory="Sneakers", size=size, kids_gender=gender,
               brand="Nike", title="Nike Pink Sneakers", colors=["Pink"], condition="good", original_price=None)
    seen, events = fill_and_read(chrome, posh, r, steps=["_category", "_size"])
    assert diff_on(seen, r, posh, "category", "subcategory", "size") == {}, seen
    assert f"size-tab:{tab}" in events and f"size:{button}" in events and seen["size"] == button


def test_a_menu_that_reopens_inside_a_department_goes_back_through_all(chrome, posh, photos):
    r = render(photos)
    seen, events = fill_and_read(chrome, posh, r, steps=["_category"], lastDept="kids")   # Poshmark remembered Kids
    assert events == ["category:Women/Shoes", "subcategory:Flats & Loafers"]
    assert diff_on(seen, r, posh, "category", "subcategory") == {}


def test_no_subcategory_picks_none(chrome, posh, photos):
    r = render(photos, subcategory=None)
    seen, events = fill_and_read(chrome, posh, r, steps=["_category"])
    assert events == ["category:Women/Shoes", "subcategory:None"] and diff_on(seen, r, posh, "category") == {}


def test_a_js_click_on_a_subcategory_li_does_not_register_the_poster_clicks_the_a(chrome, posh, photos):
    """The trap seen on the live form: element.click() on the <li> is lost; the poster's real click on the <a> works."""
    async def scenario(ctx, site):
        page = await open_form(ctx, posh)
        await posh._category(page, render(photos))
        await page.locator("#subcategory-selector").click()                    # reopen the menu
        await page.evaluate("""[...document.querySelectorAll('#subcategory-menu li')]
                               .find(li => li.innerText.trim() === 'Sneakers').click()""")
        return await page.locator("#subcategory-selector").inner_text()
    assert drive(chrome, scenario) == "Flats & Loafers"


def test_no_style_tags_are_typed(chrome, posh, photos):
    """WO36 (the owner, 2026-10-12: the Alo set's "Coord Sets" and "Athleisure" deleted by hand): Poshmark's Style Tags
    stay empty, whatever an older listing still carries."""
    r = render(photos, tags=["Casual", "Coord Sets"])

    async def scenario(ctx, site):
        page = await open_form(ctx, posh)
        await posh._tags(page, r)
        return (await page.evaluate("window.__events"), await page.locator("#tag-chips .tag-chip").all_inner_texts())
    events, chips = drive(chrome, scenario)
    assert not [e for e in events if "tag" in e] and chips == []


def test_curated_style_tags_only(chrome, posh, photos, monkeypatch):
    """WO12's step, kept behind ALLOW_STYLE_TAGS: Poshmark's curated tags only, never a free-typed one."""
    monkeypatch.setattr(poshmark, "ALLOW_STYLE_TAGS", True)
    r = render(photos, tags=["Casual", "Sparkly unicorn", "Denim"])

    async def scenario(ctx, site):
        page = await open_form(ctx, posh)
        await posh._tags(page, r)
        return (await page.evaluate("window.__events"), await page.locator("#tag-chips .tag-chip").all_inner_texts(),
                await page.locator('input[data-vv-name="style-tag-input"]').input_value())
    events, chips, box = drive(chrome, scenario)
    assert [e for e in events if "tag" in e] == ["tag:Casual", "tag:Denim"]         # never a free-typed tag
    assert chips == ["Casual", "Denim"] and box == ""
    assert posh.notes == ["style tags Poshmark doesn't offer, left out: Sparkly unicorn"]


# ---------------------------------------------------------------- never stops to ask (WO27)

@pytest.mark.parametrize("brand,offer", [("Tory", " (it offers: Tory Burch, Tory Sport, J. Crew Factory)"),   # facTORY
                                         ("Zzyzx", "")])
def test_a_brand_poshmark_does_not_offer_is_left_empty_and_the_form_goes_on(chrome, posh, photos, brand, offer,
                                                                            monkeypatch):
    monkeypatch.setattr(poshmark, "SUGGEST_TIMEOUT_MS", 400)             # only the brand waits on suggestions
    r = render(photos, brand=brand)
    seen, events = fill_and_read(chrome, posh, r)
    assert not [e for e in events if e.startswith("brand:")] and seen["brand"] == ""
    assert compare(seen, posh.expected(r)) == {}, seen                    # filled to the end: the price is on
    assert seen["price"] == "$85"
    assert posh.guesses == [f"brand left empty: Poshmark's list has no '{brand}'{offer}"]


def test_j_crew_is_poshmarks_j_crew_never_the_factory_line_and_is_learned(chrome, posh, photos, tmp_path, monkeypatch):
    monkeypatch.setattr(brands, "SEED", {})                               # as live, before anything was learned
    posh.aliases = brands.Aliases(tmp_path / "brand_aliases.learned.json", curated=None)
    r = render(photos, brand="J.Crew", title="J.Crew Red Flats size 7.5")
    seen, events = fill_and_read(chrome, posh, r, steps=["_brand"])      # "J.Crew" finds nothing, "J. Crew" both lines
    assert events == ["brand:J. Crew"] and seen["brand"] == "J. Crew" and diff_on(seen, r, posh, "brand") == {}
    assert posh.guesses == ["brand set to 'J. Crew' (from 'J.Crew')"]
    assert posh.aliases.table() == {"jcrew": "J. Crew"}                   # learned: exact from now on

    posh.guesses = []
    seen, events = fill_and_read(chrome, posh, r, steps=["_brand"])
    assert events == ["brand:J. Crew"] and posh.guesses == []             # the second time: no guess to report


def test_a_two_part_brand_finds_the_part_poshmark_lists(chrome, posh, photos, tmp_path, monkeypatch):
    """WO34, live: "MNG (Mango)" left the brand empty — the full name, then each part ("MNG", "Mango"), case aside;
    the part Poshmark lists is picked, reported once and learned."""
    monkeypatch.setattr(brands, "SEED", {})
    posh.aliases = brands.Aliases(tmp_path / "brand_aliases.learned.json", curated=None)
    r = render(photos, brand="MNG (Mango)", title="MNG (Mango) Knit Cardigan size S")
    seen, events = fill_and_read(chrome, posh, r, steps=["_brand"])
    assert events == ["brand:Mango"] and seen["brand"] == "Mango"
    assert posh.guesses == ["brand set to 'Mango' (from 'MNG (Mango)')"]
    assert posh.aliases.spell("MNG (Mango)") == "Mango"                   # learned: straight to it next time


def test_a_brand_poshmark_lists_only_by_one_word_of_ours_is_never_taken(chrome, posh, photos, monkeypatch):
    """The owner, 2026-10-11: "Tory Burch Sport" isn't on the list — Poshmark's "Tory Burch" (fewer words) and "Tory
    Sport" (a word of ours) are never taken, nor its first or longest word typed: Brand left empty (it is optional);
    the real brand stays in the title and description."""
    monkeypatch.setattr(poshmark, "SUGGEST_TIMEOUT_MS", 400)
    monkeypatch.setattr(brands, "SEED", {})
    r = render(photos, brand="Tory Burch Sport")
    seen, events = fill_and_read(chrome, posh, r, steps=["_brand"])
    assert not [e for e in events if e.startswith("brand:")] and seen["brand"] == ""
    assert posh.guesses == ["brand left empty: Poshmark's list has no 'Tory Burch Sport'"]


# ---------------------------------------------------------------- WO36: the brand Poshmark lists (the Alo set)

def typed_brands(chrome, p, r, **variant):
    """The brand step alone: (read_back, events, every text typed into Brand)."""
    async def scenario(ctx, site):
        page = await open_form(ctx, p)
        await p._brand(page, r)
        events = await page.evaluate("window.__events")
        return await p.read_back(page), events, await page.evaluate("window.__brandTyped")
    return drive(chrome, scenario, **variant)


@pytest.mark.parametrize("brand,label,curated,guesses", [
    ("Alo Yoga", "alo", True, []),                         # the copy step's name for an "alo" label: as listed
    ("Alo", None, True, []),                               # an older listing: the alias file's Alo Yoga
    ("Alo", None, False, ["brand set to 'ALO Yoga' (from 'Alo')"]),   # no alias: the one name that is Alo + more words
])
def test_alo_is_poshmarks_alo_yoga(chrome, posh, photos, tmp_path, monkeypatch, brand, label, curated, guesses):
    """WO36, live 2026-10-12: "Alo" typed, Poshmark offered ALO Yoga, ALOHA Collection, ALOHAS, Aloha Island, Alorna —
    and the exact-name rule left Brand empty. Now: the shop's name, the label's, the alias file's, then the ONE option
    that is a name of ours followed by more words (whole words: never ALOHAS)."""
    monkeypatch.setattr(poshmark, "SUGGEST_TIMEOUT_MS", 600)
    posh.aliases = brands.Aliases(tmp_path / "learned.json", curated=brands.CURATED if curated else None)
    r = render(photos, brand=brand, brand_label=label, title=f"{brand} Cropped Hoodie & Joggers Set size XS")
    seen, events, typed = typed_brands(chrome, posh, r)
    assert [e for e in events if e.startswith("brand:")] == ["brand:ALO Yoga"] and seen["brand"] == "ALO Yoga"
    assert posh.guesses == guesses and diff_on(seen, r, posh, "brand") == {}
    assert not any(t.lower().startswith("aloha") for t in typed)


def test_two_names_that_start_with_ours_are_no_guess(chrome, posh, photos, tmp_path, monkeypatch):
    """WO36: an option that is our name followed by more words is taken only when it is the ONLY one — two (Alo Yoga,
    Alo Kids) are never guessed between: Brand left empty, the note naming what Poshmark offered (for the alias)."""
    monkeypatch.setattr(poshmark, "SUGGEST_TIMEOUT_MS", 600)
    posh.aliases = brands.Aliases(tmp_path / "learned.json", curated=None)
    r = render(photos, brand="Alo")
    seen, events, _ = typed_brands(chrome, posh, r, brands=["Alo Yoga", "Alo Kids", "ALOHAS"])
    assert not [e for e in events if e.startswith("brand:")] and seen["brand"] == ""
    assert posh.guesses == ["brand left empty: Poshmark's list has no 'Alo' (it offers: Alo Yoga, Alo Kids, ALOHAS)"]


def test_solid_and_striped_is_never_solid(chrome, posh, photos, monkeypatch):
    """The owner, 2026-10-11 and WO36: never the other way round — "Solid & Striped" never lands on Poshmark's
    "Solid", and its first word is never typed."""
    monkeypatch.setattr(poshmark, "SUGGEST_TIMEOUT_MS", 600)
    r = render(photos, brand="Solid & Striped")
    seen, events, typed = typed_brands(chrome, posh, r)
    assert not [e for e in events if e.startswith("brand:")] and seen["brand"] == ""
    assert "Solid" not in typed and set(typed) <= {"Solid & Striped", "Solid and Striped", ""}
    assert posh.guesses == ["brand left empty: Poshmark's list has no 'Solid & Striped'"]


def test_mng_finds_mango_by_the_alias_file(chrome, posh, photos, tmp_path):
    """WO36: "MNG" — the label's text — is Mango by the alias file; nothing to report (not a guess)."""
    posh.aliases = brands.Aliases(tmp_path / "learned.json")
    r = render(photos, brand="MNG")
    seen, events, typed = typed_brands(chrome, posh, r)
    assert [e for e in events if e.startswith("brand:")] == ["brand:Mango"] and posh.guesses == []
    assert typed[-1] == "Mango"


def test_a_brand_the_form_shows_differently_is_cleared_once_before_the_diff(chrome, posh, photos):
    """WO36 §4: the read-back finds another brand on the form (an optional field) — cleared, read back again: the form
    goes on without a brand, never with a wrong one."""
    r = render(photos)

    async def scenario(ctx, site):
        page = await open_form(ctx, posh)
        await posh._brand(page, r)
        await page.locator("#brand-input").fill("Vince Camuto")          # the form shows another brand
        seen = await posh.read_back(page)
        diff = {k: v for k, v in compare(seen, posh.expected(r)).items() if k == "brand"}
        fixed = await posh.fix(page, r, diff)
        return diff, fixed, await posh.read_back(page)
    diff, fixed, seen = drive(chrome, scenario)
    assert diff == {"brand": ("Tory Burch", "Vince Camuto")} and fixed
    assert seen["brand"] == "" and diff_on(seen, r, posh, "brand") == {}
    assert posh.guesses == ["brand left empty: the form read 'Vince Camuto', not 'Tory Burch'"]


def test_the_factory_line_is_picked_only_for_the_factory_line(chrome, posh, photos, monkeypatch):
    monkeypatch.setattr(brands, "SEED", {})
    r = render(photos, brand="J.Crew Factory")
    seen, events = fill_and_read(chrome, posh, r, steps=["_brand"])
    assert events == ["brand:J. Crew Factory"] and posh.guesses == ["brand set to 'J. Crew Factory' (from "
                                                                    "'J.Crew Factory')"]


def test_the_seeded_spelling_needs_no_guess(chrome, posh, photos):
    r = render(photos, brand="J.Crew")
    seen, events = fill_and_read(chrome, posh, r, steps=["_brand"])
    assert events == ["brand:J. Crew"] and posh.guesses == [] and diff_on(seen, r, posh, "brand") == {}


def test_brand_match_ignores_case_and_curly_quotes(chrome, posh, photos):
    r = render(photos, brand="levi’s", title="Levi's Red Flats size 7.5")
    seen, events = fill_and_read(chrome, posh, r, steps=["_brand"])
    assert events == ["brand:Levi's"] and seen["brand"] == "Levi's" and diff_on(seen, r, posh, "brand") == {}


@pytest.mark.parametrize("change,chosen,guess", [
    (dict(subcategory="Flats"), {"subcategory": "Flats & Loafers"}, "subcategory set to 'Flats & Loafers' (from 'Flats')"),
    # WO36: never a merely similar one (WO27 took "Over the Knee Boots" for knee-high boots)
    (dict(subcategory="Knee High Boots"), {"subcategory": None},
     "subcategory left out (Poshmark has no 'Knee High Boots' under Shoes)"),
    (dict(subcategory="Zzyzx Shoes"), {"subcategory": None}, "subcategory left out (Poshmark has no 'Zzyzx Shoes' "
                                                             "under Shoes)"),
])
def test_a_subcategory_not_offered_is_ours_with_more_words_or_none(chrome, posh, photos, change, chosen, guess):
    r = render(photos, **change)
    seen, events = fill_and_read(chrome, posh, r, steps=["_category"])
    assert {k: posh.chosen[k] for k in chosen} == chosen and posh.guesses == [guess]
    assert diff_on(seen, r, posh, "category", "subcategory") == {}, seen
    if chosen.get("subcategory", "") is None:
        assert "subcategory:None" in events


@pytest.mark.parametrize("change,why", [
    (dict(category="Sweatshirts"), "Poshmark has no category 'Sweatshirts' under Women (it offers: Accessories, "
                                   "Bags, Dresses, Jeans, Shorts, Shoes, Sweaters, Tops, Other) (the category is "
                                   "required)"),
    (dict(category="Dress", subcategory=None), "Poshmark has no category 'Dress' under Women (it offers: Accessories, "
                                               "Bags, Dresses, Jeans, Shorts, Shoes, Sweaters, Tops, Other) (the "
                                               "category is required)"),    # WO36: "Dresses" is not "Dress"
    (dict(department="Unisex"), "Poshmark has no 'Unisex' department (the category is required)"),
])
def test_a_category_nothing_on_the_form_comes_close_to_skips_the_item(chrome, posh, photos, change, why,
                                                                       monkeypatch):
    err, price = fill_expecting(chrome, posh, render(photos, **change), Skipped, steps=["_category"],
                                monkeypatch=monkeypatch)
    assert str(err) == why and price == ""


def test_a_size_not_on_the_menu_takes_the_nearest_of_its_kind_one_size_away_at_most(chrome, posh, photos,
                                                                                    monkeypatch):
    r = render(photos, size="12.5")                                  # Women's shoes stop at 12
    seen, _ = fill_and_read(chrome, posh, r, steps=["_category", "_size"])
    assert posh.chosen["size"] == "12" and posh.guesses == ["size set to '12' (from '12.5')"]
    assert diff_on(seen, r, posh, "size") == {}, seen

    posh.guesses = []
    err, _ = fill_expecting(chrome, posh, render(photos, size="15"), Skipped, steps=["_category", "_size"],
                            monkeypatch=monkeypatch)
    assert str(err).startswith("size '15' isn't on Poshmark's Women/Shoes (Standard) menu, nor one near it (it offers: "
                               "5, 5.5, ")
    assert str(err).endswith("(the size is required)")

    dress = render(photos, category="Dresses", subcategory="Midi", size="5X")         # Plus: 0X-3X
    err, _ = fill_expecting(chrome, posh, dress, Skipped, steps=["_category", "_size"], monkeypatch=monkeypatch)
    assert "size '5X' isn't on Poshmark's Women/Dresses (Plus) menu" in str(err)


def test_smart_sell_switched_on_stops_the_item(chrome, posh, photos):
    err, price = fill_expecting(chrome, posh, render(photos), PosterError, steps=["_price"], smartSell=True)
    assert "Smart Sell is on" in str(err) and price == ""                   # Done was never pressed


def test_a_condition_item_that_reads_differently_stops_the_item(chrome, posh, photos):
    err, _ = fill_expecting(chrome, posh, render(photos, condition="like_new"), PosterError, steps=["_condition"],
                            conditionLabel={"uln": "Excellent"})
    assert "condition uln reads 'Excellent', expected 'Like New'" in str(err)


def test_categories_shaped_like_the_department_links_work_too(chrome, posh, photos):
    """The snapshot shows the class dropdown__menu__item on the departments' <a>; the inspection saw it on the
    categories' <li>. Either shape is picked."""
    r = render(photos)
    seen, events = fill_and_read(chrome, posh, r, steps=["_category"], categoryLinks=True)
    assert events == ["category:Women/Shoes", "subcategory:Flats & Loafers"]
    assert diff_on(seen, r, posh, "category", "subcategory") == {}


# ---------------------------------------------------------------- the cover dialog after the upload (WO11)

def test_the_cover_dialog_is_applied_with_poshmarks_default_crop(chrome, posh, photos):
    """Recorded on the Mac: "Select a Covershot.", one tile per photo, the first preselected, a crop frame with a zoom
    slider and rotate buttons, Cancel / Apply. Apply, crop untouched, the dialog closes, every tile shows."""
    async def scenario(ctx, site):
        page = await open_form(ctx, posh)
        await posh._photos(page, render(photos))
        return (await page.evaluate("window.__events"), await page.locator("#cover-modal").is_visible(),
                await posh.read_back(page))
    events, dialog_open, seen = drive(chrome, scenario)
    assert events == ["cover-apply"]                     # never the zoom slider, rotate, Cancel or Replace Photo
    assert not dialog_open and seen["photos"] == 3
    assert seen["cover_dialog"] == {"photos": 3, "crop": "Poshmark's default"}


def test_a_cover_dialog_that_lists_the_photos_slowly_is_waited_for(chrome, posh, photos):
    async def scenario(ctx, site):
        page = await open_form(ctx, posh)
        await posh._photos(page, render(photos))
        return await page.evaluate("window.__events"), await posh.read_back(page)
    events, seen = drive(chrome, scenario, slowCover=True)
    assert events == ["cover-apply"] and seen["cover_dialog"]["photos"] == 3 and seen["photos"] == 3


def test_a_cover_dialog_whose_checkmark_comes_late_is_waited_for(chrome, posh, photos):
    """Live 2026-10-18 (the Marc Jacobs bag, 11 photos): the first thumb's green checkmark was drawn a moment after the
    last thumbnail — read once, "did not preselect the first photo"."""
    seen, events = fill_and_read(chrome, posh, render(photos), steps=["_photos"], coverCheckLate=True)
    assert events == ["cover-apply"] and seen["cover_dialog"]["photos"] == 3 and seen["photos"] == 3


def test_no_cover_dialog_is_fine(chrome, posh, photos):
    async def scenario(ctx, site):
        page = await open_form(ctx, posh)
        await posh._photos(page, render(photos))
        return await posh.read_back(page)
    seen = drive(chrome, scenario, noCoverDialog=True)
    assert seen["photos"] == 3 and seen["cover_dialog"] is None


@pytest.mark.parametrize("variant,why", [
    (dict(coverNoApply=True), "Poshmark's cover dialog differs from the recorded one: the confirm button reads 'Save'"),
    (dict(coverSecondSelected=True), "Poshmark's cover dialog did not preselect the first photo (the cover)"),
    (dict(strangeDialog=True), "a dialog that isn't recorded opened after the photo upload: 'Photo upload failed OK'"),
])
def test_a_dialog_unlike_the_recorded_one_fails_the_item(chrome, posh, photos, variant, why):
    err, _ = fill_expecting(chrome, posh, render(photos), PosterError, steps=["_photos"], **variant)
    assert not isinstance(err, Skipped) and str(err) == why


def test_a_failure_inside_the_cover_dialog_leaves_through_discard(chrome, posh, photos):
    """Live 2026-10-18 (the Marc Jacobs bag): the cover dialog stayed open after the failure and its backdrop took the
    Cancel click — "left the form without Poshmark's Discard dialog". The dialog is cancelled first."""
    async def scenario(ctx, site):
        page = await open_form(ctx, posh)
        with pytest.raises(PosterError):
            await posh._photos(page, render(photos))
        note = await posh.discard(page)
        return note, page.url, posh.notes
    note, url, notes = drive(chrome, scenario, coverSecondSelected=True)
    assert note is None and url.endswith("/feed?discarded=1")           # Discard Changes was pressed, no draft
    assert "the cover dialog was still open: cancelled before leaving" in notes


def test_an_unrecorded_dialog_fails_the_dry_run_with_the_evidence(chrome, posh, photos, tmp_path):
    shots = tmp_path / "shots"

    async def scenario(ctx, site):
        return await posh.post(ctx, render(photos), "draft", True, shots), site
    out, site = drive(chrome, scenario, strangeDialog=True)
    assert out.status == "failed" and "a dialog that isn't recorded opened after the photo upload" in out.error
    shot = Path(out.screenshot)
    assert shot.exists() and "Photo upload failed" in shot.with_suffix(".html").read_text(encoding="utf-8")
    assert not site.visited("/listing/") and not site.visited("draft=1")


# ---------------------------------------------------------------- Poster.post(): dry-run stages end to end

def post(chrome, p, r, shots, stage="form", mode="draft", **variant):
    async def scenario(ctx, site):
        return await p.post(ctx, r, mode, True, shots, stage=stage), site
    return drive(chrome, scenario, **variant)


def test_dry_run_form_stage_fills_keeps_the_evidence_and_leaves_through_discard(chrome, posh, photos, tmp_path):
    shots = tmp_path / "shots"
    out, site = post(chrome, posh, render(photos), shots)
    assert out.status == "dryrun" and out.note is None, out
    assert site.visited("/closet/closet") and site.visited("/feed?discarded=1")        # account check, then Discard
    assert not site.visited("/listing/") and not site.visited("draft=1")            # nothing published or saved
    shot = Path(out.screenshot)
    assert shot.exists() and 'data-vv-name="title"' in shot.with_suffix(".html").read_text(encoding="utf-8")
    record = json.loads(shot.with_suffix(".json").read_text(encoding="utf-8"))
    assert record["diff"] == {} and record["seen"]["size"] == "7.5" and record["expected"]["size"] == "contains '7.5'"


def test_dry_run_review_stage_records_the_page_after_next_and_never_publishes(chrome, posh, photos, tmp_path):
    shots, r = tmp_path / "shots", render(photos)
    out, site = post(chrome, posh, r, shots, stage="review", mode="publish")
    # Before WO15 this ended "could not go back (TimeoutError); left the form without Poshmark's Discard dialog
    # (TimeoutError)", as on the Mac: the old back-out clicked the form's Cancel, under the panel's backdrop.
    assert out.status == "dryrun" and out.note == f"review page recorded in {SKU}-review.json", out
    record = json.loads((shots / f"{SKU}-review.json").read_text(encoding="utf-8"))
    assert "List This Item" in {b["text"] for b in record["buttons"]}
    assert {r.title, "Promote My Closet", "Pinterest", "Facebook"} <= set(record["headings"])   # as on the Mac
    assert list(shots.glob(f"{SKU}-poshmark-*-review.png"))
    assert "Share Listing" in (shots / f"{SKU}-review.html").read_text(encoding="utf-8")     # the panel's markup
    assert site.list_clicks == 0 and not site.visited("/listing/")                     # never List This Item
    assert site.visited("/feed?discarded=1") and out.draft_left is None               # ‹ Back, Cancel, Discard


def test_a_read_back_mismatch_fails_and_still_discards(chrome, posh, photos, tmp_path):
    out, site = post(chrome, posh, render(photos), tmp_path / "shots", priceBug=True)
    assert out.status == "failed" and out.diff == {"price": (85, "")}, out
    assert site.visited("/feed?discarded=1") and not site.visited("/listing/")


def test_each_dry_run_counts_the_drafts_before_and_after(chrome, posh, photos, tmp_path):
    """WO12: the create page is reopened after the dry-run; its Drafts count must not have grown."""
    out, site = post(chrome, posh, render(photos), tmp_path / "shots")
    assert out.status == "dryrun" and out.draft_left is None and out.note is None, out
    assert len(site.visited("/create-listing")) == 2                     # the form, then the recount

    out, site = post(chrome, posh, render(photos), tmp_path / "shots", leaveDraft=True)
    assert out.status == "dryrun" and out.draft_left == "a draft was left behind (Drafts 0 → 1)"
    assert out.note == "a draft was left behind (Drafts 0 → 1)"


def test_a_failed_dry_run_is_counted_too(chrome, posh, photos, tmp_path):
    out, _ = post(chrome, posh, render(photos), tmp_path / "shots", priceBug=True, leaveDraft=True)
    assert out.status == "failed" and out.draft_left == "a draft was left behind (Drafts 0 → 1)"


def test_no_leave_dialog_is_reported_not_hidden(chrome, posh, photos, tmp_path, monkeypatch):
    monkeypatch.setattr(poshmark, "LEAVE_TIMEOUT_MS", 400)
    out, site = post(chrome, posh, render(photos), tmp_path / "shots", noLeaveDialog=True)
    assert out.status == "dryrun" and "without Poshmark's Discard dialog" in out.note
    assert "check the closet's drafts" in out.note


# ---------------------------------------------------------------- WO13: a size menu that closes itself

KIDS_TODDLER = dict(department="Kids", category="Shoes", subcategory="Sneakers", size="EU 24 / US Toddler 7.5",
                    kids_gender="unisex", brand="Nike", title="Nike Pink Sneakers", colors=["Pink"], condition="good",
                    original_price=None)


@pytest.mark.parametrize("variant,menu", [({}, "Done"), (dict(sizeAutoClose=True), "closed by itself")])
@pytest.mark.parametrize("kw,button", [
    (KIDS_TODDLER, "7.5 (Toddler Girl)"),                                     # Mac dry-run #2: no Done, it closed
    (dict(), "7.5"),                                                          # Women's shoes
    (dict(category="Tops", subcategory="Blouses", size="M"), "M"),
])
def test_the_size_menu_may_wait_for_done_or_close_by_itself(chrome, posh, photos, variant, menu, kw, button):
    r = render(photos, **kw)
    seen, events = fill_and_read(chrome, posh, r, steps=["_category", "_size"], **variant)
    assert diff_on(seen, r, posh, "size") == {} and seen["size"] == button and seen["size_menu"] == menu
    assert f"size:{button}" in events


@pytest.mark.parametrize("variant,shown", [
    (dict(sizeStuck=True), "Select Size"),                                   # neither Done nor closing
    (dict(sizeAutoClose=True, sizeFieldLabel="7"), "7"),                     # closed, with another size on the form
])
def test_a_size_menu_that_does_neither_fails_with_the_evidence(chrome, posh, photos, monkeypatch, variant, shown):
    err, _ = fill_expecting(chrome, posh, render(photos), PosterError, steps=["_category", "_size"],
                            monkeypatch=monkeypatch, **variant)
    assert not isinstance(err, Skipped)
    assert str(err) == ("after picking '7.5' the size menu neither showed Done nor closed with that size on the form "
                        f"(the size field shows '{shown}')")


def test_the_mac_kids_dry_run_gets_past_a_self_closing_size_menu(chrome, posh, photos, tmp_path):
    out, site = post(chrome, posh, render(photos, **KIDS_TODDLER), tmp_path / "shots", sizeAutoClose=True)
    assert out.status == "dryrun" and out.draft_left is None, out
    record = json.loads(Path(out.screenshot).with_suffix(".json").read_text(encoding="utf-8"))
    assert record["diff"] == {} and record["seen"]["size"] == "7.5 (Toddler Girl)"
    assert record["seen"]["size_menu"] == "closed by itself"


# ---------------------------------------------------------------- WO15: the supervised first publish

def publish(chrome, p, r, shots, answer=True, closet=(), appear=(), **variant):
    """Publish `r` for real on the fixture (dry_run off), the owner answering the LIST prompt with `answer`.
    Returns (Outcome, site, what the owner was shown)."""
    shown = []

    async def confirm(render_, panel_text):
        shown.append(panel_text)
        return answer
    p.confirm = confirm

    async def scenario(ctx, site):
        return await p.post(ctx, r, "publish", False, shots), site
    out, site = drive(chrome, scenario, closet=closet, appear=appear, **variant)
    return out, site, shown


def evidence(shots, kind):
    return json.loads(next(shots.glob(f"{SKU}-poshmark-*-{kind}.json")).read_text(encoding="utf-8"))


def test_supervised_publish_presses_list_once_and_finds_the_listing_in_the_closet(chrome, posh, photos, tmp_path):
    """As on the Mac (2026-10-03): List This Item -> /closet/<user>?created_listing_id=<id>, a closet that doesn't show
    the new listing yet, then shows it on a reload."""
    shots = tmp_path / "shots"
    r = render(photos)
    out, site, shown = publish(chrome, posh, r, shots)
    made = site.listings[-1]
    assert out.status == "posted" and out.clicked and site.list_clicks == 1, out
    assert out.url == f"{BASE}/listing/{poshmark.posh_slug(r.title)}-{made['id']}"
    assert len(shown) == 1 and r.title in shown[0] and "Promote My Closet" in shown[0]
    after = sorted(shots.glob(f"{SKU}-poshmark-*-after-list.*"))
    assert [f.suffix for f in after] == [".html", ".json", ".png"]
    record = evidence(shots, "after-list")
    assert record["url_before"].endswith("/create-listing") and record["url_after"] == f"{BASE}/closet/closet"
    assert record["created_listing_id"] == made["id"]
    assert any(f"created_listing_id={made['id']}" in u for u in record["navigations"])
    closet = evidence(shots, "closet")
    assert closet["landed_on_closet"] and closet["found"] == out.url
    assert [bool(p["ours"]) for p in closet["polls"]] == [False, True]     # the landed closet first, then a reload
    live = evidence(shots, "live")
    assert live["url"] == out.url and live["sku_on_page"] is False             # the public page doesn't carry it
    assert not site.visited("connect")                                        # Pinterest / Facebook never touched


def test_a_closet_slower_than_the_poll_fails_as_possibly_live_and_names_the_id(chrome, posh, photos, tmp_path):
    other = listing("Madewell Leopard Shirt Jacket size 4", n=2)        # a closet with something in it already
    out, site, _ = publish(chrome, posh, render(photos), tmp_path / "shots", closet=[other], lag=1000)
    assert out.status == "failed" and out.clicked and out.url is None and site.list_clicks == 1, out
    assert f"Poshmark named it {site.listings[-1]['id']}" in out.error and "thrift mark-posted" in out.error
    assert len(evidence(tmp_path / "shots", "closet")["polls"]) > 2            # reloaded, never clicked


def test_the_created_id_picks_ours_over_a_twin_listed_meanwhile(chrome, posh, photos, tmp_path):
    r = render(photos)
    twin = listing(r.title, made=time.time(), n=77)          # the same title, listed by hand during the post
    out, site, _ = publish(chrome, posh, r, tmp_path / "shots", appear=[twin])
    assert out.status == "posted" and out.url.endswith(site.listings[-1]["id"]), out
    closet = evidence(tmp_path / "shots", "closet")
    assert closet["other_new_with_this_title"] == [twin["id"]]
    assert "another new listing with this title showed up" in out.note         # told: maybe a duplicate


def test_without_a_redirect_the_closet_names_the_new_listing_not_an_older_twin(chrome, posh, photos, tmp_path):
    r = render(photos)
    old = listing(r.title, n=1)                              # the same title, listed an hour ago
    out, site, _ = publish(chrome, posh, r, tmp_path / "shots", closet=[old], listStays=True)
    assert out.status == "posted" and out.url.endswith(site.listings[-1]["id"]), out
    closet = evidence(tmp_path / "shots", "closet")
    assert not closet["landed_on_closet"] and closet["created_listing_id"] is None
    assert old["id"] not in closet["polls"][-1]["new"]                          # it was there before the post
    assert "Listed! Your listing is live." in evidence(tmp_path / "shots", "after-list")["dialogs"]   # never clicked


def test_without_a_redirect_two_new_listings_with_this_title_are_never_guessed(chrome, posh, photos, tmp_path):
    r = render(photos)
    twin = listing(r.title, made=time.time(), n=77)
    out, site, _ = publish(chrome, posh, r, tmp_path / "shots", appear=[twin], listStays=True)
    assert out.status == "failed" and out.url is None and out.clicked and site.list_clicks == 1, out
    assert "2 new listings with this title in the closet, none named by Poshmark: never a guess" in out.error


def test_a_redirect_straight_to_the_listing_is_taken_as_it_is(chrome, posh, photos, tmp_path):
    out, site, _ = publish(chrome, posh, render(photos), tmp_path / "shots", toListing=True)
    assert out.status == "posted" and out.url == f"{BASE}/listing/{site.listings[-1]['slug']}", out
    assert not list((tmp_path / "shots").glob("*-closet.json"))                # no closet needed


def test_an_unrecognised_page_after_list_is_never_clicked_again(chrome, posh, photos, tmp_path):
    out, site, _ = publish(chrome, posh, render(photos), tmp_path / "shots", listError=True)
    assert out.status == "failed" and out.clicked and out.url is None, out
    assert "after List This Item no listing address" in out.error and "may be live" in out.error
    assert site.list_clicks == 1                                               # once, never again
    assert not site.visited("discarded=1")                                    # and the form is never discarded


@pytest.mark.parametrize("variant,answer,status,why", [
    (dict(promoteOn=True), True, "failed", "Promote My Closet is on in the Share Listing panel; it must stay off"),
    (dict(promoteOn=True, promoteText=True), True, "failed", "can't tell whether Promote My Closet is off"),
    ({}, False, "cancelled", "not published: LIST wasn't typed"),
])
def test_nothing_is_listed_unless_the_panel_checks_out_and_the_owner_typed_list(chrome, posh, photos, tmp_path,
                                                                               variant, answer, status, why):
    out, site, _ = publish(chrome, posh, render(photos), tmp_path / "shots", answer=answer, **variant)
    assert out.status == status and not out.clicked and site.list_clicks == 0, out
    assert why in (out.error or "") + (out.note or "")
    assert site.visited("/feed?discarded=1")                                  # ‹ Back, Cancel, Discard Changes


def test_a_toggle_without_a_checkbox_is_read_by_its_off_text(chrome, posh, photos, tmp_path):
    out, site, shown = publish(chrome, posh, render(photos), tmp_path / "shots", promoteText=True)
    assert out.status == "posted" and site.list_clicks == 1 and "Promote My Closet Off" in shown[0], out


def test_an_unsupervised_publish_goes_through_once_the_listing_address_is_recorded(chrome, posh, photos, tmp_path):
    """The adapter no longer refuses (listing_url is pinned); whether the poster loop publishes at all is the runner's
    poster.dry_run + poster.autopublish_confirmed (test_runner)."""
    async def scenario(ctx, site):
        return await posh.post(ctx, render(photos), "publish", False, tmp_path / "shots"), site
    out, site = drive(chrome, scenario)
    assert out.status == "posted" and site.list_clicks == 1 and out.clicked, out


def test_a_native_dialog_after_list_is_recorded_and_the_listing_still_found(chrome, posh, photos, tmp_path):
    out, site, _ = publish(chrome, posh, render(photos), tmp_path / "shots", listAlert=True)
    assert out.status == "posted" and site.list_clicks == 1, out
    assert evidence(tmp_path / "shots", "after-list")["native_dialogs"] == [
        {"type": "alert", "message": "Your listing is being processed"}]


def test_one_click_that_made_two_listings_records_poshmarks_and_warns(chrome, posh, photos, tmp_path):
    out, site, _ = publish(chrome, posh, render(photos), tmp_path / "shots", listTwice=True)
    assert out.status == "posted" and out.url.endswith(site.listings[-1]["id"]) and site.list_clicks == 1, out
    assert f"({site.listings[-2]['id']}): check for a duplicate" in out.note


def test_one_click_that_made_two_listings_without_a_redirect_is_never_guessed(chrome, posh, photos, tmp_path):
    out, site, _ = publish(chrome, posh, render(photos), tmp_path / "shots", listStays=True, listTwice=True)
    assert out.status == "failed" and out.url is None and site.list_clicks == 1, out
    assert "never a guess" in out.error and "may be live" in out.error


def test_a_list_click_that_raises_is_watched_recorded_and_never_repeated(chrome, posh, photos, tmp_path):
    """The click can raise after it went through (a navigation racing it): nothing is clicked again, the page and the
    closet are still read, and without an address the item fails as possibly live."""
    r = render(photos)

    async def scenario(ctx, site):
        page = await ctx.new_page()
        await page.goto(BASE + "/create-listing")
        posh._render, posh.shot, posh._closet_before = r, tmp_path / f"{SKU}-poshmark-x.png", set()
        posh._started = time.time()
        with pytest.raises(PosterError) as e:
            await posh._after_list(page, r, page.url, [], [], "TimeoutError: locator.click: Timeout 30000ms exceeded.")
        return e.value, site
    err, site = drive(chrome, scenario)
    assert "the click raised TimeoutError: locator.click" in str(err) and "may be live" in str(err)
    record = json.loads((tmp_path / f"{SKU}-poshmark-x-after-list.json").read_text(encoding="utf-8"))
    assert record["click_error"].startswith("TimeoutError") and site.list_clicks == 0



@pytest.mark.parametrize("condition,label", [("fair", "Good"), ("good", "Good"), ("excellent", "Like New")])
def test_the_form_never_selects_fair(chrome, posh, photos, condition, label):
    """WO17: the shop never lists Fair. A fair reading that slipped past the pipeline still goes up as Good."""
    r = render(photos, condition=condition)
    seen, events = fill_and_read(chrome, posh, r, steps=["_condition"])
    assert seen["condition"] == label and f"condition:{label}" in events and "condition:Fair" not in events, events


# ---------------------------------------------------------------- WO33: the take-down on the edit page
# WO33 Part D on Poshmark: the take-down is the listing's Availability set to "Not For Sale" on its edit page, then
# Update — reversible (`thrift relist`: back to "For Sale"), never a delete — and the public listing confirms it (D3).
# A stand-in edit page in real headless Chrome (the fixture site of test_poshmark_form.py, https://fixture.invalid: no
# network; the public check answers from the stand-in): the probe finds the control and changes nothing; the steps
# refuse while UNVERIFIED; once recorded, the take-down and the relist each choose their value and press Update once; a
# public page that still sells it is a failure; a listing that's gone is None.
LISTING_ID = "6701a2b3c4d5e6f708192a3b"
URL = f"{BASE}/listing/J-Crew-Wide-Leg-Pants-{LISTING_ID}"

EDIT = """<html><body><h1>Edit Listing</h1>
<section><div class="listing-editor__section__title"> Availability * </div>
<div data-et-name="listingEditorAvailabilitySection">
  <div data-test="dropdown" class="dropdown" tabindex="0" items="available,not_for_sale"
       onclick="document.getElementById('opts').hidden = false"><div class="dropdown__selector"><span>%(now)s</span></div></div>
  <ul id="opts" data-test="dropdown_menu_list" hidden>
    <li class="dropdown__menu__item"><a class="dropdown__link" data-et-on-name="availability" data-et-name="available"
       onclick="pick('For Sale')">For Sale</a></li>
    <li class="dropdown__menu__item"><a class="dropdown__link" data-et-on-name="availability" data-et-name="not_for_sale"
       onclick="pick('Not For Sale')">Not For Sale</a></li>
  </ul>
</div></section>
<a data-et-name="delete" onclick="fetch('/api/delete')">Delete Listing</a>
<button data-et-name="update" onclick="update()">Update</button>
<div data-test="modal-container" id="share" hidden><div>‹ Back</div><h2>Share Listing</h2>
  <span>Promote My Closet</span><span>OFF</span>
  <button data-et-name="list" data-et-prop-flow_type="edit" data-et-prop-listing_id="%(lid)s"
          onclick="save()">List This Item</button>
</div>
<script>
  let chosen = %(now_js)s;
  function pick(v) {
    chosen = v;
    document.querySelector('[data-test=dropdown] span').textContent = v;
    document.getElementById('opts').hidden = true;
  }
  // WO40, as live (the UGG, 2026-10-16): Update only slides up the Share Listing panel; its List This Item saves.
  function save() {
    fetch('/api/update?availability=' + encodeURIComponent(chosen)).then(() => { location.href = '/listing/x'; });
  }
  function update() {
    fetch('/api/update-pressed');
    if (%(panel)s) document.getElementById('share').hidden = false; else save();
  }
</script></body></html>"""


class EditSite:
    """The listing's edit page (its Availability as it is now), Update recorded; any other id is a 404."""

    def __init__(self, now: str = "For Sale", panel: bool = True):
        self.now, self.updates, self.urls, self.panel = now, [], [], panel

    async def handle(self, route):
        from urllib.parse import parse_qs, urlparse
        url = route.request.url
        self.urls.append(url)
        path = urlparse(url).path
        if not url.startswith(BASE + "/"):
            await route.abort()
        elif path == f"/edit-listing/{LISTING_ID}":
            body = EDIT % {"now": self.now, "now_js": repr(self.now), "lid": LISTING_ID,
                           "panel": "true" if self.panel else "false"}
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)
        elif path == "/api/update-pressed":
            await route.fulfill(status=204, body="")
        elif path == "/api/update":
            self.updates.append(parse_qs(urlparse(url).query)["availability"][0])
            self.now = self.updates[-1]
            await route.fulfill(status=204, body="")
        elif path.startswith("/edit-listing/"):
            await route.fulfill(status=404, content_type="text/html", body="<html><body>Not Found</body></html>")
        else:
            await route.fulfill(status=200, content_type="text/html", body="<html><body>listing</body></html>")


def run(chrome, site, scenario):
    loop, browser = chrome

    async def go():
        ctx = await browser.new_context()
        await ctx.route("**/*", site.handle)
        try:
            return await scenario(ctx)
        finally:
            await ctx.close()
    return loop.run_until_complete(go())


@pytest.fixture
def recorded(monkeypatch):
    """The take-down's steps as if a Mac probe had recorded them; the public check quick."""
    monkeypatch.setattr(poshmark, "UNVERIFIED", poshmark.UNVERIFIED - poshmark.AVAILABILITY_NEEDS)
    monkeypatch.setattr(poshmark, "PUBLIC_CHECK_S", 0.3)
    monkeypatch.setattr(poshmark, "PUBLIC_EVERY_S", 0.1)


def public(site, stuck=None):
    """The public listing as the stand-in holds it (for sale unless "Not For Sale"); `stuck` pins an answer."""
    return lambda url: stuck if stuck is not None else site.now != "Not For Sale"


def test_the_probe_finds_the_control_and_changes_nothing(chrome, posh, tmp_path):
    site = EditSite()
    found = run(chrome, site, lambda ctx: posh.set_availability(ctx, URL, False, probe=True, shots=tmp_path))
    assert found is True and site.updates == [] and site.now == "For Sale"
    assert posh.shot.with_suffix(".html").exists() and posh.shot.with_suffix(".json").exists()


def test_the_steps_refuse_while_unverified(chrome, posh, tmp_path, monkeypatch):
    monkeypatch.setattr(poshmark, "UNVERIFIED", poshmark.UNVERIFIED | poshmark.AVAILABILITY_NEEDS)
    site = EditSite()
    with pytest.raises(PosterError, match="isn't recorded yet"):
        run(chrome, site, lambda ctx: posh.set_availability(ctx, URL, False, shots=tmp_path))
    assert site.urls == [] and site.updates == []                  # refused before any page opened


def test_the_take_down_is_not_for_sale_and_the_relist_for_sale(chrome, posh, tmp_path, recorded, monkeypatch):
    site = EditSite()
    monkeypatch.setattr(poshmark, "PUBLIC_STATE", public(site))
    assert run(chrome, site, lambda ctx: posh.set_availability(ctx, URL, False, shots=tmp_path)) is True
    assert site.updates == ["Not For Sale"]
    assert run(chrome, site, lambda ctx: posh.set_availability(ctx, URL, True, shots=tmp_path)) is True
    assert site.updates == ["Not For Sale", "For Sale"]               # thrift relist: reversed, one Update each
    assert not [u for u in site.urls if "/api/delete" in u]              # Delete Listing never touched
    assert '"public": true' in posh.shot.with_suffix(".json").read_text(encoding="utf-8")


def test_wo40_update_only_opens_the_panel_its_list_this_item_saves(chrome, posh, tmp_path, recorded, monkeypatch):
    # The live miss (the UGG, 2026-10-16): Update slid up the Share Listing panel and nothing was saved — three times.
    site = EditSite(panel=True)
    monkeypatch.setattr(poshmark, "PUBLIC_STATE", public(site))
    assert run(chrome, site, lambda ctx: posh.set_availability(ctx, URL, False, shots=tmp_path)) is True
    assert site.updates == ["Not For Sale"] and site.now == "Not For Sale"   # List This Item pressed once
    assert '"saved": "List This Item pressed"' in posh.shot.with_suffix(".json").read_text(encoding="utf-8")


def test_wo40_a_panel_for_another_listing_is_never_pressed(chrome, posh, tmp_path, recorded, monkeypatch):
    monkeypatch.setattr(poshmark, "UPDATE_TIMEOUT_MS", 800)
    site = EditSite(panel=True)
    orig = site.handle

    async def other(route):
        if route.request.url.endswith(f"/edit-listing/{LISTING_ID}"):
            body = EDIT % {"now": site.now, "now_js": repr(site.now), "lid": "f" * 24, "panel": "true"}
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)
        else:
            await orig(route)
    site.handle = other
    monkeypatch.setattr(poshmark, "PUBLIC_STATE", public(site))
    assert run(chrome, site, lambda ctx: posh.set_availability(ctx, URL, False, shots=tmp_path)) is False
    assert site.updates == []


def test_wo40_an_update_that_saves_by_itself_still_works(chrome, posh, tmp_path, recorded, monkeypatch):
    site = EditSite(panel=False)
    monkeypatch.setattr(poshmark, "PUBLIC_STATE", public(site))
    assert run(chrome, site, lambda ctx: posh.set_availability(ctx, URL, False, shots=tmp_path)) is True
    assert site.updates == ["Not For Sale"]


def test_a_public_page_that_still_sells_it_is_a_failure(chrome, posh, tmp_path, recorded, monkeypatch):
    site = EditSite()
    monkeypatch.setattr(poshmark, "PUBLIC_STATE", public(site, stuck=True))
    assert run(chrome, site, lambda ctx: posh.set_availability(ctx, URL, False, shots=tmp_path)) is False
    assert site.updates == ["Not For Sale"]                           # pressed once; tried again by the 3 attempts


def test_a_listing_that_is_gone_is_none(chrome, posh, tmp_path, recorded):
    site = EditSite()
    gone = f"{BASE}/listing/Old-Thing-{'f' * 24}"
    assert run(chrome, site, lambda ctx: posh.set_availability(ctx, gone, False, shots=tmp_path)) is None
    assert site.updates == []


def test_a_page_without_the_control_is_false_with_evidence(chrome, posh, tmp_path, recorded, monkeypatch):
    monkeypatch.setattr(poshmark, "MENU_TIMEOUT_MS", 300)
    site = EditSite()
    site.handle_orig = site.handle

    async def bare(route):
        if "/edit-listing/" in route.request.url:
            await route.fulfill(status=200, content_type="text/html",
                                body="<html><body><label>Availability</label><p>something else</p></body></html>")
        else:
            await site.handle_orig(route)
    site.handle = bare
    assert run(chrome, site, lambda ctx: posh.set_availability(ctx, URL, False, shots=tmp_path)) is False
    assert '"label_on_page": true' in posh.shot.with_suffix(".json").read_text(encoding="utf-8")


# ---------------------------------------------------------------- WO37: tonight's three Poshmark failures

def test_a_size_pick_that_closed_the_menu_with_no_size_is_picked_again(chrome, posh, photos):
    """Live 2026-10-12 (the Marc Jacobs bag's "One Size"): the pick closed the menu and the form still said "Select
    Size" — the menu is opened again and the size picked again."""
    r = render(photos)
    seen, events = fill_and_read(chrome, posh, r, steps=["_category", "_size"], sizeLostOnce=True)
    assert "size-lost" in events and diff_on(seen, r, posh, "size") == {} and seen["size"] == "7.5"
    assert seen["size_menu"] == "Done (picked 2×)"


def test_a_size_poshmark_preselected_is_kept_not_toggled_off(chrome, posh, photos):
    """Recorded live 2026-10-18 (the Marc Jacobs bag, traced): Women › Bags opens its size menu with "One Size"
    selected and the field reading "OS"; a click on the selected tile toggles it OFF — three live failures."""
    r = render(photos, title="Marc Jacobs Leather Hobo Bag", category="Bags", subcategory=None,
               size="One Size", size_tab="Standard", size_value="One Size")
    seen, events = fill_and_read(chrome, posh, r, steps=["_category", "_size"], oneSizeMenu=True)
    assert seen["size"] == "OS" and seen["size_menu"] == "preselected" and diff_on(seen, r, posh, "size") == {}
    assert "size-toggled-off" not in events
    assert any("preselected by Poshmark" in n for n in posh.notes)


def test_the_size_as_the_form_shows_it():
    SizeShown = poshmark.SizeShown
    assert SizeShown("One Size").matches("OS") and SizeShown("One Size").matches("One Size")
    assert not SizeShown("One Size").matches("Select Size") and not SizeShown("8").matches("8.5")
    assert SizeShown("8").matches("8") and SizeShown("One Size") == SizeShown("One Size")
    assert SizeShown("7.5") != SizeShown("7") and repr(SizeShown("7.5")) == "contains '7.5'"
    assert repr(SizeShown("One Size")) == "contains 'One Size' (or 'OS')"


def test_the_promote_part_drawn_after_the_panel_is_waited_for(chrome, posh, photos, tmp_path):
    """Live 2026-10-12 (the UGG Tazz): the panel was read before its Promote My Closet part was drawn."""
    out, site, shown = publish(chrome, posh, render(photos), tmp_path / "shots", promoteLate=True)
    assert out.status == "posted" and site.list_clicks == 1, out
    assert "Promote My Closet Off" in shown[0]


def test_a_panel_that_cant_be_read_is_kept_with_its_text(chrome, posh, photos, tmp_path):
    out, site, _ = publish(chrome, posh, render(photos), tmp_path / "shots", promoteOn=True, promoteText=True)
    assert out.status == "failed" and site.list_clicks == 0
    assert "can't tell whether Promote My Closet is off" in out.error and "the panel read: " in out.error
    assert "Promote My Closet On" in out.error
    assert list((tmp_path / "shots").glob(f"{SKU}-poshmark-*-panel.png"))      # the panel before ‹ Back


def test_an_item_without_a_size_is_skipped_before_next(chrome, posh, photos, monkeypatch):
    """Live 2026-10-12 (the Clarks): no size on any photo — Next only showed "Required" under Size."""
    err, price = fill_expecting(chrome, posh, render(photos, size=None), Skipped, steps=["_category", "_size"])
    assert str(err) == "no size on the item" and price == ""


# ---------------------------------------------------------------- WO37: a live listing's cover, on its edit page

EDIT_URL = BASE + "/listing/UGG-New-Chestnut-Shearling-Platform-Slide-Slippers-size-7-6acd8ea6ce5b0ac5ccde36eb"


def cover_fix(chrome, p, photos, monkeypatch, dry, swap=1, **variant):
    """set_cover on the edit-page stand-in: photo 3 (blue) as the cover, the old cover's photo (red) where it was."""
    monkeypatch.setattr(poshmark, "PUBLIC_CHECK_S", 2.0)
    monkeypatch.setattr(poshmark, "PUBLIC_EVERY_S", 0.05)

    async def scenario(ctx, site):
        monkeypatch.setattr(poshmark, "PUBLIC_COVER", lambda url: "new-cover" if site.updates else "old-cover")
        out = await p.set_cover(ctx, EDIT_URL, Path(photos[2]), (swap, Path(photos[0])) if swap else None, dry=dry,
                                shots=Path(photos[0]).parent)
        return out, site
    return drive(chrome, scenario, **variant)


def test_a_cover_dry_run_replaces_on_the_page_and_never_updates(chrome, posh, photos, monkeypatch):
    out, site = cover_fix(chrome, posh, photos, monkeypatch, dry=True)
    assert out["ok"] and not out["saved"] and site.updates == 0, out
    assert site.visited("/edit-listing/6acd8ea6ce5b0ac5ccde36eb")


def test_a_cover_fix_never_saves_on_poshmark_without_the_owners_go_ahead(chrome, posh, photos, monkeypatch):
    """Poshmark saves an edit only through Update → Share Listing → List This Item (recorded 2026-10-13); that press
    needs the owner's go-ahead: the photos are replaced on the page, nothing is saved."""
    out, site = cover_fix(chrome, posh, photos, monkeypatch, dry=False)
    assert not out["ok"] and not out["saved"] and site.updates == 0, out
    assert "List This Item" in out["error"] and "nothing saved" in out["error"]


@pytest.mark.parametrize("variant", [dict(replaceIgnored=True), dict(applyLost=True)])
def test_a_replacement_that_doesnt_take_is_never_saved(chrome, posh, photos, monkeypatch, variant):
    monkeypatch.setattr(poshmark, "MENU_TIMEOUT_MS", 600)
    out, site = cover_fix(chrome, posh, photos, monkeypatch, dry=False, **variant)
    assert not out["ok"] and not out["saved"] and site.updates == 0, out
    assert "nothing saved" in out["error"]


# ---------------------------------------------------------------- WO41: a price drop on the edit page

EDIT_PRICE = """<html><body><h1>Edit Listing</h1>
<div class="listing-editor__price-text-container"><span>Listing Price</span></div>
<input type="text" data-vv-name="listingPrice" aria-label="form__text" readonly value="$%(price)s">
<div data-test="modal-container" class="modal listing-price-suggestion-modal" id="price-modal" style="display:none">
  <input id="listing-price-modal-listing-price-input" aria-label="Listing Price" type="text" value="%(price)s">
  <label><input type="checkbox" data-test="toggle-input" id="smart-sell" %(smart)s></label>
  <input id="listing-price-modal-original-price-input" type="text" value="120">
  <button type="button" onclick="done()">Done</button>
</div>
<a data-et-name="delete" onclick="fetch('/api/delete')">Delete Listing</a>
<button data-et-name="update" onclick="update()">Update</button>
<div data-test="modal-container" id="share" hidden><div>‹ Back</div><h2>Share Listing</h2>
  <button data-et-name="list" data-et-prop-flow_type="edit" data-et-prop-listing_id="%(lid)s"
          onclick="save()">List This Item</button>
</div>
<script>
  const box = document.querySelector('[data-vv-name=listingPrice]');
  box.addEventListener('click', () => { document.getElementById('price-modal').style.display = 'block'; });
  function done() {
    box.value = '$' + document.getElementById('listing-price-modal-listing-price-input').value.trim();
    document.getElementById('price-modal').style.display = 'none';
  }
  function save() {
    fetch('/api/price?value=' + encodeURIComponent(box.value.replace('$', ''))).then(() => { location.href = '/listing/x'; });
  }
  function update() { document.getElementById('share').hidden = false; }
</script></body></html>"""


class PriceSite:
    """The edit page with its price (WO41); every save recorded — the public listing then shows it."""

    def __init__(self, price: int = 25, smart: bool = False):
        self.price, self.smart, self.saves = price, smart, []

    async def handle(self, route):
        from urllib.parse import parse_qs, urlparse
        url = route.request.url
        path = urlparse(url).path
        if not url.startswith(BASE + "/"):
            await route.abort()
        elif path == f"/edit-listing/{LISTING_ID}":
            body = EDIT_PRICE % {"price": self.price, "lid": LISTING_ID, "smart": "checked" if self.smart else ""}
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)
        elif path == "/api/price":
            self.saves.append(parse_qs(urlparse(url).query)["value"][0])
            self.price = int(self.saves[-1])
            await route.fulfill(status=204, body="")
        else:
            await route.fulfill(status=200, content_type="text/html", body="<html><body>listing</body></html>")


@pytest.fixture
def price_site(monkeypatch):
    site = PriceSite()
    monkeypatch.setattr(poshmark, "PUBLIC_PAGE", lambda url: (True, site.price))
    monkeypatch.setattr(poshmark, "PUBLIC_CHECK_S", 0.3)
    monkeypatch.setattr(poshmark, "PUBLIC_EVERY_S", 0.1)
    return site


def test_wo41_a_price_drop_types_the_new_price_and_saves_once(chrome, posh, tmp_path, price_site):
    out = run(chrome, price_site, lambda ctx: posh.set_price(ctx, URL, 25, 22, shots=tmp_path))
    assert out["ok"] and out["saved"] and out["live_price"] == 22, out
    assert price_site.saves == ["22"]                                  # List This Item once — the edit's save
    assert out["save"] == "List This Item pressed"


def test_wo41_a_dry_run_fills_the_price_and_saves_nothing(chrome, posh, tmp_path, price_site):
    out = run(chrome, price_site, lambda ctx: posh.set_price(ctx, URL, 25, 22, dry=True, shots=tmp_path))
    assert out["ok"] and not out["saved"] and out["price_typed"] == 22 and price_site.saves == []
    assert posh.shot.with_suffix(".png").exists()


def test_wo41_another_price_on_the_page_or_smart_sell_on_is_never_saved(chrome, posh, tmp_path, price_site):
    price_site.price = 30                                              # she changed it since it was read
    out = run(chrome, price_site, lambda ctx: posh.set_price(ctx, URL, 25, 22, shots=tmp_path))
    assert out["mismatch"] == 30 and price_site.saves == []
    price_site.price, price_site.smart = 25, True
    out = run(chrome, price_site, lambda ctx: posh.set_price(ctx, URL, 25, 22, shots=tmp_path))
    assert "Smart Sell is on" in out["error"] and price_site.saves == []
    price_site.smart = False
    out = run(chrome, price_site, lambda ctx: posh.set_price(ctx, URL, 25, 22, shots=tmp_path, go=lambda: False))
    assert out["error"] == "stopped before Save" and price_site.saves == []



# ---------------------------------------------------------------- WO50: a title / description changed on the edit page

EDIT_TEXT = """<html><body><h1>Edit Listing</h1>
<input type="text" data-vv-name="title" value="%(title)s">
<textarea data-vv-name="description">%(desc)s</textarea>
<a data-et-name="delete" onclick="fetch('/api/delete')">Delete Listing</a>
<button data-et-name="update" onclick="update()">Update</button>
<div data-test="modal-container" id="share" hidden><div>‹ Back</div><h2>Share Listing</h2>
  <button data-et-name="list" data-et-prop-flow_type="edit" data-et-prop-listing_id="%(lid)s"
          onclick="save()">List This Item</button>
</div>
<script>
  function save() {
    const t = document.querySelector('[data-vv-name=title]').value;
    const d = document.querySelector('[data-vv-name=description]').value;
    fetch('/api/text?title=' + encodeURIComponent(t) + '&desc=' + encodeURIComponent(d))
      .then(() => { location.href = '/listing/x'; });
  }
  function update() { document.getElementById('share').hidden = false; }
</script></body></html>"""


class TextSite:
    """The edit page with its title and description (WO50); every save recorded — the public listing then shows it."""

    def __init__(self, title: str = "BCBGMAXAZRIA New Black Slouchy Shoulder Bag", desc: str = "BCBGMAXAZRIA bag."):
        self.title, self.desc, self.saves = title, desc, []

    async def handle(self, route):
        import html as html_mod
        from urllib.parse import parse_qs, urlparse
        url = route.request.url
        path = urlparse(url).path
        if not url.startswith(BASE + "/"):
            await route.abort()
        elif path == f"/edit-listing/{LISTING_ID}":
            body = EDIT_TEXT % {"title": html_mod.escape(self.title), "desc": html_mod.escape(self.desc),
                                "lid": LISTING_ID}
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)
        elif path == "/api/text":
            q = parse_qs(urlparse(url).query)
            self.saves.append(q["title"][0])
            self.title, self.desc = q["title"][0], q.get("desc", [""])[0]
            await route.fulfill(status=204, body="")
        else:
            await route.fulfill(status=200, content_type="text/html", body="<html><body>listing</body></html>")


@pytest.fixture
def text_site(monkeypatch):
    site = TextSite()
    monkeypatch.setattr(poshmark, "PUBLIC_TEXT", lambda url: {"title": site.title, "description": site.desc})
    monkeypatch.setattr(poshmark, "PUBLIC_CHECK_S", 0.3)
    monkeypatch.setattr(poshmark, "PUBLIC_EVERY_S", 0.1)
    return site


NEW_TITLE = "BCBG Max Azria Black Slouchy Shoulder Bag, New with Tags"


def test_wo50_a_title_edit_types_the_new_title_and_saves_once(chrome, posh, tmp_path, text_site):
    payload = {"title": NEW_TITLE, "expect_title": "BCBGMAXAZRIA New Black Slouchy Shoulder Bag"}
    out = run(chrome, text_site, lambda ctx: posh.set_text(ctx, URL, payload, shots=tmp_path))
    assert out["ok"] and out["saved"] and out["live_title"] == NEW_TITLE, out
    assert text_site.saves == [NEW_TITLE] and text_site.desc == "BCBGMAXAZRIA bag."      # nothing else changed
    assert out["save"] == "List This Item pressed"


def test_wo50_a_dry_run_fills_the_title_and_saves_nothing(chrome, posh, tmp_path, text_site):
    payload = {"title": NEW_TITLE, "expect_title": "BCBGMAXAZRIA New Black Slouchy Shoulder Bag"}
    out = run(chrome, text_site, lambda ctx: posh.set_text(ctx, URL, payload, dry=True, shots=tmp_path))
    assert out["ok"] and not out["saved"] and out["title_typed"] == NEW_TITLE and text_site.saves == []


def test_wo50_a_title_she_changed_by_hand_is_never_overwritten(chrome, posh, tmp_path, text_site):
    text_site.title = "Her own BCBG bag title"
    payload = {"title": NEW_TITLE, "expect_title": "BCBGMAXAZRIA New Black Slouchy Shoulder Bag"}
    out = run(chrome, text_site, lambda ctx: posh.set_text(ctx, URL, payload, shots=tmp_path))
    assert out["mismatch"] == "Her own BCBG bag title" and text_site.saves == []
