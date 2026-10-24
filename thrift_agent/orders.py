"""WO38 — the sites' sold orders, read only (none of the three sites emails the seller when an order ships, so the
reminders, the overdue line and the dashboard's "to ship" must follow what each site itself says).

Section 1, the diagnostic instrument (`probe`): each site's sold-orders page read in a tab of its own — Depop and
Vinted through the Thrift Chrome's extension (an "orders" job, in a separate "Orders" window), Poshmark as a second page
of its own browser (never the poster's page) — nothing clicked, and every JSON the page itself loaded read again
same-origin (GET only). Buyer data is never kept: `scrub` keeps a document's shape and only the values of fields that
cannot name a person (ids, status words, titles, prices, dates, the listing's address); a buyer, an address, a name,
a tracking number, a message are "<scrubbed>" — in the extension too (common.js T.scrub), before anything leaves the
page."""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from thrift_agent import power
from thrift_agent.config import Settings

SITES = ("poshmark", "depop", "vinted")
# (Poshmark's /news/order is never opened: its page marks the order notifications as seen when it opens — WO38 §1)
URLS = {"poshmark": "https://poshmark.com/order/sales",
        "depop": "https://www.depop.com/sellinghub/sold-items/",
        "vinted": "https://www.vinted.com/my_orders?order_type=sold"}
SCRUBBED = "<scrubbed>"
# The same rules as the page's (ext/content/common.js T.scrub): a key is cut into its words (camelCase and snake_case
# alike); a value under a DENY word is "<scrubbed>" (its whole subtree), a string is kept only under a SAFE word.
DENY_WORDS = frozenset((
    "buyer buyers seller sellers user users username usernames login email emails mail phone address addresses street "
    "city zip zipcode postal postcode recipient recipients name names firstname lastname fullname nickname tracking "
    "carrier photo photos avatar avatars picture pictures image images thumb thumbnail thumbnails message messages "
    "body note notes comment comments feedback review reviews token tokens secret password signature lat lng latitude "
    "longitude location geo ip").split())
SAFE_WORDS = frozenset((
    "id ids uuid status state statuses title price amount currency total subtotal fee fees earning earnings payout "
    "date time at created updated paid sold shipped sent delivered completed cancelled canceled refunded returned type "
    "kind count size brand sku slug url path href code transaction order item items listing product variant quantity "
    "condition category val value step stage phase display text label").split())
FILE_WORDS = frozenset({"url", "pdf", "file", "link", "download"})
ORDER_VOCAB = frozenset((
    "a an the to be is was has have been on in out of for by at its it your you this that and or not yet no all new "
    "view details more order orders ordered sold sale sales item items paid payment received pending processing "
    "awaiting await waiting ship shipped shipping shipment sent send parcel package label labels printed print "
    "generated ready transit delivered delivery deliver arrived complete completed completion cancelled canceled "
    "cancel cancellation returned return returns refund refunded dispute disputed issue reported open opened closed "
    "status tracking track way dropped off picked up confirm confirmed accept accepted rate rated rating left day days "
    "hour hours minute minutes ago today tomorrow yesterday overdue late buyer seller mark marked as expected "
    "estimated date due jan feb mar apr may jun jul aug sep sept oct nov dec january february march april june july "
    "august september october november december mon tue wed thu fri sat sun am pm est edt").split())


