"""`thrift` command. Dev on Windows: init, process, confirm, eval, status. Prod on the Mac: run, poster."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import traceback
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich import print
from rich.markup import escape
from rich.table import Table

from thrift_agent import alerts, approve, config, notify, pipeline, runlock
from thrift_agent.config import settings
from thrift_agent.db import DB, loads

app = typer.Typer(no_args_is_help=True, add_completion=False)
telegram_app = typer.Typer(help="Telegram bot helpers: setup (find the ids for .env) and test (send a message).")
app.add_typer(telegram_app, name="telegram")


@app.callback()
def _startup() -> None:
    """Load .env once, before any command runs. Commands that never call settings() (telegram setup) used to read
    os.environ cold and report the token missing although .env had it. Exported variables still win (no override)."""
    load_dotenv(config.ENV_FILE)
RESEND_CHECK_SECONDS = 3600      # how often the worker looks for batches/items waiting longer than resend_after_hours
WORKER_LOCK = "worker.lock"      # next to the DB: one `thrift run` per machine (runlock)
SCAN_KEY = alerts.SCAN_DONE      # kv: when the worker last finished looking at the inbox (deploy checks it moves)


def _db() -> DB:
    return DB(settings().path("db"))


@app.command()
def init() -> None:
    """Create folders and the state DB."""
    s = settings()
    s.ensure_dirs()
    _db()
    print(f"[green]ok[/] role={s['machine_role']} inbox={s.path('inbox')}")


def _tick(s, db) -> int:
    """Register what the iPhone shared, then process every new batch and item in the owner's queue order (WO20), one
    at a time and looking again after each: the card the owner needs next is ready first, and an item sent back for
    reprocessing by an answer jumps ahead of the rest. Each is taken once per tick."""
    n = _scan_inbox(s, db)
    done: set[tuple[str, str]] = set()
    while job := next((j for j in approve.processing_order(db) if j not in done), None):
        done.add(job)
        kind, ref = job
        if kind == approve.NEW_BATCH:
            _guard(db, ref, lambda: pipeline.process_batch(s, db, ref), lambda: db.set_batch(ref, status="failed"))
        else:
            _guard(db, ref, lambda: pipeline.process_item(s, db, ref), lambda: db.set_item(ref, status="failed"))
        n += 1
    return n


def _scan_inbox(s, db) -> int:
    """Register what the iPhone shared. A read of the iCloud inbox that macOS refuses or interrupts (python3.14 still
    waiting for its iCloud Drive permission) is not an error to report each time (WO22): it is retried quietly on the
    next tick, and the owner gets one message if it lasts (alerts.inbox_trouble). The start and the end of each look
    are recorded: the Telegram thread notices a look stuck on the permission prompt, deploy that the worker got
    through one. Processing of the items already split goes on either way."""
    alerts.scan_started(db)
    try:
        n = sum(1 for folder in pipeline.ready_folders(s) if pipeline.register(s, db, folder))
    except OSError as e:
        if not alerts.inbox_unreadable(e):
            raise
        alerts.inbox_trouble(db, f"{type(e).__name__}: {e}")
        return 0
    alerts.scan_done(db)
    alerts.inbox_ok(db)
    return n


def _safe_tick(s, db) -> int:
    """One worker iteration that never kills the service: an error is logged, retried on the next tick, and told
    once (then at most once a day while it keeps happening, WO22)."""
    try:
        return _tick(s, db)
    except Exception as e:  # noqa: BLE001
        db.log(None, "error", traceback.format_exc())
        alerts.once(db, f"❌ worker tick: {type(e).__name__}: {e}")
        return 0


def _guard(db, ref, fn, on_fail) -> None:
    """One batch or item: a failure marks it failed and is told once a day per identical error, whichever item it
    hits (ten items failing on one bad API key are one message; thrift status lists them all)."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        on_fail()
        db.log(ref, "error", traceback.format_exc())
        alerts.once(db, f"❌ {ref}: {type(e).__name__}: {e}")


def _tick_unless_worker(s, db) -> None:
    """Dev: process the new rows right now. Prod: leave them to the `thrift run` worker — a second process
    ticking the same DB can pick the same 'new' row (double LLM spend, double pings, last writer wins)."""
    if s.is_prod:
        print("queued — the worker will pick it up within 15 s")
    else:
        _tick(s, db)


