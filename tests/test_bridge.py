"""WO32: the extension bridge (thrift_agent/bridge.py) and the extension driver (post/ext_driver.py), without a browser:
the token, the host and origin checks, CORS for the two sites only, the photos, one job at a time, the socket and the
alarm's HTTP path, the watch (2 minutes → the Thrift Chrome started, 5 → one ops line a window), a result recorded in
`listings`, the publish gate, the one go-ahead, the shop check after an interrupted publish, the stop pages — and the
files around it: the manifest's permissions, the LaunchAgent's switches, the reload hash."""
import asyncio
import base64
import json
import plistlib
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from ext_fake import PNG, FakeExtension, ws_open, ws_recv, ws_send

from thrift_agent import bridge as bm
from thrift_agent import crosslist, notify
from thrift_agent.config import ROOT, Settings
from thrift_agent.db import DB
from thrift_agent.post import ext_driver, runner
from thrift_agent.post.base import AccountBlocked, Outcome, PosterError
from thrift_agent.post.depop_api import DepopApiPoster, product_request
from thrift_agent.post.ext_driver import ExtensionPoster
from thrift_agent.schema import Render

TOKEN = "bridge-test-token-" + "z" * 30
TITLE = "Tory Burch Red Ballet Flats size 7.5"
RENDER = Render(marketplace="vinted", title=TITLE, description="Red flats.", tags=[], brand="Tory Burch",
                department="Women", category="Shoes", subcategory=None, size="7.5", colors=["Red"],
                condition="good", price=85, photos=[], sku="i_1")


def run(coro):
    return asyncio.run(coro)


class Fields(SimpleNamespace):
    def model_dump(self):
        return dict(self.__dict__)


def vinted_fields(photos=("/w/cover.jpg",)):
    return Fields(category_id=2955, category_path="Women > Shoes > Ballerinas", title=TITLE,
                  description=f"{TITLE}\n\nRed flats.", brand="Tory Burch", size="7.5", condition="Good",
                  colors=["Red"], materials=[], skirt_length=None, package_sizes=["SMALL", "MEDIUM"], price=85,
                  photos=list(photos), guesses=[])


def depop_fields():
    return Fields(category="Women > Footwear > Ballet shoes", description=f"{TITLE}\n\nRed flats.\n\n#toryburch",
                  brand="Tory Burch", size="US 7.5", condition="Used - Good", colors=["Red"], source=[], age=None,
                  style=[], attributes={}, shipping="Depop Shipping", package_size="Small", price=85,
                  photos=["/w/cover.jpg"], guesses=[])


@pytest.fixture
def selectors(tmp_path):
    """A copy of ext/selectors.json with nothing recorded yet; `verify(*steps)` marks steps recorded for both sites."""
    data = json.loads((bm.EXT_DIR / "selectors.json").read_text(encoding="utf-8"))
    for site in ("vinted", "depop"):
        for step in data[site]["steps"].values():
            step["verified"] = False
    path = tmp_path / "selectors.json"

    def verify(*steps):
        for site in ("vinted", "depop"):
            for name in steps or data[site]["steps"]:
                if name in data[site]["steps"]:              # hide is Vinted's, mark_sold Depop's
                    data[site]["steps"][name]["verified"] = True
        path.write_text(json.dumps(data), encoding="utf-8")
        return path
    path.write_text(json.dumps(data), encoding="utf-8")
    verify.path = path
    return verify


async def _bridge(**kw) -> bm.Bridge:
    return await bm.Bridge(TOKEN, port=0, pace=0.01, **kw).start()


def _client(b: bm.Bridge) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=b.base, timeout=5)


# ---------------------------------------------------------------- HTTP: token, host, CORS, photos

def test_the_token_and_the_host_are_checked():
    async def go():
        b = await _bridge()
        async with _client(b) as c:
            assert (await c.get("/health")).status_code == 200                       # nothing in it: no token
            assert (await c.get("/jobs/next")).status_code == 401
            assert (await c.get("/jobs/next", headers={"X-Thrift-Token": "wrong"})).status_code == 401
            assert (await c.get("/jobs/next", headers={"X-Thrift-Token": TOKEN})).status_code == 204
            r = await c.get("/jobs/next", headers={"X-Thrift-Token": TOKEN, "Host": "evil.example"})
            assert r.status_code == 403                                               # DNS rebinding: refused
            assert (await c.post("/events", headers={"X-Thrift-Token": "wrong"}, json={})).status_code == 401
        await b.close()
    run(go())


def test_cors_answers_only_the_two_sites():
    async def go():
        b = await _bridge()
        async with _client(b) as c:
            for origin in bm.SITE_ORIGINS:
                r = await c.get("/health", headers={"Origin": origin})
                assert r.headers["access-control-allow-origin"] == origin
                pre = await c.options("/photos/i_1/1.jpg", headers={"Origin": origin})
                assert pre.status_code == 204 and "X-Thrift-Token" in pre.headers["access-control-allow-headers"]
            for origin in ("https://evil.example", "https://www.vinted.com.evil.example", "null"):
                r = await c.get("/health", headers={"Origin": origin})
                assert "access-control-allow-origin" not in r.headers
        await b.close()
    run(go())


def test_photos_are_served_with_the_token_cover_first(tmp_path):
    cover, back = tmp_path / "cover.jpg", tmp_path / "back.jpg"
    cover.write_bytes(b"\xff\xd8cover")
    back.write_bytes(b"\xff\xd8back")

    async def go():
        b = await _bridge()
        urls = b.photo_urls("i_1", [cover, back])
        assert urls == [f"{b.base}/photos/i_1/1.jpg", f"{b.base}/photos/i_1/2.jpg"]
        h = {"X-Thrift-Token": TOKEN}
        async with _client(b) as c:
            r = await c.get("/photos/i_1/1.jpg", headers=h)
            assert r.status_code == 200 and r.content == b"\xff\xd8cover" and r.headers["content-type"] == "image/jpeg"
            assert (await c.get("/photos/i_1/2.jpg", headers=h)).content == b"\xff\xd8back"
            assert (await c.get("/photos/i_1/3.jpg", headers=h)).status_code == 404
            assert (await c.get("/photos/i_9/1.jpg", headers=h)).status_code == 404
            assert (await c.get("/photos/i_1/1.jpg")).status_code == 401
            assert (await c.get("/photos/i_1/..%2F..%2Fstate.db", headers=h)).status_code == 404
        await b.close()
    run(go())


