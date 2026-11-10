"""WO56 (the owner, 2026-11-09 8:34 PM: "give me a WO so I can pass captcha or whatever is needed by human"): human steps
in one minute. Since WO54 only four things wait for a person — the price, a size no photo shows, a password or sign-in
code, a real CAPTCHA — and WO54 §4 hid the agent's browser (the Thrift Chrome opens only for work, quits after 10 idle
minutes, its tidy closes every page no job holds), a 🚨 "Vinted shows a CAPTCHA" pointed to a window nobody could find.
The owner does the step from his PC or phone on the Mac's screen (Remote Management / VNC over Tailscale).

What a human step is (`kind_of`): a sign-in that needs a password ("login"); a one-time code or the site's own "confirm
it's you" ("verify"); a real CAPTCHA ("captcha"); a "verify you are human" page that does not clear by itself within 2
minutes, on two loads 15 minutes apart ("challenge" — the first is waited out quietly: `quiet_challenge`). A block page
("turned the Mac away") is none: nobody can type or click it away — it passes by itself (its self-test and the 20-minute
🚨, as before WO56). The agent never solves a CAPTCHA, never uses a solving service, never types a password.

- begin: the site paused as an account pause (crosslist.block → health: every other site and item keeps posting), its
  page HELD — the bridge tells the extension (`for_bridge`): the tab in a window of its own titled "<Site> — human step",
  never tidied, the Thrift Chrome never quit while a step is pending — and ONE private message with the screenshot. No
  group line, no 🚨 (WO54 §2's account alarm and the group's plain line are replaced by it).
- "show" in the private chat: that window to the front on the Mac (only then, only that one), back to hidden after 5
  minutes or when it clears; "later": no reminder for 12 h; "done": looked at again now (accepted, never needed).
- clear: the extension reads the held page every 10 seconds (`seen`); a page that is no stop page any more → ONE
  "✅ <Site> back — posting resumed", the site resumed (health.resume), the item goes on. A step whose page is gone (the
  Chrome quit for the quiet hours, a tab closed) is looked at again by the site's own self-test.
- reminders: every 12 h while pending and once at 2:05 PM after the quiet hours (they still win: at 8:30 everything
  stops, the step waits for 2:00 PM unless she says "post now"); never more.
- the record: event `human_step` {site, kind, page, at, cleared_at, seconds, person_needed} for every step — a security
  check that passed by itself too (person_needed false) — kv `human_step_log` (the last 30), and `thrift doctor`.

kv `human_steps` (written by the poster): {site: {"kind", "page", "since", "reason", "said": [iso…], "snooze", "shown",
"done", "seen", "seen_page", "seen_url", "good", "shot"}}; kv `human_step_asks` (the worker's replies, taken by the
poster): {site: {"show" | "later" | "done": iso}}; kv `human_challenge` {site: iso}: a first "Just a moment" waited out."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thrift_agent import notify
from thrift_agent.db import DB, loads

KEY = "human_steps"
ASKS = "human_step_asks"
CHALLENGED = "human_challenge"
LOG = "human_step_log"
LOG_MAX = 30
HUMAN = ("login", "captcha", "verify", "challenge")          # the pages only a person settles (a block passes by itself)
STOPS = ("login", "captcha", "verify", "challenge", "block")  # a held page that still shows one of these: not cleared
GOOD = ("form", "listing", "landing")                       # a page that is plainly the site's own again: cleared at once
EXTENSION_SITES = ("depop", "vinted")                        # their steps are held in the Thrift Chrome (the bridge)
REMIND_EVERY = timedelta(hours=12)
AFTER_QUIET = timedelta(minutes=5)       # the reminder after the quiet hours: at 2:05 PM
SNOOZE = timedelta(hours=12)             # "later"
SHOW_FOR = 300.0                         # "show": the window in front this long at most (seconds), then hidden again
CHALLENGE_RETRY = 15 * 60                # a first "Just a moment" still there after 2 min: tried again this long after
CHALLENGE_WITHIN = timedelta(hours=2)    # ... a second one within this is the human step
WATCH_FRESH = 45.0                       # the extension read the held page this recently (s): watched, no self-test
STALE_AFTER = 120.0                      # ... no reading this long: its page is opened again by the self-test (once)
DEFAULT_ADDRESS = "100.109.116.57"       # the Mac's Tailscale address (settings human_steps.vnc_address)
LABEL = {"poshmark": "Poshmark", "depop": "Depop", "vinted": "Vinted"}
WHAT = {"captcha": "a CAPTCHA", "verify": "a check that it's you (a code or a confirmation)",
        "challenge": "a “verify you are human” check that didn't pass by itself", "login": "a sign-in"}
DO = {"captcha": "solve it", "verify": "do the check (any code is typed by you)",
      "challenge": "do the check (tick its box)", "login": "sign in"}
WORDS = {"show": "show", "later": "later", "done": "done"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def _when(v) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _clock(t: datetime) -> str:
    return t.astimezone().strftime("%I:%M %p").lstrip("0")


def _all(db: DB) -> dict:
    return loads(db.kv_get(KEY)) or {}


def _save(db: DB, data: dict) -> None:
    db.kv_set(KEY, json.dumps(data))


def address(s=None) -> str:
    """The Mac's address for Screen Sharing (settings human_steps.vnc_address)."""
    try:
        if s is None:
            from thrift_agent.config import settings
            s = settings()
        return str(s.get("human_steps.vnc_address") or DEFAULT_ADDRESS).strip() or DEFAULT_ADDRESS
    except Exception:  # noqa: BLE001 — the message never fails for its address
        return DEFAULT_ADDRESS


