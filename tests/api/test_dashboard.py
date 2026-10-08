"""The dashboard page (WO33, thrift_api.dashboard.render): the cards, the table and its filter flags, the owner's local
days, escaping, nothing loaded from anywhere, no buyer data."""
import base64
import hashlib
import html
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

from thrift_api.dashboard import render

UTC = timezone.utc
NOW = datetime(2026, 10, 7, 16, 0, tzinfo=UTC)                     # Wed Oct 7, noon in New York
POSH = "https://poshmark.com/listing/Lacoste-Tee-White-size-M-6700aa11bb22cc33dd44ee55"
DEPOP = "https://www.depop.com/products/thriftshop-lacoste-tee-white/"
VINTED = "https://www.vinted.com/items/7012345678-lacoste-tee?ref=closet&page=2"
SITES = ("Poshmark", "Depop", "Vinted")
CARDS = ("c-poshmark", "c-depop", "c-vinted", "c-sold", "c-toship", "c-unmatched")
VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})


class Page(HTMLParser):
    """The rendered page, parsed: every tag closed in order and every id once (else AssertionError); the <tbody> rows
    with their cells (by data-label), the text and attributes of every element with an id, the metas, scripts, styles."""

    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.open: list[str] = []
        self.rows: list[dict] = []
        self.text: dict[str, str] = {}
        self.attrs: dict[str, dict] = {}
        self.metas: list[dict] = []
        self.scripts: list[list] = []
        self.styles: list[str] = []
        self._ids: list[tuple[str, int]] = []
        self._cell: dict | None = None
        self.feed(source)
        self.close()
        assert not self.open, f"never closed: {self.open}"

    def handle_starttag(self, tag, attrs):
        a = {key: value or "" for key, value in attrs}
        if tag == "meta":
            self.metas.append(a)
        if tag in VOID:
            return
        self.open.append(tag)
        if "id" in a:
            assert a["id"] not in self.attrs, f"id {a['id']!r} twice"
            self._ids.append((a["id"], len(self.open)))
            self.text[a["id"]], self.attrs[a["id"]] = "", a
        if tag == "tr" and "tbody" in self.open:
            self.rows.append({"attrs": a, "cells": []})
        elif tag == "td" and "tbody" in self.open:
            self._cell = {"label": a.get("data-label", ""), "text": "", "links": []}
            self.rows[-1]["cells"].append(self._cell)
        elif tag == "a" and self._cell is not None:
            self._cell["links"].append(a)
        elif tag == "script":
            self.scripts.append([a, ""])
        elif tag == "style":
            self.styles.append("")

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        assert self.open and self.open[-1] == tag, f"</{tag}> while {self.open[-3:]} are open"
        if self._ids and self._ids[-1][1] == len(self.open):
            self._ids.pop()
        self.open.pop()
        if tag == "td":
            self._cell = None

    def handle_data(self, data):
        for key, _ in self._ids:
            self.text[key] += data
        if self._cell is not None:
            self._cell["text"] += data
        if self.open and self.open[-1] == "script":
            self.scripts[-1][1] += data
        elif self.open and self.open[-1] == "style":
            self.styles[-1] += data


def rows(page: Page) -> dict[str, dict]:
    """The item rows by data-id: {"attrs": the <tr>'s, "cells": {column: text}, "links": {column: [<a> attrs]}}."""
    return {row["attrs"]["data-id"]: {"attrs": row["attrs"],
                                      "cells": {cell["label"]: cell["text"].strip() for cell in row["cells"]},
                                      "links": {cell["label"]: cell["links"] for cell in row["cells"] if cell["links"]}}
            for row in page.rows if "data-id" in row["attrs"]}


def order(page: Page) -> list[str]:
    return [row["attrs"]["data-id"] for row in page.rows if "data-id" in row["attrs"]]


def item(iid, title="Lacoste Tee White size M", price=35, created="2026-10-01T15:00:00Z", sale=None, **sites):
    """One item of the API's snapshot; a site as poshmark=("posted", url)."""
    return {"id": iid, "title": title, "price": price, "created_at": created, "sale": sale,
            "listings": {site: {"status": status, "url": url} for site, (status, url) in sites.items()}}


