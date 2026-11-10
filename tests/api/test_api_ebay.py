"""WO57 — eBay, thrift-api's side (thrift_api.ebay), part 1: the account-deletion endpoint (eBay's challenge answered,
a notice acknowledged with nothing kept), the owner's consent (the link, the callback, the refresh token stored sealed
in Azure only), the short-lived tokens the Mac asks for, and token trouble as a human step — ONE private message with
the link, reminders, never the group. With no EBAY_CLIENT_ID nothing here calls anyone and every answer is as before.
(The orders, the take-downs and the fourth site in the nets: test_api_ebay_sales.py.)"""
import hashlib
import json
import logging
import os
from datetime import timedelta

import pytest
from apitools import BUYER, IDS, NOW, URLS, call, posh_sale, seed
from ebayfake import (CLIENT, CODE, EBAY_URL, ENDPOINT, LISTING, REFRESH, RUNAME, SECRET, START_LINK, VERIFICATION,
                      connect, everything, start)

from thrift_api import core, ebay, timers
from thrift_api.http import handle

DAY, HOUR, MINUTE = timedelta(days=1), timedelta(hours=1), timedelta(minutes=1)
AGREE = ("open this link signed in to eBay as the shop's account and click Agree and Continue: " + START_LINK)


def wants_a_person(since: str, how: str = AGREE) -> str:
    return (f"🔐 eBay wants a person — its permission for the agent ended (since {since}). 1 minute: {how}. Until then "
            "nothing new goes up on eBay, and a sale elsewhere can't end its eBay listing by itself.")


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-10-01T00:00:00+00:00")


# --- the account-deletion endpoint -----------------------------------------------------------------------------------

def test_the_challenge_is_the_hash_of_the_code_the_token_and_the_registered_endpoint(fake_ebay):
    """SHA-256 hex of challengeCode + verificationToken + endpoint, in that order; the endpoint is the string
    registered with eBay (EBAY_DELETION_ENDPOINT) whatever host the request names; "application/json", made by the
    JSON library (no byte order mark); no key and no database."""
    code = "a8628072-3d33-45ee-9004-bee86830a22d"
    status, headers, body = handle("GET", "/ebay/account-deletion", {"challenge_code": code}, b"", now=NOW,
                                   headers={"Host": "another-host.example", "X-Forwarded-Host": "evil.example"})
    want = hashlib.sha256((code + VERIFICATION + ENDPOINT).encode("utf-8")).hexdigest()
    assert (status, headers["Content-Type"]) == (200, "application/json")
    assert body == json.dumps({"challengeResponse": want}).encode("utf-8") and body.startswith(b'{"challengeResponse"')
    assert len(want) == 64 and want == want.lower()
    assert handle("GET", "/ebay/account-deletion", {"challenge_code": [code]}, b"")[2] == body      # Azure's list form
    assert fake_ebay.calls == []


def test_the_challenge_refuses_what_it_cant_answer(fake_ebay, monkeypatch):
    assert call(None, "GET", "/ebay/account-deletion")[:2] == (400, {"error": "challenge_code is required"})
    assert call(None, "GET", "/ebay/account-deletion", query={"challenge_code": "x" * 600})[0] == 400
    for name in ("EBAY_VERIFICATION_TOKEN", "EBAY_DELETION_ENDPOINT"):
        with monkeypatch.context() as m:
            m.delenv(name)
            assert call(None, "GET", "/ebay/account-deletion", query={"challenge_code": "123"})[:2] == (
                503, {"error": "the account-deletion endpoint is not set up"})


def test_the_endpoint_answers_before_any_ebay_key_exists(monkeypatch):
    """Today: the verification token and the endpoint are in Azure, the keyset isn't — the check passes all the same."""
    monkeypatch.setenv("EBAY_VERIFICATION_TOKEN", VERIFICATION)
    monkeypatch.setenv("EBAY_DELETION_ENDPOINT", ENDPOINT)
    status, out, _ = call(None, "GET", "/ebay/account-deletion", query={"challenge_code": "123"})
    assert (status, out) == (200, {"challengeResponse": hashlib.sha256(("123" + VERIFICATION + ENDPOINT).encode()).hexdigest()})
    assert call(None, "POST", "/ebay/account-deletion", raw=b"{}")[:2] == (200, {})


