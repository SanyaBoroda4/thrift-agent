"""The HTTP side (WO33): `handle(method, path, query, body)` → (status, headers, body bytes), the whole API without
Azure in the way (function_app.py only adapts it; the tests call it directly). JSON in and out; a body that isn't a
JSON object is 400, an unknown path 404, a known path with another method 405; a refused request says why
({"error": …}); anything unexpected is a 500 that is logged — its type and where, never its message or a body
(WO35: no email text, buyer details, keys or tokens in the logs) — and says nothing more.

The routes (every one needs the function key — Azure checks it before this code runs — but the three eBay must reach
without one: /ebay/account-deletion, /ebay/oauth/callback, /ebay/oauth/declined, /ebay/privacy, WO57 below):
  POST /email               {message_id, thread_id, from, subject, date, text}   one email from the Gmail reader
  POST /samples             {"samples": [the same payloads]}                    the developer's examples
  POST /heartbeat           {source, info}
  POST /sync                {"items": [...], "listings": [...]}                 the Mac's items and listings
  GET  /tasks?sites=depop,vinted                                                 take-downs for the Mac (leased)
  POST /tasks/{id}          {result: done|not_found|failed, error, evidence, manual}
  POST /mac-event           {"kind": "shipped" | "sold_found" | "not_for_sale", …}
  POST /orders              {"marketplace", "swept_at", "orders": [...]}         a site's sold orders (WO38)
  GET  /sales?state=open|unmatched|all
  POST /sales/{id}/match    {"item_id"}
  POST /test-message        {"chat": "ops" | "group", "text": "…" (optional: sent as given, WO45)}
  POST /ship-list           {"preview": true}: tonight's shipping list to the private chat, marked (WO60)
  GET  /dashboard           the owner's page (HTML)
  GET  /health
  WO45 §2 (uploads.py): the iPhone's shares straight to the API, the Mac fetching them —
  POST /share/{id}/photo?n=&of=&name=&test=1   the photo itself (a raw image body, or one multipart file field)
  POST /share/{id}/end?count=N&test=1          the end of the share (an empty or JSON body)
  GET  /shares/pending                         the shares the Mac hasn't fetched, with their photos
  GET  /shares/{id}/photo/{name}               the bytes, with their Content-Type
  POST /shares/{id}/fetched {"names": [...]}   the Mac has it
  WO57 (ebay.py): eBay, the fourth marketplace —
  GET  /ebay/account-deletion?challenge_code=  eBay's endpoint check: {"challengeResponse": 64 hex}      (no key)
  POST /ebay/account-deletion                  a deletion notice: 200 {} — nothing read, kept or logged   (no key)
  GET  /ebay/oauth/start                       302 to eBay's consent page (the owner's link, with its own key)
  GET  /ebay/oauth/callback?code=&state=       eBay's redirect after Agree: the token stored, a small page (no key:
                                               eBay's `code` would be read as the function key; the single-use
                                               `state` guards it)
  GET  /ebay/oauth/declined                    eBay's redirect after "Not now": a small page               (no key)
  GET  /ebay/privacy                           the privacy page eBay's RuName form asks for               (no key)
  GET  /ebay/token?kind=user|app               {"access_token", "expires_at", "environment"} for the Mac; 409 not
                                               connected, 404 eBay not set up, 503 eBay away
  GET  /ebay/status                            configured / connected / the consent's end / the orders read (no secret)
  GET /tasks then also answers "ended": the eBay take-downs thrift-api did itself (an eBay take-down is never the Mac's).

The database is DATABASE_URL's, opened at the first request that needs it (migrations then, once per process). In live
mode every request that uses it is a live call: the first one writes go_live_at when it is missing."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Callable
from datetime import datetime

from . import core, dashboard, ebay, orders, shiplist, uploads, util
from .db import Database
from .schema import migrate
from .util import ApiError, BadRequest

log = logging.getLogger(__name__)

JSON_TYPE = "application/json; charset=utf-8"
HTML_HEADERS = {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
                "Content-Security-Policy": dashboard.CSP, "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}


class Raw:
    """A request body handed to a route as it came (WO45: a photo's bytes), with the request's Content-Type."""

    def __init__(self, data: bytes, content_type: str):
        self.data, self.content_type = data, content_type


class Binary:
    """A route's answer that is neither JSON nor the dashboard's HTML (WO45: a photo's bytes, its Content-Type)."""

    def __init__(self, data: bytes, content_type: str):
        self.data, self.content_type = data, content_type


class Redirect:
    """A route's answer that sends the browser on (WO57: to eBay's consent page) — its status with a Location."""

    def __init__(self, url: str):
        self.url = url


_DB: Database | None = None
_DB_LOCK = threading.Lock()


def get_db() -> Database:
    """DATABASE_URL's database, created and migrated at the first call."""
    global _DB
    if _DB is None:
        with _DB_LOCK:
            if _DB is None:
                url = os.environ.get("DATABASE_URL", "").strip()
                if not url:
                    raise RuntimeError("DATABASE_URL is not set")
                database = Database(url)
                migrate(database)
                _DB = database
    return _DB


# --- the routes: (db, body, query, path params, now) -> (status, payload) ------------------------------------------

def _email(db, body, query, params, now):
    return 200, core.process_email(db, body, now)


def _samples(db, body, query, params, now):
    return 200, core.store_samples(db, body.get("samples"))


def _heartbeat(db, body, query, params, now):
    return 200, core.heartbeat(db, body.get("source"), body.get("info"), now)


def _sync(db, body, query, params, now):
    return 200, core.sync(db, body, now)


def _tasks(db, body, query, params, now):
    raw = _param(query, "sites")
    sites = [s.strip().lower() for s in raw.split(",") if s.strip()] if raw else list(util.SITES)
    unknown = [s for s in sites if s not in util.SITES]       # WO57: `ebay` is a site here whether or not it is on yet
    if unknown:
        raise BadRequest(f"unknown sites: {', '.join(unknown)} (poshmark, depop, vinted)")
    answer = {"tasks": core.take_tasks(db, sites, now), "sold": core.sold_items(db, now),
              "sold_on": core.sold_sites(db, now), "mode": util.mode()}
    if ebay.configured():
        # WO57: an eBay take-down is never in "tasks" — thrift-api ends the eBay listing itself; here is what it ended
        # (30 days), for the Mac to mark its own row. Without eBay set up the answer is as it always was.
        answer["ended"] = ebay.ended(db, now)
    return 200, answer


def _task_result(db, body, query, params, now):
    manual = body.get("manual", False)
    if not isinstance(manual, bool):
        raise BadRequest("manual is true or false")
    return 200, core.task_result(db, params["task_id"], body.get("result"), body.get("error"), body.get("evidence"),
                                 now, manual=manual)


def _mac_event(db, body, query, params, now):
    return 200, core.mac_event(db, body, now)


def _orders(db, body, query, params, now):
    return 200, orders.report(db, body, now)


def _sales(db, body, query, params, now):
    return 200, {"sales": core.sales_list(db, _param(query, "state") or "open")}


def _match(db, body, query, params, now):
    return 200, core.match_sale(db, params["sale_id"], body.get("item_id"), now)


def _replay(db, body, query, params, now):
    return 200, core.replay(db, now)


def _test_message(db, body, query, params, now):
    chat = body.get("chat", "ops")
    if chat not in ("ops", "group"):
        raise BadRequest("chat is ops or group")
    return 200, core.test_message(chat, now, text=body.get("text"))


def _ship_list(db, body, query, params, now):
    """WO60: the evening shipping list rendered now and sent to the private chat, marked "Preview" — only a preview
    here: the group's list is the hourly timer's, once a night."""
    if body.get("preview") is not True:
        raise BadRequest('only {"preview": true}: the group gets the list from the 10 PM timer')
    return 200, shiplist.night_list(db, now, preview=True)


def _dashboard(db, body, query, params, now):
    return 200, dashboard.render(core.dashboard_data(db, now), now=now)


def _health(db, body, query, params, now):
    return 200, core.health(db, now)


# WO45 §2 — the upload routes answer a refusal as {"ok": false, "error": …} (the Shortcut shows "ok")

def _upload(handler: Callable) -> Callable:
    def run(db, body, query, params, now):
        try:
            return handler(db, body, query, params, now)
        except ApiError as e:
            return e.status, {"ok": False, "error": str(e)}
    return run


@_upload
def _share_photo(db, body, query, params, now):
    return 200, uploads.receive_photo(db, uploads.store(), params["share_id"], body.data, body.content_type, query, now)


@_upload
def _share_end(db, body, query, params, now):
    return 200, uploads.end_share(db, params["share_id"], query, now)


def _shares_pending(db, body, query, params, now):
    return 200, uploads.pending(db)


def _share_photo_get(db, body, query, params, now):
    data, content_type = uploads.photo(db, uploads.store(), params["share_id"], params["name"])
    return 200, Binary(data, content_type)


def _share_fetched(db, body, query, params, now):
    return 200, uploads.mark_fetched(db, params["share_id"], body, now, uploads.store())   # WO47: the blobs go too


# WO57 — eBay (ebay.py)

def _ebay_challenge(db, body, query, params, now):
    """eBay's check of the account-deletion endpoint. Exactly "application/json", made by the JSON library (eBay: a
    hand-written body often carries a byte order mark, which fails its check)."""
    answer = ebay.challenge(_param(query, "challenge_code"))
    return 200, Binary(json.dumps(answer).encode("utf-8"), "application/json")


def _ebay_deletion(db, body, query, params, now):
    """A marketplace account deletion / closure notice: acknowledged, and that is all. thrift-api keeps nothing about
    any other eBay user (no buyer's name, username or address is ever stored), so there is nothing to delete — the
    body isn't read (function_app never asks Azure for it), kept or logged. eBay's signature isn't checked: its own
    wording is that a developer "should" verify it, and an endpoint that does nothing with the notice has nothing a
    forged one could abuse."""
    return 200, {}


def _ebay_start(db, body, query, params, now):
    return 302, Redirect(ebay.oauth_start(db, now))


def _ebay_callback(db, body, query, params, now):
    return ebay.oauth_callback(db, {"code": _param(query, "code"), "state": _param(query, "state")}, now)


def _ebay_declined(db, body, query, params, now):
    return 200, ebay.page(ebay.PAGE_DECLINED)


def _ebay_privacy(db, body, query, params, now):
    """The "Privacy Policy URL" eBay's RuName form asks for."""
    return 200, ebay.page(ebay.PAGE_PRIVACY, "Privacy")


def _ebay_token(db, body, query, params, now):
    """The one route that answers with a token (never logged: no line of this package carries an answer)."""
    return 200, ebay.access_token(db, _param(query, "kind") or "user", now)


def _ebay_status(db, body, query, params, now):
    return 200, ebay.status(db, now)


Route = tuple[str, re.Pattern, Callable, bool]          # method, path, handler, needs the database; a fifth element
#                                                         "raw" hands the handler the body as it came (http.Raw)
ROUTES: tuple[tuple, ...] = (
    ("POST", re.compile(r"/email"), _email, True),
    ("POST", re.compile(r"/samples"), _samples, True),
    ("POST", re.compile(r"/heartbeat"), _heartbeat, True),
    ("POST", re.compile(r"/sync"), _sync, True),
    ("GET", re.compile(r"/tasks"), _tasks, True),
    ("POST", re.compile(r"/tasks/(?P<task_id>[^/]+)"), _task_result, True),
    ("POST", re.compile(r"/mac-event"), _mac_event, True),
    ("POST", re.compile(r"/orders"), _orders, True),
    ("GET", re.compile(r"/sales"), _sales, True),
    ("POST", re.compile(r"/sales/(?P<sale_id>[^/]+)/match"), _match, True),
    ("POST", re.compile(r"/test-message"), _test_message, False),
    ("POST", re.compile(r"/ship-list"), _ship_list, True),                                               # WO60
    ("POST", re.compile(r"/replay"), _replay, True),
    ("GET", re.compile(r"/dashboard"), _dashboard, True),
    ("GET", re.compile(r"/health"), _health, True),
    ("POST", re.compile(r"/share/(?P<share_id>[^/]+)/photo"), _share_photo, True, "raw"),               # WO45 §2
    ("POST", re.compile(r"/share/(?P<share_id>[^/]+)/end"), _share_end, True, "raw"),
    ("GET", re.compile(r"/shares/pending"), _shares_pending, True),
    ("GET", re.compile(r"/shares/(?P<share_id>[^/]+)/photo/(?P<name>[^/]+)"), _share_photo_get, True),
    ("POST", re.compile(r"/shares/(?P<share_id>[^/]+)/fetched"), _share_fetched, True),
    ("GET", re.compile(r"/ebay/account-deletion"), _ebay_challenge, False),                             # WO57
    ("POST", re.compile(r"/ebay/account-deletion"), _ebay_deletion, False, "raw"),
    ("GET", re.compile(r"/ebay/oauth/start"), _ebay_start, True),
    ("GET", re.compile(r"/ebay/oauth/callback"), _ebay_callback, True),
    ("GET", re.compile(r"/ebay/oauth/declined"), _ebay_declined, False),
    ("GET", re.compile(r"/ebay/privacy"), _ebay_privacy, False),
    ("GET", re.compile(r"/ebay/token"), _ebay_token, True),
    ("GET", re.compile(r"/ebay/status"), _ebay_status, True),
)


def handle(method: str, path: str, query: dict | None = None, body: bytes | None = b"", now: datetime | None = None,
           db: Database | None = None, headers: dict | None = None) -> tuple[int, dict, bytes]:
    """One request: (status, headers, body). `db` and `now` are for the tests; the Function passes neither. `headers`
    (WO45): the request's, for a route that takes its body raw (the Content-Type tells a form from an image)."""
    method = (method or "GET").upper()
    path = "/" + (path or "").split("?", 1)[0].strip("/")
    found = [(route, m) for route in ROUTES if (m := route[1].fullmatch(path))]
    if not found:
        return _json(404, {"error": f"no such path: {path}"})
    chosen = next(((route, m) for route, m in found if route[0] == method), None)
    if chosen is None:
        allowed = ", ".join(sorted({route[0] for route, _ in found}))
        status, headers_, data = _json(405, {"error": f"{method} is not allowed here", "allowed": allowed})
        return status, {**headers_, "Allow": allowed}, data
    (_, _, handler, needs_db, *extra), match = chosen
    now = util.aware(now) if now else util.utcnow()
    try:
        if "raw" in extra:
            data = body if isinstance(body, bytes) else (body or "").encode("utf-8")
            payload = Raw(data, _header(headers, "content-type"))
        else:
            payload = _body(body) if method == "POST" else {}
        database = None
        if needs_db:
            try:
                database = db if db is not None else get_db()
            except Exception as e:  # noqa: BLE001 — no database: say so, never what the URL is
                log.error("database unavailable: %s", type(e).__name__)
                if path == "/health":
                    return _json(503, {"ok": False, "db": "unavailable", "mode": util.mode()})
                return _json(503, {"error": "database unavailable"})
            if util.mode() == "live":
                core.go_live(database, now)           # the first live call starts the live clock
        status, result = handler(database, payload, query or {}, match.groupdict(), now)
    except ApiError as e:
        return _json(e.status, {"error": str(e)})
    except Exception as e:  # noqa: BLE001 — logged as its type and place only (WO35); the caller learns it failed
        log.error("%s %s failed: %s", method, path, util.where(e))
        if path == "/health":
            return _json(503, {"ok": False, "db": "error", "mode": util.mode()})
        return _json(500, {"error": "internal error"})
    if isinstance(result, str):
        return status, dict(HTML_HEADERS), result.encode("utf-8")
    if isinstance(result, Binary):
        return status, {"Content-Type": result.content_type, "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff"}, result.data
    if isinstance(result, Redirect):
        return status, {"Location": result.url, "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}, b""
    return _json(status, result)


def _header(headers: dict | None, name: str) -> str:
    """One request header, whatever its case (Azure hands them lower-cased, a test as written)."""
    for key, value in (headers or {}).items():
        if str(key).lower() == name:
            return str(value or "")
    return ""


def _body(raw: bytes | str | None) -> dict:
    if raw is None or (isinstance(raw, (bytes, str)) and not raw.strip()):
        return {}
    try:
        value = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (ValueError, UnicodeDecodeError):
        raise BadRequest("the body is not valid JSON") from None
    if not isinstance(value, dict):
        raise BadRequest("the body must be a JSON object")
    return value


def _param(query: dict, name: str) -> str:
    value = query.get(name)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip()


def _json(status: int, payload: object) -> tuple[int, dict, bytes]:
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    return status, {"Content-Type": JSON_TYPE, "Cache-Control": "no-store"}, data
