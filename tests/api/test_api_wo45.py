"""WO45: the API's share ledger (synced), its watchdog (a complete share with no outcome), the safety net for an item
priced but not live on a site (one retry asked, then one line), the day's one line at 21:00, the dashboard's Shares
panel, the repost task kind through GET /tasks and POST /tasks/{id}."""
from datetime import timedelta

import pytest
from apitools import NOW, texts

from thrift_api import core, dashboard, http, timers, watch


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-10-01T00:00:00+00:00")


def _share(db, sid, now=NOW, **fields):
    core.sync(db, {"items": [], "listings": [], "shares": [{"id": sid, "source": "icloud", "expected": 4, "received": 4,
                                                            "received_at": (now - timedelta(minutes=5)).isoformat(),
                                                            "updated_at": now.isoformat(), **fields}]}, now)


def test_the_share_ledger_syncs_and_the_dashboard_shows_the_day(db, live):
    out = core.sync(db, {"items": [], "listings": [],
                         "shares": [{"id": "2026-10-08_110500", "source": "both", "expected": 4, "received": 4,
                                     "received_at": NOW.isoformat(), "complete_at": NOW.isoformat(),
                                     "grouped_at": NOW.isoformat(), "carded_at": NOW.isoformat(), "outcome": "carded",
                                     "updated_at": NOW.isoformat()}]}, NOW)
    assert out["shares"] == {"inserted": 1, "updated": 0, "unchanged": 0}
    row = db.one("SELECT * FROM shares WHERE id = '2026-10-08_110500'")
    assert row["outcome"] == "carded" and row["expected"] == 4 and row["source"] == "both"
    data = core.dashboard_data(db, NOW)
    assert [s["id"] for s in data["shares"]] == ["2026-10-08_110500"]
    page = dashboard.render(data, NOW)
    assert "Shares today" in page and "11:05 AM" in page and "cards sent" in page and "4 photos" in page
    with pytest.raises(http.BadRequest):
        core.sync(db, {"items": [], "listings": [], "shares": [{"source": "icloud"}]}, NOW)
    assert "No shares today" in dashboard.render(core.dashboard_data(db, NOW + timedelta(days=2)), NOW + timedelta(days=2))


def test_a_complete_share_with_no_outcome_is_one_private_line_while_the_mac_is_online(db, live, sent):
    core.heartbeat(db, "mac", {}, NOW)
    _share(db, "2026-10-08_110500", complete_at=(NOW - timedelta(minutes=10)).isoformat())
    assert timers.every_5_min(db, NOW)["shares"] == []                      # 10 minutes: not yet
    later = NOW + timedelta(minutes=6)
    core.heartbeat(db, "mac", {}, later)
    assert timers.every_5_min(db, later)["shares"] == ["2026-10-08_110500"]
    assert sent == [("ops", "⏳ Share 11:05 AM (4 photos) stuck at complete since 10:50", False)]
    assert timers.every_5_min(db, later + timedelta(minutes=5))["shares"] == []      # once
    _share(db, "2026-10-08_120000", now=later, complete_at=(later - timedelta(minutes=20)).isoformat(),
           grouped_at=(later - timedelta(minutes=18)).isoformat(), outcome="carded")
    assert watch.shares_watch(db, later + timedelta(minutes=5), "live") == []          # an outcome: nothing
    _share(db, "2026-10-08_123000", now=later, complete_at=(later - timedelta(minutes=20)).isoformat(), received=25,
           expected=25)
    assert watch.shares_watch(db, later + timedelta(minutes=5), "live") == []          # 25 photos: 30 minutes
    assert watch.shares_watch(db, later + timedelta(minutes=25), "live") == []         # ... but the Mac went quiet
    core.heartbeat(db, "mac", {}, later + timedelta(minutes=25))
    assert watch.shares_watch(db, later + timedelta(minutes=25), "live") == ["2026-10-08_123000"]
    assert texts(sent)[-1] == "⏳ Share 12:30 PM (25 photos) stuck at complete since 10:46"


def _priced_item(db, iid="i_1", title="Ralph Lauren Shirt", status="ready", test=0, now=NOW):
    core.sync(db, {"items": [{"id": iid, "title": title, "price": 40, "status": status, "priced": True, "test": test,
                              "created_at": (now - timedelta(hours=2)).isoformat(),
                              "updated_at": (now - timedelta(hours=2)).isoformat()}], "listings": []}, now)


