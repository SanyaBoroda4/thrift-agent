"""The dashboard (WO33): one private, read-only page for the phone — what is live on Poshmark, Depop and Vinted, what
sold this month, what is still to ship. `render(data)` turns the API's snapshot into a complete HTML document: stdlib
only (Python 3.11+), no web framework, nothing loaded from anywhere — the CSS and the filter script are inline, and the
page's Content-Security-Policy allows exactly those two (by their hashes); the only links are the listings' own pages.

The snapshot. Only these keys are read, so nothing else a record carries (the buyer, an address) can reach the page:

    {"items": [{"id", "title", "price", "created_at",
                "listings": {"poshmark" | "depop" | "vinted": {"status", "url"}},      # a site may be missing
                "sale": None | {"marketplace", "sold_at", "price", "ship_by", "shipped_at", "status",
                                "shipping"},                      # WO38: to_ship | shipped | delivered | completed …
                "still_listed": ["Poshmark", …],     # WO40: sold, still listed there 2 h after the sale (absent: none)
                "drops": {"live_since", "days_live", "stage", "next_drop_at", "no_drop", "price_now",     # WO41
                          "done": [{"marketplace", "old_price", "new_price", "done_at"}]}}],
     "unmatched": int,                                                             # sales that matched no item
     "swept": {"poshmark" | "depop" | "vinted": "ISO-8601"},          # WO38: when each site's orders were last read
     "generated_at": "ISO-8601"}

Every value is escaped; only absolute http(s) URLs become links. Times without an offset are UTC; everything is shown in
the owner's zone (`tz`), days as "Thu Oct 9" from fixed English names, never the server's locale (", 2025" is added for
another year). A bare "YYYY-MM-DD" (ship_by) is a calendar day and is not moved between zones."""

import base64
import hashlib
import html
import math
from datetime import date, datetime, timezone, tzinfo
from decimal import Decimal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["render"]

SITES = (("poshmark", "Poshmark"), ("depop", "Depop"), ("vinted", "Vinted"))
SITE_NAMES = dict(SITES)
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DASH = "—"

CANCELLED = frozenset({"cancelled", "canceled"})          # a cancelled sale is no sale
CLOSED = CANCELLED | {"double_sale", "done"}              # a sale in one of these waits for no shipment
SALE_NOTES = {"cancelled": ("cancelled", "dim"), "canceled": ("cancelled", "dim"), "double_sale": ("double sale", "warn")}

# a listing's status -> (icon, CSS class, what it means); any other status is shown as its own word, muted
STATUS = {
    "posted": ("✓", "ok", "live"),
    "queued": ("⏳", "wait", "queued"),
    "posting": ("⏳", "wait", "posting"),
    "failed": ("✗", "bad", "failed"),
    "skipped": ("–", "dim", "skipped"),
    "delisted": ("↓", "dim", "delisted"),
    "sold": ("💰", "sold", "sold"),
    "dryrun": ("·", "dim", "dry run"),
    "drafted": ("·", "dim", "draft"),
}
NOT_LISTED = ("—", "dim", "not listed")
LEGEND = (("✓", "ok", "live (tap to open)"), ("⏳", "wait", "queued"), ("✗", "bad", "failed"), ("–", "dim", "skipped"),
          ("↓", "dim", "delisted"), ("💰", "sold", "sold"), ("·", "dim", "draft / dry run"), NOT_LISTED)
FILTERS = (("all", "All"), ("active", "Active"), ("sold", "Sold"), ("toship", "To ship"))
COLUMNS = (("t", "Item"), ("p", "Price"), *(("s", name) for _, name in SITES),
           ("d", "Sold"), ("d", "Ship by"), ("d", "Shipping"))
# WO38: where a sale is, as the site itself last showed it (the order sweep) or our records say
SHIPPING = {"to_ship": ("To ship", "late"), "shipped": ("Shipped", "ok"), "delivered": ("Delivered", "ok"),
            "completed": ("Completed", "ok"), "returned": ("Returned", "warn"), "cancelled": ("Cancelled", "dim")}

