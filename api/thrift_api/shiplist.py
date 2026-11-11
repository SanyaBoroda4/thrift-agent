"""WO60 — the evening shipping list (the owner, 2026-11-11: "Every evening at 10pm agent will send a beautifully
formatted message (a list) of all items that needs to be sent (either are due or not by tomorrow). Each item in the
list must have a deadline shipping date."); WO62 — its layout, take two ("It should be with links to each item so she
can click. And I want even more clear and nicer formatting.").

Once a local day in the 21:00 hour New York time (WO63, the owner: "lets send this list at 9pm actually"; the hour of
`settings` `shipping.night_list.at`, "21:00" by default — the hourly timer, its last job, after the 9 PM line;
DST-safe through zoneinfo), ONE message to the
group — Tetiana packs — of every sale not yet shipped on all four sites, oldest ship-by first. Every package has a
ship-by day: the sale's own (`sales.ship_by`: the email's or eBay's date, else the site's rule), else worked out from
the sale's day by the site's rule and marked "≈"; a sale with no sale day at all counts from the day it was recorded
(ONE private line about it). An order of several items (a Poshmark bundle, an eBay order of several lines) is one
block. Her own eBay sales (`core.hers`) are on it — they need packing too — "· your listing". Nothing open → "📦
Nothing to ship tonight — all caught up ✨" (she knows it looked).

WO62's layout (Telegram HTML; link previews off in `telegram.http_send`): "📦 SHIPPING LIST", the full date, the
non-zero counts; then each group that has a package — 🔴 LATE — SHIP FIRST · 🟠 SHIP TODAY · 🟠 SHIP TOMORROW · <day> ·
🟢 LATER — its title in bold capitals and its packages in ONE <blockquote> (Telegram draws its bar; spaces would be
dropped), a blank line between packages. A package: "1. <b><a href=listing>title</a></b> · size", "Site · $120 · sold
Mon Nov 3", "⏰ Was due … · N days late" (LATE) or "⏰ Ship by … · in N days" (LATER; in TODAY / TOMORROW only for a day
worked out here), and "🏷 Order & label" linking that sale's own order page. Links are only addresses we hold or a
pattern verified on the site itself (`listing_url`, `order_url`) — never one made up: no address, no link. Past 25
packages or near Telegram's 4,096 characters the next message goes on "📦 SHIPPING LIST (continued)", never inside a
package. The footer: a thin line and "✓ Shipped ones drop off by themselves once the site shows them on the way."

The day is claimed in `settings` (`ship_list_sent_on`, one conditional write) before anything is sent: a second run or
a retry in the hour sends nothing. The list goes with sound (9 PM is before the night hours; at 10 PM or later it
would be their one exception) unless `settings` holds `shipping.night_list.silent` = true. Azure's job: the Mac's
quiet hours don't touch it.
Nothing of a buyer is read: only what `sales`, `items`, `listings`, `orders` and the sale email's parsed links hold."""
from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from . import core, deadlines, emails, util
from .db import Database
from .util import EBAY, iso, parse_time, site_name

log = logging.getLogger(__name__)

AT = "shipping.night_list.at"                # settings: "21:00" — the hour the list goes (WO63)
DEFAULT_AT = "21:00"
SENT_ON = "ship_list_sent_on"                # settings: the local day the list went out
LAST = "ship_list_last"                      # settings: what the last list said (GET /health shows it)
SILENT = "shipping.night_list.silent"        # settings: "true" sends it silently; sound by default
NO_DAY = "ship_list_no_day"                  # reminders' kind: the one private line about a sale with no sale day
PER_MESSAGE = 25                             # packages in one message
MAX_CHARS = 3800                             # the text Telegram counts (tags and addresses aside), under its 4,096
TITLE_CHARS = 40
LEFT_ON_SITE = ("shipped", "delivered", "completed", "cancelled", "returned")   # the order sweep: on its way, or off
NOTHING = "📦 <b>Nothing to ship tonight</b> — all caught up ✨"
FOOTER = "──────────────\n✓ Shipped ones drop off by themselves once the site shows them on the way."
LABEL_LINK = "🏷 Order &amp; label"
SOLD_LINK = "🏷 Sold orders &amp; labels"

