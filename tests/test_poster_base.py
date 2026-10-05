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

    async def content(self):
        return "<html>form</html>"

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


SKIP = "size '15' isn't on Poshmark's Women/Shoes (Standard) menu, nor one near it (the size is required)"


def test_a_skip_is_its_own_outcome_with_a_screenshot(tmp_path):
    """WO27: a required field the form can't take, even as a guess, is neither a question nor a failure: the item is
    skipped (nothing saved) and the runner reports it."""
    from thrift_agent.post.base import Skipped

    class Stuck(StubPoster):
        async def fill(self, page, r):
            self.guesses.append("brand left empty: Poshmark's list has no 'Zzyzx'")
            raise Skipped(SKIP)

    p = Stuck({"title": "Tory Burch Red Flats size 7.5", "price": "85"})
    out = run(p, shots=tmp_path, ctx=FakeCtx(page=CloseFailsPage()))
    assert (out.status, out.error) == ("skipped", SKIP) and p.submitted is None
    assert out.guesses == ["brand left empty: Poshmark's list has no 'Zzyzx'"]
    assert list(tmp_path.glob("i_1-stub-*.png")) and Path(out.screenshot).exists()


def test_other_fill_errors_are_still_a_failed_outcome(tmp_path):
    class Broken(StubPoster):
        async def fill(self, page, r):
            raise RuntimeError("selector broke")

    p = Broken({"title": "Tory Burch Red Flats size 7.5", "price": "85"})
    out = run(p, shots=tmp_path)
    assert out.status == "failed" and "selector broke" in out.error and p.submitted is None


# ---------------------------------------------------------------- dry-run stages, leaving the form, evidence

class Leaving(StubPoster):
    """Records the order of review() / discard() / submit() calls."""

    def __init__(self, seen, review_error=None, discard_note=None):
        super().__init__(seen)
        self.calls, self.review_error, self.discard_note = [], review_error, discard_note

    async def review(self, page, r, shots):
        self.calls.append("review")
        if self.review_error:
            raise self.review_error
        return "review page recorded in i_1-review.json"

    async def discard(self, page):
        self.calls.append("discard")
        return self.discard_note

    async def submit(self, page, mode):
        self.calls.append("submit")
        return await super().submit(page, mode)


GOOD = {"title": "Tory Burch Red Flats size 7.5", "price": "85"}


def test_form_stage_dry_run_leaves_through_discard(tmp_path):
    p = Leaving(GOOD)
    out = run(p, dry_run=True, shots=tmp_path)
    assert out.status == "dryrun" and p.calls == ["discard"] and out.note is None


def test_review_stage_records_then_discards_and_never_submits(tmp_path):
    p = Leaving(GOOD, discard_note="left the form without Poshmark's Discard dialog (TimeoutError)")
    out = asyncio.run(p.post(FakeCtx(), RENDER, "publish", True, tmp_path, stage="review"))
    assert out.status == "dryrun" and p.calls == ["review", "discard"] and p.submitted is None
    assert out.note == ("review page recorded in i_1-review.json; "
                        "left the form without Poshmark's Discard dialog (TimeoutError)")


def test_a_failed_review_is_a_failed_dry_run_and_still_discards(tmp_path):
    p = Leaving(GOOD, review_error=RuntimeError("no Next button"))
    out = asyncio.run(p.post(FakeCtx(), RENDER, "draft", True, tmp_path, stage="review"))
    assert out.status == "failed" and "no Next button" in out.error and p.calls == ["review", "discard"]


def test_mismatch_and_errors_discard_the_form(tmp_path):
    p = Leaving({"title": "Tory Burch Red Flats size 7.5", "price": "80"})
    out = run(p, dry_run=True, shots=tmp_path)
    assert out.status == "failed" and out.diff == {"price": (85, "80")} and p.calls == ["discard"]

    class Broken(Leaving):
        async def fill(self, page, r):
            raise RuntimeError("selector broke")

    p = Broken(GOOD, discard_note="could not leave")
    out = run(p, shots=tmp_path)
    assert out.status == "failed" and p.calls == ["discard"] and out.note == "could not leave"


def test_a_skip_discards_the_form_and_keeps_the_screenshot(tmp_path):
    from thrift_agent.post.base import Skipped

    class Stuck(Leaving):
        async def fill(self, page, r):
            raise Skipped("Poshmark has no 'Unisex' department (the category is required)")

    p = Stuck(GOOD)
    out = run(p, shots=tmp_path)
    assert out.status == "skipped" and p.calls == ["discard"] and Path(out.screenshot).exists()


def test_a_blocked_account_or_a_started_submit_is_never_discarded(tmp_path):
    from thrift_agent.post.base import AccountBlocked

    class Blocked(Leaving):
        async def check_account(self, page):
            raise AccountBlocked("CAPTCHA shown")

    p = Blocked(GOOD)
    with pytest.raises(AccountBlocked):
        run(p, shots=tmp_path)
    assert p.calls == []                                   # stop, don't touch a blocked account

    class SubmitBreaks(Leaving):
        async def submit(self, page, mode):
            self.calls.append("submit")
            raise TimeoutError("no listing URL after List This Item")

    p = SubmitBreaks(GOOD)
    out = run(p, shots=tmp_path)
    assert out.status == "failed" and p.calls == ["submit"]  # it may be live: only the closet can tell

    p = Leaving(GOOD)
    assert run(p, shots=tmp_path).status == "posted" and p.calls == ["submit"]


