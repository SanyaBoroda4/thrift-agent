"""WO52 §3 (the owner, 2026-11-01): site changes are found before her items find them — a form check.

Live Oct 27 → Nov 1: Vinted rebuilt its sell form and Depop removed its shipping choice, and nobody knew until her 12
items hit all three sites at once. Now each site worker fills the sell form for a CANARY item and leaves it — once at
night (02:00 local, `formcheck.slots`), after every poster start (a deploy; 2:00 PM after the quiet hours) and after its
browser was reopened.

WO54 §1.6 / §3 (live 2026-11-09 8:37: the "08:30" check ran the canary on Depop and Vinted while 16 real items were being
priced and queued there; at 8:40 both "failed" — "the form wasn't done in 3 min (nothing was submitted)" — and two FALSE
🚨 went out, while the same sites posted the real items fine 8:42–8:47): a form check never competes with real work.
- Never in the quiet hours (`quiet.closing`: nothing new from 8:20 until 2:00 PM); a night's slot the Mac slept through
  is let go after SLOT_LATE (her laptop comes first: no check when she opens it in the morning).
- A site with real items queued or posting is proven by them (`busy`): its check is skipped while they go (event
  `form_check_skipped`, once) — and a real listing live on the site since the check fell due counts as its pass
  (`prove`); so does one live since a failed check (the site is no longer degraded).
- A check that ended without a verdict on the form (`no_verdict`: its job not taken or not done in time, the bridge,
  the page or the browser gone) while the site had real work is no failure — no degraded state, no 🚨 — and is asked
  again (`busy_retry`): proven by the real items meanwhile, else the canary once the site is idle.
- A check asked for by hand (`thrift canary --check`) always fills the canary's form (outside the quiet hours).
The poster's call sites are unchanged: all of this happens in `due()` and the `run_*` functions.

- **The canary** (`make_canary`, `thrift canary --make [<item>]`): the photos and facts of an old sold item copied into
  an item of its own — status `canary`, `test` 1, in a batch of its own (status `canary`), its photos under
  `<var>/canary/<id>/` (the cleanup never sweeps there). It is in no queue (no listing row, never `ready`), never
  carded, never published: only this module fills a form for it, always as a dry run. Its sync marks are dropped
  (`_unsync`): thrift-api never hears of it. Made by itself the first time a check finds none.
- **A check** (`run_poshmark` / `run_cross`): the poster's own dry run — every field filled and read back, then the
  form left (Poshmark: Discard, its Drafts count compared before and after — `Outcome.draft_left`; Depop / Vinted: the
  extension closes the tab, nothing saved — "not checkable" for a draft: neither site shows a draft list).
- **The form's structure** (`structure_of`, the same reading in `post/base.STRUCTURE_JS` for Poshmark and
  `T.structure()` in the extension's dry-run result): every labelled control — label, control type, its hook (test id,
  data-test, vv-name, id, name), required — kept as the last good one under `<var>/formcheck/<site>.json`; a difference
  is ONE private line ("Vinted's form changed: Brand is now a button."), even when the dry run passed.
- **A failed check**: the site `degraded` (kv `formcheck_state`, `thrift status` / doctor); on the second failure in a
  row, or at once when the site has items waiting, `failed()` — the call point for the 🚨 alarm (WO52 §5: `alarm.form_
  check_failed` when that module exists, else one private line an episode). Real items keep trying meanwhile.
- **The evening line**: the results ride on the heartbeat (`beat()` → `cli.beat_info`), thrift-api's 9 PM summary says
  "Sites: Poshmark ✓ · Depop ✓ · Vinted ✓ — form checks today: Poshmark 2:00 ✓, …" (`api/thrift_api/watch.sites_line`).
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from thrift_agent import notify, quiet
from thrift_agent.db import DB, loads, new_id, now as db_now

STATE = "formcheck_state"       # kv: {site: {"ok", "at", "why", "fails", "degraded", "error", "last_ok_at", "slot",
#                                  "started_at", "draft", "changed", "alarmed", "skipped", "proven_by"}}
REQUESTS = "formcheck_request"  # kv: {site: why} — a check asked for (the poster's start, a browser reopened)
REQUESTS_AT = "formcheck_request_at"   # kv: {site: iso} — when it was asked (WO54: a real listing after it proves it)
CANARY = "canary_item"          # kv: the canary item's id
SAID = "formcheck_said"         # kv: {site: diff signature} — a structure change said once
SLOTS = ("02:00",)              # WO54 §3: once at night (WO52's 08:30 and 14:30: the 8:37 check ran beside real work)
SLOT_LATE = timedelta(hours=4)  # WO54 §4: a night's slot the Mac slept through is let go after this — no check when she
#                                 opens her laptop in the morning (the poster's 2:00 PM start checks the sites anyway)
PROVEN_BEFORE = timedelta(hours=1)   # WO54: a real listing live this long before a slot proves the site for that slot
HAND = "asked by hand"          # `thrift canary --check`: the canary's form always (never skipped for real work)
LABEL = {"poshmark": "Poshmark", "depop": "Depop", "vinted": "Vinted"}
SITES = ("poshmark", "depop", "vinted")
# WO54: the check ended before it could judge the form — its job not taken or not done in time, the bridge or the page
# gone (health's "infra" kind says the rest: the browser closed, the network …)
NO_VERDICT = re.compile(r"wasn't done in|didn't take the job|went away before|bridge|cancelled|called off|"
                        r"the page doesn't answer|Receiving end does not exist|Could not establish connection", re.I)
SLOT_WHY = re.compile(r"the (\d\d:\d\d) check")    # the reason due() gives for a slot: "the 02:00 check"


def enabled(s) -> bool:
    """On a prod Mac whose settings turn it on (config/settings.yaml); off in a test unless its settings say so."""
    return bool(s.get("formcheck.enabled", False)) and (s.is_prod or bool(s.get("formcheck.anywhere", False)))


def _tz(s) -> ZoneInfo:
    return ZoneInfo(str(s.get("schedule.timezone", "America/New_York") or "America/New_York"))


def _when(v) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def _state(db: DB) -> dict:
    return loads(db.kv_get(STATE)) or {}


def state(db: DB, site: str) -> dict:
    return _state(db).get(site) or {}


def _save(db: DB, data: dict) -> None:
    db.kv_set(STATE, json.dumps(data))


# ---------------------------------------------------------------- when

def _hm(v) -> tuple[int, int]:
    """ "02:00" → (2, 0); an unquoted YAML 2:00 arrives as the int 120 (minutes, YAML 1.1)."""
    if isinstance(v, int) and not isinstance(v, bool):
        return v // 60 % 24, v % 60
    h, m = (int(x) for x in str(v).strip().split(":")[:2])
    return h, m


def last_slot(s, at: datetime | None = None) -> datetime:
    """The most recent check time (02:00 local — WO54; the zone's own clock, DST included: on the spring-forward night
    02:00 doesn't exist and is 03:00 EDT) at or before `at`."""
    tz = _tz(s)
    local = (at or datetime.now(timezone.utc)).astimezone(tz)
    slots = []
    for days in (0, 1):
        day = (local - timedelta(days=days)).date()
        for hm in s.get("formcheck.slots", list(SLOTS)) or SLOTS:
            h, m = _hm(hm)
            slots.append(datetime.combine(day, dtime(h, m), tzinfo=tz))
    return max(t for t in slots if t.astimezone(timezone.utc) <= local.astimezone(timezone.utc))


def request(db: DB, sites, why: str, at: datetime | None = None) -> None:
    """A check asked for on `sites` (the poster's start, a browser reopened) — made on the site worker's next turn
    (WO54: unless real work proves the site first). When it was asked is kept: a real listing after it proves it."""
    data = loads(db.kv_get(REQUESTS)) or {}
    when = loads(db.kv_get(REQUESTS_AT)) or {}
    for site in ([sites] if isinstance(sites, str) else sites):
        data[site] = why
        when[site] = _iso(at or datetime.now(timezone.utc))
    db.kv_set(REQUESTS, json.dumps(data))
    db.kv_set(REQUESTS_AT, json.dumps(when))


def _reason(s, db: DB, site: str, t: datetime) -> tuple[str | None, datetime | None]:
    """(why a check is due, since when a real listing proves it): the request (since it was asked), else the night's
    slot not yet checked (since an hour before it, or the last check after that) — one missed by more than SLOT_LATE
    is let go — else (None, None)."""
    asked = (loads(db.kv_get(REQUESTS)) or {}).get(site)
    if asked:
        return asked, _when((loads(db.kv_get(REQUESTS_AT)) or {}).get(site)) or t
    slot = last_slot(s, t)
    started = _when(state(db, site).get("started_at"))
    if started is not None and started >= slot:
        return None, None
    if t - slot > SLOT_LATE:
        return None, None                             # WO54 §4: the night's check missed (the Mac asleep): let go
    since = slot - PROVEN_BEFORE
    return f"the {slot:%H:%M} check", (max(since, started) if started is not None else since)


def due(s, db: DB, site: str, at: datetime | None = None) -> str | None:
    """Why a check of `site` is due now — the reason asked for, or the night's slot ("02:00") not yet checked — else
    None. WO54 (live 2026-11-09 8:37, two false 🚨): never in the quiet hours (from 8:20 until 2:00 PM); a real
    listing live on the site since the check fell due proves it — recorded as its pass, nothing filled (`prove`); a
    site with real items queued or posting is proven by them — the check is skipped while they go (`busy`, the event
    `form_check_skipped` once). A check asked for by hand always runs. Called on every worker turn: a few cheap reads
    (and the writes above when they happen); the poster's call sites are unchanged."""
    if not enabled(s):
        return None
    t = at or datetime.now(timezone.utc)
    if quiet.closing(t, db):
        return None                                   # WO54 §3: no checks in the quiet hours, nothing new from 8:20
    _lift_if_proven(s, db, site, t)
    why, since = _reason(s, db, site, t)
    if why is None or why == HAND:
        return why
    proof = real_listing_since(db, site, since)
    if proof is not None:
        prove(s, db, site, why, proof, t)
        return None
    work = busy(s, db, site)
    if work is not None:
        _skip(db, site, why, work)
        return None
    return why


def _taken(db: DB, site: str, why: str, at: datetime) -> None:
    data = loads(db.kv_get(REQUESTS)) or {}
    if data.pop(site, None) is not None:
        db.kv_set(REQUESTS, json.dumps(data))
    when = loads(db.kv_get(REQUESTS_AT)) or {}
    if when.pop(site, None) is not None:
        db.kv_set(REQUESTS_AT, json.dumps(when))
    st = _state(db)
    st.setdefault(site, {})["started_at"] = _iso(at)
    st[site]["why"] = why
    st[site]["skipped"] = None
    _save(db, st)


# ---------------------------------------------------------------- WO54: real work proves a site

def real_listing_since(db: DB, site: str, since: datetime | None):
    """The newest real listing (never a test share's, never the canary's) that went live on `site` at or after `since`
    — its row (item_id, posted_at) — else None."""
    if since is None:
        return None
    return db.conn.execute(
        "SELECT l.item_id, l.posted_at FROM listings l JOIN items i ON i.id=l.item_id WHERE l.marketplace=? AND "
        "l.status IN ('posted', 'sold', 'delisted') AND l.url IS NOT NULL AND l.posted_at >= ? AND COALESCE(i.test, 0)=0 "
        "ORDER BY l.posted_at DESC LIMIT 1", (site, _iso(since))).fetchone()


def _queued(s, db: DB, site: str, tonight: bool = False) -> list[str]:
    """The real items the site's worker takes now: Depop's / Vinted's queue (crosslist.pending: not a test item, not
    one waiting for her brand); Poshmark's ready, priced items it hasn't posted. `tonight` (outside the listing hours,
    WO53 rule 6): only items live on another site."""
    from thrift_agent import approve, crosslist
    if site != "poshmark":
        return [iid for iid, _ in crosslist.pending(s, db, [site], live_elsewhere_only=tonight)]
    out = []
    for it in db.items("ready"):
        if ("test" in it.keys() and it["test"]) or not it["owner_price"] or approve.needs_brand(it):
            continue
        row = db.listing(it["id"], "poshmark")
        if row is not None and row["status"] not in ("queued", "dryrun"):
            continue
        if tonight and not crosslist.live_elsewhere(db, it["id"], "poshmark"):
            continue
        out.append(it["id"])
    return out


def busy(s, db: DB, site: str) -> str | None:
    """WO54: the real work that proves the site right now, in words — a real listing being posted there, or real items
    in its queue that its worker takes now — else None. A paused site's queue proves nothing (it posts none), nor one
    at its daily cap or under the owner's PAUSE: their form check runs."""
    from thrift_agent import crosslist, health
    if health.paused(db, site):
        return None
    n = db.conn.execute("SELECT COUNT(*) FROM listings l JOIN items i ON i.id=l.item_id WHERE l.marketplace=? AND "
                        "l.status='posting' AND COALESCE(i.test, 0)=0", (site,)).fetchone()[0]
    if n:
        return f"{n} real listing{'s' if n != 1 else ''} being posted there"
    try:
        from thrift_agent.scheduler import can_post, outside_hours
        if crosslist.capped(s, db, site):
            return None
        ok, why_not = can_post(s, 0, 0)
    except (KeyError, TypeError, ValueError, OSError):   # settings without a schedule (a test), the control folder away
        ok, why_not, outside_hours = True, None, (lambda _why: False)
    if not ok and not outside_hours(why_not):
        return None                                   # the owner's PAUSE: nothing posts
    try:
        n = len(_queued(s, db, site, tonight=not ok))
    except (KeyError, TypeError, ValueError):        # settings without a schedule (a test's): no queue to read
        n = 0
    return f"{n} real item{'s' if n != 1 else ''} queued there" if n else None


def _skip(db: DB, site: str, why: str, work: str) -> None:
    """The check skipped while real work proves the site — the event once per check (it is asked on every turn)."""
    data = _state(db)
    st = data.setdefault(site, {})
    if st.get("skipped") == why:
        return
    st["skipped"] = why
    _save(db, data)
    db.log(None, "form_check_skipped", {"site": site, "why": work, "check": why})


def prove(s, db: DB, site: str, why: str, proof, at: datetime | None = None) -> dict:
    """WO54: a real listing live on the site since the check fell due — its form works: the check counts as passed
    (recorded like one, `proven_by` its item; nothing is filled), the request or the slot taken."""
    from thrift_agent.post.base import Outcome
    t = at or datetime.now(timezone.utc)
    _taken(db, site, why, t)
    return record(s, db, site, Outcome("dryrun"), why, at=t, proven_by=proof["item_id"])


def _lift_if_proven(s, db: DB, site: str, t: datetime) -> None:
    """A degraded site (its last check failed) with a real listing live there since that check: its form works — a
    pass. A failure that was no verdict at all (the canary's job timed out) leaves the day's failed slots too (live
    2026-11-09: Depop's and Vinted's 8:37 checks, "the form wasn't done in 3 min", while their real items went up at
    8:42–8:47)."""
    st = state(db, site)
    if not st.get("degraded"):
        return
    proof = real_listing_since(db, site, _when(st.get("started_at")) or _when(st.get("at")))
    if proof is None:
        return
    if NO_VERDICT.search(str(st.get("error") or "")):
        data = _state(db)
        today = (data.get(site) or {}).get("today")
        if isinstance(today, dict) and st.get("slot") in (today.get("failed") or []):
            today["failed"].remove(st.get("slot"))
            _save(db, data)
    from thrift_agent.post.base import Outcome
    record(s, db, site, Outcome("dryrun"), "a real listing went live", at=t, proven_by=proof["item_id"])


def no_verdict(out) -> bool:
    """The check ended before it could judge the form: its job not taken or not done in time, called off, the bridge,
    the page or the browser gone (health's infra kind) — never a form that read back wrong."""
    if out is None:
        return True
    status = getattr(out, "status", None)
    if status == "cancelled":
        return True
    if status != "failed" or getattr(out, "draft_left", None):
        return False
    error = str(getattr(out, "error", None) or "")
    if NO_VERDICT.search(error):
        return True
    from thrift_agent import health
    return health.kind_of(error) == health.INFRA


def _moved(db: DB, site: str, since: datetime) -> int:
    """Real listing rows of the site that changed since `since` (queued, posted, failed): work came during the check."""
    return db.conn.execute("SELECT COUNT(*) FROM listings l JOIN items i ON i.id=l.item_id WHERE l.marketplace=? AND "
                           "l.updated_at >= ? AND COALESCE(i.test, 0)=0", (site, _iso(since))).fetchone()[0]


def was_busy(s, db: DB, site: str, out, since: datetime) -> str | None:
    """WO54: why a check that ended without a verdict met a busy site — real work there now, or real listings that
    moved there while it ran — else None (then it counts as before: a failure)."""
    if not no_verdict(out):
        return None
    work = busy(s, db, site)
    if work is not None:
        return work
    n = _moved(db, site, since)
    return f"{n} real listing{'s' if n != 1 else ''} moved there during the check" if n else None


def busy_retry(db: DB, site: str, why: str, out, work: str, at: datetime | None = None) -> dict:
    """WO54: a check that couldn't judge the form while the site had real work is no failure — no degraded state, no
    🚨 — and is asked again: the real items prove the site meanwhile (`prove`), else the canary runs once it is idle.
    One asked for by hand isn't asked again by itself (it is never skipped for real work: it would only meet it).
    `at`: when the check began — a real listing since then proves the site."""
    if why != HAND:
        request(db, site, why, at=at)
    data = _state(db)
    st = data.setdefault(site, {})
    st["skipped"] = why                               # said here: the next turns' skip says nothing more
    _save(db, data)
    db.log(None, "form_check_skipped", {"site": site, "why": f"busy: {work}", "check": why,
                                        "error": (getattr(out, "error", None) or "")[:300] or None})
    return st


# ---------------------------------------------------------------- the canary

def var_dir(s) -> Path:
    return Path(s.path("db")).parent


def canary(db: DB):
    """The canary item's row, or None."""
    iid = db.kv_get(CANARY)
    it = db.item(iid) if iid else None
    return it if it is not None and it["status"] == "canary" else None


def _photos_of(it) -> list[Path]:
    d = Path(it["dir"])
    return sorted((d / "photos").glob("*.jpg")) if d.is_dir() else []


def pick_source(db: DB, prefer: str | None = None):
    """The item the canary is copied from: `prefer`, else an old SOLD item whose photos are still here, else the
    oldest live one with photos (a sold item's photos go once it shipped, WO47)."""
    if prefer:
        it = db.item(prefer)
        return it if it is not None and _photos_of(it) and (loads(it["renders"]) or {}).get("poshmark") else None
    for status in ("sold", "delisted", "posted"):
        for r in db.conn.execute(
                "SELECT DISTINCT i.* FROM items i JOIN listings l ON l.item_id=i.id WHERE l.status=? "
                "AND i.status <> 'canary' AND COALESCE(i.test,0)=0 ORDER BY i.created_at", (status,)).fetchall():
            if _photos_of(r) and (loads(r["renders"]) or {}).get("poshmark") and (r["owner_price"] or 0):
                return r
    return None


def _unsync(db: DB, iid: str) -> None:
    """thrift-api never hears of the canary (its sync mark dropped: the item is in no listing, so that is all)."""
    db.conn.execute("DELETE FROM api_dirty WHERE kind='item' AND key=?", (iid,))


def make_canary(s, db: DB, source: str | None = None) -> str | None:
    """The canary made from `source` (else pick_source): a new item, status `canary`, test 1, in its own batch, its
    photos copied under <var>/canary/<id>/, the Poshmark render's photo paths and SKU its own. Returns its id."""
    src = pick_source(db, source)
    if src is None:
        return None
    old = canary(db)
    iid = f"i_canary_{new_id('x').rsplit('_', 1)[-1]}"
    dest = var_dir(s) / "canary" / iid
    (dest / "photos").mkdir(parents=True, exist_ok=True)
    sdir = Path(src["dir"])
    for p in _photos_of(src):
        shutil.copy2(p, dest / "photos" / p.name)
    if (sdir / "cover.jpg").is_file():
        shutil.copy2(sdir / "cover.jpg", dest / "cover.jpg")
    renders = loads(src["renders"]) or {}

    def moved(path: str) -> str:
        p = Path(path)
        for cand in (dest / "photos" / p.name, dest / p.name):
            if cand.is_file():
                return str(cand)
        return path
    for r in renders.values():
        if isinstance(r, dict):
            r["photos"] = [moved(p) for p in r.get("photos") or []]
            r["sku"] = iid
    bid = "b_canary"
    with db.conn:
        db.conn.execute("INSERT OR IGNORE INTO batches (id, src_dir, status, n_photos, created_at, updated_at, test) "
                        "VALUES (?,?,?,?,?,?,1)", (bid, "canary:", "canary", 0, db_now(), db_now()))
        db.conn.execute(
            "INSERT INTO items (id, batch_id, seq, status, dir, note, facts, price, renders, gate, created_at, "
            "updated_at, owner_price, test) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (iid, bid, 1, "canary", str(dest), f"canary — a copy of {src['id']} (WO52 §3, never published)",
             src["facts"], src["price"], json.dumps(renders), src["gate"], db_now(), db_now(),
             src["owner_price"] or 0))
        if old is not None:
            db.conn.execute("UPDATE items SET status='retired_canary' WHERE id=?", (old["id"],))
            _unsync(db, old["id"])
        db.kv_set(CANARY, iid)
        _unsync(db, iid)
    db.log(iid, "canary_made", {"from": src["id"], "photos": len(_photos_of(src))})
    return iid


