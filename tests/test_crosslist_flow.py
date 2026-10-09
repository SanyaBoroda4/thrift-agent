"""WO30 cross-listing through the poster loop, with stub posters (no browser, no network, no model): Poshmark → Depop
→ Vinted per item, 30–90 s apart; ONE "Posted ✓" line per item naming every marketplace; "All done" only when nothing
is left on any of them; a dry run's screenshot and fields to the ops chat; a logged-out marketplace stopped for the
window with one plain line; failures retried next window, 3 attempts; the daily cap; the backfill.

WO32: every test of the loop runs with both drivers — the Playwright-style stubs, and the extension driver (the real
ExtensionPoster and bridge, with a scripted stand-in for the Chrome extension answering the same results)."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from ext_fake import FakeExtension

from thrift_agent import approve, crosslist, daily, notify, pipeline
from thrift_agent.bridge import EXT_DIR
from thrift_agent.catalogs.common import MappingError
from thrift_agent.config import Settings
from thrift_agent.db import ANNOUNCED_MIGRATED, DB
from thrift_agent.post import runner
from thrift_agent.post.base import AccountBlocked, Outcome
from thrift_agent.post.ext_driver import ExtensionPoster
from thrift_agent.schema import Render

TITLE = "Tory Burch Red Ballet Flats size 7.5"
RENDER = Render(marketplace="poshmark", title=TITLE, description="Red flats.", tags=[], brand="Tory Burch",
                department="Women", category="Shoes", subcategory="Flats & Loafers", size="7.5", colors=["Red"],
                condition="good", price=85, photos=[], sku="i_1")


def _settings(tmp_path, depop_live=True, vinted_live=True, cap=25, parallel=False) -> Settings:
    paths = {k: str(tmp_path / k) for k in ("inbox", "work", "archive", "failed", "chrome_profile", "control")}
    s = Settings({
        "machine_role": "prod",
        "paths": {**paths, "db": str(tmp_path / "state.db")},
        "poster": {"dry_run": False, "autopublish_confirmed": True, "max_consecutive_failures": 3, "parallel": parallel},
        "schedule": {"timezone": "America/New_York", "hours": ["00:00", "23:59"], "per_hour_max": 10, "daily_cap": 25,
                     "gap_seconds": [150, 420]},
        "marketplaces": {"poshmark": {"enabled": True, "username": "closet", "autopublish": True, "max_photos": 16},
                         "depop": {"enabled": True, "autopublish": depop_live, "daily_cap": cap},
                         "vinted": {"enabled": True, "autopublish": vinted_live, "daily_cap": cap}},
        "crosslist": {"gap_seconds": [30, 90]},
        "models": {},
        "bridge": {"token_file": str(tmp_path / "ext_token"), "port": 0},      # WO32: the extension driver's bridge
    })
    (tmp_path / "ext_token").write_text("flow-test-token-" + "x" * 24, encoding="utf-8")
    s.ensure_dirs()
    return s


def _item(db: DB, seq: int = 1) -> str:
    bid = db.add_batch(f"share_{seq}", 3)
    db.set_batch(bid, status="split")
    iid = db.add_item(bid, seq, f"work/{seq}")
    db.set_item(iid, status="ready", owner_price=85, gate={"decision": "publish", "reasons": []},
                renders={"poshmark": RENDER.model_copy(update={"sku": iid}).model_dump()})
    return iid


class Stub:
    """A poster: each post() returns the next Outcome (an Exception instance is raised)."""

    def __init__(self, name, *results):
        self.name, self.results, self.calls = name, list(results), []
        self.fields = self.confirm = None
        self.strict = False

    async def post(self, ctx, r, mode, dry_run, shots, stage="form"):
        self.calls.append((r.sku, dry_run))
        res = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(res, Exception):
            raise res
        url = (res.url or "").replace("{iid}", r.sku).replace("{slug}", r.sku.replace("_", "-"))
        res = Outcome(res.status, url=url or None, error=res.error,
                      clicked=res.clicked, guesses=list(res.guesses))
        shots.mkdir(parents=True, exist_ok=True)
        res.screenshot = str(shots / f"{r.sku}-{self.name}.png")
        Path(res.screenshot).write_bytes(b"png")
        return res

    async def find_live(self, ctx, r, since, created=None):
        return None, {}

    def listing_address(self, url):
        return url


class Fields(SimpleNamespace):
    def model_dump(self):
        return dict(self.__dict__)


def fields_for(mp):
    return Fields(price=85, photos=["/w/cover.jpg"], description=f"{TITLE}\n\nRed flats.", size="US 7.5",
                  condition="Used - Good" if mp == "depop" else "Good", colors=["Red"], guesses=[],
                  category="Women > Footwear > Ballet shoes" if mp == "depop" else None,
                  category_path="Women > Shoes > Ballerinas" if mp == "vinted" else None,
                  title=TITLE if mp == "vinted" else None, hashtags=[], brand="Tory Burch",
                  category_id=2955 if mp == "vinted" else None, materials=[], skirt_length=None,
                  package_sizes=["SMALL", "MEDIUM"], source=[], age=None, style=[], attributes={},
                  shipping="Depop Shipping", package_size="Small")


class ExtStub(ExtensionPoster):
    """WO32: the extension driver itself — ExtensionPoster, its job, the bridge — with a stand-in for the Chrome extension
    that answers each job from the WO30 stub's results (its calls recorded on the stub, so the tests read them as
    before). Addresses are the stubs' own (the tests' URLs are placeholders)."""

    def __init__(self, stub, selectors_path):
        self._bridge_obj, self.stub, self.fake = None, stub, None
        super().__init__(stub.name)
        self.selectors_path = selectors_path         # the publish steps recorded (verified) for these tests

    @property
    def bridge(self):
        return self._bridge_obj

    @bridge.setter
    def bridge(self, b):
        self._bridge_obj = b
        if b is not None:                         # open_bridge attaches it: the ONE "extension" serves both sites
            if not hasattr(b, "fake"):
                b.fake = FakeExtension(b.port, b.token)
                b.fake.start()
            b.fake.sites[self.name] = self.stub
            self.fake = b.fake

    def available(self) -> bool:
        return True

    def listing_address(self, url):
        return url or None

    @property
    def calls(self):
        return self.stub.calls


@pytest.fixture(params=["playwright", "extension"])
def driver(request):
    return request.param


@pytest.fixture
def verified_selectors(tmp_path):
    """ext/selectors.json with every step recorded: the publish gate open, as after the Mac's first dry run."""
    data = json.loads((EXT_DIR / "selectors.json").read_text(encoding="utf-8"))
    for site in ("vinted", "depop"):
        for step in data[site]["steps"].values():
            step["verified"] = True
    path = tmp_path / "selectors-verified.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def loop(monkeypatch, driver, verified_selectors):
    """The poster loop with its browser, pacing and Telegram stubbed. Returns (said, run). With the extension driver,
    Depop's and Vinted's stubs answer through ExtensionPoster, the bridge and a stand-in extension (WO32)."""
    said = SimpleNamespace(group=[], ops=[], pauses=[])

    class Ctx:
        async def close(self):
            pass

    class PW:
        async def stop(self):
            pass

    async def browser(profile, tz):
        return PW(), Ctx()

    async def pause(stop, seconds):
        said.pauses.append(seconds)
        if seconds == 60 or len(said.pauses) > 40:
            stop.set()

    monkeypatch.setattr(runner, "open_browser", browser)
    monkeypatch.setattr(runner, "start_thrift_chrome", lambda s: "not in a test")   # never the Mac's real Chrome
    monkeypatch.setattr(runner, "_pause", pause)
    monkeypatch.setattr(runner, "_paused", lambda db: None)
    monkeypatch.setattr(runner, "map_fields", lambda mp, view: fields_for(mp))
    monkeypatch.setattr(runner.ItemView, "from_row", classmethod(lambda cls, it: SimpleNamespace(
        render=SimpleNamespace(title=TITLE))))
    monkeypatch.setattr(notify, "group", lambda text: said.group.append(text))
    monkeypatch.setattr(notify, "group_photo", lambda path, caption: said.group.append(caption))
    monkeypatch.setattr(notify, "say", lambda text: said.ops.append(text))
    monkeypatch.setattr(notify, "photo", lambda path, caption: said.ops.append(caption))
    monkeypatch.setattr(notify, "ops_photo", lambda path, caption: said.ops.append(caption))
    monkeypatch.setattr("thrift_agent.catalogs.refresh.due", lambda s, db, mp, now=None: False)

    def run(s, db, posters):
        if driver == "extension":
            posters = {mp: ExtStub(p, verified_selectors) if mp in ("depop", "vinted") and isinstance(p, Stub) else p
                       for mp, p in posters.items()}
        monkeypatch.setattr(runner, "posters", lambda s_: posters)
        asyncio.run(runner.run(s, db))
    return said, run


