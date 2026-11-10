"""eBay, the fourth marketplace — thrift-api's part (WO57). The Mac makes the listings; everything that must go on
while the Mac sleeps or keeps its quiet hours is here:

- **the account-deletion endpoint** eBay asks of every developer (`challenge`, `GET/POST /ebay/account-deletion`): the
  challenge answered, a notification acknowledged. Nothing about another eBay user is ever stored here, so there is
  nothing to delete — and nothing of a notification is read, kept or logged.
- **the consent** (`oauth_start`, `oauth_callback`): the owner clicks Agree on eBay's page once; the refresh token stays
  in Azure only — in `settings`, ENCRYPTED with EBAY_TOKEN_KEY (Fernet), never in clear, never logged. The Mac asks for
  a short-lived access token (`access_token`, `GET /ebay/token`) with its function key.
- **the orders** (`read_orders`, every 5 minutes): eBay's Fulfillment API, read only. An order line whose SKU is one of
  our item ids is the sale — recorded once by its order id through the order sweeps' own path (orders.sweep: the 💰
  line, the ship-by date, the take-downs on the other sites, shipped / cancelled after). An order for a listing that
  isn't ours is ONE quiet private line and nothing else. Only the order's id, dates and states and the line's SKU,
  listing id, title and price are read: never the buyer, the checkout notes, the ship-to or any address.
- **ending a listing** (`run_takedowns`): a sale on another site ends the eBay listing from here, at once (`kick`, right
  after the request's transaction committed) and again from the timer — whatever the Mac does. An eBay take-down is
  never handed to the Mac; it learns of it from GET /tasks (`ended`). Before anything is ended the listing is READ and
  must be ours: its SKU is the item's id. A listing of hers is never touched.
- **token trouble is a human step** (`needs_consent`, `consent_watch`): eBay refusing the refresh token, or the token
  30 / 14 / 7 / 3 / 2 / 1 days from its end → ONE private message with the link to click Agree again, never the
  group; a refused one is reminded every 12 hours while it lasts; a new consent clears it all.

Settings (all optional — without EBAY_CLIENT_ID the whole eBay side is off and nothing here calls anyone):
EBAY_ENV (production | sandbox), EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, EBAY_RUNAME, EBAY_SCOPES, EBAY_VERIFICATION_TOKEN,
EBAY_DELETION_ENDPOINT, EBAY_TOKEN_KEY, EBAY_START_LINK, EBAY_API (listing | inventory), EBAY_SELLER (the shop's eBay
username: a consent given with another account is refused).

`SENDER(method, url, headers, body, timeout) -> (status, bytes)` is the one way out to eBay (urllib; the tests replace
it). Nothing here logs or raises a token, a key, a secret, a response body or a buyer's data: a failure is its HTTP
status, eBay's own error code, or the exception's type."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from . import core, deadlines, orders, util
from .db import Database
from .util import ApiError, BadRequest, iso, parse_time

log = logging.getLogger(__name__)

SITE = util.EBAY
AUTH_URL = {"production": "https://auth.ebay.com/oauth2/authorize",
            "sandbox": "https://auth.sandbox.ebay.com/oauth2/authorize"}
TOKEN_URL = {"production": "https://api.ebay.com/identity/v1/oauth2/token",
             "sandbox": "https://api.sandbox.ebay.com/identity/v1/oauth2/token"}
ORDERS_URL = {"production": "https://apiz.ebay.com/sell/fulfillment/v1/order",
              "sandbox": "https://apiz.sandbox.ebay.com/sell/fulfillment/v1/order"}
GRAPHQL_SANDBOX = "https://graphqlapi.sandbox.ebay.com/graphql"      # UNVERIFIED: eBay's guide prints no sandbox host
GRAPHQL_URL = {"production": "https://graphqlapi.ebay.com/graphql", "sandbox": GRAPHQL_SANDBOX}
MARKETPLACE = "EBAY_US"
APP_SCOPE = "https://api.ebay.com/oauth/api_scope"
DEFAULT_SCOPES = " ".join((APP_SCOPE, f"{APP_SCOPE}/sell.listing", f"{APP_SCOPE}/sell.inventory",
                           f"{APP_SCOPE}/sell.fulfillment.readonly"))
TIMEOUT = 10                             # seconds a call to eBay may take (a take-down is 2 to 5 of them)
MAX_ANSWER = 4_000_000                   # bytes of one answer read
FREE_LISTINGS = 250                      # eBay's free listings a month (the 9 PM line)

# settings keys
CONSENT = "ebay_consent"                 # {"refresh": sealed, "expires_at", "scopes", "connected_at", "environment"}
TOKEN = "ebay_token:"                    # + user | app: {"token": sealed, "expires_at", "environment"}
STATE = "ebay_state:"                    # + the consent link's state: when it ends
NEEDS = "ebay_needs_consent"             # {"since", "said_at", "why", "tried_at"} while the permission is refused
FIRST_LINK = "ebay_first_link_said"     # the first consent link was sent (once per keyset: its client id)
EXPIRY_SAID = "ebay_expiry_said"         # {"expires_at", "said": [30, 14, …]}: the reminders said for this consent
ORDERS_READ = "ebay_orders_read_at"      # the start of the last successful read
ORDERS_ERROR = "ebay_orders_error"       # {"since", "at", "what", "said"} while the read fails
ON_SINCE = "ebay_on_since"               # when eBay was first seen on (watch.not_live expects it from then)

STATE_FOR = timedelta(minutes=10)
TOKEN_MARGIN = timedelta(minutes=5)      # a cached access token is handed out until this long before its end
REFRESH_LIFE = 47_304_000                # seconds: eBay's own figure (18 months), when its answer names none
REMIND_EVERY = timedelta(hours=12)       # a refused permission, while it lasts
RETRY_REFUSED = timedelta(hours=1)       # … and tried again by itself this often (a fixed setting needs no click)
EXPIRY_DAYS = (30, 14, 7, 3, 2, 1)       # days before the permission ends: one reminder each
ORDERS_FIRST = timedelta(days=3)         # the first read looks this far back
ORDERS_OVERLAP = timedelta(minutes=15)   # each read starts this long before the last one
ORDERS_BACK = timedelta(days=60)         # … and never further back than this
ORDERS_LIMIT = 200                       # eBay's most a page
ORDERS_PAGES = 10
ORDERS_DOWN = timedelta(hours=1)         # the read failing this long → ONE private line
ORDERS_CHUNK = orders.MAX_ORDERS
DAY_ZONE = "America/Los_Angeles"         # eBay's ship-by is the end of a US day; the Pacific date reads every US zone's right
TASKS_A_RUN = 10
RETRY_AFTER = timedelta(minutes=4)       # a failed take-down waits this long (the 5-minute timer tries it again)
ENDED_DAYS = 30
ENDED_RESULTS = ("done", "not_found", "sold")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
          "December")

ENDED_LINE = ("🔐 eBay wants a person — its permission for the agent ended (since {since}). 1 minute: {how}. Until then "
              "nothing new goes up on eBay, and a sale elsewhere can't end its eBay listing by itself.")
EXPIRING_LINE = "🔐 eBay's permission for the agent ends on {day} — 1 minute: {how}."
CONNECTED_LINE = "✅ eBay connected — the agent can list on eBay and read its orders. Valid until {day}."
WORKS_AGAIN_LINE = "✅ eBay's permission for the agent works again — nothing to do."
FIRST_LINE = ("🔗 eBay consent link — 1 minute, once: in a browser signed in to eBay as the shop's account, open "
              "{link} and click Agree and Continue. The permission lasts about 18 months.")
HOW_LINK = "open this link signed in to eBay as the shop's account and click Agree and Continue: {link}"
HOW_ASK = ("ask Claude Code for the eBay consent link, open it signed in to eBay as the shop's account and click Agree "
           "and Continue")
PAGE_CONNECTED = "eBay is connected. You can close this page."
PAGE_DECLINED = "Not connected — nothing was changed."
PAGE_STATE = ("This link has expired or was already used — nothing was changed. Open the link from the agent's message "
              "again.")
OTHER_ACCOUNT = ("eBay's consent was given with another eBay account than the shop's — nothing was changed. Sign in to "
                 "eBay as the shop's account and open the link again.")
PAGE_PRIVACY = ("This is a private tool used by one eBay seller to list and manage their own items. It reads and changes "
                "only that seller's own listings and reads their own orders. It stores no eBay user's personal data: no "
                "buyer names, usernames or addresses. Account-deletion notices from eBay are acknowledged; there is "
                "nothing to delete.")
PAGE_FAILED = "It didn't work — eBay didn't accept the sign-in ({what}). Nothing was changed; open the link again."


class NotSetUp(ApiError):
    """EBAY_CLIENT_ID is unset: the eBay side is off."""
    status = 404


class Incomplete(ApiError):
    """A setting eBay needs is missing or unusable (named, never its value)."""
    status = 503


class NotConnected(ApiError):
    """No usable consent: none given yet, or eBay refuses it (the human step)."""
    status = 409


class Unreachable(ApiError):
    """eBay didn't answer, or not with what was asked."""
    status = 503


