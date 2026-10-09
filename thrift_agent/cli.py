"""`thrift` command. Dev on Windows: init, process, confirm, eval, status. Prod on the Mac: run, poster."""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import traceback
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich import print
from rich.markup import escape
from rich.table import Table

from thrift_agent import alerts, approve, config, daily, notify, pipeline, power, runlock
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
        started = time.monotonic()                       # the Mac's clock that stops while it sleeps
        slept = _sleep_watch()
        if kind == approve.NEW_BATCH:
            _guard(db, ref, lambda: pipeline.process_batch(s, db, ref), lambda: db.set_batch(ref, status="failed"),
                   slept)
        else:
            _guard(db, ref, lambda: pipeline.process_item(s, db, ref), lambda: db.set_item(ref, status="failed"),
                   slept)
        # the daily window's estimate (WO28 §2) is the median of these
        db.log(ref, "worked", {"kind": "batch" if kind == approve.NEW_BATCH else "item",
                               "seconds": round(time.monotonic() - started, 1)})
        n += 1
    return n


def _scan_inbox(s, db) -> int:
    """Register what the iPhone shared. A read of the iCloud inbox that macOS refuses or interrupts (python3.14 still
    waiting for its iCloud Drive permission) is not an error to report each time (WO22): it is retried quietly on the
    next tick, and the owner gets one message if it lasts (alerts.inbox_trouble). The start and the end of each look
    are recorded: the Telegram thread notices a look stuck on the permission prompt, deploy that the worker got
    through one. Processing of the items already split goes on either way. iCloud still downloading a share
    (EDEADLK, WO29) is quieter still: retried, and only the ops chat hears if it lasts 10 minutes."""
    alerts.scan_started(db)
    try:
        n = sum(1 for folder in pipeline.ready_folders(s, db) if pipeline.register(s, db, folder))
    except OSError as e:
        if alerts.icloud_busy(e):
            alerts.busy(db, "the inbox", f"{type(e).__name__}: {e}")
            return 0
        if not alerts.inbox_unreadable(e):
            raise
        alerts.inbox_trouble(db, f"{type(e).__name__}: {e}")
        return 0
    alerts.scan_done(db)
    alerts.inbox_ok(db)
    alerts.not_busy(db, "the inbox")
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


def _sleep_watch():
    """A question to ask after a job: did the Mac sleep while it ran (the lid closed mid-item; WO28)?"""
    wall, mono, wake = time.time(), time.monotonic(), power.last_wake()
    return lambda: power.slept_since(wall, wake, mono)


def _guard(db, ref, fn, on_fail, slept=None) -> None:
    """One batch or item: a failure marks it failed and is told once a day per identical error, whichever item it
    hits (ten items failing on one bad API key are one message; thrift status lists them all). A failure while the
    Mac slept (a model call cut off by the lid closing, WO28) is not the item's: it stays as it was and the next tick
    takes it again. Nor is iCloud still downloading the share's photos (EDEADLK, WO29): retried quietly, the ops chat
    hears only if it lasts 10 minutes. Errors go to the ops chat, never the group."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        if slept is not None and slept():
            db.log(ref, "interrupted_by_sleep", f"{type(e).__name__}: {e}")
            return
        if alerts.icloud_busy(e):
            alerts.busy(db, ref, f"{type(e).__name__}: {e}")
            return
        on_fail()
        db.log(ref, "error", traceback.format_exc())
        alerts.once(db, f"❌ {ref}: {type(e).__name__}: {e}")
        return
    alerts.not_busy(db, ref)


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


def _worker_iteration(s, db, interval: int, window: daily.Window | None = None) -> None:
    """One turn of the worker's main thread: notice a wake (the daily window's catch-up and "Back online"), process
    the inbox in the queue's order, send the next question if none is open, bring the window's status message up to
    date, sleep. Telegram's side (replies, buttons, the next question after an answer) runs on its own thread, so the
    owner is answered at once while items are still being processed."""
    began = None
    if window is not None:
        try:
            began = window.step()
        except Exception as e:  # noqa: BLE001 — the window's side never stops the worker
            db.log(None, "error", traceback.format_exc())
            print(f"[window] {type(e).__name__}: {e}")
    if window is None or not window.lid_closed:          # lid closed: a short maintenance wake, nothing is started
        _safe_tick(s, db)
        _safe_pump(s, db)
        _safe_window(db, window.update if window else None)
        _safe_sales(db, force_beat=bool(began))         # WO33: the changes to thrift-api, the heartbeat on a wake
    time.sleep(interval)


def _safe_sales(db, force_beat: bool = False) -> None:
    """Sales tracking's turn (WO33): never the reason the worker stops (the API away is a quiet event)."""
    from thrift_agent import sales
    try:
        sales.tick(db, force_beat=force_beat)
    except Exception as e:  # noqa: BLE001
        db.log(None, "error", f"sales tick: {type(e).__name__}: {e}")


