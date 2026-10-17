"""WO40 — the UGG sold on Depop (2026-10-16) and still live on Poshmark and Vinted the next morning, nobody told: a Depop
sale of an item whose Depop listing was edited is still matched (SKU) and its take-downs made; a listing that reaches
the API's mirror after the sale gets its take-down; the 15-minute timer says ONE group line for a take-down not done 2
hours after the sale (and the dashboard shows it until it's done); the order sweep makes a missing take-down and hands
a failed one to the Mac once more; the API counts only a failure of a task it leased."""
from datetime import timedelta

import pytest
from apitools import ITEM, NOW, URLS, call, depop_sale, posh_sale, seed, tasks, texts

from thrift_api import core, dashboard, timers

LATE = "⚠️ Lacoste Tee White size M sold on Depop but is still listed on Poshmark and Vinted — please mark it sold there."


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-10-01T00:00:00+00:00")


def test_a_depop_sale_of_an_edited_listing_is_matched_by_sku_and_its_take_downs_made(db, live):
    # the Oct 13 cover edit: Depop's address in our records is not the one the email links to
    seed(db, urls={**URLS, "depop": "https://www.depop.com/products/myshop-lacoste-tee-white-edited-87d9/"},
         ids={"poshmark": "6702aa11bb22cc33dd44ee55", "depop": "myshop-lacoste-tee-white-edited-87d9", "vinted": "55"})
    out = core.process_email(db, depop_sale(), NOW)
    assert out["item_id"] == ITEM and out["sale_status"] == "delisting"
    assert [t["marketplace"] for t in tasks(db, out["sale_id"])] == ["poshmark", "vinted"]


def test_a_listing_the_mirror_lacked_at_the_sale_gets_its_take_down_when_it_syncs(db, live):
    seed(db, sites=("depop", "poshmark"))                       # Vinted not in the mirror yet
    sale_id = core.process_email(db, depop_sale(), NOW)["sale_id"]
    assert [t["marketplace"] for t in tasks(db, sale_id)] == ["poshmark"]
    seed(db, sites=("vinted",))                                  # the sync brings it after the sale
    assert [t["marketplace"] for t in tasks(db, sale_id)] == ["poshmark", "vinted"]
    seed(db, sites=("vinted",))                                  # again: never a second task
    assert len(tasks(db, sale_id)) == 2


def test_no_take_down_from_a_sync_outside_live_mode(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "replay")
    seed(db, sites=("depop",))
    core.process_email(db, depop_sale(), NOW)
    seed(db, sites=("vinted",))
    assert tasks(db) == []
    assert core.setting(db, "go_live_at") is None                # a read never starts the live clock


def test_a_take_down_not_done_in_two_hours_is_one_group_line_once_and_a_dashboard_flag(db, live, sent):
    seed(db)
    sale_id = core.process_email(db, depop_sale(), NOW)["sale_id"]
    sold_at = core.parse_time(db.one("SELECT sold_at FROM sales WHERE id = ?", (sale_id,))["sold_at"])
    sent.clear()
    assert timers.every_15_min(db, sold_at + timedelta(minutes=110))["late_takedowns"] == []
    assert timers.every_15_min(db, sold_at + timedelta(hours=2, minutes=5))["late_takedowns"] == [sale_id]
    assert texts(sent, "group") == [LATE]
    assert timers.every_15_min(db, sold_at + timedelta(hours=3))["late_takedowns"] == []        # once
    assert texts(sent, "group") == [LATE]
    page = dashboard.render(core.dashboard_data(db, sold_at + timedelta(hours=3)), sold_at + timedelta(hours=3))
    assert "Sold, still listed" in page and "still listed on Poshmark, Vinted" in page
    for task in tasks(db, sale_id):                              # both taken down: the flag goes
        db.execute("UPDATE delist_tasks SET status = 'running' WHERE id = ?", (task["id"],))
        core.task_result(db, task["id"], "done", now=sold_at + timedelta(hours=4))
    page = dashboard.render(core.dashboard_data(db, sold_at + timedelta(hours=4)), sold_at + timedelta(hours=4))
    assert "Sold, still listed" not in page and "still listed on" not in page


def test_a_failed_take_down_counts_only_on_a_lease_three_leases_ask_the_owner(db, live, sent):
    """The live miss: three quick failures on ONE lease — the API counted the first, refused the next two, and the
    task stayed 'running' (re-leased every 10 minutes, never a word). The Mac now waits for its next lease."""
    seed(db)
    sale_id = core.process_email(db, depop_sale(), NOW)["sale_id"]
    posh = next(t for t in tasks(db, sale_id) if t["marketplace"] == "poshmark")
    sent.clear()
    for n in range(1, 4):
        leased = core.take_tasks(db, ["poshmark"], NOW + timedelta(minutes=3 * n))
        assert [t["id"] for t in leased] == [posh["id"]]
        out = core.task_result(db, posh["id"], "failed", error="the take-down didn't take",
                               now=NOW + timedelta(minutes=3 * n))
        assert out["attempts"] == n
    assert out["status"] == "failed"
    assert "Couldn't take Lacoste Tee White size M down on Poshmark — please mark it sold there." in texts(sent, "group")


def test_the_order_sweep_makes_a_missing_take_down_and_hands_a_failed_one_out_once_more(db, live):
    seed(db, sites=("poshmark", "depop"))
    sale_id = core.process_email(db, posh_sale(), NOW)["sale_id"]
    depop_task = tasks(db, sale_id)[0]
    db.execute("UPDATE delist_tasks SET status = 'failed', attempts = 3 WHERE id = ?", (depop_task["id"],))
    seed(db, sites=("vinted",), status="queued")                 # not posted: nothing to take down there
    db.execute("INSERT INTO listings (item_id, marketplace, status, url, updated_at) VALUES (?, 'vinted', 'posted', ?, ?) "
               "ON CONFLICT (item_id, marketplace) DO UPDATE SET status = 'posted'", (ITEM, URLS["vinted"], NOW.isoformat()))
    order = {"order_id": "6702bb11cc22dd33ee44ff55", "status": "to_ship", "status_text": "Awaiting Shipment",
             "title": "Lacoste Tee White size M", "price": 35, "sold_at": "2026-10-08T14:05:00+00:00"}
    status, out, _ = call(db, "POST", "/orders", {"marketplace": "poshmark", "orders": [order]})
    assert status == 200, out
    assert "take-downs" in out["results"][0]["action"]
    by_site = {t["marketplace"]: t for t in tasks(db, sale_id)}
    assert by_site["depop"]["status"] == "pending" and by_site["vinted"]["status"] == "pending"
    db.execute("UPDATE delist_tasks SET status = 'failed', attempts = 3 WHERE id = ?", (depop_task["id"],))
    call(db, "POST", "/orders", {"marketplace": "poshmark", "orders": [order]})
    assert {t["marketplace"]: t["status"] for t in tasks(db, sale_id)}["depop"] == "failed"      # once only
