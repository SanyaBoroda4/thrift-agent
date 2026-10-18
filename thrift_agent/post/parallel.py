"""Parallel posting (WO33 A1): three site workers instead of the item-by-item loop. An approved item goes to Poshmark,
Depop and Vinted at the same time — about the slowest site's minute, not the sum.

- Poshmark: its own thread with its own event loop, Playwright instance (the poster's Chrome profile) and database
  connection. Depop and Vinted: asyncio workers in the main loop, beside the extension's bridge (one job per site).
- Each worker, between its own listings: that site's pending take-downs first (`takedowns`, WO33 Part D), then the
  next approved item with a 'queued' row for it, in approval order — one at a time, inside the hours, under its own
  daily cap, a pause after each listing (invariant 6): Poshmark the schedule's human gap (150–420 s), Depop and Vinted
  their own `marketplaces.<m>.gap_seconds` (30–90 s — the owner, 2026-10-11; `crosslist.listing_gap`).
- A failure, a timeout, a login wall or a CAPTCHA on one site never stops the others: that site alone is stopped for
  the window (crosslist.block — Poshmark too, in parallel mode; the owner's PAUSE stays the brake for all).
- The cross rows are queued when the owner approves the price (`feed`), not after Poshmark is live.
- Unchanged: one click per publish, 'posting' at the go-ahead (extension) / before the form (Poshmark), never retried
  blind, the shop check, 3 attempts then skipped.
- The group's line for an item goes out once all its sites are settled (crosslist.announce, under one lock).
- WO41: a worker with no take-down and no listing to do makes its site's next due price drop (pricedrop.run_one); the
  housekeeping starts the stages that fell due and sends the day's ONE "🔻 Prices down" line once they're done.
`poster.parallel: false` keeps the sequential loop (runner.run's own)."""
from __future__ import annotations

import asyncio
import functools
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from thrift_agent import catalogs, crosslist, daily, notify, orders, power, pricedrop, sales
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads
from thrift_agent.post import runner
from thrift_agent.post.base import AccountBlocked, open_browser
from thrift_agent.scheduler import can_post, next_gap, windows

IDLE = 20.0            # a worker with nothing to do looks again after this many seconds
HOUSEKEEPING = 15.0    # the shared duties (heartbeat, feeding the cross rows, requests) run this often
PLAN_EVERY = 600.0     # WO41: the price-drop stages that fell due are looked for this often
ANNOUNCE_LOCK = threading.Lock()


def parallel_ok(s: Settings, ps: dict, once: bool) -> bool:
    """Parallel posting, unless turned off (`poster.parallel: false`), a one-shot run, or a cross site still on the
    Playwright driver (it would need Poshmark's Chrome profile, which the Poshmark worker holds)."""
    if once or not s.get("poster.parallel", True) or not any(mp in crosslist.CROSS for mp in ps):
        return False                                   # (one site alone: nothing to run side by side)
    return not any(getattr(p, "driver", "playwright") == "playwright" for mp, p in ps.items() if mp != "poshmark")


def delist_ready(site: str, poster) -> str | None:
    """None when the site's take-down control is recorded (WO32's gate), else why not — then a take-down is never
    tried there and the owner is asked to mark the item sold by hand."""
    if site == "poshmark":
        from thrift_agent.post import poshmark
        missing = sorted(poshmark.AVAILABILITY_NEEDS & poshmark.UNVERIFIED)
        return f"Poshmark's Not for Sale isn't recorded yet ({', '.join(missing)})" if missing else None
    from thrift_agent.post.ext_driver import DELIST_NEEDS, SELECTORS, unverified
    missing = sorted(DELIST_NEEDS.get(site, frozenset()) & unverified(site, getattr(poster, "selectors_path", SELECTORS)))
    what = {"vinted": "Hide", "depop": "Mark as sold"}.get(site, "take-down")
    return f"{crosslist.LABEL[site]}'s {what} isn't recorded yet ({', '.join(missing)})" if missing else None


def may_take_down(s: Settings, db: DB, site: str) -> bool:
    """Take-downs run in any hour — they keep a sold item from selling twice — and before any listing, but never with
    the owner's PAUSE (the brake for everything that touches a site), the lid closed (a maintenance wake would cut one
    off half-way), or the site stopped for the window by a login / CAPTCHA / block / check page (it would only meet
    the same wall). WO40: a site stopped for posting failures (three in a row) still takes down what sold."""
    return not s.flag_set("PAUSE") and power.lid_closed() is not True and not crosslist.account_blocked(db, site)


