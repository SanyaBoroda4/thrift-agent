"""api/function_app.py (WO33): every route behind the function key, the two timers, a route going through
http.handle — read from the app itself (skipped where azure-functions isn't installed); the deployment files."""
import json
import tomllib

import pytest

from thrift_agent.config import ROOT

ROUTES = {("POST", "email"), ("POST", "samples"), ("POST", "heartbeat"), ("POST", "sync"), ("GET", "tasks"),
          ("POST", "tasks/{task_id}"), ("POST", "mac-event"), ("POST", "orders"), ("GET", "sales"),
          ("POST", "sales/{sale_id}/match"),
          ("POST", "test-message"), ("GET", "dashboard"), ("GET", "health"), ("POST", "replay"),
          # WO45 §2: the iPhone's shares
          ("POST", "share/{share_id}/photo"), ("POST", "share/{share_id}/end"), ("GET", "shares/pending"),
          ("GET", "shares/{share_id}/photo/{name}"), ("POST", "shares/{share_id}/fetched"),
          # WO57: eBay
          ("GET", "ebay/account-deletion"), ("POST", "ebay/account-deletion"), ("GET", "ebay/oauth/start"),
          ("GET", "ebay/oauth/callback"), ("GET", "ebay/oauth/declined"), ("GET", "ebay/token"), ("GET", "ebay/status"),
          ("GET", "ebay/privacy")}
# WO57: the four routes eBay itself must reach — its account-deletion endpoint (the check and the notices), the two
# pages its consent sends the owner's browser back to, the privacy page. No other route is open without the function key.
ANONYMOUS = {("GET", "ebay/account-deletion"), ("POST", "ebay/account-deletion"), ("GET", "ebay/oauth/callback"),
             ("GET", "ebay/oauth/declined"), ("GET", "ebay/privacy")}


@pytest.fixture(scope="module")
def func():
    return pytest.importorskip("azure.functions")


@pytest.fixture(scope="module")
def app(func):
    """The app's functions, indexed once as the host does (azure-functions refuses a second get_functions())."""
    import function_app
    return function_app.app.get_functions()


def triggers(app) -> dict:
    return {function.get_function_name(): function.get_trigger().get_dict_repr() for function in app}


def user_function(app, name: str):
    return next(f for f in app if f.get_function_name() == name).get_user_function()


def test_every_route_needs_the_function_key_but_the_four_ebay_reaches(app, func):
    http = [t for t in triggers(app).values() if t["type"] == "httpTrigger"]
    assert {(str(getattr(m, "value", m)), t["route"]) for t in http for m in t["methods"]} == ROUTES
    assert len(http) == len(ROUTES)                                     # one method per route
    levels = {(str(getattr(m, "value", m)), t["route"]): t["authLevel"] for t in http for m in t["methods"]}
    assert {route for route, level in levels.items() if level != func.AuthLevel.FUNCTION} == ANONYMOUS
    assert all(levels[route] == func.AuthLevel.ANONYMOUS for route in ANONYMOUS)
    assert {route for _, route in ANONYMOUS} == {"ebay/account-deletion", "ebay/oauth/callback", "ebay/oauth/declined",
                                                 "ebay/privacy"}                           # four paths, no other


def test_ebays_deletion_notice_is_acknowledged_without_reading_its_body(app, func):
    """WO57: POST /ebay/account-deletion answers 200 {} and never asks Azure for the body (nothing of a notice is
    read, kept or logged); the challenge goes through the app without a key or a database."""
    class NoBody(func.HttpRequest):
        def get_body(self):
            raise AssertionError("the notice's body was read")
    notice = NoBody(method="POST", url="https://thrift.example/ebay/account-deletion", headers={}, params={},
                    body=b'{"notification": {"data": {"username": "janeq_closet"}}}')
    answer = user_function(app, "post_ebay_account_deletion")(notice)
    assert (answer.status_code, json.loads(answer.get_body())) == (200, {})
    check = user_function(app, "get_ebay_account_deletion")(func.HttpRequest(
        method="GET", url="https://thrift.example/ebay/account-deletion", headers={}, params={}, body=b""))
    assert check.status_code == 400                                     # no challenge code (and no database asked for)


