"""thrift_api.timers (WO33): reminders from 09:00 local, never twice, none once shipped, overdue to the ops chat; the
Gmail reader's silence said once per episode and its return once; failed emails tried again (3 attempts in all); the
cleanup once a local day at 3 AM."""
from datetime import timedelta

import pytest
from apitools import NOW, depop_sale, email, posh_sale, sale, seed, texts, utc

from thrift_api import core, timers

STOPPED = "⚠️ Gmail reader stopped — nothing from it since Thu Oct 8, 11:00 AM EDT. Check its Executions in Apps Script."


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-10-01T00:00:00+00:00")


def a_sale(db, ship_by: str = "2026-10-09") -> str:
    """The Lacoste tee sold on Poshmark, due on `ship_by` (a Friday by default)."""
    seed(db)
    out = core.process_email(db, posh_sale(), NOW)
    db.execute("UPDATE sales SET ship_by = ? WHERE id = ?", (ship_by, out["sale_id"]))
    return out["sale_id"]


def test_reminders_from_nine_and_never_twice(db, live, sent):
    sale_id = a_sale(db)
    sent.clear()
    assert timers.every_15_min(db, utc(2026, 10, 8, 12, 45))["reminders"] == []          # 08:45 in New York
    assert timers.every_15_min(db, utc(2026, 10, 8, 13, 0))["reminders"] == [[sale_id, "day_before"]]
    assert sent == [("group", "📦 Ship tomorrow: Lacoste Tee White size M (Poshmark) — due Fri Oct 9.", False)]
    for when in (utc(2026, 10, 8, 13, 15), utc(2026, 10, 8, 22, 0), utc(2026, 10, 9, 12, 59)):
        assert timers.every_15_min(db, when)["reminders"] == []                           # never twice
    for when in (utc(2026, 10, 9, 13, 0), utc(2026, 10, 9, 18, 0), utc(2026, 10, 10, 13, 0), utc(2026, 10, 11, 13, 0)):
        timers.every_15_min(db, when)
    assert sent[1:] == [("group", "📦 Due today: Lacoste Tee White size M (Poshmark).", False),
                        ("ops", "⏰ Overdue: Lacoste Tee White size M (Poshmark) — ship-by was Fri Oct 9 and no shipment "
                                f"is recorded ({sale_id}).", False)]
    assert {(row["sale_id"], row["kind"]) for row in db.query("SELECT * FROM reminders")} == {
        (sale_id, kind) for kind in ("day_before", "due_today", "overdue")}


def test_no_reminder_once_shipped(db, live, sent):
    a_sale(db)
    core.mac_event(db, {"kind": "shipped", "words": "lacoste"}, utc(2026, 10, 8, 12, 0))
    sent.clear()
    for when in (utc(2026, 10, 8, 13, 0), utc(2026, 10, 9, 13, 0), utc(2026, 10, 10, 13, 0)):
        assert timers.every_15_min(db, when)["reminders"] == []
    assert sent == []


def test_no_reminder_for_a_double_sale_or_a_cancelled_one(db, live, sent):
    first = a_sale(db)
    second = core.process_email(db, depop_sale(), NOW)["sale_id"]
    db.execute("UPDATE sales SET ship_by = '2026-10-09' WHERE id = ?", (second,))
    sent.clear()
    assert timers.every_15_min(db, utc(2026, 10, 8, 13, 0))["reminders"] == [[first, "day_before"]]
    db.execute("UPDATE sales SET status = 'cancelled' WHERE id = ?", (first,))
    assert timers.every_15_min(db, utc(2026, 10, 9, 13, 0))["reminders"] == []


def test_replay_reminds_the_ops_chat_and_off_does_nothing(db, monkeypatch, sent):
    monkeypatch.setenv("SALES_MODE", "replay")
    a_sale(db)
    sent.clear()
    timers.every_15_min(db, utc(2026, 10, 8, 13, 0))
    assert sent == [("ops", "[replay] 📦 Ship tomorrow: Lacoste Tee White size M (Poshmark) — due Fri Oct 9.", False)]
    monkeypatch.setenv("SALES_MODE", "off")
    assert timers.every_15_min(db, utc(2026, 10, 9, 13, 0)) == {"mode": "off"}
    assert len(sent) == 1 and len(db.query("SELECT * FROM reminders")) == 1


