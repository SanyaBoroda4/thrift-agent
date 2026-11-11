"""WO61 — the eBay backfill sends a link for every item it puts on eBay (the owner, 2026-11-11: "whenever agent will
post previous listings i want it to send links for each one"): ONE silent group message per backfilled item once its
eBay listing is live, never twice; nothing for a failed one or a new share; the day's one line."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from test_ebay_flow import ebay, env, go, item  # noqa: F401 (env: fixture)
from thrift_agent import telegram
from thrift_agent.ebay import backfill, duties

TODAY = datetime(2026, 11, 12, 19, 30, tzinfo=timezone.utc)      # Thu Nov 12, 2:30 PM New York
READ = lambda url: (True, None)                                  # noqa: E731 — Poshmark's page, unread: the price kept


def live_on_ebay(db, iid, n, price=161, title=None):
    """The eBay worker's result: the row posted (read back) with its address and price."""
    fields = {"title": title or f"Zara TRF Leather Moto Jacket {n}"}
    db.upsert_listing(iid, "ebay", status="posted", url=f"https://www.ebay.com/itm/40735626{n:04d}", price=price,
                      posted_at=TODAY.isoformat(timespec="seconds"), fields_json=fields)


def planned(s, db, k, start=TODAY, seed=True):
    """`k` old items planned and let in today (queued for eBay); `seed`: the links' first turn has run (the deploy's
    poster recorded what was live then — nothing here yet)."""
    ids = [item(s, db, n, posted=f"2026-10-{13 + n:02d}T12:00:00+00:00") for n in range(1, k + 1)]
    backfill.plan(s, db)
    queued = [backfill.tick(s, db, start + timedelta(seconds=n), READ) for n in range(k)]
    assert queued == ids
    if seed:
        assert backfill.seed_links(db) == 0
    return ids


def links(said):
    return [m for m in said.group if m.startswith("🔵 Now on eBay:")]


def test_each_backfilled_item_gets_one_link_message_when_it_is_live(env):  # noqa: F811 (the fixture)
    s, db, said = env
    a, b, c = planned(s, db, 3)
    live_on_ebay(db, a, 1)
    live_on_ebay(db, b, 2, price=49, title="Birkenstock Rio Kids Sandals Silver & White, Toddler size 8")
    assert backfill.send_links(s, db) == [a, b]                   # c isn't live yet: nothing for it
    assert links(said) == ["🔵 Now on eBay: Zara TRF Leather Moto Jacket 1 — $161\nhttps://www.ebay.com/itm/407356260001",
                           "🔵 Now on eBay: Birkenstock Rio Kids Sandals Silver &amp; White, Toddler size 8 — $49\n"
                           "https://www.ebay.com/itm/407356260002"]
    live_on_ebay(db, c, 3)
    assert backfill.send_links(s, db) == [c] and len(links(said)) == 3


def test_never_twice_after_a_restart_or_a_second_turn(env):  # noqa: F811 (the fixture)
    s, db, said = env
    [a] = planned(s, db, 1)
    live_on_ebay(db, a, 1)
    assert backfill.send_links(s, db) == [a]
    assert backfill.send_links(s, db) == [] and backfill.send_links(s, db) == []   # the next turns, a restart
    assert len(links(said)) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE ref=? AND kind=?", (a, backfill.LINK_EVENT)).fetchone()[0] == 1


def test_a_send_that_never_left_goes_with_the_next_turn_and_one_that_may_have_arrived_is_never_resent(env, monkeypatch):  # noqa: F811 (the fixture)
    s, db, said = env
    a, b = planned(s, db, 2)
    live_on_ebay(db, a, 1)
    live_on_ebay(db, b, 2)
    real = backfill._to_group

    def down(s_, text, silent):
        raise RuntimeError("telegram sendMessage: ConnectError: [Errno 8] nodename nor servname provided")
    monkeypatch.setattr(backfill, "_to_group", down)
    assert backfill.send_links(s, db) == [] and links(said) == []          # never left: taken back, the rest wait
    monkeypatch.setattr(backfill, "_to_group", real)
    assert backfill.send_links(s, db) == [a, b] and len(links(said)) == 2  # the next turn


def test_a_failed_item_gets_no_message(env):  # noqa: F811 (the fixture)
    s, db, said = env
    a, b = planned(s, db, 2)
    live_on_ebay(db, a, 1)
    db.upsert_listing(b, "ebay", status="failed", error="Mismatch: set aside")
    assert backfill.send_links(s, db) == [a] and len(links(said)) == 1


def test_a_new_share_gets_no_extra_message(env):  # noqa: F811 (the fixture)
    s, db, said = env
    fresh = item(s, db, 7, posted=TODAY.isoformat(timespec="seconds"))     # never planned: its own card is fresh
    backfill.seed_links(db)
    live_on_ebay(db, fresh, 7)
    assert backfill.send_links(s, db) == [] and links(said) == []


def test_the_items_live_before_the_links_began_are_never_sent(env):  # noqa: F811 (the fixture)
    """The owner's first five (2026-11-11) went up before WO61: she has their links — none is sent again."""
    s, db, said = env
    five = planned(s, db, 5, start=datetime(2026, 11, 11, 19, 55, tzinfo=timezone.utc), seed=False)
    for n, iid in enumerate(five, start=1):
        live_on_ebay(db, iid, n)
    assert backfill.send_links(s, db) == [] and links(said) == []
    assert backfill.seed_links(db) == 0                                    # once


