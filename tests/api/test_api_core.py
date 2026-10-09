"""thrift_api.core (WO33): the three modes, a sale matched / unmatched / sold twice, the follow-up emails, the
take-down tasks and the Mac's answers, the Mac's events, a manual match, the sync, the views."""
from datetime import timedelta

import pytest
from apitools import (ITEM, NOW, TITLE, URLS, depop_sale, email, followup, posh_sale, sale, seed, tasks, texts, utc,
                      vinted_sale)

from thrift_api import core, dashboard, timers, util
from thrift_api.util import BadRequest, Conflict, NotFound

GO_LIVE = "2026-10-01T00:00:00+00:00"
ZARA = "i_261004_000002"
ZARA_TITLE = "Zara Linen Shirt Blue size S"
POSH_ORDER = "6702bb11cc22dd33ee44ff55"
SOLD_POSH = "💰 Sold on Poshmark: Lacoste Tee White size M — $35. Ship by Thu Oct 15."


@pytest.fixture
def live(db, monkeypatch):
    """Live since Oct 1: the sample emails (Oct 8) are acted on."""
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", GO_LIVE)


def count(db, table: str) -> int:
    return db.one(f"SELECT COUNT(*) AS n FROM {table}")["n"]


def listing_states(db) -> dict:
    return {row["marketplace"]: row["status"] for row in db.query("SELECT marketplace, status FROM listings "
                                                                    "WHERE item_id = ?", (ITEM,))}


# --- the modes -----------------------------------------------------------------------------------------------------

def test_the_mode_comes_from_sales_mode(monkeypatch):
    assert util.mode() == "replay"                                    # unset: replay
    for value, mode in (("LIVE ", "live"), ("off", "off"), ("on", "replay"), ("", "replay")):
        monkeypatch.setenv("SALES_MODE", value)
        assert util.mode() == mode


def test_off_stores_and_classifies_only(db, monkeypatch, sent):
    monkeypatch.setenv("SALES_MODE", "off")
    seed(db)
    out = core.process_email(db, posh_sale(), NOW)
    assert (out["status"], out["kind"], out["handling"]) == ("new", "SALE", "off")
    assert core.process_email(db, email("poshmark", "jane liked your listing", "Likes!", "like-1"), NOW)["status"] == \
        "ignored"
    assert (count(db, "sales"), count(db, "delist_tasks"), sent) == (0, 0, [])
    rows = {row["message_id"]: row for row in db.query("SELECT * FROM email_events")}
    assert (rows["posh-sale-1"]["kind"], rows["posh-sale-1"]["status"]) == ("SALE", "new")
    assert "Lacoste" in rows["posh-sale-1"]["raw_text"]
    assert (rows["like-1"]["kind"], rows["like-1"]["raw_text"]) == ("OTHER", None)       # OTHER's text is never kept
    assert core.process_email(db, posh_sale(), NOW)["status"] == "duplicate"
    assert core.setting(db, "go_live_at") is None


def test_replay_sends_only_to_ops_and_never_makes_tasks(db, monkeypatch, sent):
    monkeypatch.setenv("SALES_MODE", "replay")
    seed(db)
    out = core.process_email(db, posh_sale(), NOW)
    assert (out["handling"], out["sale_status"], out["item_id"], out["tasks"]) == ("replay", "matched", ITEM, [])
    second = core.process_email(db, depop_sale(), NOW)
    assert second["sale_status"] == "double_sale"
    assert count(db, "delist_tasks") == 0 and count(db, "sales") == 2
    assert sent == [("ops", "[replay] " + SOLD_POSH, False),
                    ("ops", "[replay] ⚠️ Sold twice: Lacoste Tee White size M sold on Poshmark and Depop. "
                            "Cancel the Depop order in its app.", False)]
    assert core.take_tasks(db, ["depop", "vinted"], NOW) == []
    assert core.setting(db, "go_live_at") is None                     # replay never starts the live clock


def test_live_acts_only_on_emails_after_go_live(db, monkeypatch, sent):
    monkeypatch.setenv("SALES_MODE", "live")
    seed(db)
    seed(db, ZARA, ZARA_TITLE, sites=("poshmark", "vinted"), urls={}, ids={})
    assert core.setting(db, "go_live_at") is None
    old = core.process_email(db, posh_sale(date="2026-10-08T14:05:00Z"), NOW)      # the first live call, at 15:00
    assert core.setting(db, "go_live_at") == "2026-10-08T15:00:00+00:00"
    assert (old["handling"], old["sale_status"], old["tasks"]) == ("history", "matched", [])
    assert sent == [] and count(db, "delist_tasks") == 0 and count(db, "sales") == 1
    new = core.process_email(db, vinted_sale("vinted-zara", title=ZARA_TITLE, url=None, date="2026-10-08T15:30:00Z"),
                             utc(2026, 10, 8, 15, 31))
    assert (new["handling"], new["sale_status"], new["matched_by"]) == ("live", "delisting", "title")
    assert [(t["item_id"], t["marketplace"]) for t in tasks(db)] == [(ZARA, "poshmark")]
    assert texts(sent) == ["💰 Sold on Vinted: Zara Linen Shirt Blue size S — $35. Ship by Tue Oct 13."]
    later = core.process_email(db, followup("poshmark", "SHIPPED", "ship-old", order=POSH_ORDER,
                                            date="2026-10-08T14:30:00Z"), utc(2026, 10, 8, 16, 0))
    assert later["handling"] == "history" and sale(db, old["sale_id"])["shipped_at"] == "2026-10-08T14:30:00+00:00"
    assert len(sent) == 1                                              # history: recorded, never announced


