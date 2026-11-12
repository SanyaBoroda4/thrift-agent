"""`thrift ebay …` (WO57): the fourth site's own commands. Everything here is an API call or a look at our own
records — no browser. `status` / `price` / `plan` / `validate` change nothing on eBay; `first` makes ONE real listing
(the owner's go-live step, WO57 §7); `spike` runs the whole round on the environment thrift-api is set to (the sandbox,
the day the keys arrive); `backfill` plans the items already live elsewhere (the running poster lets 25 a day in);
`record` keeps eBay's own category answers for the tests."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import typer
from rich import print
from rich.markup import escape
from rich.table import Table

app = typer.Typer(help="eBay, the fourth site (WO57): status, the price formula, a plan, the backfill.",
                  no_args_is_help=True)


def _s():
    from thrift_agent.config import settings
    return settings()


def _db():
    from thrift_agent.db import DB
    s = _s()
    return DB(s.path("db"))


def _poster():
    from thrift_agent import brands
    from thrift_agent.ebay.poster import EbayPoster
    s = _s()
    return EbayPoster(s, brands.for_settings(s))


def _view(db, item_id: str):
    from thrift_agent.catalogs.common import ItemView
    from thrift_agent.ebay import backfill
    it = db.item(item_id)
    if it is None:
        raise typer.BadParameter(f"unknown item {item_id}")
    view = ItemView.from_row(it)
    if (base := backfill.base(db, item_id)) is not None:
        view.price = base
    return view


@app.command("status")
def status() -> None:
    """Is eBay on, ready and connected; the fee table's date; the month's free listings; the backfill."""
    from thrift_agent import crosslist, ebay, sales
    from thrift_agent.ebay import backfill, duties, fees
    s, db = _s(), _db()
    on = bool(s.get("marketplaces.ebay.enabled", False))
    print(f"eBay: {'ON' if on else 'off'} (marketplaces.ebay.enabled) · publishes for real: {crosslist.live(s, 'ebay')}"
          f" · environment {s.get('marketplaces.ebay.environment', 'production')}")
    print(f"ready: {ebay.ready(s) or 'yes'}")
    api = sales.api()
    if api is None:
        print("thrift-api: not set up on this machine (THRIFT_API_URL / THRIFT_API_KEY) — no eBay token can be asked for")
    else:
        try:
            st = api.get("/ebay/status")
            print("thrift-api: " + ", ".join(f"{k} {v}" for k, v in st.items() if v not in (None, "")))
        except sales.ApiError as e:
            print(f"thrift-api: /ebay/status didn't answer ({e})")
    table = fees.load()
    print(f"fee table checked {table.checked} ({fees.age_days(table)} days ago) · about {duties.free_used(s, db)} of "
          f"{table.insertion.free_per_month} free listings used this month")
    st = backfill.status(db)
    print(f"backfill: {st['planned']} planned, {st['queued']} queued, {st['live']} live, {st['waiting']} waiting, "
          f"{st['left']} left out ({backfill.per_day(s)} a day, since {st.get('since') or backfill.since_of(s)})")


@app.command("price")
def price(bases: list[int] = typer.Argument(None, help="her prices (the price she taps), e.g. 10 20 50"),
          shipping: float = typer.Option(6.0, "--shipping", help="what the buyer pays for shipping"),
          tax: float = typer.Option(None, "--tax", help="the average sales-tax rate (default: the setting)"),
          category: list[str] = typer.Option(None, "--category", help="eBay category ids of the listing's path "
                                                                      "(its fee rule), e.g. --category 169291"),
          items: int = typer.Option(0, "--items", help="instead: this many of the newest live items, each at its own "
                                                       "price, family parcel and (when it has an eBay row) category")
          ) -> None:
    """The eBay price for her price, with the profit on Poshmark and on eBay side by side (WO57 §3). Nothing is sent."""
    from thrift_agent import families
    from thrift_agent.catalogs import common
    from thrift_agent.ebay import fees, prices
    from thrift_agent.ebay import shipping as parcels
    s = _s()
    rate = float(tax if tax is not None else s.get("marketplaces.ebay.tax_rate", fees.DEFAULT_TAX))
    t = Table("item", "her price", "Poshmark leaves", "eBay price", "eBay leaves", "difference", "ships", "fee rule")
    rows = []
    if items:
        db = _db()
        for r in db.conn.execute("SELECT i.id FROM items i WHERE i.status='posted' AND COALESCE(i.test, 0)=0 AND "
                                 "i.owner_price IS NOT NULL ORDER BY i.created_at DESC LIMIT ?", (items,)).fetchall():
            try:
                view = _view(db, r["id"])
            except Exception as e:  # noqa: BLE001 — an item that can't be read is said, the table goes on
                print(f"[yellow]{r['id']}[/]: {escape(str(e))}")
                continue
            row = db.listing(r["id"], "ebay")
            cats = prices.fields_of(row).get("category_ids") if row is not None else None
            plan = parcels.plan(families.family_of_type(view.facts.item_type), common.package_class(view),
                                view.render.department, kind=str(s.get("marketplaces.ebay.shipping.type", "calculated")),
                                flat=s.get("marketplaces.ebay.shipping.flat"))
            rows.append((view.render.title[:44], view.price, cats or (), plan.buyer_pays))
    for b in bases or ([] if items else [10, 20, 50]):
        rows.append((f"${b}", b, tuple(category or ()), shipping))
    for name, base, cats, ship in rows:
        q = fees.quote(base, cats, ship, rate)
        t.add_row(escape(name), f"${base}", f"${q.poshmark_payout:.2f}", f"${q.price}", f"${q.ebay_payout:.2f}",
                  f"{q.difference:+.2f}", f"${ship:.2f}", q.rule)
    print(t)
    print(f"tax rate {rate:.0%}; fee table checked {fees.load().checked}")


@app.command("plan")
def plan(item_id: str) -> None:
    """The item as an eBay listing — eBay's own category for it, its item specifics, the price — printed. Nothing is
    created, nothing is uploaded (it reads eBay's category data: a token is needed)."""
    db, poster = _db(), _poster()
    f = asyncio.run(poster.plan(_view(db, item_id)))
    print(escape(json.dumps(f.model_dump(), indent=1, ensure_ascii=False)))


@app.command("validate")
def validate(item_id: str) -> None:
    """eBay's own check of the item's listing (its VALIDATE mode): the cover goes up to eBay's picture service, the
    listing is checked, NOTHING is created."""
    from thrift_agent.schema import Render
    s, db, poster = _s(), _db(), _poster()

    async def go():
        view = _view(db, item_id)
        poster.fields = await poster.plan(view)
        r = Render(marketplace="ebay", title=poster.fields.title, description=poster.fields.description, tags=[],
                   brand=view.render.brand, department=view.render.department, category=poster.fields.category,
                   subcategory=None, size=poster.fields.size, colors=poster.fields.colors,
                   condition=view.render.condition, price=poster.fields.price, photos=poster.fields.photos, sku=item_id)
        return await poster.post(None, r, "publish", True, s.path("failed") / "shots")
    out = asyncio.run(go())
    print(f"{out.status}: {escape(out.note or out.error or '')}")
    if out.guesses:
        print("checks: " + escape("; ".join(out.guesses)))
    raise typer.Exit(0 if out.status == "dryrun" else 1)


@app.command("first")
def first(item_id: str, yes: bool = typer.Option(False, "--yes", help="really create the listing")) -> None:
    """WO57 §7 step 3: ONE real eBay listing — validated, created, read back by API — and its link to the private
    chat. On the Mac only; the item must be live on another site and have no eBay row that reached eBay. Without
    --yes it only validates."""
    from thrift_agent import crosslist, notify
    from thrift_agent.post import runner
    s, db = _s(), _db()
    if not s.is_prod:
        print("[red]the first real listing is made on the Mac only[/] (machine_role: prod)")
        raise typer.Exit(1)
    row = db.listing(item_id, "ebay")
    if row is not None and (row["status"] in ("posted", "posting") or row["url"]):
        print(f"[red]it reached eBay already[/]: {row['status']} {row['url'] or ''}")
        raise typer.Exit(1)
    if not crosslist.live_elsewhere(db, item_id, "ebay"):
        print("[red]the item isn't live on another site[/]")
        raise typer.Exit(1)
    ps = {"ebay": _poster()}
    if row is None:
        crosslist.queue(s, db, item_id, ["ebay"], why="the first eBay listing")
    elif row["status"] in ("failed", "skipped", "dryrun"):
        db.upsert_listing(item_id, "ebay", status="queued")
    out = asyncio.run(runner.run_cross(s, db, ps, None, item_id, "ebay", dry=not yes))
    row = db.listing(item_id, "ebay")
    print(f"{out.status if out is not None else 'not run'}: {escape((out.note or out.error or '') if out else '')}")
    print(f"row: {row['status']} {row['url'] or ''} ${row['price']}")
    if out is not None and out.status == "posted":
        crosslist.announce(s, db, item_id)
        notify.say(f"🔵 The first eBay listing is live: {crosslist.item_name(db, item_id)} — ${row['price']} — "
                   f"{row['url']}\nRead back by API: {out.note or 'it shows what was sent'}.")
    raise typer.Exit(0 if out is not None and out.status in ("posted", "dryrun") else 1)


@app.command("hers")
def hers() -> None:
    """Read-only: how the owner's OWN hand-made eBay listings are set — shipping type and service, handling time,
    returns, location, Best Offer — so the agent's listings mirror them (WO57 §4). Nothing is changed; her listings
    are never touched."""
    poster = _poster()

    async def go():
        listings, complete = await poster.client.active()
        return [x for x in listings if not poster.ours(x)], complete
    mine, complete = asyncio.run(go())
    t = Table("title", "price", "shipping", "service", "cost", "handling", "returns", "ZIP", "best offer")
    for x in mine:
        t.add_row(escape((x.title or "")[:40]), f"${x.price:g}" if x.price is not None else "", x.shipping_type or "",
                  x.shipping_service or "", f"${x.shipping_cost:g}" if x.shipping_cost is not None else "",
                  x.handling or "", f"{x.returns_accepted} {x.returns_days or ''} {x.returns_payer or ''}".strip(),
                  x.postal_code or "", str(x.best_offer))
    print(t)
    kinds = sorted({x.shipping_type for x in mine if x.shipping_type})
    print(f"{len(mine)} listings of hers" + ("" if complete else " (not read to the end)")
          + (f" · shipping type{'s' if len(kinds) != 1 else ''}: {', '.join(kinds)}" if kinds else ""))


@app.command("backfill")
def backfill_cmd(since: str = typer.Option(None, "--since", help="items first posted on or after this day (YYYY-MM-DD)"),
                 dry_run: bool = typer.Option(False, "--dry-run", help="only say what would be planned"),
                 show: bool = typer.Option(False, "--status", help="where the backfill stands"),
                 only: str = typer.Option(None, "--items", help="just these items, comma-separated, in this order"),
                 not_before: str = typer.Option(None, "--not-before",
                                                help="let nothing in before this local time, e.g. '2026-11-12 14:00'"),
                 now: int = typer.Option(None, "--now", help="WO64: N more planned items today, from now (today's cap "
                                                             "raised to midnight; tomorrow from 2:00 PM, 25 a day)"),
                 go_on: bool = typer.Option(False, "--resume", help="WO64: go on after eBay's refusals stopped it")
                 ) -> None:
    """Every item posted since the day that is still live on at least one site and not sold goes to eBay too — planned
    here, let into eBay's queue by the running poster 25 a day, oldest first, each at its price that day. The
    price-drop clocks aren't reset. New shares always go first. `--items`: only those (the first five);
    `--not-before`: the poster starts on the plan only from then."""
    from zoneinfo import ZoneInfo

    from thrift_agent import crosslist
    from thrift_agent.ebay import backfill
    s, db = _s(), _db()
    if show:
        print(backfill.status(db))
        return
    if go_on:
        print("resumed: the backfill goes on" if backfill.resume(db) else "it wasn't stopped")
        return
    if now is not None:
        try:
            b = backfill.boost(s, db, now)
        except ValueError as e:
            print(f"[red]not started[/] — {escape(str(e))}")
            raise typer.Exit(1) from None
        st = backfill.status(db)
        print(f"from now: {b['n']} more planned items today (the oldest first; today's limit {b['limit']}, today's eBay "
              f"cap {b['cap']} until midnight); tomorrow nothing before {b['then']} (2:00 PM), then "
              f"{backfill.per_day(s)} a day — {st['waiting']} waiting")
        return
    start = None
    if not_before:
        try:
            start = datetime.fromisoformat(not_before.strip())
        except ValueError:
            raise typer.BadParameter(f"--not-before {not_before!r}: give 'YYYY-MM-DD HH:MM'") from None
        if start.tzinfo is None:
            start = start.replace(tzinfo=ZoneInfo(str(s.get("schedule.timezone") or "America/New_York")))
    items = [i.strip() for i in only.split(",") if i.strip()] if only else None
    try:
        todo = backfill.plan(s, db, since, dry=dry_run, only=items, not_before=start)
    except ValueError as e:
        print(f"[red]not planned[/] — {escape(str(e))}")
        raise typer.Exit(1) from None
    if start is not None:
        print(f"{'would start' if dry_run else 'starts'}: {start.isoformat(timespec='minutes')} (nothing is let in before)")
    days = -(-len(todo) // max(1, backfill.per_day(s)))
    print(f"{'would plan' if dry_run else 'planned'}: {len(todo)} items still to go to eBay — {backfill.per_day(s)} a "
          f"day, about {days} day{'s' if days != 1 else ''}, the oldest first")
    for iid in todo[:60]:
        print(f"  {iid} {escape(crosslist.item_name(db, iid))}")
    if len(todo) > 60:
        print(f"  … and {len(todo) - 60} more")


@app.command("record")
def record(out: Path = typer.Option(Path("tests/fixtures/ebay"), "--out", help="where eBay's own answers are kept"),
           items: int = typer.Option(20, "--items", help="how many of the newest live items to plan"),
           private: Path = typer.Option(Path("private/tests/ebay/items.json"), "--private",
                                        help="where the items themselves go (the seller's data: never public)")) -> None:
    """Record eBay's own category answers (suggestions, item specifics, conditions) for the newest live items — the
    tests then check the mapping against the real thing instead of the stand-in. Reads only; nothing is created."""
    from thrift_agent.db import loads
    from thrift_agent.ebay import client as api
    db = _db()
    out.mkdir(parents=True, exist_ok=True)
    poster = _poster()
    poster.client = api.EbayClient(poster.client.tokens, poster.client.environment, cache_dir=out, cache_days=0)
    kept = []

    async def go():
        for r in db.conn.execute("SELECT id FROM items WHERE status='posted' AND COALESCE(test, 0)=0 AND owner_price "
                                 "IS NOT NULL ORDER BY created_at DESC LIMIT ?", (items,)).fetchall():
            it = db.item(r["id"])
            try:
                f = await poster.plan(_view(db, r["id"]))
            except Exception as e:  # noqa: BLE001 — said; the others go on
                print(f"[yellow]{r['id']}[/]: {escape(f'{type(e).__name__}: {e}')}")
                continue
            kept.append({"id": r["id"], "facts": loads(it["facts"]), "render": (loads(it["renders"]) or {})["poshmark"],
                         "price": int(it["owner_price"]), "ebay": {"category": f.category, "aspects": f.aspects,
                                                                   "condition": f.condition, "price": f.price}})
            print(f"{r['id']}: {escape(f.category)} · ${f.price} · {len(f.aspects)} item specifics")
    asyncio.run(go())
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_text(json.dumps(kept, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(kept)} items → {private} (private); eBay's answers → {out} ({len(list(out.glob('*.json')))} files)")


@app.command("spike")
def spike(item_id: str = typer.Argument(..., help="an item of ours to list (on the sandbox: it is no real listing)"),
          keep: bool = typer.Option(False, "--keep", help="don't end the listing at the end")) -> None:
    """WO57 §1/§7: the whole round by API on the environment thrift-api is set to — validate, create, read back, change
    the price, change the title, the text and the photos, end — with what each call answered. Refused on production
    (there the one real listing is `thrift ebay first`)."""
    from thrift_agent.ebay import client as api
    s, db, poster = _s(), _db(), _poster()
    c = poster.client
    if c.environment != "sandbox":
        print("[red]the spike runs on the sandbox only[/] (marketplaces.ebay.environment: sandbox, and thrift-api's "
              "EBAY_ENV=sandbox)")
        raise typer.Exit(1)
    report: list[str] = []

    def said(step: str, ok: bool, detail: str = "") -> None:
        report.append(f"{'✓' if ok else '✗'} {step}" + (f": {detail}" if detail else ""))
        print(report[-1])

    async def go() -> None:
        f = await poster.plan(_view(db, item_id))
        said("plan (Taxonomy + Metadata, an application token)", True, f"{f.category} · ${f.price}")
        urls = [await c.upload_image(p) for p in f.photos[:3]]
        said("photos (Media createImageFromFile)", bool(urls), f"{len(urls)} up")
        key = api.idempotency_key(f"{item_id}-spike-{datetime.now():%H%M%S}")
        checked = await c.create(f, urls, key, validate=True)
        said("createListing VALIDATE", checked.validated, "; ".join(checked.warnings) or "no warnings")
        made = await c.create(f, urls, key)
        said("createListing", bool(made.listing_id), f"{made.listing_id} · fees {made.fees}")
        again = await c.create(f, urls, key)
        said("the same call again gives the same listing (idempotency key)", again.listing_id == made.listing_id)
        live = await c.read_one(made.listing_id)
        said("read back (sellerListings)", live is not None and live.for_sale, f"{live.title} · ${live.price} · "
             f"{len(live.photos)} photos · {live.shipping_type} {live.shipping_service} · returns {live.returns_days} "
             f"{live.returns_payer} · {live.postal_code}" if live else "nothing")
        found = await c.by_sku(f.sku)
        said("found by SKU (sellerListingSearch)", any(x.listing_id == made.listing_id for x in found))
        await c.set_price(made.listing_id, f.price + 3)
        said("price change (bulkUpdateInventory)", (await c.read_one(made.listing_id)).price == f.price + 3)
        new = await c.update(made.listing_id, title=(f.title[:70] + " (edit)"), description=f.description + "\n\nEdited.",
                             image_urls=list(reversed(urls)))
        said("title, text and photos changed (updateListing)", new is not None and new.title.endswith("(edit)")
             and new.photos[:1] != urls[:1] if len(urls) > 1 else new is not None)
        if not keep:
            ended = await c.end(made.listing_id)
            said("endListing", ended is not None and ended.status == "ENDED")
        said("calls made", True, json.dumps(c.calls))
        print(f"\nlisting: {c.item_url(made.listing_id)}")
    try:
        asyncio.run(go())
    except api.EbayError as e:
        said(f"stopped ({e.kind})", False, str(e))
    shots = s.path("failed") / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    path = shots / f"ebay-spike-{datetime.now():%Y%m%d-%H%M%S}.txt"
    path.write_text("\n".join(report), encoding="utf-8")
    print(f"kept: {path}")
