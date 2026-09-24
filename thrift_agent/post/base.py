"""Poster framework: fill → read back → diff → (dry-run | publish | draft) → verify → record.

The model never presses publish. Deterministic code fills the form, reads every field back,
and only publishes when what's on screen matches the approved Render exactly.
"""
from __future__ import annotations

import random
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from playwright.async_api import BrowserContext, Locator, Page, async_playwright

from thrift_agent.schema import Render

Mode = Literal["publish", "draft"]


class PosterError(Exception):
    """Anything that should stop this item and page the seller."""


class AccountBlocked(PosterError):
    """Logged out, restricted, or a CAPTCHA — stop the whole poster, not just this item."""


class Mismatch(PosterError):
    """The form on screen differs from the approved Render. Carries the diff so it is recorded."""

    def __init__(self, diff: dict):
        super().__init__(f"form doesn't match plan: {diff}")
        self.diff = diff


@dataclass
class Outcome:
    status: Literal["posted", "drafted", "dryrun", "failed"]
    url: str | None = None
    screenshot: str | None = None
    diff: dict = field(default_factory=dict)
    error: str | None = None


async def open_browser(profile_dir: Path, timezone_id: str) -> tuple[object, BrowserContext]:
    """Real Chrome, headed, persistent profile — the same browser you log into by hand."""
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        str(profile_dir), channel="chrome", headless=False,
        viewport={"width": 1440, "height": 900}, locale="en-US", timezone_id=timezone_id,
    )
    return pw, ctx


async def human_type(loc: Locator, value: str) -> None:
    await loc.scroll_into_view_if_needed()
    await loc.click()
    await loc.press_sequentially(value, delay=random.randint(35, 95))


async def settle(page: Page, lo: float = 0.4, hi: float = 1.2) -> None:
    await page.wait_for_timeout(int(random.uniform(lo, hi) * 1000))


def _norm(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


def compare(seen: dict, expected: dict) -> dict:
    """{field: (expected, seen)} for every field that differs."""
    diff = {}
    for k, want in expected.items():
        got = seen.get(k)
        if k in ("price", "original_price"):
            ok = (want is None and not got) or (want is not None and str(got).replace("$", "").strip() == str(want))
        elif isinstance(want, list):
            ok = sorted(map(_norm, want)) == sorted(map(_norm, got or []))
        else:
            ok = _norm(want) == _norm(got)
        if not ok:
            diff[k] = (want, got)
    return diff


class Poster(ABC):
    name: str
    create_url: str

    @abstractmethod
    async def check_account(self, page: Page) -> None: ...

    @abstractmethod
    async def fill(self, page: Page, r: Render) -> None: ...

    @abstractmethod
    async def read_back(self, page: Page) -> dict: ...

    @abstractmethod
    def expected(self, r: Render) -> dict: ...

    @abstractmethod
    async def submit(self, page: Page, mode: Mode) -> str | None: ...

    async def verify_live(self, page: Page, url: str, r: Render) -> None:
        await page.goto(url)
        await page.wait_for_load_state("domcontentloaded")
        body = _norm(await page.inner_text("body"))
        if _norm(r.title)[:40] not in body:
            raise PosterError(f"live page at {url} doesn't show the title")

    async def post(self, ctx: BrowserContext, r: Render, mode: Mode, dry_run: bool, shots: Path) -> Outcome:
        shots.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shot = shots / f"{r.sku}-{self.name}-{stamp}.png"
        page = await ctx.new_page()
        try:
            await self.check_account(page)
            await page.goto(self.create_url)
            await page.wait_for_load_state("domcontentloaded")
            await self.fill(page, r)
            seen = await self.read_back(page)
            diff = compare(seen, self.expected(r))
            await page.screenshot(path=str(shot), full_page=True)
            if diff:
                raise Mismatch(diff)
            if dry_run:
                return Outcome("dryrun", screenshot=str(shot))
            url = await self.submit(page, mode)
            if mode != "publish":
                return Outcome("drafted", url=url, screenshot=str(shot))
            if not url:
                raise PosterError("published but no listing URL captured")
            try:
                await self.verify_live(page, url, r)
            except Exception as e:  # noqa: BLE001 — the listing IS live: keep its URL, never re-post it
                return Outcome("failed", url=url, screenshot=str(shot),
                               error=f"published but the live check failed ({type(e).__name__}: {e}) — check {url}")
            return Outcome("posted", url=url, screenshot=str(shot))
        except AccountBlocked:
            await page.screenshot(path=str(shot), full_page=True)
            raise
        except Exception as e:  # noqa: BLE001 — record everything, never retry blindly
            try:
                await page.screenshot(path=str(shot), full_page=True)
            except Exception:
                pass
            return Outcome("failed", screenshot=str(shot), error=f"{type(e).__name__}: {e}",
                           diff=getattr(e, "diff", {}))
        finally:
            await page.close()
