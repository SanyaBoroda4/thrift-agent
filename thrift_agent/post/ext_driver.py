"""The extension driver (WO32): Vinted and Depop posted through the Thrift Chrome extension (ext/) in the seller's own,
normally opened Chrome, over the bridge (thrift_agent/bridge.py). Vinted (DataDome) and Depop turn away any browser
driven over the DevTools protocol; a content script in a person's Chrome fills the form the way the person would.

It has the Playwright posters' interface — post(), find_live(), verify_live(), listing_address() — so the loop, the
records and the Telegram lines can't tell the drivers apart; and the WO32 jobs by name: check_login(), dry_run(),
publish(), delist().

- post(): one job (the catalog values, the copy, the approved price, the photos as bridge addresses) → the extension
  fills the form and reads it back → that read-back is diffed with WO30's plan (the same expectations as the
  Playwright posters) → a dry run ends there: the screenshot and the page's HTML kept in failed/shots, the tab closed,
  nothing saved. A publish then needs the steps it relies on verified in ext/selectors.json (PUBLISH_NEEDS for the
  supervised one, AUTOPUBLISH_NEEDS for the loop), exactly one Post / Upload button on the form, the owner's POST at
  the terminal when supervised — then ONE go-ahead: the extension clicks once, the tab lands on the listing page, the
  live page is read and compared with the fields (a difference is a note; the listing stays up).
- A login / block / CAPTCHA / verification page stops the site for the window (AccountBlocked with what it showed).
- A job lasts at most 6 minutes. After the go-ahead, no listing page (a timeout, an unknown page) → the seller's shop is
  looked at (a "find" job): the one listing with this title is recorded, else "unconfirmed" — never published again."""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path

from thrift_agent import bridge as bridge_mod
from thrift_agent.post.base import (AccountBlocked, Mode, Outcome, Poster, PosterError, _joined, _norm, _shows_price,
                                    compare)
from thrift_agent.schema import Render

SELECTORS = bridge_mod.EXT_DIR / "selectors.json"
PUBLISH_NEEDS = frozenset({"submit"})                       # the supervised `--publish-first`
AUTOPUBLISH_NEEDS = frozenset({"submit", "after_publish"})  # the unattended loop: the landing page recorded too
DELIST_NEEDS = {"vinted": frozenset({"hide"}), "depop": frozenset({"mark_sold"})}
STOP_PAGES = ("login", "block", "captcha", "verify")
WHAT = {"login": "not logged in in the Thrift Chrome", "block": "the site turned the Thrift Chrome away (block page)",
        "captcha": "a CAPTCHA is shown in the Thrift Chrome", "verify": "the site asks for a check (verification)"}
AFTER_CLICK_MIN = 120.0     # after the go-ahead the job gets at least this long to land on the listing page
CONNECT_WAIT = 90.0         # the shop check waits this long for the extension to (re)connect


class OneOf:
    """A read-back value that must be one of the planned ones (Vinted's package size: the first of ours its category
    offers)."""

    def __init__(self, values):
        self.values = [str(v).upper() for v in values]

    def matches(self, seen) -> bool:
        return bool(seen) and str(seen).upper() in self.values

    def __repr__(self) -> str:
        return f"one of {self.values}"


def selectors(path: Path = SELECTORS) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def unverified(site: str, path: Path = SELECTORS) -> set[str]:
    """The steps of a site not yet recorded working (ext/selectors.json "verified": false)."""
    steps = (selectors(path).get(site) or {}).get("steps") or {}
    return {name for name, step in steps.items() if not step.get("verified")}