def sale(marketplace="poshmark", sold="2026-10-05T18:00:00Z", price=30, ship_by=None, shipped=None, status="sold"):
    return {"marketplace": marketplace, "sold_at": sold, "price": price, "ship_by": ship_by, "shipped_at": shipped,
            "status": status}


def test_empty_data_renders_a_whole_page():
    for data in ({}, {"items": [], "unmatched": 0, "generated_at": "2026-10-07T19:05:00Z"}):
        text = render(data, now=NOW)
        assert text.startswith("<!doctype html>")
        for tag in ('<meta charset="utf-8">', '<meta name="viewport" content="width=device-width, initial-scale=1">',
                    "<title>Thrift dashboard</title>"):
            assert tag in text
        page = Page(text)
        assert {key: page.text[key] for key in CARDS} == dict.fromkeys(CARDS, "0")
        assert page.text["c-sold-total"] == "$0"
        assert page.text["c-shipby"] == "nothing to ship"
        assert [row["cells"][0]["text"] for row in page.rows] == ["Nothing yet"]
    assert "Nothing yet" in Page(render({})).source           # the default now (the current time)


def test_every_status_has_its_icon_and_live_listings_link_out():
    data = {"items": [
        item("i_a", poshmark=("posted", POSH), depop=("queued", None), vinted=("failed", None)),
        item("i_b", poshmark=("posting", None), depop=("skipped", None), vinted=("delisted", None)),
        item("i_c", poshmark=("sold", POSH), depop=("dryrun", None), vinted=("drafted", None)),
        item("i_d", poshmark=("relisting", None), depop=("posted", None)),
        item("i_e", poshmark=("posted", "javascript:alert(1)"), depop=("posted", DEPOP), vinted=("posted", VINTED)),
    ]}
    page = Page(render(data, now=NOW))
    got = rows(page)
    assert {iid: tuple(row["cells"][site] for site in SITES) for iid, row in got.items()} == {
        "i_a": ("✓", "⏳", "✗"),
        "i_b": ("⏳", "–", "↓"),
        "i_c": ("💰", "·", "·"),
        "i_d": ("relisting", "✓", "—"),                   # an unknown status as its word; no URL, no link; missing
        "i_e": ("✓ javascript:alert(1)", "✓", "✓"),        # only http(s) becomes a link, the rest is text
    }
    links = {(iid, site): found for iid, row in got.items() for site, found in row["links"].items()}
    assert {key: [a["href"] for a in found] for key, found in links.items()} == {
        ("i_a", "Poshmark"): [POSH], ("i_e", "Depop"): [DEPOP], ("i_e", "Vinted"): [VINTED]}
    for found in links.values():
        assert (found[0]["target"], found[0]["rel"]) == ("_blank", "noopener noreferrer")
    assert 'href="javascript' not in page.source


def test_every_value_is_escaped():
    nasty = '<script>alert("x")</script> Tom & Jerry\'s "best" <b>tee</b>'
    url = 'https://poshmark.com/listing/a"onmouseover="alert(1)<x>&y=1'
    data = {"generated_at": "<b>never</b>", "items": [
        item("i_<1>", title=nasty, poshmark=("posted", url), depop=("<img src=x onerror=alert(2)>", None),
             sale=sale(marketplace="<i>ebay</i>"))]}
    text = render(data, now=NOW)
    assert html.escape(nasty, quote=True) in text
    for raw in ("<script>alert", "<b>tee", "<img", "<i>ebay", "<b>never", 'a"onmouseover'):
        assert raw not in text
    assert 'href="https://poshmark.com/listing/a&quot;onmouseover=&quot;alert(1)&lt;x&gt;&amp;y=1"' in text
    page = Page(text)
    assert len(page.scripts) == 1                              # the filter script, nothing injected
    row = rows(page)["i_<1>"]
    assert row["cells"]["Item"] == nasty
    assert row["links"]["Poshmark"][0]["href"] == url
    assert row["cells"]["Depop"] == "<img src=x onerror=alert(2)>"
    assert row["cells"]["Sold"] == "<i>ebay</i> · Mon Oct 5"


