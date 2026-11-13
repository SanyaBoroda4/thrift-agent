"""WO60 — the evening shipping list (the owner, 2026-11-11: "Every evening at 10pm agent will send a beautifully
formatted message (a list) of all items that needs to be sent (either are due or not by tomorrow). Each item in the
list must have a deadline shipping date."); WO62 — every item clickable, a clearer layout ("It should be with links to
each item so she can click. And I want even more clear and nicer formatting.")."""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from thrift_api import core, http, shiplist, telegram, timers

NINE_PM = datetime(2026, 11, 12, 2, 0, tzinfo=timezone.utc)         # Wed Nov 11, 9:00 PM New York (EST) — WO63
TEN_PM = NINE_PM + timedelta(hours=1)                                 # the night hours begin
CREATED = "2026-11-01T12:00:00+00:00"
POSH_ORDER = "6a3f00aa11bb22cc33dd44ee"


@pytest.fixture
def out(monkeypatch):
    """Every message as (chat, text, silent, html)."""
    log = []
    monkeypatch.setattr(telegram, "SENDER",
                        lambda chat, text, silent, html=False: log.append((chat, text, silent, html)) or True)
    return log


def item(db, iid, title, size=None, mp=None, url=None):
    db.execute("INSERT INTO items (id, title, size, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
               (iid, title, size, CREATED, CREATED))
    if mp:
        db.execute("INSERT INTO listings (item_id, marketplace, status, url, updated_at) VALUES (?, ?, ?, ?, ?)",
                   (iid, mp, "sold", url, CREATED))


def sale(db, sid, mp, *, item_id=None, title=None, price=50.0, ship_by=None, sold_at="2026-11-09T18:00:00+00:00",
         order_id=None, shipped_at=None, status="matched", created_at=CREATED, listing_id=None, message_id=None):
    db.execute("INSERT INTO sales (id, marketplace, item_id, listing_id, title_seen, price, order_id, sold_at, ship_by, "
               "ship_by_source, shipped_at, status, message_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (sid, mp, item_id, listing_id, title, price, order_id, sold_at, ship_by, "rule" if ship_by else None,
                shipped_at, status, message_id, created_at))


def order(db, mp, oid, sid, status):
    db.execute("INSERT INTO orders (marketplace, order_id, sale_id, status, last_seen, first_seen) VALUES (?, ?, ?, ?, ?, ?)",
               (mp, oid, sid, status, CREATED, CREATED))


def run(db, now=NINE_PM, mode="live"):
    return timers.night_list(db, now, mode)


def text_of(out):
    return "\n".join(t for _, t, _, _ in out)


def plain(text):
    """The message as Telegram shows it: tags gone, entities read."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def hrefs(text):
    return [html.unescape(u) for u in re.findall(r'<a href="([^"]*)">', text)]


def four(db):
    """The four sites, mixed days — WO62's own example."""
    item(db, "i1", "Tory Burch Croc Loafers size 8.5", None, "poshmark",
         "https://poshmark.com/listing/Tory-Burch-Croc-Loafers-6acd94d6cbbea59039cf0001")
    item(db, "i2", "Zara TRF Leather Moto Jacket Black size S", "S", "ebay", "https://www.ebay.com/itm/407356268788")
    item(db, "i3", "Birkenstock Rio Sandals", "38", "depop", "https://www.depop.com/products/shop-birkenstock-rio-1a2b/")
    item(db, "i4", "Moncler Down Snowsuit", "9-12M", "vinted", "https://www.vinted.com/items/10734492762")
    sale(db, "s4", "vinted", item_id="i4", price=76, ship_by="2026-11-17", sold_at="2026-11-09T18:00:00+00:00")
    sale(db, "s2", "ebay", item_id="i2", price=161, ship_by="2026-11-12", sold_at="2026-11-10T18:00:00+00:00",
         order_id="21-15038-17628")
    sale(db, "s1", "poshmark", item_id="i1", price=120, ship_by="2026-11-10", sold_at="2026-11-03T18:00:00+00:00",
         order_id=POSH_ORDER)
    sale(db, "s3", "depop", item_id="i3", price=49, ship_by="2026-11-12", sold_at="2026-11-07T18:00:00+00:00")
    order(db, "depop", "387265019", "s3", "to_ship")


# ---------------------------------------------------------------- WO62: the layout and the links