def test_a_deletion_notice_is_acknowledged_and_nothing_of_it_is_kept(db, fake_ebay, sent, caplog):
    notice = json.dumps({"metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION", "schemaVersion": "1.0"},
                         "notification": {"notificationId": "49feeaeb-4982", "eventDate": "2026-10-08T14:43:59.462Z",
                                          "data": {"username": BUYER[1], "userId": "ma8vp1jySJC",
                                                   "eiasToken": "nY+sHZ2PrBmdj6wVnY+sEZ2PrA2dj6wJnY+gAZGEpwmdj6x9nY+seQ=="}}})
    before = everything(db)
    with caplog.at_level(logging.DEBUG):
        for body in (notice.encode(), b"not json at all \xff\xfe", b"x" * 2_000_000, b""):
            status, headers, out = handle("POST", "/ebay/account-deletion", {}, body, now=NOW,
                                          headers={"X-EBAY-SIGNATURE": "eyJhbGciOiJlY2RzYSJ9"})
            assert (status, json.loads(out)) == (200, {})
    assert everything(db) == before and sent == [] and fake_ebay.calls == []
    assert not any(word in caplog.text for word in (BUYER[1], "ma8vp1jySJC", "eiasToken", "eyJhbGciOiJlY2RzYSJ9"))
    assert call(db, "DELETE", "/ebay/account-deletion")[2]["Allow"] == "GET, POST"


# --- eBay off --------------------------------------------------------------------------------------------------------

def test_without_a_client_id_ebay_is_off_and_nobody_is_called(db, live, monkeypatch, sent):
    """EBAY_CLIENT_ID unset: no call to eBay from any route or timer — even with an eBay listing in the mirror and a
    sale that asks for its take-down — and GET /tasks answers as it always did."""
    calls = []
    monkeypatch.setattr(ebay, "SENDER", lambda *request: calls.append(request) or (200, b"{}"))
    seed(db, sites=("poshmark", "depop", "vinted", "ebay"), urls={**URLS, "ebay": EBAY_URL}, ids={**IDS, "ebay": LISTING})
    assert call(db, "GET", "/ebay/token")[:2] == (404, {"error": "eBay is not set up"})
    assert call(db, "GET", "/ebay/token", query={"kind": "app"})[0] == 404
    assert call(db, "GET", "/ebay/oauth/start")[:2] == (404, {"error": "eBay is not set up"})
    assert call(db, "GET", "/ebay/oauth/callback", query={"code": "c", "state": "s" * 32})[0] == 404
    status = call(db, "GET", "/ebay/status")[1]
    assert (status["configured"], status["connected"], status["consent_expires_at"], status["ended"]) == (False, False, None, 0)
    assert core.process_email(db, posh_sale(), NOW)["status"] == "matched"
    for minutes in (5, 10, 15):
        timers.every_5_min(db, NOW + minutes * MINUTE)
    timers.every_15_min(db, NOW + 15 * MINUTE)
    assert calls == []
    assert set(call(db, "GET", "/tasks")[1]) == {"tasks", "sold", "sold_on", "mode"}
    assert not any("🔐" in text for _, text, _ in sent)


def test_ebay_is_a_site_in_get_tasks_whether_or_not_it_is_on(db):
    assert call(db, "GET", "/tasks", query={"sites": "poshmark,depop,vinted,ebay"})[:2] == (
        200, {"tasks": [], "sold": [], "sold_on": [], "mode": "replay"})


# --- the consent -----------------------------------------------------------------------------------------------------

