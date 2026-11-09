"""Cross-listing on Depop and Vinted (WO30): once an item is live on Poshmark, the same item at the same approved price
goes up on Depop, then Vinted — no extra questions, every dropdown value from the saved catalogs (thrift_agent.catalogs).

- A `listings` row per marketplace: queued when Poshmark posts (or by `thrift crosslist`), then the poster takes it,
  right after the item's Poshmark listing (5–15 s apart, `crosslist.gap_seconds`, WO32b), at any hour outside the
  owner's quiet hours (WO54) and within `marketplaces.<m>.daily_cap` (25 a day each).
- The same two keys as Poshmark (poster.dry_run off + poster.autopublish_confirmed on) AND
  `marketplaces.<m>.autopublish`; otherwise a dry run: the whole form filled, the screenshot and the mapped fields to the
  ops chat, the tab closed without publishing.
- A logged-out / CAPTCHA / verification wall stops that marketplace for the window (one plain line in the group); a
  failure before the publish button is retried next window (3 attempts, then skipped with an ops note); a publish the
  Mac was interrupted in is looked for in that site's shop (WO28), never published again blind.
- The group hears ONE card per item (WO34), sent the first time the item is live on ANY site (WO54 — live 2026-11-09:
  it had waited for every site to settle, and with Poshmark paused 19 items live on Depop and Vinted got no card and
  no link), a "⏳ <Site> — …" line for each site still to come, edited in place as each other site lands, fails for
  good (left out) or changes state; the details go to the ops chat."""
from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thrift_agent import catalogs, daily, notify
from thrift_agent.config import Settings
from thrift_agent.brain.gate import NO_BRAND_NOTE
from thrift_agent.db import DB, listing_id_from, loads

CROSS = catalogs.CROSS
ORDER = ("poshmark", *CROSS)
LABEL = {"poshmark": "Poshmark", "depop": "Depop", "vinted": "Vinted"}
BLOCKS = "crosslist_blocked"           # kv: {mp: {"window", "reason", "since"}}: stopped for this window
WINDOW_SEEN = "crosslist_window"       # kv: the daily window the poster last saw
STREAK = "crosslist_failures"          # kv: {mp: failures in a row}: N in a row stop that marketplace for the window
EXT_LINK = "ext_link"                  # kv: {"connected", "at"}: the poster's view of the Thrift Chrome extension (WO32)
MAX_ATTEMPTS = 3                       # a failure before the publish button: retried next window, up to this many
UNCONFIRMED = "unconfirmed publish: "
SKIPPED = "skipped: "
REFUSED = " refused the form: "      # WO39: "Vinted refused the form: …" — the site kept its sell form after the click
MISMATCH = "Mismatch: "                 # WO36 §4: the form read back other than planned — the listing waits
# The group's one plain line when a marketplace stops for the window, by what the site showed (WO32).
ALERTS = {"login": "{site} needs you to log in on the Mac.",
          "captcha": "{site} shows a CAPTCHA — solve it in its Chrome window on the Mac.",
          "verify": "{site} asks for a check — open it on the Mac.",
          "block": "{site} turned the Mac away for now — I'll try again by myself later."}
DRIVERS = {"depop": ("extension", "playwright", "api"), "vinted": ("extension", "playwright")}
ALL_DONE_LINE = "💤 All done — nothing is waiting."             # WO34: the group card's last line (WO52 §6: no Mac)
DOT = {"poshmark": "🟣", "depop": "🔴", "vinted": "🟢"}
CARRIER = "posted_done_carrier"         # kv: the item whose group card carries the 💤 line now (WO34)
LATER = ("queued", "posting", "dryrun", "failed")   # a site still to come: "⏳ Vinted — later"
WAS_LIVE = ("posted", "sold", "delisted")           # a site the card links to (WO54: edits keep a sold one's link)
UNCONF = "unconfirmed_state"   # kv (WO35 §4): "<item>:<mp>" → {"stage": wait|retry|reposted|given_up, "due", "round",
                               # "tries"}: an unconfirmed publish on Depop / Vinted, settled by the site's own data
RECHECK_AFTER = 600            # ... looked for again, quietly, this long after it counted as unconfirmed
RECHECK_TRIES = 6              # ... the site's data unreadable this many times in a row: the ops chat is told


def enabled(s: Settings) -> list[str]:
    """The cross-list marketplaces that are on and whose catalog loads (a broken catalog turns only that one off)."""
    ok = catalogs.check()
    return [mp for mp in CROSS if s.get(f"marketplaces.{mp}.enabled", False) and ok.get(mp) is None]


def driver(s: Settings, mp: str) -> str:
    """How a marketplace is posted to (WO32): "extension" — the Thrift Chrome's extension, the seller's own Chrome (the
    default); "playwright" — WO30's poster profile (Vinted and Depop turn it away); "api" — Depop's Selling API (a
    stub until its key arrives)."""
    d = str(s.get(f"marketplaces.{mp}.driver") or "extension").strip().lower()
    if d not in DRIVERS.get(mp, ("playwright",)):
        raise ValueError(f"marketplaces.{mp}.driver must be one of {', '.join(DRIVERS.get(mp, ()))}, not {d!r}")
    return d


def alert_kind(reason: str, page: str | None = None) -> str:
    """What the site showed, for the owner's line: the poster's own word for it, else read from the reason."""
    if page in ALERTS:
        return page
    low = reason.lower()
    if "captcha" in low:
        return "captcha"
    if "verif" in low or "check" in low:
        return "verify"
    if "turned" in low or "block" in low or "403" in low or "not authorized" in low:
        return "block"
    return "login"


def reachable(s: Settings, db: DB) -> list[str]:
    """The enabled marketplaces the poster can work on now: one on the extension driver only while the poster sees the
    extension connected (WO32) — for the daily window's "still to publish", which mustn't wait on a missing Chrome."""
    link = loads(db.kv_get(EXT_LINK)) or {}
    return [mp for mp in enabled(s) if driver(s, mp) == "playwright"
            or (driver(s, mp) == "extension" and link.get("connected"))]


def live(s: Settings, mp: str) -> bool:
    """Publishes for real: on the Mac, both poster keys AND the marketplace's own autopublish (WO30 §5)."""
    return bool(s.is_prod and not s.get("poster.dry_run", True) and s.get("poster.autopublish_confirmed", False)
                and s.get(f"marketplaces.{mp}.autopublish", False))


def daily_cap(s: Settings, mp: str) -> int:
    return int(s.get(f"marketplaces.{mp}.daily_cap") or s["schedule"]["daily_cap"])


def gap(s: Settings) -> float:
    lo, hi = s.get("crosslist.gap_seconds") or [5, 15]
    return random.uniform(float(lo), float(hi))


def listing_gap(s: Settings, mp: str) -> float:
    """The pause after a listing on `mp` before its next one: Depop and Vinted their own `marketplaces.<m>.gap_seconds`,
    Poshmark the schedule's — all [0, 0] since WO37 (the owner: each site goes straight on, no longer breaks)."""
    from thrift_agent.scheduler import next_gap
    own = s.get(f"marketplaces.{mp}.gap_seconds") if mp != "poshmark" else None
    if not own:
        return next_gap(s["schedule"])                 # unset: the schedule's (WO37: [0, 0], no pause)
    lo, hi = own[:2]
    return random.uniform(float(lo), float(hi)) if float(hi) > 0 else 0.0     # [0, 0]: none (the owner, WO37)


# WO37: what the private chat needn't hear about a listing that went up — the form's routine (the category menu read, a
# suggestion of the site's own removed or set back, a value added back), and a "problem" banner the site showed after
# Post for a listing that IS live (Depop's "There was a problem listing your product", nine times on 2026-10-12)
ROUTINE = re.compile(r"^(?:category menu:|removed '|'[^']*' was gone from|Depop changed the |closed the open |"
                     r"after the click: form page: refused: |Boost was on: turned off|the (?:size|condition|category) "
                     r"read |\w+ read '.*': set to |package: Depop's '[^']*' kept|Depop's package size '[^']*' .* is "
                     r"below ours)", re.I)     # 2026-10-22: Depop's bigger package kept / a smaller one set back: routine


def noteworthy(note: str | None) -> str | None:
    """The parts of a posted listing's note the owner should know (a value guessed or left out), or None."""
    parts = [p.strip() for p in re.split(r";\s+", note or "") if p.strip()]
    keep = [p for p in parts if not ROUTINE.match(p) and not p.startswith("looked up:")]
    return "; ".join(keep) or None


def unconfirmed_ref(iid: str, mp: str) -> str:
    """The outbox ref of an "unconfirmed publish" message: the item, plus the marketplace when it isn't Poshmark."""
    return iid if mp == "poshmark" else f"{iid}:{mp}"


def split_ref(ref: str) -> tuple[str, str]:
    iid, _, mp = ref.partition(":")
    return iid, (mp or "poshmark")


# ---------------------------------------------------------------- the queue

def queue(s: Settings, db: DB, iid: str, mps: list[str] | None = None, why: str = "poshmark posted") -> list[str]:
    """One 'queued' row per cross-list marketplace the item has none on, at the owner's approved price. Returns them."""
    it = db.item(iid)
    if it is None:
        raise ValueError(f"unknown item {iid}")
    out = []
    for mp in mps if mps is not None else enabled(s):
        if db.listing(iid, mp) is None:
            db.upsert_listing(iid, mp, status="queued", price=it["owner_price"])
            out.append(mp)
    if out:
        db.log(iid, "crosslist_queued", {"mps": out, "why": why})
    return out


def window(db: DB) -> str:
    """The current daily window (WO28: the worker begins one at a start or a wake with the lid open)."""
    return db.kv_get(daily.SESSION_KEY) or datetime.now().strftime("%Y-%m-%d")


RETRY_AFTER = 30 * 60          # WO51: an item failure is tried again this long after it (was: the next daily window —
#                                with the Mac always on, a window may never come)


def new_window(s: Settings, db: DB) -> bool:
    """Between listings: a window the poster hasn't seen (the owner at the Mac) tests every paused site at once (WO51:
    a pause ends only by a passing test — it no longer waits for a window) and puts the failures that never reached the
    publish button back in line (`retry_failed`)."""
    w = window(db)
    if db.kv_get(WINDOW_SEEN) == w:
        return False
    db.kv_set(WINDOW_SEEN, w)
    db.kv_set(STREAK, "{}")
    from thrift_agent import health
    health.all_due_now(db)
    retry_failed(s, db, at=None)
    return True


