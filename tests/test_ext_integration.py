"""WO32 end to end: Playwright's own Chromium (not branded Chrome: Chrome 137+ ignores --load-extension) with the
unpacked extension (ext/) loaded by --disable-extensions-except / --load-extension, the real bridge on 127.0.0.1:8765
(the extension's address) and the extension driver, against the stand-in forms (tests/fixtures). A full dry run, a
publish, a logged-out page, a block page, a CAPTCHA page, the 6-minute timeout, a refused token and the alarm's poll
while the socket is down.

No network: the two sites are answered by the test browser's router, and every other hostname resolves nowhere
(--host-resolver-rules) — a missed route fails the test instead of reaching a real site. Skipped where Playwright's
Chromium isn't installed (python -m playwright install chromium) or the port is taken."""
import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

pw_api = pytest.importorskip("playwright.async_api")

from thrift_agent import bridge as bm  # noqa: E402
from thrift_agent.config import ROOT  # noqa: E402
from thrift_agent.post.base import AccountBlocked  # noqa: E402
from thrift_agent.post.ext_driver import ExtensionPoster  # noqa: E402
from thrift_agent.schema import Render  # noqa: E402

TOKEN = "integration-token-" + "q" * 30
FIX = ROOT / "tests" / "fixtures"
HOST_RULES = "MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"
TITLE = "J. Crew Red Skirt size S"
RENDER = Render(marketplace="vinted", title=TITLE, description="A red skirt.", tags=[], brand="J. Crew",
                department="Women", category="Skirts", subcategory=None, size="S", colors=["Red"],
                condition="like_new", price=35, photos=[], sku="i_1")
LISTING = ("<html><head><title>{title} | {site}</title></head><body><main><h1 data-testid='item-page-title'>{title}"
           "</h1><p data-testid='product__description'>{title}</p><div data-testid='item-price'>${price}.00</div>"
           "<div data-testid='product__price'>${price}.00</div><p>{size}</p></main></body></html>")
LOGIN = ("<html><head><title>Vinted</title></head><body><header><a href='/member/signup'>Sign up</a></header>"
         "<h1>Join and sell pre-loved clothes with no fees</h1></body></html>")
BLOCK = ("<html><head><title>403 Forbidden</title></head><body><h1>Sorry, not authorized.</h1><p>403 Forbidden. "
         "You were blocked.</p></body></html>")
CAPTCHA = ("<html><head><title>Depop</title></head><body><div id='px-captcha'></div><p>Press &amp; Hold to confirm "
           "you are a human</p></body></html>")


class Site:
    """The two sites as the test browser sees them: the stand-in forms, the listing page Upload / Post lands on, and
    a counter of the publish clicks (the stand-ins call /api/upload-click and /api/post-click)."""

    def __init__(self, vinted=None, depop=None, depop_status=200, fixture=None):
        self.vinted = vinted if vinted is not None else (FIX / "vinted_new_item.html").read_text(encoding="utf-8")
        self.depop = depop if depop is not None else (FIX / "depop_create.html").read_text(encoding="utf-8")
        self.depop_status = depop_status
        self.fixture = fixture or {"sizeLag": 0}
        self.clicks: list[str] = []
        self.urls: list[str] = []

    def _html(self, html: str) -> str:
        return html.replace("<head>", f"<head><script>window.__FIXTURE = {json.dumps(self.fixture)}</script>", 1)

    async def handle(self, route):
        url = route.request.url
        self.urls.append(url)
        path = re.sub(r"^https://www\.(vinted|depop)\.com", "", url).split("?")[0]
        site = "vinted" if "vinted.com" in url else "depop"
        if path in ("/api/upload-click", "/api/post-click"):
            self.clicks.append(path)
            return await route.fulfill(status=200, body="{}", content_type="application/json")
        if re.match(r"^/items/\d+", path) or (re.match(r"^/products/[a-z0-9-]+/?$", path) and "create" not in path):
            body = LISTING.format(title=TITLE, site=site.title(), price=35, size="S / US 4-6")
            return await route.fulfill(status=200, body=body, content_type="text/html")
        if site == "vinted":
            return await route.fulfill(status=200, body=self._html(self.vinted), content_type="text/html")
        return await route.fulfill(status=self.depop_status, body=self._html(self.depop), content_type="text/html")