STYLE = """
:root {
  color-scheme: light dark;
  --bg: #f4f5f7; --card: #ffffff; --fg: #17191c; --muted: #59616b; --line: #d9dde3;
  --ok: #12703a; --bad: #b42318; --warn: #8f4d00; --link: #0b57d0;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #101317; --card: #1a1e24; --fg: #e9ebee; --muted: #a3abb5; --line: #2f353d;
    --ok: #6fd391; --bad: #ff8f85; --warn: #f2c06b; --link: #8ab4f8;
  }
}
*, *::before, *::after { box-sizing: border-box; }
[hidden] { display: none !important; }
html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif,
    "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji";
}
main { max-width: 1100px; margin: 0 auto; padding: 16px; }
h1 { margin: 0 0 12px; font-size: 1.3rem; }
h2 { margin: 0 0 4px; font-size: .8rem; font-weight: 600; color: var(--muted); }
a { color: var(--link); }
.cards { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin: 0 0 14px; }
.card { min-width: 0; padding: 10px 12px; background: var(--card); border: 1px solid var(--line); border-radius: 12px; }
.card.alert { border-color: var(--warn); }
.card.alert .num { color: var(--warn); }
.num { margin: 0; font-size: 1.7rem; font-weight: 700; line-height: 1.15; }
.sub { margin: 2px 0 0; font-size: .85rem; color: var(--muted); }
.sites { display: grid; grid-template-columns: 1fr auto; gap: 1px 10px; margin: 0; }
.sites dt { color: var(--muted); }
.sites dd { margin: 0; font-weight: 700; text-align: right; }
.num, .sites dd, td.p { font-variant-numeric: tabular-nums; }
.filters { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 10px; }
.filters button {
  min-height: 36px; padding: 4px 14px; font: inherit; font-size: .9rem; cursor: pointer;
  color: var(--fg); background: var(--card); border: 1px solid var(--line); border-radius: 999px;
}
.filters button[aria-pressed="true"] { color: var(--bg); background: var(--fg); border-color: var(--fg); }
.wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { width: 100%; border-collapse: separate; border-spacing: 0; }
td { overflow-wrap: break-word; }
td.t { font-weight: 600; overflow-wrap: anywhere; }
.st { display: inline-block; min-width: 1.5em; text-align: center; }
a.st { padding: 2px 4px; font-weight: 700; text-underline-offset: 3px; }
.ok { color: var(--ok); }
.bad, .late { color: var(--bad); }
.late { font-weight: 600; }
.warn { color: var(--warn); }
.dim, .other { color: var(--muted); }
.other, .note { font-size: .8rem; }
.raw {
  display: inline-block; max-width: 12em; vertical-align: top; text-align: left; white-space: normal;
  font-size: .75rem; color: var(--muted); word-break: break-all;
}
footer { margin-top: 14px; font-size: .8rem; color: var(--muted); }
footer p { margin: 0; }
.legend { display: flex; flex-wrap: wrap; gap: 2px 14px; margin: 6px 0 0; padding: 0; list-style: none; }
@media (max-width: 719.98px) {
  thead { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  table, tbody { display: block; }
  tr {
    display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px 10px; margin: 0 0 8px; padding: 10px 12px;
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  }
  tr.empty { display: block; color: var(--muted); }
  td { display: block; min-width: 0; padding: 0; }
  td::before { content: attr(data-label); display: block; font-size: .72rem; color: var(--muted); }
  td.t { grid-column: span 2; }
  td.p { text-align: right; font-weight: 600; }
  td.t::before, td.p::before, tr.empty td::before { content: none; }
  td.sale .sep { display: none; }
  td.sale .day { display: block; }
}
@media (min-width: 720px) {
  .cards { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  table { background: var(--card); border: 1px solid var(--line); border-radius: 12px; }
  th, td { padding: 8px 10px; text-align: left; vertical-align: top; border-bottom: 1px solid var(--line); }
  th { font-size: .78rem; font-weight: 600; color: var(--muted); white-space: nowrap; }
  th.s, td.s { text-align: center; white-space: nowrap; }
  td.p, .day { white-space: nowrap; }
  tbody tr:last-child td { border-bottom: 0; }
  tr.empty td { color: var(--muted); }
}
"""

