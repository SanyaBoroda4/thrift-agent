"""thrift_api.http (WO33): every route through handle(), as the Function calls it — JSON in and out, 400 / 404 / 405 /
409 / 500 / 503, the dashboard's page and headers, the database opened lazily from DATABASE_URL and migrated once."""
import json

import pytest
from apitools import ITEM, NOW, TITLE, URLS, call, depop_sale, posh_sale, seed, texts, utc

from thrift_api import core, http

GO_LIVE = "2026-10-01T00:00:00+00:00"
DONE = {"result": "done", "error": None, "evidence": "failed/shots/x.png", "manual": False}


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", GO_LIVE)


def test_post_email_and_its_duplicate(db, live, sent):
    seed(db)
    status, out, headers = call(db, "POST", "/email", posh_sale())
    assert (status, headers["Content-Type"]) == (200, "application/json; charset=utf-8")
    assert (out["status"], out["kind"], out["marketplace"], out["sale_status"]) == ("matched", "SALE", "poshmark",
                                                                                     "delisting")
    assert call(db, "POST", "/email", posh_sale())[:2] == (200, {"message_id": "posh-sale-1", "kind": "SALE",
                                                                 "marketplace": "poshmark", "status": "duplicate"})
    assert len(texts(sent, "group")) == 1


def test_bad_requests(db):
    assert call(db, "POST", "/email", raw=b"{not json")[:2] == (400, {"error": "the body is not valid JSON"})
    assert call(db, "POST", "/email", raw=b'["a list"]')[:2] == (400, {"error": "the body must be a JSON object"})
    assert call(db, "POST", "/email", raw=b"\xff\xfe")[0] == 400
    assert call(db, "POST", "/email", {"subject": "no message id"})[0] == 400
    assert call(db, "POST", "/heartbeat", {})[0] == 400
    assert call(db, "POST", "/samples", {"samples": "nope"})[0] == 400
    assert call(db, "POST", "/sync", {"items": [{"title": "no id"}]})[0] == 400
    assert call(db, "GET", "/sales", query={"state": "closed"})[0] == 400
    assert call(db, "GET", "/tasks", query={"sites": "depop,ebay"})[0] == 400
    assert call(db, "POST", "/tasks/t_1", {"result": "done", "manual": "yes"})[0] == 400
    assert call(db, "POST", "/mac-event", {"kind": "bogus"})[0] == 400


def test_unknown_paths_and_methods(db):
    assert call(db, "GET", "/nope")[:2] == (404, {"error": "no such path: /nope"})
    assert call(db, "GET", "/api/health")[0] == 404                       # the route prefix is ""
    status, _, headers = call(db, "GET", "/email")
    assert (status, headers["Allow"]) == (405, "POST")
    assert call(db, "DELETE", "/tasks")[2]["Allow"] == "GET"
    assert call(db, "POST", "/email/", posh_sale())[0] == 200              # a trailing slash is the same path


def test_samples_are_stored_once(db):
    samples = [posh_sale("s1"), depop_sale("s2"), {"message_id": "s3", "from": "jane@gmail.com"}, "junk",
               {"from": "noreply@poshmark.com"}]
    assert call(db, "POST", "/samples", {"samples": samples})[:2] == (200, {"stored": 2, "duplicates": 0, "skipped": 3})
    assert call(db, "POST", "/samples", {"samples": samples})[1] == {"stored": 0, "duplicates": 2, "skipped": 3}
    rows = db.query("SELECT * FROM samples ORDER BY message_id")
    assert [(r["message_id"], r["marketplace"], r["received_at"]) for r in rows] == [
        ("s1", "poshmark", "2026-10-08T14:05:00+00:00"), ("s2", "depop", "2026-10-08T14:05:00+00:00")]
    assert "Lacoste" in rows[0]["text"]


def test_heartbeat(db, live):
    status, out, _ = call(db, "POST", "/heartbeat", {"source": "gmail", "info": {"sent": 1, "errors": 0, "left": 0}})
    assert status == 200
    assert {k: out[k] for k in ("ok", "mode", "go_live_at", "source", "last_seen")} == {
        "ok": True, "mode": "live", "go_live_at": GO_LIVE, "source": "gmail", "last_seen": "2026-10-08T15:00:00+00:00"}


