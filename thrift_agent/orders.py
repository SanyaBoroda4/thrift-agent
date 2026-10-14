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
from datetime import datetime, timezone

from thrift_agent.config import Settings

SITES = ("poshmark", "depop", "vinted")
URLS = {"poshmark": "https://poshmark.com/order/sales",
        "poshmark_news": "https://poshmark.com/news/order",
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
    (/order/sales: its rows, their status texts, its own state) and the order notifications (/news/order), then each
    of the first `max_orders` orders' own page as JSON (same-origin GET, `$_order_details.order`), every document
    scrubbed. Nothing is clicked. Closed after."""
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
        for name, url in (("sales", URLS["poshmark"]), ("news", URLS["poshmark_news"])):
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
    """Our items' titles and the titles of the sales the API knows (ours or not): the only titles a probe keeps."""
    from thrift_agent import sales
    from thrift_agent.db import DB
    titles = [row["title"] for row in DB(s.path("db")).conn.execute("SELECT title FROM items WHERE title IS NOT NULL")]
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
