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

import json
import re
from datetime import datetime, timezone
from pathlib import Path

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
    out: dict = {"site": "poshmark", "read_at": _now(), "pages": {}}
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
        hrefs = list(dict.fromkeys(r["href"] for r in out["pages"]["sales"].get("rows", []) if r.get("href")))
        orders = []
        for href in hrefs[:max_orders]:
            try:
                state = parse_state(await page.evaluate(FETCH_TEXT, href))
                order = (state.get("$_order_details") or {}).get("order") or {}
                orders.append({"href": href, "order": scrub(order, max_list=2)})
            except Exception as e:  # noqa: BLE001 — a page that can't be read is said, never a reason to stop
                orders.append({"href": href, "error": f"{type(e).__name__}: {str(e)[:120]}"})
        out["orders"] = orders
    finally:
        await page.close()
    return out


# ---------------------------------------------------------------- Depop, Vinted: the extension's "orders" job

async def probe_extension(poster, url: str, timeout: float = 150) -> dict:
    """Section 1 on Depop / Vinted: the extension's "orders" job (ext/content/common.js T.orders) in the Thrift
    Chrome's "Orders" window — the page read, nothing clicked: the JSON the page itself loaded (its addresses, and each
    one read again same-origin, scrubbed in the page), the status words it shows. {"error", "page"} when it couldn't."""
    ev = await poster._job("orders", {"orders_url": url, "probe": True}, timeout)
    keep = ("url", "title", "resources", "documents", "status_texts", "testids", "message", "page", "stage", "event")
    return {"site": poster.name, "read_at": _now(), **{k: ev.get(k) for k in keep if k in ev}}


# ---------------------------------------------------------------- the diagnostic run

async def probe(s: Settings, sites: list[str]) -> dict:
    """`thrift orders --probe`: each site's sold orders read (the poster stopped: its browser profile, the bridge's
    port), every result saved scrubbed to <control>/orders_probe/<site>.json. Returns {site: result}."""
    from thrift_agent.post import takedown
    ps, bridge, pw, ctx = await takedown._open(s, sites)
    results = {}
    try:
        for mp in sites:
            try:
                if mp == "poshmark":
                    results[mp] = await probe_poshmark(ctx)
                else:
                    results[mp] = await probe_extension(ps[mp], URLS[mp])
            except Exception as e:  # noqa: BLE001 — one site's failure is said; the others go on
                results[mp] = {"site": mp, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        await takedown._close(bridge, pw, ctx)
    folder = s.path("control") / "orders_probe"
    folder.mkdir(parents=True, exist_ok=True)
    for mp, result in results.items():
        (folder / f"{mp}.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    return results


def probe_line(r: dict) -> str:
    """One line per site for the terminal: what the probe read, or why not."""
    if r.get("error") or r.get("event") == "error":
        return f"✗ {r.get('page') or ''} {r.get('error') or r.get('message') or ''}".strip()
    if r.get("site") == "poshmark":
        sales = r.get("pages", {}).get("sales", {})
        return (f"{len(sales.get('rows') or [])} order rows, {len(r.get('orders') or [])} order pages read; "
                f"statuses: {', '.join(sales.get('status_texts') or [])[:160]}")
    jsons = [x["url"] for x in r.get("resources") or [] if x.get("json") is not None]
    return (f"{len(r.get('resources') or [])} requests ({len(jsons)} JSON read); statuses: "
            f"{', '.join(r.get('status_texts') or [])[:160]}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
