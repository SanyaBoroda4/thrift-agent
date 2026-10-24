"""Sales tracking, the Mac's side (WO33 Part G): the link to thrift-api (the Azure Function that reads the seller's
marketplace emails), never the source of truth for posting — SQLite stays that; Postgres is the sales'.

- `THRIFT_API_URL`, `THRIFT_API_KEY` (the `mac` function key) in .env, added by the owner. Without them everything
  here is off, posting works as before, and the ops chat hears it once a day.
- Changes: every insert or update of an item or a listing marks it in `api_dirty` (SQLite triggers, db.py) and
  `push` sends their current state to POST /sync — in order, whatever was missed while the Mac was offline (the rows
  wait). Other calls (a take-down's result, the owner's 'shipped' reply) go through `api_outbox`, replayed in order.
- `tick` (the worker's loop): the push and the outbox every minute, the heartbeat (source `mac`) every 15 minutes and
  on every wake.
- Take-downs (Part D): `fetch_tasks` takes the pending ones for the poster's sites (GET /tasks leases them for 10
  minutes) — one call for all of them, at most every 2 minutes (WO35: each worker had asked on each idle turn, ~5,600
  calls on 2026-10-09, and the API's log hit its 0.1 GB daily cap); each site's worker runs its own before any new
  listing (`run_takedown`): the item's other queued rows
  are skipped (sold), the reversible action is done (Poshmark Not for Sale, Depop Mark as sold, Vinted Hide — never a
  delete), and the result goes back to POST /tasks/{id}. A site whose control isn't recorded yet (UNVERIFIED) is
  never tried: the API is told at once, and the group asks the owner to mark it sold there."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

from pathlib import Path

import httpx

from thrift_agent import notify
from thrift_agent.db import DB, loads, now

API_URL_ENV, API_KEY_ENV = "THRIFT_API_URL", "THRIFT_API_KEY"
OFF_SAID = "api_off_said"            # kv: the day "sales tracking is off" was said
LAST_PUSH = "api_last_push"          # kv: when the changes and the outbox were last sent
LAST_BEAT = "api_last_beat"          # kv: the last heartbeat
LAST_TASKS = "api_last_tasks"        # kv: the last GET /tasks
PUSH_EVERY, BEAT_EVERY = 60, 900
TASKS_EVERY = 120                    # GET /tasks at most this often, all the poster's sites at once (WO35)
GOLIVE = "api_golive_checked"         # kv: the go_live_at the go-live check (D4) ran for
BATCH = 200
SOLD_SKIP = "skipped: sold"          # a queued row of an item that sold elsewhere (WO33 A1)
MAX_ATTEMPTS = 3                     # a take-down that failed this often is the owner's (the group asks)
WAITING = "waiting"                  # WO40: a take-down that failed, tried again only once the API hands it out again


class ApiError(Exception):
    """The API couldn't be reached or refused the call (`status`: its HTTP status, None when it didn't answer)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def refused(self) -> bool:
        """The API answered and will never take this call as it is (a 4xx but a key, timeout or rate refusal)."""
        return self.status is not None and 400 <= self.status < 500 and self.status not in (401, 403, 408, 429)