def test_an_item_priced_an_hour_ago_and_not_live_gets_one_retry_then_one_line(db, live, sent):
    _priced_item(db)
    core.sync(db, {"items": [], "listings": [
        {"item_id": "i_1", "marketplace": "poshmark", "status": "failed",
         "error": "PosterError: Poshmark's cover dialog differs", "updated_at": (NOW - timedelta(hours=2)).isoformat()},
        {"item_id": "i_1", "marketplace": "depop", "status": "posted", "url": "https://www.depop.com/products/x-1/",
         "updated_at": (NOW - timedelta(hours=2)).isoformat()},
        {"item_id": "i_1", "marketplace": "vinted", "status": "skipped", "error": "skipped: no size on the item",
         "updated_at": (NOW - timedelta(hours=2)).isoformat()}]}, NOW)
    out = timers.every_15_min(db, NOW)["unposted"]
    assert out == {"asked": ["i_1|poshmark"], "said": []}                   # Vinted's reason is hers: left alone
    [task] = core.take_tasks(db, ["poshmark", "depop", "vinted"], NOW)
    assert task["kind"] == "repost" and task["item_id"] == "i_1" and task["marketplace"] == "poshmark"
    assert watch.unposted(db, NOW + timedelta(minutes=15), "live") == {"asked": [], "said": []}   # asked once
    core.task_result(db, task["id"], "done", now=NOW + timedelta(minutes=20))
    assert db.one("SELECT status FROM listings WHERE item_id = 'i_1' AND marketplace = 'poshmark'")["status"] == "failed"
    assert watch.unposted(db, NOW + timedelta(minutes=40), "live") == {"asked": [], "said": []}   # the retry's hour
    assert watch.unposted(db, NOW + timedelta(hours=1, minutes=25), "live") == {"asked": [], "said": ["i_1|poshmark"]}
    assert texts(sent) == ["⚠️ Ralph Lauren Shirt isn't on Poshmark: PosterError: Poshmark's cover dialog differs"]
    assert watch.unposted(db, NOW + timedelta(hours=3), "live") == {"asked": [], "said": []}       # once
    night = NOW.replace(hour=4)                                                                     # 00:00 New York
    _priced_item(db, "i_2", "Night Item")
    core.sync(db, {"items": [], "listings": [{"item_id": "i_2", "marketplace": "poshmark", "status": "failed",
                                              "error": "boom", "updated_at": (night - timedelta(hours=2)).isoformat()}]},
              night)
    assert watch.unposted(db, night, "live") == {"asked": [], "said": []}                         # not at night
    assert watch.unposted(db, NOW, "replay") == {"asked": [], "said": []}                           # live only


def test_a_test_item_and_a_sold_or_twin_row_are_never_chased(db, live, sent):
    _priced_item(db, "i_t", "Test Item", test=1)
    _priced_item(db, "i_s", "Sold Item")
    core.sync(db, {"items": [], "listings": [
        {"item_id": "i_t", "marketplace": "poshmark", "status": "failed", "error": "boom",
         "updated_at": (NOW - timedelta(hours=2)).isoformat()},
        {"item_id": "i_s", "marketplace": "poshmark", "status": "skipped", "error": "skipped: shared again as i_x",
         "updated_at": (NOW - timedelta(hours=2)).isoformat()}]}, NOW)
    assert watch.unposted(db, NOW, "live") == {"asked": [], "said": []} and sent == []


def test_the_evening_line_goes_once_in_the_21_hour(db, live, sent):
    evening = NOW.replace(hour=1) + timedelta(days=1)                        # 9:00 PM New York (01:00 UTC next day)
    day = evening - timedelta(hours=5)
    for sid in ("2026-10-08_120000", "2026-10-08_130000"):
        _share(db, sid, now=day, complete_at=day.isoformat(), outcome="carded")
    _share(db, "2026-10-08_140000", now=day, complete_at=day.isoformat(), outcome="repeat")
    for iid in ("i_a", "i_b"):
        core.sync(db, {"items": [{"id": iid, "title": iid, "price": 40, "status": "posted", "priced": True,
                                  "created_at": day.isoformat(), "updated_at": day.isoformat()}],
                       "listings": [{"item_id": iid, "marketplace": mp, "status": "posted", "url": f"https://x/{mp}",
                                     "photos_sent": 4, "photos_live": 4 if iid == "i_a" else 1,
                                     "updated_at": day.isoformat()} for mp in ("poshmark", "depop", "vinted")]}, day)
    core.sync(db, {"items": [{"id": "i_c", "title": "i_c", "price": 25, "status": "ready", "priced": True,
                              "created_at": day.isoformat(), "updated_at": day.isoformat()}],
                   "listings": [{"item_id": "i_c", "marketplace": "poshmark", "status": "skipped",
                                 "error": "skipped: no size on the item", "updated_at": day.isoformat()}]}, day)
    assert timers.evening(db, day)["line"] is None                            # 4 PM: not the hour
    line = timers.evening(db, evening)["line"]
    assert line == "📊 Today: 3 shares → 3 items, 1 live on all 3 sites with all photos, 1 waiting for a size."
    assert sent == [("ops", line, False)]                                      # 9 PM: not the quiet hours
    assert timers.evening(db, evening + timedelta(minutes=30))["line"] is None   # once a day


def test_a_repost_task_answered_changes_no_listing(db, live, sent):
    _priced_item(db)
    # a refused form is the Mac's own re-check's, never chased (live 2026-10-20: the net re-queued the Tory Burch
    # before that re-check could run)
    core.sync(db, {"items": [], "listings": [{"item_id": "i_1", "marketplace": "vinted", "status": "failed",
                                              "error": "Vinted refused the form: x",
                                              "updated_at": (NOW - timedelta(hours=2)).isoformat()}]}, NOW)
    assert watch.unposted(db, NOW, "live") == {"asked": [], "said": []}
    core.sync(db, {"items": [], "listings": [{"item_id": "i_1", "marketplace": "vinted", "status": "failed",
                                              "error": "vinted photos: the form wasn't done in 3 min",
                                              "updated_at": (NOW - timedelta(hours=1, minutes=5)).isoformat()}]}, NOW)
    assert watch.unposted(db, NOW, "live")["asked"] == ["i_1|vinted"]
    [task] = core.take_tasks(db, ["vinted"], NOW)
    out = core.task_result(db, task["id"], "not_found", error="no such listing here", now=NOW)
    assert out["status"] == "not_found" and out["changed"]
    assert db.one("SELECT status FROM listings WHERE item_id = 'i_1' AND marketplace = 'vinted'")["status"] == "failed"
    assert sent == []                                                          # no take-down words for a retry
