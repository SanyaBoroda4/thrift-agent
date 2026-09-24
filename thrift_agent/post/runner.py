"""The poster process: one browser, one item at a time, paced, and stoppable from the phone."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from thrift_agent import notify
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads
from thrift_agent.post.base import AccountBlocked, Poster, open_browser
from thrift_agent.post.depop import DepopPoster
from thrift_agent.post.poshmark import PoshmarkPoster
from thrift_agent.scheduler import can_post, next_gap, windows
from thrift_agent.schema import Render


def posters(s: Settings) -> dict[str, Poster]:
    out: dict[str, Poster] = {}
    mps = s["marketplaces"]
    if mps["poshmark"]["enabled"]:
        out["poshmark"] = PoshmarkPoster(s.get("marketplaces.poshmark.username", ""))
    if mps["depop"]["enabled"]:
        out["depop"] = DepopPoster()
    return out


def next_job(s: Settings, db: DB, enabled: list[str], dry: bool) -> tuple[str, str, Render, str] | None:
    for it in db.items("ready"):
        gate = loads(it["gate"]) or {}
        renders = loads(it["renders"]) or {}
        for mp in enabled:
            if mp not in renders:
                continue
            post = db.post(it["id"], mp)
            if post and post["status"] in ("posted", "drafted", "posting", "failed"):
                continue        # 'posting' after a crash = check the closet by hand, never re-post blindly
            if post and post["status"] == "dryrun" and dry:
                continue
            autop = s.get(f"marketplaces.{mp}.autopublish", False) and s.is_prod
            mode = "publish" if gate.get("decision") == "publish" and autop else "draft"
            return it["id"], mp, Render.model_validate(renders[mp]), mode
    return None


async def run(s: Settings, db: DB, once: bool = False, force_dry: bool = False) -> None:
    dry = force_dry or not s.is_prod or s.get("poster.dry_run", True)
    ps = posters(s)
    pw, ctx = await open_browser(s.path("chrome_profile"), s["schedule"]["timezone"])
    notify.say(f"Poster started ({'DRY-RUN' if dry else 'LIVE'}) — {', '.join(ps)}")
    try:
        while True:
            hour_ago, midnight = windows(tz=s["schedule"]["timezone"])
            ok, why = can_post(s, db.posted_since(hour_ago), db.posted_since(midnight))
            job = next_job(s, db, list(ps), dry) if ok else None
            if job is None:
                if once:
                    print(f"nothing to do ({why})")
                    return
                await asyncio.sleep(60)
                continue

            iid, mp, render, mode = job
            db.upsert_post(iid, mp, status="posting", mode=mode, attempts=(db.post(iid, mp) or {"attempts": 0})["attempts"] + 1)
            try:
                out = await ps[mp].post(ctx, render, mode, dry, s.path("failed") / "shots")
            except AccountBlocked as e:
                db.upsert_post(iid, mp, status="queued", last_error=str(e))
                s.flag("PAUSE").write_text(f"{datetime.now():%F %T} {e}\n")
                notify.say(f"⛔ Poster paused: {e}\nFix it in the poster Chrome window, then delete the PAUSE file.")
                return

            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            db.upsert_post(iid, mp, status=out.status, url=out.url, last_error=out.error,
                           posted_at=stamp if out.status in ("posted", "drafted") else None)
            db.log(iid, f"post_{out.status}", {"mp": mp, "url": out.url, "error": out.error, "shot": out.screenshot})
            if out.status == "failed":
                notify.photo(Path(out.screenshot), f"❌ {mp} failed ({iid}): {render.title}\n{out.error}")
            elif out.status == "dryrun":
                notify.photo(Path(out.screenshot), f"🧪 dry-run {mp}: {render.title} — ${render.price}")
            else:
                notify.say(f"✅ {out.status} on {mp}: {render.title} — ${render.price}\n{out.url or ''}")

            if all((db.post(iid, m) or {"status": ""})["status"] in ("posted", "drafted") for m in ps):
                db.set_item(iid, status="posted")
            if once:
                return
            await asyncio.sleep(next_gap(s["schedule"]))
    finally:
        await ctx.close()
        await pw.stop()
