"""WO57 §5 — the fourth site's poster: `marketplaces.ebay.driver: api`. Everything is an HTTPS call (thrift_agent.ebay.
client); nothing opens on the owner's Mac.

A listing, in order: the item mapped (`plan`: eBay's own category suggestion, its item specifics and conditions, the
price worked out from her price) → eBay's VALIDATE (everything checked, nothing created) → a dry run ends here → the
seller's listings looked at by SKU (a listing of this item already there is recorded, never made twice) → every photo
up to eBay Picture Services, the cover first → the ONE go-ahead (the row becomes 'posting') → createListing with the
item's idempotency key → the listing read back by API (title, price, photos, size, shipping, returns, location)
before anything says live. eBay's answer carries the listing id: confirmed at once. An answer that never came is
"verifying" like on every site — looked for by SKU, and a repeat of the call carries the same key.

The agent acts only on listings whose SKU is one of our item ids (`ours`): the owner's hand-made listings are never
read as ours, changed or ended. A refusal about the item is the item's (WO54 rule 4); a policy refusal or eBay's own
removal is recorded and never posted again; a missing consent stops eBay quietly — thrift-api's private message
carries the link."""
from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime
from pathlib import Path

from thrift_agent.catalogs.common import ItemView, MappingError
from thrift_agent.ebay import LABEL, SITE, mapping
from thrift_agent.ebay import client as api
from thrift_agent.post.base import AccountBlocked, Mode, Outcome, Poster, PosterError
from thrift_agent.post import fallback
from thrift_agent.schema import Render

ITEM_URL = re.compile(r"/itm/(?:[^/?#]+/)?(\d{9,})")
OUR_SKU = re.compile(r"^i_\d{6}_[0-9a-f]{6}$")
RECONNECT_EVERY = 300.0        # a missing consent is asked about again this often (thrift-api knows first)
REFUSED_FOR_GOOD = "refused by eBay"       # the error of a row eBay refused or removed for good: never posted again
NOT_CONNECTED = "eBay isn't connected"


def listing_id_of(url: str | None) -> str | None:
    """eBay's listing id from a listing's address (https://www.ebay.com/itm/<id>, with or without a title slug)."""
    m = ITEM_URL.search(str(url or ""))
    return m.group(1) if m else None