def test_the_consent_link_redirects_to_ebays_page_with_a_single_use_state(db, fake_ebay, monkeypatch):
    where, query = start(db)
    assert where.startswith("https://auth.ebay.com/oauth2/authorize?")
    assert query == {"client_id": CLIENT, "redirect_uri": RUNAME, "response_type": "code", "scope": ebay.DEFAULT_SCOPES,
                     "state": query["state"]}
    assert ebay.DEFAULT_SCOPES.split() == [
        "https://api.ebay.com/oauth/api_scope", "https://api.ebay.com/oauth/api_scope/sell.listing",
        "https://api.ebay.com/oauth/api_scope/sell.inventory",
        "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly"]
    assert "scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope%20https%3A" in where     # space-separated, URL-encoded
    assert len(query["state"]) >= 24 and core.setting(db, "ebay_state:" + query["state"]) == "2026-10-08T15:10:00+00:00"
    assert start(db)[1]["state"] != query["state"]                                         # a new one each time
    monkeypatch.setenv("EBAY_ENV", "sandbox")
    monkeypatch.setenv("EBAY_SCOPES", "https://api.ebay.com/oauth/api_scope   https://api.ebay.com/oauth/api_scope/sell.listing")
    where, query = start(db)
    assert where.startswith("https://auth.sandbox.ebay.com/oauth2/authorize?")
    assert query["scope"] == "https://api.ebay.com/oauth/api_scope https://api.ebay.com/oauth/api_scope/sell.listing"
    assert fake_ebay.calls == []                                                            # the link itself calls nobody


def test_the_consent_link_says_which_setting_is_missing(db, fake_ebay, monkeypatch):
    for name in ("EBAY_RUNAME", "EBAY_CLIENT_SECRET"):
        with monkeypatch.context() as m:
            m.delenv(name)
            assert call(db, "GET", "/ebay/oauth/start")[:2] == (503, {"error": f"{name} is not set"})
    assert db.query("SELECT key FROM settings WHERE key LIKE 'ebay_state:%'") == []


def test_the_consent_link_needs_the_token_key_before_she_agrees(db, fake_ebay, monkeypatch):
    pytest.importorskip("cryptography")
    monkeypatch.delenv("EBAY_TOKEN_KEY")
    assert call(db, "GET", "/ebay/oauth/start")[:2] == (503, {"error": "EBAY_TOKEN_KEY is not set"})
    assert fake_ebay.calls == []


def test_agree_stores_the_refresh_token_sealed_and_says_so_once(db, fake_ebay, sent):
    status, page = connect(db)
    assert status == 200 and "<p>eBay is connected. You can close this page.</p>" in page
    assert sent == [("ops", "[replay] ✅ eBay connected — the agent can list on eBay and read its orders. Valid until "
                            "October 8, 2027.", False)]
    assert fake_ebay.calls == [("token", "authorization_code")]
    asked = fake_ebay.requests[-1]
    assert asked["url"] == "https://api.ebay.com/identity/v1/oauth2/token" and asked["method"] == "POST"
    assert b"grant_type=authorization_code" in asked["body"] and b"redirect_uri=" + RUNAME.encode() in asked["body"]
    consent = json.loads(core.setting(db, "ebay_consent"))
    assert {k: consent[k] for k in ("expires_at", "scopes", "connected_at", "environment")} == {
        "expires_at": "2027-10-08T15:00:00+00:00", "scopes": ebay.DEFAULT_SCOPES,
        "connected_at": "2026-10-08T15:00:00+00:00", "environment": "production"}
    stored = everything(db)
    assert not any(secret in stored for secret in (REFRESH, "user-access-token-1", CODE, SECRET))   # sealed, or not at all
    assert db.query("SELECT key FROM settings WHERE key LIKE 'ebay_state:%'") == []                 # the state is used up
    status = call(db, "GET", "/ebay/status")[1]
    assert status == {"configured": True, "connected": True, "environment": "production",
                      "consent_expires_at": "2027-10-08T15:00:00+00:00", "needs_consent_since": None,
                      "orders_read_at": None, "orders_error": None, "ended": 0}


def test_ebays_own_figure_is_the_end_when_its_answer_names_none(db, fake_ebay, sent):
    fake_ebay.refresh_life = None
    assert connect(db)[0] == 200
    assert json.loads(core.setting(db, "ebay_consent"))["expires_at"] == ebay.iso(NOW + timedelta(seconds=47_304_000))