def _safe_window(db, fn) -> None:
    """The daily window's side of a turn (WO28): never the reason the worker stops."""
    if fn is None:
        return
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        db.log(None, "error", traceback.format_exc())
        print(f"[window] {type(e).__name__}: {e}")


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
    # The daily window (WO28): its first step() is the start's catch-up — the inbox, "Back online" when there is work,
    # the open question re-sent only if it is old (never at each deploy) — and so is every wake with the lid open.
    # WO29: the window's status message and "Back online" go to the ops chat; the group hears neither.
    window = daily.Window(s, db, approve.ops_bot_for(s), scan=lambda: _scan_inbox(s, db))
    telegram = None
    if bot:
        telegram = threading.Thread(target=_telegram_loop, args=(s, bot), name="telegram", daemon=True)
        telegram.start()
    try:
        while lock:                                       # the lock is held for as long as this loop runs
            if telegram is not None and not telegram.is_alive():
                raise SystemExit("the Telegram thread stopped — exiting so launchd restarts the worker")
            _worker_iteration(s, db, interval, window)
    finally:
        window.awake.hold(False)


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
                                 "the item's stored ones"),
            relabel: bool = typer.Option(False, "--relabel", help="read the labels again (one model call; WO27)")) -> None:
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
            out = pipeline.recover_item(s, db, iid, recheck=recheck, relabel=relabel)
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
def edit(item_id: str,
         title: str = typer.Option(None, "--title", help="the listing's title, exactly (at most 80 characters)"),
         brand: str = typer.Option(None, "--brand", help="the brand, spelled as Poshmark lists it ('no brand' for none)"),
         sizes: str = typer.Option(None, "--sizes", help="a set's pieces and their sizes: 'Cardigan=S,Pants=XS'")
         ) -> None:
    """Set an item's title and/or brand exactly as given (WO27): the owner's words, kept through any reprocessing; no
    model call, the price kept. Not for an item already on the marketplace. `--sizes` (WO33): a set's pieces with
    their own sizes — listed under the bigger one, the description says which piece is which.
    e.g.  thrift edit i_... --brand 'J. Crew' --title 'J. Crew 100% Merino Wide Leg Sweater Pants Blue size M'
          thrift edit i_... --sizes 'Cardigan=S,Pants=XS'"""
    if title is None and brand is None and sizes is None:
        print("[red]nothing to set[/]: give --title, --brand and/or --sizes")
        raise typer.Exit(1)
    s, db = settings(), _db()
    try:
        status = db.item(item_id)["status"] if db.item(item_id) else None
        if sizes is not None:
            pairs = [tuple(part.replace(":", "=").split("=", 1)) for part in sizes.split(",") if part.strip()]
            if any(len(pair) != 2 for pair in pairs):
                raise ValueError("--sizes takes piece=size pairs: 'Cardigan=S,Pants=XS'")
            status = pipeline.set_piece_sizes(s, db, item_id, pairs)
        if title is not None or brand is not None:
            status = pipeline.edit_listing(s, db, item_id, title=title, brand=brand)
    except ValueError as e:
        print(f"[red]not set[/] {item_id}: {escape(str(e))}")
        raise typer.Exit(1) from None
    posh = (loads(db.item(item_id)["renders"]) or {}).get("poshmark") or {}
    print(f"[green]set[/] {item_id}: {escape(str(posh.get('title')))} · brand {escape(str(posh.get('brand')))} · "
          f"${posh.get('price')} · {status}")
    approve.announce(s, f"{item_id}: " + ", ".join(f"{k} '{v}'" for k, v in (("title", title), ("brand", brand),
                                                                            ("sizes", sizes)) if v)
                     + " set from the CLI")
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
                                           help="What to send, e.g. --text 'worker started over SSH'"),
                  ops: bool = typer.Option(False, "--ops", help="to the ops chat instead of the group")) -> None:
    """Send a test message to TELEGRAM_CHAT_ID (the group) with the configured bot — or, with --ops, to the ops chat
    (TELEGRAM_OPS_CHAT_ID, the owner's private chat: it works once he has sent the bot /start)."""
    s = settings()
    bot = approve.ops_bot_for(s) if ops else approve.bot_for(s)
    if bot is None:
        raise typer.BadParameter("Telegram is not configured: telegram.enabled plus TELEGRAM_BOT_TOKEN, "
                                 f"TELEGRAM_CHAT_ID and TELEGRAM_ALLOWED_USER_IDS in {config.ENV_FILE}"
                                 + (" — and an ops chat: TELEGRAM_OPS_CHAT_ID or telegram.ops_chat_id" if ops else ""))
    mid = bot.send_message(text)
    print(f"[green]sent[/] message {mid} to {'the ops chat' if ops else 'the group'} ({bot.chat_id})")