# ---------------------------------------------------------------- what a human step is

def kind_of(page: str | None, reason: str = "") -> str | None:
    """"login" | "captcha" | "verify" | "challenge" for a page only a person settles, else None (a block page, a
    restricted account, anything else). `page`: the poster's word for what the site showed; without one (Poshmark's
    own poster), read from the reason."""
    if page in HUMAN:
        return page
    if page:
        return None
    low = (reason or "").lower()
    if "restricted" in low or "403" in low or "turned" in low or "block" in low:
        return None
    if "captcha" in low:
        return "captcha"
    if re.search(r"not logged in|logged out|log ?in|sign ?in", low):
        return "login"
    if re.search(r"verif|confirm it", low):
        return "verify"
    return None


def pending(db: DB, site: str | None = None) -> dict:
    """The pending human steps ({site: record}), or one site's record ({} when none)."""
    data = _all(db)
    return (data.get(site) or {}) if site is not None else data


def watching(db: DB, site: str, at: datetime | None = None) -> bool:
    """The step's page is held and the extension read it within WATCH_FRESH seconds, still a stop page: the 10-second
    check decides when it clears (the site's self-test waits)."""
    rec = pending(db, site)
    seen = _when(rec.get("seen")) if rec else None
    if seen is None:
        return False
    return ((at or _now()) - seen).total_seconds() < WATCH_FRESH and rec.get("seen_page") in STOPS


def _others(db: DB, site: str, s=None) -> list[str]:
    """The other sites that are on and not paused: "Depop and Poshmark keep posting meanwhile"."""
    from thrift_agent import health
    try:
        if s is None:
            from thrift_agent.config import settings
            s = settings()
        on = [mp for mp in ("depop", "vinted") if s.get(f"marketplaces.{mp}.enabled", False)] + \
            (["poshmark"] if s.get("marketplaces.poshmark.enabled", True) is not False else [])
    except Exception:  # noqa: BLE001 — the message never fails for this
        on = ["depop", "vinted", "poshmark"]
    return [LABEL[mp] for mp in on if mp != site and not health.state(db, mp)]


def _keep_posting(others: list[str]) -> str:
    if not others:
        return ""
    names = " and ".join(others) if len(others) <= 2 else ", ".join(others[:-1]) + f" and {others[-1]}"
    return f" {names} keep{'s' if len(others) == 1 else ''} posting meanwhile."


