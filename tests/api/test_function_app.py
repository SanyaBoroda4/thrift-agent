"""api/function_app.py (WO33): every route behind the function key, the two timers, a route going through
http.handle — read from the app itself (skipped where azure-functions isn't installed); the deployment files."""
import json
import tomllib

import pytest

from thrift_agent.config import ROOT

ROUTES = {("POST", "email"), ("POST", "samples"), ("POST", "heartbeat"), ("POST", "sync"), ("GET", "tasks"),
          ("POST", "tasks/{task_id}"), ("POST", "mac-event"), ("GET", "sales"), ("POST", "sales/{sale_id}/match"),
          ("POST", "test-message"), ("GET", "dashboard"), ("GET", "health")}


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


def test_every_route_needs_the_function_key(app, func):
    http = [t for t in triggers(app).values() if t["type"] == "httpTrigger"]
    assert {(str(getattr(m, "value", m)), t["route"]) for t in http for m in t["methods"]} == ROUTES
    assert len(http) == len(ROUTES)                                     # one method per route
    assert all(t["authLevel"] == func.AuthLevel.FUNCTION for t in http)


def test_two_timers(app):
    timers = {name: t["schedule"] for name, t in triggers(app).items() if t["type"] == "timerTrigger"}
    assert timers == {"every_15_min": "0 */15 * * * *", "hourly": "0 0 * * * *"}
    assert len(triggers(app)) == len(ROUTES) + 2


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


def test_the_deployment_files():
    api = ROOT / "api"
    host = json.loads((api / "host.json").read_text(encoding="utf-8"))
    assert (host["version"], host["extensions"]["http"]["routePrefix"]) == ("2.0", "")
    assert host["logging"]["applicationInsights"]["samplingSettings"]["isEnabled"] is True
    assert (api / "requirements.txt").read_text(encoding="utf-8").split() == ["azure-functions", "psycopg[binary]>=3.2",
                                                                              "tzdata"]
    assert {"tests", "__pycache__", ".venv", "local.settings.json"} <= set((api / ".funcignore").read_text(
        encoding="utf-8").split())
    dev = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]["dev"]
    assert "azure-functions" in dev and not any("psycopg" in dep for dep in dev)
