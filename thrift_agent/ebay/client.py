"""WO57 §1 — eBay's API, the calls themselves. Plain HTTPS; no browser, nothing opened on the Mac.

- **The Listing API** (eBay's GraphQL API, `https://graphqlapi.ebay.com/graphql`, scope `sell.listing`): `createListing`
  (with its `VALIDATE` mode: everything checked, nothing created; an idempotency key, so a call repeated after a lost
  answer never makes a second listing), `updateListing`, `bulkUpdateInventory` (the price), `endListing`, and the two
  reads — `sellerListings` (by listing id) and `sellerListingSearch` (the seller's active / ended listings, by SKU).
  Shipping, returns and the item's location go INLINE: no business-policy opt-in is needed.
- **The Media API** (`https://apim.ebay.com/commerce/media/v1_beta`, scope `sell.inventory`): `createImageFromFile` —
  each photo's bytes to eBay Picture Services, the listing then names their EPS addresses.
- **The Taxonomy and Metadata APIs** (`https://api.ebay.com`, an application token): the category suggestions, a
  category's item specifics and the conditions it takes.

The tokens come from thrift-api (`TokenSource`: the refresh token and the app's secret never leave Azure; the Mac gets
a short-lived access token with its function key). Every failure is an `EbayError` with its kind — `item` (eBay
refused THIS listing: its violations), `policy` (refused or removed for good), `auth` (the consent is gone), `limit`
(the account's selling limit or the call limit), `site` (eBay's own failure or an answer we don't know), `infra` (the
network) — so one item never stops the site (WO54 rule 4). Nothing is logged that carries a token."""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from thrift_agent.ebay import mapping

MARKETPLACE = "EBAY_US"
TREE = "0"                                   # EBAY_US's category tree
HOSTS = {
    "production": {"graphql": "https://graphqlapi.ebay.com/graphql", "api": "https://api.ebay.com",
                   "media": "https://apim.ebay.com", "item": "https://www.ebay.com/itm/"},
    "sandbox": {"graphql": "https://graphqlapi.sandbox.ebay.com/graphql", "api": "https://api.sandbox.ebay.com",
                "media": "https://apim.sandbox.ebay.com", "item": "https://sandbox.ebay.com/itm/"},
}
TIMEOUT = 30.0
UPLOAD_TIMEOUT = 90.0
ITEM, POLICY, AUTH, LIMIT, SITE, INFRA = "item", "policy", "auth", "limit", "site", "infra"
# a refusal that no other try changes: eBay's rules about the item or the brand, or its own removal
POLICY_WORDS = re.compile(r"\b(?:polic(?:y|ies)|prohibited|restricted|not allowed|not permitted|counterfeit|"
                          r"replica|infring\w*|trademark|vero\b|authentic\w*|banned|removed|recall\w*)", re.I)
LIMIT_WORDS = re.compile(r"selling limit|exceed\w* (?:the|your) (?:limit|number|amount)|rate limit|too many requests|"
                         r"call limit|quota", re.I)


class EbayError(Exception):
    """A call that didn't do what was asked. `kind`: item | policy | auth | limit | site | infra; `unknown`: the call
    may have reached eBay (a lost answer after a create: the listing is looked for, never made again blind)."""

    def __init__(self, message: str, kind: str = SITE, status: int | None = None, code: str | None = None,
                 violations: list[dict] | None = None, unknown: bool = False):
        super().__init__(message)
        self.kind, self.status, self.code = kind, status, code
        self.violations = list(violations or [])
        self.unknown = unknown


class NotConnected(EbayError):
    """thrift-api has no working consent for the eBay account (never given, revoked, expired): nothing can be done
    until the owner clicks Agree again — thrift-api's own private message carries the link."""

    def __init__(self, message: str = "eBay isn't connected"):
        super().__init__(message, AUTH)


def idempotency_key(sku: str, generation: int = 0) -> str:
    """The 32 hex characters eBay keeps a createListing by: the same for every repeat of the same listing (a call
    whose answer was lost), another one only for a new listing of the item (`generation`)."""
    return hashlib.md5(f"thrift-agent:{sku}:{generation}".encode()).hexdigest().upper()


# ---------------------------------------------------------------- tokens (from thrift-api)

