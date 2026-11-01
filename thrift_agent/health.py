"""WO51 §2 (live 2026-11-01): what a failure is about, and each site's health — paused, cooling down, tested, back.

That day three sites failed within two minutes, none of it about the items: Poshmark's Chrome had been quit by hand from
the Dock four days before (the poster never reopened it), Vinted had rebuilt its listing form (our brand step threw
"Illegal invocation"), Depop had removed its shipping choice. Every failure used up an item's attempts and pushed its
site to "stopped for this window" — and with the Mac always on, a window never ended: the sites would never have tried
again by themselves. Now:

- every failure has a KIND (`kind_of`): item — the site refused something about this listing, or a field this item can't
  satisfy; site — an expected element is missing, the form changed, an unknown page or dialog; infra — the browser,
  context or page closed, the extension away, the network, our own code throwing (TypeError, Illegal invocation…);
- only item failures count toward the item's attempts and the site's "3 in a row" brake;
- a site or infra failure puts the item back in line untouched (`non_item`): the attempt given back, the item behind the
  others for a while (`deferred`), and the same failure twice in a row pauses the site at once — one private line per
  site per problem (`pause`), the number of items waiting, at most one picture; never a ❌ per item;
- an item that keeps meeting the same "site" failure while it is alone in it (ITEM_AFTER times) is about the item after
  all: then it counts as an item failure — one item never holds its site for good;
- a paused site comes back by itself: after COOL (30 min, doubling to COOL_MAX) one dry run on its next item
  (`test_due` → the poster's test run → `tested`): passing resumes posting with ONE line, failing pauses it again with
  no new line; a pause by the owner (`thrift pause`) waits for `thrift resume` (a test first, `--dry`: the test only).

kv `site_health` holds the pauses, `site_nonitem` the last non-item failure per site, `nonitem_tries` an item's own run,
`site_defer` the items put behind the others."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from thrift_agent import notify
from thrift_agent.db import DB, loads

ITEM, SITE, INFRA = "item", "site", "infra"
HEALTH = "site_health"        # kv: {mp: {"kind", "problem", "reason", "since", "due", "wait", "said", "tests", "owner", …}}
STREAK = "site_nonitem"       # kv: {mp: {"sig", "item", "n", "at"}} — the last non-item failure on the site
TRIES = "nonitem_tries"       # kv: {"<item>|<mp>": {"sig", "n"}} — one item's run of the same non-item failure
DEFER = "site_defer"          # kv: {mp: {item: until}} — put behind the others after a non-item failure
COOL = 30 * 60                # the first test run this long after a pause …
COOL_MAX = 4 * 3600           # … doubling up to this
DEFER_FOR = 10 * 60           # an item that met a site/infra failure waits this long behind the others
ITEM_AFTER = 3                # the same non-item failure on one item alone this many times: it is the item's
LABEL = {"poshmark": "Poshmark", "depop": "Depop", "vinted": "Vinted"}
ACCOUNT_PAGES = ("login", "captcha", "block", "verify")

# What a failure is about — read from its words (the extension also names a code error and a page it doesn't know).
INFRA_RE = re.compile(
    r"TargetClosedError|Target (?:page, context or browser|closed)|browser has (?:been closed|disconnected)|"
    r"Browser closed|Connection closed|Execution context was destroyed|Illegal invocation|"
    r"\b(?:TypeError|ReferenceError|RangeError|SyntaxError|EvalError)\b|is not a function|Cannot read propert|"
    r"is not defined|didn't take the job|extension (?:isn't|is not|away|disconnected)|bridge|"
    r"the page doesn't answer|Receiving end does not exist|Could not establish connection|the form wasn't done in|"
    r"no answer in \d+|net::ERR_|ConnectError|ConnectTimeout|ReadTimeout|Network is unreachable|ECONNRESET|"
    r"the browser (?:closed|couldn't)", re.I)
SITE_RE = re.compile(
    r"not on the page|the page changed|can't type into|not the sell form|unknown (?:page|dialog)|not a listing page|"
    r"nothing happened for \d+ ?s|differs from the recording|not recorded yet|no search box|did not show|didn't show|"
    r"neither showed|can't tell whether|offered: ?\)|not offered \(\s*\)|system error|didn't take|stayed disabled|"
    r"toggles|didn't preselect|did not preselect|isn't (?:there|on the form)|^Mismatch: |no 'List without brand'|"
    r"photos? \d+ of \d+|the page reloaded|a big brand is never", re.I)
ITEM_RE = re.compile(r"refused the form: (?!no error shown)|\bFill in |\(offered: [^)\s][^)]*\)|"
                     r"not offered \([^)\s][^)]*\)|\bskipped: ", re.I)


def now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def _when(v) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def kind_of(error: str | None, hint: str | None = None, clicked: bool = False) -> str:
    """item | site | infra for a failure's words. `hint`: what the extension said it was ("code" — our own script threw
    — is infra; "site"). A failure after the final click is always the item's own flow (unconfirmed / refused), never
    put back in line here."""
    if clicked:
        return ITEM
    if hint in ("code", "infra"):
        return INFRA
    e = error or ""
    if ITEM_RE.search(e):
        return ITEM
    if INFRA_RE.search(e):
        return INFRA
    if hint == "site" or SITE_RE.search(e):
        return SITE
    return ITEM


def signature(error: str | None) -> str:
    """The failure without its particulars (values, numbers, quoted text, details in brackets), to tell "the same
    failure again" — "Mismatch: …" by the fields that differed."""
    e = (error or "").strip()
    if e.lower().startswith("mismatch: "):
        fields = sorted(set(re.findall(r"(?:^|;\s*)([a-z_][\w-]*)", e[10:].lower())))
        return "mismatch: " + ",".join(fields)
    e = re.sub(r"\([^)]*\)", "", e)
    e = re.sub(r"'[^']*'|\"[^\"]*\"|“[^”]*”", "''", e)
    e = re.sub(r"https?://\S+", "<url>", e)
    e = re.sub(r"\d+", "#", e)
    return re.sub(r"\s+", " ", e).strip(" :.;").lower()[:160]


