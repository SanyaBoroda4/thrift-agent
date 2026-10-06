"""Depop's Selling API (WO32 §4.5): `marketplaces.depop.driver: api`, for when Depop's API key arrives. A STUB: it builds
the request a listing would be — PUT /api/v1/products/{sku} with the department, the product type and the US size set
straight from data/depop_catalog.json, the photos as public (Azure Blob) addresses — and publishes nothing. Without
DEPOP_API_KEY in .env it refuses before anything is sent, and even with one it refuses until the client is written
against the API's documentation (the size ids, the photo upload, the answer's listing address)."""
from __future__ import annotations

import os

from thrift_agent import catalogs
from thrift_agent.catalogs import DEPOP_DEPARTMENT
from thrift_agent.post.base import Mode, Outcome, Poster, PosterError
from thrift_agent.post.depop import listing_address
from thrift_agent.schema import Render

API_BASE = "https://partnerapi.depop.com"
PRODUCTS = "/api/v1/products/{sku}"


def product_request(fields, sku: str, photo_urls: list[str], catalog=None) -> dict:
    """The PUT a listing would be: {"method", "path", "body"} — the catalog's ids where the API takes ids (the
    department, the product type, the size set) and the same values the form gets elsewhere."""
    cat = catalog or catalogs.depop_catalog()
    dept = fields.category.split(" > ")[0]
    product_type = cat.product_type(fields.category)
    if product_type is None:
        raise PosterError(f"depop api: no product type for {fields.category!r} in the catalog")
    size_set = cat.size_set_id(fields.category)
    body = {
        "description": fields.description,
        "price": {"amount": f"{fields.price:.2f}", "currency": "USD"},
        "department": DEPOP_DEPARTMENT.get(dept, dept.lower()),
        "product_type": product_type.split("/", 1)[1],
        "group": product_type.split("/", 1)[0],
        "brand": fields.brand,
        "condition": fields.condition,
        "colours": list(fields.colors),
        "source": list(fields.source),
        "age": fields.age,
        "style": list(fields.style),
        "attributes": dict(fields.attributes),
        "size_set_id": size_set,
        "size": fields.size,            # the API's size id: read from its size mapping once the key arrives
        "quantity": 1,
        "shipping": {"method": fields.shipping, "parcel_size": fields.package_size},
        "pictures": [{"url": u} for u in photo_urls],
    }
    return {"method": "PUT", "path": PRODUCTS.format(sku=sku), "body": {k: v for k, v in body.items()
                                                                     if v not in (None, [], {})}}


class DepopApiPoster(Poster):
    driver = "api"
    name = "depop"
    site = "Depop"
    create_url = API_BASE

    def __init__(self, shop: str = ""):
        self.shop = shop.strip()
        self.fields = None
        self.confirm = None
        self.strict = False
        self.notes, self.guesses = [], []

    def available(self) -> bool:
        return False                    # a stub: its rows wait, nothing is tried

    async def check_account(self, page) -> None:
        raise PosterError("depop api: no page")

    async def fill(self, page, r: Render) -> None:
        raise PosterError("depop api: no page")

    async def read_back(self, page) -> dict:
        raise PosterError("depop api: no page")

    def expected(self, r: Render) -> dict:
        return {}

    async def submit(self, page, mode: Mode) -> str | None:
        raise PosterError("depop api: no page")

    def listing_address(self, url: str) -> str | None:
        return listing_address(url)

    async def post(self, ctx, r: Render, mode: Mode, dry_run: bool, shots, stage: str = "form") -> Outcome:
        if not os.environ.get("DEPOP_API_KEY"):
            raise PosterError("depop api: DEPOP_API_KEY isn't set (.env) — the API driver waits for Depop's key; "
                              "set marketplaces.depop.driver back to extension meanwhile")
        raise PosterError("depop api: the client isn't written yet (WO32 stub): nothing was sent")
