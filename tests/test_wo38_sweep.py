"""WO38 §2 on the Mac — the order sweep: each site's status words in ours (from scrubbed saved JSON / pages shaped like
the probes recorded them), Poshmark's own sales JSON read in a page of its own (buyer fields never copied, the open
orders' own pages for the exact times and our SKU), the extension's orders read; when a sweep is due; a login / CAPTCHA
page stopping that site's sweep for the day with ONE private line; one site at a time; and the poster's workers
sweeping only between their own listings — a listing never waits for it (measured)."""
import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from thrift_agent import orders
from thrift_agent.config import ROOT
from thrift_agent.db import DB

FIX = ROOT / "tests" / "fixtures"
UTC = timezone.utc


def ny(y, m, d, hh, mm=0) -> datetime:
    """A New York wall-clock time (EDT in October) as UTC."""
    return datetime(y, m, d, hh + 4, mm, tzinfo=UTC) if hh + 4 < 24 else datetime(y, m, d + 1, hh - 20, mm, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "state.db")


@pytest.fixture
def quiet(monkeypatch):
    """The private chat and the API recorded, the lid open."""
    said, posted = [], []
    from thrift_agent import notify, sales
    monkeypatch.setattr(notify, "say", lambda text: said.append(text))
    monkeypatch.setattr(sales, "call", lambda db, path, body: posted.append((path, body)) or True)
    monkeypatch.setattr(orders.power, "lid_closed", lambda: False)
    return SimpleNamespace(said=said, posted=posted)


# ---------------------------------------------------------------- every status word maps to ours

@pytest.mark.parametrize("site, words, ours", [
    ("poshmark", "Awaiting Shipment", "to_ship"), ("poshmark", "Shipped", "shipped"), ("poshmark", "In Transit", "shipped"),
    ("poshmark", "Delivered", "delivered"), ("poshmark", "Order Complete", "completed"),
    ("poshmark", "Order Completed", "completed"), ("poshmark", "Completed", "completed"),
    ("poshmark", "Order Cancelled", "cancelled"), ("poshmark", "Cancelled", "cancelled"),
    ("vinted", "Order shipped and on its way!", "shipped"), ("vinted", "Package delivered.", "delivered"),
    ("vinted", "Order completed successfully. The buyer accepted the item.", "completed"),
    ("vinted", "The order was cancelled.", "cancelled"),
    ("depop", "To ship", "to_ship"), ("depop", "In transit", "shipped"), ("depop", "Shipped", "shipped"),
    ("depop", "Completed", "completed"), ("depop", "Canceled", "cancelled"), ("depop", "TO_SHIP", "to_ship"),
    ("vinted", "Send the item by Oct 14", None), ("poshmark", "Processing", None), ("depop", "Sold on", None)])
def test_every_status_word_maps_to_ours(site, words, ours):
    assert orders.status_of(site, words) == ours


def test_poshmarks_saved_sales_json_reads_as_our_orders_without_a_buyer_field():
    rows = json.loads((FIX / "wo38_poshmark_sales.json").read_text(encoding="utf-8"))["data"]["sales_summary"]
    got = [orders.poshmark_order(r) for r in rows]
    assert [(o["order_id"][-1], o["status"]) for o in got] == [("1", "to_ship"), ("2", "shipped"), ("3", "delivered"),
                                                               ("4", "completed"), ("5", "cancelled"), ("6", "completed")]
    assert got[0] == {"order_id": "6ac431effa146f6457260001", "title": "Lacoste Tee White size M", "price": 35.0,
                      "sold_at": "2026-10-12T16:25:38-07:00", "status_text": "Awaiting Shipment", "status": "to_ship"}
    assert all(set(o) == {"order_id", "title", "price", "sold_at", "status_text", "status"} for o in got)


def test_poshmarks_order_page_gives_our_sku_the_listing_and_the_exact_times():
    detail = orders.poshmark_detail({
        "line_items": [{"sku": "i_261004_80586f", "product_id": "6ac301264dfc815534ea174f", "title": "x"}],
        "inventory_units": [{"status": "received", "shipped_on": "2026-09-29T10:37:43-07:00",
                             "delivered_on": "2026-10-05T16:46:18-07:00", "received_at": "2026-10-05T16:46:31-07:00",
                             "shipment_status": "DELIVERED IN/AT MAILBOX"}],
        "buyer": {"username": "janedoe"}, "shipping_address": {"street": "12 Main St"}})
    assert detail == {"sku": "i_261004_80586f", "listing_id": "6ac301264dfc815534ea174f",
                      "shipped_at": "2026-09-29T10:37:43-07:00", "delivered_at": "2026-10-05T16:46:18-07:00",
                      "completed_at": "2026-10-05T16:46:31-07:00", "unit_status": "completed"}
    assert orders.furthest("shipped", "completed") == "completed"
    assert orders.furthest("delivered", None) == "delivered" and orders.furthest("cancelled", "shipped") == "cancelled"


