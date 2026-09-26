"""`thrift` command. Dev on Windows: init, process, confirm, eval, status. Prod on the Mac: run, poster."""
from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
from pathlib import Path

import typer
from rich import print
from rich.table import Table

from thrift_agent import approve, notify, pipeline
from thrift_agent.config import settings
from thrift_agent.db import DB, loads

app = typer.Typer(no_args_is_help=True, add_completion=False)
telegram_app = typer.Typer(help="Telegram bot helpers: setup (find the ids for .env) and test (send a message).")
app.add_typer(telegram_app, name="telegram")
RESEND_CHECK_SECONDS = 3600      # how often the worker looks for batches/items waiting longer than resend_after_hours


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
    n = 0
    for folder in pipeline.ready_folders(s):
        if pipeline.register(s, db, folder):
            n += 1
    for b in db.batches("new"):
        _guard(db, b["id"], lambda: pipeline.process_batch(s, db, b["id"]), lambda: db.set_batch(b["id"], status="failed"))
        n += 1
    for it in db.items("new"):
        _guard(db, it["id"], lambda: pipeline.process_item(s, db, it["id"]), lambda: db.set_item(it["id"], status="failed"))
        n += 1
    return n


def _safe_tick(s, db) -> int:
    """One worker iteration that never kills the service: an error while scanning the inbox (iCloud evicting a
    file mid-scan, a permissions hiccup) is logged and simply retried on the next tick."""
    try:
        return _tick(s, db)
    except Exception as e:  # noqa: BLE001
        db.log(None, "error", traceback.format_exc())
        notify.say(f"❌ worker tick: {type(e).__name__}: {e}")
        return 0


def _guard(db, ref, fn, on_fail) -> None:
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        on_fail()
        db.log(ref, "error", traceback.format_exc())
        notify.say(f"❌ {ref}: {type(e).__name__}: {e}")


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


def _worker_iteration(s, db, bot, interval: int, state: dict) -> None:
    """One turn of the worker: process the inbox, then wait — on Telegram's long poll when the bot is configured
    (replies and button presses arrive at once), otherwise a plain sleep. Every RESEND_CHECK_SECONDS, re-send what
    the owner has left waiting longer than telegram.resend_after_hours."""
    _safe_tick(s, db)
    if bot is None:
        time.sleep(interval)
        return
    _safe_poll(s, db, bot, int(s.get("telegram.poll_timeout", interval)))
    if time.monotonic() - state.get("last_resend", 0) >= RESEND_CHECK_SECONDS:
        state["last_resend"] = time.monotonic()
        try:
            approve.resend_pending(s, db)
        except Exception as e:  # noqa: BLE001
            db.log(None, "error", traceback.format_exc())
            print(f"[telegram] resend failed: {type(e).__name__}: {e}")


@app.command()
def run(interval: int = 15) -> None:
    """Watch the inbox, process batches/items and talk to the owner on Telegram, forever (the worker service)."""
    s, db = settings(), _db()
    s.ensure_dirs()
    if s.is_prod:
        notify.check(s)                                   # a silent Telegram is not an option on the Mac
    bot = approve.bot_for(s)
    print(f"worker watching {s.path('inbox')}" + (" — Telegram on" if bot else " — Telegram off (dev: messages print)"))
    state = {"last_resend": time.monotonic()}
    if bot:
        approve.resend_pending(s, db, force=True)        # Telegram keeps updates 24 h: whatever waited over a sleep
    while True:
        _worker_iteration(s, db, bot, interval, state)


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
    _tick_unless_worker(s, db)


@app.command()
def answer(item_id: str, note: str) -> None:
    """Answer a needs-info / needs-owner item, e.g.  thrift answer i_... "size 8, brand Vince" """
    s, db = settings(), _db()
    pipeline.answer(s, db, item_id, note)
    _tick_unless_worker(s, db)


@app.command()
def price(item_id: str, amount: int) -> None:
    """Approve or set the owner's price for an item (the CLI twin of the Telegram Approve/Change buttons)."""
    s, db = settings(), _db()
    status = pipeline.set_price(s, db, item_id, amount)
    print(f"[green]${amount}[/] set for {item_id} — status {status}")


@telegram_app.command("setup")
def telegram_setup() -> None:
    """Print the chat ids and user ids seen in recent updates, to fill TELEGRAM_CHAT_ID and
    TELEGRAM_ALLOWED_USER_IDS in .env. Send the bot a message (or /start in the group) first."""
    from thrift_agent.telegram import Bot
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise typer.BadParameter("TELEGRAM_BOT_TOKEN is not set in .env (create the bot with @BotFather first)")
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
def telegram_test() -> None:
    """Send a test message to TELEGRAM_CHAT_ID with the configured bot."""
    bot = approve.bot_for(settings())
    if bot is None:
        raise typer.BadParameter("Telegram is not configured: telegram.enabled plus TELEGRAM_BOT_TOKEN, "
                                 "TELEGRAM_CHAT_ID and TELEGRAM_ALLOWED_USER_IDS in .env")
    mid = bot.send_message("thrift-agent: test message — the bot can reach this chat.")
    print(f"[green]sent[/] message {mid} to chat {bot.chat_id}")


@app.command()
def requeue(item_id: str, marketplace: str = typer.Argument(None)) -> None:
    """Queue a failed or dry-run post again (only rows with NO listing URL: anything that reached the site is
    reconciled by hand, never re-posted). e.g.  thrift requeue i_...  |  thrift requeue i_... poshmark"""
    s, db = settings(), _db()
    done = pipeline.requeue(s, db, item_id, marketplace)
    print(f"[green]queued[/] {item_id}: {', '.join(done)}")


@app.command()
def poster(once: bool = False, dry_run: bool = False,
           allow_dev_browser: bool = typer.Option(False, "--allow-dev-browser",
                                                  help="Open Chrome against the live site on a dev machine "
                                                       "(still a dry-run). The dev machine never touches the shop "
                                                       "otherwise.")) -> None:
    """Run the Chrome poster (the poster service on the Mac)."""
    from thrift_agent.post.runner import run as run_poster
    s = settings()
    if s.is_prod:
        notify.check(s)
    asyncio.run(run_poster(s, _db(), once=once, force_dry=dry_run, allow_dev_browser=allow_dev_browser))


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
    for b in db.batches("needs_confirm"):
        print(f"[yellow]awaiting confirm[/] {b['id']}  →  thrift confirm {b['id']} ok")
    for it in db.items("awaiting_price"):
        print(f"[yellow]awaiting price[/] {it['id']}  →  thrift price {it['id']} <amount>")
    for it in db.items("needs_owner"):
        print(f"[yellow]needs owner[/] {it['id']}  →  thrift answer {it['id']} \"<answer>\"")


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