def retry_failed(s: Settings, db: DB, at: datetime | None = None) -> list[tuple[str, str]]:
    """WO51: an item failure (nothing published) is tried again RETRY_AFTER after it; WO54 rule 4: after
    health.ASIDE_AFTER tries it is SET ASIDE on that site (never skipped for good) and tried again after its hold.
    `at` None: every such row now (a new window). Never an unconfirmed publish (the site's data settles it, WO35
    §4). WO54 §2: a refused form comes back on its own schedule (refusal_due: once another way, then once per
    code update) — never the same form at once."""
    from thrift_agent import health
    out = []
    t = at or datetime.now(timezone.utc)
    cutoff = None if at is None else (at - timedelta(seconds=RETRY_AFTER)).isoformat(timespec="seconds")
    sites = ("poshmark", *CROSS)                        # WO54 rule 4: Poshmark's item failures come back too
    for row in db.conn.execute(f"SELECT * FROM listings WHERE status='failed' AND url IS NULL AND marketplace IN "
                               f"({','.join('?' * len(sites))})", sites).fetchall():
        if (row["error"] or "").startswith(UNCONFIRMED):
            continue                                    # it may be live: settled by the site's data (WO35 §4)
        if refused(row["error"]):
            if (why := refusal_due(db, row["item_id"], row["marketplace"], at)) is not None:
                take_refusal_retry(db, row["item_id"], row["marketplace"], why)
                out.append((row["item_id"], row["marketplace"]))
            continue                                    # WO54 §2: by itself, on its own schedule
        iid, mp = row["item_id"], row["marketplace"]
        if (row["error"] or "").startswith(health.SET_ASIDE):
            # WO54 rule 4: set aside after two tries on this site — back in line once its hold is over (or at once
            # after a fix: the poster's start lets every set-aside go), with fresh attempts
            if health.aside_due(db, iid, mp, t):
                db.upsert_listing(iid, mp, status="queued", attempts=0,
                                  error="tried again after being set aside: " + row["error"][len(health.SET_ASIDE):])
                db.log(iid, "set_aside_retry", {"mp": mp})
                out.append((iid, mp))
            continue
        if cutoff is not None and (row["updated_at"] or "") > cutoff:
            continue
        # A form that read back other than planned (a "Mismatch": nothing was published) had waited for ever, one try
        # a window (WO36 §4). WO54 rule 4 (live 2026-11-09): an item that failed twice on a site is SET ASIDE there —
        # the site goes on with the next item and this one is tried again by itself later, never skipped for good
        if row["attempts"] < health.ASIDE_AFTER:
            db.upsert_listing(iid, mp, status="queued")
            db.log(iid, "crosslist_retry", {"mp": mp, "attempts": row["attempts"]})
        else:
            health.aside(db, mp, iid, row["error"], health.ITEM, at=t)
        out.append((iid, mp))
    return out


def next_try(row) -> str:
    """WO52 §6: when a failed row is tried again — RETRY_AFTER after its failure, as a local time ("4:45 PM")."""
    try:
        t = datetime.fromisoformat(str(row["updated_at"]))
    except (TypeError, ValueError, KeyError, IndexError):
        return "in about 30 min"
    t = (t if t.tzinfo else t.replace(tzinfo=timezone.utc)) + timedelta(seconds=RETRY_AFTER)
    if t <= datetime.now(timezone.utc):
        return "now"                    # WO54: never a time already gone (a card drawn after the quiet hours)
    return t.astimezone().strftime("%I:%M %p").lstrip("0")


def step_text(error: str | None) -> str:
    """The failing step in a person's words, from a listing row's error (the ❌ line, the ⏭ line): "photos 0 of 8 on
    the form; package LARGE (plan MEDIUM)" (a Mismatch), "photos: Vinted's system error during the photo upload: …"
    (a step that failed) — the prefixes ("Mismatch: ", "PosterError: ", the site's name) off, 140 characters at most."""
    e = (error or "").strip()
    for prefix in (MISMATCH, "PosterError: "):
        if e.startswith(prefix):
            e = e[len(prefix):].strip()
    for mp in ORDER:
        if e.lower().startswith(f"{mp} "):
            e = e[len(mp) + 1:]
    e = e.replace("form doesn't match plan: ", "")
    return (e[:139] + "…") if len(e) > 140 else (e or "no reason recorded")


def step_name(error: str | None) -> str:
    """A few words for the group's card: "photos", "the package size", "the brand", "the upload" (a refused form),
    else "the listing"."""
    first = step_text(error).lower().split(":")[0].split(" (")[0]
    for key, name in (("photos", "photos"), ("package", "the package size"), ("brand", "the brand"),
                      ("condition", "the condition"), ("category", "the category"), ("price", "the price"),
                      ("size", "the size"), ("refused", "the upload")):
        if key in first:
            return name
    return "the listing"


def blocked(db: DB, mp: str) -> str | None:
    """Why `mp` starts no new listing now — its pause (WO51, thrift_agent.health: paused until a test run passes or the
    owner resumes it; before, "stopped for this window", which an always-on Mac never ended) — or None."""
    from thrift_agent import health
    return health.paused(db, mp)


def account_blocked(db: DB, mp: str) -> str | None:
    """WO40: the site paused by what it SHOWED — a login, CAPTCHA, block or check page — not by failures (live
    2026-10-16: Vinted refused a form twice; a take-down must still run there)."""
    from thrift_agent import health
    return health.account_paused(db, mp)


def block(db: DB, mp: str, reason: str, page: str | None = None, shot=None) -> None:
    """Pause `mp` (logged out, CAPTCHA, a verification wall, a block page, item failures in a row): ONE plain line in
    the group for an account problem — what to do, by what the site showed (`page`) — and one private line; Poshmark
    and the other marketplace go on. WO51: it comes back by itself — a test run after the cool-down (health)."""
    from thrift_agent import health
    account = not reason.startswith("failures:")
    new = health.pause(db, mp, reason, "account" if account else "items", error=reason, page=page, shot=shot)
    if new:
        db.log(None, "crosslist_blocked", {"mp": mp, "reason": reason, "page": page})
        if account:                                  # WO53 §3: said again (at most every 12 h) while items wait
            from thrift_agent import alarm
            alarm.account_line(db, mp, page, force=True, reason=reason)
            # WO54 §2: a sign-out / CAPTCHA / check page — the 🚨 at once, whatever the hour (only her hands settle it);
            # the other sites go on posting
            alarm.account_alarm(db, mp, health.state(db, mp) or {})


def failure(db: DB, mp: str, failed: bool, limit: int = 3) -> None:
    """The per-marketplace circuit breaker: `limit` ITEM failures in a row pause that marketplace (WO51: a failure that
    isn't about the item never counts — thrift_agent.health). WO35: an unconfirmed publish is no failure until the
    site's own data shows it absent (live 2026-10-09: two of Vinted's three "failures in a row" at 08:30 were listings
    that had gone up)."""
    streak = loads(db.kv_get(STREAK)) or {}
    streak[mp] = streak.get(mp, 0) + 1 if failed else 0
    db.kv_set(STREAK, json.dumps(streak))
    if streak[mp] >= limit:
        block(db, mp, f"failures: {streak[mp]} listings in a row failed")


def capped(s: Settings, db: DB, mp: str) -> bool:
    from thrift_agent.scheduler import windows
    _, midnight = windows(tz=s["schedule"]["timezone"])
    return db.listed_since(midnight, mp) >= daily_cap(s, mp)


def live_elsewhere(db: DB, iid: str, mp: str) -> bool:
    """WO53 rule 6: the item is live on another site (a posted row with its address) — finished on the others the same
    night, whatever the hour."""
    return db.conn.execute("SELECT 1 FROM listings WHERE item_id=? AND marketplace<>? AND status='posted' AND url IS "
                           "NOT NULL LIMIT 1", (iid, mp)).fetchone() is not None


def pending(s: Settings, db: DB, mps: list[str] | None = None, paused_too: bool = False,
            live_elsewhere_only: bool = False) -> list[tuple[str, str]]:
    """(item, marketplace) the poster takes now, oldest Poshmark listing first: a 'queued' row — and a 'dryrun' one
    once the marketplace publishes for real — on an enabled marketplace that isn't paused or at its daily cap
    (`paused_too`: a paused one too — its test run's item, WO51). An item put behind the others after a failure that
    wasn't its own goes last (health.behind)."""
    from thrift_agent import health
    out = []
    for mp in mps if mps is not None else enabled(s):
        if (blocked(db, mp) and not paused_too) or capped(s, db, mp):
            continue
        states = ("queued", "dryrun") if live(s, mp) else ("queued",)
        rows = db.conn.execute(
            f"SELECT l.item_id FROM listings l LEFT JOIN listings p ON p.item_id=l.item_id AND p.marketplace='poshmark' "
            f"JOIN items i ON i.id=l.item_id "
            f"WHERE l.marketplace=? AND l.status IN ({','.join('?' * len(states))}) AND COALESCE(i.test, 0)=0 "
            f"ORDER BY COALESCE(p.posted_at, l.updated_at), l.rowid", (mp, *states)).fetchall()   # ties: queue order
        #   (WO45 §2: a test share's item is never listed; WO54 §2: never held for a brand — settle_brand decided it)
        mine = [r["item_id"] for r in rows
                if not live_elsewhere_only or live_elsewhere(db, r["item_id"], mp)]   # WO53 rule 6
        out += [(iid, mp) for iid in health.behind(db, mp, mine)]
    return out


def next_job(s: Settings, db: DB, prefer: str | None = None, mps: list[str] | None = None,
             paused_too: bool = False, live_elsewhere: bool = False) -> tuple[str, str] | None:
    """The next cross-list job: the item just listed first (Poshmark → Depop → Vinted, then the next item), else the
    oldest waiting one. `mps`: only these marketplaces (WO32: the ones whose poster can take a job now). WO53 rule 6
    (`live_elsewhere`): only items already live on another site (moot since WO54 §3: no listing hours any more)."""
    jobs = pending(s, db, mps, paused_too=paused_too, live_elsewhere_only=live_elsewhere)
    if not jobs:
        return None
    if prefer is not None:
        mine = [j for j in jobs if j[0] == prefer]
        if mine:
            return min(mine, key=lambda j: ORDER.index(j[1]))
    first = jobs[0][0]
    return min((j for j in jobs if j[0] == first), key=lambda j: ORDER.index(j[1]))