# WO62 — each site's own order page, only as verified on the site (never a made-up address):
ORDER_PAGES = {
    # the sale emails' own links (emails.ORDER_URLS, from Poshmark's real sale emails): the order and its label
    "poshmark": ("https://poshmark.com/order/sales/{id}", re.compile(r"[0-9a-f]{24}")),
    # the receipts' purchase_id IS the id in the sold list's links /sellinghub/sold-items/<id>/ (WO38, live 2026-10-14)
    "depop": ("https://www.depop.com/sellinghub/sold-items/{id}/", re.compile(r"[A-Za-z0-9_-]{3,40}")),
    # Seller Hub's order page — opened 2026-11-11 for a real order of the shop (21-15038-17628: "Order details",
    # its order number, its label controls); /sh/ord/details?orderid= lands here
    EBAY: ("https://www.ebay.com/mesh/ord/details?orderid={id}", re.compile(r"\d{2}-\d{5}-\d{5}")),
}
# No per-order page verified for Vinted (its sweep keeps the transaction id, but no order page was ever opened):
# the sold list, verified by the order sweep (WO38: my_orders, 200) — said in the report
SOLD_PAGES = {"poshmark": "https://poshmark.com/order/sales",
              "depop": "https://www.depop.com/sellinghub/sold-items/",
              "vinted": "https://www.vinted.com/my_orders?order_type=sold"}
# A listing by its id where no address is held — each form verified (CLAUDE.md "Verified on the live sites")
LISTING_PAGES = {"poshmark": "https://poshmark.com/listing/{id}",          # redirects to /listing/<slug>-<id>
                 "vinted": "https://www.vinted.com/items/{id}",
                 "depop": "https://www.depop.com/products/{id}/",
                 EBAY: "https://www.ebay.com/itm/{id}"}
GROUPS = (("overdue", "🔴", "LATE — SHIP FIRST"), ("today", "🟠", "SHIP TODAY"), ("tomorrow", "🟠", "SHIP TOMORROW"),
          ("later", "🟢", "LATER"))
COUNT_WORDS = {"overdue": ("🔴", "late"), "today": ("🟠", "today"), "tomorrow": ("🟠", "tomorrow"),
               "later": ("🟢", "later")}
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
SIZE_TAIL = re.compile(r"[\s,]*\b(?:(?:toddler|little kid|big kid|kids?|baby|women'?s|men'?s|girls'?|boys'?)\s+)?"
                       r"size\s+(?P<size>[\w./()-]+)\b.*$", re.I)
NWT_TAIL = re.compile(r"[\s,]*\bNew with(?:out)? Tags\s*$", re.I)


@dataclass
class Sale:
    id: str
    site: str
    title: str
    size: str | None
    price: float | None
    ship_by: date
    approx: bool                              # worked out here (the site's rule), not the sale's own date
    hers: bool                                # her own eBay listing (core.hers)
    sold: date | None = None
    url: str | None = None                    # the listing on its site
    order_url: str | None = None              # that sale's own order page (or the site's sold list)
    order_page: bool = False                  # True: its own order page; False: the sold list


@dataclass
class Package:
    """One thing to pack: a sale, or the sales of one order (a bundle)."""
    site: str
    sales: list[Sale] = field(default_factory=list)

    @property
    def ship_by(self) -> date:
        return min(s.ship_by for s in self.sales)

    @property
    def approx(self) -> bool:
        return any(s.approx for s in self.sales if s.ship_by == self.ship_by)

    @property
    def price(self) -> float | None:
        known = [s.price for s in self.sales if s.price is not None]
        return sum(known) if known else None

    @property
    def sold(self) -> date | None:
        days = [s.sold for s in self.sales if s.sold]
        return min(days) if days else None

    @property
    def order(self) -> tuple[str | None, bool]:
        own = next((s.order_url for s in self.sales if s.order_url and s.order_page), None)
        return (own, True) if own else (next((s.order_url for s in self.sales if s.order_url), None), False)