MARKETPLACE = typer.Option(None, "--marketplace", metavar="poshmark|depop|vinted", help="Which marketplace (WO30)")


@app.command()
def requeue(item_id: str, marketplace: str = typer.Argument(None), mp: str = MARKETPLACE) -> None:
    """Queue a failed, skipped or dry-run listing again, or an item the poster parked with a question, as it is (only
    rows with NO listing URL: anything that reached the site is reconciled by hand, never re-posted). A failed batch
    (b_...) goes back to the worker, which splits it again on its next tick.
    e.g.  thrift requeue i_...  |  thrift requeue i_... --marketplace depop  |  thrift requeue b_..."""
    s, db = settings(), _db()
    marketplace = mp or marketplace
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
def mark_posted(item_id: str, first: str = typer.Argument(..., metavar="[MARKETPLACE] URL"),
                second: str = typer.Argument(None, hidden=True), mp: str = MARKETPLACE) -> None:
    """Record a listing that went live while the poster couldn't find its address (a listing in "unconfirmed
    publish"): checks that the page shows the item's title and price, then marks it posted with that address. Mac
    only. e.g.  thrift mark-posted i_... poshmark https://poshmark.com/listing/...-<id>
          thrift mark-posted i_... https://www.depop.com/products/.../ --marketplace depop"""
    from thrift_agent.post.base import PosterError
    from thrift_agent.post.runner import mark_posted as run_mark_posted
    s, db = settings(), _db()
    marketplace, url = (first, second) if second is not None else ((mp or "poshmark"), first)
    marketplace = mp or marketplace
    if s.is_prod:
        notify.check(s)
    if daily.poster_now(db).running:                     # the poster service has Chrome: it checks the page (WO28)
        try:
            address = pipeline.request_posted(s, db, item_id, url, marketplace)
        except ValueError as e:
            print(f"[red]not marked[/] {item_id}: {escape(str(e))}")
            raise typer.Exit(1) from None
        print(f"[green]queued[/] {item_id}: the running poster opens {address} between listings and records it "
              "(✅ confirmed live in Telegram)")
        return
    try:
        address = asyncio.run(run_mark_posted(s, db, item_id, marketplace, url))
    except (ValueError, RuntimeError, PosterError) as e:
        print(f"[red]not marked[/] {item_id}: {e}")
        raise typer.Exit(1) from None
    print(f"[green]posted[/] {item_id} on {marketplace}: {address}")