# ---------------------------------------------------------------- jobs

def test_one_job_at_a_time_per_site_through_the_alarms_poll():
    """The extension's HTTP path (its socket down): GET /jobs/next hands out one job PER SITE (WO33: Depop and Vinted run
    together); a site's next job only once its first has its last word; an event's answer carries the job's go-ahead."""
    async def go():
        b = await _bridge()
        first = b.submit("vinted", "dry_run", {"fields": {}, "copy": {}, "price": 1, "photos": []})
        again = b.submit("vinted", "dry_run", {"fields": {}, "copy": {}, "price": 1, "photos": []})
        other = b.submit("depop", "dry_run", {"fields": {}, "copy": {}, "price": 1, "photos": []})
        h = {"X-Thrift-Token": TOKEN}
        async with _client(b) as c:
            got = (await c.get("/jobs/next", headers=h)).json()
            assert got["job_id"] == first.id and got["site"] == "vinted" and got["mode"] == "dry_run"
            assert got["pace"] == 0.01 and b.connected()
            assert (await c.get("/jobs/next", headers=h)).json()["job_id"] == other.id     # Depop's, beside it
            assert (await c.get("/jobs/next", headers=h)).status_code == 204               # Vinted's second waits
            b.command(first, "submit")
            r = await c.post("/events", headers=h, json={"event": "waiting", "job_id": first.id})
            assert r.json() == {"commands": [{"type": "submit", "job_id": first.id}]} and first.clicked
            await c.post("/events", headers=h, json={"event": "step", "job_id": first.id, "name": "title", "ok": True})
            await c.post("/events", headers=h, json={"event": "result", "job_id": first.id, "url": "u"})
            assert (await c.get("/jobs/next", headers=h)).json()["job_id"] == again.id
        assert (await first.wait(("result",), 1))["url"] == "u" and first.steps[0]["name"] == "title"
        await b.close()
    run(go())


def test_a_job_s_photos_are_served_under_its_site():
    """Depop's ≤ 8 and Vinted's ≤ 20 photos of one item are different lists, and their jobs run together (WO33)."""
    async def go(tmp):
        from PIL import Image
        a, z = tmp / "a.jpg", tmp / "z.jpg"
        Image.new("RGB", (4, 4), (255, 0, 0)).save(a)
        Image.new("RGB", (4, 4), (0, 0, 255)).save(z)
        b = await _bridge()
        try:
            dep = b.photo_urls("i_1", [a], "depop")
            vin = b.photo_urls("i_1", [z, a], "vinted")
            async with _client(b) as c:
                h = {"X-Thrift-Token": TOKEN}
                d1 = (await c.get(dep[0].replace(b.base, ""), headers=h)).content
                v1 = (await c.get(vin[0].replace(b.base, ""), headers=h)).content
            return dep, vin, d1 == a.read_bytes(), v1 == z.read_bytes()
        finally:
            await b.close()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        dep, vin, d_ok, v_ok = run(go(Path(d)))
    assert dep[0].endswith("/photos/depop/i_1/1.jpg") and vin[1].endswith("/photos/vinted/i_1/2.jpg")
    assert d_ok and v_ok


def test_the_socket_needs_an_extension_origin_and_the_token():
    async def go():
        b = await _bridge()
        _, w, status = await ws_open(b.port, origin="https://evil.example")
        assert " 403 " in status
        w.close()
        r, w, status = await ws_open(b.port)
        assert " 101 " in status
        ws_send(w, {"type": "hello", "token": "wrong"})
        assert (await ws_recv(r)) == {"type": "refused"} and not b.connected()
        w.close()
        r, w, _ = await ws_open(b.port)
        ws_send(w, {"type": "hello", "token": TOKEN, "loaded": bm.files_hash()})
        welcome = await ws_recv(r)
        assert welcome == {"type": "welcome", "ext_hash": bm.files_hash()} and b.connected()
        w.close()
        await b.close()
        off = await _bridge(ws_enabled=False)                     # the tests' "socket down"
        _, w, status = await ws_open(off.port)
        assert " 403 " in status
        w.close()
        await off.close()
    run(go())


def test_jobs_are_pushed_over_the_socket_and_their_evidence_kept(tmp_path):
    async def go():
        b = await _bridge()
        r, w, _ = await ws_open(b.port)
        ws_send(w, {"type": "hello", "token": TOKEN, "loaded": bm.files_hash()})
        await ws_recv(r)
        shot = tmp_path / "i_1-vinted-x.png"
        job = b.submit("vinted", "publish", {"fields": {"a": 1}, "copy": {}, "price": 85, "photos": []}, shot=shot)
        pushed = await ws_recv(r)
        assert pushed["type"] == "job" and pushed["job"]["job_id"] == job.id and job.handed is not None
        ws_send(w, {"type": "event", "event": "screenshot", "job_id": job.id, "label": "form",
                    "png_b64": base64.b64encode(PNG).decode(), "html": "<p>form</p>", "url": "https://x"})
        ws_send(w, {"type": "event", "event": "ready", "job_id": job.id, "seen": {"title": TITLE}})
        ready = await job.wait(("ready",), 2)
        assert ready["seen"] == {"title": TITLE}
        assert shot.read_bytes() == PNG and shot.with_suffix(".html").read_text(encoding="utf-8") == "<p>form</p>"
        b.command(job, "submit")
        assert (await ws_recv(r)) == {"type": "submit", "job_id": job.id}
        ws_send(w, {"type": "event", "event": "screenshot", "job_id": job.id, "label": "after-publish",
                    "png_b64": base64.b64encode(PNG).decode()})
        ws_send(w, {"type": "event", "event": "result", "job_id": job.id, "url": "https://www.vinted.com/items/1"})
        assert (await job.wait(("result",), 2))["url"].endswith("/items/1") and job.done
        assert (tmp_path / "i_1-vinted-x-after-publish.png").read_bytes() == PNG
        ws_send(w, {"type": "event", "event": "error", "job_id": job.id, "message": "late"})   # a second last word
        await asyncio.sleep(0.1)
        assert job.final["event"] == "result"
        w.close()
        await b.close()
    run(go())


