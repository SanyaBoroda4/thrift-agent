"""Poster framework: fill → read back → diff → (dry-run | publish | draft) → verify → record.

The model never presses publish. Deterministic code fills the form, reads every field back,
and only publishes when what's on screen matches the approved Render exactly.
"""
from __future__ import annotations

import json
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
Stage = Literal["form", "review"]
STAGES = ("form", "review")     # dry-run stages: fill + read back + discard | also press Next, record, back out


class PosterError(Exception):
    """Anything that should stop this item and page the seller."""


class AccountBlocked(PosterError):
    """Logged out, restricted, or a CAPTCHA — stop the whole poster, not just this item. `page`: what the site showed
    (login | captcha | block | verify) when the poster knows it: the owner's one line says what to do (WO32)."""

    def __init__(self, message: str = "", page: str | None = None):
        super().__init__(message)
        self.page = page


class Skipped(PosterError):
    """A REQUIRED field the form can't take, even as a best guess (WO27: the poster never stops to ask — a brand, a
    subcategory, a size or a colour it can't match exactly is guessed and reported instead). Raised by an adapter's
    fill() before anything is submitted: nothing is saved, the item is skipped and reported, the others continue."""


class Cancelled(PosterError):
    """The owner declined at the last prompt of a supervised publish (didn't type LIST): nothing was submitted."""


class Mismatch(PosterError):
    """The form on screen differs from the approved Render. Carries the diff so it is recorded."""

    def __init__(self, diff: dict):
        super().__init__(f"form doesn't match plan: {diff}")
        self.diff = diff


@dataclass
class Outcome:
    status: Literal["posted", "drafted", "dryrun", "failed", "cancelled", "skipped"]
    url: str | None = None
    screenshot: str | None = None
    diff: dict = field(default_factory=dict)
    error: str | None = None
    note: str | None = None      # remarks for the owner: a tag left out, the review page recorded, Discard unconfirmed
    draft_left: str | None = None  # "a draft was left behind (Drafts 0 → 1)": a dry-run that should have left none
    clicked: bool = False        # the final publish/draft control was pressed: the listing may be live
    guesses: list[str] = field(default_factory=list)   # what the poster chose that the listing didn't say exactly
    created_id: str | None = None   # the listing id the site named after the final click (WO28: the closet check)


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


def _money(v) -> float | None:
    """Price fields as numbers, so a form echoing '85.00' or '$85' matches a plan of 85.
    None / blank → None (matches only a None plan); unparseable → NaN, which matches nothing."""
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(re.sub(r"[^\d.]", "", str(v)))
    except ValueError:
        return float("nan")


class Contains:
    """The expected value of a field that is read back as display text (a dropdown's breadcrumb, a size chip): every
    part must appear in it as a whole word or phrase, case and spacing ignored. "Men" is not in "Women", "8" is not in
    "8.5", "7.5" is not in "17.5"; blank text never matches."""

    def __init__(self, *parts: str):
        self.parts = tuple(p for p in parts if p)

    def matches(self, seen) -> bool:
        text = _norm(seen)
        return bool(text) and all(re.search(rf"(?<![\w.]){re.escape(_norm(p))}(?!\w|\.\d)", text) for p in self.parts)

    def __eq__(self, other) -> bool:
        return isinstance(other, Contains) and self.parts == other.parts

    def __hash__(self) -> int:
        return hash(self.parts)

    def __repr__(self) -> str:
        return "contains " + " + ".join(repr(p) for p in self.parts)


def compare(seen: dict, expected: dict) -> dict:
    """{field: (expected, seen)} for every field that differs."""
    diff = {}
    for k, want in expected.items():
        got = seen.get(k)
        if isinstance(want, Contains) or callable(getattr(want, "matches", None)):
            ok = want.matches(got)
        elif k == "original_price":
            # Poshmark's form holds "0" for an Original Price left empty (Mac dry-run #3): no price, "" and 0 are one.
            # A real one still has to match: 120 planned, 0 on the form is a mismatch.
            ok = (_money(got) or None) == (_money(want) or None)
        elif k == "price":
            ok = _money(got) == _money(want)
        elif isinstance(want, list):
            ok = sorted(map(_norm, want)) == sorted(map(_norm, got or []))
        else:
            ok = _norm(want) == _norm(got)
        if not ok:
            diff[k] = (want, got)
    return diff


def _shows_price(text: str, price: int) -> bool:
    """The price as a listing page shows it: "$50", "$ 50", "$50.00", "$1,200" — not "$500" or "$50.99" for 50."""
    amount = "|".join(re.escape(a) for a in {str(price), f"{price:,}"})
    return re.search(rf"\$\s?(?:{amount})(?:\.00)?(?!\d|[.,]\d)", text) is not None


