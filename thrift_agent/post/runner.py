"""The poster process: one browser, one item at a time, paced, and stoppable from the phone.

The daily window (WO28): it starts no listing with the lid closed or on a low battery, keeps the Mac from idle-sleeping
while it has listings to publish, says what it is doing (daily.poster_beat) and, after the Mac slept in the middle of
a listing, looks for it in the closet before calling it "unconfirmed"."""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thrift_agent import approve, brands, catalogs, crosslist, daily, health, notify, pipeline, postcheck, power
from thrift_agent import bridge as bridge_mod
from thrift_agent.brain.copy import NEGATIVE_WORDS, STYLE_HEMS, USED, USED_CLAIMS
from thrift_agent.catalogs import CatalogError, refresh
from thrift_agent.catalogs.common import ItemView, MappingError
from thrift_agent.config import ROOT, Settings
from thrift_agent.db import DB, listing_id_from, loads
from thrift_agent.post.base import STAGES, AccountBlocked, Outcome, Poster, keep_evidence, open_browser
from thrift_agent.post import poshmark
from thrift_agent.post.depop import DepopPoster
from thrift_agent.post.depop_api import DepopApiPoster
from thrift_agent.post.ext_driver import ExtensionPoster
from thrift_agent.post.poshmark import PoshmarkPoster
from thrift_agent.post.vinted import VintedPoster
from thrift_agent.scheduler import can_post, next_gap, windows
from thrift_agent.schema import Render


DEV_BROWSER_MSG = ("machine_role is 'dev': the dev machine never touches the shop (CLAUDE.md). A dry-run still opens "
                   "Chrome, visits the create-listing page and uploads photos. Pass --allow-dev-browser to do that on "
                   "purpose; it stays a dry-run.")
HOLD_REASON = "unshipped orders — publish held (drafts and dry-runs still run)"
# error of a failed post whose final click (List This Item) happened but whose listing URL wasn't found: the
# listing may be live, so nothing re-posts it — not the poster, not `thrift requeue` (invariant 4).
UNCONFIRMED = "unconfirmed publish: "
SKIPPED = "skipped: "            # error of an item the form couldn't take (a required field): requeue it once fixed
SLEPT = "interrupted by sleep: "   # error of a listing the Mac slept through before its final click: requeued
SLEEP_RETRIES = 3                  # ... at most this many attempts in all; then it is an ordinary failure
PRESSED = {"poshmark": ("List", "closet"), "depop": ("Post", "shop"), "vinted": ("Upload", "closet")}
LEGACY_AFTER_S = 6 * 3600   # WO53: an unconfirmed row with no verifying state, older than this, is never re-posted


def posters(s: Settings) -> dict[str, Poster]:
    """Poshmark (its closet name is required: the account check reads it) and the cross-list marketplaces that are on
    and whose catalog loads (WO30; their shop name is optional: only the closet check after an interrupted publish
    needs it), each by its driver (WO32: the extension by default; WO30's Playwright poster; Depop's API stub). An
    extension poster gets its bridge from open_bridge()."""
    out: dict[str, Poster] = {}
    if s.get("marketplaces.poshmark.enabled", False):
        username = str(s.get("marketplaces.poshmark.username") or "").strip()
        if not username:
            raise ValueError("marketplaces.poshmark.username is empty — set it in private/settings.yaml "
                             "(the poster checks the closet page to detect a logged-out or restricted account)")
        out["poshmark"] = PoshmarkPoster(username, brands.for_settings(s))
    for mp in crosslist.enabled(s):
        shop = str(s.get(f"marketplaces.{mp}.shop") or s.get(f"marketplaces.{mp}.username") or "")
        driver = crosslist.driver(s, mp)
        if driver == "extension":
            out[mp] = ExtensionPoster(mp, shop, brands.for_settings(s))     # WO36: the alias file on both sites
        elif driver == "api":
            out[mp] = DepopApiPoster(shop)
        else:
            out[mp] = DepopPoster(shop, brands.for_settings(s)) if mp == "depop" else VintedPoster(shop)
    from thrift_agent import heal          # WO52 §2: the learned selectors beside the database, the model that heals
    learned, model = heal.store(s), heal.model_for(s)
    for p in out.values():
        if hasattr(p, "learned"):
            p.learned, p.heal_model = learned, model
    return out


def uses_browser(ps: dict) -> bool:
    """Does any poster need the Playwright Chrome profile (Poshmark, a Playwright-driven Depop / Vinted)?"""
    return any(getattr(p, "driver", "playwright") == "playwright" for p in ps.values())


def ready_cross(ps: dict, cross: list[str]) -> list[str]:
    """The cross-list marketplaces whose poster can take a job now: an extension poster only while the extension is
    connected (WO32) — its rows wait meanwhile, and the bridge's watch tells the owner."""
    return [mp for mp in cross if mp in ps and getattr(ps[mp], "available", lambda: True)()]


EXT_MISSING_SAID = "ext_missing_said"     # kv: the window the "extension hasn't checked in" line was said in
THRIFT_CHROME = "com.thrift.chrome-cross"          # its LaunchAgent (deploy/com.thrift.chrome-cross.plist)
THRIFT_CHROME_DIR = "~/thrift/chrome-cross"        # its own profile: the one --user-data-dir that is the Thrift Chrome
QUIT_WAIT = 30.0                                   # WO54 §4: SIGTERM, then this long for Chrome's own shutdown


def start_thrift_chrome(s: Settings) -> str:
    """`services.sh start chrome` (the Thrift Chrome's LaunchAgent, WO32; WO54 §4: its installed plist loaded first,
    then `launchctl kickstart` — RunAtLoad false, it runs only when asked): on the Mac only."""
    if not s.is_prod or sys.platform != "darwin":
        return "not on the Mac: the Thrift Chrome isn't started here"
    try:
        r = subprocess.run(["bash", str(ROOT / "deploy" / "services.sh"), "start", "chrome"], capture_output=True,
                           text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return f"{type(e).__name__}: {e}"
    return (r.stdout + r.stderr).strip()[-300:]


def restart_thrift_chrome(s: Settings) -> str:
    """WO49: the Thrift Chrome restarted (`launchctl kickstart -k` of its LaunchAgent: WO54 §4, its launch script sets
    the last session aside — nothing comes back) — it can run with its extension's worker asleep. On the Mac only."""
    if not s.is_prod or sys.platform != "darwin":
        return "not on the Mac: the Thrift Chrome isn't restarted here"
    try:
        r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{THRIFT_CHROME}"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return f"{type(e).__name__}: {e}"
    return (r.stdout + r.stderr).strip()[-300:] or "restarted"


def _args_of(pid: int) -> str:
    try:
        return subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def thrift_chrome_pid() -> int | None:
    """WO54 §4: the Thrift Chrome's main process — its LaunchAgent's pid, else the one process started some other way
    — only when that process's own arguments carry the Thrift profile (`--user-data-dir=~/thrift/chrome-cross`) and
    it is no helper (`--type=`): never the owner's own Chrome, never another profile. None when it isn't running."""
    mark = f"--user-data-dir={os.path.expanduser(THRIFT_CHROME_DIR)}"
    ours = re.compile(re.escape(mark) + r"(\s|$)")
    pids: list[int] = []
    try:
        r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{THRIFT_CHROME}"], capture_output=True,
                           text=True, timeout=10)
        if m := re.search(r"^\s*pid = (\d+)\s*$", r.stdout or "", re.M):
            pids.append(int(m[1]))
    except (OSError, subprocess.SubprocessError):
        pass
    if not pids:
        try:
            r = subprocess.run(["pgrep", "-f", "--", mark], capture_output=True, text=True, timeout=10)
            pids = [int(x) for x in (r.stdout or "").split() if x.isdigit()]
        except (OSError, subprocess.SubprocessError):
            pids = []
    for pid in pids:
        args = _args_of(pid)
        if ours.search(args) and "--type=" not in args:
            return pid
    return None


def quit_thrift_chrome(s: Settings, wait: float = QUIT_WAIT) -> str:
    """WO54 §4: the Thrift Chrome quit the way Chrome quits — SIGTERM to its own main process (thrift_chrome_pid: matched
    by its --user-data-dir), then up to `wait` seconds for it to shut down normally. Its LaunchAgent has no KeepAlive:
    nothing starts it again until the next job. Never a SIGKILL, never another Chrome (no app quit by name: the owner's
    Chrome is the same app). On the Mac only. Runs in a thread (the bridge's watch)."""
    if not s.is_prod or sys.platform != "darwin":
        return "not on the Mac: the Thrift Chrome isn't quit here"
    pid = thrift_chrome_pid()
    if pid is None:
        return "not running"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "not running"
    except OSError as e:
        return f"{type(e).__name__}: {e}"
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return f"quit (pid {pid})"
        except OSError:
            break
        time.sleep(0.5)
    return f"asked to quit (pid {pid}); still running after {wait:.0f} s — a page may have asked to stay"


def chrome_waker(s: Settings):
    """WO54 §4: what starts the Thrift Chrome when a job comes and no extension is there — on the Mac only (None
    elsewhere, and in the test suite: tests/conftest.py). It says whether the Chrome was running when asked (the
    bridge tells a Chrome that vanished in the middle of a job from one quit on purpose)."""
    if not s.is_prod or sys.platform != "darwin":
        return None

    def wake(why: str) -> dict:
        running = thrift_chrome_running()
        return {"out": start_thrift_chrome(s), "was_running": running}
    return wake


