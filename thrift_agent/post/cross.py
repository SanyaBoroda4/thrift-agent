"""What Depop's and Vinted's posters share (WO30), on top of Poster (post/base.py): the mapped fields of the item in
hand (catalogs.depop / catalogs.vinted, set by the runner), field-by-field filling where a dry run records a field that
won't fill and goes on (the read-back diff then fails it, with the whole form kept as evidence) while a publish stops at
the first one, the supervised confirmation, exactly one click on the site's publish button, everything after it kept
as evidence, the listing's address from the page it lands on or else from the shop, and the live page compared with
the fields (a difference is a note for the ops chat: the listing stays up)."""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Locator, Page

from thrift_agent.post.base import AccountBlocked, Cancelled, Mode, Poster, PosterError, Skipped, _norm, keep_evidence
from thrift_agent.schema import Render

AFTER_PUBLISH_MS = 45_000


class Chosen:
    """A read-back value: the field's catalog values were all picked (the option whose text IS the value)."""

    def __init__(self, values: list[str]):
        self.values = sorted(_norm(v) for v in values)

    def matches(self, seen) -> bool:
        return sorted(_norm(v) for v in (seen or [])) == self.values

    def __repr__(self) -> str:
        return f"picked {self.values}"


class Startswith:
    """A read-back value that must begin with the planned one ("Small" for "Small — up to 12 oz")."""

    def __init__(self, prefix: str):
        self.prefix = prefix

    def matches(self, seen) -> bool:
        return bool(seen) and _norm(seen).startswith(_norm(self.prefix))

    def __repr__(self) -> str:
        return f"starts with {self.prefix!r}"