def _posters(posh=None, depop=None, vinted=None):
    return {"poshmark": posh or Stub("poshmark", Outcome("posted", url="https://poshmark.com/listing/{iid}")),
            "depop": depop or Stub("depop", Outcome("posted", url="https://www.depop.com/products/shop-{slug}/")),
            "vinted": vinted or Stub("vinted", Outcome("posted", url="https://www.vinted.com/items/{iid}"))}


def test_one_item_on_three_marketplaces_one_line(tmp_path, loop):
    said, run = loop
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    ps = _posters()
    run(s, db, ps)
    assert [len(p.calls) for p in ps.values()] == [1, 1, 1]
    assert said.group == [f"Posted ✓ {TITLE} — $85 · Poshmark https://poshmark.com/listing/{iid} · Depop "
                          f"https://www.depop.com/products/shop-{iid.replace('_', '-')}/ · Vinted https://www.vinted.com/items/{iid}\n"
                          "✓ All done — safe to close the Mac."]
    assert all(30 <= p <= 90 for p in said.pauses[:2]) and said.pauses[2] >= 150      # then the human gap
    rows = {r["marketplace"]: r for r in db.listings_for(iid)}
    assert [rows[m]["status"] for m in ("poshmark", "depop", "vinted")] == ["posted"] * 3
    assert rows["depop"]["listing_id"] == f"shop-{iid.replace('_', '-')}" and rows["vinted"]["price"] == 85
    assert json.loads(rows["vinted"]["fields_json"])["condition"] == "Good"
    assert db.item(iid)["status"] == "posted"