# ---------------------------------------------------------------- recording an outcome

def fields_summary(fields: dict) -> str:
    keys = ("category", "category_path", "size", "condition", "colors", "materials", "skirt_length", "attributes",
            "source", "age", "package_size", "package_sizes", "brand", "price")
    lines = [f"{k}: {fields[k]}" for k in keys if fields.get(k) not in (None, [], {}, "")]
    lines.append(f"photos: {len(fields.get('photos') or [])}")
    return "\n".join(lines)


def record(s: Settings, db: DB, iid: str, mp: str, title: str, out, fields: dict, seconds: float | None = None,
           slept: bool = False, lines: list[str] | None = None) -> None:
    """The listing row, the event, and the messages for one Depop/Vinted Outcome. Nothing here goes to the group but an
    unconfirmed publish's question (the "Posted ✓" line comes from announce()). `lines`: the extension job's progress
    ("photos 6/6 · category ✓ · … · filled in 21.3 s", WO32b), in a dry run's ops message."""
    from thrift_agent.post import runner           # the poster's own wording for an unconfirmed publish
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    error = out.error
    if out.status == "failed" and out.clicked and not out.url:
        error = UNCONFIRMED + (error or "")
    if out.status == "skipped":
        error = SKIPPED + (error or "")
    status = {"cancelled": "queued"}.get(out.status, out.status)
    guesses = [*fields.get("guesses", []), *out.guesses]
    db.upsert_listing(iid, mp, status=status, url=out.url, listing_id=listing_id_from(mp, out.url),
                      error=error if out.status != "cancelled" else out.note,
                      posted_at=stamp if out.status in ("posted", "dryrun") else None,
                      fields_json={**fields, "guesses": guesses, "note": out.note})
    if out.status == "posted" and mp == "vinted":       # WO52 follow-up: a repost keeps its price-drop clock
        from thrift_agent import vinted_repost
        vinted_repost.after_posted(s, db, iid, mp)
    db.log(iid, f"post_{out.status}", {"mp": mp, "url": out.url, "error": error, "shot": out.screenshot,
                                       "note": out.note, "clicked": out.clicked, "guesses": guesses,
                                       "seconds": round(seconds, 1) if seconds else None, "slept": slept or None,
                                       "progress": list(lines) if lines else None})
    shot = Path(out.screenshot or "")
    note = f"\n{out.note}" if out.note else ""
    progress = f"\n{' · '.join(lines)}" if lines else ""
    if out.status == "failed" and out.clicked and not out.url:
        clicked = (datetime.fromtimestamp(out.clicked_at, timezone.utc) if getattr(out, "clicked_at", None) else None)
        unconfirmed_wait(db, iid, mp, out.screenshot, clicked)   # WO35 §4 / WO53: verifying — never a question
    elif out.status == "failed" and refused(error) and autofix_refusal(db, iid, mp, title, error):
        pass                                             # WO50: fixed by the code and retried once — no 'retry' asked
    elif out.status == "failed" and refused(error):
        was = unconf_state(db, iid, mp) or {}
        if silent_refusal(error) and was.get("stage") != "reposted":
            # WO39 follow-up (the owner): no field error — Vinted's own data looked at in 10 minutes; still absent →
            # posted once more by itself (one last look right before); only a second refusal asks for 'retry'
            due = (datetime.now(timezone.utc) + timedelta(seconds=RECHECK_AFTER)).isoformat(timespec="seconds")
            set_unconf(db, iid, mp, stage="wait", due=due, round=1, tries=0, shot=out.screenshot, refused=True)
            db.log(iid, "refused_wait", {"mp": mp, "due": due, "error": error})
        elif was.get("stage") != "reposted" and retry_refusal_later(db, iid, mp, title, error):
            set_unconf(db, iid, mp, stage=None)          # WO54 §2: tried again by itself, another way — nothing said
        else:
            set_unconf(db, iid, mp, stage=None)
            say_refused(db, iid, mp, title, error, shot)
    elif out.status == "failed":
        # the step and the next try, in words (the Vinted incident of 2026-10-20: "it waits, tried again next window"
        # named neither); a Mismatch published nothing (WO36 §4) and the group hears nothing until the ⏭ line
        row = db.listing(iid, mp)
        attempts = (row["attempts"] if row else 0) or 0
        what = "nothing was published; " if (error or "").startswith(MISMATCH) else ""
        from thrift_agent import health
        if attempts < health.ASIDE_AFTER:
            again = f"{what}attempt {attempts} of {health.ASIDE_AFTER} — tried again in {RETRY_AFTER // 60} min"
        else:
            again = (f"{what}attempt {attempts} of {health.ASIDE_AFTER} — set aside on {LABEL[mp]} (the other items go "
                     f"on); tried again by itself later")
        notify.ops_photo(shot, f"❌ {LABEL[mp]} failed ({iid}): {title}\n{step_text(error)}{note}\n({again})")
    elif out.status == "skipped" and NO_SIZE in (out.error or ""):
        say_no_size(title)
    elif out.status == "skipped":
        notify.ops_photo(shot, f"⏭ {LABEL[mp]} skipped ({iid}): {title}\n{out.error}{note}")
        from thrift_agent.post import runner
        runner.ask_skipped(db, iid, mp, title, f"{LABEL[mp]}'s form doesn't take one of its details")
    elif out.status == "dryrun":
        notify.ops_photo(shot, f"🧪 dry-run {LABEL[mp]} ({iid}): {title} — ${fields.get('price')}\n"
                               f"{fields_summary(fields)}{progress}{note}")
    elif out.status == "posted" and (worth := noteworthy(out.note)):
        notify.ops_photo(shot, f"{LABEL[mp]} posted ({iid}) {out.url}: {worth}")
    if out.status == "posted":
        autofix_posted(db, iid, mp, fields)


AUTOFIX = "title_autofix"          # kv: {item|site: {"fix", "error", "stage": "retry"|"done"}} (WO50)


def autofix_refusal(db: DB, iid: str, mp: str, title: str, error: str | None) -> bool:
    """WO50: a refusal the code can fix itself (data/site_rules.json "refusals": Vinted's "too many capital letters", a
    title too long, characters a site rejects) — the row back in line ONCE, its title made stricter at the next
    mapping (`strict_title`); the owner is never asked to reply 'retry' for it. A second refusal after that retry is
    the usual ⛔ line (no loop). True when it was taken here."""
    from thrift_agent import titles
    said = (error or "").split(REFUSED, 1)[-1]
    fix = titles.fixable(mp, said)
    state = loads(db.kv_get(AUTOFIX)) or {}
    key = f"{iid}|{mp}"
    if not fix or key in state:
        if key in state:
            state[key]["stage"] = "done"
            db.kv_set(AUTOFIX, json.dumps(state))
        return False
    state[key] = {"fix": fix, "error": said[:300], "stage": "retry", "title": title}
    db.kv_set(AUTOFIX, json.dumps(state))
    db.upsert_listing(iid, mp, status="queued", error=None)
    db.log(iid, "refused_autofix", {"mp": mp, "fix": fix, "error": said[:300]})
    return True


def strict_level(db: DB, iid: str, mp: str) -> int:
    return 1 if ((loads(db.kv_get(AUTOFIX)) or {}).get(f"{iid}|{mp}") or {}).get("stage") == "retry" else 0


def strict_title(db: DB, iid: str, mp: str, fields, view):
    """WO50: the mapped title (Vinted's title, Depop's description's first line) passes the site's rules before the
    form opens — stricter after the site refused it (`strict_level`). WO54 §2 (never hold): a title that still breaks
    them is replaced by one built from the facts (titles.fallback), said in the item's "Check —" line; MappingError
    only when there are no facts to build one from."""
    from thrift_agent import titles
    from thrift_agent.catalogs.common import MappingError
    level = strict_level(db, iid, mp)
    facts = getattr(view, "facts", None)
    brand = ((facts.brand.value or "").strip() or None) if facts is not None else None
    condition = facts.condition if facts is not None else None
    if facts is not None:                    # WO51 §3: no brand or character mark that isn't the item's own
        from thrift_agent import brands, pipeline
        it = db.item(iid)
        owner = it["owner_brand"] if it is not None and "owner_brand" in it.keys() else None
        own = brands.own_names(getattr(fields, "brand", None) or brand, brand, owner)
        ok = pipeline.mark_ok_text(facts)
        gone: list[str] = []
        for name in ("title", "description"):
            if getattr(fields, name, None):
                text, found = brands.guard(getattr(fields, name), own, ok)
                if found:
                    setattr(fields, name, text)
                    gone += found
        if gone:
            db.log(iid, "marks_removed", {"mp": mp, "marks": sorted(set(gone))})
    if mp == "vinted":
        if not getattr(fields, "title", None):
            return fields
        fields.title = titles.for_site(fields.title, "vinted", level, brand, condition)
        current = fields.title
    elif not getattr(fields, "description", None):
        return fields
    else:
        first, _, rest = (fields.description or "").partition("\n")
        first = titles.for_site(first, "depop", level, brand, condition)
        fields.description = f"{first}\n{rest}" if rest else first
        current = first
    if problems := titles.check(current, mp):
        if facts is None or not hasattr(facts, "brand"):
            raise MappingError(f"the title breaks {LABEL.get(mp, mp)}'s rules ({'; '.join(problems)}): {current}")
        built = titles.fallback(facts, mp, level)        # WO54 §2: built from the facts, never a skipped site
        if mp == "vinted":
            fields.title = built
        else:
            _, _, rest = (fields.description or "").partition("\n")
            fields.description = f"{built}\n{rest}" if rest else built
        db.log(iid, "title_fallback", {"mp": mp, "from": current, "to": built, "problems": problems})
        from thrift_agent import pipeline
        pipeline.add_check(db, iid, f"title on {LABEL.get(mp, mp)}: “{current}” broke its rules "
                                    f"({'; '.join(problems)}) — built from the facts: {built}")
    return fields