@pytest.mark.parametrize("how", ["unknown", "reused", "expired", "malformed", "missing"])
def test_a_state_that_isnt_ours_exchanges_nothing(db, fake_ebay, sent, how):
    state, when = start(db)[1]["state"], NOW + MINUTE
    if how == "unknown":
        state = "u" * len(state)
    elif how == "reused":
        assert call(db, "GET", "/ebay/oauth/callback", query={"code": CODE, "state": state}, now=when)[0] == 200
    elif how == "expired":
        when = NOW + 11 * MINUTE
    elif how == "malformed":
        state = "'; DROP TABLE settings; --"
    before, said = fake_ebay.count("token"), len(sent)
    query = {"code": CODE} if how == "missing" else {"code": CODE, "state": state}
    status, page, headers = call(db, "GET", "/ebay/oauth/callback", query=query, now=when)
    assert status == 400 and headers["Content-Type"].startswith("text/html")
    assert "This link has expired or was already used — nothing was changed." in page
    assert fake_ebay.count("token") == before and len(sent) == said            # nothing exchanged, nothing said
    assert (core.setting(db, "ebay_consent") is not None) == (how == "reused")
    if how == "expired":                                                        # used up all the same
        assert call(db, "GET", "/ebay/oauth/callback", query=query, now=NOW + MINUTE)[0] == 400


def test_a_refused_exchange_says_the_status_and_ebays_code_only(db, fake_ebay, sent):
    status, page = connect(db, code="a-code-ebay-doesnt-know")
    assert status == 502 and "It didn&#x27;t work — eBay didn&#x27;t accept the sign-in (HTTP 400 invalid_grant)." in page
    assert sent == [("ops", "[replay] ⚠️ eBay's consent didn't finish: HTTP 400 invalid_grant. Nothing was changed; the "
                            "link can be opened again.", False)]
    assert REFRESH not in page + sent[0][1]                                    # never eBay's own words (its body)
    assert core.setting(db, "ebay_consent") is None and call(db, "GET", "/ebay/status")[1]["connected"] is False
    fake_ebay.down = True
    status, page = connect(db)
    assert status == 502 and sent[-1][1] == ("[replay] ⚠️ eBay's consent didn't finish: eBay couldn't be reached (OSError). "
                                             "Nothing was changed; the link can be opened again.")
    assert call(db, "GET", "/ebay/oauth/callback", query={"state": start(db)[1]["state"]})[0] == 400      # no code
    assert "Not connected — nothing was changed." in call(db, "GET", "/ebay/oauth/declined")[1]


# --- the tokens ------------------------------------------------------------------------------------------------------

def test_the_mac_gets_a_short_lived_token_kept_until_five_minutes_before_its_end(db, fake_ebay, sent):
    connect(db)
    status, first, _ = call(db, "GET", "/ebay/token", query={"kind": "user"})
    assert (status, first) == (200, {"access_token": "user-access-token-1", "expires_at": "2026-10-08T17:00:00+00:00",
                                     "environment": "production"})
    assert call(db, "GET", "/ebay/token", now=NOW + 114 * MINUTE)[1] == first              # user is the default kind
    assert fake_ebay.count("token") == 1                                                   # the consent's own: none minted
    status, second, _ = call(db, "GET", "/ebay/token", now=NOW + 116 * MINUTE)             # 4 minutes before its end
    assert (status, second) == (200, {"access_token": "user-access-token-2", "expires_at": "2026-10-08T18:56:00+00:00",
                                      "environment": "production"})
    assert fake_ebay.calls[-1] == ("token", "refresh_token") and b"scope" not in fake_ebay.requests[-1]["body"]
    assert call(db, "GET", "/ebay/token", now=NOW + 120 * MINUTE)[1] == second
    app = call(db, "GET", "/ebay/token", query={"kind": "app"})[1]
    assert app == {"access_token": "app-access-token-3", "expires_at": "2026-10-08T17:00:00+00:00",
                   "environment": "production"}
    assert call(db, "GET", "/ebay/token", query={"kind": "app"}, now=NOW + HOUR)[1] == app
    assert [grant for _, grant in fake_ebay.calls] == ["authorization_code", "refresh_token", "client_credentials"]
    assert call(db, "GET", "/ebay/token", query={"kind": "robot"})[:2] == (400, {"error": "kind is user or app"})
    stored = everything(db)
    assert not any(f"{kind}-access-token-{n}" in stored for kind in ("user", "app") for n in (1, 2, 3))


