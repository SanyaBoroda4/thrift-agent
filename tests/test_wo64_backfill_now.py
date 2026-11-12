"""WO64 — 50 older items to eBay tonight, each with its link (the owner, 2026-11-11 6:53 PM: "let's 50 listings now
please. I want it now with links posted in the groups with links"): `thrift ebay backfill --now 50` is one switch in
the plan's own record — today's limit and cap raised until midnight, tomorrow nothing before 2:00 PM and then the old
pace; three account-level refusals in a row stop it until the owner's go."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from test_ebay_flow import env, item  # noqa: F401 (env: fixture)
from test_wo61_backfill_links import READ, links, live_on_ebay
from thrift_agent.ebay import backfill

TONIGHT = datetime(2026, 11, 12, 0, 5, tzinfo=timezone.utc)       # Wed Nov 11, 7:05 PM New York
TOMORROW_9AM = datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc)  # Thu Nov 12, 9:00 AM
TOMORROW_2PM = datetime(2026, 11, 12, 19, 0, tzinfo=timezone.utc)  # Thu Nov 12, 2:00 PM


@pytest.fixture
def plan(env):  # noqa: F811 (the fixture)
    """Today's five live (seeded, as on the Mac), and 20 older items waiting with a start tomorrow at 2 PM."""
    s, db, said = env
    ids = [item(s, db, n, posted=f"2026-10-{12 + n % 18 + 1:02d}T12:00:00+00:00") for n in range(1, 26)]
    five = ids[:5]
    backfill.plan(s, db, only=five)
    for k in range(5):
        backfill.tick(s, db, datetime(2026, 11, 11, 19, 55, k, tzinfo=timezone.utc), READ)
    s.data["marketplaces"]["ebay"]["backfill"] = {"per_day": 4}            # small numbers: 4 a day (25 on the Mac)
    for n, iid in enumerate(five, start=1):
        live_on_ebay(db, iid, n)
    backfill.seed_links(db)
    rest = backfill.plan(s, db, not_before=TOMORROW_2PM)
    assert len(rest) == 20
    return s, db, said, five, rest


def let_in(s, db, start, k):
    return [q for q in (backfill.tick(s, db, start + timedelta(seconds=n), READ) for n in range(k)) if q]


def test_now_lets_n_more_in_tonight_the_oldest_first_and_raises_todays_cap(plan):
    s, db, said, five, rest = plan
    b = backfill.boost(s, db, 6, TONIGHT)
    assert b["limit"] == 11 and b["day"] == "2026-11-11"                    # today's five + 6
    assert b["then"] == "2026-11-12T19:00:00+00:00"                          # tomorrow 2:00 PM New York
    assert let_in(s, db, TONIGHT, 20) == rest[:6]                            # 6, the oldest first, then nothing
    assert backfill.cap_today(s, db, TONIGHT) == b["cap"] >= 5 + 6 + backfill.BOOST_ROOM - 5
    assert backfill.status(db)["waiting"] == 14


def test_the_cap_reads_the_boost_until_midnight_then_the_setting(plan, monkeypatch):
    s, db, said, five, rest = plan
    b = backfill.boost(s, db, 50, TONIGHT)
    assert b["cap"] == 5 + 50 + backfill.BOOST_ROOM == 60                     # the day's five + 50 + room: WO64's 60
    assert backfill.cap_today(s, db, TONIGHT + timedelta(hours=4)) == b["cap"]   # 11:05 PM: still today
    assert backfill.cap_today(s, db, TONIGHT + timedelta(hours=5)) is None        # midnight passed: the setting's 50


def test_tomorrow_nothing_before_2_pm_then_the_old_pace(plan):
    s, db, said, five, rest = plan
    backfill.boost(s, db, 6, TONIGHT)
    assert let_in(s, db, TONIGHT, 10) == rest[:6]
    assert let_in(s, db, TOMORROW_9AM, 10) == []                            # the quiet morning: nothing
    assert let_in(s, db, datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc), 10) == []   # 1 AM: nothing
    assert let_in(s, db, TOMORROW_2PM, 10) == rest[6:10]                     # from 2 PM: per_day (4 here, 25 on the Mac)
    assert backfill.cap_today(s, db, TOMORROW_2PM) is None                  # the cap back to the setting
    assert let_in(s, db, TOMORROW_2PM + timedelta(days=1), 10) == rest[10:14]