async def takedowns(site: str, poster, db: DB, ctx, sites: list[str] | None = None, shots: Path | None = None) -> bool:
    """The site's take-downs before its next listing (WO33 Part D): the API asked for every poster site at once, at
    most every `sales.TASKS_EVERY` (WO35: each worker had asked on each idle turn — three workers every 20 s, ~5,600
    calls on 2026-10-09 — and the API's log hit its 0.1 GB daily cap), then one done."""
    sales.fetch_tasks(db, sites or [site])
    return await sales.run_takedown(site, poster, db, ctx, ready=delist_ready, shots=shots)


async def listing_edit(s: Settings, db: DB, site: str, poster, ctx, sites: list[str], dry: bool) -> bool:
    """WO43: the site's next due listing edit (a department / category change), before a price drop."""
    from thrift_agent import edits
    try:
        return await edits.run_one(s, db, site, poster, ctx, sites, dry_poster=dry)
    except AccountBlocked as e:
        crosslist.block(db, site, str(e), e.page)
        return True
    except Exception as e:  # noqa: BLE001 — never the worker's end
        notify.say(f"❌ {crosslist.LABEL.get(site, site)} listing edit: {type(e).__name__}: {e}")
        return True


async def price_drop(s: Settings, db: DB, site: str, poster, ctx, sites: list[str], dry: bool) -> bool:
    """WO41: the site's next due price drop, on a turn with no take-down and no listing to do. A login / CAPTCHA page
    stops the site for the window, as a listing's would. True when it worked on one."""
    try:
        return await pricedrop.run_one(s, db, site, poster, ctx, sites, dry=dry)
    except AccountBlocked as e:
        crosslist.block(db, site, str(e), e.page)
        return True


def maybe_announce(s: Settings, db: DB, iid: str) -> str | None:
    """The item's group line (or its ops "Added:" line) once its sites are settled — one at a time across the
    workers, so a line never goes out twice."""
    with ANNOUNCE_LOCK:
        return crosslist.announce(s, db, iid)


def feed(s: Settings, db: DB) -> list[str]:
    """WO33: an approved item goes to every site at once — its Depop and Vinted rows are queued as soon as Poshmark's
    would publish (ready, the owner's price — WO33: never held by the gate), not after Poshmark is live. Returns the
    items fed."""
    from thrift_agent import pipeline
    cross = crosslist.enabled(s)
    if not cross:
        return []
    fed = []
    for it in db.items("ready"):
        batch = db.batch(it["batch_id"])
        if batch is not None and batch["status"] == "regroup":
            continue
        render = (loads(it["renders"]) or {}).get("poshmark")
        if not render or not runner.approved(it, render):
            continue
        if pipeline.settle_twin(s, db, it["id"]) and (db.item(it["id"]) or {"status": ""})["status"] != "ready":
            continue                                   # WO39: priced before the rule (set_price applies it now)
        missing = [mp for mp in cross if db.listing(it["id"], mp) is None]
        if missing and crosslist.queue(s, db, it["id"], missing, why="approved"):
            fed.append(it["id"])
    return fed


@dataclass
class State:
    """What the workers are doing, for the heartbeat and the idle check."""
    busy: dict[str, str | None] = field(default_factory=dict)        # site → the item in hand
    idle_turns: dict[str, int] = field(default_factory=dict)         # site → turns in a row with nothing to do
    lock: threading.Lock = field(default_factory=threading.Lock)

    def set(self, mp: str, iid: str | None) -> None:
        with self.lock:
            self.busy[mp] = iid
            self.idle_turns[mp] = 0 if iid else self.idle_turns.get(mp, 0)

    def idle(self, mp: str) -> None:
        with self.lock:
            self.busy[mp] = None
            self.idle_turns[mp] = self.idle_turns.get(mp, 0) + 1

    def first_busy(self) -> str | None:
        with self.lock:
            return next((iid for iid in self.busy.values() if iid), None)

    def all_idle(self, mps: list[str], turns: int = 2) -> bool:
        with self.lock:
            return all(not self.busy.get(mp) and self.idle_turns.get(mp, 0) >= turns for mp in mps)


async def _rest(flag, seconds: float) -> None:
    """Sleep, waking at once when `flag` (an asyncio.Event or a threading.Event) is set."""
    end = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < end and not flag.is_set():
        await asyncio.sleep(min(0.25, end - time.monotonic()))