def test_a_deploy_that_changes_the_extension_is_announced(tmp_path):
    """The extension reloads itself on a new files hash; the bridge tells it when ext/ changes on disk."""
    import shutil
    ext = tmp_path / "ext"
    shutil.copytree(bm.EXT_DIR, ext)

    async def go():
        b = await bm.Bridge(TOKEN, port=0, ext_dir=ext).start()
        r, w, _ = await ws_open(b.port)
        ws_send(w, {"type": "hello", "token": TOKEN, "loaded": bm.files_hash()})
        first = await ws_recv(r)
        assert first == {"type": "welcome", "ext_hash": bm.files_hash(ext)} and b.announce_files() is False
        (ext / "selectors.json").write_text((ext / "selectors.json").read_text(encoding="utf-8") + "\n",
                                            encoding="utf-8")
        assert b.announce_files() is True and b.announce_files() is False
        again = await ws_recv(r)
        assert again["type"] == "welcome" and again["ext_hash"] != first["ext_hash"]
        w.close()
        await b.close()
    run(go())


def test_an_extension_about_to_reload_gets_no_job_meanwhile(monkeypatch):
    """WO32b: after a deploy the extension reloads as soon as it is welcomed with the new files' hash — a job handed to it
    then would be lost with the reload. Its socket gets none for RELOAD_HOLD; one that stays connected gets it after."""
    monkeypatch.setattr(bm, "RELOAD_HOLD", 0.5)

    async def go():
        b = await _bridge()
        try:
            r, w, _ = await ws_open(b.port)
            ws_send(w, {"type": "hello", "token": TOKEN, "loaded": "the-files-before-the-deploy"})
            assert (await ws_recv(r))["type"] == "welcome"
            job = b.submit("vinted", "dry_run", {"fields": {}, "copy": {}, "price": 1, "photos": []})
            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                await ws_recv(r, timeout=0.3)                       # held: it is reloading
            got = await ws_recv(r, timeout=3)                       # still here after the hold: it gets the job
            w.close()                                               # it reloads after all; the new one says it's
            for _ in range(50):                                     # up to date: a job at once
                if not b.sockets:
                    break
                await asyncio.sleep(0.02)
            r2, w2, _ = await ws_open(b.port)
            ws_send(w2, {"type": "hello", "token": TOKEN, "loaded": bm.files_hash()})
            assert (await ws_recv(r2))["type"] == "welcome"
            job2 = b.submit("depop", "dry_run", {"fields": {}, "copy": {}, "price": 1, "photos": []})
            b.jobs[job.id].final, b.jobs[job.id].done = {"event": "result"}, True     # the first one is over
            b._push()
            got2 = await ws_recv(r2, timeout=1)
            w2.close()
            return got, job, got2, job2
        finally:
            await b.close()
    got, job, got2, job2 = run(go())
    assert got["type"] == "job" and got["job"]["job_id"] == job.id
    assert got2["type"] == "job" and got2["job"]["job_id"] == job2.id


def test_a_job_given_up_on_ignores_late_events():
    async def go():
        b = await _bridge()
        job = b.submit("depop", "dry_run", {"fields": {}, "copy": {}, "price": 1, "photos": []})
        b.drop(job)
        assert b._event({"event": "result", "job_id": job.id}) == [] and job._events.empty()   # the driver never sees it
        h = {"X-Thrift-Token": TOKEN}
        async with _client(b) as c:
            assert (await c.get("/jobs/next", headers=h)).status_code == 204      # never handed out
        await b.close()
    run(go())


def test_a_job_called_off_is_cancelled_in_the_extension_and_the_next_waits_for_its_end():
    async def go():
        b = await _bridge()
        r, w, _ = await ws_open(b.port)
        ws_send(w, {"type": "hello", "token": TOKEN, "loaded": bm.files_hash()})
        await ws_recv(r)
        first = b.submit("vinted", "publish", {"fields": {}, "copy": {}, "price": 1, "photos": []})
        assert (await ws_recv(r))["job"]["job_id"] == first.id
        second = b.submit("vinted", "dry_run", {"fields": {}, "copy": {}, "price": 1, "photos": []})
        b.drop(first)                                           # the driver's time is up
        assert (await ws_recv(r)) == {"type": "cancel", "job_id": first.id}
        assert second.handed is None                            # the extension still has Vinted's first in hand
        ws_send(w, {"type": "event", "event": "error", "job_id": first.id, "stage": "cancelled", "message": "x"})
        assert (await ws_recv(r))["job"]["job_id"] == second.id and first._events.empty()
        w.close()
        await b.close()
    run(go())


def test_the_port_taken_is_a_clear_error():
    async def go():
        b = await _bridge()
        with pytest.raises(bm.BridgeError, match="is the poster service running"):
            await bm.Bridge(TOKEN, port=b.port).start()
        await b.close()
    run(go())


def test_the_token_file(tmp_path):
    with pytest.raises(bm.BridgeError, match="mac_setup.sh makes it"):
        bm.read_token(tmp_path / "missing")
    (tmp_path / "short").write_text("abc\n", encoding="utf-8")
    with pytest.raises(bm.BridgeError, match="too short"):
        bm.read_token(tmp_path / "short")
    (tmp_path / "ok").write_text(TOKEN + "\n", encoding="utf-8")
    assert bm.read_token(tmp_path / "ok") == TOKEN and len(bm.new_token()) >= 40


# ---------------------------------------------------------------- the watch

