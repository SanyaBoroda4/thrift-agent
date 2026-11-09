"""WO54 §1.5 (live 2026-11-09): the ✅ card goes out the first time an item is live ANYWHERE. That morning Poshmark was
paused by one item's size ("12M" read back as other than "12 Months") and crosslist.announce() sent the first card only
once every site was "settled" — a paused Poshmark never settles — so 19 items live on Depop and Vinted got no card and
no link, and the owner saw "nothing posted". Now the first live site sends the card at once, with a plain "⏳" line for
each site still to come (paused and trying again by itself, the daily cap, a size …); each later landing edits the same
message; never twice — the poster's sweep and `thrift cards --backfill` included."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from cards import ALL_DONE
from test_crosslist_flow import NAME, TITLE, CardBot, _item, _settings, fields_for   # helpers only
from thrift_agent import approve, cli, crosslist, health, notify
from thrift_agent.db import DB

POSH = "https://poshmark.com/listing/Moncler-Down-Hooded-Snowsuit-Camel-12-Months-6af1c3d63dfc0025edecc4aa"
DEPOP = "https://www.depop.com/products/tetianasorokinl-moncler-down-hooded-snowsuit-camel-6199/"
VINTED = "https://www.vinted.com/items/1073449276"
SIZE_ERROR = ("PosterError: after picking '12 Months' the size menu neither showed Done nor closed with that size on the "
              "form (the size field shows '12M')")


@pytest.fixture
def said(monkeypatch):
    out = SimpleNamespace(group=[], ops=[])
    monkeypatch.setattr(notify, "group", out.group.append)
    monkeypatch.setattr(notify, "group_photo", lambda path, caption: out.group.append(caption))
    monkeypatch.setattr(notify, "say", out.ops.append)
    monkeypatch.setattr(notify, "photo", lambda path, caption: out.ops.append(caption))
    monkeypatch.setattr(notify, "ops_photo", lambda path, caption: out.ops.append(caption))
    return out


@pytest.fixture
def bot(monkeypatch):
    b = CardBot()
    monkeypatch.setattr(approve, "bot_for", lambda _s: b)
    return b


def _sends(bot):
    return [c for c in bot.calls if c[0] == "send"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _live_on_depop_and_vinted(db, n=1, at="2026-11-09T13:00:46+00:00"):
    """An item as the morning left it: live on Depop and Vinted; Poshmark's queue (it keeps no row: none was made)."""
    iid = _item(db, n)
    db.upsert_listing(iid, "depop", status="posted", url=DEPOP.replace("6199", f"{6199 + n}"), posted_at=at)
    db.upsert_listing(iid, "vinted", status="posted", url=f"{VINTED}{n}", posted_at=at)
    return iid


def _pause_poshmark(db, wait=4 * 3600):
    health.pause(db, "poshmark", f"site: {SIZE_ERROR}", health.SITE, error=SIZE_ERROR, wait=wait)