# The filters: show the rows whose data-<filter> is "1" (All: every row). Never reloads, never touches the URL; the bar
# is rendered hidden and shown only here, so a page without JS has every row and no dead buttons.
SCRIPT = """
(function () {
  var bar = document.getElementById("filters");
  if (!bar || !bar.addEventListener) { return; }
  var rows = document.querySelectorAll("#items tbody tr[data-active]");
  var buttons = bar.querySelectorAll("button[data-f]");
  var none = document.getElementById("none");
  bar.hidden = false;
  bar.addEventListener("click", function (event) {
    var pick = event.target.closest ? event.target.closest("button[data-f]") : null;
    if (!pick) { return; }
    var want = pick.getAttribute("data-f"), shown = 0;
    for (var i = 0; i < rows.length; i++) {
      var on = want === "all" || rows[i].getAttribute("data-" + want) === "1";
      rows[i].hidden = !on;
      if (on) { shown++; }
    }
    if (none) { none.hidden = shown > 0; }
    for (var j = 0; j < buttons.length; j++) {
      buttons[j].setAttribute("aria-pressed", buttons[j] === pick ? "true" : "false");
    }
  });
})();
"""


def _csp_hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


# Nothing but this page's own <style> and <script> (byte for byte: edit either and its hash follows), no other source.
CSP = (f"default-src 'none'; style-src {_csp_hash(STYLE)}; script-src {_csp_hash(SCRIPT)}; "
       "base-uri 'none'; form-action 'none'")


def render(data: dict, now: datetime | None = None, tz: str = "America/New_York") -> str:
    """The dashboard page for `data` (the snapshot above) as a complete HTML5 document. `now` (default: the current
    UTC time; a naive one is UTC) decides "this month", "late" and which dates need their year, all in `tz` (an unknown
    zone falls back to UTC, which the footer then shows)."""
    zone = _zone(tz)
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local_now = moment.astimezone(zone)
    today = local_now.date()
    data = data if isinstance(data, dict) else {}
    raw = data.get("items")
    items = [_item(x) for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []
    items.sort(key=lambda item: _sort_key(item, zone), reverse=True)      # stable: equal keys keep their order
    parts = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">',
        '<meta name="referrer" content="no-referrer">',
        '<meta name="robots" content="noindex, nofollow">',
        "<title>Thrift dashboard</title>",
        f"<style>{STYLE}</style>",
        "</head>",
        "<body>",
        "<main>",
        "<h1>Thrift dashboard</h1>",
        _cards(items, _count(data.get("unmatched")), zone, local_now),
        _filters(),
        _table(items, zone, today),
        _footer(data.get("generated_at"), zone, today, data.get("swept")),
        "</main>",
        f"<script>{SCRIPT}</script>",
        "</body>",
        "</html>",
    ]
    return "\n".join(parts) + "\n"


# --- the snapshot, read --------------------------------------------------------------------------------------------

def _item(raw: dict) -> dict:
    """Only the keys the page shows, copied out of the raw item: whatever else it or its sale carries stays behind."""
    listings = raw.get("listings") if isinstance(raw.get("listings"), dict) else {}
    sites = {}
    for key, _ in SITES:
        entry = listings.get(key)
        if isinstance(entry, dict) and _norm(entry.get("status")):
            sites[key] = {"status": _norm(entry.get("status")), "url": entry.get("url")}
    sale = raw.get("sale")
    if isinstance(sale, dict):
        sale = {key: sale.get(key) for key in ("marketplace", "sold_at", "price", "ship_by", "shipped_at", "status",
                                               "shipping")}
        sale["status"] = _norm(sale["status"])
        sale["shipping"] = _norm(sale["shipping"]) if _norm(sale["shipping"]) in SHIPPING else None
    else:
        sale = None
    still = raw.get("still_listed")
    still = [str(s) for s in still if isinstance(s, str)][:3] if isinstance(still, list) else []
    return {"id": raw.get("id"), "title": raw.get("title"), "price": raw.get("price"),
            "created_at": raw.get("created_at"), "sites": sites, "sale": sale, "still_listed": still,
            "drops": _drops(raw.get("drops"))}


