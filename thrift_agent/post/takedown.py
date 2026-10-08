"""Take-downs from the command line (WO33 Part D), with the poster service stopped — it holds Poshmark's Chrome profile
and the extension bridge's port; the running poster does the same between listings by itself (parallel.takedowns).

- `thrift delist --run`: the pending take-downs fetched from thrift-api and done now, site by site.
- `thrift delist --verify --marketplace m --url <listing>`: the take-down control recorded WITHOUT using it — the
  listing (its edit page on Poshmark) opened, Not for Sale / Mark as sold / Hide looked for, the page and its picture
  kept and sent to the ops chat; nothing clicked, nothing changed. The control is marked verified from that evidence.
- `thrift relist <item> [--marketplace m]`: our reversible take-down undone where the site allows it — Poshmark's
  Availability back to For Sale; Depop and Vinted are relisted by hand until their controls are recorded. Owner-run
  only, never automatic."""
from __future__ import annotations

from pathlib import Path

from thrift_agent import crosslist, notify, sales
from thrift_agent.config import Settings
from thrift_agent.db import DB
from thrift_agent.post import parallel, runner
from thrift_agent.post.base import open_browser

SITES = ("poshmark", "depop", "vinted")


async def _open(s: Settings, sites: list[str]):
    """(posters, bridge, Playwright, context) for these sites: the bridge for the extension sites (the poster stopped:
    its port), Poshmark's Chrome profile for Poshmark."""
    ps = {mp: p for mp, p in runner.posters(s).items() if mp in sites}
    bridge = pw = ctx = None
    ext = {mp: p for mp, p in ps.items() if getattr(p, "driver", "playwright") == "extension"}
    if ext:
        from thrift_agent import bridge as bridge_mod
        try:
            bridge, _ = await runner.open_bridge(s, None, ext, watch=False, strict=True)
        except bridge_mod.BridgeError as e:
            raise RuntimeError(f"the extension bridge didn't start: {e}") from None
        await runner.connect_extension(bridge, next(iter(ext.values())))
    if "poshmark" in ps:
        try:
            pw, ctx = await open_browser(s.path("chrome_profile"), s["schedule"]["timezone"])
        except Exception as e:  # noqa: BLE001
            await runner.close_bridge(bridge, None)
            raise RuntimeError(f"Chrome didn't open ({type(e).__name__}) — is the poster service still running? Stop it "
                               "first: bash deploy/services.sh stop poster") from e
    return ps, bridge, pw, ctx


async def _close(bridge, pw, ctx) -> None:
    if ctx is not None:
        await ctx.close()
        await pw.stop()
    await runner.close_bridge(bridge, None)


async def run_pending(s: Settings, db: DB, sites: list[str] | None = None) -> list[str]:
    """The pending take-downs, done now (the poster stopped). Returns one line per take-down handled."""
    sites = [mp for mp in (sites or SITES) if mp in runner.posters(s)]
    sales.fetch_tasks(db, sites, force=True)
    todo = sorted({r["marketplace"] for r in sales.pending(db)} & set(sites))
    if not todo:
        return ["no take-downs pending"]
    ps, bridge, pw, ctx = await _open(s, todo)
    lines = []
    try:
        for mp in todo:
            while sales.pending(db, mp):
                before = {r["id"] for r in sales.pending(db, mp)}
                if not await parallel.takedowns(mp, ps[mp], db, ctx):
                    break
                for r in db.conn.execute(f"SELECT * FROM takedowns WHERE id IN ({','.join('?' * len(before))})",
                                         tuple(before)).fetchall():
                    if r["status"] != "pending":
                        lines.append(f"{crosslist.LABEL[mp]}: {r['title'] or r['item_id']} — {r['status']}"
                                     + (f" ({r['error']})" if r["error"] else ""))
    finally:
        await _close(bridge, pw, ctx)
    return lines


async def verify(s: Settings, db: DB, mp: str, url: str) -> str:
    """The take-down control looked for on `url` — never used. Its picture goes to the ops chat."""
    ps, bridge, pw, ctx = await _open(s, [mp])
    shots = s.path("failed") / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    try:
        poster = ps[mp]
        if mp == "poshmark":
            found = await poster.set_availability(ctx, url, False, probe=True, shots=shots)
            shot, text = str(poster.shot), None
        else:
            poster.shot = shots / f"probe-{mp}.png"
            out = await poster.probe_delist(url)
            found, shot, text = out["found"], out["shot"], out.get("text")
    finally:
        await _close(bridge, pw, ctx)
    what = {"poshmark": "Availability (Not For Sale)", "depop": "Mark as sold", "vinted": "Hide"}[mp]
    line = (f"🔎 {crosslist.LABEL[mp]} take-down control {'found' if found else 'NOT found'}: {what}"
            + (f" — {text!r}" if text else "") + f" on {url} (nothing clicked)")
    notify.ops_photo(Path(shot), line)
    db.log(None, "takedown_probe", {"mp": mp, "url": url, "found": bool(found), "text": text, "shot": shot})
    return line


async def practice(s: Settings, db: DB, url: str) -> str:
    """The owner's practice run of Depop's delete (WO33): the bin, the window recorded, Cancel, still live — never
    Delete listing. Its picture goes to the ops chat."""
    ps, bridge, pw, ctx = await _open(s, ["depop"])
    shots = s.path("failed") / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    try:
        poster = ps["depop"]
        poster.shot = shots / "practice-depop.png"
        out = await poster.practice_delete(url)
    finally:
        await _close(bridge, pw, ctx)
    w = out["window"]
    line = (f"🧪 Depop delete, practice (Cancel pressed, never Delete listing): the window "
            f"\"{w.get('title') or '?'}\" — buttons {', '.join(w.get('buttons') or []) or '?'}; "
            f"{'closed' if out['closed'] else 'NOT closed'}; the listing afterwards: {out['state_after']}")
    notify.ops_photo(Path(out["shot"]).with_name("practice-depop-delete-window.png"), line)
    db.log(None, "depop_delete_practice", {"url": url, **{k: v for k, v in out.items() if k != "shot"}})
    return line


async def relist(s: Settings, db: DB, iid: str, mp: str | None = None) -> list[str]:
    """Our take-down undone where the site allows it (the owner's command): Poshmark's Availability back to For Sale;
    Depop and Vinted by hand until their controls are recorded."""
    rows = [r for r in db.listings_for(iid) if r["status"] == "delisted" and (mp is None or r["marketplace"] == mp)]
    if not rows:
        return [f"{iid}: nothing taken down{' on ' + mp if mp else ''}"]
    lines = []
    if any(r["marketplace"] == "poshmark" for r in rows):
        ps, bridge, pw, ctx = await _open(s, ["poshmark"])
        try:
            row = next(r for r in rows if r["marketplace"] == "poshmark")
            if why := parallel.delist_ready("poshmark", ps["poshmark"]):
                lines.append(f"Poshmark: by hand — {why}")
            else:
                ok = await ps["poshmark"].set_availability(ctx, row["url"], True, shots=s.path("failed") / "shots")
                if ok:
                    db.upsert_listing(iid, "poshmark", status="posted")
                lines.append(f"Poshmark: {'For Sale again' if ok else 'not done'} — {row['url']}")
        finally:
            await _close(bridge, pw, ctx)
    for r in rows:
        if r["marketplace"] != "poshmark":
            lines.append(f"{crosslist.LABEL[r['marketplace']]}: relist it by hand in the app (its control isn't "
                         f"recorded yet) — {r['url']}")
    db.log(iid, "relist", {"lines": lines})
    return lines
