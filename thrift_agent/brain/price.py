"""Rule-based pricing seeded from the seller's own sold history."""
from __future__ import annotations

import math
import re

from thrift_agent.schema import Facts, PriceResult

# "price 44", "price: 45", "price=45", "list at $50". Bare "\bprice" also sits inside "original price $120",
# so note_price() rejects a match whose preceding word marks it as what the item cost new.
_ASK_RE = re.compile(r"\b(?:price|list)\s*(?::|=|\bat\b)?\s*\$?(\d{2,4})\b", re.I)
_NOT_ASKING = frozenset({"original", "retail", "paid", "was", "rrp", "msrp"})
# "original price 120", "retail price $120", "retail 200", "paid 40", "msrp 150".
_ORIGINAL_RE = re.compile(
    r"\b(?:(?:original|retail|rrp|msrp)\s+price|retail|rrp|msrp|paid)\s*(?::|=|\bat\b)?\s*\$?(\d{2,4})\b", re.I
)


def _norm_key(name: str) -> str:
    return re.sub(r"\s+", " ", str(name).strip().lower())


def norm_brand(name: str | None, aliases: dict[str, str]) -> str | None:
    if not name:
        return None
    key = _norm_key(name)
    return aliases.get(key, key)


def _round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def nice_round(x: float, step: int) -> int:
    # round-half-up: Python's round() is half-to-even, so 22.5 would go to 20 while 27.5 goes to 30.
    return int(max(step, step * math.floor(x / step + 0.5)))


def _preceding_word(text: str, end: int) -> str | None:
    m = re.search(r"(\w+)\W*$", text[:end])
    return m[1].lower() if m else None


def note_price(note: str | None) -> int | None:
    """The asking price the seller typed ("price 44", "list at $50"); never the original/retail price."""
    if not note:
        return None
    for m in _ASK_RE.finditer(note):
        if _preceding_word(note, m.start()) not in _NOT_ASKING:
            return int(m[1])
    return None


def note_original_price(note: str | None) -> int | None:
    """What the item cost new, if the seller mentioned it ("original price $120", "retail 200", "paid 40")."""
    if note and (m := _ORIGINAL_RE.search(note)):
        return int(m[1])
    return None


def note_floor(note: str | None) -> int | None:
    if note and (m := re.search(r"\bfloor\s*\$?(\d{1,4})\b", note, re.I)):
        return int(m[1])
    return None


def price(facts: Facts, tiers: dict | None, cfg: dict, note: str | None = None) -> PriceResult:
    # An empty YAML section ("aliases:" with nothing under it) loads as None, and an empty file as None overall.
    tiers = tiers or {}
    brands = {_norm_key(k): v for k, v in (tiers.get("brands") or {}).items()}
    aliases = {_norm_key(k): _norm_key(v) for k, v in (tiers.get("aliases") or {}).items()}
    defaults = tiers.get("category_defaults") or {}

    floor = max(cfg["floor"], note_floor(note) or 0)
    mkt = cfg["marketplace_multiplier"]
    original = note_original_price(note)     # what it cost new: shown as "Original Price", never the asking price

    def finish(list_price: int, target: int | None, source: str, basis: str) -> PriceResult:
        list_price = max(list_price, floor)
        if source == "note":
            # The seller typed an exact number: keep it verbatim where the multiplier is 1.0, and round the
            # others to the nearest dollar rather than to the step ("price 44" must not become $45).
            by_mp = {mp: list_price if m == 1 else max(floor, _round_half_up(list_price * m)) for mp, m in mkt.items()}
        else:
            by_mp = {mp: max(floor, nice_round(list_price * m, cfg["round_to"])) for mp, m in mkt.items()}
        return PriceResult(target=target, list_price=list_price, source=source, by_marketplace=by_mp, basis=basis,
                           original_price=original)

    if (fixed := note_price(note)) is not None:
        return finish(fixed, None, "note", f"seller note price ${fixed}")

    brand = norm_brand(facts.brand.value, aliases)
    entry = brands.get(brand) if brand else None
    target, source = None, "none"
    if entry:
        cat_override = (entry.get("categories") or {}).get(facts.category)
        target, source = (cat_override, "brand_category") if cat_override else (entry["target"], "brand")
    elif (default := defaults.get(facts.category)) is not None:
        target, source = default, "category_default"
    if target is None:
        return PriceResult(target=None, list_price=None, source="none", basis="no brand or category price",
                           original_price=original)

    cond = cfg["condition_multiplier"][facts.condition]
    list_price = nice_round(target * cond * cfg["list_markup"], cfg["round_to"])
    matched = f" {brand!r}" if source in ("brand", "brand_category") else f" {facts.category!r}"
    basis = f"{source}{matched}: target ${target} × {facts.condition} {cond} × markup {cfg['list_markup']}"
    return finish(list_price, target, source, basis)