def test_a_second_post_is_a_duplicate(db, live, sent):
    seed(db)
    core.process_email(db, posh_sale(), NOW)
    before = (count(db, "sales"), count(db, "delist_tasks"), count(db, "email_events"), len(sent))
    assert core.process_email(db, posh_sale(), NOW) == {"message_id": "posh-sale-1", "kind": "SALE",
                                                        "marketplace": "poshmark", "status": "duplicate"}
    assert (count(db, "sales"), count(db, "delist_tasks"), count(db, "email_events"), len(sent)) == before


def test_bad_payloads(db, live):
    for payload in ({}, {"message_id": "  "}, {"message_id": "x" * 300}, ["not", "a", "dict"]):
        with pytest.raises(BadRequest):
            core.process_email(db, payload, NOW)


def test_a_sender_that_is_no_marketplace_is_ignored(db, live, sent):
    out = core.process_email(db, {"message_id": "m1", "from": "Jane <jane@gmail.com>", "subject": "You made a sale!",
                                  "text": "Item: Tee", "date": None}, NOW)
    assert (out["status"], out["kind"], out["marketplace"]) == ("ignored", "OTHER", None)
    assert db.one("SELECT received_at FROM email_events")["received_at"] == "2026-10-08T15:00:00+00:00"  # no date: now


# --- a sale --------------------------------------------------------------------------------------------------------

def test_a_poshmark_sale_matched_by_its_address(db, live, sent):
    seed(db)
    out = core.process_email(db, posh_sale(), NOW)
    assert (out["status"], out["sale_status"], out["item_id"], out["matched_by"]) == ("matched", "delisting", ITEM,
                                                                                       "listing")
    assert [(t["marketplace"], t["status"], t["listing_url"], t["sale_id"]) for t in tasks(db)] == [
        ("depop", "pending", URLS["depop"], out["sale_id"]), ("vinted", "pending", URLS["vinted"], out["sale_id"])]
    row = sale(db, out["sale_id"])
    assert (row["ship_by"], row["ship_by_source"], row["price"], row["order_id"], row["title_seen"], row["sold_at"]) == (
        "2026-10-15", "rule", 35.0, POSH_ORDER, TITLE, "2026-10-08T14:05:00+00:00")
    assert sent == [("group", SOLD_POSH, False)]
    assert listing_states(db) == dict.fromkeys(("poshmark", "depop", "vinted"), "posted")    # left as they are
    event = db.one("SELECT * FROM email_events WHERE message_id = 'posh-sale-1'")
    assert (event["status"], event["attempts"]) == ("matched", 1)
    for private in ("Jane", "janeq", "Secret", "Springfield"):
        assert private not in event["parsed_json"]


def test_a_depop_sale_matched_by_our_sku(db, live, sent):
    seed(db, urls={}, ids={})                                         # no addresses: only the SKU can tell
    out = core.process_email(db, depop_sale(), NOW)
    assert (out["item_id"], out["matched_by"]) == (ITEM, "sku")
    assert [t["marketplace"] for t in tasks(db)] == ["poshmark", "vinted"]
    assert texts(sent) == ["💰 Sold on Depop: Lacoste Tee White size M — $35. Ship by Tue Oct 13."]


def test_a_vinted_sale_matched_by_title_with_its_stated_date(db, live, sent):
    seed(db)
    out = core.process_email(db, vinted_sale(url=None, ship_by="Oct 14"), NOW)
    assert (out["item_id"], out["matched_by"], out["ship_by"]) == (ITEM, "title", "2026-10-14")
    assert sale(db, out["sale_id"])["ship_by_source"] == "email"
    assert [t["marketplace"] for t in tasks(db)] == ["poshmark", "depop"]


def test_only_posted_listings_are_taken_down(db, live):
    seed(db, sites=("poshmark",))
    seed(db, sites=("depop",), status="failed")
    seed(db, sites=("vinted",), status="delisted")
    out = core.process_email(db, posh_sale(), NOW)
    assert (out["sale_status"], out["tasks"]) == ("matched", [])      # nothing else is live


def test_an_unmatched_sale(db, live, sent):
    seed(db)
    out = core.process_email(db, posh_sale("posh-x", title="Nike Air Max 90 size 9", url=None, order=None), NOW)
    assert (out["status"], out["sale_status"], out["item_id"]) == ("unmatched", "unmatched", None)
    assert texts(sent, "group") == ["💰 Sold on Poshmark: Nike Air Max 90 size 9 — $35. Ship by Thu Oct 15."]
    assert texts(sent, "ops") == [f"Unmatched sale on Poshmark ({out['sale_id']}): Congratulations! Your item has sold"
                                  f"\n{core.UNMATCHED_HOW}"]       # WO33: a reply settles it, never a command
    assert tasks(db) == []


def test_a_second_email_about_the_same_sale_only_fills_in(db, live, sent):
    seed(db)
    first = core.process_email(db, posh_sale(), NOW)
    sent.clear()
    again = email("poshmark", "You made a sale!", f"Order ID: {POSH_ORDER}\nItem: {TITLE}\nPlease ship by Oct 14.",
                  "posh-sale-2")
    out = core.process_email(db, again, NOW)
    assert (out["status"], out["sale_id"], out["duplicate"]) == ("parsed", first["sale_id"], True)
    assert (count(db, "sales"), len(tasks(db)), sent) == (1, 2, [])
    row = sale(db, first["sale_id"])
    assert (row["ship_by"], row["ship_by_source"]) == ("2026-10-14", "email")


