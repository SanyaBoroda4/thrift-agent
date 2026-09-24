"""Rule-based pricing seeded from the seller's own sold history."""
from __future__ import annotations

import re

from thrift_agent.schema import Facts, PriceResult


def norm_brand(name: str | None, aliases: dict[str, str]) -> str | None:
    if not name:
        return None
    key = re.sub(r"\s+", " ", name.strip().lower())
    return aliases.get(key, key)


def nice_round(x: float, step: int) -> int:
    return int(max(step, step * round(x / step)))


def note_price(note: str | None) -> int | None:
    if note and (m := re.search(r"\b(?:price|list)\s*\$?(\d{2,4})\b", note, re.I)):
        return int(m[1])
    return None


def note_floor(note: str | None) -> int | None:
    if note and (m := re.search(r"\bfloor\s*\$?(\d{1,4})\b", note, re.I)):
        return int(m[1])
    return None


def price(facts: Facts, tiers: dict, cfg: dict, note: str | None = None) -> PriceResult:
    floor = max(cfg["floor"], note_floor(note) or 0)
    mkt = cfg["marketplace_multiplier"]

    def finish(list_price: int, target: int | None, source: str, basis: str) -> PriceResult:
        list_price = max(list_price, floor)
        by_mp = {mp: max(floor, nice_round(list_price * m, cfg["round_to"])) for mp, m in mkt.items()}
        return PriceResult(target=target, list_price=list_price, source=source, by_marketplace=by_mp, basis=basis)

    if (fixed := note_price(note)) is not None:
        return finish(fixed, None, "note", f"seller note price ${fixed}")

    brand = norm_brand(facts.brand.value, tiers.get("aliases", {}))
    entry = tiers.get("brands", {}).get(brand) if brand else None
    target, source = None, "none"
    if entry:
        cat_override = (entry.get("categories") or {}).get(facts.category)
        target, source = (cat_override, "brand_category") if cat_override else (entry["target"], "brand")
    elif (default := tiers.get("category_defaults", {}).get(facts.category)) is not None:
        target, source = default, "category_default"
    if target is None:
        return PriceResult(target=None, list_price=None, source="none", basis="no brand or category price")

    cond = cfg["condition_multiplier"][facts.condition]
    list_price = nice_round(target * cond * cfg["list_markup"], cfg["round_to"])
    basis = f"{source}: target ${target} × {facts.condition} {cond} × markup {cfg['list_markup']}"
    return finish(list_price, target, source, basis)