def _safe_poll(s, db, bot, timeout: int) -> int:
    """One Telegram long poll that never kills the worker (network blips, an API hiccup): log and go on."""
    try:
        return approve.poll_once(s, db, bot, timeout)
    except Exception as e:  # noqa: BLE001
        db.log(None, "error", traceback.format_exc())
        print(f"[telegram] poll failed: {type(e).__name__}: {e}")
        time.sleep(min(timeout, 5))
        return 0


def _safe_pump(s, db) -> None:
    """Send the next question if none is open; an error (Telegram down) is logged and retried on the next turn."""
    try:
        approve.pump(s, db)
    except Exception as e:  # noqa: BLE001
        db.log(None, "error", traceback.format_exc())
        print(f"[telegram] send failed: {type(e).__name__}: {e}")


def _worker_iteration(s, db, interval: int) -> None:
    """One turn of the worker's main thread: process the inbox in the queue's order, send the next question if none
    is open, sleep. Telegram's side (replies, buttons, the next question after an answer) runs on its own thread, so
    the owner is answered at once while items are still being processed."""
    _safe_tick(s, db)
    _safe_pump(s, db)
    time.sleep(interval)


def _telegram_iteration(s, db, bot, state: dict) -> None:
    """One turn of the Telegram thread: long-poll for the owner's replies and button presses (each answer sends the
    next question), make sure one question is out, and every RESEND_CHECK_SECONDS re-send the open one if it has
    waited longer than telegram.resend_after_hours."""
    _safe_poll(s, db, bot, int(s.get("telegram.poll_timeout", 25)))
    _safe_pump(s, db)
    if started := alerts.scan_stuck(db):                   # the main thread waits on macOS's permission prompt
        alerts.inbox_trouble(db, f"inbox scan stuck since {started}", since=started)
    if time.monotonic() - state.get("last_resend", 0) >= RESEND_CHECK_SECONDS:
        state["last_resend"] = time.monotonic()
        try:
            approve.resend_pending(s, db)
        except Exception as e:  # noqa: BLE001
            db.log(None, "error", traceback.format_exc())
            print(f"[telegram] resend failed: {type(e).__name__}: {e}")


def _telegram_loop(s, bot) -> None:
    """The Telegram thread, on its own DB connection (a sqlite3 connection belongs to one thread; WAL lets both
    write)."""
    db = _db()
    state = {"last_resend": time.monotonic()}
    while True:
        _telegram_iteration(s, db, bot, state)


@app.command()
def run(interval: int = 15) -> None:
    """Watch the inbox, process batches/items and talk to the owner on Telegram, forever (the worker service)."""
    s = settings()
    try:
        lock = runlock.hold(s.path("db").parent / WORKER_LOCK)   # one worker: two would split the Telegram updates
    except runlock.AlreadyRunning as e:
        print(f"[red]not started[/]: {escape(str(e))}")
        raise typer.Exit(1) from None
    db = _db()
    s.ensure_dirs()
    if s.is_prod:
        notify.check(s)                                   # a silent Telegram is not an option on the Mac
    bot = approve.bot_for(s)
    print(f"worker watching {s.path('inbox')}" + (" — Telegram on" if bot else " — Telegram off (dev: messages print)"))
    telegram = None
    if bot:
        approve.resend_pending(s, db)                    # the open question only if it is old: never at each deploy
        telegram = threading.Thread(target=_telegram_loop, args=(s, bot), name="telegram", daemon=True)
        telegram.start()
    while lock:                                           # the lock is held for as long as this loop runs
        if telegram is not None and not telegram.is_alive():
            raise SystemExit("the Telegram thread stopped — exiting so launchd restarts the worker")
        _worker_iteration(s, db, interval)


@app.command()
def process(folder: Path) -> None:
    """One-off: treat FOLDER as a shared batch (dev: point it at any folder of photos)."""
    s, db = settings(), _db()
    s.ensure_dirs()
    bid = pipeline.register(s, db, folder.resolve())
    if bid is None:                                   # already registered — or nothing to register
        row = db.conn.execute("SELECT id FROM batches WHERE src_dir=?", (str(folder.resolve()),)).fetchone()
        if row is None:
            raise typer.BadParameter(f"no photos found in {folder}")
        bid = row[0]
    pipeline.process_batch(s, db, bid)
    b = db.batch(bid)
    print(f"batch {bid}: {b['status']}  sheet: {s.path('work') / bid / 'contact_sheet.png'}")
    if b["status"] == "split":
        _tick_unless_worker(s, db)