def test_live_on_depop_and_vinted_while_poshmark_is_paused_the_card_goes_at_once_and_poshmark_is_edited_in(
        tmp_path, said, bot):
    """§5: "An item live on Depop and Vinted while Poshmark is paused → ✅ card at once with both links, edited when
    Poshmark goes live"."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    _pause_poshmark(db)
    iid = _live_on_depop_and_vinted(db)
    text = crosslist.announce(s, db, iid)
    assert [c[2] for c in _sends(bot)] == [text]
    lines = text.split("\n\n")
    assert lines[0] == f"✅ {NAME} · $85"
    assert lines[1].startswith("⏳ Poshmark — next try ") and lines[1].endswith(", paused after a problem")
    assert lines[2].startswith('🔴 <a href="https://www.depop.com/') and lines[3].startswith(
        '🟢 <a href="https://www.vinted.com/items/')
    assert not any(w in text.lower() for w in ("error", "12m", "i_26", "reply"))     # nothing technical, no question
    assert crosslist.waiting_lines(db, s) == []                     # out, and nothing changed since
    # Poshmark back (its test passed): the sweep sees the card's line change, and edits it
    health.resume(db, "poshmark", "test passed")
    assert crosslist.waiting_lines(db, s) == [iid]
    edited = crosslist.announce(s, db, iid)
    assert edited.split("\n\n")[1] == "⏳ Poshmark — later" and bot.calls[-1][:3] == ("edit", 41, edited)
    # Poshmark lands: the same message, its link first; the card complete, the 💤 line on it (the newest card)
    db.upsert_listing(iid, "poshmark", status="posted", url=POSH, posted_at=_now())
    db.set_item(iid, status="posted")
    edited = crosslist.announce(s, db, iid)
    assert edited.split("\n\n")[1] == f'🟣 <a href="{POSH}">Poshmark</a>' and "⏳" not in edited
    assert edited.endswith(ALL_DONE)
    assert len(_sends(bot)) == 1 and bot.texts()[41] == edited     # ONE message, edited in place
    assert crosslist.waiting_lines(db, s) == [] and crosslist.announce(s, db, iid) is None
    assert crosslist.announced(db, iid) == {"poshmark", "depop", "vinted"}


def test_a_site_at_its_daily_cap_says_so_plainly(tmp_path, said, bot):
    """WO54 §3: the daily cap (25 a site) stays, and the card says when it stops a site."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    for n in range(25):                                             # Vinted's 25 today
        other = _item(db, 100 + n)
        db.upsert_listing(other, "vinted", status="posted", url=f"https://www.vinted.com/items/9{n}", posted_at=_now())
    iid = _item(db, 1)
    db.upsert_listing(iid, "poshmark", status="posted", url=POSH, posted_at=_now())
    db.upsert_listing(iid, "depop", status="posted", url=DEPOP, posted_at=_now())
    db.upsert_listing(iid, "vinted", status="queued")
    db.set_item(iid, status="posted")
    text = crosslist.announce(s, db, iid)
    assert text.split("\n\n")[-1] == "⏳ Vinted — today's 25 are up; it goes up after midnight"


def test_the_waiting_words_for_a_size_and_a_site_still_to_come(tmp_path, said, bot):
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _item(db, 1)
    db.upsert_listing(iid, "depop", status="posted", url=DEPOP, posted_at=_now())
    db.upsert_listing(iid, "vinted", status="skipped", error=f"{crosslist.SKIPPED}{crosslist.NO_SIZE}")
    text = crosslist.card_text(s, db, iid)
    assert text.split("\n\n")[1:] == ["⏳ Poshmark — later", f'🔴 <a href="{DEPOP}">Depop</a>',
                                      "⏳ Vinted — waiting for a size"]
    db.upsert_listing(iid, "vinted", status="skipped", error="skipped: category 'Women > Accessories' isn't the item's "
                                                              "family (a outerwear, not a accessory)")
    assert "Vinted" not in crosslist.card_text(s, db, iid)             # skipped for good: left out, as before


def test_a_card_edited_after_a_sale_keeps_its_links(tmp_path, said, bot):
    """Cards were never drawn again before WO54; now an open one is — a sale must not take its links off: sold on
    Depop, taken down on Vinted, Poshmark's line gone (skipped: sold) — the links stay, the card is complete."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _live_on_depop_and_vinted(db)
    first = crosslist.announce(s, db, iid)
    assert "⏳ Poshmark — later" in first
    db.upsert_listing(iid, "depop", status="sold")
    db.upsert_listing(iid, "vinted", status="delisted")
    db.upsert_listing(iid, "poshmark", status="skipped", error="skipped: sold")
    assert crosslist.waiting_lines(db, s) == [iid]
    edited = crosslist.announce(s, db, iid)
    assert "Depop</a>" in edited and "Vinted</a>" in edited and "⏳" not in edited and len(_sends(bot)) == 1


def test_a_pause_whose_test_time_is_gone_says_now_never_a_past_time(tmp_path, said, bot):
    """At 2:00 PM, after the quiet hours, the morning's pause "next try 11:31 AM" is gone by: its test is due now."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    health.pause(db, "poshmark", f"site: {SIZE_ERROR}", health.SITE, error=SIZE_ERROR, wait=7200,
                 at=datetime(2026, 1, 1, 13, 3, 51, tzinfo=timezone.utc))
    iid = _live_on_depop_and_vinted(db)
    assert crosslist.card_text(s, db, iid).split("\n\n")[1] == "⏳ Poshmark — next try now, paused after a problem"
    db.upsert_listing(iid, "vinted", status="failed", error="Mismatch: photos 0 of 8 on the form", url=None)
    db.conn.execute("UPDATE listings SET updated_at='2026-01-01T13:00:00+00:00' WHERE item_id=? AND marketplace='vinted'",
                    (iid,))                                         # failed long ago: its retry is due now
    assert "⏳ Vinted — photos failed, next try now" in crosslist.card_text(s, db, iid)