class NotBuilt(ApiError):
    """EBAY_API=inventory: the Inventory API's side isn't built yet (after the sandbox spike)."""
    status = 501


class Refused(Exception):
    """eBay answered no; the message is the HTTP status or eBay's own code, never a body."""


# --- settings ------------------------------------------------------------------------------------------------------

def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def configured() -> bool:
    """EBAY_CLIENT_ID is set: without it nothing here calls eBay."""
    return bool(_env("EBAY_CLIENT_ID"))


def environment() -> str:
    return "sandbox" if _env("EBAY_ENV").lower() == "sandbox" else "production"


def scopes() -> str:
    return " ".join(_env("EBAY_SCOPES").split()) or DEFAULT_SCOPES


def listing_api() -> str:
    return "inventory" if _env("EBAY_API").lower() == "inventory" else "listing"


def _get(db: Database, key: str) -> dict | None:
    return core._loads(core.setting(db, key))


def _put(db: Database, key: str, value: dict) -> None:
    core.put_setting(db, key, json.dumps(value, sort_keys=True))


def _clock(now: datetime | None) -> datetime:
    return util.aware(now) if now is not None else util.utcnow()


def _long_day(moment: datetime) -> str:
    """"May 11, 2028" in the owner's zone."""
    day = deadlines.local(moment).date()
    return f"{MONTHS[day.month - 1]} {day.day}, {day.year}"


def _say(now: datetime, mode: str | None, text: str) -> None:
    """One private line, by the API's own sending rules (replay: marked; off: nothing; silent at night)."""
    out = core.Outbox(now, mode or util.mode())
    out.ops(text)
    out.flush()


# --- the one way out -----------------------------------------------------------------------------------------------

class _NoRedirect(urlrequest.HTTPRedirectHandler):
    """A redirect is an answer (its status), never followed: a token goes to the eBay address it was meant for only."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urlrequest.build_opener(_NoRedirect)


def http_send(method: str, url: str, headers: dict, body: bytes | None, timeout: float = TIMEOUT) -> tuple[int, bytes]:
    """One request to eBay: (status, body) for any answer, an HTTP error's too; a network failure raises."""
    request = urlrequest.Request(url, data=body, method=method, headers=headers)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return int(response.status), response.read(MAX_ANSWER)
    except urlerror.HTTPError as e:
        try:
            return int(e.code), e.read(MAX_ANSWER)
        except OSError:
            return int(e.code), b""


SENDER = http_send


def _send(method: str, url: str, headers: dict, body: bytes | None = None) -> tuple[int, bytes]:
    try:
        status, data = SENDER(method, url, headers, body, TIMEOUT)
    except Exception as e:  # noqa: BLE001 — its type only: a URL error's text can carry the address and its query
        raise Unreachable(f"eBay couldn't be reached ({type(e).__name__})") from None
    return int(status), data or b""


def _json(data: bytes) -> dict:
    try:
        value = json.loads(data.decode("utf-8")) if data else {}
    except (ValueError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _code(value: object, limit: int = 60) -> str:
    """One of eBay's codes as it may be said: letters, digits, _ and - only (never a sentence, never a value)."""
    return re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))[:limit]


# --- the tokens, sealed --------------------------------------------------------------------------------------------

def _cipher():
    """Fernet over EBAY_TOKEN_KEY (the `cryptography` package, imported only here: a deployment without eBay, and the
    tests of everything else, never need it)."""
    key = _env("EBAY_TOKEN_KEY")
    if not key:
        raise Incomplete("EBAY_TOKEN_KEY is not set")
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise Incomplete("the cryptography package is not installed") from None
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError):
        raise Incomplete("EBAY_TOKEN_KEY is not a Fernet key") from None


def seal(text: str) -> str:
    return _cipher().encrypt(text.encode("utf-8")).decode("ascii")


def unseal(sealed: object) -> str | None:
    """The clear text, or None for a value another key sealed (or a damaged one): as if nothing were stored."""
    if not isinstance(sealed, str) or not sealed:
        return None
    cipher = _cipher()
    try:
        return cipher.decrypt(sealed.encode("ascii")).decode("utf-8")
    except Exception:  # noqa: BLE001 — InvalidToken and friends: nothing to say about the value
        return None