@app.command()
def confirm(batch_id: str, cmd: str = typer.Argument("ok")) -> None:
    """Accept or correct a batch split: ok | 12>2 | split 7 | merge 2 3 | drop 7"""
    s, db = settings(), _db()
    pipeline.confirm(s, db, batch_id, cmd)
    print(f"[green]split[/] {batch_id}")
    for kind in ("batch", "regroup"):                             # the Telegram copy of this question is answered
        db.outbox_resolve(kind, batch_id)
    approve.announce(s, f"batch {batch_id}: confirmed from the CLI ({cmd})")
    _tick_unless_worker(s, db)
    approve.pump(s, db)                                           # the next question, if one is ready


@app.command()
def answer(item_id: str, note: str) -> None:
    """Answer a needs-info / needs-owner item, e.g.  thrift answer i_... "size 8, brand Vince" """
    s, db = settings(), _db()
    outcome = pipeline.answer(s, db, item_id, note)
    for kind in ("item", "owner_q"):
        db.outbox_resolve(kind, item_id)
    approve.announce(s, f"{item_id}: answered from the CLI: {note!r} -> {outcome}")
    _tick_unless_worker(s, db)
    approve.pump(s, db)


@app.command()
def price(item_id: str, amount: int) -> None:
    """Approve or set the owner's price for an item (the CLI twin of the Telegram Approve/Change buttons)."""
    s, db = settings(), _db()
    status = pipeline.set_price(s, db, item_id, amount)
    print(f"[green]${amount}[/] set for {item_id} — status {status}")
    if status != "awaiting_price":                                 # a held re-share keeps its message pending
        db.outbox_resolve("item", item_id)
    approve.announce(s, f"{item_id}: price ${amount} set from the CLI -> {status}")
    approve.pump(s, db)                                            # the next card (one at a time)


@app.command()
def condition(item_id: str, choice: str = typer.Argument(..., metavar="nwt|like_new|good")) -> None:
    """Answer "Brand new or worn?" for a pair of shoes (the CLI twin of the [NWT] [Like New] [Good] buttons): nwt =
    new with tags, like_new = brand new without tags, good = worn. The item is reprocessed with that condition and
    the price card follows."""
    s, db = settings(), _db()
    try:
        pipeline.set_condition(s, db, item_id, choice)
    except ValueError as e:
        print(f"[red]not set[/] {item_id}: {e}")
        raise typer.Exit(1) from None
    db.outbox_resolve("condition", item_id)                          # the pending question is settled
    label = approve.CONDITION_TAPPED[pipeline.owner_choice(choice)]
    print(f"[green]{label}[/] set for {item_id} — reprocessing, the price card follows")
    approve.announce(s, f"{item_id}: condition {label} set from the CLI - repricing")
    _tick_unless_worker(s, db)
    approve.pump(s, db)


@app.command()
def kids(item_id: str, choice: str = typer.Argument(..., metavar="girls|boys")) -> None:
    """Answer "Girls or Boys?" for a kids item (the CLI twin of the [Girls] [Boys] buttons): which of Poshmark's
    size lists its size is picked from. No reprocessing; the price card follows if it hasn't been priced yet."""
    s, db = settings(), _db()
    try:
        status = pipeline.set_kids_gender(s, db, item_id, choice)
    except ValueError as e:
        print(f"[red]not set[/] {item_id}: {escape(str(e))}")
        raise typer.Exit(1) from None
    db.outbox_resolve("kids", item_id)
    print(f"[green]{choice.strip().lower()}[/] set for {item_id} — status {status}")
    approve.announce(s, f"{item_id}: {choice.strip().lower()} set from the CLI")
    approve.pump(s, db)


