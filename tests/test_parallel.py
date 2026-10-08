"""WO33 A1–A3: the three site workers (thrift_agent/post/parallel.py) — Poshmark in its own thread, Depop and Vinted
beside the bridge — with stub posters (no browser, no network): an item takes about its slowest site, not the sum; one
site failing or turned away never stops the others; one group line per item, links in order; "All done" only at the
end; and the publish rules unchanged with the workers concurrent (one click, 'posting' at the go-ahead)."""
import asyncio
import time
from types import SimpleNamespace

import pytest
from ext_fake import FakeExtension

from thrift_agent import crosslist, notify
from thrift_agent.db import DB
from thrift_agent.post import parallel, runner
from thrift_agent.post.base import AccountBlocked, Outcome

from test_crosslist_flow import TITLE, ExtStub, _item, _settings, fields_for


class Timed:
    """A poster that takes `delay` seconds per listing and posts (or raises `fail` for the items in `fail_for`)."""

    def __init__(self, name, delay, driver="stub", fail=None, fail_for=()):
        self.name, self.delay, self.driver = name, delay, driver
        self.fail, self.fail_for = fail, set(fail_for)
        self.calls, self.fields, self.confirm, self.strict = [], None, None, False
        self.on_go_ahead = self.progress = None
        self.lines = []

    async def post(self, ctx, r, mode, dry_run, shots, stage="form"):
        self.calls.append((r.sku, time.monotonic()))
        await asyncio.sleep(self.delay)
        if self.fail is not None and r.sku in self.fail_for:
            raise self.fail
        url = {"poshmark": f"https://poshmark.com/listing/{r.sku}", "depop": f"https://www.depop.com/products/shop-{r.sku}/",
               "vinted": f"https://www.vinted.com/items/{abs(hash(r.sku)) % 10**8}"}[self.name]
        return Outcome("posted", url=url, clicked=True)

    async def find_live(self, ctx, r, since, created=None):
        return None, {}

    def listing_address(self, url):
        return url


class Ctx:
    async def close(self):
        pass


class PW:
    async def stop(self):
        pass


async def fake_open(profile, tz):
    return PW(), Ctx()


@pytest.fixture
def quick(monkeypatch):
    """The workers' pauses near zero; the group, the ops chat and the item views stubbed."""
    said = SimpleNamespace(group=[], ops=[])
    monkeypatch.setattr(parallel, "IDLE", 0.05)
    monkeypatch.setattr(parallel, "HOUSEKEEPING", 0.05)
    monkeypatch.setattr(parallel, "next_gap", lambda schedule: 0.0)
    monkeypatch.setattr(runner, "start_thrift_chrome", lambda s: "not in a test")
    monkeypatch.setattr(runner, "map_fields", lambda mp, view: fields_for(mp))
    monkeypatch.setattr(runner.ItemView, "from_row", classmethod(lambda cls, it: SimpleNamespace(
        render=SimpleNamespace(title=TITLE))))
    monkeypatch.setattr(notify, "group", lambda text: said.group.append(text))
    monkeypatch.setattr(notify, "group_photo", lambda path, caption: said.group.append(caption))
    monkeypatch.setattr(notify, "say", lambda text: said.ops.append(text))
    monkeypatch.setattr(notify, "photo", lambda path, caption: said.ops.append(caption))
    monkeypatch.setattr(notify, "ops_photo", lambda path, caption: said.ops.append(caption))
    monkeypatch.setattr(runner, "_paused", lambda db: None)
    return said


def _run(s, db, ps):
    t0 = time.monotonic()
    asyncio.run(parallel.run_parallel(s, db, ps, dry=False, stage="form", opener=fake_open, until_idle=True))
    return time.monotonic() - t0