def test_sync_is_idempotent(db):
    body = {"items": [{"id": ITEM, "title": TITLE, "price": 35, "updated_at": "2026-10-05T12:00:00Z"}],
            "listings": [{"item_id": ITEM, "marketplace": "depop", "status": "posted", "url": URLS["depop"],
                          "updated_at": "2026-10-05T12:00:00Z"}]}
    assert call(db, "POST", "/sync", body)[:2] == (200, {"items": {"inserted": 1, "updated": 0, "unchanged": 0},
                                                         "listings": {"inserted": 1, "updated": 0, "unchanged": 0}})
    assert call(db, "POST", "/sync", body)[1] == {"items": {"inserted": 0, "updated": 0, "unchanged": 1},
                                                  "listings": {"inserted": 0, "updated": 0, "unchanged": 1}}


def test_tasks_round_trip(db, live, sent):
    seed(db)
    call(db, "POST", "/email", posh_sale())
    status, out, _ = call(db, "GET", "/tasks", query={"sites": "depop,vinted"})
    assert (status, out["mode"], out["sold"]) == (200, "live", [ITEM])
    assert [{k: t[k] for k in ("item_id", "marketplace", "listing_url", "title", "attempts")} for t in out["tasks"]] == [
        {"item_id": ITEM, "marketplace": "depop", "listing_url": URLS["depop"], "title": TITLE, "attempts": 0},
        {"item_id": ITEM, "marketplace": "vinted", "listing_url": URLS["vinted"], "title": TITLE, "attempts": 0}]
    assert call(db, "GET", "/tasks", query={"sites": "depop,vinted"})[1]["tasks"] == []           # leased
    depop, vinted = out["tasks"]
    assert call(db, "POST", f"/tasks/{depop['id']}", DONE)[1] == {"id": depop["id"], "status": "done", "attempts": 0,
                                                                  "changed": True}
    assert call(db, "POST", f"/tasks/{depop['id']}", DONE)[1]["changed"] is False                   # repeated
    failed = {"result": "failed", "error": "Hide isn't recorded yet", "evidence": None, "manual": True}
    assert call(db, "POST", f"/tasks/{vinted['id']}", failed)[1]["status"] == "failed"
    assert texts(sent, "group")[-1] == "Couldn't take Lacoste Tee White size M down on Vinted — please mark it sold there."
    assert call(db, "POST", "/tasks/t_000000000000", DONE)[0] == 404
    assert call(db, "POST", f"/tasks/{depop['id']}", {"result": "maybe"})[0] == 400


def test_task_leases_run_out_and_every_site_is_the_default(db, live):
    seed(db)
    call(db, "POST", "/email", depop_sale())
    first = call(db, "GET", "/tasks")[1]["tasks"]
    assert [t["marketplace"] for t in first] == ["poshmark", "vinted"]
    assert call(db, "GET", "/tasks", now=utc(2026, 10, 8, 15, 9))[1]["tasks"] == []
    again = call(db, "GET", "/tasks", query={"sites": ["vinted"]}, now=utc(2026, 10, 8, 15, 11))[1]["tasks"]
    assert [t["id"] for t in again] == [first[1]["id"]]


def test_no_tasks_outside_live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "replay")
    assert call(db, "GET", "/tasks", query={"sites": "depop"})[1] == {"tasks": [], "sold": [], "mode": "replay"}


def test_mac_events(db, live, sent):
    seed(db)
    sale_id = call(db, "POST", "/email", posh_sale())[1]["sale_id"]
    out = call(db, "POST", "/mac-event", {"kind": "shipped", "words": "lacoste tee"})[1]
    assert (out["status"], out["matched"], out["title"]) == ("shipped", sale_id, TITLE)
    assert call(db, "POST", "/mac-event", {"kind": "shipped", "words": "lacoste tee"})[1]["matched"] is None
    found = call(db, "POST", "/mac-event", {"kind": "sold_found", "marketplace": "vinted", "listing_id": "7012345678",
                                            "item_id": ITEM})[1]
    assert found["sale_status"] == "double_sale"
    gone = {"kind": "not_for_sale", "item_id": ITEM, "marketplace": "poshmark", "url": URLS["poshmark"]}
    assert call(db, "POST", "/mac-event", gone)[1]["tasks"] == 0                # its take-downs are under way already