def test_evidence_keeps_the_dom_and_what_was_read_back(tmp_path):
    import json

    out = run(Leaving({"title": "Tory Burch Red Flats size 7.5", "price": "80"}), dry_run=True, shots=tmp_path)
    shot = Path(out.screenshot)
    assert shot.exists() and shot.with_suffix(".html").read_text(encoding="utf-8") == "<html>form</html>"
    record = json.loads(shot.with_suffix(".json").read_text(encoding="utf-8"))
    assert record["item"] == "i_1" and record["seen"]["price"] == "80" and record["diff"] == {"price": [85, "80"]}


def test_contains_matches_whole_words_and_phrases():
    from thrift_agent.post.base import Contains

    assert compare({"category": "Women / Shoes"}, {"category": Contains("Women", "Shoes")}) == {}
    assert compare({"category": "WOMEN > SHOES"}, {"category": Contains("Women", "Shoes")}) == {}
    assert compare({"category": "Women / Shoes"}, {"category": Contains("Men", "Shoes")}) != {}     # not in "Women"
    assert compare({"size": "8.5"}, {"size": Contains("8")}) != {}
    assert compare({"size": "17.5"}, {"size": Contains("7.5")}) != {}
    assert compare({"size": "US 7.5"}, {"size": Contains("7.5")}) == {}
    assert compare({"size": "7.5 (Toddler Girl)"}, {"size": Contains("7.5 (Toddler Girl)")}) == {}
    assert compare({"sub": "Ankle Boots & Booties"}, {"sub": Contains("Ankle Boots & Booties")}) == {}
    assert compare({"colors": "Red, Pink"}, {"colors": Contains("Pink", "Red")}) == {}
    assert compare({"colors": "Red"}, {"colors": Contains("Pink", "Red")}) != {}
    for blank in (None, "", "   "):
        assert compare({"category": blank}, {"category": Contains("Women")}) != {}
    assert repr(Contains("Women", "Shoes")) == "contains 'Women' + 'Shoes'"


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


def test_an_empty_original_price_reads_back_as_zero():
    """Mac dry-run #3: with no Original Price set, Poshmark's form holds "0". Only for original_price."""
    for empty in ("", "0", "0.00", "$0", "$0.00", " 0 ", None, 0):
        assert compare({"original_price": empty}, {"original_price": None}) == {}, empty
    assert compare({"original_price": "0"}, {"original_price": 120}) == {"original_price": (120, "0")}
    assert compare({"original_price": "$120.00"}, {"original_price": 120}) == {}
    assert compare({"original_price": "n/a"}, {"original_price": None}) == {"original_price": (None, "n/a")}
    assert compare({"price": "0"}, {"price": 85}) == {"price": (85, "0")}          # the listing price: no leniency
    assert compare({"photos": 15}, {"photos": 16}) == {"photos": (16, 15)}


# ---------------------------------------------------------------- WO12: a dry-run leaves no draft behind

class Counting(Leaving):
    """A site that shows its draft count; `counts` is what drafts() reads each time (an Exception is raised)."""
    counts_drafts = True

    def __init__(self, seen, counts, **kw):
        super().__init__(seen, **kw)
        self.counts = list(counts)

    async def drafts(self, page):
        value = self.counts.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


LEFT = "a draft was left behind (Drafts 0 → 1)"


def test_a_dry_run_that_left_a_draft_says_so(tmp_path):
    p = Counting(GOOD, [0, 1])
    out = run(p, dry_run=True, shots=tmp_path)
    assert out.status == "dryrun" and out.draft_left == LEFT and out.note == LEFT and p.counts == []
    p = Counting(GOOD, [3, 3])
    out = run(p, dry_run=True, shots=tmp_path)
    assert out.draft_left is None and out.note is None


def test_an_unreadable_draft_count_is_a_note_not_a_warning(tmp_path):
    out = run(Counting(GOOD, [None]), dry_run=True, shots=tmp_path)            # no reopening without a "before"
    assert out.draft_left is None and out.note == ("could not read the Drafts count before the dry-run, so no "
                                                   "left-behind check")
    out = run(Counting(GOOD, [0, RuntimeError("gone")]), dry_run=True, shots=tmp_path)
    assert out.draft_left is None and out.note == "could not read the Drafts count after the dry-run"


def test_the_drafts_are_counted_after_failed_and_skipped_dry_runs_but_never_live(tmp_path):
    from thrift_agent.post.base import Skipped

    out = run(Counting({"title": "Tory Burch Red Flats size 7.5", "price": "80"}, [0, 1]), dry_run=True, shots=tmp_path)
    assert out.status == "failed" and out.draft_left == LEFT

    class Stuck(Counting):
        async def fill(self, page, r):
            raise Skipped(SKIP)

    out = run(Stuck(GOOD, [0, 1]), dry_run=True, shots=tmp_path)
    assert out.status == "skipped" and out.draft_left == LEFT

    p = Counting(GOOD, [0, 1])
    assert run(p, shots=tmp_path).status == "posted" and p.counts == [0, 1]     # a live post: never counted


def test_the_live_check_reads_the_price_exactly():
    from thrift_agent.post.base import _shows_price
    assert _shows_price("Naturino sneakers $50 $120", 50) and _shows_price("$ 50.00", 50)
    assert _shows_price("Size 7 · $1,200", 1200) and _shows_price("$1200", 1200)
    assert not _shows_price("$500", 50) and not _shows_price("$50.99", 50) and not _shows_price("$5", 50)
    assert not _shows_price("$50,5", 50) and not _shows_price("50 dollars", 50)