@app.command()
def retry(item_id: str, marketplace: str = typer.Argument("poshmark"), mp: str = MARKETPLACE) -> None:
    """A listing that may be live ("unconfirmed publish": the Mac slept while publishing, or its address wasn't found)
    that you checked on the marketplace and is NOT there: it goes back in line and is listed again. The twin of the
    reply 'retry' (WO28). If it IS there: thrift mark-posted <item> <marketplace> <url>."""
    s, db = settings(), _db()
    marketplace = mp or marketplace
    try:
        pipeline.retry_unconfirmed(s, db, item_id, marketplace)
    except ValueError as e:
        print(f"[red]not retried[/] {item_id}: {escape(str(e))}")
        raise typer.Exit(1) from None
    print(f"[green]back in line[/] {item_id} on {marketplace}: the poster lists it again")
    approve.announce(s, f"{item_id}: retry from the CLI — it goes back in line")


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
                                                  "approved price, after you type LIST (Poshmark) or POST (Depop, "
                                                  "Vinted) in this terminal. Ignores poster.dry_run for this call; "
                                                  "stop the poster service first."),
           mp: str = MARKETPLACE) -> None:
    """Run the Chrome poster (the poster service on the Mac)."""
    from thrift_agent.post.runner import dry_run_stage, publish_first as run_publish_first, run as run_poster
    s = settings()
    if publish_first:
        from thrift_agent.post.base import PosterError
        if s.is_prod:
            notify.check(s)
        try:
            out = asyncio.run(run_publish_first(s, _db(), publish_first, mp=mp or "poshmark"))
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
    from thrift_agent import crosslist as cl
    from thrift_agent.post.base import open_browser
    urls = {"poshmark": "https://poshmark.com/login", "depop": "https://www.depop.com/login/",
            "vinted": "https://www.vinted.com/"}
    if site in cl.CROSS and cl.driver(settings(), site) == "extension":
        print(f"{site} is logged in by hand in the Thrift Chrome window (WO32: its own Dock icon, the second Chrome), "
              f"not in the poster profile. Open {urls[site]} there and log in the normal way.")
        return

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
    t = Table("item", "status", "title", "price", "gate", "listings")
    for row in db.conn.execute("SELECT * FROM items ORDER BY created_at DESC LIMIT 30"):
        renders, gate, pr = loads(row["renders"]) or {}, loads(row["gate"]) or {}, loads(row["price"]) or {}
        posts = ", ".join(f"{p['marketplace']}:{p['status']}" for p in db.listings_for(row["id"]))
        title = next(iter(renders.values()), {}).get("title", "")
        t.add_row(row["id"], row["status"], title[:50], str(pr.get("list_price") or ""),
                  gate.get("decision", ""), posts)
    print(t)
    # Per marketplace (WO30): every listing row by status, and a marketplace stopped for this window.
    from thrift_agent import crosslist
    counts = db.counts_by_marketplace()
    if counts:
        print("listings: " + " · ".join(f"{mp} " + ", ".join(f"{st} {n}" for st, n in sorted(counts[mp].items()))
                                        for mp in crosslist.ORDER if mp in counts))
    for mp in crosslist.CROSS:
        if why := crosslist.blocked(db, mp):
            print(f"[red]{mp} stopped for this window[/]: {escape(why)}")
    link = loads(db.kv_get(crosslist.EXT_LINK)) or {}
    if link:                                           # WO32: the Thrift Chrome's extension, as the poster last saw it
        print(f"extension: {'connected' if link.get('connected') else '[yellow]not connected[/]'} "
              f"(the poster's view at {str(link.get('at', ''))[:16].replace('T', ' ')} UTC)")
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
    for it in db.items("needs_owner"):                 # parked by a poster from before WO27 (it asks nothing now)
        print(f"[yellow]needs owner[/] {it['id']}  →  thrift requeue {it['id']} (retry as it is; the poster now guesses)")
    # The owner's Telegram queue (WO20): one open question at a time, the rest in order behind it.
    queue = approve.queue(db)
    if queue:
        row = approve.open_message(db, settle=False)
        is_open = f"{row['kind']} {row['ref']}" if row else "nothing"
        print(f"[cyan]Telegram[/] open: {is_open}; {len(queue)} in the queue, next: "
              + ", ".join(f"{k} {r}" for k, r in queue[:4]) + (" …" if len(queue) > 4 else ""))
    print(f"worker inbox scan: {db.kv_get(SCAN_KEY) or 'never'}")
    # The daily window (WO28): the poster as it last said, and the window's status message.
    p = daily.poster_now(db)
    state = ("not running" if not p.running else ("LIVE" if p.live else "dry-run")
             + (f", publishing {p.busy}" if p.busy else "") + (f", next at {p.next_at:%H:%M} UTC" if p.next_at else "")
             + (f", paused: {p.paused}" if p.paused else ""))
    print(f"poster: {state}")
    st = loads(db.kv_get(daily.STATUS_KEY)) or {}
    if st.get("text"):
        print(f"window: {escape(st['text'])}")
    _status_sales(db)


def _status_sales(db) -> None:
    """WO33: the sales API as this Mac sees it — reachable, its mode, open sales, take-downs, Gmail's last heartbeat."""
    from thrift_agent import sales
    client = sales.api()
    pending = sales.pending_count(db)
    if client is None:
        print("sales tracking: off (THRIFT_API_URL / THRIFT_API_KEY not in .env)")
        return
    try:
        h = client.get("/health")
    except sales.ApiError as e:
        print(f"[yellow]sales API: not reachable[/] ({escape(str(e))}) · take-downs pending here: {pending}")
        return
    beats = h.get("heartbeats") or {}
    gmail = beats.get("gmail") if isinstance(beats.get("gmail"), str) else (beats.get("gmail") or {}).get("last_seen")
    print(f"sales API: reachable · mode {h.get('mode')} · open sales {h.get('open_sales', '?')} · take-downs pending "
          f"here {pending} · Gmail's last heartbeat {gmail or 'never'}")


@app.command()
def show(item_id: str) -> None:
    """Print an item's facts, price, gate and renders, and its listing on each marketplace (WO30)."""
    db = _db()
    row = db.item(item_id)
    if row is None:
        raise typer.BadParameter(f"unknown item {item_id}")
    print(json.dumps({k: loads(row[k]) for k in ("facts", "price", "gate", "renders")}, indent=1))
    t = Table("marketplace", "status", "price", "url", "listing id", "attempts", "error")
    for mp in ("poshmark", "depop", "vinted"):
        r = db.listing(item_id, mp)
        t.add_row(mp, *(["—"] * 6) if r is None else
                  (r["status"], str(r["price"] or ""), r["url"] or "", r["listing_id"] or "", str(r["attempts"]),
                   escape((r["error"] or "")[:80])))
    print(t)
    for mp in ("depop", "vinted"):
        if (r := db.listing(item_id, mp)) is not None and r["fields_json"]:
            print(f"{mp} fields: " + escape(json.dumps(loads(r["fields_json"]), ensure_ascii=False)[:1500]))