def test_a_live_timer_starts_the_live_clock(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    timers.every_15_min(db, NOW)
    assert core.setting(db, "go_live_at") == "2026-10-08T15:00:00+00:00"


def test_the_gmail_reader_stopping_and_coming_back(db, live, sent):
    assert timers.every_15_min(db, NOW)["gmail"] == "never"                              # never beat: nothing missed
    core.heartbeat(db, "gmail", {"sent": 0}, NOW)
    assert timers.gmail_watch(db, NOW + timedelta(minutes=29), "live") == "ok"
    assert timers.every_15_min(db, NOW + timedelta(minutes=31))["gmail"] == "stopped"
    for minutes in (46, 61, 180):
        assert timers.gmail_watch(db, NOW + timedelta(minutes=minutes), "live") == "stopped"
    assert sent == [("ops", STOPPED, False)]                                             # once per episode
    core.heartbeat(db, "gmail", {"sent": 1}, NOW + timedelta(minutes=185))
    assert timers.every_15_min(db, NOW + timedelta(minutes=190))["gmail"] == "back"
    assert timers.gmail_watch(db, NOW + timedelta(minutes=205), "live") == "ok"
    assert texts(sent) == [STOPPED, "✓ Gmail reader back"]
    assert timers.gmail_watch(db, NOW + timedelta(minutes=230), "live") == "stopped"   # a new episode: said again
    assert len(sent) == 3 and all(chat == "ops" for chat, _, _ in sent)


def test_failed_emails_are_tried_three_times_in_all(db, live, sent):
    core.process_email(db, email("poshmark", "You made a sale!", "Congrats! Ship within 7 days.", "posh-bad"), NOW)
    for minutes in (15, 30, 45, 60):
        timers.every_15_min(db, NOW + timedelta(minutes=minutes))
    row = db.one("SELECT status, attempts FROM email_events")
    assert (row["status"], row["attempts"], sent) == ("failed", 3, [])


def test_a_failure_that_clears_is_acted_on_by_the_timer(db, live, sent, monkeypatch):
    seed(db)
    real = core.match.match_sale

    def broken(*args):
        raise RuntimeError("database hiccup")
    monkeypatch.setattr(core.match, "match_sale", broken)
    first = core.process_email(db, posh_sale(), NOW)
    assert first["status"] == "failed"
    monkeypatch.setattr(core.match, "match_sale", real)
    out = timers.every_15_min(db, NOW + timedelta(minutes=15))
    assert out["retried"] == [{"message_id": "posh-sale-1", "status": "matched"}]
    assert texts(sent, "group") == ["💰 Sold on Poshmark: Lacoste Tee White size M — $35. Ship by Thu Oct 15."]
    [row] = db.query("SELECT id FROM sales")
    assert sale(db, row["id"])["status"] == "delisting"


@pytest.mark.parametrize("three_am", [utc(2026, 10, 8, 7, 30), utc(2026, 12, 2, 8, 30)])     # EDT, EST
def test_the_daily_cleanup_once_a_local_day_at_three(db, three_am):
    def store(message_id, subject, text, days_ago):
        core.process_email(db, email("poshmark", subject, text, message_id), three_am - timedelta(days=days_ago),
                           mode="live")
    store("old-other", "Order update", "nothing to see", 91)
    store("new-other", "Order update", "nothing to see", 10)
    store("old-failed", "You made a sale!", "Congrats!", 31)
    store("new-failed", "You made a sale!", "Congrats again!", 2)
    assert timers.daily(db, three_am - timedelta(minutes=31))["ran"] is False             # 02:59
    assert timers.daily(db, three_am) == {"ran": True, "raw_text_cleared": 1, "other_deleted": 1}
    rows = {row["message_id"]: row for row in db.query("SELECT * FROM email_events")}
    assert set(rows) == {"new-other", "old-failed", "new-failed"}
    assert (rows["old-failed"]["raw_text"], rows["new-failed"]["raw_text"]) == (None, "Congrats again!")
    assert timers.daily(db, three_am + timedelta(minutes=20))["ran"] is False             # once a day
    assert timers.daily(db, three_am + timedelta(hours=1))["ran"] is False                # 4 AM
    assert timers.daily(db, three_am + timedelta(days=1))["ran"] is True