# ---------------------------------------------------------------- Poshmark: a page of its own, its own JSON

class Resp:
    def __init__(self, url, data):
        self.url, self._data, self.headers = url, data, {"content-type": "application/json; charset=utf-8"}

    async def json(self):
        return self._data


class Page:
    """Poshmark's seller hub as Playwright sees it: the page's own sales JSON arrives as a response when it opens;
    an order's page answers the same-origin fetch."""

    def __init__(self, data, details=None, lands="https://poshmark.com/seller-hub/sales", state=None):
        self.data, self.details, self.lands, self.state = data, details or {}, lands, state or {}
        self.handlers, self.fetched, self.clicked, self.closed, self.url = [], [], [], False, ""

    def on(self, event, fn):
        self.handlers.append(fn)

    async def goto(self, url, wait_until=None):
        self.url = self.lands
        if self.data is not None:
            for fn in self.handlers:
                fn(Resp("https://poshmark.com/vm-rest/users/u1/orders/sales?request=x&pm_version=1", self.data))

    async def content(self):
        return f"<script>window.__INITIAL_STATE__ = {json.dumps(self.state)}</script>"

    async def evaluate(self, js, url):
        self.fetched.append(url)
        order = self.details.get(url.rsplit("/", 1)[-1], {})
        return f"<script>__INITIAL_STATE__ = {json.dumps({'$_order_details': {'order': order}})}</script>"

    async def click(self, *a, **kw):                     # never called: the sweep only reads
        self.clicked.append(a)

    async def close(self):
        self.closed = True


class Ctx:
    def __init__(self, page):
        self.page, self.pages = page, 0

    async def new_page(self):
        self.pages += 1
        return self.page


def sales_json():
    return json.loads((FIX / "wo38_poshmark_sales.json").read_text(encoding="utf-8"))


def test_poshmark_is_read_from_its_own_json_in_a_new_page_open_orders_detailed(monkeypatch):
    monkeypatch.setattr(orders, "_recent", lambda o, now=None: o["order_id"] != "6ac431effa146f6457260006")
    details = {"6ac431effa146f6457260003": {"line_items": [{"sku": "i_261004_aaaaaa", "product_id": "p3"}],
                                            "inventory_units": [{"status": "delivered",
                                                                 "shipped_on": "2026-10-08T12:33:01-07:00",
                                                                 "delivered_on": "2026-10-13T10:48:18-07:00"}]}}
    page = Page(sales_json(), details)
    ctx = Ctx(page)
    got = asyncio.run(orders.read_poshmark(ctx))
    assert ctx.pages == 1 and page.closed and page.clicked == []
    assert [o["order_id"][-1] for o in got] == ["1", "2", "3", "4", "5"]           # May's is too old
    # the open ones' own pages only (a handful): awaiting shipment, in transit, delivered — never completed / cancelled
    assert page.fetched == [f"/order/sales/6ac431effa146f645726000{n}" for n in (1, 2, 3)]
    third = got[2]
    assert (third["status"], third["sku"], third["listing_id"], third["delivered_at"]) == (
        "delivered", "i_261004_aaaaaa", "p3", "2026-10-13T10:48:18-07:00")


def test_poshmark_waits_for_a_posting_job_and_says_login_or_captcha(monkeypatch):
    monkeypatch.setattr(orders, "_recent", lambda o, now=None: True)
    page = Page(sales_json())
    with pytest.raises(orders.Yielded):
        asyncio.run(orders.read_poshmark(Ctx(page), yield_to=lambda: True))
    assert page.fetched == [] and page.closed
    with pytest.raises(orders.Stopped) as e:
        asyncio.run(orders.read_poshmark(Ctx(Page(None, lands="https://poshmark.com/login?next=/order/sales")), timeout=1))
    assert e.value.page == "login"
    with pytest.raises(orders.Stopped) as e:
        asyncio.run(orders.read_poshmark(Ctx(Page(None, state={"showCaptcha": True})), timeout=0.5))
    assert e.value.page == "captcha"
    with pytest.raises(RuntimeError, match="didn't load"):
        asyncio.run(orders.read_poshmark(Ctx(Page(None)), timeout=0.5))


# ---------------------------------------------------------------- Depop and Vinted: the extension's job

