"""The Azure Function (WO33; Python v2 programming model, Flex Consumption): every HTTP route and the two timers, a thin
adapter over thrift_api.http.handle and thrift_api.timers. Every route needs the function key (AuthLevel.FUNCTION):
the x-functions-key header, or ?code= for the dashboard opened on the phone — but the four eBay itself must reach
(WO57, AuthLevel.ANONYMOUS): its account-deletion endpoint, the two pages its consent sends the owner's browser
back to, and the privacy page its RuName form asks for. host.json sets the route prefix to "", so the paths are
/email, /tasks, … (no /api)."""
from __future__ import annotations

from datetime import datetime, timezone

import azure.functions as func

from thrift_api import http, timers, util

FUNCTION = func.AuthLevel.FUNCTION
ANONYMOUS = func.AuthLevel.ANONYMOUS
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


# WO57: eBay, the fourth marketplace (thrift_api/ebay.py). eBay calls the account-deletion endpoint without a key, and
# its consent redirects the owner's browser to the callback with eBay's own ?code= — which Azure would read as the
# function key on a keyed route: those are ANONYMOUS (the callback is guarded by its single-use `state`). The link the
# owner opens (/ebay/oauth/start) and the Mac's two routes keep the key.

@app.route(route="ebay/account-deletion", methods=["GET"], auth_level=ANONYMOUS)
def get_ebay_account_deletion(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/ebay/account-deletion")


@app.route(route="ebay/account-deletion", methods=["POST"], auth_level=ANONYMOUS)
def post_ebay_account_deletion(req: func.HttpRequest) -> func.HttpResponse:
    """A deletion notice is acknowledged without its body ever being asked for: nothing of it is read, kept or logged."""
    status, headers, body = http.handle("POST", "/ebay/account-deletion", {}, b"")
    return func.HttpResponse(body=body, status_code=status, headers=headers)


@app.route(route="ebay/oauth/start", methods=["GET"], auth_level=FUNCTION)
def get_ebay_oauth_start(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/ebay/oauth/start")


@app.route(route="ebay/oauth/callback", methods=["GET"], auth_level=ANONYMOUS)
def get_ebay_oauth_callback(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/ebay/oauth/callback")


@app.route(route="ebay/oauth/declined", methods=["GET"], auth_level=ANONYMOUS)
def get_ebay_oauth_declined(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/ebay/oauth/declined")


@app.route(route="ebay/privacy", methods=["GET"], auth_level=ANONYMOUS)
def get_ebay_privacy(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/ebay/privacy")


@app.route(route="ebay/token", methods=["GET"], auth_level=FUNCTION)
def get_ebay_token(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/ebay/token")


@app.route(route="ebay/status", methods=["GET"], auth_level=FUNCTION)
def get_ebay_status(req: func.HttpRequest) -> func.HttpResponse:
    return _respond(req, "/ebay/status")


@app.timer_trigger(schedule="0 */15 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def every_15_min(timer: func.TimerRequest) -> None:
    """Reminders, the Gmail reader's heartbeat, failed emails again."""
    _timer(lambda: timers.every_15_min(http.get_db(), datetime.now(timezone.utc)))


@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def every_5_min(timer: func.TimerRequest) -> None:
    """WO45 §3: the share watchdog (a complete share with no outcome while the Mac is online). WO57: eBay's orders read,
    its open take-downs done."""
    _timer(lambda: timers.every_5_min(http.get_db(), datetime.now(timezone.utc)))


@app.timer_trigger(schedule="0 0 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def hourly(timer: func.TimerRequest) -> None:
    """The daily cleanup, done in the 3 AM hour New York time (the schedule itself is UTC); WO45 §9: the day's one
    line in the 21:00 hour."""
    _timer(lambda: timers.daily(http.get_db(), datetime.now(timezone.utc)))
    _timer(lambda: timers.evening(http.get_db(), datetime.now(timezone.utc)))