def fields(mp: str, photos: list[str]):
    if mp == "vinted":
        return SimpleNamespace(category_id=5523, category_path="Women > Clothing > Skirts", title=TITLE,
                               description="A red skirt.\nNew without tags.", brand="J. Crew", size="S / US 4-6",
                               condition="New without tags", colors=["Red"], materials=["Cotton"], skirt_length=None,
                               package_sizes=["MEDIUM", "LARGE"], price=35, photos=photos, guesses=[])
    return SimpleNamespace(category="Women > Bottoms > Skirts", description=f"{TITLE}\n\nA red skirt.\n\n#jcrew",
                           brand="J. Crew", size="S", condition="Used - Good", colors=["Red"], source=["Preloved"],
                           age=None, style=[], attributes={}, shipping="Depop Shipping", package_size="Small",
                           price=35, photos=photos, guesses=[])


async def session(tmp_path, site: Site, *, token=TOKEN, ws=True, poll_minutes=None, pace=0.02):
    """(bridge, Playwright, browser context, the extension's service worker), the extension told its token."""
    try:
        b = await bm.Bridge(TOKEN, port=bm.PORT, pace=pace, ws_enabled=ws).start()
    except bm.BridgeError as e:
        pytest.skip(f"the bridge's port is taken here: {e}")
    pw = await pw_api.async_playwright().start()
    try:
        ctx = await pw.chromium.launch_persistent_context(
            str(tmp_path / "profile"), channel="chromium", headless=True,
            args=[f"--disable-extensions-except={bm.EXT_DIR}", f"--load-extension={bm.EXT_DIR}",
                  f"--host-resolver-rules={HOST_RULES}"])
    except Exception as e:  # noqa: BLE001
        await pw.stop()
        await b.close()
        pytest.skip(f"Playwright's Chromium isn't available here ({str(e).splitlines()[0]})")
    await ctx.route(re.compile(r"^https://www\.(vinted|depop)\.com/"), site.handle)
    sw = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker", timeout=15000)
    settings = {"token": token} | ({"poll_minutes": poll_minutes} if poll_minutes else {})
    await sw.evaluate("s => chrome.storage.local.set(s)", settings)
    await sw.evaluate("connect()")
    return b, pw, ctx, sw


async def close(b, pw, ctx):
    await ctx.close()
    await pw.stop()
    await b.close()


def photo(tmp_path) -> str:
    from PIL import Image
    path = tmp_path / "cover.jpg"
    Image.new("RGB", (300, 400), (190, 30, 40)).save(path)
    return str(path)


def poster(mp, b, tmp_path, verified=False) -> ExtensionPoster:
    p = ExtensionPoster(mp, bridge=b)
    p.fields = fields(mp, [photo(tmp_path)])
    if verified:
        data = json.loads((bm.EXT_DIR / "selectors.json").read_text(encoding="utf-8"))
        for step in data[mp]["steps"].values():
            step["verified"] = True
        p.selectors_path = tmp_path / "selectors.json"
        p.selectors_path.write_text(json.dumps(data), encoding="utf-8")
    return p


async def connected(b, seconds=15):
    for _ in range(int(seconds * 10)):
        if b.connected():
            return True
        await asyncio.sleep(0.1)
    return False


def test_a_full_vinted_dry_run_in_chromium(tmp_path):
    site = Site()

    async def go():
        b, pw, ctx, sw = await session(tmp_path, site)
        try:
            assert await connected(b)
            out = await poster("vinted", b, tmp_path).post(None, RENDER, "publish", True, tmp_path / "shots")
            return out
        finally:
            await close(b, pw, ctx)
    out = asyncio.run(go())
    assert out.status == "dryrun", (out.error, out.diff, out.note)
    assert site.clicks == []                                       # never Upload in a dry run
    png = Path(out.screenshot)
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and png.stat().st_size > 2000   # the captured tab
    assert "upload-form-save-button" in png.with_suffix(".html").read_text(encoding="utf-8")
    record = json.loads(png.with_suffix(".json").read_text(encoding="utf-8"))
    assert record["diff"] == {} and [s["name"] for s in record["steps"]][:3] == ["photos", "title", "description"]
    assert all(s["ok"] for s in record["steps"])
    assert not [u for u in site.urls if "vinted.com" not in u and "depop.com" not in u]