class Ext:
    name, driver = "vinted", "extension"

    def __init__(self, answer):
        self.answer, self.jobs = answer, []

    def available(self):
        return True

    async def _job(self, mode, payload, timeout):
        self.jobs.append((mode, payload))
        return self.answer


def test_the_extensions_orders_are_put_in_our_words():
    data = json.loads((FIX / "wo38_vinted_my_orders.json").read_text(encoding="utf-8"))
    rows = [{"order_id": str(o["transaction_id"]), "title": o["title"], "price": o["price"]["amount"],
             "sold_at": o["date"], "status_text": o["status"]} for o in data["my_orders"]]  # vinted.js `orders`
    ext = Ext({"event": "result", "orders": rows, "source": "json"})
    got = asyncio.run(orders.read_extension(ext, "vinted"))
    assert ext.jobs == [("orders", {"orders_url": orders.URLS["vinted"], "sweep": True})]
    assert [(o["order_id"], o["status"]) for o in got] == [("22942401001", "delivered"), ("22845581002", "shipped"),
                                                           ("22859497003", "completed"), ("22533303004", "cancelled")]
    assert got[0]["price"] == 5.0
    depop = Ext({"event": "result", "source": "page",
                 "orders": [{"order_id": "915093750", "status_text": "Shipped", "sold_ago": "20 days ago"}]})
    one = asyncio.run(orders.read_extension(depop, "depop"))[0]
    assert one["status"] == "shipped" and one["sold_at"][:4] == "2026"


def test_a_login_or_captcha_page_from_the_extension_stops_the_read():
    for page in ("login", "captcha", "block", "verify"):
        with pytest.raises(orders.Stopped):
            asyncio.run(orders.read_extension(Ext({"event": "error", "page": page, "message": "x"}), "depop"))
    with pytest.raises(RuntimeError, match="answered 500"):
        asyncio.run(orders.read_extension(Ext({"event": "error", "page": "unknown", "stage": "orders",
                                               "message": "Vinted's sold orders answered 500"}), "vinted"))


@pytest.mark.parametrize("text, days", [("20 days ago", 20), ("a day ago", 1), ("3 hours ago", 0.125),
                                        ("yesterday", 1), ("just now", 0), ("2 weeks ago", 14)])
def test_depops_relative_times(text, days):
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    got = datetime.fromisoformat(orders.ago(text, now))
    assert abs((now - got).total_seconds() - days * 86400) < 1
    assert orders.ago("Sold on", now) is None


# ---------------------------------------------------------------- when a sweep is due

def stamp(db, key, site, value):
    orders._put(db, key, site, value)


def test_due_from_eight_then_every_six_hours_and_the_days_first(db, quiet):
    assert not orders.due(db, "vinted", ny(2026, 10, 14, 7, 59))                  # never read, before 08:00: not yet
    assert orders.due(db, "vinted", ny(2026, 10, 14, 8, 1))                       # the first moment after 08:00
    stamp(db, orders.KV_SWEPT, "vinted", ny(2026, 10, 14, 8, 1).isoformat())
    assert not orders.due(db, "vinted", ny(2026, 10, 14, 13, 0))
    assert orders.due(db, "vinted", ny(2026, 10, 14, 14, 2))                      # 6 hours on
    stamp(db, orders.KV_SWEPT, "vinted", ny(2026, 10, 14, 2, 0).isoformat())       # read at 2 AM (the Mac was open)
    assert not orders.due(db, "vinted", ny(2026, 10, 14, 7, 30))
    assert orders.due(db, "vinted", ny(2026, 10, 14, 8, 0))                       # the day's first after 08:00
    stamp(db, orders.KV_SWEPT, "vinted", ny(2026, 10, 13, 20, 0).isoformat())      # on wake, the last one 6 h+ old
    assert orders.due(db, "vinted", ny(2026, 10, 14, 2, 30))


def test_never_with_the_lid_closed_a_stopped_day_or_a_fresh_failure(db, quiet, monkeypatch):
    now = ny(2026, 10, 14, 10, 0)
    monkeypatch.setattr(orders.power, "lid_closed", lambda: True)
    assert not orders.due(db, "depop", now)
    monkeypatch.setattr(orders.power, "lid_closed", lambda: False)
    stamp(db, orders.KV_FAILED, "depop", (now - timedelta(minutes=10)).isoformat())
    assert not orders.due(db, "depop", now)
    assert orders.due(db, "depop", now + timedelta(minutes=25))
    stamp(db, orders.KV_STOPPED, "depop", "2026-10-14")
    assert not orders.due(db, "depop", now + timedelta(hours=1))
    assert orders.due(db, "depop", ny(2026, 10, 15, 8, 5))                        # tomorrow