@app.command()
def crosslist(item_id: str = typer.Argument(None, metavar="[ITEM]"),
              dry_run: bool = typer.Option(False, "--dry-run", help="Fill the forms, screenshot to the ops chat, "
                                                                    "publish nothing (with --backfill: only list)"),
              backfill: bool = typer.Option(False, "--backfill", help="Queue every item still live on Poshmark"),
              check_login: bool = typer.Option(False, "--check-login", help="Open the sell pages in the Thrift "
                                                                            "Chrome: logged in? (WO32)"),
              one_by_one: bool = typer.Option(False, "--one-by-one", help="--dry-run: the sites one after another "
                                                                          "(the old way), not together (WO33)"),
              hand_listed: bool = typer.Option(False, "--hand-listed", help="Read the Depop and Vinted shops: which "
                                               "of our items she already listed by hand? The list goes to the ops "
                                               "chat; nothing is recorded (WO33)"),
              apply: str = typer.Option(None, "--apply", metavar="1,2", help="--hand-listed: record the numbers the "
                                        "owner confirmed (the row posted with her listing's address)"),
              mp: str = MARKETPLACE) -> None:
    """Cross-list on Depop and Vinted (WO30). `thrift crosslist <item>` queues an item already live on Poshmark;
    `--dry-run <item>` fills both forms (the running poster does it between listings), the screenshots and the mapped
    fields go to the ops chat, nothing is published; `--backfill` queues every item still live on Poshmark (checked
    first), oldest first — the poster takes 25 a day per marketplace (`--dry-run`: only lists them)."""
    from thrift_agent import crosslist as cl
    s, db = settings(), _db()
    mps = [mp] if mp else cl.enabled(s)
    bad = [m for m in mps if m not in cl.enabled(s)]
    if bad:
        raise typer.BadParameter(f"{', '.join(bad)}: not enabled, or its catalog doesn't load (thrift catalogs check)")
    if hand_listed:
        from thrift_agent import handlisted
        if apply:
            numbers = [int(x) for x in re.findall(r"\d+", apply)]
            for line in handlisted.apply(db, numbers):
                print(escape(line))
            return
        if daily.poster_now(db).running:
            raise typer.BadParameter("the poster is running (it holds the extension's bridge): stop it first — "
                                     "bash deploy/services.sh stop poster")
        if not s.is_prod:
            raise typer.BadParameter("the dev machine never opens the marketplaces: run it on the Mac")
        shops = asyncio.run(_shop_listings(s, [m for m in mps if m in ("depop", "vinted")]))
        items = []
        for it in db.conn.execute("SELECT * FROM items WHERE status NOT IN ('dropped')").fetchall():
            render = (loads(it["renders"]) or {}).get("poshmark") or {}
            items.append({"id": it["id"], "title": render.get("title") or "", "brand": render.get("brand") or "",
                          "size": render.get("size") or "", "price": it["owner_price"] or render.get("price")})
        ours = {r["url"] for r in db.conn.execute("SELECT url FROM listings WHERE url IS NOT NULL").fetchall()}
        found = handlisted.candidates(items, shops, ours)
        handlisted.save(db, found)
        text = handlisted.message(found, {m: len(v) for m, v in shops.items()})
        notify.say(text)
        print(escape(text))
        return
    if check_login:
        from thrift_agent.post import runner
        if daily.poster_now(db).running:
            runner.request_check_login(db, mps)
            print(f"[green]asked[/] the running poster to open the sell pages of {', '.join(mps)} in the Thrift Chrome: "
                  "the answer goes to the ops chat")
            return
        if not s.is_prod:
            raise typer.BadParameter("the dev machine never opens the marketplaces: run it on the Mac")
        for line in asyncio.run(_check_logins(s, db, mps)):
            print(escape(line))
        return
    if backfill:
        rows = cl.backfill(s, db, mps, check=not s.get("crosslist.skip_live_check", False), dry=dry_run)
        for iid, verdict in rows:
            print(f"{'[green]' if verdict.startswith(('queued', 'would')) else '[yellow]'}{iid}[/] {escape(verdict)}")
        caps = ", ".join(f"{m} {cl.daily_cap(s, m)}" for m in mps)
        print(f"{sum(v.startswith(('queued', 'would')) for _, v in rows)} of {len(rows)} "
              f"{'would be ' if dry_run else ''}queued; the poster takes {caps} a day")
        return
    if not item_id:
        raise typer.BadParameter("give an item, or --backfill")
    if db.item(item_id) is None:
        raise typer.BadParameter(f"unknown item {item_id}")
    posh = db.listing(item_id, "poshmark")
    if posh is None or posh["status"] != "posted":
        raise typer.BadParameter(f"{item_id} isn't live on Poshmark: cross-listing follows Poshmark")
    if dry_run:
        from thrift_agent.post import runner
        if daily.poster_now(db).running:
            runner.request_dry_run(db, item_id, mps)
            print(f"[green]asked[/] the running poster for a dry run of {item_id} on {', '.join(mps)}: it fills the "
                  "forms between listings and sends the screenshots and the fields to the ops chat")
            return
        if not s.is_prod:
            raise typer.BadParameter("the dev machine never opens the marketplaces: run it on the Mac")
        asyncio.run(_crosslist_dry_run(s, db, item_id, mps, together=not one_by_one and s.get("poster.parallel", True)))
        return
    queued = cl.queue(s, db, item_id, mps, why="thrift crosslist")
    print(f"[green]queued[/] {item_id}: {', '.join(queued)}" if queued else
          f"{item_id}: already has a row on {', '.join(mps)} (thrift requeue {item_id} --marketplace <m>)")