def test_sold_twice(db, live, sent):
    seed(db)
    core.process_email(db, posh_sale(), NOW)
    sent.clear()
    out = core.process_email(db, depop_sale(), NOW)
    assert (out["sale_status"], out["tasks"]) == ("double_sale", [])
    assert texts(sent) == ["⚠️ Sold twice: Lacoste Tee White size M sold on Poshmark and Depop. "
                           "Cancel the Depop order in its app."]
    assert len(tasks(db)) == 2                                        # the first sale's take-downs only


# --- shipped, delivered, cancelled ---------------------------------------------------------------------------------

def test_shipped_stops_the_reminders_and_tells_only_ops(db, live, sent):
    seed(db)
    first = core.process_email(db, posh_sale(), NOW)
    sent.clear()
    out = core.process_email(db, followup("poshmark", "SHIPPED", "posh-ship", order=POSH_ORDER,
                                          date="2026-10-09T16:00:00Z"), utc(2026, 10, 9, 16, 1))
    assert (out["status"], out["sale_id"]) == ("matched", first["sale_id"])
    assert sale(db, first["sale_id"])["shipped_at"] == "2026-10-09T16:00:00+00:00"
    assert sent == [("ops", "Shipped: Lacoste Tee White size M (Poshmark).", False)]
    assert timers.reminders(db, utc(2026, 10, 14, 13, 0), "live") == []          # the day before: nothing
    assert timers.reminders(db, utc(2026, 10, 16, 13, 0), "live") == []          # and never overdue


def test_shipped_found_by_title_when_no_order_is_named(db, live, sent):
    seed(db)
    first = core.process_email(db, depop_sale(), NOW)
    out = core.process_email(db, followup("depop", "SHIPPED", "depop-ship", title=TITLE), NOW)
    assert out["sale_id"] == first["sale_id"] and sale(db, first["sale_id"])["shipped_at"]


def test_delivered_closes_the_sale(db, live, sent):
    seed(db)
    first = core.process_email(db, vinted_sale(), NOW)
    sent.clear()
    core.process_email(db, followup("vinted", "DELIVERED", "v-del", order="9876543210", date="2026-10-12T15:00:00Z"),
                       utc(2026, 10, 12, 15, 1))
    row = sale(db, first["sale_id"])
    assert (row["status"], row["delivered_at"], row["shipped_at"]) == ("done", "2026-10-12T15:00:00+00:00",
                                                                       "2026-10-12T15:00:00+00:00")
    assert sent == []


def test_cancelled_says_so_and_never_relists(db, live, sent):
    seed(db)
    first = core.process_email(db, posh_sale(), NOW)
    depop_task, vinted_task = core.take_tasks(db, ["depop", "vinted"], NOW)
    core.task_result(db, depop_task["id"], "done", now=NOW)
    sent.clear()
    cancel = followup("poshmark", "CANCELLED", "posh-cancel", order=POSH_ORDER)
    assert core.process_email(db, cancel, NOW)["status"] == "matched"
    assert sale(db, first["sale_id"])["status"] == "cancelled"
    assert texts(sent, "group") == ["↩️ Lacoste Tee White size M's sale on Poshmark was cancelled."]
    assert texts(sent, "ops") == [f"Cancelled: Lacoste Tee White size M on Poshmark ({first['sale_id']}, order "
                                  f"{POSH_ORDER}). Take-downs called off: Vinted. Already taken down on Depop — relist "
                                  "by hand if wanted. Nothing is relisted automatically."]
    assert {t["marketplace"]: t["status"] for t in tasks(db)} == {"depop": "done", "vinted": "cancelled"}
    assert listing_states(db) == {"poshmark": "posted", "depop": "delisted", "vinted": "posted"}
    assert core.task_result(db, vinted_task["id"], "done", now=NOW)["changed"] is False     # called off: final
    sent.clear()
    core.process_email(db, followup("poshmark", "CANCELLED", "posh-cancel-2", order=POSH_ORDER), NOW)
    assert sent == []                                                   # cancelled once is enough


def test_when_the_first_of_two_sales_is_cancelled_the_other_stands(db, live, sent):
    seed(db)
    first = core.process_email(db, posh_sale(), NOW)
    second = core.process_email(db, depop_sale(), NOW)
    sent.clear()
    core.process_email(db, followup("poshmark", "CANCELLED", "posh-cancel", order=POSH_ORDER), NOW)
    assert sale(db, second["sale_id"])["status"] == "delisting"
    assert {t["marketplace"]: (t["status"], t["sale_id"]) for t in tasks(db)} == {
        "depop": ("cancelled", first["sale_id"]), "vinted": ("pending", second["sale_id"])}
    assert texts(sent, "group") == ["↩️ Lacoste Tee White size M's sale on Poshmark was cancelled. "
                                    "The Depop sale stands — ship it by Tue Oct 13."]


def test_a_followup_without_its_sale(db, live, sent):
    out = core.process_email(db, followup("vinted", "DELIVERED", "v-del", order="000111"), NOW)
    assert out["status"] == "unmatched"
    assert sent == [("ops", "No sale found for a delivered email on Vinted (no open sale matches): "
                            "Your order was delivered", False)]