def _drops(raw) -> dict | None:
    """WO41: the item's price drops, only the keys the page shows."""
    if not isinstance(raw, dict):
        return None
    done = [{k: d.get(k) for k in ("marketplace", "old_price", "new_price", "done_at")}
            for d in raw.get("done") or [] if isinstance(d, dict)][:6]
    stage = raw.get("stage")
    days = raw.get("days_live")
    return {"live_since": raw.get("live_since"), "days_live": days if isinstance(days, int) else None,
            "stage": stage if isinstance(stage, int) else 0, "next_drop_at": raw.get("next_drop_at"),
            "no_drop": raw.get("no_drop") is True, "price_now": _number(raw.get("price_now")), "done": done}


def _is_sold(sale: dict | None) -> bool:
    """A sale that stands: any but a cancelled one."""
    return sale is not None and sale["status"] not in CANCELLED


def _to_ship(sale: dict | None) -> bool:
    """An open sale: not cancelled, double_sale or done, not shipped yet — and (WO38) its site doesn't show it left."""
    return (sale is not None and sale["status"] not in CLOSED and _blank(sale["shipped_at"])
            and sale.get("shipping") in (None, "to_ship"))


def _is_active(item: dict) -> bool:
    """Live on at least one site and not sold."""
    return any(entry["status"] == "posted" for entry in item["sites"].values()) and not _is_sold(item["sale"])


def _sort_key(item: dict, zone: tzinfo) -> datetime:
    """Newest first by the sale's sold_at, else the item's created_at; an item with neither goes last."""
    for value in ((item["sale"] or {}).get("sold_at"), item["created_at"]):
        when = _when(value)
        if isinstance(when, datetime):
            return when
        if isinstance(when, date):
            return datetime(when.year, when.month, when.day, tzinfo=zone)
    return datetime.min.replace(tzinfo=timezone.utc)


# --- the page's parts --------------------------------------------------------------------------------------------

def _cards(items: list[dict], unmatched: int, zone: tzinfo, now: datetime) -> str:
    today = now.date()
    live = {key: 0 for key, _ in SITES}
    sold, total, to_ship, next_by = 0, 0.0, 0, None
    for item in items:
        for key, entry in item["sites"].items():
            if entry["status"] == "posted":
                live[key] += 1
        sale = item["sale"]
        if _is_sold(sale):
            day = _local_day(sale["sold_at"], zone)
            if day is not None and (day.year, day.month) == (now.year, now.month):
                sold += 1
                total += float(_number(sale["price"]) or 0)
        if _to_ship(sale):
            to_ship += 1
            by = _local_day(sale["ship_by"], zone)
            if by is not None and (next_by is None or by < next_by):
                next_by = by
    late = next_by is not None and next_by < today
    if next_by is not None:
        ship = f'Ship by <b id="c-shipby">{_esc(_day(next_by, today))}</b>'
        ship += ' <span class="late">late</span>' if late else ""
    else:
        ship = f'<span id="c-shipby">{"no ship-by date yet" if to_ship else "nothing to ship"}</span>'
    sites = "".join(f'<dt>{name}</dt><dd id="c-{key}">{live[key]}</dd>' for key, name in SITES)
    return "".join((
        '<section class="cards" aria-label="Summary">',
        f'<div class="card"><h2>Active listings</h2><dl class="sites">{sites}</dl></div>',
        f'<div class="card"><h2>Sold this month</h2><p class="num" id="c-sold">{sold}</p>',
        f'<p class="sub" id="c-sold-total">{_esc(_money(total))}</p></div>',
        f'<div class="card{" alert" if late else ""}"><h2>Awaiting shipment</h2>',
        f'<p class="num" id="c-toship">{to_ship}</p><p class="sub">{ship}</p></div>',
        f'<div class="card{" alert" if unmatched else ""}"><h2>Unmatched sales</h2>',
        f'<p class="num" id="c-unmatched">{unmatched}</p><p class="sub">matched no item</p></div>',
        _still_card(items),
        _drops_card(items, zone, now),
        "</section>",
    ))


