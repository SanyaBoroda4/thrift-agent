"""WO57 §3/§5 — the eBay price of a LIVE listing when its base changes. The base is her price: the one she tapped,
then the item's price after the agent's drops and her own "price 35". eBay's price is never typed: it is worked out
from the base with the listing's own category (its fee rule), its parcel (what the buyer pays for shipping) and the
tax rate it was listed with — all kept in the listing row's fields (`fields_json`)."""
from __future__ import annotations

from thrift_agent.db import loads
from thrift_agent.ebay import fees


def fields_of(listing) -> dict:
    """The mapped fields of an eBay listing row (a row, or its fields already)."""
    if isinstance(listing, dict):
        return listing
    try:
        return loads(listing["fields_json"]) or {}
    except (KeyError, IndexError, TypeError, ValueError):
        return {}


def base_of(listing) -> int | None:
    """Her price the listing's eBay price was last worked out from."""
    base = fields_of(listing).get("base_price")
    try:
        return int(base) if base else None
    except (TypeError, ValueError):
        return None


def for_base(listing, base: float, table: fees.Fees | None = None) -> int:
    """The eBay price for `base` with this listing's own category, parcel and tax rate."""
    f = fields_of(listing)
    quote = f.get("quote") or {}
    shipping = (f.get("shipping") or {}).get("buyer_pays", quote.get("shipping", 0.0))
    return fees.price_for(base, f.get("category_ids") or [], float(shipping or 0.0),
                          float(quote.get("tax_rate", fees.DEFAULT_TAX)), table)


def quote_for(listing, base: float, table: fees.Fees | None = None) -> fees.Quote:
    f = fields_of(listing)
    quote = f.get("quote") or {}
    shipping = (f.get("shipping") or {}).get("buyer_pays", quote.get("shipping", 0.0))
    return fees.quote(base, f.get("category_ids") or [], float(shipping or 0.0),
                      float(quote.get("tax_rate", fees.DEFAULT_TAX)), table)


def after_drop(listing, live: float, dropped) -> tuple[int, int | None]:
    """A price drop's new eBay price: worked out from the base after its own 10% drop (the same profit as Poshmark's
    new price). A drop never raises a price: when that would not be below what eBay shows now — she lowered it there
    by hand — it is a plain 10% off the price eBay shows. Returns (the new price, the new base — None when the
    listing carries no base: then it is a plain 10% off too)."""
    base = base_of(listing)
    plain = int(dropped(live))
    if base is None:
        return plain, None
    new_base = int(dropped(base))
    new = for_base(listing, new_base)
    return (new if new < float(live) else plain), new_base


def record(db, iid: str, *, base: int | None, price: float) -> None:
    """The listing row's fields follow a price change eBay shows: the base it stands for and the price itself."""
    row = db.listing(iid, "ebay")
    if row is None:
        return
    f = fields_of(row)
    if base is not None:
        f["base_price"] = int(base)
        try:
            q = quote_for(f, base)
            f["quote"] = {**(f.get("quote") or {}), "rule": q.rule, "exact": q.exact, "poshmark_payout": q.poshmark_payout,
                          "ebay_payout": round(fees.ebay_payout(price, f.get("category_ids") or [], q.shipping,
                                                                q.tax_rate), 2)}
        except (ValueError, fees.FeesError):
            pass
    f["price"] = int(round(float(price)))
    db.upsert_listing(iid, "ebay", fields_json=f, price=int(round(float(price))))