def test_nothing_is_loaded_from_anywhere():
    data = {"items": [item("i_1", poshmark=("posted", POSH), depop=("posted", DEPOP), vinted=("posted", VINTED))]}
    text = render(data, now=NOW)
    low = text.lower()
    assert not re.search(r"<script[^>]*\ssrc\s*=", low)
    for tag in ("<link", "<img", "<iframe", "<object", "<embed", "<audio", "<video", "<base", "@import", "url(", "@font-face"):
        assert tag not in low, tag
    assert not re.search(r"""\b(?:src|href|action|srcset)\s*=\s*["']?//""", low)
    assert set(re.findall(r"""https?://[^\s"'<>]+""", text)) == {html.escape(u, quote=True) for u in (POSH, DEPOP, VINTED)}
    assert {html.unescape(href) for href in re.findall(r'\shref="([^"]*)"', text)} == {POSH, DEPOP, VINTED}


def test_inline_style_and_script_dark_mode_phone_layout_and_csp():
    page = Page(render({"items": [item("i_1", poshmark=("posted", POSH))]}, now=NOW))
    [(attrs, script)] = page.scripts
    [style] = page.styles
    assert attrs == {}                                         # inline: no src
    assert "@media (prefers-color-scheme: dark)" in style
    assert "system-ui" in style
    assert "@media (max-width: 719.98px)" in style            # phones: each row a card of labelled cells
    assert ".wrap { overflow-x: auto" in style                 # wider: the table scrolls inside its box, never the page
    assert '<div class="wrap"><table id="items">' in page.source
    assert all(cell["label"] for row in page.rows if "data-id" in row["attrs"] for cell in row["cells"])
    for word in ("location", "history", "fetch", "XMLHttpRequest", "http", "import", "reload"):
        assert word not in script                              # toggles rows only: no reload, the URL left alone
    for used in ('"data-" + want', "hidden", "aria-pressed"):
        assert used in script
    [csp] = [meta["content"] for meta in page.metas if meta.get("http-equiv") == "Content-Security-Policy"]
    assert "default-src 'none'" in csp
    for block in (script, style):
        assert "'sha256-" + base64.b64encode(hashlib.sha256(block.encode()).digest()).decode() + "'" in csp


def test_rows_carry_the_filter_flags():
    data = {"unmatched": 3, "items": [
        item("i_active", poshmark=("posted", POSH), depop=("queued", None)),
        item("i_sold_open", poshmark=("sold", None), depop=("posted", DEPOP), sale=sale(ship_by="2026-10-09")),
        item("i_shipped", poshmark=("sold", None), sale=sale(shipped="2026-10-06T20:00:00Z")),
        item("i_cancelled", poshmark=("posted", POSH), sale=sale(status="cancelled")),
        item("i_double", depop=("sold", None), sale=sale(marketplace="depop", status="double_sale")),
        item("i_done", vinted=("sold", None), sale=sale(marketplace="vinted", status="done")),
        item("i_queued", poshmark=("queued", None), vinted=("failed", None)),
    ]}
    page = Page(render(data, now=NOW))
    got = rows(page)
    assert {iid: tuple(row["attrs"][f"data-{flag}"] for flag in ("active", "sold", "toship")) for iid, row in got.items()} == {
        "i_active": ("1", "0", "0"),
        "i_sold_open": ("0", "1", "1"),                       # sold on Poshmark, still live on Depop: not active
        "i_shipped": ("0", "1", "0"),
        "i_cancelled": ("1", "0", "0"),                       # a cancelled sale is no sale
        "i_double": ("0", "1", "0"),
        "i_done": ("0", "1", "0"),
        "i_queued": ("0", "0", "0"),
    }
    assert {key: page.text[key] for key in CARDS} == {
        "c-poshmark": "2", "c-depop": "1", "c-vinted": "0", "c-sold": "4", "c-toship": "1", "c-unmatched": "3"}
    assert page.text["c-sold-total"] == "$120"
    assert "hidden" in page.attrs["filters"]                   # the script shows the bar: without JS, every row and no bar
    assert not any("hidden" in row["attrs"] for row in got.values())
    assert re.findall(r'<button type="button" data-f="(\w+)"[^>]*>([^<]+)</button>', page.source) == [
        ("all", "All"), ("active", "Active"), ("sold", "Sold"), ("toship", "To ship")]


