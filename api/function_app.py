"""The Azure Function (WO33; Python v2 programming model, Flex Consumption): every HTTP route and the two timers, a thin
adapter over thrift_api.http.handle and thrift_api.timers. Every route needs the function key (AuthLevel.FUNCTION):
the x-functions-key header, or ?code= for the dashboard opened on the phone. host.json sets the route prefix to "", so
the paths are /email, /tasks, … (no /api)."""
from __future__ import annotations

from datetime import datetime, timezone

import azure.functions as func

from thrift_api import http, timers

FUNCTION = func.AuthLevel.FUNCTION
app = func.FunctionApp(http_auth_level=FUNCTION)


def _respond(req: func.HttpRequest, path: str) -> func.HttpResponse:
    status, headers, body = http.handle(req.method, path, dict(req.params), req.get_body())
    return func.HttpResponse(body=body, status_code=status, headers=headers)


@app.route(route="email", methods=["POST"], auth_level=FUNCTION)
def post_email(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/email")


@app.route(route="samples", methods=["POST"], auth_level=FUNCTION)
def post_samples(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/samples")


@app.route(route="heartbeat", methods=["POST"], auth_level=FUNCTION)
def post_heartbeat(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/heartbeat")


@app.route(route="sync", methods=["POST"], auth_level=FUNCTION)
def post_sync(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/sync")


@app.route(route="tasks", methods=["GET"], auth_level=FUNCTION)
def get_tasks(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/tasks")


@app.route(route="tasks/{task_id}", methods=["POST"], auth_level=FUNCTION)
def post_task_result(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, f"/tasks/{req.route_params.get('task_id', '')}")


@app.route(route="mac-event", methods=["POST"], auth_level=FUNCTION)
def post_mac_event(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/mac-event")


@app.route(route="sales", methods=["GET"], auth_level=FUNCTION)
def get_sales(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/sales")


@app.route(route="sales/{sale_id}/match", methods=["POST"], auth_level=FUNCTION)
def post_sale_match(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, f"/sales/{req.route_params.get('sale_id', '')}/match")


@app.route(route="replay", methods=["POST"], auth_level=FUNCTION)
def post_replay(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/replay")


@app.route(route="test-message", methods=["POST"], auth_level=FUNCTION)
def post_test_message(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/test-message")


@app.route(route="dashboard", methods=["GET"], auth_level=FUNCTION)
def get_dashboard(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/dashboard")


@app.route(route="health", methods=["GET"], auth_level=FUNCTION)
def get_health(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/health")


@app.timer_trigger(schedule="0 */15 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def every_15_min(timer: func.TimerRequest) -> None:
    """Reminders, the Gmail reader's heartbeat, failed emails again."""
    timers.every_15_min(http.get_db(), datetime.now(timezone.utc))


@app.timer_trigger(schedule="0 0 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def hourly(timer: func.TimerRequest) -> None:
    """The daily cleanup, done in the 3 AM hour New York time (the schedule itself is UTC)."""
    timers.daily(http.get_db(), datetime.now(timezone.utc))
