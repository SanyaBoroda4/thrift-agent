"""WO52 §3 (the owner, 2026-11-01): site changes are found before her items find them — a twice-daily form check.

Live Oct 27 → Nov 1: Vinted rebuilt its sell form and Depop removed its shipping choice, and nobody knew until her 12
items hit all three sites at once. Now each site worker fills the sell form for a CANARY item and leaves it — every day
at 08:30 and 14:30 local (`formcheck.slots`), after every poster start (a deploy) and after its browser was reopened.

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
  "Sites: Poshmark ✓ Depop ✓ Vinted ✓ — form checks 8:30, 2:30 passed." (`api/thrift_api/watch.sites_line`).
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from thrift_agent import notify
from thrift_agent.db import DB, loads, new_id, now as db_now

STATE = "formcheck_state"       # kv: {site: {"ok", "at", "why", "fails", "degraded", "error", "last_ok_at", "slot",
#                                  "started_at", "draft", "changed", "alarmed"}}
REQUESTS = "formcheck_request"  # kv: {site: why} — a check asked for (the poster's start, a browser reopened)
CANARY = "canary_item"          # kv: the canary item's id
SAID = "formcheck_said"         # kv: {site: diff signature} — a structure change said once
SLOTS = ("08:30", "14:30")
LABEL = {"poshmark": "Poshmark", "depop": "Depop", "vinted": "Vinted"}
SITES = ("poshmark", "depop", "vinted")


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

def last_slot(s, at: datetime | None = None) -> datetime:
    """The most recent check time (08:30 / 14:30 local, the zone's own clock — DST included) at or before `at`."""
    tz = _tz(s)
    local = (at or datetime.now(timezone.utc)).astimezone(tz)
    slots = []
    for days in (0, 1):
        day = (local - timedelta(days=days)).date()
        for hm in s.get("formcheck.slots", list(SLOTS)) or SLOTS:
            h, m = (int(x) for x in str(hm).split(":"))
            slots.append(datetime.combine(day, dtime(h, m), tzinfo=tz))
    return max(t for t in slots if t <= local)


def request(db: DB, sites, why: str) -> None:
    """A check asked for on `sites` (the poster's start, a browser reopened) — made on the site worker's next turn."""
    data = loads(db.kv_get(REQUESTS)) or {}
    for site in ([sites] if isinstance(sites, str) else sites):
        data[site] = why
    db.kv_set(REQUESTS, json.dumps(data))


def due(s, db: DB, site: str, at: datetime | None = None) -> str | None:
    """Why a check of `site` is due now — the reason asked for, or the slot ("08:30") not yet checked — else None."""
    if not enabled(s):
        return None
    asked = (loads(db.kv_get(REQUESTS)) or {}).get(site)
    if asked:
        return asked
    slot = last_slot(s, at)
    started = _when(state(db, site).get("started_at"))
    if started is None or started < slot:
        return f"the {slot:%H:%M} check"
    return None


def _taken(db: DB, site: str, why: str, at: datetime) -> None:
    data = loads(db.kv_get(REQUESTS)) or {}
    if data.pop(site, None) is not None:
        db.kv_set(REQUESTS, json.dumps(data))
    st = _state(db)
    st.setdefault(site, {})["started_at"] = _iso(at)
    st[site]["why"] = why
    _save(db, st)


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
    elif hasattr(jobs, "fail"):
        jobs.fail(db, "form_check", error)


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


def record(s, db: DB, site: str, out, why: str, structure=None, at: datetime | None = None) -> dict:
    """A check's outcome: passed (a dry run, no draft left) → ok, degraded lifted; else degraded, `failed()` on the
    second failure in a row or at once with items waiting. Returns the site's state."""
    t = at or datetime.now(timezone.utc)
    status = getattr(out, "status", None) if out is not None else None
    draft = getattr(out, "draft_left", None) if out is not None else None
    ok = status == "dryrun" and not draft
    error = None if ok else (draft or getattr(out, "error", None) or f"the dry run ended '{status}'")
    changed = compare(s, db, site, structure, ok) if structure else []
    data = _state(db)
    st = data.setdefault(site, {})
    was_degraded = st.get("degraded")
    st.update(at=_iso(t), ok=ok, why=why, slot=f"{last_slot(s, t).astimezone(_tz(s)):%H:%M}",
              error=(error or "")[:300] or None, draft="not checkable" if site != "poshmark" else (draft or "none left"),
              changed=changed or None, structure_seen=bool(structure))
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
                                "shot": getattr(out, "screenshot", None), "changed": changed or None})
    if ok:
        _job_ok(db)
        if was_degraded:
            notify.say(f"✅ {LABEL.get(site, site)}'s form check passed again.")
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
        when = f"{at.astimezone():%a %H:%M}" if at else "?"
        out.append(f"{LABEL[site]} form check {when}: " + ("✓ passed" if st.get("ok") else
                                                          f"✗ DEGRADED ({st.get('fails')}×): {st.get('error') or ''}"[:160]))
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
    return record(s, db, site, out, why, getattr(poster, "last_structure", None), at=t)


def _no_canary(db: DB) -> None:
    if not db.kv_get("canary_missing_said"):
        db.kv_set("canary_missing_said", db_now())
        notify.say("🔎 The twice-daily form check has no canary item (no sold or live item with its photos here) — "
                   "make one with `thrift canary --make <item>`.")