def autofix_posted(db: DB, iid: str, mp: str, fields: dict) -> None:
    """WO50: a form the site had refused, fixed by the code and now live — ONE private line with what changed."""
    state = loads(db.kv_get(AUTOFIX)) or {}
    entry = state.pop(f"{iid}|{mp}", None)
    if entry is None:
        return
    db.kv_set(AUTOFIX, json.dumps(state))
    refusals = _refusals(db)
    if refusals.pop(f"{iid}|{mp}", None) is not None:   # WO54 §2: its own retry worked — nothing left to try
        db.kv_set(REFUSAL_RETRY, json.dumps(refusals))
    new = fields.get("title") or str(fields.get("description") or "").split("\n", 1)[0]
    how = ("tried again by itself another way (a fresh page, a stricter title) and it is live" if entry.get("fix") ==
           "another way" else "the title was fixed and it is live")
    notify.say(f"✓ {LABEL.get(mp, mp)} had refused {entry.get('title') or iid} ({entry.get('error')}); {how}: {new}")


def refused(error: str | None) -> bool:
    """WO39: the site kept its sell form after the one click (its own data agreeing nothing was published)."""
    return REFUSED in (error or "")


GENERIC_REFUSAL = re.compile(r"no error shown on the form|kept the sell form|went wrong|try again|an error occurred|"
                             r"unexpected error", re.I)


def silent_refusal(error: str | None) -> bool:
    """WO39 follow-up: a refusal that names no field — nothing on the form, or only a generic "Something went wrong"
    (live 2026-10-16: the Sam Edelman twice, the third identical form went live). Such a form is posted once more by
    itself after the site's own data shows it absent; a field error ("Brand: Fill in brand to continue", a field marked
    invalid) is the owner's to look at at once."""
    if not refused(error):
        return False
    said = (error or "").split(REFUSED, 1)[1].split(" (its requests after the click", 1)[0]
    if "marked invalid" in said:
        return False
    parts = [p.strip() for p in said.split(" | ") if p.strip()]
    return bool(parts) and all(GENERIC_REFUSAL.search(p) for p in parts)


REFUSAL_RETRY = "refusal_retry"   # kv (WO54 §2): {"<item>|<site>": {"due", "error", "title", "version", "after_code"}}
CODE_RETRIES = 3                  # a form refused for want of a code change: tried once per new version, this many times


def code_version() -> str:
    """WO54 §2: this checkout's code as a short hash — the Python package, the extension, the data tables — so a form
    refused for want of a fix is tried once more by itself after a deploy changed them. Read once per process."""
    global _VERSION
    if _VERSION is None:
        import hashlib

        from thrift_agent.config import ROOT
        h = hashlib.sha256()
        for pattern in ("thrift_agent/**/*.py", "ext/**/*.js", "ext/**/*.json", "data/*.json"):
            for p in sorted(ROOT.glob(pattern)):
                try:
                    h.update(p.relative_to(ROOT).as_posix().encode())
                    h.update(p.read_bytes())
                except OSError:
                    continue
        _VERSION = h.hexdigest()[:12]
    return _VERSION


_VERSION: str | None = None


def _refusals(db: DB) -> dict:
    return loads(db.kv_get(REFUSAL_RETRY)) or {}


def retry_refusal_later(db: DB, iid: str, mp: str, title: str, error: str | None, at: datetime | None = None) -> bool:
    """WO54 §2 (the owner: every failure is debugged, fallen back on and tried again by itself): a form the site
    refused with a field error — and no other way tried for it yet (the fixable refusals' stricter title, WO50, is one)
    — goes back in line by itself RETRY_AFTER later, another way: a fresh page, the title made stricter (strict_level),
    and a look at the site's own data first (stage "retry", runner.run_cross) so a listing that went up after all is
    recorded, never posted twice. Quiet: the ⛔ and the 🚨 are for a second refusal (say_refused). True when taken."""
    key = f"{iid}|{mp}"
    auto = loads(db.kv_get(AUTOFIX)) or {}
    state = _refusals(db)
    if key in auto or key in state:
        return False                                    # a way was tried already: the code must change (the 🚨)
    t = at or datetime.now(timezone.utc)
    said = (error or "").split(REFUSED, 1)[-1][:300]
    state[key] = {"due": (t + timedelta(seconds=RETRY_AFTER)).isoformat(timespec="seconds"), "error": said,
                  "title": title}
    db.kv_set(REFUSAL_RETRY, json.dumps(state))
    auto[key] = {"fix": "another way", "error": said, "stage": "retry", "title": title}
    db.kv_set(AUTOFIX, json.dumps(auto))
    db.log(iid, "refused_retry", {"mp": mp, "due": state[key]["due"], "error": said})
    return True


def refusal_due(db: DB, iid: str, mp: str, at: datetime | None = None) -> str | None:
    """WO54 §2: is this refused row's own retry due now — "another way" (its RETRY_AFTER passed) or "after the code"
    (refused twice, a deploy changed the code since: once per version, CODE_RETRIES at most)? None while it waits."""
    entry = _refusals(db).get(f"{iid}|{mp}")
    if not entry:
        return None
    t = at or datetime.now(timezone.utc)
    if entry.get("version") is None:
        if not entry.get("due"):
            return None                                  # taken already: its outcome decides what comes next
        try:
            due = datetime.fromisoformat(str(entry["due"]))
        except ValueError:
            return "another way"
        return "another way" if (due if due.tzinfo else due.replace(tzinfo=timezone.utc)) <= t else None
    tried = list(entry.get("after_code") or [])
    if entry["version"] != code_version() and code_version() not in tried and len(tried) < CODE_RETRIES:
        return "after the code"
    return None


def take_refusal_retry(db: DB, iid: str, mp: str, why: str) -> None:
    """The refused row back in line for its own retry: the site's own data looked at first (stage "retry"); a retry
    after a code change is noted so the same version never tries twice."""
    key = f"{iid}|{mp}"
    state = _refusals(db)
    entry = state.get(key) or {}
    if why == "after the code":
        entry["after_code"] = [*(entry.get("after_code") or []), code_version()]
    else:
        entry["due"] = None
    state[key] = entry
    db.kv_set(REFUSAL_RETRY, json.dumps(state))
    set_unconf(db, iid, mp, stage="retry")
    db.upsert_listing(iid, mp, status="queued", error=None)
    db.log(iid, "refused_retried", {"mp": mp, "why": why})


def refused_text(title: str, mp: str, error: str) -> str:
    """WO53 §2 / WO54 §2: what happened, never a question — refused again after the other way was tried, so the code
    must change for this form (the 🚨 carries the request); it is tried once more by itself after the next code
    update."""
    said = error.split(REFUSED, 1)[1] if REFUSED in error else error
    site = LABEL.get(mp, mp)
    return (f"⛔ {title}: {site} refused the form — {said}. It isn't on {site} (its own data agrees). Tried again by "
            f"itself another way and refused again: the code must change for it (the 🚨 has the request); it is tried "
            f"once more by itself after the next code update.")


def say_refused(db: DB, iid: str, mp: str, title: str, error: str, shot: Path | None = None) -> None:
    """WO39: a refused form, ONE private line with the site's exact error (and the picture of the form). WO54 §2: said
    only once the other way was tried too (retry_refusal_later) — the code must change for it: the 🚨 (once) carries
    the fix request, and the row is tried once more by itself after a deploy changed the code (refusal_due). The same
    refusal again is never said twice. Nothing is asked; her reply 'retry' to an older line and `thrift retry` work."""
    from thrift_agent import alarm, approve, health
    from thrift_agent.config import settings
    key = f"{iid}|{mp}"
    state = _refusals(db)
    entry = state.get(key) or {}
    sig = health.signature(error)
    first = entry.get("said") != sig
    state[key] = {**entry, "version": code_version(), "said": sig, "title": title,
                  "error": (error or "").split(REFUSED, 1)[-1][:300]}
    db.kv_set(REFUSAL_RETRY, json.dumps(state))
    db.log(iid, "refused_said", {"mp": mp, "error": error[:300], "again": not first})
    if not first:
        return
    text = refused_text(title, mp, error)
    if shot is not None and Path(shot).is_file():
        notify.ops_photo(Path(shot), f"⛔ {LABEL.get(mp, mp)} — the form after the click: {title}")
    bot = approve.ops_bot_for(settings())
    if bot is None:
        notify.say(text)
    else:
        try:            # kept reply-able (outbox "refused"): her own 'retry' still works — nothing asks for it
            db.add_outbox(bot.chat_id, bot.send_message(text), "refused", unconfirmed_ref(iid, mp), text=text)
        except Exception as e:  # noqa: BLE001 — the ops chat away: logged; `thrift retry` still works
            db.log(iid, "error", f"refused message: {type(e).__name__}: {e}")
    alarm.raise_for(db, f"{mp}:refused:{iid}", f"{title}: {LABEL.get(mp, mp)} refused the form — "
                    f"{error.split(REFUSED, 1)[-1][:200]}", step="the upload", why="blocked", label=LABEL.get(mp, mp),
                    evidence=f"var/failed/shots/{Path(shot).name}" if shot else None)


# ---------------------------------------------------------------- an unconfirmed publish (WO35 §4)

def _unconf(db: DB) -> dict:
    return loads(db.kv_get(UNCONF)) or {}


def unconf_state(db: DB, iid: str, mp: str) -> dict | None:
    return _unconf(db).get(f"{iid}:{mp}")


def set_unconf(db: DB, iid: str, mp: str, **state) -> None:
    """Change an unconfirmed publish's state (stage=None: forget it — it is settled)."""
    states = _unconf(db)
    key = f"{iid}:{mp}"
    if "stage" in state and state["stage"] is None:
        states.pop(key, None)
    else:
        states[key] = {**states.get(key, {}), **state}
    db.kv_set(UNCONF, json.dumps(states))


def unconfirmed_wait(db: DB, iid: str, mp: str, shot: str | None = None, clicked: datetime | None = None) -> int:
    """A publish on Depop / Vinted whose listing wasn't found by its address in 5 minutes (WO35 §4): nothing is said;
    WO53: "verifying" — looked for again by the site's own data on postcheck's schedule (runner.recheck_unconfirmed).
    Returns the round: 2 when it was the automatic second post."""
    from thrift_agent import postcheck
    was = unconf_state(db, iid, mp) or {}
    rnd = 2 if was.get("stage") in ("reposted", "retry") else 1
    st = postcheck.begin(db, iid, mp, clicked=clicked, shot=shot, round_=rnd)
    db.log(iid, "unconfirmed_wait", {"mp": mp, "round": rnd, "due": st["due"]})
    return rnd