def test_a_parse_failure_is_kept_for_a_retry(db, live, sent):
    bad = email("poshmark", "You made a sale!", "Congrats! Ship within 7 days.", "posh-bad")
    out = core.process_email(db, bad, NOW)
    assert out["status"] == "failed" and "no title" in out["error"]
    event = db.one("SELECT * FROM email_events WHERE message_id = 'posh-bad'")
    assert (event["status"], event["attempts"], event["raw_text"]) == ("failed", 1, "Congrats! Ship within 7 days.")
    assert (sent, count(db, "sales")) == ([], 0)
    assert core.process_email(db, bad, NOW)["status"] == "duplicate"


def test_an_unexpected_error_rolls_back_is_kept_and_retried(db, live, sent, monkeypatch):
    seed(db)
    real = core.match.match_sale

    def broken(*args):
        raise RuntimeError("database hiccup")
    monkeypatch.setattr(core.match, "match_sale", broken)
    out = core.process_email(db, posh_sale(), NOW)
    assert (out["status"], out["error"]) == ("failed", "RuntimeError: database hiccup")
    assert (count(db, "sales"), count(db, "delist_tasks"), sent) == (0, 0, [])
    monkeypatch.setattr(core.match, "match_sale", real)
    again = core.retry_email(db, "posh-sale-1", NOW)
    assert (again["status"], again["sale_status"]) == ("matched", "delisting")
    assert db.one("SELECT attempts FROM email_events")["attempts"] == 2
    assert texts(sent) == [SOLD_POSH]
    assert core.retry_email(db, "posh-sale-1", NOW)["retried"] is False          # no longer failed
    with pytest.raises(NotFound):
        core.retry_email(db, "nope", NOW)


# --- take-downs ----------------------------------------------------------------------------------------------------

def test_tasks_are_leased_and_offered_again_when_the_lease_runs_out(db, live):
    seed(db)
    core.process_email(db, posh_sale(), NOW)
    got = core.take_tasks(db, ["depop", "vinted"], NOW)
    assert [(t["marketplace"], t["item_id"], t["title"], t["listing_url"], t["attempts"]) for t in got] == [
        ("depop", ITEM, TITLE, URLS["depop"], 0), ("vinted", ITEM, TITLE, URLS["vinted"], 0)]
    assert got[0]["lease_until"] == "2026-10-08T15:10:00+00:00"
    assert {t["status"] for t in tasks(db)} == {"running"}
    assert core.take_tasks(db, ["depop", "vinted"], utc(2026, 10, 8, 15, 9)) == []        # still leased
    assert [t["marketplace"] for t in core.take_tasks(db, ["vinted"], utc(2026, 10, 8, 15, 11))] == ["vinted"]
    assert core.take_tasks(db, ["poshmark"], NOW) == [] and core.take_tasks(db, [], NOW) == []


def test_no_tasks_are_handed_out_outside_live(db, live, monkeypatch):
    seed(db)
    core.process_email(db, posh_sale(), NOW)
    monkeypatch.setenv("SALES_MODE", "replay")
    assert core.take_tasks(db, ["depop", "vinted"], NOW) == []
    monkeypatch.setenv("SALES_MODE", "off")
    assert core.take_tasks(db, ["depop", "vinted"], NOW) == []
    assert {t["status"] for t in tasks(db)} == {"pending"}


def test_taken_down_everywhere(db, live, sent):
    seed(db)
    first = core.process_email(db, depop_sale(), NOW)
    sent.clear()
    posh, vinted = core.take_tasks(db, ["poshmark", "vinted"], NOW)
    assert core.task_result(db, posh["id"], "done", evidence="shots/p.png", now=NOW) == {
        "id": posh["id"], "status": "done", "attempts": 0, "changed": True}
    assert sent == []                                                    # Vinted still to go
    core.task_result(db, vinted["id"], "done", now=NOW)
    assert texts(sent) == ["✓ Lacoste Tee White size M taken down on Poshmark and Vinted."]
    assert listing_states(db) == {"poshmark": "delisted", "depop": "posted", "vinted": "delisted"}
    assert sale(db, first["sale_id"])["status"] == "matched"             # no longer delisting
    assert db.one("SELECT evidence FROM delist_tasks WHERE id = ?", (posh["id"],))["evidence"] == "shots/p.png"
    assert core.task_result(db, vinted["id"], "done", now=NOW)["changed"] is False          # repeated: nothing
    assert len(sent) == 1


def test_not_found_counts_as_taken_down(db, live, sent):
    seed(db)
    core.process_email(db, posh_sale(), NOW)
    sent.clear()
    depop, vinted = core.take_tasks(db, ["depop", "vinted"], NOW)
    core.task_result(db, depop["id"], "not_found", error="no such listing", now=NOW)
    core.task_result(db, vinted["id"], "done", now=NOW)
    assert texts(sent, "ops") == [f"Take-down: Lacoste Tee White size M wasn't found on Depop ({depop['id']}): "
                                  "no such listing."]
    assert texts(sent, "group") == ["✓ Lacoste Tee White size M taken down on Depop and Vinted."]
    assert listing_states(db)["depop"] == "posted"                       # not found: nothing to record as delisted


