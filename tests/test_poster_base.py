"""The poster contract with the browser stubbed: fill, read back, diff, then dry-run | submit + verify."""
import asyncio
from pathlib import Path

import pytest

from thrift_agent.post.base import Poster, compare
from thrift_agent.schema import Render

RENDER = Render(marketplace="poshmark", title="Tory Burch Red Flats size 7.5", description="Red flats.", brand="Tory Burch",
                department="Women", category="Shoes", subcategory=None, size="7.5", colors=["Red"], condition="excellent",
                price=85, photos=[], sku="i_1")


class FakePage:
    async def goto(self, url):
        pass

    async def wait_for_load_state(self, *a, **k):
        pass

    async def screenshot(self, path, full_page=False):
        Path(path).write_bytes(b"png")

    async def close(self):
        pass


class CloseFailsPage(FakePage):
    """Chrome sometimes reports 'Target closed' on page.close() right after a navigation."""
    async def close(self):
        raise RuntimeError("Target page, context or browser has been closed")


class FakeCtx:
    def __init__(self, page=None, new_page_error=None):
        self.page, self.new_page_error = page, new_page_error

    async def new_page(self):
        if self.new_page_error:
            raise self.new_page_error
        return self.page or FakePage()


class StubPoster(Poster):
    name = "stub"
    create_url = "https://example.invalid/create"

    def __init__(self, seen, live_ok=True):
        self.seen, self.live_ok, self.submitted = seen, live_ok, None

    async def check_account(self, page):
        pass

    async def fill(self, page, r):
        pass

    async def read_back(self, page):
        return self.seen

    def expected(self, r):
        return {"title": r.title, "price": r.price}

    async def submit(self, page, mode):
        self.submitted = mode
        return "https://example.invalid/listing/abc" if mode == "publish" else None

    async def verify_live(self, page, url, r):
        if not self.live_ok:
            raise TimeoutError("page never loaded")


def run(p, mode="publish", dry_run=False, shots=None, ctx=None):
    return asyncio.run(p.post(ctx or FakeCtx(), RENDER, mode, dry_run, shots))


def test_dry_run_never_submits(tmp_path):
    p = StubPoster({"title": "Tory Burch Red Flats size 7.5", "price": "$85"})
    out = run(p, dry_run=True, shots=tmp_path)
    assert out.status == "dryrun" and p.submitted is None and Path(out.screenshot).exists()


def test_form_mismatch_blocks_publish(tmp_path):
    p = StubPoster({"title": "Tory Burch Red Flats size 7.5", "price": "80"})
    out = run(p, shots=tmp_path)
    assert out.status == "failed" and p.submitted is None and out.diff == {"price": (85, "80")}


def test_publish_then_verify(tmp_path):
    p = StubPoster({"title": "Tory Burch Red Flats size 7.5", "price": "85"})
    out = run(p, shots=tmp_path)
    assert out.status == "posted" and out.url.endswith("/listing/abc") and p.submitted == "publish"


def test_live_check_failure_keeps_the_url(tmp_path):
    p = StubPoster({"title": "Tory Burch Red Flats size 7.5", "price": "85"}, live_ok=False)
    out = run(p, shots=tmp_path)
    assert out.status == "failed" and out.url.endswith("/listing/abc") and "live check" in out.error


def test_draft_mode(tmp_path):
    p = StubPoster({"title": "Tory Burch Red Flats size 7.5", "price": "85"})
    out = run(p, mode="draft", shots=tmp_path)
    assert out.status == "drafted" and p.submitted == "draft"


def test_page_close_error_keeps_the_outcome(tmp_path):
    """An exception in `finally` would replace the returned Outcome: a live listing with no record (invariant 4)."""
    p = StubPoster({"title": "Tory Burch Red Flats size 7.5", "price": "85"})
    out = run(p, shots=tmp_path, ctx=FakeCtx(page=CloseFailsPage()))
    assert out.status == "posted" and out.url.endswith("/listing/abc") and p.submitted == "publish"


def test_new_page_error_is_a_failed_outcome(tmp_path):
    p = StubPoster({"title": "Tory Burch Red Flats size 7.5", "price": "85"})
    out = run(p, shots=tmp_path, ctx=FakeCtx(new_page_error=RuntimeError("browser has been closed")))
    assert out.status == "failed" and p.submitted is None and "browser has been closed" in out.error


def test_account_blocked_propagates_despite_close_error(tmp_path):
    from thrift_agent.post.base import AccountBlocked

    class Blocked(StubPoster):
        async def check_account(self, page):
            raise AccountBlocked("not logged in")

    p = Blocked({"title": "Tory Burch Red Flats size 7.5", "price": "85"})
    with pytest.raises(AccountBlocked):
        run(p, shots=tmp_path, ctx=FakeCtx(page=CloseFailsPage()))
    assert p.submitted is None


def test_needs_owner_propagates_with_a_screenshot(tmp_path):
    """A question for the owner is not a failed Outcome: the runner must see it to park the item and ask."""
    from thrift_agent.post.base import NeedsOwner

    class Stuck(StubPoster):
        async def fill(self, page, r):
            raise NeedsOwner("Poshmark's brand list has no match for 'Tory Burch'. Which brand should I pick?")

    p = Stuck({"title": "Tory Burch Red Flats size 7.5", "price": "85"})
    with pytest.raises(NeedsOwner) as info:
        run(p, shots=tmp_path, ctx=FakeCtx(page=CloseFailsPage()))
    assert info.value.question.startswith("Poshmark's brand list has no match for 'Tory Burch'")
    assert str(info.value) == info.value.question
    assert p.submitted is None
    assert list(tmp_path.glob("i_1-stub-*.png"))               # the screenshot is taken before re-raising