def test_two_timers(app):
    timers = {name: t["schedule"] for name, t in triggers(app).items() if t["type"] == "timerTrigger"}
    assert timers == {"every_15_min": "0 */15 * * * *", "hourly": "0 0 * * * *", "every_5_min": "0 */5 * * * *"}
    assert len(triggers(app)) == len(ROUTES) + 3


def test_a_route_and_a_timer_go_through_the_api(app, func, tmp_path, monkeypatch):
    from thrift_api import http
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    try:
        health = user_function(app, "get_health")(func.HttpRequest(method="GET", url="https://thrift.example/health",
                                                                   headers={}, params={}, body=b""))
        assert health.status_code == 200 and json.loads(health.get_body())["db"] == "ok"
        result = user_function(app, "post_task_result")(func.HttpRequest(
            method="POST", url="https://thrift.example/tasks/t_000000000000", headers={}, params={},
            route_params={"task_id": "t_000000000000"}, body=b'{"result": "done"}'))
        assert (result.status_code, json.loads(result.get_body())) == (404, {"error": "no such task"})
        assert result.headers["Content-Type"] == "application/json; charset=utf-8"
        user_function(app, "every_15_min")(None)
        user_function(app, "hourly")(None)
    finally:
        if http._DB is not None:
            http._DB.close()


def test_an_upload_goes_through_the_app_with_its_headers(app, func, tmp_path, monkeypatch):
    """WO45 §2: the request's headers reach http.handle (a form is told from a raw image by its Content-Type) and a
    photo comes back as its bytes."""
    from thrift_api import http, uploads
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setattr(uploads, "_STORE", uploads.MemoryStore())
    sid, photo = "2026-10-18_153045_4821", b"\xff\xd8\xff\xe0" + bytes(range(256))
    body = (b'--b\r\nContent-Disposition: form-data; name="photo"; filename="IMG_1.jpeg"\r\n\r\n' + photo + b"\r\n--b--\r\n")
    try:
        sent = user_function(app, "post_share_photo")(func.HttpRequest(
            method="POST", url=f"https://thrift.example/share/{sid}/photo", headers={"content-type":
                                                                                     "multipart/form-data; boundary=b"},
            params={"n": "1", "of": "1"}, route_params={"share_id": sid}, body=body))
        assert (sent.status_code, json.loads(sent.get_body())) == (200, {"ok": True, "received": 1, "name": "IMG_1.jpeg",
                                                                         "stored": True})
        got = user_function(app, "get_share_photo")(func.HttpRequest(
            method="GET", url=f"https://thrift.example/shares/{sid}/photo/IMG_1.jpeg", headers={}, params={},
            route_params={"share_id": sid, "name": "IMG_1.jpeg"}, body=b""))
        assert (got.status_code, got.headers["Content-Type"], got.get_body()) == (200, "image/jpeg", photo)
    finally:
        if http._DB is not None:
            http._DB.close()


def test_the_deployment_files():
    api = ROOT / "api"
    host = json.loads((api / "host.json").read_text(encoding="utf-8"))
    assert (host["version"], host["extensions"]["http"]["routePrefix"]) == ("2.0", "")
    assert host["logging"]["applicationInsights"]["samplingSettings"]["isEnabled"] is True
    assert host["logging"]["logLevel"]["default"] == "Warning"            # WO35: the log levels (test_api_logs.py)
    assert (api / "requirements.txt").read_text(encoding="utf-8").split() == [
        "azure-functions", "psycopg[binary]>=3.2", "tzdata", "azure-storage-blob>=12.19",
        "cryptography>=42"]                                               # WO57: eBay's tokens are stored sealed (Fernet)
    assert {"tests", "__pycache__", ".venv", "local.settings.json"} <= set((api / ".funcignore").read_text(
        encoding="utf-8").split())
    dev = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]["dev"]
    assert "azure-functions" in dev and not any("psycopg" in dep for dep in dev)