def ensure_canary(s, db: DB):
    it = canary(db)
    if it is None and make_canary(s, db):
        it = canary(db)
    return it


# ---------------------------------------------------------------- the structure

def _norm(t) -> str:
    return re.sub(r"\s+", " ", str(t or "")).strip().strip("*:").strip().lower()


def structure_of(fields) -> dict:
    """{label: {"kind", "key", "required"}} from the page's list of controls (unlabelled ones by their hook)."""
    out = {}
    for f in fields or []:
        if not isinstance(f, dict):
            continue
        name = _norm(f.get("label")) or (f"[{f.get('key')}]" if f.get("key") else "")
        if not name or name in out:
            continue
        out[name] = {"kind": str(f.get("kind") or ""), "key": str(f.get("key") or ""),
                     "required": bool(f.get("required"))}
    return out


KIND_WORDS = {"button": "a button", "combobox": "a dropdown", "select": "a dropdown", "textarea": "a text box",
              "input:text": "a text box", "input:search": "a search box", "radio": "radio buttons",
              "checkbox": "a checkbox", "input:file": "a file input"}


def _kind(k: str) -> str:
    return KIND_WORDS.get(k, KIND_WORDS.get(k.split(":")[0], k or "something else"))


def diff(old: dict, new: dict) -> list[str]:
    """What changed between two structures, in plain words ("Brand is now a button")."""
    if not old or not new:
        return []
    out = []
    for name, o in old.items():
        n = new.get(name)
        label = name.strip("[]").capitalize()
        if n is None:
            out.append(f"{label} is gone")
            continue
        if n["kind"] != o["kind"]:
            out.append(f"{label} is now {_kind(n['kind'])}")
        elif n["key"] != o["key"] and o["key"]:
            out.append(f"{label}'s hook changed ({o['key']} → {n['key'] or 'none'})")
        if n["required"] != o["required"]:
            out.append(f"{label} is {'now required' if n['required'] else 'no longer required'}")
    for name in new:
        if name not in old:
            out.append(f"new field {name.strip('[]').capitalize()}")
    return out