def test_three_failures_ask_the_owner(db, live, sent):
    seed(db)
    first = core.process_email(db, posh_sale(), NOW)
    [depop] = core.take_tasks(db, ["depop"], NOW)
    sent.clear()
    when = NOW
    for attempt in (1, 2, 3):
        [task] = core.take_tasks(db, ["vinted"], when)
        out = core.task_result(db, task["id"], "failed", error="Hide button not found", now=when)
        assert (out["status"], out["attempts"]) == ("pending" if attempt < 3 else "failed", attempt)
        when += timedelta(minutes=15)
    assert texts(sent, "group") == ["Couldn't take Lacoste Tee White size M down on Vinted — please mark it sold there."]
    assert texts(sent, "ops") == [f"Take-down of Lacoste Tee White size M on Vinted left to the owner ({task['id']}, "
                                  "failed 3 times): Hide button not found."]
    assert core.take_tasks(db, ["vinted"], when) == []
    assert core.task_result(db, task["id"], "failed", now=when)["changed"] is False
    sent.clear()
    core.task_result(db, depop["id"], "done", now=when)
    assert sent == []                                                    # one site is left to the owner: no "✓"
    assert sale(db, first["sale_id"])["status"] == "matched"


def test_a_manual_take_down_asks_the_owner_at_once(db, live, sent):
    seed(db, sites=("poshmark", "vinted"))
    core.process_email(db, posh_sale(), NOW)
    sent.clear()
    [task] = core.take_tasks(db, ["vinted"], NOW)
    out = core.task_result(db, task["id"], "failed", error="Hide isn't recorded yet", now=NOW, manual=True)
    assert (out["status"], out["attempts"], out["changed"]) == ("failed", 0, True)
    assert texts(sent, "group") == ["Couldn't take Lacoste Tee White size M down on Vinted — please mark it sold there."]
    assert "not automated on this site yet" in texts(sent, "ops")[0]


def test_a_repeated_failure_report_counts_once(db, live):
    seed(db)
    core.process_email(db, posh_sale(), NOW)
    [task] = core.take_tasks(db, ["vinted"], NOW)
    assert core.task_result(db, task["id"], "failed", now=NOW)["attempts"] == 1
    assert core.task_result(db, task["id"], "failed", now=NOW) == {"id": task["id"], "status": "pending",
                                                                   "attempts": 1, "changed": False}


def test_task_result_errors(db, live):
    with pytest.raises(NotFound):
        core.task_result(db, "t_000000000000", "done", now=NOW)
    with pytest.raises(BadRequest):
        core.task_result(db, "t_000000000000", "maybe", now=NOW)


def test_our_delisting_outlives_an_older_sync(db, live):
    seed(db)
    core.process_email(db, posh_sale(), NOW)
    [task] = core.take_tasks(db, ["depop"], NOW)
    core.task_result(db, task["id"], "done", now=NOW)
    stale = {"item_id": ITEM, "marketplace": "depop", "status": "posted", "updated_at": "2026-10-08T12:00:00Z"}
    assert core.sync(db, {"listings": [stale]})["listings"]["unchanged"] == 1
    assert listing_states(db)["depop"] == "delisted"


# --- the Mac's events ----------------------------------------------------------------------------------------------

def test_the_mac_marks_a_sale_shipped(db, live, sent):
    seed(db)
    first = core.process_email(db, posh_sale(), NOW)
    sent.clear()
    out = core.mac_event(db, {"kind": "shipped", "words": "lacoste WHITE"}, utc(2026, 10, 9, 18, 0))
    assert out == {"status": "shipped", "matched": first["sale_id"], "title": TITLE, "marketplace": "poshmark"}
    assert sale(db, first["sale_id"])["shipped_at"] == "2026-10-09T18:00:00+00:00"
    assert sent == [("group", "✓ Marked shipped: Lacoste Tee White size M", False)]
    again = core.mac_event(db, {"kind": "shipped", "words": ["lacoste"]}, utc(2026, 10, 9, 18, 5))
    assert (again["status"], again["matched"], again["title"]) == ("no_match", None, None)
    assert "lacoste" in again["reason"] and len(sent) == 1


def test_the_mac_shipping_several_or_none_says_why(db, live, sent):
    seed(db)
    seed(db, ZARA, "Zara White Tee size S", sites=("vinted",), urls={}, ids={})
    core.process_email(db, posh_sale(), NOW)
    core.process_email(db, vinted_sale(title="Zara White Tee size S", url=None), NOW)
    sent.clear()
    out = core.mac_event(db, {"kind": "shipped", "words": "white tee"}, NOW)
    assert (out["status"], out["matched"], len(out["candidates"])) == ("ambiguous", None, 2)
    assert core.mac_event(db, {"kind": "shipped", "words": "gucci"}, NOW)["status"] == "no_match"
    assert sent == []


def test_the_mac_found_a_sale_on_a_site(db, live, sent):
    seed(db)
    core.process_email(db, posh_sale(), NOW)
    sent.clear()
    found = {"kind": "sold_found", "marketplace": "depop", "listing_url": URLS["depop"], "item_id": ITEM, "price": 35}
    out = core.mac_event(db, found, NOW)
    assert (out["message_id"], out["sale_status"], out["item_id"]) == ("mac:depop:thriftshop-lacoste-tee-white",
                                                                       "double_sale", ITEM)
    assert texts(sent) == ["⚠️ Sold twice: Lacoste Tee White size M sold on Poshmark and Depop. "
                           "Cancel the Depop order in its app."]
    assert core.mac_event(db, found, NOW)["status"] == "duplicate"


def test_a_sale_only_the_mac_saw(db, live, sent):
    seed(db)
    out = core.mac_event(db, {"kind": "sold_found", "marketplace": "vinted", "listing_id": "7012345678",
                              "sold_at": "2026-10-08T13:00:00Z"}, NOW)
    assert (out["sale_status"], out["matched_by"]) == ("delisting", "listing")
    assert [t["marketplace"] for t in tasks(db)] == ["poshmark", "depop"]
    assert texts(sent) == ["💰 Sold on Vinted: Lacoste Tee White size M. Ship by Fri Oct 16."]  # no price; Columbus Day skipped


