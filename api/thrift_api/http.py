"""The HTTP side (WO33): `handle(method, path, query, body)` → (status, headers, body bytes), the whole API without
Azure in the way (function_app.py only adapts it; the tests call it directly). JSON in and out; a body that isn't a
JSON object is 400, an unknown path 404, a known path with another method 405; a refused request says why
({"error": …}); anything unexpected is a 500 that is logged and says nothing more.

The routes (every one needs the function key; Azure checks it before this code runs):
  POST /email               {message_id, thread_id, from, subject, date, text}   one email from the Gmail reader
  POST /samples             {"samples": [the same payloads]}                    the developer's examples
  POST /heartbeat           {source, info}
  POST /sync                {"items": [...], "listings": [...]}                 the Mac's items and listings
  GET  /tasks?sites=depop,vinted                                                 take-downs for the Mac (leased)
  POST /tasks/{id}          {result: done|not_found|failed, error, evidence, manual}
  POST /mac-event           {"kind": "shipped" | "sold_found" | "not_for_sale", …}
  GET  /sales?state=open|unmatched|all
  POST /sales/{id}/match    {"item_id"}
  POST /test-message        {"chat": "ops" | "group"}
  GET  /dashboard           the owner's page (HTML)
  GET  /health

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

from . import core, dashboard, util
from .db import Database
from .schema import migrate
from .util import ApiError, BadRequest

log = logging.getLogger(__name__)

JSON_TYPE = "application/json; charset=utf-8"
HTML_HEADERS = {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
                "Content-Security-Policy": dashboard.CSP, "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}

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
    unknown = [s for s in sites if s not in util.SITES]
    if unknown:
        raise BadRequest(f"unknown sites: {', '.join(unknown)} (poshmark, depop, vinted)")
    return 200, {"tasks": core.take_tasks(db, sites, now), "sold": core.sold_items(db, now), "mode": util.mode()}


def _task_result(db, body, query, params, now):
    manual = body.get("manual", False)
    if not isinstance(manual, bool):
        raise BadRequest("manual is true or false")
    return 200, core.task_result(db, params["task_id"], body.get("result"), body.get("error"), body.get("evidence"),
                                 now, manual=manual)


def _mac_event(db, body, query, params, now):
    return 200, core.mac_event(db, body, now)


def _sales(db, body, query, params, now):
    return 200, {"sales": core.sales_list(db, _param(query, "state") or "open")}


def _match(db, body, query, params, now):
    return 200, core.match_sale(db, params["sale_id"], body.get("item_id"), now)


def _test_message(db, body, query, params, now):
    chat = body.get("chat", "ops")
    if chat not in ("ops", "group"):
        raise BadRequest("chat is ops or group")
    return 200, core.test_message(chat, now)


def _dashboard(db, body, query, params, now):
    return 200, dashboard.render(core.dashboard_data(db, now), now=now)


def _health(db, body, query, params, now):
    return 200, core.health(db, now)


Route = tuple[str, re.Pattern, Callable, bool]          # method, path, handler, needs the database
ROUTES: tuple[Route, ...] = (
    ("POST", re.compile(r"/email"), _email, True),
    ("POST", re.compile(r"/samples"), _samples, True),
    ("POST", re.compile(r"/heartbeat"), _heartbeat, True),
    ("POST", re.compile(r"/sync"), _sync, True),
    ("GET", re.compile(r"/tasks"), _tasks, True),
    ("POST", re.compile(r"/tasks/(?P<task_id>[^/]+)"), _task_result, True),
    ("POST", re.compile(r"/mac-event"), _mac_event, True),
    ("GET", re.compile(r"/sales"), _sales, True),
    ("POST", re.compile(r"/sales/(?P<sale_id>[^/]+)/match"), _match, True),
    ("POST", re.compile(r"/test-message"), _test_message, False),
    ("GET", re.compile(r"/dashboard"), _dashboard, True),
    ("GET", re.compile(r"/health"), _health, True),
)


def handle(method: str, path: str, query: dict | None = None, body: bytes | None = b"", now: datetime | None = None,
           db: Database | None = None) -> tuple[int, dict, bytes]:
    """One request: (status, headers, body). `db` and `now` are for the tests; the Function passes neither."""
    method = (method or "GET").upper()
    path = "/" + (path or "").split("?", 1)[0].strip("/")
    found = [(route, m) for route in ROUTES if (m := route[1].fullmatch(path))]
    if not found:
        return _json(404, {"error": f"no such path: {path}"})
    chosen = next(((route, m) for route, m in found if route[0] == method), None)
    if chosen is None:
        allowed = ", ".join(sorted({route[0] for route, _ in found}))
        status, headers, data = _json(405, {"error": f"{method} is not allowed here", "allowed": allowed})
        return status, {**headers, "Allow": allowed}, data
    (_, _, handler, needs_db), match = chosen
    now = util.aware(now) if now else util.utcnow()
    try:
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
    except Exception:  # noqa: BLE001 — logged with its traceback; the caller only learns that it failed
        log.exception("%s %s failed", method, path)
        if path == "/health":
            return _json(503, {"ok": False, "db": "error", "mode": util.mode()})
        return _json(500, {"error": "internal error"})
    if isinstance(result, str):
        return status, dict(HTML_HEADERS), result.encode("utf-8")
    return _json(status, result)


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