def _baseline(s, site: str) -> Path:
    return var_dir(s) / "formcheck" / f"{site}.json"


def compare(s, db: DB, site: str, fields, passed: bool) -> list[str]:
    """The structure against the last good one: a difference said ONCE (even when the dry run passed); a passing
    check's structure becomes the new baseline. Returns the differences."""
    new = structure_of(fields)
    if not new:
        return []
    path = _baseline(s, site)
    try:
        old = json.loads(path.read_text(encoding="utf-8")).get("fields") or {}
    except (OSError, ValueError):
        old = {}
    changes = diff(old, new)
    if changes:
        sig = "|".join(sorted(changes))
        said = loads(db.kv_get(SAID)) or {}
        if said.get(site) != sig:
            said[site] = sig
            db.kv_set(SAID, json.dumps(said))
            shown = "; ".join(changes[:6]) + (f" (+{len(changes) - 6} more)" if len(changes) > 6 else "")
            notify.say(f"🔎 {LABEL.get(site, site)}'s form changed: {shown}."
                       + ("" if passed else " Its form check failed too."))
        db.log(None, "form_changed", {"site": site, "changes": changes, "passed": passed})
    if passed or not old:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": db_now(), "fields": new}, indent=1, ensure_ascii=False), encoding="utf-8")
    return changes