def rechecks_due(db: DB, mp: str, now: datetime | None = None) -> list[str]:
    """The items whose unconfirmed publish on `mp` is due for its quiet re-check (WO35 §4), oldest first; one from
    before WO35 (no state yet) is due at once."""
    now = now or datetime.now(timezone.utc)
    states = _unconf(db)
    out = []
    for row in db.conn.execute("SELECT item_id, error FROM listings WHERE marketplace=? AND status='failed' AND "
                               "url IS NULL ORDER BY updated_at, rowid", (mp,)).fetchall():
        st = states.get(f"{row['item_id']}:{mp}")
        if refused(row["error"]) and st is not None and st.get("refused"):
            pass                                        # WO39 follow-up: a silent refusal, looked at the same way
        elif not (row["error"] or "").startswith(UNCONFIRMED):
            continue
        if st is None:
            out.append(row["item_id"])
        elif st.get("stage") == "wait":
            try:
                if datetime.fromisoformat(st["due"]) <= now:
                    out.append(row["item_id"])
            except (KeyError, TypeError, ValueError):
                out.append(row["item_id"])
    return out


NO_SIZE = "no size on the item"


def say_no_size(title: str) -> None:
    """WO37: an item with no size, ONE private line whichever site found it (the same text: said once) — her reply
    'size …' to its card puts every site back in line; never the group's 'retry' line, which would only skip again."""
    notify.say(f"⏭ {title}: {NO_SIZE} — it goes up once you reply 'size …' to its card")


WRONG_FAMILY = "isn't the item's family"        # WO51 §5: a reason both safety nets keep (the category is fixed first)
FAMILY_SAID = "family_said"                     # kv: [item|site] — the line said once


def family_problem(item_type: str | None, site: str, category) -> str | None:
    """WO51 §5 (live 2026-11-01: a crochet mini dress under Poshmark's Sweaters › V-Necks and Vinted's "V-neck
    sweaters"): the site's category in another family than the item's own type words — the words, else None (unknown
    agrees with everything)."""
    from thrift_agent import families
    mine = families.family_of_type(item_type)
    theirs = families.family_of_category(site, category)
    if families.same_family(mine, theirs):
        return None
    shown = category if isinstance(category, str) else " › ".join(str(c) for c in category if c)
    return f"category '{shown}' {WRONG_FAMILY} (a {mine}, not a {theirs})"


def hold_family(db: DB, iid: str, mp: str, title: str, why: str) -> None:
    """The item waits on `mp` (skipped, a kept reason) until its category is fixed — ONE private line per item and
    site, never posted in the wrong category."""
    db.upsert_listing(iid, mp, status="skipped", error=SKIPPED + why)
    db.log(iid, "post_wrong_family", {"mp": mp, "why": why})
    said = set(loads(db.kv_get(FAMILY_SAID)) or [])
    if f"{iid}|{mp}" in said:
        return
    said.add(f"{iid}|{mp}")
    db.kv_set(FAMILY_SAID, json.dumps(sorted(said)))
    notify.say(f"⏸ {LABEL[mp]} holds {title}: its {why} — it waits until the category is fixed "
               f"(reply 'category …' to its card, or `thrift recategorize`).")


def family_check(db: DB, iid: str, mp: str, title: str, why: str) -> None:
    """WO54 §1.4 (the owner: never leave an item unposted): a category still of another family than the item's, with
    no closer one on the site — posted there anyway; ONE private check line per item and site."""
    db.log(iid, "post_family_kept", {"mp": mp, "why": why})
    said = set(loads(db.kv_get(FAMILY_SAID)) or [])
    if f"{iid}|{mp}" in said:
        return
    said.add(f"{iid}|{mp}")
    db.kv_set(FAMILY_SAID, json.dumps(sorted(said)))
    notify.say(f"Check — {title}: {LABEL[mp]}'s {why} and {LABEL[mp]} has no closer one — listed there anyway.")


def skip_unmappable(db: DB, iid: str, mp: str, title: str, why: str) -> None:
    """A required field the catalog can't take from the facts: this marketplace is skipped for this item (the others
    go on), with the reason in the ops chat."""
    db.upsert_listing(iid, mp, status="skipped", error=SKIPPED + why)
    db.log(iid, "post_skipped", {"mp": mp, "error": why, "mapping": True})
    if why == NO_SIZE:                                   # WO37: one line for the item, not one per site
        say_no_size(title)
        return
    if size_skip(why):                                   # WO50: the size and the menu named, once
        say_size_skip(db, iid, mp, title, why)
        return
    notify.say(f"⏭ {LABEL[mp]} skipped ({iid}): {title}\ncan't map: {why}")


SIZE_SKIP = re.compile(r"size '?[^']*'? (?:isn't on|has no place on)|waist \S+ isn't on", re.I)
SIZE_SKIP_SAID = "size_skip_said"         # kv: [item|site] (WO50)


def size_skip(error: str | None) -> bool:
    """WO50: a site skipped the item because its size fits none of the menu's values."""
    return bool(SIZE_SKIP.search(error or ""))


def say_size_skip(db: DB, iid: str, mp: str, title: str, error: str | None) -> None:
    """WO50: ONE private line per item and site — the size and the menu named; the item posts on the other sites, and
    a reply 'size …' to its card lists it here too (it puts every skipped site back in line)."""
    said = loads(db.kv_get(SIZE_SKIP_SAID)) or []
    key = f"{iid}|{mp}"
    if key in said:
        return
    db.kv_set(SIZE_SKIP_SAID, json.dumps([*said, key][-300:]))
    why = str(error or "").replace(SKIPPED, "").strip()
    # WO54 §2: what was decided, nothing asked — a size the photos show that no value of this site's menu comes near
    # (one step, a neutral choice) is never made up; her own 'size …' on the card still puts the site back in line
    notify.say(f"📏 {title}: {LABEL.get(mp, mp)} has no reasonable value for its size ({why}) — it goes up on the "
               "other sites, not on this one.")


# ---------------------------------------------------------------- the group's one line per item

def announced(db: DB, iid: str) -> set[str]:
    done: set[str] = set()
    for (detail,) in db.conn.execute("SELECT detail FROM events WHERE ref=? AND kind='posted_announced'", (iid,)):
        done |= set((loads(detail) or {}).get("mps") or [])
    return done