def words_of(key: str) -> list[str]:
    """"buyerUsername" → ["buyer", "username"]; "display_status" → ["display", "status"]."""
    return [w for w in re.split(r"[^a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).lower()) if w]


def mask(text: str) -> str:
    """E-mail addresses, @handles and long numbers (a tracking number) out of a kept string."""
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", str(text))
    text = re.sub(r"@[\w.-]+", "@<user>", text)
    return re.sub(r"\d{15,}", "<long number>", text)


def scrub(obj: object, key: str = "", depth: int = 0, max_list: int = 8) -> object:
    """`obj` with its shape kept and nothing that could name a person: a value under a DENY word is "<scrubbed>" (an
    is_/has_/can_ flag and a *_count / *_at / *_date aside), a label's file too; a string is kept only under a SAFE
    word, masked, 120 characters at most — any other string is "<str>"; lists keep `max_list` elements."""
    words = words_of(key) if key else []
    flag = bool(words) and (words[0] in ("is", "has", "can") or words[-1] in ("count", "at", "date"))
    file_of_label = "label" in words and bool(FILE_WORDS & set(words))
    if file_of_label or (DENY_WORDS & set(words) and not flag):
        return obj if obj in (None, "") else SCRUBBED
    if depth > 12:
        return "<deep>"
    if isinstance(obj, dict):
        return {str(k): scrub(v, str(k), depth + 1, max_list) for k, v in obj.items()}
    if isinstance(obj, list):
        out = [scrub(v, key, depth + 1, max_list) for v in obj[:max_list]]
        return out + ([f"<{len(obj) - max_list} more>"] if len(obj) > max_list else [])
    if isinstance(obj, str):
        return mask(obj)[:120] if SAFE_WORDS & set(words) else "<str>"
    return obj


def status_texts(texts: list[str], limit: int = 40) -> list[str]:
    """The short texts that say where an order is ("Awaiting shipment", "Order complete"), each once — kept only when
    every word is an order word, a number, a price or a date: a person's name never passes."""
    out: list[str] = []
    for raw in texts:
        t = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not t or len(t) > limit or t in out:
            continue
        words = [w for w in re.split(r"[\s,·•|()]+", t.lower()) if w]
        # a number is a price, a date, a count — 10 characters at most: a tracking number never passes
        numeric = [bool(re.fullmatch(r"[#$]?\d[\d,.:/-]{0,9}", w)) for w in words]
        if all(n or w.rstrip(".:!?") in ORDER_VOCAB for w, n in zip(words, numeric)) and not all(numeric):
            out.append(t)
    return out


# ---------------------------------------------------------------- Poshmark: a second page of its own browser

STATE_MARK = re.compile(r"__INITIAL_STATE__\s*=\s*")
FETCH_TEXT = "u => fetch(u, {credentials: 'include'}).then(r => r.text())"
ROWS_JS = """() => [...document.querySelectorAll("a[href^='/order/sales/']")].map((a) => {
  const row = a.closest('tr, li, [class*=order], [class*=card]') || a;
  return {href: a.getAttribute('href'), cells: [...row.querySelectorAll('td, span, div, p')]
    .filter((n) => !n.childElementCount).map((n) => n.textContent.trim()).filter(Boolean).slice(0, 40)};
})"""


def parse_state(html: str) -> dict:
    m = STATE_MARK.search(html or "")
    if not m:
        raise ValueError("no __INITIAL_STATE__ on page")
    obj, _ = json.JSONDecoder().raw_decode(html, m.end())
    return obj


async def probe_poshmark(ctx, max_orders: int = 8) -> dict:
    """Section 1 on Poshmark: a NEW page of the poster's browser (never its posting page) — the sold-orders list
    (/order/sales → /seller-hub/sales: its rows, their status texts, its own state, the JSON answers the page itself
    receives), then each of the first `max_orders` orders' own page as JSON (same-origin GET,
    `$_order_details.order`), every document scrubbed. Nothing is clicked. Closed after."""
    page = await ctx.new_page()
    out: dict = {"site": "poshmark", "read_at": _now(), "pages": {}, "captured": []}
    pending: list[asyncio.Future] = []

    async def grab(resp) -> None:
        """One JSON answer the page itself received (its own calls; nothing is asked again), scrubbed."""
        try:
            if resp.request.resource_type not in ("xhr", "fetch") or "json" not in (resp.headers.get("content-type") or ""):
                return
            out["captured"].append({"url": shape(resp.url), "http": resp.status, "json": scrub(await resp.json(),
                                                                                                max_list=4)})
        except Exception as e:  # noqa: BLE001 — a body that can't be read is only counted
            out["captured"].append({"url": shape(resp.url), "error": type(e).__name__})

    page.on("response", lambda resp: pending.append(asyncio.ensure_future(grab(resp))))
    try:
        for name, url in (("sales", URLS["poshmark"]),):
            resp = await page.goto(url)
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(2500)
            html = await page.content()
            entry: dict = {"url": page.url, "http": resp.status if resp else None, "title": await page.title()}
            try:
                entry["state"] = scrub(parse_state(html))
            except ValueError as e:
                entry["state_error"] = str(e)
            if name == "sales":
                rows = await page.evaluate(ROWS_JS)
                entry["rows"] = [{"href": r["href"], "status_texts": status_texts(r["cells"])} for r in rows[:30]]
            entry["status_texts"] = status_texts(await page.evaluate(
                "() => [...document.querySelectorAll('body *')].filter((n) => !n.childElementCount)"
                ".map((n) => n.textContent)"))
            out["pages"][name] = entry
        hrefs = list(dict.fromkeys(r["href"] for r in out["pages"]["sales"].get("rows", [])
                                   if r.get("href") and "?" not in r["href"]))     # ?action=messageModal: the same order
        orders = []
        for href in hrefs[:max_orders]:
            try:
                state = parse_state(await page.evaluate(FETCH_TEXT, href))
                order = (state.get("$_order_details") or {}).get("order") or {}
                orders.append({"href": href, "order": scrub(order, max_list=2)})
            except Exception as e:  # noqa: BLE001 — a page that can't be read is said, never a reason to stop
                orders.append({"href": href, "error": f"{type(e).__name__}: {str(e)[:120]}"})
        out["orders"] = orders
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await page.close()
    return out


def shape(url: str) -> str:
    """An address as the probe keeps it: host and path (long numbers masked), the query's keys only."""
    from urllib.parse import parse_qsl, urlsplit
    u = urlsplit(url)
    keys = "&".join(k for k, _ in parse_qsl(u.query, keep_blank_values=True))
    return f"{u.netloc}{re.sub(r'[0-9]{6,}', '<n>', u.path)}" + (f"?{keys}" if keys else "")


# ---------------------------------------------------------------- Depop, Vinted: the extension's "orders" job

# Probe 2: the site's own JSON of the sold orders, read once with GET (Depop's from its API host, the way its page does)
CANDIDATES = {"depop": ["https://webapi.depop.com/presentation/api/v1/receipts/?role=seller&limit=12"],
              "vinted": ["https://www.vinted.com/api/v2/my_orders?type=sold&status=all&page=1&per_page=20"]}


async def probe_extension(poster, url: str, timeout: float = 150, known: list[str] | None = None) -> dict:
    """Section 1 on Depop / Vinted: the extension's "orders" job (ext/content/common.js T.orders) in the Thrift
    Chrome's "Orders" window — the page read, nothing clicked: the site's own JSON of the orders (CANDIDATES, scrubbed
    in the page), the order rows as drawn (links, status words), the status words and data-testids it shows. A row's
    title leaves the page only when it is one of `known` (our titles): anything else could be a person's name. {"error",
    "page"} when it couldn't."""
    ev = await poster._job("orders", {"orders_url": url, "probe": True, "candidates": CANDIDATES.get(poster.name, []),
                                      "known": known or []}, timeout)
    keep = ("url", "title", "resources", "documents", "status_texts", "testids", "links", "candidates", "rows",
            "message", "page", "stage", "event")
    out = {"site": poster.name, "read_at": _now(), **{k: ev.get(k) for k in keep if k in ev}}
    for row in out.get("rows") or []:                 # the page kept only ours; checked again here
        if not any(same_words(row.get("title") or "", t) for t in known or []):
            row["title"] = "<other>"
    return out


def same_words(a: str, b: str, share: float = 0.6) -> bool:
    """Two titles with most of their words in common (case and punctuation aside): a row shows ours, maybe cut."""
    wa, wb = set(re.findall(r"[a-z0-9]+", (a or "").lower())), set(re.findall(r"[a-z0-9]+", (b or "").lower()))
    return bool(wa and wb) and len(wa & wb) >= share * min(len(wa), len(wb)) and len(wa & wb) >= 2


# ---------------------------------------------------------------- the diagnostic run

async def probe(s: Settings, sites: list[str]) -> dict:
    """`thrift orders --probe`: each site's sold orders read (the poster stopped: its browser profile, the bridge's
    port), every result saved scrubbed to <control>/orders_probe/<site>.json. Returns {site: result}."""
    from thrift_agent.post import takedown
    known = known_titles(s)
    ps, bridge, pw, ctx = await takedown._open(s, sites)
    results = {}
    try:
        for mp in sites:
            try:
                if mp == "poshmark":
                    results[mp] = await probe_poshmark(ctx)
                else:
                    results[mp] = await probe_extension(ps[mp], URLS[mp], known=known)
            except Exception as e:  # noqa: BLE001 — one site's failure is said; the others go on
                results[mp] = {"site": mp, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        await takedown._close(bridge, pw, ctx)
    folder = s.path("control") / "orders_probe"
    folder.mkdir(parents=True, exist_ok=True)
    for mp, result in results.items():
        (folder / f"{mp}.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    return results


def known_titles(s: Settings) -> list[str]:
    """Our items' titles (each site's, from the item's renders) and the titles of the sales the API knows (ours or
    not): the only titles a probe keeps."""
    from thrift_agent import sales
    from thrift_agent.db import DB
    titles = []
    for row in DB(s.path("db")).conn.execute("SELECT renders FROM items WHERE renders IS NOT NULL"):
        try:
            renders = json.loads(row["renders"] or "{}") or {}
        except ValueError:
            continue
        titles += [r["title"] for r in renders.values() if isinstance(r, dict) and r.get("title")]
    client = sales.api()
    if client is not None:
        try:
            data = client.get("/sales", {"state": "all"})
            titles += [x.get("title") or "" for x in (data.get("sales", []) if isinstance(data, dict) else data)]
        except Exception:  # noqa: BLE001 — the API down: our own titles only
            pass
    return [t for t in titles if t]


def probe_line(r: dict) -> str:
    """One line per site for the terminal: what the probe read, or why not."""
    if r.get("error") or r.get("event") == "error":
        return f"✗ {r.get('page') or ''} {r.get('error') or r.get('message') or ''}".strip()
    if r.get("site") == "poshmark":
        sales = r.get("pages", {}).get("sales", {})
        return (f"{len(sales.get('rows') or [])} order rows, {len(r.get('orders') or [])} order pages read; "
                f"statuses: {', '.join(sales.get('status_texts') or [])[:160]}")
    jsons = [x["url"] for x in r.get("resources") or [] if x.get("json") is not None]
    own = [f"{c.get('url')} → {c.get('http') or c.get('error')}" for c in r.get("candidates") or []]
    return (f"{len(r.get('resources') or [])} requests ({len(jsons)} JSON read); {len(r.get('rows') or [])} order rows; "
            f"own JSON: {'; '.join(own) or 'none asked'}; statuses: {', '.join(r.get('status_texts') or [])[:160]}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- §2, the sweep

EVERY = timedelta(hours=6)              # every 6 hours while the Mac is open
FIRST_HOUR = 8                          # the day's first sweep: the first moment the Mac is awake after 08:00
RETRY = timedelta(minutes=30)           # a sweep that failed is tried again after this
BUDGET = 60.0                           # inside the pause after a listing only when this much of it is left
RECENT = timedelta(days=60)             # older orders are not sent
DETAILS = 4                             # Poshmark: the open orders' own pages read, at most (a handful of loads)
KV_SWEPT, KV_STOPPED, KV_FAILED = "orders_swept", "orders_stopped", "orders_failed"
SWEEP_LOCK = threading.Lock()           # one site at a time: the Poshmark thread and the extension sites alike
STOP_PAGES = ("login", "captcha", "block", "verify")
LABEL = {"poshmark": "Poshmark", "depop": "Depop", "vinted": "Vinted"}
SEND = ("order_id", "status", "status_text", "title", "price", "sold_at", "listing_id", "listing_url", "sku",
        "shipped_at", "delivered_at", "completed_at")         # what leaves the Mac for the API: never a buyer field

# What each site's status words mean (WO38 §1): Poshmark's display_status (and its unit status), Depop's list words,
# Vinted's sentences — anything else is unknown: those orders are left as they are, and the words told once.
# WO49 (the probe of 2026-10-24): the sales JSON's display_status of a just-sold order is "Sold" — the page draws it as
# "Awaiting Shipment"; unknown here, every fresh Poshmark sale was dropped by the sweep ("no known status")
POSHMARK_STATUS = {"sold": "to_ship", "awaiting shipment": "to_ship", "shipped": "shipped", "in transit": "shipped",
                   "delivered": "delivered", "order complete": "completed", "order completed": "completed",
                   "completed": "completed", "order cancelled": "cancelled", "cancelled": "cancelled",
                   "canceled": "cancelled", "returned": "returned"}
POSHMARK_UNIT = {"shipped": "shipped", "delivered": "delivered", "received": "completed"}
DEPOP_STATUS = {"to ship": "to_ship", "in transit": "shipped", "shipped": "shipped", "delivered": "delivered",
                "completed": "completed", "canceled": "cancelled", "cancelled": "cancelled", "refunded": "returned"}
# Depop's receipts JSON: its status and its parcel's, "delivered_pending_feedback / DELIVERED" (live 2026-10-14) —
# read by their words, the strongest first
DEPOP_CODES = ((re.compile(r"\bcancel"), "cancelled"), (re.compile(r"\brefund|\breturn"), "returned"),
               (re.compile(r"\bcomplete|\bfeedback (left|given|received)\b"), "completed"),
               (re.compile(r"\bdelivered\b"), "delivered"),
               (re.compile(r"\bshipped\b|\bin transit\b|\bdispatched\b|\bout for delivery\b"), "shipped"),
               (re.compile(r"\bawaiting\b|\bto ship\b|\bnot shipped\b|\bpending shipment\b|\blabel created\b|"
                           r"\bpre transit\b|\bpaid\b"), "to_ship"))
VINTED_STATUS = ((re.compile(r"\bcompleted\b|\baccepted the item\b"), "completed"),
                 (re.compile(r"\bdelivered\b"), "delivered"),
                 (re.compile(r"\bshipped\b|\bon its way\b|\bin transit\b"), "shipped"),
                 (re.compile(r"\bcancel"), "cancelled"),
                 (re.compile(r"\brefund|\breturn"), "returned"))
RANK = ("to_ship", "shipped", "delivered", "completed")


class Stopped(Exception):
    """The site showed a login / CAPTCHA / block / check page instead of its orders."""

    def __init__(self, page: str):
        super().__init__(f"{page} page")
        self.page = page


class Found(list):
    """A reader's orders, with where they came from (the extension: "json" | "page", and its note) for the log."""
    source: str | None = None
    note: str | None = None


class Yielded(Exception):
    """A posting job of the same site came while the sweep was reading: it goes first, the sweep comes back later."""


def status_of(site: str, text: str | None) -> str | None:
    """Ours (to_ship | shipped | delivered | completed | cancelled | returned) for a site's status words, or None."""
    words = re.sub(r"[\s_]+", " ", str(text or "")).strip().lower().rstrip(".!")      # TO_SHIP → "to ship"
    if site == "poshmark":
        return POSHMARK_STATUS.get(words)
    if site == "depop":
        return DEPOP_STATUS.get(words) or next((ours for pattern, ours in DEPOP_CODES if pattern.search(words)), None)
    return next((ours for pattern, ours in VINTED_STATUS if pattern.search(words)), None)


def enabled(s: Settings) -> bool:
    """On the Mac, with the sales API set up (nowhere to send them otherwise) and `orders.sweep` not turned off."""
    from thrift_agent import sales
    return s.is_prod and bool(s.get("orders.sweep", True)) and sales.api() is not None


def due(db, site: str, now: datetime | None = None, tz: str = "America/New_York") -> bool:
    """The lid open, the site not stopped today (a login / CAPTCHA page), no failure in the last RETRY, and: never
    read yet (from 08:00), or the last sweep 6 hours old, or the day's first after 08:00."""
    now = now or datetime.now(timezone.utc)
    if power.lid_closed():
        return False
    local = now.astimezone(ZoneInfo(tz))
    if _kv(db, KV_STOPPED).get(site) == local.date().isoformat():
        return False
    failed = _when(_kv(db, KV_FAILED).get(site))
    if failed is not None and now - failed < RETRY:
        return False
    first = local.replace(hour=FIRST_HOUR, minute=0, second=0, microsecond=0)
    last = _when(_kv(db, KV_SWEPT).get(site))
    if last is None:
        return local >= first
    return now - last >= EVERY or (local >= first and last < first)


async def sweep(s: Settings, db, site: str, read, now: datetime | None = None) -> dict:
    """One site's sweep, one site at a time (SWEEP_LOCK): `read()` gives its orders; their statuses are put in our
    words and the orders sent to the API (POST /orders; kept in the outbox when it doesn't answer). A login / CAPTCHA
    page stops this site's sweep for the day with ONE private line; a failure is tried again after RETRY. Returns
    {"site", "status": swept | stopped | failed | busy | yielded, …}."""
    from thrift_agent import notify, sales
    if not SWEEP_LOCK.acquire(blocking=False):
        return {"site": site, "status": "busy"}
    t0 = time.monotonic()
    try:
        try:
            found = await read()
        except Stopped as e:
            stop_today(db, site, e.page)
            return {"site": site, "status": "stopped", "page": e.page}
        except Yielded:
            return {"site": site, "status": "yielded"}
        except Exception as e:  # noqa: BLE001 — tried again after RETRY; never the poster's end
            _put(db, KV_FAILED, site, _iso(now))
            db.log(None, "orders_failed", {"site": site, "error": f"{type(e).__name__}: {e}"[:300]})
            return {"site": site, "status": "failed", "error": f"{type(e).__name__}: {e}"[:300]}
        moment = now or datetime.now(timezone.utc)
        orders, unknown = [], []
        for raw in found:
            order = {k: raw.get(k) for k in SEND}
            if not order["order_id"]:
                continue
            sold = _when(order["sold_at"])
            if sold is not None and moment - sold > RECENT:
                continue
            if not order["status"]:
                if order["status_text"]:
                    unknown.append(order["status_text"])
                continue
            orders.append(order)
        for text in sorted(set(unknown)):
            notify.say(f"🔎 {LABEL[site]} shows an order status I don't know: “{mask(text)[:80]}” — those orders are "
                       "left as they are.")
        sent = sales.call(db, "/orders", {"marketplace": site, "swept_at": _iso(moment), "orders": orders})
        _put(db, KV_SWEPT, site, _iso(moment))
        _put(db, KV_FAILED, site, None)
        counts: dict[str, int] = {}
        for order in orders:
            counts[order["status"]] = counts.get(order["status"], 0) + 1
        db.log(None, "orders_swept", {"site": site, "orders": len(orders), "statuses": counts, "sent": sent,
                                      "seconds": round(time.monotonic() - t0, 1),
                                      "source": getattr(found, "source", None), "note": getattr(found, "note", None)})
        return {"site": site, "status": "swept", "orders": len(orders), "statuses": counts, "sent": sent}
    finally:
        SWEEP_LOCK.release()


def stop_today(db, site: str, page: str, tz: str = "America/New_York") -> None:
    """A login / CAPTCHA / block / check page: this site's sweep stops for the day, and ONE line says so in the
    private chat (posting follows its own rules)."""
    from thrift_agent import notify
    today = datetime.now(timezone.utc).astimezone(ZoneInfo(tz)).date().isoformat()
    if _kv(db, KV_STOPPED).get(site) == today:
        return
    _put(db, KV_STOPPED, site, today)
    what = {"login": "its login page", "captcha": "a CAPTCHA", "block": "a page that turns the Mac away",
            "verify": "a check page"}.get(page, f"a {page} page")
    notify.say(f"⏸ Can't read {LABEL[site]}'s orders today — it shows {what}. I'll try again tomorrow.")
    db.log(None, "orders_stopped", {"site": site, "page": page})


# ---- the readers: each site's own data, read only

def _recent(order: dict, now: datetime | None = None) -> bool:
    sold = _when(order.get("sold_at"))
    return sold is None or (now or datetime.now(timezone.utc)) - sold <= RECENT


def poshmark_order(row: dict) -> dict | None:
    """One row of Poshmark's own sales JSON (`data.sales_summary[]`) in our fields — its buyer, address and label
    never copied."""
    oid = str(row.get("id") or "")
    if not re.fullmatch(r"[0-9a-f]{24}", oid):
        return None
    text = str(row.get("display_status") or "")[:80]
    amount = row.get("total_price_amount")
    price = amount.get("val") if isinstance(amount, dict) else None
    return {"order_id": oid, "title": str(row.get("title") or "")[:200] or None, "price": _number(price),
            "sold_at": row.get("inventory_booked_at"), "status_text": text, "status": status_of("poshmark", text)}


def poshmark_detail(order: dict) -> dict:
    """An order's own page (`$_order_details.order`): our SKU, the listing, the exact shipped / delivered / accepted
    times, and the unit's status (shipped | delivered | received)."""
    items = order.get("line_items") or [{}]
    units = order.get("inventory_units") or [{}]
    item = items[0] if isinstance(items[0], dict) else {}
    unit = units[0] if isinstance(units[0], dict) else {}
    out = {"sku": item.get("sku") or None, "listing_id": item.get("product_id") or None,
           "shipped_at": unit.get("shipped_on") or None, "delivered_at": unit.get("delivered_on") or None,
           "completed_at": unit.get("received_at") or None}
    return {**out, "unit_status": POSHMARK_UNIT.get(str(unit.get("status") or "").lower())}


def furthest(*statuses: str | None) -> str | None:
    """The furthest along of these (to_ship < shipped < delivered < completed); cancelled / returned win."""
    for final in ("cancelled", "returned"):
        if final in statuses:
            return final
    known = [x for x in statuses if x in RANK]
    return max(known, key=RANK.index) if known else None


MORE_ANSWERS = 3.0                 # WO49: after the first sales answer, the page's other ones this long


async def read_poshmark(ctx, yield_to=None, timeout: float = 25.0) -> list[dict]:
    """Poshmark's sold orders, read only, in a NEW page of its own browser (never a posting page): the seller hub's
    own sales JSON as the page receives it (nothing is asked again), then the open orders' own pages (≤ DETAILS) for
    their exact times and our SKU. A login page → Stopped; a CAPTCHA → Stopped; `yield_to()` true between loads (a
    posting job is waiting) → Yielded."""
    page = await ctx.new_page()
    got: list = []

    def on_response(resp) -> None:
        # WO49 (live 2026-10-21 → 24: three Poshmark sales never seen — every order the sweep ever read was shipped,
        # delivered, complete or cancelled, an order appearing only once it shipped): EVERY sales answer the page
        # receives is read, not just the first, and their rows merged by order id
        if "/orders/sales" in resp.url and "json" in (resp.headers.get("content-type") or ""):
            got.append(resp)

    page.on("response", on_response)
    try:
        await page.goto(URLS["poshmark"], wait_until="domcontentloaded")
        end = time.monotonic() + timeout
        while not got and time.monotonic() < end:
            if re.search(r"/(login|signup)\b", page.url):
                raise Stopped("login")
            await asyncio.sleep(0.25)
        if got:                                           # the page's other sales calls (its tabs) a moment later
            await asyncio.sleep(MORE_ANSWERS)
        if not got:
            try:
                state = parse_state(await page.content())
            except ValueError:
                state = {}
            if state.get("showCaptcha") is True or state.get("showOneTimeLoginCaptcha") is True:
                raise Stopped("captcha")
            raise RuntimeError(f"Poshmark's sales list didn't load in {timeout:.0f} s ({page.url})")
        rows, ids = [], set()
        for resp in got:
            try:
                data = await resp.json()
            except Exception:  # noqa: BLE001 — an answer that can't be read is only skipped
                continue
            for r in ((data.get("data") or {}).get("sales_summary") or []) if isinstance(data, dict) else []:
                if isinstance(r, dict) and r.get("id") not in ids:
                    ids.add(r.get("id"))
                    rows.append(r)
        orders = [o for o in (poshmark_order(r) for r in rows) if o and _recent(o)]
        for order in [o for o in orders if o["status"] not in ("completed", "cancelled")][:DETAILS]:
            if yield_to is not None and yield_to():
                raise Yielded
            try:
                state = parse_state(await page.evaluate(FETCH_TEXT, f"/order/sales/{order['order_id']}"))
            except Exception:  # noqa: BLE001 — the list's status stands
                continue
            detail = poshmark_detail((state.get("$_order_details") or {}).get("order") or {})
            order["status"] = furthest(order["status"], detail.pop("unit_status"))
            order.update({k: v for k, v in detail.items() if v})
        return orders
    finally:
        await page.close()


async def read_extension(poster, site: str, timeout: float = 120.0) -> list[dict]:
    """Depop's / Vinted's sold orders, read only, by the extension's "orders" job in the Thrift Chrome's "Orders"
    window (ext/content/vinted.js / depop.js `orders`): Vinted's own JSON of the sold orders; Depop's receipts, else
    its list as drawn. The page sends only our fields; their status words are put in ours here."""
    ev = await poster._job("orders", {"orders_url": URLS[site], "sweep": True}, timeout)
    if ev.get("event") == "error":
        if ev.get("page") in STOP_PAGES:
            raise Stopped(ev["page"])
        raise RuntimeError(f"{ev.get('stage') or 'orders'}: {ev.get('message')}")
    out = Found()
    out.source, out.note = ev.get("source"), (ev.get("note") or "")[:300] or None
    for row in (ev.get("orders") or [])[:100]:
        if not isinstance(row, dict):
            continue
        order = {k: row.get(k) for k in ("order_id", "title", "price", "sold_at", "status_text", "listing_id",
                                         "listing_url", "shipped_at", "delivered_at", "completed_at")}
        order["order_id"] = str(order["order_id"] or "").strip()[:64] or None
        order["price"] = _number(order["price"])
        if not order["sold_at"] and row.get("sold_ago"):
            order["sold_at"] = ago(row["sold_ago"])
        order["status"] = status_of(site, order["status_text"])
        out.append(order)
    return out


def ago(text: str, now: datetime | None = None) -> str | None:
    """"20 days ago" / "3 hours ago" / "yesterday" / "just now" as a time (Depop's list shows when an order sold that
    way), None when it says something else."""
    now = now or datetime.now(timezone.utc)
    t = str(text or "").strip().lower()
    if t in ("just now", "now", "today"):
        return _iso(now)
    if t == "yesterday":
        return _iso(now - timedelta(days=1))
    m = re.fullmatch(r"(?:an?|(\d+))\s+(minute|hour|day|week|month)s?\s+ago", t)
    if not m:
        return None
    n = int(m[1] or 1)
    days = {"minute": n / 1440, "hour": n / 24, "day": n, "week": 7 * n, "month": 30 * n}[m[2]]
    return _iso(now - timedelta(days=days))


async def maybe_sweep(s: Settings, db, site: str, read, gap: float | None = None, poster=None) -> dict | None:
    """The site's sweep when it is due, between that site's own listings — on an idle turn (`gap` None) or inside the
    pause after a listing when at least BUDGET seconds of it are left, so a posting or take-down job of the same site
    always goes first and no listing waits for it. Never with PAUSE, or while the site's extension is away."""
    from thrift_agent.post import runner
    if not enabled(s) or (gap is not None and gap < BUDGET) or runner._paused(db):
        return None
    if poster is not None and not getattr(poster, "available", lambda: True)():
        return None
    if not due(db, site):
        return None
    return await sweep(s, db, site, read)


async def sweep_now(s: Settings, db, sites: list[str]) -> list[dict]:
    """`thrift orders --sweep`: each site read now, whether due or not (the poster stopped: its browser profile, the
    bridge's port), one site after another — the same sweep the poster runs between listings."""
    from thrift_agent.post import takedown
    ps, bridge, pw, ctx = await takedown._open(s, sites)
    out = []
    try:
        for mp in sites:
            if mp == "poshmark":
                out.append(await sweep(s, db, mp, lambda: read_poshmark(ctx)))
            else:
                out.append(await sweep(s, db, mp, lambda mp=mp: read_extension(ps[mp], mp)))
    finally:
        await takedown._close(bridge, pw, ctx)
    return out


def sweep_line(r: dict) -> str:
    """One line per site for the terminal."""
    site = LABEL.get(r.get("site"), r.get("site"))
    if r.get("status") == "swept":
        counts = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted((r.get("statuses") or {}).items()))
        return (f"{site}: {r.get('orders', 0)} orders read ({counts or 'none'}) — "
                f"{'sent to the API' if r.get('sent') else 'kept to send when the API answers'}")
    if r.get("status") == "stopped":
        return f"{site}: stopped for today — it shows a {r.get('page')} page"
    return f"{site}: {r.get('status')}{(' — ' + r['error']) if r.get('error') else ''}"


def last_sweeps(db) -> dict[str, str]:
    """{site: when its orders were last read} — `thrift status`."""
    return dict(_kv(db, KV_SWEPT))


def _kv(db, key: str) -> dict:
    row = db.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    try:
        value = json.loads(row["value"]) if row else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def _put(db, key: str, site: str, value: str | None) -> None:
    data = _kv(db, key)
    if value is None:
        data.pop(site, None)
    else:
        data[site] = value
    db.conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(data, sort_keys=True)))
    db.conn.commit()


def _when(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.utc) if moment is not None and moment.tzinfo is None else moment


def _iso(moment: datetime | None) -> str:
    return (moment or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def _number(value) -> float | None:
    try:
        return float(str(value).replace("$", "").replace(",", "").strip()) if value not in (None, "") else None
    except ValueError:
        return None