# ---------------------------------------------------------------- the result

def _module(name: str):
    """A sibling module of WO52 (§4 `jobs`, §5 `alarm`) when it is there, else None — one place, so the tests can say."""
    try:
        import importlib
        return importlib.import_module(f"thrift_agent.{name}")
    except ImportError:
        return None


def _job_ok(db: DB, error: str | None = None) -> None:
    """WO52 §4's job health, when that module is there (else kv `job_ok:form_check`)."""
    jobs = _module("jobs")
    if jobs is None or not hasattr(jobs, "ok"):
        if error is None:
            db.kv_set("job_ok:form_check", db_now())
        return
    if error is None:
        jobs.ok(db, "form_check")
    elif hasattr(jobs, "failed"):
        jobs.failed(db, "form_check", error)


def waiting(db: DB, site: str) -> int:
    """Real items waiting for the site (its queued rows; Poshmark: priced items it hasn't posted)."""
    try:
        from thrift_agent import health
        return int(health.waiting(db, site))
    except Exception:  # noqa: BLE001 — a count for the alarm's words only
        return 0


def failed(db: DB, site: str, error: str | None, shot: str | None = None, n_waiting: int = 0) -> None:
    """THE CALL POINT for WO52 §5's 🚨 alarm: a form check failed twice in a row, or once with items waiting.
    `alarm.form_check_failed(db, site, error, shot, n_waiting)` when that module exists; else one private line an
    episode (kv `formcheck_state[site].alarmed`)."""
    alarm = _module("alarm")
    if alarm is not None and hasattr(alarm, "form_check_failed"):
        alarm.form_check_failed(db, site, error, shot, n_waiting)
        return
    data = _state(db)
    st = data.setdefault(site, {})
    if st.get("alarmed"):
        return
    st["alarmed"] = True
    _save(db, data)
    notify.say(f"🚨 {LABEL.get(site, site)}'s form check failed ({st.get('fails', 1)}× in a row"
               + (f", {n_waiting} item{'s' if n_waiting != 1 else ''} waiting" if n_waiting else "")
               + f"): {(error or '')[:200]}" + (f"\nEvidence: {shot}" if shot else ""))