def test_no_consent_is_409_and_ebay_away_is_503(db, fake_ebay, sent):
    assert call(db, "GET", "/ebay/token")[:2] == (409, {"error": "not connected"})         # set up, never connected
    assert fake_ebay.calls == [] and sent == []
    assert call(db, "GET", "/ebay/token", query={"kind": "app"})[0] == 200                 # the app's own needs no consent
    connect(db)
    sent.clear()
    late = NOW + 3 * HOUR                                                                  # the cached token has ended
    fake_ebay.token_status = 500
    assert call(db, "GET", "/ebay/token", now=late)[:2] == (503, {"error": "eBay's token endpoint answered HTTP 500"})
    fake_ebay.down = True
    assert call(db, "GET", "/ebay/token", now=late)[:2] == (503, {"error": "eBay couldn't be reached (OSError)"})
    assert call(db, "GET", "/ebay/status", now=late)[1]["connected"] is True and sent == []   # away isn't refused
    fake_ebay.down, fake_ebay.token_status = False, 200
    assert call(db, "GET", "/ebay/token", now=late)[0] == 200


def test_a_sandbox_consent_is_nothing_to_production(db, fake_ebay, monkeypatch):
    connect(db)
    monkeypatch.setenv("EBAY_ENV", "sandbox")
    assert call(db, "GET", "/ebay/token")[:2] == (409, {"error": "not connected"})
    assert call(db, "GET", "/ebay/status")[1]["environment"] == "sandbox"
    assert call(db, "GET", "/ebay/token", query={"kind": "app"})[1]["environment"] == "sandbox"
    assert fake_ebay.requests[-1]["url"] == "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
    assert ebay.GRAPHQL_URL["sandbox"] == ebay.GRAPHQL_SANDBOX              # the one place its (unverified) address is


def test_the_tokens_are_sealed_with_the_token_key(db, fake_ebay, monkeypatch, sent):
    fernet = pytest.importorskip("cryptography.fernet")
    connect(db)
    consent = json.loads(core.setting(db, "ebay_consent"))
    assert fernet.Fernet(os.environ["EBAY_TOKEN_KEY"]).decrypt(consent["refresh"].encode()).decode() == REFRESH
    kept = json.loads(core.setting(db, "ebay_token:user"))
    assert fernet.Fernet(os.environ["EBAY_TOKEN_KEY"]).decrypt(kept["token"].encode()).decode() == "user-access-token-1"
    assert REFRESH not in everything(db) and ebay.unseal(ebay.seal("a token")) == "a token"
    monkeypatch.setenv("EBAY_TOKEN_KEY", "not-a-fernet-key")
    assert call(db, "GET", "/ebay/token")[:2] == (503, {"error": "EBAY_TOKEN_KEY is not a Fernet key"})
    # another key (the old one lost): what is stored can't be read — the human step, never a crash
    monkeypatch.setenv("EBAY_TOKEN_KEY", fernet.Fernet.generate_key().decode())
    sent.clear()
    assert call(db, "GET", "/ebay/token")[:2] == (409, {"error": "not connected"})
    assert len(sent) == 1 and "eBay wants a person" in sent[0][1]


