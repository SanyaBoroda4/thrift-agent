"""Rule-based pricing seeded from the seller's own sold history."""
from __future__ import annotations

import math
import re

from thrift_agent.brain import taxonomy
from thrift_agent.schema import Facts, PriceResult

DEFAULT_TARGET = 15     # the last resort when neither the brand nor any category table has a price (pricing.default_target)

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


def category_default(defaults: dict | None, department: str, category: str,
                     also: list[str] | tuple[str, ...] = ()) -> tuple[int, str] | None:
    """(target, label) for a brand the table doesn't list, or None.

    Lookup order: the department's own table — the category, then its other names (`also`: Poshmark files a kids tee
    under "Shirts & Tops" while the table says "Tops"), then 'other' — and only then the flat legacy table
    ({category: price} at the top level) the same way. The label names the table that was actually used — "Kids
    'Tops'", "flat table 'Shoes'", "Kids 'other'" — so the basis can never say Kids while quoting a number from
    somewhere else. Keys match case/whitespace-insensitively."""
    defaults = defaults or {}
    dept_tables = {_norm_key(k): {_norm_key(ck): cv for ck, cv in v.items()}
                   for k, v in defaults.items() if isinstance(v, dict)}
    flat = {_norm_key(k): v for k, v in defaults.items() if not isinstance(v, dict) and v is not None}
    for table, label in ((dept_tables.get(_norm_key(department), {}), department), (flat, "flat table")):
        for key in (category, *also, "other"):
            if (target := table.get(_norm_key(key))) is not None:
                return target, f"{label} {key!r}"
    return None


def note_floor(note: str | None) -> int | None:
    if note and (m := re.search(r"\bfloor\s*\$?(\d{1,4})\b", note, re.I)):
        return int(m[1])
    return None


def price(facts: Facts, tiers: dict | None, cfg: dict, note: str | None = None,
          premium: tuple[float, str | None] = (1.0, None)) -> PriceResult:
    """`premium`: (multiplier, why) from brain/premium.price_factor (WO26) — a premium fiber, line or confirmed vintage,
    the largest one only — applied once to the suggestion; never to a price the seller's note gives."""
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
    target, source, matched = None, "none", ""
    if entry:
        cat_override = (entry.get("categories") or {}).get(facts.category)
        target, source = (cat_override, "brand_category") if cat_override else (entry["target"], "brand")
        matched = repr(brand)
    elif default := category_default(defaults, facts.department, facts.category,
                                     taxonomy.other_names(facts.department, facts.category)):
        target, source = default[0], "category_default"
        matched = default[1]                                  # the table actually used: "Kids 'Tops'", "flat table 'Shoes'"
    if target is None:
        # Never no price (WO20): the last resort is the global default, so the owner's card always has a number.
        target, source, matched = cfg.get("default_target", DEFAULT_TARGET), "default", "(no brand or category price)"

    cond = cfg["condition_multiplier"][facts.condition]
    factor, why = premium
    list_price = nice_round(target * cond * factor * cfg["list_markup"], cfg["round_to"])
    basis = (f"{source} {matched}: target ${target} × {facts.condition} {cond}"
             + (f" × {why} {factor}" if factor != 1 else "") + f" × markup {cfg['list_markup']}")
    return finish(list_price, target, source, basis)