def record(s, db: DB, site: str, out, why: str, structure=None, at: datetime | None = None,
           proven_by: str | None = None) -> dict:
    """A check's outcome: passed (a dry run, no draft left) → ok, degraded lifted; else degraded, `failed()` on the
    second failure in a row or at once with items waiting. WO54: `proven_by` — no form filled: a real listing of that
    item went live there (a pass of the day too: the 9 PM line never says "no form check ran" for a site its real
    items proved). Returns the site's state."""
    t = at or datetime.now(timezone.utc)
    status = getattr(out, "status", None) if out is not None else None
    draft = getattr(out, "draft_left", None) if out is not None else None
    ok = status == "dryrun" and not draft
    error = None if ok else (draft or getattr(out, "error", None) or f"the dry run ended '{status}'")
    changed = compare(s, db, site, structure, ok) if structure else []
    data = _state(db)
    st = data.setdefault(site, {})
    was_degraded = st.get("degraded")
    # WO54: the night's check is named by its slot; one asked for (the 2:00 PM start, a reopened browser) by its own
    # time — with one slot at 02:00, the afternoon's check would have read "2:00" too
    named = SLOT_WHY.fullmatch(why or "")
    st.update(at=_iso(t), ok=ok, why=why, slot=named.group(1) if named else f"{t.astimezone(_tz(s)):%H:%M}",
              error=(error or "")[:300] or None, draft="not checkable" if site != "poshmark" else (draft or "none left"),
              changed=changed or None, structure_seen=bool(structure), proven_by=proven_by, skipped=None)
    day = f"{t.astimezone(_tz(s)):%Y-%m-%d}"
    if (st.get("today") or {}).get("day") != day:
        st["today"] = {"day": day, "passed": [], "failed": []}
    bucket = st["today"]["passed" if ok else "failed"]
    if st["slot"] not in bucket:
        bucket.append(st["slot"])                     # the 9 PM line names every slot checked today
    if ok:
        st.update(fails=0, degraded=False, last_ok_at=_iso(t), alarmed=False)
    else:
        st["fails"] = int(st.get("fails") or 0) + 1
        st["degraded"] = True
    _save(db, data)
    db.log(None, "form_check", {"site": site, "ok": ok, "why": why, "error": error,
                                "shot": getattr(out, "screenshot", None), "changed": changed or None,
                                **({"proven_by": proven_by} if proven_by else {})})
    if ok:
        _job_ok(db)
        if was_degraded:
            notify.say(f"✅ {LABEL.get(site, site)}'s form works again — a real listing went live there." if proven_by
                       else f"✅ {LABEL.get(site, site)}'s form check passed again.")
    else:
        _job_ok(db, error)
        n = waiting(db, site)
        if st["fails"] >= 2 or n > 0:
            failed(db, site, error, getattr(out, "screenshot", None), n)
    return st