def _step_of(mp: str, error: str) -> tuple[str, str]:
    """("brand", "Illegal invocation") from "Vinted brand: Illegal invocation"."""
    e = (error or "").strip()
    label = LABEL.get(mp, mp)
    if e.lower().startswith(label.lower() + " "):
        e = e[len(label) + 1:]
    step, _, rest = e.partition(":")
    if not rest or " " in step.strip():
        return "", e
    return step.strip(), rest.strip()


def plain(mp: str, error: str | None, kind: str) -> str:
    """The problem in the owner's words, for the one line a site's pause says."""
    e = error or ""
    step, rest = _step_of(mp, e)
    rest = re.sub(r"\s*\([^)]*\)", "", rest).strip() or rest
    if re.search(r"TargetClosedError|browser has been closed|Browser closed|Target closed", e, re.I):
        return "its browser was closed"
    if re.search(r"the browser couldn't", e, re.I):
        return e[:160]
    if re.search(r"Illegal invocation|\b(?:TypeError|ReferenceError|RangeError|SyntaxError)\b|is not a function|"
                 r"Cannot read propert|is not defined", e, re.I):
        where = f" on its {step} step" if step else ""
        return f"our form-filling broke{where} ({re.sub(r'^(?:TypeError|ReferenceError): ', '', rest)[:80]})"
    if re.search(r"not on the page|no search box|the page changed|can't type into", e, re.I):
        return f"its sell form changed ({step + ' ' if step else ''}section not found)"
    if re.search(r"the page doesn't answer|Receiving end|Could not establish", e, re.I):
        return "its page stopped answering"
    if re.search(r"didn't take the job|extension", e, re.I):
        return "the Thrift Chrome extension didn't answer"
    if re.search(r"net::ERR_|ConnectError|Network is unreachable|ConnectTimeout", e, re.I):
        return "the network was down"
    if e.lower().startswith("mismatch: "):
        return f"its form read back other than planned ({e[10:120].strip()})"
    if e.startswith("failures: "):
        return e[len("failures: "):][:160]
    text = f"{step}: {rest}" if step else rest
    return (text[:157] + "…") if len(text) > 160 else text


