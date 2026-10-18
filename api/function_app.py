"""The Azure Function (WO33; Python v2 programming model, Flex Consumption): every HTTP route and the two timers, a thin
adapter over thrift_api.http.handle and thrift_api.timers. Every route needs the function key (AuthLevel.FUNCTION):
the x-functions-key header, or ?code= for the dashboard opened on the phone. host.json sets the route prefix to "", so
the paths are /email, /tasks, … (no /api)."""
from __future__ import annotations

from datetime import datetime, timezone

import azure.functions as func

from thrift_api import http, timers, util

FUNCTION = func.AuthLevel.FUNCTION
app = func.FunctionApp(http_auth_level=FUNCTION)


def _timer(job) -> None:
    """A timer's run; a failure reaches the host's log as its type and place only (WO35: a database error's message
    can carry a row's values — an email's text, a buyer's name), and the run still counts as failed."""
    try:
        job()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(util.where(e)) from None


def _respond(req: func.HttpRequest, path: str) -> func.HttpResponse:
    status, headers, body = http.handle(req.method, path, dict(req.params), req.get_body(), headers=dict(req.headers))
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


@app.route(route="orders", methods=["POST"], auth_level=FUNCTION)
def post_orders(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/orders")


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


# WO45 §2: the iPhone's shares straight to the API (the Shortcut carries the function-scoped key `upload`, which opens
# the two upload functions only — provision.py uploads), and the Mac fetching them (the mac key).

@app.route(route="share/{share_id}/photo", methods=["POST"], auth_level=FUNCTION)
def post_share_photo(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, f"/share/{req.route_params.get('share_id', '')}/photo")


@app.route(route="share/{share_id}/end", methods=["POST"], auth_level=FUNCTION)
def post_share_end(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, f"/share/{req.route_params.get('share_id', '')}/end")


@app.route(route="shares/pending", methods=["GET"], auth_level=FUNCTION)
def get_shares_pending(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/shares/pending")


@app.route(route="shares/{share_id}/photo/{name}", methods=["GET"], auth_level=FUNCTION)
def get_share_photo(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, f"/shares/{req.route_params.get('share_id', '')}/photo/{req.route_params.get('name', '')}")


@app.route(route="shares/{share_id}/fetched", methods=["POST"], auth_level=FUNCTION)
def post_share_fetched(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, f"/shares/{req.route_params.get('share_id', '')}/fetched")


@app.timer_trigger(schedule="0 */15 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def every_15_min(timer: func.TimerRequest) -> None:
    """Reminders, the Gmail reader's heartbeat, failed emails again."""
    _timer(lambda: timers.every_15_min(http.get_db(), datetime.now(timezone.utc)))


@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def every_5_min(timer: func.TimerRequest) -> None:
    """WO45 §3: the share watchdog (a complete share with no outcome while the Mac is online)."""
    _timer(lambda: timers.every_5_min(http.get_db(), datetime.now(timezone.utc)))


@app.timer_trigger(schedule="0 0 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def hourly(timer: func.TimerRequest) -> None:
    """The daily cleanup, done in the 3 AM hour New York time (the schedule itself is UTC); WO45 §9: the day's one
    line in the 21:00 hour."""
    _timer(lambda: timers.daily(http.get_db(), datetime.now(timezone.utc)))
    _timer(lambda: timers.evening(http.get_db(), datetime.now(timezone.utc)))
