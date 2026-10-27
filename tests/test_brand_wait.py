"""The brand wait, the locked database, the NWT title (Alex, 2026-10-27).

- An item priced whose brand the gate couldn't read waits — on every site — for her reply, and ONE message asks
  ("🏷️ What brand is …?"); her reply (a name, or 'no brand') lists it. Live: the pink biker shorts went up as Depop's
  "Other" and Vinted failed on its brand.
- A Telegram line said inside a write transaction waits for the commit (live 14:03 EDT: process_item's private line
  held the database 30 s+ through notify's second connection — 'database is locked' for the poster and the worker).
- ", New with Tags" says New once (live: the Levi's 501 "… Jeans New, size 26, New with Tags" was held everywhere)."""
import httpx
import pytest
from test_wo39 import CHAT, FakeBot, _item, _reply, env  # noqa: F401, F811 (env: fixture)

from thrift_agent import approve, daily, notify, pipeline, sales, telegram, titles
from thrift_agent.db import DB, in_transaction
from thrift_agent.post import parallel, runner

SHORTS = "High-Waisted Biker Shorts Light Pink Contrast Stitching size S"
ASKED = {"decision": "needs_info", "reasons": ["brand unclear (None, 0.00)"], "notes": [], "info": [],
         "questions": ["Brand? Couldn't read it — reply 'brand …'"]}
REAL_OPS_ENABLED = notify._ops_enabled                       # before conftest switches it off


def _shorts(db, tmp_path, facts, status="ready", price=95):
    iid = _item(db, tmp_path, facts, SHORTS, status=status, size="S", name="shorts")
    db.set_item(iid, gate=ASKED, owner_price=price)
    return iid


def test_an_item_priced_with_an_unread_brand_waits_and_one_message_asks(env, tmp_path, facts, monkeypatch):  # noqa: F811
    s, db, _ = env
    bot = FakeBot()
    monkeypatch.setattr(approve, "bot_for", lambda *_: bot)
    iid = _shorts(db, tmp_path, facts)
    it = db.item(iid)
    render = {"price": 95}
    assert approve.needs_brand(it) and not runner.approved(it, render)       # nothing goes up anywhere
    monkeypatch.setattr(parallel.crosslist, "enabled", lambda s_: ["depop", "vinted"])
    assert parallel.feed(s, db) == [] and db.listings_for(iid) == []
    assert approve.ask_brands(s, db) == 1
    assert bot.texts() == [approve.BRAND_ASK.format(title=SHORTS)]
    assert approve.ask_brands(s, db) == 0                                     # once, never repeated
    assert len(daily.owner_waits(s, db)[daily.BRAND_WAIT]) == 1               # the 💤 line names it
    _reply(db, s, bot, "Zella", to=bot.next_id)
    it = db.item(iid)
    assert it["owner_brand"] == "Zella" and not approve.needs_brand(it) and runner.approved(it, render)
    assert daily.owner_waits(s, db)[daily.BRAND_WAIT] == []


def test_no_brand_lists_it_too(env, tmp_path, facts, monkeypatch):  # noqa: F811
    s, db, _ = env
    bot = FakeBot()
    monkeypatch.setattr(approve, "bot_for", lambda *_: bot)
    iid = _shorts(db, tmp_path, facts)
    approve.ask_brands(s, db)
    _reply(db, s, bot, "no brand", to=bot.next_id)
    it = db.item(iid)
    assert it["owner_brand"] == pipeline.NO_BRAND and not approve.needs_brand(it)


def test_a_brand_read_well_or_surely_absent_never_asks(env, tmp_path, facts):  # noqa: F811
    s, db, _ = env
    iid = _item(db, tmp_path, facts, "Zella High-Rise Biker Shorts Black size S", status="ready", size="S", name="z")
    db.set_item(iid, owner_price=30)
    assert not approve.needs_brand(db.item(iid))                             # the gate didn't ask
    assert approve.ask_brands(s, db) == 0


def test_the_price_says_it_waits_for_the_brand_and_asks_at_once(env, tmp_path, facts, monkeypatch):  # noqa: F811
    s, db, _ = env
    bot = FakeBot()
    monkeypatch.setattr(approve, "bot_for", lambda *_: bot)
    iid = _shorts(db, tmp_path, facts, status="awaiting_price", price=None)
    db.add_outbox(str(CHAT), 500, "item", iid, text="card")
    approve._set_price(s, db, bot, iid, 95, 501)
    assert any(m == "editMessageReplyMarkup" and approve.NEEDS_BRAND in str(p) for m, p in bot.calls)
    assert approve.BRAND_ASK.format(title=SHORTS) in bot.texts()
    assert approve.next_step(s, db, iid) == "it goes up once you reply with its brand (or 'no brand')"