# ---------------------------------------------------------------- one sweep

def run_sweep(db, site, read, s=None):
    return asyncio.run(orders.sweep(s or SimpleNamespace(), db, site, read))


def test_a_sweep_sends_our_fields_only_and_says_an_unknown_status_once(db, quiet):
    found = [{"order_id": "1", "status": "delivered", "status_text": "Package delivered.", "title": "Nike sandals",
              "price": 5.0, "sold_at": datetime.now(UTC).isoformat(), "buyer": "janedoe", "address": "12 Main St"},
             {"order_id": "2", "status": None, "status_text": "Send the item by Oct 14", "sold_at": None},
             {"order_id": "3", "status": None, "status_text": "Send the item by Oct 14", "sold_at": None},
             {"order_id": "4", "status": "completed", "status_text": "Order completed", "sold_at": "2026-05-01T00:00:00Z"}]

    async def read():
        return found
    out = run_sweep(db, "vinted", read)
    assert out["status"] == "swept" and out["orders"] == 1
    (path, body), = quiet.posted
    assert path == "/orders" and body["marketplace"] == "vinted"
    assert [o["order_id"] for o in body["orders"]] == ["1"]                     # May's is too old; unknown ones left
    assert set(body["orders"][0]) == set(orders.SEND) and "janedoe" not in json.dumps(body)
    assert quiet.said == ["🔎 Vinted shows an order status I don't know: “Send the item by Oct 14” — those orders "
                          "are left as they are."]
    assert "vinted" in orders.last_sweeps(db)


def test_a_login_page_stops_that_sites_sweep_for_the_day_with_one_private_line(db, quiet):
    async def read():
        raise orders.Stopped("login")
    assert run_sweep(db, "depop", read)["status"] == "stopped"
    assert run_sweep(db, "depop", read)["status"] == "stopped"
    assert quiet.said == ["⏸ Can't read Depop's orders today — it shows its login page. I'll try again tomorrow."]
    assert not orders.due(db, "depop") and quiet.posted == []
    assert orders._kv(db, orders.KV_STOPPED)["depop"] == datetime.now(UTC).astimezone(
        orders.ZoneInfo("America/New_York")).date().isoformat()


def test_a_failed_sweep_is_tried_again_later_and_never_raises(db, quiet):
    async def read():
        raise RuntimeError("Poshmark's sales list didn't load in 25 s")
    out = run_sweep(db, "poshmark", read)
    assert out["status"] == "failed" and "didn't load" in out["error"]
    assert "poshmark" in orders._kv(db, orders.KV_FAILED) and quiet.posted == [] and quiet.said == []


def test_one_site_at_a_time(db, quiet):
    held = threading.Event()

    async def slow():
        held.set()
        await asyncio.sleep(0.3)
        return []

    async def both():
        first = asyncio.create_task(orders.sweep(SimpleNamespace(), db, "depop", slow))
        while not held.is_set():
            await asyncio.sleep(0.01)

        async def other():
            return []
        second = await orders.sweep(SimpleNamespace(), db, "vinted", other)
        return await first, second
    first, second = asyncio.run(both())
    assert first["status"] == "swept" and second["status"] == "busy"


def test_maybe_sweep_only_between_listings_with_room_and_never_with_pause(db, quiet, monkeypatch):
    calls = []

    async def read():
        calls.append(1)
        return []
    s = SimpleNamespace()
    monkeypatch.setattr(orders, "enabled", lambda s: True)
    monkeypatch.setattr(orders, "due", lambda db, site, now=None, tz=None: True)
    from thrift_agent.post import runner
    monkeypatch.setattr(runner, "_paused", lambda db: None)
    assert asyncio.run(orders.maybe_sweep(s, db, "vinted", read, gap=30.0)) is None          # too little room
    assert asyncio.run(orders.maybe_sweep(s, db, "vinted", read, gap=None,
                                          poster=SimpleNamespace(available=lambda: False))) is None   # extension away
    assert asyncio.run(orders.maybe_sweep(s, db, "vinted", read, gap=90.0))["status"] == "swept"
    assert asyncio.run(orders.maybe_sweep(s, db, "vinted", read, gap=None))["status"] == "swept"   # an idle turn
    monkeypatch.setattr(runner, "_paused", lambda db: "PAUSE")
    assert asyncio.run(orders.maybe_sweep(s, db, "vinted", read, gap=None)) is None
    assert len(calls) == 2