def test_sales_and_a_manual_match(db, live, sent):
    seed(db)
    lost = call(db, "POST", "/email", posh_sale("posh-x", title="Mystery Tee", url=None, order=None))[1]["sale_id"]
    status, out, _ = call(db, "GET", "/sales", query={"state": "unmatched"})
    assert (status, [s["id"] for s in out["sales"]]) == (200, [lost])
    assert [s["id"] for s in call(db, "GET", "/sales")[1]["sales"]] == [lost]                   # open by default
    matched = call(db, "POST", f"/sales/{lost}/match", {"item_id": ITEM})[1]
    assert (matched["status"], len(matched["tasks"])) == ("delisting", 2)
    assert call(db, "GET", "/sales", query={"state": "unmatched"})[1]["sales"] == []
    assert call(db, "POST", "/sales/s_000000000000/match", {"item_id": ITEM})[0] == 404
    assert call(db, "POST", f"/sales/{lost}/match", {"item_id": "i_000000_000000"})[0] == 404
    seed(db, "i_other", "Other Tee", sites=("poshmark",), urls={}, ids={})
    assert call(db, "POST", f"/sales/{lost}/match", {"item_id": "i_other"})[:2] == (
        409, {"error": "this sale already has take-down tasks"})
    assert call(db, "POST", f"/sales/{lost}/match", {})[0] == 400


def test_the_test_message(db, monkeypatch, sent):
    assert call(db, "POST", "/test-message", {})[1] == {"sent": True, "chat": "ops", "mode": "replay"}
    monkeypatch.setenv("SALES_MODE", "live")
    assert call(db, "POST", "/test-message", {"chat": "group"})[1] == {"sent": True, "chat": "group", "mode": "live"}
    monkeypatch.setenv("SALES_MODE", "off")
    assert call(db, "POST", "/test-message", {"chat": "ops"})[1] == {"sent": False, "chat": "ops", "mode": "off"}
    assert call(db, "POST", "/test-message", {"chat": "everyone"})[0] == 400
    assert sent == [("ops", "[replay] ✓ thrift-api is up — Thu Oct 8, 11:00 AM EDT", False),
                    ("group", "✓ thrift-api is up — Thu Oct 8, 11:00 AM EDT", False)]


def test_the_test_message_needs_no_database(sent):
    status, _, body = http.handle("POST", "/test-message", {}, b'{"chat": "ops"}', now=NOW)     # no DATABASE_URL
    assert (status, json.loads(body)["sent"]) == (200, True)


def test_the_dashboard_page(db, live):
    seed(db)
    call(db, "POST", "/email", posh_sale())
    status, page, headers = call(db, "GET", "/dashboard")
    assert status == 200 and page.startswith("<!doctype html>")
    assert (headers["Content-Type"], headers["Cache-Control"], headers["Referrer-Policy"]) == (
        "text/html; charset=utf-8", "no-store", "no-referrer")
    assert headers["Content-Security-Policy"].startswith("default-src 'none'")
    assert f'data-id="{ITEM}"' in page and 'id="c-toship">1<' in page
    for private in ("Jane", "janeq", "Secret Lane"):
        assert private not in page


def test_health(db, live):
    call(db, "POST", "/heartbeat", {"source": "gmail", "info": {}})
    status, out, _ = call(db, "GET", "/health")
    assert status == 200
    assert {k: out[k] for k in ("ok", "db", "mode", "heartbeats", "open_sales")} == {
        "ok": True, "db": "ok", "mode": "live", "heartbeats": {"gmail": "2026-10-08T15:00:00+00:00"}, "open_sales": 0}


def test_a_live_request_starts_the_live_clock_once(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    call(db, "GET", "/health")
    call(db, "GET", "/health", now=utc(2026, 10, 9, 12, 0))
    assert core.setting(db, "go_live_at") == "2026-10-08T15:00:00+00:00"


def test_the_database_comes_from_database_url_and_is_migrated_once(tmp_path, monkeypatch):
    calls = []
    real = http.migrate
    monkeypatch.setattr(http, "migrate", lambda database: calls.append(1) or real(database))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    try:
        assert http.handle("GET", "/health", {}, b"", now=NOW)[0] == 200
        assert http.handle("POST", "/heartbeat", {}, b'{"source": "gmail"}', now=NOW)[0] == 200
        assert calls == [1] and (tmp_path / "api.db").exists()
    finally:
        if http._DB is not None:
            http._DB.close()


def test_no_database_is_a_503(monkeypatch):
    status, _, body = http.handle("GET", "/health", {}, b"", now=NOW)
    assert (status, json.loads(body)) == (503, {"ok": False, "db": "unavailable", "mode": "replay"})
    assert http.handle("POST", "/email", {}, json.dumps(posh_sale()).encode(), now=NOW)[0] == 503


def test_an_unexpected_error_is_a_bare_500(db, monkeypatch):
    monkeypatch.setattr(core, "sales_list", lambda *args: 1 / 0)
    assert call(db, "GET", "/sales")[:2] == (500, {"error": "internal error"})
    monkeypatch.setattr(core, "health", lambda *args: 1 / 0)
    assert call(db, "GET", "/health")[:2] == (503, {"ok": False, "db": "error", "mode": "replay"})
