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
  minutes); each site's worker runs its own before any new listing (`run_takedown`): the item's other queued rows
  are skipped (sold), the reversible action is done (Poshmark Not for Sale, Depop Mark as sold, Vinted Hide — never a
  delete), and the result goes back to POST /tasks/{id}. A site whose control isn't recorded yet (UNVERIFIED) is
  never tried: the API is told at once, and the group asks the owner to mark it sold there."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

from thrift_agent import notify
from thrift_agent.db import DB, loads, now

API_URL_ENV, API_KEY_ENV = "THRIFT_API_URL", "THRIFT_API_KEY"
OFF_SAID = "api_off_said"            # kv: the day "sales tracking is off" was said
LAST_PUSH = "api_last_push"          # kv: when the changes and the outbox were last sent
LAST_BEAT = "api_last_beat"          # kv: the last heartbeat
LAST_TASKS = "api_last_tasks"        # kv: the last GET /tasks
PUSH_EVERY, BEAT_EVERY, TASKS_EVERY = 60, 900, 300
GOLIVE = "api_golive_checked"         # kv: the go_live_at the go-live check (D4) ran for
BATCH = 200
SOLD_SKIP = "skipped: sold"          # a queued row of an item that sold elsewhere (WO33 A1)
MAX_ATTEMPTS = 3                     # a take-down that failed this often is the owner's (the group asks)


class ApiError(Exception):
    """The API couldn't be reached or refused the call."""


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
            raise ApiError(f"{method} {path}: HTTP {r.status_code}")
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


# ---------------------------------------------------------------- what /sync gets

def item_doc(db: DB, iid: str) -> dict | None:
    it = db.item(iid)
    if it is None:
        return None
    render = (loads(it["renders"]) or {}).get("poshmark") or {}
    return {"id": iid, "title": render.get("title"), "brand": render.get("brand"), "size": render.get("size"),
            "price": it["owner_price"] or render.get("price"), "created_at": it["created_at"],
            "updated_at": it["updated_at"]}


def listing_doc(db: DB, iid: str, mp: str) -> dict | None:
    row = db.listing(iid, mp)
    if row is None:
        return None
    return {"item_id": iid, "marketplace": mp, "status": row["status"], "url": row["url"],
            "listing_id": row["listing_id"], "sku": iid if mp == "depop" else None, "price": row["price"],
            "posted_at": row["posted_at"], "updated_at": row["updated_at"]}


def push(db: DB, client: Api) -> int:
    """The changed items and listings to POST /sync, oldest change first, in batches; a key changed again meanwhile
    stays for the next push. Returns how many went. ApiError when the API can't be reached (nothing lost)."""
    sent = 0
    while True:
        rows = db.conn.execute("SELECT kind, key, changed_at FROM api_dirty ORDER BY changed_at LIMIT ?",
                               (BATCH,)).fetchall()
        if not rows:
            return sent
        items, listings = [], []
        for kind, key, _ in rows:
            if kind == "item":
                doc = item_doc(db, key)
                if doc:
                    items.append(doc)
            else:
                iid, _, mp = key.partition("|")
                doc = listing_doc(db, iid, mp)
                if doc:
                    listings.append(doc)
        client.post("/sync", {"items": items, "listings": listings})
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
    """The outbox, oldest first; the first that fails stops it (order kept). Returns how many went."""
    sent = 0
    for row in db.conn.execute("SELECT id, path, body FROM api_outbox ORDER BY id").fetchall():
        client.post(row["path"], json.loads(row["body"]))
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
    """The pending take-downs for these sites (GET /tasks leases them for 10 minutes), kept in `takedowns`. Returns
    how many came."""
    client = api()
    if client is None or not sites or not _due(db, LAST_TASKS, TASKS_EVERY, force):
        return 0
    try:
        answer = client.get("/tasks", {"sites": ",".join(sites)})
    except ApiError as e:
        db.log(None, "api_down", {"path": "/tasks", "error": str(e)})
        return 0
    _stamp(db, LAST_TASKS)
    got = answer.get("tasks") or []
    for iid in answer.get("sold") or []:            # sold anywhere: never listed after the sale (A1)
        skip_sold(db, iid)
    for t in got:
        db.conn.execute(
            "INSERT INTO takedowns (id, item_id, marketplace, listing_url, title, status, attempts, fetched_at) "
            "VALUES (?,?,?,?,?,'pending',?,?) ON CONFLICT(id) DO UPDATE SET status=CASE WHEN takedowns.status IN "
            "('done','not_found','failed') THEN takedowns.status ELSE 'pending' END, fetched_at=excluded.fetched_at",
            (t["id"], t["item_id"], t["marketplace"], t.get("listing_url"), t.get("title"), t.get("attempts") or 0,
             now()))
    return len(got)


def pending(db: DB, site: str | None = None) -> list:
    sql = "SELECT * FROM takedowns WHERE status='pending'" + (" AND marketplace=?" if site else "") + " ORDER BY fetched_at"
    return db.conn.execute(sql, (site,) if site else ()).fetchall()


def skip_sold(db: DB, iid: str) -> list[str]:
    """The item sold: its rows still queued anywhere are skipped (sold) — never listed after the sale (WO33 A1)."""
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
    """A take-down's result to POST /tasks/{id} (through the outbox: nothing is lost while the API is away)."""
    if result == "failed" and not manual:          # tried again, 3 attempts in all (the API counts them too)
        db.conn.execute("UPDATE takedowns SET attempts=attempts+1, error=?, status=CASE WHEN attempts+1 >= ? THEN "
                        "'failed' ELSE 'pending' END WHERE id=?", (error, MAX_ATTEMPTS, task_id))
    else:
        db.conn.execute("UPDATE takedowns SET status=?, error=?, done_at=? WHERE id=?", (result, error, now(), task_id))
    call(db, f"/tasks/{task_id}", {"result": result, "error": error, "evidence": evidence, "manual": manual})


async def run_takedown(site: str, poster, db: DB, ctx, ready=None) -> bool:
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
    try:
        if site == "poshmark":
            ok = await poster.set_availability(ctx, url, False)
        else:
            ok = await poster.delist(url)
    except Exception as e:  # noqa: BLE001 — a take-down never stops the worker; tried again (3 attempts in all)
        report(db, t["id"], "failed", error=f"{type(e).__name__}: {e}"[:300])
        return True
    if ok is None:
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


def pending_count(db: DB) -> int:
    return db.conn.execute("SELECT COUNT(*) FROM takedowns WHERE status='pending'").fetchone()[0]


def lease_age_ok(fetched_at: str, minutes: int = 10) -> bool:
    """A take-down fetched less than its lease ago (after that the API may hand it out again)."""
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at) < timedelta(minutes=minutes)
    except ValueError:
        return False