def swept_tonight(db):
    """WO65: each site's orders read this evening (8:31–8:55 PM)."""
    for site, minute in (("poshmark", 31), ("depop", 33), ("vinted", 34), ("ebay", 55)):
        core.heartbeat(db, f"orders:{site}", {}, datetime(2026, 11, 12, 1, minute, tzinfo=timezone.utc))


def test_the_shipping_list_as_the_owner_sees_it(db, out):
    four(db)
    swept_tonight(db)
    got = run(db)
    assert got["sent"] == 1 and got["packages"] == 4
    [(chat, text, silent, as_html)] = out
    assert chat == "group" and as_html and silent is False             # 9 PM: with sound
    assert plain(text) == """📦  SHIPPING LIST
Wednesday, November 11
4 packages  ·  🔴 1 late  ·  🟠 2 tomorrow  ·  🟢 1 later

🔴  LATE — SHIP FIRST
1. Tory Burch Croc Loafers · 8.5
Poshmark · $120 · sold Tue Nov 3
⏰ Was due Tue Nov 10 · 1 day late
🏷 Order & label

🟠  SHIP TOMORROW · Thu Nov 12
2. Birkenstock Rio Sandals · 38
Depop · $49 · sold Sat Nov 7
🏷 Order & label

3. Zara TRF Leather Moto Jacket Black · S
eBay · $161 · sold Tue Nov 10
🏷 Order & label

🟢  LATER
4. Moncler Down Snowsuit · 9-12M
Vinted · $76 · sold Mon Nov 9
⏰ Ship by Tue Nov 17 · in 6 days
🏷 Sold orders & labels

──────────────
✓ Shipped ones drop off by themselves once the site shows them on the way.
🔎 Checked on the sites: Poshmark 8:31 PM · Depop 8:33 PM · Vinted 8:34 PM · eBay 8:55 PM
Shipped some? Reply “shipped 1 3” or “shipped all”.
Shipped or cancelled already? Reply “shipped <words>” or “cancelled <words>”."""
    assert "📦  <b>SHIPPING LIST</b>" in text and "🔴  <b>LATE — SHIP FIRST</b>" in text
    assert text.count("<blockquote>") == 3 and text.count("</blockquote>") == 3   # one per group
    assert "<b><a href=\"https://poshmark.com/listing/Tory-Burch-Croc-Loafers-6acd94d6cbbea59039cf0001\">Tory Burch " \
           "Croc Loafers</a></b> · 8.5" in text


def test_every_item_links_its_listing_and_its_order_by_verified_addresses_only(db, out):
    four(db)
    run(db)
    assert hrefs(out[0][1]) == [
        "https://poshmark.com/listing/Tory-Burch-Croc-Loafers-6acd94d6cbbea59039cf0001",
        f"https://poshmark.com/order/sales/{POSH_ORDER}",              # the sale emails' own order address
        "https://www.depop.com/products/shop-birkenstock-rio-1a2b/",       # the same day: the older sale first
        "https://www.depop.com/sellinghub/sold-items/387265019/",          # the receipt's id = the sold list's link
        "https://www.ebay.com/itm/407356268788",
        "https://www.ebay.com/mesh/ord/details?orderid=21-15038-17628",   # Seller Hub's order page (verified live)
        "https://www.vinted.com/items/10734492762",
        "https://www.vinted.com/my_orders?order_type=sold"]                # no Vinted order page verified: the list


def test_no_address_held_means_no_link_never_a_made_up_one(db, out):
    sale(db, "s1", "vinted", title="Book about basketball", price=3, ship_by="2026-11-18")
    sale(db, "s2", "poshmark", title="Loft Utility Jacket", price=20, ship_by="2026-11-13", order_id="not-an-order-id")
    run(db)
    text = out[0][1]
    assert "<b>Book about basketball</b>" in text and "<b>Loft Utility Jacket</b>" in text   # no listing address
    assert hrefs(text) == ["https://poshmark.com/order/sales", "https://www.vinted.com/my_orders?order_type=sold"]
    assert "Sold orders &amp; labels" in text