def test_not_for_sale_takes_the_item_down_elsewhere_without_a_sale(db, live, sent):
    seed(db)
    body = {"kind": "not_for_sale", "item_id": ITEM, "marketplace": "poshmark", "url": URLS["poshmark"]}
    out = core.mac_event(db, body, NOW)
    assert (out["tasks"], len(out["task_ids"]), out["mode"]) == (2, 2, "live")
    assert [(t["marketplace"], t["sale_id"], t["status"]) for t in tasks(db)] == [("depop", None, "pending"),
                                                                                  ("vinted", None, "pending")]
    assert (count(db, "sales"), sent) == (0, [])
    assert core.mac_event(db, body, NOW)["tasks"] == 0                                    # idempotent
    depop, vinted = core.take_tasks(db, ["depop", "vinted"], NOW)
    assert (depop["title"], depop["listing_url"]) == (TITLE, URLS["depop"])
    core.task_result(db, depop["id"], "done", now=NOW)
    core.task_result(db, vinted["id"], "failed", now=NOW)                                 # back to pending
    assert sent == []                                                                     # no sale: nothing to say
    assert listing_states(db) == {"poshmark": "posted", "depop": "delisted", "vinted": "posted"}
    assert core.mac_event(db, body, NOW)["tasks"] == 0                    # Vinted's take-down is still under way
    db.execute("UPDATE delist_tasks SET status = 'cancelled' WHERE id = ?", (vinted["id"],))
    again = core.mac_event(db, body, NOW)
    assert again["tasks"] == 1 and tasks(db)[-1]["marketplace"] == "vinted"


def test_a_sale_never_doubles_a_take_down_under_way(db, live, sent):
    seed(db)
    core.mac_event(db, {"kind": "not_for_sale", "item_id": ITEM, "marketplace": "poshmark"}, NOW)
    out = core.process_email(db, posh_sale(), NOW)
    assert (out["sale_status"], out["tasks"]) == ("matched", [])
    assert len(tasks(db)) == 2


def test_not_for_sale_outside_live_does_nothing(db, monkeypatch):
    seed(db)
    for mode in ("replay", "off"):
        monkeypatch.setenv("SALES_MODE", mode)
        out = core.mac_event(db, {"kind": "not_for_sale", "item_id": ITEM, "marketplace": "poshmark"}, NOW)
        assert (out["tasks"], out["mode"]) == (0, mode)
    assert tasks(db) == []


def test_mac_event_errors(db, live):
    for body in ({"kind": "lost"}, {"kind": "shipped", "words": " "}, {"kind": "sold_found", "marketplace": "ebay",
                                                                         "listing_id": "1"},
                 {"kind": "sold_found", "marketplace": "depop"}, ["x"],
                 {"kind": "not_for_sale", "marketplace": "poshmark"},
                 {"kind": "not_for_sale", "item_id": ITEM, "marketplace": "etsy"}):
        with pytest.raises(BadRequest):
            core.mac_event(db, body, NOW)