def _joined(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def done_line(state: list[str] | None, waits: dict[str, list[str]] | None = None) -> str | None:
    """The card's last line (WO34): "💤 All done — nothing is waiting." when nothing is left on any site and nothing
    waits for the owner; "💤 Done for now. Vinted catches up by itself." (WO52 §6) when a stopped site
    still has listings; WO39: "💤 Done for now — waiting for a size: Alo Yoga Sports Bralette Mauve Taupe." when an
    item waits for her reply (a size, a 'retry'); None while there is work."""
    if state is None:
        return None
    said = [f"waiting for {what}: {_joined(names[:3])}{f' (+{len(names) - 3} more)' if len(names) > 3 else ''}"
            for what, names in (waits or {}).items() if names]
    if not state and not said:
        return ALL_DONE_LINE
    head = "💤 Done for now" + (" — " + "; ".join(said) + "." if said else ".")
    if not state:
        return head
    names = [LABEL.get(mp, mp) for mp in state]
    by = "itself" if len(names) == 1 else "themselves"
    return f"{head} {_joined(names)} catch{'es' if len(names) == 1 else ''} up by {by}."


def item_name(db: DB, iid: str, limit: int = 40) -> str:
    """The item's title cut at a word near `limit` characters (WO39: "Alo Yoga Sports Bralette Mauve Taupe") — long
    enough to tell two look-alikes apart, as on their cards."""
    it = db.item(iid)
    title = (((loads(it["renders"]) or {}).get("poshmark") or {}).get("title") if it else None) or iid
    words, out = title.split(), ""
    for w in words:
        if out and len(out) + 1 + len(w) > limit:
            break
        out = f"{out} {w}".strip()
    return out or title[:limit]


def _to_come(s: Settings | None, it, mp: str) -> bool:
    """WO54: an enabled site with no row yet that will still take the item by itself — Poshmark's queue (a ready, priced
    item: Poshmark keeps no 'queued' row; live 2026-11-09 the 19 items live on Depop and Vinted had none) or a cross
    site's row about to be queued (feed) — on a site that publishes for real."""
    if s is None or it is None or it["status"] != "ready" or not it["owner_price"]:
        return False
    if "test" in it.keys() and it["test"]:
        return False
    return live(s, mp)


def card_text(s: Settings, db: DB, iid: str, last: str | None = None) -> str:
    """The group's card for an item (WO34, the owner: separate lines, little text, some emoji): "✅ <short name> ·
    $<price>", then each site on its own line — its name the link (HTML), "⏳ Vinted — later" (or what it waits for,
    `wait_line`) while it hasn't posted; WO54: also a site with no row yet that will take the item (Poshmark's queue) —
    with an empty line between, and the 💤 line when it is the newest card. A site that failed for good or was skipped
    is left out. Nothing technical."""
    import html

    from thrift_agent.brain import copy as copywriter
    from thrift_agent.schema import Facts
    it = db.item(iid)
    render = ((loads(it["renders"]) or {}).get("poshmark") or {}) if it else {}
    try:
        facts = Facts.model_validate(loads(it["facts"])) if it and it["facts"] else None
    except Exception:  # noqa: BLE001 — the name falls back to the title alone
        facts = None
    name = copywriter.short_name(render.get("short_name"), render.get("title") or iid, facts)
    rows = {r["marketplace"]: r for r in db.listings_for(iid)}
    price = (it["owner_price"] if it else None) or render.get("price") or \
        next((r["price"] for r in rows.values() if r["price"]), "")
    lines = [f"✅ {html.escape(name)} · ${price}"]
    sites = [*(["poshmark"] if s is None or s.get("marketplaces.poshmark.enabled", True) else []),
             *(enabled(s) if s is not None else [m for m in ORDER if m != "poshmark" and m in rows])]
    for mp in sites:
        row = rows.get(mp)
        if row is None:
            if _to_come(s, it, mp):                    # WO54: Poshmark's queue has no row — said all the same
                lines.append(html.escape(wait_line(db, iid, mp, None, s), quote=False))
            continue
        if row["status"] in WAS_LIVE and row["url"]:          # WO54: a card edited later keeps a sold site's link
            lines.append(f'{DOT.get(mp, "•")} <a href="{html.escape(row["url"], quote=True)}">{LABEL.get(mp, mp)}</a>')
        elif row["status"] in LATER and not (row["status"] == "dryrun" and not (s is not None and live(s, mp))):
            lines.append(html.escape(wait_line(db, iid, mp, row, s), quote=False))   # a dry run that won't publish: out
        elif row["status"] == "skipped" and NO_SIZE in (row["error"] or ""):
            lines.append(html.escape(wait_line(db, iid, mp, row, s), quote=False))   # WO54: waits for her size
    if last:
        lines.append(last)
    return "\n\n".join(lines)


ACCOUNT_WHY = {"login": "it needs a login on the Mac", "captcha": "it shows a CAPTCHA on the Mac",
               "verify": "it asks for a check on the Mac", "block": "it turned the Mac away for now"}


def _clock(t) -> str:
    return t.astimezone().strftime("%I:%M %p").lstrip("0")


def _capped(s: Settings, db: DB, mp: str) -> bool:
    """`capped`, never raising for settings without a schedule (a card drawn from the CLI, a test's)."""
    try:
        return capped(s, db, mp)
    except (KeyError, TypeError, ValueError):
        return False


def wait_line(db: DB, iid: str, mp: str, row, s: Settings | None = None) -> str:
    """WO53 §6 / WO54: a site not live yet on the item's card, in plain words — "⏳ Poshmark — submitted 10:50 PM,
    verifying", "⏳ Vinted — photos failed, next try 4:45 PM", "⏳ Depop — next try 11:28 PM, it needs a login on the
    Mac", "⏳ Poshmark — waiting for a size", "⏳ Poshmark — after the quiet hours, from 2:00 PM", "⏳ Vinted — today's
    25 are up; it goes up after midnight" (the daily cap), else "⏳ Depop — later". `row` None: the site keeps no row
    for it yet (Poshmark's queue)."""
    from thrift_agent import approve, health, postcheck, quiet
    site = LABEL.get(mp, mp)
    status, err = (row["status"], row["error"] or "") if row is not None else (None, "")
    if status == "failed" and err.startswith(UNCONFIRMED):
        st = unconf_state(db, iid, mp) or {}
        when = _clock(postcheck.clicked_of(st, row))
        return f"⏳ {site} — submitted {when}, " + ("still being checked" if st.get("stage") == "given_up"
                                                    else "verifying")
    if status == "failed" and err.startswith(getattr(health, "SET_ASIDE", "set aside: ")):
        # WO54 rule 4: set aside after two tries on this site — tried again by itself after its hold (hours, not the
        # 30 minutes of a plain failure), never failed for good
        rec = (getattr(health, "asides", lambda _db: {})(db) or {}).get(f"{iid}|{mp}") or {}
        until = postcheck.utc(rec.get("until"))
        when = ("now" if until <= datetime.now(timezone.utc) else f"at {_clock(until)}") if until else "later"
        return f"⏳ {site} — set aside for now; it tries again by itself {when}"
    if status == "failed":
        # the Vinted incident of 2026-10-20 (the owner): a failed site names its step and the next try
        return f"⏳ {site} — {step_name(err)} failed, next try {next_try(row)}"
    if status == "skipped" and NO_SIZE in err:
        return f"⏳ {site} — waiting for a size"
    if approve.needs_brand(db.item(iid)):
        return f"⏳ {site} — waiting for the brand"
    p = health.state(db, mp)
    if p:
        due = postcheck.utc(p.get("due"))
        why = (ACCOUNT_WHY.get(p.get("page") or alert_kind(p.get("reason") or "", p.get("page")), "it is paused")
               if p.get("kind") == "account" else "paused by you" if p.get("owner") else "paused after a problem")
        # WO54: a test time already gone (the poster stopped for the quiet hours) is "now" — never 11:31 AM at 2 PM
        when = ("now" if due <= datetime.now(timezone.utc) else _clock(due)) if due else "when it is back"
        return f"⏳ {site} — next try {when}, {why}"
    if quiet.closing(None, db) and (back := quiet.resume_at()) is not None:
        return f"⏳ {site} — after the quiet hours, from {quiet.clock(back)}"      # WO54 §3: nothing goes up till then
    if s is not None and _capped(s, db, mp):
        return f"⏳ {site} — today's {daily_cap(s, mp)} are up; it goes up after midnight"   # WO54 §3: the daily cap
    return f"⏳ {site} — later"


def card_row(db: DB, iid: str):
    """The item's ✅ card in the outbox (its newest 'posted' row), or None."""
    return db.conn.execute("SELECT * FROM outbox WHERE kind='posted' AND ref=? ORDER BY rowid DESC LIMIT 1",
                           (iid,)).fetchone()


def _body(text: str | None) -> str:
    """A card's text without its 💤 line."""
    return (text or "").split("\n\n💤", 1)[0]


def refresh_card(s: Settings, db: DB, iid: str) -> None:
    """The item's ✅ card re-rendered from its listings as they are now and edited in place (its 💤 line kept) — a
    site's address changed after the card went out (WO52: a Vinted listing posted again)."""
    row = card_row(db, iid)
    if row is None:
        return
    old = row["text"] or ""
    text = card_text(s, db, iid, None) + (old[old.index("\n\n💤"):] if "\n\n💤" in old else "")
    _card(s, db, iid, text, edit=True)


def _card(s: Settings | None, db: DB, iid: str, text: str, edit: bool) -> bool:
    """The card out, HTML, previews off — never twice (WO37, WO54). A new card is recorded in the outbox BEFORE it
    goes, under a placeholder id, in ONE statement that takes it only when the item has no card yet (the running
    poster's sweep and `thrift cards --backfill` may try at the same moment: one sends); a send that never left is
    taken back (the next sweep tries again), one that may have arrived counts as sent. `edit`: the same message
    changed, and only when its text differs — never a new message, also when the card's message id isn't known.
    On the dev machine (no bot) printed. True when a message was sent or edited."""
    from thrift_agent import approve
    bot = approve.bot_for(s) if s is not None else None
    row = card_row(db, iid)
    if edit:
        if row is None or (row["text"] or "") == text:
            return False
        if bot is not None:
            if str(row["chat_id"]) != str(bot.chat_id) or int(row["message_id"]) <= 0:
                db.log(iid, "card_edit", {"error": "its message id isn't known (sent without an answer): left as is"})
                return False
            try:
                bot.edit_message(int(row["message_id"]), text, parse_mode="HTML")
            except Exception as e:  # noqa: BLE001 — "message is not modified" and the like: the card stays as it is
                db.log(iid, "card_edit", {"error": str(e)[:200]})
                if "not modified" not in str(e):
                    return False
        else:
            notify.group(text)
        db.conn.execute("UPDATE outbox SET text=? WHERE chat_id=? AND message_id=?",
                        (text, row["chat_id"], row["message_id"]))
        return True
    chat = str(bot.chat_id) if bot is not None else "dev"
    placeholder = -int(time.time() * 1000) - random.randint(0, 999)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    taken = db.conn.execute(
        "INSERT INTO outbox (chat_id, message_id, kind, ref, text, sent_at, resolved_at) SELECT ?, ?, 'posted', ?, ?, ?, "
        "? WHERE NOT EXISTS (SELECT 1 FROM outbox WHERE kind='posted' AND ref=?)",
        (chat, placeholder, iid, text, stamp, stamp, iid)).rowcount
    if taken != 1:
        return False                                   # its card is out already (another process, a moment ago)
    if bot is None:
        notify.group(text)
        return True
    from thrift_agent import telegram
    try:
        mid = bot.send_message(text, parse_mode="HTML")
    except RuntimeError as e:
        if not telegram.maybe_delivered(e):
            db.conn.execute("DELETE FROM outbox WHERE chat_id=? AND message_id=?", (chat, placeholder))
            raise                                      # never left the Mac: the caller leaves it for the next sweep
        db.log(iid, "telegram_maybe_sent", {"kind": "posted", "error": str(e)[:200]})
        return True                                    # it went out, no answer: counted as sent, never twice (WO37)
    db.conn.execute("UPDATE outbox SET message_id=? WHERE chat_id=? AND message_id=?", (int(mid), chat, placeholder))
    return True


LISTED_NO_BRAND = "listed with no brand (no brand label, tag or logo in the photos)"
_SITE_NO_BRAND = re.compile(r"\(no brand\)$|^listed without a brand$")
# WO54 §2: a correction hint or an old "look before approving" is no part of a decision's words in the private chat
# (a reply there reaches no item) — "…; reply with the condition if it is wrong", "— a reply 'boys' / 'girls' changes
# it", "; check before approving", "(WO37: a warning, never a hold)"
_HINT_TAIL = re.compile(r"\s*(?:[;,]|—|–|-)\s*(?:a\s+)?(?:reply|check before approving)\b.*$|\s*\(WO\d+[^)]*\)\s*$",
                        re.I | re.S)


def item_checks(it) -> list[str]:
    """WO54 §2 (the owner: every doubt gets the most reasonable choice, the post, and ONE check line): what was decided
    for the item under doubt — the card's ⚠️ statements, the record's notes, the decisions made later (`checks`) — in
    plain words, each once, for its private "Check —" line. Left out: what her own answer settled since (her size,
    brand, condition, Girls / Boys, category), the no-brand note (LISTED_NO_BRAND says it), the copy's own findings."""
    if it is None:
        return []
    gate = loads(it["gate"]) or {}
    owner = {"size": it["owner_size"], "brand": it["owner_brand"], "condition": it["owner_condition"],
             "kids": it["owner_kids_gender"], "category": it["owner_category"]}
    out: list[str] = []
    for raw in [*(gate.get("notes") or []), *(gate.get("info") or []), *(gate.get("checks") or [])]:
        text = _HINT_TAIL.sub("", str(raw or "")).strip(" ;,")
        if text.startswith("looks like item") and "?" in text:      # an older re-share flag, a question then
            text = text.split(" — ", 1)[0] + " — the same photos: listed once (settled by her price)"
        low = text.lower()
        if not text or raw == NO_BRAND_NOTE or text in out:
            continue
        if (owner["size"] and low.startswith("size:")) or (owner["brand"] and low.startswith(("brand:", "no brand:")))\
                or (owner["condition"] and ("condition" in low or "listed as like new" in low or "well-worn" in low))\
                or (owner["kids"] and low.startswith("kids:")) \
                or (owner["category"] and low.startswith(("category:", "department:"))):
            continue
        out.append(text)
    return out


def _say_checks(db: DB, iid: str, mps: list[str]) -> None:
    """The posters' notes (a brand left empty, a size taken as the nearest, …) to the ops chat, each once per item —
    never to the group (WO34, the owner: nothing technical in the group; the MNG card had a Depop note twice). WO54 §2:
    with them, every decision the agent made for the item under doubt (item_checks) — the ONE check line that replaces
    the questions it no longer asks."""
    said = set()
    for (detail,) in db.conn.execute("SELECT detail FROM events WHERE ref=? AND kind='checks_said'", (iid,)):
        said |= set((loads(detail) or {}).get("notes") or [])
    rows = {r["marketplace"]: r for r in db.listings_for(iid)}
    notes = []
    it = db.item(iid)
    gate = (loads(it["gate"]) or {}) if it is not None else {}
    said_by_gate = [*(gate.get("notes") or []), *(gate.get("info") or [])]   # the gate's notes are kept as its "info"
    decided_none = any(str(n).startswith("Brand: none") for n in gate.get("notes") or [])   # pipeline.BRAND_NONE
    no_brand = it is not None and not it["owner_brand"] and (NO_BRAND_NOTE in said_by_gate or decided_none)
    if it is not None and not it["owner_brand"] and NO_BRAND_NOTE in said_by_gate and LISTED_NO_BRAND not in said:
        notes.append(LISTED_NO_BRAND)                # 2026-10-27: no brand to be seen — said once, for the item
    notes += [c for c in item_checks(it) if c not in said and c not in notes]
    for mp in mps:
        f = loads((rows.get(mp) or {})["fields_json"]) if rows.get(mp) and rows[mp]["fields_json"] else {}
        for c in [*(f.get("guesses") or []), *(f"copy: {x}" for x in (f.get("checks") or []))]:
            if no_brand and _SITE_NO_BRAND.search(c):
                continue                               # the item's one line says it
            note = f"{LABEL.get(mp, mp)}: {c}"
            if note not in said and note not in notes:
                notes.append(note)
    if notes:
        it = db.item(iid)
        title = (((loads(it["renders"]) or {}).get("poshmark") or {}).get("title") if it else None) or iid
        db.log(iid, "checks_said", {"notes": notes})
        notify.say(f"Check — {title}:\n" + "\n".join(f"- {n}" for n in notes))


def _carry_done(s: Settings, db: DB, iid: str | None) -> None:
    """The 💤 line rides on the newest card (WO34): `iid` takes it (None: it stays where it is) and the card that had
    it is edited without it; the carrier's line follows the state (Done for now -> All done once Vinted caught up; gone
    while there is work). Each card is edited only when its text changes."""
    from thrift_agent import daily
    before = db.kv_get(CARRIER)
    carrier = iid or before
    if not carrier:
        return
    if before and before != carrier:
        _card(s, db, before, card_text(s, db, before), edit=True)
    db.kv_set(CARRIER, carrier)
    _card(s, db, carrier, card_text(s, db, carrier, daily.done_text(s, db)), edit=True)


def _sleep_line(s: Settings | None, db: DB) -> None:
    """WO54: every card can change now (each site edits its card as it lands), so after any change the 💤 line is
    settled on the newest card — taken there, its words as things are now (gone while there is work). Nothing while a
    first card is still due: that one will be the newest."""
    if s is None or cards_due(db):
        return
    newest = db.conn.execute("SELECT ref FROM outbox WHERE kind='posted' ORDER BY rowid DESC LIMIT 1").fetchone()
    if newest is not None:
        _carry_done(s, db, newest[0])


def unconfirmed(db: DB, iid: str) -> list[str]:
    """The item's marketplaces where a publish may be live but its address wasn't found (the owner's ⚠️ question)."""
    return [r["marketplace"] for r in db.listings_for(iid)
            if r["status"] == "failed" and not r["url"] and (r["error"] or "").startswith(UNCONFIRMED)]


def added_line(db: DB, iid: str, mps: list[str]) -> str:
    """The ops chat's "Added: <title> · Depop <url>" for sites added to an item already announced (WO32b)."""
    it = db.item(iid)
    title = (((loads(it["renders"]) or {}).get("poshmark") or {}).get("title") if it else None) or iid
    rows = {r["marketplace"]: r for r in db.listings_for(iid)}
    parts = " · ".join(f"{LABEL[mp]} {rows[mp]['url'] or ''}".rstrip() for mp in mps)
    return f"Added: {title} · {parts}"


SETTLED = ("posted", "failed", "skipped", "drafted", "dryrun", "delisted", "sold")
AWAY_WAIT = 600.0      # a site whose extension is away holds an item's line this long after its first live listing


def settled(s: Settings, db: DB, iid: str, now: datetime | None = None) -> bool:
    """Has every enabled site of the item had its say (WO33 A3)? Posted, failed (an unconfirmed publish too: its ⚠️
    question waits for the owner), skipped, drafted or dry-run — or queued on a site stopped for the window or at its
    daily cap, or on one whose extension has been away for 10 minutes since the item's first live listing (a moment's
    disconnect doesn't send the line without it). A site with no row yet, or one in progress, is not settled.
    WO54: it no longer holds the card back (live 2026-11-09: a paused Poshmark never settled — 19 items got no card)."""
    rows = {r["marketplace"]: r for r in db.listings_for(iid)}
    mps = [*(["poshmark"] if s.get("marketplaces.poshmark.enabled", False) else []), *enabled(s)]
    first = min((r["posted_at"] for r in rows.values() if r["status"] == "posted" and r["posted_at"]), default=None)
    now = now or datetime.now(timezone.utc)
    waited = first is not None and (now - datetime.fromisoformat(first)).total_seconds() >= AWAY_WAIT
    for mp in mps:
        row = rows.get(mp)
        if row is None:
            return False
        if row["status"] in SETTLED:
            continue
        if row["status"] == "queued" and mp != "poshmark" and (blocked(db, mp) or capped(s, db, mp) or
                                                               (mp not in reachable(s, db) and waited)):
            continue
        return False
    return True


OPEN_DAYS = 30         # WO54: an open card ("⏳" on it) older than this is no longer swept (its own landing still edits it)
SWEEP_MAX = 5          # WO54: cards sent or edited per housekeeping turn — Telegram takes about 20 messages a minute in a
#                        group (the 2:00 PM deploy finds the morning's 19 cards due at once)
_RENDER_SAID: set[str] = set()      # the items whose card couldn't be drawn, logged once a process


def cards_due(db: DB) -> list[str]:
    """WO54: the items live on a site whose ✅ card never went out — no card in the outbox and nothing announced (an
    item announced before WO34 by the one-line message, or with an ops "Added:" line, has its event) — the oldest live
    listing first. Three plain reads (the events table has no index on ref)."""
    said = {r[0] for r in db.conn.execute("SELECT DISTINCT ref FROM events WHERE kind='posted_announced'")}
    carded = {r[0] for r in db.conn.execute("SELECT DISTINCT ref FROM outbox WHERE kind='posted'")}
    rows = db.conn.execute("SELECT item_id, MIN(COALESCE(posted_at, updated_at)) AS first FROM listings WHERE "
                           "status='posted' GROUP BY item_id ORDER BY first, item_id").fetchall()
    return [r["item_id"] for r in rows if r["item_id"] not in said and r["item_id"] not in carded]


def open_cards(s: Settings, db: DB) -> list[str]:
    """WO54: the items whose ✅ card is out with a site still to come ("⏳" on it) and whose text, drawn now, differs
    (the 💤 line aside): a site live or failed for good, paused or back, at its daily cap … — edited by the sweep,
    oldest card first."""
    since = (datetime.now(timezone.utc) - timedelta(days=OPEN_DAYS)).isoformat(timespec="seconds")
    out = []
    for r in db.conn.execute("SELECT o.ref, o.text FROM outbox o WHERE o.kind='posted' AND o.sent_at >= ? AND "
                             "o.text LIKE '%⏳%' AND o.rowid=(SELECT MAX(x.rowid) FROM outbox x WHERE x.kind='posted' "
                             "AND x.ref=o.ref) ORDER BY o.rowid", (since,)).fetchall():
        try:
            if _body(card_text(s, db, r["ref"])) != _body(r["text"]):
                out.append(r["ref"])
        except Exception as e:  # noqa: BLE001 — one card that can't be drawn never stops the sweep
            if r["ref"] not in _RENDER_SAID:
                _RENDER_SAID.add(r["ref"])
                db.log(r["ref"], "card_render_failed", {"error": f"{type(e).__name__}: {e}"[:200]})
    return out


def waiting_lines(db: DB, s: Settings | None = None, limit: int | None = SWEEP_MAX) -> list[str]:
    """The cards the poster's sweep sees to on each housekeeping turn: first cards still due (Telegram away when the
    item went live; WO54: the morning's 19 items that waited for a paused Poshmark), then — with `s` — the open cards
    whose text would change. At most `limit` a turn (Telegram's group pace); the rest on the next turns."""
    due = cards_due(db)
    if limit and len(due) >= limit:
        return due[:limit]
    out = due + ([iid for iid in open_cards(s, db) if iid not in due] if s is not None else [])
    return out[:limit] if limit else out


def _line_due(s: Settings, db: DB, iid: str) -> bool:
    """Another item's first card is due now (not sent yet): "All done" goes on the newest card, so not on this one
    (WO33, a loaded CI runner: two items finished every site before either line went out, and both said "All done")."""
    return any(other != iid for other in cards_due(db))


def _update_card(s: Settings, db: DB, iid: str, new: list[str]) -> str | None:
    """WO54: the item's card is out — drawn again as its listings are now (a site's link in, a site that failed for good
    left out, a waiting site's state) and edited in place when it changed, never a second message; the new sites
    recorded as announced (their notes to the ops chat, once each); the 💤 line settled. The text when something
    changed, else None."""
    carrier = db.kv_get(CARRIER) == iid
    text = card_text(s, db, iid, daily.done_text(s, db) if carrier else None)
    changed = _card(s, db, iid, text, edit=True)
    if new:
        db.log(iid, "posted_announced", {"mps": new, "to": "edit"})
        _say_checks(db, iid, new)
    if changed or new:
        _sleep_line(s, db)
        return text
    return None


def announce(s: Settings | None, db: DB, iid: str) -> str | None:
    """The item's ONE card in the group (WO32b, WO34). WO54 (live 2026-11-09: Poshmark paused by one item's size, and
    the card waited for every site to settle — 19 items live on Depop and Vinted got no card and no link; the owner saw
    "nothing posted"): it goes out the FIRST time any of the item's sites is live, with a "⏳ <Site> — …" line for each
    site still to come, and is EDITED in place as each other site lands, fails for good (its line left out) or changes
    state (`_update_card`; the sweep sees to the states). Never twice: the outbox row is taken before the send (`_card`).
    Sites added to an item announced before WO34 (no card) go to the ops chat only: "Added: <title> · Depop <url>".
    Returns the text sent or edited (None: nothing new)."""
    seen = announced(db, iid)
    new = [r["marketplace"] for r in db.listings_for(iid) if r["status"] == "posted" and r["marketplace"] not in seen]
    if card_row(db, iid) is not None:                  # WO34: its card is out — changed in place, no new message
        return _update_card(s, db, iid, new) if s is not None else None   # (no settings: never a plain-text copy)
    if not new:
        return None
    if seen:                                           # announced before WO34 (the one-line message): the ops chat
        text = added_line(db, iid, new)
        db.log(iid, "posted_announced", {"mps": new, "to": "ops"})
        notify.say(text)
        return text
    last = daily.done_text(s, db) if s is not None and not _line_due(s, db, iid) else None
    text = card_text(s, db, iid, last)
    try:
        sent = _card(s, db, iid, text, edit=False)
    except RuntimeError as e:      # it never left the Mac (2026-10-27): not announced — the poster's sweep sends it
        db.log(iid, "card_not_sent", {"error": str(e)[:200]})
        return None
    if not sent:                                       # another process sent it a moment ago: never twice
        return None
    db.log(iid, "posted_announced", {"mps": new})     # only once the card is out (it had been logged before the send)
    _say_checks(db, iid, new)
    _sleep_line(s, db)                                 # on the newest card — nothing while another first card is due
    return text


BACKFILL_PACE = 3.2    # WO54: seconds between the backfill's cards (Telegram: about 20 messages a minute in a group)


def backfill_cards(s: Settings, db: DB, dry: bool = False, pace: float = BACKFILL_PACE,
                   sleep=None) -> list[tuple[str, str]]:
    """WO54 §0.5 (live 2026-11-09: 19 items live on Depop and Vinted had no ✅ card — the card waited for a paused
    Poshmark): `thrift cards --backfill` — every item live on a site whose card never went out (`cards_due`), oldest
    first, its card sent as `announce` sends it (its links, a "⏳" line for each site still to come), at Telegram's
    pace, never twice (the running poster's sweep may send the same cards: the outbox row decides). `dry`: only drawn.
    Returns (item, the card's text — or why it wasn't sent)."""
    wait = sleep or time.sleep
    out = []
    for n, iid in enumerate(cards_due(db)):
        if dry:
            out.append((iid, card_text(s, db, iid)))
            continue
        if n and pace > 0:
            wait(pace)
        text = announce(s, db, iid)
        if text is None:
            text = ("not sent: its card was out already (the poster's sweep)" if card_row(db, iid) is not None else
                    "not sent now: Telegram didn't take it (the poster's sweep sends it)")
        out.append((iid, text))
    return out


# ---------------------------------------------------------------- backfill: the existing closet

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 "
      "Safari/537.36")