def short(title: str | None, limit: int = TITLE_CHARS) -> str:
    """The item's title, at most `limit` characters, cut at a word ("…" when cut)."""
    t = " ".join(str(title or "").split()) or "an item"
    if len(t) <= limit:
        return t
    cut = t[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-·&") or t[:limit - 1]
    return cut + "…"


def title_and_size(title: str | None, size: str | None) -> tuple[str, str | None]:
    """WO62: the title without its size (and without ", New with Tags"), cut to 40 at a word; the size apart — the
    item's own, else the one the title named."""
    t = " ".join(str(title or "").split())
    named = None
    if (m := SIZE_TAIL.search(t)) is not None:
        named, t = m.group("size"), t[:m.start()]
    t = NWT_TAIL.sub("", t).strip(" ,;:-·")
    size = " ".join(str(size or "").split()) or named
    return short(t), (size or None)


def money(value: float | None) -> str:
    if value is None:
        return ""
    return f"${value:,.0f}" if abs(value - round(value)) < 0.005 else f"${value:,.2f}"


def _base_order(site: str, order_id: str | None) -> str | None:
    """The order a sale belongs to: eBay names each line of a several-item order "<order>:<line>"."""
    if not order_id:
        return None
    return order_id.split(":", 1)[0] if site == EBAY else order_id


def listing_url(site: str, held: str | None, email_url: str | None, listing_id: str | None) -> str | None:
    """The listing's address: the one recorded (the ✅ card's), else the sale email's own, else built from its id by a
    verified form — each checked to be that site's listing address; else None (never made up)."""
    for candidate in (held, email_url):
        if candidate:
            found, _ = emails.listing_ref(site, str(candidate))
            if found:
                return found
    lid = str(listing_id or "").strip()
    if lid and site in LISTING_PAGES:
        url = LISTING_PAGES[site].format(id=lid)
        found, _ = emails.listing_ref(site, url)
        if found:
            return url
    return None


def order_url(site: str, *ids: str | None) -> tuple[str | None, bool]:
    """(that sale's own order page, True) by a verified pattern and an id of its shape; else (the site's sold list,
    False); else (None, False)."""
    page = ORDER_PAGES.get(site)
    if page is not None:
        for raw in ids:
            ident = _base_order(site, str(raw).strip()) if raw else None
            if ident and page[1].fullmatch(ident):
                return page[0].format(id=ident), True
    return SOLD_PAGES.get(site), False


def open_sales(db: Database, now: datetime) -> tuple[list[tuple[Sale, str | None]], list[dict]]:
    """Every sale still to ship as (Sale, its order), and the sales whose day had to come from their record (no sale
    day: the one private line)."""
    rows = db.query(
        "SELECT s.id, s.marketplace, s.item_id, s.listing_id, s.title_seen, s.price, s.order_id, s.sold_at, s.ship_by, "
        "s.created_at, i.title AS item_title, i.size AS item_size, l.url AS held_url, o.status AS site_status, "
        "o.order_id AS site_order_id, e.parsed_json "
        "FROM sales s LEFT JOIN items i ON i.id = s.item_id "
        "LEFT JOIN listings l ON l.item_id = s.item_id AND l.marketplace = s.marketplace "
        "LEFT JOIN orders o ON o.sale_id = s.id "
        "LEFT JOIN email_events e ON e.message_id = s.message_id "
        "WHERE s.shipped_at IS NULL AND s.status NOT IN ('cancelled', 'double_sale', 'done', 'merged') "
        "ORDER BY s.ship_by, s.sold_at, s.id")
    by_id: dict[str, dict] = {}
    gone: set[str] = set()
    for row in rows:
        if row["site_status"] in LEFT_ON_SITE:
            gone.add(row["id"])                       # the site shows it on its way (or not to be shipped)
        by_id.setdefault(row["id"], row)
    out, no_day = [], []
    for sid, row in by_id.items():
        if sid in gone:
            continue
        site = str(row["marketplace"] or "").lower()
        approx = False
        sold = parse_time(row["sold_at"])
        day = core.day_of(row["ship_by"]) if row["ship_by"] else None
        if day is None:
            start = sold
            if start is None:
                start = parse_time(row["created_at"]) or now
                no_day.append(row)
            try:
                day, _ = deadlines.ship_by(site, start)
            except ValueError:                        # a site with no rule: its sale's own day, never no date
                day = deadlines.local(start).date()
            approx = True
        parsed = _parsed(row["parsed_json"])
        title, size = title_and_size(row["item_title"] or row["title_seen"] or parsed.get("title"), row["item_size"])
        own, is_page = order_url(site, row["site_order_id"], row["order_id"], parsed.get("order_id"))
        sale = Sale(id=sid, site=site, title=title, size=size,
                    price=float(row["price"]) if row["price"] is not None else None, ship_by=day, approx=approx,
                    hers=core.hers(site, row["item_id"]), sold=deadlines.local(sold).date() if sold else None,
                    url=listing_url(site, row["held_url"], parsed.get("listing_url"),
                                    row["listing_id"] or parsed.get("listing_id")),
                    order_url=own, order_page=is_page)
        out.append((sale, _base_order(site, row["order_id"] or row["site_order_id"])))
    return out, no_day


def _parsed(text: str | None) -> dict:
    try:
        got = json.loads(text) if text else {}
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def packages(sales: list[tuple[Sale, str | None]]) -> list[Package]:
    """The sales as packages — one per order (a bundle's sales together) — oldest ship-by first, then the order the
    sales came in (the oldest sale first)."""
    by_order: dict[tuple[str, str], Package] = {}
    out: list[Package] = []
    for sale, order in sales:
        if order is not None and (sale.site, order) in by_order:
            by_order[(sale.site, order)].sales.append(sale)
            continue
        pkg = Package(site=sale.site, sales=[sale])
        out.append(pkg)
        if order is not None:
            by_order[(sale.site, order)] = pkg
    return sorted(out, key=lambda p: p.ship_by)                    # stable: the query's sale order within a day


def group_of(ship_by: date, today: date) -> str:
    left = (ship_by - today).days
    return "overdue" if left < 0 else "today" if left == 0 else "tomorrow" if left == 1 else "later"


def when(ship_by: date, today: date) -> str:
    left = (ship_by - today).days
    if left < 0:
        return f"{-left} day{'s' if left < -1 else ''} late"
    return "today" if left == 0 else "tomorrow" if left == 1 else f"in {left} days"


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _link(url: str | None, inner: str) -> str:
    """`inner` (already HTML) linked to `url` — the address only in href, quoted and escaped; no address, no link."""
    return f'<a href="{html.escape(url, quote=True)}">{inner}</a>' if url else inner


def _name(s: Sale) -> str:
    return f"<b>{_link(s.url, _esc(s.title))}</b>" + (f" · {_esc(s.size)}" if s.size else "")


def long_day(d: date) -> str:
    return f"{DAYS[d.weekday()]}, {MONTHS[d.month - 1]} {d.day}"


def block(n: int, pkg: Package, today: date) -> str:
    """One package (WO62): its title line(s), the site line, the ⏰ line where the group doesn't say the day, the
    order & label link."""
    lines = []
    if len(pkg.sales) == 1:
        lines.append(f"{n}. {_name(pkg.sales[0])}")
    else:
        lines.append(f"{n}. <b>{len(pkg.sales)} items · one order</b>")
        lines += [f"• {_name(s)}" for s in pkg.sales]
    hers = all(s.hers for s in pkg.sales)
    site = site_name(pkg.site) + (" · your listing" if hers else "")
    lines.append(" · ".join(x for x in (site, money(pkg.price), f"sold {deadlines.fmt_day(pkg.sold)}" if pkg.sold
                                        else "") if x))
    g, approx = group_of(pkg.ship_by, today), "≈ " if pkg.approx else ""
    day = deadlines.fmt_day(pkg.ship_by)
    if g == "overdue":
        lines.append(f"⏰ Was due {approx}{day} · {when(pkg.ship_by, today)}")
    elif g == "later" or pkg.approx:                  # TODAY / TOMORROW say the day in their title
        lines.append(f"⏰ Ship by {approx}{day} · {when(pkg.ship_by, today)}")
    url, own = pkg.order
    if url:
        lines.append(_link(url, LABEL_LINK if own else SOLD_LINK))
    return "\n".join(lines)


def counts_line(pkgs: list[Package], today: date) -> str:
    groups = [group_of(p.ship_by, today) for p in pkgs]
    parts = [f"{len(pkgs)} package{'s' if len(pkgs) != 1 else ''}"]
    parts += [f"{COUNT_WORDS[g][0]} {groups.count(g)} {COUNT_WORDS[g][1]}" for g, _, _ in GROUPS if groups.count(g)]
    return "  ·  ".join(parts)


def group_title(g: str, today: date) -> str:
    emoji, name = next((e, w) for k, e, w in GROUPS if k == g)
    if g == "tomorrow":
        name += f" · {deadlines.fmt_day(date.fromordinal(today.toordinal() + 1))}"
    return f"{emoji}  <b>{name}</b>"


def visible(text: str) -> int:
    """The length Telegram counts: the text without its tags, entities read."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def render(pkgs: list[Package], today: date) -> list[str]:
    """The message(s) (WO62's layout — see the module)."""
    if not pkgs:
        return [NOTHING]
    head = ["📦  <b>SHIPPING LIST</b>", long_day(today), counts_line(pkgs, today)]
    cont = ["📦  <b>SHIPPING LIST</b> (continued)"]
    pages: list[list[str]] = []
    page: list[str] = list(head)
    group, blocks, shown = None, [], 0

    def close() -> None:
        if blocks:
            page.append("<blockquote>" + "\n\n".join(blocks) + "</blockquote>")

    def size_with(extra: str) -> int:
        return visible("\n".join([*page, "<blockquote>" + "\n\n".join([*blocks, extra]) + "</blockquote>"]))

    for n, pkg in enumerate(pkgs, start=1):
        g = group_of(pkg.ship_by, today)
        text = block(n, pkg, today)
        title = "" if g == group else "\n" + group_title(g, today)
        if shown and (shown == PER_MESSAGE or size_with(title + text) + visible(FOOTER) + 2 > MAX_CHARS):
            close()
            pages.append(page)
            page, blocks, shown = list(cont), [], 0
            title = "\n" + group_title(g, today)       # the group's title again where it goes on
        elif g != group:
            close()
            blocks = []
        if title:
            page.append(title)
        group = g
        blocks.append(text)
        shown += 1
    close()
    page.append("\n" + FOOTER)
    pages.append(page)
    return ["\n".join(p) for p in pages]


def send_hour(db: Database) -> int:
    """WO63: the local hour the list goes — the hour of `shipping.night_list.at` ("21:00"; the hourly timer runs on
    the hour, so the minutes don't count); a value that isn't a time of day keeps 21."""
    raw = str(core.setting(db, AT) or DEFAULT_AT).strip()
    try:
        hour = int(raw.split(":", 1)[0])
    except ValueError:
        return int(DEFAULT_AT[:2])
    return hour if 0 <= hour <= 23 else int(DEFAULT_AT[:2])


def silent(db: Database) -> bool:
    return str(core.setting(db, SILENT) or "").strip().lower() in ("true", "1", "yes", "on")


def _claim(db: Database, day: str) -> bool:
    """The day taken for the list in ONE conditional write: True for exactly one run."""
    return bool(db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = "
                           "excluded.value WHERE settings.value IS NULL OR settings.value <> excluded.value",
                           (SENT_ON, day)))


PREVIEW = "🔎 <b>Preview</b> — tonight's list as the group will see it:\n\n"
SAMPLE = "🔎 <b>Preview (sample)</b> — nothing is open now; the look, with made-up sales:\n\n"


def sample(today: date) -> list[Package]:
    """WO62's made-up sales (the owner sees the look when nothing is open) — no address: the links show as text."""
    def day(n: int) -> date:
        return date.fromordinal(today.toordinal() + n)
    rows = [("poshmark", "Tory Burch Croc Loafers", "8.5", 120, -1, -8), ("ebay", "Zara TRF Leather Moto Jacket", "S",
            161, 1, -1), ("depop", "Birkenstock Rio Sandals", "38", 49, 1, -4),
            ("vinted", "Moncler Down Snowsuit", "9-12M", 76, 6, -2)]
    return [Package(site, [Sale(f"sample{n}", site, t, size, float(p), day(due), False, False, sold=day(sold),
                                order_url=None)]) for n, (site, t, size, p, due, sold) in enumerate(rows)]


def night_list(db: Database, now: datetime | None = None, mode: str | None = None, preview: bool = False) -> dict:
    """The timer's job (see the module): in the hour of `send_hour` (21:00), once a day, the list to the group.
    `preview` (WO60, the owner sees the look first): the same list now, to the private chat, marked — the day is not
    taken; with nothing open, WO62's made-up sales marked "(sample)"."""
    now, mode = (util.aware(now) if now else util.utcnow()), mode or util.mode()
    local = deadlines.local(now)
    if mode == "off":
        return {"sent": 0, "why": "off"}
    hour = send_hour(db)
    if not preview and local.hour != hour:
        return {"sent": 0, "why": f"sent in the {hour}:00 hour New York time"}
    sales, no_day = open_sales(db, now)
    pkgs = packages(sales)
    mark = PREVIEW
    if preview and not pkgs:
        pkgs, mark = sample(local.date()), SAMPLE
    pages = render(pkgs, local.date())
    today = local.date().isoformat()
    out = core.Outbox(now, mode)
    with db.tx():
        if not preview and not _claim(db, today):
            return {"sent": 0, "why": "already sent today"}
        for row in no_day:
            if not preview and db.execute("INSERT INTO reminders (sale_id, kind, sent_at) VALUES (?, ?, ?) "
                                          "ON CONFLICT (sale_id, kind) DO NOTHING", (row["id"], NO_DAY, iso(now))):
                out.ops(f"📦 {site_name(row['marketplace'])} sale “{short(row['item_title'] or row['title_seen'])}” has "
                        f"no sale day — tonight's list counts its ship-by from the day it was recorded ({row['id']}).")
    sent = out.flush()
    from . import telegram
    quiet_send = silent(db)
    for page in pages:
        sent += bool(telegram.send((mark + page) if preview else page, "ops" if preview else "group", now=now, mode=mode,
                                   html=True, silent=quiet_send if not preview else None))
    got = {"sent": sent, "packages": len(pkgs), "messages": len(pages), "preview": preview,
           "groups": {g: sum(1 for p in pkgs if group_of(p.ship_by, local.date()) == g) for g, _, _ in GROUPS},
           "links": {"listing": sum(1 for p in pkgs for s in p.sales if s.url),
                     "order_page": sum(1 for p in pkgs if p.order[1]),
                     "sold_list": sum(1 for p in pkgs if p.order[0] and not p.order[1])}}
    if preview:
        got["pages"] = pages                          # the preview's answer carries its text (titles and prices only)
        got["sample"] = mark == SAMPLE
        return got
    with db.tx():
        core.put_setting(db, LAST, json.dumps({"at": iso(now), "day": today, **got}))
    log.info("ship list %s: %d package(s) in %d message(s), %d sent", today, len(pkgs), len(pages), sent)
    return got


def status(db: Database, now: datetime | None = None) -> dict:
    """For GET /health: the day the list last went, what it said, and how many packages are open now."""
    now = util.aware(now) if now else util.utcnow()
    last = core.setting(db, LAST)
    try:
        last = json.loads(last) if last else None
    except ValueError:
        last = None
    return {"sent_on": core.setting(db, SENT_ON), "last": last, "open_packages": len(packages(open_sales(db, now)[0]))}