def test_an_item_takes_its_slowest_site_not_the_sum(tmp_path, quick):
    """3 items × 3 sites: Poshmark 0.6 s, Depop 0.4 s, Vinted 0.2 s a listing — one after the other that is 3.6 s; the
    workers side by side take about Poshmark's 1.8 s."""
    # The machine's own overhead first (workers starting, the idle turns before the end): the same run with listings
    # that take no time — a slow CI runner pays it in both runs.
    (tmp_path / "base").mkdir()
    s0 = _settings(tmp_path / "base", parallel=True)
    db0 = DB(s0.path("db"))
    for n in (1, 2, 3):
        _item(db0, n)
    base = _run(s0, db0, {"poshmark": Timed("poshmark", 0.0), "depop": Timed("depop", 0.0),
                          "vinted": Timed("vinted", 0.0)})
    s = _settings(tmp_path, parallel=True)
    db = DB(s.path("db"))
    items = [_item(db, n) for n in (1, 2, 3)]
    ps = {"poshmark": Timed("poshmark", 0.6), "depop": Timed("depop", 0.4), "vinted": Timed("vinted", 0.2)}
    took = _run(s, db, ps)
    assert all(db.listing(i, mp)["status"] == "posted" for i in items for mp in ps), [
        dict(r) for r in db.conn.execute("SELECT item_id, marketplace, status, error FROM listings")]
    assert took - base < 2.9, (took, base)                         # about Poshmark's 1.8 s, not the 3.6 s sum
    first = {mp: p.calls[0][1] for mp, p in ps.items()}
    assert max(first.values()) - min(first.values()) < 0.5          # one item, three sites at once


def test_one_line_per_item_in_order_and_all_done_last(tmp_path, quick):
    s = _settings(tmp_path, parallel=True)
    db = DB(s.path("db"))
    items = [_item(db, n) for n in (1, 2)]
    ps = {"poshmark": Timed("poshmark", 0.2), "depop": Timed("depop", 0.1), "vinted": Timed("vinted", 0.3)}
    _run(s, db, ps)
    lines = [g for g in quick.group if g.startswith("Posted ✓")]
    assert len(lines) == 2
    for line in lines:
        assert line.index("Poshmark https://") < line.index("Depop https://") < line.index("Vinted https://")
    assert "All done" in lines[-1] and "All done" not in lines[0]
    assert sorted(i for line in lines for i in items if i in line) == sorted(items)


def test_one_site_turned_away_never_stops_the_others(tmp_path, quick):
    """Depop shows a login page on the first item: Depop alone stops for the window (its one plain line); Poshmark and
    Vinted list every item, and each item's line names the sites that took it."""
    s = _settings(tmp_path, parallel=True)
    db = DB(s.path("db"))
    items = [_item(db, n) for n in (1, 2, 3)]
    ps = {"poshmark": Timed("poshmark", 0.1),
          "depop": Timed("depop", 0.1, fail=AccountBlocked("Depop: not logged in", page="login"), fail_for=items),
          "vinted": Timed("vinted", 0.1)}
    _run(s, db, ps)
    assert all(db.listing(i, "poshmark")["status"] == "posted" for i in items)
    assert all(db.listing(i, "vinted")["status"] == "posted" for i in items)
    assert crosslist.blocked(db, "depop") and len(ps["depop"].calls) == 1
    assert quick.group.count("Depop needs you to log in on the Mac.") == 1
    lines = [g for g in quick.group if g.startswith("Posted ✓")]
    assert len(lines) == 3 and all("Depop" not in line and "Vinted" in line for line in lines)


def test_a_poshmark_failure_stops_poshmark_alone(tmp_path, quick):
    """A login wall on Poshmark (its own worker, its own thread): Poshmark stops for the window — never the owner's
    PAUSE for all — and Depop and Vinted go on with every item."""
    s = _settings(tmp_path, parallel=True)
    db = DB(s.path("db"))
    items = [_item(db, n) for n in (1, 2)]
    ps = {"poshmark": Timed("poshmark", 0.1, fail=AccountBlocked("Poshmark: logged out", page="login"),
                            fail_for=items),
          "depop": Timed("depop", 0.1), "vinted": Timed("vinted", 0.1)}
    _run(s, db, ps)
    assert not s.flag_set("PAUSE") and crosslist.blocked(db, "poshmark")
    assert all(db.listing(i, mp)["status"] == "posted" for i in items for mp in ("depop", "vinted"))
    assert db.listing(items[0], "poshmark")["status"] == "queued"     # put back: nothing reached Poshmark
    assert db.listing(items[1], "poshmark") is None                    # stopped for the window: never taken
    assert quick.group.count("Poshmark needs you to log in on the Mac.") == 1