def poshmark_live(url: str) -> bool | None:
    """Is this Poshmark listing still for sale? Its public page: listingDetails.inventory.status "available" → True;
    any other status, or a page that's gone (404 / 410) → False; a page that can't be read → None (unknown)."""
    return poshmark_page(url)[0]


_TLS = None


def _tls():
    """The TLS settings for Poshmark's public pages: a classic key exchange (P-256). Its CloudFront refuses OpenSSL
    3.5's default handshake — the post-quantum key share python.org's Python 3.14 on the Mac offers: every public page
    answered 403 "Request blocked" there (WO33, live), while the same request with P-256 — what OpenSSL 3.0 on the PC
    offers by default — is answered as it should be (404 for a deleted listing, 200 for a live one)."""
    global _TLS
    if _TLS is None:
        import ssl

        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        ctx.set_ecdh_curve("prime256v1")
        _TLS = ctx
    return _TLS


def poshmark_page(url: str) -> tuple[bool | None, int | None]:
    """(still for sale — as poshmark_live —, its price today: listingDetails.price_amount, whole dollars, or None)."""
    import httpx

    from thrift_agent.harvest import parse_state
    try:
        r = httpx.get(url, headers={"User-Agent": UA, "Accept": "text/html"}, timeout=20, follow_redirects=True,
                      verify=_tls())
    except httpx.HTTPError:
        return None, None
    if r.status_code in (404, 410):
        return False, None
    if r.status_code != 200:
        return None, None
    try:
        details = parse_state(r.text).get("$_listing_details", {}).get("listingDetails", {})
    except ValueError:
        return None, None
    status = ((details.get("inventory") or {}).get("status") or "").lower()
    try:
        price = round(float((details.get("price_amount") or {}).get("val")))
    except (TypeError, ValueError):
        price = None
    return (None if not status else status == "available"), (price or None)