def test_dry_run_marketplaces_send_their_screenshot_to_the_ops_chat(tmp_path, loop):
    said, run = loop
    s = _settings(tmp_path, depop_live=False, vinted_live=False)
    db = DB(s.path("db"))
    iid = _item(db)
    ps = _posters(depop=Stub("depop", Outcome("dryrun")), vinted=Stub("vinted", Outcome("dryrun")))
    run(s, db, ps)
    assert ps["depop"].calls == [(iid, True)] and ps["vinted"].calls == [(iid, True)]   # dry: never published
    assert said.group == [f"Posted ✓ {TITLE} — $85 · Poshmark https://poshmark.com/listing/{iid}\n"
                          "✓ All done — safe to close the Mac."]
    dry = [m for m in said.ops if m.startswith("🧪 dry-run")]
    assert len(dry) == 2 and "condition: Used - Good" in dry[0] and "category: Women > Footwear" in dry[0]
    assert [r["status"] for r in db.listings_for(iid)] == ["posted", "dryrun", "dryrun"]


def test_two_items_settled_together_all_done_on_the_last_line_only(tmp_path, loop):
    """WO33, seen on a loaded CI runner: both items finished every site before either line went out — each line saw
    nothing left to do, and both said "All done". While another item's line is due, a line says nothing of the kind."""
    said, run = loop
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    a, b = _item(db, 1), _item(db, 2)
    for iid in (a, b):
        for mp, url in (("poshmark", f"https://poshmark.com/listing/{iid}"),
                        ("depop", f"https://www.depop.com/products/shop-{iid.replace('_', '-')}/"),
                        ("vinted", f"https://www.vinted.com/items/{iid}")):
            db.upsert_listing(iid, mp, status="posted", url=url, posted_at="2026-10-08T20:00:00+00:00")
        db.set_item(iid, status="posted")
    assert crosslist.announce(s, db, a) and crosslist.announce(s, db, b)
    assert len(said.group) == 2 and "All done" not in said.group[0]
    assert said.group[1].endswith("✓ All done — safe to close the Mac.") and f"/listing/{b}" in said.group[1]


def test_all_done_only_when_no_marketplace_has_anything_left(tmp_path, loop):
    said, run = loop
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    a, b = _item(db, 1), _item(db, 2)
    run(s, db, _posters())
    assert len(said.group) == 2
    assert "All done" not in said.group[0] and said.group[0].startswith(f"Posted ✓ {TITLE} — $85 · Poshmark ")
    assert said.group[1].endswith("✓ All done — safe to close the Mac.") and f"/listing/{b}" in said.group[1]
    assert f"/listing/{a}" in said.group[0]