def message(site: str, kind: str, at: datetime, others: list[str], addr: str, shot: bool = True) -> str:
    """The one private message: what the site wants, what to do in one minute, and that the rest goes on."""
    label = LABEL.get(site, site)
    pic = " (screenshot)" if shot else ""
    keep = _keep_posting(others)
    if kind == "login":
        return (f"🔐 {label} wants you to sign in{pic}, since {_clock(at)}. Connect to the Mac's screen (Screen "
                f"Sharing, {addr}), reply show and type the password or code yourself on the Mac's screen — the agent "
                f"never types it.{keep}")
    return (f"🔐 {label} wants a person — {WHAT.get(kind, 'a check')}{pic}, since {_clock(at)}. 1 minute: connect to "
            f"the Mac's screen (Screen Sharing, {addr}), reply show, {DO.get(kind, 'do it')} in the window that comes "
            f"up, and that's it.{keep}")


def reminder(site: str, rec: dict, at: datetime, addr: str) -> str:
    label = LABEL.get(site, site)
    since = _when(rec.get("since")) or at
    what = "a sign-in" if rec.get("kind") == "login" else WHAT.get(rec.get("kind"), "a check")
    return (f"🔐 Reminder ({_clock(at)}): {label} still wants a person — {what}, since {_clock(since)}. Connect to the "
            f"Mac's screen (Screen Sharing, {addr}) and reply show — or later: no reminder for 12 hours.")


# ---------------------------------------------------------------- a step begins

def quiet_challenge(db: DB, site: str, reason: str, page: str | None, at: datetime | None = None) -> bool:
    """WO56 §1: a "Just a moment… verifying you are human" page that did not pass by itself in 2 minutes (the page waits
    it out: ext/content/common.js). The FIRST is no human step — the site paused quietly (no line, no picture), tried
    again CHALLENGE_RETRY later by its self-test. There again within CHALLENGE_WITHIN: False — the caller makes it a
    human step. True when it was the quiet first one."""
    if page != "challenge" or pending(db, site):
        return False
    from thrift_agent import health
    t = at or _now()
    seen = loads(db.kv_get(CHALLENGED)) or {}
    first = _when(seen.get(site))
    if first is not None and t - first < CHALLENGE_WITHIN:
        seen.pop(site, None)
        db.kv_set(CHALLENGED, json.dumps(seen))
        return False
    seen[site] = _iso(t)
    db.kv_set(CHALLENGED, json.dumps(seen))
    health.pause(db, site, reason, "account", error=reason, page="challenge", say=False, wait=CHALLENGE_RETRY, at=t)
    db.log(None, "human_challenge", {"site": site, "at": _iso(t),
                                     "retry_at": _iso(t + timedelta(seconds=CHALLENGE_RETRY))})
    return True


def begin(db: DB, site: str, page: str | None, reason: str = "", shot=None, at: datetime | None = None,
          s=None) -> bool:
    """A human step on `site` (its pause is health's, made by the caller): recorded and said ONCE, in the private chat,
    with the page's screenshot. A step pending already keeps its record (the newer page is the held one; never a
    second message). True when the message went now."""
    kind = kind_of(page, reason)
    if kind is None:
        return False
    t = at or _now()
    data = _all(db)
    cur = data.get(site)
    if cur:
        cur.update(page=page or cur.get("page"), reason=(reason or cur.get("reason") or "")[:300], again=_iso(t),
                   good=0)
        if kind != cur.get("kind"):
            cur["kinds"] = sorted({*(cur.get("kinds") or [cur.get("kind")]), kind})
        data[site] = cur
        _save(db, data)
        db.log(None, "human_step_again", {"site": site, "kind": kind, "page": page})
        return False
    shown_shot = bool(shot) and Path(str(shot)).is_file()
    data[site] = {"kind": kind, "page": page or kind, "since": _iso(t), "reason": (reason or "")[:300],
                  "said": [_iso(t)], "snooze": None, "shown": None, "done": None, "seen": None, "seen_page": None,
                  "seen_url": None, "good": 0, "shot": str(shot) if shown_shot else None}
    _save(db, data)
    db.log(None, "human_step_pending", {"site": site, "kind": kind, "page": page, "at": _iso(t),
                                        "reason": (reason or "")[:200]})
    text = message(site, kind, t, _others(db, site, s), address(s), shot=shown_shot)
    if shown_shot:
        notify.ops_photo(Path(str(shot)), text)
    else:
        notify.say(text)
    return True