class CrossPoster(Poster):
    """One of the cross-list marketplaces. Subclasses set name, base_url, create_url, SEL-backed steps, `unverified`,
    `publish_needs` (the supervised publish) and `autopublish_needs` (the unattended loop), and implement fill(),
    _account() and _shop_listings()."""
    base_url = ""
    unverified: frozenset = frozenset()
    publish_needs: frozenset = frozenset()
    autopublish_needs: frozenset = frozenset()
    site = ""                                     # "Depop" / "Vinted" for the owner's messages

    def __init__(self, shop: str = "", strict: bool = False):
        self.shop = shop.strip()                  # the shop / member page path, for the closet check
        self.fields = None
        self.notes, self.guesses = [], []
        self.confirm = None                       # the supervised publish: async (fields, site) -> bool
        self.strict = strict
        self._seen: dict = {}
        self._started = 0.0
        self._shop_before: set[str] = set()

    # ---------------------------------------------------------------- per field

    async def _step(self, name: str, coro) -> None:
        """One field. A dry run notes a field that won't fill and goes on (the read-back diff fails the item, with the
        whole form recorded); a publish never goes on past one (strict)."""
        try:
            await coro
        except (AccountBlocked, Cancelled, Skipped):
            raise
        except Exception as e:  # noqa: BLE001
            if self.strict:
                raise PosterError(f"{self.name} {name}: {type(e).__name__}: {e}") from e
            self.notes.append(f"{name}: {type(e).__name__}: {str(e).splitlines()[0][:200]}")
            self._seen.setdefault("failed_steps", []).append(name)

    def _picked(self, key: str, value: str, shown: str) -> None:
        self._seen.setdefault("picked", {}).setdefault(key, []).append(value)
        self._seen.setdefault("shown", {}).setdefault(key, []).append(shown)

    # ---------------------------------------------------------------- the account

    async def check_account(self, page: Page) -> None:
        await page.goto(self.create_url)
        await page.wait_for_load_state("domcontentloaded")
        await self._account(page)
        self._started = time.time()
        try:
            self._shop_before = set(await self._shop_listings(page.context))
        except Exception:  # noqa: BLE001 — the shop is only for the closet check after an interrupted publish
            self._shop_before = set()

    async def _account(self, page: Page) -> None:
        """Raise AccountBlocked for a login page, a CAPTCHA or a verification wall."""
        raise NotImplementedError

    async def _shop_listings(self, ctx: BrowserContext) -> dict[str, str]:
        """{listing address: its text (title)} on the shop page; {} when there is no shop to look at."""
        return {}

    # ---------------------------------------------------------------- publish

    async def submit(self, page: Page, mode: Mode) -> str | None:
        self.clicked = False
        if mode != "publish":
            raise PosterError(f"{self.name}: saving a draft isn't recorded; only a dry run or a publish")
        needs = self.publish_needs if self.confirm is not None else self.autopublish_needs
        if missing := sorted(needs & self.unverified):
            raise PosterError(f"the publish step is not recorded yet ({', '.join(missing)} UNVERIFIED in "
                              f"post/{self.name}.py): dry runs only until it is")
        if self._seen.get("failed_steps"):
            raise PosterError(f"the form didn't fill completely: {'; '.join(self.notes)}")
        button = self._publish_button(page)
        if (n := await button.count()) != 1:
            raise PosterError(f"{self.site}'s publish button differs from the recording ({n} found)")
        if self.confirm is not None and not await self.confirm(self.fields, self.site):
            raise Cancelled(f"not published on {self.site}: the confirmation wasn't typed")
        before, navigations = page.url, []
        page.on("framenavigated", lambda frame: navigations.append(frame.url) if frame == page.main_frame else None)
        self.clicked = True
        click_error = None
        try:
            await button.first.click()                   # exactly once, whatever happens next
        except Exception as e:  # noqa: BLE001 — it may still have gone through: watch, record, never click again
            click_error = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
        return await self._after_publish(page, before, navigations, click_error)

    def _publish_button(self, page: Page) -> Locator:
        raise NotImplementedError

    async def _after_publish(self, page: Page, before: str, navigations: list[str], click_error: str | None) -> str:
        """Watch the page until it is a listing; keep it as evidence (<shot>-after-publish.*); else look in the shop for
        the one new listing with this title. No address: it may be live — 'unconfirmed', never published again."""
        waited = 0
        while waited < AFTER_PUBLISH_MS and not self.listing_address(page.url) and not page.is_closed():
            await self._after_click(page)
            await asyncio.sleep(0.5)
            waited += 500
        record = {"url_before": before, "url_after": page.url, "navigations": navigations,
                  "click_error": click_error, "waited_ms": waited}
        await keep_evidence(page, self._evidence("after-publish"), record)
        if url := self.listing_address(page.url):
            return url
        url, seen = await self.find_live(page.context, None, since=self._started)
        self._evidence("shop").with_suffix(".json").write_text(json.dumps(seen, indent=1, default=str),
                                                               encoding="utf-8")
        if url:
            return url
        raise PosterError(f"after publishing no listing address: the page went to {page.url}. It may be live: check "
                          f"the {self.site} shop")

    async def _after_click(self, page: Page) -> None:
        """While waiting for the listing page: close a promotion offer (never accept one)."""
        return None

    def _evidence(self, name: str) -> Path:
        shot = self.shot or Path(f"item-{self.name}.png")
        return shot.with_name(f"{shot.stem}-{name}.png")

    # ---------------------------------------------------------------- after

    async def verify_live(self, page: Page, url: str, r: Render) -> None:
        """The listing page against the fields: the title (Depop: the description's first line), the price, the size
        and the photo count. A difference is a note (the ops chat, with the screenshot); the listing stays up."""
        await page.goto(url)
        await page.wait_for_load_state("domcontentloaded")
        body = _norm(await page.inner_text("body"))
        problems = []
        if _norm(r.title)[:40] not in body:
            problems.append("the title isn't on the page")
        if not re.search(rf"\$\s?{r.price}(?:\.00)?(?!\d)", body):
            problems.append(f"the price ${r.price} isn't on the page")
        size = getattr(self.fields, "size", None)
        if size and _norm(size) not in body:
            problems.append(f"the size {size!r} isn't on the page")
        try:
            photos = await self._live_photo_count(page)
        except Exception:  # noqa: BLE001
            photos = None
        if photos is not None and self.fields is not None and photos != len(self.fields.photos):
            problems.append(f"{photos} photos on the page, {len(self.fields.photos)} uploaded")
        await keep_evidence(page, self._evidence("live"), {"url": url, "problems": problems})
        if problems:
            self.notes.append("live check: " + "; ".join(problems))

    async def _live_photo_count(self, page: Page) -> int | None:
        return None

    async def find_live(self, ctx: BrowserContext, r: Render | None, since: float,
                        created: str | None = None) -> tuple[str | None, dict]:
        """This listing in the shop after an interrupted publish: the one listing that wasn't there before the post
        whose text carries the title's first words. Several: never a guess. No shop configured: it can't look."""
        title = r.title if r is not None else getattr(self.fields, "title", None) or \
            (self.fields.description.splitlines()[0] if self.fields is not None else "")
        want = re.findall(r"[a-z0-9]+", title.lower())[:4]
        try:
            listings = await self._shop_listings(ctx)
        except Exception as e:  # noqa: BLE001
            return None, {"error": f"{type(e).__name__}: {e}"}
        if not listings:
            return None, {"error": f"no {self.name} shop page to look at (set marketplaces.{self.name}.shop)"}
        new = {u: t for u, t in listings.items() if u not in self._shop_before}
        ours = sorted(u for u, t in new.items() if all(w in re.findall(r"[a-z0-9]+", f"{t} {u}".lower())
                                                       for w in want))
        seen = {"listings": len(listings), "new": list(new)[:10], "with_this_title": ours[:10]}
        return (ours[0], seen) if len(ours) == 1 else (None, seen)


def same_host(url: str, hosts: tuple[str, ...]) -> bool:
    u = urlparse(url.strip())
    return u.scheme in ("http", "https") and u.netloc.lower() in hosts
