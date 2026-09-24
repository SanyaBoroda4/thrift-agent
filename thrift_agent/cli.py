"""`thrift` command. Dev on Windows: init, process, confirm, eval, status. Prod on the Mac: run, poster."""
from __future__ import annotations

import asyncio
import json
import time
import traceback
from pathlib import Path

import typer
from rich import print
from rich.table import Table

from thrift_agent import notify, pipeline
from thrift_agent.config import settings
from thrift_agent.db import DB, loads

app = typer.Typer(no_args_is_help=True, add_completion=False)


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


def _guard(db, ref, fn, on_fail) -> None:
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        on_fail()
        db.log(ref, "error", traceback.format_exc())
        notify.say(f"❌ {ref}: {type(e).__name__}: {e}")


@app.command()
def run(interval: int = 15) -> None:
    """Watch the inbox and process batches/items forever (the worker service)."""
    s, db = settings(), _db()
    s.ensure_dirs()
    print(f"worker watching {s.path('inbox')}")
    while True:
        _tick(s, db)
        time.sleep(interval)


@app.command()
def process(folder: Path) -> None:
    """One-off: treat FOLDER as a shared batch (dev: point it at any folder of photos)."""
    s, db = settings(), _db()
    s.ensure_dirs()
    bid = pipeline.register(s, db, folder.resolve()) or db.conn.execute(
        "SELECT id FROM batches WHERE src_dir=?", (str(folder.resolve()),)).fetchone()[0]
    pipeline.process_batch(s, db, bid)
    b = db.batch(bid)
    print(f"batch {bid}: {b['status']}  sheet: {s.path('work') / bid / 'contact_sheet.png'}")
    if b["status"] == "split":
        _tick(s, db)


@app.command()
def confirm(batch_id: str, cmd: str = typer.Argument("ok")) -> None:
    """Accept or correct a batch split: ok | 12>2 | split 7 | merge 2 3"""
    s, db = settings(), _db()
    pipeline.confirm(s, db, batch_id, cmd)
    _tick(s, db)
    print(f"[green]split[/] {batch_id}")


@app.command()
def answer(item_id: str, note: str) -> None:
    """Answer a needs-info item, e.g.  thrift answer i_... "size 8, brand Vince" """
    s, db = settings(), _db()
    pipeline.answer(s, db, item_id, note)
    _tick(s, db)


@app.command()
def poster(once: bool = False, dry_run: bool = False) -> None:
    """Run the Chrome poster (the poster service on the Mac)."""
    from thrift_agent.post.runner import run as run_poster
    asyncio.run(run_poster(settings(), _db(), once=once, force_dry=dry_run))


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


@app.command()
def show(item_id: str) -> None:
    """Print an item's facts, price, gate and renders."""
    row = _db().item(item_id)
    print(json.dumps({k: loads(row[k]) for k in ("facts", "price", "gate", "renders")}, indent=1))


@app.command()
def harvest(max_orders: int = 0) -> None:
    """Pull sales + listing pages from the logged-in poster profile into data/harvest/."""
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
    from thrift_agent.eval import run_case, summarize
    s = settings()
    results = [run_case(s, c) for c in sorted(fixtures.iterdir()) if (c / "expected.yaml").exists()]
    for r in results:
        print(r)
    print(summarize(results))


if __name__ == "__main__":
    app()