def test_a_title_with_markup_is_escaped(env):  # noqa: F811 (the fixture)
    s, db, said = env
    [a] = planned(s, db, 1)
    live_on_ebay(db, a, 1, title="H&M <Studio> Wool Coat")
    backfill.send_links(s, db)
    assert links(said)[0].startswith("🔵 Now on eBay: H&amp;M &lt;Studio&gt; Wool Coat — $161\n")


def test_the_link_goes_silently(monkeypatch):
    """The Bot's send with `silent`: disable_notification whatever the hour; without it the night hours decide."""
    seen = []
    bot = telegram.Bot("tok", "-100", set())
    monkeypatch.setattr(bot, "call", lambda method, **params: seen.append(params) or {"message_id": 7})
    bot.send_message("🔵 Now on eBay: x — $1\nhttps://www.ebay.com/itm/1", parse_mode="HTML", silent=True)
    bot.send_message("✅ card", parse_mode="HTML")
    assert seen[0]["disable_notification"] == "true" and seen[0]["parse_mode"] == "HTML"
    assert seen[1].get("disable_notification") is None


def test_the_housekeeping_turn_lets_the_next_in_and_sends_the_links(env):  # noqa: F811 (the fixture)
    """Through the real eBay worker: let in, posted, read back — the link goes on the next housekeeping turn."""
    s, db, said = env
    a, _ = planned(s, db, 2)
    poster, shop = ebay(s)
    go(s, db, {"ebay": poster}, a)
    row = db.listing(a, "ebay")
    assert row["status"] == "posted"
    done = duties.tick(s, db, {"ebay": poster}, now=TODAY + timedelta(minutes=5), read_poshmark=READ)
    assert done["links"] == [a]
    [msg] = links(said)
    assert msg.endswith(f"\n{row['url']}") and f"— ${row['price']:,.0f}" in msg
    assert duties.tick(s, db, {"ebay": poster}, dry=True, now=TODAY + timedelta(minutes=10)).get("links") is None


@pytest.fixture
def two_a_day(env):  # noqa: F811 (the fixture)
    s, db, said = env
    s.data["marketplaces"]["ebay"]["backfill"] = {"per_day": 2}
    return env


def test_the_days_line_once_when_the_days_batch_is_done(two_a_day):
    s, db, said = two_a_day
    ids = [item(s, db, n, posted=f"2026-10-{13 + n:02d}T12:00:00+00:00") for n in range(1, 8)]
    backfill.plan(s, db)
    a = backfill.tick(s, db, TODAY, READ)
    b = backfill.tick(s, db, TODAY + timedelta(seconds=1), READ)
    assert backfill.tick(s, db, TODAY + timedelta(seconds=2), READ) is None          # the day's two
    live_on_ebay(db, a, 1)
    assert backfill.day_line(s, db, TODAY + timedelta(minutes=5)) is None            # b not settled yet
    live_on_ebay(db, b, 2)
    line = backfill.day_line(s, db, TODAY + timedelta(minutes=10))
    assert line == "🔵 eBay today: 2 more items up · 5 to go · about 3 more days." and said.group[-1] == line
    assert backfill.day_line(s, db, TODAY + timedelta(minutes=15)) is None            # once a day
    assert backfill.day_line(s, db, TODAY + timedelta(hours=6)) is None
    assert len([m for m in said.group if m.startswith("🔵 eBay today")]) == 1
    assert ids


def test_the_days_line_at_9_pm_when_the_batch_isnt_done_and_never_on_a_day_nothing_went_up(two_a_day):
    s, db, said = two_a_day
    [item(s, db, n, posted=f"2026-10-{13 + n:02d}T12:00:00+00:00") for n in range(1, 4)]
    backfill.plan(s, db)
    a = backfill.tick(s, db, TODAY, READ)
    b = backfill.tick(s, db, TODAY + timedelta(seconds=1), READ)
    assert backfill.day_line(s, db, TODAY + timedelta(hours=6)) is None              # 8:30 PM, nothing up
    live_on_ebay(db, a, 1)                                                          # b still queued
    assert backfill.day_line(s, db, datetime(2026, 11, 13, 1, 59, tzinfo=timezone.utc)) is None   # 8:59 PM
    line = backfill.day_line(s, db, datetime(2026, 11, 13, 2, 0, tzinfo=timezone.utc))            # 9:00 PM
    assert line == "🔵 eBay today: 1 more item up · 1 to go · about 1 more day."
    assert b


def test_the_last_days_line_says_every_planned_item_is_up(two_a_day):
    s, db, said = two_a_day
    [item(s, db, n, posted=f"2026-10-{13 + n:02d}T12:00:00+00:00") for n in range(1, 3)]
    backfill.plan(s, db)
    for n in range(2):
        live_on_ebay(db, backfill.tick(s, db, TODAY + timedelta(seconds=n), READ), n + 1)
    assert backfill.day_line(s, db, TODAY + timedelta(minutes=5)) == \
        "🔵 eBay today: 2 more items up — every planned item is on eBay now ✓"