def test_the_sold_list_for_the_mac(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-10-08T12:00:00+00:00")
    seed(db)
    seed(db, ZARA, ZARA_TITLE, sites=("poshmark",), urls={}, ids={})
    seed(db, "i_old", "Old Navy Wool Coat size L", sites=("vinted",), urls={}, ids={})
    core.process_email(db, posh_sale(), NOW)                                          # 14:05: after go-live
    core.process_email(db, vinted_sale("v-old", title="Old Navy Wool Coat size L", url=None,
                                       date="2026-10-08T11:00:00Z"), NOW)                # before: history
    core.process_email(db, posh_sale("posh-zara", title=ZARA_TITLE, url=None, order="aaaa1111bbbb2222cccc3333"), NOW)
    core.process_email(db, followup("poshmark", "CANCELLED", "zara-cancel", order="aaaa1111bbbb2222cccc3333"), NOW)
    core.process_email(db, posh_sale("posh-x", title="Mystery Tee", url=None, order=None), NOW)     # unmatched
    assert core.sold_items(db, NOW) == [ITEM]
    assert core.sold_items(db, NOW + timedelta(days=31)) == []                        # past 30 days
    monkeypatch.setenv("SALES_MODE", "replay")
    assert core.sold_items(db, NOW) == []


# --- a manual match ------------------------------------------------------------------------------------------------

def test_a_manual_match_makes_the_take_downs(db, live, sent):
    seed(db)
    seed(db, ZARA, ZARA_TITLE, sites=("poshmark",), urls={}, ids={})
    lost = core.process_email(db, posh_sale("posh-x", title="Mystery Tee", url=None, order=None), NOW)
    sent.clear()
    out = core.match_sale(db, lost["sale_id"], ITEM, NOW)
    assert (out["status"], len(out["tasks"]), out["changed"]) == ("delisting", 2, True)
    assert texts(sent) == [f"Matched the Poshmark sale {lost['sale_id']} to Lacoste Tee White size M (delisting); "
                           "take-downs: Depop and Vinted."]
    assert core.match_sale(db, lost["sale_id"], ITEM, NOW)["changed"] is False
    with pytest.raises(Conflict):
        core.match_sale(db, lost["sale_id"], ZARA, NOW)                 # it has take-downs now
    with pytest.raises(NotFound):
        core.match_sale(db, "s_000000000000", ITEM, NOW)
    with pytest.raises(NotFound):
        core.match_sale(db, lost["sale_id"], "i_000000_000000", NOW)
    with pytest.raises(BadRequest):
        core.match_sale(db, lost["sale_id"], " ", NOW)


def test_a_manual_match_in_replay_makes_no_take_downs(db, monkeypatch, sent):
    monkeypatch.setenv("SALES_MODE", "replay")
    seed(db)
    lost = core.process_email(db, posh_sale("posh-x", title="Mystery Tee", url=None, order=None), NOW)
    out = core.match_sale(db, lost["sale_id"], ITEM, NOW)
    assert (out["status"], out["tasks"]) == ("matched", []) and tasks(db) == []
    assert all(chat == "ops" for chat, _, _ in sent)


# --- the sync and the views ----------------------------------------------------------------------------------------

def test_sync_last_write_wins(db):
    item = {"id": ITEM, "title": "Old", "price": "35", "updated_at": "2026-10-05T10:00:00Z"}
    assert core.sync(db, {"items": [item]})["items"] == {"inserted": 1, "updated": 0, "unchanged": 0}
    assert core.sync(db, {"items": [item]})["items"] == {"inserted": 0, "updated": 0, "unchanged": 1}
    assert core.sync(db, {"items": [{**item, "title": "New", "updated_at": "2026-10-06T10:00:00Z"}]})["items"][
        "updated"] == 1
    assert core.sync(db, {"items": [item]})["items"]["unchanged"] == 1                    # older: ignored
    core.sync(db, {"items": [{"id": ITEM, "brand": "Lacoste", "updated_at": "2026-10-07T10:00:00Z"}]})
    row = db.one("SELECT * FROM items")
    assert (row["title"], row["brand"], row["price"], row["updated_at"]) == ("New", "Lacoste", 35.0,
                                                                             "2026-10-07T10:00:00+00:00")
    listing = {"item_id": ITEM, "marketplace": "Depop", "status": "POSTED", "url": URLS["depop"],
               "posted_at": "2026-10-06T09:00:00.123Z", "updated_at": "2026-10-06T10:00:00Z"}
    assert core.sync(db, {"listings": [listing, listing]})["listings"] == {"inserted": 1, "updated": 0, "unchanged": 1}
    row = db.one("SELECT * FROM listings")
    assert (row["marketplace"], row["status"], row["posted_at"]) == ("depop", "posted", "2026-10-06T09:00:00+00:00")


def test_sync_refuses_a_bad_batch_whole(db):
    for body in ({"items": [{"title": "no id"}]}, {"listings": [{"item_id": ITEM}]}, {"items": "nope"}, []):
        with pytest.raises(BadRequest):
            core.sync(db, body)
    with pytest.raises(BadRequest):
        core.sync(db, {"items": [{"id": "i_ok"}, {"title": "no id"}]})
    assert db.query("SELECT * FROM items") == []                      # the good one rolled back with it


def test_the_heartbeat_answers_the_mode_and_go_live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "replay")
    out = core.heartbeat(db, "mac", {"queued": 3}, NOW)
    assert {k: out[k] for k in ("ok", "mode", "go_live_at", "source", "last_seen")} == {
        "ok": True, "mode": "replay", "go_live_at": None, "source": "mac", "last_seen": "2026-10-08T15:00:00+00:00"}
    monkeypatch.setenv("SALES_MODE", "live")                     # the first live call starts the live clock
    assert core.heartbeat(db, "mac", None, NOW)["go_live_at"] == "2026-10-08T15:00:00+00:00"
    assert core.heartbeat(db, "mac", None, utc(2026, 10, 9, 0, 0))["go_live_at"] == "2026-10-08T15:00:00+00:00"


def test_heartbeat_and_health(db, live):
    core.heartbeat(db, "gmail", {"sent": 1}, NOW)
    core.heartbeat(db, "gmail", {"sent": 2}, utc(2026, 10, 8, 15, 5))
    seed(db)
    core.process_email(db, posh_sale(), NOW)
    health = core.health(db, utc(2026, 10, 8, 15, 6))
    assert {k: health[k] for k in ("ok", "db", "mode", "heartbeats", "open_sales", "go_live_at")} == {
        "ok": True, "db": "ok", "mode": "live", "heartbeats": {"gmail": "2026-10-08T15:05:00+00:00"}, "open_sales": 1,
        "go_live_at": GO_LIVE}
    assert db.one("SELECT info FROM heartbeats")["info"] == '{"sent": 2}'
    with pytest.raises(BadRequest):
        core.heartbeat(db, " ", None, NOW)


def test_sales_lists(db, live):
    seed(db)
    matched = core.process_email(db, posh_sale(), NOW)
    lost = core.process_email(db, posh_sale("posh-x", title="Mystery Tee", url=None, order=None), NOW)
    core.mac_event(db, {"kind": "shipped", "words": "mystery"}, NOW)
    assert [s["id"] for s in core.sales_list(db, "open")] == [matched["sale_id"]]
    assert [s["id"] for s in core.sales_list(db, "unmatched")] == [lost["sale_id"]]
    everything = {s["id"]: s for s in core.sales_list(db, "all")}
    assert set(everything) == {matched["sale_id"], lost["sale_id"]}
    assert everything[matched["sale_id"]]["title"] == TITLE
    assert [t["marketplace"] for t in everything[matched["sale_id"]]["tasks"]] == ["depop", "vinted"]
    with pytest.raises(BadRequest):
        core.sales_list(db, "closed")


