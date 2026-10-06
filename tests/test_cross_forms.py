"""DepopPoster and VintedPoster (WO30) against stand-ins of their forms (tests/fixtures/depop_create.html,
vinted_new_item.html): real headless Chrome, real clicks, no network — every request of the test browser is answered
here or aborted. The stand-ins are the poster's current picture of the forms (UNVERIFIED until a Mac dry run records
the real ones); these tests pin the poster's behaviour: a dry run never presses the publish button, a publish presses
it once and records the address, a logged-out or verification page stops the marketplace, an interrupted publish is
looked for in the shop."""
import asyncio
import json
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image

pw_api = pytest.importorskip("playwright.async_api")

from thrift_agent.catalogs.depop import DepopFields  # noqa: E402
from thrift_agent.catalogs.vinted import VintedFields  # noqa: E402
from thrift_agent.post import depop as dmod  # noqa: E402
from thrift_agent.post import vinted as vmod  # noqa: E402
from thrift_agent.post.base import AccountBlocked  # noqa: E402
from thrift_agent.schema import Render  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
TITLE = "Tory Burch Red Ballet Flats size 7.5"
SKU = "i_261006_abc123"


@pytest.fixture(scope="module")
def chrome():
    loop = asyncio.new_event_loop()

    async def start():
        pw = await pw_api.async_playwright().start()
        errors = []
        for kw in ({"channel": "chrome"}, {}):
            try:
                return pw, await pw.chromium.launch(headless=True, **kw)
            except Exception as e:  # noqa: BLE001
                errors.append(str(e).splitlines()[0])
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
    """Depop's and Vinted's hosts, answered from the fixtures; `clicks` counts the publish buttons pressed."""

    def __init__(self, logged_out=False, shop_after_click=()):
        self.logged_out, self.clicks, self.urls = logged_out, 0, []
        self.shop: list[str] = []
        self.shop_after_click = list(shop_after_click)

    async def _html(self, route, body):
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

    async def handle(self, route):
        url = route.request.url
        self.urls.append(url)
        u = urlparse(url)
        host, path = u.netloc, u.path
        if host == "www.depop.com":
            if path == "/products/create/":
                if self.logged_out:
                    return await self._html(route, '<form action="/login/"><input autocomplete="username"></form>')
                return await self._html(route, (FIX / "depop_create.html").read_text(encoding="utf-8"))
            if path == "/api/post-click":
                self.clicks += 1
                self.shop += self.shop_after_click
                return await route.fulfill(status=204, body="")
            if path.startswith("/products/"):
                return await self._html(route, f"<html><body><p>{escape(TITLE)}</p><p>$85.00</p><p>US 7.5</p>"
                                               "</body></html>")
            if path.rstrip("/") == "/shopname":
                links = "".join(f'<a href="{h}">x</a>' for h in self.shop)
                return await self._html(route, f"<html><body>{links}</body></html>")
            return await self._html(route, "<html><body>home</body></html>")
        if host == "www.vinted.com":
            if path == "/items/new":
                return await self._html(route, (FIX / "vinted_new_item.html").read_text(encoding="utf-8"))
            if path == "/api/upload-click":
                self.clicks += 1
                return await route.fulfill(status=204, body="")
            if path == "/api/v2/item_upload/brands":
                kw = parse_qs(u.query).get("keyword", [""])[0].lower()
                brands = [{"id": i, "title": t, "requires_authenticity_check": t == "Tory Burch", "is_luxury": False}
                          for i, t in enumerate(["Tory Burch", "Tory Sport"]) if kw in t.lower()]
                return await route.fulfill(status=200, content_type="application/json",
                                           body=json.dumps({"brands": brands}))
            if path.startswith("/items/"):
                return await self._html(route, f"<html><body><h1>{escape(TITLE)}</h1><p>$85.00</p><p>7.5</p>"
                                               "</body></html>")
            return await self._html(route, "<html><body>home</body></html>")
        if host == "api.www.vinted.com":
            return await route.fulfill(status=200, content_type="application/json",
                                       headers={"Access-Control-Allow-Origin": "https://www.vinted.com",
                                                "Access-Control-Allow-Credentials": "true"},
                                       body=json.dumps({"package_sizes": [{"code": "SMALL"}, {"code": "MEDIUM"},
                                                                          {"code": "LARGE"}]}))
        await route.abort()