def test_the_watch_starts_the_thrift_chrome_then_says_once():
    b = bm.Bridge(TOKEN)
    started, said = [], []
    t0 = b.started
    tick = lambda dt, window=True: b.watch_tick(t0 + dt, window, lambda: started.append(dt), said.append)  # noqa: E731
    assert tick(60) is None and started == []
    assert tick(130, window=False) is None and started == []             # the lid closed: nothing
    assert tick(130) == "chrome started" and started == [130]
    assert tick(200) is None and started == [130]                         # once
    assert tick(310) == "said" and said == [bm.MISSING_LINE]
    assert tick(900) is None and len(said) == 1
    b.last_seen = t0 + 1000                                               # it checked in: all reset
    assert b.connected() is False or True
    b.chrome_started, b.said_missing = None, False


def test_open_bridge_says_the_missing_line_once_a_window(tmp_path, monkeypatch):
    s = Settings({"machine_role": "prod", "paths": {"db": str(tmp_path / "state.db")},
                  "bridge": {"token_file": str(tmp_path / "ext_token"), "port": 0}})
    (tmp_path / "ext_token").write_text(TOKEN, encoding="utf-8")
    db = DB(s.path("db"))
    said, started = [], []
    monkeypatch.setattr(notify, "say", said.append)
    monkeypatch.setattr(runner, "start_thrift_chrome", lambda s_: started.append(1) or "ok")
    monkeypatch.setattr(runner.power, "lid_closed", lambda: False)
    monkeypatch.setattr(bm, "WATCH_EVERY", 0.02)
    monkeypatch.setattr(bm, "START_CHROME_AFTER", 0.05)
    monkeypatch.setattr(bm, "SAY_AFTER", 0.1)

    async def go():
        for _ in range(2):                            # two poster starts in one window
            ps = {"vinted": ExtensionPoster("vinted")}
            b, task = await runner.open_bridge(s, db, ps)
            assert ps["vinted"].bridge is b and task is not None
            await asyncio.sleep(0.4)
            await runner.close_bridge(b, task)
    run(go())
    assert said == [bm.MISSING_LINE] and len(started) == 2
    assert json.loads(db.kv_get(crosslist.EXT_LINK))["connected"] is False
    assert [e["kind"] for e in db.conn.execute("SELECT kind FROM events WHERE kind='thrift_chrome_started'")] == \
        ["thrift_chrome_started"] * 2


def test_no_token_turns_only_the_extension_marketplaces_off(tmp_path, monkeypatch):
    s = Settings({"machine_role": "prod", "bridge": {"token_file": str(tmp_path / "none"), "port": 0}})
    said = []
    monkeypatch.setattr(notify, "say", said.append)
    ps = {"poshmark": object(), "vinted": ExtensionPoster("vinted"), "depop": ExtensionPoster("depop")}
    b, task = run(runner.open_bridge(s, None, ps))
    assert b is None and task is None and list(ps) == ["poshmark"]
    assert said and said[0].startswith("❗ Vinted, Depop off for this run: no extension token")


# ---------------------------------------------------------------- the driver

def _poster(mp, b, selectors_path, fields=None, shop=""):
    p = ExtensionPoster(mp, shop, bridge=b)
    p.fields = fields or (vinted_fields() if mp == "vinted" else depop_fields())
    p.selectors_path = selectors_path
    return p


async def _with_fake(results, mp="vinted", selectors_path=None, fields=None, shop="", dry=False, confirm=None,
                     tmp=None, on_go_ahead=None, progress=None):
    b = await _bridge()
    fake = FakeExtension(b.port, TOKEN, results)
    fake.start()
    await asyncio.wait_for(fake.connected.wait(), 5)
    p = _poster(mp, b, selectors_path, fields, shop)
    p.confirm, p.on_go_ahead, p.progress = confirm, on_go_ahead, progress
    p.fake = fake
    try:
        out = await p.post(None, RENDER, "publish", dry, tmp)
    finally:
        fake.stop()
        await b.close()
    return out, fake, p


def test_a_dry_run_reads_back_and_never_clicks(tmp_path, selectors):
    out, fake, _ = run(_with_fake([Outcome("dryrun", guesses=["brand set to 'J. Crew' (from 'J.Crew')"])],
                                  selectors_path=selectors.path, dry=True, tmp=tmp_path))
    assert out.status == "dryrun" and fake.clicks == 0 and fake.jobs[0]["mode"] == "dry_run"
    assert out.guesses == ["brand set to 'J. Crew' (from 'J.Crew')"]
    assert Path(out.screenshot).read_bytes() == PNG and Path(out.screenshot).name.startswith("i_1-vinted-")
    record = json.loads(Path(out.screenshot).with_suffix(".json").read_text(encoding="utf-8"))
    assert record["diff"] == {} and record["steps"][0]["name"] == "photos"
    job = fake.jobs[0]
    assert job["fields"]["category_id"] == 2955 and job["copy"]["title"] == TITLE and job["price"] == 85
    assert job["photos"][0].endswith("/photos/vinted/i_1/1.jpg")


def test_a_read_back_that_differs_fails_the_dry_run(tmp_path, selectors, monkeypatch):
    import ext_fake
    real = ext_fake.seen_for
    monkeypatch.setattr(ext_fake, "seen_for", lambda job: {**real(job), "price": "58"})
    out, fake, _ = run(_with_fake([Outcome("dryrun")], selectors_path=selectors.path, dry=True, tmp=tmp_path))
    assert out.status == "failed" and "price" in out.diff and out.error.startswith("Mismatch")