def test_a_vinted_publish_clicks_once_and_lands_on_the_listing(tmp_path):
    site = Site()

    async def go():
        b, pw, ctx, sw = await session(tmp_path, site)
        try:
            assert await connected(b)
            return await poster("vinted", b, tmp_path, verified=True).post(None, RENDER, "publish", False,
                                                                           tmp_path / "shots")
        finally:
            await close(b, pw, ctx)
    out = asyncio.run(go())
    assert out.status == "posted", (out.error, out.note)
    assert out.url == "https://www.vinted.com/items/9876543210" and out.clicked
    assert site.clicks == ["/api/upload-click"]                     # exactly once
    assert Path(out.screenshot).name.endswith("-after-publish.png")
    assert "live check" not in (out.note or "")


def test_depop_dry_run_then_publish(tmp_path):
    site = Site(fixture={"sizeLag": 300})

    async def go():
        b, pw, ctx, sw = await session(tmp_path, site)
        try:
            assert await connected(b)
            dry = await poster("depop", b, tmp_path).post(None, RENDER, "publish", True, tmp_path / "shots")
            live = await poster("depop", b, tmp_path, verified=True).post(None, RENDER, "publish", False,
                                                                          tmp_path / "shots")
            return dry, live
        finally:
            await close(b, pw, ctx)
    dry, live = asyncio.run(go())
    assert dry.status == "dryrun", (dry.error, dry.diff, dry.note)
    assert live.status == "posted" and live.url == "https://www.depop.com/products/shopname-j-crew-red-skirt-size-s/"
    assert site.clicks == ["/api/post-click"]


@pytest.mark.parametrize("mp,html,status,page", [("vinted", LOGIN, 200, "login"), ("depop", BLOCK, 403, "block"),
                                                 ("depop", CAPTCHA, 200, "captcha")])
def test_a_stop_page_stops_the_site_and_is_left_open_for_the_owner(tmp_path, mp, html, status, page):
    site = Site(vinted=html if mp == "vinted" else None, depop=html if mp == "depop" else None, depop_status=status)

    async def go():
        b, pw, ctx, sw = await session(tmp_path, site)
        try:
            assert await connected(b)
            with pytest.raises(AccountBlocked) as e:
                await poster(mp, b, tmp_path).post(None, RENDER, "publish", True, tmp_path / "shots")
            await asyncio.sleep(2)
            open_tabs = [p.url for p in ctx.pages if mp in p.url]
            return e.value, open_tabs
        finally:
            await close(b, pw, ctx)
    err, tabs = asyncio.run(go())
    assert err.page == page and tabs                               # the tab stays for a human to act on
    assert site.clicks == []


def test_a_job_past_its_time_is_called_off_and_never_clicks(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "JOB_TIMEOUT", 2.0)
    site = Site()

    async def go():
        b, pw, ctx, sw = await session(tmp_path, site, pace=1)      # a person's pace: 2-5 s before the first field
        try:
            assert await connected(b)
            out = await poster("vinted", b, tmp_path, verified=True).post(None, RENDER, "publish", False,
                                                                          tmp_path / "shots")
            for _ in range(200):                                    # the extension lets it go: called off
                if not b._busy():
                    break
                await asyncio.sleep(0.1)
            return out, b._busy()
        finally:
            await close(b, pw, ctx)
    out, busy = asyncio.run(go())
    assert out.status == "failed" and "wasn't done in" in out.error and not out.clicked
    assert site.clicks == [] and busy is False


def test_a_wrong_token_is_refused(tmp_path):
    site = Site()

    async def go():
        b, pw, ctx, sw = await session(tmp_path, site, token="not-the-token-" + "w" * 20)
        try:
            ok = await connected(b, seconds=3)
            state = await sw.evaluate("chrome.storage.local.get('bridge_state')")
            return ok, state
        finally:
            await close(b, pw, ctx)
    ok, state = asyncio.run(go())
    assert ok is False and "refused the token" in state["bridge_state"]["link"]


def test_with_the_socket_down_the_alarm_polls_for_the_job(tmp_path):
    site = Site()

    async def go():
        b, pw, ctx, sw = await session(tmp_path, site, ws=False, poll_minutes=0.05)
        try:
            out = await poster("vinted", b, tmp_path).post(None, RENDER, "publish", True, tmp_path / "shots")
            return out, len(b.sockets)
        finally:
            await close(b, pw, ctx)
    out, sockets = asyncio.run(go())
    assert out.status == "dryrun", (out.error, out.diff)
    assert sockets == 0 and site.clicks == []
