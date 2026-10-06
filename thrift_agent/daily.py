"""The daily window (WO28): the Mac is opened about once a day for ~30 minutes, often on battery, then closed.

On worker start and on every wake: the same catch-up as a restart — the inbox looked at, the open card re-sent only if
it is older than telegram.resend_after_hours — and ONE line, "Back online — …", when there is work. During the
window, ONE status message per window is kept up to date by editing it: "⏳ Working …, please don't close the Mac
yet" while items are processed or listings published, "✓ Safe to close …" once only the owner's answers (or the listing
hours) are left, "✓ All done — safe to close the Mac." when nothing is. Nothing is said while the lid is closed (the
Mac's short maintenance wakes at night). The estimate uses the real timings of the last runs.

WO29 (a quiet group): "Back online" and the status message go to the OPS chat (the owner's private chat; `thrift
status` shows the message too). The group's only "safe to close" is the line the last "Posted ✓" of a window carries
(all_done); the battery line is one of the group's plain action-needed lines."""
from __future__ import annotations

import json
import math
import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from thrift_agent import approve, notify, power
from thrift_agent.config import Settings
from thrift_agent.db import DB, loads
from thrift_agent.scheduler import can_post, in_hours, windows

POSTER_STATE = "poster_state"      # kv: the poster's heartbeat and what it is doing (runner writes it)
POSTER_FRESH_S = 180               # an older heartbeat: the poster isn't running — unless it said why it is quiet:
POSTER_BUSY_S = 900                # ... in the middle of a listing (one never takes this long), or in its human pause
SESSION_KEY = "daily_session"      # kv: when the current window began (worker start, or a wake with the lid open)
STATUS_KEY = "daily_status"        # kv: {"session", "message_id", "text", "worked"} — the window's status message
DEFAULT_S = {"batch": 60.0, "item": 120.0, "post": 180.0}   # until the event log has real timings
LAST_N = 20                        # the timings are the medians of this many latest runs
ALL_DONE = "✓ All done — safe to close the Mac."
DONT_CLOSE = "Please don't close the Mac yet."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


# ---------- what there is to do ----------

@dataclass(frozen=True)
class Work:
    new_shares: int        # shares registered, not yet split into items
    processing: int        # items being processed
    cards: int             # questions waiting for the owner's answer
    to_publish: int        # approved listings the poster will publish
    held: int              # approved listings that need the owner first (never published by the loop)

    @property
    def busy(self) -> bool:
        return self.new_shares + self.processing > 0

    @property
    def items_waiting(self) -> int:
        return self.processing + self.cards + self.to_publish + self.held


def live(s: Settings) -> bool:
    """The poster publishes by itself: the Mac, and both keys (WO16)."""
    return bool(s.is_prod and not s.get("poster.dry_run", True) and s.get("poster.autopublish_confirmed", False))


def work(s: Settings, db: DB) -> Work:
    from thrift_agent.post import runner           # the poster's own rules: what it takes, what it leaves alone
    q = approve.queue(db)
    enabled = [mp for mp, c in (s.get("marketplaces") or {}).items() if (c or {}).get("enabled")]
    publishable = {iid for iid, _ in runner.publishable(s, db, enabled)} if live(s) else set()
    if live(s):                                        # the one being published right now is still to publish
        publishable |= {r[0] for r in db.conn.execute("SELECT item_id FROM listings WHERE status='posting'")}
        from thrift_agent import crosslist              # WO30: Depop and Vinted still to do count as work
        publishable |= {iid for iid, _ in crosslist.pending(s, db)}
    approved = {it["id"] for it in db.items("ready") if it["owner_price"]}
    held = {iid for iid, _, _ in runner.held(s, db, enabled)} if live(s) else approved - publishable
    return Work(new_shares=sum(1 for kind, _ in q if kind == approve.NEW_BATCH),
                processing=sum(1 for kind, _ in q if kind == approve.NEW_ITEM),
                cards=sum(1 for kind, _ in q if kind not in (approve.NEW_BATCH, approve.NEW_ITEM)),
                to_publish=len(publishable), held=len(held - publishable))


# ---------- how long things take (the last runs) ----------

@dataclass(frozen=True)
class Timing:
    batch: float           # seconds to split a share into items
    item: float            # seconds to process one item
    post: float            # seconds to publish one listing
    gap: float             # seconds of the poster's human pause between two listings