def test_an_unmatched_sale_links_by_the_sale_emails_own_address_or_its_listing_id(db, out):
    db.execute("INSERT INTO email_events (message_id, marketplace, kind, status, parsed_json, created_at) VALUES "
               "(?, ?, ?, ?, ?, ?)", ("m1", "depop", "SALE", "unmatched",
                                      json.dumps({"listing_url": "https://www.depop.com/products/shop-jens-maxi-9f9f/"}),
                                      CREATED))
    sale(db, "s1", "depop", title="Jen's Pirate Booty Maxi", ship_by="2026-11-13", message_id="m1")
    sale(db, "s2", "ebay", title="Vintage Coach Legacy Bag", price=95, ship_by="2026-11-13", listing_id="406909579692",
         order_id="22-15011-34002")
    run(db)
    links = hrefs(out[0][1])
    assert "https://www.depop.com/products/shop-jens-maxi-9f9f/" in links
    assert "https://www.ebay.com/itm/406909579692" in links and \
        "https://www.ebay.com/mesh/ord/details?orderid=22-15011-34002" in links


def test_link_previews_are_off(monkeypatch):
    seen = {}

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def urlopen(request, timeout):
        seen.update(json.loads(request.data))
        return Answer()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_GROUP_CHAT_ID", "-1")
    monkeypatch.setattr(telegram.urlrequest, "urlopen", urlopen)
    assert telegram.http_send("group", '<a href="https://x">x</a>', False, html=True)
    assert seen["parse_mode"] == "HTML" and seen["link_preview_options"] == {"is_disabled": True}
    assert seen["disable_notification"] is False
    seen.clear()
    assert telegram.http_send("group", "plain", True)
    assert "parse_mode" not in seen


@pytest.mark.parametrize("ship_by,clock", [
    ("2026-11-10", "⏰ Was due Tue Nov 10 · 1 day late"),
    ("2026-11-11", None),                                                # SHIP TODAY says the day
    ("2026-11-12", None),                                                # SHIP TOMORROW · Thu Nov 12 says it
    ("2026-11-20", "⏰ Ship by Fri Nov 20 · in 9 days"),
    (None, "⏰ Ship by ≈ Fri Nov 13 · in 2 days"),                        # worked out here (Depop +5): always, with ≈
])
def test_the_clock_line_only_where_the_group_doesnt_say_the_day(db, out, ship_by, clock):
    sale(db, "s1", "depop", title="Something", ship_by=ship_by, sold_at="2026-11-08T18:00:00+00:00")
    run(db)
    lines = [ln for ln in plain(out[0][1]).splitlines() if ln.startswith("⏰")]
    assert lines == ([clock] if clock else [])


def test_due_today_is_its_own_group(db, out):
    sale(db, "s1", "depop", title="Frame Le High Straight Jeans", ship_by="2026-11-11")
    run(db)
    text = plain(out[0][1])
    assert "1 package  ·  🟠 1 today" in text and "🟠  SHIP TODAY\n1. Frame Le High Straight Jeans" in text


def test_a_bundle_is_one_block_one_site_line_one_label(db, out):
    item(db, "i1", "Zara Girls Textured Tie Top size 8", None, "ebay", "https://www.ebay.com/itm/407356260001")
    item(db, "i2", "Zara Girls Wide Leg Pants", "10", "ebay", "https://www.ebay.com/itm/407356260002")
    sale(db, "s1", "ebay", item_id="i1", price=20, ship_by="2026-11-13", order_id="12-11111-22222:L1")
    sale(db, "s2", "ebay", item_id="i2", price=25, ship_by="2026-11-14", order_id="12-11111-22222:L2")
    sale(db, "s3", "depop", title="Another Order", price=30, ship_by="2026-11-15")
    got = run(db)
    text = plain(out[0][1])
    assert got["packages"] == 2 and "2 packages" in text
    assert """1. 2 items · one order
• Zara Girls Textured Tie Top · 8
• Zara Girls Wide Leg Pants · 10
eBay · $45 · sold Mon Nov 9
⏰ Ship by Fri Nov 13 · in 2 days
🏷 Order & label

2. Another Order""" in text
    assert hrefs(out[0][1]).count("https://www.ebay.com/mesh/ord/details?orderid=12-11111-22222") == 1


def test_her_own_ebay_sale_says_your_listing(db, out):
    sale(db, "s1", "ebay", item_id=None, title="Vintage Coach Legacy Shoulder Bag", price=95, ship_by="2026-11-13",
         order_id="22-15011-34002", listing_id="406909579692")
    run(db)
    assert "1. Vintage Coach Legacy Shoulder Bag\neBay · your listing · $95 · sold Mon Nov 9" in plain(out[0][1])