def adopt(db: DB, site: str, p: dict, at: datetime | None = None) -> bool:
    """An account pause with a human-step page that has no record (one made before WO56, or by a direct pause): its
    step begins now — the one message. A first challenge waited out quietly is left to its retry. True when said."""
    if pending(db, site):
        return False
    page, reason = p.get("page"), p.get("reason") or ""
    if page == "challenge" and site in (loads(db.kv_get(CHALLENGED)) or {}):
        return False
    if kind_of(page, reason) is None:
        return False
    return begin(db, site, page, reason, at=at)


# ---------------------------------------------------------------- the held page, read every 10 seconds

def seen(db: DB, site: str, page: str | None, url: str | None = None, at: datetime | None = None,
         held: bool = True) -> str | None:
    """The extension's reading of a step's held page (its `step_page` event): still a stop page → "held"; no page any
    more (the tab closed, the Chrome restarted) → "gone" — the site's self-test looks at it again now; a page that is
    the site's own again → "cleared" (a form / listing at once, any other page on the second reading in a row: a page
    between two states is no proof). None when no step is pending there."""
    data = _all(db)
    rec = data.get(site)
    if not rec:
        return None
    t = at or _now()
    page = str(page or "unreadable")
    rec.update(seen=_iso(t), seen_page=page if held else "gone", seen_url=(url or None) and str(url)[:200])
    if not held or page == "gone":
        rec["good"] = 0
        retest = not rec.get("retest")
        rec["retest"] = rec.get("retest") or _iso(t)
        data[site] = rec
        _save(db, data)
        if retest:                                 # once: its pause's self-test now (it opens the page again)
            _test_now(db, site, t)
        return "gone"
    if page in STOPS or page in ("unreadable", "loading"):
        rec["good"] = 0
        rec.pop("retest", None)                    # held again: a later loss asks for a fresh look again
        data[site] = rec
        _save(db, data)
        return "held"
    rec["good"] = int(rec.get("good") or 0) + 1
    data[site] = rec
    _save(db, data)
    if page in GOOD or rec["good"] >= 2:
        clear(db, site, f"its page is {page} now", at=t)
        return "cleared"
    return "clearing"


def _test_now(db: DB, site: str, t: datetime) -> None:
    """The step's page is gone: the site's self-test is due now (it opens the page again; still there → held again,
    no new message; clear → the site resumes)."""
    from thrift_agent import health
    hs = loads(db.kv_get(health.HEALTH)) or {}
    if site in hs and not hs[site].get("owner"):
        hs[site]["due"] = _iso(t)
        db.kv_set(health.HEALTH, json.dumps(hs))


def recheck(db: DB, site: str, at: datetime | None = None) -> None:
    """Look at the step again now: its site's self-test is due (Poshmark's page left its sign-in; "done")."""
    _test_now(db, site, at or _now())


# ---------------------------------------------------------------- a step ends

def _end(db: DB, site: str, why: str, at: datetime | None = None) -> bool:
    """The step's record ends: its event, the log, ONE "✅ <Site> back — posting resumed". True when there was one."""
    data = _all(db)
    rec = data.pop(site, None)
    if rec is None:
        return False
    _save(db, data)
    t = at or _now()
    since = _when(rec.get("since")) or t
    person = bool(rec.get("shown") or rec.get("done"))
    record(db, site, rec.get("kind") or "login", rec.get("page") or rec.get("kind"), since, t, person, why=why,
           shown=rec.get("shown"), reminders=max(0, len(rec.get("said") or []) - 1))
    from thrift_agent import health
    notify.say(f"✅ {LABEL.get(site, site)} back — posting resumed{health.going_text(db, site)} (its check cleared at "
               f"{_clock(t)}).")
    return True