def test_a_value_that_cant_be_mapped_skips_only_that_marketplace(tmp_path, loop, monkeypatch):
    said, run = loop
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)

    def mapping(mp, view):
        if mp == "depop":
            raise MappingError("size 'XL' isn't on Depop's Kids > Clothing > US sizes")
        return fields_for(mp)
    monkeypatch.setattr(runner, "map_fields", mapping)
    ps = _posters()
    run(s, db, ps)
    assert ps["depop"].calls == [] and db.listing(iid, "depop")["status"] == "skipped"
    assert said.group[0].startswith(f"Posted ✓ {TITLE} — $85 · Poshmark https://poshmark.com/listing/{iid} · Vinted ")
    assert any(m.startswith(f"⏭ Depop skipped ({iid})") and "can't map" in m for m in said.ops)


def test_a_logged_out_marketplace_stops_for_the_window_with_one_plain_line(tmp_path, loop):
    said, run = loop
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    a, b = _item(db, 1), _item(db, 2)
    ps = _posters(depop=Stub("depop", AccountBlocked("Depop: not logged in in the poster profile")))
    run(s, db, ps)
    assert said.group.count("Depop needs you to log in on the Mac.") == 1
    assert len(ps["depop"].calls) == 1                                     # not tried again this window
    assert [db.listing(i, "depop")["status"] for i in (a, b)] == ["queued", "queued"]
    assert all(" · Vinted " in line for line in said.group if line.startswith("Posted ✓"))
    assert crosslist.blocked(db, "depop")
    db.kv_set(daily.SESSION_KEY, "next-window")                           # the Mac opened again
    ps["depop"] = Stub("depop", Outcome("posted", url="https://www.depop.com/products/shop-{slug}/"))
    run(s, db, ps)
    assert [db.listing(i, "depop")["status"] for i in (a, b)] == ["posted", "posted"]
    assert not crosslist.blocked(db, "depop")


def test_a_failure_before_publishing_is_retried_next_window_three_times(tmp_path, loop):
    said, run = loop
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    ps = _posters(vinted=Stub("vinted", Outcome("failed", error="PosterError: no row 'Ballerinas'")))
    for window in ("w1", "w2", "w3"):
        db.kv_set(daily.SESSION_KEY, window)
        run(s, db, ps)
    assert len(ps["vinted"].calls) == 3 and db.listing(iid, "vinted")["status"] == "failed"
    db.kv_set(daily.SESSION_KEY, "w4")
    run(s, db, ps)
    row = db.listing(iid, "vinted")
    assert len(ps["vinted"].calls) == 3 and row["status"] == "skipped" and "3 attempts" in row["error"]
    assert any(m.startswith(f"⏭ Vinted skipped for {iid} after 3 attempts") for m in said.ops)
    told = [m for m in said.group if "Vinted" in m and not m.startswith("Posted ✓")]   # WO33: one line, a reply
    assert len(told) == 1 and told[0].startswith("⏭ ") and "Reply 'retry'" in told[0]  # settles it, never a command
    assert not any("thrift " in m for m in [*said.group, *said.ops])


def test_an_unconfirmed_publish_is_asked_and_never_published_again(tmp_path, loop, monkeypatch):
    said, run = loop
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    asked = []
    monkeypatch.setattr(runner, "ask_unconfirmed", lambda db_, i, mp, text: asked.append((i, mp, text)))
    ps = _posters(depop=Stub("depop", Outcome("failed", clicked=True, error="PosterError: no product address")))
    run(s, db, ps)
    assert asked == [(iid, "depop", f"⚠️ {TITLE}: I pressed Post on Depop but can't see it in the shop. Check Depop: "
                                    "if it's there, reply 'posted <url>'; if not, reply 'retry'.")]
    db.kv_set(daily.SESSION_KEY, "next")
    run(s, db, ps)
    assert len(ps["depop"].calls) == 1                                    # never retried blind (invariant 4)
    assert db.listing(iid, "depop")["error"].startswith("unconfirmed publish: ")


def test_the_daily_cap_is_per_marketplace(tmp_path, loop):
    said, run = loop
    s = _settings(tmp_path, cap=1)
    db = DB(s.path("db"))
    _a, b = _item(db, 1), _item(db, 2)
    ps = _posters()
    run(s, db, ps)
    assert [len(p.calls) for p in ps.values()] == [2, 1, 1]               # Poshmark has its own cap
    assert db.listing(b, "depop")["status"] == "queued" and said.group[-1].endswith("safe to close the Mac.")


