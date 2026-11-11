"""WO60 — the evening shipping list (the owner, 2026-11-11: "Every evening at 10pm agent will send a beautifully
formatted message (a list) of all items that needs to be sent (either are due or not by tomorrow). Each item in the
list must have a deadline shipping date.").

Once a local day in the 22:00 hour New York time (the hourly timer, DST-safe through zoneinfo), ONE message to the
group — Tetiana packs — of every sale not yet shipped on all four sites, oldest ship-by first, in groups (🔴 Overdue,
🟠 Due today, 🟠 Due by tomorrow, 🟢 Later). Every line has a ship-by day: the sale's own (`sales.ship_by`: the email's
or eBay's date, else the site's rule), else worked out from the sale's day by the site's rule and marked "≈"; a sale
with no sale day at all counts from the day it was recorded (ONE private line about it). An order of several items (a
Poshmark bundle, an eBay order of several lines) is one block. Her own eBay sales (`core.hers`) are on it — they need
packing too — marked "(your listing)": title, price and ship-by only. Nothing open → "📦 Nothing to ship tonight — all
caught up ✨" (she knows it looked). More than 25 packages → several messages, never a block cut in two.

The day is claimed in `settings` (`ship_list_sent_on`, one conditional write) before anything is sent: a second run or
a retry in the hour sends nothing. The list goes with sound — the night hours' one exception, asked for at 10 PM —
unless `settings` holds `shipping.night_list.silent` = true. Azure's job: the Mac's quiet hours don't touch it.
Nothing of a buyer is read: only what `sales`, `items` and `orders` hold. Telegram HTML; every title escaped."""
from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from . import core, deadlines, util
from .db import Database
from .util import EBAY, iso, parse_time, site_name

log = logging.getLogger(__name__)

HOUR = 22                                    # 10 PM New York
SENT_ON = "ship_list_sent_on"                # settings: the local day the list went out
LAST = "ship_list_last"                      # settings: what the last list said (GET /health shows it)
SILENT = "shipping.night_list.silent"        # settings: "true" sends it silently; sound by default
NO_DAY = "ship_list_no_day"                  # reminders' kind: the one private line about a sale with no sale day
PER_MESSAGE = 25                             # packages in one message
MAX_CHARS = 3800                             # under Telegram's 4096, whatever the titles
TITLE_CHARS = 45
OPEN = ("cancelled", "double_sale", "done", "merged")       # the statuses that are never on the list
LEFT_ON_SITE = ("shipped", "delivered", "completed", "cancelled", "returned")   # the order sweep: on its way, or off
NOTHING = "📦 Nothing to ship tonight — all caught up ✨"
LABELS = {"poshmark": "Poshmark — the sale email", "ebay": "eBay — eBay app → Ship / Print label"}
GROUPS = (("overdue", "🔴", "Overdue"), ("today", "🟠", "Due today"), ("tomorrow", "🟠", "Due by tomorrow"),
          ("later", "🟢", "Later"))


@dataclass
class Sale:
    id: str
    site: str
    title: str
    price: float | None
    ship_by: date
    approx: bool                              # worked out here (the site's rule), not the sale's own date
    hers: bool                                # her own eBay listing (core.hers)


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