def _still_card(items: list[dict]) -> str:
    """WO40: sold items still listed elsewhere 2 hours after the sale — shown until they're down."""
    still = [item for item in items if item.get("still_listed")]
    if not still:
        return ""
    lines = "".join(f'<p class="sub warn">{_esc(_title(item["title"]))} — {_esc(", ".join(item["still_listed"]))}</p>'
                    for item in still)
    return (f'<div class="card alert" id="c-still"><h2>Sold, still listed</h2><p class="num">{len(still)}</p>'
            f"{lines}<p class=\"sub\">mark it sold there</p></div>")


def _drops_card(items: list[dict], zone: tzinfo, now: datetime) -> str:
    """WO41: the price drops due in the next 7 days (live, not sold), the soonest first."""
    today = now.date()
    due = []
    for item in items:
        drops = item.get("drops") or {}
        day = _local_day(drops.get("next_drop_at"), zone) if drops.get("next_drop_at") else None
        if day is not None and (day - today).days <= 7 and _is_active(item):
            due.append((day, item))
    due.sort(key=lambda x: x[0])
    lines = "".join(f'<p class="sub">{_esc(_title(item["title"]))} — {_esc(_day(day, today))}</p>' for day, item in due[:8])
    return (f'<div class="card" id="c-drops"><h2>Drops due this week</h2><p class="num">{len(due)}</p>'
            f'{lines or "<p class=\"sub\">none</p>"}</div>')


def _drops_line(item: dict, zone: tzinfo, today: date) -> str:
    """WO41: under the item's title — live since (days), the drops made (day, old → new), the next drop's day."""
    drops = item.get("drops")
    if not drops or not drops.get("live_since") or not _is_active(item) and not drops["done"]:
        return ""
    bits = []
    since = _local_day(drops["live_since"], zone)
    if since is not None:
        bits.append(f"live since {_day(since, today)}" + (f" · {drops['days_live']} days" if drops["days_live"]
                                                          is not None else ""))
    seen = set()
    for d in drops["done"]:
        day = _local_day(d.get("done_at"), zone)
        key = (day, d.get("old_price"), d.get("new_price"))
        if key in seen:
            continue
        seen.add(key)
        bits.append(f"drop {_day(day, today) if day else ''} {_money(d.get('old_price'))} → {_money(d.get('new_price'))}")
    nxt = _local_day(drops.get("next_drop_at"), zone) if drops.get("next_drop_at") else None
    if drops.get("no_drop"):
        bits.append("no drops (owner)")
    elif nxt is not None and _is_active(item):
        bits.append(f"next drop {_day(nxt, today)}")
    elif drops["stage"] >= 2:
        bits.append("no more drops")
    return f' <span class="note dim">{_esc(" · ".join(bits))}</span>' if bits else ""


def _filters() -> str:
    buttons = "".join(f'<button type="button" data-f="{key}" aria-pressed="{"true" if key == "all" else "false"}">'
                      f"{label}</button>" for key, label in FILTERS)
    return f'<div class="filters" id="filters" role="group" aria-label="Filter items" hidden>{buttons}</div>'