def test_the_unconfirmed_reply_names_its_marketplace(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    db.upsert_listing(iid, "poshmark", status="posted", url="https://poshmark.com/listing/x")
    db.upsert_listing(iid, "depop", status="failed", error="unconfirmed publish: no product address")
    db.add_outbox("100", 77, "unconfirmed", crosslist.unconfirmed_ref(iid, "depop"))
    seen = []
    monkeypatch.setattr(pipeline, "request_posted", lambda s_, db_, i, url, mp="poshmark": seen.append((i, url, mp))
                        or url)
    bot = SimpleNamespace(chat_id="100", calls=[], call=lambda m, **p: True, send_message=lambda *a, **k: 1)
    monkeypatch.setattr(approve, "_ack", lambda *a, **k: None)
    out = approve._reply_unconfirmed(s, db, bot, f"{iid}:depop", "posted https://www.depop.com/products/shop-x/", 77)
    assert seen == [(iid, "https://www.depop.com/products/shop-x/", "depop")] and "queued" in out
    assert pipeline.listing_address("depop", "https://www.depop.com/products/shop-x") == \
        "https://www.depop.com/products/shop-x/"
    assert pipeline.listing_address("vinted", "https://www.vinted.com/items/123-tee?ref=1") == \
        "https://www.vinted.com/items/123"
    pipeline.retry_unconfirmed(s, db, iid, "depop")
    assert db.listing(iid, "depop")["status"] == "queued" and db.item(iid)["status"] == "ready"   # item unchanged


def test_backfill_queues_what_is_still_live_oldest_first(tmp_path):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    old, sold, odd, new = (_item(db, i) for i in range(1, 5))
    for n, iid in enumerate((old, sold, odd, new)):
        db.upsert_listing(iid, "poshmark", status="posted", url=f"https://poshmark.com/listing/x-{iid}",
                          posted_at=f"2026-10-0{n + 1}T10:00:00+00:00")
    db.upsert_listing(new, "depop", status="posted", url="https://www.depop.com/products/a/")
    states = {old: True, sold: False, odd: None, new: True}
    live = lambda url: states[url.rsplit("-", 1)[-1]]   # noqa: E731
    dry = crosslist.backfill(s, db, ["depop", "vinted"], dry=True, live=live)
    assert dry == [(old, "would queue depop, vinted"), (sold, "no longer for sale on Poshmark: left out"),
                   (odd, "couldn't read its Poshmark page: left out"), (new, "would queue vinted")]
    assert db.listing(old, "depop") is None                              # --dry-run writes nothing
    done = crosslist.backfill(s, db, ["depop", "vinted"], live=live)
    assert [v for _, v in done] == ["queued depop, vinted", "no longer for sale on Poshmark: left out",
                                    "couldn't read its Poshmark page: left out", "queued vinted"]
    assert crosslist.next_job(s, db) == (old, "depop")


def test_backfill_takes_todays_poshmark_price_and_tells_the_ops_chat(tmp_path, monkeypatch):
    """WO33, the owner: some prices were changed on Poshmark by hand — the backfill lists at each item's price there
    TODAY: a price that differs becomes the item's (Depop and Vinted get it), one ops message lists the differences;
    the dry run changes nothing and only says so."""
    said = []
    monkeypatch.setattr(notify, "say", lambda text: said.append(text))
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    changed, same = _item(db, 1), _item(db, 2)                               # both approved at $85
    for n, iid in enumerate((changed, same)):
        db.upsert_listing(iid, "poshmark", status="posted", url=f"https://poshmark.com/listing/x-{iid}", price=85,
                          posted_at=f"2026-10-0{n + 1}T10:00:00+00:00")
    prices = {changed: 65, same: 85}
    page = lambda url: (True, prices[url.rsplit("-", 1)[-1]])               # noqa: E731
    dry = crosslist.backfill(s, db, ["depop", "vinted"], dry=True, page=page)
    assert dry == [(changed, "would queue depop, vinted — Poshmark price $65 (ours $85)"),
                   (same, "would queue depop, vinted")]
    assert db.item(changed)["owner_price"] == 85 and not said                # the dry run changes nothing
    done = crosslist.backfill(s, db, ["depop", "vinted"], page=page)
    assert done[0] == (changed, "queued depop, vinted — Poshmark price $65 (ours $85)")
    assert db.item(changed)["owner_price"] == 65 and db.listing(changed, "poshmark")["price"] == 65
    assert db.item(same)["owner_price"] == 85
    assert len(said) == 1 and said[0].startswith("💲 Backfill at today's Poshmark prices — 1 differ")
    assert f"{TITLE}: $85 → $65" in said[0]                                 # owner_price is what Depop and Vinted get


def test_poshmark_pages_are_read_with_a_classic_tls_handshake(monkeypatch):
    """WO33, live: Poshmark's CloudFront answered 403 to the Mac's default OpenSSL 3.5 handshake for every public page;
    the reads use a context limited to P-256 — and still read the page: for sale + price, sold, gone, blocked."""
    import ssl
    from types import SimpleNamespace as NS

    import httpx

    seen = []
    state = '<script>window.__INITIAL_STATE__ = {"$_listing_details": {"listingDetails": %s}};</script>'
    pages = {"live": NS(status_code=200, text=state % '{"inventory": {"status": "available"}, '
                                                       '"price_amount": {"val": "45.00"}}'),
             "sold": NS(status_code=200, text=state % '{"inventory": {"status": "sold_out"}}'),
             "gone": NS(status_code=404, text=""), "blocked": NS(status_code=403, text="Request blocked")}

    def get(url, **kw):
        seen.append(kw.get("verify"))
        return pages[url.rsplit("/", 1)[-1]]
    monkeypatch.setattr(httpx, "get", get)
    assert crosslist.poshmark_page("https://poshmark.com/listing/live") == (True, 45)
    assert crosslist.poshmark_page("https://poshmark.com/listing/sold") == (False, None)
    assert crosslist.poshmark_page("https://poshmark.com/listing/gone") == (False, None)
    assert crosslist.poshmark_page("https://poshmark.com/listing/blocked") == (None, None)   # unknown, never a guess
    assert all(isinstance(v, ssl.SSLContext) for v in seen) and len(seen) == 4


def test_a_new_window_lifts_blocks_and_never_retries_an_unconfirmed_publish(tmp_path):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    db.upsert_listing(iid, "depop", status="failed", error="unconfirmed publish: x", attempts=1)
    db.upsert_listing(iid, "vinted", status="failed", error="PosterError: y", attempts=1)
    db.kv_set(daily.SESSION_KEY, "w1")
    assert crosslist.new_window(s, db) is True and crosslist.new_window(s, db) is False
    assert db.listing(iid, "depop")["status"] == "failed" and db.listing(iid, "vinted")["status"] == "queued"
    crosslist.block(db, "vinted", "Vinted: a verification (first listing / account check) is asked")
    assert crosslist.blocked(db, "vinted") and crosslist.pending(s, db) == []
    db.kv_set(daily.SESSION_KEY, "w2")
    assert crosslist.new_window(s, db) and not crosslist.blocked(db, "vinted")
    assert crosslist.pending(s, db) == [(iid, "vinted")]


def test_an_asked_for_dry_run_never_stops_the_poster(tmp_path, loop, monkeypatch):
    """Live on the Mac (WO30 deploy): Depop logged out in the poster's profile raised AccountBlocked out of the asked-for
    dry run and the poster process ended. Now: Depop stopped for the window with the group's one plain line, Vinted
    still filled, the poster goes on."""
    said, run = loop
    s = _settings(tmp_path, depop_live=False, vinted_live=False)
    db = DB(s.path("db"))
    iid = _item(db)
    db.upsert_listing(iid, "poshmark", status="posted", url="https://poshmark.com/listing/x")
    db.set_item(iid, status="posted")
    runner.request_dry_run(db, iid, ["depop", "vinted"])
    ps = _posters(depop=Stub("depop", AccountBlocked("Depop: not logged in in the poster profile")),
                  vinted=Stub("vinted", Outcome("dryrun")))
    run(s, db, ps)
    assert said.group == ["Depop needs you to log in on the Mac."]
    assert ps["vinted"].calls == [(iid, True)] and any(m.startswith("🧪 dry-run Vinted") for m in said.ops)
    assert db.listing(iid, "depop") is None and db.listing(iid, "vinted") is None      # no row taken or changed
    kinds = [json.loads(d)["status"] for (d,) in db.conn.execute(
        "SELECT detail FROM events WHERE kind='crosslist_dry_run' ORDER BY rowid")]
    assert kinds == ["blocked", "dryrun"]


# ---------------------------------------------------------------- WO32b: the supervised publish, one group line per item

DEPOP_URL = "https://www.depop.com/products/shopname-tory-burch-red-ballet-7f3a/"


@pytest.fixture
def quiet(monkeypatch):
    """The group's and the ops chat's messages, collected."""
    said = SimpleNamespace(group=[], ops=[])
    monkeypatch.setattr(notify, "group", lambda text: said.group.append(text))
    monkeypatch.setattr(notify, "group_photo", lambda path, caption: said.group.append(caption))
    monkeypatch.setattr(notify, "say", lambda text: said.ops.append(text))
    monkeypatch.setattr(notify, "photo", lambda path, caption: said.ops.append(caption))
    monkeypatch.setattr(notify, "ops_photo", lambda path, caption: said.ops.append(caption))
    monkeypatch.setattr(runner, "ask_unconfirmed", lambda db_, i, mp, text: said.group.append(text))
    return said


def _announced_item(tmp_path) -> tuple[Settings, DB, str]:
    """An item live on Poshmark for days, its "Posted ✓" said back then."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    db.upsert_listing(iid, "poshmark", status="posted", url="https://poshmark.com/listing/x-" + "a" * 24,
                      posted_at="2026-10-05T14:00:00+00:00")
    db.set_item(iid, status="posted")
    db.log(iid, "posted_announced", {"mps": ["poshmark"]})
    return s, db, iid


def _supervised(monkeypatch, s, db, iid, stub, confirm, verified_selectors):
    poster = ExtStub(stub, verified_selectors)
    monkeypatch.setattr(runner, "posters", lambda s_: {"depop": poster})
    monkeypatch.setattr(runner, "map_fields", lambda mp, view: fields_for(mp))
    monkeypatch.setattr(runner.ItemView, "from_row", classmethod(lambda cls, it: SimpleNamespace(
        render=SimpleNamespace(title=TITLE))))
    return poster, asyncio.run(runner.publish_first_cross(s, db, iid, "depop", confirm))


def test_a_supervised_add_to_an_announced_item_is_one_ops_line(tmp_path, monkeypatch, quiet, verified_selectors,
                                                                capsys):
    """WO32b §7: the group heard about the item once; Depop added later goes to the ops chat only."""
    s, db, iid = _announced_item(tmp_path)

    async def post(fields, site):
        return True
    poster, out = _supervised(monkeypatch, s, db, iid, Stub("depop", Outcome("posted", url=DEPOP_URL)), post,
                              verified_selectors)
    assert out.status == "posted" and poster.fake.clicks == 1
    assert db.listing(iid, "depop")["status"] == "posted" and db.listing(iid, "depop")["url"] == DEPOP_URL
    assert quiet.group == []
    assert [m for m in quiet.ops if m.startswith("Added:")] == [f"Added: {TITLE} · Depop {DEPOP_URL}"]
    printed = capsys.readouterr().out                                   # the CLI's progress, line by line
    for line in ("extension connected", "tab opened", "photos 1/1", "category ✓", "filled in 1.2 s",
                 "go-ahead sent: one click"):
        assert line in printed, (line, printed)


def test_ctrl_c_before_post_leaves_the_row_queued_and_clicks_nothing(tmp_path, monkeypatch, quiet,
                                                                     verified_selectors):
    """WO32b §4: before POST nothing can be published — a Ctrl+C cancels the job (the extension closes the tab) and
    the row stays 'queued', never 'posting'."""
    s, db, iid = _announced_item(tmp_path)
    box = {}

    async def ctrl_c(fields, site):
        box["status"] = db.listing(iid, "depop")["status"]           # what the row is while the terminal waits
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        _supervised(monkeypatch, s, db, iid, Stub("depop", Outcome("posted", url=DEPOP_URL)), ctrl_c,
                    verified_selectors)
    row = db.listing(iid, "depop")
    assert box["status"] == "queued" and row["status"] == "queued" and row["url"] is None and row["attempts"] == 1
    assert [e["kind"] for e in db.conn.execute("SELECT kind FROM events WHERE kind='publish_cancelled'")] == \
        ["publish_cancelled"]
    assert quiet.group == [] and runner.publish_first_cross is not None
    # and it can be run again: nothing is 'posting', nothing to reconcile
    assert db.claim_listing(iid, "depop", count=False)


def test_a_posted_reply_marks_the_depop_row_posted_with_its_address(tmp_path, monkeypatch, quiet):
    """WO32b: the owner's 'posted <url>' — Depop's /manage/ address as the tab showed it — checked on the listing page in
    the Thrift Chrome (its title and price), then the row posted with the listing's own address; the item was
    announced already, so one ops line."""
    s, db, iid = _announced_item(tmp_path)
    db.upsert_listing(iid, "depop", status="failed",
                      error="unconfirmed publish: after the click no listing page (unknown page: not a listing page)")
    address = pipeline.request_posted(s, db, iid, DEPOP_URL + "manage/", "depop")
    assert address == DEPOP_URL

    async def go():
        from thrift_agent.bridge import Bridge
        b = await Bridge("flow-test-token-" + "x" * 24, port=0).start()
        fake = FakeExtension(b.port, b.token, [SimpleNamespace(status="ok", title=TITLE, price="$85.00",
                                                               shop="shopname", body=f"{TITLE}\n$85.00\nSize US 7.5")])
        fake.start()
        await asyncio.wait_for(fake.connected.wait(), 5)
        poster = ExtensionPoster("depop", bridge=b)
        try:
            return await runner.serve_requests(s, db, {"depop": poster}, None), fake, poster
        finally:
            fake.stop()
            await b.close()
    done, fake, poster = asyncio.run(go())
    row = db.listing(iid, "depop")
    assert done == [iid] and row["status"] == "posted" and row["url"] == DEPOP_URL
    assert row["listing_id"] == "shopname-tory-burch-red-ballet-7f3a" and row["error"] is None
    assert fake.jobs[-1]["mode"] == "verify" and fake.jobs[-1]["listing_url"] == DEPOP_URL
    assert poster.learned_shop == "shopname"
    assert quiet.group == [] and f"Added: {TITLE} · Depop {DEPOP_URL}" in quiet.ops


def test_a_new_item_s_line_waits_for_every_site_to_settle_then_goes_once(tmp_path, quiet):
    """WO33 A3: the item's one "Posted ✓" line goes out once every enabled site has had its say — posted, failed,
    skipped, or waiting on the owner's answer to the ⚠️ question (that site is left out of the line); a site still in
    progress holds it. Never a second line: a site confirmed later is an ops "Added:" line."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    db.upsert_listing(iid, "poshmark", status="posted", url="https://poshmark.com/listing/x-" + "b" * 24)
    db.upsert_listing(iid, "vinted", status="posting")
    db.upsert_listing(iid, "depop", status="failed", error="unconfirmed publish: no listing page")
    assert crosslist.announce(s, db, iid) is None and quiet.group == []           # Vinted still in progress
    db.upsert_listing(iid, "vinted", status="posted", url="https://www.vinted.com/items/77")
    text = crosslist.announce(s, db, iid)
    assert quiet.group == [text] and text.startswith(f"Posted ✓ {TITLE} — $85 · Poshmark https://poshmark.com/")
    assert "Vinted https://www.vinted.com/items/77" in text and "Depop" not in text
    db.upsert_listing(iid, "depop", status="posted", url=DEPOP_URL, error=None)       # the owner's link, checked
    assert crosslist.announce(s, db, iid) == f"Added: {TITLE} · Depop {DEPOP_URL}" and len(quiet.group) == 1


def test_items_live_before_wo32b_count_as_announced(tmp_path, quiet):
    """The live miss: a Poshmark listing from days before (announced by the old flow, no event) was announced again —
    with no new link — when Depop was added. Such items are marked announced once; a site added later is an ops line."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db)
    db.upsert_listing(iid, "poshmark", status="posted", url="https://poshmark.com/listing/y-" + "c" * 24)
    db.conn.execute("DELETE FROM kv WHERE key=?", (ANNOUNCED_MIGRATED,))       # a database from before WO32b
    db = DB(s.path("db"))
    assert crosslist.announced(db, iid) == {"poshmark"}
    db.upsert_listing(iid, "depop", status="posted", url=DEPOP_URL)
    assert crosslist.announce(s, db, iid) == f"Added: {TITLE} · Depop {DEPOP_URL}"
    assert quiet.group == []
    db = DB(s.path("db"))                                                       # once: nothing more marked
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE kind='announced_migrated'").fetchone()[0] == 1
