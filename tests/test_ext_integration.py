"""WO32 end to end: Playwright's own Chromium (not branded Chrome: Chrome 137+ ignores --load-extension) with the
unpacked extension (ext/) loaded by --disable-extensions-except / --load-extension, the real bridge on 127.0.0.1:8765
(the extension's address) and the extension driver, against the stand-in forms (tests/fixtures). A full dry run, a
publish, a logged-out page, a block page, a CAPTCHA page, the job's timeout, a refused token and the alarm's poll
while the socket is down. WO32b: the fast pace's fill times, a job tab in the background, the extension back within
seconds of a bridge restart, and a Ctrl+C (or a bridge that goes away) before POST closing the tab with nothing clicked.

No network: the two sites are answered by the test browser's router, and every other hostname resolves nowhere
(--host-resolver-rules) — a missed route fails the test instead of reaching a real site. Skipped where Playwright's
Chromium isn't installed (python -m playwright install chromium) or the port is taken."""
import asyncio
import json
import re
import time
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
TITLE = "J. Crew Wide Leg Sweater Pants Cream size S"
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
        self.vinted = vinted if vinted is not None else (FIX / "ext_vinted_new_item.html").read_text(encoding="utf-8")
        self.depop = depop if depop is not None else (FIX / "ext_depop_create.html").read_text(encoding="utf-8")
        self.depop_status = depop_status
        self.fixture = fixture or {"sizeLag": 0, "brandDelay": 200}
        self.clicks: list[str] = []
        self.urls: list[str] = []

    def _html(self, html: str) -> str:
        return html.replace("<head>", f"<head><script>window.__FIXTURE = {json.dumps(self.fixture)}</script>", 1)

    async def media(self, route):
        """Depop's uploaded photos as its tiles load them: a small JPEG (never the real CDN)."""
        import io as _io

        from PIL import Image
        buf = _io.BytesIO()
        Image.new("RGB", (30, 40), (230, 220, 200)).save(buf, "JPEG")
        await route.fulfill(status=200, body=buf.getvalue(), content_type="image/jpeg")

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
        return SimpleNamespace(category_id=1071, category_path="Women > Clothing > Pants & leggings > Wide-leg pants",
                               title=TITLE, description="Wide leg pants.\nNew without tags.", brand="J. Crew",
                               size="S / US 4-6", condition="New without tags", colors=["Cream"], materials=["Wool"],
                               skirt_length=None, package_sizes=["MEDIUM", "LARGE"], price=35, photos=photos,
                               guesses=[])
    return SimpleNamespace(category="Women > Bottoms > Pants", description=f"{TITLE}\n\nWide leg pants.\n\n#jcrew",
                           brand="J. Crew", size="S", condition="Like new", colors=["Cream"], source=["Preloved"],
                           age="Modern", style=[], attributes={"material": ["Wool"]}, shipping="Depop Shipping",
                           package_size="Large", price=35, photos=photos, guesses=[])


async def session(tmp_path, site: Site, *, token=TOKEN, ws=True, poll_minutes=None, pace="fast"):
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
    await ctx.route(re.compile(r"^https://media-photos\.depop\.com/"), site.media)     # the uploaded photos' tiles
    sw = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker", timeout=15000)
    settings = {"token": token} | ({"poll_minutes": poll_minutes} if poll_minutes else {})
    await sw.evaluate("s => chrome.storage.local.set(s)", settings)
    await sw.evaluate("""async () => {           // its files' hash noted (onInstalled) — else it counts as reloading
      for (let i = 0; i < 100 && !(await chrome.storage.local.get("loaded_hash")).loaded_hash; i++) {
        await new Promise((r) => setTimeout(r, 50));
      }
    }""")
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
            p = poster("vinted", b, tmp_path)
            out = await p.post(None, RENDER, "publish", True, tmp_path / "shots")
            return out, p
        finally:
            await close(b, pw, ctx)
    out, p = asyncio.run(go())
    assert out.status == "dryrun", (out.error, out.diff, out.note)
    assert p.fill_seconds is not None and p.fill_seconds < 15, p.fill_seconds    # WO32b: fast (live target ≤ 45 s)
    assert p.lines[:2] == ["tab opened", "page loaded"] and p.lines[-1].startswith("filled in ")
    assert {"page script running", "photos 1/1", "category ✓"} <= set(p.lines)
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
    site = Site(fixture={"sizeLag": 300, "brandDelay": 400})

    async def go():
        b, pw, ctx, sw = await session(tmp_path, site)
        try:
            assert await connected(b)
            dp = poster("depop", b, tmp_path)
            dry = await dp.post(None, RENDER, "publish", True, tmp_path / "shots")
            assert dp.fill_seconds is not None and dp.fill_seconds < 15, dp.fill_seconds
            live = await poster("depop", b, tmp_path, verified=True).post(None, RENDER, "publish", False,
                                                                          tmp_path / "shots")
            return dry, live
        finally:
            await close(b, pw, ctx)
    dry, live = asyncio.run(go())
    assert dry.status == "dryrun", (dry.error, dry.diff, dry.note)
    assert live.status == "posted" and live.url == \
        "https://www.depop.com/products/shopname-j-crew-wide-leg-sweater-pants-cream-size-s/"
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