def _token_call(form: dict, path: str = "") -> tuple[int, dict]:
    """One POST to eBay's token endpoint (Basic client id : client secret, a form body): (status, its JSON). `path`:
    "/introspect" for the question whose token it is."""
    client, secret = _env("EBAY_CLIENT_ID"), _env("EBAY_CLIENT_SECRET")
    if not client:
        raise NotSetUp("eBay is not set up")
    if not secret:
        raise Incomplete("EBAY_CLIENT_SECRET is not set")
    basic = base64.b64encode(f"{client}:{secret}".encode("utf-8")).decode("ascii")
    status, data = _send("POST", TOKEN_URL[environment()] + path,
                         {"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic}"},
                         urlencode(form).encode("ascii"))
    return status, _json(data)


def _consent(db: Database) -> dict | None:
    """The stored consent of this environment (a sandbox one is nothing to production)."""
    consent = _get(db, CONSENT)
    return consent if consent and consent.get("environment") == environment() and consent.get("refresh") else None


def connected(db: Database, now: datetime | None = None) -> bool:
    """A consent is stored, not ended, and eBay hasn't refused it."""
    now = _clock(now)
    consent = _consent(db) if configured() else None
    if consent is None or _get(db, NEEDS):
        return False
    ends = parse_time(consent.get("expires_at"))
    return ends is None or ends > now


def _refresh_token(db: Database, now: datetime, mode: str | None) -> str:
    """The refresh token to mint with — or NotConnected: none stored, refused before (no call until a new consent or
    the hourly look), ended, or sealed by another key (both: the human step)."""
    consent = _consent(db)
    if consent is None or _get(db, NEEDS):
        raise NotConnected("not connected")
    ends = parse_time(consent.get("expires_at"))
    if ends is not None and ends <= now:
        needs_consent(db, now, mode, "expired")
        raise NotConnected("not connected")
    refresh = unseal(consent.get("refresh"))
    if not refresh:
        needs_consent(db, now, mode, "unreadable")
        raise NotConnected("not connected")
    return refresh


def _mint_user(db: Database, now: datetime, mode: str | None, refresh: str) -> dict:
    status, answer = _token_call({"grant_type": "refresh_token", "refresh_token": refresh})
    if status in (400, 401):                    # eBay's own no: revoked, ended, or its password changed
        needs_consent(db, now, mode, f"HTTP {status} {_code(answer.get('error'))}".strip())
        raise NotConnected("not connected")
    if status != 200 or not answer.get("access_token"):
        raise Unreachable(f"eBay's token endpoint answered HTTP {status}")
    return answer


def _keep_token(db: Database, kind: str, answer: dict, now: datetime) -> dict:
    try:
        life = max(int(answer.get("expires_in") or 0), 0)
    except (TypeError, ValueError):
        life = 0
    ends = now + timedelta(seconds=life or 7200)
    _put(db, TOKEN + kind, {"token": seal(str(answer["access_token"])), "expires_at": iso(ends),
                            "environment": environment()})
    return {"access_token": str(answer["access_token"]), "expires_at": iso(ends), "environment": environment()}


def access_token(db: Database, kind: str = "user", now: datetime | None = None, mode: str | None = None) -> dict:
    """{"access_token", "expires_at", "environment"} — `user`: minted from the stored refresh token; `app`: the
    client-credentials grant. Kept sealed until 5 minutes before its end, then minted again. NotSetUp (404) without
    EBAY_CLIENT_ID, NotConnected (409) without a usable consent, Unreachable (503) when eBay didn't give one."""
    now = _clock(now)
    if kind not in ("user", "app"):
        raise BadRequest("kind is user or app")
    if not configured():
        raise NotSetUp("eBay is not set up")
    cached = _get(db, TOKEN + kind)
    if cached and cached.get("environment") == environment() and (kind == "app" or connected(db, now)):
        ends = parse_time(cached.get("expires_at"))
        if ends is not None and now < ends - TOKEN_MARGIN:
            token = unseal(cached.get("token"))
            if token:
                return {"access_token": token, "expires_at": iso(ends), "environment": environment()}
    if kind == "app":
        status, answer = _token_call({"grant_type": "client_credentials", "scope": APP_SCOPE})
        if status != 200 or not answer.get("access_token"):
            raise Unreachable(f"eBay's token endpoint answered HTTP {status}")
    else:
        answer = _mint_user(db, now, mode, _refresh_token(db, now, mode))
    return _keep_token(db, kind, answer, now)


# --- token trouble: a human step (private chat only) -------------------------------------------------------------------

def _how() -> str:
    link = _env("EBAY_START_LINK")
    return HOW_LINK.format(link=link) if link else HOW_ASK


def _since(moment: datetime | None, now: datetime) -> str:
    from .timers import clock                   # "7:12 PM", with its day when that isn't today
    return clock(moment, now)


def needs_consent(db: Database, now: datetime, mode: str | None = None, why: str = "refused") -> bool:
    """eBay refuses the stored consent: recorded once, with ONE private message (the link to click Agree again); the
    cached user token goes. True when it was said now — False when it was known already."""
    value = json.dumps({"since": iso(now), "said_at": iso(now), "tried_at": iso(now), "why": why[:80]}, sort_keys=True)
    if not db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING", (NEEDS, value)):
        return False
    core.drop_setting(db, TOKEN + "user")
    _say(now, mode, ENDED_LINE.format(since=_since(now, now), how=_how()))
    return True


def _first_link(db: Database, now: datetime, mode: str) -> bool:
    """The keyset is in and never connected: the consent link, ONE private message (once per keyset; `provision.py
    ebay show` prints it as well). Its key opens only the consent's start (a function key of its own)."""
    link, keyset = _env("EBAY_START_LINK"), _env("EBAY_CLIENT_ID")
    if not link or core.setting(db, FIRST_LINK) == keyset:
        return False
    if not db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = "
                      "excluded.value WHERE settings.value <> excluded.value", (FIRST_LINK, keyset)):
        return False
    _say(now, mode, FIRST_LINE.format(link=link))
    return True


def _swap(db: Database, key: str, old: str, new: dict) -> bool:
    """The setting replaced only if it still is what was read (two timer runs at once say a thing once)."""
    return bool(db.execute("UPDATE settings SET value = ? WHERE key = ? AND value = ?",
                           (json.dumps(new, sort_keys=True), key, old)))


