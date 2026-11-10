"""eBay, the fourth site (WO57): posted through eBay's official API — plain HTTPS calls, no browser, nothing opened on
the owner's Mac. `fees` — the price that leaves her the same profit as the same sale on Poshmark; `shipping` — what the
buyer is charged; `mapping` — the item as an eBay listing (category, condition, item specifics from eBay's own values);
`client` — the calls themselves; `poster` — the site worker's poster (`marketplaces.ebay.driver: api`); `backfill` —
the items already live elsewhere, 25 a day."""

SITE = "ebay"
LABEL = "eBay"


def ready(s) -> str | None:
    """None when eBay can be worked on with these settings, else why not (eBay is then off for the run; the other
    sites are never affected): its fee and parcel tables load, and the items' location is set (the ZIP code — the
    seller's own data, private/settings.yaml)."""
    from thrift_agent.ebay import fees, shipping
    try:
        fees.load()
        shipping.load()
    except (fees.FeesError, shipping.ShippingError) as e:
        return str(e)
    if not str((s.get("marketplaces.ebay.location.postal_code") if s is not None else "") or "").strip():
        return "marketplaces.ebay.location.postal_code isn't set (private/settings.yaml)"
    return None