async def run_parallel(s: Settings, db: DB, ps: dict, *, dry: bool, stage: str, force_dry: bool = False,
                       opener=open_browser, takedowns=None, until_idle: bool = False) -> None:
    """The poster with its three site workers (see the module). `takedowns(site, poster, db, ctx)` runs a site's pending
    take-downs before its next listing (WO33 Part D). `until_idle`: return once every worker has had nothing to do for
    two turns (the tests); the service never does."""
    stop = asyncio.Event()
    runner._install_stop(stop)
    halt = threading.Event()
    cross = [mp for mp in ps if mp in crosslist.CROSS]
    sites = [*(["poshmark"] if "poshmark" in ps else []), *cross]
    if takedowns is None and sales.api() is not None:     # the API is set up (.env): the sales' take-downs too
        takedowns = functools.partial(globals()["takedowns"], sites=sites, shots=shots_dir(s))
    bridge, watch = await runner.open_bridge(s, db, ps)
    started = f"Poster started ({f'DRY-RUN, {stage} stage' if dry else 'LIVE'}) — {', '.join(ps)} · parallel"
    notify.say(f"{started} · {datetime.now():%H:%M}" + (f"\n{runner.why_dry(s, force_dry)}" if dry and s.is_prod else ""))
    print(started)
    db.log(None, "poster_started", {"live": not dry, "stage": None if not dry else stage, "pid": os.getpid(),
                                    "parallel": True})
    if bad := {mp: why for mp, why in catalogs.check().items() if why}:
        notify.say("❗ cross-listing off: " + "; ".join(f"{mp}: {why}" for mp, why in bad.items()))
    state = State()
    thread = None
    if "poshmark" in ps:
        thread = threading.Thread(target=_poshmark_thread, name="poshmark-worker", daemon=True,
                                  args=(s, ps["poshmark"], dry, stage, halt, state, opener, takedowns))
        thread.start()
    awake = power.Awake()
    tasks = []
    try:
        await runner.reconcile_stale(s, db, {mp: ps[mp] for mp in cross}, None)   # Poshmark's: in its own worker
        tasks = [asyncio.create_task(_cross_worker(s, db, ps, mp, dry, stop, state, awake, takedowns)) for mp in cross]
        tasks.append(asyncio.create_task(_housekeeping(s, db, ps, cross, dry, stop, state, sites, until_idle)))
        while not stop.is_set():
            if thread is not None and not thread.is_alive() and not halt.is_set():
                notify.say("❌ the Poshmark worker stopped — Depop and Vinted go on; restart the poster for Poshmark")
                thread = None
            await _rest(stop, 1.0)
        print("stop requested — each worker finishes the listing in hand")
    finally:
        stop.set()
        halt.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        if thread is not None:
            await asyncio.to_thread(thread.join)
        awake.hold(False)
        daily.poster_beat(db, stopped=True, busy=None, next_at=None)
        await runner.close_bridge(bridge, watch)


# ---------------------------------------------------------------- Poshmark, in its own thread

def _poshmark_thread(s, poster, dry, stage, halt, state, opener, takedowns) -> None:
    try:
        asyncio.run(_poshmark_worker(s, poster, dry, stage, halt, state, opener, takedowns))
    except Exception as e:  # noqa: BLE001 — said in the ops chat; the main loop sees the thread gone
        notify.say(f"❌ Poshmark worker: {type(e).__name__}: {e}")


async def _poshmark_worker(s, poster, dry, stage, halt, state, opener, takedowns) -> None:
    db = DB(s.path("db"))                              # this thread's own connection
    ps = {"poshmark": poster}
    marketplaces = ["poshmark", *crosslist.enabled(s)]
    pw, ctx = await opener(s.path("chrome_profile"), s["schedule"]["timezone"])
    awake = power.Awake()
    failures = {"n": 0}
    try:
        await runner.reconcile_stale(s, db, ps, ctx)
        while not halt.is_set():
            try:
                worked = await _poshmark_turn(s, db, ps, ctx, dry, stage, state, awake, failures, marketplaces,
                                              takedowns)
            except Exception as e:  # noqa: BLE001 — a worker never dies of one bad turn
                notify.say(f"❌ Poshmark worker: {type(e).__name__}: {e}")
                worked = False
            gap = next_gap(s["schedule"]) if worked else IDLE
            started = time.monotonic()
            if not worked:
                awake.hold(False)
            if not halt.is_set():
                # WO38: the sold orders read between listings — on an idle turn or inside this pause, never longer
                waiting = functools.partial(_posh_waiting, s, db, dry)
                await _sweep(s, db, "poshmark",
                             lambda: orders.read_poshmark(ctx, yield_to=waiting), gap if worked else None)
                await _rest(halt, gap - (time.monotonic() - started))
    finally:
        awake.hold(False)
        state.idle("poshmark")
        for close in (ctx.close, pw.stop):
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass


async def _poshmark_turn(s, db, ps, ctx, dry, stage, state, awake, failures, marketplaces, takedowns) -> bool:
    """One Poshmark turn: requests, take-downs, then one listing. True when it listed (or tried to)."""
    await runner.serve_requests(s, db, ps, ctx)
    if takedowns is not None and may_take_down(s, db, "poshmark") and await takedowns("poshmark", ps["poshmark"], db, ctx):
        return True
    if crosslist.blocked(db, "poshmark"):
        state.idle("poshmark")
        return False
    hour_ago, midnight = windows(tz=s["schedule"]["timezone"])
    ok, _ = can_post(s, db.listed_since(hour_ago, "poshmark"), db.listed_since(midnight, "poshmark"))
    hold = s.flag_set("HOLD_UNSHIPPED")
    if not dry:
        runner._tell_held(s, db, ["poshmark"])
    job = runner.next_job(s, db, ["poshmark"], dry, allow_publish=not hold) if ok else None
    if job is not None and runner._paused(db):
        job = None
    if job is None:
        if (await listing_edit(s, db, "poshmark", ps["poshmark"], ctx, marketplaces, dry)
                or await price_drop(s, db, "poshmark", ps["poshmark"], ctx, marketplaces, dry)):
            return True
        state.idle("poshmark")
        return False
    iid, mp, render, mode = job
    if not db.claim_listing(iid, mp):
        return True
    state.set("poshmark", iid)
    awake.hold(True)
    t0, m0, k0 = time.time(), time.monotonic(), power.last_wake()
    try:
        out = await ps[mp].post(ctx, render, mode, dry, s.path("failed") / "shots", stage=stage)
    except AccountBlocked as e:                       # this site alone stops for the window; the others go on
        db.upsert_listing(iid, mp, status="queued", error=str(e))
        crosslist.block(db, mp, str(e), e.page)
        state.set("poshmark", None)
        return True
    except Exception as e:  # noqa: BLE001 — before the form: nothing submitted; Poshmark stops for the window
        err = f"{type(e).__name__}: {e}"
        db.upsert_listing(iid, mp, status="queued", error=err)
        crosslist.block(db, mp, f"failures: {err}")
        state.set("poshmark", None)
        return True
    seconds, slept = time.monotonic() - m0, power.slept_since(t0, k0, m0)
    requeued = False
    if slept:
        out, requeued = await runner.after_sleep(db, ps, ctx, iid, mp, render, out, t0)
    if not requeued:
        runner.record_outcome(db, iid, mp, render, out, marketplaces, stage,
                              bool(s.get("poster.notify_dry_runs", False)), seconds=seconds, slept=slept, s=s,
                              announce=False)
        maybe_announce(s, db, iid)
        failures["n"] = failures["n"] + 1 if out.status == "failed" else 0 if out.status != "skipped" else failures["n"]
        if failures["n"] >= int(s.get("poster.max_consecutive_failures", 3)):
            crosslist.block(db, mp, f"failures: {failures['n']} in a row: {out.error}")
            failures["n"] = 0
    state.set("poshmark", None)
    return True


# ---------------------------------------------------------------- Depop and Vinted

async def _cross_worker(s, db, ps, mp, dry, stop, state, awake, takedowns) -> None:
    while not stop.is_set():
        try:
            worked = await _cross_turn(s, db, ps, mp, dry, state, awake, takedowns)
        except Exception as e:  # noqa: BLE001 — a worker never dies of one bad turn
            notify.say(f"❌ {crosslist.LABEL[mp]} worker: {type(e).__name__}: {e}")
            state.set(mp, None)
            worked = False
        gap = crosslist.listing_gap(s, mp) if worked else IDLE                # Depop / Vinted: 30–90 s (the owner)
        started = time.monotonic()
        if not stop.is_set() and hasattr(ps[mp], "_job"):
            # WO38: the sold orders read between listings (the extension's "orders" job, its own window)
            await _sweep(s, db, mp, functools.partial(orders.read_extension, ps[mp], mp), gap if worked else None,
                         ps[mp])
        await _rest(stop, gap - (time.monotonic() - started))