def _median(values: list[float], default: float) -> float:
    values = [v for v in values if v and v > 0][:LAST_N]
    return float(statistics.median(values)) if values else default


def timings(s: Settings, db: DB) -> Timing:
    """Medians of the last runs: the worker's "worked" events (seconds per batch / item), the poster's post_posted
    events (seconds per listing) and the pauses between consecutive listings (under an hour apart: one window)."""
    worked = [loads(r[0]) or {} for r in db.conn.execute(
        "SELECT detail FROM events WHERE kind='worked' ORDER BY rowid DESC LIMIT 400")]
    posts = [(datetime.fromisoformat(r[0]), (loads(r[1]) or {}).get("seconds")) for r in db.conn.execute(
        "SELECT ts, detail FROM events WHERE kind='post_posted' ORDER BY rowid DESC LIMIT 60")]
    gaps = []
    for (later, secs), (earlier, _) in zip(posts, posts[1:]):
        gap = (later - earlier).total_seconds() - (secs or 0)
        if 0 < gap < 3600:
            gaps.append(gap)
    lo, hi = (s.get("schedule.gap_seconds") or [150, 420])[:2]
    return Timing(batch=_median([w.get("seconds") for w in worked if w.get("kind") == "batch"], DEFAULT_S["batch"]),
                  item=_median([w.get("seconds") for w in worked if w.get("kind") == "item"], DEFAULT_S["item"]),
                  post=_median([secs for _, secs in posts], DEFAULT_S["post"]),
                  gap=_median(gaps, (lo + hi) / 2 * 1.2))       # next_gap: a 2-4x longer break one time in ten


# ---------- the poster, as it last said ----------

@dataclass(frozen=True)
class PosterNow:
    running: bool
    live: bool = False
    busy: str | None = None            # the item it is publishing right now
    next_at: datetime | None = None    # when its human pause ends
    paused: str | None = None          # why it starts no listing: lid, battery, ...


def poster_now(db: DB, now: datetime | None = None) -> PosterNow:
    st = loads(db.kv_get(POSTER_STATE)) or {}
    now = now or _now()
    beat = datetime.fromisoformat(st["beat"]) if st.get("beat") else None
    nxt = datetime.fromisoformat(st["next_at"]) if st.get("next_at") else None
    if st.get("stopped") or beat is None:
        return PosterNow(running=False)
    quiet = (now - beat).total_seconds()                  # it beats once a minute, except in a listing or a pause
    if quiet > POSTER_FRESH_S and not (st.get("busy") and quiet <= POSTER_BUSY_S) and \
            not (nxt is not None and now <= nxt + timedelta(seconds=POSTER_FRESH_S)):
        return PosterNow(running=False)
    return PosterNow(True, bool(st.get("live")), st.get("busy"), nxt, st.get("paused"))


def blocked(s: Settings, db: DB, now: datetime | None = None) -> tuple[bool, str | None, str]:
    """(inside the listing hours, why listings can't go up now or None, the hour they open). The daily cap counts as
    outside the hours: the listings go up next time."""
    now = now or _now()
    sch = s["schedule"]
    local = now.astimezone(ZoneInfo(sch["timezone"]))
    opens = str(sch["hours"][0]).strip()
    hours_open = in_hours(local, sch["hours"])
    if s.flag_set("PAUSE"):
        return hours_open, "publishing is paused (the PAUSE file in iCloud Drive → Posh)", opens
    if s.flag_set("HOLD_UNSHIPPED"):
        return hours_open, "unshipped orders hold publishing", opens
    if db.kv_get(power.BATTERY_KEY):
        return hours_open, "the battery is low — they go on once the Mac is charging", opens
    hour_ago, midnight = windows(now, sch["timezone"])
    ok, why = can_post(s, db.listed_since(hour_ago), db.listed_since(midnight), now)
    if not ok and why == "daily cap reached":
        return False, None, opens
    return hours_open, None, opens