def clear(db: DB, site: str, why: str = "its page cleared", at: datetime | None = None) -> bool:
    """The step cleared (its page, or "done" and the self-test): the ✅ line once, the site resumed. True when it
    was pending."""
    if not _end(db, site, why, at):
        return False
    from thrift_agent import health
    health.resume(db, site, f"the human step cleared: {why}", say=False)
    return True


def ended(db: DB, site: str, why: str = "", page: str | None = None) -> bool:
    """health.resume's hook: the site's pause ended (its self-test passed, the owner resumed it) — a human step there
    ends with its own ✅ line (True: health says nothing more); a first "Just a moment" waited out quietly (`page`
    "challenge": the pause's own) is recorded as a check that passed by itself on its retry (no line)."""
    if _end(db, site, why or "its pause ended"):
        return True
    seen_ = loads(db.kv_get(CHALLENGED)) or {}
    if site not in seen_:
        return False
    first = _when(seen_.pop(site, None))
    db.kv_set(CHALLENGED, json.dumps(seen_))
    if first is not None and page == "challenge":
        record(db, site, "challenge", "challenge", first, _now(), False, why=why or "its retry passed")
    return False


def record(db: DB, site: str, kind: str, page: str | None, at: datetime, cleared_at: datetime | None,
           person_needed: bool, **extra) -> dict:
    """WO56 §4: every human step logged — event `human_step` {site, kind, page, at, cleared_at, seconds, person_needed}
    and the small kv `human_step_log` (the last LOG_MAX), for `thrift doctor` and "more than once a day?"."""
    row = {"site": site, "kind": kind, "page": page, "at": _iso(at), "cleared_at": _iso(cleared_at) if cleared_at else None,
           "seconds": round((cleared_at - at).total_seconds()) if cleared_at else None, "person_needed": person_needed,
           **{k: v for k, v in extra.items() if v is not None}}
    db.log(None, "human_step", row)
    log = loads(db.kv_get(LOG)) or []
    db.kv_set(LOG, json.dumps([*log, row][-LOG_MAX:]))
    return row


def on_ext_event(db: DB, ev: dict, at: datetime | None = None) -> str | None:
    """The extension's own events about the steps (bridge.EXT_EVENTS): `step_page` (the held page, every 10 s) →
    `seen`; `challenge` {state: "cleared", seconds} — a "Just a moment" that passed by itself on a job's page → its
    `human_step` record (person_needed false), nothing said."""
    kind = ev.get("event")
    site = str(ev.get("site") or "")
    if site not in LABEL:
        return None
    if kind == "step_page":
        return seen(db, site, ev.get("page"), ev.get("url"), at=at, held=bool(ev.get("held", True)))
    if kind == "step_shown":
        data = _all(db)
        if site in data:
            data[site]["shown"] = _iso(at or _now())
            _save(db, data)
        return "shown"
    if kind == "challenge" and ev.get("state") == "cleared":
        t = at or _now()
        try:
            secs = max(0.0, float(ev.get("seconds") or 0))
        except (TypeError, ValueError):
            secs = 0.0
        record(db, site, "challenge", "challenge", t - timedelta(seconds=secs), t, False, mode=ev.get("mode"),
               by="the page itself")
        return "challenge cleared"
    return None


# ---------------------------------------------------------------- the owner's words: show, later, done

_WORD = re.compile(r"^\s*/?\s*(show|later|done)\s*(poshmark|depop|vinted)?\s*[.!]*\s*$", re.I)