def test_other_fill_errors_are_still_a_failed_outcome(tmp_path):
    class Broken(StubPoster):
        async def fill(self, page, r):
            raise RuntimeError("selector broke")

    p = Broken({"title": "Tory Burch Red Flats size 7.5", "price": "85"})
    out = run(p, shots=tmp_path)
    assert out.status == "failed" and "selector broke" in out.error and p.submitted is None


# ---------------------------------------------------------------- PoshmarkPoster: where it asks the owner

class FakeLoc:
    """A Playwright Locator with just what fill() calls; `n` is what count() reports."""

    def __init__(self, n=1, click_error=None):
        self.n, self.click_error, self.clicks, self.value = n, click_error, 0, None

    async def count(self):
        return self.n

    async def click(self):
        if self.click_error:
            raise self.click_error
        self.clicks += 1

    async def fill(self, value):
        self.value = value

    async def set_input_files(self, files):
        pass


class FormPage(FakePage):
    class keyboard:
        @staticmethod
        async def press(key):
            pass

    async def wait_for_timeout(self, ms):
        pass


def _posh(monkeypatch, **overrides):
    """PoshmarkPoster over fake locators: every SEL entry finds one element unless overridden. The real SEL
    values are untouched (they are UNVERIFIED and recorded on the Mac); only the module binding is swapped."""
    from thrift_agent.post import poshmark

    locs = {k: overrides.get(k, FakeLoc()) for k in poshmark.SEL if k != "listing_url"}
    fake_sel = {k: (lambda *a, _l=loc: _l) for k, loc in locs.items()}
    monkeypatch.setattr(poshmark, "SEL", {**fake_sel, "listing_url": poshmark.SEL["listing_url"]})

    async def instant(*a, **k):
        pass

    monkeypatch.setattr(poshmark, "settle", instant)
    monkeypatch.setattr(poshmark, "human_type", instant)
    return poshmark.PoshmarkPoster("closet"), locs


def test_poshmark_fill_picks_a_matching_brand(monkeypatch):
    p, locs = _posh(monkeypatch)
    asyncio.run(p.fill(FormPage(), RENDER))
    assert locs["brand_option"].clicks == 1 and locs["listing_price"].value == "85" and locs["sku"].value == "i_1"


def test_poshmark_asks_the_owner_when_the_brand_list_has_no_match(monkeypatch):
    from thrift_agent.post.base import NeedsOwner

    p, locs = _posh(monkeypatch, brand_option=FakeLoc(n=0))
    with pytest.raises(NeedsOwner, match=r"no match for 'Tory Burch'\. Which brand should I pick\?") as info:
        asyncio.run(p.fill(FormPage(), RENDER))
    assert "reply e.g. 'brand Vince'" in info.value.question
    assert locs["brand_option"].clicks == 0 and locs["listing_price"].value is None    # stopped at the brand


def test_poshmark_asks_the_owner_when_a_category_option_never_appears(monkeypatch):
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    from thrift_agent.post.base import NeedsOwner

    timeout = PlaywrightTimeout("Locator.click: Timeout 30000ms exceeded.")
    p, locs = _posh(monkeypatch, category_option=FakeLoc(click_error=timeout))
    with pytest.raises(NeedsOwner, match=r"no category option 'Women' under Women/Shoes\.") as info:
        asyncio.run(p.fill(FormPage(), RENDER))
    assert "Which category/subcategory should I pick?" in info.value.question
    assert locs["size_open"].clicks == 0                       # nothing after the category was touched

    p, _ = _posh(monkeypatch, category_option=FakeLoc(click_error=RuntimeError("detached")))
    with pytest.raises(RuntimeError):                          # only a timeout is a question; the rest stays an error
        asyncio.run(p.fill(FormPage(), RENDER))


def test_kids_size_options_is_a_lookup_for_m2():
    from thrift_agent.post.poshmark import KIDS_SIZE_OPTIONS

    assert KIDS_SIZE_OPTIONS["US Toddler 7.5"] == "7.5C"
    assert KIDS_SIZE_OPTIONS["US Little Kid 12"] == "12C"
    assert KIDS_SIZE_OPTIONS["US Big Kid 4"] == "4Y"
    assert all(isinstance(k, str) and isinstance(v, str) and v for k, v in KIDS_SIZE_OPTIONS.items())


def test_compare_normalises():
    assert compare({"title": "  Red  Flats ", "price": "$85", "colors": ["red", "Pink"]},
                   {"title": "red flats", "price": 85, "colors": ["Pink", "Red"]}) == {}
    for echoed in ("85.00", "$85", "85", "$85.00", " 85 ", 85, 85.0):      # what a price field may echo back
        assert compare({"price": echoed}, {"price": 85}) == {}, echoed
    assert compare({"price": "80"}, {"price": 85}) == {"price": (85, "80")}
    assert compare({"price": None}, {"price": 85}) == {"price": (85, None)}
    assert compare({"price": "n/a"}, {"price": 85}) == {"price": (85, "n/a")}
    assert compare({"original_price": ""}, {"original_price": None}) == {}
    assert compare({}, {"original_price": None}) == {}
    assert compare({"original_price": "120"}, {"original_price": None}) == {"original_price": (None, "120")}
    assert compare({"photos": 15}, {"photos": 16}) == {"photos": (16, 15)}