def test_a_title_with_markup_and_quotes_is_escaped_and_the_address_only_in_href(db, out):
    item(db, "i1", 'H&M <Studio> "Wool" Coat', "M", "depop", 'https://www.depop.com/products/shop-h-m-"x"/')
    sale(db, "s1", "depop", item_id="i1", price=40, ship_by="2026-11-12")
    run(db)
    text = out[0][1]
    assert "H&amp;M &lt;Studio&gt; \"Wool\" Coat" in text and "<Studio>" not in text
    assert "1. H&M <Studio> \"Wool\" Coat · M" in plain(text)
    for url in hrefs(text):
        assert '"' not in url and "<" not in url


def test_the_title_loses_its_size_and_is_cut_at_a_word():
    assert shiplist.title_and_size("Zadig & Voltaire New 100% Genuine Leather Tyler Ankle Boots Black size 7.5",
                                   None) == ("Zadig & Voltaire New 100% Genuine…", "7.5")
    assert shiplist.title_and_size("Birkenstock Rio Kids Sandals Silver & White, Toddler size 8, New with Tags",
                                   None) == ("Birkenstock Rio Kids Sandals Silver…", "8")
    assert shiplist.title_and_size("Moncler Down Hooded Snowsuit size 12M", "12M") == \
        ("Moncler Down Hooded Snowsuit", "12M")
    assert shiplist.title_and_size("Unicorn head", None) == ("Unicorn head", None)


def test_thirty_packages_split_between_items_into_two_messages(db, out):
    for n in range(30):
        sale(db, f"s{n:02d}", ["poshmark", "depop", "vinted", "ebay"][n % 4],
             title=f"A Rather Long Item Title Number {n:02d}", ship_by=f"2026-11-{10 + n % 8:02d}")
    got = run(db)
    assert got["messages"] == 2 and len(out) == 2
    first, second = plain(out[0][1]), plain(out[1][1])
    assert len(re.findall(r"^\d+\. ", first, re.M)) == 25 and len(re.findall(r"^\d+\. ", second, re.M)) == 5
    assert second.startswith("📦  SHIPPING LIST (continued)") and "✓ Shipped ones drop off" in second
    assert "✓ Shipped ones" not in first
    for raw in (out[0][1], out[1][1]):
        assert raw.count("<blockquote>") == raw.count("</blockquote>")             # never a quote left open
    numbers = [int(n) for n in re.findall(r"^(\d+)\. ", first + "\n" + second, re.M)]
    assert numbers == list(range(1, 31))


def test_long_items_split_before_telegrams_limit_never_inside_an_item(db, out):
    for n in range(24):
        sale(db, f"s{n:02d}", "ebay", title=f"{'Wool ' * 8}{n}", price=1234.56, ship_by="2026-11-20",
             order_id=f"12-{n:05d}-22222", listing_id=f"4073562{n:05d}")
    for n in range(24):                                                  # a bundle of many lines in one order
        sale(db, f"b{n:02d}", "ebay", title=f"{'Silk ' * 7}{n}", price=99, ship_by="2026-11-21",
             order_id=f"13-99999-11111:L{n}")
    run(db)
    assert len(out) >= 2
    for _, raw, _, _ in out:
        assert shiplist.visible(raw) <= 4096 and raw.count("<blockquote>") == raw.count("</blockquote>")
        for blk in plain(raw).split("\n\n"):
            if re.match(r"^\d+\. ", blk):
                assert "🏷" in blk                                              # every item whole, its label with it


# ---------------------------------------------------------------- WO60: what's on it, when, once

def test_a_sale_with_no_ship_by_gets_the_sites_rule_marked_approx(db, out):
    sale(db, "s1", "vinted", title="Club Monaco Cashmere Crewneck", ship_by=None, sold_at="2026-11-09T18:00:00+00:00")
    run(db)
    # Vinted: 5 business days from Mon Nov 9, Veterans Day (Wed Nov 11) not one of them → Tue Nov 17
    assert "⏰ Ship by ≈ Tue Nov 17 · in 6 days" in plain(text_of(out))