def drive(chrome, scenario, site=None, **variant):
    loop, browser = chrome

    async def go():
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        s = site or Site()
        await ctx.route("**/*", s.handle)
        await ctx.add_init_script(f"window.__FIXTURE = {json.dumps(variant)};")
        try:
            return await scenario(ctx, s), s
        finally:
            await ctx.close()

    return loop.run_until_complete(go())


@pytest.fixture
def photos(tmp_path):
    out = []
    for i, color in enumerate(["red", "white", "pink"]):
        p = tmp_path / f"{i:02d}.jpg"
        Image.new("RGB", (60, 80), color).save(p)
        out.append(str(p))
    return out


@pytest.fixture(autouse=True)
def quick(monkeypatch, tmp_path):
    async def instant(*a, **k):
        pass

    async def fill(loc, value):
        await loc.click()
        await loc.fill(value)

    for mod in (dmod, vmod):
        monkeypatch.setattr(mod, "settle", instant)
        monkeypatch.setattr(mod, "MENU_MS", 2000)
        monkeypatch.setattr(mod, "THUMB_MS", 4000)
    monkeypatch.setattr(dmod, "SIZE_SET_MS", 6000)
    monkeypatch.setattr(vmod, "human_type", fill)
    monkeypatch.setattr(vmod, "PACKAGE_CACHE", tmp_path / "vinted_package_sizes.json")
    monkeypatch.setattr("thrift_agent.post.cross.AFTER_PUBLISH_MS", 3000)


def render(photos, mp):
    return Render(marketplace=mp, title=TITLE, description="Red flats.", tags=[], brand="Tory Burch",
                  department="Women", category="x", subcategory=None, size="7.5", colors=["Red"], condition="good",
                  price=85, photos=photos, sku=SKU)


def depop_fields(photos, **kw):
    return DepopFields(category="Women > Footwear > Ballet shoes", description=f"{TITLE}\n\nRed flats.\n\n#toryburch",
                       hashtags=["toryburch"], brand="Tory Burch", size="US 7.5", size_set="46",
                       condition="Used - Good", colors=["Red"], source=["Preloved"], age="Modern",
                       package_size="Medium", price=85, photos=photos, **kw)


def vinted_fields(photos, **kw):
    base = dict(category_id=2955, category_path="Women > Shoes > Ballerinas", title=TITLE, description="Red flats.",
                brand="Tory Burch", size_id=1198, size="7.5", condition_id=3, condition="Good", color_ids=[7],
                colors=["Red"], material_ids=[43], materials=["Leather"], package_sizes=["MEDIUM"], price=85,
                photos=photos)
    return VintedFields(**{**base, **kw})


def _depop(fields, **kw):
    p = dmod.DepopPoster(**kw)
    p.fields = fields
    return p


def _vinted(fields, **kw):
    p = vmod.VintedPoster(**kw)
    p.fields = fields
    return p


# ---------------------------------------------------------------- Depop

def test_depop_dry_run_fills_every_field_and_never_presses_post(chrome, photos, tmp_path):
    poster = _depop(depop_fields(photos))
    out, site = drive(chrome, lambda ctx, s: poster.post(ctx, render(photos, "depop"), "publish", True,
                                                          tmp_path / "shots"))
    assert out.status == "dryrun", (out.error, out.note, out.diff)
    assert site.clicks == 0                                              # the Post button: never in a dry run
    record = json.loads(Path(out.screenshot).with_suffix(".json").read_text(encoding="utf-8"))
    assert record["seen"]["variants-input"] == ["US 7.5"] and record["seen"]["brand-input"] == ["Tory Burch"]
    assert record["seen"]["package"].startswith("Medium") and record["seen"]["boost"] is False
    assert "US 7.5" in record["seen"]["size_menu"]                      # read until the new size set showed


def test_depop_publishes_once_and_records_the_product_address(chrome, photos, tmp_path, monkeypatch):
    poster = _depop(depop_fields(photos), strict=True)
    monkeypatch.setattr(poster, "unverified", frozenset())               # as once a Mac dry run recorded the form
    out, site = drive(chrome, lambda ctx, s: poster.post(ctx, render(photos, "depop"), "publish", False,
                                                          tmp_path / "shots"))
    assert out.status == "posted" and site.clicks == 1
    assert out.url == "https://www.depop.com/products/shopname-tory-burch-red-ballet-flats-size-75/"
    assert Path(out.screenshot).with_name(Path(out.screenshot).stem + "-after-publish.json").exists()