def test_a_site_that_set_the_item_aside_says_when_it_tries_again(tmp_path, said, bot, monkeypatch):
    """WO54 rule 4 (health on main): an item that failed twice on a site is set aside there (row 'failed', "set aside:
    …") and tried again after hours — the card says so, never "failed, next try" in 30 minutes."""
    from datetime import timedelta
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _live_on_depop_and_vinted(db)
    db.upsert_listing(iid, "poshmark", status="failed", error=f"set aside: {SIZE_ERROR}")
    assert crosslist.card_text(s, db, iid).split("\n\n")[1] == "⏳ Poshmark — set aside for now; it tries again by " \
                                                                "itself later"
    until = datetime.now(timezone.utc) + timedelta(hours=3)
    monkeypatch.setattr(health, "asides", lambda db_: {f"{iid}|poshmark": {"until": until.isoformat()}}, raising=False)
    assert crosslist.card_text(s, db, iid).split("\n\n")[1] == ("⏳ Poshmark — set aside for now; it tries again by "
                                                                f"itself at {crosslist._clock(until)}")
    monkeypatch.setattr(health, "asides", lambda db_: {f"{iid}|poshmark": {"until": "2026-01-01T00:00:00+00:00"}},
                        raising=False)
    assert crosslist.card_text(s, db, iid).split("\n\n")[1] == ("⏳ Poshmark — set aside for now; it tries again by "
                                                                "itself now")      # its hold over: due now


@pytest.mark.quiet_on
def test_from_8_20_a_site_still_to_come_says_it_goes_up_after_the_quiet_hours(tmp_path, said, monkeypatch):
    from thrift_agent import quiet
    monkeypatch.setattr(quiet, "_now", lambda now=None: now or datetime(2026, 11, 10, 13, 25, tzinfo=timezone.utc))
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _live_on_depop_and_vinted(db)                              # 8:25 AM EST: nothing new goes up now
    assert crosslist.card_text(s, db, iid).split("\n\n")[1] == "⏳ Poshmark — after the quiet hours, from 2:00 PM"


def test_never_twice_the_backfill_and_the_posters_sweep_at_the_same_moment(tmp_path, said, bot):
    """Two processes (the running poster's sweep and `thrift cards --backfill`) see the same card due: the outbox row
    is taken in ONE statement before the send — one of them sends."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _live_on_depop_and_vinted(db)
    poster_db = DB(s.path("db"))                                    # the poster's own connection
    assert crosslist.cards_due(db) == [iid]
    poster_db.conn.execute("INSERT INTO outbox (chat_id, message_id, kind, ref, text, sent_at) VALUES "
                           "('100', -1, 'posted', ?, 'its card, going out', ?)", (iid, _now()))   # its placeholder
    assert crosslist._card(s, db, iid, "✅ …", edit=False) is False and _sends(bot) == []
    assert crosslist.backfill_cards(s, db, sleep=lambda x: None) == []    # nothing due: its card is out


def test_a_card_that_may_have_arrived_is_never_sent_again_even_when_a_site_lands_later(tmp_path, said, monkeypatch):
    """WO37's rule kept with the edits: a send that timed out after it went counts as sent (its message id unknown);
    a later landing can't edit it — and never sends a second card (the old edit path fell through to a new send)."""
    calls = []

    class SlowBot(CardBot):
        def send_message(self, text, buttons=None, reply_to=None, parse_mode=None):
            calls.append(("send", text))
            raise RuntimeError("telegram sendMessage: ReadTimeout: The read operation timed out")

        def edit_message(self, message_id, text, parse_mode=None):
            calls.append(("edit", message_id))
    monkeypatch.setattr(approve, "bot_for", lambda _s: SlowBot())
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _live_on_depop_and_vinted(db)
    assert crosslist.announce(s, db, iid)                           # counted as sent
    assert crosslist.cards_due(db) == [] and len(calls) == 1
    db.upsert_listing(iid, "poshmark", status="posted", url=POSH, posted_at=_now())
    crosslist.announce(s, db, iid)
    assert calls == [calls[0]]                                      # no second card, no edit of an unknown message
    assert db.conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='posted'").fetchone()[0] == 1


