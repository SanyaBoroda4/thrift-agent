"""The poster process: one browser, one item at a time, paced, and stoppable from the phone."""
from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timezone
from pathlib import Path

from thrift_agent import approve, notify
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads
from thrift_agent.post.base import STAGES, AccountBlocked, NeedsOwner, Outcome, Poster, keep_evidence, open_browser
from thrift_agent.post.depop import DepopPoster
from thrift_agent.post.poshmark import PoshmarkPoster
from thrift_agent.scheduler import can_post, next_gap, windows
from thrift_agent.schema import Render


DEV_BROWSER_MSG = ("machine_role is 'dev': the dev machine never touches the shop (CLAUDE.md). A dry-run still opens "
                   "Chrome, visits the create-listing page and uploads photos. Pass --allow-dev-browser to do that on "
                   "purpose; it stays a dry-run.")
HOLD_REASON = "unshipped orders — publish held (drafts and dry-runs still run)"
# last_error of a failed post whose final click (List This Item) happened but whose listing URL wasn't found: the
# listing may be live, so nothing re-posts it — not the poster, not `thrift requeue` (invariant 4).
UNCONFIRMED = "unconfirmed publish: "


def posters(s: Settings) -> dict[str, Poster]:
    out: dict[str, Poster] = {}
    for mp in ("poshmark", "depop"):
        if not s.get(f"marketplaces.{mp}.enabled", False):
            continue
        username = str(s.get(f"marketplaces.{mp}.username") or "").strip()
        if not username:
            raise ValueError(f"marketplaces.{mp}.username is empty — set it in private/settings.yaml "
                             "(the poster checks the closet page to detect a logged-out or restricted account)")
        out[mp] = PoshmarkPoster(username) if mp == "poshmark" else DepopPoster()
    return out


def next_job(s: Settings, db: DB, enabled: list[str], dry: bool,
             allow_publish: bool = True) -> tuple[str, str, Render, str] | None:
    """The next (item, marketplace, render, mode) to post, or None.

    `allow_publish=False` (HOLD_UNSHIPPED) skips jobs that would go live; drafts still run, and so does a dry-run of
    a publish-gated item, since a dry-run never submits."""
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
            if mode == "publish" and not allow_publish and not dry:
                continue        # ship first; the item stays 'ready' and is picked up once the hold is lifted
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


def _warn_draft(mp: str, iid: str, left: str | None) -> None:
    """A dry-run must leave no draft: say so at once, apart from the dry-run's own message."""
    if left:
        notify.say(f"\u26a0\ufe0f {mp}: {left} by the dry-run of {iid}. Delete it in the closet's Drafts; the "
                   "leave step (Cancel \u2192 Discard Changes) needs a look.")


def why_dry(s: Settings, force_dry: bool = False) -> str:
    """Why the poster loop runs dry on the Mac: it publishes only with poster.dry_run off AND
    poster.autopublish_confirmed on (both deliberate flips; the defaults keep it dry)."""
    if force_dry:
        return "dry-run: --dry-run"
    if s.get("poster.dry_run", True):
        return "dry-run: poster.dry_run is on"
    return "dry-run: poster.autopublish_confirmed is off (poster.dry_run alone doesn't publish)"