# ---------------------------------------------------------------- the pauses

def _all(db: DB) -> dict:
    return loads(db.kv_get(HEALTH)) or {}


def _save(db: DB, data: dict) -> None:
    db.kv_set(HEALTH, json.dumps(data))


def state(db: DB, mp: str) -> dict | None:
    """The site's pause, or None when it posts normally."""
    return _all(db).get(mp)


def paused(db: DB, mp: str) -> str | None:
    """Why the site starts no new listing now (its pause's reason), or None."""
    p = state(db, mp)
    return (p.get("reason") or "paused") if p else None


def account_paused(db: DB, mp: str) -> str | None:
    """Paused by what the site SHOWED — a login, CAPTCHA, block or check page — not by failures (take-downs and reads
    still run on a site paused for failures: a sold item must not sell twice)."""
    p = state(db, mp)
    return (p.get("reason") or "paused") if p and p.get("kind") == "account" else None


def waiting(db: DB, mp: str) -> int:
    """The items waiting for this site: queued (or failed before any click) rows on Depop / Vinted; for Poshmark, the
    ready, priced items it hasn't posted."""
    if mp == "poshmark":
        n = 0
        for it in db.conn.execute("SELECT id, renders, status FROM items WHERE status='ready' AND COALESCE(test,0)=0"):
            row = db.listing(it["id"], "poshmark")
            if row is None or row["status"] in ("queued", "dryrun"):
                n += 1
        return n
    return db.conn.execute(
        "SELECT COUNT(*) FROM listings l JOIN items i ON i.id=l.item_id WHERE l.marketplace=? AND COALESCE(i.test,0)=0 "
        "AND (l.status='queued' OR (l.status='failed' AND l.url IS NULL AND COALESCE(l.error,'') NOT LIKE "
        "'unconfirmed publish:%'))", (mp,)).fetchone()[0]


def pause(db: DB, mp: str, reason: str, kind: str = SITE, *, error: str | None = None, page: str | None = None,
          shot: str | Path | None = None, wait: int = COOL, say: bool = True, at: datetime | None = None) -> bool:
    """Pause `mp`: no new listing until a test run passes (or the owner resumes it). ONE private line per site per
    problem — "⛔ Depop paused: its sell form changed (shipping section not found). 6 items waiting — nothing was
    published." — with one picture; an account page also gets the group's plain line (its kind). True when the pause
    is new (a site already paused for the same problem is left as it is)."""
    t = at or now()
    data = _all(db)
    sig = signature(error or reason)
    was = data.get(mp)
    if was and (was.get("owner") or was.get("problem") == sig or kind == "items" and was.get("kind") != "items"):
        return False
    said = list((was or {}).get("said") or [])
    data[mp] = {"kind": kind, "problem": sig, "reason": reason, "since": (was or {}).get("since") or _iso(t),
                "due": None if kind == "owner" else _iso(t + timedelta(seconds=wait)), "wait": wait,
                "said": said, "tests": 0, "owner": kind == "owner", "page": page}
    if say and kind != "owner" and sig not in said:
        n = waiting(db, mp)
        text = (f"⛔ {LABEL.get(mp, mp)} paused: {plain(mp, error or reason, kind)}. "
                f"{n} item{'s' if n != 1 else ''} waiting — nothing was published.")
        if shot and Path(shot).is_file():
            notify.ops_photo(Path(shot), text)
        else:
            notify.say(text)
        data[mp]["said"] = [*said, sig]
    _save(db, data)
    db.log(None, "site_paused", {"mp": mp, "kind": kind, "reason": reason[:300], "problem": sig, "page": page,
                                 "due": data[mp]["due"]})
    return True