# ---------------------------------------------------------------- WO32b

def test_the_fill_completes_with_its_tab_in_the_background(tmp_path):
    """The job's tab not the active one of its window (the owner on another tab): the form still fills, quickly.
    (Playwright keeps every page "visible", so Chrome's throttling of a hidden tab is the jsdom test's to prove:
    tests/js/content.test.mjs, "a hidden tab's throttled timers".)"""
    site = Site()

    async def go():
        b, pw, ctx, sw = await session(tmp_path, site)
        try:
            assert await connected(b)
            other = await sw.evaluate("""async () => {
              const w = await chrome.windows.getLastFocused({ windowTypes: ["normal"] });
              return (await chrome.tabs.create({ url: "about:blank", active: true, windowId: w.id })).id;
            }""")

            async def keep_it_behind():          # the owner's tab back on top whenever the job's comes forward
                while not [pg for pg in ctx.pages if "vinted.com/items/new" in pg.url]:
                    await asyncio.sleep(0.05)    # (from its first load on: switching tabs while Playwright attaches to a
                while True:                      # new one leaves it unrouted — a test-browser quirk)
                    await sw.evaluate("id => chrome.tabs.update(id, { active: true }).catch(() => {})", other)
                    await asyncio.sleep(0.2)
            behind = asyncio.create_task(keep_it_behind())
            p = poster("vinted", b, tmp_path)
            try:
                out = await p.post(None, RENDER, "publish", True, tmp_path / "shots")
            finally:
                behind.cancel()
            return out, p
        finally:
            await close(b, pw, ctx)
    out, p = asyncio.run(go())
    assert out.status == "dryrun", (out.error, out.diff, out.note)
    assert p.fill_seconds < 15 and site.clicks == []


def test_the_extension_is_back_within_seconds_of_a_bridge_restart(tmp_path):
    """The poster (or the CLI) restarts its bridge: the extension tries again 1 s, 2 s, 5 s after the drop and then every
    5 s, so it is back within ~5 s of the new bridge — no longer up to a minute (the live waits: 13 s and 30 s)."""
    site = Site()

    async def go():
        b, pw, ctx, sw = await session(tmp_path, site)
        b2 = None
        try:
            assert await connected(b)
            await b.close()
            await asyncio.sleep(9)                      # long enough for its tries to reach the 5 s rhythm
            b2 = await bm.Bridge(TOKEN, port=bm.PORT).start()
            t0 = time.monotonic()
            ok = await connected(b2, seconds=15)
            return ok, time.monotonic() - t0
        finally:
            await ctx.close()
            await pw.stop()
            if b2 is not None:
                await b2.close()
    ok, took = asyncio.run(go())
    assert ok and took < 6.5, took


async def _ready_then(tmp_path, site, then):
    """A supervised Vinted publish up to the terminal's POST prompt, then `then(task, bridge)`; returns the Vinted tabs
    left open a few seconds later."""
    b, pw, ctx, sw = await session(tmp_path, site)
    try:
        assert await connected(b)
        p = poster("vinted", b, tmp_path, verified=True)
        ready = asyncio.Event()

        async def waiting_for_post(fields, site_name):      # the terminal, waiting for POST
            ready.set()
            await asyncio.sleep(3600)
            return True
        p.confirm = waiting_for_post
        task = asyncio.create_task(p.post(None, RENDER, "publish", False, tmp_path / "shots"))
        await asyncio.wait_for(ready.wait(), 60)
        assert [pg for pg in ctx.pages if "vinted.com/items/new" in pg.url]      # the filled form is open
        await then(task, b)
        for _ in range(80):
            if not [pg for pg in ctx.pages if "vinted.com" in pg.url]:
                break
            await asyncio.sleep(0.1)
        return [pg.url for pg in ctx.pages if "vinted.com" in pg.url]
    finally:
        await close(b, pw, ctx)


def test_a_ctrl_c_before_post_closes_the_tab_and_clicks_nothing(tmp_path):
    site = Site()

    async def ctrl_c(task, b):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    tabs = asyncio.run(_ready_then(tmp_path, site, ctrl_c))
    assert tabs == [] and site.clicks == []


def test_a_bridge_that_goes_away_before_post_closes_the_tab(tmp_path):
    """The CLI killed outright (no cancel sent): the extension sees its bridge go and calls the job off itself."""
    site = Site()

    async def gone(task, b):
        for sock in list(b.sockets):
            sock.close()
        b.sockets.clear()
        b.ws_enabled = False                            # and it doesn't come back for this job
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):     # noqa: BLE001
            pass
    tabs = asyncio.run(_ready_then(tmp_path, site, gone))
    assert tabs == [] and site.clicks == []



BUSY = ("<html><head><title>Sell an item | Vinted</title></head><body><h1>Sell an item</h1><script>"
        "setTimeout(() => { const end = Date.now() + 90000; while (Date.now() < end) {} }, 0)</script></body></html>")