def test_nothing_private_is_logged_on_the_way(db, fake_ebay, caplog, sent):
    with caplog.at_level(logging.DEBUG):
        connect(db)
        call(db, "GET", "/ebay/token", now=NOW + 3 * HOUR)
        fake_ebay.token_status = 400
        call(db, "GET", "/ebay/token", now=NOW + 6 * HOUR)
        connect(db, code="wrong", now=NOW + 7 * HOUR)
        fake_ebay.down = True
        call(db, "GET", "/ebay/token", query={"kind": "app"}, now=NOW + 8 * HOUR)
    assert not any(secret in caplog.text for secret in (REFRESH, CODE, SECRET, "access-token", "start-key-0123"))


# --- token trouble: a human step -------------------------------------------------------------------------------------

def test_a_refused_refresh_is_one_private_message_with_the_link(db, live, fake_ebay, sent):
    connect(db)
    sent.clear()
    fake_ebay.token_status = 400
    at = NOW + 3 * HOUR                                                       # 2:00 PM in New York
    assert call(db, "GET", "/ebay/token", now=at)[:2] == (409, {"error": "not connected"})
    assert sent == [("ops", wants_a_person("2:00 PM"), False)]
    asked = len(fake_ebay.calls)
    assert call(db, "GET", "/ebay/token", now=at + MINUTE)[0] == 409           # known: eBay isn't asked again, nor she
    status = call(db, "GET", "/ebay/status", now=at)[1]
    assert (status["connected"], status["needs_consent_since"]) == (False, "2026-10-08T18:00:00+00:00")
    for minutes in (15, 30, 45):
        assert timers.every_15_min(db, at + minutes * MINUTE)["ebay"] == {"state": "refused", "said": None}
    assert len(fake_ebay.calls) == asked and len(sent) == 1
    timers.every_15_min(db, at + HOUR)                                         # tried again by itself, once an hour
    assert len(fake_ebay.calls) == asked + 1 and len(sent) == 1                # … still refused: not a word
    timers.every_15_min(db, at + 11 * HOUR + 45 * MINUTE)
    assert len(sent) == 1
    assert timers.every_15_min(db, at + 12 * HOUR)["ebay"] == {"state": "refused", "said": "reminder"}
    assert sent[-1] == ("ops", wants_a_person("Thu Oct 8, 2:00 PM EDT"), True)   # 2 AM: silently
    timers.every_15_min(db, at + 12 * HOUR + 15 * MINUTE)
    timers.every_15_min(db, at + 23 * HOUR)
    assert len(sent) == 2
    timers.every_15_min(db, at + 24 * HOUR)
    assert len(sent) == 3 and all(chat == "ops" for chat, _, _ in sent)        # never the group


def test_a_permission_that_works_again_clears_itself(db, live, fake_ebay, sent):
    """A setting fixed in Azure (or eBay's own hiccup): the hourly look finds the stored consent taken again — no
    click needed, one line."""
    connect(db)
    fake_ebay.token_status = 401
    at = NOW + 3 * HOUR
    assert call(db, "GET", "/ebay/token", now=at)[0] == 409
    fake_ebay.token_status = 200
    sent.clear()
    assert timers.every_15_min(db, at + 30 * MINUTE)["ebay"]["said"] is None   # not yet an hour
    assert timers.every_15_min(db, at + HOUR)["ebay"] == {"state": "connected", "said": "works again"}
    assert sent == [("ops", "✅ eBay's permission for the agent works again — nothing to do.", False)]
    status, token, _ = call(db, "GET", "/ebay/token", now=at + HOUR)
    assert status == 200 and call(db, "GET", "/ebay/status", now=at + HOUR)[1]["needs_consent_since"] is None


def test_without_a_stored_link_the_message_says_whom_to_ask(db, live, fake_ebay, sent, monkeypatch):
    monkeypatch.delenv("EBAY_START_LINK")
    connect(db)
    sent.clear()
    fake_ebay.token_status = 400
    call(db, "GET", "/ebay/token", now=NOW + 3 * HOUR)
    assert sent == [("ops", wants_a_person("2:00 PM", "ask Claude Code for the eBay consent link, open it signed in to "
                                                      "eBay as the shop's account and click Agree and Continue"), False)]
    assert "http" not in sent[0][1]


