"""Pull the closet's sales history + listing pages into paths.harvest (private/harvest — buyer data, never public).

Runs in the poster's Chrome profile (already logged in). Read-only: it only loads pages.
Order pages expose __INITIAL_STATE__.$_order_details.order (verified); listing pages expose
__INITIAL_STATE__.$_listing_details.listingDetails (verified shape; returns PostRemovedError while the
account is restricted — reinstate first).

Re-runnable: a listing whose JSON is already in listings/ is not fetched again (delete the file to refetch),
a page that fails to parse becomes an {"href", "error"} row instead of aborting the run, and orders.json is
flushed every FLUSH_EVERY orders and again on exit so a partial run is still usable.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from pathlib import Path

from thrift_agent.config import PRIVATE_DIR, Settings
from thrift_agent.post.base import open_browser

STATE_MARK = re.compile(r"__INITIAL_STATE__\s*=\s*")
FLUSH_EVERY = 25                      # orders between orders.json writes
FETCH_JS = "u => fetch(u, {credentials: 'include'}).then(r => r.text())"


def parse_state(html: str) -> dict:
    m = STATE_MARK.search(html)
    if not m:
        raise ValueError("no __INITIAL_STATE__ on page")
    obj, _ = json.JSONDecoder().raw_decode(html, m.end())
    return obj


def slim_order(o: dict) -> dict:
    li = (o.get("line_items") or [{}])[0]
    val = lambda d: (d or {}).get("val")  # noqa: E731
    return {
        "title": o.get("title"), "brand": li.get("brand"), "category": li.get("category"),
        "size": li.get("size"), "product_url": li.get("product_url"),
        "price": val(o.get("total_price_amount")), "earnings": val(o.get("seller_earning_amount")),
        "status": o.get("display_status"), "cancel_reason": o.get("cancel_reason"),
        "via_offer": bool(o.get("offer_id")), "booked_at": o.get("inventory_booked_at"),
        "rating": (o.get("order_rating") or {}).get("rating"),
        "rating_comment": (o.get("order_rating") or {}).get("comment"),
        "picture_url": li.get("picture_url"),
    }


def _num(v) -> float:
    """Poshmark amounts are strings ('45.00', '$1,200.00'); None/'' → 0.0 so sort keys never mix types."""
    if v is None or isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("$", "").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def _merge_hrefs(hrefs: list[str], links: list[str]) -> list[str]:
    """Order links in first-seen order, no duplicates (one page can link the same order twice), no query strings."""
    return list(dict.fromkeys([*hrefs, *(h for h in links if h and "?" not in h)]))


async def _fetch_order(page, href: str, out_dir: Path) -> dict:
    """Order page → slim dict (+ href). Saves the listing page's JSON unless it is already on disk."""
    o = slim_order(parse_state(await page.evaluate(FETCH_JS, href))["$_order_details"]["order"])
    o["href"] = href
    if o["product_url"]:
        lid = o["product_url"].rstrip("/").split("-")[-1]
        f = out_dir / "listings" / f"{lid}.json"
        if not f.exists():
            state = parse_state(await page.evaluate(FETCH_JS, o["product_url"]))
            details = state.get("$_listing_details", {}).get("listingDetails", {})
            f.write_text(json.dumps(details, indent=1), encoding="utf-8")
    return o


async def harvest(s: Settings, max_orders: int | None = None) -> Path:
    out_dir = s.path("harvest")
    (out_dir / "listings").mkdir(parents=True, exist_ok=True)
    path = out_dir / "orders.json"
    orders: list[dict] = []

    def save() -> None:
        path.write_text(json.dumps(orders, indent=1), encoding="utf-8")

    pw, ctx = await open_browser(s.path("chrome_profile"), s["schedule"]["timezone"])
    page = await ctx.new_page()
    try:
        await page.goto("https://poshmark.com/order/sales")
        await page.wait_for_selector("tr td", timeout=30_000)
        hrefs: list[str] = []
        while True:
            links = await page.eval_on_selector_all(
                "tr a[href^='/order/sales/']", "els => els.map(e => e.getAttribute('href'))")
            hrefs = _merge_hrefs(hrefs, links)
            nxt = page.locator("button[data-et-name='pagination_next']")
            before = await page.inner_text("body")
            if not await nxt.count() or await nxt.is_disabled():
                break
            await nxt.click()
            await page.wait_for_timeout(2000)
            if await page.inner_text("body") == before:
                break
        if max_orders:
            hrefs = hrefs[:max_orders]

        for n, h in enumerate(hrefs, 1):
            # One bad page (login redirect, removed order, expired session) must not throw away the rest of the run.
            try:
                o = await _fetch_order(page, h, out_dir)
                print(f"{n}/{len(hrefs)} {o['title']}")
            except Exception as e:  # recorded in the row; the run goes on
                o = {"href": h, "error": f"{type(e).__name__}: {e}"}
                print(f"{n}/{len(hrefs)} {h} FAILED: {o['error']}")
            orders.append(o)
            if n % FLUSH_EVERY == 0:
                save()
            await asyncio.sleep(random.uniform(0.6, 1.4))
        return path
    finally:
        if orders or not path.exists():   # never overwrite a previous run's file with an empty one
            save()
        await ctx.close()
        await pw.stop()


def build_style(s: Settings, keep: int = 30) -> Path:
    """Turn harvested listings of completed, well-rated sales into few-shot examples."""
    hd = s.path("harvest")
    orders = json.loads((hd / "orders.json").read_text(encoding="utf-8"))
    examples, removed = [], 0
    for o in orders:
        if "error" in o or o.get("status") != "Order Complete" or not o.get("product_url"):
            continue
        lid = o["product_url"].rstrip("/").split("-")[-1]
        f = hd / "listings" / f"{lid}.json"
        if not f.exists():
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        if "error" in d:
            removed += 1
            continue
        examples.append({
            "title": d.get("title"), "description": d.get("description"),
            "brand": o["brand"], "category": o["category"], "size": o["size"],
            "list_price": (d.get("price_amount") or {}).get("val"),
            "original_price": (d.get("original_price_amount") or {}).get("val"),
            "sold_price": o["price"], "via_offer": o["via_offer"], "rating": o["rating"],
        })
    if removed:
        print(f"{removed} listings unreadable (account restricted or removed)")
    if examples and all(e["description"] is None for e in examples):
        print("listing JSON has no 'description' key — inspect one file and adjust build_style()")
    # Amounts are strings on Poshmark: compare as numbers or '9.00' outranks '45.00' and a None tie raises.
    examples.sort(key=lambda e: (_num(e["rating"]), _num(e["sold_price"])), reverse=True)
    dst = PRIVATE_DIR / "style_examples" / "poshmark_listings.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(examples[:keep], indent=1, ensure_ascii=False), encoding="utf-8")
    return dst
