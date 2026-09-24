"""Pull the closet's sales history + listing pages into data/harvest/ for style and pricing.

Runs in the poster's Chrome profile (already logged in). Read-only: it only loads pages.
Order pages expose __INITIAL_STATE__.$_order_details.order (verified); listing pages expose
__INITIAL_STATE__.$_listing_details.listingDetails (verified shape; returns PostRemovedError while the
account is restricted — reinstate first).
"""
from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path

from thrift_agent.config import PRIVATE_DIR, Settings
from thrift_agent.post.base import open_browser

STATE_MARK = "__INITIAL_STATE__="


def parse_state(html: str) -> dict:
    i = html.find(STATE_MARK)
    if i < 0:
        raise ValueError("no __INITIAL_STATE__ on page")
    obj, _ = json.JSONDecoder().raw_decode(html, i + len(STATE_MARK))
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


async def harvest(s: Settings, max_orders: int | None = None) -> Path:
    out_dir = s.path("harvest")
    (out_dir / "listings").mkdir(parents=True, exist_ok=True)
    pw, ctx = await open_browser(s.path("chrome_profile"), s["schedule"]["timezone"])
    page = await ctx.new_page()
    try:
        await page.goto("https://poshmark.com/order/sales")
        await page.wait_for_selector("tr td", timeout=30_000)
        hrefs: list[str] = []
        while True:
            links = await page.eval_on_selector_all(
                "tr a[href^='/order/sales/']", "els => els.map(e => e.getAttribute('href'))")
            hrefs += [h for h in links if "?" not in h and h not in hrefs]
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

        orders = []
        for n, h in enumerate(hrefs, 1):
            html = await page.evaluate("u => fetch(u, {credentials: 'include'}).then(r => r.text())", h)
            o = slim_order(parse_state(html)["$_order_details"]["order"])
            orders.append(o)
            if o["product_url"]:
                lhtml = await page.evaluate("u => fetch(u, {credentials: 'include'}).then(r => r.text())",
                                            o["product_url"])
                details = parse_state(lhtml).get("$_listing_details", {}).get("listingDetails", {})
                lid = o["product_url"].rstrip("/").split("-")[-1]
                (out_dir / "listings" / f"{lid}.json").write_text(json.dumps(details, indent=1), encoding="utf-8")
            print(f"{n}/{len(hrefs)} {o['title']}")
            await asyncio.sleep(random.uniform(0.6, 1.4))
        path = out_dir / "orders.json"
        path.write_text(json.dumps(orders, indent=1), encoding="utf-8")
        return path
    finally:
        await ctx.close()
        await pw.stop()


def build_style(s: Settings, keep: int = 30) -> Path:
    """Turn harvested listings of completed, well-rated sales into few-shot examples."""
    hd = s.path("harvest")
    orders = json.loads((hd / "orders.json").read_text(encoding="utf-8"))
    examples, removed = [], 0
    for o in orders:
        if o["status"] != "Order Complete" or not o["product_url"]:
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
    examples.sort(key=lambda e: (e["rating"] or 0, e["sold_price"] or 0), reverse=True)
    dst = PRIVATE_DIR / "style_examples" / "poshmark_listings.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(examples[:keep], indent=1, ensure_ascii=False), encoding="utf-8")
    return dst