def _table(items: list[dict], zone: tzinfo, today: date) -> str:
    head = "".join(f'<th scope="col" class="{cls}">{name}</th>' for cls, name in COLUMNS)
    span = len(COLUMNS)
    if items:
        rows = [f'<tr class="empty" id="none" hidden><td colspan="{span}">Nothing here</td></tr>']
        rows += [_row(item, zone, today) for item in items]
    else:
        rows = [f'<tr class="empty"><td colspan="{span}">Nothing yet</td></tr>']
    body = "\n".join(rows)
    return f'<div class="wrap"><table id="items"><thead><tr>{head}</tr></thead>\n<tbody>\n{body}\n</tbody></table></div>'


def _row(item: dict, zone: tzinfo, today: date) -> str:
    sale = item["sale"]
    flags = (("active", _is_active(item)), ("sold", _is_sold(sale)), ("toship", _to_ship(sale)))
    attrs = "".join(f' data-{name}="{int(on)}"' for name, on in flags)
    if item["id"] not in (None, ""):
        attrs = f' data-id="{_esc(item["id"])}"' + attrs
    cells = (
        _td("t", "Item", _esc(_title(item["title"])) + (
            f' <span class="warn still">⚠️ still listed on {_esc(", ".join(item["still_listed"]))}</span>'
            if item.get("still_listed") else "") + _drops_line(item, zone, today)),
        _td("p", "Price", _esc(_money((item.get("drops") or {}).get("price_now") or item["price"]))),
        *(_td("s", name, _site(item["sites"].get(key), name)) for key, name in SITES),
        _td("d sale", "Sold", _sold_cell(sale, zone, today)),
        _td("d", "Ship by", _ship_by_cell(sale, zone, today)),
        _td("d", "Shipping", _shipping_cell(sale, zone, today)),
    )
    return f"<tr{attrs}>{''.join(cells)}</tr>"


def _td(cls: str, label: str, inner: str) -> str:
    return f'<td class="{cls}" data-label="{label}">{inner}</td>'


def _icon(icon: str, cls: str, words: str) -> str:
    return f'<span class="st {cls}" role="img" aria-label="{_esc(words)}" title="{_esc(words)}">{icon}</span>'


def _site(entry: dict | None, name: str) -> str:
    """One site's cell: ✓ linking to the live listing, another status's icon, its own word if unknown, — if missing."""
    if entry is None:
        return _icon(*NOT_LISTED)
    status, url = entry["status"], entry["url"]
    if status == "posted":
        link = _http(url)
        if link:
            return (f'<a class="st ok" href="{_esc(link)}" target="_blank" rel="noopener noreferrer" '
                    f'title="Open on {name}" aria-label="Open on {name}">✓</a>')
        shown = f' <span class="raw">{_esc(url.strip())}</span>' if isinstance(url, str) and url.strip() else ""
        return _icon(*STATUS["posted"]) + shown
    if status in STATUS:
        return _icon(*STATUS[status])
    return f'<span class="st other">{_esc(status)}</span>'


def _sold_cell(sale: dict | None, zone: tzinfo, today: date) -> str:
    """Where and the local day it sold ("Poshmark · Thu Oct 9"), a cancelled or double sale said so."""
    if sale is None:
        return DASH
    where = _esc(_marketplace(sale["marketplace"]))
    day = _local_day(sale["sold_at"], zone)
    when = _day_html(day, today) if day else ""
    text = f'{where}<span class="sep"> · </span>{when}' if where and when else (where or when or "sold")
    if sale["status"] in SALE_NOTES:
        note, cls = SALE_NOTES[sale["status"]]
        text += f' <span class="note {cls}">{note}</span>'
    return text


def _ship_by_cell(sale: dict | None, zone: tzinfo, today: date) -> str:
    day = _local_day(sale["ship_by"], zone) if sale else None
    if day is None:
        return DASH
    text = _day_html(day, today)
    return f'<span class="late">{text} · late</span>' if _to_ship(sale) and day < today else text