class TokenSource:
    """Short-lived eBay access tokens from thrift-api: `GET /ebay/token?kind=user|app` with the Mac's function key.
    A token is kept until two minutes before it ends. `fetch(kind)` → {"access_token", "expires_at"}; it raises
    NotConnected for a consent that isn't there, EbayError(infra) when thrift-api can't be reached."""

    EARLY = 120.0

    def __init__(self, fetch=None):
        self._fetch = fetch or self._from_api
        self._kept: dict[str, tuple[str, float]] = {}
        self.connected: bool | None = None          # the last answer about the consent (None: not asked yet)
        self.asked_at = 0.0
        self.why = ""

    @staticmethod
    def _from_api(kind: str) -> dict:
        from thrift_agent import sales
        api = sales.api()
        if api is None:
            raise NotConnected("thrift-api isn't set up on this machine (THRIFT_API_URL / THRIFT_API_KEY)")
        try:                                             # a short wait: the caller is a site worker's turn
            return sales.Api(api.url, api.key, timeout=8.0).get("/ebay/token", {"kind": kind})
        except sales.ApiError as e:
            if e.status in (404, 409, 410):
                raise NotConnected("eBay isn't connected: thrift-api has no working consent") from None
            raise EbayError(f"thrift-api didn't give a token ({e})", INFRA, e.status) from None

    def token(self, kind: str = "user", fresh: bool = False) -> str:
        now = time.time()
        kept = self._kept.get(kind)
        if kept and not fresh and kept[1] - self.EARLY > now:
            return kept[0]
        self.asked_at = now
        try:
            answer = self._fetch(kind)
        except NotConnected as e:
            if kind == "user":
                self.connected, self.why = False, str(e)
            raise
        token = str((answer or {}).get("access_token") or "")
        if not token:
            raise EbayError("thrift-api answered without a token", INFRA)
        ends = _epoch((answer or {}).get("expires_at")) or now + 600
        self._kept[kind] = (token, ends)
        if kind == "user":
            self.connected, self.why = True, ""
        return token

    def forget(self, kind: str = "user") -> None:
        self._kept.pop(kind, None)