def test_depop_refuses_to_publish_while_its_post_button_is_unverified(chrome, photos, tmp_path):
    poster = _depop(depop_fields(photos), strict=True)
    out, site = drive(chrome, lambda ctx, s: poster.post(ctx, render(photos, "depop"), "publish", False,
                                                          tmp_path / "shots"))
    assert out.status == "failed" and "UNVERIFIED" in out.error and site.clicks == 0 and not out.clicked


def test_depop_logged_out_stops_depop(chrome, photos, tmp_path):
    poster = _depop(depop_fields(photos))
    with pytest.raises(AccountBlocked, match="Depop: not logged in"):
        drive(chrome, lambda ctx, s: poster.post(ctx, render(photos, "depop"), "publish", True, tmp_path / "shots"),
              site=Site(logged_out=True))


def test_depop_an_interrupted_publish_is_found_in_the_shop(chrome, photos, tmp_path, monkeypatch):
    """Post was pressed but the page never became a product page: the shop is looked at, by the title's words."""
    poster = _depop(depop_fields(photos), shop="shopname", strict=True)
    monkeypatch.setattr(poster, "unverified", frozenset())
    site = Site(shop_after_click=["/products/shopname-tory-burch-red-ballet-flats-size-75/"])
    out, site = drive(chrome, lambda ctx, s: poster.post(ctx, render(photos, "depop"), "publish", False,
                                                          tmp_path / "shots"), site=site, land="/")
    assert out.status == "posted" and site.clicks == 1
    assert out.url == "https://www.depop.com/products/shopname-tory-burch-red-ballet-flats-size-75/"


def test_depop_brand_not_offered_is_left_empty_and_said(chrome, photos, tmp_path):
    poster = _depop(depop_fields(photos, ).model_copy(update={"brand": "Naturino"}))
    out, _ = drive(chrome, lambda ctx, s: poster.post(ctx, render(photos, "depop"), "publish", True,
                                                       tmp_path / "shots"))
    assert out.status == "dryrun" and out.guesses == ["brand left empty (Depop has no 'Naturino')"]


# ---------------------------------------------------------------- Vinted

def test_vinted_dry_run_fills_every_field_and_never_uploads(chrome, photos, tmp_path):
    poster = _vinted(vinted_fields(photos))
    out, site = drive(chrome, lambda ctx, s: poster.post(ctx, render(photos, "vinted"), "publish", True,
                                                          tmp_path / "shots"))
    assert out.status == "dryrun", (out.error, out.note, out.diff)
    assert site.clicks == 0
    record = json.loads(Path(out.screenshot).with_suffix(".json").read_text(encoding="utf-8"))
    assert record["seen"]["category"] == ["Women > Shoes > Ballerinas"] and record["seen"]["package"] == "MEDIUM"
    assert "authenticity" in (out.note or "")                          # a flagged brand: listed, and said (ops)


def test_vinted_extra_small_only_where_the_leaf_offers_it(chrome, photos, tmp_path):
    poster = _vinted(vinted_fields(photos, package_sizes=["X_SMALL", "SMALL"]))
    out, _ = drive(chrome, lambda ctx, s: poster.post(ctx, render(photos, "vinted"), "publish", True,
                                                       tmp_path / "shots"))
    record = json.loads(Path(out.screenshot).with_suffix(".json").read_text(encoding="utf-8"))
    assert out.status == "dryrun" and record["seen"]["package"] == "SMALL"
    assert json.loads(vmod.PACKAGE_CACHE.read_text(encoding="utf-8")) == {"2955": ["SMALL", "MEDIUM", "LARGE"]}


def test_vinted_publishes_once_and_records_the_item_address(chrome, photos, tmp_path, monkeypatch):
    poster = _vinted(vinted_fields(photos), strict=True)
    monkeypatch.setattr(poster, "unverified", frozenset())
    out, site = drive(chrome, lambda ctx, s: poster.post(ctx, render(photos, "vinted"), "publish", False,
                                                          tmp_path / "shots"))
    assert out.status == "posted" and site.clicks == 1 and out.url == "https://www.vinted.com/items/9876543210"


def test_vinted_verification_wall_stops_vinted(chrome, photos, tmp_path):
    poster = _vinted(vinted_fields(photos))
    with pytest.raises(AccountBlocked, match="verification"):
        drive(chrome, lambda ctx, s: poster.post(ctx, render(photos, "vinted"), "publish", True, tmp_path / "shots"),
              verify=True)