def resume(db: DB, mp: str, why: str = "test passed", say: bool = True, force: bool = False) -> bool:
    """The site posts again: its pause lifted, its runs of failures forgotten; "✅ <Site> back to normal — posting
    resumed." once when its pause was said (`force`: the owner's `thrift resume` says it too). True when it was paused."""
    data = _all(db)
    was = data.pop(mp, None)
    if was is None:
        return False
    _save(db, data)
    streak = loads(db.kv_get(STREAK)) or {}
    streak.pop(mp, None)
    db.kv_set(STREAK, json.dumps(streak))
    db.log(None, "site_resumed", {"mp": mp, "why": why, "kind": was.get("kind"), "tests": was.get("tests")})
    if say and (was.get("said") or force):
        n = waiting(db, mp)
        going = f", {n} item{'s' if n != 1 else ''} going up" if n else ""
        notify.say(f"✅ {LABEL.get(mp, mp)} back to normal — posting resumed{going}.")
    return True


def test_due(db: DB, mp: str, at: datetime | None = None) -> str | None:
    """"auto" when the site's cool-down is over, "resume" / "dry" when the owner asked (`thrift resume`), else None."""
    p = state(db, mp)
    if not p:
        return None
    if p.get("request") in ("resume", "dry"):
        return p["request"]
    due = _when(p.get("due"))
    return "auto" if due is not None and not p.get("owner") and (at or now()) >= due else None


def tested(db: DB, mp: str, ok: bool, detail: str | None = None, at: datetime | None = None) -> str:
    """A test run's result: passed → resumed (an owner's `--dry` request: told, still paused); failed → paused again,
    the wait doubled (≤ COOL_MAX), no new line. Returns what happened: resumed | still | dry."""
    t = at or now()
    data = _all(db)
    p = data.get(mp)
    if p is None:
        return "resumed"
    request = p.pop("request", None)
    db.log(None, "site_tested", {"mp": mp, "ok": ok, "request": request, "detail": (detail or "")[:300]})
    if ok and request != "dry":
        resume(db, mp, f"test passed ({request or 'auto'})", force=request == "resume")
        return "resumed"
    if not ok:
        p["wait"] = min(int(p.get("wait") or COOL) * 2, COOL_MAX)
        p["tests"] = int(p.get("tests") or 0) + 1
        p["last_test"] = (detail or "")[:200]
        if not p.get("owner"):
            p["due"] = _iso(t + timedelta(seconds=p["wait"]))
    data[mp] = p
    _save(db, data)
    return "dry" if request == "dry" else "still"


def owner_pause(db: DB, mp: str, why: str = "paused by hand") -> None:
    data = _all(db)
    data[mp] = {**(data.get(mp) or {}), "kind": "owner", "owner": True, "reason": why, "due": None,
                "since": (data.get(mp) or {}).get("since") or _iso(now()), "problem": "owner",
                "said": (data.get(mp) or {}).get("said") or []}
    data[mp].pop("request", None)
    _save(db, data)
    db.log(None, "site_paused", {"mp": mp, "kind": "owner", "reason": why})


def owner_resume(db: DB, mp: str, dry: bool = False) -> bool:
    """`thrift resume <site>`: a test run first (the next item filled, never published), then posting — `dry`: the test
    only, the site stays paused. False when the site wasn't paused."""
    data = _all(db)
    if mp not in data:
        return False
    data[mp]["request"] = "dry" if dry else "resume"
    if not dry:
        data[mp]["owner"] = False
    _save(db, data)
    db.log(None, "site_resume_asked", {"mp": mp, "dry": dry})
    return True


def all_due_now(db: DB) -> None:
    """A new daily window (the owner at the Mac): every pause but hers is tested at once."""
    data = _all(db)
    for p in data.values():
        if not p.get("owner"):
            p["due"] = _iso(now())
    _save(db, data)


# ---------------------------------------------------------------- one failure that isn't about the item