def test_a_page_that_never_answers_ends_the_job_with_what_it_shows(tmp_path):
    """The live stall (WO32b): the Vinted tab opened and then nothing, for minutes — its page never answered the worker
    (here: the site's own script holding the page's thread). Now the job ends within ~20 s, saying so, with the tab's
    state and a picture of it; nothing is ever clicked."""
    site = Site(vinted=BUSY)

    async def go():
        b, pw, ctx, sw = await session(tmp_path, site)
        try:
            assert await connected(b)
            p = poster("vinted", b, tmp_path)
            t0 = time.monotonic()
            out = await p.post(None, RENDER, "publish", True, tmp_path / "shots")
            return out, time.monotonic() - t0, p
        finally:
            await close(b, pw, ctx)
    out, took, p = asyncio.run(go())
    assert out.status == "failed" and "the page doesn't answer" in out.error, out.error
    assert '"url":"https://www.vinted.com/items/new"' in out.error and '"frozen":false' in out.error
    assert took < 45 and site.clicks == [] and p.lines[:2] == ["tab opened", "page loaded"]


class RefreshingSite(Site):
    """Vinted as it was live on Oct 7 (WO32b, the Thrift Chrome's history): /items/new answered with a redirect to
    /session-refresh, which went back to /items/new a second later — or (`reload_once`) the form reloading itself once
    while it is being filled."""

    def __init__(self, reload_once=False, **kw):
        super().__init__(**kw)
        self.reload_once, self.forms = reload_once, 0

    async def handle(self, route):
        url = route.request.url
        path = re.sub(r"^https://www\.vinted\.com", "", url).split("?")[0]
        if "vinted.com" in url and path == "/items/new":
            self.forms += 1
            if self.forms == 1 and not self.reload_once:     # (a server redirect in Chrome; Playwright's router
                self.urls.append(url)                          # commits a 302 first: the page hops at once instead)
                return await route.fulfill(status=200, content_type="text/html", body=(
                    "<html><head><script>location.replace('/session-refresh?ref_url=%2Fitems%2Fnew')</script></head>"
                    "<body></body></html>"))
            if self.forms == 1 and self.reload_once:
                self.urls.append(url)
                body = self._html(self.vinted).replace(
                    "</body>", "<script>setTimeout(() => location.reload(), 1200)</script></body>", 1)
                return await route.fulfill(status=200, body=body, content_type="text/html")
        if "vinted.com" in url and path == "/session-refresh":
            self.urls.append(url)
            return await route.fulfill(status=200, content_type="text/html", body=(
                "<html><head><title>Vinted</title></head><body>…<script>setTimeout(() => "
                "location.replace('/items/new'), 1000)</script></body></html>"))
        return await super().handle(route)


@pytest.mark.parametrize("reload_once", [False, True])
def test_a_page_that_moves_on_by_itself_still_gets_its_job(tmp_path, reload_once):
    """The live stall's cause: the job went to Vinted's /session-refresh page, which then went back to /items/new — the
    job died with the refresh page. Now the worker waits for the form, and a page that reloads mid-fill gets the job
    again (nothing can have been published before the go-ahead)."""
    site = RefreshingSite(reload_once=reload_once)

    async def go():
        b, pw, ctx, sw = await session(tmp_path, site)
        try:
            assert await connected(b)
            p = poster("vinted", b, tmp_path)
            return await p.post(None, RENDER, "publish", True, tmp_path / "shots"), p
        finally:
            await close(b, pw, ctx)
    out, p = asyncio.run(go())
    assert out.status == "dryrun", (out.error, out.diff, out.note, p.lines)
    assert site.clicks == []
    assert any(line.startswith("the page reloaded (/items/new) — filling it again") or
               line == "passed through /session-refresh" for line in p.lines), p.lines


def test_the_worker_waits_through_a_page_that_moves_on_not_one_that_stays(tmp_path):
    """landed(): the job's tab on another page than the job's (live: /session-refresh, which went back to /items/new
    a second later) is waited through; a page that stays (a login page) is handed over after 2.5 s, for the page script
    to name — never a wait to the end."""
    site = RefreshingSite()

    async def go():
        b, pw, ctx, sw = await session(tmp_path, site)
        try:
            assert await connected(b)
            return await sw.evaluate("""async () => {
              const w = await chrome.windows.getLastFocused({ windowTypes: ["normal"] });
              const out = [];
              for (const start of ["https://www.vinted.com/session-refresh?ref_url=%2Fitems%2Fnew",
                                   "https://www.vinted.com/member/login"]) {
                const t = await chrome.tabs.create({ url: start, active: true, windowId: w.id });
                const t0 = Date.now();
                const tab = await landed(t.id, "https://www.vinted.com/items/new", 45000);
                out.push([tab.url, Date.now() - t0]);
                await chrome.tabs.remove(t.id);
              }
              return out;
            }""")
        finally:
            await close(b, pw, ctx)
    (moved, waited), (stayed, waited2) = asyncio.run(go())
    assert moved == "https://www.vinted.com/items/new" and 800 < waited < 10000, (moved, waited)
    assert stayed == "https://www.vinted.com/member/login" and 2000 < waited2 < 6000, (stayed, waited2)