def status_text(w: Work, p: PosterNow, t: Timing, hours_open: bool, block: str | None, opens: str,
                now: datetime) -> str:
    """The window's status line (WO28 §2). Waiting for the owner is never "working"; approved listings are, while
    the poster can publish them (inside the hours, nothing blocking, the poster running and live)."""
    publishing = w.to_publish > 0 and hours_open and block is None and p.running and p.live
    if w.busy or publishing:
        wait_next = max(0.0, (p.next_at - now).total_seconds()) if p.next_at and not p.busy else 0.0
        secs = w.new_shares * t.batch + w.processing * t.item
        if publishing:
            secs += wait_next + w.to_publish * t.post + max(0, w.to_publish - 1) * t.gap
        mins = max(1, math.ceil(secs / 60))
        if w.busy:
            left = [x for x in ((_n(w.new_shares, "new share") if w.new_shares else ""),
                                _n(w.processing + (w.to_publish if publishing else 0), "item")) if x]
            head = f"⏳ Working — {' and '.join(left)} left, about {mins} min. {DONT_CLOSE}"
        else:
            nxt = "publishing now" if p.busy else (f"next in ~{max(1, math.ceil(wait_next / 60))} min"
                                                   if wait_next >= 30 else "next in a moment")
            head = f"⏳ {_n(w.to_publish, 'listing')} still to publish, {nxt}. {DONT_CLOSE}"
        also = f"\n{_n(w.cards, 'card is', 'cards are')} also waiting for your answer." if w.cards else ""
        return head + also
    parts = []
    if w.to_publish:
        if not hours_open:
            parts.append(f"{_n(w.to_publish, 'listing')} will go up after {opens} next time the Mac is open")
        elif block:
            parts.append(f"{_n(w.to_publish, 'listing')} wait: {block}")
        elif not (p.running and p.live):
            parts.append(f"{_n(w.to_publish, 'listing')} wait for the poster (it isn't running)")
    if w.held:
        parts.append(f"{_n(w.held, 'listing needs', 'listings need')} your look before publishing "
                     "(see “Not published automatically”)")
    if w.cards:
        parts.append(f"{_n(w.cards, 'card is', 'cards are')} waiting for your answer in Telegram "
                     "(answers within 24 h are kept)")
    return f"✓ Safe to close — {'; '.join(parts)}" if parts else ALL_DONE


def all_done(s: Settings, db: DB) -> bool:
    """Nothing left in this window (WO29): nothing being processed, no listing to publish (or being published), no
    card waiting for an answer, none held for a look. The "Posted ✓" that leaves it so carries "✓ All done — safe to
    close the Mac."."""
    w = work(s, db)
    return not (w.new_shares or w.processing or w.cards or w.to_publish or w.held)


def back_online(w: Work) -> str | None:
    """ "Back online — 2 new shares, 3 items waiting" (WO28 §1), or None when there is no work."""
    parts = [_n(w.new_shares, "new share")] if w.new_shares else []
    if w.items_waiting:
        parts.append(f"{_n(w.items_waiting, 'item')} waiting")
    return f"Back online — {', '.join(parts)}" if parts else None


# ---------- the window ----------