def record_outcome(db: DB, iid: str, mp: str, render: Render, out: Outcome, marketplaces: list[str],
                   stage: str = "form") -> None:
    """The post row, the event, the owner's message and the item's status for one Outcome.

    posted_at = when this row last hit the site. A dry-run fills the real form (uploads included), so it gets a stamp
    too and counts against the pacing caps in posted_since(). A cancelled supervised publish never reached the site:
    the row goes back to 'queued'. A failed publish after the final click without a URL is "unconfirmed": the listing
    may be live, so `thrift requeue` refuses it until the closet is checked by hand (invariant 4)."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    error = out.error
    if out.status == "failed" and out.clicked and not out.url:
        error = UNCONFIRMED + (error or "")
    status = "queued" if out.status == "cancelled" else out.status
    db.upsert_post(iid, mp, status=status, url=out.url, last_error=error if out.status != "cancelled" else out.note,
                   posted_at=stamp if out.status in ("posted", "drafted", "dryrun") else None)
    db.log(iid, f"post_{out.status}", {"mp": mp, "url": out.url, "error": error, "shot": out.screenshot,
                                       "note": out.note, "draft_left": out.draft_left, "clicked": out.clicked})
    _warn_draft(mp, iid, out.draft_left)
    note = f"\n{out.note}" if out.note else ""
    if out.status == "failed":
        notify.photo(Path(out.screenshot or ""), f"❌ {mp} failed ({iid}): {render.title}\n{error}{note}")
    elif out.status == "dryrun":
        notify.photo(Path(out.screenshot or ""),
                     f"🧪 dry-run {mp} ({stage}): {render.title} — ${render.price}{note}")
    elif out.status == "cancelled":
        notify.say(f"↩️ not published on {mp} ({iid}): {render.title}{note}")
    else:
        notify.say(f"✅ {out.status} on {mp}: {render.title} — ${render.price}\n{out.url or ''}{note}")
    _settle_item(db, iid, marketplaces)


def _settle_item(db: DB, iid: str, marketplaces: list[str]) -> None:
    """The item is done once every enabled marketplace holds it; 'drafted' until all of them went live."""
    statuses = [(db.post(iid, m) or {"status": ""})["status"] for m in marketplaces]
    if all(st in ("posted", "drafted") for st in statuses):
        db.set_item(iid, status="posted" if all(st == "posted" for st in statuses) else "drafted")


async def terminal_confirm(r: Render, panel: str) -> bool:
    """The supervised publish's last step: the Share Listing panel is open in the Chrome window, the owner types LIST."""
    print(f"\nReady to list on Poshmark: {r.title}\n  ${r.price} · {r.size or 'no size'} · {r.condition} "
          f"· SKU {r.sku}\n  The Share Listing panel is open in the Chrome window; Promote My Closet is off.")
    answer = await asyncio.to_thread(input, "Type LIST to publish (anything else cancels): ")
    return answer.strip() == "LIST"


async def publish_first(s: Settings, db: DB, iid: str, confirm=terminal_confirm) -> Outcome:
    """The supervised first publish (WO15): this one item on Poshmark, on prod, at the owner-approved price, with the
    owner typing LIST at the Share Listing panel. poster.dry_run is ignored for this one call; autopublish stays off.
    Fills, reads back and diffs as usual, presses List This Item exactly once, records everything after the click,
    finds and checks the listing, records the post. Never retried automatically."""
    if not s.is_prod:
        raise RuntimeError("the supervised publish runs on the Mac only (machine_role: prod); the dev machine never "
                           "touches the shop")
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    if it["status"] != "ready":
        raise ValueError(f"item {iid} is {it['status']}, not ready")
    renders = loads(it["renders"]) or {}
    if "poshmark" not in renders:
        raise ValueError(f"item {iid} has no Poshmark listing")
    render = Render.model_validate(renders["poshmark"])
    if not it["owner_price"] or render.price != int(it["owner_price"]):
        raise ValueError(f"item {iid} has no owner-approved price (approve it in Telegram or `thrift price`)")
    row = db.post(iid, "poshmark")
    if row and (row["status"] in ("posted", "posting", "drafted") or row["url"]):
        raise ValueError(f"poshmark: status {row['status']}{' with ' + row['url'] if row['url'] else ''} — it reached "
                         "the site: check the closet, never post it twice")
    if row and (row["last_error"] or "").startswith(UNCONFIRMED):
        raise ValueError("poshmark: an earlier List This Item may have gone live — check the closet, then "
                         f"`thrift mark-posted {iid} poshmark <url>`")
    if row and row["status"] == "failed":
        raise ValueError(f"poshmark: the last attempt failed — `thrift requeue {iid}` first (it checks that nothing "
                         "reached the site)")
    if s.flag_set("HOLD_UNSHIPPED"):
        raise ValueError(HOLD_REASON)
    hour_ago, midnight = windows(tz=s["schedule"]["timezone"])
    ok, why = can_post(s, db.posted_since(hour_ago), db.posted_since(midnight))
    if not ok:
        raise ValueError(f"not now: {why}")
    ps = posters(s)
    poster = ps.get("poshmark")
    if poster is None:
        raise ValueError("marketplaces.poshmark is not enabled")
    poster.confirm = confirm
    try:
        pw, ctx = await open_browser(s.path("chrome_profile"), s["schedule"]["timezone"])
    except Exception as e:  # noqa: BLE001 — nothing was claimed or touched yet
        raise RuntimeError(f"Chrome didn't open ({type(e).__name__}) — is the poster service still running? Stop it "
                           "first: bash deploy/services.sh stop") from e
    try:
        if not db.claim_post(iid, "poshmark", "publish"):     # 'posting' before the form opens (invariant 4)
            raise ValueError(f"poshmark: could not claim {iid} (another poster has it?)")
        try:
            out = await poster.post(ctx, render, "publish", False, s.path("failed") / "shots")
        except AccountBlocked as e:
            db.upsert_post(iid, "poshmark", status="queued", last_error=str(e))
            _halt(s, str(e), f"⛔ Poster paused: {e}\nFix it in the poster Chrome window, then delete the PAUSE file.")
            raise
        except NeedsOwner as e:
            db.upsert_post(iid, "poshmark", status="queued", last_error=f"needs owner: {e.question}")
            db.set_item(iid, status="needs_owner")
            approve.ask_owner(s, db, iid, e.question)
            raise
    finally:
        await ctx.close()
        await pw.stop()
    record_outcome(db, iid, "poshmark", render, out, list(ps))     # 'posted' once every enabled marketplace is
    return out