def test_a_new_consent_clears_the_trouble(db, live, fake_ebay, sent):
    connect(db)
    fake_ebay.token_status = 400
    call(db, "GET", "/ebay/token", now=NOW + 3 * HOUR)
    fake_ebay.token_status = 200
    sent.clear()
    assert connect(db, now=NOW + 4 * HOUR)[0] == 200
    assert sent == [("ops", "✅ eBay connected — the agent can list on eBay and read its orders. Valid until October 8, "
                            "2027.", False)]
    status = call(db, "GET", "/ebay/status", now=NOW + 4 * HOUR)[1]
    assert (status["connected"], status["needs_consent_since"]) == (True, None)
    assert call(db, "GET", "/ebay/token", now=NOW + 4 * HOUR)[0] == 200
    timers.every_15_min(db, NOW + 17 * HOUR)
    assert len(sent) == 1                                                      # no reminder of what is over


def test_the_permissions_end_is_said_30_14_7_3_2_and_1_days_ahead_each_once(db, live, fake_ebay, sent):
    fake_ebay.refresh_life = 40 * 86400
    connect(db)                                                                # ends Nov 17, 2026
    sent.clear()
    said = []
    for days in (0, 5, 9.9, 10, 10.5, 12, 25, 26, 26.2, 33, 34, 37, 37.5, 38, 39, 39.5):
        before = len(sent)
        out = ebay.consent_watch(db, NOW + days * DAY)
        if len(sent) > before:
            said.append((days, out["said"]))
    assert said == [(10, 30), (26, 14), (33, 7), (37, 3), (38, 2), (39, 1)]
    text = "🔐 eBay's permission for the agent ends on November 17, 2026 — 1 minute: " + AGREE + "."
    assert {message for _, message, _ in sent} == {text} and all(chat == "ops" for chat, _, _ in sent)
    assert call(db, "GET", "/ebay/token", now=NOW + 39.5 * DAY)[0] == 200      # it still works until its end
    assert ebay.consent_watch(db, NOW + 40 * DAY + MINUTE) == {"state": "refused", "said": "ended"}
    assert sent[-1][1] == wants_a_person("10:01 AM") and len(sent) == 7
    assert call(db, "GET", "/ebay/token", now=NOW + 40 * DAY + 2 * MINUTE)[0] == 409
    assert fake_ebay.count("token") == 2                 # the consent's exchange and one refresh: an ended one isn't sent


def test_a_late_look_says_one_reminder_and_a_new_consent_starts_over(db, live, fake_ebay, sent):
    fake_ebay.refresh_life = 40 * 86400
    connect(db)
    sent.clear()
    assert ebay.consent_watch(db, NOW + 35 * DAY) == {"state": "expiring", "said": 7}     # 30, 14 and 7 due: one line
    assert ebay.consent_watch(db, NOW + 36 * DAY) == {"state": "expiring", "said": None}
    assert ebay.consent_watch(db, NOW + 37 * DAY) == {"state": "expiring", "said": 3}
    assert len(sent) == 2
    assert connect(db, now=NOW + 37 * DAY)[0] == 200                                        # Agree again: 40 more days
    sent.clear()
    assert ebay.consent_watch(db, NOW + 38 * DAY) == {"state": "connected", "said": None}
    assert ebay.consent_watch(db, NOW + 47 * DAY) == {"state": "expiring", "said": 30}
    assert "ends on December 24, 2026" in sent[-1][1]


def test_the_privacy_page_needs_no_key_and_no_database():
    status, page, headers = call(None, "GET", "/ebay/privacy")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert "It stores no eBay user&#x27;s personal data: no buyer names, usernames or addresses." in page
    assert "Account-deletion notices from eBay are acknowledged; there is nothing to delete." in page