def test_a_card_that_never_left_goes_with_the_next_sweep(tmp_path, said, monkeypatch):
    errors = [RuntimeError("telegram sendMessage: ConnectError: [Errno 8] nodename nor servname provided")]
    sent = []

    class FlakyBot(CardBot):
        def send_message(self, text, buttons=None, reply_to=None, parse_mode=None):
            if errors:
                raise errors.pop(0)
            sent.append(text)
            return 77
    monkeypatch.setattr(approve, "bot_for", lambda _s: FlakyBot())
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    iid = _live_on_depop_and_vinted(db)
    assert crosslist.announce(s, db, iid) is None                   # never left: the placeholder taken back
    assert crosslist.waiting_lines(db, s) == [iid] and crosslist.card_row(db, iid) is None
    assert crosslist.announce(s, db, iid) and len(sent) == 1 and crosslist.card_row(db, iid)["message_id"] == 77


def test_thrift_cards_backfill_on_a_db_shaped_like_this_morning(tmp_path, said, bot, monkeypatch):
    """§0.5: "send a ✅ card with every link for every item from today, including the 19 that went up on Depop and
    Vinted with no card". The morning's shape: the 8:00 item live on all three with its card; the snowsuit live on
    Depop and Vinted, Poshmark queued behind its pause; an 8:42 item live on Depop and Vinted (Poshmark keeps no row);
    an October item announced by the old ops line (no card) — never carded now."""
    s = _settings(tmp_path)
    db = DB(s.path("db"))
    _pause_poshmark(db)
    pants = _item(db, 1)
    for mp, url in (("poshmark", POSH.replace("Moncler", "Southern-Tide")), ("depop", DEPOP), ("vinted", VINTED)):
        db.upsert_listing(pants, mp, status="posted", url=url, posted_at="2026-11-09T13:00:31+00:00")
    db.set_item(pants, status="posted")
    assert crosslist.announce(s, db, pants)                          # 8:00:44: its card went
    snowsuit = _live_on_depop_and_vinted(db, 2, "2026-11-09T13:00:46+00:00")
    db.upsert_listing(snowsuit, "poshmark", status="queued", error=f"waiting (site): {SIZE_ERROR}")
    puffer = _live_on_depop_and_vinted(db, 3, "2026-11-09T13:42:42+00:00")
    october = _item(db, 4)
    db.upsert_listing(october, "poshmark", status="posted", url=POSH + "x", posted_at="2026-10-04T15:00:00+00:00")
    db.upsert_listing(october, "depop", status="posted", url=DEPOP + "x", posted_at="2026-10-09T12:49:00+00:00")
    db.log(october, "posted_announced", {"mps": ["poshmark"], "migrated": "WO32b"})
    db.log(october, "posted_announced", {"mps": ["depop"], "to": "ops"})
    before = len(_sends(bot))
    monkeypatch.setattr(cli, "settings", lambda: s)
    r = CliRunner().invoke(cli.app, ["cards", "--backfill", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert snowsuit in r.output and puffer in r.output and r.output.index(snowsuit) < r.output.index(puffer)
    assert pants not in r.output and october not in r.output
    assert r.output.count("⏳ Poshmark — next try ") == 2 and "2 cards would send" in r.output
    assert len(_sends(bot)) == before                                # a dry run sends nothing
    r = CliRunner().invoke(cli.app, ["cards"])
    assert r.exit_code != 0                                          # --backfill is said, never implied
    pauses = []
    out = crosslist.backfill_cards(s, db, sleep=pauses.append)
    assert [iid for iid, _ in out] == [snowsuit, puffer] and pauses == [crosslist.BACKFILL_PACE]   # Telegram's pace
    new = _sends(bot)[before:]
    assert len(new) == 2 and all("⏳ Poshmark — next try " in c[2] and "Depop</a>" in c[2] and "Vinted</a>" in c[2]
                                 for c in new)
    assert crosslist.backfill_cards(s, db, sleep=pauses.append) == [] and crosslist.waiting_lines(db, s) == []
    # 2:00 PM: Poshmark back, the snowsuit goes up there — its card edited, still one message each
    health.resume(db, "poshmark", "test passed")
    db.upsert_listing(snowsuit, "poshmark", status="posted", url=POSH, posted_at=_now(), error=None)
    db.set_item(snowsuit, status="posted")
    edited = crosslist.announce(s, db, snowsuit)
    assert edited.split("\n\n")[1] == f'🟣 <a href="{POSH}">Poshmark</a>' and len(_sends(bot)) == before + 2
    for iid in crosslist.waiting_lines(db, s):                      # the housekeeping's sweep: the puffer's card too
        crosslist.announce(s, db, iid)
    assert crosslist.card_row(db, puffer)["text"].split("\n\n")[1] == "⏳ Poshmark — later"   # no longer "paused"
    assert len(_sends(bot)) == before + 2 and crosslist.waiting_lines(db, s) == []


@pytest.mark.quiet_on
def test_the_backfill_sends_nothing_in_the_quiet_hours(tmp_path, said, bot, monkeypatch):
    from thrift_agent import quiet
    monkeypatch.setattr(quiet, "_now", lambda now=None: now or datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc))
    s = _settings(tmp_path)                                         # 10:00 AM EST: quiet
    db = DB(s.path("db"))
    iid = _live_on_depop_and_vinted(db)
    monkeypatch.setattr(cli, "settings", lambda: s)
    r = CliRunner().invoke(cli.app, ["cards", "--backfill"])
    assert r.exit_code != 0 and _sends(bot) == [] and crosslist.cards_due(db) == [iid]
    r = CliRunner().invoke(cli.app, ["cards", "--backfill", "--dry-run"])
    assert r.exit_code == 0 and iid in r.output and _sends(bot) == []


def test_in_the_workers_the_card_goes_with_the_first_live_site_while_poshmark_is_paused(tmp_path, said, monkeypatch):
    """The parallel poster (live 2026-11-09's situation): Poshmark paused all along, Depop and Vinted post every item
    — each item's card goes out as it lands, Poshmark's line saying it waits; one card per item."""
    from test_parallel import Timed, fake_open
    from thrift_agent.post import parallel, runner
    monkeypatch.setattr(parallel, "IDLE", 0.05)
    monkeypatch.setattr(parallel, "HOUSEKEEPING", 0.05)
    monkeypatch.setattr(parallel, "next_gap", lambda schedule: 0.0)
    monkeypatch.setattr(parallel.crosslist, "listing_gap", lambda s_, mp: 0.0)
    monkeypatch.setattr(runner, "start_thrift_chrome", lambda s_: "not in a test")
    monkeypatch.setattr(runner, "map_fields", lambda mp, view: fields_for(mp))
    monkeypatch.setattr(runner.ItemView, "from_row", classmethod(lambda cls, it: SimpleNamespace(
        render=SimpleNamespace(title=TITLE))))
    monkeypatch.setattr(runner, "_paused", lambda db_: None)
    s = _settings(tmp_path, parallel=True)
    db = DB(s.path("db"))
    _pause_poshmark(db)                                             # its next test 4 hours away …
    db.kv_set(crosslist.WINDOW_SEEN, crosslist.window(db))          # … and no new window to test it at once
    items = [_item(db, n) for n in (1, 2)]
    ps = {"poshmark": Timed("poshmark", 0.05), "depop": Timed("depop", 0.05), "vinted": Timed("vinted", 0.05)}
    asyncio.run(parallel.run_parallel(s, db, ps, dry=False, stage="form", opener=fake_open, until_idle=True))
    assert ps["poshmark"].calls == [] and len(ps["depop"].calls) == len(ps["vinted"].calls) == 2
    for iid in items:
        text = crosslist.card_row(db, iid)["text"]
        assert text.split("\n\n")[1].startswith("⏳ Poshmark — next try ")
        assert "Depop</a>" in text and "Vinted</a>" in text
    assert db.conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='posted'").fetchone()[0] == 2   # one card each
    assert crosslist.cards_due(db) == []