async def mark_posted(s: Settings, db: DB, iid: str, mp: str, url: str) -> str:
    """Record a listing that went live while its address wasn't found (a post row in "unconfirmed publish"): the
    owner found it in the closet and gives its address. Mac only (it opens the poster's Chrome profile, read only).
    The address must be a listing page of that marketplace (https://poshmark.com/listing/<slug>-<24 hex id>) that no
    other item holds, and the page must show the item's title and price; then the row is 'posted' with the URL, the
    item 'posted' once every enabled marketplace is, and Telegram hears "✅ confirmed live". Anything else is refused
    and nothing changes. Returns the canonical address."""
    if not s.is_prod:
        raise RuntimeError("mark-posted runs on the Mac only (machine_role: prod): it opens the poster's Chrome "
                           "profile to look at the listing")
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    row = db.post(iid, mp)
    if row is None or row["status"] != "failed" or row["url"] or not (row["last_error"] or "").startswith(UNCONFIRMED):
        state = "no post" if row is None else f"status {row['status']}" + (f" with {row['url']}" if row["url"] else "")
        raise ValueError(f"{mp}: only a post in 'unconfirmed publish' can be marked posted ({iid} has {state})")
    renders = loads(it["renders"]) or {}
    if mp not in renders:
        raise ValueError(f"item {iid} has no {mp} listing")
    render = Render.model_validate(renders[mp])
    ps = posters(s)
    poster = ps.get(mp)
    if poster is None:
        raise ValueError(f"marketplaces.{mp} is not enabled")
    address = poster.listing_address(url)
    if address is None:
        raise ValueError(f"not a {mp} listing address: {url!r} (expected e.g. https://poshmark.com/listing/"
                         "<title-words>-<24 hex id>)")
    if (other := db.conn.execute("SELECT item_id FROM posts WHERE url=? AND NOT (item_id=? AND marketplace=?)",
                                 (address, iid, mp)).fetchone()) is not None:
        raise ValueError(f"{address} is already recorded for item {other['item_id']}")
    try:
        pw, ctx = await open_browser(s.path("chrome_profile"), s["schedule"]["timezone"])
    except Exception as e:  # noqa: BLE001 — nothing was touched
        raise RuntimeError(f"Chrome didn't open ({type(e).__name__}) — is the poster service still running? Stop it "
                           "first: bash deploy/services.sh stop") from e
    shots = s.path("failed") / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    poster.shot = shots / f"{iid}-{mp}-{stamp}-confirm.png"
    page = None
    try:
        page = await ctx.new_page()
        await poster.verify_live(page, address, render)             # title and price on the page, else it raises
        await keep_evidence(page, poster.shot, {"item": iid, "url": address, "title": True, "price": True})
    except Exception as e:  # noqa: BLE001
        await keep_evidence(page, poster.shot, {"item": iid, "url": address, "error": f"{type(e).__name__}: {e}"})
        raise ValueError(f"{address} doesn't show this item ({type(e).__name__}: {e}); nothing changed") from None
    finally:
        await ctx.close()
        await pw.stop()
    # posted_at: when the listing went live, as near as the row knows it (the failed attempt's last update).
    db.upsert_post(iid, mp, status="posted", url=address, last_error=None, posted_at=row["updated_at"])
    db.log(iid, "post_confirmed", {"mp": mp, "url": address, "was": row["last_error"]})
    notify.say(f"✅ confirmed live on {mp}: {render.title} — ${render.price}\n{address}")
    _settle_item(db, iid, list(ps))
    return address


