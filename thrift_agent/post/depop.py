"""Depop adapter — M4. Same contract as PoshmarkPoster; record selectors on the Mac first.

Known differences to handle: photo cap (check the form), 1000-char description including 5 hashtags,
condition scale (Brand new / Like new / Used - Excellent / Good / Fair), no SKU field — the DB is the
item ↔ listing map, so keep the short item code at the end of the description as a backup.
"""
from __future__ import annotations

from playwright.async_api import Page

from thrift_agent.post.base import Mode, Poster
from thrift_agent.schema import Render

CONDITION_TO_DEPOP = {
    "NWT": "Brand new", "NWOT": "Like new", "like_new": "Like new",
    "excellent": "Used - Excellent", "good": "Used - Good", "fair": "Used - Fair",
}


class DepopPoster(Poster):
    name = "depop"
    create_url = "https://www.depop.com/products/create/"  # UNVERIFIED

    async def check_account(self, page: Page) -> None:
        raise NotImplementedError("Depop poster is M4")

    async def fill(self, page: Page, r: Render) -> None:
        raise NotImplementedError

    async def read_back(self, page: Page) -> dict:
        raise NotImplementedError

    def expected(self, r: Render) -> dict:
        return {"description": r.description, "price": r.price}

    async def submit(self, page: Page, mode: Mode) -> str | None:
        raise NotImplementedError
