"""WO49 on the API: the sale emails Poshmark's changed subject had hidden (no quotes since 2026-10-21 — the FARM Rio
dress sold there and stayed on Depop and Vinted for a day), Poshmark's ship reminder as a sale's second word, a
"may be a sale" line for anything else that says "sold", the ignored emails read again by the fixed rules (late: the
private chat, take-downs as usual), an upload the Mac hasn't fetched, and the cleanup in the 9 PM summary."""
from datetime import timedelta

import pytest
from apitools import NOW, TITLE, URLS, email, seed, texts

from thrift_api import core, emails, timers, watch

DRESS = "FARM Rio Zodiac Print Maxi Dress Black Red Balloon Sleeve size M"


@pytest.fixture
def live(db, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    core.put_setting(db, "go_live_at", "2026-10-01T00:00:00+00:00")


def test_poshmarks_subject_without_quotes_is_a_sale_with_its_title():
    subject = f"{DRESS} just sold to @cheetahmakita on Poshmark!"
    assert emails.classify("poshmark", subject, "") == "SALE"
    assert emails.title("poshmark", subject, "") == DRESS
    assert emails.classify("poshmark", f'"{DRESS}" just sold to @x on Poshmark!', "") == "SALE"     # the old form
    assert emails.title("poshmark", f'"{DRESS}" just sold to @x on Poshmark!', "") == DRESS
    for other in ("A listing that you liked just sold: UGG Classic Ultra Mini Toddler",
                  f"New buyer offer on {DRESS} from @cheetahmakita",
                  f"New buyer counteroffer on {DRESS} from @cheetahmakita", "Make MAJOR sales"):
        assert emails.classify("poshmark", other, "") == "OTHER", other


def test_a_poshmark_sale_without_quotes_takes_the_item_down_elsewhere(db, live, sent):
    seed(db, title=DRESS)
    out = core.process_email(db, email("poshmark", f"{DRESS} just sold to @cheetahmakita on Poshmark!",
                                       "Congrats! Your item sold.", "posh-noquotes-1"), NOW)
    assert out["status"] == "matched" and out["sale_status"] == "delisting"
    tasks = db.query("SELECT marketplace FROM delist_tasks ORDER BY marketplace")
    assert [t["marketplace"] for t in tasks] == ["depop", "vinted"]
    assert any(text.startswith(f"💰 Sold on Poshmark: {DRESS}") for text in texts(sent))


def test_a_ship_reminder_is_the_same_sale_or_a_late_one(db, live, sent):
    seed(db, title=DRESS)
    core.process_email(db, email("poshmark", f"{DRESS} just sold to @x on Poshmark!", "", "posh-sold-1"), NOW)
    before = len(sent)
    out = core.process_email(db, email("poshmark", f'Reminder to ship your Poshmark order: "{DRESS}"', "",
                                       "posh-reminder-1", date="2026-10-10T11:00:00.000Z"), NOW + timedelta(days=2))
    assert out.get("duplicate") is True and len(sent) == before                    # the sale it is about: nothing new
    assert db.one("SELECT COUNT(*) AS n FROM sales")["n"] == 1
    # a reminder whose sale email was missed: the sale now — ship by its own day, the private chat, take-downs
    seed(db, item_id="i_261001_bbbbbb", title="Southern Tide Haisley Stripe Crew Neck Sweater size M",
         urls={k: v.replace("Lacoste", "Southern") for k, v in URLS.items()},
         ids={"poshmark": "6700aa11bb22cc33dd44ee99", "depop": "shop-southern-tide", "vinted": "7012345699"})
    out = core.process_email(db, email("poshmark", 'Reminder to ship your Poshmark order: "Southern Tide Haisley '
                                       'Stripe Crew Neck Sweater size M"', "", "posh-reminder-2",
                                       date="2026-10-08T14:05:00.000Z"), NOW)
    assert out["status"] == "matched" and out["item_id"] == "i_261001_bbbbbb" and out["ship_by"] == "2026-10-08"
    said = texts(sent)[-1]
    assert said.startswith("💰 Found by Poshmark's reminder to ship it: Sold on Poshmark: Southern Tide")
    assert "Taking it down on Depop and Vinted." in said
    assert not any(c == "group" and "Southern Tide" in t for c, t, _ in sent)     # never a days-late group line


def test_an_email_that_still_says_sold_is_never_silent(db, live, sent):
    out = core.process_email(db, email("poshmark", "Your closet item sold — see details", "", "posh-odd-1"), NOW)
    assert out["kind"] == "OTHER" or out["kind"] == "SALE"
    core.process_email(db, email("poshmark", "Hooray: Zara Skirt has been sold", "", "posh-odd-2"), NOW)
    lines = [t for c, t, _ in sent if c == "ops" and t.startswith("❓")]
    assert any("Hooray: Zara Skirt has been sold" in t for t in lines)
    sent.clear()
    for quiet in ("A listing that you liked just sold: UGG", "Similar items sold fast!", "Make MAJOR sales"):
        core.process_email(db, email("poshmark", quiet, "", f"q-{len(quiet)}"), NOW)
    assert not [t for c, t, _ in sent if t.startswith("❓")]


def test_ignored_emails_are_read_again_by_the_fixed_rules_late(db, live, sent):
    """The emails the old rule filed as OTHER (no quotes): the 15-minute timer reads them again — take-downs as for any
    sale, the private chat told, never a days-late group line; once."""
    seed(db, title=DRESS)
    sold_at = NOW - timedelta(days=1)
    core._insert_event(db, {"message_id": "posh-old-1", "thread_id": None, "marketplace": "poshmark", "kind": "OTHER",
                            "subject": f"{DRESS} just sold to @cheetahmakita on Poshmark!",
                            "received_at": sold_at.isoformat(), "parsed_json": None, "raw_text": None}, "ignored", NOW)
    core._insert_event(db, {"message_id": "posh-old-2", "thread_id": None, "marketplace": "poshmark", "kind": "OTHER",
                            "subject": "Stock your closet for the holidays", "received_at": sold_at.isoformat(),
                            "parsed_json": None, "raw_text": None}, "ignored", NOW)
    out = timers.every_15_min(db, NOW)
    assert out["reclassified"] == 1
    sale = db.one("SELECT * FROM sales")
    assert sale["marketplace"] == "poshmark" and sale["sold_at"].startswith(sold_at.isoformat()[:16])
    assert [t["marketplace"] for t in db.query("SELECT marketplace FROM delist_tasks ORDER BY marketplace")] == \
        ["depop", "vinted"]
    late = [t for c, t, _ in sent if c == "ops" and t.startswith("💰 Found late")]
    assert len(late) == 1 and "Taking it down on Depop and Vinted." in late[0]
    assert not any(c == "group" and DRESS in t for c, t, _ in sent)
    assert timers.every_15_min(db, NOW + timedelta(minutes=15))["reclassified"] == 0
    row = db.one("SELECT kind, status FROM email_events WHERE message_id = 'posh-old-2'")
    assert row["kind"] == "OTHER" and row["status"] == "ignored"


def test_an_upload_the_mac_hasnt_fetched_is_one_private_line(db, live, sent):
    core.heartbeat(db, "mac", {}, NOW)
    db.execute("INSERT INTO shares_in (share_id, count, received, started_at, ended_at, last_received_at) "
               "VALUES (?, 20, 20, ?, ?, ?)", ("2026-10-24_093927_7777", (NOW - timedelta(minutes=12)).isoformat(),
                                               (NOW - timedelta(minutes=11)).isoformat(),
                                               (NOW - timedelta(minutes=11)).isoformat()))
    db.execute("INSERT INTO shares_in (share_id, count, received, started_at, ended_at, last_received_at, fetched_at) "
               "VALUES (?, 5, 5, ?, ?, ?, ?)", ("2026-10-24_090000_1111", NOW.isoformat(), NOW.isoformat(),
                                                NOW.isoformat(), NOW.isoformat()))
    out = timers.every_5_min(db, NOW)
    assert out["uploads"] == ["2026-10-24_093927_7777"]
    assert texts(sent) == ["⏳ Share 9:39 AM (20 photos) is in Azure but the Mac hasn't taken it (since 10:49 AM)"]
    assert watch.uploads_watch(db, NOW + timedelta(minutes=5), "live") == []        # once
    db.execute("INSERT INTO shares_in (share_id, count, received, started_at, ended_at, last_received_at) "
               "VALUES (?, 3, 3, ?, ?, ?)", ("2026-10-24_100000_2222", NOW.isoformat(), NOW.isoformat(), NOW.isoformat()))
    assert watch.uploads_watch(db, NOW + timedelta(minutes=60), "live") == []       # the Mac quiet: WO42's line instead


def test_the_cleanup_is_in_the_evening_summary(db, live, sent):
    evening = NOW.replace(hour=1) + timedelta(days=1)                                # 21:xx in New York (EDT)
    core.heartbeat(db, "mac", {"cleanup": {"at": (evening - timedelta(hours=8)).isoformat(), "dry": False,
                                           "freed": 283097215, "removed": 17}}, evening)
    line = watch.evening_line(db, evening, "live")
    assert line and line.endswith(" 🧹 Cleanup freed 270 MB."), line
    core.drop_setting(db, watch.EVENING_DONE)
    core.heartbeat(db, "mac", {"cleanup": {"at": (evening - timedelta(days=2)).isoformat(), "freed": 5}}, evening)
    assert "Cleanup" not in watch.evening_line(db, evening, "live")                 # not today's: not said


def test_seed_title_is_the_shared_one():
    assert TITLE                                                                     # apitools' sample stays in use