def _halt(s: Settings, reason: str, text: str) -> None:
    """Invariant 5 — stop, don't guess: PAUSE makes can_post() refuse until the seller deletes the file."""
    s.flag("PAUSE").write_text(f"{datetime.now():%F %T} {reason}\n", encoding="utf-8")
    notify.say(text)


def dry_run_stage(s: Settings, override: str | None = None) -> str:
    """poster.dry_run_stage (or the CLI's --stage): "form" fills, reads back and discards; "review" also presses Next
    and records the page after it. Neither ever presses the final publish button."""
    stage = str(override or s.get("poster.dry_run_stage", "form") or "form").strip().lower()
    if stage not in STAGES:
        raise ValueError(f"poster.dry_run_stage must be one of {', '.join(STAGES)}, not {stage!r}")
    return stage


async def run(s: Settings, db: DB, once: bool = False, force_dry: bool = False, allow_dev_browser: bool = False,
              stage: str | None = None) -> None:
    # Publishing for real takes two deliberate flips on the Mac: poster.dry_run off AND poster.autopublish_confirmed on.
    dry = (force_dry or not s.is_prod or s.get("poster.dry_run", True)
           or not s.get("poster.autopublish_confirmed", False))
    if not s.is_prod and not allow_dev_browser:
        raise RuntimeError(DEV_BROWSER_MSG)
    stage = dry_run_stage(s, stage)
    max_fail = int(s.get("poster.max_consecutive_failures", 3))
    ps = posters(s)
    stop = asyncio.Event()
    _install_stop(stop)
    pw, ctx = await open_browser(s.path("chrome_profile"), s["schedule"]["timezone"])
    notify.say(f"Poster started ({f'DRY-RUN, {stage} stage' if dry else 'LIVE'}) — {', '.join(ps)}"
               + (f"\n{why_dry(s, force_dry)}" if dry and s.is_prod else ""))
    failures = 0
    try:
        while True:
            if stop.is_set():
                print("stop requested — exiting between items")
                return
            hour_ago, midnight = windows(tz=s["schedule"]["timezone"])
            ok, why = can_post(s, db.posted_since(hour_ago), db.posted_since(midnight))
            # HOLD_UNSHIPPED (invariant 8) holds publishing only: drafts and dry-runs never reach a buyer, and the
            # flag can appear mid-run (n8n sees a late order), so it is re-read every time round the loop.
            hold = s.flag_set("HOLD_UNSHIPPED")
            job = next_job(s, db, list(ps), dry, allow_publish=not hold) if ok else None
            if job is None:
                if once:
                    print(f"nothing to do ({HOLD_REASON if ok and hold else why})")
                    return
                await _pause(stop, 60)
                continue

            iid, mp, render, mode = job
            if not db.claim_post(iid, mp, mode):    # another poster process took it, or its status moved under us
                continue
            try:
                out = await ps[mp].post(ctx, render, mode, dry, s.path("failed") / "shots", stage=stage)
            except AccountBlocked as e:
                db.upsert_post(iid, mp, status="queued", last_error=str(e))
                _halt(s, str(e), f"⛔ Poster paused: {e}\nFix it in the poster Chrome window, then delete the PAUSE file.")
                return
            except NeedsOwner as e:
                # A field only the owner can answer (a brand or category Poshmark's lists don't have). Nothing was
                # submitted, so 'queued' cannot double-post. The item leaves 'ready' (next_job takes only 'ready'),
                # so it waits until the owner's reply reprocesses it; the poster carries on with the others.
                # Not a failure for the circuit breaker: the form and the account are fine, the item is unusual.
                db.upsert_post(iid, mp, status="queued", last_error=f"needs owner: {e.question}")
                db.set_item(iid, status="needs_owner")
                approve.ask_owner(s, db, iid, e.question)
                db.log(iid, "needs_owner", {"mp": mp, "question": e.question})
                _warn_draft(mp, iid, e.draft_left)
                if once:
                    return
                await _pause(stop, next_gap(s["schedule"]))
                continue
            except Exception as e:  # noqa: BLE001
                # Poster.post() turns everything after the page opens into an Outcome, so an exception reaching
                # here came from before the form was touched — nothing was submitted, and 'queued' cannot
                # double-post. It is still an unknown failure mode, so pause rather than retry (invariant 5).
                err = f"{type(e).__name__}: {e}"
                db.upsert_post(iid, mp, status="queued", last_error=err)
                _halt(s, err, f"⛔ Poster paused: {mp} raised before the form ({err}).\nFix it, then delete the PAUSE file.")
                return

            record_outcome(db, iid, mp, render, out, list(ps), stage)

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