def degraded(db: DB, site: str) -> bool:
    return bool(state(db, site).get("degraded"))


def summary(db: DB) -> list[str]:
    """One line per site for `thrift status` / doctor."""
    out = []
    for site in SITES:
        st = state(db, site)
        if not st:
            continue
        at = _when(st.get("at"))
        skipped = " — skipped while real items post" if st.get("skipped") else ""    # WO54
        if at is None:
            out.append(f"{LABEL[site]} form check: not run yet{skipped}")
            continue
        ok = "✓ proven by a real listing" if st.get("proven_by") else "✓ passed"
        out.append(f"{LABEL[site]} form check {at.astimezone():%a %H:%M}: " + (
            ok if st.get("ok") else f"✗ DEGRADED ({st.get('fails')}×): {st.get('error') or ''}"[:160]) + skipped)
    return out


def beat(db: DB) -> dict:
    """What the heartbeat carries for thrift-api's 9 PM line: per site ok / at / slot / degraded."""
    return {site: {k: st.get(k) for k in ("ok", "at", "slot", "degraded", "fails", "today")}
            for site, st in _state(db).items() if site in SITES}


# ---------------------------------------------------------------- running it (from the site workers)

def _render(it):
    from thrift_agent.schema import Render
    return Render.model_validate((loads(it["renders"]) or {})["poshmark"])