def consent_watch(db: Database, now: datetime | None = None, mode: str | None = None) -> dict:
    """The 15-minute look at the consent. Refused: tried again by itself once an hour (works → cleared, one line), and
    its message again every 12 hours while it lasts. Not refused: 30, 14, 7, 3, 2 and 1 days before the permission ends
    ONE message each (a look that comes late says one, for the nearest); ended → the refused path. {"state", "said"}."""
    now, mode = _clock(now), mode or util.mode()
    if not configured() or mode == "off":
        return {"state": "off", "said": None}
    consent = _consent(db)
    if consent is None:
        return {"state": "not connected", "said": "link" if _first_link(db, now, mode) else None}
    ends = parse_time(consent.get("expires_at"))
    raw = core.setting(db, NEEDS)
    needs = core._loads(raw)
    if needs and raw:
        tried = parse_time(needs.get("tried_at"))
        if (ends is None or ends > now) and (tried is None or now - tried >= RETRY_REFUSED):
            if _swap(db, NEEDS, raw, {**needs, "tried_at": iso(now)}) and _works_again(db, now, mode):
                return {"state": "connected", "said": "works again"}
            raw = core.setting(db, NEEDS) or raw
            needs = core._loads(raw) or needs
        said = parse_time(needs.get("said_at"))
        if said is None or now - said >= REMIND_EVERY:
            if _swap(db, NEEDS, raw, {**needs, "said_at": iso(now)}):
                _say(now, mode, ENDED_LINE.format(since=_since(parse_time(needs.get("since")), now), how=_how()))
                return {"state": "refused", "said": "reminder"}
        return {"state": "refused", "said": None}
    if ends is None:
        return {"state": "connected", "said": None}
    if ends <= now:
        return {"state": "refused", "said": "ended" if needs_consent(db, now, mode, "expired") else None}
    due = [days for days in EXPIRY_DAYS if ends - now <= timedelta(days=days)]
    if not due:
        return {"state": "connected", "said": None}
    raw = core.setting(db, EXPIRY_SAID)
    record = core._loads(raw) or {}
    said = set(record.get("said") or []) if record.get("expires_at") == consent.get("expires_at") else set()
    if set(due) <= said:
        return {"state": "expiring", "said": None}
    new = {"expires_at": consent.get("expires_at"), "said": sorted(said | set(due), reverse=True)}
    if raw is None:
        first = db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING",
                           (EXPIRY_SAID, json.dumps(new, sort_keys=True)))
    else:
        first = _swap(db, EXPIRY_SAID, raw, new)
    if not first:
        return {"state": "expiring", "said": None}
    _say(now, mode, EXPIRING_LINE.format(day=_long_day(ends), how=_how()))
    return {"state": "expiring", "said": min(due)}


def _works_again(db: Database, now: datetime, mode: str) -> bool:
    """The refused consent tried once more (a setting fixed in Azure needs no click): True when eBay takes it."""
    consent = _consent(db)
    refresh = unseal(consent.get("refresh")) if consent else None
    if not refresh:
        return False
    try:
        status, answer = _token_call({"grant_type": "refresh_token", "refresh_token": refresh})
    except ApiError:
        return False
    if status != 200 or not answer.get("access_token"):
        return False
    with db.tx():
        core.drop_setting(db, NEEDS)
        _keep_token(db, "user", answer, now)
    _say(now, mode, WORKS_AGAIN_LINE)
    return True


# --- the account-deletion endpoint ---------------------------------------------------------------------------------

def challenge(code: str) -> dict:
    """eBay's endpoint check: {"challengeResponse": SHA-256 hex of challengeCode + verificationToken + endpoint}, in
    that order — the endpoint being EBAY_DELETION_ENDPOINT, the exact string registered with eBay (never one rebuilt
    from the request)."""
    code = (code or "").strip()
    if not code or len(code) > 512:
        raise BadRequest("challenge_code is required")
    token, endpoint = _env("EBAY_VERIFICATION_TOKEN"), _env("EBAY_DELETION_ENDPOINT")
    if not token or not endpoint:
        raise Incomplete("the account-deletion endpoint is not set up")
    digest = hashlib.sha256()
    for part in (code, token, endpoint):
        digest.update(part.encode("utf-8"))
    return {"challengeResponse": digest.hexdigest()}


# --- the consent ---------------------------------------------------------------------------------------------------

def page(text: str, title: str = "eBay and the thrift agent") -> str:
    """A small plain page for the owner's browser."""
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="robots" content="noindex, nofollow">\n'
            f"<title>{html.escape(title)}</title>\n</head>\n<body>\n<p>{html.escape(text)}</p>\n</body>\n</html>\n")


def oauth_start(db: Database, now: datetime | None = None) -> str:
    """eBay's consent page for the shop's account: its address, carrying a random single-use `state` kept 10 minutes."""
    now = _clock(now)
    if not configured():
        raise NotSetUp("eBay is not set up")
    for name in ("EBAY_CLIENT_SECRET", "EBAY_RUNAME"):
        if not _env(name):
            raise Incomplete(f"{name} is not set")
    _cipher()                                   # a missing or bad token key says so now, not after she agreed
    state = secrets.token_urlsafe(24)
    with db.tx():
        for row in db.query("SELECT key, value FROM settings WHERE key LIKE ?", (STATE + "%",)):
            ends = parse_time(row["value"])
            if ends is None or ends <= now:
                core.drop_setting(db, row["key"])
        core.put_setting(db, STATE + state, iso(now + STATE_FOR))
    query = urlencode({"client_id": _env("EBAY_CLIENT_ID"), "redirect_uri": _env("EBAY_RUNAME"),
                       "response_type": "code", "scope": scopes(), "state": state}, quote_via=quote)
    return f"{AUTH_URL[environment()]}?{query}"