@app.command()
def recover(ref: str, recheck: bool = typer.Option(
        False, "--recheck", help="ask the front check and the upright check again (model calls) instead of keeping "
                                 "the item's stored ones")) -> None:
    """Recompute ONLY the cover (which photo shows the front, turned upright), the photo order, the category and the
    size of an item — or of every item of a batch (b_...) — that is not on the marketplace. Price, approved price,
    condition and Girls/Boys answers and the listing text stay; nothing settled is asked again (WO23). The item's
    stored front check is kept (run twice, nothing changes; --recheck asks it again), and a waiting card is sent
    again only when what it shows changed (WO24).  e.g.  thrift recover b_...  |  thrift recover i_..."""
    s, db = settings(), _db()
    if ref.startswith("b_"):
        if db.batch(ref) is None:
            print(f"[red]unknown batch[/] {ref}")
            raise typer.Exit(1)
        ids = [r[0] for r in db.conn.execute("SELECT id FROM items WHERE batch_id=? ORDER BY seq", (ref,))]
    else:
        ids = [ref]
    done = 0
    for iid in ids:
        try:
            out = pipeline.recover_item(s, db, iid, recheck=recheck)
        except ValueError as e:
            print(f"[yellow]left as it is[/] {iid}: {escape(str(e))}")
            continue
        done += 1
        b = out["before"]
        card = {"sent again": "card sent again", "changed": "card changed (goes out when its turn comes)",
                "unchanged": "card unchanged (not sent again)"}.get(out.get("card"), "no card")
        print(f"[green]recovered[/] {iid}: cover #{out['cover']} ({out['role']}, front check: {out['view']}, turned "
              f"{out['upright']}°) was #{b['cover']}; category {escape(out['category'])} › "
              f"{escape(str(out.get('subcategory') or '-'))} was {escape(str(b['category']))} › "
              f"{escape(str(b.get('subcategory') or '-'))}; size {out['size']} was {b['size']}; questions: "
              f"{escape('; '.join(out['questions'])) or 'none'}; {out['status']}; {card}")
        if out.get("features") is not None:                      # WO26: what the labels gave, and the title now
            print(f"    title: {escape(str(out.get('title')))} | {escape(out['features'])}")
    if not done and len(ids) == 1:
        raise typer.Exit(1)
    approve.pump(s, db)                                           # a card that changed comes again, one at a time


@app.command()
def reprocess(item_id: str) -> None:
    """Send an item that waits for the owner (or is ready) through the pipeline again, in place — today's prompts and
    copy rules (a set's "2-Piece Set"), the owner's answers kept (price, condition, Girls/Boys, cover, category, no
    brand). Its card stays open meanwhile and is sent again only when what it shows changed (WO25). Model calls.
    e.g.  thrift reprocess i_..."""
    s, db = settings(), _db()
    try:
        out = pipeline.reprocess(s, db, item_id)
    except ValueError as e:
        print(f"[red]not reprocessed[/] {item_id}: {escape(str(e))}")
        raise typer.Exit(1) from None
    card = {"sent again": "card sent again", "changed": "card changed (goes out when its turn comes)",
            "unchanged": "card unchanged (not sent again)"}.get(out.get("card"), "no card")
    print(f"[green]reprocessed[/] {item_id}: {escape(str(out.get('title')))}; {escape(str(out.get('category')))}; "
          f"{out['status']}; {card}")
    if out.get("features") is not None:
        print(f"    {escape(out['features'])}")
    approve.pump(s, db)


@app.command()
def category(item_id: str, path: str = typer.Argument(..., metavar='"<category › subcategory>"')) -> None:
    """Answer "Which category?" (the CLI twin of its buttons): a real Poshmark path, e.g. "Skirts › Skirt Sets" or
    "Shorts" ("Kids > Matching Sets" for another department). The model's own pick is settled at once; another path
    reprocesses the item with it."""
    s, db = settings(), _db()
    it = db.item(item_id)
    facts = pipeline.Facts.model_validate(loads(it["facts"])) if it is not None and it["facts"] else None
    placed = pipeline.taxonomy.parse_path(path, facts) if facts else None
    try:
        if placed is None:
            raise ValueError(f"no such Poshmark category: {path!r}" if facts else f"item {item_id} has no listing yet")
        status = pipeline.set_category(s, db, item_id, placed)
    except ValueError as e:
        print(f"[red]not set[/] {item_id}: {escape(str(e))}")
        raise typer.Exit(1) from None
    db.outbox_resolve("category", item_id)
    label = pipeline.taxonomy.path_label(placed)
    print(f"[green]{escape(label)}[/] set for {item_id} — status {status}")
    approve.announce(s, f"{item_id}: category {label} set from the CLI")
    _tick_unless_worker(s, db)
    approve.pump(s, db)


