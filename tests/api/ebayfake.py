"""eBay as thrift-api talks to it (WO57), for the tests: one object in place of thrift_api.ebay.SENDER — the token
endpoint (the three grants), the GraphQL Listing API (a listing read by id, the active / ended ones by SKU, endListing)
and the Fulfillment API's orders (with the buyer's name, address and notes in every order, as eBay sends them: the
tests assert none of it is ever kept). Every call is recorded; nothing reaches the network."""
import base64
import json
from urllib.parse import parse_qs, urlsplit

from apitools import BUYER, ITEM, NOW, TITLE, call

CLIENT, SECRET, RUNAME = "client-id-PRD-1234", "client-secret-5678", "Thrift_Shop-ThriftSh-thrift-abcdefg"
VERIFICATION = "v" * 20 + "_-" + "A1b2C3" * 7                    # 64 characters of the allowed kind
ENDPOINT = "https://thrift-api.example.net/ebay/account-deletion"
START_LINK = "https://thrift-api.example.net/ebay/oauth/start?code=start-key-0123"
CODE = "v^1.1#i^1#f^0#r^1#p^3#I^3#t^Ul41XzA6the-auth-code"
REFRESH = "v^1.1#i^1#p^3#r^1#the-refresh-token-zYjRV4xMjg0"
LISTING = "325000000001"                                         # the eBay listing id of apitools.ITEM
EBAY_URL = f"https://www.ebay.com/itm/{LISTING}"
NOTES = "Leave it with the neighbour at number 9, thanks — Jane"
EMAIL = "jane.q.buyer@example.com"
PRIVATE = (*BUYER, NOTES, EMAIL, "Springfield")                  # what must never be stored or logged


class StandIn:
    """Where the `cryptography` package isn't installed (the Mac's venv has no Azure packages): a reversible stand-in
    for Fernet, so everything around the sealing is tested there too. The sealing itself: test_api_ebay.py, with the
    real package."""

    def encrypt(self, data: bytes) -> bytes:
        return b"sealed:" + base64.urlsafe_b64encode(data[::-1])

    def decrypt(self, data: bytes) -> bytes:
        if not data.startswith(b"sealed:"):
            raise ValueError("not sealed here")
        return base64.urlsafe_b64decode(data[7:])[::-1]


def order(order_id: str = "12-34567-89012", sku: str | None = ITEM, listing: str = LISTING, title: str = TITLE,
          price: str = "35.0", created: str = "2026-10-08T14:05:00.000Z", status: str = "NOT_STARTED",
          cancel: str = "NONE_REQUESTED", payment: str = "PAID", ship_by: str | None = "2026-10-13T06:59:59.000Z",
          line: str = "10011111111111") -> dict:
    """One order as getOrders answers it — with everything about the buyer eBay puts in."""
    item = {"lineItemId": line, "legacyItemId": listing, "title": title, "quantity": 1,
            "lineItemCost": {"value": price, "currency": "USD"}, "total": {"value": "42.97", "currency": "USD"},
            "lineItemFulfillmentStatus": status, "soldFormat": "FIXED_PRICE",
            "lineItemFulfillmentInstructions": {"shipByDate": ship_by} if ship_by else {}}
    if sku:
        item["sku"] = sku
    return {"orderId": order_id, "creationDate": created, "lastModifiedDate": created,
            "orderFulfillmentStatus": status, "orderPaymentStatus": payment,
            "cancelStatus": {"cancelState": cancel, "cancelRequests": []}, "sellerId": "thrift_shop",
            "buyer": {"username": BUYER[1], "taxAddress": {"city": "Springfield", "postalCode": "62704"},
                      "buyerRegistrationAddress": {"fullName": BUYER[0], "email": EMAIL,
                                                   "contactAddress": {"addressLine1": BUYER[2], "city": "Springfield"}}},
            "buyerCheckoutNotes": NOTES,
            "fulfillmentStartInstructions": [{"shippingStep": {"shipTo": {
                "fullName": BUYER[0], "email": EMAIL, "contactAddress": {"addressLine1": BUYER[2], "city": BUYER[3]}}}}],
            "pricingSummary": {"total": {"value": "42.97", "currency": "USD"}}, "lineItems": [item]}