async def keep_evidence(page: Page | None, shot: Path, record: dict | None = None) -> None:
    """The page as it is, next to each other in failed/shots: <shot>.png (full page), <shot>.html (the DOM, so a
    selector can be recorded from the file instead of another run) and, when given, <shot>.json (what was read back,
    what was expected, the diff). Best effort: evidence never replaces the outcome it documents."""
    if page is None:
        return
    try:
        await page.screenshot(path=str(shot), full_page=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        shot.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if record is not None:
        try:
            shot.with_suffix(".json").write_text(json.dumps(record, indent=1, default=repr), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def _joined(notes: list[str | None]) -> str | None:
    return "; ".join(n for n in notes if n) or None


class Poster(ABC):
    name: str
    create_url: str
    notes: list[str]            # remarks the adapter collects while filling one item (reset by post())
    guesses: list[str]          # its best guesses (WO27): "brand set to 'J. Crew' (from 'J.Crew')" (reset by post())
    counts_drafts = False       # the adapter reads the site's draft count on the create page (drafts())
    # Set by post() to None ("don't know"). An adapter that tracks it sets False when submit() starts and True right
    # before the click that can't be undone; until then the form can still be left through discard().
    clicked: bool | None = None
    shot: Path | None = None    # this post's evidence path (<sku>-<site>-<time>.png): adapters add files next to it

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

    async def review(self, page: Page, r: Render, shots: Path) -> str | None:
        """Dry-run stage "review": go past the form (Next), record the page there, come back. Never the final
        publish button. Returns a remark for the owner."""
        raise PosterError(f"{self.name}: the dry-run review stage is not implemented")

    async def discard(self, page: Page) -> str | None:
        """Leave the filled form without saving anything (no draft left behind). Returns a remark, or None when
        the site confirmed it."""
        return None

    async def drafts(self, page: Page) -> int | None:
        """How many drafts the site holds, read on the create page (counts_drafts adapters); None when unreadable."""
        return None

    async def _drafts_now(self, page: Page) -> int | None:
        try:
            return await self.drafts(page)
        except Exception:  # noqa: BLE001 — unreadable is a note, never a failure
            return None

    async def _left_behind(self, ctx: BrowserContext, before: int | None) -> tuple[str | None, str | None]:
        """(draft_left, note) after a dry-run: the create page reopened in a fresh tab, its draft count compared with
        the count before the run. A higher count means the leave step saved a draft instead of dropping it."""
        if before is None:
            return None, "could not read the Drafts count before the dry-run, so no left-behind check"
        page = None
        try:
            page = await ctx.new_page()
            await page.goto(self.create_url)
            await page.wait_for_load_state("domcontentloaded")
            after = await self._drafts_now(page)
        except Exception as e:  # noqa: BLE001
            return None, f"could not reopen the create page to count the drafts ({type(e).__name__})"
        finally:
            if page:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
        if after is None:
            return None, "could not read the Drafts count after the dry-run"
        if after > before:
            left = f"a draft was left behind (Drafts {before} \u2192 {after})"
            return left, left
        return None, None

    def _may_be_live(self, submitted: bool) -> bool:
        """Once submit() started the listing may be live — unless the adapter says its final click hasn't happened."""
        return submitted and self.clicked is not False

    async def _discard_quietly(self, page: Page | None) -> str | None:
        if page is None:
            return None
        try:
            return await self.discard(page)
        except Exception as e:  # noqa: BLE001 — leaving is a courtesy; it never replaces the item's outcome
            return f"could not discard the form ({type(e).__name__}: {e})"

    def listing_address(self, url: str) -> str | None:
        """The canonical address of a listing page on this site, or None. An adapter that knows its listing addresses
        overrides this; without it a listing found by hand can't be recorded (`thrift mark-posted` refuses)."""
        return None

    async def verify_live(self, page: Page, url: str, r: Render) -> None:
        """The live listing shows the title (its first 40 characters) and the price."""
        await page.goto(url)
        await page.wait_for_load_state("domcontentloaded")
        body = _norm(await page.inner_text("body"))
        if _norm(r.title)[:40] not in body:
            raise PosterError(f"live page at {url} doesn't show the title")
        if not _shows_price(body, r.price):
            raise PosterError(f"live page at {url} doesn't show the price ${r.price}")

    async def find_live(self, ctx: BrowserContext, r: Render, since: float,
                        created: str | None = None) -> tuple[str | None, dict]:
        """This listing on the site after an interrupted publish (WO28 §3): (its address, what was seen), or (None, …)
        when it can't be found — or, as here, when the adapter can't look (then it stays "unconfirmed")."""
        return None, {"unsupported": self.name}

    async def post(self, ctx: BrowserContext, r: Render, mode: Mode, dry_run: bool, shots: Path,
                   stage: Stage = "form") -> Outcome:
        """Returns an Outcome for everything that happens once the page exists; only AccountBlocked (stop the
        poster) propagates. A required field the form can't take is "skipped" (nothing saved); every best guess the
        adapter made rides on the Outcome (WO27: the poster never stops to ask).

        A dry-run fills, reads back, diffs and keeps the evidence; stage "review" also presses Next and records the
        page after it; then the form is left through the site's discard path, so no draft is left behind. The final
        publish button is never pressed in a dry-run. A form that fails or waits for the owner is discarded too —
        but never once submit() has started: from then on the listing may be live, and only the closet can tell.

        Nothing after `submit` may raise out of here: an exception in `finally` would REPLACE the returned
        Outcome, and a live listing without a recorded URL is exactly the double-post invariant 4 forbids.
        """
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shot = shots / f"{r.sku}-{self.name}-{stamp}.png"
        self.shot, self.clicked, self.created_id = shot, None, None
        page: Page | None = None
        opened = submitted = kept = False
        count_drafts = dry_run and self.counts_drafts      # a dry-run must leave no draft: counted before and after
        drafts_before: int | None = None
        self.notes, self.guesses = [], []
        try:
            shots.mkdir(parents=True, exist_ok=True)
            page = await ctx.new_page()
            await self.check_account(page)
            await page.goto(self.create_url)
            await page.wait_for_load_state("domcontentloaded")
            if count_drafts:
                drafts_before = await self._drafts_now(page)
            opened = True
            await self.fill(page, r)
            seen = await self.read_back(page)
            want = self.expected(r)
            diff = compare(seen, want)
            await keep_evidence(page, shot, {"item": r.sku, "seen": seen, "expected": want, "diff": diff,
                                             "notes": self.notes})
            kept = True
            if diff:
                raise Mismatch(diff)
            if dry_run:
                if stage == "review":
                    self.notes.append(await self.review(page, r, shots))
                opened = False                      # leaving now; a failure below must not discard twice
                self.notes.append(await self._discard_quietly(page))
                left, note = await self._left_behind(ctx, drafts_before) if count_drafts else (None, None)
                return Outcome("dryrun", screenshot=str(shot), note=_joined(self.notes + [note]), draft_left=left,
                               guesses=list(self.guesses))
            submitted = True
            url = await self.submit(page, mode)
            clicked = self._may_be_live(submitted)
            if mode != "publish":
                return Outcome("drafted", url=url, screenshot=str(shot), clicked=clicked, note=_joined(self.notes),
                               guesses=list(self.guesses))
            if not url:
                raise PosterError("published but no listing URL captured")
            try:
                await self.verify_live(page, url, r)
            except Exception as e:  # noqa: BLE001 — the listing IS live: keep its URL, never re-post it
                return Outcome("failed", url=url, screenshot=str(shot), clicked=clicked, note=_joined(self.notes),
                               error=f"published but the live check failed ({type(e).__name__}: {e}) — check {url}",
                               guesses=list(self.guesses))
            return Outcome("posted", url=url, screenshot=str(shot), clicked=clicked, note=_joined(self.notes),
                           guesses=list(self.guesses))
        except AccountBlocked:
            await keep_evidence(page, shot)     # and nothing else: stop, don't touch a blocked account
            raise
        except Cancelled as e:
            await keep_evidence(page, shot)
            left = await self._discard_quietly(page) if opened and not self._may_be_live(submitted) else None
            return Outcome("cancelled", screenshot=str(shot), note=_joined(self.notes + [str(e), left]))
        except Skipped as e:                    # before submit: nothing was saved, the form is left through Discard
            await keep_evidence(page, shot)
            left, draft_left = None, None
            if opened and not submitted:
                left = await self._discard_quietly(page)
                if count_drafts:
                    draft_left, _ = await self._left_behind(ctx, drafts_before)
            return Outcome("skipped", screenshot=str(shot), error=str(e), note=_joined(self.notes + [left]),
                           draft_left=draft_left, guesses=list(self.guesses))
        except Exception as e:  # noqa: BLE001 — record everything, never retry blindly
            if not kept:
                await keep_evidence(page, shot)
            left, draft_left, draft_note = None, None, None
            if opened and not self._may_be_live(submitted):
                left = await self._discard_quietly(page)
                if count_drafts:
                    draft_left, draft_note = await self._left_behind(ctx, drafts_before)
            return Outcome("failed", screenshot=str(shot), error=f"{type(e).__name__}: {e}",
                           diff=getattr(e, "diff", {}), note=_joined(self.notes + [left, draft_note]),
                           draft_left=draft_left, clicked=self._may_be_live(submitted),
                           guesses=list(self.guesses), created_id=self.created_id)
        finally:
            if page:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001 — a close error must never eat the Outcome
                    pass