def command(text: str | None) -> tuple[str, str | None] | None:
    """("show" | "later" | "done", the site named or None) for the owner's reply in the private chat, else None."""
    m = _WORD.match(text or "")
    if not m:
        return None
    return m[1].lower(), (m[2].lower() if m[2] else None)


def ask(db: DB, word: str, site: str | None = None, at: datetime | None = None) -> list[str]:
    """The worker's side of a reply: the ask recorded for the poster (its housekeeping and the bridge's watch take it
    within ~15 s). Returns the sites it is for (every pending one when none is named)."""
    data = _all(db)
    sites = [site] if site in data else ([] if site else list(data))
    if not sites:
        return []
    t = at or _now()
    asks = loads(db.kv_get(ASKS)) or {}
    for mp in sites:
        asks.setdefault(mp, {})[word] = _iso(t)
    db.kv_set(ASKS, json.dumps(asks))
    db.log(None, "human_step_ask", {"word": word, "sites": sites})
    return sites


def reply(db: DB, word: str, site: str | None = None, at: datetime | None = None, s=None) -> str:
    """The answer to "show" / "later" / "done" in the private chat (sent at once by the worker)."""
    sites = ask(db, word, site, at)
    if not sites:
        return "Nothing waits for a person on the Mac now — every site posts by itself."
    names = " and ".join(LABEL.get(x, x) for x in sites)
    if word == "show":
        what = "; ".join(f"{LABEL.get(x, x)}: {DO.get(pending(db, x).get('kind'), 'do the check')}" for x in sites)
        return (f"✓ {names}: the window comes to the front on the Mac's screen within 15 seconds (Screen Sharing, "
                f"{address(s)}) — {what} there. It goes back by itself after 5 minutes, or as soon as it's done.")
    if word == "later":
        return f"✓ No reminder about {names} for 12 hours — it still waits; reply show when you're at a screen."
    return f"✓ Looking at {names} again now — “✅ {LABEL.get(sites[0], sites[0])} back” comes as soon as its page is clear."


def take_show(db: DB, sites) -> list[str]:
    """The poster's side: the sites among `sites` whose window the owner asked to see (each ask taken once)."""
    asks = loads(db.kv_get(ASKS)) or {}
    out = []
    for mp in list(asks):
        if mp in sites and asks[mp].pop("show", None) is not None:
            if pending(db, mp):
                out.append(mp)
            if not asks[mp]:
                asks.pop(mp)
    if out or any(mp in sites for mp in asks):
        db.kv_set(ASKS, json.dumps(asks))
    return out


def shown(db: DB, site: str, at: datetime | None = None) -> None:
    data = _all(db)
    if site in data:
        data[site]["shown"] = _iso(at or _now())
        _save(db, data)


def for_bridge(db: DB) -> dict:
    """The pending steps the Thrift Chrome holds (Depop / Vinted): {site: {"label", "kind", "since"}} — what the bridge
    hands the extension (its tab kept in a window of its own, titled "<Site> — human step", read every 10 s)."""
    return {mp: {"label": f"{LABEL[mp]} — human step", "kind": rec.get("kind"), "since": rec.get("since")}
            for mp, rec in _all(db).items() if mp in EXTENSION_SITES}


# ---------------------------------------------------------------- the reminders (the poster's housekeeping)

def _after_quiet(since: datetime, t: datetime, rec: dict, s=None) -> str | None:
    """"due" when the step was pending through a quiet window that ended 5+ minutes ago with no reminder since its end;
    "wait" inside the 5 minutes after one (the 2:05 PM reminder is the one); else None."""
    from thrift_agent import quiet
    q = quiet.cfg(s)
    said = [x for x in (_when(v) for v in rec.get("said") or []) if x is not None]
    for a, b in reversed(quiet.windows(t - timedelta(days=2), t, q)):
        if b > t or since >= a:
            continue
        if any(x >= b for x in said):
            return None
        return "due" if t >= b + AFTER_QUIET else "wait"
    return None