@app.command()
def redo(batch_id: str) -> None:
    """Rebuild a batch's items from the grouping already confirmed (no new contact sheet): every item that never
    reached the site goes through the pipeline again — new cover, new price, a new card in the queue — and its
    Telegram messages are closed. An item that is posting, posted, drafted or an unconfirmed publish is left as it
    is, and so is one the owner dropped as a re-share. The owner's condition and Girls/Boys answers are kept; the
    price is asked again.  e.g.  thrift redo b_..."""
    s, db = settings(), _db()
    try:
        rebuilt, kept = pipeline.redo_batch(s, db, batch_id)
    except ValueError as e:
        print(f"[red]not rebuilt[/] {batch_id}: {escape(str(e))}")
        raise typer.Exit(1) from None
    n = len(rebuilt)
    print(f"[green]rebuilding[/] {n} item{'s' if n != 1 else ''} of {batch_id}: " + ", ".join(rebuilt))
    for line in kept:
        print(f"[yellow]left as it is[/] {escape(line)}")
    approve.announce(s, f"batch {batch_id}: {n} item{'s' if n != 1 else ''} rebuilt from the CLI — new cards follow, "
                        "one at a time")
    _tick_unless_worker(s, db)
    approve.pump(s, db)


@telegram_app.command("setup")
def telegram_setup() -> None:
    """Print the chat ids and user ids seen in recent updates, to fill TELEGRAM_CHAT_ID and
    TELEGRAM_ALLOWED_USER_IDS in .env. Send the bot a message (or /start in the group) first."""
    from thrift_agent.telegram import Bot
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise typer.BadParameter(f"TELEGRAM_BOT_TOKEN is not set (read {config.ENV_FILE}"
                                 f"{'' if config.ENV_FILE.exists() else ', which does not exist'}); "
                                 "create the bot with @BotFather and put the token there")
    updates = Bot(token, os.getenv("TELEGRAM_CHAT_ID", ""), set()).get_updates(offset=None, timeout=0)
    chats, users = {}, {}
    for u in updates:
        msg = u.get("message") or (u.get("callback_query") or {}).get("message") or {}
        sender = (u.get("message") or {}).get("from") or (u.get("callback_query") or {}).get("from") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            chats[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name") or chat.get("type", "")
        if sender.get("id") is not None:
            users[sender["id"]] = sender.get("username") or sender.get("first_name") or ""
    if not updates:
        print("no updates seen — send the bot a message (in a group: reply to it or /start), then run this again")
    for cid, name in chats.items():
        print(f"chat  {cid}  {name}   → TELEGRAM_CHAT_ID={cid}")
    for uid, name in users.items():
        print(f"user  {uid}  {name}   → add to TELEGRAM_ALLOWED_USER_IDS")


@telegram_app.command("test")
def telegram_test(text: str = typer.Option("thrift-agent: test message — the bot can reach this chat.", "--text",
                                           help="What to send, e.g. --text 'worker started over SSH'")) -> None:
    """Send a test message to TELEGRAM_CHAT_ID with the configured bot."""
    bot = approve.bot_for(settings())
    if bot is None:
        raise typer.BadParameter("Telegram is not configured: telegram.enabled plus TELEGRAM_BOT_TOKEN, "
                                 f"TELEGRAM_CHAT_ID and TELEGRAM_ALLOWED_USER_IDS in {config.ENV_FILE}")
    mid = bot.send_message(text)
    print(f"[green]sent[/] message {mid} to chat {bot.chat_id}")


@app.command()
def requeue(item_id: str, marketplace: str = typer.Argument(None)) -> None:
    """Queue a failed or dry-run post again, or an item the poster parked with a question, as it is (only rows with
    NO listing URL: anything that reached the site is reconciled by hand, never re-posted). A failed batch (b_...)
    goes back to the worker, which splits it again on its next tick.
    e.g.  thrift requeue i_...  |  thrift requeue i_... poshmark  |  thrift requeue b_..."""
    s, db = settings(), _db()
    if item_id.startswith("b_"):
        try:
            pipeline.requeue_batch(s, db, item_id)
        except ValueError as e:
            print(f"[red]not queued[/] {item_id}: {e}")
            raise typer.Exit(1) from None
        print(f"[green]queued[/] batch {item_id} — the worker (thrift run) splits it again within ~15 s")
        return
    done = pipeline.requeue(s, db, item_id, marketplace)
    print(f"[green]queued[/] {item_id}: {', '.join(done)}")


@app.command("mark-posted")
def mark_posted(item_id: str, marketplace: str, url: str) -> None:
    """Record a listing that went live while the poster couldn't find its address (a post in "unconfirmed publish"):
    checks that the page shows the item's title and price, then marks the post posted with that address. Mac only;
    stop the poster service first. e.g.  thrift mark-posted i_... poshmark https://poshmark.com/listing/...-<id>"""
    from thrift_agent.post.base import PosterError
    from thrift_agent.post.runner import mark_posted as run_mark_posted
    s = settings()
    if s.is_prod:
        notify.check(s)
    try:
        address = asyncio.run(run_mark_posted(s, _db(), item_id, marketplace, url))
    except (ValueError, RuntimeError, PosterError) as e:
        print(f"[red]not marked[/] {item_id}: {e}")
        raise typer.Exit(1) from None
    print(f"[green]posted[/] {item_id} on {marketplace}: {address}")


@app.command()
def poster(once: bool = False, dry_run: bool = False,
           allow_dev_browser: bool = typer.Option(False, "--allow-dev-browser",
                                                  help="Open Chrome against the live site on a dev machine "
                                                       "(still a dry-run). The dev machine never touches the shop "
                                                       "otherwise."),
           stage: str = typer.Option(None, "--stage",
                                     help="Dry-run stage for this run: 'form' (fill, read back, Discard) or 'review' "
                                          "(also press Next and record the page after it). Default: "
                                          "poster.dry_run_stage. Never publishes."),
           publish_first: str = typer.Option(None, "--publish-first", metavar="ITEM",
                                             help="Publish this one item, supervised: on the Mac, at the owner-"
                                                  "approved price, after you type LIST in this terminal. Ignores "
                                                  "poster.dry_run for this call; stop the poster service first.")
           ) -> None:
    """Run the Chrome poster (the poster service on the Mac)."""
    from thrift_agent.post.runner import dry_run_stage, publish_first as run_publish_first, run as run_poster
    s = settings()
    if publish_first:
        from thrift_agent.post.base import PosterError
        if s.is_prod:
            notify.check(s)
        try:
            out = asyncio.run(run_publish_first(s, _db(), publish_first))
        except (ValueError, RuntimeError, PosterError) as e:
            print(f"[red]not published[/] {publish_first}: {e}")
            raise typer.Exit(1) from None
        print(f"{out.status}: {out.url or out.error or out.note or ''}")
        if out.status == "failed":
            raise typer.Exit(1)
        return
    try:
        stage = dry_run_stage(s, stage)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    if s.is_prod:
        notify.check(s)
    asyncio.run(run_poster(s, _db(), once=once, force_dry=dry_run, allow_dev_browser=allow_dev_browser, stage=stage))


@app.command()
def login(site: str = "poshmark") -> None:
    """Open the poster Chrome profile to log in by hand (once per site). Stop the poster service first."""
    from thrift_agent.post.base import open_browser
    urls = {"poshmark": "https://poshmark.com/login", "depop": "https://www.depop.com/login/"}

    async def go():
        s = settings()
        pw, ctx = await open_browser(s.path("chrome_profile"), s["schedule"]["timezone"])
        page = await ctx.new_page()
        await page.goto(urls[site])
        await asyncio.to_thread(input, f"Log in to {site} in the Chrome window, then press Enter here… ")
        await ctx.close()
        await pw.stop()

    asyncio.run(go())


@app.command()
def status() -> None:
    """What's in the pipeline."""
    db = _db()
    t = Table("item", "status", "title", "price", "gate", "posts")
    for row in db.conn.execute("SELECT * FROM items ORDER BY created_at DESC LIMIT 30"):
        renders, gate, pr = loads(row["renders"]) or {}, loads(row["gate"]) or {}, loads(row["price"]) or {}
        posts = ", ".join(f"{p['marketplace']}:{p['status']}" for p in
                          db.conn.execute("SELECT * FROM posts WHERE item_id=?", (row["id"],)))
        title = next(iter(renders.values()), {}).get("title", "")
        t.add_row(row["id"], row["status"], title[:50], str(pr.get("list_price") or ""),
                  gate.get("decision", ""), posts)
    print(t)
    # Shares that haven't become items: waiting for the worker, waiting for the contact-sheet answer, or failed.
    open_batches = db.conn.execute("SELECT * FROM batches WHERE status IN ('new', 'needs_confirm', 'failed') "
                                   "ORDER BY created_at DESC LIMIT 20").fetchall()
    if open_batches:
        bt = Table("batch", "status", "photos", "shared")
        for b in open_batches:
            bt.add_row(b["id"], b["status"], str(b["n_photos"] or ""), b["created_at"][:16].replace("T", " "))
        print(bt)
    for b in db.batches("new"):
        print(f"[yellow]waiting for the worker[/] {b['id']}  (thrift run splits it within ~15 s)")
    for b in db.batches("needs_confirm"):
        print(f"[yellow]awaiting confirm[/] {b['id']}  →  thrift confirm {b['id']} ok")
    for b in db.batches("regroup"):
        print(f"[yellow]wrong photos[/] {b['id']}  →  thrift confirm {b['id']} \"<fix>\"  (12>2 | split 7 | merge 2 3 | "
              f"drop 7 | ok)")
    # Groupings taken without the owner (segmentation.auto_confirm, WO20b) that the code had doubts about: kept here,
    # never sent. [Wrong photos] on a card (or thrift confirm while it is reopened) fixes one.
    for b in db.conn.execute("SELECT * FROM batches WHERE status='split' AND reasons IS NOT NULL AND reasons != '[]' "
                             "ORDER BY created_at DESC LIMIT 5"):
        if (loads(b["segmentation"]) or {}).get("auto_accepted"):
            print(f"[dim]grouping accepted with doubts[/] {b['id']}: " + escape("; ".join(loads(b["reasons"]))))
    for b in db.batches("failed"):
        err = pipeline.last_error(db, b["id"])
        print(f"[red]failed batch[/] {b['id']}  →  thrift requeue {b['id']}"
              + (f"\n    {escape(err[:200])}" if err else ""))
    for it in db.items("awaiting_condition"):
        print(f"[yellow]brand new or worn?[/] {it['id']}  →  thrift condition {it['id']} nwt|like_new|good")
    for it in db.items("awaiting_price"):
        print(f"[yellow]awaiting price[/] {it['id']}  →  thrift price {it['id']} <amount>")
    for it in db.items("needs_owner"):
        print(f"[yellow]needs owner[/] {it['id']}  →  thrift answer {it['id']} \"<answer>\"  (or thrift requeue "
              f"{it['id']} to retry as it is)")
    # The owner's Telegram queue (WO20): one open question at a time, the rest in order behind it.
    queue = approve.queue(db)
    if queue:
        row = approve.open_message(db, settle=False)
        is_open = f"{row['kind']} {row['ref']}" if row else "nothing"
        print(f"[cyan]Telegram[/] open: {is_open}; {len(queue)} in the queue, next: "
              + ", ".join(f"{k} {r}" for k, r in queue[:4]) + (" …" if len(queue) > 4 else ""))
    print(f"worker inbox scan: {db.kv_get(SCAN_KEY) or 'never'}")


@app.command()
def show(item_id: str) -> None:
    """Print an item's facts, price, gate and renders."""
    row = _db().item(item_id)
    if row is None:
        raise typer.BadParameter(f"unknown item {item_id}")
    print(json.dumps({k: loads(row[k]) for k in ("facts", "price", "gate", "renders")}, indent=1))


@app.command()
def harvest(max_orders: int = 0) -> None:
    """Pull sales + listing pages from the logged-in poster profile into paths.harvest (private/harvest)."""
    from thrift_agent.harvest import harvest as run_harvest
    print(asyncio.run(run_harvest(settings(), max_orders or None)))


@app.command("build-style")
def build_style(keep: int = 30) -> None:
    """Turn harvested listings into few-shot style examples for the copywriter."""
    from thrift_agent.harvest import build_style as run_build
    print(run_build(settings(), keep))


@app.command("eval")
def eval_(fixtures: Path = Path("eval/fixtures")) -> None:
    """Score segmentation + extraction against eval/fixtures/*/expected.yaml."""
    from thrift_agent.eval import run_all, summarize
    print(summarize(run_all(settings(), fixtures)))     # one failing case no longer discards the paid results


if __name__ == "__main__":
    app()