def test_the_dashboard_snapshot_renders(db, live):
    seed(db)
    seed(db, ZARA, ZARA_TITLE, sites=("vinted",), urls={}, ids={})
    first = core.process_email(db, posh_sale(), NOW)
    core.process_email(db, depop_sale(), NOW)                            # the double sale
    core.process_email(db, posh_sale("posh-x", title="Mystery Tee", url=None, order=None), NOW)
    data = core.dashboard_data(db, NOW)
    by_id = {item["id"]: item for item in data["items"]}
    assert data["unmatched"] == 1 and data["generated_at"] == "2026-10-08T15:00:00+00:00"
    assert by_id[ITEM]["listings"]["depop"] == {"status": "posted", "url": URLS["depop"]}
    assert by_id[ITEM]["sale"] == {"marketplace": "poshmark", "sold_at": "2026-10-08T14:05:00+00:00", "price": 35.0,
                                   "ship_by": "2026-10-15", "shipped_at": None, "status": "delisting"}
    assert by_id[ZARA]["sale"] is None
    assert sale(db, first["sale_id"])["status"] == "delisting"
    page = dashboard.render(data, now=NOW)
    assert f'data-id="{ITEM}"' in page and 'id="c-unmatched">1<' in page and 'id="c-toship">1<' in page


def test_the_test_message(monkeypatch, sent):
    monkeypatch.setenv("SALES_MODE", "off")
    assert core.test_message("ops", NOW) == {"sent": False, "chat": "ops", "mode": "off"} and sent == []
    monkeypatch.setenv("SALES_MODE", "replay")
    assert core.test_message("group", NOW) == {"sent": True, "chat": "ops", "mode": "replay"}
    monkeypatch.setenv("SALES_MODE", "live")
    assert core.test_message("group", NOW) == {"sent": True, "chat": "group", "mode": "live"}
    assert sent == [("ops", "[replay] ✓ thrift-api is up — Thu Oct 8, 11:00 AM EDT", False),
                    ("group", "✓ thrift-api is up — Thu Oct 8, 11:00 AM EDT", False)]
    with pytest.raises(BadRequest):
        core.test_message("everyone", NOW)


def test_an_email_gmail_wouldnt_give_in_full_is_told_to_the_owner_never_lost(db, live, sent):
    """WO33: the Gmail reader sends an email it can't read by its headers (text "", `unreadable`): one plain ops line
    when it might be a sale, nothing for plain noise."""
    sold = email("poshmark", "Congratulations! Your item has sold", "", "unread-1")
    core.process_email(db, {**sold, "unreadable": "Exception: Internal error"})
    lines = [t for chat, t, _ in sent if chat == "ops"]
    assert len(lines) == 1 and "arrived without its text" in lines[0] and "Your item has sold" in lines[0]
    promo = email("poshmark", "Your weekly closet tips", "", "unread-2")
    core.process_email(db, {**promo, "unreadable": "Exception: Internal error"})
    assert len([t for chat, t, _ in sent if chat == "ops"]) == 1             # noise: not a word


def test_a_sample_stored_without_its_text_gets_it_when_sent_again(db):
    """WO33, live: the first dumpSamples() sent 110 emails with empty text (a decoding bug in the reader); sent again,
    each gets its text — one already holding text is left alone."""
    first = email("vinted", "You sold an item on Vinted", "", "s-1")
    assert core.store_samples(db, [first])["stored"] == 1
    again = {**first, "text": "You sold J. Crew pants for $35.00"}
    assert core.store_samples(db, [again]) == {"stored": 0, "duplicates": 0, "skipped": 0, "filled": 1}
    assert db.one("SELECT text FROM samples WHERE message_id = 's-1'")["text"] == "You sold J. Crew pants for $35.00"
    assert core.store_samples(db, [{**first, "text": "something else"}])["duplicates"] == 1


def test_depop_already_sold_is_sold_twice_once_never_deleted(db, live, sent):
    """WO33, the owner's Depop rule: the Mac finds the Depop listing sold already (nothing deleted) → result "sold":
    the task is closed, the listing sold, ONE "Sold twice" line — and none again when Depop's own sale email follows."""
    seed(db)
    core.process_email(db, posh_sale(), NOW)
    sent.clear()
    depop, vinted = core.take_tasks(db, ["depop", "vinted"], NOW)
    out = core.task_result(db, depop["id"], "sold", evidence="shots/sold.png", now=NOW)
    assert out["status"] == "sold" and out["changed"] is True
    assert texts(sent, "group") == ["⚠️ Sold twice: Lacoste Tee White size M sold on Poshmark and Depop. "
                                    "Cancel the Depop order in its app."]
    assert listing_states(db)["depop"] == "sold"
    core.task_result(db, vinted["id"], "done", now=NOW)
    assert not [t for t in texts(sent, "group") if "taken down on" in t]       # not down everywhere: it sold
    core.process_email(db, depop_sale(message_id="depop-sale-2"), NOW)          # Depop's own sale email, later
    assert len([t for t in texts(sent, "group") if t.startswith("⚠️ Sold twice")]) == 1


def test_the_readers_third_try_is_always_told_even_for_plain_noise(db, live, sent):
    """WO33: poll() sends an email by its headers only after its 3rd try (`tries`): the ops chat hears it, sale or not."""
    promo = email("poshmark", "Your weekly closet tips", "", "unread-3")
    core.process_email(db, {**promo, "unreadable": "Exception: Internal error", "tries": 3})
    assert [t for chat, t, _ in sent if chat == "ops" and "arrived without its text" in t]