class Api:
    """thrift-api over HTTPS: the `mac` function key in x-functions-key on every call."""

    def __init__(self, url: str, key: str, timeout: float = 15.0, client: httpx.Client | None = None):
        self.url, self.key, self.timeout = url.rstrip("/"), key, timeout
        self.client = client

    def _call(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        headers = {"x-functions-key": self.key}
        try:
            if self.client is not None:
                r = self.client.request(method, self.url + path, json=body, params=params, headers=headers)
            else:
                r = httpx.request(method, self.url + path, json=body, params=params, headers=headers,
                                  timeout=self.timeout)
        except httpx.HTTPError as e:
            raise ApiError(f"{method} {path}: {type(e).__name__}") from None
        if r.status_code >= 300:
            raise ApiError(f"{method} {path}: HTTP {r.status_code}", r.status_code)
        try:
            return r.json() if r.content else {}
        except ValueError:
            return {"text": r.text}

    def post(self, path: str, body: dict) -> dict:
        return self._call("POST", path, body=body)

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._call("GET", path, params=params)


def api() -> Api | None:
    """The API from .env, or None (sales tracking off)."""
    url, key = os.getenv(API_URL_ENV, "").strip(), os.getenv(API_KEY_ENV, "").strip()
    return Api(url, key) if url and key else None


def say_off(db: DB) -> None:
    """Once a day: sales tracking is off for want of its two .env lines (posting goes on as before)."""
    today = datetime.now().strftime("%Y-%m-%d")
    if db.kv_get(OFF_SAID) != today:
        db.kv_set(OFF_SAID, today)
        notify.say(f"Sales tracking is off: {API_URL_ENV} / {API_KEY_ENV} aren't in .env on the Mac — posting goes on "
                   "as before.")


def _due(db: DB, key: str, every: float, force: bool = False) -> bool:
    last = db.kv_get(key)
    if force or not last:
        return True
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() >= every
    except ValueError:
        return True


def _stamp(db: DB, key: str) -> None:
    db.kv_set(key, datetime.now(timezone.utc).isoformat(timespec="seconds"))


def _claim(db: DB, key: str, every: float, force: bool = False) -> bool:
    """Due → the stamp taken now, in ONE transaction: two callers at the same moment (the Poshmark worker's thread and
    the Depop / Vinted workers, each on its own connection) never both go ahead — a CI runner saw two GET /tasks at the
    poster's start (WO35)."""
    if db.conn.in_transaction:                      # inside the caller's own transaction: as before
        if not _due(db, key, every, force):
            return False
        _stamp(db, key)
        return True
    with db.tx():
        if not _due(db, key, every, force):
            return False
        _stamp(db, key)
    return True


# ---------------------------------------------------------------- what /sync gets

def item_doc(db: DB, iid: str) -> dict | None:
    it = db.item(iid)
    if it is None:
        return None
    render = (loads(it["renders"]) or {}).get("poshmark") or {}
    return {"id": iid, "title": render.get("title"), "brand": render.get("brand"), "size": render.get("size"),
            "price": it["owner_price"] or render.get("price"), "created_at": it["created_at"],
            "updated_at": it["updated_at"],
            # WO45 §7: the API's safety net for an item priced but not live — the status, whether the owner priced
            # it, a test item (never listed), and how many photos it has
            "status": it["status"], "priced": bool(it["owner_price"]), "test": bool(it["test"]),
            "photos": len((render.get("photos") or [])),
            # WO41: the price-drop clock and stage
            "all_live_at": it["all_live_at"], "drop_stage": it["drop_stage"] or 0, "next_drop_at": it["next_drop_at"],
            "no_drop": it["no_drop"]}


def price_doc(db: DB, rid: str) -> dict | None:
    """WO41: one price change (never a dry run) as /sync takes it."""
    row = db.conn.execute("SELECT * FROM price_changes WHERE id=? AND dry=0", (rid,)).fetchone()
    if row is None:
        return None
    return {k: row[k] for k in ("id", "item_id", "marketplace", "stage", "old_price", "new_price", "pct", "status",
                                "attempts", "error", "due_at", "done_at", "created_at", "updated_at")}


def listing_doc(db: DB, iid: str, mp: str) -> dict | None:
    row = db.listing(iid, mp)
    if row is None:
        return None
    fields = loads(row["fields_json"]) or {}
    return {"item_id": iid, "marketplace": mp, "status": row["status"], "url": row["url"],
            "listing_id": row["listing_id"], "sku": iid if mp == "depop" else None, "price": row["price"],
            "posted_at": row["posted_at"], "updated_at": row["updated_at"],
            "price_live": row["price_live"], "price_read_at": row["price_read_at"],           # WO41
            "error": (row["error"] or "")[:200] or None,                                     # WO45 §7: its reason
            "photos_sent": len(fields.get("photos") or []) or None,                            # WO45 §6
            "photos_live": row["photos_live"], "photos_read_at": row["photos_read_at"]}


def share_doc(db: DB, sid: str) -> dict | None:
    """WO45 §3: a share's ledger row as /sync takes it."""
    from thrift_agent import shares
    return shares.doc(db, sid)


def push(db: DB, client: Api) -> int:
    """The changed items and listings to POST /sync, oldest change first, in batches; a key changed again meanwhile
    stays for the next push. Returns how many went. ApiError when the API can't be reached (nothing lost)."""
    sent = 0
    while True:
        rows = db.conn.execute("SELECT kind, key, changed_at FROM api_dirty ORDER BY changed_at LIMIT ?",
                               (BATCH,)).fetchall()
        if not rows:
            return sent
        items, listings, changes, shares_ = [], [], [], []
        for kind, key, _ in rows:
            if kind == "item":
                doc = item_doc(db, key)
                if doc:
                    items.append(doc)
            elif kind == "price_change":                 # WO41
                doc = price_doc(db, key)
                if doc:
                    changes.append(doc)
            elif kind == "share":                        # WO45 §3: the share ledger
                doc = share_doc(db, key)
                if doc:
                    shares_.append(doc)
            else:
                iid, _, mp = key.partition("|")
                doc = listing_doc(db, iid, mp)
                if doc:
                    listings.append(doc)
        client.post("/sync", {"items": items, "listings": listings, **({"price_changes": changes} if changes else {}),
                              **({"shares": shares_} if shares_ else {})})
        with db.tx() as c:
            for kind, key, changed_at in rows:
                c.execute("DELETE FROM api_dirty WHERE kind=? AND key=? AND changed_at=?", (kind, key, changed_at))
        sent += len(rows)
        if len(rows) < BATCH:
            return sent


def queue(db: DB, path: str, body: dict) -> None:
    """A call to the API kept until it goes (the outbox), in order."""
    db.conn.execute("INSERT INTO api_outbox (path, body, created_at) VALUES (?,?,?)", (path, json.dumps(body), now()))


def flush(db: DB, client: Api) -> int:
    """The outbox, oldest first; the first that fails stops it (order kept). Returns how many went. WO38: a call the
    API refuses for good (`ApiError.refused`, e.g. a route it doesn't have yet) is dropped with an event — kept, it would
    hold every call after it forever."""
    sent = 0
    for row in db.conn.execute("SELECT id, path, body FROM api_outbox ORDER BY id").fetchall():
        try:
            client.post(row["path"], json.loads(row["body"]))
        except ApiError as e:
            if not e.refused:
                raise
            db.log(None, "api_refused", {"path": row["path"], "status": e.status})
        db.conn.execute("DELETE FROM api_outbox WHERE id=?", (row["id"],))
        sent += 1
    return sent


def call(db: DB, path: str, body: dict) -> bool:
    """Send now if the API answers, else keep it in the outbox. True when it went now."""
    client = api()
    if client is None:
        say_off(db)
        return False
    queue(db, path, body)
    try:
        flush(db, client)
        return True
    except ApiError as e:
        db.log(None, "api_down", {"path": path, "error": str(e)})
        return False


def tick(db: DB, force_beat: bool = False, info: dict | None = None) -> dict:
    """The worker's turn: the changes and the outbox every minute, the heartbeat every 15 minutes (and on a wake:
    `force_beat`). Returns what it did; never raises (the API down is a quiet event)."""
    client = api()
    if client is None:
        say_off(db)
        return {"off": True}
    done = {}
    try:
        if _due(db, LAST_PUSH, PUSH_EVERY, force_beat):
            done["synced"] = push(db, client)
            done["flushed"] = flush(db, client)
            _stamp(db, LAST_PUSH)
        if _due(db, LAST_BEAT, BEAT_EVERY, force_beat):
            beat = client.post("/heartbeat", {"source": "mac", "info": info or {}})
            _stamp(db, LAST_BEAT)
            done["beat"] = True
            when = beat.get("go_live_at") if beat.get("mode") == "live" else None
            if when and db.kv_get(GOLIVE) != when:
                done["golive"] = golive_check(db, client, when)
    except ApiError as e:
        db.log(None, "api_down", {"error": str(e)})
        done["error"] = str(e)
    return done


def golive_check(db: DB, client: Api, go_live_at: str, live=None, pause: float = 1.0) -> dict:
    """WO33 D4, once when sales go live: every item listed on Depop or Vinted whose Poshmark listing is no longer for
    sale (its public page — the WO30 backfill checker: sold, not for sale, removed) is told to the API, which makes
    its take-downs; ONE ops summary. A page that can't be read is counted, not acted on."""
    from thrift_agent import crosslist
    live = live or crosslist.poshmark_live
    rows = db.conn.execute(
        "SELECT p.item_id, p.url FROM listings p WHERE p.marketplace='poshmark' AND p.status='posted' "
        "AND p.url IS NOT NULL AND EXISTS (SELECT 1 FROM listings o WHERE o.item_id=p.item_id "
        "AND o.marketplace IN ('depop','vinted') AND o.status='posted') ORDER BY p.posted_at, p.item_id").fetchall()
    gone, unknown = [], []
    for n, r in enumerate(rows):
        if n and pause:
            time.sleep(pause)                       # polite to Poshmark's public pages
        state = live(r["url"])
        if state is False:
            gone.append(r["item_id"])
            queue(db, "/mac-event", {"kind": "not_for_sale", "item_id": r["item_id"], "marketplace": "poshmark",
                                     "url": r["url"]})
        elif state is None:
            unknown.append(r["item_id"])
    try:
        flush(db, client)
    except ApiError as e:                           # kept in the outbox: they go with the next push
        db.log(None, "api_down", {"path": "/mac-event", "error": str(e)})
    db.kv_set(GOLIVE, go_live_at)
    titles = [((item_doc(db, i) or {}).get("title") or i) for i in gone]
    notify.say(f"Go-live check: {len(rows)} item{'s' if len(rows) != 1 else ''} on Depop/Vinted checked against "
               f"Poshmark — {len(gone)} no longer for sale there" + (f" (take-downs made: {'; '.join(titles)})" if gone
                                                                      else "")
               + (f", {len(unknown)} unreadable (left as they are)" if unknown else "") + ".")
    db.log(None, "golive_check", {"checked": len(rows), "gone": gone, "unknown": unknown, "go_live_at": go_live_at})
    return {"checked": len(rows), "gone": gone, "unknown": unknown}


# ---------------------------------------------------------------- take-downs

def fetch_tasks(db: DB, sites: list[str], force: bool = False) -> int:
    """The pending take-downs for these sites (GET /tasks leases them for 10 minutes), kept in `takedowns`. Asked at
    most every TASKS_EVERY seconds — one stamp for every caller, so the poster asks for all its sites at once —
    unless `force` (the CLI's `thrift delist --run`). Returns how many came.

    WO40: the API counts the attempts and decides when one is the owner's — a task it hands out is pending here again
    whatever this Mac thought of it (live: the UGG's rows were 'failed' here after three quick tries the API had taken
    as one, and the API, never reaching 3, re-leased them silently for a day)."""
    client = api()
    if client is None or not sites or not _claim(db, LAST_TASKS, TASKS_EVERY, force):
        return 0                                    # (the API down: asked again after TASKS_EVERY, never a storm)
    try:
        answer = client.get("/tasks", {"sites": ",".join(sites)})
    except ApiError as e:
        db.log(None, "api_down", {"path": "/tasks", "error": str(e)})
        return 0
    got = answer.get("tasks") or []
    for iid in answer.get("sold") or []:            # sold anywhere: never listed after the sale (A1)
        skip_sold(db, iid)
    reposts = [t for t in got if t.get("kind") == "repost"]      # WO45 §7: the API's safety net asks for one retry
    got = [t for t in got if t.get("kind") != "repost"]
    for t in reposts:
        repost(db, t)
    for t in got:
        db.conn.execute(
            "INSERT INTO takedowns (id, item_id, marketplace, listing_url, title, status, attempts, fetched_at) "
            "VALUES (?,?,?,?,?,'pending',?,?) ON CONFLICT(id) DO UPDATE SET status=CASE WHEN takedowns.status IN "
            "('done','not_found','sold') THEN takedowns.status ELSE 'pending' END, attempts=excluded.attempts, "
            "fetched_at=excluded.fetched_at",
            (t["id"], t["item_id"], t["marketplace"], t.get("listing_url"), t.get("title"), t.get("attempts") or 0,
             now()))
    return len(got)


KEEP_REASONS = ("no size on the item", "shared again", "already live", "sold", "not published automatically",
                "unconfirmed publish", "Mismatch", "refused the form")   # a listing row left as it is: its reason is
# the owner's to settle, or the Mac's own flow settles it (a refused form: WO39's re-check — live 2026-10-20 the net
# re-queued the Tory Burch before the re-check could run)


def repost(db: DB, task: dict) -> None:
    """WO45 §7: the API found an item priced an hour ago and not live on a site with no reason it knows — its row
    here goes back in line ONCE (failed or skipped → queued; a row with a reason of the owner's — no size, a twin,
    sold, autopublish off, an unconfirmed publish, a mismatch — stays), and the task is answered at once."""
    iid, mp = task.get("item_id"), task.get("marketplace")
    row = db.listing(iid, mp) if iid and mp else None
    it = db.item(iid) if iid else None
    if row is None or it is None:
        call(db, f"/tasks/{task['id']}", {"result": "not_found", "error": "no such listing here"})
        return
    error = row["error"] or ""
    if it["status"] in ("dropped", "failed") or ("test" in it.keys() and it["test"]):
        call(db, f"/tasks/{task['id']}", {"result": "done", "error": f"item {it['status']}: left as it is"})
        return
    if row["status"] not in ("failed", "skipped") or any(k in error for k in KEEP_REASONS):
        call(db, f"/tasks/{task['id']}", {"result": "done", "error": f"left as it is ({row['status']}: {error[:80]})"})
        return
    db.upsert_listing(iid, mp, status="queued", error=f"retried by the safety net (was: {error[:120]})")
    if it["status"] in ("failed",):
        pass
    db.log(iid, "reposted", {"site": mp, "was": error[:200], "by": "api"})
    call(db, f"/tasks/{task['id']}", {"result": "done"})


def pending(db: DB, site: str | None = None) -> list:
    sql = "SELECT * FROM takedowns WHERE status='pending'" + (" AND marketplace=?" if site else "") + " ORDER BY fetched_at"
    return db.conn.execute(sql, (site,) if site else ()).fetchall()


def skip_sold(db: DB, iid: str) -> list[str]:
    """The item sold: its rows still queued anywhere are skipped (sold) — never listed after the sale (WO33 A1) —
    and its price drops not yet saved are called off (WO41)."""
    from thrift_agent import pricedrop
    pricedrop.cancel(db, iid, "sold")
    skipped = []
    for row in db.listings_for(iid):
        if row["status"] == "queued":
            db.upsert_listing(iid, row["marketplace"], status="skipped", error=SOLD_SKIP)
            skipped.append(row["marketplace"])
    if skipped:
        db.log(iid, "skipped_sold", {"mps": skipped})
    return skipped


def report(db: DB, task_id: str, result: str, error: str | None = None, evidence: str | None = None,
           manual: bool = False) -> None:
    """A take-down's result to POST /tasks/{id} (through the outbox: nothing is lost while the API is away). WO40: a
    failure waits here until the API hands the task out again (its lease taken anew — the API counts only a failure
    of a task it leased), never tried again at once on the same lease; the API's third failure asks the owner."""
    if result == "failed" and not manual:
        db.conn.execute("UPDATE takedowns SET attempts=attempts+1, error=?, status=? WHERE id=?",
                        (error, WAITING, task_id))
    else:
        db.conn.execute("UPDATE takedowns SET status=?, error=?, done_at=? WHERE id=?", (result, error, now(), task_id))
    call(db, f"/tasks/{task_id}", {"result": result, "error": error, "evidence": evidence, "manual": manual})


async def run_takedown(site: str, poster, db: DB, ctx, ready=None, shots: Path | None = None) -> bool:
    """One pending take-down of this site, before any new listing (the site's worker): the item's queued rows skipped,
    the reversible action, the listing marked delisted, the result to the API. `ready(site, poster)` → None when the
    site's control is recorded, else why not — then nothing is tried and the owner is asked to do it by hand. True
    when one was handled."""
    rows = pending(db, site)
    if not rows:
        return False
    t = rows[0]
    iid, url = t["item_id"], t["listing_url"]
    skip_sold(db, iid)
    why = ready(site, poster) if ready is not None else None
    if why:
        report(db, t["id"], "failed", error=f"not done automatically: {why}", manual=True)
        return True
    from thrift_agent import crosslist
    from thrift_agent.post.base import AccountBlocked
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if shots is not None:
        shots.mkdir(parents=True, exist_ok=True)
    try:
        if site == "poshmark":
            ok = await poster.set_availability(ctx, url, False, shots=shots)
        else:
            if shots is not None:                   # the job's pictures next to the poster's evidence (WO40)
                poster.shot = shots / f"{iid}-{site}-{stamp}-takedown.png"
            ok = await poster.delist(url)
    except AccountBlocked as e:                 # a login / CAPTCHA / block page: the site stops for the window (one
        crosslist.block(db, site, str(e), e.page)   # plain group line); the take-down waits — not an attempt
        db.conn.execute("UPDATE takedowns SET status=?, error=? WHERE id=?", (WAITING, str(e)[:300], t["id"]))
        return True
    except Exception as e:  # noqa: BLE001 — a take-down never stops the worker; tried again (3 attempts in all)
        report(db, t["id"], "failed", error=f"{type(e).__name__}: {e}"[:300])
        return True
    if ok is None:
        # WO49 (live: the dress's and the Alo set's Depop rows stayed 'posted' after "gone already" — the photo check,
        # the price drops and the dashboard then took them for live): the row says what the site says
        db.upsert_listing(iid, site, status="delisted")
        report(db, t["id"], "not_found", error="the listing is gone already")
    elif ok == "sold":                          # Depop shows it sold already: nothing deleted — it sold twice
        db.upsert_listing(iid, site, status="sold")
        report(db, t["id"], "sold", evidence=str(getattr(poster, "shot", "") or "") or None)
    elif ok:
        db.upsert_listing(iid, site, status="delisted")
        report(db, t["id"], "done", evidence=str(getattr(poster, "shot", "") or "") or None)
    else:
        report(db, t["id"], "failed", error="the take-down didn't take")
    return True


def repair_gone(db: DB) -> list[tuple[str, str]]:
    """WO49, once a poster start: a listing still 'posted' here whose take-down the site answered "gone already"
    (the rows made before that result marked them) is delisted. [(item, site)]."""
    rows = db.conn.execute(
        "SELECT l.item_id, l.marketplace FROM listings l JOIN takedowns t ON t.item_id = l.item_id "
        "AND t.marketplace = l.marketplace WHERE l.status = 'posted' AND t.status = 'not_found'").fetchall()
    out = []
    for r in rows:
        db.upsert_listing(r["item_id"], r["marketplace"], status="delisted")
        db.log(r["item_id"], "listing_repaired", {"site": r["marketplace"], "status": "delisted",
                                                  "why": "its take-down found it gone"})
        out.append((r["item_id"], r["marketplace"]))
    return out


def listing_gone(db: DB, iid: str, site: str, why: str, sold: bool, url: str | None, title: str | None = None,
                 price=None) -> str:
    """WO49, the sale safety net that needs no email: a listing this Mac has 'posted' that the site itself now shows
    sold (or no longer for sale) — the photo check reads every live listing. A take-down of ours for the item on any
    site means we did it (or are doing it): the row only follows. Otherwise thrift-api is told — `sold_found` (a sale on
    that site: its take-downs everywhere else, as for a sale email) or, not for sale by hand, `not_for_sale` (take-downs
    elsewhere, no sale; WO33's go-live rule) — and the row follows. Returns what was done."""
    ours = db.conn.execute("SELECT 1 FROM takedowns WHERE item_id=? LIMIT 1", (iid,)).fetchone()
    db.upsert_listing(iid, site, status="sold" if sold else "delisted")
    if ours:
        db.log(iid, "listing_gone_ours", {"site": site, "why": why})
        return "ours"
    body = ({"kind": "sold_found", "marketplace": site, "listing_url": url, "item_id": iid, "title": title,
             "price": price} if sold else
            {"kind": "not_for_sale", "item_id": iid, "marketplace": site, "url": url})
    sent = call(db, "/mac-event", body)
    db.log(iid, "listing_gone", {"site": site, "why": why, "sold": sold, "told_api": sent})
    return "sold" if sold else "not_for_sale"


def pending_count(db: DB) -> int:
    return db.conn.execute("SELECT COUNT(*) FROM takedowns WHERE status='pending'").fetchone()[0]


def lease_age_ok(fetched_at: str, minutes: int = 10) -> bool:
    """A take-down fetched less than its lease ago (after that the API may hand it out again)."""
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at) < timedelta(minutes=minutes)
    except ValueError:
        return False
