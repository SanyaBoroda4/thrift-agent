"""WO41 on thrift-api: the Mac's price drops mirrored by /sync, the items' clock and stage, the dashboard's per-item
drops line and its "Drops due this week" card."""
from datetime import datetime, timedelta, timezone

from thrift_api import core, dashboard
from thrift_api.db import Database
from thrift_api.schema import migrate

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
URL = "https://poshmark.com/listing/Levis-501-Cutoff-Shorts-6702aa11bb22cc33dd44ee55"


def iso(t):
    return t.isoformat(timespec="seconds")


def test_the_api_keeps_the_drops_and_the_dashboard_shows_them():
    adb = Database("sqlite://")
    migrate(adb)
    now = NOW
    core.sync(adb, {"items": [{"id": "i_1", "title": "Levi's 501 Cutoff Shorts", "price": 25, "updated_at": iso(now),
                               "all_live_at": iso(now - timedelta(days=31)), "drop_stage": 1,
                               "next_drop_at": iso(now + timedelta(days=5))}],
                    "listings": [{"item_id": "i_1", "marketplace": "poshmark", "status": "posted", "url": URL,
                                  "price": 25, "price_live": 22, "updated_at": iso(now)}],
                    "price_changes": [{"id": "pc_i_1_poshmark_auto_1", "item_id": "i_1", "marketplace": "poshmark",
                                       "stage": "auto_1", "old_price": 25, "new_price": 22, "pct": 12, "status": "done",
                                       "done_at": iso(now - timedelta(days=1)), "updated_at": iso(now)}]}, now)
    data = core.dashboard_data(adb, now)
    item = next(i for i in data["items"] if i["id"] == "i_1")
    assert item["drops"]["stage"] == 1 and item["drops"]["price_now"] == 22 and item["drops"]["days_live"] == 31
    page = dashboard.render(data, now)
    assert "Drops due this week" in page and "$25 → $22" in page and "next drop" in page and "31 days" in page

    assert adb.one("SELECT drops FROM sales") is None                     # no sale yet


def test_a_sale_keeps_how_many_drops_the_item_had():
    adb = Database("sqlite://")
    migrate(adb)
    core.sync(adb, {"items": [{"id": "i_1", "title": "Levi's 501 Cutoff Shorts", "drop_stage": 2, "updated_at": iso(NOW)}],
                    "listings": [{"item_id": "i_1", "marketplace": m, "status": "posted", "url": URL + m,
                                  "updated_at": iso(NOW)} for m in ("poshmark", "depop")]}, NOW)
    adb.execute("INSERT INTO sales (id, marketplace, item_id, price, status, created_at) VALUES "
                "('s_1', 'poshmark', 'i_1', 20, 'matched', ?)", (iso(NOW),))
    core._create_tasks(adb, "s_1", "i_1", "poshmark", NOW)
    assert adb.one("SELECT drops, price FROM sales WHERE id = 's_1'") == {"drops": 2, "price": 20}