def test_publish_waits_for_the_steps_to_be_recorded(tmp_path, selectors):
    """The same gate as WO30: until a Mac dry run recorded the Upload button (ext/selectors.json), no publish."""
    out, fake, _ = run(_with_fake([Outcome("posted", url="https://www.vinted.com/items/123")],
                                  selectors_path=selectors.path, tmp=tmp_path))
    assert out.status == "failed" and "after_publish, submit UNVERIFIED" in out.error and fake.clicks == 0
    supervised = SimpleNamespace(calls=0)

    async def yes(fields, site):
        supervised.calls += 1
        return True
    out, fake, _ = run(_with_fake([Outcome("posted", url="https://www.vinted.com/items/123")],
                                  selectors_path=selectors("submit"), confirm=yes, tmp=tmp_path))
    assert out.status == "posted" and out.url == "https://www.vinted.com/items/123" and fake.clicks == 1
    assert supervised.calls == 1 and out.clicked                         # the supervised one needs only the button


def test_publish_clicks_once_and_checks_the_live_page(tmp_path, selectors):
    out, fake, p = run(_with_fake([Outcome("posted", url="https://www.vinted.com/items/4242-red-flats?ref=x")],
                                  selectors_path=selectors(), tmp=tmp_path))
    assert out.status == "posted" and out.url == "https://www.vinted.com/items/4242" and fake.clicks == 1
    assert not any("live check" in n for n in (out.note or "").split("; "))
    assert Path(out.screenshot).name.endswith("-after-publish.png")


def test_the_owner_declining_cancels_without_a_click(tmp_path, selectors):
    async def no(fields, site):
        return False
    out, fake, _ = run(_with_fake([Outcome("posted", url="https://www.vinted.com/items/1")],
                                  selectors_path=selectors(), confirm=no, tmp=tmp_path))
    assert out.status == "cancelled" and fake.clicks == 0 and "confirmation wasn't typed" in out.note


@pytest.mark.parametrize("page,line", [("login", "Vinted needs you to log in on the Mac."),
                                       ("captcha", "Vinted shows a CAPTCHA — solve it in its Chrome window on the Mac."),
                                       ("block", "Vinted turned the Mac away for now — I'll try again next time the "
                                                 "Mac is open."),
                                       ("verify", "Vinted asks for a check — open it on the Mac.")])
def test_a_stop_page_stops_the_site_with_its_own_line(tmp_path, selectors, page, line, monkeypatch):
    with pytest.raises(AccountBlocked) as e:
        run(_with_fake([AccountBlocked("the page", page=page)], selectors_path=selectors(), tmp=tmp_path))
    assert e.value.page == page
    s = Settings({"machine_role": "prod", "paths": {"db": str(tmp_path / "state.db")}})
    db = DB(s.path("db"))
    group, ops = [], []
    monkeypatch.setattr(notify, "group", group.append)
    monkeypatch.setattr(notify, "say", ops.append)
    crosslist.block(db, "vinted", str(e.value), e.value.page)
    assert group == [line] and ops[0].startswith("⛔ Vinted stopped for this window")


def test_after_the_click_without_a_listing_page_the_shop_is_looked_at(tmp_path, selectors):
    """A publish that ended anywhere but a listing page: the seller's shop, the one listing with this title —
    never a second click; not found, it stays unconfirmed."""
    found = SimpleNamespace(status="posted", listings=[
        {"url": "https://www.vinted.com/items/77-tory-burch-red-ballet-flats", "text": TITLE},
        {"url": "https://www.vinted.com/items/78-blue-jeans", "text": "Levi's Blue Jeans"}])
    out, fake, _ = run(_with_fake([Outcome("failed", clicked=True, error="no listing page"), found],
                                  selectors_path=selectors(), shop="seller", tmp=tmp_path))
    assert out.status == "posted" and out.url == "https://www.vinted.com/items/77" and fake.clicks == 1
    assert [j["mode"] for j in fake.jobs] == ["publish", "find"] and fake.jobs[1]["shop"] == "seller"
    two = SimpleNamespace(status="posted", listings=[{"url": "https://www.vinted.com/items/77-a", "text": TITLE},
                                                     {"url": "https://www.vinted.com/items/79-b", "text": TITLE}])
    out, fake, _ = run(_with_fake([Outcome("failed", clicked=True, error="no listing page"), two],
                                  selectors_path=selectors(), shop="seller", tmp=tmp_path))
    assert out.status == "failed" and out.clicked and out.url is None and "It may be live" in out.error
    out, fake, _ = run(_with_fake([Outcome("failed", clicked=True, error="no listing page")],
                                  selectors_path=selectors(), tmp=tmp_path))     # no shop configured
    assert out.status == "failed" and out.clicked and [j["mode"] for j in fake.jobs] == ["publish"]


def test_a_job_no_extension_takes_is_given_up_before_anything_opens(tmp_path, selectors, monkeypatch):
    monkeypatch.setattr(bm, "PICKUP_TIMEOUT", 0.3)

    async def go():
        b = await _bridge()
        p = _poster("vinted", b, selectors.path)
        try:
            with pytest.raises(PosterError, match="didn't take the job"):
                await p.post(None, RENDER, "publish", True, tmp_path)
            assert not b.queue
        finally:
            await b.close()
    run(go())


# ---------------------------------------------------------------- WO32b: progress, the go-ahead, the connect

def test_the_progress_reaches_the_cli_line_by_line(tmp_path, selectors):
    """What the owner sees at the terminal while the form fills: the tab, the photos, each field, the time it took."""
    lines = []
    out, fake, p = run(_with_fake([Outcome("dryrun")], selectors_path=selectors.path, dry=True, tmp=tmp_path,
                                  progress=lines.append))
    assert out.status == "dryrun"
    assert lines == ["tab opened", "photos 1/1", "title ✓", "description ✓", "category ✓", "price ✓",
                     "filled in 1.2 s"]
    assert p.lines == lines and p.fill_seconds == 1.2
    record = json.loads(Path(out.screenshot).with_suffix(".json").read_text(encoding="utf-8"))
    assert record["steps"][0] == {"name": "photos", "ok": True, "detail": "1/1 shown", "clicked": None, "ms": 120}