def test_the_extension_sites_run_side_by_side_one_click_each_posting_at_the_go_ahead(tmp_path, quick,
                                                                                         verified_selectors):
    """The real driver and bridge, the stand-in extension answering Depop and Vinted at the same time: each listing
    clicked exactly once, and no row 'posting' before its go-ahead (WO32b's rule, with the workers concurrent)."""
    s = _settings(tmp_path, parallel=True)
    db = DB(s.path("db"))
    items = [_item(db, n) for n in (1, 2)]
    seen_states = []
    real_claim = db.claim_listing

    def claim(iid, mp, count=True):                                   # the go-ahead: what the row was just before
        seen_states.append((mp, db.listing(iid, mp)["status"]))
        return real_claim(iid, mp, count)
    db.claim_listing = claim
    stubs = {mp: SimpleNamespace(name=mp, calls=[], results=[Outcome("posted", url=(
        "https://www.depop.com/products/shop-{slug}/" if mp == "depop" else "https://www.vinted.com/items/{iid}"),
        clicked=True)]) for mp in ("depop", "vinted")}
    ps = {"poshmark": Timed("poshmark", 0.1), **{mp: ExtStub(stubs[mp], verified_selectors) for mp in stubs}}
    _run(s, db, ps)
    fake: FakeExtension = ps["depop"].fake
    assert sorted(fake.clicked) == sorted((mp, i) for i in items for mp in ("depop", "vinted"))   # once each
    assert all(db.listing(i, mp)["status"] == "posted" for i in items for mp in ("depop", "vinted"))
    assert sorted(seen_states) == sorted((mp, "queued") for _ in items for mp in ("depop", "vinted"))


@pytest.fixture
def verified_selectors(tmp_path):
    import json

    from thrift_agent.bridge import EXT_DIR
    data = json.loads((EXT_DIR / "selectors.json").read_text(encoding="utf-8"))
    for site in ("vinted", "depop"):
        for step in data[site]["steps"].values():
            step["verified"] = True
    path = tmp_path / "selectors-verified.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_the_cross_rows_are_queued_at_approval_not_after_poshmark(tmp_path, quick):
    s = _settings(tmp_path, parallel=True)
    db = DB(s.path("db"))
    iid = _item(db)
    assert parallel.feed(s, db) == [iid]
    assert db.listing(iid, "depop")["status"] == "queued" and db.listing(iid, "vinted")["status"] == "queued"
    assert parallel.feed(s, db) == []                                  # once


def test_parallel_is_the_default_and_the_switch_falls_back(tmp_path):
    s = _settings(tmp_path, parallel=True)
    ext = {"poshmark": Timed("poshmark", 0), "depop": Timed("depop", 0, driver="extension")}
    assert parallel.parallel_ok(s, ext, once=False)
    assert not parallel.parallel_ok(s, ext, once=True)
    assert not parallel.parallel_ok(_settings(tmp_path, parallel=False), ext, once=False)
    playwright_cross = {"poshmark": Timed("poshmark", 0), "depop": Timed("depop", 0, driver="playwright")}
    assert not parallel.parallel_ok(s, playwright_cross, once=False)  # it would need Poshmark's Chrome profile


def test_the_crosslist_dry_run_fills_the_extension_sites_together(tmp_path, monkeypatch, capsys):
    """WO33: `thrift crosslist --dry-run <item>` (the poster stopped) fills Depop and Vinted at the same time, each in
    its own window — about the slower site's time; `--one-by-one` the old way, the sum. Each time is printed."""
    from thrift_agent import cli
    from thrift_agent.post import runner as runner_mod

    async def fake_cross(s, db, ps, ctx, iid, mp, dry, request, progress):
        progress("form open")
        await asyncio.sleep(0.4)
        return SimpleNamespace(status="dryrun", error=None, screenshot=None)

    async def nothing(*a, **k):
        return None

    async def no_bridge(*a, **k):
        return None, None
    monkeypatch.setattr(runner_mod, "posters", lambda s: {"depop": SimpleNamespace(driver="extension"),
                                                         "vinted": SimpleNamespace(driver="extension")})
    monkeypatch.setattr(runner_mod, "open_bridge", no_bridge)
    monkeypatch.setattr(runner_mod, "connect_extension", nothing)
    monkeypatch.setattr(runner_mod, "close_bridge", nothing)
    monkeypatch.setattr(runner_mod, "uses_browser", lambda ps: False)
    monkeypatch.setattr(runner_mod, "run_cross", fake_cross)
    s = _settings(tmp_path)
    timings = {}
    for together in (True, False):
        t0 = time.monotonic()
        asyncio.run(cli._crosslist_dry_run(s, None, "i_x", ["depop", "vinted"], together=together))
        timings[together] = time.monotonic() - t0
    out = capsys.readouterr().out
    assert timings[False] >= 0.8 and timings[True] < timings[False] - 0.25, timings    # the slower site, not the sum
    assert "depop: form open" in out and "together" in out and "one after another" in out