async def _shop_listings(s, mps: list[str]) -> dict[str, list[dict]]:
    """Her Depop and Vinted shops read through the Thrift Chrome's extension (this process's bridge: the poster
    stopped). {site: [{url, text}]}."""
    from thrift_agent.post import runner
    ps = {mp: p for mp, p in runner.posters(s).items() if mp in mps and getattr(p, "driver", "") == "extension"}
    b, _ = await runner.open_bridge(s, None, ps, watch=False, strict=True)
    try:
        out = {}
        for mp, p in ps.items():
            await runner.connect_extension(b, p)
            out[mp] = await p.shop_listings()
            print(f"{mp}: {len(out[mp])} listings in the shop")
        return out
    finally:
        await runner.close_bridge(b, None)


async def _check_logins(s, db, mps: list[str]) -> list[str]:
    from thrift_agent.post import runner
    ps = {mp: p for mp, p in runner.posters(s).items() if mp in mps and getattr(p, "driver", "") == "extension"}
    b, _ = await runner.open_bridge(s, None, ps, watch=False)
    try:
        if b is None or not ps:
            return ["no extension-driven marketplace to check (or the bridge didn't start: see the ops chat)"]
        if not await next(iter(ps.values())).wait_connected(120):
            return ["the Thrift Chrome extension didn't connect in 2 minutes — is the Thrift Chrome open?"]
        return await runner.check_logins(db, ps, list(ps))
    finally:
        await runner.close_bridge(b, None)


async def _crosslist_dry_run(s, db, iid: str, mps: list[str], together: bool = False) -> None:
    """The dry run in this process (the poster isn't running): the Thrift Chrome's extension through a bridge here
    (WO32), or the poster's Chrome profile for a Playwright-driven marketplace; the forms filled, nothing saved.
    `together` (WO33): the extension's sites at the same time, each in its own window — the parallel poster's way;
    else one after another. Each site's time and the whole run's are printed."""
    import time as _time

    from thrift_agent.post import runner
    from thrift_agent.post.base import open_browser
    ps = {mp: p for mp, p in runner.posters(s).items() if mp in mps}
    b, _ = await runner.open_bridge(s, None, ps, watch=False)
    pw = ctx = None
    took: dict[str, float] = {}

    async def one(mp: str) -> None:
        tag = f"{mp}: " if together else "  "
        if not together:
            print(f"{mp}:")
        t0 = _time.monotonic()
        out = await runner.run_cross(s, db, ps, ctx, iid, mp, dry=True, request=True,
                                     progress=lambda line: print(f"{tag}{line}", flush=True))
        took[mp] = _time.monotonic() - t0
        print(f"{mp}: {out.status if out else 'not done (see the ops chat)'} in {took[mp]:.1f} s"
              + (f" — {out.error}" if out and out.error else "")
              + (f" — {out.screenshot}" if out and out.screenshot else ""))
    try:
        if runner.uses_browser(ps):
            pw, ctx = await open_browser(s.path("chrome_profile"), s["schedule"]["timezone"])
        ready = []
        for mp in mps:
            if mp not in ps:
                print(f"{mp}: off (see the ops chat)")
                continue
            if getattr(ps[mp], "driver", "") == "extension":
                try:
                    await runner.connect_extension(b, ps[mp])          # seconds; past 30 s it says why (WO32b)
                except RuntimeError as e:
                    print(f"{mp}: {e}")
                    continue
            ready.append(mp)
        t0 = _time.monotonic()
        ext = [mp for mp in ready if getattr(ps[mp], "driver", "") == "extension"]
        if together and len(ext) > 1:
            await asyncio.gather(*(one(mp) for mp in ext))
            for mp in ready:
                if mp not in ext:
                    await one(mp)
        else:
            for mp in ready:
                await one(mp)
        if len(took) > 1:
            print(f"all: {_time.monotonic() - t0:.1f} s {'together' if together else 'one after another'} "
                  f"(the sites' own times add up to {sum(took.values()):.1f} s)")
    finally:
        if ctx is not None:
            await ctx.close()
            await pw.stop()
        await runner.close_bridge(b, None)