def test_a_sale_with_no_sale_day_counts_from_its_record_and_is_said_privately_once(db, out):
    sale(db, "s1", "poshmark", title="Tory Burch Slides", ship_by=None, sold_at=None,
         created_at="2026-11-10T15:00:00+00:00")
    run(db)
    group = [t for c, t, _, _ in out if c == "group"]
    ops = [t for c, t, _, _ in out if c == "ops"]
    assert "⏰ Ship by ≈ Tue Nov 17" in plain(group[0])                  # Poshmark 7 days from Tue Nov 10
    assert len(ops) == 1 and "no sale day" in ops[0] and "s1" in ops[0]
    db.execute("DELETE FROM settings WHERE key = ?", (shiplist.SENT_ON,))
    run(db, NINE_PM + timedelta(days=1))
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
    text = plain(text_of(out))
    assert "Still to ship" in text and "Gone Already" not in text and "1 package" in text


def test_nothing_open_says_all_caught_up(db, out):
    sale(db, "s1", "poshmark", title="Shipped", ship_by="2026-11-12", shipped_at="2026-11-11T15:00:00+00:00")
    swept_tonight(db)
    got = run(db)
    assert out == [("group", "📦 <b>Nothing to ship tonight</b> — all caught up ✨", False, True)]
    assert got["packages"] == 0


def test_sent_once_however_often_the_timer_runs_in_the_hour(db, out):
    sale(db, "s1", "poshmark", title="Tory Burch Croc Loafers", ship_by="2026-11-12")
    assert run(db)["sent"] == 1
    assert run(db)["sent"] == 0                                          # a second run, the same hour
    assert run(db, NINE_PM + timedelta(minutes=40))["sent"] == 0          # a retry later in the hour
    assert run(db, TEN_PM)["sent"] == 0                                   # nothing at 10 any more
    assert len(out) == 1
    assert core.setting(db, shiplist.SENT_ON) == "2026-11-11"
    assert run(db, NINE_PM + timedelta(days=1))["sent"] == 1              # the next evening


@pytest.mark.parametrize("hour_utc,sent", [(1, 0), (2, 1), (3, 0)])
def test_nine_pm_local_on_the_day_the_clocks_go_back(db, out, hour_utc, sent):
    sale(db, "s1", "depop", title="Something", ship_by="2026-11-03")
    # Sun Nov 1 2026: EDT ends at 2 AM — 9 PM that evening is EST, 02:00 UTC on Nov 2
    assert run(db, datetime(2026, 11, 2, hour_utc, 5, tzinfo=timezone.utc))["sent"] == sent


@pytest.mark.parametrize("hour_utc,sent", [(0, 0), (1, 1), (2, 0)])
def test_nine_pm_local_on_the_day_the_clocks_go_forward(db, out, hour_utc, sent):
    sale(db, "s1", "depop", title="Something", ship_by="2027-03-16")
    # Sun Mar 14 2027: EDT from 2 AM — 9 PM that evening is EDT, 01:00 UTC on Mar 15
    assert run(db, datetime(2027, 3, 15, hour_utc, 5, tzinfo=timezone.utc))["sent"] == sent


def test_not_outside_the_nine_pm_hour(db, out):
    sale(db, "s1", "depop", title="Something", ship_by="2026-11-12")
    assert run(db, NINE_PM - timedelta(minutes=1))["sent"] == 0 and out == []     # 8:59 PM
    assert run(db, NINE_PM + timedelta(minutes=59))["sent"] == 1                   # 9:59 PM: still its hour


@pytest.mark.parametrize("value,hour_utc", [("22:00", 3), ("20:00", 1), ("21:00", 2), ("not a time", 2), ("25:00", 2)])
def test_the_setting_moves_the_hour(db, out, value, hour_utc):
    core.put_setting(db, shiplist.AT, value)
    sale(db, "s1", "depop", title="Something", ship_by="2026-11-13")
    hits = [h for h in (0, 1, 2, 3, 4) if run(db, datetime(2026, 11, 12, h, 0, tzinfo=timezone.utc))["sent"]]
    assert hits == [hour_utc]                                                      # once, in that hour only


def test_it_is_the_hourly_timers_last_job_below_the_9_pm_line():
    from thrift_agent.config import ROOT
    src = (ROOT / "api" / "function_app.py").read_text(encoding="utf-8")
    body = src[src.index("def hourly("):]
    body = body[:body.index("\n\n\n") if "\n\n\n" in body else len(body)]
    jobs = re.findall(r"timers\.(\w+)\(", body)
    assert jobs[-1] == "night_list" and jobs.index("evening") < jobs.index("night_list")