def test_the_sweep_needs_the_mac_and_the_api(monkeypatch):
    from thrift_agent import sales
    monkeypatch.setattr(sales, "api", lambda: object())
    prod = SimpleNamespace(is_prod=True, get=lambda key, default=None: default)
    assert orders.enabled(prod)
    assert not orders.enabled(SimpleNamespace(is_prod=False, get=lambda key, default=None: default))
    assert not orders.enabled(SimpleNamespace(is_prod=True, get=lambda key, default=None: False))
    monkeypatch.setattr(sales, "api", lambda: None)
    assert not orders.enabled(prod)


# ---------------------------------------------------------------- isolation: the poster's workers (measured)

def test_the_sweep_runs_inside_the_pause_after_a_listing_and_a_listing_never_waits(tmp_path, monkeypatch):
    """Poshmark's worker with three listings waiting and a 0.6 s pause after each: the sweep (0.25 s) runs inside the
    first pause, after the listing; the next listing starts when the pause ends — the time per listing and between
    listings the same as without the sweep (measured both ways)."""
    import test_parallel as tp
    from thrift_agent import notify, sales
    from thrift_agent.post import parallel, runner

    def run(sweeping: bool):
        folder = tmp_path / ("with" if sweeping else "without")
        folder.mkdir()
        mp = pytest.MonkeyPatch()
        try:
            mp.setattr(parallel, "IDLE", 0.05)
            mp.setattr(parallel, "HOUSEKEEPING", 0.05)
            mp.setattr(parallel, "next_gap", lambda schedule: 0.6)
            mp.setattr(parallel.crosslist, "listing_gap", lambda s, m: 0.0)
            mp.setattr(runner, "start_thrift_chrome", lambda s: "not in a test")
            mp.setattr(runner, "map_fields", lambda m, view: tp.fields_for(m))
            mp.setattr(runner.ItemView, "from_row", classmethod(lambda cls, it: SimpleNamespace(
                render=SimpleNamespace(title=tp.TITLE))))
            for name in ("group", "say"):
                mp.setattr(notify, name, lambda text: None)
            for name in ("group_photo", "photo", "ops_photo"):
                mp.setattr(notify, name, lambda path, caption: None)
            mp.setattr(runner, "_paused", lambda db: None)
            mp.setattr(orders, "enabled", lambda s: sweeping)
            mp.setattr(orders, "BUDGET", 0.3)
            swept = []
            mp.setattr(orders, "due", lambda db, site, now=None, tz=None: site == "poshmark" and not swept)
            mp.setattr(sales, "call", lambda db, path, body: True)

            async def fake_read(ctx, yield_to=None):
                t0 = time.monotonic()
                await asyncio.sleep(0.25)
                swept.append((t0, time.monotonic()))
                return []
            mp.setattr(orders, "read_poshmark", fake_read)
            s = tp._settings(folder, parallel=True)
            db = DB(s.path("db"))
            for n in (1, 2, 3):
                tp._item(db, n)
            posh = Clocked("poshmark", 0.3)
            ps = {"poshmark": posh, "depop": tp.Timed("depop", 0.0), "vinted": tp.Timed("vinted", 0.0)}
            asyncio.run(parallel.run_parallel(s, db, ps, dry=False, stage="form", opener=tp.fake_open,
                                              until_idle=True))
            return posh.spans, swept
        finally:
            mp.undo()

    spans_without, _ = run(False)
    spans_with, swept = run(True)
    assert len(spans_with) == len(spans_without) == 3 and len(swept) == 1
    (s0, s1), = swept
    assert spans_with[0][1] <= s0 and s1 <= spans_with[1][0]                     # after a listing, before the next
    each = lambda spans: [end - start for start, end in spans]                    # noqa: E731
    between = lambda spans: [b[0] - a[1] for a, b in zip(spans, spans[1:])]       # noqa: E731
    for a, b in zip(each(spans_with), each(spans_without)):
        assert abs(a - b) < 0.15, (each(spans_with), each(spans_without))         # the time per listing, unchanged
    for a, b in zip(between(spans_with), between(spans_without)):
        assert abs(a - b) < 0.2, (between(spans_with), between(spans_without))    # no listing waited for the sweep


class Clocked:
    """A stub poster recording each listing's start and end."""

    def __init__(self, name, delay):
        import test_parallel as tp
        self.inner, self.spans = tp.Timed(name, delay), []
        self.name, self.driver = name, "stub"

    def __getattr__(self, attr):
        return getattr(self.inner, attr)

    async def post(self, ctx, r, mode, dry_run, shots, stage="form"):
        t0 = time.monotonic()
        try:
            return await self.inner.post(ctx, r, mode, dry_run, shots, stage)
        finally:
            self.spans.append((t0, time.monotonic()))