async def _cross_turn(s, db, ps, mp, dry, state, awake, takedowns) -> bool:
    """One Depop / Vinted turn: take-downs, then one listing. True when it listed (or tried to)."""
    poster = ps[mp]
    reachable = getattr(poster, "available", lambda: True)()
    if takedowns is not None and reachable and may_take_down(s, db, mp) and await takedowns(mp, poster, db, None):
        return True
    # WO35 §4: an unconfirmed publish looked for again by the site's own data (quiet; any hour, never with the lid
    # closed or the site stopped)
    if (reachable and may_take_down(s, db, mp) and not crosslist.blocked(db, mp)
            and await runner.recheck_unconfirmed(s, db, ps, mp)):
        return True
    hours_ok, _ = can_post(s, 0, 0)
    if dry or not reachable or not hours_ok or crosslist.blocked(db, mp) or runner._paused(db):
        state.idle(mp)
        return False
    job = crosslist.next_job(s, db, mps=[mp])
    if job is None:
        if (await listing_edit(s, db, mp, poster, None, list(ps), dry)
                or await price_drop(s, db, mp, poster, None, list(ps), dry)):
            return True
        state.idle(mp)
        return False
    iid = job[0]
    state.set(mp, iid)
    awake.hold(True)
    try:
        await runner.run_cross(s, db, ps, None, iid, mp, dry=not crosslist.live(s, mp), hold=s.flag_set("HOLD_UNSHIPPED"))
    finally:
        state.set(mp, None)
    maybe_announce(s, db, iid)
    return True


# ---------------------------------------------------------------- WO38: the sold orders, between listings

async def _sweep(s, db, site: str, read, gap: float | None, poster=None) -> None:
    """The site's order sweep when due (orders.maybe_sweep: on an idle turn, or inside a pause after a listing that
    leaves room for it) — never the worker's end."""
    try:
        await orders.maybe_sweep(s, db, site, read, gap, poster)
    except Exception as e:  # noqa: BLE001 — logged; the sweep comes back next time
        db.log(None, "orders_failed", {"site": site, "error": f"{type(e).__name__}: {e}"[:300]})


def _posh_waiting(s, db, dry) -> bool:
    """A Poshmark take-down or listing that could start now: the sweep steps aside for it."""
    hour_ago, midnight = windows(tz=s["schedule"]["timezone"])
    ok, _ = can_post(s, db.listed_since(hour_ago, "poshmark"), db.listed_since(midnight, "poshmark"))
    return bool(sales.pending(db, "poshmark")) or (ok and runner.next_job(s, db, ["poshmark"], dry) is not None)


# ---------------------------------------------------------------- shared duties

async def _housekeeping(s, db, ps, cross, dry, stop, state, sites, until_idle) -> None:
    beat_live = not dry
    planned = None
    while not stop.is_set():
        try:
            daily.poster_beat(db, pid=os.getpid(), live=beat_live, busy=state.first_busy(), next_at=None,
                              paused=runner._paused(db), stopped=None)
            if cross:
                crosslist.new_window(s, db)
                if not dry:
                    feed(s, db)
                if not runner._paused(db):
                    await runner.serve_cross_requests(s, db, ps, None)
                    if asked := runner.refresh.take_requests(db):
                        await runner._refresh(s, db, None, asked)
                await runner.serve_requests(s, db, {mp: ps[mp] for mp in cross}, None)
            for iid in crosslist.waiting_lines(db):          # a line held by a site that was away: due now?
                maybe_announce(s, db, iid)
            if not dry and s.get("price_drop.enabled", True):  # WO41: the stages that fell due; the day's one line
                if planned is None or time.monotonic() - planned >= PLAN_EVERY:
                    pricedrop.plan(db)
                    planned = time.monotonic()
                pricedrop.announce(s, db)
            if until_idle and state.all_idle(sites) and _nothing_left(s, db, sites, dry):
                stop.set()
                return
        except Exception as e:  # noqa: BLE001 — never the poster's end
            notify.say(f"❌ poster housekeeping: {type(e).__name__}: {e}")
        await _rest(stop, HOUSEKEEPING)


def _nothing_left(s, db, sites, dry) -> bool:
    """No row any worker could still take now (the test runs' end): a site stopped for the window takes none."""
    if not dry and any(pricedrop.due_rows(db, m) and pricedrop.may_drop(s, db, m, dry) for m in sites):
        return False
    if ("poshmark" in sites and not crosslist.blocked(db, "poshmark")
            and runner.next_job(s, db, ["poshmark"], dry) is not None):
        return False
    return dry or crosslist.next_job(s, db, mps=[m for m in sites if m != "poshmark"]) is None


def shots_dir(s: Settings) -> Path:
    return s.path("failed") / "shots"