def _take_state(db: Database, state: object, now: datetime) -> bool:
    """The state is one of ours, unused and not ended; it is used up either way."""
    if not isinstance(state, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", state):
        return False
    ends = parse_time(core.setting(db, STATE + state))
    if not db.execute("DELETE FROM settings WHERE key = ?", (STATE + state,)):
        return False
    return ends is not None and now < ends


def oauth_callback(db: Database, query: dict, now: datetime | None = None, mode: str | None = None) -> tuple[int, str]:
    """eBay's redirect after Agree (?code=…&state=…): the code exchanged, the refresh token stored sealed with its end
    and scopes, every "consent needed" state cleared, ONE private line, a small page. An unknown, ended or used state:
    a page saying so, nothing exchanged. A refused exchange: a page, and one private line with the HTTP status and
    eBay's error code only. (status, the page)."""
    now = _clock(now)
    if not configured():
        return 404, page("eBay is not set up.")
    if not _take_state(db, query.get("state"), now):
        return 400, page(PAGE_STATE)
    code = query.get("code")
    if not isinstance(code, str) or not code.strip() or len(code) > 4096:
        return 400, page(PAGE_FAILED.format(what="no code came with it"))
    try:
        status, answer = _token_call({"grant_type": "authorization_code", "code": code.strip(),
                                      "redirect_uri": _env("EBAY_RUNAME")})
        what = f"HTTP {status} {_code(answer.get('error'))}".strip()
        refresh, access = answer.get("refresh_token"), answer.get("access_token")
        if status == 200 and refresh and access and not _shops_account(str(access)):
            # the consent must be the SHOP's: another account's token is never stored
            _say(now, mode, "⚠️ " + OTHER_ACCOUNT)
            return 409, page(OTHER_ACCOUNT)
        if status == 200 and refresh and access:
            try:
                life = int(answer.get("refresh_token_expires_in") or 0)
            except (TypeError, ValueError):
                life = 0
            ends = now + timedelta(seconds=life if life > 0 else REFRESH_LIFE)
            sealed = seal(str(refresh))
            with db.tx():
                _put(db, CONSENT, {"refresh": sealed, "expires_at": iso(ends), "scopes": scopes(),
                                   "connected_at": iso(now), "environment": environment()})
                _keep_token(db, "user", answer, now)
                for key in (NEEDS, EXPIRY_SAID):
                    core.drop_setting(db, key)
            _say(now, mode, CONNECTED_LINE.format(day=_long_day(ends)))
            return 200, page(PAGE_CONNECTED)
        if status == 200:
            what = "HTTP 200 without a token"
    except ApiError as e:
        what = str(e)
    _say(now, mode, f"⚠️ eBay's consent didn't finish: {what}. Nothing was changed; the link can be opened again.")
    return 502, page(PAGE_FAILED.format(what=what))


def _shops_account(access: str) -> bool:
    """With EBAY_SELLER set (the shop's eBay username): eBay is asked whose token this is (token introspection) and
    False is the answer only when it NAMES another account (case aside). Best effort — the answer's shape is documented
    but untested: unset, eBay away, or no username in the answer → accepted as before. Neither name is ever said."""
    seller = _env("EBAY_SELLER").casefold()
    if not seller:
        return True
    try:
        status, answer = _token_call({"token": access, "token_type_hint": "access_token"}, "/introspect")
    except ApiError:
        return True
    name = answer.get("username") if status == 200 else None
    return not isinstance(name, str) or not name.strip() or name.strip().casefold() == seller


def status(db: Database, now: datetime | None = None) -> dict:
    """GET /ebay/status: where eBay stands, without a secret."""
    now = _clock(now)
    consent = _consent(db) if configured() else None
    needs, error = _get(db, NEEDS), _get(db, ORDERS_ERROR)
    return {"configured": configured(), "connected": connected(db, now), "environment": environment(),
            "consent_expires_at": consent.get("expires_at") if consent else None,
            "needs_consent_since": needs.get("since") if needs else None,
            "orders_read_at": core.setting(db, ORDERS_READ),
            "orders_error": error.get("what") if error else None,
            "ended": len(_ended_rows(db, now))}


# --- is eBay on? ---------------------------------------------------------------------------------------------------

def beat(db: Database) -> dict:
    """The Mac's last word on eBay (its heartbeat's `ebay`): {"on", "live", "free_used", "free_month", "fees_checked"}."""
    row = db.one("SELECT info FROM heartbeats WHERE source = 'mac'")
    info = core._loads(row["info"]) if row and row["info"] else None
    got = info.get(SITE) if info else None
    return got if isinstance(got, dict) else {}


def on(db: Database) -> bool:
    """eBay is one of the sites: the Mac's heartbeat says so (`ebay.on`) — or, when it says nothing of eBay, the mirror
    holds an eBay listing. While it is off the three sites are the sites, exactly as before."""
    said = beat(db).get("on")
    if isinstance(said, bool):
        return said
    return db.one("SELECT 1 AS n FROM listings WHERE marketplace = ? LIMIT 1", (SITE,)) is not None


def sites(db: Database) -> tuple[str, ...]:
    """The marketplaces an item should be live on now: the three, and eBay while it is on."""
    return util.SITES if on(db) else util.CORE_SITES


def on_since(db: Database, now: datetime) -> datetime | None:
    """When eBay was first seen on (remembered; forgotten when it goes off): the 2-hour net expects an eBay listing of
    the items priced from then — never of the ones priced before it began."""
    if not on(db):
        core.drop_setting(db, ON_SINCE)
        return None
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING", (ON_SINCE, iso(now)))
    return parse_time(core.setting(db, ON_SINCE)) or now


def site_mark(db: Database, now: datetime) -> str:
    """eBay in the 9 PM line: "eBay ✓" when connected with no take-down of its own left open, else what is wrong."""
    if not configured():
        wrong = "not set up"
    elif _get(db, NEEDS):
        wrong = "its permission ended"
    elif not connected(db, now):
        wrong = "not connected yet"
    elif db.one("SELECT 1 AS n FROM delist_tasks t LEFT JOIN listings l ON l.item_id = t.item_id AND l.marketplace = "
                "t.marketplace WHERE t.marketplace = ? AND COALESCE(t.kind, 'delist') <> 'repost' AND ((t.status = "
                "'failed' AND l.status = 'posted') OR (t.status IN ('pending', 'running') AND COALESCE(t.attempts, 0) "
                "> 0)) LIMIT 1", (SITE,)):
        wrong = "a take-down failed"
    elif (_get(db, ORDERS_ERROR) or {}).get("said"):
        wrong = "its orders aren't read"
    else:
        return "eBay ✓"
    return f"eBay ✗ {wrong}"


def free_line(db: Database, now: datetime) -> str:
    """" eBay: 12 of 250 free listings used this month." from the Mac's heartbeat, when it says so for this month."""
    info = beat(db)
    used = info.get("free_used")
    if isinstance(used, bool) or not isinstance(used, int) or used < 0:
        return ""
    month = str(info.get("free_month") or "")
    if month and month != deadlines.local(now).strftime("%Y-%m"):
        return ""
    return f" eBay: {used} of {FREE_LISTINGS} free listings used this month."


# --- the orders (read only) ------------------------------------------------------------------------------------------

def _stamp(moment: datetime) -> str:
    return util.aware(moment).astimezone(util.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _ship_day(value: object) -> str | None:
    """eBay's ship-by moment as its calendar day."""
    moment = parse_time(value)
    return moment.astimezone(ZoneInfo(DAY_ZONE)).date().isoformat() if moment else None


def _line_status(order: dict, line: dict) -> tuple[str, str]:
    """(our status, its words) of one order line — from the order's cancel, payment and fulfillment states."""
    cancel = order.get("cancelStatus") if isinstance(order.get("cancelStatus"), dict) else {}
    if str(cancel.get("cancelState") or "").upper() == "CANCELED":
        return "cancelled", "Cancelled"
    if str(order.get("orderPaymentStatus") or "").upper() == "FULLY_REFUNDED":
        return "returned", "Refunded"
    state = str(line.get("lineItemFulfillmentStatus") or order.get("orderFulfillmentStatus") or "").upper()
    return ("shipped", "Shipped") if state == "FULFILLED" else ("to_ship", "Awaiting shipment")


def order_entries(order: object) -> list[dict]:
    """One eBay order as the order sweeps' entries, one per line (a single line's id is the order's own). ONLY these
    fields are read — the order's id, creation date and states; the line's id, SKU, listing id, title, price and
    ship-by — never `buyer`, `buyerCheckoutNotes`, `fulfillmentStartInstructions` (the ship-to) or anything else."""
    if not isinstance(order, dict):
        return []
    order_id = _code(order.get("orderId"), 40)
    lines = [line for line in order.get("lineItems") or [] if isinstance(line, dict)] \
        if isinstance(order.get("lineItems"), list) else []
    if not order_id or not lines:
        return []
    entries = []
    for line in lines:
        state, words = _line_status(order, line)
        cost = line.get("lineItemCost") if isinstance(line.get("lineItemCost"), dict) else {}
        ship = line.get("lineItemFulfillmentInstructions")
        entries.append({
            "order_id": order_id if len(lines) == 1 else f"{order_id}:{_code(line.get('lineItemId'), 22)}",
            "status": state, "status_text": words, "title": line.get("title"), "price": cost.get("value"),
            "sold_at": order.get("creationDate"), "listing_id": _code(line.get("legacyItemId"), 40) or None,
            "sku": line.get("sku"),
            "ship_by": _ship_day(ship.get("shipByDate")) if isinstance(ship, dict) else None})
    return entries


def _fetch_orders(token: str, since: datetime) -> tuple[list[dict], bool]:
    """Every order modified since `since`, page by page (eBay's own `next`, followed only on eBay's own address):
    (the entries, whether the last page was reached)."""
    base = ORDERS_URL[environment()]
    url = f"{base}?filter={quote(f'lastmodifieddate:[{_stamp(since)}..]', safe=':.')}&limit={ORDERS_LIMIT}"
    entries: list[dict] = []
    for _ in range(ORDERS_PAGES):
        status, data = _send("GET", url, {"Authorization": f"Bearer {token}", "Accept": "application/json"})
        if status != 200:
            raise Refused(f"HTTP {status}")
        answer = _json(data)
        for order in answer.get("orders") or []:
            entries += order_entries(order)
        url = answer.get("next")
        if not isinstance(url, str) or not url.startswith(base):
            return entries, True
    return entries, False


def read_orders(db: Database, now: datetime | None = None, mode: str | None = None) -> dict:
    """The 5-minute read of eBay's orders (see the module): from 15 minutes before the last successful read (the first
    one: 3 days back), each line through orders.sweep. An order read again changes nothing. A read that fails is tried
    again in 5 minutes; failing for an hour → ONE private line, and one when it works again."""
    now, mode = _clock(now), mode or util.mode()
    if mode == "off" or not configured() or _consent(db) is None:
        return {"read": False}
    last = parse_time(core.setting(db, ORDERS_READ))
    since = max(last - ORDERS_OVERLAP, now - ORDERS_BACK) if last else now - ORDERS_FIRST
    try:
        token = access_token(db, "user", now, mode)["access_token"]
        try:
            entries, whole = _fetch_orders(token, since)
        except Refused as e:
            if str(e) == "HTTP 401":            # the token eBay just gave is refused: the next read mints another
                core.drop_setting(db, TOKEN + "user")
            raise
    except NotConnected:
        return {"read": False, "why": "not connected"}
    except (ApiError, Refused) as e:
        _orders_failed(db, now, mode, str(e))
        return {"read": False, "why": str(e)}
    results = []
    for start in range(0, len(entries), ORDERS_CHUNK):
        results += orders.sweep(db, SITE, entries[start:start + ORDERS_CHUNK], now, now, mode)
    if not entries:
        orders.sweep(db, SITE, [], now, now, mode)             # the read itself is the news: "orders read at …"
    with db.tx():
        if whole:
            core.put_setting(db, ORDERS_READ, iso(now))
        # an order still to ship that this read didn't bring is unchanged on eBay: it was looked at now
        db.execute("UPDATE orders SET last_seen = ? WHERE marketplace = ? AND status = 'to_ship' AND last_seen < ?",
                   (iso(now), SITE, iso(now)))
    _orders_ok(db, now, mode)
    return {"read": True, "orders": len(entries),
            "recorded": sum(1 for r in results if str(r.get("action", "")).startswith("recorded"))}


def _orders_failed(db: Database, now: datetime, mode: str, what: str) -> None:
    error = _get(db, ORDERS_ERROR) or {"since": iso(now)}
    error.update(at=iso(now), what=what[:120])
    began = parse_time(error.get("since")) or now
    say = not error.get("said") and now - began >= ORDERS_DOWN
    if say:
        error["said"] = iso(now)
    _put(db, ORDERS_ERROR, error)
    if say:
        _say(now, mode, f"⚠️ eBay's orders couldn't be read since {_since(began, now)} ({error['what']}) — a sale on eBay "
                        "isn't seen until it works again; tried every 5 minutes.")


def _orders_ok(db: Database, now: datetime, mode: str) -> None:
    error = _get(db, ORDERS_ERROR)
    if error is None:
        return
    core.drop_setting(db, ORDERS_ERROR)
    if error.get("said"):
        _say(now, mode, "✓ eBay's orders are read again.")


# --- ending a listing ------------------------------------------------------------------------------------------------

_LISTING = ("listingId items { sku itemTerms { availability { quantityAvailable quantitySold } } } "
            "listingTerms { lifecycleTerms { listingStatus actualEndAt } }")
Q_LISTING = ("query SellerListing($input: SellerListingsInput!) { sellerListings(input: $input) { __typename "
             "... on ListingsSuccess { listings { __typename "
             f"... on ListingSuccess {{ listingId listing {{ {_LISTING} }} }} "
             f"... on ListingPartialSuccess {{ listingId listing {{ {_LISTING} }} errors {{ errorCode }} }} }} }} "
             "... on ListingIdsMaxLimitError { errorCode } } }")


def _search_query(name: str, kind: str, field: str) -> str:
    return (f"query {name}($search: {kind}) {{ sellerListingSearch {{ {field}(searchInput: $search) {{ __typename "
            f"... on SellerListings {{ listings {{ listingId listing {{ {_LISTING} }} }} }} "
            "... on SellerListingSearchInvalidInput { errorCode } } } }")


Q_ACTIVE = _search_query("ActiveBySku", "SellerActiveListingSearchInput", "activeListings")
Q_ENDED = _search_query("EndedBySku", "SellerEndedListingSearchInput", "endedListings")
M_END =("mutation EndListing($input: EndListingInput!) { endListing(input: $input) { __typename "
         "... on EndListingSuccess { listing { listingId listingTerms { lifecycleTerms { listingStatus actualEndAt } } } } "
         "... on EndListingValidationError { errorCode violations { violationCode } } } }")
END_REASON = "NOT_AVAILABLE"             # "the seller no longer wishes to sell the item": it sold elsewhere
LIVE_STATES = ("ACTIVE", "SCHEDULED")


def _graphql(token: str, query: str, variables: dict) -> dict:
    """One GraphQL call (inputs only through variables); its `data`. HTTP 200 can carry `errors`: refused too."""
    status, data = _send("POST", GRAPHQL_URL[environment()],
                         {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
                          "Content-Type": "application/json"},
                         json.dumps({"query": query, "variables": variables}).encode("utf-8"))
    if status != 200:
        raise Refused(f"HTTP {status}")
    answer = _json(data)
    errors = answer.get("errors")
    if errors:
        first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
        ext = first.get("extensions") if isinstance(first.get("extensions"), dict) else {}
        raise Refused(f"GraphQL error {_code(ext.get('errorCode') or ext.get('code')) or 'unnamed'}")
    if not isinstance(answer.get("data"), dict):
        raise Refused("an answer without data")
    return answer["data"]


def _listing(node: object) -> dict | None:
    """What a take-down needs of a listing: its id, its SKUs, its state, how many sold."""
    if not isinstance(node, dict) or not node.get("listingId"):
        return None
    items = [i for i in node.get("items") or [] if isinstance(i, dict)] if isinstance(node.get("items"), list) else []
    terms = node.get("listingTerms") if isinstance(node.get("listingTerms"), dict) else {}
    life = terms.get("lifecycleTerms") if isinstance(terms.get("lifecycleTerms"), dict) else {}
    sold = 0
    for item in items:
        have = (item.get("itemTerms") or {}).get("availability") if isinstance(item.get("itemTerms"), dict) else None
        count = have.get("quantitySold") if isinstance(have, dict) else None
        sold += count if isinstance(count, int) and not isinstance(count, bool) else 0
    return {"id": str(node["listingId"]), "skus": [str(i["sku"]) for i in items if i.get("sku")],
            "state": str(life.get("listingStatus") or "").upper(), "sold": sold}


def read_listing(token: str, listing_id: str) -> dict | None:
    """The listing with this id as eBay shows it to its seller; None when eBay has none."""
    data = _graphql(token, Q_LISTING, {"input": {"listings": [{"listingId": str(listing_id)}]}})
    out = data.get("sellerListings") if isinstance(data.get("sellerListings"), dict) else {}
    if out.get("__typename") != "ListingsSuccess":
        raise Refused(f"eBay didn't show the listing ({_code(out.get('errorCode') or out.get('__typename')) or 'no answer'})")
    for entry in out.get("listings") or []:
        found = _listing(entry.get("listing")) if isinstance(entry, dict) else None
        if found:
            return found
    return None


def search_listings(token: str, sku: str, ended: bool = False) -> list[dict]:
    """The seller's active (or ended) listings carrying exactly this SKU."""
    data = _graphql(token, Q_ENDED if ended else Q_ACTIVE,
                    {"search": {"filters": {"skus": [sku], "marketplaces": [MARKETPLACE]},
                                "pagination": {"maxPageSize": 20}}})
    search = data.get("sellerListingSearch") if isinstance(data.get("sellerListingSearch"), dict) else {}
    out = search.get("endedListings" if ended else "activeListings")
    out = out if isinstance(out, dict) else {}
    if out.get("__typename") != "SellerListings":
        raise Refused(f"eBay didn't search its listings ({_code(out.get('errorCode') or out.get('__typename')) or 'no answer'})")
    found = []
    for entry in out.get("listings") or []:
        if not isinstance(entry, dict):
            continue
        listing = _listing(entry.get("listing")) or (read_listing(token, entry["listingId"]) if entry.get("listingId")
                                                     else None)
        if listing:
            found.append(listing)
    return found


def end_listing(token: str, listing_id: str) -> None:
    """endListing, once. Refused when eBay doesn't answer EndListingSuccess."""
    data = _graphql(token, M_END, {"input": {"listingIdentifier": {"listingId": str(listing_id)},
                                             "endListingReason": END_REASON}})
    out = data.get("endListing") if isinstance(data.get("endListing"), dict) else {}
    if out.get("__typename") == "EndListingSuccess":
        return
    codes = [_code(v.get("violationCode")) for v in out.get("violations") or [] if isinstance(v, dict)]
    said = ", ".join(c for c in [_code(out.get("errorCode")), *codes[:3]] if c)
    raise Refused(f"eBay refused to end it ({said or 'no reason given'})")


def take_down(db: Database, task: dict, now: datetime, mode: str | None = None) -> tuple[str, str | None, str | None]:
    """One eBay take-down: (result, error, evidence) for core.task_result. The listing is found (the id our mirror
    holds; the item's id as its SKU among the active ones unless that one is live — a relisting the mirror doesn't
    hold yet must come down too; then among the ended ones), READ, and must be OURS: its SKU is the item's id; a
    listing with another SKU or none is never ended ("failed"). Live → ended once ("done"); ended already with a sale
    → "sold" (sold twice); ended already, nothing sold → "done"; none on eBay → "not_found"."""
    if listing_api() != "listing":              # the one seam: the Inventory API's withdraw comes after the sandbox spike
        raise NotBuilt("ending an eBay listing made with the Inventory API isn't built yet (EBAY_API=inventory)")
    item = str(task["item_id"])
    token = access_token(db, "user", now, mode)["access_token"]
    row = db.one("SELECT listing_id FROM listings WHERE item_id = ? AND marketplace = ?", (item, SITE))
    held = str(row["listing_id"]).strip() if row and row["listing_id"] else ""
    found: dict[str, dict] = {}

    def ours_in(states: tuple[str, ...] | None = None) -> bool:
        return any(item in listing["skus"] and (states is None or listing["state"] in states)
                   for listing in found.values())
    try:
        if held and (listing := read_listing(token, held)):
            found[listing["id"]] = listing
        if not ours_in(LIVE_STATES):
            for listing in search_listings(token, item):
                found.setdefault(listing["id"], listing)
        if not ours_in():
            for listing in search_listings(token, item, ended=True):
                found.setdefault(listing["id"], listing)
        if not found:
            return "not_found", "eBay has no listing with this item's SKU", None
        ours = [listing for listing in found.values() if item in listing["skus"]]
        if not ours:
            return "failed", "the eBay listing our records hold carries another SKU — not ours to end", None
        unknown = [listing for listing in ours if listing["state"] not in (*LIVE_STATES, "ENDED")]
        if unknown:
            return "failed", "eBay didn't say whether the listing is live — nothing ended", None
        live = [listing for listing in ours if listing["state"] in LIVE_STATES]
        for listing in live:
            try:
                end_listing(token, listing["id"])
            except Refused:
                again = read_listing(token, listing["id"])      # ended meanwhile (it sold, or a first try got through)?
                if again is None or again["state"] != "ENDED":
                    raise
                if again["sold"] > 0:
                    return "sold", None, f"eBay listing {again['id']} sold before it could be ended"
        if live:
            return "done", None, "ended on eBay: " + ", ".join(listing["id"] for listing in live)
        if any(listing["sold"] > 0 for listing in ours):
            return "sold", None, "already ended on eBay, with a sale"
        return "done", None, "already ended on eBay"
    except Refused as e:
        if str(e) == "HTTP 401":                # the token eBay just gave is refused: the next try mints another
            core.drop_setting(db, TOKEN + "user")
        raise


# an eBay take-down to do now (two `now` parameters): pending — a failed try waits out its lease_until — or running
# with its lease run out (a run that died)
OPEN_TASKS = ("marketplace = 'ebay' AND COALESCE(kind, 'delist') <> 'repost' AND ((status = 'pending' AND "
              "(lease_until IS NULL OR lease_until <= ?)) OR (status = 'running' AND (lease_until IS NULL OR "
              "lease_until < ?)))")


def run_takedowns(db: Database, now: datetime | None = None, mode: str | None = None) -> list[dict]:
    """Every open eBay take-down, done from here (see the module): leased as the Mac leases its own (running, 10
    minutes), taken down OUTSIDE any transaction, answered through core.task_result — so the mirror's row, the "✓ …
    taken down" line, "⚠️ Sold twice" and, after 3 failed tries, the group's "Couldn't take … down on eBay — please
    mark it sold there." are the existing ones. A failure of any kind (no consent, eBay away, a listing that isn't
    ours) is a failed try: the owner must hear of a listing still up. Live mode only. [{id, item, result}]."""
    now, mode = _clock(now), mode or util.mode()
    if mode != "live" or db.in_transaction:
        return []
    rows = db.query(f"SELECT id, item_id FROM delist_tasks WHERE {OPEN_TASKS} ORDER BY created_at, id LIMIT ?",
                    (iso(now), iso(now), TASKS_A_RUN))
    done = []
    for task in rows:
        if not db.execute(f"UPDATE delist_tasks SET status = 'running', lease_until = ? WHERE id = ? AND {OPEN_TASKS}",
                          (iso(now + core.LEASE), task["id"], iso(now), iso(now))):
            continue                                              # another run has it
        try:
            result, error, evidence = take_down(db, task, now, mode)
        except (ApiError, Refused) as e:
            result, error, evidence = "failed", str(e), None
        except Exception as e:  # noqa: BLE001 — a failed try like any other; its type and place only
            log.error("ebay: take-down %s failed: %s", task["id"], util.where(e))
            result, error, evidence = "failed", type(e).__name__, None
        answer = core.task_result(db, task["id"], result, error, evidence, now, mode)
        if answer.get("status") == "pending":                     # tried again by the timer, not by the next request
            db.execute("UPDATE delist_tasks SET lease_until = ? WHERE id = ? AND status = 'pending'",
                       (iso(now + RETRY_AFTER), task["id"]))
        done.append({"id": task["id"], "item": task["item_id"], "result": result})
    return done


def kick(db: Database, now: datetime | None = None, mode: str | None = None) -> None:
    """Right after a request that may have made an eBay take-down committed: do it now. Never inside a transaction
    (the timer takes it then), never raising — the request that recorded the sale has succeeded."""
    try:
        now, mode = _clock(now), mode or util.mode()
        if mode != "live" or db.in_transaction:
            return
        if db.one(f"SELECT id FROM delist_tasks WHERE {OPEN_TASKS} LIMIT 1", (iso(now), iso(now))):
            run_takedowns(db, now, mode)
    except Exception as e:  # noqa: BLE001
        log.error("ebay: the take-downs after a request failed: %s", util.where(e))


def _ended_rows(db: Database, now: datetime) -> list[dict]:
    marks = ", ".join("?" * len(ENDED_RESULTS))
    return db.query("SELECT item_id, status, done_at FROM delist_tasks WHERE marketplace = ? AND "
                    f"COALESCE(kind, 'delist') <> 'repost' AND status IN ({marks}) AND done_at >= ? "
                    "ORDER BY done_at, id", (SITE, *ENDED_RESULTS, iso(now - timedelta(days=ENDED_DAYS))))


def ended(db: Database, now: datetime | None = None, mode: str | None = None) -> list[dict]:
    """GET /tasks' `ended`: every eBay take-down finished here in the last 30 days — {"item", "site": "ebay", "at",
    "result": done | not_found | sold} — for the Mac to mark its own row (again and again: it is idempotent there).
    Live only (else [])."""
    now, mode = _clock(now), mode or util.mode()
    if mode != "live":
        return []
    return [{"item": row["item_id"], "site": SITE, "at": row["done_at"], "result": row["status"]}
            for row in _ended_rows(db, now)]


def tick(db: Database, now: datetime | None = None, mode: str | None = None) -> dict:
    """The 5-minute timer's eBay work: the orders read, then every open take-down. One failing never stops the other
    (nor the timer's other work): it is logged as its type and place."""
    now, mode = _clock(now), mode or util.mode()
    out: dict = {}
    for name, job in (("orders", read_orders), ("takedowns", run_takedowns)):
        try:
            out[name] = job(db, now, mode)
        except Exception as e:  # noqa: BLE001
            log.error("ebay: %s failed: %s", name, util.where(e))
            out[name] = "failed"
    return out