async def open_bridge(s: Settings, db: DB | None, ps: dict, watch: bool = True, strict: bool = False):
    """WO32: the bridge for the extension-driven marketplaces, on 127.0.0.1:8765, its token from ~/thrift/var/ext_token;
    with `watch`, the 2-minute / 5-minute watch for an extension that doesn't check in. A bridge that can't start (no
    token, the port taken) turns those marketplaces off for this run, with an ops line — Poshmark goes on (`strict`:
    the BridgeError is raised instead, for the CLI to say). Returns (the bridge or None, the watch task or None)."""
    ext = {mp: p for mp, p in ps.items() if getattr(p, "driver", "") == "extension"}
    if not ext:
        return None, None
    try:
        pace = str(s.get("ext.pace", "fast") or "fast").lower()
        b = await bridge_mod.Bridge(bridge_mod.read_token(bridge_mod.token_path(s)),
                                    port=int(s.get("bridge.port", bridge_mod.PORT)),   # tests: 0, any port
                                    pace=pace if pace in ("fast", "human") else "fast").start()
    except bridge_mod.BridgeError as e:
        if strict:
            raise
        for mp in ext:
            ps.pop(mp, None)
        notify.say(f"❗ {', '.join(crosslist.LABEL[mp] for mp in ext)} off for this run: {e}")
        return None, None
    for p in ext.values():
        p.bridge = b
    b.waker = chrome_waker(s)                    # WO54 §4: the Thrift Chrome on demand (the Mac only)
    if db is not None:
        b.on_change = lambda kind, detail: chrome_changed(db, b, list(ext), kind, detail)
        b.on_ext_event = bridge_mod.ext_event_logger(db)      # WO54: the extension's own events (the tidy) recorded
    if not watch or db is None:
        return b, None
    shown = {"seen": None, "at": 0.0}

    def state(available: bool) -> None:          # thrift status / the daily window: is the extension there?
        # WO54 §4: "connected" stays what the daily window counts on (crosslist.reachable) — a Thrift Chrome asleep
        # counts, it opens for the next job; `link` says which ("connected", "asleep", "waking", "missing", "refused")
        now_seen = (available, b.link(), b.ext_tabs, b.ext_windows, b.owner_using())
        if now_seen != shown["seen"] or time.monotonic() - shown["at"] > 300:
            shown.update(seen=now_seen, at=time.monotonic())
            db.kv_set(crosslist.EXT_LINK, json.dumps({
                "connected": available, "link": now_seen[1], "tabs": b.ext_tabs, "windows": b.ext_windows,
                "using": now_seen[4], "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}))

    def say(text: str) -> None:                  # once a window
        if db.kv_get(EXT_MISSING_SAID) != crosslist.window(db):
            db.kv_set(EXT_MISSING_SAID, crosslist.window(db))
            notify.say(text)

    def start_chrome() -> None:                  # work waited 2 min for an extension that didn't come: asked again
        out = start_thrift_chrome(s)
        db.log(None, "thrift_chrome_started", {"why": "work waiting, no extension for 2 min", "out": out})

    def restart_chrome() -> None:
        out = restart_thrift_chrome(s)
        db.log(None, "thrift_chrome_restarted", {"why": "work waiting, no extension for 5 min", "out": out})
        from thrift_agent import formcheck                     # WO52 §3: a form check once the browser is back
        formcheck.request(db, [mp for mp in ext], "the Thrift Chrome was restarted")

    def squeeze() -> str | None:                 # WO54 §4: the Mac short of memory → it quits at once when idle
        from thrift_agent import memory
        return memory.squeeze("thrift")

    task = asyncio.create_task(b.watch(in_window=lambda: power.lid_closed() is not True, start_chrome=start_chrome,
                                       say=say, state=state, restart_chrome=restart_chrome,
                                       quit_chrome=lambda why: quit_thrift_chrome(s), squeeze=squeeze))
    return b, task


def chrome_changed(db: DB, b, sites: list[str], kind: str, detail: dict) -> None:
    """WO54 §4: the Thrift Chrome started for work or quit when idle — its event (`thrift_chrome_started` /
    `thrift_chrome_quit`). One that went away in the middle of a job without our quit and was found not running when
    work came: WO52 §4's line (at most every 6 h) and a form check — never for one the poster quit on purpose."""
    out = detail.get("out")
    flat = {**{k: v for k, v in detail.items() if k != "out"}, **out} if isinstance(out, dict) else detail
    db.log(None, kind, {k: (str(v)[:300] if isinstance(v, str) else v) for k, v in flat.items()})
    if kind == "thrift_chrome_started" and flat.get("was_running") is False and b.lost_mid_job is not None:
        b.lost_mid_job = None
        closed_line(db, "thrift", "The Thrift Chrome was closed on the Mac in the middle of a listing — reopened. It "
                                  "opens and closes by itself when there is work; no need to quit it.")
        from thrift_agent import formcheck                     # WO52 §3: a form check once the browser is back
        formcheck.request(db, sites, "the Thrift Chrome was closed in the middle of a job")


CLOSED_SAID = "browser_closed_said"     # kv: {which: epoch} — WO52 §4's line, at most every 6 h per browser


def closed_line(db: DB, which: str, text: str, every: float = 6 * 3600) -> bool:
    """WO52 §4: "<browser> was closed on the Mac — reopened…", once per `every` per browser. True when said."""
    said = loads(db.kv_get(CLOSED_SAID)) or {}
    if time.time() - float(said.get(which) or 0) < every:
        return False
    said[which] = time.time()
    db.kv_set(CLOSED_SAID, json.dumps(said))
    notify.say(text)
    return True


def thrift_chrome_running() -> bool | None:
    """Is the Thrift Chrome (its own profile, ~/thrift/chrome-cross) running? None where it can't be told (not a Mac)."""
    if sys.platform != "darwin":
        return None
    try:
        r = subprocess.run(["pgrep", "-f", "--", "--user-data-dir=.*/thrift/chrome-cross"], capture_output=True,
                           timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.returncode == 0


async def connect_extension(b, poster, say=print, first: float = 30.0, then: float = 60.0) -> float:
    """The CLI's wait for the Thrift Chrome extension (WO32b): it reconnects within ~5 s; past `first` seconds the
    reason it isn't there is said (its token refused, the Thrift Chrome not running, the extension never knocking),
    and past `first + then` it gives up with that reason (RuntimeError). Returns the seconds it took."""
    t0 = time.monotonic()
    say("Waiting for the Thrift Chrome extension…")
    b.want("the CLI waits for the extension")          # WO54 §4: a Thrift Chrome that isn't running is started
    if not await poster.wait_connected(first):
        say(f"  not yet: {b.why_missing(thrift_chrome_running())}")
        if not await poster.wait_connected(then):
            raise RuntimeError(f"the Thrift Chrome extension didn't connect in {first + then:.0f} s — "
                               f"{b.why_missing(thrift_chrome_running())}")
    took = time.monotonic() - t0
    say(f"  extension connected ({took:.1f} s)")
    return took


async def ask_line(prompt: str) -> str:
    """A line typed at the terminal, read on a daemon thread: a Ctrl+C while it waits ends the CLI at once (a pool
    thread would hold the exit until Enter)."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def done(value=None, error=None) -> None:
        if not fut.done():
            fut.set_exception(error) if error is not None else fut.set_result(value)

    def read() -> None:
        try:
            line = input(prompt)
        except BaseException:  # noqa: BLE001 — EOF, a closed terminal: nothing typed
            loop.call_soon_threadsafe(done, None, EOFError())
            return
        loop.call_soon_threadsafe(done, line)

    threading.Thread(target=read, daemon=True, name="ask-line").start()
    return await fut


async def close_bridge(b, task) -> None:
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    if b is not None:
        await b.close()


def next_job(s: Settings, db: DB, enabled: list[str], dry: bool,
             allow_publish: bool = True, live_elsewhere: bool = False) -> tuple[str, str, Render, str] | None:
    """The next (item, marketplace, render, mode) to post, or None — the oldest ready item first.

    Nothing posts without the owner's price: the listing's price must be the owner-approved one (WO27). A gate
    "publish" item publishes when the marketplace's autopublish is on; anything else is a draft — and a draft the
    poster can't save yet (can_draft) is left alone outside a dry-run: held(), never failed, never asked about.
    `allow_publish=False` (HOLD_UNSHIPPED) skips jobs that would go live; drafts still run, and so does a dry-run of a
    publish-gated item, since a dry-run never submits. WO51: an item put behind the others after a failure that wasn't
    its own (health.defer) comes after them. WO53 rule 6 (`live_elsewhere`, outside the listing hours): only an item
    already live on another site — it is finished the same night."""
    for it in health.behind(db, "poshmark", list(db.items("ready")), key=lambda x: x["id"]):
        if live_elsewhere and not crosslist.live_elsewhere(db, it["id"], "poshmark"):
            continue
        batch = db.batch(it["batch_id"])
        if batch is not None and batch["status"] == "regroup":
            continue            # the owner is fixing this batch's photos ([Wrong photos]): not until the fix
        if "test" in it.keys() and it["test"]:
            continue            # WO45 §2: a test share's item is never listed
        renders = loads(it["renders"]) or {}
        for mp in [m for m in enabled if m == "poshmark"]:   # Depop and Vinted follow Poshmark (crosslist, WO30)
            if mp not in renders:
                continue
            if not approved(it, renders[mp]):
                continue        # nothing publishes without the owner's approved price
            post = db.listing(it["id"], mp)
            if post and post["status"] in ("posted", "drafted", "posting", "failed", "skipped", "delisted", "sold"):
                continue        # 'posting' after a crash = check the closet by hand, never re-post blindly; WO49: a
                #                 listing taken down or sold is never posted again by itself
            if post and post["status"] == "dryrun" and dry:
                continue
            autop = s.get(f"marketplaces.{mp}.autopublish", False) and s.is_prod
            mode = "publish" if autop else "draft"     # WO33: ready + the owner's price = it publishes, never held
            if mode == "publish" and not allow_publish and not dry:
                continue        # ship first; the item stays 'ready' and is picked up once the hold is lifted
            if mode == "draft" and not dry and not can_draft(mp):
                continue        # held(): the form would be filled for nothing
            render = Render.model_validate(renders[mp])
            item_type = ((loads(it["facts"]) or {}).get("item_type") or None)
            if isinstance(item_type, dict):
                item_type = item_type.get("value")
            if wrong := crosslist.family_problem(item_type, mp, tuple(getattr(render, k, None) for k in
                                                                      ("department", "category", "subcategory"))):
                # WO51 §5: never the wrong family — WO54 §1.4: never refused for it either; the closest category of
                # its own family instead (every site follows: Depop and Vinted map from it), else as it is
                fixed = refamily(db, it, render, renders, item_type)
                if fixed is not None:
                    render = fixed
                else:
                    crosslist.family_check(db, it["id"], mp, render.title, wrong)
            if (render := shop_window_render(db, it, render, renders)) is None:
                continue        # WO50: a title that breaks the rules never reaches the site (said once)
            return it["id"], mp, render, mode
    return None


def refamily(db: DB, it, render: Render, renders: dict, item_type) -> Render | None:
    """WO54 §1.4 (live 2026-11-09: a category of another family than the item's held it everywhere — the owner: never
    leave it unposted): Poshmark's category is in another family than the item's own type words → the closest
    Poshmark category of its family in its department (the taxonomy's categories and subcategories,
    families.closest); the item's facts and EVERY render follow (category, subcategory, the size menu's tab / value),
    so Depop and Vinted map from it too; ONE private check line. None when Poshmark has no category of that family."""
    from thrift_agent import families
    from thrift_agent.brain import sizes, taxonomy
    from thrift_agent.schema import Facts
    fam = families.family_of_type(item_type)
    dept = render.department
    cats = ((taxonomy.load().get("departments") or {}).get(dept) or {}).get("categories") or {}
    options = {}
    for cat, subs in cats.items():
        options[(cat, None)] = f"{dept} > {cat}"
        for sub in subs or []:
            options[(cat, sub)] = f"{dept} > {cat} > {sub}"
    key = families.closest("poshmark", fam, options, f"{item_type or ''} {render.title}")
    if key is None:
        return None
    cat, sub = key
    try:
        facts = Facts.model_validate(loads(it["facts"]))
        facts = facts.model_copy(update={"category": cat, "subcategory": sub})
        tab, value = sizes.poshmark_size(facts) or (None, None)
    except Exception:  # noqa: BLE001 - a facts row this old: the category alone
        facts, tab, value = None, render.size_tab, render.size_value
    new = {mp: ({**r, "category": cat, "subcategory": sub, "size_tab": tab, "size_value": value}
                if isinstance(r, dict) else r) for mp, r in renders.items()}
    was = " › ".join(x for x in (render.department, render.category, render.subcategory) if x)
    now_ = " › ".join(x for x in (dept, cat, sub) if x)
    db.set_item(it["id"], renders=new, **({"facts": facts.model_dump()} if facts is not None else {}))
    db.log(it["id"], "category_refamily", {"family": fam, "was": was, "now": now_})
    notify.say(f"Check — {render.title}: its category {was} wasn't what the item is (a {fam}) — listed under {now_} "
               f"instead.")
    return Render.model_validate(new["poshmark"])


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


def publishable(s: Settings, db: DB, enabled: list[str]) -> list[tuple[str, str]]:
    """(item, marketplace) the live loop will publish, outside the hours and caps: approved, ready, gate "publish",
    the marketplace's autopublish on, not posted / failed / in the middle of a listing (WO28: "listings still to
    publish")."""
    out = []
    for it in db.items("ready"):
        batch = db.batch(it["batch_id"])
        if batch is not None and batch["status"] == "regroup":
            continue
        if "test" in it.keys() and it["test"]:
            continue            # WO53 §3: never listed (next_job) — never counted either ("4 listings" for 3 items)
        renders = loads(it["renders"]) or {}
        for mp in [m for m in enabled if m == "poshmark"]:
            post = db.listing(it["id"], mp)
            if mp in renders and approved(it, renders[mp]) \
                    and s.get(f"marketplaces.{mp}.autopublish", False) and s.is_prod \
                    and (post is None or post["status"] in ("queued", "dryrun")):
                out.append((it["id"], mp))
    return out


TITLE_HELD = "title_held_said"      # kv: [item] — the line said (WO50)


def _guard_render(db: DB, it, render: Render, renders: dict) -> Render:
    """WO51 §3: the Poshmark title (never the owner's own) and description without a brand or character mark that
    isn't the item's own (an item processed before the guard is put right here, its render kept)."""
    facts = loads(it["facts"]) or {}
    fb = facts.get("brand") if isinstance(facts.get("brand"), dict) else {}
    owner = it["owner_brand"] if "owner_brand" in it.keys() else None
    own = brands.own_names(getattr(render, "brand", None) or fb.get("value"), fb.get("value"), owner)
    ok = " · ".join(str(x) for x in [(facts.get("style_name") or {}).get("value"),
                                     ((facts.get("premium") or {}).get("collab") or {}).get("value"),
                                     ((facts.get("premium") or {}).get("line") or {}).get("value")] if x)
    title, t_gone = (render.title, []) if it["owner_title"] else brands.guard(render.title, own, ok)
    desc, d_gone = brands.guard(getattr(render, "description", None), own, ok)
    if not (t_gone or d_gone):
        return render
    renders["poshmark"] = {**renders["poshmark"], "title": title, "description": desc}
    db.set_item(it["id"], renders=renders)
    db.log(it["id"], "marks_removed", {"mp": "poshmark", "marks": sorted(set(t_gone + d_gone))})
    return render.model_copy(update={"title": title, "description": desc})


def shop_window_render(db: DB, it, render: Render, renders: dict) -> Render | None:
    """WO50: the Poshmark title passes the shop-window rules before the form opens — an item processed before them is
    fixed here (deterministically; its render kept), the owner's own title (WO27) stays hers. A title that still breaks
    them: None (the item waits) and ONE private line."""
    from thrift_agent import titles
    render = _guard_render(db, it, render, renders)          # WO51 §3: no mark that isn't the item's own
    if it["owner_title"] or not titles.check(render.title):
        return render
    facts = loads(it["facts"]) or {}
    brand = (facts.get("brand") or {}).get("value") if isinstance(facts.get("brand"), dict) else None
    fixed = titles.fix(render.title, brand, facts.get("condition"))
    if problems := titles.check(fixed):
        said = loads(db.kv_get(TITLE_HELD)) or []
        if it["id"] not in said:
            db.kv_set(TITLE_HELD, json.dumps([*said, it["id"]][-200:]))
            notify.say(f"✏️ {render.title}: its title breaks the shop-window rules ({'; '.join(problems)}) and couldn't "
                       "be fixed — it waits; reply to its card with the title to use (or `thrift edit --title`).")
        return None
    renders["poshmark"] = {**renders["poshmark"], "title": fixed}
    db.set_item(it["id"], renders=renders)
    db.log(it["id"], "title_fixed", {"from": render.title, "to": fixed})
    return render.model_copy(update={"title": fixed})


def approved(it, render: dict) -> bool:
    """The listing carries the owner's approved price (WO27: nothing publishes without it) — and, when its brand
    couldn't be read, her answer to the brand (Alex, 2026-10-27: Depop and Vinted need one; approve.needs_brand)."""
    return bool(it["owner_price"]) and int(render.get("price") or 0) == int(it["owner_price"]) \
        and not approve.needs_brand(it)


def can_draft(mp: str) -> bool:
    """Whether the poster can save a draft on `mp` yet: Poshmark's Save Draft landing (draft_saved) is UNVERIFIED, and
    so is all of Depop. Until then a draft job outside a dry-run would fill the whole form only to be refused."""
    return mp == "poshmark" and not (poshmark.DRAFT_NEEDS & poshmark.UNVERIFIED)


def held(s: Settings, db: DB, enabled: list[str]) -> list[tuple[str, str, list[str]]]:
    """(item, marketplace, the gate's reasons) of the approved, ready items the loop leaves alone outside a dry-run:
    the gate said "draft" (the copy needs one look: lint, a verifier edit) or the marketplace's autopublish is off,
    and the poster can't save a draft yet. The owner looks, then publishes it with --publish-first."""
    out = []
    for it in db.items("ready"):
        gate, renders = loads(it["gate"]) or {}, loads(it["renders"]) or {}
        for mp in [m for m in enabled if m == "poshmark"]:
            post = db.listing(it["id"], mp)
            if mp not in renders or not approved(it, renders[mp]) or (post and post["status"] not in ("queued",
                                                                                                    "dryrun")):
                continue
            autop = s.get(f"marketplaces.{mp}.autopublish", False) and s.is_prod
            if not autop and not can_draft(mp):      # WO33: the gate never holds; only autopublish off does
                out.append((it["id"], mp, list(gate.get("reasons") or [])))
    return out


def _tell_held(s: Settings, db: DB, enabled: list[str]) -> None:
    """One message per held item, once (event held_draft): what to look at, and the command that publishes it."""
    for iid, mp, reasons in held(s, db, enabled):
        if db.conn.execute("SELECT 1 FROM events WHERE ref=? AND kind='held_draft'", (iid,)).fetchone():
            continue
        title = ((loads(db.item(iid)["renders"]) or {}).get(mp) or {}).get("title") or iid
        why = ("the copy needs a look: " + "; ".join(reasons)) if reasons else f"marketplaces.{mp}.autopublish is off"
        db.log(iid, "held_draft", {"mp": mp, "reasons": reasons})
        notify.group(f"⏸ Not published automatically: {title} — {crosslist.LABEL.get(mp, mp)} publishes by itself "
                     "only once its autopublish is on.")   # WO33: the gate holds nothing; only autopublish off does
        notify.say(f"⏸ {iid} held: {title}\n{why}")


def condition_rule_breaks(r: Render) -> list[str]:
    """What in a stored listing breaks the owner's condition rule (wear words; a used item's grade claims): a listing
    rendered before the rule existed. The pipeline never makes one now (copy.condition_rule + lint)."""
    text = f"{r.title}\n{r.description}\n{' '.join(r.tags)}"
    if r.subcategory in ("Jean Shorts",) or r.category == "Jeans" or "cutoff" in r.title.lower().replace("-", ""):
        text = STYLE_HEMS.sub(" ", text)                  # a cutoff's frayed / raw hem is its style (WO27)
    found = {m.group(0).lower() for m in NEGATIVE_WORDS.finditer(text)}
    if r.condition in USED:
        found |= {m.group(0).lower() for m in USED_CLAIMS.finditer(text)}
    return sorted(found)


def why_dry(s: Settings, force_dry: bool = False) -> str:
    """Why the poster loop runs dry on the Mac: it publishes only with poster.dry_run off AND
    poster.autopublish_confirmed on (both deliberate flips; the defaults keep it dry)."""
    if force_dry:
        return "dry-run: --dry-run"
    if s.get("poster.dry_run", True):
        return "dry-run: poster.dry_run is on"
    return "dry-run: poster.autopublish_confirmed is off (poster.dry_run alone doesn't publish)"


def verifying(db: DB, iid: str, mp: str, out: Outcome) -> dict:
    """WO53 §2: the final click happened and the listing's address wasn't confirmed at once — "submitted — verifying":
    the site's own data is looked at again on postcheck's schedule, quietly; nobody is asked anything. The second post
    of an item proven absent once is round 2 (never a third)."""
    was = crosslist.unconf_state(db, iid, mp) or {}
    rnd = 2 if was.get("stage") in ("reposted", "retry") else 1
    clicked = None
    if getattr(out, "clicked_at", None):
        clicked = datetime.fromtimestamp(float(out.clicked_at), timezone.utc)
    elif out.created_id and (t := postcheck.id_time(out.created_id)):
        clicked = datetime.fromtimestamp(t, timezone.utc) - timedelta(seconds=5)    # Poshmark stamps the id at once
    return postcheck.begin(db, iid, mp, clicked=clicked, created=out.created_id, shot=out.screenshot, round_=rnd)


def unconfirmed_alarm(db: DB, iid: str, mp: str, title: str, state: dict, why: str) -> bool:
    """WO53 §2: still unknown 2 hours after the click (or the second post unconfirmed too) — ONE 🚨 with the evidence
    and a ready Claude Code request, never a question to the owner; the looks go on."""
    from thrift_agent import alarm
    if state.get("alarmed"):
        return False
    site = crosslist.LABEL.get(mp, mp)
    clicked = postcheck.clicked_of(state, db.listing(iid, mp))
    reads = state.get("reads") or []
    seen = ", ".join(f"{r.get('result')}" for r in reads[-6:]) or "none"
    shot = state.get("shot")
    evidence = f"var/failed/shots/{Path(shot).name}" if shot else None
    error = (f"{title}: {why} — pressed {PRESSED.get(mp, ('Post',))[0]} at {clicked.astimezone():%I:%M %p}; "
             f"{site}'s own data since: {seen}; not posted again (rule 3)")
    crosslist.set_unconf(db, iid, mp, alarmed=True)
    return alarm.raise_for(db, f"{mp}:verify:{iid}", error, step="confirming the listing", evidence=evidence,
                           since=clicked, why="unconfirmed", label=site)


def held_by_others(db: DB, iid: str, mp: str) -> set[str]:
    """The addresses other items hold on `mp`: never taken for this one (WO35)."""
    return {r["url"] for r in db.conn.execute("SELECT url FROM listings WHERE marketplace=? AND item_id<>? AND url IS "
                                              "NOT NULL", (mp, iid))}


def close_question(db: DB, iid: str, mp: str, label: str) -> None:
    """An unconfirmed publish's old ⚠️ question (the group's, before WO35; the ops chat's since) closed: resolved, and
    its message marked with one inert "✓ …" button (no new message) — a late reply then gets "already settled"."""
    ref = crosslist.unconfirmed_ref(iid, mp)
    try:
        from thrift_agent.config import settings
        s = settings()
        for bot in (approve.bot_for(s), approve.ops_bot_for(s)):
            if bot is not None:
                approve._ack(db, bot, "unconfirmed", ref, label)
    except Exception as e:  # noqa: BLE001 — the mark is a courtesy; the outbox below is what counts
        db.log(iid, "card_mark_failed", f"{type(e).__name__}: {e}")
    db.outbox_resolve("unconfirmed", ref)


def confirm_found(s: Settings | None, db: DB, ps: dict, iid: str, mp: str, url: str, how: str) -> None:
    """An unconfirmed publish found live by the site's own data (WO35 §4, WO53): posted with its address (posted_at
    the click, as near as it is known), its old question closed, its state forgotten, the item's card brought up to
    date (Depop and Vinted queued once Poshmark is live)."""
    row = db.listing(iid, mp)
    state = crosslist.unconf_state(db, iid, mp)
    when = postcheck.iso(postcheck.clicked_of(state, row)) if (state or row is not None) else None
    db.upsert_listing(iid, mp, status="posted", url=url, error=None, listing_id=listing_id_from(mp, url),
                      posted_at=when)
    db.log(iid, "post_confirmed", {"mp": mp, "url": url, "how": how, "was": row["error"] if row is not None else None})
    close_question(db, iid, mp, "✓ it's live")
    crosslist.set_unconf(db, iid, mp, stage=None)
    _settle_item(db, iid, list(ps))
    if mp == "poshmark" and s is not None:
        crosslist.queue(s, db, iid)
    health.worked(db, mp, iid)
    crosslist.announce(s, db, iid)


async def last_look(s: Settings | None, db: DB, ps: dict, ctx, iid: str, mp: str, render: Render) -> bool:
    """WO35 §4 / WO53 rule 3: one last look by the site's own data right before a listing proven absent is posted
    again. Found → recorded, never posted twice; unreadable → not now (it stays in line, behind the others); absent →
    go (round 2). True when it may be posted now."""
    st = crosslist.unconf_state(db, iid, mp) or {}
    try:
        url, looked = await ps[mp].find_live(ctx, render, since=postcheck.clicked_of(st, db.listing(iid, mp)).timestamp(),
                                             created=st.get("created"))
    except AccountBlocked as e:
        crosslist.block(db, mp, str(e), e.page)
        return False
    except Exception as e:  # noqa: BLE001 — can't look: never "absent"
        url, looked = None, {"error": f"{type(e).__name__}: {e}"}
    db.log(iid, "unconfirmed_last_check", {"mp": mp, "found": url, "seen": looked})
    if url:
        confirm_found(s, db, ps, iid, mp, url, "the last look before posting it again")
        notify.say(f"✅ {crosslist.LABEL[mp]}: {render.title} turned out live — found right before posting it again "
                   f"(not posted twice): {url}")
        return False
    if "error" in looked or looked.get("result") == "unknown":
        health.defer(db, mp, iid)
        return False
    crosslist.set_unconf(db, iid, mp, stage="reposted")
    return True


def _render_for(db: DB, iid: str, mp: str) -> Render:
    if mp == "poshmark":
        return Render.model_validate((loads(db.item(iid)["renders"]) or {})["poshmark"])
    return cross_render_of(db, iid, mp)


async def recheck_unconfirmed(s: Settings | None, db: DB, ps: dict, mp: str, ctx=None) -> bool:
    """WO35 §4 + WO53 §2, one due look at a publish being verified on `mp`, by the site's own data (find_live —
    Poshmark's listing page by the id it named and its closet newest first, Vinted's newest items, Depop's shop and
    SKU), on postcheck's schedule (10 s … 10 min after the click, then every 10 minutes), quietly. Live → posted (the
    card follows). Absent → noted; only rule 3's proof (two absent reads 10+ minutes apart, the last 30+ minutes after
    the click) puts it back in line, once, with one last look right before the click; the second post unconfirmed too
    → never a third. Unreadable → never "absent". Still unknown 2 hours after the click → ONE 🚨. It is never a failure
    (no pause) and nobody is asked. True when one was handled."""
    poster = ps.get(mp)
    if poster is None or not hasattr(poster, "find_live") or not getattr(poster, "available", lambda: True)():
        return False
    due = crosslist.rechecks_due(db, mp, postcheck.now())
    if not due:
        return False
    iid = due[0]
    state = crosslist.unconf_state(db, iid, mp) or {}
    site = crosslist.LABEL[mp]
    row = db.listing(iid, mp)
    clicked = postcheck.clicked_of(state, row)
    if not state.get("clicked_at"):                 # a row from before WO53: the schedule starts from its last update
        old = (postcheck.now() - clicked).total_seconds() > LEGACY_AFTER_S
        crosslist.set_unconf(db, iid, mp, stage="given_up" if old else "wait", clicked_at=postcheck.iso(clicked),
                             round=state.get("round", 1), legacy=True)
        state = crosslist.unconf_state(db, iid, mp) or {}
        if old:                                     # never re-posted on a guess about an old listing: the 🚨, once
            unconfirmed_alarm(db, iid, mp, iid, state, "an unconfirmed publish older than 6 hours (from before the "
                                                       "WO53 checks): never posted again by itself")
            return True
    try:
        render = _render_for(db, iid, mp)
    except Exception as e:  # noqa: BLE001 — an item that can't be rendered: the 🚨 (code must look), never a question
        crosslist.set_unconf(db, iid, mp, stage="given_up")
        unconfirmed_alarm(db, iid, mp, iid, state, f"can't be looked for ({type(e).__name__}: {e})")
        return True
    poster.held = held_by_others(db, iid, mp)
    try:
        url, looked = await poster.find_live(ctx, render, since=clicked.timestamp(), created=state.get("created"))
    except AccountBlocked as e:
        crosslist.block(db, mp, str(e), e.page)
        return True
    except Exception as e:  # noqa: BLE001 — not readable now: looked at again later, never "absent"
        url, looked = None, {"error": f"{type(e).__name__}: {e}"}
    result = "found" if url else ("unknown" if "error" in looked or looked.get("result") == "unknown" else "absent")
    state = postcheck.note_read(db, iid, mp, result, looked.get("how") or looked.get("error") or looked.get("why"))
    db.log(iid, "unconfirmed_recheck", {"mp": mp, "found": url, "result": result, "seen": looked,
                                        "round": state.get("round", 1), "due": state.get("due")})
    if url:
        confirm_found(s, db, ps, iid, mp, url, f"verified by {site}'s own data ({looked.get('how') or 're-check'})")
        if looked.get("note"):
            notify.say(f"{site} ({iid}): {looked['note']}")
        return True
    now = postcheck.now()
    if result == "absent" and postcheck.absent_proven(state.get("reads") or [], clicked):
        if int(state.get("round") or 1) >= 2:        # the automatic second post wasn't confirmed either: never a third
            crosslist.set_unconf(db, iid, mp, stage="given_up")
            unconfirmed_alarm(db, iid, mp, render.title, state, "posted twice and neither shows on the site")
            return True
        how = looked.get("how") or f"{site}'s own data"
        db.upsert_listing(iid, mp, status="queued", error=f"not posted: {how} showed it absent twice, 10+ minutes apart "
                                                          f"— posted again after one last look")
        if mp == "poshmark" and db.item(iid)["status"] != "ready":
            db.set_item(iid, status="ready")
        crosslist.set_unconf(db, iid, mp, stage="retry")
        close_question(db, iid, mp, f"✓ not on {site} — it goes up again")
        db.log(iid, "unconfirmed_absent", {"mp": mp, "reads": state.get("reads")})
        notify.say(f"↻ {site}: {render.title} isn't there ({how} showed it absent twice, 10+ minutes apart) — it goes "
                   f"up again after one last look")
        return True
    if postcheck.overdue(clicked, now):
        unconfirmed_alarm(db, iid, mp, render.title, state, "still not confirmed 2 hours after the click")
    return True


SKIPPED_SAID = "skipped_said"     # kv: ["<item>:<mp>"] — the 🚨 for a listing the poster couldn't make, said once


def ask_skipped(db: DB, iid: str, mp: str, title: str, why: str) -> None:
    """A listing the poster couldn't make on a site (WO27: a required field nothing came close to; WO30: its tries used
    up). WO53 §2/§6: never a question to the owner — no 'retry' asked; ONE private 🚨 with the evidence and a ready
    Claude Code request (the code must change for it), once per item and site; the ✅ card shows the site's state.
    (Her reply 'retry' to an older ⏭ message, and `thrift requeue`, still work.)"""
    from thrift_agent import alarm
    key = crosslist.unconfirmed_ref(iid, mp)
    said = loads(db.kv_get(SKIPPED_SAID)) or []
    if key in said:
        db.log(iid, "skipped_line_kept", {"site": mp})
        return
    db.kv_set(SKIPPED_SAID, json.dumps([*said, key][-200:]))
    site = crosslist.LABEL.get(mp, mp)
    alarm.raise_for(db, f"{mp}:skipped:{iid}", f"{title} couldn't be listed on {site}: {why}", step="the listing",
                    evidence=alarm._evidence(db, mp), why="blocked", label=site)


def record_outcome(db: DB, iid: str, mp: str, render: Render, out: Outcome, marketplaces: list[str],
                   stage: str = "form", say_dry_run: bool = True, checks: list[str] = (),
                   seconds: float | None = None, slept: bool = False, s: Settings | None = None,
                   announce: bool = True) -> None:
    """The post row, the event, the owner's message and the item's status for one Outcome.

    posted_at = when this row last hit the site. A dry-run fills the real form (uploads included), so it gets a stamp
    too and counts against the pacing caps in listed_since(). A cancelled supervised publish never reached the site:
    the row goes back to 'queued'. A failed publish after the final click without a URL is "unconfirmed": the listing
    may be live, so `thrift requeue` refuses it until the closet is checked by hand (invariant 4).

    WO29: the group hears "Posted ✓" (with "✓ All done …" on the window's last listing, `s` given), the ⚠️ of an
    unconfirmed publish and a plain "⏭ … was skipped"; failures, dry-runs and the details go to the ops chat.
    WO30: a Poshmark listing that went live queues the item on Depop and Vinted (`s` given) and keeps its "check:"
    notes on its row; its "Posted ✓" line is crosslist.announce's — at once (`announce`), or from the poster loop
    once the item's other marketplaces are done."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    error = out.error
    if out.status == "failed" and out.clicked and not out.url:
        error = UNCONFIRMED + (error or "")
    if out.status == "skipped":                     # nothing was saved; never retried until the owner requeues it
        error = SKIPPED + (error or "")
    status = {"cancelled": "queued"}.get(out.status, out.status)
    db.upsert_listing(iid, mp, status=status, url=out.url, error=error if out.status != "cancelled" else out.note,
                      posted_at=stamp if out.status in ("posted", "drafted", "dryrun") else None,
                      listing_id=listing_id_from(mp, out.url),
                      price=render.price, fields_json={"guesses": list(out.guesses), "checks": list(checks)})
    db.log(iid, f"post_{out.status}", {"mp": mp, "url": out.url, "error": error, "shot": out.screenshot,
                                       "note": out.note, "draft_left": out.draft_left, "clicked": out.clicked,
                                       "guesses": out.guesses, "seconds": round(seconds, 1) if seconds else None,
                                       "slept": slept or None})
    _warn_draft(mp, iid, out.draft_left)
    note = f"\n{out.note}" if out.note else ""
    guesses = f"\ncheck: {'; '.join(out.guesses)}" if out.guesses else ""
    if out.status == "failed" and out.clicked and not out.url:     # WO53: verifying — the site's own data settles it
        verifying(db, iid, mp, out)
        if out.landing:
            postcheck.landed(db, mp, out.landing)
    elif out.status == "failed":
        notify.photo(Path(out.screenshot or ""), f"❌ {mp} failed ({iid}): {render.title}\n{error}{note}")
    elif out.status == "skipped" and crosslist.NO_SIZE in (error or ""):
        crosslist.say_no_size(render.title)            # WO37: her size is the answer, never 'retry'; one line an item
    elif out.status == "skipped" and crosslist.size_skip(error):
        crosslist.say_size_skip(db, iid, mp, render.title, error)   # WO50: one private line; the other sites go on
    elif out.status == "skipped":
        notify.photo(Path(out.screenshot or ""), f"⏭ skipped on {mp} ({iid}): {render.title}\n{out.error}{note}")
        ask_skipped(db, iid, mp, render.title, "Poshmark's form doesn't take one of its details")   # the 🚨, once
    elif out.status == "dryrun" and say_dry_run:          # poster.notify_dry_runs: off, the owner's chat stays quiet
        notify.photo(Path(out.screenshot or ""),
                     f"🧪 dry-run {mp} ({stage}): {render.title} — ${render.price}{guesses}{note}")
    elif out.status == "cancelled":
        notify.say(f"↩️ not published on {mp} ({iid}): {render.title}{note}")
    elif out.status == "posted":
        if out.landing:
            postcheck.landed(db, mp, out.landing)       # WO53 §7: a new landing said once ("🩹 … — handled.")
        _settle_item(db, iid, marketplaces)
        if s is not None and mp == "poshmark":
            crosslist.queue(s, db, iid)                    # then Depop and Vinted (WO30)
        if announce:
            crosslist.announce(s, db, iid)
        if out.note:
            notify.say(f"{iid} posted: {out.note}")
    else:
        notify.say(f"✅ {out.status} on {mp}: {render.title} — ${render.price}\n{out.url or ''}{guesses}{note}")
    _settle_item(db, iid, marketplaces)


def _settle_item(db: DB, iid: str, marketplaces: list[str] = ()) -> None:
    """WO30: the item counts as posted once Poshmark is ('drafted' while Poshmark only holds a draft); Depop and
    Vinted are extra rows that never change the item's status."""
    row = db.listing(iid, "poshmark")
    if row is not None and row["status"] in ("posted", "drafted"):
        db.set_item(iid, status=row["status"])


async def terminal_confirm(r: Render, panel: str) -> bool:
    """The supervised publish's last step: the Share Listing panel is open in the Chrome window, the owner types LIST."""
    print(f"\nReady to list on Poshmark: {r.title}\n  ${r.price} · {r.size or 'no size'} · {r.condition} "
          f"· SKU {r.sku}\n  The Share Listing panel is open in the Chrome window; Promote My Closet is off.")
    try:
        answer = await ask_line("Type LIST to publish (anything else cancels): ")
    except EOFError:
        return False
    return answer.strip() == "LIST"


async def terminal_confirm_cross(fields, site: str) -> bool:
    """The supervised publish on Depop / Vinted (WO30): the filled form is in the Chrome window, the owner types POST."""
    print(f"\nReady to publish on {site}: {getattr(fields, 'title', None) or fields.description.splitlines()[0]}\n"
          f"  ${fields.price} · {fields.size or 'no size'} · {fields.condition} · "
          f"{getattr(fields, 'category', None) or getattr(fields, 'category_path', '')} · "
          f"{len(fields.photos)} photo{'s' if len(fields.photos) != 1 else ''}\n"
          f"  The form is filled in the Thrift Chrome window (nothing is published before POST).", flush=True)
    try:
        answer = await ask_line("Type POST to publish (anything else cancels): ")
    except EOFError:
        return False
    return answer.strip() == "POST"


async def publish_first(s: Settings, db: DB, iid: str, confirm=terminal_confirm, mp: str = "poshmark") -> Outcome:
    """The supervised first publish (WO15): this one item on Poshmark, on prod, at the owner-approved price, with the
    owner typing LIST at the Share Listing panel. poster.dry_run is ignored for this one call; autopublish stays off.
    Fills, reads back and diffs as usual, presses List This Item exactly once, records everything after the click,
    finds and checks the listing, records the post. Never retried automatically. WO30: `mp` depop / vinted publishes
    the item there the same way (it must be live on Poshmark; the owner types POST)."""
    if mp != "poshmark":
        return await publish_first_cross(s, db, iid, mp, terminal_confirm_cross if confirm is terminal_confirm
                                         else confirm)
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
    if broken := condition_rule_breaks(render):
        raise ValueError(f"item {iid}'s listing text was written before the condition rule ({', '.join(broken)}): "
                         f"reprocess it first — thrift answer {iid} \"recheck\" (the price is kept)")
    row = db.listing(iid, "poshmark")
    if row and (row["status"] in ("posted", "posting", "drafted") or row["url"]):
        raise ValueError(f"poshmark: status {row['status']}{' with ' + row['url'] if row['url'] else ''} — it reached "
                         "the site: never posted twice")
    if row and (row["error"] or "").startswith(UNCONFIRMED):
        raise ValueError("poshmark: an earlier List This Item is being verified by Poshmark's own data — never "
                         "pressed again until that data proves it absent")
    if row and row["status"] == "failed":
        raise ValueError(f"poshmark: the last attempt failed — `thrift requeue {iid}` first (it checks that nothing "
                         "reached the site)")
    if s.flag_set("HOLD_UNSHIPPED"):
        raise ValueError(HOLD_REASON)
    hour_ago, midnight = windows(tz=s["schedule"]["timezone"])
    ok, why = can_post(s, db.listed_since(hour_ago), db.listed_since(midnight))
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
        if not db.claim_listing(iid, "poshmark"):     # 'posting' before the form opens (invariant 4)
            raise ValueError(f"poshmark: could not claim {iid} (another poster has it?)")
        try:
            out = await poster.post(ctx, render, "publish", False, s.path("failed") / "shots")
        except AccountBlocked as e:
            db.upsert_listing(iid, "poshmark", status="queued", error=str(e))
            _halt(s, str(e), f"⛔ Poster paused: {e}\nFix it in the poster Chrome window, then delete the PAUSE file.")
            raise
    finally:
        await ctx.close()
        await pw.stop()
    record_outcome(db, iid, "poshmark", render, out, list(ps), checks=_checks(it), s=s)   # 'posted' once all are
    return out


async def confirm_live(s: Settings, db: DB, ps: dict, ctx, iid: str, mp: str, url: str) -> str:
    """The owner's "posted <url>" (a reply to the ⚠️ message, or `thrift mark-posted`), checked in the browser
    context `ctx`: the row must be an unconfirmed publish, the address a listing page of `mp` that no other item
    holds, and the page must show the item's title and price; then the row is 'posted' with the URL and Telegram
    hears "✅ confirmed live". Anything else raises ValueError and nothing changes. Returns the canonical address."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    row = db.listing(iid, mp)
    if row is None or row["status"] != "failed" or row["url"] or not (row["error"] or "").startswith(UNCONFIRMED):
        state = "no post" if row is None else f"status {row['status']}" + (f" with {row['url']}" if row["url"] else "")
        raise ValueError(f"{mp}: only a post in 'unconfirmed publish' can be marked posted ({iid} has {state})")
    renders = loads(it["renders"]) or {}
    if "poshmark" not in renders or (mp == "poshmark" and mp not in renders):
        raise ValueError(f"item {iid} has no {mp} listing")
    render = cross_render_of(db, iid, mp) if mp != "poshmark" else Render.model_validate(renders[mp])
    poster = ps.get(mp)
    if poster is None:
        raise ValueError(f"marketplaces.{mp} is not enabled")
    address = poster.listing_address(url)
    if address is None:
        raise ValueError(f"not a {mp} listing address: {url!r}" + (" (expected e.g. https://poshmark.com/listing/"
                                                                  "<title-words>-<24 hex id>)" if mp == "poshmark"
                                                                  else ""))
    if (other := db.conn.execute("SELECT item_id FROM listings WHERE url=? AND NOT (item_id=? AND marketplace=?)",
                                 (address, iid, mp)).fetchone()) is not None:
        raise ValueError(f"{address} is already recorded for item {other['item_id']}")
    shots = s.path("failed") / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    poster.shot = shots / f"{iid}-{mp}-{stamp}-confirm.png"
    page = None
    try:
        if getattr(poster, "driver", "playwright") == "playwright":
            page = await ctx.new_page()                             # an extension poster opens it in the Thrift Chrome
        await poster.verify_live(page, address, render)             # title and price on the page, else it raises
        await keep_evidence(page, poster.shot, {"item": iid, "url": address, "title": True, "price": True})
    except Exception as e:  # noqa: BLE001
        await keep_evidence(page, poster.shot, {"item": iid, "url": address, "error": f"{type(e).__name__}: {e}"})
        raise ValueError(f"{address} doesn't show this item ({type(e).__name__}: {e}); nothing changed") from None
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
    # posted_at: when the listing went live, as near as the row knows it (the failed attempt's last update).
    db.upsert_listing(iid, mp, status="posted", url=address, error=None, posted_at=row["updated_at"],
                      listing_id=listing_id_from(mp, address))
    db.log(iid, "post_confirmed", {"mp": mp, "url": address, "was": row["error"]})
    db.outbox_resolve("unconfirmed", crosslist.unconfirmed_ref(iid, mp))
    crosslist.set_unconf(db, iid, mp, stage=None)
    _settle_item(db, iid, list(ps))
    if mp == "poshmark":
        crosslist.queue(s, db, iid)
    crosslist.announce(s, db, iid)                                                     # WO29, WO30
    notify.say(f"✅ {iid} confirmed live on {mp} (the owner's link): {address}")
    return address


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
    ps = posters(s)
    pipeline.check_unconfirmed(db, iid, mp)                       # refused before Chrome opens: nothing touched
    if getattr(ps.get(mp), "driver", "playwright") == "extension":   # WO32: the listing is read in the Thrift Chrome
        try:
            b, _ = await open_bridge(s, None, ps, watch=False, strict=True)
        except bridge_mod.BridgeError as e:
            raise RuntimeError(f"{mp}: the extension bridge didn't start: {e}") from None
        try:
            await connect_extension(b, ps[mp])
            return await confirm_live(s, db, ps, None, iid, mp, url)
        finally:
            await close_bridge(b, None)
    try:
        pw, ctx = await open_browser(s.path("chrome_profile"), s["schedule"]["timezone"])
    except Exception as e:  # noqa: BLE001 — nothing was touched
        raise RuntimeError(f"Chrome didn't open ({type(e).__name__}) — is the poster service still running? Stop it "
                           "first: bash deploy/services.sh stop") from e
    try:
        return await confirm_live(s, db, ps, ctx, iid, mp, url)
    finally:
        await ctx.close()
        await pw.stop()


def cross_render_of(db: DB, iid: str, mp: str, fields=None) -> Render:
    """The Render a Depop / Vinted post goes by: Poshmark's title and the approved price, the mapped description, size,
    colours and photos (`fields`, else the row's fields_json)."""
    it = db.item(iid)
    posh = Render.model_validate((loads(it["renders"]) or {})["poshmark"])
    f = fields.model_dump() if hasattr(fields, "model_dump") else (fields or loads((db.listing(iid, mp) or {})["fields_json"]
                                                                                  if db.listing(iid, mp) else None) or {})
    return Render(marketplace=mp, title=posh.title, description=f.get("description") or posh.description,
                  tags=f.get("hashtags") or [], brand=posh.brand, department=posh.department,
                  category=f.get("category") or f.get("category_path") or posh.category, subcategory=None,
                  size=f.get("size"), colors=f.get("colors") or posh.colors, condition=posh.condition,
                  price=int(it["owner_price"] or posh.price), photos=f.get("photos") or posh.photos, sku=iid)


def map_fields(mp: str, view: ItemView):
    from thrift_agent.catalogs.depop import map_depop
    from thrift_agent.catalogs.vinted import map_vinted
    return map_depop(view) if mp == "depop" else map_vinted(view)


_SITE_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


def site_lock(mp: str) -> asyncio.Lock:
    """WO53: ONE Depop / Vinted job at a time per site — the poster object carries the job's planned fields, its strict
    flag and its go-ahead (the row's claim). Live 2026-11-08 01:01: an asked-for dry run (the housekeeping's) ran beside
    the site's own form check and the canary's form was compared with the dress's plan; the same race could have
    cleared a real publish's go-ahead. Keyed by the running loop (each test runs its own)."""
    key = (id(asyncio.get_running_loop()), mp)
    lock = _SITE_LOCKS.get(key)
    if lock is None:
        lock = _SITE_LOCKS[key] = asyncio.Lock()
    return lock


async def run_cross(s: Settings, db: DB, ps: dict, ctx, iid: str, mp: str, *, dry: bool, hold: bool = False,
                    request: bool = False, progress=None, quiet: bool = False) -> Outcome | None:
    """One item on Depop or Vinted (`_run_cross`), never beside another job of the same site (`site_lock`)."""
    async with site_lock(mp):
        return await _run_cross(s, db, ps, ctx, iid, mp, dry=dry, hold=hold, request=request, progress=progress,
                                quiet=quiet)


async def _run_cross(s: Settings, db: DB, ps: dict, ctx, iid: str, mp: str, *, dry: bool, hold: bool = False,
                     request: bool = False, progress=None, quiet: bool = False) -> Outcome | None:
    """One item on Depop or Vinted (WO30): map its values from the catalogs (a value that can't be mapped skips this
    marketplace only), keep them on the row, take the row ('posting' before the form opens, invariant 4), fill the
    form — a dry run leaves without publishing — and record what happened. A logged-out / CAPTCHA / verification wall
    pauses the marketplace. `request`: an explicit dry run (`thrift crosslist --dry-run`, a paused site's test run —
    WO51): the row is neither taken nor changed, only the screenshot and the fields go to the ops chat (`quiet`: not
    even those — the automatic test runs). `progress`: each progress line of an extension job as it comes (WO32b).
    WO51: a failure that isn't about the item (health.kind_of: site or infra) puts it back in line untouched — never a
    ❌ line, never an attempt; the same one twice pauses the site (one line)."""
    it = db.item(iid)
    title = ((loads(it["renders"]) or {}).get("poshmark") or {}).get("title") or iid
    try:
        view = ItemView.from_row(it)
        fields = crosslist.strict_title(db, iid, mp, map_fields(mp, view), view)    # WO50: the site's title rules
    except MappingError as e:
        if request:
            if not quiet:
                notify.say(f"⏭ {crosslist.LABEL[mp]} dry run ({iid}): {title}\ncan't map: {e}")
            return Outcome("skipped", error=f"can't map: {e}")   # a test run's: no verdict on the site (WO51)
        crosslist.skip_unmappable(db, iid, mp, title, str(e))
        return None
    except CatalogError as e:
        crosslist.block(db, mp, f"failures: catalog — {e}")
        return None
    facts = getattr(view, "facts", None)
    wrong = crosslist.family_problem(getattr(facts, "item_type", None), mp,
                                     getattr(fields, "category_path", None) or getattr(fields, "category", None))
    if wrong:                                       # WO51 §5: never posted in another family's category …
        if request:
            if not quiet:
                notify.say(f"⏸ {crosslist.LABEL[mp]} dry run ({iid}): {title}\n{wrong}")
            return Outcome("skipped", error=wrong)
        # … WO54 §1.4: the mapping already took the closest category of the item's family the site has
        # (categories.in_family); what is left has no closer one there — posted, never refused, one check line
        crosslist.family_check(db, iid, mp, title, wrong)
    render = cross_render_of(db, iid, mp, fields)
    poster = ps[mp]
    poster.fields, poster.confirm, poster.strict = fields, None, not dry
    poster.held = held_by_others(db, iid, mp)        # WO35: another item's listing is never taken for this one
    extension = getattr(poster, "driver", "playwright") == "extension"
    if extension:
        poster.on_go_ahead, poster.progress = None, progress
    shots = s.path("failed") / "shots"
    if request:
        try:
            out = await poster.post(ctx, render, "publish", True, shots)
        except AccountBlocked as e:              # logged out / CAPTCHA / verification: the owner must act on the Mac
            db.log(iid, "crosslist_dry_run", {"mp": mp, "status": "blocked", "error": str(e)})
            crosslist.block(db, mp, str(e), e.page)
            # WO53 §3: a verdict, so a paused site's test run waits its doubled cool-down — live 2026-11-08 a test that
            # met the login page returned nothing and ran again every ~5 s (500+ screenshots in 9 minutes)
            return Outcome("failed", error=str(e))
        except Exception as e:  # noqa: BLE001 — an asked-for dry run never stops the poster
            db.log(iid, "crosslist_dry_run", {"mp": mp, "status": "error", "error": f"{type(e).__name__}: {e}"})
            if not quiet:
                notify.say(f"❌ dry run {crosslist.LABEL[mp]} ({iid}): {type(e).__name__}: {e}")
            return Outcome("failed", error=f"{type(e).__name__}: {e}")
        lines = list(getattr(poster, "lines", []) or [])
        db.log(iid, "crosslist_dry_run", {"mp": mp, "status": out.status, "shot": out.screenshot, "error": out.error,
                                          "fill_seconds": getattr(poster, "fill_seconds", None), "progress": lines,
                                          "quiet": quiet or None})
        if quiet:
            return out
        notify.ops_photo(Path(out.screenshot or ""),
                         f"🧪 dry-run {crosslist.LABEL[mp]} ({iid}): {title} — ${fields.price} [{out.status}]\n"
                         f"{crosslist.fields_summary(fields.model_dump())}"
                         + (f"\n{' · '.join(lines)}" if lines else "")
                         + (f"\n{out.error}" if out.error else "") + (f"\n{out.note}" if out.note else ""))
        return out
    if (crosslist.unconf_state(db, iid, mp) or {}).get("stage") == "retry" and not (dry or hold):
        # WO35 §4: one last look by the site's own data right before it is posted again — never twice on one site
        site = crosslist.LABEL[mp]
        try:
            url, looked = await poster.find_live(ctx, render, since=0)
        except AccountBlocked as e:
            crosslist.block(db, mp, str(e), e.page)
            return None
        except Exception as e:  # noqa: BLE001 — can't look: not posted now (retried next window)
            url, looked = None, {"error": f"{type(e).__name__}: {e}"}
        db.log(iid, "unconfirmed_last_check", {"mp": mp, "found": url, "seen": looked})
        if url:
            confirm_found(s, db, ps, iid, mp, url, "the last check before posting again")
            notify.say(f"✅ {site}: {title} turned out live — found right before posting it again (not posted twice): "
                       f"{url}")
            return None
        if "error" in looked or looked.get("result") == "unknown":
            # WO53 rule 3: data that can't be read is never "absent" — not posted now; it stays in line, looked at first
            health.defer(db, mp, iid)
            db.upsert_listing(iid, mp, error=f"waiting: couldn't look at {site} before posting it again "
                                             f"({looked.get('error') or looked.get('why')})")
            return None
        crosslist.set_unconf(db, iid, mp, stage="reposted")
    db.upsert_listing(iid, mp, fields_json=fields.model_dump(), price=fields.price)
    if extension:
        # WO32b: the attempt is counted now; the row becomes 'posting' only at the go-ahead (on_go_ahead) — before it
        # nothing can be published, so a failure, a timeout or a stop leaves it 'queued'
        if not db.begin_attempt(iid, mp):
            return None
        if not (dry or hold):
            poster.on_go_ahead = lambda: db.claim_listing(iid, mp, count=False)
    elif not db.claim_listing(iid, mp):
        return None
    t0, m0, k0 = time.time(), time.monotonic(), power.last_wake()
    try:
        out = await poster.post(ctx, render, "publish", dry or hold, shots)
    except AccountBlocked as e:
        row = db.listing(iid, mp)
        db.upsert_listing(iid, mp, status="queued", error=str(e), attempts=max(0, (row["attempts"] or 1) - 1))
        crosslist.block(db, mp, str(e), e.page)
        return None
    except Exception as e:  # noqa: BLE001 — before the form: nothing submitted; never about the item (WO51)
        err = f"{type(e).__name__}: {e}"
        kind = health.kind_of(err)
        health.non_item(db, mp, iid, err, health.INFRA if kind == health.ITEM else kind)
        return None
    seconds, slept = time.monotonic() - m0, power.slept_since(t0, k0, m0)
    if slept:
        out, requeued = await after_sleep(db, ps, ctx, iid, mp, render, out, t0)
        if requeued:
            return out
    if out.status == "failed" and not out.clicked:
        kind = health.kind_of(out.error, getattr(poster, "error_kind", None))
        if kind != health.ITEM:                      # WO51: the site's or our own — the item back in line untouched
            db.upsert_listing(iid, mp, fields_json={**fields.model_dump(), "note": out.note})
            if health.non_item(db, mp, iid, out.error, kind, shot=out.screenshot) != "item":
                db.log(iid, "post_failed", {"mp": mp, "error": out.error, "shot": out.screenshot, "kind": kind,
                                            "note": out.note, "seconds": round(seconds, 1),
                                            "progress": list(getattr(poster, "lines", None) or []) or None})
                return out
    crosslist.record(s, db, iid, mp, title, out, fields.model_dump(), seconds=seconds, slept=slept,
                     lines=getattr(poster, "lines", None))
    if out.status == "failed" and not out.clicked and not dry:   # WO53: an unconfirmed publish never counts
        # WO54 rule 4: only DIFFERENT items failing in a row pause the site (the same one is set aside)
        health.brake(db, mp, iid, out.error, limit=int(s.get("poster.max_consecutive_failures", 3)))
    if out.status == "posted":
        health.worked(db, mp, iid)
        crosslist.set_unconf(db, iid, mp, stage=None)
    elif not out.clicked and (crosslist.unconf_state(db, iid, mp) or {}).get("stage") == "reposted":
        crosslist.set_unconf(db, iid, mp, stage="retry")   # nothing was clicked: the next try still looks first
    return out


async def publish_first_cross(s: Settings, db: DB, iid: str, mp: str, confirm) -> Outcome:
    """`thrift poster --publish-first <item> --marketplace depop|vinted` (WO30): the supervised first publish there —
    on the Mac, the poster service stopped, an item already live on Poshmark, at its approved price; the owner types
    POST at the filled form. Never retried automatically."""
    if not s.is_prod:
        raise RuntimeError("the supervised publish runs on the Mac only (machine_role: prod); the dev machine never "
                           "touches the shop")
    if mp not in crosslist.CROSS:
        raise ValueError(f"unknown marketplace {mp!r}")
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    posh = db.listing(iid, "poshmark")
    if posh is None or posh["status"] != "posted":
        raise ValueError(f"item {iid} isn't live on Poshmark: cross-listing follows Poshmark")
    row = db.listing(iid, mp)
    if row and (row["status"] in ("posted", "posting") or row["url"]):
        raise ValueError(f"{mp}: status {row['status']}{' with ' + row['url'] if row['url'] else ''} — it reached "
                         "the site: never post it twice")
    if row and (row["error"] or "").startswith(UNCONFIRMED):
        raise ValueError(f"{mp}: an earlier publish may have gone live — check the shop, then "
                         f"`thrift mark-posted {iid} {mp} <url>`")
    if s.flag_set("HOLD_UNSHIPPED"):
        raise ValueError(HOLD_REASON)
    if row is None:
        crosslist.queue(s, db, iid, [mp], why="publish-first")
    elif row["status"] in ("failed", "skipped", "dryrun"):
        db.upsert_listing(iid, mp, status="queued")
    view = ItemView.from_row(it)
    fields = map_fields(mp, view)
    ps = posters(s)
    poster = ps.get(mp)
    if poster is None:
        raise ValueError(f"marketplaces.{mp} is not enabled (or its catalog doesn't load)")
    pw = ctx = b = None
    extension = getattr(poster, "driver", "playwright") == "extension"
    if extension:                                                  # WO32: the Thrift Chrome, through this bridge
        try:
            b, _ = await open_bridge(s, None, {mp: poster}, watch=False, strict=True)
        except bridge_mod.BridgeError as e:
            raise RuntimeError(f"{mp}: the extension bridge didn't start: {e}") from None
        try:
            await connect_extension(b, poster)
        except BaseException:
            await close_bridge(b, None)
            raise
    else:
        try:
            pw, ctx = await open_browser(s.path("chrome_profile"), s["schedule"]["timezone"])
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Chrome didn't open ({type(e).__name__}) — is the poster service still running? Stop "
                               "it first: bash deploy/services.sh stop poster") from e
    render = cross_render_of(db, iid, mp, fields)
    try:
        db.upsert_listing(iid, mp, fields_json=fields.model_dump(), price=fields.price)
        if extension:            # WO32b: 'posting' only at the go-ahead, after POST — a Ctrl+C before it changes nothing
            if not db.begin_attempt(iid, mp):
                raise ValueError(f"{mp}: could not take {iid} (its row isn't queued)")
            poster.on_go_ahead = lambda: db.claim_listing(iid, mp, count=False)
            poster.progress = lambda line: print(f"  {line}", flush=True)
        elif not db.claim_listing(iid, mp):
            raise ValueError(f"{mp}: could not claim {iid}")
        poster.fields, poster.confirm, poster.strict = fields, confirm, True
        try:
            out = await poster.post(ctx, render, "publish", False, s.path("failed") / "shots")
        except AccountBlocked as e:
            db.upsert_listing(iid, mp, status="queued", error=str(e))
            notify.say(f"⛔ {crosslist.LABEL[mp]}: {e}")
            raise
        except (asyncio.CancelledError, KeyboardInterrupt):         # Ctrl+C at the terminal
            if extension and getattr(poster, "clicked", None):      # after the go-ahead: it may be live
                crosslist.record(s, db, iid, mp, view.render.title, Outcome(
                    "failed", clicked=True, error=f"interrupted (Ctrl+C) after the click on {crosslist.LABEL[mp]}, "
                                                  "before its page was seen"), fields.model_dump())
            elif extension:
                db.log(iid, "publish_cancelled", {"mp": mp, "why": "Ctrl+C before POST: nothing was published",
                                                  "status": (db.listing(iid, mp) or {})["status"]})
                print(f"\nCancelled — nothing was published on {crosslist.LABEL[mp]}; the tab is closed and the item "
                      "stays in line.", flush=True)
            raise
    finally:
        if ctx is not None:
            await ctx.close()
            await pw.stop()
        await close_bridge(b, None)
    crosslist.record(s, db, iid, mp, view.render.title, out, fields.model_dump())
    crosslist.announce(s, db, iid)
    return out


CROSSLIST_REQUESTS = "crosslist_requests"     # kv: [{"item", "mps", "at"}]: dry runs asked for by `thrift crosslist`


def request_dry_run(db: DB, iid: str, mps: list[str]) -> None:
    """`thrift crosslist --dry-run <item>` while the poster runs: the poster fills those forms between listings."""
    with db.tx():
        reqs = [r for r in (loads(db.kv_get(CROSSLIST_REQUESTS)) or []) if r.get("item") != iid]
        db.kv_set(CROSSLIST_REQUESTS, json.dumps([*reqs, {"item": iid, "mps": mps, "at": datetime.now(timezone.utc)
                                                           .isoformat(timespec="seconds")}]))
        db.log(iid, "crosslist_dry_run_requested", {"mps": mps})


def request_check_login(db: DB, mps: list[str]) -> None:
    """`thrift crosslist --check-login` while the poster runs: the sell pages opened in the Thrift Chrome (WO32)."""
    with db.tx():
        reqs = [r for r in (loads(db.kv_get(CROSSLIST_REQUESTS)) or []) if not r.get("check_login")]
        db.kv_set(CROSSLIST_REQUESTS, json.dumps([*reqs, {"check_login": True, "mps": mps, "at": datetime.now(
            timezone.utc).isoformat(timespec="seconds")}]))


async def check_logins(db: DB, ps: dict, mps: list[str]) -> list[str]:
    """Each marketplace's sell page in the Thrift Chrome: logged in → one ops line; a login / block / CAPTCHA page →
    the marketplace stops for the window with the owner's one line (WO32). Returns the lines said."""
    said = []
    for mp in mps:
        poster = ps.get(mp)
        if not hasattr(poster, "check_login"):
            continue
        try:
            await poster.check_login()
            said.append(f"✓ {crosslist.LABEL[mp]}: logged in — its sell form opens in the Thrift Chrome")
            notify.say(said[-1])
        except AccountBlocked as e:
            crosslist.block(db, mp, str(e), e.page)
            said.append(f"⛔ {crosslist.LABEL[mp]}: {e}")
        except Exception as e:  # noqa: BLE001 — a check never stops the poster
            said.append(f"❌ {crosslist.LABEL[mp]} login check: {type(e).__name__}: {e}")
            notify.say(said[-1])
    return said


async def serve_cross_requests(s: Settings, db: DB, ps: dict, ctx) -> list[str]:
    """The dry runs `thrift crosslist --dry-run` asked for: each marketplace's form filled for the item, the screenshot
    and the mapped fields to the ops chat, the tab closed — nothing published, no row changed. And the login checks
    `thrift crosslist --check-login` asked for (WO32)."""
    from thrift_agent import vinted_repost             # WO52 follow-up: a Vinted listing deleted and posted again
    for (key,) in db.conn.execute("SELECT key FROM kv WHERE key LIKE ? AND value != ''",
                                  (vinted_repost.BEFORE + "%",)).fetchall():
        vinted_repost.after_posted(s, db, key[len(vinted_repost.BEFORE):], "vinted")
    if "vinted" in ps:
        await vinted_repost.serve(db, ps)
    with db.tx():
        reqs = loads(db.kv_get(CROSSLIST_REQUESTS)) or []
        if reqs:
            db.kv_set(CROSSLIST_REQUESTS, "[]")
    done = []
    for req in reqs:
        if req.get("check_login"):
            await check_logins(db, ps, [mp for mp in req.get("mps") or [] if mp in ps])
            continue
        iid = req.get("item")
        for mp in req.get("mps") or []:
            if mp in ps and db.item(iid) is not None:
                try:
                    await run_cross(s, db, ps, ctx, iid, mp, dry=True, request=True)
                except Exception as e:  # noqa: BLE001 — never the poster's end
                    db.log(iid, "error", f"crosslist dry run {mp}: {type(e).__name__}: {e}")
                    notify.say(f"❌ dry run {crosslist.LABEL[mp]} ({iid}): {type(e).__name__}: {e}")
        done.append(iid)
    return done


def _checks(it) -> list[str]:
    """The copy's flags (the gate's "draft" reasons) of an item that publishes anyway: things to check by hand."""
    gate = loads(it["gate"]) if it is not None else None
    return list((gate or {}).get("reasons") or []) if (gate or {}).get("decision") == "draft" else []


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
              stage: str | None = None, takedowns=None) -> None:
    # Publishing for real takes two deliberate flips on the Mac: poster.dry_run off AND poster.autopublish_confirmed on.
    dry = (force_dry or not s.is_prod or s.get("poster.dry_run", True)
           or not s.get("poster.autopublish_confirmed", False))
    if not s.is_prod and not allow_dev_browser:
        raise RuntimeError(DEV_BROWSER_MSG)
    stage = dry_run_stage(s, stage)
    max_fail = int(s.get("poster.max_consecutive_failures", 3))
    ps = posters(s)
    from thrift_agent.post import parallel                     # WO33: the three site workers (else the loop below)
    if parallel.parallel_ok(s, ps, once):
        return await parallel.run_parallel(s, db, ps, dry=dry, stage=stage, force_dry=force_dry, takedowns=takedowns)
    stop = asyncio.Event()
    _install_stop(stop)
    bridge, watch = await open_bridge(s, db, ps)         # WO32: Depop / Vinted through the Thrift Chrome's extension
    for mp in [m for m, p in ps.items() if getattr(p, "driver", "") == "api"]:
        notify.say(f"❗ {crosslist.LABEL[mp]}: marketplaces.{mp}.driver is api, a stub until Depop's API key arrives "
                   f"(WO32) — its listings wait. Set it back to extension to list there.")
    try:
        pw, ctx = await open_browser(s.path("chrome_profile"), s["schedule"]["timezone"])
    except BaseException:
        await close_bridge(bridge, watch)
        raise
    started = f"Poster started ({f'DRY-RUN, {stage} stage' if dry else 'LIVE'}) — {', '.join(ps)}"
    # WO29: the ops chat (never the group); the time keeps a second start the same day from being "a repeat"
    notify.say(f"{started} · {datetime.now():%H:%M}" + (f"\n{why_dry(s, force_dry)}" if dry and s.is_prod else ""))
    print(started)
    db.log(None, "poster_started", {"live": not dry, "stage": None if not dry else stage, "pid": os.getpid()})
    awake = power.Awake()
    failures = 0
    cross = [mp for mp in ps if mp in crosslist.CROSS]
    if bad := {mp: why for mp, why in catalogs.check().items() if why}:
        notify.say("❗ cross-listing off: " + "; ".join(f"{mp}: {why}" for mp, why in bad.items()))
    last: str | None = None                              # the item in hand: its next marketplace comes first (WO30)
    try:
        await reconcile_stale(s, db, ps, ctx)            # a listing the Mac (or a crash) cut short: the closet first
        while True:
            daily.poster_beat(db, pid=os.getpid(), live=not dry, busy=None, next_at=None, paused=None, stopped=None)
            if stop.is_set():
                print("stop requested — exiting between items")
                return
            await serve_requests(s, db, ps, ctx)         # the owner's "posted <url>" replies, checked between items
            if cross:
                crosslist.new_window(s, db)              # a new window: blocks lifted, failures retried (WO30)
                if not _paused(db):
                    await serve_cross_requests(s, db, ps, ctx)
                    if asked := refresh.take_requests(db):             # `thrift catalogs refresh`
                        await _refresh(s, db, ctx, asked)
                    for site in ready_cross(ps, cross):  # WO35 §4: an unconfirmed publish, settled by the site's data
                        if not crosslist.blocked(db, site):
                            await recheck_unconfirmed(s, db, ps, site, ctx)
                        elif not dry and not s.flag_set("PAUSE") and (test := health.test_due(db, site)):
                            from thrift_agent.post import parallel   # WO51: a paused site's test run (dry), then back
                            await parallel._cross_test(s, db, ps, site, test)
                crosslist.retry_failed(s, db, at=datetime.now(timezone.utc))     # WO51: an item failure, 30 min on
            ready = ready_cross(ps, cross)                 # WO32: an extension marketplace while the extension is there
            hour_ago, midnight = windows(tz=s["schedule"]["timezone"])
            ok, why = can_post(s, db.listed_since(hour_ago, "poshmark"), db.listed_since(midnight, "poshmark"))
            hours_ok, _ = can_post(s, 0, 0)              # PAUSE and the hours: the cross-list jobs' own caps apply
            # HOLD_UNSHIPPED (invariant 8) holds publishing only: drafts and dry-runs never reach a buyer, and the
            # flag can appear mid-run (n8n sees a late order), so it is re-read every time round the loop.
            hold = s.flag_set("HOLD_UNSHIPPED")
            # Per item: Poshmark → Depop → Vinted, then the next item (WO30) — the item in hand goes on first.
            cont = crosslist.next_job(s, db, prefer=last, mps=ready) if ready and hours_ok and last and not dry else None
            cont = cont if cont and cont[0] == last else None
            job = next_job(s, db, list(ps), dry, allow_publish=not hold) if ok and cont is None else None
            cjob = cont or (crosslist.next_job(s, db, mps=ready) if ready and hours_ok and job is None and not dry
                            else None)
            if not dry:
                _tell_held(s, db, list(ps))              # approved items it won't publish: told once each
            if (job is not None or cjob is not None) and (paused := _paused(db)):
                daily.poster_beat(db, paused=paused)     # lid closed / battery low: no new listing (WO28 §3, §5)
                job = cjob = None
                why = f"paused: {paused}"
            if job is None and cjob is not None:
                iid, mp = cjob
                last = iid
                awake.hold(True)
                daily.poster_beat(db, busy=iid)
                await run_cross(s, db, ps, ctx, iid, mp, dry=not crosslist.live(s, mp), hold=hold)
                if once:
                    crosslist.announce(s, db, iid)
                    return
                await _pause(stop, await _between(s, db, iid, awake, stop, hours_ok and not dry,
                                                  ready_cross(ps, cross)))
                continue
            if job is None:
                awake.hold(False)
                if once:
                    print(f"nothing to do ({HOLD_REASON if ok and hold else why})")
                    return
                if cross and not _paused(db) and (due := [m for m in cross if crosslist.driver(s, m) == "playwright"
                                                          and refresh.due(s, db, m)]):
                    await refresh.run(s, db, ctx, due)   # once a week, while idle in the daily window (WO30 §8)
                await _pause(stop, 60)
                continue

            iid, mp, render, mode = job
            if not db.claim_listing(iid, mp):    # another poster process took it, or its status moved under us
                continue
            last = iid
            awake.hold(True)                            # no idle sleep in the middle of a listing (WO28 §5)
            daily.poster_beat(db, busy=iid)
            t0, m0, k0 = time.time(), time.monotonic(), power.last_wake()
            try:
                out = await ps[mp].post(ctx, render, mode, dry, s.path("failed") / "shots", stage=stage)
            except AccountBlocked as e:
                db.upsert_listing(iid, mp, status="queued", error=str(e))
                _halt(s, str(e), f"⛔ Poster paused: {e}\nFix it in the poster Chrome window, then delete the PAUSE file.")
                return
            except Exception as e:  # noqa: BLE001
                # Poster.post() turns everything after the page opens into an Outcome, so an exception reaching
                # here came from before the form was touched — nothing was submitted, and 'queued' cannot
                # double-post. It is still an unknown failure mode, so pause rather than retry (invariant 5).
                err = f"{type(e).__name__}: {e}"
                db.upsert_listing(iid, mp, status="queued", error=err)
                _halt(s, err, f"⛔ Poster paused: {mp} raised before the form ({err}).\nFix it, then delete the PAUSE file.")
                return

            seconds = time.monotonic() - m0
            slept = power.slept_since(t0, k0, m0)
            requeued = False
            if slept:                                   # nothing submitted: it goes again, after the usual pause
                out, requeued = await after_sleep(db, ps, ctx, iid, mp, render, out, t0)
            if not requeued:
                record_outcome(db, iid, mp, render, out, list(ps), stage,
                               bool(s.get("poster.notify_dry_runs", False)), seconds=seconds, slept=slept, s=s,
                               announce=False)
                # Circuit breaker: N failures in a row means the form, the account or the network changed, not
                # the items. Every further attempt is 16 uploads of noise on the account, so stop and ask. A skipped
                # item is not one: the form and the account are fine, the item is unusual (WO27). Nor is a listing
                # the Mac slept through before its final click (WO28): it simply goes again.
                failures = failures + 1 if out.status == "failed" else 0 if out.status != "skipped" else failures
                if failures >= max_fail:
                    _halt(s, f"{failures} consecutive failures: {out.error}",
                          f"⛔ Poster paused after {failures} consecutive failures: {out.error}\n"
                          f"Fix it, then delete the PAUSE file.")
                    return
            if once:
                crosslist.announce(s, db, iid)
                return
            await _pause(stop, await _between(s, db, iid, awake, stop, bool(cross) and hours_ok and not dry,
                                              ready_cross(ps, cross)))
    finally:
        awake.hold(False)
        daily.poster_beat(db, stopped=True, busy=None, next_at=None)
        await close_bridge(bridge, watch)
        await ctx.close()
        await pw.stop()


async def _between(s: Settings, db: DB, iid: str, awake, stop, cross_ok: bool, mps: list[str] | None = None) -> float:
    """After a listing: the item's next marketplace follows in 5–15 s (Poshmark → Depop → Vinted, WO30, WO32b); else the
    item's round is over — its one "Posted ✓" line — and the human pause before the next item. `mps`: the cross-list
    marketplaces that can take a job now (WO32)."""
    cross_ok = cross_ok and mps != []
    following = crosslist.next_job(s, db, prefer=iid, mps=mps) if cross_ok else None
    if following and following[0] == iid:
        gap = crosslist.gap(s)
        awake.hold(True)
        daily.poster_beat(db, busy=None, next_at=daily.in_seconds(gap))
        return gap
    crosslist.announce(s, db, iid)
    gap = next_gap(s["schedule"])
    more = (next_job(s, db, ["poshmark"], False, allow_publish=not s.flag_set("HOLD_UNSHIPPED")) is not None
            or (cross_ok and crosslist.next_job(s, db, mps=mps) is not None))
    awake.hold(more)                            # awake through the human pause only if a listing follows
    daily.poster_beat(db, busy=None, next_at=daily.in_seconds(gap) if more else None)
    return gap


async def _refresh(s: Settings, db: DB, ctx, asked: list[str]) -> None:
    """`thrift catalogs refresh`, between listings: through the poster profile for a Playwright-driven marketplace;
    never for one on the extension (WO32: the driven browser is turned away there, and Vinted then flags the network)."""
    if mine := [m for m in asked if crosslist.driver(s, m) == "playwright"]:
        await refresh.run(s, db, ctx, mine)
    if other := [m for m in asked if m not in mine]:
        notify.say(f"catalog refresh: not for {', '.join(crosslist.LABEL.get(m, m) for m in other)} — on the "
                   f"extension driver the poster profile doesn't visit it (WO32); the saved catalogs stay")


def _paused(db: DB) -> str | None:
    """Why the poster starts no new listing now, or None: the lid is closed (a short maintenance wake: the Mac would
    sleep again mid-listing), or the battery rule (WO28 §5: below 15% on battery, until charging or 20%)."""
    if power.lid_closed() is True:
        return "lid closed"
    if power.publish_paused(db, power.battery(), True, notify.group):     # the battery line: the group (WO29)
        return "battery low"
    return None


async def after_sleep(db: DB, ps: dict, ctx, iid: str, mp: str, render: Render, out: Outcome,
                      started: float) -> tuple[Outcome, bool]:
    """A listing the Mac slept through (WO28 §3). After the final click without an address: the closet is looked at
    once more (created_listing_id, else exactly this title, since the listing started) — found, it is posted with its
    URL; not found, it stays "unconfirmed" (never retried) and the owner gets ONE message. Before the final click
    nothing was submitted: the row goes back to the queue (at most SLEEP_RETRIES attempts) — True. Anything else is
    left as it is."""
    if out.status != "failed":
        return out, False
    if out.clicked and not out.url:
        try:
            url, seen = await ps[mp].find_live(ctx, render, since=started, created=out.created_id)
        except Exception as e:  # noqa: BLE001 — can't look: it stays unconfirmed, the owner looks
            url, seen = None, {"error": f"{type(e).__name__}: {e}"}
        db.log(iid, "closet_check", {"mp": mp, "found": url, "seen": seen, "why": "slept while publishing"})
        if url:
            note = "; ".join(x for x in (out.note, "found in the closet after the Mac woke up") if x)
            return Outcome("posted", url=url, screenshot=out.screenshot, clicked=True, note=note,
                           guesses=out.guesses, created_id=out.created_id), False
        return out, False
    row = db.listing(iid, mp)
    if not out.clicked and row is not None and row["attempts"] < SLEEP_RETRIES:
        db.upsert_listing(iid, mp, status="queued", error=SLEPT + (out.error or ""))
        db.log(iid, "post_requeued_after_sleep", {"mp": mp, "error": out.error, "attempts": row["attempts"]})
        return out, True
    return out, False


async def reconcile_stale(s: Settings, db: DB, ps: dict, ctx) -> list[str]:
    """At the poster's start (its Chrome profile is ours: no other poster can be in the middle of a listing): a row
    still 'posting' was cut short — the Mac slept for good, shut down, or the process died. Never retried (invariant
    4): the closet is looked at — found, it is posted with its URL; not found (or unknown), it is "unconfirmed" and
    the owner gets the ONE message. Returns the items looked at."""
    done = []
    for row in db.conn.execute("SELECT * FROM listings WHERE status='posting'").fetchall():
        iid, mp = row["item_id"], row["marketplace"]
        it, poster = db.item(iid), ps.get(mp)
        renders = (loads(it["renders"]) or {}) if it is not None else {}
        if poster is None or "poshmark" not in renders:
            continue
        render = Render.model_validate(renders["poshmark"]) if mp == "poshmark" else cross_render_of(db, iid, mp)
        since = datetime.fromisoformat(row["updated_at"]).timestamp()        # the claim: just before the form
        try:
            url, seen = await poster.find_live(ctx, render, since=since)
        except Exception as e:  # noqa: BLE001 — it stays 'posting': looked at again at the next start
            db.log(iid, "error", f"closet check of a stale listing: {type(e).__name__}: {e}")
            continue
        db.log(iid, "closet_check", {"mp": mp, "found": url, "seen": seen, "why": "posting at start"})
        out = Outcome("posted", url=url, clicked=True, note="found in the closet: the Mac slept (or the poster "
                      "stopped) in the middle of this listing") if url else \
            Outcome("failed", clicked=True, error="the Mac slept (or the poster stopped) in the middle of this listing "
                    "and it isn't in the closet")
        if mp == "poshmark":
            record_outcome(db, iid, mp, render, out, list(ps), slept=True, s=s)
        else:
            crosslist.record(s, db, iid, mp, render.title, out, loads(row["fields_json"]) or {}, slept=True)
            crosslist.announce(s, db, iid)
        done.append(iid)
    return done


async def serve_requests(s: Settings, db: DB, ps: dict, ctx) -> list[str]:
    """The owner's "posted <url>" for an unconfirmed listing (a Telegram reply, or `thrift mark-posted` while the
    poster runs), queued by pipeline.request_posted: each is checked here, between listings, in the poster's own
    browser. A wrong address is said once (WO53: nothing asked back — the row stays "verifying" and the site's own data
    settles it). Returns the items done."""
    done = []
    for req in pipeline.take_requests(db, list(ps)):
        iid, mp, url = req.get("item"), req.get("mp") or "poshmark", req.get("url")
        try:
            done.append(await confirm_live(s, db, ps, ctx, iid, mp, url) and iid)
        except (ValueError, RuntimeError) as e:
            db.log(iid, "post_confirm_refused", {"mp": mp, "url": url, "error": str(e)})
            notify.say(f"⚠️ {iid}: the link {url} doesn't show this item ({e}) — it stays verifying by the site's own "
                       f"data")
    return done