@pytest.mark.parametrize("whose, stored", [("thrift_shop", True), ("Someone_Else", False), (None, True), ("away", True)])
def test_the_consent_must_be_the_shops_account(db, live, fake_ebay, sent, monkeypatch, whose, stored):
    """EBAY_SELLER set: eBay is asked whose token it is before anything is stored. Another account → nothing stored,
    one private line naming neither account; the same name (case aside), no username in the answer, or eBay not
    answering the question → accepted."""
    monkeypatch.setenv("EBAY_SELLER", "Thrift_Shop")
    fake_ebay.username = None if whose in (None, "away") else whose
    fake_ebay.introspect_status = 500 if whose == "away" else 200
    status, page = connect(db)
    assert fake_ebay.calls == [("token", "authorization_code"), ("token", "introspect")]
    assert (core.setting(db, "ebay_consent") is not None) == stored
    assert call(db, "GET", "/ebay/status")[1]["connected"] is stored
    if stored:
        assert status == 200 and sent[0][1].startswith("✅ eBay connected")
        return
    line = ("eBay's consent was given with another eBay account than the shop's — nothing was changed. Sign in to eBay "
            "as the shop's account and open the link again.")
    assert status == 409 and "another eBay account than the shop&#x27;s" in page
    assert sent == [("ops", "⚠️ " + line, False)]
    assert not any(name.lower() in (page + sent[0][1] + everything(db)).lower() for name in ("thrift_shop", "someone_else"))
    assert core.setting(db, "ebay_token:user") is None and call(db, "GET", "/ebay/token")[0] == 409


def test_without_a_named_shop_nobody_is_asked_whose_token_it_is(db, fake_ebay, sent):
    fake_ebay.username = "Someone_Else"
    assert connect(db)[0] == 200 and fake_ebay.calls == [("token", "authorization_code")]


def test_the_sender_never_follows_a_redirect_and_answers_an_errors_status(monkeypatch):
    """A token goes to the eBay address it was meant for only: a redirect is an answer, not a second request."""
    import io
    from urllib import error as urlerror
    seen = []

    class Opener:
        def open(self, request, timeout):
            seen.append((request.full_url, request.get_method(), timeout))
            raise urlerror.HTTPError(request.full_url, 302, "Found", {"Location": "https://elsewhere.example/"},
                                     io.BytesIO(b"moved"))
    assert ebay._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example/") is None
    monkeypatch.setattr(ebay, "_OPENER", Opener())
    assert ebay.http_send("GET", "https://apiz.ebay.com/x", {"Authorization": "Bearer t"}, None, 7) == (302, b"moved")
    assert seen == [("https://apiz.ebay.com/x", "GET", 7)]


def test_the_first_consent_link_goes_to_the_private_chat_once_per_keyset(db, fake_ebay, sent, monkeypatch):
    monkeypatch.setenv("EBAY_START_LINK", "https://thrift-api.example/ebay/oauth/start?code=k")
    assert ebay.consent_watch(db, NOW) == {"state": "not connected", "said": "link"}
    assert ebay.consent_watch(db, NOW + MINUTE) == {"state": "not connected", "said": None}
    lines = [m for m in sent if "consent link" in str(m)]
    assert len(lines) == 1 and "oauth/start?code=k" in str(lines[0])
    assert lines[0][0] == "ops"                                                       # the private chat, never the group
    monkeypatch.setenv("EBAY_CLIENT_ID", "another-keyset")             # a new keyset (sandbox → production): again
    assert ebay.consent_watch(db, NOW + 2 * MINUTE) == {"state": "not connected", "said": "link"}
    assert fake_ebay.calls == []


def test_the_watch_rests_while_ebay_is_off_or_unconnected(db, fake_ebay, sent, monkeypatch):
    monkeypatch.delenv("EBAY_START_LINK", raising=False)
    assert ebay.consent_watch(db, NOW) == {"state": "not connected", "said": None}
    monkeypatch.setenv("SALES_MODE", "off")
    assert ebay.consent_watch(db, NOW) == {"state": "off", "said": None}
    monkeypatch.delenv("EBAY_CLIENT_ID")
    monkeypatch.setenv("SALES_MODE", "live")
    assert ebay.consent_watch(db, NOW) == {"state": "off", "said": None} and sent == [] and fake_ebay.calls == []
