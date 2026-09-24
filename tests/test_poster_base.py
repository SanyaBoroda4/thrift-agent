"""The poster contract with the browser stubbed: fill, read back, diff, then dry-run | submit + verify."""
import asyncio
from pathlib import Path

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


class FakeCtx:
    async def new_page(self):
        return FakePage()


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


def run(p, mode="publish", dry_run=False, shots=None):
    return asyncio.run(p.post(FakeCtx(), RENDER, mode, dry_run, shots))


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


def test_compare_normalises():
    assert compare({"title": "  Red  Flats ", "price": "$85", "colors": ["red", "Pink"]},
                   {"title": "red flats", "price": 85, "colors": ["Pink", "Red"]}) == {}
    assert compare({"price": "85.00"}, {"price": 85}) == {"price": (85, "85.00")}