def test_the_row_is_taken_only_at_the_go_ahead(tmp_path, selectors):
    """WO32b: the row becomes 'posting' (on_go_ahead) after POST and right before the one click — and a row that can't
    be taken then is never clicked."""
    order = []

    async def post_typed(fields, site):
        order.append("POST")
        return True

    def take():
        order.append("taken")
        return True
    out, fake, _ = run(_with_fake([Outcome("posted", url="https://www.vinted.com/items/123")],
                                  selectors_path=selectors("submit"), confirm=post_typed, tmp=tmp_path,
                                  on_go_ahead=take))
    assert out.status == "posted" and fake.clicks == 1 and order == ["POST", "taken"]
    assert [c["type"] for c in fake.commands] == ["submit"]
    out, fake, _ = run(_with_fake([Outcome("posted", url="https://www.vinted.com/items/123")],
                                  selectors_path=selectors("submit"), confirm=post_typed, tmp=tmp_path,
                                  on_go_ahead=lambda: False))
    assert out.status == "failed" and not out.clicked and fake.clicks == 0 and "couldn't be taken" in out.error
    assert [c["type"] for c in fake.commands] == ["cancel"]


def test_a_form_that_went_away_before_post_is_never_clicked(tmp_path, selectors):
    """The owner closed the tab (or the extension ended the job) while the terminal waited for POST: no go-ahead,
    nothing taken, nothing clicked."""
    taken = []
    box = {}

    async def post_after_the_tab_closed(fields, site):
        fake = box["p"].fake
        fake.event(fake.writer, event="error", job_id=fake.jobs[-1]["job_id"], stage="tab_closed", page="unknown",
                   message="the job's tab was closed")
        await fake.writer.drain()
        await asyncio.sleep(0.3)
        return True

    async def go():
        b = await _bridge()
        fake = FakeExtension(b.port, TOKEN, [Outcome("posted", url="https://www.vinted.com/items/123")])
        fake.start()
        await asyncio.wait_for(fake.connected.wait(), 5)
        p = _poster("vinted", b, selectors("submit"))
        p.fake, box["p"] = fake, p
        p.confirm, p.on_go_ahead = post_after_the_tab_closed, lambda: taken.append(1) or True
        try:
            return await p.post(None, RENDER, "publish", False, tmp_path), fake
        finally:
            fake.stop()
            await b.close()
    out, fake = run(go())
    assert out.status == "failed" and not out.clicked and fake.clicks == 0 and taken == []
    assert "went away before the go-ahead (the job's tab was closed)" in out.error


def test_a_ctrl_c_calls_the_job_off_in_the_extension_before_it_ends(tmp_path, selectors):
    """A Ctrl+C while the terminal waits for POST: the job is cancelled in the extension (it closes the tab) and its
    last word is awaited before the CLI goes — never a click."""
    async def ctrl_c(fields, site):
        raise asyncio.CancelledError

    async def go():
        b = await _bridge()
        fake = FakeExtension(b.port, TOKEN, [Outcome("posted", url="https://www.vinted.com/items/123")])
        fake.start()
        await asyncio.wait_for(fake.connected.wait(), 5)
        p = _poster("vinted", b, selectors("submit"))
        p.confirm = ctrl_c
        try:
            with pytest.raises(asyncio.CancelledError):
                await p.post(None, RENDER, "publish", False, tmp_path)
            return fake, list(b.jobs.values())[0]
        finally:
            fake.stop()
            await b.close()
    fake, job = run(go())
    assert [c["type"] for c in fake.commands] == ["cancel"] and fake.clicks == 0
    assert job.final and job.final["message"] == "cancelled"            # its end came back before post() returned


def test_why_no_extension_is_connected():
    """The CLI's reason after 30 s: a token refused, the Thrift Chrome not running, or an extension that never
    knocked."""
    async def go():
        b = await _bridge()
        try:
            never, not_running = b.why_missing(chrome_running=True), b.why_missing(chrome_running=False)
            r, w, _ = await ws_open(b.port)
            ws_send(w, {"type": "hello", "token": "not-the-token-" + "q" * 20})
            assert (await ws_recv(r))["type"] == "refused"
            w.close()
            refused = b.why_missing(chrome_running=True)
            async with _client(b) as c:
                await c.get("/jobs/next", headers={"X-Thrift-Token": TOKEN})     # the right token: not refused now
            return never, not_running, refused, b.why_missing(chrome_running=True)
        finally:
            await b.close()
    never, not_running, refused, later = run(go())
    assert "never knocked" in never and "chrome://extensions" in never
    assert "isn't running" in not_running and "services.sh start chrome" in not_running
    assert "token the bridge refused" in refused and "ext_token" in refused
    assert "refused" not in later


def test_the_cli_waits_seconds_for_the_extension_and_says_why_it_isnt_there(monkeypatch):
    monkeypatch.setattr(runner, "thrift_chrome_running", lambda: False)

    async def go():
        b = await _bridge()
        said = []
        try:
            fake = FakeExtension(b.port, TOKEN, [Outcome("dryrun")])
            fake.start()
            took = await runner.connect_extension(b, _poster("vinted", b, None), said.append)
            fake.stop()
            await asyncio.sleep(0.2)
            b.last_seen, b.sockets = None, set()                       # gone again
            with pytest.raises(RuntimeError, match="isn't running"):
                await runner.connect_extension(b, _poster("vinted", b, None), said.append, first=0.3, then=0.3)
        finally:
            await b.close()
        return took, said
    took, said = run(go())
    assert took < 5 and said[0] == "Waiting for the Thrift Chrome extension…" and said[1].startswith(
        "  extension connected (")
    assert said[3] == ("  not yet: the Thrift Chrome isn't running — start it: bash ~/thrift-agent/deploy/services.sh "
                       "start chrome")


def test_a_job_past_three_minutes_fails_with_nothing_submitted(tmp_path, selectors, monkeypatch):
    assert bm.JOB_TIMEOUT == 180.0                                       # WO32b: 3 minutes, not 6
    monkeypatch.setattr(bm, "JOB_TIMEOUT", 0.5)

    async def go():
        b = await _bridge()
        r, w, _ = await ws_open(b.port)
        ws_send(w, {"type": "hello", "token": TOKEN, "loaded": bm.files_hash()})
        await ws_recv(r)
        p = _poster("vinted", b, selectors())
        out = await p.post(None, RENDER, "publish", False, tmp_path)       # the extension takes it, then nothing
        w.close()
        await b.close()
        return out
    out = run(go())
    assert out.status == "failed" and "wasn't done in" in out.error and not out.clicked