def plain_text(description: str | None) -> str:
    """Our simple HTML description back as text (paragraphs and line breaks), for a read-back."""
    t = re.sub(r"</p>\s*<p>", "\n\n", str(description or ""), flags=re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", t)).strip()


def _same(a, b) -> bool:
    return " ".join(str(a or "").split()) == " ".join(str(b or "").split())


def differences(f: mapping.EbayFields, live: api.Live, photos: int) -> list[str]:
    """What the listing eBay shows differs in from the one we sent — the read-back before anything says live."""
    out = []
    if not _same(live.title, f.title):
        out.append(f"title '{live.title}' (sent '{f.title}')")
    if live.price is None or abs(float(live.price) - float(f.price)) >= 0.005:
        out.append(f"price {live.price} (sent {f.price})")
    if len(live.photos) != photos:
        out.append(f"photos {len(live.photos)} of {photos}")
    if f.size and f.size not in [v for vs in live.aspects.values() for v in vs]:
        out.append(f"size '{f.size}' not among its item specifics")
    want_type = "CALCULATED" if f.shipping.get("type") == "calculated" else "FLAT"
    if live.shipping_type and live.shipping_type != want_type:
        out.append(f"shipping {live.shipping_type} (sent {want_type})")
    if f.returns.get("accepted") and (live.returns_accepted is False or
                                      (live.returns_payer and live.returns_payer != f.returns.get("payer")) or
                                      (live.returns_days and live.returns_days != f"DAYS_{f.returns.get('days')}")):
        out.append(f"returns {live.returns_accepted} / {live.returns_days} / {live.returns_payer}")
    if live.postal_code and str(live.postal_code) != str(f.location.get("postal_code")):
        out.append(f"location {live.postal_code} (sent {f.location.get('postal_code')})")
    if live.sku and live.sku != f.sku:
        out.append(f"SKU {live.sku} (sent {f.sku})")
    return out


class EbayPoster(Poster):
    driver = "api"
    name = SITE
    site = LABEL
    create_url = api.HOSTS["production"]["graphql"]

    def __init__(self, s=None, aliases=None, client: api.EbayClient | None = None):
        self.s = s
        self.aliases = aliases
        e = (s.get("marketplaces.ebay") or {}) if s is not None else {}
        self.cfg = mapping.Config.from_settings(s)
        cache = (s.path("db").parent / "ebay") if s is not None else None
        self.client = client or api.EbayClient(api.TokenSource(), str(e.get("environment") or "production"),
                                               cache_dir=cache)
        self.create_url = self.client.hosts["graphql"]
        self.fields: mapping.EbayFields | None = None
        self.confirm = None             # a supervised publish: async (fields, site) -> bool
        self.on_go_ahead = None         # () -> bool: take the row for posting (False: nothing is created)
        self.progress = None
        self.lines: list[str] = []
        self.strict = False
        self.held: set[str] = set()
        self.notes, self.guesses = [], []
        self.error_kind = None          # health.kind_of's hint: "site" | "code" (ours) — an item failure leaves it None
        self.shot = None
        self.created_id = None
        self.last_fee = None            # the insertion fee eBay named for the last listing (0.35 past the free ones)
        self._probed = 0.0

    # ------------------------------------------------------------ the Playwright posters' page steps: not here

    async def check_account(self, page) -> None:
        raise PosterError("the eBay poster has no page: it calls eBay's API")

    async def fill(self, page, r: Render) -> None:
        raise PosterError("the eBay poster has no page: it calls eBay's API")

    async def read_back(self, page) -> dict:
        raise PosterError("the eBay poster has no page: it calls eBay's API")

    async def submit(self, page, mode: Mode) -> str | None:
        raise PosterError("the eBay poster has no page: it calls eBay's API")

    def expected(self, r: Render, seen: dict | None = None) -> dict:
        return {}

    # ------------------------------------------------------------ is eBay there?

    def available(self) -> bool:
        """Can a job start now? Only with a working consent (a short-lived token from thrift-api). A missing one is
        asked about again every RECONNECT_EVERY — never per turn — and thrift-api's own message has the link."""
        t = self.client.tokens
        if t.connected is True:
            return True
        if time.time() - self._probed < RECONNECT_EVERY and self._probed:
            return False
        self._probed = time.time()
        try:
            t.token("user", fresh=True)
        except api.EbayError:
            return False
        return t.connected is True

    def why_unavailable(self) -> str:
        return self.client.tokens.why or "thrift-api gave no eBay token"

    def listing_address(self, url: str) -> str | None:
        lid = listing_id_of(url)
        return self.client.item_url(lid) if lid else None

    def _say(self, line: str) -> None:
        self.lines.append(line)
        if self.progress is not None:
            try:
                self.progress(line)
            except Exception:  # noqa: BLE001 — a progress line never fails a listing
                pass

    def _keep(self, shots: Path | None, sku: str, record: dict) -> str | None:
        """The attempt's evidence beside the other sites' (failed/shots/<item>-ebay-<time>.json): what was sent
        (never a token) and what eBay said."""
        if shots is None:
            return None
        try:
            shots.mkdir(parents=True, exist_ok=True)
            path = shots / f"{sku}-ebay-{datetime.now():%Y%m%d-%H%M%S}.json"
            path.write_text(json.dumps(record, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
            return str(path)
        except OSError:
            return None

    @staticmethod
    def ours(live: api.Live | None, sku: str | None = None) -> bool:
        """Is this listing one the agent made? Its SKU is one of our item ids (and the one expected, when given). A
        listing the owner made by hand has no such SKU: never read as ours, changed or ended."""
        if live is None or not OUR_SKU.fullmatch(str(live.sku or "")):
            return False
        return sku is None or live.sku == sku

    # ------------------------------------------------------------ the plan

    async def plan(self, view: ItemView) -> mapping.EbayFields:
        """The item as an eBay listing: eBay's own category for it, the specifics and conditions that category takes,
        eBay's own code for the shipping service, the price from her price. Raises MappingError (no size at all, a
        size nowhere near eBay's list, a used item where eBay takes only new) or EbayError (eBay couldn't be read)."""
        if (ban := mapping.policy_ban(view)) is not None:     # WO58 rule 5: eBay's written ban — no API call made
            raise ban
        refused: MappingError | None = None
        tried: list[tuple[mapping.Category, str | None]] = []
        best = None                                 # (closeness, order, fields, note): the list that comes closest
        f = note = None
        for n, (suggestion, its_note) in enumerate(await self.client.categories_for(view)):
            category = await self.client.category(suggestion)
            try:
                got = mapping.map_ebay(view, category, self.cfg, self.aliases)
            except MappingError as e:               # the size or the condition isn't THIS category's: the next one
                if not (mapping.size_refused(e) or mapping.NEW_ONLY in str(e)):
                    raise
                refused = refused if refused is not None and mapping.size_refused(refused) else e
                if mapping.size_refused(e):
                    tried.append((category, its_note))
                continue
            if mapping.size_as_written(got):
                f, note = got, its_note
                break
            close = mapping.size_closeness(got)
            if best is None or (close, n) < best[:2]:
                best = (close, n, got, its_note)
        if f is None and best is not None:
            _, _, f, note = best
        if f is None and tried:                     # WO58: no category of the item's holds its size — the first's list
            category, note = tried[0]               # gives its neutral value or its middle, said; never a skip
            f = mapping.map_ebay(view, category, self.cfg, self.aliases, last=True)
        if f is None:
            # what's left: eBay suggested nothing, or every category takes only new items and this one is used — eBay
            # refusing it after every fallback (the one other honest reason, said privately)
            if refused is not None and mapping.NEW_ONLY in str(refused):
                raise mapping.PolicyBan("a pre-owned one in the categories eBay suggests for it", str(refused),
                                        "item conditions for those categories")
            raise refused or MappingError("eBay suggested no category")
        f.shipping["service_code"] = await self.client.service_code(f.shipping["service"], f.shipping["service_code"])
        if note:
            f.guesses.insert(0, note)
        return f

    # ------------------------------------------------------------ a listing

    def _failed(self, e: api.EbayError, shots: Path | None, sku: str, record: dict, clicked_at: float | None = None
                ) -> Outcome:
        """An eBay failure as the poster's Outcome, by its kind (so one item never stops the site — rule 4)."""
        shot = self._keep(shots, sku, {**record, "error": str(e), "kind": e.kind, "violations": e.violations})
        note = "; ".join(self.notes) or None
        if e.kind == api.AUTH:
            raise AccountBlocked(f"{NOT_CONNECTED}: {e}", page="login", shot=shot)
        if clicked_at is not None and e.unknown:
            # the create call may have reached eBay: "verifying" — looked for by SKU, never made again blind
            return Outcome("failed", screenshot=shot, clicked=True, clicked_at=clicked_at, note=note,
                           guesses=list(self.guesses), error=f"no answer to createListing ({e}); looked for by its SKU")
        if e.kind == api.POLICY:
            return Outcome("skipped", screenshot=shot, note=note, guesses=list(self.guesses),
                           error=f"{REFUSED_FOR_GOOD} ({str(e)[:300]}) — not posted there again")
        if e.kind == api.ITEM:
            return Outcome("failed", screenshot=shot, note=note, guesses=list(self.guesses), error=str(e)[:600])
        self.error_kind = "site"                         # eBay's own failure, its limit, or the network: not the item's
        what = {api.LIMIT: "eBay's limit", api.INFRA: "the network", api.SITE: "eBay's API"}.get(e.kind, "eBay's API")
        return Outcome("failed", screenshot=shot, note=note, guesses=list(self.guesses), error=f"{what}: {e}"[:600])

    async def post(self, ctx, r: Render, mode: Mode, dry_run: bool, shots: Path, stage: str = "form") -> Outcome:
        """One item on eBay (see the module). Nothing after the go-ahead raises: the listing may be live."""
        self.notes, self.guesses, self.lines, self.error_kind, self.created_id = [], [], [], None, None
        f = self.fields
        if f is None:
            return Outcome("failed", error="ebay: no mapped fields for this item")
        if mode != "publish":
            return Outcome("failed", error="ebay: saving a draft isn't a call; only a validation or a listing")
        self.guesses = list(f.guesses)
        sku = f.sku
        record = {"item": sku, "fields": f.model_dump(), "environment": self.client.environment}
        t0 = time.monotonic()
        try:
            generation = 0
            if not dry_run:
                # 1. never twice: a listing of this item already there (an answer lost before) is the listing; one
                #    eBay itself removed is never followed by another (rule 4)
                mine = await self._mine(sku)
                if held := next((x for x in mine if x.on_hold), None):
                    raise api.EbayError(f"eBay removed this item's listing ({held.hold or 'on hold'})", api.POLICY)
                if (found := next((x for x in mine if x.for_sale), None)) is not None:
                    self._say("found already live by its SKU")
                    return self._posted(f, found, len(found.photos), shots, record, "found by its SKU before creating")
                generation = sum(1 for x in mine if not x.for_sale)
            # 2. the cover up to eBay's picture service, then eBay's own check of the whole listing: nothing is created
            urls = [await self.client.upload_image(f.photos[0])] if f.photos else []
            key = api.idempotency_key(sku, generation)
            checked = await self.client.create(f, urls, key, validate=True)
            self._say("validated ✓")
            record["validation"] = {"fees": checked.fees, "warnings": checked.warnings}
            if dry_run:
                shot = self._keep(shots, sku, record)
                fee = f"insertion fee ${checked.insertion_fee:.2f}" if checked.fees else "no fee named"
                return Outcome("dryrun", screenshot=shot, guesses=list(self.guesses),
                               note="; ".join([f"validated by eBay, nothing created ({fee})", *checked.warnings]))
            # 3. the rest of the photos, in the listing's order (the cover stays first)
            for n, photo in enumerate(f.photos[1:], 2):
                urls.append(await self.client.upload_image(photo))
                self._say(f"photos {n}/{len(f.photos)}")
        except api.EbayError as e:
            return self._failed(e, shots, sku, record)
        if self.confirm is not None and not await self.confirm(f, LABEL):
            return Outcome("cancelled", note="not confirmed: nothing was created")
        if self.on_go_ahead is not None and not self.on_go_ahead():
            return Outcome("cancelled", note="the row was taken elsewhere: nothing was created")
        clicked_at = time.time()
        try:
            made = await self.client.create(f, urls, key)
        except api.EbayError as e:
            return self._failed(e, shots, sku, record, clicked_at)
        self.created_id = made.listing_id
        self.last_fee = made.insertion_fee
        record["created"] = {"listing_id": made.listing_id, "fees": made.fees, "warnings": made.warnings}
        self._say(f"created in {time.monotonic() - t0:.1f} s")
        live = made.live
        try:
            live = await self.client.read_one(made.listing_id) or made.live
        except api.EbayError as e:                       # it IS live (eBay named its id): the read-back is a note
            self.notes.append(f"created, but the read-back failed ({e})")
        return self._posted(f, live, len(urls), shots, record, "eBay's answer", made.warnings)

    def _posted(self, f: mapping.EbayFields, live: api.Live, photos: int, shots: Path | None, record: dict, how: str,
                warnings: list[str] | None = None) -> Outcome:
        diff = differences(f, live, photos)
        if diff:
            self.notes.append("eBay shows it differently: " + "; ".join(diff))
        if warnings:
            self.notes.append("eBay's warnings: " + "; ".join(warnings)[:300])
        record["read_back"] = {"url": live.url, "status": live.status, "title": live.title, "price": live.price,
                               "photos": len(live.photos), "differences": diff, "how": how}
        shot = self._keep(shots, f.sku, record)
        return Outcome("posted", url=live.url or self.client.item_url(live.listing_id), screenshot=shot, clicked=True,
                       guesses=list(self.guesses), note="; ".join(self.notes) or None, created_id=live.listing_id)

    async def _mine(self, sku: str) -> list[api.Live]:
        return [x for x in await self.client.by_sku(sku) if self.ours(x, sku)]

    # ------------------------------------------------------------ the site's own data (verifying, the card, the checks)

    async def find_live(self, ctx, r: Render | None, since: float, created: str | None = None
                        ) -> tuple[str | None, dict]:
        """Is this item's listing on eBay? By its SKU among the seller's listings (the item id eBay stores with it):
        (its address, how) — or (None, {"result": "absent" | "unknown", …}). An ended listing is not "found"."""
        sku = getattr(r, "sku", None)
        if not sku:
            return None, {"result": "unknown", "why": "no SKU to look for"}
        try:
            mine = await self._mine(sku)
        except api.EbayError as e:
            if e.kind == api.AUTH:
                raise AccountBlocked(f"{NOT_CONNECTED}: {e}", page="login") from None
            return None, {"result": "unknown", "why": str(e)[:200]}
        live = next((x for x in mine if x.for_sale and x.url not in self.held), None)
        if live is not None:
            return live.url, {"result": "found", "how": "eBay's listings by SKU", "listing_id": live.listing_id}
        return None, {"result": "absent", "how": "eBay's listings by SKU", "ended": [x.listing_id for x in mine]}

    async def verify_live(self, page, url: str, r: Render) -> None:
        live = await self._read(url, r.sku)
        if live is None or not live.for_sale:
            raise PosterError(f"eBay doesn't show {url} for sale")

    async def _read(self, url: str, sku: str | None = None) -> api.Live | None:
        """A listing of OURS by its address; None when eBay has none. Raises PosterError for one that isn't ours."""
        lid = listing_id_of(url)
        if not lid:
            raise PosterError(f"not an eBay listing address: {url}")
        try:
            live = await self.client.read_one(lid)
        except api.EbayError as e:
            if e.kind == api.AUTH:
                raise AccountBlocked(f"{NOT_CONNECTED}: {e}", page="login") from None
            raise
        if live is not None and not self.ours(live, sku):
            raise PosterError(f"eBay listing {lid} isn't one of the agent's (SKU {live.sku or 'none'}): left alone")
        return live

    async def read_listing(self, url: str) -> dict:
        live = await self._read(url)
        if live is None:
            return {"state": "gone"}
        return {"state": self._state(live), "title": live.title, "price": live.price, "photos": len(live.photos),
                "description": plain_text(live.description), "sku": live.sku, "aspects": live.aspects}

    @staticmethod
    def _state(live: api.Live) -> str:
        if live.for_sale:
            return "live"
        if live.was_sold:
            return "sold"
        return "gone"                                     # ended by her, or removed by eBay (its hold says why)

    async def read_shop(self) -> dict:
        """The photo check's bulk read: every ACTIVE listing of ours, in one paged read — the owner's hand-made
        listings (no SKU of ours) are left out of it."""
        try:
            listings, complete = await self.client.active()
        except api.EbayError as e:
            return {"error": str(e)[:200]}
        mine = [x for x in listings if self.ours(x)]
        return {"listings": [{"id": x.listing_id, "url": x.url, "photos": len(x.photos), "price": x.price,
                              "sold": False, "is_closed": False, "sku": x.sku} for x in mine],
                "source": "eBay's Listing API", "complete": complete, "total": len(listings),
                "note": f"{len(listings) - len(mine)} not the agent's (left alone)" if len(listings) != len(mine) else None,
                "shape": "listingId,sku,price,images"}

    async def listing_states(self, keys: list[dict], control: dict | None = None) -> dict:
        """The listings the bulk read lacked, each by its own data (one read for all): live | sold | gone."""
        ids = [str(k.get("key")) for k in keys if k.get("key")]
        try:
            found = await self.client.read(ids)
        except api.EbayError as e:
            return {"error": str(e)[:200]}
        states = {}
        for lid in ids:
            live = found.get(lid)
            if live is None:
                states[lid] = {"state": "gone", "http": 404, "how": "eBay's Listing API"}
            elif not self.ours(live):
                states[lid] = {"state": "unknown", "how": "not the agent's listing"}
            else:
                st = self._state(live)
                how = "eBay's Listing API" + (f": {live.hold}" if st == "gone" and live.hold else
                                               f": ended ({live.end_reason})" if st == "gone" else "")
                states[lid] = {"state": st, "how": how, "photos": len(live.photos), "price": live.price,
                               "http": 404 if st == "gone" else 200}
        return {"states": states, "trusted": True}

    async def read_photos(self, url: str, shots: Path | None = None, sku: str | None = None) -> dict:
        live = await self._read(url, sku)
        return {"count": len(live.photos) if live is not None else None}

    # ------------------------------------------------------------ the price

    async def read_price(self, ctx, url: str) -> dict:
        live = await self._read(url)
        if live is None:
            return {"error": "eBay has no such listing"}
        return {"price": live.price, "unavailable": None if live.for_sale else self._state(live)}

    async def set_price(self, ctx, url: str, old: float, new: float, dry: bool = False, shots: Path | None = None,
                        go=None, fields: dict | None = None) -> dict:
        """The listing's price to `new` — only when eBay shows `old` (else its own price is reported: read again),
        after the go-ahead (a sale that arrived meanwhile stops it), then read back."""
        sku = (fields or {}).get("sku")
        live = await self._read(url, sku)
        if live is None or not live.for_sale:
            return {"ok": False, "saved": False, "error": "not for sale on eBay"}
        if live.price is None or abs(float(live.price) - float(old)) >= 0.005:
            return {"ok": False, "saved": False, "mismatch": live.price}
        if dry:
            return {"ok": True, "saved": False}
        if go is not None and not go():
            return {"ok": False, "saved": False, "error": "stopped before the change"}
        await self.client.set_price(live.listing_id, float(new))
        after = await self.client.read_one(live.listing_id)
        shown = after.price if after is not None else None
        return {"ok": shown is not None and abs(float(shown) - float(new)) < 0.005, "saved": True, "live_price": shown}

    # ------------------------------------------------------------ changes to a live listing

    async def set_text(self, url: str, payload: dict, dry: bool = False, shots: Path | None = None, go=None,
                       sku: str | None = None, fields: dict | None = None) -> dict:
        """The title and/or the description — never over a text she changed by hand (`expect_*`: what we posted)."""
        live = await self._read(url, sku)
        if live is None or not live.for_sale:
            return {"ok": False, "saved": False, "error": "not for sale on eBay"}
        if payload.get("expect_title") is not None and not _same(live.title, payload["expect_title"]) \
                and not _same(live.title, payload.get("title")):
            return {"ok": False, "saved": False, "mismatch": live.title}
        if payload.get("description") is not None and payload.get("expect_description") is not None and \
                not _same(plain_text(live.description), payload["expect_description"]) and \
                not _same(plain_text(live.description), payload["description"]):
            return {"ok": False, "saved": False, "mismatch": "the description"}
        if dry:
            await self.client.update(live.listing_id, title=payload.get("title"),
                                     description=payload.get("description"), validate=True)
            return {"ok": True, "saved": False, "seen": live.title}
        if go is not None and not go():
            return {"ok": False, "saved": False, "error": "stopped before the change"}
        after = await self.client.update(live.listing_id, title=payload.get("title"),
                                         description=payload.get("description"))
        shows = after is not None and (payload.get("title") is None or _same(after.title, payload["title"])) and \
            (payload.get("description") is None or _same(plain_text(after.description), payload["description"]))
        return {"ok": bool(shows), "saved": True}

    async def read_text(self, url: str, shots: Path | None = None, sku: str | None = None) -> dict:
        live = await self._read(url, sku)
        return {"title": live.title, "description": plain_text(live.description)} if live is not None else {}

    async def add_photos(self, url: str, files: list[str], dry: bool = False, shots: Path | None = None, go=None,
                         sku: str | None = None, had: int = 0) -> dict:
        """The listing's missing photos added after the ones it shows (its own photos are kept, in their order)."""
        live = await self._read(url, sku)
        if live is None or not live.for_sale:
            return {"ok": False, "saved": False, "error": "not for sale on eBay"}
        room = max(0, mapping.PHOTOS_MAX - len(live.photos))
        files = list(files)[:room]
        if not files:
            return {"ok": True, "saved": True, "already": True, "after": len(live.photos)}
        if dry:
            return {"ok": True, "saved": False, "seen": f"{len(live.photos)} photos"}
        urls = [await self.client.upload_image(p) for p in files]
        if go is not None and not go():
            return {"ok": False, "saved": False, "error": "stopped before the change"}
        after = await self.client.update(live.listing_id, image_urls=[*live.photos, *urls])
        n = len(after.photos) if after is not None else None
        return {"ok": n is not None and n >= len(live.photos) + len(urls), "saved": True, "after": n}

    async def set_fields(self, url: str, fields: dict, dry: bool = False, shots: Path | None = None, go=None,
                         sku: str | None = None) -> dict:
        """A brand correction (`fields.brand`, with our names for it): the listing's Brand item specific, nothing
        else. eBay takes the brand as text; its own spelling is taken when its list has one."""
        live = await self._read(url, sku)
        if live is None or not live.for_sale:
            return {"ok": False, "saved": False, "error": "not for sale on eBay"}
        brand = str(fields.get("brand") or "").strip()
        if fields.get("size") and not brand:          # WO58: her size tapped after the agent's went up
            return await self._set_size(live, fields, dry, go)
        if not brand:
            return {"ok": False, "saved": False, "error": "only the brand or the size of an eBay listing is changed "
                                                          "this way"}
        aspects = {**live.aspects, "Brand": [brand]}
        if dry:
            await self.client.update(live.listing_id, aspects=aspects, validate=True)
            return {"ok": True, "saved": False, "seen": (live.aspects.get("Brand") or [None])[0]}
        if go is not None and not go():
            return {"ok": False, "saved": False, "error": "stopped before the change"}
        after = await self.client.update(live.listing_id, aspects=aspects)
        return {"ok": after is not None and (after.aspects.get("Brand") or [None])[0] == brand, "saved": True}

    async def _set_size(self, live, fields: dict, dry: bool, go) -> dict:
        """The listing's Size item specific as her size, in eBay's own spelling for its category (`size_value`; the
        list's own value nearest it, else as written)."""
        want = str(fields.get("size") or "").strip()
        name = next((a for a in live.aspects if a.strip().lower() in mapping.SIZE_ASPECTS), "Size")
        value = want
        if fields.get("category_id"):
            try:
                meta = mapping.parse_aspects(await self.client.aspects_raw(str(fields["category_id"])))
                a = next((a for a in meta if a.name == name), None)
                if a is not None and a.values:
                    chosen = {k: v for k, v in live.aspects.items() if k != name}
                    found, _ = mapping.size_value(want, a.allowed(chosen), fields.get("item_type"))
                    value = found or fallback.last_choice(a.allowed(chosen)) or want
            except Exception:  # noqa: BLE001 — eBay's list can't be read: her size as written
                value = want
        aspects = {**live.aspects, name: [value]}
        if dry:
            await self.client.update(live.listing_id, aspects=aspects, validate=True)
            return {"ok": True, "saved": False, "seen": (live.aspects.get(name) or [None])[0]}
        if go is not None and not go():
            return {"ok": False, "saved": False, "error": "stopped before the change"}
        after = await self.client.update(live.listing_id, aspects=aspects)
        return {"ok": after is not None and (after.aspects.get(name) or [None])[0] == value, "saved": True,
                "note": f"size {value}" + (f" (her size {want} in eBay's list)" if value != want else "")}

    async def read_fields(self, url: str, fields: dict, shots: Path | None = None, sku: str | None = None) -> dict:
        live = await self._read(url, sku)
        brand = str(fields.get("brand") or "").strip()
        if fields.get("size") and not brand and live is not None:
            size = next((v[0] for a, v in live.aspects.items() if a.strip().lower() in mapping.SIZE_ASPECTS and v), None)
            return {"ok": size is not None and (size == fields["size"]
                                                or size in mapping.size_candidates(str(fields["size"])))}
        return {"ok": live is not None and (not brand or (live.aspects.get("Brand") or [None])[0] == brand)}

    # ------------------------------------------------------------ a take-down (thrift-api does these; here for the CLI)

    async def delist(self, url: str) -> bool | None | str:
        """End OUR listing: True (ended) | None (gone already) | "sold" (eBay shows it sold: nothing ended)."""
        live = await self._read(url)
        if live is None:
            return None
        if live.was_sold:
            return "sold"
        if not live.for_sale:
            return None
        ended = await self.client.end(live.listing_id)
        return bool(ended is not None and ended.status == "ENDED")

    async def check_login(self) -> str:
        return "connected" if self.available() else f"not connected ({self.why_unavailable()})"