def short(title: str | None, limit: int = TITLE_CHARS) -> str:
    """The item's title, at most `limit` characters, cut at a word ("…" when cut)."""
    t = " ".join(str(title or "").split()) or "an item"
    if len(t) <= limit:
        return t
    cut = t[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-·&") or t[:limit - 1]
    return cut + "…"


def money(value: float | None) -> str:
    if value is None:
        return ""
    return f"${value:,.0f}" if abs(value - round(value)) < 0.005 else f"${value:,.2f}"


def _base_order(site: str, order_id: str | None) -> str | None:
    """The order a sale belongs to: eBay names each line of a several-item order "<order>:<line>"."""
    if not order_id:
        return None
    return order_id.split(":", 1)[0] if site == EBAY else order_id


def open_sales(db: Database, now: datetime) -> tuple[list[tuple[Sale, str | None]], list[dict]]:
    """Every sale still to ship as (Sale, its order), and the sales whose day had to come from their record (no sale
    day: the one private line)."""
    rows = db.query(
        "SELECT s.id, s.marketplace, s.item_id, s.title_seen, s.price, s.order_id, s.sold_at, s.ship_by, s.created_at, "
        "i.title AS item_title, o.status AS site_status "
        "FROM sales s LEFT JOIN items i ON i.id = s.item_id LEFT JOIN orders o ON o.sale_id = s.id "
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
        day = core.day_of(row["ship_by"]) if row["ship_by"] else None
        if day is None:
            sold = parse_time(row["sold_at"])
            if sold is None:
                sold = parse_time(row["created_at"]) or now
                no_day.append(row)
            try:
                day, _ = deadlines.ship_by(site, sold)
            except ValueError:                        # a site with no rule: its sale's own day, never no date
                day = deadlines.local(sold).date()
            approx = True
        sale = Sale(id=sid, site=site, title=short(row["item_title"] or row["title_seen"]),
                    price=float(row["price"]) if row["price"] is not None else None, ship_by=day, approx=approx,
                    hers=core.hers(site, row["item_id"]))
        out.append((sale, _base_order(site, row["order_id"])))
    return out, no_day


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


def block(n: int, pkg: Package, today: date) -> str:
    """One package: its number, title, site and price, then its ship-by line ("≈" when worked out here)."""
    site = site_name(pkg.site)
    due = f"   ⏰ ship-by {'≈ ' if pkg.approx else ''}<b>{deadlines.fmt_day(pkg.ship_by)}</b> — {when(pkg.ship_by, today)}"
    if len(pkg.sales) == 1:
        s = pkg.sales[0]
        name = _esc(s.title) + (" (your listing)" if s.hers else "")
        return "\n".join([f"{n}. {name} · {site}" + (f" · {money(s.price)}" if s.price is not None else ""), due])
    head = f"{n}. {site} order · {len(pkg.sales)} items" + (f" · {money(pkg.price)}" if pkg.price is not None else "")
    items = [f"   ▫️ {_esc(s.title)}" + (" (your listing)" if s.hers else "")
             + (f" · {money(s.price)}" if s.price is not None else "") for s in pkg.sales]
    return "\n".join([head, *items, due])


def labels_line(sites: set[str]) -> str | None:
    parts = []
    if "poshmark" in sites:
        parts.append(LABELS["poshmark"])
    apps = [site_name(s) for s in ("depop", "vinted") if s in sites]
    if apps:
        parts.append(f"{' / '.join(apps)} — the app")
    if EBAY in sites:
        parts.append(LABELS["ebay"])
    return f"🏷 Labels: {' · '.join(parts)}" if parts else None


def render(pkgs: list[Package], today: date) -> list[str]:
    """The message(s): the header, the groups in order (a group only when it has a package), each package one block
    numbered through, the labels line for tonight's sites; past 25 packages (or Telegram's length) the next message
    goes on — a group's heading again where it continues, never a block cut in two."""
    title = f"📦 <b>To ship — {deadlines.fmt_day(today)}</b>"
    if not pkgs:
        return [NOTHING]
    groups = [group_of(p.ship_by, today) for p in pkgs]
    count = {g: groups.count(g) for g, _, _ in GROUPS}
    summary = [f"{len(pkgs)} package{'s' if len(pkgs) != 1 else ''}"]
    summary += [f"{count[g]} {w}" for g, w in (("overdue", "overdue"), ("today", "due today"),
                                               ("tomorrow", "due by tomorrow")) if count[g]]
    labels = labels_line({p.site for p in pkgs})
    pages: list[list[str]] = [[title, " · ".join(summary)]]
    size, shown, last = len("\n".join(pages[0])), 0, None
    for n, (pkg, g) in enumerate(zip(pkgs, groups), start=1):
        emoji, name = next((e, w) for k, e, w in GROUPS if k == g)
        text = block(n, pkg, today)
        heading = f"\n{emoji} <b>{name}</b>"
        if shown == PER_MESSAGE or size + len(heading) + len(text) + 2 > MAX_CHARS:
            pages.append([f"{title} (continued)"])
            size, shown, last = len(pages[-1][0]), 0, None
        if g != last:
            pages[-1].append(heading)
            size += len(heading) + 1
            last = g
        pages[-1].append(text)
        size, shown = size + len(text) + 1, shown + 1
    if labels:
        if size + len(labels) + 2 > MAX_CHARS:
            pages.append([f"{title} (continued)"])
        pages[-1].append("\n" + labels)
    return ["\n".join(p) for p in pages]


def silent(db: Database) -> bool:
    return str(core.setting(db, SILENT) or "").strip().lower() in ("true", "1", "yes", "on")


def _claim(db: Database, day: str) -> bool:
    """The day taken for the list in ONE conditional write: True for exactly one run."""
    return bool(db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = "
                           "excluded.value WHERE settings.value IS NULL OR settings.value <> excluded.value",
                           (SENT_ON, day)))


def night_list(db: Database, now: datetime | None = None, mode: str | None = None, preview: bool = False) -> dict:
    """The timer's job (see the module): in the 22:00 hour, once a day, the list to the group. `preview` (WO60, the
    owner sees the look first): the same list now, to the private chat, marked — the day is not taken."""
    now, mode = (util.aware(now) if now else util.utcnow()), mode or util.mode()
    local = deadlines.local(now)
    if mode == "off":
        return {"sent": 0, "why": "off"}
    if not preview and local.hour != HOUR:
        return {"sent": 0, "why": f"sent in the {HOUR}:00 hour New York time"}
    sales, no_day = open_sales(db, now)
    pkgs = packages(sales)
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
        text = ("🔎 <b>Preview</b> — tonight's 10 PM list as the group will see it:\n\n" + page) if preview else page
        sent += bool(telegram.send(text, "ops" if preview else "group", now=now, mode=mode, html=True,
                                   silent=quiet_send if not preview else None))
    got = {"sent": sent, "packages": len(pkgs), "messages": len(pages), "preview": preview,
           "groups": {g: sum(1 for p in pkgs if group_of(p.ship_by, local.date()) == g) for g, _, _ in GROUPS}}
    if preview:
        got["pages"] = pages                          # the preview's answer carries its text (titles and prices only)
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