def test_check_login_verify_and_delist(tmp_path, selectors):
    async def go():
        b = await _bridge()
        fake = FakeExtension(b.port, TOKEN, [SimpleNamespace(status="ok", body=f"{TITLE}\n$85.00")])
        fake.start()
        await asyncio.wait_for(fake.connected.wait(), 5)
        p = _poster("vinted", b, selectors.path)
        assert await p.check_login() == "form"
        await p.verify_live(None, "https://www.vinted.com/items/55-flats", RENDER)
        with pytest.raises(PosterError, match="delisting isn't recorded yet .hide UNVERIFIED"):
            await p.delist("https://www.vinted.com/items/55")
        p.selectors_path = selectors("hide")
        assert await p.delist("https://www.vinted.com/items/55-flats") is True
        assert [j["mode"] for j in fake.jobs] == ["check_login", "verify", "delist"]
        assert fake.jobs[2]["listing_url"] == "https://www.vinted.com/items/55"
        fake.results = [SimpleNamespace(status="ok", body="Some other page")]
        with pytest.raises(PosterError, match="doesn't show the title"):
            await p.verify_live(None, "https://www.vinted.com/items/55", RENDER)
        fake.results = [AccountBlocked("logged out", page="login")]
        with pytest.raises(AccountBlocked):
            await p.check_login()
        fake.stop()
        await b.close()
    run(go())


def test_the_job_payload_for_depop(tmp_path, selectors):
    out, fake, _ = run(_with_fake([Outcome("dryrun")], mp="depop", selectors_path=selectors.path, dry=True,
                                  tmp=tmp_path))
    job = fake.jobs[0]
    assert out.status == "dryrun" and job["site"] == "depop"
    assert job["copy"] == {"title": TITLE, "description": f"{TITLE}\n\nRed flats.\n\n#toryburch"}
    assert job["fields"]["category"] == "Women > Footwear > Ballet shoes" and job["fields"]["shipping"] == "Depop Shipping"
    assert job["fields"]["brand_typed"] == "Tory Burch"


def test_a_result_is_recorded_in_listings(tmp_path, selectors, monkeypatch):
    """run_cross with the extension driver: the row taken ('posting'), the result recorded posted with its address."""
    s = Settings({"machine_role": "prod", "paths": {k: str(tmp_path / k) for k in ("failed", "control")} |
                  {"db": str(tmp_path / "state.db")},
                  "poster": {"dry_run": False, "autopublish_confirmed": True},
                  "schedule": {"timezone": "America/New_York", "daily_cap": 25},
                  "marketplaces": {"vinted": {"enabled": True, "autopublish": True}}})
    db = DB(s.path("db"))
    bid = db.add_batch("share", 1)
    iid = db.add_item(bid, 1, "work/1")
    db.set_item(iid, status="posted", owner_price=85,
                renders={"poshmark": RENDER.model_copy(update={"marketplace": "poshmark", "sku": iid}).model_dump()})
    db.upsert_listing(iid, "vinted", status="queued", price=85)
    monkeypatch.setattr(runner, "map_fields", lambda mp, view: vinted_fields())
    monkeypatch.setattr(runner.ItemView, "from_row", classmethod(lambda cls, it: SimpleNamespace()))
    monkeypatch.setattr(notify, "ops_photo", lambda *a: None)
    monkeypatch.setattr(notify, "say", lambda *a: None)

    async def go():
        b = await _bridge()
        fake = FakeExtension(b.port, TOKEN, [Outcome("posted", url="https://www.vinted.com/items/9001-flats")])
        fake.start()
        await asyncio.wait_for(fake.connected.wait(), 5)
        p = ExtensionPoster("vinted", bridge=b)
        p.selectors_path = selectors()
        out = await runner.run_cross(s, db, {"vinted": p}, None, iid, "vinted", dry=False)
        fake.stop()
        await b.close()
        return out
    out = run(go())
    row = db.listing(iid, "vinted")
    assert out.status == "posted" and row["status"] == "posted" and row["url"] == "https://www.vinted.com/items/9001"
    assert row["listing_id"] == "9001" and row["attempts"] == 1 and json.loads(row["fields_json"])["category_id"] == 2955


# ---------------------------------------------------------------- the settings, the daily window, the stub

def test_the_driver_setting_and_what_the_window_waits_for(tmp_path):
    s = Settings({"machine_role": "prod", "paths": {"db": str(tmp_path / "state.db")},
                  "marketplaces": {"depop": {"enabled": True}, "vinted": {"enabled": True, "driver": "playwright"}}})
    db = DB(s.path("db"))
    assert crosslist.driver(s, "depop") == "extension" and crosslist.driver(s, "vinted") == "playwright"
    assert crosslist.reachable(s, db) == ["vinted"]                      # no extension seen yet: Depop waits
    db.kv_set(crosslist.EXT_LINK, json.dumps({"connected": True}))
    assert crosslist.reachable(s, db) == ["depop", "vinted"]
    s.data["marketplaces"]["vinted"]["driver"] = "api"
    with pytest.raises(ValueError, match="marketplaces.vinted.driver must be one of extension, playwright"):
        crosslist.driver(s, "vinted")


def test_alert_lines_read_the_reason_when_no_page_is_given():
    assert crosslist.alert_kind("Depop: not logged in in the poster profile") == "login"
    assert crosslist.alert_kind("Vinted: a verification (first listing / account check) is asked") == "verify"
    assert crosslist.alert_kind("Depop: the site turned the poster's browser away (HTTP 403: …)") == "block"
    assert crosslist.alert_kind("Vinted: a CAPTCHA is shown") == "captcha"
    assert crosslist.alert_kind("anything", "block") == "block"