class FakeEbay:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []       # (what, detail): token/<grant>, graphql/<operation>, orders
        self.requests: list[dict] = []               # every request as it came: method, url, headers, body
        self.down = False                            # the network: every call raises
        self.token_status = 200                      # the token endpoint's answer to a refresh / client-credentials
        self.refresh_life: int | None = 365 * 86400  # refresh_token_expires_in (None: eBay names none)
        self.graphql_status = 200
        self.graphql_errors: list | None = None      # HTTP 200 carrying `errors`
        self.end_refused = False
        self.username: str | None = "Thrift_Shop"    # whose token it is, as introspection answers (None: no username)
        self.introspect_status = 200
        self.sells_as_it_ends = False
        self.orders_status = 200
        self.orders: list[dict] = []
        self.page_size = 200
        self.listings: dict[str, dict] = {}          # listing id → {"sku", "state", "sold"}
        self.ended: list[str] = []                   # the listing ids endListing ended
        self.tokens: set[str] = set()                # the access tokens handed out
        self.minted = 0

    # --- what the tests set up

    def listing(self, listing_id: str = LISTING, sku: str | None = ITEM, state: str = "ACTIVE", sold: int = 0) -> None:
        self.listings[listing_id] = {"sku": sku, "state": state, "sold": sold}

    def count(self, what: str) -> int:
        return sum(1 for name, _ in self.calls if name == what)

    # --- eBay

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None, timeout: float):
        self.requests.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if self.down:
            raise OSError(f"connection refused while sending {body!r}")
        host, path = urlsplit(url).netloc, urlsplit(url).path
        if path == "/identity/v1/oauth2/token/introspect":
            self.calls.append(("token", "introspect"))
            form = {k: v[0] for k, v in parse_qs((body or b"").decode()).items()}
            assert form["token"] in self.tokens and form["token_type_hint"] == "access_token"
            assert headers["Authorization"].startswith("Basic ")
            if self.introspect_status != 200:
                return self.introspect_status, b'{"error": "server_error"}'
            answer = {"active": True, "token_type": "bearer", "client_id": CLIENT}
            if self.username:
                answer["username"] = self.username
            return 200, json.dumps(answer).encode()
        if path == "/identity/v1/oauth2/token":
            return self._token(headers, body)
        if host.startswith("graphqlapi."):
            return self._graphql(headers, body)
        if path == "/sell/fulfillment/v1/order":
            return self._orders(url, headers)
        raise AssertionError(f"not an eBay address the API should call: {url}")

    def _access(self, kind: str) -> str:
        self.minted += 1
        token = f"{kind}-access-token-{self.minted}"
        self.tokens.add(token)
        return token

    def _token(self, headers: dict, body: bytes | None):
        assert headers["Content-Type"] == "application/x-www-form-urlencoded"
        assert headers["Authorization"] == "Basic " + base64.b64encode(f"{CLIENT}:{SECRET}".encode()).decode()
        form = {k: v[0] for k, v in parse_qs((body or b"").decode()).items()}
        grant = form.get("grant_type")
        self.calls.append(("token", grant))
        refused = {"error": "invalid_grant", "error_description": f"the provided token {REFRESH} is invalid"}
        if grant == "authorization_code":
            if form.get("code") != CODE or form.get("redirect_uri") != RUNAME:
                return 400, json.dumps(refused).encode()
            answer = {"access_token": self._access("user"), "expires_in": 7200, "refresh_token": REFRESH,
                      "token_type": "User Access Token"}
            if self.refresh_life is not None:
                answer["refresh_token_expires_in"] = self.refresh_life
            return 200, json.dumps(answer).encode()
        if self.token_status != 200:
            return self.token_status, json.dumps(refused).encode()
        if grant == "refresh_token":
            if form.get("refresh_token") != REFRESH:
                return 400, json.dumps(refused).encode()
            return 200, json.dumps({"access_token": self._access("user"), "expires_in": 7200}).encode()
        if grant == "client_credentials":
            assert form.get("scope") == "https://api.ebay.com/oauth/api_scope"
            return 200, json.dumps({"access_token": self._access("app"), "expires_in": 7200}).encode()
        raise AssertionError(f"an unknown grant: {grant}")

    def _bearer(self, headers: dict) -> bool:
        return headers.get("Authorization", "").removeprefix("Bearer ") in self.tokens

    def _node(self, listing_id: str) -> dict | None:
        row = self.listings.get(str(listing_id))
        if row is None:
            return None
        ended = row["state"] == "ENDED"
        item = {"itemTerms": {"availability": {"quantityAvailable": 0 if ended else 1, "quantitySold": row["sold"]}}}
        if row["sku"]:
            item["sku"] = row["sku"]
        return {"listingId": str(listing_id), "items": [item],
                "listingTerms": {"lifecycleTerms": {"listingStatus": row["state"],
                                                    "actualEndAt": "2026-10-08T15:00:01.000Z" if ended else None}}}

    def _graphql(self, headers: dict, body: bytes | None):
        assert headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US" and headers["Content-Type"] == "application/json"
        sent = json.loads(body or b"{}")
        query, variables = sent["query"], sent["variables"]
        operation = next(name for name in ("endListing", "sellerListings", "activeListings", "endedListings")
                         if f"{name}(" in query)
        self.calls.append(("graphql", operation))
        assert not any(word in query for word in (ITEM, *self.listings)), "an input written into the query itself"
        if self.graphql_status != 200:
            return self.graphql_status, b'{"errors": [{"message": "Internal error for token ' + REFRESH.encode() + b'"}]}'
        if not self._bearer(headers):
            return 401, b'{"errors": [{"message": "Invalid access token"}]}'
        if self.graphql_errors:
            return 200, json.dumps({"errors": self.graphql_errors, "data": None}).encode()
        if operation == "sellerListings":
            listing_id = variables["input"]["listings"][0]["listingId"]
            data = {"sellerListings": {"__typename": "ListingsSuccess", "listings": [
                {"__typename": "ListingSuccess", "listingId": listing_id, "listing": self._node(listing_id)}]}}
        elif operation in ("activeListings", "endedListings"):
            skus = variables["search"]["filters"]["skus"]
            assert variables["search"]["filters"]["marketplaces"] == ["EBAY_US"]
            want_ended = operation == "endedListings"
            hits = [lid for lid, row in self.listings.items()
                    if row["sku"] in skus and (row["state"] == "ENDED") == want_ended]
            data = {"sellerListingSearch": {operation: {"__typename": "SellerListings", "listings": [
                {"listingId": lid, "listing": self._node(lid)} for lid in hits]}}}
        else:
            given = variables["input"]
            listing_id = given["listingIdentifier"]["listingId"]
            assert given["endListingReason"] == "NOT_AVAILABLE"
            row = self.listings.get(listing_id)
            if self.sells_as_it_ends and row is not None:        # a buyer was a moment faster
                row.update(state="ENDED", sold=1)
            if self.end_refused or row is None or row["state"] == "ENDED":
                data = {"endListing": {"__typename": "EndListingValidationError", "errorCode": "LSTNG_END_VALIDATION_ERROR",
                                       "violations": [{"violationCode": "LSTNG_ALREADY_ENDED"}]}}
            else:
                row["state"] = "ENDED"
                self.ended.append(listing_id)
                data = {"endListing": {"__typename": "EndListingSuccess", "listing": self._node(listing_id)}}
        return 200, json.dumps({"data": data}).encode()

    def _orders(self, url: str, headers: dict):
        self.calls.append(("orders", url))
        if self.orders_status != 200:
            return self.orders_status, json.dumps({"errors": [{"message": f"down, {BUYER[0]}"}]}).encode()
        if not self._bearer(headers):
            return 401, b'{"errors": [{"errorId": 1001, "message": "Invalid access token"}]}'
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert query["filter"].startswith("lastmodifieddate:[") and query["filter"].endswith("..]")
        offset = int(query.get("offset", 0))
        page = self.orders[offset:offset + self.page_size]
        answer = {"href": url, "total": len(self.orders), "limit": self.page_size, "offset": offset, "orders": page}
        if offset + self.page_size < len(self.orders):
            answer["next"] = url.split("&offset=")[0] + f"&offset={offset + self.page_size}"
        return 200, json.dumps(answer).encode()

    def since(self, n: int = -1) -> str:
        """The start of the window the n-th orders read asked for ("2026-10-05T15:00:00.000Z")."""
        urls = [detail for name, detail in self.calls if name == "orders"]
        query = parse_qs(urlsplit(urls[n]).query)["filter"][0]
        return query.removeprefix("lastmodifieddate:[").removesuffix("..]")