def tick(db: DB, at: datetime | None = None, s=None) -> list[str]:
    """From the poster's housekeeping: the owner's "later" / "done" applied, and the reminders — every 12 h while a step
    is pending, once at 2:05 PM after the quiet hours, never more, none while snoozed. Returns the sites reminded."""
    t = at or _now()
    data = _all(db)
    if not data:
        return []
    asks = loads(db.kv_get(ASKS)) or {}
    took = False
    for mp, words in list(asks.items()):
        rec = data.get(mp)
        for word in ("later", "done"):
            when = _when(words.pop(word, None))
            if when is None:
                continue
            took = True
            if rec is None:
                continue
            if word == "later":
                rec["snooze"] = _iso(when + SNOOZE)
            else:
                rec["done"] = _iso(when)
                if not watching(db, mp, t):
                    _test_now(db, mp, t)            # its page isn't held: the self-test looks again now
        if not words:
            asks.pop(mp)
    if took:
        db.kv_set(ASKS, json.dumps(asks))
    out = []
    addr = address(s)
    for mp, rec in data.items():
        since = _when(rec.get("since")) or t
        quiet_since = _when(rec.get("seen")) or since
        if (mp in EXTENSION_SITES and not rec.get("retest") and not watching(db, mp, t)
                and (t - quiet_since).total_seconds() > STALE_AFTER):
            # its page isn't being read (the Thrift Chrome quit for the quiet hours, crashed, or was never told): the
            # site's self-test opens it again now — once, until a reading comes (no test every turn)
            rec["retest"] = _iso(t)
            _test_now(db, mp, t)
            took = True
        snooze = _when(rec.get("snooze"))
        if snooze is not None and t < snooze:
            continue
        quiet_state = _after_quiet(since, t, rec, s)
        if quiet_state == "wait":
            continue
        said = [x for x in (_when(v) for v in rec.get("said") or []) if x is not None]
        last = max(said) if said else since
        if quiet_state != "due" and t - last < REMIND_EVERY:
            continue
        notify.say(reminder(mp, rec, t, addr))
        rec["said"] = [*(rec.get("said") or []), _iso(t)][-20:]
        db.log(None, "human_step_reminded", {"site": mp, "kind": rec.get("kind"), "after_quiet": quiet_state == "due"})
        out.append(mp)
    if took or out:
        _save(db, data)
    return out


# ---------------------------------------------------------------- thrift doctor

def doctor_lines(db: DB, at: datetime | None = None) -> list[str]:
    """A pending step (what, since when, its page held or not, when it was last read) and the last day's steps."""
    t = at or _now()
    out = []
    for mp, rec in _all(db).items():
        since = _when(rec.get("since")) or t
        seen_at = _when(rec.get("seen"))
        held = (f"its page held in the Thrift Chrome, read {round((t - seen_at).total_seconds())} s ago "
                f"({rec.get('seen_page')})" if seen_at and rec.get("seen_page") != "gone" else
                "its page isn't open now — the site's self-test opens it again" if mp in EXTENSION_SITES else
                "in Poshmark's own browser")
        snooze = _when(rec.get("snooze"))
        out.append(f"🔐 Human step: {LABEL.get(mp, mp)} wants a person — {WHAT.get(rec.get('kind'), 'a check')} since "
                   f"{_clock(since)} · {held}" + (f" · reminders snoozed until {_clock(snooze)}" if snooze and
                                                   snooze > t else "") + " · reply show in the private chat")
    day = [r for r in (loads(db.kv_get(LOG)) or []) if (w := _when(r.get("at"))) and t - w < timedelta(days=1)]
    if day:
        parts = [f"{LABEL.get(r.get('site'), r.get('site'))} {r.get('kind')} {_clock(_when(r['at']))}"
                 + (f" ({r['seconds']} s" + (", a person" if r.get("person_needed") else ", by itself") + ")"
                    if r.get("seconds") is not None else "") for r in day[-6:]]
        out.append(f"Human steps in the last day: {len(day)} — " + " · ".join(parts))
    return out
