"""WO57 §3 — the eBay price: the same profit as the same sale on Poshmark.

The owner taps ONE price (the base, B). Poshmark keeps $2.95 of a sale under $15 and 20% from $15, so that sale pays
her `poshmark_payout(B)`. eBay keeps a share of the TOTAL amount of the sale — the item's price, what the buyer pays
for shipping and the sales tax on both — by category, plus a per-order fee (`data/ebay_fees.json`, dated: `Fees.checked`).
`price_for` solves

    price − rate × (price + shipping + tax) − order fee  =  poshmark_payout(B)

for the price and rounds it to the nearest whole dollar. Nobody types an eBay price: it is always computed, from her
price at approval, from the new base at a price drop, from her "price 35" reply. Pure functions; the file is read once
per change on disk.

    quote(20, shipping=6, tax_rate=0.07).price   → 20      quote(50, …).price → 48      quote(10, …).price → 10
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

DATA = Path(__file__).resolve().parents[2] / "data" / "ebay_fees.json"
STALE_DAYS = 90              # the fee file compared with eBay's fee page this long ago: ONE private line
POSHMARK_FLAT = 2.95         # Poshmark's fee for a sale under POSHMARK_FLAT_UNDER …
POSHMARK_FLAT_UNDER = 15.0
POSHMARK_RATE = 0.20         # … and its share from there on
MIN_PRICE = 1                # eBay's lowest fixed price is $0.99: never under a dollar
DEFAULT_TAX = 0.07           # an average sales-tax rate (settings: marketplaces.ebay.tax_rate)


class FeesError(ValueError):
    """The fee file is missing or doesn't hold what the formula needs."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Tier(_Strict):
    rate: float = Field(ge=0, lt=1)
    up_to: float | None = Field(default=None, gt=0)     # None: every dollar from the last tier's end


class OrderFee(_Strict):
    fee: float = Field(ge=0)
    up_to: float | None = Field(default=None, gt=0)     # the order's total up to this (inclusive); None: any total


class Rule(_Strict):
    name: str
    tiers: list[Tier] = Field(min_length=1)
    categories: list[str] = Field(default_factory=list)
    when_total_at_least: float | None = None
    order_fee: bool = True


class Insertion(_Strict):
    free_per_month: int = Field(ge=0)
    fee: float = Field(ge=0)


class Fees(_Strict):
    checked: date
    source: str = ""
    seller: str = ""
    insertion: Insertion
    order_fee: list[OrderFee] = Field(min_length=1)
    default: Rule
    rules: list[Rule] = Field(default_factory=list)

    def rule_for(self, categories, total: float) -> Rule:
        """The rule of a sale: the first whose categories hold the listing's category or one of its ancestors and
        whose total condition the sale meets; else the default."""
        mine = {str(c) for c in categories or ()}
        for rule in self.rules:
            if mine & set(rule.categories) and (rule.when_total_at_least is None
                                                or total + 1e-9 >= rule.when_total_at_least):
                return rule
        return self.default

    def per_order(self, total: float) -> float:
        for step in self.order_fee:
            if step.up_to is None or total <= step.up_to + 1e-9:
                return step.fee
        return self.order_fee[-1].fee


_CACHE: dict[Path, tuple[float, Fees]] = {}


def load(path: Path | None = None) -> Fees:
    """The validated fee file (cached until it changes on disk). Raises FeesError."""
    file = Path(path or DATA)
    try:
        mtime = file.stat().st_mtime
    except OSError:
        raise FeesError(f"{file.name} is missing") from None
    hit = _CACHE.get(file)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        fees = Fees.model_validate(json.loads(file.read_text(encoding="utf-8")))
    except ValidationError as e:
        why = "; ".join(f"{'.'.join(map(str, er['loc'])) or 'file'}: {er['msg']}" for er in e.errors()[:3])
        raise FeesError(f"{file.name} is not usable ({why[:300]})") from None
    except (json.JSONDecodeError, ValueError) as e:
        raise FeesError(f"{file.name} is not usable ({type(e).__name__}: {str(e).splitlines()[0][:200]})") from None
    _CACHE[file] = (mtime, fees)
    return fees


def age_days(fees: Fees, today: date | None = None) -> int:
    return ((today or datetime.now().date()) - fees.checked).days


def stale(fees: Fees, today: date | None = None) -> int | None:
    """How many days ago the file was checked against eBay's fee page, once that is STALE_DAYS or more; else None."""
    days = age_days(fees, today)
    return days if days >= STALE_DAYS else None


# ---------------------------------------------------------------- the two payouts

def poshmark_payout(base: float) -> float:
    """What a sale at `base` on Poshmark leaves her: B − $2.95 under $15, else 80% of it."""
    base = float(base)
    return base - POSHMARK_FLAT if base < POSHMARK_FLAT_UNDER else base * (1 - POSHMARK_RATE)


