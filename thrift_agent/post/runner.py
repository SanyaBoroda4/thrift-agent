"""The poster process: one browser, one item at a time, paced, and stoppable from the phone."""
from __future__ import annotations

import asyncio
import signal
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


async def _pause(stop: asyncio.Event, seconds: float) -> None:
    """Sleep between items, but wake at once when a stop was requested."""
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except TimeoutError:
        pass


def _install_stop(stop: asyncio.Event) -> None:
    """SIGTERM (`launchctl kickstart -k` on deploy) and SIGINT finish the current item, then exit at the top
    of the loop — never mid-form, which would orphan a 'posting' row or leave a live listing unrecorded."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):   # Windows loops have no signal handlers
            pass


def _halt(s: Settings, reason: str, text: str) -> None:
    """Invariant 5 — stop, don't guess: PAUSE makes can_post() refuse until the seller deletes the file."""
    s.flag("PAUSE").write_text(f"{datetime.now():%F %T} {reason}\n", encoding="utf-8")
    notify.say(text)


async def run(s: Settings, db: DB, once: bool = False, force_dry: bool = False) -> None:
    dry = force_dry or not s.is_prod or s.get("poster.dry_run", True)
    max_fail = int(s.get("poster.max_consecutive_failures", 3))
    ps = posters(s)
    stop = asyncio.Event()
    _install_stop(stop)
    pw, ctx = await open_browser(s.path("chrome_profile"), s["schedule"]["timezone"])
    notify.say(f"Poster started ({'DRY-RUN' if dry else 'LIVE'}) — {', '.join(ps)}")
    failures = 0
    try:
        while True:
            if stop.is_set():
                print("stop requested — exiting between items")
                return
            hour_ago, midnight = windows(tz=s["schedule"]["timezone"])
            ok, why = can_post(s, db.posted_since(hour_ago), db.posted_since(midnight))
            job = next_job(s, db, list(ps), dry) if ok else None
            if job is None:
                if once:
                    print(f"nothing to do ({why})")
                    return
                await _pause(stop, 60)
                continue

            iid, mp, render, mode = job
            if not db.claim_post(iid, mp, mode):    # another poster process took it, or its status moved under us
                continue
            try:
                out = await ps[mp].post(ctx, render, mode, dry, s.path("failed") / "shots")
            except AccountBlocked as e:
                db.upsert_post(iid, mp, status="queued", last_error=str(e))
                _halt(s, str(e), f"⛔ Poster paused: {e}\nFix it in the poster Chrome window, then delete the PAUSE file.")
                return
            except Exception as e:  # noqa: BLE001
                # Poster.post() turns everything after the page opens into an Outcome, so an exception reaching
                # here came from before the form was touched — nothing was submitted, and 'queued' cannot
                # double-post. It is still an unknown failure mode, so pause rather than retry (invariant 5).
                err = f"{type(e).__name__}: {e}"
                db.upsert_post(iid, mp, status="queued", last_error=err)
                _halt(s, err, f"⛔ Poster paused: {mp} raised before the form ({err}).\nFix it, then delete the PAUSE file.")
                return

            # posted_at = when this row last hit the site. A dry-run fills the real form (uploads included),
            # so it gets a stamp too and counts against the pacing caps in posted_since().
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            db.upsert_post(iid, mp, status=out.status, url=out.url, last_error=out.error,
                           posted_at=stamp if out.status in ("posted", "drafted", "dryrun") else None)
            db.log(iid, f"post_{out.status}", {"mp": mp, "url": out.url, "error": out.error, "shot": out.screenshot})
            if out.status == "failed":
                notify.photo(Path(out.screenshot or ""), f"❌ {mp} failed ({iid}): {render.title}\n{out.error}")
            elif out.status == "dryrun":
                notify.photo(Path(out.screenshot or ""), f"🧪 dry-run {mp}: {render.title} — ${render.price}")
            else:
                notify.say(f"✅ {out.status} on {mp}: {render.title} — ${render.price}\n{out.url or ''}")

            if all((db.post(iid, m) or {"status": ""})["status"] in ("posted", "drafted") for m in ps):
                db.set_item(iid, status="posted")

            # Circuit breaker: N failures in a row means the form, the account or the network changed, not
            # the items. Every further attempt is 16 uploads of noise on the account, so stop and ask.
            failures = failures + 1 if out.status == "failed" else 0
            if failures >= max_fail:
                _halt(s, f"{failures} consecutive failures: {out.error}",
                      f"⛔ Poster paused after {failures} consecutive failures: {out.error}\n"
                      f"Fix it, then delete the PAUSE file.")
                return
            if once:
                return
            await _pause(stop, next_gap(s["schedule"]))
    finally:
        await ctx.close()
        await pw.stop()
