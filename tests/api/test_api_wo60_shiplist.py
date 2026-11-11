"""WO60 — the evening shipping list (the owner, 2026-11-11: "Every evening at 10pm agent will send a beautifully
formatted message (a list) of all items that needs to be sent (either are due or not by tomorrow). Each item in the
list must have a deadline shipping date.")."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from thrift_api import core, http, shiplist, telegram, timers

TEN_PM = datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)          # Wed Nov 11, 10:00 PM New York (EST)
CREATED = "2026-11-01T12:00:00+00:00"


@pytest.fixture
def out(monkeypatch):
    """Every message as (chat, text, silent, html)."""
    log = []
    monkeypatch.setattr(telegram, "SENDER",
                        lambda chat, text, silent, html=False: log.append((chat, text, silent, html)) or True)
    return log


def item(db, iid, title):
    db.execute("INSERT INTO items (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)", (iid, title, CREATED, CREATED))


def sale(db, sid, mp, *, item_id=None, title=None, price=50.0, ship_by=None, sold_at="2026-11-09T18:00:00+00:00",
         order_id=None, shipped_at=None, status="matched", created_at=CREATED):
    db.execute("INSERT INTO sales (id, marketplace, item_id, title_seen, price, order_id, sold_at, ship_by, "
               "ship_by_source, shipped_at, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (sid, mp, item_id, title, price, order_id, sold_at, ship_by, "rule" if ship_by else None, shipped_at,
                status, created_at))


def order(db, mp, oid, sid, status):
    db.execute("INSERT INTO orders (marketplace, order_id, sale_id, status, last_seen, first_seen) VALUES (?, ?, ?, ?, ?, ?)",
               (mp, oid, sid, status, CREATED, CREATED))


def run(db, now=TEN_PM, mode="live"):
    return timers.night_list(db, now, mode)


def text_of(out):
    return "\n".join(t for _, t, _, _ in out)


def test_every_open_sale_on_the_four_sites_in_ship_by_order_each_with_its_day(db, out):
    item(db, "i1", "Tory Burch Croc Loafers 8.5")
    item(db, "i2", "Zara TRF Faux Leather Jacket M")
    item(db, "i3", "Birkenstock Rio Sandals 38")
    item(db, "i4", "Moncler Down Snowsuit 9-12M")
    sale(db, "s4", "vinted", item_id="i4", price=76, ship_by="2026-11-16")
    sale(db, "s2", "ebay", item_id="i2", price=161, ship_by="2026-11-12")
    sale(db, "s1", "poshmark", item_id="i1", price=120, ship_by="2026-11-10")
    sale(db, "s3", "depop", item_id="i3", price=49, ship_by="2026-11-12")
    got = run(db)
    assert got["sent"] == 1 and got["packages"] == 4
    [(chat, text, silent, as_html)] = out
    assert chat == "group" and as_html and silent is False             # 10 PM is night, but this one has sound
    assert text.splitlines()[:2] == ["📦 <b>To ship — Wed Nov 11</b>", "4 packages · 1 overdue · 2 due by tomorrow"]
    assert "🔴 <b>Overdue</b>\n1. Tory Burch Croc Loafers 8.5 · Poshmark · $120\n   ⏰ ship-by <b>Tue Nov 10</b> — 1 day " \
           "late" in text
    assert "🟠 <b>Due by tomorrow</b>\n2. Zara TRF Faux Leather Jacket M · eBay · $161\n   ⏰ ship-by <b>Thu Nov 12</b> — " \
           "tomorrow\n3. Birkenstock Rio Sandals 38 · Depop · $49\n   ⏰ ship-by <b>Thu Nov 12</b> — tomorrow" in text
    assert "🟢 <b>Later</b>\n4. Moncler Down Snowsuit 9-12M · Vinted · $76\n   ⏰ ship-by <b>Mon Nov 16</b> — in 5 days" \
           in text
    assert text.rstrip().endswith("🏷 Labels: Poshmark — the sale email · Depop / Vinted — the app · eBay — eBay app → "
                                  "Ship / Print label")
    for n in range(1, 5):                                               # every line has its day
        assert re.search(rf"^{n}\. .*\n   ⏰ ship-by (≈ )?<b>\w{{3}} \w{{3}} \d+</b> — ", text, re.M)


def test_due_today_is_its_own_group_and_the_labels_name_only_tonights_sites(db, out):
    sale(db, "s1", "depop", title="Frame Le High Straight Jeans", ship_by="2026-11-11")
    run(db)
    text = text_of(out)
    assert "1 package · 1 due today" in text and "🟠 <b>Due today</b>" in text and "— today" in text
    assert "🏷 Labels: Depop — the app" in text and "Poshmark" not in text and "eBay" not in text


def test_a_sale_with_no_ship_by_gets_the_sites_rule_marked_approx(db, out):
    sale(db, "s1", "vinted", title="Club Monaco Cashmere Crewneck", ship_by=None, sold_at="2026-11-09T18:00:00+00:00")
    run(db)
    # Vinted: 5 business days from Mon Nov 9, Veterans Day (Wed Nov 11) not one of them → Tue Nov 17
    assert "⏰ ship-by ≈ <b>Tue Nov 17</b> — in 6 days" in text_of(out)


def test_a_sale_with_no_sale_day_counts_from_its_record_and_is_said_privately_once(db, out):
    sale(db, "s1", "poshmark", title="Tory Burch Slides", ship_by=None, sold_at=None, created_at="2026-11-10T15:00:00+00:00")
    run(db)
    group = [t for c, t, _, _ in out if c == "group"]
    ops = [t for c, t, _, _ in out if c == "ops"]
    assert "⏰ ship-by ≈ <b>Tue Nov 17</b>" in group[0]                  # Poshmark 7 days from Tue Nov 10
    assert len(ops) == 1 and "no sale day" in ops[0] and "s1" in ops[0]
    db.execute("DELETE FROM settings WHERE key = ?", (shiplist.SENT_ON,))
    run(db, TEN_PM + timedelta(days=1))
    assert len([t for c, t, _, _ in out if c == "ops"]) == 1            # the private line once


@pytest.mark.parametrize("how", ["shipped_at", "swept_shipped", "swept_delivered", "cancelled", "done", "double_sale",
                                 "merged"])
def test_a_shipped_or_closed_sale_is_not_on_it(db, out, how):
    sale(db, "s0", "depop", title="Still to ship", ship_by="2026-11-13")
    kw = {"shipped_at": "2026-11-10T20:00:00+00:00"} if how == "shipped_at" else \
        {"status": how} if how in ("cancelled", "done", "double_sale", "merged") else {}
    sale(db, "s1", "poshmark", title="Gone Already", ship_by="2026-11-12", order_id="P1", **kw)
    if how.startswith("swept_"):
        order(db, "poshmark", "P1", "s1", how.split("_", 1)[1])
    run(db)
    text = text_of(out)
    assert "Still to ship" in text and "Gone Already" not in text and "1 package" in text


def test_her_own_ebay_sale_is_on_it_marked_with_title_price_and_day_only(db, out):
    sale(db, "s1", "ebay", item_id=None, title="Vintage Coach Legacy Shoulder Bag", price=95, ship_by="2026-11-13",
         order_id="12-34567-89012")
    run(db)
    text = text_of(out)
    assert "1. Vintage Coach Legacy Shoulder Bag (your listing) · eBay · $95\n   ⏰ ship-by <b>Fri Nov 13</b> — in 2 days" \
           in text


@pytest.mark.parametrize("mp,ids", [("ebay", ("12-111:L1", "12-111:L2")), ("poshmark", ("P9", "P9"))])
def test_one_order_of_two_items_is_one_block_with_one_day(db, out, mp, ids):
    item(db, "i1", "Zara Girls Textured Tie Top")
    item(db, "i2", "Zara Girls Wide Leg Pants")
    sale(db, "s1", mp, item_id="i1", price=20, ship_by="2026-11-13", order_id=ids[0])
    sale(db, "s2", mp, item_id="i2", price=25, ship_by="2026-11-14", order_id=ids[1])
    sale(db, "s3", "depop", title="Another Order", price=30, ship_by="2026-11-15")
    got = run(db)
    text = text_of(out)
    assert got["packages"] == 2 and "2 packages" in text
    name = "eBay" if mp == "ebay" else "Poshmark"
    assert f"1. {name} order · 2 items · $45\n   ▫️ Zara Girls Textured Tie Top · $20\n   ▫️ Zara Girls Wide Leg Pants · " \
           f"$25\n   ⏰ ship-by <b>Fri Nov 13</b> — in 2 days" in text                # the order's (earliest) day
    assert "2. Another Order · Depop · $30" in text


def test_nothing_open_says_all_caught_up(db, out):
    sale(db, "s1", "poshmark", title="Shipped", ship_by="2026-11-12", shipped_at="2026-11-11T15:00:00+00:00")
    got = run(db)
    assert out == [("group", "📦 Nothing to ship tonight — all caught up ✨", False, True)] and got["packages"] == 0


def test_thirty_packages_go_in_two_messages_and_no_block_is_cut(db, out):
    for n in range(30):
        sale(db, f"s{n:02d}", ["poshmark", "depop", "vinted", "ebay"][n % 4],
             title=f"A Rather Long Item Title Number {n:02d} With Words", ship_by=f"2026-11-{10 + n % 8:02d}")
    got = run(db)
    assert got["messages"] == 2 and len(out) == 2
    first, second = out[0][1], out[1][1]
    assert len(re.findall(r"^\d+\. ", first, re.M)) == 25 and len(re.findall(r"^\d+\. ", second, re.M)) == 5
    assert second.startswith("📦 <b>To ship — Wed Nov 11</b> (continued)") and "🏷 Labels:" in second
    for page in (first, second):
        assert len(page) <= 4096
        blocks = re.findall(r"^(\d+)\. .*\n   ⏰ ship-by", page, re.M)
        assert len(blocks) == len(re.findall(r"^\d+\. ", page, re.M))    # every block with its own day line
    numbers = [int(n) for n in re.findall(r"^(\d+)\. ", first + "\n" + second, re.M)]
    assert numbers == list(range(1, 31))


def test_sent_once_however_often_the_timer_runs_in_the_hour(db, out):
    sale(db, "s1", "poshmark", title="Tory Burch Croc Loafers", ship_by="2026-11-12")
    assert run(db)["sent"] == 1
    assert run(db)["sent"] == 0                                          # a second run, the same hour
    assert run(db, TEN_PM + timedelta(minutes=40))["sent"] == 0           # a retry later in the hour
    assert len(out) == 1
    assert core.setting(db, shiplist.SENT_ON) == "2026-11-11"
    assert run(db, TEN_PM + timedelta(days=1))["sent"] == 1               # the next evening


@pytest.mark.parametrize("hour_utc,sent", [(2, 0), (3, 1), (4, 0)])
def test_ten_pm_local_on_the_day_the_clocks_go_back(db, out, hour_utc, sent):
    sale(db, "s1", "depop", title="Something", ship_by="2026-11-03")
    # Sun Nov 1 2026: EDT ends at 2 AM — 10 PM that evening is EST, 03:00 UTC on Nov 2
    assert run(db, datetime(2026, 11, 2, hour_utc, 5, tzinfo=timezone.utc))["sent"] == sent


@pytest.mark.parametrize("hour_utc,sent", [(1, 0), (2, 1), (3, 0)])
def test_ten_pm_local_on_the_day_the_clocks_go_forward(db, out, hour_utc, sent):
    sale(db, "s1", "depop", title="Something", ship_by="2027-03-16")
    # Sun Mar 14 2027: EDT from 2 AM — 10 PM that evening is EDT, 02:00 UTC on Mar 15
    assert run(db, datetime(2027, 3, 15, hour_utc, 5, tzinfo=timezone.utc))["sent"] == sent


def test_not_outside_the_ten_pm_hour(db, out):
    sale(db, "s1", "depop", title="Something", ship_by="2026-11-12")
    assert run(db, TEN_PM - timedelta(minutes=1))["sent"] == 0 and out == []      # 9:59 PM


def test_a_title_with_markup_characters_is_escaped(db, out):
    sale(db, "s1", "depop", title="H&M <Studio> Wool Coat", price=40, ship_by="2026-11-12")
    run(db)
    assert "1. H&amp;M &lt;Studio&gt; Wool Coat · Depop · $40" in text_of(out)


def test_a_long_title_is_cut_at_a_word(db, out):
    sale(db, "s1", "depop", title="Zadig & Voltaire New 100% Genuine Leather Tyler Ankle Boots Black size 7.5",
         ship_by="2026-11-12")
    run(db)
    assert "1. Zadig &amp; Voltaire New 100% Genuine Leather…" in text_of(out)
    assert len(shiplist.short("Zadig & Voltaire New 100% Genuine Leather Tyler Ankle Boots Black size 7.5")) <= 45


@pytest.mark.parametrize("value,silent", [(None, False), ("false", False), ("true", True)])
def test_sound_on_by_default_and_the_setting_makes_it_silent(db, out, value, silent):
    if value is not None:
        core.put_setting(db, shiplist.SILENT, value)
    sale(db, "s1", "depop", title="Something", ship_by="2026-11-12")
    run(db)
    assert out[0][2] is silent


def test_the_other_reminders_keep_their_night_rule(db, out):
    """WO60 changes only the list: any other message at 10 PM still goes silently."""
    telegram.send("📦 Ship tomorrow: x (Depop) — due Thu Nov 12.", "group", now=TEN_PM, mode="live")
    assert out == [("group", "📦 Ship tomorrow: x (Depop) — due Thu Nov 12.", True, False)]


def test_off_mode_sends_nothing_and_replay_goes_to_the_private_chat(db, out):
    sale(db, "s1", "depop", title="Something", ship_by="2026-11-12")
    assert run(db, mode="off")["sent"] == 0 and out == []
    run(db, mode="replay")
    assert out[0][0] == "ops" and out[0][1].startswith("[replay] 📦 <b>To ship")


def test_the_preview_goes_to_the_private_chat_marked_and_takes_no_day(db, out):
    sale(db, "s1", "depop", title="Something", ship_by="2026-11-12")
    got = shiplist.night_list(db, datetime(2026, 11, 11, 20, 30, tzinfo=timezone.utc), "live", preview=True)
    assert got["sent"] == 1 and out[0][0] == "ops" and out[0][1].startswith("🔎 <b>Preview</b>")
    assert out[0][1].endswith(got["pages"][0]) and "pages" not in run(db, mode="off")
    assert core.setting(db, shiplist.SENT_ON) is None                    # tonight's real one still goes
    assert run(db)["sent"] == 1 and out[-1][0] == "group"


def test_the_preview_route_takes_only_a_preview(db, out, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    sale(db, "s1", "depop", title="Something", ship_by="2026-11-12")
    code, _, _ = http.handle("POST", "/ship-list", body=b'{"preview": true}', now=TEN_PM - timedelta(hours=2), db=db)
    assert code == 200 and out[0][0] == "ops"
    code, _, _ = http.handle("POST", "/ship-list", body=b"{}", now=TEN_PM - timedelta(hours=2), db=db)
    assert code == 400 and len(out) == 1


def test_telegram_sends_html_with_parse_mode(monkeypatch):
    seen = {}

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def urlopen(request, timeout):
        import json
        seen.update(json.loads(request.data))
        return Answer()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_GROUP_CHAT_ID", "-1")
    monkeypatch.setattr(telegram.urlrequest, "urlopen", urlopen)
    assert telegram.http_send("group", "<b>x</b>", False, html=True)
    assert seen["parse_mode"] == "HTML" and seen["disable_notification"] is False
    seen.clear()
    assert telegram.http_send("group", "plain", True)
    assert "parse_mode" not in seen