def _tiered(rule: Rule, total: float) -> float:
    """The final value fee: each tier's rate on its part of the total."""
    fee, start = 0.0, 0.0
    for tier in rule.tiers:
        end = total if tier.up_to is None else min(total, tier.up_to)
        if end > start:
            fee += tier.rate * (end - start)
            start = end
        if tier.up_to is None or total <= tier.up_to:
            break
    return fee


class Breakdown(BaseModel):
    """One sale on eBay, in dollars."""
    price: float
    shipping: float
    tax: float
    total: float                 # price + shipping + tax: what the final value fee is a share of
    rule: str
    final_value_fee: float
    order_fee: float
    payout: float                # price − final value fee − order fee (the shipping she is paid goes to the label)


def sale(price: float, categories=(), shipping: float = 0.0, tax_rate: float = DEFAULT_TAX,
         fees: Fees | None = None) -> Breakdown:
    """What eBay keeps of a sale at `price` and what it leaves her. `shipping`: what the buyer is charged for it;
    the tax is `tax_rate` of price + shipping."""
    fees = fees or load()
    price, shipping = float(price), max(0.0, float(shipping))
    tax = tax_rate * (price + shipping)
    total = price + shipping + tax
    rule = fees.rule_for(categories, total)
    fvf = _tiered(rule, total)
    per_order = fees.per_order(total) if rule.order_fee else 0.0
    return Breakdown(price=price, shipping=shipping, tax=tax, total=total, rule=rule.name, final_value_fee=fvf,
                     order_fee=per_order, payout=price - fvf - per_order)


def ebay_payout(price: float, categories=(), shipping: float = 0.0, tax_rate: float = DEFAULT_TAX,
                fees: Fees | None = None) -> float:
    return sale(price, categories, shipping, tax_rate, fees).payout


class Quote(BaseModel):
    """The eBay price for a base price, and the two profits side by side."""
    base: float                  # her price: the one she tapped, or the item's price after drops and her changes
    price: int                   # the eBay price, a whole dollar
    exact: float                 # the price before rounding
    shipping: float
    tax_rate: float
    rule: str                    # the fee rule that applies at this price ("Most categories", "Women's bags …")
    poshmark_payout: float       # what the sale leaves her on Poshmark at `base`
    ebay_payout: float           # … and on eBay at `price`
    checked: date                # the fee file's date

    @property
    def difference(self) -> float:
        """eBay's payout minus Poshmark's (the rounding to a whole dollar; cents)."""
        return self.ebay_payout - self.poshmark_payout


def _half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def quote(base: float, categories=(), shipping: float = 0.0, tax_rate: float = DEFAULT_TAX,
          fees: Fees | None = None) -> Quote:
    """The eBay price that leaves the same payout as a Poshmark sale at `base` — solved exactly, then rounded to the
    nearest whole dollar. A fee rule that begins at a total (athletic shoes from $150) makes the payout jump there: a
    price just under the jump would lose the cheaper rate, so the dollar above it is taken when rounding down would
    cost more than the rounding itself."""
    fees = fees or load()
    base = float(base)
    if not math.isfinite(base) or base <= 0:
        raise ValueError(f"a base price must be a positive number, not {base!r}")
    target = poshmark_payout(base)

    def gain(p: float) -> float:
        return ebay_payout(p, categories, shipping, tax_rate, fees) - target

    lo, hi = 0.0, max(2.0, 2 * base + shipping + 10)
    while gain(hi) < 0 and hi < 1e7:
        hi *= 2
    for _ in range(80):                      # the lowest price whose payout reaches the target, to a hundredth of a cent
        mid = (lo + hi) / 2
        if gain(mid) < 0:
            lo = mid
        else:
            hi = mid
    exact = hi
    price = max(MIN_PRICE, _half_up(exact))
    if price < exact and gain(price) < -0.5:
        price = max(MIN_PRICE, math.ceil(exact - 1e-9))        # rounding down fell under a rule's threshold
    at = sale(price, categories, shipping, tax_rate, fees)
    return Quote(base=base, price=price, exact=round(exact, 4), shipping=float(shipping), tax_rate=tax_rate,
                 rule=at.rule, poshmark_payout=round(target, 2), ebay_payout=round(at.payout, 2), checked=fees.checked)


def price_for(base: float, categories=(), shipping: float = 0.0, tax_rate: float = DEFAULT_TAX,
              fees: Fees | None = None) -> int:
    """The eBay price, in whole dollars, for her price `base` (see `quote`)."""
    return quote(base, categories, shipping, tax_rate, fees).price


def insertion_fee(fees: Fees, listed_this_month: int) -> float:
    """What the NEXT new listing of the month costs to insert: nothing while free ones are left."""
    return 0.0 if listed_this_month < fees.insertion.free_per_month else fees.insertion.fee