@pytest.mark.parametrize("value,silent", [(None, False), ("false", False), ("true", True)])
def test_sound_on_by_default_and_the_setting_makes_it_silent(db, out, value, silent):
    """9 PM is no night hour: sound like any message; `true` makes it silent; at 10 PM (the setting moved) still sound."""
    if value is not None:
        core.put_setting(db, shiplist.SILENT, value)
    sale(db, "s1", "depop", title="Something", ship_by="2026-11-12")
    run(db)
    assert out[0][2] is silent


def test_html_is_never_cut_raw_and_plain_text_still_is(db, out):
    long = "<blockquote>" + "x" * 5000 + "</blockquote>"
    telegram.send(long, "group", now=TEN_PM, mode="live", html=True)
    telegram.send("y" * 5000, "group", now=TEN_PM, mode="live")
    assert out[0][1] == long and len(out[1][1]) == telegram.LIMIT


def test_the_other_reminders_keep_their_night_rule(db, out):
    """WO60 changes only the list: any other message at 10 PM still goes silently."""
    telegram.send("📦 Ship tomorrow: x (Depop) — due Thu Nov 12.", "group", now=TEN_PM, mode="live")
    assert out == [("group", "📦 Ship tomorrow: x (Depop) — due Thu Nov 12.", True, False)]


def test_off_mode_sends_nothing_and_replay_goes_to_the_private_chat(db, out):
    sale(db, "s1", "depop", title="Something", ship_by="2026-11-12")
    assert run(db, mode="off")["sent"] == 0 and out == []
    run(db, mode="replay")
    assert out[0][0] == "ops" and out[0][1].startswith("[replay] 📦  <b>SHIPPING LIST")


def test_the_preview_goes_to_the_private_chat_marked_and_takes_no_day(db, out):
    sale(db, "s1", "depop", title="Something", ship_by="2026-11-12")
    got = shiplist.night_list(db, datetime(2026, 11, 11, 20, 30, tzinfo=timezone.utc), "live", preview=True)
    assert got["sent"] == 1 and out[0][0] == "ops" and out[0][1].startswith("🔎 <b>Preview</b>")
    assert out[0][1].endswith(got["pages"][0]) and not got["sample"]
    assert core.setting(db, shiplist.SENT_ON) is None                    # tonight's real one still goes
    assert run(db)["sent"] == 1 and out[-1][0] == "group"


def test_a_preview_with_nothing_open_shows_the_sample_marked(db, out):
    got = shiplist.night_list(db, datetime(2026, 11, 11, 23, 30, tzinfo=timezone.utc), "live", preview=True)
    assert got["sample"] and got["packages"] == 4 and out[0][1].startswith("🔎 <b>Preview (sample)</b>")
    assert "Tory Burch Croc Loafers" in out[0][1] and hrefs(out[0][1]) == []        # made up: no address


def test_the_preview_route_takes_only_a_preview(db, out, monkeypatch):
    monkeypatch.setenv("SALES_MODE", "live")
    sale(db, "s1", "depop", title="Something", ship_by="2026-11-12")
    code, _, _ = http.handle("POST", "/ship-list", body=b'{"preview": true}', now=NINE_PM - timedelta(hours=2), db=db)
    assert code == 200 and out[0][0] == "ops"
    code, _, _ = http.handle("POST", "/ship-list", body=b"{}", now=NINE_PM - timedelta(hours=2), db=db)
    assert code == 400 and len(out) == 1


def test_health_shows_the_last_list_and_the_packages_open_now(db, out):
    sale(db, "s1", "depop", title="Something", ship_by="2026-11-12")
    run(db)
    sale(db, "s2", "vinted", title="Sold after ten", ship_by="2026-11-18")
    code, _, body = http.handle("GET", "/health", db=db, now=NINE_PM + timedelta(minutes=5))
    got = json.loads(body)["ship_list"]
    assert code == 200 and got["sent_on"] == "2026-11-11" and got["open_packages"] == 2
    assert got["last"]["packages"] == 1 and got["last"]["sent"] == 1 and got["last"]["groups"]["tomorrow"] == 1


def test_moved_to_ten_it_still_has_sound_the_night_hours_exception(db, out):
    core.put_setting(db, shiplist.AT, "22:00")
    sale(db, "s1", "depop", title="Something", ship_by="2026-11-13")
    assert run(db, TEN_PM)["sent"] == 1 and out[0][2] is False