# ---------------------------------------------------------------- WO33: sales, take-downs, the API

sales_app = typer.Typer(help="Sales tracked by thrift-api (WO33): open, unmatched or all; `match` links one to an item")
app.add_typer(sales_app, name="sales")
api_app = typer.Typer(help="thrift-api, the sales tracking's Azure Function (WO33)")
app.add_typer(api_app, name="api")


def _api_or_exit():
    from thrift_agent import sales
    client = sales.api()
    if client is None:
        print("[red]sales tracking is off[/]: add THRIFT_API_URL and THRIFT_API_KEY to .env")
        raise typer.Exit(1)
    return client


@sales_app.callback(invoke_without_command=True)
def sales_list(ctx: typer.Context, open_: bool = typer.Option(False, "--open", help="unshipped sales (the default)"),
               unmatched: bool = typer.Option(False, "--unmatched", help="sales that matched no item"),
               all_: bool = typer.Option(False, "--all", help="every sale")) -> None:
    """The sales thrift-api found in the marketplace emails."""
    if ctx.invoked_subcommand:
        return
    from thrift_agent import sales
    client = _api_or_exit()
    state = "unmatched" if unmatched else "all" if all_ else "open"
    try:
        rows = client.get("/sales", {"state": state}).get("sales") or []
    except sales.ApiError as e:
        print(f"[red]the API didn't answer[/]: {escape(str(e))}")
        raise typer.Exit(1) from None
    t = Table("sale", "site", "title", "$", "sold", "ship by", "shipped", "status", "item")
    for r in rows:
        t.add_row(str(r.get("id")), str(r.get("marketplace")), escape(str(r.get("title_seen") or r.get("title") or ""))[:40],
                  str(r.get("price") or ""), str(r.get("sold_at") or "")[:10], str(r.get("ship_by") or ""),
                  str(r.get("shipped_at") or "")[:10], str(r.get("status")), str(r.get("item_id") or ""))
    print(t if rows else f"no {state} sales")


@sales_app.command("match")
def sales_match(sale_id: str, item_id: str) -> None:
    """Link an unmatched sale to its item — then its take-downs follow."""
    from thrift_agent import sales
    client = _api_or_exit()
    try:
        out = client.post(f"/sales/{sale_id}/match", {"item_id": item_id})
    except sales.ApiError as e:
        print(f"[red]not matched[/]: {escape(str(e))}")
        raise typer.Exit(1) from None
    print(f"[green]matched[/] {sale_id} → {item_id}: {escape(json.dumps(out)[:300])}")


@app.command()
def delist(run: bool = typer.Option(False, "--run", help="do the pending take-downs now (the poster stopped)"),
           verify: bool = typer.Option(False, "--verify", help="record a site's take-down control without using it"),
           marketplace: str = typer.Option(None, "--marketplace", help="poshmark | depop | vinted (--verify)"),
           url: str = typer.Option(None, "--url", help="the listing to look at (--verify)"),
           practice: bool = typer.Option(False, "--practice", help="Depop: the bin, the window recorded, Cancel — "
                                                                   "never Delete listing (WO33)"),
           item: str = typer.Option(None, "--item", help="the owner's take-down of this item's listing "
                                                         "(--marketplace poshmark: Not For Sale)")) -> None:
    """Take-downs (WO33): reversible only — Poshmark Not for Sale, Depop Mark as sold, Vinted Hide. On the Mac, with
    the poster service stopped (the running poster does them by itself between listings)."""
    from thrift_agent.post import takedown
    s = settings()
    if not s.is_prod:
        raise typer.BadParameter("take-downs run on the Mac only (machine_role: prod)")
    if item:
        db = _db()
        if db.item(item) is None:
            raise typer.BadParameter(f"unknown item {item}")
        for line in asyncio.run(takedown.take_down(s, db, item, marketplace or "poshmark")):
            print(escape(line))
        return
    if practice:
        if marketplace != "depop" or not url:
            raise typer.BadParameter("--practice is for Depop: --marketplace depop --url <listing>")
        print(asyncio.run(takedown.practice(s, _db(), url)))
        return
    if verify:
        if marketplace not in takedown.SITES or not url:
            raise typer.BadParameter("--verify needs --marketplace poshmark|depop|vinted and --url <listing>")
        print(asyncio.run(takedown.verify(s, _db(), marketplace, url)))
        return
    if not run:
        raise typer.BadParameter("say --run (do the pending take-downs), --verify (record a control) or --item <item> "
                                 "(the owner's take-down of one listing)")
    for line in asyncio.run(takedown.run_pending(s, _db())):
        print(escape(line))