def test_the_depop_api_stub_builds_the_request_and_sends_nothing(monkeypatch):
    req = product_request(depop_fields(), "i_1", ["https://blob.example/1.jpg"])
    body = req["body"]
    assert req["method"] == "PUT" and req["path"] == "/api/v1/products/i_1"
    assert body["department"] == "womenswear" and body["group"] == "footwear" and body["product_type"]
    assert body["size"] == "US 7.5" and body["pictures"] == [{"url": "https://blob.example/1.jpg"}]
    assert body["price"] == {"amount": "85.00", "currency": "USD"}
    p = DepopApiPoster()
    assert p.available() is False and p.driver == "api"
    monkeypatch.delenv("DEPOP_API_KEY", raising=False)
    with pytest.raises(PosterError, match="DEPOP_API_KEY isn't set"):
        run(p.post(None, RENDER, "publish", False, Path(".")))
    monkeypatch.setenv("DEPOP_API_KEY", "k")
    with pytest.raises(PosterError, match="nothing was sent"):
        run(p.post(None, RENDER, "publish", False, Path(".")))


def test_a_catalog_refresh_never_takes_the_poster_profile_to_an_extension_site(tmp_path, monkeypatch):
    s = Settings({"marketplaces": {"depop": {"enabled": True, "driver": "playwright"}, "vinted": {"enabled": True}}})
    ran, said = [], []

    async def refresh_run(s_, db, ctx, mps):
        ran.append(mps)
    monkeypatch.setattr(runner.refresh, "run", refresh_run)
    monkeypatch.setattr(notify, "say", said.append)
    run(runner._refresh(s, None, None, ["depop", "vinted"]))
    assert ran == [["depop"]] and "not for Vinted" in said[0]


# ---------------------------------------------------------------- the files

def test_the_manifest_asks_for_what_wo32_lists_and_the_capture():
    m = json.loads((bm.EXT_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert m["manifest_version"] == 3 and m["background"] == {"service_worker": "background.js"}
    assert m["permissions"] == ["storage", "alarms", "tabs", "scripting"]
    # "<all_urls>" only because chrome.tabs.captureVisibleTab requires it (the owner's choice, WO32): the content
    # scripts still run on the two sites only.
    assert m["host_permissions"] == ["https://www.vinted.com/*", "https://www.depop.com/*", "http://127.0.0.1:8765/*",
                                     "<all_urls>"]
    assert [cs["matches"] for cs in m["content_scripts"]] == [["https://www.vinted.com/*"], ["https://www.depop.com/*"]]
    assert not {"optional_permissions", "externally_connectable", "web_accessible_resources"} & set(m)


def test_the_extension_is_plain_js_without_third_party_code():
    files = sorted(str(p.relative_to(bm.EXT_DIR)).replace("\\", "/") for p in bm.EXT_DIR.rglob("*") if p.is_file())
    assert files == sorted(bm.EXT_FILES)                                   # nothing else shipped, no build output
    background = (bm.EXT_DIR / "background.js").read_text(encoding="utf-8")
    listed = re.search(r"const FILES = \[(.*?)\];", background, re.S).group(1)
    assert re.findall(r'"([^"]+)"', listed) == list(bm.EXT_FILES)          # the reload hash: the same files, same order
    for name in bm.EXT_FILES:
        text = (bm.EXT_DIR / name).read_text(encoding="utf-8")
        assert "import " not in text.split("\n", 1)[0] and "chrome.debugger" not in text and "eval(" not in text
    assert 'ws://127.0.0.1:8765/ext' in background and "captureVisibleTab" in background
    assert len(bm.files_hash()) == 64


def test_the_thrift_chrome_launch_agent():
    plist = plistlib.loads((ROOT / "deploy" / "com.thrift.chrome-cross.plist").read_bytes())
    assert plist["Label"] == "com.thrift.chrome-cross" and plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["ProgramArguments"] == [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--user-data-dir=__HOME__/thrift/chrome-cross",
        "--no-first-run", "--no-default-browser-check", "--restore-last-session",
        "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding", "https://www.vinted.com/"]
    joined = " ".join(plist["ProgramArguments"])
    for forbidden in ("--remote-debugging", "--enable-automation", "--load-extension", "--headless"):
        assert forbidden not in joined
    services = (ROOT / "deploy" / "services.sh").read_text(encoding="utf-8")
    assert 'echo "com.thrift.chrome-cross"' in services and "for svc in worker poster chrome" in services
    setup = (ROOT / "deploy" / "mac_setup.sh").read_text(encoding="utf-8")
    assert 'if [ ! -s "$TOKEN_FILE" ]' in setup and 'chmod 600 "$TOKEN_FILE"' in setup
    assert "com.thrift.chrome-cross.plist" in setup
    assert "services.sh start chrome" in (ROOT / "deploy" / "mac_deploy.sh").read_text(encoding="utf-8")


def test_verified_steps_carry_their_evidence_and_the_publish_gate_stays_closed():
    """A step is marked verified only with what the live run showed ("seen"); the publish steps (the button, the page
    after it) stay UNVERIFIED until a run proves them — no publish before that (WO32 §1)."""
    data = ext_driver.selectors()
    for site in ("vinted", "depop"):
        for name, step in [*data[site]["steps"].items(), *data[site]["pages"].items()]:
            if step.get("verified"):
                assert step.get("seen"), f"{site} {name}: verified without its evidence"
        # The live dry runs (2026-10-06) recorded the forms and their one publish button: the owner's supervised publish
        # may go; the unattended loop waits for a real publish to record the page after the click — Depop's first
        # supervised publish recorded it (2026-10-07, WO32b), Vinted's too (2026-10-08, WO33: the member page and its
        # "Item listed" dialog, Later pressed).
        assert not (ext_driver.PUBLISH_NEEDS & ext_driver.unverified(site))
        assert not (ext_driver.AUTOPUBLISH_NEEDS & ext_driver.unverified(site))
        assert data[site]["pages"]["login"]["verified"]