def non_item(db: DB, mp: str, iid: str, error: str | None, kind: str, *, shot: str | Path | None = None,
             counted: bool = True, at: datetime | None = None) -> str:
    """A site or infra failure (nothing was clicked): the item back in line untouched — its attempt given back
    (`counted`: the attempt was counted when the job began) — and behind the others for DEFER_FOR (an infra failure
    is retried at once, after the heal). The same failure as the site's last one pauses the site; the same one on this
    item alone ITEM_AFTER times makes it the item's after all. Returns "requeued" | "paused" | "item"."""
    t = at or now()
    sig = signature(error)
    tries = loads(db.kv_get(TRIES)) or {}
    key = f"{iid}|{mp}"
    mine = tries.get(key) or {}
    mine = {"sig": sig, "n": (mine.get("n", 0) + 1) if mine.get("sig") == sig else 1}
    tries[key] = mine
    db.kv_set(TRIES, json.dumps(tries))
    streak = loads(db.kv_get(STREAK)) or {}
    last = streak.get(mp) or {}
    again = last.get("sig") == sig
    db.log(iid, "post_nonitem", {"mp": mp, "kind": kind, "error": (error or "")[:300], "problem": sig,
                                 "same_as_last": again, "item_tries": mine["n"]})
    if mine["n"] >= ITEM_AFTER and not (again and last.get("item") != iid):
        tries.pop(key, None)
        db.kv_set(TRIES, json.dumps(tries))
        return "item"
    row = db.listing(iid, mp)
    attempts = (row["attempts"] if row else 0) or 0
    db.upsert_listing(iid, mp, status="queued", error=f"waiting ({kind}): {(error or '')[:300]}",
                      attempts=max(0, attempts - 1) if counted else attempts)
    if kind != INFRA:
        defer(db, mp, iid, t)
    streak[mp] = {"sig": sig, "item": iid, "n": (last.get("n", 0) + 1) if again else 1, "at": _iso(t)}
    db.kv_set(STREAK, json.dumps(streak))
    if again:
        pause(db, mp, f"{kind}: {error}", kind, error=error, shot=shot, at=t)
        return "paused"
    return "requeued"


def worked(db: DB, mp: str, iid: str | None = None) -> None:
    """A listing went through on the site: its run of non-item failures ends (and the item's own)."""
    streak = loads(db.kv_get(STREAK)) or {}
    if streak.pop(mp, None) is not None:
        db.kv_set(STREAK, json.dumps(streak))
    if iid:
        tries = loads(db.kv_get(TRIES)) or {}
        if tries.pop(f"{iid}|{mp}", None) is not None:
            db.kv_set(TRIES, json.dumps(tries))


def defer(db: DB, mp: str, iid: str, at: datetime | None = None, seconds: int = DEFER_FOR) -> None:
    data = loads(db.kv_get(DEFER)) or {}
    data.setdefault(mp, {})[iid] = _iso((at or now()) + timedelta(seconds=seconds))
    db.kv_set(DEFER, json.dumps(data))


def deferred(db: DB, mp: str, at: datetime | None = None) -> set[str]:
    """The items put behind the others on `mp` for now."""
    t = at or now()
    data = loads(db.kv_get(DEFER)) or {}
    mine = data.get(mp) or {}
    live = {iid: until for iid, until in mine.items() if (_when(until) or t) > t}
    if live != mine:
        data[mp] = live
        db.kv_set(DEFER, json.dumps(data))
    return set(live)


def behind(db: DB, mp: str, ids: list, key=lambda x: x) -> list:
    """`ids` with the deferred items moved to the end (stable) — they still go when nothing else is waiting."""
    later = deferred(db, mp)
    return [x for x in ids if key(x) not in later] + [x for x in ids if key(x) in later]


def summary(db: DB) -> list[str]:
    """One line per paused site, for `thrift status` / doctor."""
    out = []
    for mp, p in _all(db).items():
        due = _when(p.get("due"))
        nxt = (f"next test {due.astimezone():%H:%M}" if due else ("waits for `thrift resume`" if p.get("owner")
                                                                   else "test pending"))
        if p.get("request"):
            nxt = f"test asked ({p['request']})"
        out.append(f"{LABEL.get(mp, mp)} PAUSED ({p.get('kind')}): {plain(mp, p.get('reason'), p.get('kind'))[:120]} · "
                   f"{nxt}" + (f" · {p.get('tests')} test(s) failed" if p.get("tests") else ""))
    return out
