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
from pathlib import Path

import pytest
from PIL import Image

pw_api = pytest.importorskip("playwright.async_api")

from thrift_agent.post import poshmark  # noqa: E402
from thrift_agent.post.base import NeedsOwner, PosterError, compare  # noqa: E402
from thrift_agent.schema import Render  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "poshmark_create_listing.html"
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


class Site:
    """Answers every request of one test context: the fixture for /create-listing, a plain page for any other path of
    BASE, an abort for anything else. `urls` is every address the browser asked for."""

    def __init__(self):
        self.urls: list[str] = []

    async def handle(self, route):
        url = route.request.url
        self.urls.append(url)
        if not url.startswith(BASE + "/"):
            await route.abort()
        elif url.startswith(BASE + "/create-listing"):
            await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                                body=FIXTURE.read_text(encoding="utf-8"))
        else:
            await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                                body="<html><body><p>elsewhere</p></body></html>")

    def visited(self, part: str) -> list[str]:
        return [u for u in self.urls if part in u]


def drive(chrome, scenario, **variant):
    """Run `scenario(ctx, site)` in a fresh context; `variant` becomes window.__FIXTURE (lastDept, smartSell, crop,
    priceBug, noLeaveDialog)."""
    loop, browser = chrome

    async def go():
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        site = Site()
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
                     ("LEAVE_TIMEOUT_MS", 1500), ("THUMB_TIMEOUT_MS", 5000), ("POLL_MS", 25)):
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
                      "size:7.5", "condition:Good", "brand:Tory Burch", "color:Red", "tag:Casual", "price-done",
                      "show-details"]                              # the SKU sits behind "show details"
    assert seen["cover_dialog"] == {"photos": 3, "crop": "Poshmark's default"}
    assert seen["photos"] == 3 and seen["price"] == "$85" and seen["original_price"] == "$228"
    assert seen["category"] == "Women / Shoes" and seen["subcategory"] == "Flats & Loafers" and seen["size"] == "7.5"
    assert seen["smart_sell"] == "off" and seen["sku"] == SKU
    assert "Shipping Discount Optional" in seen["price_dialog"]           # the recorded default: nothing chosen
    assert posh.notes == []


def test_the_form_as_filled_matches_every_condition_label(chrome, posh, photos):
    """NWOT has no label of its own on Poshmark: it goes up as Like New; excellent as Good."""
    r = render(photos, condition="NWOT", colors=["Black", "White"], original_price=None, tags=[])
    seen, events = fill_and_read(chrome, posh, r, steps=["_condition", "_colors", "_price"])
    assert diff_on(seen, r, posh, "condition", "colors", "price", "original_price", "smart_sell") == {}, seen
    assert "condition:Like New" in events and ["color:Black", "color:White"] == [e for e in events if "color" in e]
    assert seen["original_price"] == "" and seen["colors"] == "Black, White"


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


def test_curated_style_tags_only(chrome, posh, photos):
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


# ---------------------------------------------------------------- where it stops

@pytest.mark.parametrize("brand,offer", [("Tory", " (it offers: Tory Burch, Tory Sport)"), ("Zzyzx", "")])
def test_a_brand_poshmark_does_not_offer_is_the_owners_question(chrome, posh, photos, brand, offer, monkeypatch):
    monkeypatch.setattr(poshmark, "SUGGEST_TIMEOUT_MS", 400)             # only the brand waits on suggestions
    err, price = fill_expecting(chrome, posh, render(photos, brand=brand), NeedsOwner)
    assert err.question == (f"Poshmark's brand list has no match for '{brand}'{offer}. Which brand should I pick? "
                            "(reply e.g. 'brand Vince')")
    assert price == ""                                                     # stopped at the brand


def test_brand_match_ignores_case_and_curly_quotes(chrome, posh, photos):
    r = render(photos, brand="levi’s", title="Levi's Red Flats size 7.5")
    seen, events = fill_and_read(chrome, posh, r, steps=["_brand"])
    assert events == ["brand:Levi's"] and seen["brand"] == "Levi's" and diff_on(seen, r, posh, "brand") == {}


@pytest.mark.parametrize("change,question", [
    (dict(category="Sweatshirts"), "Poshmark has no category 'Sweatshirts' under Women (it offers: Accessories, "
                                   "Bags, Dresses, Jeans, Shorts, Shoes, Sweaters, Tops, Other). Which "
                                   "category should I pick? (reply e.g. 'category Tops')"),
    (dict(subcategory="Knee High Boots"), "Poshmark has no subcategory 'Knee High Boots' under Women/Shoes (it offers: "
                                          "None, Ankle Boots & Booties, Athletic Shoes"),
    (dict(department="Unisex"), "Poshmark has no 'Unisex' department. Which one should it go under? "
                                "(reply e.g. 'department Women')"),
])
def test_a_category_poshmark_does_not_have_is_the_owners_question(chrome, posh, photos, change, question, monkeypatch):
    err, _ = fill_expecting(chrome, posh, render(photos, **change), NeedsOwner, steps=["_category"],
                            monkeypatch=monkeypatch)
    assert err.question.startswith(question)


def test_a_size_missing_from_a_verified_list_is_a_question_from_an_unrecorded_one_an_error(chrome, posh, photos,
                                                                                             monkeypatch):
    err, _ = fill_expecting(chrome, posh, render(photos, size="15"), NeedsOwner, steps=["_category", "_size"],
                            monkeypatch=monkeypatch)
    assert err.question.startswith("Poshmark's size list for Women/Shoes (Standard) has no '15' (it offers: 5, 5.5, ")
    assert err.question.endswith("Which size should I pick? (reply e.g. 'size 8')")

    dress = render(photos, category="Dresses", subcategory="Midi", size="5X")         # Plus tab: labels not recorded
    err, _ = fill_expecting(chrome, posh, dress, PosterError, steps=["_category", "_size"], monkeypatch=monkeypatch)
    assert not isinstance(err, NeedsOwner) and "size '5X' is not in Women/Dresses (Plus)" in str(err)


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
    assert not isinstance(err, NeedsOwner) and str(err) == why


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
    shots = tmp_path / "shots"
    out, site = post(chrome, posh, render(photos), shots, stage="review", mode="publish")
    assert out.status == "dryrun" and out.note == f"review page recorded in {SKU}-review.json", out
    record = json.loads((shots / f"{SKU}-review.json").read_text(encoding="utf-8"))
    assert {"Edit", "List This Item"} <= {b["text"] for b in record["buttons"]}
    assert "Review your listing" in record["headings"]
    assert list(shots.glob(f"{SKU}-poshmark-*-review.png"))
    assert not site.visited("/listing/") and site.visited("/feed?discarded=1")       # backed out, then Discard


def test_a_read_back_mismatch_fails_and_still_discards(chrome, posh, photos, tmp_path):
    out, site = post(chrome, posh, render(photos), tmp_path / "shots", priceBug=True)
    assert out.status == "failed" and out.diff == {"price": (85, "")}, out
    assert site.visited("/feed?discarded=1") and not site.visited("/listing/")


def test_no_leave_dialog_is_reported_not_hidden(chrome, posh, photos, tmp_path, monkeypatch):
    monkeypatch.setattr(poshmark, "LEAVE_TIMEOUT_MS", 400)
    out, site = post(chrome, posh, render(photos), tmp_path / "shots", noLeaveDialog=True)
    assert out.status == "dryrun" and "without Poshmark's Discard dialog" in out.note
    assert "check the closet's drafts" in out.note