def test_sold_this_month_is_the_owners_month():
    """2026-11-01T03:30Z is Oct 31, 23:30 in New York: October there, November in UTC."""
    data = {"items": [
        item("i_halloween", sale=sale(sold="2026-11-01T03:30:00Z", price=40, shipped="2026-11-02T15:00:00Z")),
        item("i_october", sale=sale(sold="2026-10-10T15:00:00Z", price=25, shipped="2026-10-11T15:00:00Z")),
        item("i_september", sale=sale(sold="2026-10-01T02:00:00Z", price=60, shipped="2026-10-02T15:00:00Z")),
        item("i_cancelled", sale=sale(sold="2026-10-20T15:00:00Z", price=99, status="cancelled")),
        item("i_unsold", poshmark=("posted", POSH)),
    ]}

    def sold(now, tz="America/New_York"):
        page = Page(render(data, now=now, tz=tz))
        return page.text["c-sold"], page.text["c-sold-total"]

    assert sold(datetime(2026, 10, 31, 16, 0, tzinfo=UTC)) == ("2", "$65")
    assert sold(datetime(2026, 11, 1, 3, 45, tzinfo=UTC)) == ("2", "$65")     # now itself: still Oct 31 in New York
    assert sold(datetime(2026, 10, 31, 16, 0)) == ("2", "$65")                # a naive now is UTC
    assert sold(datetime(2026, 11, 2, 15, 0, tzinfo=UTC)) == ("0", "$0")
    assert sold(datetime(2026, 11, 2, 15, 0, tzinfo=UTC), tz="UTC") == ("1", "$40")
    got = rows(Page(render(data, now=datetime(2026, 10, 31, 16, 0, tzinfo=UTC))))
    assert got["i_halloween"]["cells"]["Sold"] == "Poshmark · Sat Oct 31"
    assert got["i_september"]["cells"]["Sold"] == "Poshmark · Wed Sep 30"


def test_awaiting_shipment_counts_open_sales_and_shows_the_nearest_ship_by():
    data = {"items": [
        item("i_mon", sale=sale(ship_by="2026-10-12")),
        item("i_fri", sale=sale(ship_by="2026-10-09")),
        item("i_undated", sale=sale(ship_by=None)),                                          # open, no date yet
        item("i_shipped", sale=sale(ship_by="2026-10-05", shipped="2026-10-04T21:00:00Z")),  # Sun Oct 4, 17:00 local
        item("i_cancelled", sale=sale(ship_by="2026-10-02", status="cancelled")),
        item("i_double", sale=sale(ship_by="2026-10-03", status="double_sale")),
        item("i_done", sale=sale(ship_by="2026-10-04", status="done")),
    ]}
    page = Page(render(data, now=NOW))
    assert (page.text["c-toship"], page.text["c-shipby"]) == ("3", "Fri Oct 9")
    got = rows(page)
    assert {iid: got[iid]["cells"]["Ship by"] for iid in ("i_mon", "i_fri", "i_undated", "i_shipped")} == {
        "i_mon": "Mon Oct 12", "i_fri": "Fri Oct 9", "i_undated": "—", "i_shipped": "Mon Oct 5"}   # shipped: not late
    assert got["i_shipped"]["cells"]["Shipped"] == "✓ Sun Oct 4"
    assert got["i_fri"]["cells"]["Shipped"] == "—"
    assert got["i_cancelled"]["cells"]["Sold"] == "Poshmark · Mon Oct 5 cancelled"
    assert got["i_double"]["cells"]["Sold"] == "Poshmark · Mon Oct 5 double sale"
    late = Page(render({"items": [item("i_late", sale=sale(ship_by="2026-10-06"))]}, now=NOW))
    assert late.text["c-shipby"] == "Tue Oct 6"
    assert rows(late)["i_late"]["cells"]["Ship by"] == "Tue Oct 6 · late"
    assert '<div class="card alert"><h2>Awaiting shipment</h2>' in late.source