def _epoch(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()


# ---------------------------------------------------------------- a listing as eBay shows it

@dataclass
class Live:
    """One listing read back from eBay."""
    listing_id: str
    sku: str | None = None
    status: str | None = None                # ACTIVE | ENDED | SCHEDULED
    on_hold: bool = False                    # removed by eBay for a policy (its words in `hold`)
    hold: str | None = None
    title: str | None = None
    description: str | None = None
    price: float | None = None
    available: int | None = None
    sold: int | None = None
    photos: list[str] = field(default_factory=list)
    aspects: dict[str, list[str]] = field(default_factory=dict)
    category_id: str | None = None
    condition_id: str | None = None
    best_offer: bool | None = None
    format: str | None = None
    duration: str | None = None
    postal_code: str | None = None
    country: str | None = None
    returns_accepted: bool | None = None
    returns_days: str | None = None
    returns_payer: str | None = None
    shipping_type: str | None = None
    shipping_service: str | None = None
    shipping_cost: float | None = None
    handling: str | None = None
    end_reason: str | None = None
    ended_at: str | None = None
    created_at: str | None = None
    url: str | None = None

    @property
    def for_sale(self) -> bool:
        return self.status == "ACTIVE" and not self.on_hold and (self.available is None or self.available > 0)

    @property
    def was_sold(self) -> bool:
        return bool(self.sold) and not self.for_sale


def _money(node) -> float | None:
    """ConvertedAmount / Amount / a bare Decimal → a number."""
    if isinstance(node, dict):
        node = node.get("original") or node
        node = node.get("value") if isinstance(node, dict) else node
    try:
        return float(node) if node is not None else None
    except (TypeError, ValueError):
        return None


def live_from(listing: dict | None, item_base: str) -> Live | None:
    """A SellerListing (a mutation's answer) or a ListingV2 (a query's) as `Live`."""
    if not isinstance(listing, dict) or not listing.get("listingId"):
        return None
    items = listing.get("items") or []
    first = items[0] if items else {}
    terms = first.get("itemTerms") or {}
    product = listing.get("sellerProduct") or {}
    lt = listing.get("listingTerms") or {}
    life = lt.get("lifecycleTerms") or {}
    ret = ((lt.get("listingReturnTerms") or {}).get("domesticReturnTerms")) or {}
    ship = ((lt.get("listingFulfillmentTerms") or {}).get("shippingTerms")) or {}
    services = sorted(ship.get("shippingServices") or [], key=lambda s: s.get("rank") or 99)
    holds = ((listing.get("listingOnHoldPolicies") or {}).get("onHoldPolicies")) or []
    images = sorted(product.get("images") or [], key=lambda i: i.get("order") or 0)
    lid = str(listing["listingId"])
    return Live(
        listing_id=lid, sku=listing.get("sku") or first.get("sku"), status=life.get("listingStatus"),
        on_hold=bool(life.get("onHold") or holds),
        hold="; ".join(str(h.get("policyViolationMessage") or h.get("policyName") or "") for h in holds)[:300] or None,
        title=product.get("title"), description=product.get("description"),
        price=_money((terms.get("pricingTerms") or {}).get("fixedPrice")),
        available=(terms.get("availability") or {}).get("quantityAvailable"),
        sold=(terms.get("availability") or {}).get("quantitySold"),
        photos=[str(i.get("ebayImageUrl") or i.get("externalImageUrl") or "") for i in images],
        aspects={str(a.get("name")): [str(v) for v in a.get("values") or []] for a in product.get("aspects") or []
                 if a.get("name")},
        category_id=str((((product.get("categories") or {}).get("primaryCategory")) or {}).get("categoryId") or "") or None,
        condition_id=str((product.get("itemCondition") or {}).get("itemConditionId") or "") or None,
        best_offer=(lt.get("bestOfferTerms") or {}).get("acceptBestOffer"), format=lt.get("listingFormat"),
        duration=life.get("listingDurationInDays"), postal_code=(lt.get("itemLocation") or {}).get("postalCode"),
        country=(lt.get("itemLocation") or {}).get("country"), returns_accepted=ret.get("returnAccepted"),
        returns_days=ret.get("returnDuration"), returns_payer=ret.get("returnShipmentInvoicee"),
        shipping_type=(ship.get("marketplaceShippingOptions") or {}).get("shippingCostType"),
        shipping_service=((services[0].get("shippingService") or {}).get("shippingServiceCode")) if services else None,
        shipping_cost=_money(services[0].get("shippingCostAmount")) if services else None,
        handling=ship.get("handlingTimeDuration"), end_reason=life.get("endListingReason"),
        ended_at=life.get("actualEndAt"), created_at=life.get("createdAt"), url=f"{item_base}{lid}")


# ---------------------------------------------------------------- the GraphQL documents

_LISTING = """
  listingId
  items { sku itemTerms { pricingTerms { fixedPrice { original { value currency } } }
                          availability { quantityAvailable quantitySold } } }
  sellerProduct { title description aspects { name values } images { ebayImageUrl order }
                  itemCondition { itemConditionId displayName }
                  categories { primaryCategory { categoryId name } } }
  listingTerms { listingFormat
    lifecycleTerms { listingStatus onHold createdAt actualEndAt endListingReason listingDurationInDays }
    itemLocation { country postalCode location }
    bestOfferTerms { acceptBestOffer }
    listingReturnTerms { domesticReturnTerms { returnAccepted returnDuration returnShipmentInvoicee
                                               listingRefundMethod } }
    listingFulfillmentTerms { shippingTerms { handlingTimeDuration
      marketplaceShippingOptions { shippingCostType }
      shippingServices { rank shippingCostAmount { original { value currency } }
                         shippingService { shippingServiceCode displayName } } } } }
"""
_WARN = "warnings { warningCode warningMessage }"
_VIOL = "violations { violationCode violationDescription inputPath }"
_FEES = "listingFees { listingFeeType feeAmount { value currency } }"
CREATE = f"""mutation CreateListing($input: CreateListingInput!) {{ createListing(input: $input) {{ __typename
  ... on CreateListingSuccess {{ listing {{ sku {_LISTING} }} {_FEES} {_WARN} }}
  ... on CreateListingValidationSuccess {{ listing {{ listingId }} {_FEES} {_WARN} }}
  ... on CreateListingValidationError {{ errorCode errorMessage {_VIOL} {_WARN} }} }} }}"""
UPDATE = f"""mutation UpdateListing($input: UpdateListingInput!) {{ updateListing(input: $input) {{ __typename
  ... on UpdateListingSuccess {{ listing {{ sku {_LISTING} }} {_WARN} }}
  ... on UpdateListingValidationSuccess {{ listing {{ listingId }} {_WARN} }}
  ... on UpdateListingValidationError {{ errorCode errorMessage {_VIOL} {_WARN} }} }} }}"""
END = f"""mutation EndListing($input: EndListingInput!) {{ endListing(input: $input) {{ __typename
  ... on EndListingSuccess {{ listing {{ sku {_LISTING} }} {_WARN} }}
  ... on EndListingValidationError {{ errorCode errorMessage {_VIOL} {_WARN} }} }} }}"""
_ID = "listingItemIdentifier { listingId sku }"
PRICE = f"""mutation BulkUpdateInventory($input: BulkUpdateInventoryInput!) {{ bulkUpdateInventory(input: $input) {{
  __typename
  ... on BulkUpdateInventorySuccess {{ successes {{ {_ID} {_WARN} }} }}
  ... on BulkUpdateInventoryFailure {{ errorCode errorMessage failures {{ {_ID} {_VIOL} }} }}
  ... on BulkUpdateInventoryPartialFailure {{ errorCode errorMessage failures {{ {_ID} {_VIOL} }}
                                               successes {{ {_ID} }} }} }} }}"""
_HOLD = "listingOnHoldPolicies { onHoldPolicies { policyName policyViolationMessage } }"
BY_ID = f"""query SellerListings($input: SellerListingsInput!) {{ sellerListings(input: $input) {{ __typename
  ... on ListingsSuccess {{ listings {{ __typename
    ... on ListingSuccess {{ listingId listing {{ {_LISTING} {_HOLD} }} }}
    ... on ListingPartialSuccess {{ listingId listing {{ {_LISTING} {_HOLD} }} errors {{ errorCode errorMessage }} }} }} }}
  ... on ListingIdsMaxLimitError {{ errorCode errorMessage maxAllowed }} }} }}"""
_FOUND = f"""__typename
  ... on SellerListings {{ listings {{ listingId listing {{ {_LISTING} {_HOLD} }} }}
                          pagination {{ nextCursor totalElements totalPages }} }}
  ... on SellerListingSearchInvalidInput {{ errorCode errorMessage }}"""
ACTIVE = f"""query ActiveListings($search: SellerActiveListingSearchInput) {{ sellerListingSearch {{
  activeListings(searchInput: $search) {{ {_FOUND} }} }} }}"""
ENDED = f"""query EndedListings($search: SellerEndedListingSearchInput) {{ sellerListingSearch {{
  endedListings(searchInput: $search) {{ {_FOUND} }} }} }}"""
PAGE = 100
BY_ID_MAX = 20
PACKAGE_TYPE = "PACKAGE_THICK_ENVELOPE"


def _days(n: int) -> str:
    return f"DAYS_{int(n)}"


def create_input(f: mapping.EbayFields, image_urls: list[str], key: str, validate: bool = False) -> dict:
    """The createListing input for a mapped item: fixed price, Good 'Til Cancelled, one of it, SKU = our item id; the
    buyer pays the shipping (calculated from the parcel, or flat); returns as the owner set them; Best Offer on."""
    ship = f.shipping
    calculated = ship.get("type") == "calculated"
    service = {"shippingServiceCode": ship.get("service_code"), "rank": 1}
    if not calculated:
        service["shippingCost"] = f"{float(ship.get('buyer_pays') or 0):.2f}"
    fulfillment: dict[str, Any] = {"shippingTerms": {
        "handlingTimeDuration": _days(ship.get("handling_days") or 3),
        "includedShipToLocations": [{"country": f.location.get("country") or "US"}],
        "marketplaceShippingOptions": {"costType": "CALCULATED" if calculated else "FLAT"},
        "shippingServices": [service]}}
    fulfillment["packageSpecifications"] = {
        # eBay's "Package (or thick envelope)" — what all of her own listings carry; PARCEL_OR_PADDED_ENVELOPE is in
        # the schema but createListing refuses it with USPS Ground Advantage (LSTNG_SHIPPING_PACKAGE_TYPE_INVALID,
        # VALIDATE on production 2026-11-11)
        "measurementSystem": "ENGLISH", "packageType": PACKAGE_TYPE,
        "packageWeight": {"majorUnit": int(ship.get("pounds") or 0), "minorUnit": int(ship.get("ounces") or 0)},
        "packageDimensions": {"length": float(ship.get("length") or 0), "width": float(ship.get("width") or 0),
                              "height": float(ship.get("height") or 0)}}
    ret = f.returns
    returns = {"returnAccepted": bool(ret.get("accepted"))}
    if ret.get("accepted"):
        returns.update(returnDuration=_days(ret.get("days") or 30), refundMethod="MONEY_BACK",
                       returnShipmentInvoicee=str(ret.get("payer") or "BUYER").upper())
    place = ", ".join(p for p in (f.location.get("city"), f.location.get("state")) if p)
    out = {
        "idempotencyKey": key, "marketplace": MARKETPLACE, "sku": f.sku,
        "product": {"title": f.title, "description": mapping.description_html(f.description),
                    "categories": {"primaryCategoryId": f.category_id},
                    "itemCondition": {"conditionId": f.condition_id},
                    "aspects": [{"name": n, "values": list(v)} for n, v in f.aspects.items() if v],
                    "imageUrls": list(image_urls)},
        "listingItems": {"items": [{"sku": f.sku, "itemTerms": {
            "availability": {"quantityAvailable": 1},
            "pricingTerms": {"fixedPrice": f"{float(f.price):.2f}"}}}]},
        "listingTerms": {
            "listingFormat": "FIXED_PRICE", "lifecycleTerms": {"listingDurationInDays": "GTC"},
            "itemLocation": {"country": f.location.get("country") or "US",
                             "postalCode": str(f.location.get("postal_code") or ""), **({"location": place} if place else {})},
            "bestOfferTerms": {"bestOfferEnabled": bool(f.best_offer)},
            "fulfillmentTerms": fulfillment,
            "returnTerms": {"domesticReturnTerms": returns}},
    }
    if validate:
        out["options"] = {"operationMode": "VALIDATE"}
    return out


@dataclass
class Created:
    listing_id: str | None                   # None for a validation (nothing was created)
    live: Live | None = None
    fees: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    validated: bool = False

    @property
    def insertion_fee(self) -> float:
        return sum(_money(x.get("feeAmount")) or 0.0 for x in self.fees
                   if "INSERTION" in str(x.get("listingFeeType") or "").upper())


def _warnings(node: dict) -> list[str]:
    return [f"{w.get('warningMessage') or ''} ({w.get('warningCode')})".strip() for w in node.get("warnings") or []]


def refusal(node: dict, what: str) -> EbayError:
    """eBay's validation error as ours: its violations in words; a policy refusal or a limit is told apart."""
    violations = [{"code": v.get("violationCode"), "text": v.get("violationDescription"),
                   "path": v.get("inputPath") or []} for v in node.get("violations") or []]
    said = "; ".join(f"{v['text']} ({v['code']})" for v in violations if v.get("text")) or \
        str(node.get("errorMessage") or node.get("errorCode") or "no reason given")
    text = f"{said} {node.get('errorMessage') or ''}"
    kind = LIMIT if LIMIT_WORDS.search(text) else POLICY if POLICY_WORDS.search(text) else ITEM
    return EbayError(f"eBay refused the {what}: {said}"[:600], kind, 200, str(node.get("errorCode") or "") or None,
                     violations)


# ---------------------------------------------------------------- the client

class EbayClient:
    """eBay's API for one seller account. `http`: an httpx.AsyncClient (the tests hand one with a fake transport)."""

    def __init__(self, tokens: TokenSource, environment: str = "production", http: httpx.AsyncClient | None = None,
                 cache_dir: Path | None = None, cache_days: float = 7.0):
        if environment not in HOSTS:
            raise ValueError(f"marketplaces.ebay.environment must be production or sandbox, not {environment!r}")
        self.tokens, self.environment, self.hosts = tokens, environment, HOSTS[environment]
        self._http = http
        self.cache_dir, self.cache_days = cache_dir, cache_days
        self.calls: dict[str, int] = {}              # how many calls of each kind this process made (doctor)

    def item_url(self, listing_id: str) -> str:
        return f"{self.hosts['item']}{listing_id}"

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=TIMEOUT)
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _send(self, what: str, method: str, url: str, *, token_kind: str = "user", **kw) -> httpx.Response:
        """One HTTP call with a token; a 401 gets ONE more try with a fresh token. Raises EbayError."""
        http = await self._client()
        self.calls[what] = self.calls.get(what, 0) + 1
        for attempt in (0, 1):
            token = self.tokens.token(token_kind, fresh=attempt == 1)
            headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
                       "Accept-Language": "en-US", **(kw.pop("headers", None) or {})}
            try:
                r = await http.request(method, url, headers=headers, **kw)
            except httpx.TimeoutException as e:
                raise EbayError(f"{what}: no answer from eBay ({type(e).__name__})", INFRA, unknown=True) from None
            except httpx.HTTPError as e:
                sent = not isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
                raise EbayError(f"{what}: {type(e).__name__}", INFRA, unknown=sent) from None
            if r.status_code == 401 and attempt == 0:
                self.tokens.forget(token_kind)
                continue
            return r
        return r

    @staticmethod
    def _rest_error(what: str, r: httpx.Response) -> EbayError:
        try:
            errors = (r.json() or {}).get("errors") or []
        except ValueError:
            errors = []
        said = "; ".join(f"{e.get('message') or e.get('longMessage') or ''} ({e.get('errorId')})" for e in errors)[:400]
        if r.status_code in (401, 403):
            kind = AUTH
        elif r.status_code == 429:
            kind = LIMIT
        elif r.status_code >= 500:
            kind = SITE
        else:
            kind = ITEM
        return EbayError(f"{what}: HTTP {r.status_code}" + (f" — {said}" if said else ""), kind, r.status_code,
                         str(errors[0].get("errorId")) if errors else None, unknown=r.status_code >= 500)

    # ------------------------------------------------------------ taxonomy and metadata (recorded per category)

    def _cached(self, name: str) -> dict | None:
        if self.cache_dir is None:
            return None
        file = self.cache_dir / name
        try:
            if self.cache_days and time.time() - file.stat().st_mtime > self.cache_days * 86400:
                return None
            return json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _keep(self, name: str, answer: dict) -> None:
        if self.cache_dir is None:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / name).write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    async def _get(self, what: str, path: str, params: dict, cache: str) -> dict:
        if (hit := self._cached(cache)) is not None:
            return hit
        r = await self._send(what, "GET", self.hosts["api"] + path, token_kind="app", params=params)
        if r.status_code == 204:
            answer: dict = {}
        elif r.status_code != 200:
            raise self._rest_error(what, r)
        else:
            try:
                answer = r.json()
            except ValueError:
                raise EbayError(f"{what}: eBay's response isn't JSON", SITE, r.status_code) from None
        self._keep(cache, answer)
        return answer

    async def suggest_raw(self, query: str) -> dict:
        name = "suggest_" + hashlib.md5(query.strip().lower().encode()).hexdigest()[:16] + ".json"
        return await self._get("category suggestions", f"/commerce/taxonomy/v1/category_tree/{TREE}/"
                               "get_category_suggestions", {"q": query}, name)

    async def suggest(self, query: str) -> list[mapping.Suggestion]:
        return mapping.parse_suggestions(await self.suggest_raw(query))

    async def aspects_raw(self, category_id: str) -> dict:
        return await self._get("item aspects", f"/commerce/taxonomy/v1/category_tree/{TREE}/"
                               "get_item_aspects_for_category", {"category_id": category_id},
                               f"aspects_{category_id}.json")

    async def conditions_raw(self, category_id: str) -> dict:
        return await self._get("condition policies", f"/sell/metadata/v1/marketplace/{MARKETPLACE}/"
                               "get_item_condition_policies", {"filter": f"categoryIds:{{{category_id}}}"},
                               f"conditions_{category_id}.json")

    async def service_code(self, name: str, default: str) -> str:
        """eBay's own code for a shipping service by its name ("USPS Ground Advantage") — read from eBay's list of
        the services a US listing may name (Metadata getShippingServices), never typed from memory; `default` when the
        list can't be read or doesn't name it (the listing's validation then says so)."""
        try:
            answer = await self._get("shipping services", f"/sell/metadata/v1/shipping/marketplace/{MARKETPLACE}/"
                                     "get_shipping_services", {}, "shipping_services.json")
        except EbayError:
            return default
        want = mapping._key(name)
        hits = [s for s in answer.get("shippingServices") or []
                if s.get("validForSellingFlow", True) and not s.get("internationalService")
                and want in (mapping._key(s.get("description")), mapping._key(s.get("shippingService")))]
        return str(hits[0].get("shippingService")) if hits else default

    async def category_for(self, view) -> tuple[mapping.Category, str | None]:
        """The item's eBay category with its item specifics and conditions: eBay's suggester asked what the item is
        (`mapping.category_queries`, in order) until an answer holds a category of the item's own family and
        department; else the closest of everything it suggested, with the note that says so."""
        answers: list[list[mapping.Suggestion]] = []
        for query in mapping.category_queries(view):
            found = await self.suggest(query)
            answers.append(found)
            if found and mapping.choose_category(view, found)[1] is None:
                break
        chosen, note = mapping.pick_category(view, answers)
        return await self.category(chosen), note

    async def categories_for(self, view) -> list[tuple[mapping.Suggestion, str | None]]:
        """The item's eBay categories to try, the picked one first (`mapping.ranked_categories`): every query asked
        (eBay's answers are kept 7 days)."""
        answers = [await self.suggest(query) for query in mapping.category_queries(view)]
        return mapping.ranked_categories(view, answers)

    async def category(self, suggestion: mapping.Suggestion) -> mapping.Category:
        """A suggested category with its item specifics and the conditions it takes."""
        aspects = mapping.parse_aspects(await self.aspects_raw(suggestion.id))
        conditions = mapping.parse_conditions(await self.conditions_raw(suggestion.id))
        return mapping.Category(id=suggestion.id, name=suggestion.name, path=suggestion.path, ids=suggestion.ids,
                                aspects=aspects, conditions=conditions)

    # ------------------------------------------------------------ photos

    async def upload_image(self, path: Path | str) -> str:
        """One photo's bytes to eBay Picture Services; its EPS address (what a listing names)."""
        p = Path(path)
        try:
            data = p.read_bytes()
        except OSError as e:
            raise EbayError(f"photo {p.name}: {type(e).__name__}", ITEM) from None
        r = await self._send("photo upload", "POST", f"{self.hosts['media']}/commerce/media/v1_beta/image/"
                             "create_image_from_file", files={"image": (p.name, data, "image/jpeg")},
                             timeout=UPLOAD_TIMEOUT)
        if r.status_code not in (200, 201):
            raise self._rest_error(f"photo {p.name}", r)
        try:
            url = str((r.json() or {}).get("imageUrl") or "")
        except ValueError:
            url = ""
        if not url.startswith("https://"):
            raise EbayError(f"photo {p.name}: eBay answered without an image address", SITE, r.status_code)
        return url

    # ------------------------------------------------------------ the Listing API

    async def graphql(self, what: str, document: str, variables: dict, field_name: str) -> dict:
        """One GraphQL operation; its field's own object. HTTP 200 may still carry errors: eBay's `errors` array."""
        r = await self._send(what, "POST", self.hosts["graphql"], json={"query": document, "variables": variables},
                             headers={"Content-Type": "application/json"})
        if r.status_code != 200:
            raise self._rest_error(what, r)
        try:
            body = r.json() or {}
        except ValueError:
            raise EbayError(f"{what}: eBay's response isn't JSON", SITE, 200, unknown=True) from None
        node = (body.get("data") or {}).get(field_name)
        errors = body.get("errors") or []
        if node is None:
            said = "; ".join(str(e.get("message") or "") for e in errors)[:400] or "no data"
            ext = (errors[0].get("extensions") or {}) if errors else {}
            category = str(ext.get("category") or "").upper()
            code = str(ext.get("code") or "") or None
            text = f"{said} {ext.get('remedy') or ''}"
            if re.search(r"\b(?:unauthori[sz]ed|access denied|invalid (?:access )?token|insufficient (?:scope|permission))",
                         text, re.I):
                kind = AUTH
            elif LIMIT_WORDS.search(text):
                kind = LIMIT
            elif category == "BUSINESS":
                kind = POLICY if POLICY_WORDS.search(text) else ITEM
            else:
                kind = SITE                                  # REQUEST (our call is wrong) or APPLICATION (eBay's own)
            raise EbayError(f"{what}: {said}", kind, 200, code, unknown=category == "APPLICATION")
        return node

    async def create(self, f: mapping.EbayFields, image_urls: list[str], key: str, validate: bool = False) -> Created:
        """createListing — `validate`: eBay checks everything and creates nothing (its VALIDATE mode)."""
        what = "validation" if validate else "listing"
        node = await self.graphql(f"createListing ({what})", CREATE,
                                  {"input": create_input(f, image_urls, key, validate)}, "createListing")
        kind = node.get("__typename")
        if kind == "CreateListingValidationError":
            raise refusal(node, what)
        if kind == "CreateListingValidationSuccess":
            return Created(None, None, list(node.get("listingFees") or []), _warnings(node), validated=True)
        if kind != "CreateListingSuccess":
            raise EbayError(f"createListing: an answer we don't know ({kind})", SITE, 200, unknown=not validate)
        live = live_from(node.get("listing"), self.hosts["item"])
        if live is None or not re.fullmatch(r"\d{6,}", live.listing_id):
            raise EbayError("createListing: success without a listing id", SITE, 200, unknown=True)
        return Created(live.listing_id, live, list(node.get("listingFees") or []), _warnings(node))

    async def read(self, listing_ids: list[str]) -> dict[str, Live | None]:
        """Listings by id ({id: Live, or None for one eBay doesn't have}), BY_ID_MAX a call."""
        out: dict[str, Live | None] = {}
        ids = [str(i) for i in dict.fromkeys(listing_ids) if i]
        for start in range(0, len(ids), BY_ID_MAX):
            part = ids[start:start + BY_ID_MAX]
            node = await self.graphql("sellerListings", BY_ID, {"input": {"listings": [{"listingId": i} for i in part]}},
                                      "sellerListings")
            if node.get("__typename") != "ListingsSuccess":
                raise EbayError(f"sellerListings: {node.get('errorMessage') or node.get('__typename')}", SITE, 200)
            for entry in node.get("listings") or []:
                out[str(entry.get("listingId"))] = live_from(entry.get("listing"), self.hosts["item"])
            for i in part:
                out.setdefault(i, None)
        return out

    async def read_one(self, listing_id: str) -> Live | None:
        return (await self.read([listing_id])).get(str(listing_id))

    async def _search(self, document: str, field_name: str, filters: dict | None, limit: int | None) -> tuple[list[Live], bool]:
        """A listing search page by page to its end (or `limit` listings): (the listings, reached the end)."""
        out: list[Live] = []
        cursor: str | None = None
        for _ in range(200):
            page: dict[str, Any] = {"maxPageSize": PAGE}
            if cursor:
                page["pageCursor"] = cursor
                if cursor.isdigit():
                    page["pageIndex"] = int(cursor)
            search = {"pagination": page, **({"filters": filters} if filters else {})}
            node = (await self.graphql(field_name, document, {"search": search}, "sellerListingSearch")).get(field_name) or {}
            if node.get("__typename") != "SellerListings":
                raise EbayError(f"{field_name}: {node.get('errorMessage') or node.get('__typename')}", SITE, 200)
            for entry in node.get("listings") or []:
                if (live := live_from(entry.get("listing"), self.hosts["item"])) is not None:
                    out.append(live)
            cursor = (node.get("pagination") or {}).get("nextCursor")
            if not cursor or not node.get("listings"):
                return out, True
            if limit and len(out) >= limit:
                return out, False
        return out, False

    async def by_sku(self, sku: str) -> list[Live]:
        """Every listing of the seller with this SKU — active first, then ended (the newest first)."""
        active, _ = await self._search(ACTIVE, "activeListings", {"skus": [sku]}, None)
        ended, _ = await self._search(ENDED, "endedListings", {"skus": [sku]}, None)
        mine = [x for x in [*active, *ended] if (x.sku or "") == sku]
        return sorted(mine, key=lambda x: (x.status != "ACTIVE", -(_epoch(x.created_at) or 0)))

    async def active(self) -> tuple[list[Live], bool]:
        """Every active listing of the seller (hers by hand too: the caller keeps only ours, by SKU)."""
        return await self._search(ACTIVE, "activeListings", None, None)

    async def set_price(self, listing_id: str, price: float) -> list[str]:
        """The price of a fixed-price listing (bulkUpdateInventory). Returns eBay's warnings."""
        node = await self.graphql("bulkUpdateInventory", PRICE, {"input": {"inventory": [{
            "listingItemIdentifier": {"listingId": str(listing_id)}, "fixedPrice": f"{float(price):.2f}"}]}},
            "bulkUpdateInventory")
        if node.get("__typename") != "BulkUpdateInventorySuccess":
            failures = node.get("failures") or [{}]
            raise refusal({**node, "violations": failures[0].get("violations") or []}, "price change")
        return [w for s in node.get("successes") or [] for w in _warnings(s)]

    async def update(self, listing_id: str, *, title: str | None = None, description: str | None = None,
                     image_urls: list[str] | None = None, aspects: dict[str, list[str]] | None = None,
                     category_id: str | None = None, validate: bool = False) -> Live | None:
        """updateListing: only what is given changes (a list given replaces the listing's whole list)."""
        product: dict[str, Any] = {}
        if title is not None:
            product["title"] = title
        if description is not None:
            product["description"] = mapping.description_html(description)
        if image_urls is not None:
            product["imageUrls"] = list(image_urls)
        if aspects is not None:
            product["aspects"] = [{"name": n, "values": list(v)} for n, v in aspects.items() if v]
        if category_id is not None:
            product["categories"] = {"primaryCategoryId": str(category_id)}
        if not product:
            return await self.read_one(listing_id)
        variables = {"input": {"listingIdentifier": {"listingId": str(listing_id)}, "marketplace": MARKETPLACE,
                               "product": product, **({"options": {"operationMode": "VALIDATE"}} if validate else {})}}
        node = await self.graphql("updateListing", UPDATE, variables, "updateListing")
        kind = node.get("__typename")
        if kind == "UpdateListingValidationError":
            raise refusal(node, "change")
        if kind == "UpdateListingValidationSuccess":
            return None
        if kind != "UpdateListingSuccess":
            raise EbayError(f"updateListing: an answer we don't know ({kind})", SITE, 200, unknown=True)
        return live_from(node.get("listing"), self.hosts["item"])

    async def end(self, listing_id: str, reason: str = "NOT_AVAILABLE") -> Live | None:
        """endListing. A listing that has ended already is no error (the answer is read by the caller)."""
        node = await self.graphql("endListing", END, {"input": {
            "endListingReason": reason, "listingIdentifier": {"listingId": str(listing_id)}}}, "endListing")
        if node.get("__typename") == "EndListingValidationError":
            raise refusal(node, "ending")
        if node.get("__typename") != "EndListingSuccess":
            raise EbayError(f"endListing: an answer we don't know ({node.get('__typename')})", SITE, 200, unknown=True)
        return live_from(node.get("listing"), self.hosts["item"])