def test_three_account_level_refusals_in_a_row_stop_it_with_one_line_in_ebays_words(plan):
    s, db, said, five, rest = plan
    backfill.boost(s, db, 10, TONIGHT)
    got = let_in(s, db, TONIGHT, 3)
    words = "eBay's limit: createListing: You have exceeded your selling limit for this month (LSTNG_SELLING_LIMIT)"
    for iid in got:
        db.upsert_listing(iid, "ebay", status="queued", error=words)        # a site failure: back in line, its words kept
    assert backfill.tick(s, db, TONIGHT + timedelta(minutes=5), READ) is None
    st = backfill.status(db)
    assert st["stopped"] and "selling limit" in st["stopped"]["why"]
    lines = [m for m in said.ops if m.startswith("⛔ eBay backfill stopped")]
    assert len(lines) == 1 and "exceeded your selling limit" in lines[0] and "--resume" in lines[0]
    assert let_in(s, db, TONIGHT + timedelta(minutes=10), 5) == []          # nothing more until the owner's go
    assert len([m for m in said.ops if m.startswith("⛔")]) == 1             # said once
    assert backfill.resume(db) is True
    for iid in got:
        live_on_ebay(db, iid, 90)                                            # (the limit lifted; they went up)
    assert len(let_in(s, db, TONIGHT + timedelta(minutes=15), 20)) == 7      # the rest of tonight's 10


def test_item_refusals_or_different_reasons_never_stop_it(plan):
    s, db, said, five, rest = plan
    backfill.boost(s, db, 10, TONIGHT)
    a, b, c = let_in(s, db, TONIGHT, 3)
    db.upsert_listing(a, "ebay", status="failed", error="createListing: Item specific Size is missing")
    db.upsert_listing(b, "ebay", status="failed", error="createListing: Item specific Size is missing")
    db.upsert_listing(c, "ebay", status="failed", error="createListing: Item specific Size is missing")
    assert backfill.tick(s, db, TONIGHT + timedelta(minutes=5), READ) is not None     # an item's own refusal: goes on
    d, e = let_in(s, db, TONIGHT + timedelta(minutes=6), 2)
    db.upsert_listing(c, "ebay", status="skipped", error="refused by eBay for good (counterfeit policy)")
    db.upsert_listing(d, "ebay", status="queued", error="eBay's limit: You have exceeded your selling limit")
    db.upsert_listing(e, "ebay", status="failed", error="createListing: your account is on hold")
    assert backfill.status(db)["stopped"] is None
    assert backfill.tick(s, db, TONIGHT + timedelta(minutes=7), READ) is not None     # three reasons, not one


def test_tonights_links_and_the_days_line_count_only_the_items_that_went_up_tonight(plan):
    s, db, said, five, rest = plan
    backfill.boost(s, db, 6, TONIGHT)
    tonight = let_in(s, db, TONIGHT, 10)
    for n, iid in enumerate(tonight[:5], start=10):
        live_on_ebay(db, iid, n)
    db.upsert_listing(tonight[5], "ebay", status="failed", error="createListing: Item specific Size is missing")
    assert backfill.send_links(s, db) == tonight[:5]                        # nothing for the failed one, nor the five
    assert len(links(said)) == 5
    line = backfill.day_line(s, db, TONIGHT + timedelta(minutes=30))
    assert line == "🔵 eBay today: 5 more items up · 14 to go · about 4 more days."   # 14 at 4 a day; the five of
    assert backfill.day_line(s, db, TONIGHT + timedelta(hours=1)) is None              # this afternoon aren't counted


def test_the_cli_switch_and_its_refusals(plan, monkeypatch):
    s, db, said, five, rest = plan
    with pytest.raises(ValueError, match="1 or more"):
        backfill.boost(s, db, 0, TONIGHT)
    for iid in rest:
        backfill.tick                                                        # noqa: B018 — (all let in below)
    backfill.boost(s, db, 30, TONIGHT)
    let_in(s, db, TONIGHT, 40)
    with pytest.raises(ValueError, match="nothing planned is waiting"):
        backfill.boost(s, db, 5, TONIGHT)