def _shipping_cell(sale: dict | None, zone: tzinfo, today: date) -> str:
    """WO38: where the sale is — To ship / Shipped (with the day we recorded it) / Delivered / Completed — as its site
    last showed it or our records say; a sale without a shipping state as before: ✓ and the day it shipped."""
    if sale is None:
        return DASH
    state = sale.get("shipping")
    day = None if _blank(sale["shipped_at"]) else _local_day(sale["shipped_at"], zone)
    if state in SHIPPING:
        if state == "to_ship" and sale["status"] in CLOSED:
            return DASH
        words, cls = SHIPPING[state]
        when = f' <span class="sep">·</span> {_day_html(day, today)}' if day and state == "shipped" else ""
        return f'<span class="{cls}">{words}</span>{when}'
    if day is None:
        return DASH
    tick = '<span class="ok">✓</span>'
    return f"{tick} {_day_html(day, today)}"


def _footer(generated_at: object, zone: tzinfo, today: date, swept: object = None) -> str:
    when = _when(generated_at)
    if isinstance(when, datetime):
        local = when.astimezone(zone)
        stamp = f"{_day(local.date(), today)}, {_clock(local)} {local.tzname() or ''}".rstrip()
    elif isinstance(when, date):
        stamp = _day(when, today)
    else:
        stamp = DASH
    legend = "".join(f'<li><span class="st {cls}" aria-hidden="true">{icon}</span> {words}</li>'
                     for icon, cls, words in LEGEND)
    swept = swept if isinstance(swept, dict) else {}
    read = []
    for key, name in SITES:                          # WO38: when each site's orders were last read by the Mac
        moment = _when(swept.get(key))
        if isinstance(moment, datetime):
            local = moment.astimezone(zone)
            read.append(f"{name} {_day(local.date(), today)}, {_clock(local)}")
        else:
            read.append(f"{name} {DASH}")
    return (f'<footer><p id="updated">Updated {_esc(stamp)}</p>'
            f'<p id="swept">Orders read on the sites: {_esc(" · ".join(read))}</p>'
            f'<ul class="legend" aria-label="What the icons mean">{legend}</ul></footer>')


# --- values ------------------------------------------------------------------------------------------------------

def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _norm(value: object) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _blank(value: object) -> bool:
    return not value or (isinstance(value, str) and not value.strip())


def _title(value: object) -> str:
    text = value.strip() if isinstance(value, str) else ("" if value is None else str(value))
    return text or "(untitled)"


def _marketplace(value: object) -> str:
    return SITE_NAMES.get(_norm(value)) or (value.strip() if isinstance(value, str) else "")


def _http(url: object) -> str | None:
    """The URL when it is an absolute http(s) address — the only kind that becomes a link — else None."""
    if not isinstance(url, str):
        return None
    text = url.strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    return text if parts.scheme in ("http", "https") and parts.netloc else None


def _number(value: object) -> int | float | Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _money(value: object) -> str:
    """$35, $1,250, $12.50; — when there is no amount."""
    number = _number(value)
    if number is None:
        return DASH
    if number == int(number):
        return f"${int(number):,}"
    return f"${float(number):,.2f}"


def _count(value: object) -> int:
    number = _number(value)
    return max(int(number), 0) if number is not None else 0


def _zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return timezone.utc


def _when(value: object) -> datetime | date | None:
    """An ISO-8601 time as an aware datetime (no offset = UTC); a bare "YYYY-MM-DD" as a date; anything else None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _local_day(value: object, zone: tzinfo) -> date | None:
    """The owner's calendar day of a time; a bare date as it is."""
    when = _when(value)
    return when.astimezone(zone).date() if isinstance(when, datetime) else when


def _day(day: date, today: date) -> str:
    """A day as "Thu Oct 9", with ", 2025" added when its year is not today's."""
    text = f"{DAYS[day.weekday()]} {MONTHS[day.month - 1]} {day.day}"
    return text if day.year == today.year else f"{text}, {day.year}"


def _day_html(day: date, today: date) -> str:
    """The day for a table cell: kept on one line on wide screens."""
    return f'<span class="day">{_esc(_day(day, today))}</span>'


def _clock(moment: datetime) -> str:
    """A time of day as "3:05 PM"."""
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"