def poshmark_listing(url: str) -> dict | None:
    """WO43: what the public listing says it is — {"department", "category", "subcategory", "size", "for_sale"} from
    listingDetails (department.display, category_v2.display, category_features[0].display, size) — or None when the
    page can't be read."""
    import httpx

    from thrift_agent.harvest import parse_state
    try:
        r = httpx.get(url, headers={"User-Agent": UA, "Accept": "text/html"}, timeout=20, follow_redirects=True,
                      verify=_tls())
    except httpx.HTTPError:
        return None
    if r.status_code in (404, 410):                   # WO49: the listing is gone (deleted) — not for sale
        return {"for_sale": False, "status": "gone", "sold": False, "photos": None, "price": None}
    if r.status_code != 200:
        return None
    try:
        d = parse_state(r.text).get("$_listing_details", {}).get("listingDetails", {})
    except ValueError:
        return None
    if not d:
        return None
    features = d.get("category_features") or []
    status = ((d.get("inventory") or {}).get("status") or "").lower()
    pictures = d.get("pictures")
    photos = (len(pictures) + (1 if d.get("cover_shot") else 0)) if isinstance(pictures, list) else None   # WO45 §6
    try:
        price = float(((d.get("price_amount") or {}).get("val")) or d.get("price") or 0) or None
    except (TypeError, ValueError):
        price = None
    return {"department": (d.get("department") or {}).get("display"),
            "category": (d.get("category_v2") or {}).get("display") or d.get("category"),
            "subcategory": (features[0].get("display") if features and isinstance(features[0], dict) else None),
            "size": d.get("size"), "for_sale": status == "available" if status else None,
            "status": status or None, "sold": "sold" in status,          # WO49: sold_out vs not_for_sale
            "photos": photos, "price": price,
            "title": d.get("title"), "description": d.get("description"),     # WO50: a text edit read back
            "brand": _brand_of(d)}                                            # WO52: a brand edit read back


def _brand_of(d: dict) -> str | None:
    """Poshmark's listing JSON names the brand as a string or an object (display / canonical_name / name)."""
    b = d.get("brand") or d.get("brand_obj")
    if isinstance(b, dict):
        b = b.get("display") or b.get("canonical_name") or b.get("name")
    return str(b) if b else None


def poshmark_cover(url: str) -> str | None:
    """The Poshmark listing's cover as its public page shows it (listingDetails.picture_url), or None (WO37)."""
    import httpx

    from thrift_agent.harvest import parse_state
    try:
        r = httpx.get(url, headers={"User-Agent": UA, "Accept": "text/html"}, timeout=20, follow_redirects=True,
                      verify=_tls())
        details = parse_state(r.text).get("$_listing_details", {}).get("listingDetails", {}) if r.status_code == 200 \
            else {}
    except (httpx.HTTPError, ValueError):
        return None
    return details.get("picture_url") or None


def backfill(s: Settings, db: DB, mps: list[str], check: bool = True, dry: bool = False,
             live=None, page=None) -> list[tuple[str, str]]:
    """`thrift crosslist --backfill`: every item live on Poshmark that isn't on these marketplaces yet, oldest first,
    queued — after its Poshmark page says it is still for sale (sold, removed or unreadable: left out). The poster
    then takes them within each marketplace's daily cap. `dry`: only says what it would queue.
    At each item's price on Poshmark TODAY (WO33, the owner: some were changed there by hand): a price that differs
    from ours becomes the item's price — the one Depop and Vinted get — and the differences go to the ops chat in one
    message. (`live`: a for-sale check without a price, the tests'.)"""
    read = page or ((lambda url: (live(url), None)) if live is not None else poshmark_page)
    out, changed = [], []
    rows = db.conn.execute("SELECT item_id, url FROM listings WHERE marketplace='poshmark' AND status='posted' "
                           "AND url IS NOT NULL ORDER BY posted_at, item_id").fetchall()
    for row in rows:
        iid = row["item_id"]
        missing = [mp for mp in mps if db.listing(iid, mp) is None]
        if not missing:
            continue
        note = ""
        if check:
            state, price = read(row["url"])
            if state is None:
                out.append((iid, "couldn't read its Poshmark page: left out"))
                continue
            if not state:
                out.append((iid, "no longer for sale on Poshmark: left out"))
                continue
            ours = int((db.item(iid) or {})["owner_price"] or 0)
            if price and price != ours:
                note = f" — Poshmark price ${price} (ours ${ours})" if ours else f" — Poshmark price ${price}"
                changed.append((iid, ours, price))
                if not dry:
                    db.set_item(iid, owner_price=price)
                    db.upsert_listing(iid, "poshmark", price=price)
                    db.log(iid, "price_from_poshmark", {"was": ours or None, "now": price})
        if dry:
            out.append((iid, f"would queue {', '.join(missing)}{note}"))
        else:
            queue(s, db, iid, missing, why="backfill")
            out.append((iid, f"queued {', '.join(missing)}{note}"))
    if changed and not dry:
        lines = [f"• {_title_of(db, iid)}: ${ours} → ${price}" if ours else f"• {_title_of(db, iid)}: ${price}"
                 for iid, ours, price in changed]
        text = (f"💲 Backfill at today's Poshmark prices — {len(changed)} differ from our records (now the price on "
                f"Depop and Vinted too):\n" + "\n".join(lines))
        notify.say(text[:3900])
    return out


def _title_of(db: DB, iid: str) -> str:
    it = db.item(iid)
    return ((loads(it["renders"]) or {}).get("poshmark") or {}).get("title") or iid if it else iid