def install(monkeypatch) -> FakeEbay:
    """eBay set up as provision.py sets it up (the keyset, the endpoint, the token key, the consent link) and the fake
    in place of the one way out."""
    from thrift_api import ebay
    try:
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
    except ImportError:
        key = "a-stand-in-key"
        monkeypatch.setattr(ebay, "_cipher", StandIn)
    for name, value in (("EBAY_CLIENT_ID", CLIENT), ("EBAY_CLIENT_SECRET", SECRET), ("EBAY_RUNAME", RUNAME),
                        ("EBAY_VERIFICATION_TOKEN", VERIFICATION), ("EBAY_DELETION_ENDPOINT", ENDPOINT),
                        ("EBAY_TOKEN_KEY", key), ("EBAY_START_LINK", START_LINK)):
        monkeypatch.setenv(name, value)
    fake = FakeEbay()
    monkeypatch.setattr(ebay, "SENDER", fake)
    return fake


def start(db, now=NOW) -> tuple[str, dict]:
    """GET /ebay/oauth/start: (the address it redirects to, its query)."""
    status, _, headers = call(db, "GET", "/ebay/oauth/start", now=now)
    assert status == 302, status
    where = headers["Location"]
    return where, {k: v[0] for k, v in parse_qs(urlsplit(where).query).items()}


def connect(db, now=NOW, code: str = CODE) -> tuple[int, str]:
    """The owner's consent, start to callback: (the callback's status, its page)."""
    _, query = start(db, now)
    status, page, _ = call(db, "GET", "/ebay/oauth/callback", query={"code": code, "state": query["state"],
                                                                     "expires_in": "299"}, now=now)
    return status, page


def beat(db, now=NOW, **ebay) -> None:
    """The Mac's heartbeat saying eBay is on (`info["ebay"]`), with whatever else it says of it."""
    from thrift_api import core
    core.heartbeat(db, "mac", {"quiet": {"enabled": False}, "ebay": {"on": True, "live": True, **ebay}}, now)


def everything(db) -> str:
    """Every row of every table, as one text: what a test searches for something that must not be there."""
    tables = [row["name"] for row in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return json.dumps({table: db.query(f"SELECT * FROM {table}") for table in tables}, default=str)