async def run_poshmark(s, db: DB, poster, ctx, why: str, at: datetime | None = None) -> dict | None:
    """Poshmark's check: the canary's form filled, read back, its structure read, left through Discard with the
    Drafts count compared. None when there is no canary to check with."""
    it = ensure_canary(s, db)
    t = at or datetime.now(timezone.utc)
    _taken(db, "poshmark", why, t)
    if it is None:
        _no_canary(db)
        return None
    poster.capture_structure = True
    try:
        out = await poster.post(ctx, _render(it), "publish", True, s.path("failed") / "shots", stage="form")
    except Exception as e:  # noqa: BLE001 — AccountBlocked included: the check failed, the site's own stop follows
        from thrift_agent.post.base import Outcome
        out = Outcome("failed", error=f"{type(e).__name__}: {e}")
    finally:
        poster.capture_structure = False
    _unsync(db, it["id"])
    if (work := was_busy(s, db, "poshmark", out, t)) is not None:
        return busy_retry(db, "poshmark", why, out, work, at=t)      # WO54: no verdict beside real work: no failure
    return record(s, db, "poshmark", out, why, getattr(poster, "last_structure", None), at=t)


async def run_cross(s, db: DB, ps: dict, site: str, why: str, at: datetime | None = None) -> dict | None:
    """Depop's / Vinted's check: the canary through the extension's dry run (the tab closed, nothing saved)."""
    it = ensure_canary(s, db)
    t = at or datetime.now(timezone.utc)
    _taken(db, site, why, t)
    if it is None:
        _no_canary(db)
        return None
    from thrift_agent.post import runner
    poster = ps[site]
    poster.last_structure = None
    try:
        out = await runner.run_cross(s, db, ps, None, it["id"], site, dry=True, request=True, quiet=True)
    except Exception as e:  # noqa: BLE001
        from thrift_agent.post.base import Outcome
        out = Outcome("failed", error=f"{type(e).__name__}: {e}")
    _unsync(db, it["id"])
    # WO54 (live 2026-11-09 8:40: "the form wasn't done in 3 min" on Depop and Vinted while their real items queued —
    # two false 🚨): a check that couldn't judge the form while the site had real work is no failure, asked again
    if (work := was_busy(s, db, site, out, t)) is not None:
        return busy_retry(db, site, why, out, work, at=t)
    return record(s, db, site, out, why, getattr(poster, "last_structure", None), at=t)


def _no_canary(db: DB) -> None:
    if not db.kv_get("canary_missing_said"):
        db.kv_set("canary_missing_said", db_now())
        notify.say("🔎 The form check has no canary item (no sold or live item with its photos here) — "
                   "make one with `thrift canary --make <item>`.")