@app.command()
def relist(item_id: str, marketplace: str = typer.Option(None, "--marketplace", help="poshmark | depop | vinted")) -> None:
    """Undo our take-down of an item where the site allows it (Poshmark: For Sale again). The owner's command — never
    automatic."""
    from thrift_agent.post import takedown
    s = settings()
    if not s.is_prod:
        raise typer.BadParameter("relisting runs on the Mac only (machine_role: prod)")
    for line in asyncio.run(takedown.relist(s, _db(), item_id, marketplace)):
        print(escape(line))


@app.command("sync")
def sync_cmd(push: bool = typer.Option(True, "--push/--no-push", help="send the changes now"),
             all_: bool = typer.Option(False, "--all", help="every item and listing again, not only the changed ones")
             ) -> None:
    """The items and listings changed since the last sync, and the kept calls, to thrift-api now."""
    from thrift_agent import sales
    db = _db()
    client = _api_or_exit()
    if all_:
        print(f"marked {db.seed_api_dirty(again=True)} items and listings to send again")
    try:
        n, m = sales.push(db, client), sales.flush(db, client)
    except sales.ApiError as e:
        print(f"[red]the API didn't answer[/]: {escape(str(e))} (nothing lost: it goes next time)")
        raise typer.Exit(1) from None
    print(f"synced {n} change{'s' if n != 1 else ''}, {m} kept call{'s' if m != 1 else ''}")


@api_app.command("ping")
def api_ping() -> None:
    """thrift-api's health and mode."""
    from thrift_agent import sales
    client = _api_or_exit()
    try:
        print(escape(json.dumps(client.get("/health"), indent=1)))
    except sales.ApiError as e:
        print(f"[red]not reachable[/]: {escape(str(e))}")
        raise typer.Exit(1) from None


catalogs_app = typer.Typer(help="The marketplaces' listing-form catalogs (data/*_catalog.json, WO30)")
app.add_typer(catalogs_app, name="catalogs")


@catalogs_app.command("check")
def catalogs_check() -> None:
    """Load and validate the Depop and Vinted catalogs (the startup check)."""
    from thrift_agent import catalogs as cats
    problems = cats.check()
    for mp, why in problems.items():
        if why:
            print(f"[red]{mp}[/]: {escape(why)}")
        else:
            c = cats.load(mp)
            print(f"[green]{mp}[/]: {len(c.categories)} categories")
    if any(problems.values()):
        raise typer.Exit(1)


@catalogs_app.command("refresh")
def catalogs_refresh(depop: bool = typer.Option(False, "--depop"), vinted: bool = typer.Option(False, "--vinted"),
                     ) -> None:
    """Re-read the catalogs from the Mac's logged-in Chrome (read-only APIs) and rewrite data/*_catalog.json in the
    same format; the diff goes to the ops chat. The running poster does it between listings."""
    from thrift_agent import crosslist as cl
    from thrift_agent.catalogs import refresh as cat_refresh
    s, db = settings(), _db()
    mps = [m for m, on in (("depop", depop), ("vinted", vinted)) if on] or ["depop", "vinted"]
    if ext := [m for m in mps if cl.driver(s, m) != "playwright"]:
        print(f"[yellow]not refreshed[/] {', '.join(ext)}: on the extension driver (WO32) the poster profile never "
              "visits the site — the saved catalogs stay")
        mps = [m for m in mps if m not in ext]
        if not mps:
            return
    if daily.poster_now(db).running:
        cat_refresh.request(db, mps)
        print(f"[green]asked[/] the running poster to refresh {', '.join(mps)}: the diff goes to the ops chat")
        return
    if not s.is_prod:
        raise typer.BadParameter("the dev machine never opens the marketplaces: run it on the Mac")
    for line in asyncio.run(cat_refresh.run_standalone(s, db, mps)):
        print(escape(line))


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