class ExtensionPoster(Poster):
    driver = "extension"

    def __init__(self, mp: str, shop: str = "", aliases=None, bridge: bridge_mod.Bridge | None = None):
        from thrift_agent.post.depop import DepopPoster
        from thrift_agent.post.vinted import VintedPoster
        self.name = mp
        self.shape = DepopPoster(shop, aliases) if mp == "depop" else VintedPoster(shop)   # its plan and addresses
        self.site = self.shape.site
        self.create_url = self.shape.create_url
        self.shop = shop.strip()
        self.aliases = aliases
        self.bridge = bridge
        self.fields = None
        self.confirm = None             # the supervised publish: async (fields, site) -> bool
        self.strict = False
        self.notes, self.guesses = [], []
        self.created_id = None
        self.selectors_path = SELECTORS

    # ------------------------------------------------------------ the Playwright posters' page steps: not here

    async def check_account(self, page) -> None:
        raise PosterError("the extension driver has no Playwright page")

    async def fill(self, page, r: Render) -> None:
        raise PosterError("the extension driver has no Playwright page")

    async def read_back(self, page) -> dict:
        raise PosterError("the extension driver has no Playwright page")

    async def submit(self, page, mode: Mode) -> str | None:
        raise PosterError("the extension driver has no Playwright page")

    def expected(self, r: Render) -> dict:
        """WO30's plan, and two things the live forms showed matter (2026-10-06): every photo has to display (Depop's
        tiles stayed blank once), and Vinted's package size — it picks a "Recommended" one by itself — is one of ours."""
        self.shape.fields = self.fields
        want = self.shape.expected(r)
        want["photos_loaded"] = str(len(self.fields.photos))
        if self.name == "vinted" and getattr(self.fields, "package_sizes", None):
            want["package"] = OneOf(self.fields.package_sizes)
        return want

    def listing_address(self, url: str) -> str | None:
        return self.shape.listing_address(url)

    def available(self) -> bool:
        """The extension can take a job now (its socket is open, or it polled within 90 s)."""
        return self.bridge is not None and self.bridge.connected()

    # ------------------------------------------------------------ the job

    def _bridge(self) -> bridge_mod.Bridge:
        if self.bridge is None:
            raise PosterError(f"{self.site}: the extension bridge isn't running")
        return self.bridge

    def _title(self) -> str:
        f = self.fields
        return getattr(f, "title", None) or f.description.splitlines()[0]

    def payload(self, r: Render) -> dict:
        """The job (WO32 §2): the catalog values and ids, the copy, the price, the photos as bridge addresses."""
        f = self.fields
        if self.name == "vinted":
            fields = {"category_id": f.category_id, "category_path": f.category_path, "brand": f.brand,
                      "size": f.size, "condition": f.condition, "colors": list(f.colors), "materials": list(f.materials),
                      "skirt_length": f.skirt_length, "package_sizes": list(f.package_sizes)}
            copy = {"title": f.title, "description": f.description}
        else:
            spelled = self.aliases.spell(f.brand) if (self.aliases is not None and f.brand) else None
            fields = {"category": f.category, "brand": f.brand, "brand_typed": spelled or f.brand, "size": f.size,
                      "condition": f.condition, "colors": list(f.colors), "source": list(f.source), "age": f.age,
                      "style": list(f.style), "attributes": dict(f.attributes), "shipping": f.shipping,
                      "package_size": f.package_size}
            copy = {"title": f.description.splitlines()[0], "description": f.description}
        return {"fields": fields, "copy": copy, "price": f.price,
                "photos": self._bridge().photo_urls(r.sku, list(f.photos)), "listing_url": None}

    async def _await(self, job: bridge_mod.Job, kinds: tuple[str, ...], deadline: float) -> dict:
        """The next event of these kinds before the deadline — and a job no extension took within PICKUP_TIMEOUT is
        given up on (nothing was opened)."""
        while True:
            now = time.monotonic()
            limit = deadline - now
            if job.handed is None:
                limit = min(limit, job.created + bridge_mod.PICKUP_TIMEOUT - now)
            if limit <= 0:
                raise TimeoutError
            try:
                return await job.wait(kinds, min(limit, 5.0))
            except (TimeoutError, asyncio.TimeoutError):
                continue

    def _kept(self, job: bridge_mod.Job, *labels: str) -> str | None:
        """The screenshot of one of these labels (the first that was kept), else the last one, else None."""
        shots = [x for x in job.shots if x.get("png")]
        for label in labels:
            for x in shots:
                if x["label"] == label:
                    return x["png"]
        return shots[-1]["png"] if shots else None

    def _record(self, job: bridge_mod.Job, record: dict) -> None:
        if self.shot is None:
            return
        record = {**record, "steps": job.steps, "shots": job.shots, "notes": self.notes, "guesses": self.guesses}
        try:
            self.shot.with_suffix(".json").write_text(json.dumps(record, indent=1, default=repr), encoding="utf-8")
        except OSError:
            pass

    def _stop(self, ev: dict) -> AccountBlocked:
        page = ev.get("page")
        return AccountBlocked(f"{self.site}: {WHAT.get(page, page)} — {ev.get('message') or ''}".strip(" —"), page=page)

    # ------------------------------------------------------------ post

    async def post(self, ctx, r: Render, mode: Mode, dry_run: bool, shots: Path, stage: str = "form") -> Outcome:
        """The Playwright posters' contract: an Outcome for everything once the job exists; AccountBlocked (stop the
        site) and a missing bridge propagate. Nothing after the go-ahead raises: the listing may be live."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.shot = shots / f"{r.sku}-{self.name}-{stamp}.png"
        self.clicked, self.created_id = None, None
        self.notes, self.guesses = [], []
        if self.fields is None:
            return Outcome("failed", error=f"{self.name}: no mapped fields for this item")
        if mode != "publish":
            return Outcome("failed", error=f"{self.name}: saving a draft isn't a job; only a dry run or a publish")
        bridge = self._bridge()
        shots.mkdir(parents=True, exist_ok=True)
        job = bridge.submit(self.name, "dry_run" if dry_run else "publish", self.payload(r), shot=self.shot)
        try:
            return await self._post(bridge, job, r, dry_run)
        finally:
            if not job.done:
                bridge.drop(job)

    async def _post(self, bridge: bridge_mod.Bridge, job: bridge_mod.Job, r: Render, dry_run: bool) -> Outcome:
        deadline = job.created + bridge_mod.JOB_TIMEOUT
        started = time.time()
        try:
            ev = await self._await(job, ("ready", "result", "error"), deadline)
        except TimeoutError:
            if job.handed is None:
                raise PosterError(f"{self.site}: the Thrift Chrome extension didn't take the job in "
                                  f"{bridge_mod.PICKUP_TIMEOUT / 60:.1f} min — is the Thrift Chrome open?") from None
            self._record(job, {"item": r.sku, "error": "timeout"})
            return Outcome("failed", screenshot=self._kept(job, "form"), error=f"{self.site}: the form wasn't done in "
                           f"{bridge_mod.JOB_TIMEOUT / 60:.0f} min (nothing was submitted)", note=_joined(self.notes))
        if ev.get("event") == "error":
            self._record(job, {"item": r.sku, "error": ev})
            if ev.get("page") in STOP_PAGES:
                raise self._stop(ev)
            return Outcome("failed", screenshot=self._kept(job, "form"),
                           error=f"{self.site} {ev.get('stage') or 'form'}: {ev.get('message')}", note=_joined(self.notes))
        seen = ev.get("seen") or {}
        self.guesses += [g for g in ev.get("guesses") or [] if g]
        self.notes += [*(ev.get("notes") or []), *(f"step {x}" for x in ev.get("failed") or [])]
        want = self.expected(r)
        diff = compare(seen, want)
        self._record(job, {"item": r.sku, "seen": seen, "expected": want, "diff": diff})
        screenshot = self._kept(job, "form")
        if diff:
            if not dry_run:
                bridge.command(job, "cancel")
            return Outcome("failed", screenshot=screenshot, diff=diff, error=f"Mismatch: form doesn't match plan: {diff}",
                           note=_joined(self.notes), guesses=list(self.guesses))
        if dry_run:
            return Outcome("dryrun", screenshot=screenshot, note=_joined(self.notes), guesses=list(self.guesses))
        # ---- publish: the gate, the confirmation, then exactly one go-ahead
        needs = PUBLISH_NEEDS if self.confirm is not None else AUTOPUBLISH_NEEDS
        refusal = None
        if missing := sorted(needs & unverified(self.name, self.selectors_path)):
            refusal = (f"the publish step is not recorded yet ({', '.join(missing)} UNVERIFIED in ext/selectors.json): "
                       f"dry runs only until it is")
        elif (n := seen.get("submit_buttons")) != 1:
            refusal = f"{self.site}'s publish button differs from the recording ({n} found)"
        if refusal is None and self.confirm is not None and not await self.confirm(self.fields, self.site):
            bridge.command(job, "cancel")
            return Outcome("cancelled", screenshot=screenshot, note=_joined(self.notes + [
                f"not published on {self.site}: the confirmation wasn't typed"]))
        if refusal is not None:
            bridge.command(job, "cancel")
            return Outcome("failed", screenshot=screenshot, error=f"PosterError: {refusal}", note=_joined(self.notes),
                           guesses=list(self.guesses))
        self.clicked = True
        bridge.command(job, "submit")
        try:
            ev = await job.wait(("result", "error"), max(deadline - time.monotonic(), AFTER_CLICK_MIN))
        except (TimeoutError, asyncio.TimeoutError):
            ev = {"event": "error", "stage": "after_publish", "page": "unknown",
                  "message": f"no answer in {bridge_mod.JOB_TIMEOUT / 60:.0f} min after the click"}
        self._record(job, {"item": r.sku, "seen": seen, "expected": want, "after": ev})
        after = self._kept(job, "after-publish", "form")
        if ev.get("event") == "result" and (url := self.listing_address(ev.get("url") or "")):
            self._live_check(ev.get("live"), r)
            return Outcome("posted", url=url, screenshot=after, clicked=True, note=_joined(self.notes),
                           guesses=list(self.guesses))
        why = f"{ev.get('page') or 'unknown'} page: {ev.get('message') or ev.get('url') or ''}"
        try:
            url, looked = await self.find_live(None, r, since=started)
        except Exception as e:  # noqa: BLE001 — it stays unconfirmed: the owner looks
            url, looked = None, {"error": f"{type(e).__name__}: {e}"}
        self.notes.append(f"after the click: {why}; shop check: {json.dumps(looked, default=str)[:300]}")
        if url:
            return Outcome("posted", url=url, screenshot=after, clicked=True, guesses=list(self.guesses),
                           note=_joined(self.notes + ["found in the shop after the click"]))
        return Outcome("failed", screenshot=after, clicked=True, note=_joined(self.notes), guesses=list(self.guesses),
                       error=f"after the click no listing page ({why}). It may be live: check the {self.site} shop")

    def _live_check(self, live: dict | None, r: Render) -> None:
        """The listing page against the fields: the title, the price, the size, the photo count — a difference is a
        note for the ops chat; the listing stays up."""
        if not live:
            self.notes.append("live check: the listing page wasn't read")
            return
        body = _norm(live.get("body"))
        problems = []
        if _norm(self._title())[:40] not in body:
            problems.append("the title isn't on the page")
        if not _shows_price(live.get("body") or "", int(self.fields.price)):
            problems.append(f"the price ${self.fields.price} isn't on the page")
        if self.fields.size and _norm(self.fields.size) not in body:
            problems.append(f"the size {self.fields.size!r} isn't on the page")
        photos = live.get("photos")
        if photos and photos != len(self.fields.photos):
            problems.append(f"{photos} photos on the page, {len(self.fields.photos)} uploaded")
        if problems:
            self.notes.append("live check: " + "; ".join(problems))

    # ------------------------------------------------------------ the other jobs

    async def _job(self, mode: str, payload: dict, timeout: float, shot: Path | None = None) -> dict:
        bridge = self._bridge()
        job = bridge.submit(self.name, mode, {"fields": {}, "copy": {}, "price": None, "photos": [],
                                               "listing_url": None, **payload}, shot=shot)
        try:
            return await self._await(job, ("result", "error"), time.monotonic() + timeout)
        except TimeoutError:
            raise PosterError(f"{self.site} {mode}: no answer from the extension in {timeout / 60:.1f} min") from None
        finally:
            if not job.done:
                bridge.drop(job)

    async def wait_connected(self, timeout: float = CONNECT_WAIT) -> bool:
        end = time.monotonic() + timeout
        while self.bridge is not None and not self.bridge.connected() and time.monotonic() < end:
            await asyncio.sleep(1)
        return self.available()

    async def check_login(self) -> str:
        """The sell page in the Thrift Chrome: "form" when logged in; AccountBlocked for a login / block / CAPTCHA /
        verification page."""
        ev = await self._job("check_login", {}, 120)
        if ev.get("event") == "error":
            if ev.get("page") in STOP_PAGES:
                raise self._stop(ev)
            raise PosterError(f"{self.site} check_login: {ev.get('message')}")
        return ev.get("page") or "form"

    async def dry_run(self, r: Render, shots: Path) -> Outcome:
        return await self.post(None, r, "publish", True, shots)

    async def publish(self, r: Render, shots: Path) -> str | None:
        out = await self.post(None, r, "publish", False, shots)
        return out.url

    async def verify_live(self, page, url: str, r: Render) -> None:
        """The listing page (opened in the Thrift Chrome) shows the title and the price, else PosterError."""
        address = self.listing_address(url)
        if address is None:
            raise PosterError(f"not a {self.name} listing address: {url!r}")
        ev = await self._job("verify", {"listing_url": address}, 180, shot=self.shot)
        if ev.get("event") == "error":
            if ev.get("page") in STOP_PAGES:
                raise self._stop(ev)
            raise PosterError(f"{address}: {ev.get('message')}")
        live = ev.get("live") or {}
        body = _norm(live.get("body"))
        if _norm(r.title)[:40] not in body and _norm(self._title() if self.fields else r.title)[:40] not in body:
            raise PosterError(f"the listing page at {address} doesn't show the title")
        if not _shows_price(live.get("body") or "", r.price):
            raise PosterError(f"the listing page at {address} doesn't show the price ${r.price}")

    async def find_live(self, ctx, r: Render | None, since: float, created: str | None = None
                        ) -> tuple[str | None, dict]:
        """This listing in the seller's shop after an interrupted publish: the one listing whose text or address
        carries the title's first words. Several, or none: never a guess. No shop configured: it can't look."""
        if not self.shop:
            return None, {"error": f"no {self.name} shop page to look at (set marketplaces.{self.name}.shop)"}
        if not await self.wait_connected():
            raise PosterError(f"{self.site}: the Thrift Chrome extension isn't connected — the shop check waits")
        ev = await self._job("find", {"shop": self.shop}, 180)
        if ev.get("event") == "error":
            return None, {"error": ev.get("message"), "page": ev.get("page")}
        listings: dict[str, str] = {}
        for x in ev.get("listings") or []:
            if address := self.listing_address(str(x.get("url") or "")):
                listings[address] = f"{listings.get(address, '')} {x.get('text') or ''}".strip()
        title = r.title if r is not None else self._title()
        want = re.findall(r"[a-z0-9]+", title.lower())[:4]
        ours = sorted(u for u, t in listings.items()
                      if all(w in re.findall(r"[a-z0-9]+", f"{t} {u}".lower()) for w in want))
        seen = {"listings": len(listings), "with_this_title": ours[:10]}
        return (ours[0], seen) if len(ours) == 1 else (None, seen)

    async def delist(self, url: str) -> bool:
        """WO31's take-down, never a delete: Vinted's Hide, Depop's Mark as sold — only once that step is recorded."""
        if missing := sorted(DELIST_NEEDS[self.name] & unverified(self.name, self.selectors_path)):
            raise PosterError(f"{self.site}: delisting isn't recorded yet ({', '.join(missing)} UNVERIFIED in "
                              "ext/selectors.json)")
        address = self.listing_address(url)
        if address is None:
            raise PosterError(f"not a {self.name} listing address: {url!r}")
        ev = await self._job("delist", {"listing_url": address}, 180, shot=self.shot)
        if ev.get("event") == "error":
            if ev.get("page") in STOP_PAGES:
                raise self._stop(ev)
            raise PosterError(f"{self.site} delist {address}: {ev.get('message')}")
        return bool(ev.get("delisted"))