def test_buyer_data_never_reaches_the_page():
    private = ["Jane Q. Buyer", "janeq_closet", "123 Secret Lane, Springfield, IL 62704", "jane@example.com",
               "+1 555 0100", "9400111899223817", "gift note: happy birthday"]
    it = item("i_1", poshmark=("posted", POSH), sale=sale(ship_by="2026-10-09"))
    it.update(buyer=private[1], buyer_name=private[0], address=private[2])
    it["sale"].update(buyer=private[1], buyer_name=private[0], address={"street": private[2], "phone": private[4]},
                      email=private[3], tracking=private[5], note=private[6])
    it["listings"]["poshmark"]["buyer"] = private[1]
    data = {"items": [it], "unmatched": 1, "generated_at": "2026-10-07T19:05:00Z", "buyer": private[1],
            "unmatched_sales": [{"buyer_name": private[0], "address": private[2]}]}
    text = render(data, now=NOW)
    assert rows(Page(text))["i_1"]["cells"]["Sold"] == "Poshmark · Mon Oct 5"      # the sale itself is there
    for value in private:
        assert value not in text and html.escape(value, quote=True) not in text


def test_rows_newest_first_with_prices_and_local_days():
    data = {"items": [
        item("i_old", created="2026-09-01T12:00:00Z", price=1250),
        item("i_new", created="2026-10-06T12:00:00Z", price=None, title=None),
        item("i_sold", created="2026-08-01T12:00:00Z",                          # sold Tue Oct 6, 21:00 in New York
             sale=sale(sold="2026-10-07T01:00:00Z", ship_by="2026-10-09", shipped="2026-10-08T14:00:00Z")),
        item("i_undated", created=None),
        item("i_last_year", created="2025-10-01T12:00:00Z",
             sale=sale(sold="2025-10-09T16:00:00Z", shipped="2025-10-10T16:00:00Z")),
    ]}
    page = Page(render(data, now=NOW))
    assert order(page) == ["i_sold", "i_new", "i_old", "i_last_year", "i_undated"]
    got = rows(page)
    assert {iid: (row["cells"]["Item"], row["cells"]["Price"]) for iid, row in got.items()} == {
        "i_sold": ("Lacoste Tee White size M", "$35"), "i_new": ("(untitled)", "—"),
        "i_old": ("Lacoste Tee White size M", "$1,250"), "i_last_year": ("Lacoste Tee White size M", "$35"),
        "i_undated": ("Lacoste Tee White size M", "$35")}
    assert [got["i_sold"]["cells"][column] for column in ("Sold", "Ship by", "Shipped")] == [
        "Poshmark · Tue Oct 6", "Fri Oct 9", "✓ Thu Oct 8"]
    assert [got["i_last_year"]["cells"][column] for column in ("Sold", "Shipped")] == [
        "Poshmark · Thu Oct 9, 2025", "✓ Fri Oct 10, 2025"]
    assert [got["i_old"]["cells"][column] for column in (*SITES, "Sold", "Ship by", "Shipped")] == ["—"] * 6


def test_footer_says_when_the_data_was_made_in_local_time():
    def updated(at, tz="America/New_York"):
        return Page(render({"generated_at": at}, now=NOW, tz=tz)).text["updated"]

    assert updated("2026-10-07T19:05:00Z") == "Updated Wed Oct 7, 3:05 PM EDT"
    assert updated("2026-12-01T05:00:00+00:00") == "Updated Tue Dec 1, 12:00 AM EST"
    assert updated("2026-10-07T12:30:00") == "Updated Wed Oct 7, 8:30 AM EDT"           # no offset = UTC
    assert updated("2026-10-07T19:05:00Z", tz="Mars/Olympus") == "Updated Wed Oct 7, 7:05 PM UTC"   # unknown zone
    assert updated(None) == updated("yesterday") == "Updated —"


def test_odd_input_still_renders():
    for data in (None, {"items": "nope", "unmatched": "x"}, {"items": [None, 5, "x"]}):
        page = Page(render(data, now=NOW))
        assert page.text["c-unmatched"] == "0"
        assert [row["cells"][0]["text"] for row in page.rows] == ["Nothing yet"]
    page = Page(render({"items": [{"title": 7, "price": "35", "sale": "y",
                                   "listings": {"poshmark": None, "depop": {"status": None}, "vinted": "x"}}]}, now=NOW))
    [row] = [row for row in page.rows if "data-active" in row["attrs"]]
    assert [cell["text"] for cell in row["cells"]] == ["7", "—", "—", "—", "—", "—", "—", "—"]
    assert (row["attrs"]["data-active"], row["attrs"]["data-sold"], row["attrs"]["data-toship"]) == ("0", "0", "0")