class Window:
    """The worker's side of the daily window. step() before each tick: a start or a wake (with the lid open) begins a
    window — the catch-up and "Back online". update() after each tick: the status message, the idle-sleep assertion
    while the worker has processing to do, the battery line. Every seam is injectable (tests)."""

    def __init__(self, s: Settings, db: DB, bot=None, *, say: Callable[[str], object] | None = None,
                 group: Callable[[str], object] | None = None,
                 scan: Callable[[], object] | None = None, lid: Callable[[], bool | None] = power.lid_closed,
                 watch: power.WakeWatch | None = None, awake: power.Awake | None = None,
                 battery: Callable[[], power.Battery | None] = power.battery, now: Callable[[], datetime] = _now):
        self.s, self.db, self.bot, self.scan = s, db, bot, scan
        self.say = say or (lambda text: notify.say(text))          # looked up when used: the ops chat
        self.group = group or (lambda text: notify.group(text))    # ... and the group (the battery line)
        self.lid, self.battery, self.now = lid, battery, now
        self.watch = watch or power.WakeWatch()
        self.awake = awake or power.Awake()
        self._first, self._lid_was_closed = True, None
        self.lid_closed = False             # as of the last step(): the worker starts nothing while it is closed

    def step(self) -> str | None:
        """Before a tick: "start" or "wake" when a window begins now (its catch-up done), else None. With the lid
        closed nothing is said; the window begins when the lid opens."""
        woke = self.watch.check()
        closed = self.lid()
        opened = self._lid_was_closed is True and closed is False
        self._lid_was_closed, self.lid_closed = closed, closed is True
        if closed is True:
            if woke:
                self.db.log(None, "woke", {"lid": "closed"})
            return None
        if not (self._first or woke or opened):
            return None
        why = "start" if self._first else "wake"
        self._first = False
        self.begin(why)
        return why

    def begin(self, why: str) -> None:
        """A new window: its own status message from now on; the inbox looked at; "Back online" when there is work;
        the open card re-sent only if it is older than telegram.resend_after_hours (the 24 h Telegram keeps
        answers: one older than that is lost and the card simply comes again)."""
        stamp = self.now().isoformat(timespec="seconds")
        old = loads(self.db.kv_get(STATUS_KEY)) or {}
        if old.get("message_id") and self.bot is not None:     # ONE status message: the last window's goes
            try:
                self.bot.delete_message(int(old["message_id"]))
            except Exception:  # noqa: BLE001 — older than Telegram's 48 h, or already gone: it stays, harmless
                pass
        self.db.kv_set(STATUS_KEY, "{}")
        self.db.kv_set(SESSION_KEY, f"{stamp}#{uuid.uuid4().hex[:6]}")
        self.db.log(None, "window", {"why": why, "at": stamp})
        if self.scan is not None:
            try:
                self.scan()
            except Exception as e:  # noqa: BLE001 — the tick reports inbox trouble; the window goes on
                self.db.log(None, "error", f"window scan: {type(e).__name__}: {e}")
        if text := back_online(work(self.s, self.db)):
            self.say(text)
        try:
            approve.resend_pending(self.s, self.db)
        except Exception as e:  # noqa: BLE001
            self.db.log(None, "error", f"window resend: {type(e).__name__}: {e}")

    def update(self) -> str | None:
        """After a tick: the status message (edited, never re-sent), the idle-sleep assertion, the battery line.
        Returns the status text shown, or None (lid closed)."""
        if self.lid() is True:
            return None
        now = self.now()
        w = work(self.s, self.db)
        self.awake.hold(w.busy)
        p = poster_now(self.db, now)
        publishing_left = w.to_publish > 0 and live(self.s)
        power.publish_paused(self.db, self.battery(), w.busy or publishing_left, self.group)   # the group: plain
        hours_open, block, opens = blocked(self.s, self.db, now)
        text = status_text(w, p, timings(self.s, self.db), hours_open, block, opens, now)
        self.show(text)
        return text

    def show(self, text: str) -> None:
        """ONE status message per window, edited when the text changes (WO28 §2). A window in which nothing happened
        sends no "All done" out of the blue; a message that can't be edited any more is sent anew."""
        session = self.db.kv_get(SESSION_KEY) or ""
        st = loads(self.db.kv_get(STATUS_KEY)) or {}
        if st.get("session") != session:
            st = {"session": session}
        if st.get("text") == text:
            return
        worked = bool(st.get("worked")) or text != ALL_DONE
        if not worked:
            return
        if self.bot is None:
            self.say(text)
        elif not notify.ops_down(self.db):              # the ops chat (WO29); not reached yet: tried again later
            try:
                if st.get("message_id"):
                    try:
                        self.bot.edit_message(int(st["message_id"]), text)
                    except RuntimeError as e:
                        if "not modified" not in str(e):
                            st["message_id"] = None
                if not st.get("message_id"):
                    st["message_id"] = self.bot.send_message(text)
            except Exception as e:  # noqa: BLE001 — never the group, never the worker's end: logged, dropped
                notify.mark_ops_down(self.db, f"status message: {type(e).__name__}: {e}")
        st.update(text=text, worked=worked)
        self.db.kv_set(STATUS_KEY, json.dumps(st))


def poster_beat(db: DB, **state) -> None:
    """The poster's heartbeat (runner): what it is doing, for the window's status message."""
    st = loads(db.kv_get(POSTER_STATE)) or {}
    st.update(state, beat=_now().isoformat(timespec="seconds"))
    db.kv_set(POSTER_STATE, json.dumps(st))


def in_seconds(seconds: float) -> str:
    return (_now() + timedelta(seconds=seconds)).isoformat(timespec="seconds")