def test_an_item_live_on_some_sites_gets_her_brand_for_the_rest(env, tmp_path, facts, monkeypatch):  # noqa: F811
    """The biker shorts went up on Poshmark and Depop before the rule; Vinted failed on its brand: that row waits
    (both safety nets leave it), and her 'no brand' puts it back in line — the live listings untouched."""
    s, db, _ = env
    bot = FakeBot()
    monkeypatch.setattr(approve, "bot_for", lambda *_: bot)
    iid = _shorts(db, tmp_path, facts)
    db.set_item(iid, status="posted")
    db.upsert_listing(iid, "poshmark", status="posted", url="https://poshmark.com/listing/x-" + "a" * 24)
    db.upsert_listing(iid, "depop", status="posted", url="https://www.depop.com/products/shop-shorts-8413/")
    db.upsert_listing(iid, "vinted", status="failed", error="Vinted brand: no 'List without brand' in Vinted's brand menu")
    db.set_item(iid, status="ready")                                          # what ask_brands looks at
    assert approve.ask_brands(s, db) == 1
    row = db.listing(iid, "vinted")
    assert row["status"] == "skipped" and row["error"].startswith(approve.BRAND_WAITING)
    assert any(k in row["error"] for k in sales.KEEP_REASONS)               # the Mac's net leaves it
    _reply(db, s, bot, "no brand", to=bot.next_id)
    assert db.listing(iid, "vinted")["status"] == "queued"
    assert db.listing(iid, "poshmark")["status"] == "posted" and db.item(iid)["owner_brand"] == pipeline.NO_BRAND


def test_the_apis_net_keeps_a_row_waiting_for_the_brand():
    from thrift_agent import config
    assert '"waiting for the brand"' in (config.ROOT / "api" / "thrift_api" / "watch.py").read_text(encoding="utf-8")


def test_a_row_a_net_re_queued_waits_for_the_brand_too(env, tmp_path, facts, monkeypatch):  # noqa: F811
    s, db, _ = env
    monkeypatch.setattr(approve, "bot_for", lambda *_: FakeBot())
    iid = _shorts(db, tmp_path, facts)
    db.upsert_listing(iid, "vinted", status="queued")                         # the API's repost, before her answer
    from thrift_agent import crosslist
    monkeypatch.setattr(crosslist, "enabled", lambda s_: ["vinted"])
    monkeypatch.setattr(crosslist, "live", lambda s_, mp: True)
    assert crosslist.pending(s, db, ["vinted"]) == []                         # never taken while it waits
    approve.ask_brands(s, db)
    assert db.listing(iid, "vinted")["error"].startswith(approve.BRAND_WAITING)


# ---------------------------------------------------------------- the locked database

def test_a_line_said_inside_a_transaction_goes_after_the_commit(tmp_path, monkeypatch, settings_override):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_OPS_CHAT_ID", "5550001")
    settings_override(telegram={"enabled": True})
    monkeypatch.setattr(notify, "_ops_enabled", REAL_OPS_ENABLED)
    sent = []

    class Resp:
        def raise_for_status(self):
            return None

    def wire(url, data=None, files=None, timeout=None, **kw):
        sent.append((data or {}).get("text"))
        return Resp()
    monkeypatch.setattr(telegram, "http_post", wire)
    db = DB(tmp_path / "state.db")
    with db.tx():
        assert in_transaction()
        assert notify.ops("No full photo of a skirt — a full front shot next time", once=False) is True
        assert sent == []                                                     # not while the database is held
    assert not in_transaction() and sent == ["No full photo of a skirt — a full front shot next time"]
    with pytest.raises(RuntimeError):
        with db.tx():
            notify.ops("never said: the work was undone", once=False)
            raise RuntimeError("boom")
    assert sent == ["No full photo of a skirt — a full front shot next time"]


def test_a_failed_poshmark_turn_looks_for_its_posting_row_in_the_closet():
    import inspect
    src = inspect.getsource(parallel._poshmark_worker)
    assert "reconcile_stale" in src.split("except Exception as e:")[1]       # after a failed turn, not only at start


# ---------------------------------------------------------------- the NWT title

@pytest.mark.parametrize("title,brand", [
    ("Levi's 501 Original Distressed Straight Leg Jeans New, size 26", "Levi's"),
    ("Levi's 501 Original Distressed Straight Leg Jeans New, size 26, New with Tags", "Levi's"),
    ("Levi's New 501 Original Distressed Straight Leg Jeans size 26", "Levi's"),
])
def test_new_with_tags_says_new_once(title, brand):
    out = titles.fix(title, brand, "NWT")
    assert out == "Levi's 501 Original Distressed Straight Leg Jeans size 26, New with Tags"
    assert titles.check(out) == [] and titles.fix(out, brand, "NWT") == out


def test_a_brand_that_starts_with_new_keeps_it():
    out = titles.fix("New Balance Grey Sneakers size 9", "New Balance", "NWT")
    assert out == "New Balance Grey Sneakers size 9, New with Tags" and titles.check(out) == []
    assert titles.check("Levi's 501 Jeans New, size 26, New with Tags") == ["repeated: New"]


def test_http_status_is_not_a_reason_to_hold(monkeypatch):
    """(the link's rule, unchanged) a refusal is not held: only what never left the Mac is."""
    def refused(url, **kw):
        raise httpx.HTTPStatusError("400", request=None, response=None)
    monkeypatch.setattr(telegram, "http_post", refused)
    with pytest.raises(httpx.HTTPStatusError):
        telegram.post("T", "sendMessage", data={})
