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
- A job's form is filled within 3 minutes (WO32b) or given up. After the go-ahead the new listing is confirmed by its
  address (WO35): Depop's success page names it (View listing → /products/<slug>/manage/, where the SKU must be our
  item id); Vinted's own JSON of the seller's newest items has it under its exact title. Not there yet → looked for
  again, every 30 s for 5 minutes (Depop: its shop, by the title's words after the username part of each address —
  Depop scrambles the username — then the SKU on /manage/); still not → "unconfirmed", settled later by the poster
  from the site's own data (runner.recheck_unconfirmed), never published again blind. A form the site refused (its
  error shown) is looked up once: absent → a plain failure.
- WO32b: the progress as it happens ("tab opened", "photos 6/6", "category ✓" …) goes to `progress` (the CLI prints
  it) and to `lines` (a dry run's ops message). The row is taken for posting by `on_go_ahead` — the moment before the
  one go-ahead is sent, never earlier: before it nothing can be published, so a Ctrl+C, a failure or a form that went
  away leaves the row as it was. A job given up on is called off in the extension (its tab closed) before post()
  returns or raises."""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from thrift_agent import brands, bridge as bridge_mod
from thrift_agent.post.base import (AccountBlocked, Mode, Outcome, Poster, PosterError, _joined, _norm, _shows_price,
                                    compare)
from thrift_agent.post.cross import Chosen
from thrift_agent.schema import Render

SELECTORS = bridge_mod.EXT_DIR / "selectors.json"
PUBLISH_NEEDS = frozenset({"submit"})                       # the supervised `--publish-first`
AUTOPUBLISH_NEEDS = frozenset({"submit", "after_publish"})  # the unattended loop: the landing page recorded too
# WO33: Vinted's Hide; Depop's DELETE (the owner's decision, 2026-10-08: its page offers no Mark as sold)
DELIST_NEEDS = {"vinted": frozenset({"hide"}), "depop": frozenset({"delete_button", "delete_dialog", "delete_confirm"})}
STOP_PAGES = ("login", "block", "captcha", "verify")
WHAT = {"login": "not logged in in the Thrift Chrome", "block": "the site turned the Thrift Chrome away (block page)",
        "captcha": "a CAPTCHA is shown in the Thrift Chrome", "verify": "the site asks for a check (verification)"}
AFTER_CLICK_MIN = 200.0     # after the go-ahead the job gets at least this long to land (the extension waits 3 min)
CONNECT_WAIT = 90.0         # the shop check waits this long for the extension to (re)connect
CONFIRM_FOR = 300.0         # WO35: a new listing is looked for by its address this long after the click …
CONFIRM_EVERY = 30.0        # … this often, before the publish counts as unconfirmed
SKU = re.compile(r"\bi_\d{6}_[0-9a-f]{6}\b")


def title_words(text: str) -> list[str]:
    """A title's words as an address spells them: lower case, apostrophes dropped ("Levi's" → levis), every other mark a
    break ("Cut-Off" → cut off, "&" gone)."""
    return re.findall(r"[a-z0-9]+", re.sub(r"['’`]", "", str(text or "").lower()))


def slug_words(address: str) -> list[str]:
    """The title's words in a Depop address /products/<username>-<words>-<4 hex>/: the username part (Depop scrambles
    one character of it every time — tetiana9orokina, tetian3sorokina…) and the id dropped (WO35)."""
    m = re.search(r"/products/([a-z0-9]+(?:-[a-z0-9]+)+)/?", address or "")
    if not m:
        return []
    words = m.group(1).split("-")[1:]
    if words and re.fullmatch(r"[0-9a-f]{4}", words[-1]):
        words = words[:-1]
    return words


def slug_matches(address: str, title: str) -> bool:
    """The address's words begin our title's (the last may be cut short): never the username."""
    sw, tw = slug_words(address), title_words(title)
    if len(sw) < 2 or len(sw) > len(tw):
        return False
    return sw[:-1] == tw[:len(sw) - 1] and tw[len(sw) - 1].startswith(sw[-1])


def same_title(a: str | None, b: str | None) -> bool:
    """The exact title (spaces aside): Vinted keeps ours as typed."""
    return bool(a) and " ".join(str(a).split()) == " ".join(str(b or "").split())


class OneOf:
    """A read-back value that must be one of the planned ones (Vinted's package size: the first of ours its category
    offers)."""

    def __init__(self, values):
        self.values = [str(v).upper() for v in values]

    def matches(self, seen) -> bool:
        return bool(seen) and str(seen).upper() in self.values

    def __repr__(self) -> str:
        return f"one of {self.values}"


class BrandOf:
    """WO36: a read-back brand that is one of ours — exactly one of our names (brands.candidates), or a name of ours
    followed by more words (the one the step took: "Alo Yoga" for "alo") — or the site's own fallback when none of ours
    is listed: Depop's "Other", Vinted's "List without brand" (live 2026-10-11: what its field shows then). Never a
    brand of someone else (live 2026-10-09: the silk pants read Vinted's own "H&M")."""

    def __init__(self, names: list[str], fallback: list[str]):
        self.names, self.fallback = list(names), list(fallback)

    def matches(self, seen) -> bool:
        v = str(seen or "")
        if brands.key(v) in {brands.key(f) for f in self.fallback}:
            return True
        return bool(brands.key(v)) and any(brands.key(n) == brands.key(v) or brands.extends(n, v) for n in self.names)

    def __repr__(self) -> str:
        return f"one of {self.names} or {self.fallback}"


# WO36 §4: the multi-selects a Depop listing may hold — ours only; every other one (Style, an occasion…) empty
DEPOP_MULTI = ("colour-input", "source-input", "age-input", "style-input")
DEPOP_SINGLE = ("group-input", "variants-input", "condition-input", "brand-input", "shippingMethods-input")


def selectors(path: Path = SELECTORS) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def unverified(site: str, path: Path = SELECTORS) -> set[str]:
    """The steps of a site not yet recorded working (ext/selectors.json "verified": false)."""
    steps = (selectors(path).get(site) or {}).get("steps") or {}
    return {name for name, step in steps.items() if not step.get("verified")}


def photo_key(url: str | None) -> str:
    """A site photo's own id, the same on its edit page, its listing page and in its JSON (WO37, recorded 2026-10-12):
    Depop media-photos.depop.com/b1/<user>/<id>/P2.jpg (P0 in the JSON-LD), Vinted images1.vinted.net/tc|t/<id>/f800/…
    — the longest part of the path with an underscore in it."""
    parts = [p for p in urlparse(str(url or "")).path.split("/") if "_" in p]
    return max(parts, key=len) if parts else str(url or "")


def cover_moved(before: list, after: list, photo: int) -> bool:
    """The order after the move is the old one with photo `photo` first and every other one in its order (WO37)."""
    return 0 < photo < len(before) and after == [before[photo], *before[:photo], *before[photo + 1:]]


def refusal_text(message) -> str:
    """What the site's form said after the click (WO39): the extension's "refused: …" without its prefix; "not a
    listing page: …" from an older extension becomes "it kept the sell form"."""
    m = str(message or "")
    if m.startswith("refused: "):
        return m[len("refused: "):]
    return f"it kept the sell form ({m})" if m else "it kept the sell form"


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
        self.on_go_ahead = None         # () -> bool: take the row for posting (False: no go-ahead, nothing clicked)
        self.progress = None            # (line) -> None: each progress line as it comes (the CLI prints them)
        self.lines: list[str] = []      # the job's progress lines (a dry run's ops message)
        self.learned_shop = None        # the shop name a listing page showed (Depop's shop link)
        self.fill_seconds = None        # how long the last form took to fill (the page's own clock)
        self.shot = None                # the evidence path of the job in hand
        self.strict = False
        self.notes, self.guesses = [], []
        self.created_id = None
        self.selectors_path = SELECTORS
        self.held: set[str] = set()     # addresses other items hold on this site: never taken for this one (WO35)
        self.confirmed_in = None        # seconds from the click to the confirmed address (WO35 §5)

    # ------------------------------------------------------------ the Playwright posters' page steps: not here

    async def check_account(self, page) -> None:
        raise PosterError("the extension driver has no Playwright page")

    async def fill(self, page, r: Render) -> None:
        raise PosterError("the extension driver has no Playwright page")

    async def read_back(self, page) -> dict:
        raise PosterError("the extension driver has no Playwright page")

    async def submit(self, page, mode: Mode) -> str | None:
        raise PosterError("the extension driver has no Playwright page")

    def expected(self, r: Render, seen: dict | None = None) -> dict:
        """WO30's plan, and two things the live forms showed matter (2026-10-06): every photo has to display (Depop's
        tiles stayed blank once), and Vinted's package size — it picks a "Recommended" one by itself — is one of ours.

        WO36 §4 (the Alo listing, 2026-10-12): the brand is one of ours or the site's fallback (BrandOf); Depop's Style
        is empty and every multi-select holds only ours (an occasion or a style Depop put in by itself fails the diff);
        an optional value the form didn't offer (`seen["left"]`, said in the notes) is expected absent — never
        replaced by another option."""
        f = self.fields
        self.shape.fields = f
        want = self.shape.expected(r)
        want["photos_loaded"] = str(len(f.photos))
        left = (seen or {}).get("left") or {}

        def ours(key: str, values) -> list[str]:
            return [v for v in values or [] if v not in (left.get(key) or [])]
        names = brands.candidates(f.brand, getattr(f, "brand_label", None), self.aliases) if f.brand else []
        if self.name == "vinted":
            if getattr(f, "package_sizes", None):
                want["package"] = OneOf(f.package_sizes)
            want["brand_shown"] = BrandOf(names, ["List without brand", ""])
            want["color"] = Chosen(ours("color", f.colors[:2]))
            want["material"] = Chosen(ours("material", f.materials[:3]))
        if self.name == "depop":
            if "sku" not in unverified("depop", self.selectors_path):
                want["sku"] = r.sku                    # WO33 A4: compared once a Mac dry run has recorded it filled
            want["brand-input"] = BrandOf(names, ["Other"])
            want["colour-input"] = Chosen(ours("colour-input", f.colors[:2]))
            want["source-input"] = Chosen(ours("source-input", f.source))
            want["age-input"] = Chosen(ours("age-input", [f.age] if f.age else []))
            want["style-input"] = Chosen([])           # never (the owner, 2026-10-12)
            for name, values in (f.attributes or {}).items():
                want[f"attributes.{name}-input"] = Chosen(ours(f"attributes.{name}-input", values))
            for key in (seen or {}):                   # any other multi-select on the form (an occasion…): empty
                if key.endswith("-input") and key not in want and key not in DEPOP_SINGLE:
                    want[key] = Chosen([])
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
        # WO36: the names to try, in order — the brand as shops list it, the label's text, the alias file's names, the
        # two parts of a name like "MNG (Mango)" — each picked only exactly, else the one option that is the whole
        # name followed by more words; else Depop's "Other" / Vinted's "List without brand"
        names = brands.candidates(f.brand, getattr(f, "brand_label", None), self.aliases)
        if self.name == "vinted":
            fields = {"category_id": f.category_id, "category_path": f.category_path, "brand": f.brand,
                      "brand_candidates": names,
                      "size": f.size, "condition": f.condition, "colors": list(f.colors), "materials": list(f.materials),
                      "skirt_length": f.skirt_length, "package_sizes": list(f.package_sizes)}
            copy = {"title": f.title, "description": f.description}
        else:
            fields = {"category": f.category, "brand": f.brand, "brand_typed": names[0] if names else f.brand,
                      "brand_candidates": names, "size": f.size,
                      "condition": f.condition, "colors": list(f.colors), "source": list(f.source), "age": f.age,
                      "style": [], "attributes": dict(f.attributes), "shipping": f.shipping,   # Style: never (WO36)
                      "package_size": f.package_size, "sku": r.sku}
            copy = {"title": f.description.splitlines()[0], "description": f.description}
        return {"fields": fields, "copy": copy, "price": f.price,
                "photos": self._bridge().photo_urls(r.sku, list(f.photos), self.name), "listing_url": None}

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

    def _say(self, line: str) -> None:
        if not line:
            return
        self.lines.append(line)
        if self.progress is not None:
            try:
                self.progress(line)
            except Exception:  # noqa: BLE001 — a progress line never breaks the job
                pass

    def _listen(self, ev: dict) -> None:
        """A job's step or progress event as one line: "tab opened", "photos 6/6", "category ✓", "size ✗ <why>"."""
        if ev.get("event") == "progress":
            return self._say(str(ev.get("what") or ""))
        name = str(ev.get("name") or "")
        if name in ("submit_seen", "submit") or not name:
            return None
        label = re.sub(r"-input$", "", name).removeprefix("attributes.")
        if name == "photos" and ev.get("ok") and ev.get("detail"):
            return self._say(f"photos {str(ev['detail']).split(' ')[0]}")
        return self._say(f"{label} ✓" if ev.get("ok") else f"{label} ✗ {ev.get('detail') or ''}".rstrip())

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
        self.notes, self.guesses, self.lines, self.fill_seconds = [], [], [], None
        if self.fields is None:
            return Outcome("failed", error=f"{self.name}: no mapped fields for this item")
        if mode != "publish":
            return Outcome("failed", error=f"{self.name}: saving a draft isn't a job; only a dry run or a publish")
        bridge = self._bridge()
        shots.mkdir(parents=True, exist_ok=True)
        job = bridge.submit(self.name, "dry_run" if dry_run else "publish", self.payload(r), shot=self.shot)
        job.listener = self._listen
        try:
            return await self._post(bridge, job, r, dry_run)
        finally:
            if not job.done:                      # given up on (a timeout, a Ctrl+C): called off, its tab closed
                bridge.drop(job)
                try:
                    await bridge.settle(job, 5.0)
                except BaseException:  # noqa: BLE001 — a second Ctrl+C: the extension ends it by itself
                    pass

    async def _post(self, bridge: bridge_mod.Bridge, job: bridge_mod.Job, r: Render, dry_run: bool) -> Outcome:
        deadline = job.created + bridge_mod.JOB_TIMEOUT
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
        if isinstance(seen.get("fill_ms"), (int, float)):
            self.fill_seconds = round(seen["fill_ms"] / 1000, 1)
            self._say(f"filled in {self.fill_seconds} s")
        want = self.expected(r, seen)
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
        if refusal is None and (job.final is not None or job.dropped is not None):
            gone = (job.final or {}).get("message") or "given up on"
            refusal = f"the form went away before the go-ahead ({gone}): nothing was published"
        if refusal is not None:
            bridge.command(job, "cancel")
            return Outcome("failed", screenshot=screenshot, error=f"PosterError: {refusal}", note=_joined(self.notes),
                           guesses=list(self.guesses))
        if self.on_go_ahead is not None and not self.on_go_ahead():      # the row → 'posting', or no go-ahead
            bridge.command(job, "cancel")
            return Outcome("failed", screenshot=screenshot, note=_joined(self.notes), guesses=list(self.guesses),
                           error=f"PosterError: {self.site}: the row couldn't be taken for posting (another poster "
                                 "has it?) — nothing was published")
        self.clicked = True
        self._say("go-ahead sent: one click")
        clicked_at = time.monotonic()
        bridge.command(job, "submit")
        try:
            ev = await job.wait(("result", "error"), max(deadline - time.monotonic(), AFTER_CLICK_MIN))
        except (TimeoutError, asyncio.TimeoutError):
            ev = {"event": "error", "stage": "after_publish", "page": "unknown",
                  "message": f"no answer in {AFTER_CLICK_MIN / 60:.0f} min after the click"}
        self._record(job, {"item": r.sku, "seen": seen, "expected": want, "after": ev})
        after = self._kept(job, "after-publish", "landing", "refused", "form")
        address = self.listing_address(ev.get("url") or "") if ev.get("event") == "result" else None
        if address and address in self.held:          # another item's listing: never this one's
            self.notes.append(f"the page after the click was {address}, which another item holds")
            address = None
        if address and self.name == "depop" and r.sku:
            # WO35: the address Depop's success page named — its /manage/ page must show our SKU
            try:
                ok, skus = await self._sku_on(address, r.sku)
            except Exception as e:  # noqa: BLE001 — the address stands (Depop named it after our click); said
                ok, skus = None, [f"{type(e).__name__}: {e}"]
            if ok is not True:
                self.notes.append(f"the SKU on {address}manage/ is {', '.join(skus) or 'not shown'}, not {r.sku}")
        if address:
            if ev.get("live"):
                self._live_check(ev.get("live"), r)
            return self._confirmed(address, clicked_at, after, ev.get("how") or "the page after the click")
        why = f"{ev.get('page') or 'unknown'} page: {ev.get('message') or ev.get('url') or ''}"
        # WO39: still the sell form after the click is a refusal, never "posted but unconfirmed" (live 2026-10-16: the
        # Sam Edelman twice on Vinted) — the extension says it with every error the form showed since the click
        refused = ev.get("page") == "form" and ev.get("stage") == "after_publish"
        self._say("looking for the new listing by its address")
        url, looked = await self.confirm_by_address(r, until=clicked_at + (0.0 if refused else CONFIRM_FOR))
        self.notes.append(f"after the click: {why}; looked up: {json.dumps(looked, default=str)[:300]}")
        if url:
            return self._confirmed(url, clicked_at, after, looked.get("how") or "the site's own data")
        if refused and "error" not in looked:          # the site said no, and its own data agrees: nothing published
            return Outcome("failed", screenshot=after, note=_joined(self.notes), guesses=list(self.guesses),
                           error=f"{self.site} refused the form: {refusal_text(ev.get('message'))}")
        return Outcome("failed", screenshot=after, clicked=True, note=_joined(self.notes), guesses=list(self.guesses),
                       error=f"after the click no listing page ({why}); not found by its address in "
                             f"{CONFIRM_FOR / 60:.0f} min — checked again later")

    def _confirmed(self, url: str, clicked_at: float, shot: str | None, how: str) -> Outcome:
        self.confirmed_in = round(time.monotonic() - clicked_at, 1)
        self._say(f"confirmed in {self.confirmed_in:.0f} s ({how})")
        return Outcome("posted", url=url, screenshot=shot, clicked=True, guesses=list(self.guesses),
                       note=_joined(self.notes))

    async def confirm_by_address(self, r: Render | None, until: float) -> tuple[str | None, dict]:
        """WO35 §3: the new listing by the site's own data (find_live), looked for until `until` (monotonic), every
        CONFIRM_EVERY seconds. Returns (address, what was seen); `seen["error"]` when the data couldn't be read."""
        while True:
            try:
                url, looked = await self.find_live(None, r, since=0)
            except Exception as e:  # noqa: BLE001 — not readable now: looked at again, else unconfirmed
                url, looked = None, {"error": f"{type(e).__name__}: {e}"}
            left = until - time.monotonic()
            if url or left <= 0:
                return url, looked
            await asyncio.sleep(min(CONFIRM_EVERY, left))

    async def _sku_on(self, address: str, sku: str | None) -> tuple[bool | None, list[str]]:
        """Depop's /manage/ page of a listing: does it show our SKU (WO35)? (True / False / None when unread, the SKUs
        it shows)."""
        ev = await self._job("verify", {"listing_url": address.rstrip("/") + "/manage/"}, 120)
        if ev.get("event") == "error":
            if ev.get("page") in STOP_PAGES:
                raise self._stop(ev)
            return None, []
        live = ev.get("live") or {}
        skus = [str(x) for x in live.get("skus") or []]
        if not skus:
            skus = SKU.findall(f"{live.get('body') or ''}")
        return ((sku in skus) if sku else None), skus

    @staticmethod
    def shows_title(live: dict, title: str) -> bool:
        """The listing page shows this title: its own title (Depop: the first line of its description), else its text."""
        want = _norm(title)[:40]
        return bool(want) and (want in _norm(live.get("title")) or want in _norm(live.get("body")))

    @staticmethod
    def shows_price(live: dict, price: int) -> bool:
        return _shows_price(str(live.get("price") or ""), price) or _shows_price(live.get("body") or "", price)

    def _live_check(self, live: dict | None, r: Render) -> None:
        """The listing page against the fields: the title, the price, the size, the photo count — a difference is a
        note for the ops chat; the listing stays up."""
        if not live:
            self.notes.append("live check: the listing page wasn't read")
            return
        if live.get("shop"):
            self.learned_shop = str(live["shop"])
        body = _norm(f"{live.get('body') or ''} {live.get('attributes') or ''}")
        problems = []
        if not self.shows_title(live, self._title()):
            problems.append("the title isn't on the page")
        if not self.shows_price(live, int(self.fields.price)):
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

    # ---- WO37: a live listing's cover fixed on its edit page (the owner, 2026-10-12)

    def edit_url(self, url: str) -> str | None:
        """The listing's edit page, as WO37's probe recorded it: Depop /products/edit/<slug>/, Vinted /items/<id>/edit."""
        address = self.listing_address(url or "") or ""
        if self.name == "depop" and (m := re.search(r"/products/([a-z0-9]+(?:-[a-z0-9]+)+)/?$", address)):
            return f"https://www.depop.com/products/edit/{m[1]}/"
        if self.name == "vinted" and (m := re.search(r"/items/(\d+)", address)):
            return f"https://www.vinted.com/items/{m[1]}/edit"
        return None

    async def set_cover(self, url: str, photo: int, count: int, dry: bool = False) -> dict:
        """The listing's photo at `photo` (0-based, its order as posted: `count` photos) moved first on its edit page with
        the photo grid's own keyboard reordering (ext/content/common.js T.coverFirst); the page's new order checked here
        — that photo first, every other one in its order — before the ONE go-ahead; Save pressed once (never in a dry
        run); then the listing page read: its first photo must be the one moved. Returns {"ok", "saved", "before",
        "after", "live", "error", "shot"}. Nothing is saved when anything differs."""
        edit = self.edit_url(url)
        if edit is None:
            return {"ok": False, "saved": False, "error": f"not a {self.site} listing address: {url}"}
        if not await self.wait_connected():
            raise PosterError(f"{self.site}: the Thrift Chrome extension isn't connected")
        bridge = self._bridge()
        job = bridge.submit(self.name, "cover", {"fields": {}, "copy": {}, "price": None, "photos": [],
                                                 "listing_url": url, "edit_url": edit, "from": photo, "count": count,
                                                 "dry": dry}, shot=self.shot)
        job.listener = self._listen
        try:
            out = await self._cover(bridge, job, url, photo, dry)
        finally:
            if not job.done:                      # given up on: called off, its tab closed — nothing saved
                bridge.drop(job)
                try:
                    await bridge.settle(job, 5.0)
                except BaseException:  # noqa: BLE001
                    pass
        return out

    async def _cover(self, bridge: bridge_mod.Bridge, job: bridge_mod.Job, url: str, photo: int, dry: bool) -> dict:
        try:
            ev = await self._await(job, ("ready", "result", "error"), job.created + bridge_mod.JOB_TIMEOUT)
        except TimeoutError:
            return {"ok": False, "saved": False, "error": "the edit page wasn't done in time: nothing saved"}
        if ev.get("event") == "error":
            if ev.get("page") in STOP_PAGES:
                raise self._stop(ev)
            return {"ok": False, "saved": False, "shot": self._kept(job, "form"),
                    "error": f"{ev.get('stage') or 'edit'}: {ev.get('message')}"}
        seen = ev.get("seen") or {}
        before, after = list(seen.get("cover_before") or []), list(seen.get("cover_after") or [])
        out = {"before": before, "after": after, "shot": self._kept(job, "form"), "saved": False}
        if not cover_moved(before, after, photo):
            if ev.get("event") == "ready":
                bridge.command(job, "cancel")
            return {**out, "ok": False, "error": "the page's order isn't the old one with that photo first: nothing saved"}
        if ev.get("event") == "result" or dry:
            if ev.get("event") == "ready":
                bridge.command(job, "cancel")
            return {**out, "ok": True}                                  # a dry run: moved on the page, never saved
        if (n := seen.get("submit_buttons")) != 1:
            bridge.command(job, "cancel")
            return {**out, "ok": False, "error": f"the edit page's Save differs from the recording ({n} found): "
                                                 "nothing saved"}
        self._say("go-ahead sent: Save once")
        bridge.command(job, "submit")
        try:
            ev = await job.wait(("result", "error"), AFTER_CLICK_MIN)
        except (TimeoutError, asyncio.TimeoutError):
            ev = {"event": "error", "message": "no listing page after Save"}
        # a form the site refused stays on the page with its errors: nothing was saved (live 2026-10-13, the Tory
        # loafers: Depop's edit form had come up with its category, brand and condition empty)
        refused = ev.get("event") == "error" and str(ev.get("message") or "").startswith("refused:")
        out.update(saved=not refused, shot=self._kept(job, "after-publish", "refused", "form") or out["shot"])
        if refused:
            return {**out, "ok": False, "error": f"{self.site} refused the change ({ev['message'][len('refused: '):]}): "
                                                 "nothing saved"}
        live = ev.get("live") or {} if ev.get("event") == "result" else {}
        if not live.get("first_photo"):                # the listing page read again (the landing may be another page)
            check = await self._job("verify", {"listing_url": url}, 120)
            live = check.get("live") or {} if check.get("event") == "result" else live
        moved, first = photo_key(before[photo]), photo_key(live.get("first_photo"))
        out["live"] = live
        if first == moved:
            return {**out, "ok": True}
        return {**out, "ok": False, "error": f"after Save the listing's first photo is {first or 'not shown'}, not the "
                                             f"one moved ({moved})"}

    async def wait_connected(self, timeout: float = CONNECT_WAIT) -> bool:
        end = time.monotonic() + timeout
        while self.bridge is not None and not self.bridge.connected() and time.monotonic() < end:
            await asyncio.sleep(0.2)
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
        # WO35: on Depop the seller's /manage/ page — the title and price as on the listing, and the SKU: our item id
        target = address.rstrip("/") + "/manage/" if self.name == "depop" else address
        ev = await self._job("verify", {"listing_url": target}, 180, shot=self.shot)
        if ev.get("event") == "error":
            if ev.get("page") in STOP_PAGES:
                raise self._stop(ev)
            raise PosterError(f"{address}: {ev.get('message')}")
        live = ev.get("live") or {}
        if live.get("shop"):
            self.learned_shop = str(live["shop"])
        if not self.shows_title(live, r.title) and not self.shows_title(live, self._title() if self.fields else r.title):
            raise PosterError(f"the listing page at {address} doesn't show the title")
        if not self.shows_price(live, r.price):
            raise PosterError(f"the listing page at {address} doesn't show the price ${r.price}")
        if self.name == "depop" and r.sku:
            skus = [str(x) for x in live.get("skus") or []] or SKU.findall(f"{live.get('body') or ''}")
            if r.sku not in skus:
                raise PosterError(f"the SKU on {target} is {', '.join(skus) or 'not shown'}, not {r.sku}")

    async def find_live(self, ctx, r: Render | None, since: float, created: str | None = None
                        ) -> tuple[str | None, dict]:
        """This listing by the site's own data (WO35 §3): Vinted — its JSON of the seller's newest items, the exact
        title (the newest such one no other item holds); Depop — the shop's listings whose address carries the title's
        words after the username part, confirmed by the SKU on their /manage/ page. Returns (address, what was seen);
        `seen["error"]` when the data couldn't be read (never taken for "absent"). No shop configured: it can't look."""
        shop = self.shop or self.learned_shop or ""
        if not shop:
            return None, {"error": f"no {self.name} shop page to look at (set marketplaces.{self.name}.shop)"}
        if not await self.wait_connected():
            raise PosterError(f"{self.site}: the Thrift Chrome extension isn't connected — the shop check waits")
        title = r.title if r is not None else self._title()
        sku = r.sku if r is not None else None
        ev = await self._job("find", {"shop": shop, "wardrobe": self.name == "vinted"}, 180)
        if ev.get("event") == "error":
            if ev.get("page") in STOP_PAGES:
                raise self._stop(ev)
            return None, {"error": ev.get("message"), "page": ev.get("page")}
        rows = [x for x in ev.get("listings") or [] if self.listing_address(str(x.get("url") or ""))]
        if self.name == "vinted":
            ours = []
            for x in rows:                                  # newest first, as Vinted lists them
                address = self.listing_address(str(x["url"]))
                text = str(x.get("title") or str(x.get("text") or "").split(", brand:")[0])
                if same_title(text, title) and address not in self.held and address not in ours:
                    ours.append(address)
            seen = {"listings": len(rows), "source": ev.get("source") or "tiles", "with_this_title": ours[:5],
                    "how": "Vinted's newest items, the exact title"}
            if len(ours) > 1:                               # never a guess: not absent either — the owner looks
                self.notes.append(f"several Vinted listings carry this exact title: {', '.join(ours[:3])}")
                return None, {**seen, "error": f"{len(ours)} listings carry this exact title"}
            return (ours[0], seen) if ours else (None, seen)
        candidates = []
        for x in rows:
            address = self.listing_address(str(x["url"]))
            if address not in candidates and address not in self.held and slug_matches(address, title):
                candidates.append(address)
        confirmed, checked, unread = [], {}, []
        for address in candidates[:4]:
            ok, skus = await self._sku_on(address, sku)
            checked[address] = skus
            if ok:
                confirmed.append(address)
            elif ok is None:
                unread.append(address)
        seen = {"listings": len(rows), "by_words": candidates[:5], "skus": checked,
                "how": "the Depop shop, the title's words, the SKU on /manage/"}
        if len(confirmed) > 1:                              # never a guess (one SKU, two listings: the owner looks)
            return None, {**seen, "error": f"{len(confirmed)} listings show this SKU"}
        if not confirmed and unread:                        # a /manage/ page that couldn't be read: not "absent"
            return None, {**seen, "error": f"couldn't read the SKU on {', '.join(unread[:2])}"}
        return (confirmed[0], seen) if confirmed else (None, seen)

    async def shop_listings(self) -> list[dict]:
        """Every listing the seller's shop page shows (WO33: the items she listed by hand), as {url, text} — the
        listing's own address and the longest text seen for it (Vinted: the tile's description)."""
        shop = self.shop or self.learned_shop or ""
        if not shop:
            raise PosterError(f"no {self.name} shop page to read (set marketplaces.{self.name}.shop)")
        ev = await self._job("find", {"shop": shop}, 180)
        if ev.get("event") == "error":
            if ev.get("page") in STOP_PAGES:
                raise self._stop(ev)
            raise PosterError(f"{self.site} shop {shop}: {ev.get('message')}")
        if not ev.get("listings"):
            self.notes.append(f"{self.site}: the shop page showed no listings ({ev.get('url') or shop})")
        out: dict[str, str] = {}
        for x in ev.get("listings") or []:
            if address := self.listing_address(str(x.get("url") or "")):
                text = str(x.get("text") or "").strip()
                out[address] = text if len(text) > len(out.get(address, "")) else out.get(address, "")
        return [{"url": u, "text": t} for u, t in out.items()]

    async def probe_delist(self, url: str) -> dict:
        """WO33's no-click recording of the take-down control: our listing's page opened in the Thrift Chrome, Hide /
        Mark as sold looked for, the page and its picture kept — nothing clicked. Returns {"found", "text", "url",
        "shot"}."""
        address = self.listing_address(url)
        if address is None:
            raise PosterError(f"not a {self.name} listing address: {url!r}")
        ev = await self._job("probe", {"listing_url": address}, 120, shot=self.shot)
        if ev.get("event") == "error":
            if ev.get("page") in STOP_PAGES:
                raise self._stop(ev)
            raise PosterError(f"{self.site} probe {address}: {ev.get('message')}")
        return {"found": bool(ev.get("found")), "text": ev.get("text"), "url": ev.get("url"), "shot": str(self.shot)}

    async def delist(self, url: str) -> bool | None | str:
        """The take-down, once its steps are recorded: Vinted's Hide (reversible); Depop's DELETE (the owner's
        decision, WO33 — its page offers no Mark as sold): on /products/<slug>/manage/ the bin, the "Delete listing"
        window, Delete listing pressed once, then the address opened again — gone is done. True: taken down; None:
        the listing was gone already; "sold": Depop shows it sold already (nothing pressed: it sold twice)."""
        if missing := sorted(DELIST_NEEDS[self.name] & unverified(self.name, self.selectors_path)):
            raise PosterError(f"{self.site}: delisting isn't recorded yet ({', '.join(missing)} UNVERIFIED in "
                              "ext/selectors.json)")
        address = self.listing_address(url)
        if address is None:
            raise PosterError(f"not a {self.name} listing address: {url!r}")
        if self.name != "depop":
            ev = await self._job("delist", {"listing_url": address}, 180, shot=self.shot)
            if ev.get("event") == "error":
                if ev.get("page") in STOP_PAGES:
                    raise self._stop(ev)
                raise PosterError(f"{self.site} delist {address}: {ev.get('message')}")
            return bool(ev.get("delisted"))
        before = await self._depop_state(address)
        if before == "gone":
            return None
        if before == "sold":
            return "sold"
        self.lines = []
        ev = await self._job("delist", {"listing_url": address.rstrip("/") + "/manage/", "check_url": address}, 180,
                             shot=self.shot)
        if ev.get("event") == "result" and ev.get("sold"):
            return "sold"
        pressed = ev.get("delete_pressed") or any(line.startswith("delete_listing") for line in self.lines)
        if ev.get("event") == "error" and not pressed:
            if ev.get("page") in STOP_PAGES:
                raise self._stop(ev)
            raise PosterError(f"{self.site} delete {address}: {ev.get('message')}")
        after = await self._depop_state(address)           # the page may have moved on: the address itself decides
        if after == "gone":
            return True
        raise PosterError(f"{self.site}: Delete listing pressed, but {address} is still there ({after})")

    async def _depop_state(self, address: str) -> str:
        """Depop's listing page as it is now: "live", "sold" (its JSON-LD says SoldOut) or "gone" (no product there)."""
        ev = await self._job("verify", {"listing_url": address}, 120)
        if ev.get("event") == "error":
            if ev.get("page") in STOP_PAGES:
                raise self._stop(ev)
            return "gone"                                     # not a listing page any more
        live = ev.get("live") or {}
        if not live.get("product"):
            return "gone"
        return "sold" if "soldout" in str(live.get("availability") or "").lower() else "live"

    async def practice_delete(self, url: str) -> dict:
        """WO33, the owner's practice run on a live Depop listing: the bin pressed, the "Delete listing" window
        recorded, Cancel pressed — never Delete listing — and the listing checked still live."""
        address = self.listing_address(url)
        if self.name != "depop" or address is None:
            raise PosterError(f"the practice run is for a Depop listing address, not {url!r}")
        ev = await self._job("practice", {"listing_url": address.rstrip("/") + "/manage/", "check_url": address}, 120,
                             shot=self.shot)
        if ev.get("event") == "error":
            if ev.get("page") in STOP_PAGES:
                raise self._stop(ev)
            raise PosterError(f"{self.site} practice {address}: {ev.get('message')}")
        still = await self._depop_state(address)
        return {"window": ev.get("window") or {}, "closed": bool(ev.get("closed")), "live_seen_by_page": ev.get("live"),
                "state_after": still, "shot": str(self.shot)}
